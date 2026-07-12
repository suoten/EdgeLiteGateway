"""JWT 滑动过期中间件单元测试。

覆盖 src/edgelite/middleware/token_renewal.py：
_try_renew 阈值判断/续签/异常吞没、TokenRenewalMiddleware 各场景
（无头/非Bearer/4xx/SSE/接近过期/未到阈值/阈值floor）。
"""

from __future__ import annotations

import time
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from edgelite.middleware.token_renewal import (
    _NEW_TOKEN_HEADER,
    _RENEWAL_THRESHOLD_SECONDS,
    TokenRenewalMiddleware,
)

# ─── _try_renew ───


def test_try_renew_not_near_expiry_returns_none():
    """token 未到续签阈值（剩余 > 5 分钟）返回 None。"""
    payload = {"sub": "user1", "exp": int(time.time()) + 3600, "role": "admin"}
    with patch("edgelite.security.jwt.verify_token", return_value=payload):
        result = TokenRenewalMiddleware._try_renew("valid-token")
    assert result is None


def test_try_renew_near_expiry_returns_new_token():
    """token 接近过期（剩余 < 5 分钟）返回新 token。"""
    payload = {"sub": "user1", "exp": int(time.time()) + 60, "role": "admin"}
    with (
        patch("edgelite.security.jwt.verify_token", return_value=payload),
        patch("edgelite.security.jwt.create_access_token", return_value="new-token-xyz"),
    ):
        result = TokenRenewalMiddleware._try_renew("valid-token")
    assert result == "new-token-xyz"


def test_try_renew_expired_token_returns_none():
    """过期 token verify 抛异常时返回 None。"""
    with patch("edgelite.security.jwt.verify_token", side_effect=Exception("expired")):
        result = TokenRenewalMiddleware._try_renew("expired-token")
    assert result is None


def test_try_renew_no_exp_returns_none():
    """payload 无 exp 字段返回 None。"""
    payload = {"sub": "user1", "role": "admin"}
    with patch("edgelite.security.jwt.verify_token", return_value=payload):
        result = TokenRenewalMiddleware._try_renew("token-no-exp")
    assert result is None


def test_try_renew_invalid_token_returns_none():
    """无效 token verify 抛异常时返回 None。"""
    with patch("edgelite.security.jwt.verify_token", side_effect=ValueError("invalid signature")):
        result = TokenRenewalMiddleware._try_renew("bad-token")
    assert result is None


def test_try_renew_preserves_business_claims():
    """续签时保留业务 claims（sub/role），剔除 JWT 标准字段（exp/iat/nbf/jti）。"""
    payload = {
        "sub": "user1",
        "role": "admin",
        "username": "admin",
        "exp": int(time.time()) + 60,
        "iat": int(time.time()) - 3600,
        "nbf": int(time.time()) - 3600,
        "jti": "old-jti",
    }
    captured_claims = {}

    def mock_create(claims):
        captured_claims.update(claims)
        return "new-token"

    with (
        patch("edgelite.security.jwt.verify_token", return_value=payload),
        patch("edgelite.security.jwt.create_access_token", side_effect=mock_create),
    ):
        result = TokenRenewalMiddleware._try_renew("valid-token")

    assert result == "new-token"
    assert captured_claims["sub"] == "user1"
    assert captured_claims["role"] == "admin"
    assert "exp" not in captured_claims
    assert "iat" not in captured_claims
    assert "nbf" not in captured_claims
    assert "jti" not in captured_claims


# ─── TokenRenewalMiddleware ───


def _make_renewal_app(
    response_status: int = 200,
    response: JSONResponse | StreamingResponse | None = None,
) -> Starlette:
    if response is None:
        response = JSONResponse({"ok": True}, status_code=response_status)

    def handler(request: Request):  # type: ignore[misc]
        return response

    app = Starlette(routes=[Route("/api/data", handler, methods=["GET"])])
    app.add_middleware(TokenRenewalMiddleware)
    return app


def test_middleware_no_authorization_header_no_renewal():
    """无 Authorization 头时不续签。"""
    app = _make_renewal_app()
    with TestClient(app) as client:
        resp = client.get("/api/data")
    assert resp.status_code == 200
    assert _NEW_TOKEN_HEADER not in resp.headers


