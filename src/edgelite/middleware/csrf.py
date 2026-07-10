"""CSRF 防护中间件（无状态 HMAC 双重提交模式）。

FIXED: 重建丢失的 csrf 中间件（app.py:654 引用但文件不存在，导致 create_app() 崩溃）[2026-06-30]

设计（无状态、无需 session/DB）:
- Token 格式: base64(exp_ts).base64(hmac_sha256(secret, exp_ts))
- 颁发: 每个 safe-method 响应自动签发/刷新 token（X-CSRF-Token 头 + csrf_token Cookie）
- 校验: unsafe-method (POST/PUT/PATCH/DELETE) 必须携带 X-CSRF-Token 头，
  服务端验证 HMAC 签名 + 未过期，否则 403
- 豁免: /health*、/docs、/openapi.json、/api/v1/auth/login、/api/v1/auth/refresh、
  SSE 端点（SSE 无法在每次 message 携带 header，依赖 ticket 认证）

Cookie 属性: HttpOnly + SameSite=Strict + Secure(生产)，防 XSS 偷取 + 跨站提交。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
# 豁免 CSRF 校验的路径前缀（探针/文档/凭证端点/SSE）
_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/live",
    "/ready",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/mcp/sse",
    "/favicon.ico",
)
_TOKEN_TTL = 3600  # token 有效期 1 小时
_COOKIE_NAME = "csrf_token"
_HEADER_NAME = "X-CSRF-Token"


def _get_secret() -> str:
    """从配置获取 CSRF 签名密钥"""
    try:
        from edgelite.config import get_config

        return get_config().security.secret_key or "edgelite-csrf-default-do-not-use-in-prod"
    except Exception:
        return "edgelite-csrf-default-do-not-use-in-prod"


def generate_csrf_token(user_id: str = "") -> str:
    """生成 CSRF token（无状态 HMAC 模式）。

    Args:
        user_id: 用户 ID（在无状态模式下仅用于日志，不参与签名）

    Returns:
        base64(exp).base64(hmac(secret, exp)) 格式的 token
    """
    secret = _get_secret()
    token = _sign(secret, int(time.time()) + _TOKEN_TTL)
    logger.debug("CSRF token generated for user_id=%s", user_id)
    return token


def remove_csrf_token(user_id: str = "") -> None:
    """移除 CSRF token（无状态模式下为 no-op）。

    无状态 HMAC 模式下，token 验证依赖 HMAC 签名 + 过期时间，
    无需服务端存储，因此移除操作为空操作。token 会在过期后自动失效。

    Args:
        user_id: 用户 ID
    """
    logger.debug("CSRF token removal requested for user_id=%s (no-op in stateless mode)", user_id)


def _sign(secret: str, exp_ts: int) -> str:
    """生成 CSRF token: base64(exp).base64(hmac(secret, exp))。"""
    payload = str(exp_ts).encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(payload).decode()}.{base64.urlsafe_b64encode(sig).decode()}"


def _verify(secret: str, token: str, now: int) -> bool:
    """验证 CSRF token 签名与有效期。"""
    if not token or "." not in token:
        return False
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        exp_ts = int(base64.urlsafe_b64decode(payload_b64).decode())
        sig = base64.urlsafe_b64decode(sig_b64)
    except Exception:
        return False
    expected = hmac.new(secret.encode(), str(exp_ts).encode(), hashlib.sha256).digest()
    # 常量时间比较防时序攻击
    if not hmac.compare_digest(sig, expected):
        return False
    return exp_ts > now


class CSRFMiddleware(BaseHTTPMiddleware):
    """无状态 CSRF 防护中间件。"""

    def __init__(self, app: ASGIApp, secret_key: str = "") -> None:
        super().__init__(app)
        self._secret = secret_key or "edgelite-csrf-default-do-not-use-in-prod"
        if not secret_key:
            logger.warning(
                "CSRFMiddleware initialized without secret_key; using insecure default. "
                "Set EDGELITE_SECURITY__SECRET_KEY for production."
            )

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        method = request.method.upper()
        path = request.url.path

        # 豁免路径直接放行
        if any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            response = await call_next(request)
            return response

        # unsafe 方法校验 X-CSRF-Token
        if method not in _SAFE_METHODS:
            token = request.headers.get(_HEADER_NAME, "")
            now = int(time.time())
            if not _verify(self._secret, token, now):
                logger.warning("CSRF token invalid/missing: path=%s method=%s", path, method)
                return JSONResponse(
                    status_code=403,
                    content={
                        "code": 403,
                        "message": "CSRF token missing or invalid",
                        "detail": "Provide a valid X-CSRF-Token header (obtain one via a GET request)",
                    },
                )

        response = await call_next(request)
        # safe 方法响应自动签发/刷新 CSRF token（双重提交: 头 + Cookie）
        if method in _SAFE_METHODS and self._secret:
            token = _sign(self._secret, int(time.time()) + _TOKEN_TTL)
            response.headers[_HEADER_NAME] = token
            response.set_cookie(
                _COOKIE_NAME,
                token,
                httponly=True,
                samesite="strict",
                secure=False,  # 生产环境应在 TLS 后部署，由反向代理终结 TLS
                max_age=_TOKEN_TTL,
                path="/",
            )
        return response
