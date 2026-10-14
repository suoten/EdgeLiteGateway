"""JWT 滑动过期中间件（接近过期时自动续签）。

FIXED: 重建丢失的 token_renewal 中间件（app.py:657 引用但文件不存在，导致 create_app() 崩溃）[2026-06-30]

设计:
- 读取 Authorization: Bearer <access_token>
- 验证 token 有效性（复用 security.jwt.verify_token，含撤销列表检查）
- 若 token 在过期阈值内（默认剩余 < 5 分钟），自动签发新 access token
  并通过 X-New-Token 响应头返回，客户端可无感续签
- 无 token / token 无效: 直接放行（由下游认证依赖处理 401）
- 续签失败不影响请求（best-effort，降级为正常流程）

安全:
- 仅续签 access token，不续签 refresh token（refresh 走独立端点）
- 新 token 使用相同 subject/claims，重置 exp/iat
- 已撤销 token 不续签（verify_token 内部检查 jti）
- FIXED(P3): 续签出的新 jti 必须注册进 session_manager——verify_token 的
  is_session_active 检查要求 jti 在用户活跃 session 中，原实现未注册新 jti，
  已注册过 session 的用户拿到 X-New-Token 后下一请求即 401，滑动续签实际失效。
  修复-复用 session_manager 现有 register_session API 注册新 jti
  （旧 jti 的 session 保留至自然过期：客户端可能尚未切换到新 token，
  过期会话由下次登录的 revoke_old_sessions 统一回收）。
"""

from __future__ import annotations

import logging
import threading
import time
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_RENEWAL_THRESHOLD_SECONDS = 300  # 剩余 < 5 分钟触发续签
_NEW_TOKEN_HEADER = "X-New-Token"

# FIXED(P3): 每用户最近一次滑动续签记录（username -> {jti, exp, renewed_at}）。
# 供 _invalidate_user_cache 在用户 role/enabled 变更时清除并吊销续签出的 token
# （api/users.py 早在调用该函数，但此前本模块并不存在该实现，失效静默失败）。
_recent_renewals: dict[str, dict[str, Any]] = {}
_renewals_lock = threading.Lock()


def _record_renewal(username: str, jti: str, exp: float | None) -> None:
    """记录指定用户最近一次续签出的 jti（供失效时吊销）。"""
    if not username or not jti:
        return
    with _renewals_lock:
        _recent_renewals[username] = {"jti": jti, "exp": exp, "renewed_at": time.time()}


def _invalidate_user_cache(username: str) -> None:
    """清除指定用户的滑动续签记录，并尽力吊销续签出的新 token。

    FIXED(P3): api/users.py 在用户 role/enabled 变更时调用本函数（旧代码以
    ``# type: ignore[attr-defined]`` 引用不存在的实现，失效被掩盖）。现在：
    1. 清除 _recent_renewals 中该用户的续签记录；
    2. 将该记录中的续签 jti 加入撤销黑名单（token_revocation.revoke_token），
       使已下发但未使用的 X-New-Token 立即失效，防止旧角色 claims 继续使用。

    username 找不到记录时静默返回（幂等，best-effort，不抛异常）。
    """
    if not username:
        return
    with _renewals_lock:
        record = _recent_renewals.pop(username, None)
    if not record:
        return
    jti = record.get("jti")
    if not jti:
        return
    try:
        from edgelite.security.token_revocation import revoke_token

        revoke_token(jti, record.get("exp"))
        logger.info("Token renewal invalidated for user %s (renewed jti revoked)", username)
    except Exception as e:  # noqa: BLE001 - 失效操作 best-effort，不阻断用户管理主流程
        logger.warning("Failed to revoke renewed token for user %s: %s", username, e)


class TokenRenewalMiddleware(BaseHTTPMiddleware):
    """接近过期的 access token 自动续签中间件。"""

    def __init__(
        self,
        app: ASGIApp,
        renewal_threshold_seconds: int = _RENEWAL_THRESHOLD_SECONDS,
    ) -> None:
        super().__init__(app)
        self._threshold = max(60, int(renewal_threshold_seconds))

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        # 仅对成功响应附加续签头（避免对 4xx/5xx 响应续签）
        if response.status_code >= 400:
            return response

        # 豁免 SSE/流式响应（无法安全追加头）
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            return response

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return response

        token = auth_header[7:].strip()
        if not token:
            return response

        new_token = await self._try_renew(token)
        if new_token:
            response.headers[_NEW_TOKEN_HEADER] = new_token
        return response

    @staticmethod
    def _register_renewed_session(user_id: str, new_jti: str, expires_at: float | None) -> bool:
        """FIXED(P3): 将续签出的新 jti 注册进 session_manager（复用现有 register_session API）。

        注册成功返回 True；session_manager 异常时返回 False（调用方放弃下发新 token，
        避免客户端拿到无法通过 is_session_active 校验的 token 反复 401）。
        """
        try:
            from edgelite.security.session_manager import register_session

            register_session(user_id, new_jti, expires_at=expires_at)
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to register renewed session jti=%s: %s", new_jti, e)
            return False

    @staticmethod
    async def _try_renew(token: str) -> str | None:
        """尝试续签，失败返回 None（不阻断请求）。

        FIXED(P3): 改为 async 并在续签时注册新 jti 到 session_manager（见模块 docstring）。
        """
        try:
            from edgelite.security.jwt import create_access_token, decode_token, verify_token

            payload = verify_token(token, token_type="access")
            exp = payload.get("exp")
            if not exp:
                return None
            remaining = int(exp) - int(time.time())
            if remaining > _RENEWAL_THRESHOLD_SECONDS:
                return None  # 未到续签阈值
            # 剔除 JWT 标准字段，保留业务 claims，重新签发
            claims = {k: v for k, v in payload.items() if k not in {"exp", "iat", "nbf", "jti"}}
            new_token = create_access_token(claims)

            # FIXED(P3): 注册新 jti，保证 verify_token 的 is_session_active 检查可通过
            new_payload = None
            with warnings.catch_warnings():
                # 与 api/auth.py 一致：decode_token(verify_exp=False) 会发 UserWarning，
                # 该 token 刚签发必然未过期，此处抑制为合法用途
                warnings.simplefilter("ignore", UserWarning)
                new_payload = decode_token(new_token, verify_exp=False, token_type="access")
            if not new_payload:
                return None
            new_jti = new_payload.get("jti")
            new_exp = new_payload.get("exp")
            if not new_jti:
                return None
            if not TokenRenewalMiddleware._register_renewed_session(payload.get("sub", ""), new_jti, new_exp):
                return None  # 注册失败则不下发新 token，旧 token 在剩余有效期内仍可用

            _record_renewal(payload.get("username", ""), new_jti, new_exp)
            logger.debug(
                "Access token renewed (remaining=%ds, user=%s, new_jti registered)",
                remaining,
                payload.get("sub", "unknown"),
            )
            return new_token
        except Exception as e:  # noqa: BLE001 - 续签失败不应阻断请求
            logger.debug("Token renewal skipped: %s", e)
            return None