def test_middleware_non_bearer_no_renewal():
    """非 Bearer 前缀的 Authorization 头不续签。"""
    app = _make_renewal_app()
    with TestClient(app) as client:
        resp = client.get("/api/data", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 200
    assert _NEW_TOKEN_HEADER not in resp.headers


def test_middleware_4xx_response_no_renewal():
    """4xx 响应不续签。"""
    app = _make_renewal_app(response_status=403)
    payload = {"sub": "user1", "exp": int(time.time()) + 60, "role": "admin"}
    with (
        patch("edgelite.security.jwt.verify_token", return_value=payload),
        patch("edgelite.security.jwt.create_access_token", return_value="new-token"),
    ):
        with TestClient(app) as client:
            resp = client.get("/api/data", headers={"Authorization": "Bearer old-token"})
    assert resp.status_code == 403
    assert _NEW_TOKEN_HEADER not in resp.headers


def test_middleware_5xx_response_no_renewal():
    """5xx 响应不续签。"""
    app = _make_renewal_app(response_status=500)
    payload = {"sub": "user1", "exp": int(time.time()) + 60, "role": "admin"}
    with (
        patch("edgelite.security.jwt.verify_token", return_value=payload),
        patch("edgelite.security.jwt.create_access_token", return_value="new-token"),
    ):
        with TestClient(app) as client:
            resp = client.get("/api/data", headers={"Authorization": "Bearer old-token"})
    assert resp.status_code == 500
    assert _NEW_TOKEN_HEADER not in resp.headers


def test_middleware_sse_response_no_renewal():
    """SSE 流式响应不续签（无法安全追加头）。"""
    def sse_stream():
        yield b"data: hello\n\n"

    sse_response = StreamingResponse(sse_stream(), media_type="text/event-stream")
    app = _make_renewal_app(response=sse_response)
    payload = {"sub": "user1", "exp": int(time.time()) + 60, "role": "admin"}
    with (
        patch("edgelite.security.jwt.verify_token", return_value=payload),
        patch("edgelite.security.jwt.create_access_token", return_value="new-token"),
    ):
        with TestClient(app) as client:
            resp = client.get("/api/data", headers={"Authorization": "Bearer old-token"})
    assert _NEW_TOKEN_HEADER not in resp.headers


def test_middleware_near_expiry_attaches_new_token():
    """成功响应 + 接近过期 → 附加 X-New-Token 头。"""
    app = _make_renewal_app(response_status=200)
    payload = {"sub": "user1", "exp": int(time.time()) + 60, "role": "admin"}
    with (
        patch("edgelite.security.jwt.verify_token", return_value=payload),
        patch("edgelite.security.jwt.create_access_token", return_value="new-token-abc"),
    ):
        with TestClient(app) as client:
            resp = client.get("/api/data", headers={"Authorization": "Bearer old-token"})
    assert resp.status_code == 200
    assert resp.headers.get(_NEW_TOKEN_HEADER) == "new-token-abc"


def test_middleware_not_near_expiry_no_renewal_header():
    """成功响应 + 未到阈值 → 无 X-New-Token 头。"""
    app = _make_renewal_app(response_status=200)
    payload = {"sub": "user1", "exp": int(time.time()) + 3600, "role": "admin"}
    with (
        patch("edgelite.security.jwt.verify_token", return_value=payload),
        patch("edgelite.security.jwt.create_access_token", return_value="new-token"),
    ):
        with TestClient(app) as client:
            resp = client.get("/api/data", headers={"Authorization": "Bearer old-token"})
    assert resp.status_code == 200
    assert _NEW_TOKEN_HEADER not in resp.headers


def test_middleware_empty_bearer_token_no_renewal():
    """Bearer 后空 token 不续签。"""
    app = _make_renewal_app(response_status=200)
    with TestClient(app) as client:
        resp = client.get("/api/data", headers={"Authorization": "Bearer "})
    assert resp.status_code == 200
    assert _NEW_TOKEN_HEADER not in resp.headers


def test_middleware_renewal_failure_does_not_block_request():
    """续签失败不影响请求（best-effort）。"""
    app = _make_renewal_app(response_status=200)
    with patch("edgelite.security.jwt.verify_token", side_effect=Exception("verify failed")):
        with TestClient(app) as client:
            resp = client.get("/api/data", headers={"Authorization": "Bearer bad-token"})
    assert resp.status_code == 200
    assert _NEW_TOKEN_HEADER not in resp.headers


# ─── 阈值 floor ───


def test_threshold_floor_to_60():
    """renewal_threshold_seconds < 60 时向下取整为 60。"""
    mw = TokenRenewalMiddleware(app=None, renewal_threshold_seconds=10)  # type: ignore[arg-type]
    assert mw._threshold == 60


def test_threshold_floor_negative():
    """负数阈值向下取整为 60。"""
    mw = TokenRenewalMiddleware(app=None, renewal_threshold_seconds=-1)  # type: ignore[arg-type]
    assert mw._threshold == 60


def test_threshold_exact_60():
    """恰好 60 秒保持不变。"""
    mw = TokenRenewalMiddleware(app=None, renewal_threshold_seconds=60)  # type: ignore[arg-type]
    assert mw._threshold == 60


def test_threshold_large_value():
    """大值保持不变。"""
    mw = TokenRenewalMiddleware(app=None, renewal_threshold_seconds=600)  # type: ignore[arg-type]
    assert mw._threshold == 600


def test_default_threshold_is_300():
    """默认阈值为 300 秒（5 分钟）。"""
    assert _RENEWAL_THRESHOLD_SECONDS == 300
