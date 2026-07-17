"""服务管理API路由单元测试 - 覆盖 api/services.py 全部端点。

覆盖端点:
- GET  /api/v1/services/list                 - list_services
- GET  /api/v1/services/{name}/status        - get_service_status
- POST /api/v1/services/{name}/enable        - enable_service
- POST /api/v1/services/{name}/disable       - disable_service
- POST /api/v1/services/{name}/start         - start_service
- POST /api/v1/services/{name}/stop          - stop_service
- POST /api/v1/services/{name}/install-deps  - install_service_dependencies
- PUT  /api/v1/services/{name}/config        - update_service_config
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edgelite.api.deps import get_audit_service, get_current_user
from edgelite.api.services import router

# ── Mock 构造辅助 ──


def _make_dependency(package="amqtt", installed=True, version="1.0.0"):
    return SimpleNamespace(package=package, installed=installed, version=version)


def _make_service_info(
    name="mqtt_server",
    display_name="MQTT Server",
    description="MQTT broker",
    state="running",
    config_section="mqtt_server",
    dependencies=None,
    config_schema=None,
    current_config=None,
    error_message="",
    running_info=None,
):
    """构造 ServiceInfo-like 对象 (用 SimpleNamespace 模拟 dataclass)。"""
    return SimpleNamespace(
        name=name,
        display_name=display_name,
        description=description,
        state=SimpleNamespace(value=state),
        config_section=config_section,
        dependencies=dependencies if dependencies is not None else [_make_dependency()],
        config_schema=config_schema or {},
        current_config=current_config or {},
        error_message=error_message,
        running_info=running_info or {},
    )


TEST_SERVICE_DEFS = {
    "mqtt_server": {
        "display_name": "MQTT Server",
        "icon": "radio",
        "category": "builtin",
        "use_cases": ["uc1"],
        "related_features": [{"name": "feat1"}],
        "setup_guide": ["step1"],
    },
    "modbus_slave": {
        "display_name": "Modbus Slave",
        "icon": "cpu",
        "category": "builtin",
        "use_cases": [],
        "related_features": [],
        "setup_guide": [],
    },
}


def _make_user():
    return {"user_id": "u1", "username": "admin", "role": "admin"}


# ── Fixtures ──


@pytest.fixture
def mock_audit_svc():
    svc = AsyncMock()
    svc.log = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def mock_mgr():
    """Mock ServiceManager: 同步方法用 MagicMock，异步方法用 AsyncMock。"""
    mgr = MagicMock()
    mgr.list_services = MagicMock(return_value=[])
    mgr.get_service_info = MagicMock(return_value=None)
    mgr.enable_service = AsyncMock(return_value={"success": True})
    mgr.disable_service = AsyncMock(return_value={"success": True})
    mgr.start_service = AsyncMock(return_value={"success": True})
    mgr.stop_service = AsyncMock(return_value={"success": True})
    mgr.install_service_dependencies = AsyncMock(return_value={"all_installed": True})
    mgr.update_service_config = AsyncMock(return_value={"success": True})
    return mgr


@pytest.fixture
def app(mock_audit_svc):
    app = FastAPI()
    app.include_router(router)
    # 覆盖认证依赖 + 审计服务依赖
    app.dependency_overrides[get_current_user] = lambda: _make_user()
    app.dependency_overrides[get_audit_service] = lambda: mock_audit_svc
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def mock_env(mock_mgr, mock_audit_svc):
    """统一 patch SERVICE_DEFINITIONS / get_service_manager / _get_client_ip。

    返回 SimpleNamespace(mgr, audit) 供测试配置返回值。
    """
    with (
        patch("edgelite.api.services.SERVICE_DEFINITIONS", TEST_SERVICE_DEFS),
        patch("edgelite.api.services.get_service_manager", return_value=mock_mgr),
        patch("edgelite.api.auth._get_client_ip", return_value="127.0.0.1"),
    ):
        yield SimpleNamespace(mgr=mock_mgr, audit=mock_audit_svc)


def _patches(mgr=None, side_effect=None):
    """构造常用 patch 上下文 (供不使用 mock_env 的特殊测试用)。"""
    mgr_patch = patch(
        "edgelite.api.services.get_service_manager",
        return_value=mgr,
        side_effect=side_effect,
    )
    return (
        patch("edgelite.api.services.SERVICE_DEFINITIONS", TEST_SERVICE_DEFS),
        mgr_patch,
        patch("edgelite.api.auth._get_client_ip", return_value="127.0.0.1"),
    )


# ════════════════════════════════════════════════════════════
#  list_services
# ════════════════════════════════════════════════════════════


class TestListServices:
    def test_list_services_success(self, client, mock_env):
        info = _make_service_info(name="mqtt_server", state="enabled")
        mock_env.mgr.list_services.return_value = [info]
        resp = client.get("/api/v1/services/list")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["services"]) == 1
        svc = data["services"][0]
        assert svc["name"] == "mqtt_server"
        assert svc["state"] == "enabled"
        assert svc["icon"] == "radio"
        assert svc["category"] == "builtin"
        assert svc["use_cases"] == ["uc1"]
        assert svc["dependencies"][0]["package"] == "amqtt"
        assert svc["dependencies"][0]["installed"] is True

    def test_list_services_empty(self, client, mock_env):
        mock_env.mgr.list_services.return_value = []
        resp = client.get("/api/v1/services/list")
        assert resp.status_code == 200
        assert resp.json()["data"]["services"] == []

    def test_list_services_exception_returns_500(self, client, mock_env):
        mock_env.mgr.list_services.side_effect = RuntimeError("db down")
        resp = client.get("/api/v1/services/list")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_LIST_FAILED"


# ════════════════════════════════════════════════════════════
#  get_service_status
# ════════════════════════════════════════════════════════════


class TestGetServiceStatus:
    def test_status_unknown_service_404(self, client, mock_env):
        resp = client.get("/api/v1/services/nonexistent/status")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SVC_UNKNOWN_SERVICE"

    def test_status_success(self, client, mock_env):
        info = _make_service_info(
            name="mqtt_server",
            state="running",
            running_info={"port": 1883},
            error_message="",
        )
        mock_env.mgr.get_service_info.return_value = info
        resp = client.get("/api/v1/services/mqtt_server/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["name"] == "mqtt_server"
        assert data["state"] == "running"
        assert data["running_info"] == {"port": 1883}
        assert data["icon"] == "radio"
        assert data["use_cases"] == ["uc1"]

    def test_status_not_registered_404(self, client, mock_env):
        mock_env.mgr.get_service_info.return_value = None
        resp = client.get("/api/v1/services/mqtt_server/status")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SVC_NOT_REGISTERED"

    def test_status_exception_returns_500(self, client, mock_env):
        mock_env.mgr.get_service_info.side_effect = RuntimeError("boom")
        resp = client.get("/api/v1/services/mqtt_server/status")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_STATUS_FAILED"


# ════════════════════════════════════════════════════════════
#  enable_service
# ════════════════════════════════════════════════════════════


class TestEnableService:
    def test_enable_unknown_service_404(self, client, mock_env):
        resp = client.post("/api/v1/services/nonexistent/enable")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SVC_UNKNOWN_SERVICE"

    def test_enable_success_no_body(self, client, mock_env):
        mock_env.mgr.enable_service.return_value = {"success": True, "started": True}
        resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 200
        assert resp.json()["data"]["success"] is True
        mock_env.mgr.enable_service.assert_awaited_once_with("mqtt_server", None)
        # 成功审计日志应被调用
        mock_env.audit.log.assert_awaited()

    def test_enable_success_with_config(self, client, mock_env):
        mock_env.mgr.enable_service.return_value = {"success": True}
        resp = client.post(
            "/api/v1/services/mqtt_server/enable",
            json={"config": {"host": "localhost", "port": 1883}},
        )
        assert resp.status_code == 200
        mock_env.mgr.enable_service.assert_awaited_once_with("mqtt_server", {"host": "localhost", "port": 1883})

    def test_enable_sanitizes_sensitive_config(self, client, mock_env):
        mock_env.mgr.enable_service.return_value = {"success": True}
        resp = client.post(
            "/api/v1/services/mqtt_server/enable",
            json={"config": {"password": "secret", "host": "localhost"}},
        )
        assert resp.status_code == 200
        after_value = mock_env.audit.log.call_args.kwargs.get("after_value", {})
        assert after_value["config"]["password"] == "***"
        assert after_value["config"]["host"] == "localhost"

    def test_enable_success_with_warning(self, client, mock_env):
        mock_env.mgr.enable_service.return_value = {
            "success": True,
            "warning": "partial start",
        }
        resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["message"] == "partial start"
        assert data["success"] is True

    def test_enable_missing_dependencies_424(self, client, mock_env):
        mock_env.mgr.enable_service.return_value = {
            "success": False,
            "missing_dependencies": ["amqtt", "paho-mqtt"],
            "error": "ERR_SVC_DEPS_MISSING",
        }
        resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 424
        detail = resp.json()["detail"]
        assert "missing dependencies" in detail
        assert "amqtt" in detail
        assert "paho-mqtt" in detail
        # 失败审计日志应被调用
        mock_env.audit.log.assert_awaited()

    def test_enable_runtime_error_409(self, client, mock_env):
        mock_env.mgr.enable_service.return_value = {
            "success": False,
            "error_type": "runtime",
            "error": "ERR_SVC_START_FAILED",
        }
        resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_START_FAILED"

    def test_enable_other_error_500(self, client, mock_env):
        mock_env.mgr.enable_service.return_value = {
            "success": False,
            "error_type": "config",
            "error": "ERR_SVC_ENABLE_FAILED",
        }
        resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_ENABLE_FAILED"

    def test_enable_with_hint_returns_detail_obj(self, client, mock_env):
        mock_env.mgr.enable_service.return_value = {
            "success": False,
            "error_type": "runtime",
            "error": "ERR_SVC_START_FAILED",
            "hint": "check port 1883",
        }
        resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "ERR_SVC_START_FAILED"
        assert detail["hint"] == "check port 1883"

    def test_enable_enum_error_code_uses_value(self, client, mock_env):
        from edgelite.services.service_manager import ServiceErrors as SmServiceErrors

        mock_env.mgr.enable_service.return_value = {
            "success": False,
            "error_type": "runtime",
            "error": SmServiceErrors.START_FAILED,
        }
        resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_START_FAILED"

    def test_enable_get_mgr_fails_503(self, client):
        with (
            patch("edgelite.api.services.SERVICE_DEFINITIONS", TEST_SERVICE_DEFS),
            patch("edgelite.api.services.get_service_manager", side_effect=RuntimeError("no mgr")),
            patch("edgelite.api.auth._get_client_ip", return_value="127.0.0.1"),
        ):
            resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 503
        assert "ERR_SVC_ENABLE_FAILED" in resp.json()["detail"]

    def test_enable_unexpected_exception_503(self, client, mock_env):
        mock_env.mgr.enable_service.side_effect = RuntimeError("unexpected")
        resp = client.post("/api/v1/services/mqtt_server/enable")
        assert resp.status_code == 503
        assert "ERR_SVC_ENABLE_FAILED" in resp.json()["detail"]
        # 异常路径审计日志也应被调用
        mock_env.audit.log.assert_awaited()

    def test_enable_audit_log_failure_continues(self, client, mock_env):
        mock_env.audit.log.side_effect = RuntimeError("audit down")
        mock_env.mgr.enable_service.return_value = {"success": True}
        resp = client.post("/api/v1/services/mqtt_server/enable")
        # 审计日志失败不应影响主流程
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════
#  disable_service
# ════════════════════════════════════════════════════════════


class TestDisableService:
    def test_disable_unknown_service_404(self, client, mock_env):
        resp = client.post("/api/v1/services/nonexistent/disable")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SVC_UNKNOWN_SERVICE"

    def test_disable_success(self, client, mock_env):
        mock_env.mgr.disable_service.return_value = {"success": True, "stopped": True}
        resp = client.post("/api/v1/services/mqtt_server/disable")
        assert resp.status_code == 200
        assert resp.json()["data"]["success"] is True
        mock_env.mgr.disable_service.assert_awaited_once_with("mqtt_server")
        mock_env.audit.log.assert_awaited()

    def test_disable_result_not_success_409(self, client, mock_env):
        mock_env.mgr.disable_service.return_value = {
            "success": False,
            "error": "ERR_SVC_DISABLED",
        }
        resp = client.post("/api/v1/services/mqtt_server/disable")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_DISABLED"

    def test_disable_enum_error_code_uses_value(self, client, mock_env):
        from edgelite.services.service_manager import ServiceErrors as SmServiceErrors

        mock_env.mgr.disable_service.return_value = {
            "success": False,
            "error": SmServiceErrors.NOT_RUNNING,
        }
        resp = client.post("/api/v1/services/mqtt_server/disable")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_NOT_RUNNING"

    def test_disable_value_error_422(self, client, mock_env):
        mock_env.mgr.disable_service.side_effect = ValueError("bad config")
        resp = client.post("/api/v1/services/mqtt_server/disable")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "ERR_SVC_DISABLE_FAILED"

    def test_disable_runtime_error_409(self, client, mock_env):
        mock_env.mgr.disable_service.side_effect = RuntimeError("still running")
        resp = client.post("/api/v1/services/mqtt_server/disable")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_DISABLE_FAILED"

    def test_disable_unexpected_exception_500(self, client, mock_env):
        mock_env.mgr.disable_service.side_effect = KeyError("boom")
        resp = client.post("/api/v1/services/mqtt_server/disable")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_DISABLE_FAILED"


# ════════════════════════════════════════════════════════════
#  start_service
# ════════════════════════════════════════════════════════════


class TestStartService:
    def test_start_unknown_service_404(self, client, mock_env):
        resp = client.post("/api/v1/services/nonexistent/start")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SVC_UNKNOWN_SERVICE"

    def test_start_success(self, client, mock_env):
        mock_env.mgr.start_service.return_value = {"success": True, "pid": 1234}
        resp = client.post("/api/v1/services/mqtt_server/start")
        assert resp.status_code == 200
        assert resp.json()["data"]["pid"] == 1234
        mock_env.mgr.start_service.assert_awaited_once_with("mqtt_server")
        mock_env.audit.log.assert_awaited()

    def test_start_runtime_error_409(self, client, mock_env):
        mock_env.mgr.start_service.return_value = {
            "success": False,
            "error_type": "runtime",
            "error": "ERR_SVC_START_FAILED",
        }
        resp = client.post("/api/v1/services/mqtt_server/start")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_START_FAILED"

    def test_start_runtime_error_with_hint(self, client, mock_env):
        mock_env.mgr.start_service.return_value = {
            "success": False,
            "error_type": "runtime",
            "error": "ERR_SVC_START_FAILED",
            "hint": "port in use",
        }
        resp = client.post("/api/v1/services/mqtt_server/start")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["error"] == "ERR_SVC_START_FAILED"
        assert detail["hint"] == "port in use"

    def test_start_other_error_500(self, client, mock_env):
        mock_env.mgr.start_service.return_value = {
            "success": False,
            "error_type": "config",
            "error": "ERR_SVC_START_FAILED",
        }
        resp = client.post("/api/v1/services/mqtt_server/start")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_START_FAILED"

    def test_start_enum_error_code_uses_value(self, client, mock_env):
        from edgelite.services.service_manager import ServiceErrors as SmServiceErrors

        mock_env.mgr.start_service.return_value = {
            "success": False,
            "error_type": "runtime",
            "error": SmServiceErrors.ALREADY_RUNNING,
        }
        resp = client.post("/api/v1/services/mqtt_server/start")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_ALREADY_RUNNING"

    def test_start_unexpected_exception_500(self, client, mock_env):
        mock_env.mgr.start_service.side_effect = RuntimeError("boom")
        resp = client.post("/api/v1/services/mqtt_server/start")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_START_FAILED"
        mock_env.audit.log.assert_awaited()


# ════════════════════════════════════════════════════════════
#  stop_service
# ════════════════════════════════════════════════════════════


class TestStopService:
    def test_stop_unknown_service_404(self, client, mock_env):
        resp = client.post("/api/v1/services/nonexistent/stop")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SVC_UNKNOWN_SERVICE"

    def test_stop_success(self, client, mock_env):
        mock_env.mgr.stop_service.return_value = {"success": True}
        resp = client.post("/api/v1/services/mqtt_server/stop")
        assert resp.status_code == 200
        assert resp.json()["data"]["success"] is True
        mock_env.mgr.stop_service.assert_awaited_once_with("mqtt_server")
        mock_env.audit.log.assert_awaited()

    def test_stop_result_not_success_409(self, client, mock_env):
        mock_env.mgr.stop_service.return_value = {
            "success": False,
            "error": "ERR_SVC_NOT_RUNNING",
        }
        resp = client.post("/api/v1/services/mqtt_server/stop")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_NOT_RUNNING"

    def test_stop_enum_error_code_uses_value(self, client, mock_env):
        from edgelite.services.service_manager import ServiceErrors as SmServiceErrors

        mock_env.mgr.stop_service.return_value = {
            "success": False,
            "error": SmServiceErrors.NOT_RUNNING,
        }
        resp = client.post("/api/v1/services/mqtt_server/stop")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_NOT_RUNNING"

    def test_stop_value_error_422(self, client, mock_env):
        mock_env.mgr.stop_service.side_effect = ValueError("bad")
        resp = client.post("/api/v1/services/mqtt_server/stop")
        assert resp.status_code == 422
        assert resp.json()["detail"] == "ERR_SVC_STOP_FAILED"

    def test_stop_runtime_error_409(self, client, mock_env):
        mock_env.mgr.stop_service.side_effect = RuntimeError("stuck")
        resp = client.post("/api/v1/services/mqtt_server/stop")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_SVC_STOP_FAILED"

    def test_stop_unexpected_exception_500(self, client, mock_env):
        mock_env.mgr.stop_service.side_effect = KeyError("boom")
        resp = client.post("/api/v1/services/mqtt_server/stop")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_STOP_FAILED"


# ════════════════════════════════════════════════════════════
#  install_service_dependencies
# ════════════════════════════════════════════════════════════


class TestInstallServiceDependencies:
    def test_install_deps_disabled_in_production_403(self, client):
        config = SimpleNamespace(server=SimpleNamespace(debug_api_enabled=False))
        with patch("edgelite.config.get_config", return_value=config):
            resp = client.post("/api/v1/services/mqtt_server/install-deps")
        assert resp.status_code == 403
        assert resp.json()["detail"] == "ERR_INSTALL_DEPS_DISABLED_IN_PRODUCTION"

    def test_install_deps_unknown_service_404(self, client, mock_env):
        config = SimpleNamespace(server=SimpleNamespace(debug_api_enabled=True))
        with patch("edgelite.config.get_config", return_value=config):
            resp = client.post("/api/v1/services/nonexistent/install-deps")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SVC_UNKNOWN_SERVICE"

    def test_install_deps_success(self, client, mock_env):
        config = SimpleNamespace(server=SimpleNamespace(debug_api_enabled=True))
        mock_env.mgr.install_service_dependencies.return_value = {
            "all_installed": True,
            "installed": ["amqtt"],
        }
        with patch("edgelite.config.get_config", return_value=config):
            resp = client.post("/api/v1/services/mqtt_server/install-deps")
        assert resp.status_code == 200
        assert resp.json()["data"]["all_installed"] is True
        mock_env.mgr.install_service_dependencies.assert_awaited_once_with("mqtt_server")

    def test_install_deps_not_all_installed_500(self, client, mock_env):
        config = SimpleNamespace(server=SimpleNamespace(debug_api_enabled=True))
        mock_env.mgr.install_service_dependencies.return_value = {
            "all_installed": False,
            "failed": ["amqtt"],
        }
        with patch("edgelite.config.get_config", return_value=config):
            resp = client.post("/api/v1/services/mqtt_server/install-deps")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_DEPS_INSTALL_FAILED"

    def test_install_deps_exception_500(self, client, mock_env):
        config = SimpleNamespace(server=SimpleNamespace(debug_api_enabled=True))
        mock_env.mgr.install_service_dependencies.side_effect = RuntimeError("pip fail")
        with patch("edgelite.config.get_config", return_value=config):
            resp = client.post("/api/v1/services/mqtt_server/install-deps")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_INSTALL_FAILED"


# ════════════════════════════════════════════════════════════
#  update_service_config
# ════════════════════════════════════════════════════════════


class TestUpdateServiceConfig:
    def test_update_config_unknown_service_404(self, client, mock_env):
        resp = client.put(
            "/api/v1/services/nonexistent/config",
            json={"config": {"port": 1883}},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SVC_UNKNOWN_SERVICE"

    def test_update_config_success(self, client, mock_env):
        mock_env.mgr.update_service_config.return_value = {
            "success": True,
            "message": "updated",
        }
        resp = client.put(
            "/api/v1/services/mqtt_server/config",
            json={"config": {"port": 1883}},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["success"] is True
        mock_env.mgr.update_service_config.assert_awaited_once_with("mqtt_server", {"port": 1883})

    def test_update_config_result_not_success_422(self, client, mock_env):
        mock_env.mgr.update_service_config.return_value = {
            "success": False,
            "error": "ERR_INVALID_CONFIG",
        }
        resp = client.put(
            "/api/v1/services/mqtt_server/config",
            json={"config": {"bad": True}},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "ERR_INVALID_CONFIG"

    def test_update_config_result_not_success_default_error(self, client, mock_env):
        mock_env.mgr.update_service_config.return_value = {"success": False}
        resp = client.put(
            "/api/v1/services/mqtt_server/config",
            json={"config": {}},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "ERR_SVC_CONFIG_UPDATE_FAILED"

    def test_update_config_value_error_422(self, client, mock_env):
        mock_env.mgr.update_service_config.side_effect = ValueError("bad schema")
        resp = client.put(
            "/api/v1/services/mqtt_server/config",
            json={"config": {"port": -1}},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "ERR_SVC_CONFIG_UPDATE_FAILED"

    def test_update_config_exception_500(self, client, mock_env):
        mock_env.mgr.update_service_config.side_effect = RuntimeError("db fail")
        resp = client.put(
            "/api/v1/services/mqtt_server/config",
            json={"config": {"port": 1883}},
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SVC_CONFIG_UPDATE_FAILED"
