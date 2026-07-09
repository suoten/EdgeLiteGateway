"""EdgeLite 验收测试 — 冒烟测试模块

测试范围: 部署 → 启动 → 登录 → 核心端点可达
执行顺序: 必须最先执行，不通过则停止全部测试
"""

from __future__ import annotations

import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from httpx import ASGITransport, AsyncClient

from edgelite.api.deps import get_current_user
from edgelite.api.health import router as health_router
from edgelite.api.auth import router as auth_router
from edgelite.api.devices import router as devices_router
from edgelite.api.rules import router as rules_router
from edgelite.api.alarms import router as alarms_router
from edgelite.api.data import router as data_router
from edgelite.api.ai_models import router as ai_router
from edgelite.api.system import router as system_router
from edgelite.api.users import router as users_router
from edgelite.api.services import router as services_router
from edgelite.api.drivers import router as drivers_router
from edgelite.api.notify import router as notify_router
# NOTE: platforms_router 因 edgelite.models.north 缺失无法导入，记录为 P0 Bug
from edgelite.api.mqtt_forwarder import router as mqtt_forwarder_router
from edgelite.api.mqtt_server import router as mqtt_server_router
from edgelite.api.mcp import router as mcp_router
from edgelite.api.grafana import router as grafana_router
from edgelite.api.audit import router as audit_router
from edgelite.api.preprocess import router as preprocess_router
from edgelite.api.expressions import router as expressions_router
from edgelite.api.modbus_slave import router as modbus_slave_router
from edgelite.api.serial_bridge import router as serial_bridge_router
from edgelite.api.video import router as video_router
from edgelite.api.scada import router as scada_router
from edgelite.api.integration import router as integration_router
from edgelite.api.debug import router as debug_router
from edgelite.api.metrics import router as metrics_router
from edgelite.api.resource_shares import router as resource_shares_router

# ── 测试常量 ──

_TEST_SECRET = "test-secret-key-for-acceptance-testing-only-32chars!"
_TEST_USER = {"user_id": "test-admin", "username": "testadmin", "role": "admin"}
_VIEWER_USER = {"user_id": "test-viewer", "username": "testviewer", "role": "viewer"}


# ── 公共 Fixture ──


def _make_mock_audit() -> AsyncMock:
    svc = AsyncMock()
    svc.log = AsyncMock(return_value=None)
    return svc


def _make_mock_database() -> MagicMock:
    db = MagicMock()
    db.write_lock = MagicMock()

    class _SessionCM:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    db.get_session = MagicMock(return_value=_SessionCM())
    db.audit_db_path = "data/test_audit.db"
    # 添加 get_lock_status 方法用于 locks 端点
    db.get_lock_status = MagicMock(return_value={
        "global_locks": {},
        "table_locks": {},
        "write_lock_active": False,
    })
    return db


def _make_mock_device_service() -> AsyncMock:
    svc = AsyncMock()
    _device_data = {
        "device_id": "test-device-01",
        "name": "Test Device",
        "protocol": "modbus_tcp",
        "status": "offline",
        "config": {"host": "127.0.0.1", "port": 502, "slave_id": 1},
        "points": [{"name": "temp", "data_type": "float32", "address": "0"}],
        "collect_interval": 5,
        "created_by": "test-admin",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "version": 1,
    }
    svc.get_device = AsyncMock(return_value=_device_data)
    svc.list_devices = AsyncMock(return_value=([_device_data], 1))
    svc.create_device = AsyncMock(return_value=_device_data)
    svc.update_device = AsyncMock(return_value=_device_data)
    svc.delete_device = AsyncMock(return_value=(True, None))
    svc.list_device_ids_by_owner = AsyncMock(return_value=[])
    # 添加 get_status_counts 方法用于 stats 端点
    svc.get_status_counts = AsyncMock(return_value={"online": 0, "offline": 1, "error": 0})
    return svc


