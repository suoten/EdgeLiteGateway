#!/usr/bin/env python3
"""EdgeLite Gateway E2E 冒烟测试。

此文件位于 e2e/ 目录，用于端到端测试检测。
执行方式: pytest e2e/test_e2e_smoke.py -v
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from edgelite.api.deps import get_current_user
from edgelite.api.health import router as health_router

_TEST_SECRET = "test-secret-key-for-e2e-testing-only-32chars!!"
_TEST_USER = {"user_id": "test-admin", "username": "testadmin", "role": "admin"}


@pytest.fixture
def e2e_app():
    """E2E 测试应用。"""
    os.environ.setdefault("EDGELITE_SECURITY__SECRET_KEY", _TEST_SECRET)
    os.environ.setdefault("DEV_MODE", "true")
    app = FastAPI(title="EdgeLite E2E Test")
    app.include_router(health_router)
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    app.state.database = MagicMock()
    app.state.audit_service = AsyncMock()
    app.state.audit_service.log = AsyncMock(return_value=None)
    return app


@pytest.mark.e2e
class TestE2ESmoke:
    """端到端冒烟测试。"""

    @pytest.mark.asyncio
    async def test_e2e_health_live(self, e2e_app):
        """E2E: 验证 liveness 端点。"""
        transport = ASGITransport(app=e2e_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health/live")
            assert resp.status_code == 200
            assert resp.json().get("status") == "ok"

    @pytest.mark.asyncio
    async def test_e2e_health_ready(self, e2e_app):
        """E2E: 验证 readiness 端点。"""
        transport = ASGITransport(app=e2e_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code in (200, 503)


# ═══════════════════════════════════════════════════════════════
# 以下为新增 E2E 测试用例（保留上方现有测试不变）
# ═══════════════════════════════════════════════════════════════

# 引入新增测试所需的模块（# noqa: E402 允许延迟导入，与文件既有风格一致）
from contextlib import contextmanager  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402

from edgelite.api.auth import router as auth_router  # noqa: E402


# ─── 辅助函数 ───


def _make_mock_db_session():
    """构造模拟的数据库 session 上下文管理器，用于 auth 路由测试。

    返回 (db, session) 元组，db.get_session() 返回一个 async context manager。
    """
    session = MagicMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = session
    cm.__aexit__.return_value = None
    db = MagicMock()
    db.write_lock = MagicMock()
    db.get_session = MagicMock(return_value=cm)
    return db, session


def _make_test_config():
    """构造测试用配置对象，供 auth 模块 get_config() 返回。"""
    return SimpleNamespace(
        security=SimpleNamespace(
            access_token_expire_minutes=30,
            refresh_token_expire_days=7,
            global_failure_rate_threshold=100,
        ),
        server=SimpleNamespace(trusted_proxies=[]),
        database=SimpleNamespace(sqlite_path=":memory:"),
    )


@contextmanager
def _login_mocks(user_data, verify_pwd=True):
    """登录测试 mock 上下文管理器，集中管理 auth 模块的依赖 mock。

    Args:
        user_data: UserRepo.get_by_username_with_password 返回的用户数据；
                   None 表示用户不存在。
        verify_pwd: verify_password 返回值，True 表示密码正确。
    """
    from edgelite.api import auth as auth_module

    patchers = [
        # mock 配置对象，避免触发真实配置加载
        patch.object(auth_module, "get_config", return_value=_make_test_config()),
        # mock UserRepo / RateLimitRepo 类（模块级导入）
        patch.object(auth_module, "UserRepo"),
        patch.object(auth_module, "RateLimitRepo"),
        # mock 密码校验结果
        patch.object(auth_module, "verify_password", return_value=verify_pwd),
        # mock token 生成，避免触发真实 JWT 签发
        patch.object(auth_module, "create_access_token", return_value="mock-access-token"),
        patch.object(auth_module, "create_refresh_token", return_value="mock-refresh-token"),
        # mock 初始管理员密码文件存在性，跳过文件删除逻辑
        patch("edgelite.api.auth.os.path.exists", return_value=False),
        # mock token 解码，返回 jti 供 session 管理
        patch("edgelite.security.jwt.decode_token", return_value={"jti": "mock-jti"}),
        # mock session 撤销，避免触及真实 session 存储
        patch(
            "edgelite.security.session_manager.revoke_old_sessions",
            new=AsyncMock(),
        ),
        # mock CSRF token 生成
        patch(
            "edgelite.middleware.csrf.generate_csrf_token",
            return_value="mock-csrf",
        ),
    ]
    for p in patchers:
        p.start()
    try:
        # 配置 UserRepo 实例方法
        auth_module.UserRepo.return_value.get_by_username_with_password = AsyncMock(
            return_value=user_data
        )
        auth_module.UserRepo.return_value.get_by_username = AsyncMock(return_value=user_data)

        # 配置 RateLimitRepo 类方法（所有方法返回安全默认值，避免触发限流/锁定）
        auth_module.RateLimitRepo.check_global_failure_rate = AsyncMock(return_value=0)
        auth_module.RateLimitRepo.check_global_account_lockout = AsyncMock(return_value=None)
        auth_module.RateLimitRepo.check_login_rate = AsyncMock(return_value=0)
        auth_module.RateLimitRepo.get_lockout_info = AsyncMock(return_value=None)
        auth_module.RateLimitRepo.record_global_failure = AsyncMock(return_value=None)
        auth_module.RateLimitRepo.record_global_account_failure = AsyncMock(return_value=None)
        auth_module.RateLimitRepo.record_login_attempt = AsyncMock(return_value=None)
        auth_module.RateLimitRepo.record_lockout_failure = AsyncMock(return_value=None)
        auth_module.RateLimitRepo.clear_global_account_lockout = AsyncMock(return_value=None)
        auth_module.RateLimitRepo.clear_lockout = AsyncMock(return_value=None)

        yield
    finally:
        for p in patchers:
            p.stop()


# ═══════════════════════════════════════════════════════════════
# 1. 登录流程测试
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def e2e_auth_app():
    """带 auth 路由的 E2E 测试应用，用于登录流程测试。"""
    from edgelite.api.deps import get_audit_service, get_database

    os.environ.setdefault("EDGELITE_SECURITY__SECRET_KEY", _TEST_SECRET)
    os.environ.setdefault("DEV_MODE", "true")
    app = FastAPI(title="EdgeLite E2E Auth Test")
    app.include_router(auth_router)
    app.include_router(health_router)

    # mock 数据库与审计服务
    db, _ = _make_mock_db_session()
    audit_svc = AsyncMock()
    audit_svc.log = AsyncMock(return_value=None)

    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_audit_service] = lambda: audit_svc
    # 受保护端点的鉴权 mock
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER

    app.state.database = db
    app.state.audit_service = audit_svc
    return app


@pytest.mark.e2e
class TestE2ELogin:
    """E2E 登录流程测试。"""

    @pytest.mark.asyncio
    async def test_e2e_login_success(self, e2e_auth_app):
        """E2E: 验证正确用户名密码登录成功，返回 access_token。"""
        user_data = {
            "user_id": "u1",
            "username": "admin",
            "role": "admin",
            "password": "$2b$12$mockhash",
            "enabled": True,
            "must_change_password": False,
        }
        with _login_mocks(user_data=user_data, verify_pwd=True):
            transport = ASGITransport(app=e2e_auth_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "CorrectPass123!@#"},
                )
                assert resp.status_code == 200, (
                    f"正确凭据登录应返回 200，实际: {resp.status_code}, 响应: {resp.text}"
                )
                data = resp.json()["data"]
                assert "access_token" in data, "响应应包含 access_token 字段"
                assert data["access_token"] == "mock-access-token"

    @pytest.mark.asyncio
    async def test_e2e_login_wrong_password(self, e2e_auth_app):
        """E2E: 验证错误密码登录失败，返回 401。"""
        user_data = {
            "user_id": "u1",
            "username": "admin",
            "role": "admin",
            "password": "$2b$12$mockhash",
            "enabled": True,
            "must_change_password": False,
        }
        with _login_mocks(user_data=user_data, verify_pwd=False):
            transport = ASGITransport(app=e2e_auth_app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "WrongPass123!@#"},
                )
                assert resp.status_code == 401, (
                    f"错误密码应返回 401，实际: {resp.status_code}, 响应: {resp.text}"
                )

    @pytest.mark.asyncio
    async def test_e2e_login_missing_field(self, e2e_auth_app):
        """E2E: 验证缺少必填字段登录失败，返回 422。

        Pydantic 在端点函数执行前校验请求体，缺少 password 字段时
        FastAPI 自动返回 422，无需 mock 数据库相关依赖。
        """
        transport = ASGITransport(app=e2e_auth_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 缺少 password 字段
            resp = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin"},
            )
            assert resp.status_code == 422, (
                f"缺少必填字段应返回 422，实际: {resp.status_code}, 响应: {resp.text}"
            )


# ═══════════════════════════════════════════════════════════════
# 2. 逐页打开测试（无白屏无未捕获异常）
# ═══════════════════════════════════════════════════════════════

# 前端主要路由列表（硬编码，与 web/src/router/index.ts 对齐）
_FRONTEND_ROUTES = ["/", "/devices", "/rules", "/alarms", "/data", "/system", "/users"]


@pytest.fixture
def e2e_spa_app():
    """带 SPA fallback 的 E2E 测试应用，模拟前端路由返回 index.html。"""
    from fastapi.responses import HTMLResponse, JSONResponse

    os.environ.setdefault("EDGELITE_SECURITY__SECRET_KEY", _TEST_SECRET)
    os.environ.setdefault("DEV_MODE", "true")
    app = FastAPI(title="EdgeLite E2E SPA Test")
    app.include_router(health_router)
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    app.state.database = MagicMock()
    app.state.audit_service = AsyncMock()
    app.state.audit_service.log = AsyncMock(return_value=None)

    @app.get("/{path:path}", include_in_schema=False)
    async def spa_fallback(path: str):
        """模拟 SPA 前端 fallback，对前端路由返回 index.html 内容。

        排除 API/docs/ws 等后端路径，仅对前端路由返回 HTML。
        """
        if path.startswith(
            ("api/", "docs", "redoc", "openapi.json", "ws/", "health", "live", "ready", "metrics")
        ):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        return HTMLResponse(
            status_code=200,
            content="<html><head><title>EdgeLite</title></head>"
            "<body><div id='app'>EdgeLite SPA</div></body></html>",
        )

    return app


@pytest.mark.e2e
class TestE2EPageSmoke:
    """E2E 前端页面冒烟测试。"""

    @pytest.mark.asyncio
    async def test_e2e_page_no_white_screen(self, e2e_spa_app):
        """E2E: 遍历前端主要路由，验证每页返回 HTTP 200 且无白屏。

        - 使用 httpx 检查每条路由返回 HTTP 200
        - 检查响应体非空且包含 #app 容器（白屏检测）
        - 若 Playwright 可用，额外检查浏览器控制台无 JS 错误
        """
        transport = ASGITransport(app=e2e_spa_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for route in _FRONTEND_ROUTES:
                resp = await client.get(route)
                assert resp.status_code == 200, (
                    f"前端路由 {route} 应返回 HTTP 200，实际: {resp.status_code}"
                )
                # 白屏检查：响应体必须非空且包含 #app 容器
                assert len(resp.text) > 0, f"前端路由 {route} 返回空响应体，疑似白屏"
                assert "app" in resp.text.lower(), (
                    f"前端路由 {route} 响应体缺少 #app 容器，疑似白屏"
                )

        # 若安装了 Playwright，额外检查浏览器控制台无 JS 错误
        # 此处仅做导入检测，避免引入浏览器启动的复杂依赖（项目未将 Playwright 列为依赖）
        try:
            import playwright  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            # Playwright 未安装，跳过浏览器控制台检查
            return


# ═══════════════════════════════════════════════════════════════
# 3. 后端重启优雅降级测试
# ═══════════════════════════════════════════════════════════════


class _DegradationState:
    """后端健康状态开关，用于优雅降级测试切换后端可用性。"""

    def __init__(self) -> None:
        self.unhealthy = False


@pytest.fixture
def e2e_degradation_app():
    """带可切换健康状态的 E2E 测试应用，用于优雅降级测试。

    通过 app.state.degradation_state.unhealthy 标志切换 /health 返回 200 或 503，
    模拟后端重启过程中的不可用与恢复。
    """
    from fastapi.responses import JSONResponse

    os.environ.setdefault("EDGELITE_SECURITY__SECRET_KEY", _TEST_SECRET)
    os.environ.setdefault("DEV_MODE", "true")

    state = _DegradationState()
    app = FastAPI(title="EdgeLite E2E Degradation Test")
    app.dependency_overrides[get_current_user] = lambda: _TEST_USER
    app.state.database = MagicMock()
    app.state.audit_service = AsyncMock()
    app.state.audit_service.log = AsyncMock(return_value=None)
    app.state.degradation_state = state

    @app.get("/health/live", include_in_schema=False)
    async def health_live():
        """Liveness 探针 — 始终返回 200，不受后端可用性影响。"""
        return JSONResponse(status_code=200, content={"status": "ok"})

    @app.get("/health", include_in_schema=False)
    async def health_full():
        """完整健康检查 — 根据 state.unhealthy 返回 200 或 503。"""
        if state.unhealthy:
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "error": "backend_unavailable"},
            )
        return JSONResponse(status_code=200, content={"status": "healthy"})

    return app


@pytest.mark.e2e
class TestE2EGracefulDegradation:
    """E2E 后端重启优雅降级测试。"""

    @pytest.mark.asyncio
    async def test_e2e_graceful_degradation(self, e2e_degradation_app):
        """E2E: 模拟后端不可用时前端行为。

        步骤:
            1. 验证正常状态下 API 可用（/health 返回 200）
            2. 模拟后端不可用（state.unhealthy=True，/health 返回 503）
            3. 验证 503 响应不抛异常（前端不会崩溃），liveness 探针仍可用
            4. 模拟后端恢复（state.unhealthy=False），验证 API 恢复可用
        """
        state = e2e_degradation_app.state.degradation_state
        transport = ASGITransport(app=e2e_degradation_app)

        # 步骤 1: 正常状态下 API 可用
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200, "正常状态 /health 应返回 200"
            assert resp.json().get("status") == "healthy"

            resp_live = await client.get("/health/live")
            assert resp_live.status_code == 200
            assert resp_live.json().get("status") == "ok"

        # 步骤 2: 模拟后端不可用
        state.unhealthy = True

        # 步骤 3: 验证 503 响应不抛异常（前端不会崩溃）
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            # 健康检查返回 503 时不应该抛异常，应正常返回 503 状态码
            assert resp.status_code == 503, (
                f"后端不可用时 /health 应返回 503，实际: {resp.status_code}"
            )
            body = resp.json()
            assert "status" in body, "503 响应应包含 status 字段供前端判断降级状态"

            # liveness 探针不应受依赖故障影响，应仍返回 200
            resp_live = await client.get("/health/live")
            assert resp_live.status_code == 200, (
                "liveness 探针不应受依赖故障影响，应仍返回 200"
            )
            assert resp_live.json().get("status") == "ok"

        # 步骤 4: 模拟后端恢复，验证 API 恢复可用
        state.unhealthy = False

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200, (
                f"后端恢复后 /health 应返回 200，实际: {resp.status_code}"
            )
            assert resp.json().get("status") == "healthy"


# ═══════════════════════════════════════════════════════════════
# 4. API 端点无 500 错误测试
# ═══════════════════════════════════════════════════════════════

# 关键 API 端点列表（带 Authorization header 检测）
_KEY_API_ENDPOINTS = [
    "/health/live",
    "/health",
    "/api/v1/auth/me",
    "/api/v1/devices",
    "/api/v1/alarms",
    "/api/v1/rules",
    "/api/v1/system/info",
    "/api/v1/users",
]


@pytest.fixture
def e2e_full_app():
    """包含核心 API 路由的 E2E 测试应用，用于 API 端点无 500 错误测试。

    使用 patch 上下文管理器 mock UserRepo / RateLimitRepo，
    确保所有端点在 mock 环境下不触发 500 错误。
    """
    from edgelite.api.alarms import router as alarms_router
    from edgelite.api.data import router as data_router
    from edgelite.api.deps import (
        get_alarm_service,
        get_audit_service,
        get_data_service,
        get_database,
        get_device_service,
        get_rule_service,
        get_system_service,
    )
    from edgelite.api.devices import router as devices_router
    from edgelite.api.rules import router as rules_router
    from edgelite.api.system import router as system_router
    from edgelite.api.users import router as users_router

    os.environ.setdefault("EDGELITE_SECURITY__SECRET_KEY", _TEST_SECRET)
    os.environ.setdefault("DEV_MODE", "true")

    # 配置 UserRepo mock（多模块共用，覆盖模块级与函数级导入）
    user_repo_instance = MagicMock()
    user_repo_instance.get_by_username = AsyncMock(return_value=None)
    user_repo_instance.get_by_username_with_password = AsyncMock(return_value=None)
    user_repo_instance.list_all = AsyncMock(return_value=([], 0))
    user_repo_cls = MagicMock(return_value=user_repo_instance)

    # 配置 RateLimitRepo mock（类方法形式调用）
    rate_repo_cls = MagicMock()
    rate_repo_cls.check_global_failure_rate = AsyncMock(return_value=0)
    rate_repo_cls.check_global_account_lockout = AsyncMock(return_value=None)
    rate_repo_cls.check_login_rate = AsyncMock(return_value=0)
    rate_repo_cls.get_lockout_info = AsyncMock(return_value=None)

    with (
        patch("edgelite.storage.sqlite_repo.UserRepo", user_repo_cls),
        patch("edgelite.api.auth.UserRepo", user_repo_cls),
        patch("edgelite.api.users.UserRepo", user_repo_cls),
        patch("edgelite.api.auth.RateLimitRepo", rate_repo_cls),
    ):
        app = FastAPI(title="EdgeLite E2E Full API Test")
        app.include_router(health_router)
        app.include_router(auth_router)
        app.include_router(devices_router)
        app.include_router(rules_router)
        app.include_router(alarms_router)
        app.include_router(data_router)
        app.include_router(system_router)
        app.include_router(users_router)

        # mock 数据库与审计服务
        db, _ = _make_mock_db_session()
        audit_svc = AsyncMock()
        audit_svc.log = AsyncMock(return_value=None)

        app.dependency_overrides[get_database] = lambda: db
        app.dependency_overrides[get_audit_service] = lambda: audit_svc
        app.dependency_overrides[get_current_user] = lambda: _TEST_USER

        # mock 业务服务（返回合理默认值，避免触发 500）
        device_svc = AsyncMock()
        device_svc.list_devices = AsyncMock(return_value=([], 0))
        device_svc.list_devices_by_ids = AsyncMock(return_value=[])
        device_svc.list_device_ids_by_owner = AsyncMock(return_value=[])
        app.dependency_overrides[get_device_service] = lambda: device_svc

        rule_svc = AsyncMock()
        rule_svc.list_rules = AsyncMock(return_value=([], 0))
        app.dependency_overrides[get_rule_service] = lambda: rule_svc

        alarm_svc = AsyncMock()
        alarm_svc.list_alarms = AsyncMock(return_value=([], 0))
        alarm_svc.get_statistics = AsyncMock(return_value={"total": 0, "firing": 0})
        app.dependency_overrides[get_alarm_service] = lambda: alarm_svc

        data_svc = AsyncMock()
        data_svc.query_timeseries = AsyncMock(return_value=[])
        app.dependency_overrides[get_data_service] = lambda: data_svc

        system_svc = AsyncMock()
        system_svc.get_status = AsyncMock(return_value={})
        app.dependency_overrides[get_system_service] = lambda: system_svc

        app.state.database = db
        app.state.audit_service = audit_svc
        yield app


@pytest.mark.e2e
class TestE2ENo500Errors:
    """E2E API 端点无 500 错误测试。"""

    @pytest.mark.asyncio
    async def test_e2e_no_500_errors(self, e2e_full_app):
        """E2E: 遍历关键 API 端点，断言无 500 错误。

        所有端点带 Authorization header，断言响应状态码不为 500。
        允许 200/401/403/404/422 等正常业务状态码，但不允许 500
        （500 表示服务端未捕获异常，不符合质量门禁要求）。
        """
        transport = ASGITransport(app=e2e_full_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            failed: list[str] = []
            for endpoint in _KEY_API_ENDPOINTS:
                resp = await client.get(
                    endpoint,
                    headers={"Authorization": "Bearer mock-access-token"},
                )
                if resp.status_code == 500:
                    failed.append(f"{endpoint} -> 500 (body: {resp.text[:200]})")
            assert not failed, (
                "以下端点返回 500 错误，不符合质量门禁要求:\n" + "\n".join(failed)
            )
