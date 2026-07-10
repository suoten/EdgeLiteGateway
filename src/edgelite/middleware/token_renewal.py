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
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_RENEWAL_THRESHOLD_SECONDS = 300  # 剩余 < 5 分钟触发续签
_NEW_TOKEN_HEADER = "X-New-Token"


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

        new_token = self._try_renew(token)
        if new_token:
            response.headers[_NEW_TOKEN_HEADER] = new_token
        return response

    @staticmethod
    def _try_renew(token: str) -> str | None:
        """尝试续签，失败返回 None（不阻断请求）。"""
        try:
            from edgelite.security.jwt import create_access_token, verify_token

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
            logger.debug(
                "Access token renewed (remaining=%ds, user=%s)",
                remaining,
                payload.get("sub", "unknown"),
            )
            return new_token
        except Exception as e:  # noqa: BLE001 - 续签失败不应阻断请求
            logger.debug("Token renewal skipped: %s", e)
            return None