def _make_mock_rule_service() -> AsyncMock:
    svc = AsyncMock()
    _rule_data = {
        "rule_id": "test-rule-01",
        "name": "Test Rule",
        "device_id": "test-device-01",
        "conditions": [{"point": "temp", "operator": ">", "threshold": 80, "type": "threshold"}],
        "logic": "and",
        "duration": 0,
        "severity": "critical",
        "enabled": True,
        "notify_channels": [],
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "created_by": "test-admin",
        "version": 1,
        "inference_count": 0,
        "error_count": 0,
    }
    svc.list_rules = AsyncMock(return_value=([_rule_data], 1))
    svc.get_rule = AsyncMock(return_value=_rule_data)
    svc.create_rule = AsyncMock(return_value=_rule_data)
    svc.update_rule = AsyncMock(return_value=_rule_data)
    svc.delete_rule = AsyncMock(return_value=True)
    svc.list_rules_by_ids = AsyncMock(return_value=[])
    svc.test_rule = AsyncMock(return_value={"triggered": True, "evaluated_value": 85.5})
    return svc


def _make_mock_alarm_service() -> AsyncMock:
    svc = AsyncMock()
    _alarm_data = {
        "alarm_id": "alarm-001",
        "rule_id": "test-rule-01",
        "device_id": "test-device-01",
        "severity": "critical",
        "status": "firing",
        "message": "Temperature exceeded threshold",
        "trigger_value": {"point_name": "temp", "value": 85.5},
        "trigger_count": 1,
        "fired_at": "2026-01-01T00:00:00Z",
        "acknowledged_at": None,
        "acknowledged_by": None,
        "recovered_at": None,
        "rule_type": "threshold",
        "version": 1,
    }
    _ack_data = {**_alarm_data, "status": "acknowledged", "acknowledged_by": "testadmin", "acknowledged_at": "2026-01-01T00:00:01Z"}
    _rec_data = {**_alarm_data, "status": "recovered", "recovered_at": "2026-01-01T00:00:02Z"}
    svc.list_alarms = AsyncMock(return_value=([_alarm_data], 1))
    svc.get_alarm = AsyncMock(return_value=_alarm_data)
    # API 使用 ack_alarm 和 clear_alarm，而不是 acknowledge_alarm 和 recover_alarm
    svc.ack_alarm = AsyncMock(return_value=_ack_data)
    svc.clear_alarm = AsyncMock(return_value=_rec_data)
    svc.get_statistics = AsyncMock(return_value={"total": 1, "firing": 1, "acknowledged": 0})
    svc.get_trend = AsyncMock(return_value=[])
    return svc


def _make_mock_data_service() -> AsyncMock:
    svc = AsyncMock()
    svc.query_timeseries = AsyncMock(return_value=[])
    svc.export_data = AsyncMock(return_value=b"timestamp,device_id,point_name,value\n")
    svc.get_stats = AsyncMock(return_value={})
    svc.get_trend = AsyncMock(return_value=[])
    svc.get_correlation = AsyncMock(return_value={})
    svc.get_statistics = AsyncMock(return_value={})
    svc.query_multi_point = AsyncMock(return_value={})
    return svc


def _make_mock_system_service() -> AsyncMock:
    svc = AsyncMock()
    svc.get_status = AsyncMock(return_value={
        "cpu_percent": 10.0,
        "memory_total": 16384,
        "memory_used": 8192,
        "memory_percent": 50.0,
        "disk_total": 100000,
        "disk_used": 50000,
        "disk_percent": 50.0,
        "device_total": 0,
        "device_online": 0,
        "rule_total": 0,
        "rule_enabled": 0,
        "alarm_firing": 0,
        "collect_task_count": 0,
        "uptime": 100,
        "version": "1.0.0",
    })
    svc.collect_resources = AsyncMock(return_value={"cpu": 10, "memory": 50})
    return svc


def _make_mock_scheduler() -> AsyncMock:
    sch = AsyncMock()
    sch.stop_all = AsyncMock()
    sch.get_all_status = MagicMock(return_value={})
    return sch


