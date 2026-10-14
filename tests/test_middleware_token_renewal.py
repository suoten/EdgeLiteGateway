"""JWT 滑动过期中间件单元测试。

覆盖 src/edgelite/middleware/token_renewal.py：
_try_renew 阈值判断/续签/异常吞没、TokenRenewalMiddleware 各场景
（无头/非Bearer/4xx/SSE/接近过期/未到阈值/阈值floor），
以及 FIXED(P3) 滑动续签注册新 jti 与 _invalidate_user_cache 失效逻辑。
"""

from __future__ import annotations

import asyncio
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
    _invalidate_user_cache,
    _record_renewal,
)

# ─── _try_renew ───

# FIXED(P3): 续签出的新 token 会经 decode_token 解出 jti 并注册进 session_manager，
# 单测中以 mock 提供解码结果，并默认放行 session 注册。
_DECODED_NEW = {"jti": "new-jti", "exp": int(time.time()) + 1800}


def _renew_patches(payload: dict, new_token: str = "new-token"):
    """构造 _try_renew 常用 mock 组合：verify/create/decode + session 注册放行"""
    return (
        patch("edgelite.security.jwt.verify_token", return_value=payload),
        patch("edgelite.security.jwt.create_access_token", return_value=new_token),
        patch("edgelite.security.jwt.decode_token", return_value=dict(_DECODED_NEW, jti=_DECODED_NEW["jti"])),
        patch.object(TokenRenewalMiddleware, "_register_renewed_session", return_value=True),
    )


def test_try_renew_not_near_expiry_returns_none():
    """token 未到续签阈值（剩余 > 5 分钟）返回 None。"""
    payload = {"sub": "user1", "exp": int(time.time()) + 3600, "role": "admin"}
    with patch("edgelite.security.jwt.verify_token", return_value=payload):
        result = asyncio.run(TokenRenewalMiddleware._try_renew("valid-token"))
    assert result is None


def test_try_renew_near_expiry_returns_new_token():
    """token 接近过期（剩余 < 5 分钟）返回新 token。"""
    payload = {"sub": "user1", "exp": int(time.time()) + 60, "role": "admin"}
    patches = _renew_patches(payload, "new-token-xyz")
    with patches[0], patches[1], patches[2], patches[3]:
        result = asyncio.run(TokenRenewalMiddleware._try_renew("valid-token"))
    assert result == "new-token-xyz"


def test_try_renew_expired_token_returns_none():
    """过期 token verify 抛异常时返回 None。"""
    with patch("edgelite.security.jwt.verify_token", side_effect=Exception("expired")):
        result = asyncio.run(TokenRenewalMiddleware._try_renew("expired-token"))
    assert result is None


def test_try_renew_no_exp_returns_none():
    """payload 无 exp 字段返回 None。"""
    payload = {"sub": "user1", "role": "admin"}
    with patch("edgelite.security.jwt.verify_token", return_value=payload):
        result = asyncio.run(TokenRenewalMiddleware._try_renew("token-no-exp"))
    assert result is None


def test_try_renew_invalid_token_returns_none():
    """无效 token verify 抛异常时返回 None。"""
    with patch("edgelite.security.jwt.verify_token", side_effect=ValueError("invalid signature")):
        result = asyncio.run(TokenRenewalMiddleware._try_renew("bad-token"))
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
        patch("edgelite.security.jwt.decode_token", return_value=dict(_DECODED_NEW)),
        patch.object(TokenRenewalMiddleware, "_register_renewed_session", return_value=True),
    ):
        result = asyncio.run(TokenRenewalMiddleware._try_renew("valid-token"))

    assert result == "new-token"
    assert captured_claims["sub"] == "user1"
    assert captured_claims["role"] == "admin"
    assert "exp" not in captured_claims
    assert "iat" not in captured_claims
    assert "nbf" not in captured_claims
    assert "jti" not in captured_claims


# ─── FIXED(P3): 滑动续签注册新 jti 到 session_manager ───


def test_try_renew_registers_new_jti_with_session_manager():
    """P3 回归: 续签时必须把新 jti 注册进 session_manager（register_session 被调用）。

    原问题-续签未注册新 jti，verify_token 的 is_session_active 检查拒绝续签 token，
    滑动续签实际失效（已注册 session 的用户拿到 X-New-Token 即 401）。
    """
    payload = {"sub": "user-1", "username": "alice", "exp": int(time.time()) + 60, "role": "admin"}
    patches = _renew_patches(payload, "new-token-abc")
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("edgelite.security.session_manager.register_session") as mock_register,
    ):
        result = asyncio.run(TokenRenewalMiddleware._try_renew("valid-token"))
    assert result == "new-token-abc"
    mock_register.assert_called_once()
    args, kwargs = mock_register.call_args
    assert args[0] == "user-1"
    assert args[1] == _DECODED_NEW["jti"]
    assert kwargs.get("expires_at") == _DECODED_NEW["exp"]


def test_try_renew_returns_none_when_session_registration_fails():
    """P3 回归: session 注册失败时不得下发新 token（避免客户端拿到必 401 的 token）。"""
    payload = {"sub": "user-1", "username": "alice", "exp": int(time.time()) + 60, "role": "admin"}
    patches = _renew_patches(payload, "new-token-abc")
    with (
        patches[0],
        patches[1],
        patches[2],
        patch("edgelite.security.session_manager.register_session", side_effect=RuntimeError("database is locked")),
    ):
        result = asyncio.run(TokenRenewalMiddleware._try_renew("valid-token"))
    assert result is None


def test_try_renew_records_renewal_for_invalidation():
    """P3 回归: 续签成功后记录 username -> 续签 jti，供 _invalidate_user_cache 吊销。"""
    from edgelite.middleware import token_renewal as tr

    payload = {"sub": "user-1", "username": "bob", "exp": int(time.time()) + 60, "role": "admin"}
    patches = _renew_patches(payload, "new-token-bob")
    tr._recent_renewals.pop("bob", None)
    try:
        with patches[0], patches[1], patches[2], patches[3]:
            asyncio.run(TokenRenewalMiddleware._try_renew("valid-token"))
        record = tr._recent_renewals.get("bob")
        assert record is not None
        assert record["jti"] == _DECODED_NEW["jti"]
    finally:
        tr._recent_renewals.pop("bob", None)


# ─── FIXED(P3): _invalidate_user_cache ───


def test_invalidate_user_cache_revokes_renewed_jti():
    """P3 回归: _invalidate_user_cache 应清除续签记录并吊销续签出的 jti。"""
    from edgelite.middleware import token_renewal as tr

    _record_renewal("carol", "renewed-jti-1", 12345.0)
    with patch("edgelite.security.token_revocation.revoke_token") as mock_revoke:
        _invalidate_user_cache("carol")
    mock_revoke.assert_called_once_with("renewed-jti-1", 12345.0)
    assert "carol" not in tr._recent_renewals


def test_invalidate_user_cache_idempotent_for_unknown_user():
    """_invalidate_user_cache 对无记录用户应静默幂等返回。"""
    from edgelite.middleware import token_renewal as tr

    tr._recent_renewals.pop("nobody", None)
    with patch("edgelite.security.token_revocation.revoke_token") as mock_revoke:
        _invalidate_user_cache("nobody")
    mock_revoke.assert_not_called()


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
    patches = _renew_patches(payload, "new-token-abc")
    with patches[0], patches[1], patches[2], patches[3]:
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
