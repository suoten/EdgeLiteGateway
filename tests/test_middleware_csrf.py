"""CSRF 中间件单元测试。

覆盖 src/edgelite/middleware/csrf.py：_sign/_verify 往返、过期/篡改/空 token 拒绝、
CSRFMiddleware 集成（safe 方法签发 cookie+header、unsafe 缺 token 403、豁免路径放行）、
F1 fail-closed、F2 secure cookie 配置驱动、F3 密钥轮换一致、F5 CSRF 独立于 JWT。
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route as _Route
from starlette.testclient import TestClient

from edgelite.middleware.csrf import (
    CSRFMiddleware,
    _get_cookie_secure,
    _get_secret,
    _sign,
    _verify,
    generate_csrf_token,
)

_SECRET_A = "a" * 40
_SECRET_B = "b" * 40


# ─── _sign / _verify 纯函数 ───


@pytest.mark.parametrize("exp_offset", [1, 60, 3600])
def test_sign_verify_roundtrip(exp_offset):
    """签发并在有效期内校验通过。"""
    exp = int(time.time()) + exp_offset
    token = _sign(_SECRET_A, exp)
    assert _verify(_SECRET_A, token, int(time.time()))


def test_verify_expired_rejected():
    """过期 token 被拒绝。"""
    exp = int(time.time()) - 10
    token = _sign(_SECRET_A, exp)
    assert not _verify(_SECRET_A, token, int(time.time()))


def test_verify_tampered_signature_rejected():
    """篡改签名后校验失败。"""
    exp = int(time.time()) + 60
    token = _sign(_SECRET_A, exp)
    payload_b64, sig_b64 = token.split(".", 1)
    # 翻转签名最后一位字符
    tampered_sig = sig_b64[:-1] + ("A" if sig_b64[-1] != "A" else "B")
    assert not _verify(_SECRET_A, f"{payload_b64}.{tampered_sig}", int(time.time()))


def test_verify_wrong_secret_rejected():
    """用不同密钥校验失败。"""
    exp = int(time.time()) + 60
    token = _sign(_SECRET_A, exp)
    assert not _verify(_SECRET_B, token, int(time.time()))


@pytest.mark.parametrize("bad_token", ["", "noDotHere", "a.", ".b", "..."])
def test_verify_malformed_token_rejected(bad_token):
    """格式错误的 token 被拒绝。"""
    assert not _verify(_SECRET_A, bad_token, int(time.time()))


def test_verify_payload_not_integer_rejected():
    """payload 不是整数被拒绝。"""
    import base64

    payload = base64.urlsafe_b64encode(b"not-a-number").decode()
    sig = base64.urlsafe_b64encode(b"x" * 32).decode()
    assert not _verify(_SECRET_A, f"{payload}.{sig}", int(time.time()))


# ─── generate_csrf_token ───


def test_generate_csrf_token_uses_config(mock_config):
    """generate_csrf_token 用配置中的 csrf_secret 签发可校验 token。"""
    token = generate_csrf_token("user1")
    assert token
    # mock_config 注入了独立 csrf_secret，应用它校验通过
    secret = _get_secret()
    assert secret is not None
    assert _verify(secret, token, int(time.time()))


def test_generate_csrf_token_fail_closed_when_no_secret(monkeypatch):
    """F1: 配置异常时 fail-closed，返回空串而非用弱密钥签发。"""
    from edgelite import config as cfg_module

    def _boom():
        raise RuntimeError("config not loaded")

    monkeypatch.setattr(cfg_module, "get_config", _boom)
    # csrf 模块通过延迟导入 edgelite.config.get_config 调用
    monkeypatch.setattr("edgelite.config.get_config", _boom)
    token = generate_csrf_token("user1")
    assert token == ""


def test_get_secret_prefers_csrf_secret(mock_config):
    """F5: _get_secret 优先返回 csrf_secret（独立于 JWT secret_key）。"""
    secret = _get_secret()
    assert secret == mock_config.security.csrf_secret
    assert secret != mock_config.security.secret_key


def test_get_secret_falls_back_to_secret_key(monkeypatch):
    """F5 向后兼容: csrf_secret 为空时回退 secret_key。"""
    monkeypatch.setenv("EDGELITE_SECURITY__SECRET_KEY", "z" * 40)
    monkeypatch.delenv("EDGELITE_SECURITY__CSRF_SECRET", raising=False)
    monkeypatch.setenv("DEV_MODE", "true")
    from edgelite.config import get_config, reset_config

    reset_config()
    try:
        secret = _get_secret()
        assert secret == "z" * 40
    finally:
        reset_config()


def test_get_secret_returns_none_when_both_empty(monkeypatch):
    """F1: 两者皆空时返回 None（fail-closed）。"""
    monkeypatch.delenv("EDGELITE_SECURITY__SECRET_KEY", raising=False)
    monkeypatch.delenv("EDGELITE_SECURITY__CSRF_SECRET", raising=False)
    monkeypatch.setenv("DEV_MODE", "true")
    from edgelite.config import get_config, reset_config

    reset_config()
    # secret_key 为空时 SecurityConfig validator 会拒绝；构造直接场景
    from edgelite.config import SecurityConfig

    with patch("edgelite.config.get_config") as mock_cfg:
        mock_cfg.return_value.security.csrf_secret = ""
        mock_cfg.return_value.security.secret_key = ""
        assert _get_secret() is None


# ─── CSRFMiddleware 集成 ───


def _make_app(secret_key: str = "") -> Starlette:
    def ping(request: Request) -> JSONResponse:  # type: ignore[misc]
        return JSONResponse({"ok": True})

    def echo(request: Request) -> JSONResponse:  # type: ignore[misc]
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            _Route("/api/v1/ping", ping, methods=["GET"]),
            _Route("/api/v1/echo", echo, methods=["POST"]),
        ]
    )
    app.add_middleware(CSRFMiddleware, secret_key=secret_key)
    return app


def test_safe_method_issues_token_cookie_and_header(mock_config):
    """safe 方法响应自动签发 X-CSRF-Token 头 + csrf_token cookie。"""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/ping")
    assert resp.status_code == 200
    assert "X-CSRF-Token" in resp.headers
    assert "set-cookie" in resp.headers
    cookie_header = resp.headers["set-cookie"]
    assert "csrf_token=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header


def test_unsafe_method_without_token_returns_403(mock_config):
    """unsafe 方法缺 X-CSRF-Token 返回 403。"""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/api/v1/echo")
    assert resp.status_code == 403
    assert resp.json()["code"] == 403


def test_unsafe_method_with_valid_token_passes(mock_config):
    """unsafe 方法携带有效 X-CSRF-Token 通过。"""
    app = _make_app()
    with TestClient(app) as client:
        # 先 GET 拿 token
        r0 = client.get("/api/v1/ping")
        token = r0.headers["X-CSRF-Token"]
        # 带 token POST
        resp = client.post("/api/v1/echo", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200


def test_unsafe_method_with_invalid_token_returns_403(mock_config):
    """unsafe 方法携带无效 token 返回 403。"""
    app = _make_app()
    with TestClient(app) as client:
        resp = client.post("/api/v1/echo", headers={"X-CSRF-Token": "garbage.token"})
    assert resp.status_code == 403


@pytest.mark.parametrize("path", ["/health", "/live", "/ready", "/docs", "/openapi.json", "/api/v1/auth/login"])
def test_exempt_paths_pass_without_token(mock_config, path):
    """豁免路径 unsafe 方法也放行（探针/文档/凭证端点）。"""
    def _h(request: Request) -> JSONResponse:  # type: ignore[misc]
        return JSONResponse({"ok": True})

    app = Starlette(routes=[_Route(path, _h, methods=["GET", "POST"])])
    app.add_middleware(CSRFMiddleware)
    with TestClient(app) as client:
        resp = client.post(path)
    assert resp.status_code == 200


def test_fail_closed_when_secret_unavailable(monkeypatch):
    """F1: 密钥不可用时 unsafe 方法 fail-closed（403），safe 方法不签发弱 token。"""

    def _boom():
        raise RuntimeError("config not loaded")

    monkeypatch.setattr("edgelite.config.get_config", _boom)
    app = _make_app()
    with TestClient(app) as client:
        # unsafe → 403（即使带任意 token）
        resp = client.post("/api/v1/echo", headers={"X-CSRF-Token": "x"})
        assert resp.status_code == 403
        # safe → 不签发 token（无弱密钥）
        r0 = client.get("/api/v1/ping")
        assert "X-CSRF-Token" not in r0.headers


def test_secure_cookie_config_driven(mock_config, monkeypatch):
    """F2: cookie_secure=True 时 Set-Cookie 含 Secure 标记。"""
    monkeypatch.setattr("edgelite.middleware.csrf._get_cookie_secure", lambda: True)
    app = _make_app()
    with TestClient(app) as client:
        resp = client.get("/api/v1/ping")
    cookie_header = resp.headers.get("set-cookie", "")
    assert "Secure" in cookie_header


def test_get_cookie_secure_reads_config(mock_config, monkeypatch):
    """F2: _get_cookie_secure 从配置读取。"""
    mock_config.security.cookie_secure = True
    assert _get_cookie_secure() is True
    mock_config.security.cookie_secure = False
    assert _get_cookie_secure() is False


def test_key_rotation_consistency(mock_config):
    """F3: 密钥轮换后签发与校验使用同一运行时密钥（无 self._secret 缓存问题）。"""
    app = _make_app()
    with TestClient(app) as client:
        # 轮换前签发
        r0 = client.get("/api/v1/ping")
        token_before = r0.headers["X-CSRF-Token"]
        secret_before = _get_secret()

        # 模拟密钥轮换：修改 csrf_secret
        mock_config.security.csrf_secret = "rotated-csrf-secret-key-32chars-long!!!"
        secret_after = _get_secret()
        assert secret_after != secret_before

        # 旧 token 用新密钥校验应失败
        assert not _verify(secret_after, token_before, int(time.time()))
        # 新 GET 签发的 token 用新密钥校验通过
        r1 = client.get("/api/v1/ping")
        token_after = r1.headers["X-CSRF-Token"]
        assert _verify(secret_after, token_after, int(time.time()))


def test_csrf_independent_from_jwt_secret(mock_config):
    """F5: CSRF 密钥独立于 JWT secret_key。"""
    csrf_secret = _get_secret()
    jwt_secret = mock_config.security.secret_key
    assert csrf_secret != jwt_secret
    # 用 JWT 密钥校验 CSRF token 应失败
    token = generate_csrf_token("u")
    assert not _verify(jwt_secret, token, int(time.time()))
    assert _verify(csrf_secret, token, int(time.time()))