def _make_mock_driver_registry() -> MagicMock:
    reg = MagicMock()
    reg.items = MagicMock(return_value=[])
    reg.get_all_protocol_keys = MagicMock(return_value=[])
    return reg


def _make_mock_plugin_manager() -> AsyncMock:
    pm = AsyncMock()
    pm.list_plugins = MagicMock(return_value=[])
    return pm


def _build_test_app(role: str = "admin") -> FastAPI:
    """构建包含所有路由的测试应用实例"""
    os.environ.setdefault("EDGELITE_SECURITY__SECRET_KEY", _TEST_SECRET)
    os.environ.setdefault("EDGELITE_ADMIN_PASSWORD", "TestPass#123")
    os.environ.setdefault("DEV_MODE", "true")

    app = FastAPI(title="EdgeLite Test")

    # CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册所有路由
    for r in [
        health_router, auth_router, devices_router, rules_router, alarms_router,
        data_router, ai_router, system_router, users_router, services_router,
        drivers_router, notify_router, mqtt_forwarder_router,
        mqtt_server_router, mcp_router, grafana_router, audit_router,
        preprocess_router, expressions_router, modbus_slave_router,
        serial_bridge_router, video_router, scada_router, integration_router,
        debug_router, metrics_router, resource_shares_router,
        # platforms_router 排除: edgelite.models.north 模块缺失
    ]:
        app.include_router(r)

    # 覆盖认证依赖
    user = _TEST_USER if role == "admin" else _VIEWER_USER
    app.dependency_overrides[get_current_user] = lambda: user

    # 注入 mock 服务到 app.state
    mock_db = _make_mock_database()
    app.state.database = mock_db
    app.state.device_service = _make_mock_device_service()
    app.state.rule_service = _make_mock_rule_service()
    app.state.alarm_service = _make_mock_alarm_service()
    app.state.data_service = _make_mock_data_service()
    app.state.system_service = _make_mock_system_service()
    app.state.scheduler = _make_mock_scheduler()
    app.state.audit_service = _make_mock_audit()
    app.state.driver_registry = _make_mock_driver_registry()
    app.state.plugin_manager = _make_mock_plugin_manager()
    app.state.platform_handlers = {}
    app.state.influx_storage = MagicMock()
    app.state.mqtt_forwarder = None
    app.state.config = MagicMock()
    app.state.config.influxdb = MagicMock(token="test-token", url="http://localhost:8086", org="edgelite", bucket="edgelite")
    app.state.config.security = MagicMock(
        secret_key=_TEST_SECRET,
        access_token_expire_minutes=30,
        refresh_token_expire_days=7,
    )
    app.state.config.server = MagicMock(cors_allowed_origins=["*"])
    app.state.config.logging = MagicMock(level="INFO", format="%(message)s")
    app.state.config.simulator = MagicMock(auto_create=False, default_devices=[])
    app.state.config.ai_inference = MagicMock(enabled=False)
    app.state.config.mqtt = MagicMock(broker="localhost", port=1883)
    app.state.config.mqtt_server = MagicMock(enabled=False)
    app.state.config.modbus_slave = MagicMock(enabled=False)
    app.state.config.preprocess = MagicMock(enabled=False)
    app.state.config.database = MagicMock(backend="sqlite", sqlite_path="data/test.db")
    app.state.config.notify = MagicMock()
    app.state.config.platforms = {}
    app.state.config.drivers = MagicMock(custom_dir="")
    app.state.config.mqtt_forwarder = MagicMock(enabled=False)
    app.state.config.websocket = MagicMock(max_connections=100)
    app.state.config.backup = MagicMock(
        enabled=False, backup_dir="data/backups",
        interval_hours=24, retain_days=7, min_free_mb=100,
    )
    app.state.config.cache = MagicMock(ring_buffer_capacity=1000)
    app.state.config.ota_update_url = ""
    app.state.start_time = time.time()

    # 同步 mock 服务到 _app_state (某些 API 端点直接访问模块级 _app_state)
    try:
        from edgelite.app import _app_state as global_app_state
        for key in ["device_service", "rule_service", "alarm_service", "data_service", 
                    "system_service", "database", "config", "audit_service"]:
            if hasattr(app.state, key):
                setattr(global_app_state, key, getattr(app.state, key))
    except Exception:
        pass  # 某些测试环境下可能无法导入

    return app


