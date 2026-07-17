"""认证 API 辅助函数测试 - IP 解析/Cookie/限流辅助

覆盖 api/auth.py：
- _is_dev_mode: 开发模式判断
- _is_ip_in_cidr: CIDR 范围判断
- _is_trusted_proxy: 可信代理判断
- _get_client_ip: 客户端 IP 获取（含可信代理逻辑）
- _set_token_cookies / _clear_token_cookies: Token Cookie 管理
- _check_login_rate / _record_login_attempt: 限流辅助
- _check_account_lockout / _record_lockout_failure: 账户锁定辅助
- _WEAK_PASSWORDS: 弱密码集合
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from edgelite.api.auth import (
    _AUTH_COOKIE_ACCESS,
    _AUTH_COOKIE_REFRESH,
    _MAX_PASSWORD_LENGTH,
    _MIN_PASSWORD_LENGTH,
    _WEAK_PASSWORDS,
    _check_account_lockout,
    _check_login_rate,
    _clear_lockout,
    _clear_token_cookies,
    _get_client_ip,
    _is_dev_mode,
    _is_ip_in_cidr,
    _is_trusted_proxy,
    _MAX_LOGIN_attempts,
    _record_lockout_failure,
    _record_login_attempt,
    _set_token_cookies,
    router,
)


def _make_request(
    client_host: str = "1.2.3.4",
    headers: dict | None = None,
) -> Request:
    """构造带 client 和 headers 的伪 Request"""
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [],
        "client": (client_host, 12345),
    }
    if headers:
        scope["headers"] = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    req = Request(scope)
    return req


class TestIsDevMode:
    def test_dev_mode_true(self, monkeypatch):
        """DEV_MODE=true 应返回 True"""
        monkeypatch.setenv("DEV_MODE", "true")
        assert _is_dev_mode() is True

    def test_dev_mode_1(self, monkeypatch):
        """DEV_MODE=1 应返回 True"""
        monkeypatch.setenv("DEV_MODE", "1")
        assert _is_dev_mode() is True

    def test_dev_mode_yes(self, monkeypatch):
        """DEV_MODE=yes 应返回 True"""
        monkeypatch.setenv("DEV_MODE", "yes")
        assert _is_dev_mode() is True

    def test_dev_mode_false(self, monkeypatch):
        """DEV_MODE 为其他值应返回 False"""
        monkeypatch.setenv("DEV_MODE", "false")
        assert _is_dev_mode() is False

    def test_dev_mode_unset(self, monkeypatch):
        """未设置 DEV_MODE 应返回 False"""
        monkeypatch.delenv("DEV_MODE", raising=False)
        assert _is_dev_mode() is False


class TestIsIpInCidr:
    def test_ip_in_cidr(self):
        """IP 在 CIDR 范围内"""
        assert _is_ip_in_cidr("192.168.1.100", "192.168.1.0/24") is True

    def test_ip_not_in_cidr(self):
        """IP 不在 CIDR 范围内"""
        assert _is_ip_in_cidr("192.168.2.100", "192.168.1.0/24") is False

    def test_ipv6_in_cidr(self):
        """IPv6 CIDR"""
        assert _is_ip_in_cidr("::1", "::1/128") is True

    def test_invalid_ip(self):
        """非法 IP 应返回 False"""
        assert _is_ip_in_cidr("invalid", "192.168.1.0/24") is False

    def test_invalid_cidr(self):
        """非法 CIDR 应返回 False"""
        assert _is_ip_in_cidr("192.168.1.1", "invalid") is False

    def test_exact_match_single_ip(self):
        """单 IP CIDR"""
        assert _is_ip_in_cidr("10.0.0.1", "10.0.0.1/32") is True
        assert _is_ip_in_cidr("10.0.0.2", "10.0.0.1/32") is False


class TestIsTrustedProxy:
    def test_empty_trusted_proxies(self):
        """空可信代理列表应返回 False"""
        assert _is_trusted_proxy("1.2.3.4", []) is False

    def test_none_trusted_proxies(self):
        """None 可信代理列表应返回 False"""
        assert _is_trusted_proxy("1.2.3.4", None) is False

    def test_exact_match(self):
        """精确匹配"""
        assert _is_trusted_proxy("1.2.3.4", ["1.2.3.4"]) is True

    def test_no_match(self):
        """不匹配"""
        assert _is_trusted_proxy("1.2.3.4", ["5.6.7.8"]) is False

    def test_cidr_match(self):
        """CIDR 匹配"""
        assert _is_trusted_proxy("192.168.1.5", ["192.168.1.0/24"]) is True

    def test_cidr_no_match(self):
        """CIDR 不匹配"""
        assert _is_trusted_proxy("192.168.2.5", ["192.168.1.0/24"]) is False

    def test_whitespace_stripped(self):
        """应去除空白"""
        assert _is_trusted_proxy("1.2.3.4", ["  1.2.3.4  "]) is True

    def test_multiple_proxies(self):
        """多个代理列表"""
        assert _is_trusted_proxy("10.0.0.1", ["1.2.3.4", "10.0.0.0/8"]) is True


class TestGetClientIp:
    def test_direct_client_no_proxies(self):
        """无可信代理时应返回直接客户端 IP"""
        req = _make_request("1.2.3.4")
        with patch("edgelite.api.auth.get_config", side_effect=Exception("no config")):
            ip = _get_client_ip(req)
        assert ip == "1.2.3.4"

    def test_direct_client_not_trusted(self):
        """直接客户端不在可信代理列表时应返回直接 IP"""
        req = _make_request("1.2.3.4", {"X-Forwarded-For": "5.6.7.8"})
        mock_config = SimpleNamespace(server=SimpleNamespace(trusted_proxies=["10.0.0.0/8"]))
        with patch("edgelite.api.auth.get_config", return_value=mock_config):
            ip = _get_client_ip(req)
        assert ip == "1.2.3.4"

    def test_trusted_proxy_uses_xff(self):
        """可信代理时应使用 X-Forwarded-For"""
        req = _make_request("10.0.0.1", {"X-Forwarded-For": "5.6.7.8"})
        mock_config = SimpleNamespace(server=SimpleNamespace(trusted_proxies=["10.0.0.0/8"]))
        with patch("edgelite.api.auth.get_config", return_value=mock_config):
            ip = _get_client_ip(req)
        assert ip == "5.6.7.8"

    def test_trusted_proxy_uses_xrealip(self):
        """可信代理无 XFF 时应使用 X-Real-IP"""
        req = _make_request("10.0.0.1", {"X-Real-IP": "9.8.7.6"})
        mock_config = SimpleNamespace(server=SimpleNamespace(trusted_proxies=["10.0.0.0/8"]))
        with patch("edgelite.api.auth.get_config", return_value=mock_config):
            ip = _get_client_ip(req)
        assert ip == "9.8.7.6"

    def test_trusted_proxy_invalid_xff(self):
        """XFF 非法 IP 应回退到直接 IP"""
        req = _make_request("10.0.0.1", {"X-Forwarded-For": "invalid"})
        mock_config = SimpleNamespace(server=SimpleNamespace(trusted_proxies=["10.0.0.0/8"]))
        with patch("edgelite.api.auth.get_config", return_value=mock_config):
            ip = _get_client_ip(req)
        assert ip == "10.0.0.1"

    def test_trusted_proxy_multiple_xff(self):
        """XFF 含多个 IP 时应取第一个"""
        req = _make_request("10.0.0.1", {"X-Forwarded-For": "5.6.7.8, 9.8.7.6"})
        mock_config = SimpleNamespace(server=SimpleNamespace(trusted_proxies=["10.0.0.0/8"]))
        with patch("edgelite.api.auth.get_config", return_value=mock_config):
            ip = _get_client_ip(req)
        assert ip == "5.6.7.8"

    def test_no_client(self):
        """无 client 信息应返回 unknown"""
        scope = {"type": "http", "method": "GET", "headers": [], "client": None}
        req = Request(scope)
        with patch("edgelite.api.auth.get_config", side_effect=Exception("no config")):
            ip = _get_client_ip(req)
        assert ip == "unknown"


class TestSetTokenCookies:
    def test_set_cookies_dev_mode(self, monkeypatch):
        """开发模式应设置非 secure cookie"""
        monkeypatch.setenv("DEV_MODE", "true")
        mock_config = SimpleNamespace(
            security=SimpleNamespace(
                access_token_expire_minutes=30,
                refresh_token_expire_days=7,
            )
        )
        response = JSONResponse(content={})
        with patch("edgelite.api.auth.get_config", return_value=mock_config):
            _set_token_cookies(response, "access_tok", "refresh_tok")
        # 验证 cookie 被设置
        set_cookie_headers = [h for h in response.headers.getlist("set-cookie")]
        assert len(set_cookie_headers) == 2

    def test_set_cookies_prod_mode(self, monkeypatch):
        """生产模式应设置 secure cookie"""
        monkeypatch.delenv("DEV_MODE", raising=False)
        mock_config = SimpleNamespace(
            security=SimpleNamespace(
                access_token_expire_minutes=30,
                refresh_token_expire_days=7,
            )
        )
        response = JSONResponse(content={})
        with patch("edgelite.api.auth.get_config", return_value=mock_config):
            _set_token_cookies(response, "access_tok", "refresh_tok")
        set_cookie_headers = [h for h in response.headers.getlist("set-cookie")]
        assert len(set_cookie_headers) == 2


class TestClearTokenCookies:
    def test_clear_cookies(self):
        """应清除 token cookie"""
        response = JSONResponse(content={})
        _clear_token_cookies(response)
        set_cookie_headers = [h for h in response.headers.getlist("set-cookie")]
        # 应有 2 个删除 cookie 的 header
        assert len(set_cookie_headers) == 2
        # cookie 值应为空
        for header in set_cookie_headers:
            assert "=" in header


class TestCheckLoginRate:
    @pytest.mark.asyncio
    async def test_under_limit_no_exception(self):
        """未超限不应抛异常"""
        with patch("edgelite.api.auth.RateLimitRepo") as MockRepo:
            MockRepo.check_login_rate = AsyncMock(return_value=0)
            await _check_login_rate("1.2.3.4")  # 不应抛异常

    @pytest.mark.asyncio
    async def test_over_limit_raises_429(self):
        """超限应抛 429 异常"""
        with patch("edgelite.api.auth.RateLimitRepo") as MockRepo:
            MockRepo.check_login_rate = AsyncMock(return_value=_MAX_LOGIN_attempts)
            with pytest.raises(HTTPException) as exc_info:
                await _check_login_rate("1.2.3.4")
            assert exc_info.value.status_code == 429


class TestRecordLoginAttempt:
    @pytest.mark.asyncio
    async def test_record_called(self):
        """应调用 RateLimitRepo.record_login_attempt"""
        with patch("edgelite.api.auth.RateLimitRepo") as MockRepo:
            MockRepo.record_login_attempt = AsyncMock()
            await _record_login_attempt("1.2.3.4")
            MockRepo.record_login_attempt.assert_called_once_with("1.2.3.4")


class TestCheckAccountLockout:
    @pytest.mark.asyncio
    async def test_no_lockout(self):
        """无锁定记录不应抛异常"""
        with patch("edgelite.api.auth.RateLimitRepo") as MockRepo:
            MockRepo.get_lockout_info = AsyncMock(return_value=None)
            await _check_account_lockout("admin", "1.2.3.4")  # 不应抛异常

    @pytest.mark.asyncio
    async def test_lockout_expired(self):
        """过期锁定不应抛异常"""
        import time

        with patch("edgelite.api.auth.RateLimitRepo") as MockRepo:
            MockRepo.get_lockout_info = AsyncMock(return_value={"locked_until": time.time() - 100})
            await _check_account_lockout("admin", "1.2.3.4")  # 已过期，不应抛异常

    @pytest.mark.asyncio
    async def test_lockout_active_raises_423(self):
        """活跃锁定应抛 423 异常"""
        import time

        with patch("edgelite.api.auth.RateLimitRepo") as MockRepo:
            MockRepo.get_lockout_info = AsyncMock(return_value={"locked_until": time.time() + 3600})
            with pytest.raises(HTTPException) as exc_info:
                await _check_account_lockout("admin", "1.2.3.4")
            assert exc_info.value.status_code == 423


class TestRecordLockoutFailure:
    @pytest.mark.asyncio
    async def test_record_called(self):
        """应调用 RateLimitRepo.record_lockout_failure"""
        with patch("edgelite.api.auth.RateLimitRepo") as MockRepo:
            MockRepo.record_lockout_failure = AsyncMock()
            await _record_lockout_failure("admin", "1.2.3.4")
            MockRepo.record_lockout_failure.assert_called_once_with("admin", "1.2.3.4")


class TestClearLockout:
    @pytest.mark.asyncio
    async def test_clear_called(self):
        """应调用 RateLimitRepo.clear_lockout"""
        with patch("edgelite.api.auth.RateLimitRepo") as MockRepo:
            MockRepo.clear_lockout = AsyncMock()
            await _clear_lockout("admin", "1.2.3.4")
            MockRepo.clear_lockout.assert_called_once_with("admin", "1.2.3.4")


class TestWeakPasswords:
    def test_common_weak_passwords(self):
        """常见弱密码应在集合中"""
        assert "password" in _WEAK_PASSWORDS
        assert "123456" in _WEAK_PASSWORDS
        assert "admin" in _WEAK_PASSWORDS
        assert "admin123" in _WEAK_PASSWORDS

    def test_weak_passwords_case_sensitive(self):
        """弱密码检查应区分大小写（使用 .lower()）"""
        assert "password" in _WEAK_PASSWORDS
        assert "PASSWORD" not in _WEAK_PASSWORDS  # 集合中是小写


class TestPasswordLengthConstants:
    def test_min_password_length(self):
        """最小密码长度应为 8"""
        assert _MIN_PASSWORD_LENGTH == 8

    def test_max_password_length(self):
        """最大密码长度应大于最小长度"""
        assert _MAX_PASSWORD_LENGTH > _MIN_PASSWORD_LENGTH


class TestCookieConstants:
    def test_cookie_names(self):
        """Cookie 名称应正确定义"""
        assert _AUTH_COOKIE_ACCESS == "edgelite_access"
        assert _AUTH_COOKIE_REFRESH == "edgelite_refresh"


class TestRouterDefinition:
    def test_router_prefix(self):
        """路由前缀应为 /api/v1/auth"""
        assert router.prefix == "/api/v1/auth"

    def test_router_tags(self):
        """路由标签应包含 Auth"""
        assert "Auth" in router.tags


# ── 端点测试 ──
import os as _os
import time as _time

from edgelite.api.deps import get_audit_service, get_current_user, get_database


def _mkdb():
    """Mock db with async session context manager."""
    db = MagicMock()
    db.write_lock = MagicMock()
    s = MagicMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = s
    cm.__aexit__.return_value = None
    db.get_session = MagicMock(return_value=cm)
    return db, s


def _mkcfg(smtp_host="smtp.example.com", from_addr="from@example.com", gfrt=100):
    """Mock config for auth endpoints."""
    ec = SimpleNamespace(
        smtp_host=smtp_host, smtp_port=465, smtp_user="u", smtp_password="p", from_addr=from_addr, use_starttls=False
    )
    return SimpleNamespace(
        security=SimpleNamespace(
            access_token_expire_minutes=30, refresh_token_expire_days=7, global_failure_rate_threshold=gfrt
        ),
        server=SimpleNamespace(trusted_proxies=[]),
        database=SimpleNamespace(sqlite_path=_os.path.join(_os.path.dirname(__file__), "nonexistent.db")),
        notify=SimpleNamespace(email=ec),
    )


def _app(db=None, audit=None, user=None):
    """Build FastAPI app with overridden deps."""
    app = FastAPI()
    app.include_router(router)
    if db:
        app.dependency_overrides[get_database] = lambda: db
    if audit:
        app.dependency_overrides[get_audit_service] = lambda: audit
    if user:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


def _rl_defaults(ML, **ov):
    """Set default async return values on RateLimitRepo mock."""
    d = dict(
        check_global_failure_rate=0,
        check_global_account_lockout=None,
        check_login_rate=0,
        get_lockout_info=None,
        record_global_failure=None,
        record_global_account_failure=None,
        record_login_attempt=None,
        record_lockout_failure=None,
        clear_global_account_lockout=None,
        clear_lockout=None,
    )
    d.update(ov)
    for k, v in d.items():
        setattr(ML, k, AsyncMock(return_value=v))


def _audit():
    a = AsyncMock()
    a.log = AsyncMock()
    return a


# ── 端点测试 ──
from contextlib import contextmanager


def _mkdb():
    db = MagicMock()
    db.write_lock = MagicMock()
    s = MagicMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = s
    cm.__aexit__.return_value = None
    db.get_session = MagicMock(return_value=cm)
    return db, s


def _mkcfg(smtp_host="smtp.example.com", from_addr="from@example.com", gfrt=100):
    ec = SimpleNamespace(
        smtp_host=smtp_host, smtp_port=465, smtp_user="u", smtp_password="p", from_addr=from_addr, use_starttls=False
    )
    return SimpleNamespace(
        security=SimpleNamespace(
            access_token_expire_minutes=30, refresh_token_expire_days=7, global_failure_rate_threshold=gfrt
        ),
        server=SimpleNamespace(trusted_proxies=[]),
        database=SimpleNamespace(sqlite_path=_os.path.join(_os.path.dirname(__file__), "nx.db")),
        notify=SimpleNamespace(email=ec),
    )


def _app(db=None, audit=None, user=None):
    app = FastAPI()
    app.include_router(router)
    if db:
        app.dependency_overrides[get_database] = lambda: db
    if audit:
        app.dependency_overrides[get_audit_service] = lambda: audit
    if user:
        app.dependency_overrides[get_current_user] = lambda: user
    return app


def _rl(ML, **ov):
    d = dict(
        check_global_failure_rate=0,
        check_global_account_lockout=None,
        check_login_rate=0,
        get_lockout_info=None,
        record_global_failure=None,
        record_global_account_failure=None,
        record_login_attempt=None,
        record_lockout_failure=None,
        clear_global_account_lockout=None,
        clear_lockout=None,
    )
    d.update(ov)
    for k, v in d.items():
        setattr(ML, k, AsyncMock(return_value=v))


def _au():
    a = AsyncMock()
    a.log = AsyncMock()
    return a


_U = {"user_id": "u1", "username": "admin", "role": "admin", "password": "$2b$14$h", "enabled": True}
_MU = {"user_id": "u1", "username": "admin", "role": "admin"}


@contextmanager
def _login_ctx(db, user_data=_U, vp=True, **rl):
    with (
        patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
        patch("edgelite.api.auth.UserRepo") as MU,
        patch("edgelite.api.auth.RateLimitRepo") as ML,
        patch("edgelite.api.auth.verify_password", return_value=vp),
        patch("edgelite.api.auth.create_access_token", return_value="at"),
        patch("edgelite.api.auth.create_refresh_token", return_value="rt"),
        patch("edgelite.api.auth.os.path.exists", return_value=False),
        patch("edgelite.security.jwt.decode_token", return_value={"jti": "j1"}),
        patch("edgelite.security.session_manager.revoke_old_sessions", new=AsyncMock()),
        patch("edgelite.middleware.csrf.generate_csrf_token", return_value="ct"),
    ):
        MU.return_value.get_by_username_with_password = AsyncMock(return_value=user_data)
        _rl(ML, **rl)
        yield TestClient(_app(db=db, audit=_au()))


@contextmanager
def _refresh_ctx(db, payload=None, user_data=_U):
    p = payload or {"jti": "oj", "sub": "u1", "username": "admin", "iat": 9, "exp": 9}
    with (
        patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
        patch("edgelite.api.auth.UserRepo") as MU,
        patch("edgelite.api.auth.verify_token", return_value=p),
        patch("edgelite.api.auth.create_access_token", return_value="na"),
        patch("edgelite.api.auth.create_refresh_token", return_value="nr"),
        patch("edgelite.security.jwt.decode_token", return_value={"jti": "nj"}),
        patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
        patch("edgelite.security.session_manager.register_session"),
        patch("edgelite.security.token_revocation.revoke_token"),
        patch("edgelite.security.session_manager.remove_session"),
        patch("edgelite.middleware.csrf.generate_csrf_token", return_value="nc"),
    ):
        MU.return_value.get_by_username = AsyncMock(return_value=user_data)
        yield TestClient(_app(db=db))


@contextmanager
def _chg_ctx(db, vp=True, user_data=_U):
    with (
        patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
        patch("edgelite.api.auth.UserRepo") as MU,
        patch("edgelite.api.auth.verify_password", return_value=vp),
        patch("edgelite.security.password.hash_password", return_value="nh"),
        patch("edgelite.security.session_manager.clear_user_sessions", return_value=[]),
        patch("edgelite.security.token_revocation.revoke_token_async", new=AsyncMock()),
    ):
        MU.return_value.get_by_username_with_password = AsyncMock(return_value=user_data)
        MU.return_value.update_password_and_clear_flag = AsyncMock(return_value=True)
        yield TestClient(_app(db=db, audit=_au(), user=_MU))


class TestLogin:
    def test_success(self):
        db, _ = _mkdb()
        with _login_ctx(db) as c:
            r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "Pass123!@#"})
            assert r.status_code == 200 and r.json()["data"]["access_token"] == "at"

    def test_user_not_found(self):
        db, _ = _mkdb()
        with _login_ctx(db, user_data=None, vp=False) as c:
            r = c.post("/api/v1/auth/login", json={"username": "x", "password": "p"})
            assert r.status_code == 401

    def test_wrong_password(self):
        db, _ = _mkdb()
        with _login_ctx(db, vp=False) as c:
            r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "w"})
            assert r.status_code == 401

    def test_disabled_user(self):
        db, _ = _mkdb()
        u = dict(_U, enabled=False)
        with _login_ctx(db, user_data=u) as c:
            r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "p"})
            assert r.status_code == 401

    def test_global_rate_exceeded(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg(gfrt=5)),
            patch("edgelite.api.auth.RateLimitRepo") as ML,
        ):
            ML.check_global_failure_rate = AsyncMock(return_value=10)
            r = TestClient(_app(db=db, audit=_au())).post("/api/v1/auth/login", json={"username": "a", "password": "p"})
            assert r.status_code == 429

    def test_global_lockout(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.api.auth.RateLimitRepo") as ML,
        ):
            ML.check_global_failure_rate = AsyncMock(return_value=0)
            ML.check_global_account_lockout = AsyncMock(return_value={"locked_until": _time.time() + 3600})
            r = TestClient(_app(db=db, audit=_au())).post("/api/v1/auth/login", json={"username": "a", "password": "p"})
            assert r.status_code == 423

    def test_ip_rate_limited(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.api.auth.RateLimitRepo") as ML,
        ):
            ML.check_global_failure_rate = AsyncMock(return_value=0)
            ML.check_global_account_lockout = AsyncMock(return_value=None)
            ML.check_login_rate = AsyncMock(return_value=_MAX_LOGIN_attempts)
            r = TestClient(_app(db=db, audit=_au())).post("/api/v1/auth/login", json={"username": "a", "password": "p"})
            assert r.status_code == 429

    def test_pw_file_removal_fails(self):
        db, _ = _mkdb()
        with (
            _login_ctx(db) as c,
            patch("edgelite.api.auth.os.path.exists", return_value=True),
            patch("edgelite.api.auth.os.remove", side_effect=OSError("d")),
        ):
            r = c.post("/api/v1/auth/login", json={"username": "admin", "password": "p"})
            assert r.status_code == 500


class TestRefresh:
    def test_success(self):
        db, _ = _mkdb()
        with _refresh_ctx(db) as c:
            r = c.post("/api/v1/auth/refresh", json={"refresh": "old"})
            assert r.status_code == 200 and r.json()["data"]["access_token"] == "na"

    def test_no_token(self):
        db, _ = _mkdb()
        r = TestClient(_app(db=db)).post("/api/v1/auth/refresh", json={})
        assert r.status_code == 401

    def test_invalid_token(self):
        db, _ = _mkdb()
        from jwt import PyJWTError

        with patch("edgelite.api.auth.verify_token", side_effect=PyJWTError("b")):
            r = TestClient(_app(db=db)).post("/api/v1/auth/refresh", json={"refresh": "bad"})
            assert r.status_code == 401

    def test_revoked(self):
        db, _ = _mkdb()
        with _refresh_ctx(db) as c, patch("edgelite.security.token_revocation.is_token_revoked", return_value=True):
            r = c.post("/api/v1/auth/refresh", json={"refresh": "rev"})
            assert r.status_code == 401

    def test_user_not_found(self):
        db, _ = _mkdb()
        with _refresh_ctx(db, user_data=None) as c:
            r = c.post("/api/v1/auth/refresh", json={"refresh": "old"})
            assert r.status_code == 401

    def test_password_changed(self):
        db, _ = _mkdb()
        from datetime import datetime

        u = dict(_U, password_changed_at=datetime.now().isoformat())
        with _refresh_ctx(
            db, payload={"jti": "oj", "sub": "u1", "username": "admin", "iat": 1, "exp": 9}, user_data=u
        ) as c:
            r = c.post("/api/v1/auth/refresh", json={"refresh": "old"})
            assert r.status_code == 401

    def test_from_cookie(self):
        db, _ = _mkdb()
        with _refresh_ctx(db) as c:
            r = c.post("/api/v1/auth/refresh", json={}, cookies={_AUTH_COOKIE_REFRESH: "cr"})
            assert r.status_code == 200


class TestMe:
    def test_success(self):
        db, _ = _mkdb()
        with patch("edgelite.storage.sqlite_repo.UserRepo") as MU:
            MU.return_value.get_by_username = AsyncMock(return_value={"username": "a", "must_change_password": True})
            r = TestClient(_app(db=db, user=_MU)).get("/api/v1/auth/me")
            assert r.status_code == 200 and r.json()["data"]["must_change_password"] is True

    def test_user_not_found(self):
        db, _ = _mkdb()
        with patch("edgelite.storage.sqlite_repo.UserRepo") as MU:
            MU.return_value.get_by_username = AsyncMock(return_value=None)
            r = TestClient(_app(db=db, user=_MU)).get("/api/v1/auth/me")
            assert r.status_code == 200 and r.json()["data"]["must_change_password"] is False

    def test_db_error(self):
        db, _ = _mkdb()
        with patch("edgelite.storage.sqlite_repo.UserRepo") as MU:
            MU.return_value.get_by_username = AsyncMock(side_effect=RuntimeError("db"))
            r = TestClient(_app(db=db, user=_MU)).get("/api/v1/auth/me")
            assert r.status_code == 500


class TestChangePassword:
    def test_success(self):
        db, _ = _mkdb()
        with _chg_ctx(db) as c:
            r = c.post("/api/v1/auth/change-password", json={"old_password": "Old1@", "new_password": "New456@#"})
            assert r.status_code == 200, r.text

    def test_wrong_old(self):
        db, _ = _mkdb()
        with _chg_ctx(db, vp=False) as c:
            r = c.post("/api/v1/auth/change-password", json={"old_password": "w", "new_password": "New456@#"})
            assert r.status_code == 400

    def test_user_not_found(self):
        db, _ = _mkdb()
        with _chg_ctx(db, user_data=None) as c:
            r = c.post("/api/v1/auth/change-password", json={"old_password": "x", "new_password": "New456@#"})
            assert r.status_code == 404

    def test_too_short(self):
        db, _ = _mkdb()
        with _chg_ctx(db) as c:
            r = c.post("/api/v1/auth/change-password", json={"old_password": "Old1@", "new_password": "Ab1@"})
            assert r.status_code == 400

    def test_same_as_old(self):
        db, _ = _mkdb()
        with _chg_ctx(db) as c:
            r = c.post("/api/v1/auth/change-password", json={"old_password": "Same1@", "new_password": "Same1@"})
            assert r.status_code == 400

    def test_no_special(self):
        db, _ = _mkdb()
        with _chg_ctx(db) as c:
            r = c.post("/api/v1/auth/change-password", json={"old_password": "Old1@", "new_password": "NewPass456"})
            assert r.status_code == 400

    def test_no_letter_digit(self):
        db, _ = _mkdb()
        with _chg_ctx(db) as c:
            r = c.post("/api/v1/auth/change-password", json={"old_password": "Old1@", "new_password": "@@@@@@@@"})
            assert r.status_code == 400

    def test_too_long(self):
        db, _ = _mkdb()
        with _chg_ctx(db) as c:
            r = c.post(
                "/api/v1/auth/change-password", json={"old_password": "Old1@", "new_password": "A1@" + "a" * 126}
            )
            assert r.status_code == 400


class TestForgotPassword:
    def test_rate_limited(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
        ):
            ML.check_password_reset_ip_rate = AsyncMock(return_value=(-1, 3600))
            r = TestClient(_app(db=db, audit=_au())).post("/api/v1/auth/forgot-password", json={"username": "x"})
            assert r.status_code == 429

    def test_user_not_found(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
            patch("edgelite.api.auth.asyncio.sleep", new=AsyncMock()),
            patch("edgelite.api.auth.UserRepo") as MU,
        ):
            ML.check_password_reset_ip_rate = AsyncMock(return_value=(0, 0))
            ML.check_password_reset_user_rate = AsyncMock(return_value=(0, 0))
            ML.record_password_reset_ip_attempt = AsyncMock(return_value=1)
            ML.record_password_reset_user_attempt = AsyncMock(return_value=1)
            MU.return_value.get_by_username = AsyncMock(return_value=None)
            r = TestClient(_app(db=db, audit=_au())).post("/api/v1/auth/forgot-password", json={"username": "nouser"})
            assert r.status_code == 200

    def test_email_not_configured(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg(smtp_host="", from_addr="")),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
            patch("edgelite.api.auth.UserRepo") as MU,
            patch("edgelite.api.auth.create_access_token", return_value="rt"),
        ):
            ML.check_password_reset_ip_rate = AsyncMock(return_value=(0, 0))
            ML.check_password_reset_user_rate = AsyncMock(return_value=(0, 0))
            ML.record_password_reset_ip_attempt = AsyncMock(return_value=1)
            ML.record_password_reset_user_attempt = AsyncMock(return_value=1)
            MU.return_value.get_by_username = AsyncMock(return_value={"username": "a", "email": "a@b.com"})
            r = TestClient(_app(db=db, audit=_au())).post("/api/v1/auth/forgot-password", json={"username": "admin"})
            assert r.status_code == 200

    def test_no_frontend_url(self):
        db, _ = _mkdb()
        import os as _om

        _om.environ.pop("EDGELITE_FRONTEND_URL", None)
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
            patch("edgelite.api.auth.UserRepo") as MU,
            patch("edgelite.api.auth.create_access_token", return_value="rt"),
        ):
            ML.check_password_reset_ip_rate = AsyncMock(return_value=(0, 0))
            ML.check_password_reset_user_rate = AsyncMock(return_value=(0, 0))
            ML.record_password_reset_ip_attempt = AsyncMock(return_value=1)
            ML.record_password_reset_user_attempt = AsyncMock(return_value=1)
            MU.return_value.get_by_username = AsyncMock(return_value={"username": "a", "email": "a@b.com"})
            r = TestClient(_app(db=db, audit=_au())).post("/api/v1/auth/forgot-password", json={"username": "admin"})
            assert r.status_code == 200 and r.json()["code"] == 500

    def test_success(self):
        db, _ = _mkdb()
        import os as _om

        _om.environ["EDGELITE_FRONTEND_URL"] = "https://reset.example.com"
        try:
            with (
                patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
                patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
                patch("edgelite.api.auth.UserRepo") as MU,
                patch("edgelite.api.auth.create_access_token", return_value="rt"),
                patch("edgelite.api.auth.asyncio.to_thread", new=AsyncMock()),
            ):
                ML.check_password_reset_ip_rate = AsyncMock(return_value=(0, 0))
                ML.check_password_reset_user_rate = AsyncMock(return_value=(0, 0))
                ML.record_password_reset_ip_attempt = AsyncMock(return_value=1)
                ML.record_password_reset_user_attempt = AsyncMock(return_value=1)
                MU.return_value.get_by_username = AsyncMock(return_value={"username": "a", "email": "a@b.com"})
                r = TestClient(_app(db=db, audit=_au())).post(
                    "/api/v1/auth/forgot-password", json={"username": "admin"}
                )
                assert r.status_code == 200
        finally:
            _om.environ.pop("EDGELITE_FRONTEND_URL", None)


class TestResetPassword:
    def test_rate_limited(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
        ):
            ML.check_reset_usage_ip_rate = AsyncMock(return_value=(-1, 3600))
            r = TestClient(_app(db=db, audit=_au())).post(
                "/api/v1/auth/reset-password", json={"token": "t", "new_password": "New456@#"}
            )
            assert r.status_code == 429

    def test_invalid_token(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
            patch("edgelite.security.jwt.verify_token", side_effect=Exception("bad")),
        ):
            ML.check_reset_usage_ip_rate = AsyncMock(return_value=(0, 0))
            ML.record_reset_usage_attempt = AsyncMock()
            r = TestClient(_app(db=db, audit=_au())).post(
                "/api/v1/auth/reset-password", json={"token": "bad", "new_password": "New456@#"}
            )
            assert r.status_code == 400

    def test_already_used(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
            patch(
                "edgelite.security.jwt.verify_token",
                return_value={"type": "password_reset", "sub": "a", "jti": "j", "exp": 9},
            ),
        ):
            ML.check_reset_usage_ip_rate = AsyncMock(return_value=(0, 0))
            ML.record_reset_usage_attempt = AsyncMock()
            ML.is_password_reset_token_used = AsyncMock(return_value=True)
            r = TestClient(_app(db=db, audit=_au())).post(
                "/api/v1/auth/reset-password", json={"token": "t", "new_password": "New456@#"}
            )
            assert r.status_code == 410

    def test_success(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
            patch("edgelite.api.auth.UserRepo") as MU,
            patch(
                "edgelite.security.jwt.verify_token",
                return_value={"type": "password_reset", "sub": "a", "jti": "j", "exp": 9},
            ),
            patch("edgelite.security.password.hash_password", return_value="nh"),
            patch("edgelite.security.token_revocation.revoke_token"),
        ):
            ML.check_reset_usage_ip_rate = AsyncMock(return_value=(0, 0))
            ML.record_reset_usage_attempt = AsyncMock()
            ML.is_password_reset_token_used = AsyncMock(return_value=False)
            ML.mark_password_reset_token_used = AsyncMock(return_value=True)
            MU.return_value.get_by_username_with_password = AsyncMock(return_value=_U)
            MU.return_value.update_password = AsyncMock()
            r = TestClient(_app(db=db, audit=_au())).post(
                "/api/v1/auth/reset-password", json={"token": "t", "new_password": "New456@#"}
            )
            assert r.status_code == 200, r.text

    def test_user_not_found(self):
        db, _ = _mkdb()
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
            patch("edgelite.api.auth.UserRepo") as MU,
            patch(
                "edgelite.security.jwt.verify_token",
                return_value={"type": "password_reset", "sub": "a", "jti": "j", "exp": 9},
            ),
            patch("edgelite.security.password.hash_password", return_value="nh"),
        ):
            ML.check_reset_usage_ip_rate = AsyncMock(return_value=(0, 0))
            ML.record_reset_usage_attempt = AsyncMock()
            ML.is_password_reset_token_used = AsyncMock(return_value=False)
            ML.mark_password_reset_token_used = AsyncMock(return_value=True)
            MU.return_value.get_by_username_with_password = AsyncMock(return_value=None)
            r = TestClient(_app(db=db, audit=_au())).post(
                "/api/v1/auth/reset-password", json={"token": "t", "new_password": "New456@#"}
            )
            assert r.status_code == 404

    def test_disabled_account(self):
        db, _ = _mkdb()
        u = dict(_U, enabled=False)
        with (
            patch("edgelite.api.auth.get_config", return_value=_mkcfg()),
            patch("edgelite.storage.sqlite_repo.RateLimitRepo") as ML,
            patch("edgelite.api.auth.UserRepo") as MU,
            patch(
                "edgelite.security.jwt.verify_token",
                return_value={"type": "password_reset", "sub": "a", "jti": "j", "exp": 9},
            ),
            patch("edgelite.security.password.hash_password", return_value="nh"),
        ):
            ML.check_reset_usage_ip_rate = AsyncMock(return_value=(0, 0))
            ML.record_reset_usage_attempt = AsyncMock()
            ML.is_password_reset_token_used = AsyncMock(return_value=False)
            ML.mark_password_reset_token_used = AsyncMock(return_value=True)
            MU.return_value.get_by_username_with_password = AsyncMock(return_value=u)
            r = TestClient(_app(db=db, audit=_au())).post(
                "/api/v1/auth/reset-password", json={"token": "t", "new_password": "New456@#"}
            )
            assert r.status_code == 200


class TestLogout:
    def test_with_bearer(self):
        with (
            patch("edgelite.security.jwt.decode_token", return_value={"jti": "j", "exp": 9}),
            patch("edgelite.security.token_revocation.revoke_token"),
            patch("edgelite.security.session_manager.remove_session"),
            patch("edgelite.middleware.csrf.remove_csrf_token"),
        ):
            r = TestClient(_app(user=_MU, audit=_au())).post(
                "/api/v1/auth/logout", headers={"Authorization": "Bearer fake"}
            )
            assert r.status_code == 200, r.text

    def test_with_cookies(self):
        with (
            patch("edgelite.security.jwt.decode_token", return_value={"jti": "j", "exp": 9}),
            patch("edgelite.security.token_revocation.revoke_token"),
            patch("edgelite.security.session_manager.remove_session"),
            patch("edgelite.middleware.csrf.remove_csrf_token"),
        ):
            r = TestClient(_app(user=_MU, audit=_au())).post(
                "/api/v1/auth/logout", cookies={_AUTH_COOKIE_ACCESS: "ca", _AUTH_COOKIE_REFRESH: "cr"}
            )
            assert r.status_code == 200

    def test_no_tokens(self):
        with patch("edgelite.middleware.csrf.remove_csrf_token"):
            r = TestClient(_app(user=_MU, audit=_au())).post("/api/v1/auth/logout")
            assert r.status_code == 200