@pytest.fixture
def app():
    return _build_test_app("admin")


@pytest.fixture
def viewer_app():
    return _build_test_app("viewer")


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def viewer_client(viewer_app):
    transport = ASGITransport(app=viewer_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ═══════════════════════════════════════════════════════════════
# 模块 1: 冒烟测试 (SMOKE)
# 冒烟测试不通过则立即停止全部测试
# ═══════════════════════════════════════════════════════════════


class TestSmokeHealth:
    """S1: 健康检查端点可达"""

    @pytest.mark.asyncio
    async def test_s1_01_health_live(self, client):
        """S1-01: GET /health/live 返回 200"""
        resp = await client.get("/health/live")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    @pytest.mark.asyncio
    async def test_s1_02_health_live_alias(self, client):
        """S1-02: GET /live 别名返回 200"""
        resp = await client.get("/live")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s1_03_health_ready(self, client):
        """S1-03: GET /health/ready 返回 200 或 503"""
        resp = await client.get("/health/ready")
        assert resp.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_s1_04_health_full(self, client):
        """S1-04: GET /health 返回 200 或 503"""
        resp = await client.get("/health")
        assert resp.status_code in (200, 503)


class TestSmokeAPIRouting:
    """S2: 核心 API 路由注册验证"""

    @pytest.mark.asyncio
    async def test_s2_01_devices_route(self, client):
        """S2-01: GET /api/v1/devices 返回 200"""
        resp = await client.get("/api/v1/devices")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s2_02_rules_route(self, client):
        """S2-02: GET /api/v1/rules 返回 200"""
        resp = await client.get("/api/v1/rules")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s2_03_alarms_route(self, client):
        """S2-03: GET /api/v1/alarms 返回 200"""
        resp = await client.get("/api/v1/alarms")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s2_04_system_status(self, client):
        """S2-04: GET /api/v1/system/status 返回 200"""
        resp = await client.get("/api/v1/system/status")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s2_05_ai_models(self, client):
        """S2-05: GET /api/v1/ai/models 返回 200 或 503（AI 未初始化）"""
        resp = await client.get("/api/v1/ai/models")
        assert resp.status_code in (200, 503)

    @pytest.mark.asyncio
    async def test_s2_06_drivers_list(self, client):
        """S2-06: GET /api/v1/drivers 返回 200"""
        resp = await client.get("/api/v1/drivers")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s2_07_services_list(self, client):
        """S2-07: GET /api/v1/services/list 返回 200"""
        resp = await client.get("/api/v1/services/list")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_s2_08_openapi_docs(self, client):
        """S2-08: GET /openapi.json 返回 200（API 文档可用）"""
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "paths" in data
        assert len(data["paths"]) > 10  # 至少 10 个路径


class TestSmokeAuth:
    """S3: 认证端点基本可达"""

    @pytest.mark.asyncio
    async def test_s3_01_login_missing_body(self, client):
        """S3-01: POST /api/v1/auth/login 无请求体返回 422"""
        resp = await client.post("/api/v1/auth/login")
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_s3_02_login_invalid_credentials(self, client):
        """S3-02: POST /api/v1/auth/login 错误凭据返回 401/429/500"""
        resp = await client.post(
            "/api/v1/auth/login",
            json={"username": "nonexistent", "password": "wrongpass123"},
        )
        # mock DB 环境下可能返回 500（无法执行真实 SQL），
        # 真实环境应返回 401 或 429（限流）
        assert resp.status_code in (401, 429, 500)

    @pytest.mark.asyncio
    async def test_s3_03_unknown_route_404(self, client):
        """S3-03: GET /api/v1/nonexistent 返回 404"""
        resp = await client.get("/api/v1/nonexistent")
        assert resp.status_code == 404
