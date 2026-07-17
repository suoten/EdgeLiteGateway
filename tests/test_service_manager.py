"""服务管理器测试 - 覆盖 ServiceManager 全部公开/私有方法与边界条件。

覆盖 edgelite/services/service_manager.py:
- ServiceErrors / ServiceState 枚举
- DependencyInfo / ServiceInfo 数据类
- SERVICE_DEFINITIONS / _PIP_TO_IMPORT 常量
- ServiceManager: 依赖检查/安装/启停/配置更新/状态查询/错误提示
- get_service_manager 单例工厂
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.services.service_manager import (
    _PIP_TO_IMPORT,
    SERVICE_DEFINITIONS,
    DependencyInfo,
    ServiceErrors,
    ServiceInfo,
    ServiceManager,
    ServiceState,
    get_service_manager,
)


def _sec(enabled=False, **kw):
    """构造带 model_fields 的模拟配置段。"""
    s = SimpleNamespace(enabled=enabled, **kw)
    s.model_fields = {"enabled": {}, **{k: {} for k in kw}}
    return s


def _inst(running=True, **kw):
    """构造模拟服务实例。"""
    m = MagicMock()
    m.is_running = running
    for k, v in kw.items():
        setattr(m, k, v)
    return m


@pytest.fixture
def manager():
    return ServiceManager()


@pytest.fixture
def app_state():
    state = SimpleNamespace()
    with patch("edgelite.app._app_state", state):
        yield state


@pytest.fixture
def mock_get_config():
    config = MagicMock()
    with patch("edgelite.services.service_manager.get_config", return_value=config):
        yield config


@pytest.fixture
def mock_update_config():
    with patch("edgelite.services.service_manager.update_config_section") as m:
        yield m


class TestEnums:
    """枚举值校验。"""

    def test_service_errors(self):
        assert ServiceErrors.START_FAILED == "ERR_SVC_START_FAILED"
        assert ServiceErrors.UNKNOWN_SERVICE == "ERR_SVC_UNKNOWN_SERVICE"
        assert ServiceErrors.DEPS_INSTALL_FAILED == "ERR_SVC_DEPS_INSTALL_FAILED"
        assert ServiceErrors.ALREADY_RUNNING == "ERR_SVC_ALREADY_RUNNING"
        assert ServiceErrors.NOT_RUNNING == "ERR_SVC_NOT_RUNNING"
        assert ServiceErrors.CONFIG_UPDATE_FAILED == "ERR_SVC_CONFIG_UPDATE_FAILED"
        assert ServiceErrors.DEPS_MISSING == "ERR_SVC_DEPS_MISSING"
        assert ServiceErrors.PIP_VERIFY_FAILED == "ERR_SVC_PIP_VERIFY_FAILED"
        assert ServiceErrors.SERVICE_ENABLED_STARTED == "ERR_SVC_ENABLED_STARTED"
        assert ServiceErrors.SERVICE_DISABLED == "ERR_SVC_DISABLED"
        assert ServiceErrors.SERVICE_STARTED == "ERR_SVC_STARTED"
        assert ServiceErrors.SERVICE_STOPPED == "ERR_SVC_STOPPED"
        assert ServiceErrors.CONFIG_UPDATED_RESTARTED == "ERR_SVC_CONFIG_UPDATED_RESTARTED"
        assert ServiceErrors.CONFIG_UPDATED_RESTART_FAILED == "ERR_SVC_CONFIG_UPDATED_RESTART_FAILED"
        assert ServiceErrors.CONFIG_UPDATED == "ERR_SVC_CONFIG_UPDATED"

    def test_service_state(self):
        assert ServiceState.DISABLED == "disabled"
        assert ServiceState.ENABLED == "enabled"
        assert ServiceState.RUNNING == "running"
        assert ServiceState.ERROR == "error"
        assert ServiceState.INSTALLING == "installing"


class TestDataclasses:
    """数据类。"""

    def test_dependency_info_defaults(self):
        d = DependencyInfo(package="amqtt")
        assert d.installed is False
        assert d.version == ""

    def test_dependency_info_with_values(self):
        d = DependencyInfo(package="p", installed=True, version="1.0")
        assert d.installed is True

    def test_service_info_defaults(self):
        i = ServiceInfo(name="n", display_name="d", description="desc", config_section="c")
        assert i.state == ServiceState.DISABLED
        assert i.dependencies == []
        assert i.icon == ""
        assert i.category == "builtin"

    def test_service_info_with_values(self):
        i = ServiceInfo(name="n", display_name="d", description="s", config_section="c", state=ServiceState.RUNNING)
        assert i.state == ServiceState.RUNNING


class TestConstants:
    """常量校验。"""

    def test_pip_to_import(self):
        assert _PIP_TO_IMPORT["pyserial"] == "serial"
        assert _PIP_TO_IMPORT["pyserial-asyncio"] == "serial_asyncio"
        assert _PIP_TO_IMPORT["onvif-zeep"] == "onvif"

    def test_service_definitions(self):
        assert set(SERVICE_DEFINITIONS.keys()) == {
            "mqtt_server",
            "modbus_slave",
            "serial_bridge",
            "mcp_server",
            "grafana",
        }
        for svc in SERVICE_DEFINITIONS.values():
            assert "display_name" in svc and "config_section" in svc and "dependencies" in svc


class TestCheckDependency:
    """check_dependency 方法。"""

    def test_installed_with_version(self, manager):
        mod = MagicMock()
        mod.__version__ = "1.2.3"
        with patch("edgelite.services.service_manager.importlib.import_module", return_value=mod):
            info = manager.check_dependency("amqtt")
        assert info.installed is True
        assert info.version == "1.2.3"

    def test_installed_no_version(self, manager):
        with patch("edgelite.services.service_manager.importlib.import_module", return_value=MagicMock(spec=[])):
            info = manager.check_dependency("amqtt")
        assert info.installed is True
        assert info.version == ""

    def test_not_installed(self, manager):
        with patch("edgelite.services.service_manager.importlib.import_module", side_effect=ImportError):
            info = manager.check_dependency("amqtt")
        assert info.installed is False

    def test_pip_to_import_mapping(self, manager):
        with patch("edgelite.services.service_manager.importlib.import_module", return_value=MagicMock()) as m:
            manager.check_dependency("pyserial")
        m.assert_called_once_with("serial")


class TestCheckDependencies:
    """check_dependencies / all_dependencies_met 方法。"""

    def test_known_service(self, manager):
        with patch.object(manager, "check_dependency", return_value=DependencyInfo("a", True)):
            assert len(manager.check_dependencies("mqtt_server")) == 1

    def test_unknown_service(self, manager):
        assert manager.check_dependencies("nope") == []

    def test_multiple_deps(self, manager):
        r = [DependencyInfo("a", True), DependencyInfo("b", False)]
        with patch.object(manager, "check_dependency", side_effect=r):
            assert len(manager.check_dependencies("serial_bridge")) == 2

    def test_all_met(self, manager):
        with patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]):
            assert manager.all_dependencies_met("mqtt_server") is True

    def test_not_met(self, manager):
        with patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", False)]):
            assert manager.all_dependencies_met("mqtt_server") is False

    def test_empty_deps(self, manager):
        with patch.object(manager, "check_dependencies", return_value=[]):
            assert manager.all_dependencies_met("mcp_server") is True


class TestInstallDependency:
    """install_dependency 方法。"""

    async def test_success(self, manager):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"ok", b""))
        proc.returncode = 0
        with patch("edgelite.services.service_manager.asyncio.create_subprocess_exec", return_value=proc):
            r = await manager.install_dependency("amqtt")
        assert r["success"] is True
        assert "ok" in r["output"]

    async def test_failure(self, manager):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"ERROR"))
        proc.returncode = 1
        with patch("edgelite.services.service_manager.asyncio.create_subprocess_exec", return_value=proc):
            r = await manager.install_dependency("amqtt")
        assert r["success"] is False
        assert "ERROR" in r["error"]

    async def test_timeout(self, manager):
        proc = MagicMock()
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        with (
            patch("edgelite.services.service_manager.asyncio.create_subprocess_exec", return_value=proc),
            patch("edgelite.services.service_manager.asyncio.wait_for", side_effect=TimeoutError),
        ):
            r = await manager.install_dependency("amqtt")
        assert r["success"] is False
        assert "timed out" in r["error"]
        proc.kill.assert_called_once()

    async def test_exception(self, manager):
        with patch("edgelite.services.service_manager.asyncio.create_subprocess_exec", side_effect=OSError("fail")):
            r = await manager.install_dependency("amqtt")
        assert r["success"] is False
        assert "fail" in r["error"]


class TestInstallServiceDeps:
    """install_service_dependencies 方法。"""

    async def test_unknown(self, manager):
        r = await manager.install_service_dependencies("nope")
        assert r["error"] == ServiceErrors.UNKNOWN_SERVICE

    async def test_all_installed(self, manager):
        with patch.object(manager, "check_dependency", return_value=DependencyInfo("a", True, "1.0")):
            r = await manager.install_service_dependencies("mqtt_server")
        assert r["all_installed"] is True
        assert r["results"][0]["skipped"] is True

    async def test_install_success(self, manager):
        with (
            patch.object(
                manager, "check_dependency", side_effect=[DependencyInfo("a", False), DependencyInfo("a", True, "1.0")]
            ),
            patch.object(manager, "install_dependency", return_value={"success": True}),
        ):
            r = await manager.install_service_dependencies("mqtt_server")
        assert r["all_installed"] is True

    async def test_install_failure(self, manager):
        with (
            patch.object(manager, "check_dependency", return_value=DependencyInfo("a", False)),
            patch.object(manager, "install_dependency", return_value={"success": False, "error": "e"}),
        ):
            r = await manager.install_service_dependencies("mqtt_server")
        assert r["all_installed"] is False

    async def test_verify_failed(self, manager):
        with (
            patch.object(manager, "check_dependency", return_value=DependencyInfo("a", False)),
            patch.object(manager, "install_dependency", return_value={"success": True}),
        ):
            r = await manager.install_service_dependencies("mqtt_server")
        assert r["all_installed"] is False
        assert r["results"][0]["error"] == ServiceErrors.PIP_VERIFY_FAILED


class TestGetServiceInfo:
    """get_service_info 方法。"""

    def test_unknown(self, manager):
        i = manager.get_service_info("nope")
        assert i.state == ServiceState.ERROR
        assert i.error_message == ServiceErrors.UNKNOWN_SERVICE

    def test_disabled(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(False, host="127.0.0.1", port=1888)
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
        ):
            i = manager.get_service_info("mqtt_server")
        assert i.state == ServiceState.DISABLED
        assert i.current_config == {"host": "127.0.0.1", "port": 1888}

    def test_enabled(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(True, host="h", port=1)
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=None),
        ):
            assert manager.get_service_info("mqtt_server").state == ServiceState.ENABLED

    def test_error_deps_missing(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(True, host="h", port=1)
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", False)]),
            patch.object(manager, "_get_instance", return_value=None),
        ):
            assert manager.get_service_info("mqtt_server").state == ServiceState.ERROR

    def test_running_is_running(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(True)
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=_inst(True)),
        ):
            assert manager.get_service_info("mqtt_server").state == ServiceState.RUNNING

    def test_running_via_get_status(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(False)
        stats = SimpleNamespace(
            running=True, serial_rx_bytes=10, serial_tx_bytes=20, tcp_rx_bytes=30, tcp_tx_bytes=40, client_count=2
        )
        inst = MagicMock(spec=["get_status"])
        inst.get_status = MagicMock(return_value=stats)
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[]),
            patch.object(manager, "_get_instance", return_value=inst),
        ):
            i = manager.get_service_info("modbus_slave")
        assert i.state == ServiceState.RUNNING
        assert i.running_info["client_count"] == 2

    def test_get_status_exception(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(False)
        inst = MagicMock(spec=["get_status"])
        inst.get_status = MagicMock(side_effect=RuntimeError("boom"))
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[]),
            patch.object(manager, "_get_instance", return_value=inst),
        ):
            assert manager.get_service_info("mqtt_server").running_info == {}

    def test_mqtt_connections_get_client_count(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(True)
        inst = _inst(True, get_client_count=MagicMock(return_value=5))
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=inst),
        ):
            assert manager.get_service_info("mqtt_server").running_info["connections"] == 5

    def test_mqtt_connections_clients_list(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(True)
        inst = MagicMock(spec=["is_running"])
        inst.is_running = True
        inst._clients = ["c1", "c2", "c3"]
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=inst),
        ):
            assert manager.get_service_info("mqtt_server").running_info["connections"] == 3

    def test_mqtt_connections_none(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(True)
        inst = MagicMock(spec=["is_running"])
        inst.is_running = True
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=inst),
        ):
            assert manager.get_service_info("mqtt_server").running_info["connections"] == 0

    def test_mqtt_connections_exception(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(True)
        inst = _inst(True)
        inst.get_client_count = MagicMock(side_effect=RuntimeError("x"))
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=inst),
        ):
            assert manager.get_service_info("mqtt_server").running_info["connections"] == 0

    def test_serial_bridge_stats(self, manager, app_state):
        cfg = MagicMock()
        cfg.serial_bridge = _sec(True)
        stats = SimpleNamespace(running=True, client_count=3, total_connections=7)
        inst = MagicMock(spec=["get_status", "is_running"])
        inst.is_running = True
        inst.get_status = MagicMock(return_value=stats)
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=inst),
        ):
            i = manager.get_service_info("serial_bridge")
        assert i.state == ServiceState.RUNNING
        assert i.running_info["total_connections"] == 7

    def test_serial_bridge_stats_exception(self, manager, app_state):
        cfg = MagicMock()
        cfg.serial_bridge = _sec(True)
        inst = MagicMock(spec=["get_status", "is_running"])
        inst.is_running = True
        inst.get_status = MagicMock(side_effect=RuntimeError("x"))
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=inst),
        ):
            assert manager.get_service_info("serial_bridge").running_info["total_connections"] == 0

    def test_api_only_running(self, manager, app_state):
        cfg = MagicMock()
        cfg.mcp_server = _sec(True)
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[]),
            patch.object(manager, "_get_instance", return_value=None),
        ):
            assert manager.get_service_info("mcp_server").state == ServiceState.RUNNING

    def test_api_only_disabled(self, manager, app_state):
        cfg = MagicMock()
        cfg.grafana = _sec(False)
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_get_instance", return_value=None),
        ):
            assert manager.get_service_info("grafana").state == ServiceState.DISABLED

    def test_no_section(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = None
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[]),
            patch.object(manager, "_get_instance", return_value=None),
        ):
            i = manager.get_service_info("mqtt_server")
        assert i.state == ServiceState.DISABLED
        assert i.current_config == {}


class TestListServices:
    """list_services 方法。"""

    def test_returns_all(self, manager, app_state):
        cfg = MagicMock()
        for n in SERVICE_DEFINITIONS:
            setattr(cfg, SERVICE_DEFINITIONS[n]["config_section"], _sec(False))
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch.object(manager, "check_dependencies", return_value=[]),
            patch.object(manager, "_get_instance", return_value=None),
        ):
            svcs = manager.list_services()
        assert len(svcs) == len(SERVICE_DEFINITIONS)
        assert {s.name for s in svcs} == set(SERVICE_DEFINITIONS.keys())


class TestGetSetInstance:
    """_get_instance / _set_instance 方法。"""

    def test_get_none(self, manager, app_state):
        assert manager._get_instance("mqtt_server") is None

    def test_get_value(self, manager, app_state):
        inst = MagicMock()
        app_state.mqtt_server = inst
        assert manager._get_instance("mqtt_server") is inst

    def test_set_normal(self, manager, app_state):
        inst = MagicMock()
        manager._set_instance("mqtt_server", inst)
        assert app_state.mqtt_server is inst

    def test_set_serial_bridge(self, manager, app_state):
        inst = MagicMock()
        manager._set_instance("serial_bridge", inst)
        assert app_state.serial_bridge is inst

    def test_set_none_clears(self, manager, app_state):
        app_state.mqtt_server = MagicMock()
        manager._set_instance("mqtt_server", None)
        assert app_state.mqtt_server is None


class TestEnableService:
    """enable_service 方法。"""

    async def test_unknown(self, manager):
        r = await manager.enable_service("nope")
        assert r["success"] is False
        assert r["error"] == ServiceErrors.UNKNOWN_SERVICE

    async def test_deps_missing(self, manager):
        with patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", False)]):
            r = await manager.enable_service("mqtt_server")
        assert r["error"] == ServiceErrors.DEPS_MISSING
        assert "a" in r["missing_dependencies"]

    async def test_config_update_failed(self, manager, mock_update_config):
        mock_update_config.side_effect = RuntimeError("db")
        with patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]):
            r = await manager.enable_service("mqtt_server")
        assert r["error"] == ServiceErrors.CONFIG_UPDATE_FAILED

    async def test_start_runtime_error(self, manager, mock_update_config, app_state):
        with (
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_start_service_instance", side_effect=RuntimeError("address already in use")),
        ):
            r = await manager.enable_service("mqtt_server")
        assert r["error"] == ServiceErrors.START_FAILED
        assert r["error_type"] == "runtime"
        assert mock_update_config.call_count == 2
        assert mock_update_config.call_args_list[1].args[1] == {"enabled": False}

    async def test_start_generic_exception(self, manager, mock_update_config, app_state):
        with (
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_start_service_instance", side_effect=ValueError("boom")),
        ):
            r = await manager.enable_service("mqtt_server")
        assert r["error"] == ServiceErrors.START_FAILED
        assert mock_update_config.call_count == 2

    async def test_success_with_config(self, manager, mock_update_config, app_state):
        cv = {"host": "0.0.0.0"}
        with (
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_start_service_instance", new=AsyncMock()) as ms,
        ):
            r = await manager.enable_service("mqtt_server", cv)
        assert r["success"] is True
        assert r["message"] == ServiceErrors.SERVICE_ENABLED_STARTED
        assert mock_update_config.call_args_list[0].args[1] == {"enabled": True, "host": "0.0.0.0"}
        ms.assert_called_once_with("mqtt_server", cv)

    async def test_success_no_config(self, manager, mock_update_config, app_state):
        with (
            patch.object(manager, "check_dependencies", return_value=[DependencyInfo("a", True)]),
            patch.object(manager, "_start_service_instance", new=AsyncMock()),
        ):
            r = await manager.enable_service("mqtt_server")
        assert r["success"] is True


class TestDisableService:
    """disable_service 方法。"""

    async def test_unknown(self, manager):
        r = await manager.disable_service("nope")
        assert r["error"] == ServiceErrors.UNKNOWN_SERVICE

    async def test_no_instance(self, manager, mock_update_config, app_state):
        with patch.object(manager, "_get_instance", return_value=None):
            r = await manager.disable_service("mqtt_server")
        assert r["success"] is True
        assert r["message"] == ServiceErrors.SERVICE_DISABLED
        mock_update_config.assert_called_once_with("mqtt_server", {"enabled": False})

    async def test_with_instance(self, manager, mock_update_config, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=MagicMock()),
            patch.object(manager, "_stop_service_instance", new=AsyncMock()) as ms,
        ):
            r = await manager.disable_service("mqtt_server")
        assert r["success"] is True
        ms.assert_called_once_with("mqtt_server")

    async def test_stop_exception_suppressed(self, manager, mock_update_config, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=MagicMock()),
            patch.object(manager, "_stop_service_instance", side_effect=RuntimeError("x")),
        ):
            r = await manager.disable_service("mqtt_server")
        assert r["success"] is True
        mock_update_config.assert_called_once()

    async def test_config_update_failed(self, manager, mock_update_config, app_state):
        mock_update_config.side_effect = RuntimeError("db")
        with patch.object(manager, "_get_instance", return_value=None):
            r = await manager.disable_service("mqtt_server")
        assert r["error"] == ServiceErrors.CONFIG_UPDATE_FAILED


class TestStartService:
    """start_service 方法。"""

    async def test_unknown(self, manager):
        r = await manager.start_service("nope")
        assert r["error"] == ServiceErrors.UNKNOWN_SERVICE

    async def test_already_running(self, manager, app_state):
        with patch.object(manager, "_get_instance", return_value=_inst(True)):
            r = await manager.start_service("mqtt_server")
        assert r["message"] == ServiceErrors.ALREADY_RUNNING

    async def test_deps_not_met(self, manager, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=None),
            patch.object(manager, "all_dependencies_met", return_value=False),
        ):
            r = await manager.start_service("mqtt_server")
        assert r["error"] == ServiceErrors.DEPS_INSTALL_FAILED

    async def test_runtime_error(self, manager, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=None),
            patch.object(manager, "all_dependencies_met", return_value=True),
            patch.object(manager, "_start_service_instance", side_effect=RuntimeError("address already in use")),
        ):
            r = await manager.start_service("mqtt_server")
        assert r["error"] == ServiceErrors.START_FAILED
        assert r["error_type"] == "runtime"
        assert r["hint"] == "ERR_SVC_HINT_MQTT_PORT_IN_USE"

    async def test_generic_exception(self, manager, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=None),
            patch.object(manager, "all_dependencies_met", return_value=True),
            patch.object(manager, "_start_service_instance", side_effect=ValueError("boom")),
        ):
            r = await manager.start_service("mqtt_server")
        assert r["error"] == ServiceErrors.START_FAILED
        assert "boom" in r["detail"]

    async def test_success(self, manager, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=None),
            patch.object(manager, "all_dependencies_met", return_value=True),
            patch.object(manager, "_start_service_instance", new=AsyncMock()),
        ):
            r = await manager.start_service("mqtt_server")
        assert r["message"] == ServiceErrors.SERVICE_STARTED

    async def test_instance_not_running(self, manager, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=_inst(False)),
            patch.object(manager, "all_dependencies_met", return_value=True),
            patch.object(manager, "_start_service_instance", new=AsyncMock()) as ms,
        ):
            r = await manager.start_service("mqtt_server")
        assert r["success"] is True
        ms.assert_called_once()


class TestStopService:
    """stop_service 方法。"""

    async def test_unknown(self, manager):
        r = await manager.stop_service("nope")
        assert r["error"] == ServiceErrors.UNKNOWN_SERVICE

    async def test_no_instance(self, manager, app_state):
        with patch.object(manager, "_get_instance", return_value=None):
            r = await manager.stop_service("mqtt_server")
        assert r["message"] == ServiceErrors.NOT_RUNNING

    async def test_success(self, manager, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=MagicMock()),
            patch.object(manager, "_stop_service_instance", new=AsyncMock()),
        ):
            r = await manager.stop_service("mqtt_server")
        assert r["message"] == ServiceErrors.SERVICE_STOPPED

    async def test_exception(self, manager, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=MagicMock()),
            patch.object(manager, "_stop_service_instance", side_effect=RuntimeError("boom")),
        ):
            r = await manager.stop_service("mqtt_server")
        assert r["success"] is False
        assert "boom" in r["error"]


class TestUpdateServiceConfig:
    """update_service_config 方法。"""

    async def test_unknown(self, manager):
        r = await manager.update_service_config("nope", {"h": "x"})
        assert r["error"] == ServiceErrors.UNKNOWN_SERVICE

    async def test_config_update_failed(self, manager, mock_update_config, app_state):
        mock_update_config.side_effect = RuntimeError("db")
        r = await manager.update_service_config("mqtt_server", {"h": "x"})
        assert r["error"] == ServiceErrors.CONFIG_UPDATE_FAILED

    async def test_not_running(self, manager, mock_update_config, app_state):
        with patch.object(manager, "_get_instance", return_value=None):
            r = await manager.update_service_config("mqtt_server", {"h": "x"})
        assert r["message"] == ServiceErrors.CONFIG_UPDATED

    async def test_restart_success(self, manager, mock_update_config, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=_inst(True)),
            patch.object(manager, "_stop_service_instance", new=AsyncMock()),
            patch.object(manager, "_start_service_instance", new=AsyncMock()),
        ):
            r = await manager.update_service_config("mqtt_server", {"h": "x"})
        assert r["message"] == ServiceErrors.CONFIG_UPDATED_RESTARTED

    async def test_restart_failure(self, manager, mock_update_config, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=_inst(True)),
            patch.object(manager, "_stop_service_instance", new=AsyncMock()),
            patch.object(manager, "_start_service_instance", side_effect=RuntimeError("address already in use")),
        ):
            r = await manager.update_service_config("mqtt_server", {"h": "x"})
        assert r["error"] == ServiceErrors.CONFIG_UPDATED_RESTART_FAILED
        assert r["hint"] == "ERR_SVC_HINT_MQTT_PORT_IN_USE"

    async def test_instance_not_running(self, manager, mock_update_config, app_state):
        with (
            patch.object(manager, "_get_instance", return_value=_inst(False)),
            patch.object(manager, "_stop_service_instance", new=AsyncMock()) as ms,
        ):
            r = await manager.update_service_config("mqtt_server", {"h": "x"})
        assert r["message"] == ServiceErrors.CONFIG_UPDATED
        ms.assert_not_called()


class TestGetStartErrorHint:
    """_get_start_error_hint 方法。"""

    @pytest.mark.parametrize(
        "svc,err,exp",
        [
            ("mqtt_server", "Address already in use", "ERR_SVC_HINT_MQTT_PORT_IN_USE"),
            ("mqtt_server", "Permission denied", "ERR_SVC_HINT_MQTT_PORT_PERMISSION"),
            ("modbus_slave", "Address already in use", "ERR_SVC_HINT_MODBUS_PORT_IN_USE"),
            ("modbus_slave", "Permission denied", "ERR_SVC_HINT_MODBUS_PORT_PERMISSION"),
            ("modbus_slave", "'break' outside loop", "ERR_SVC_HINT_CODE_SYNTAX_ERROR"),
            ("serial_bridge", "Could not open port", "ERR_SVC_HINT_SERIAL_NOT_FOUND"),
            ("serial_bridge", "Permission denied", "ERR_SVC_HINT_SERIAL_PERMISSION"),
            ("serial_bridge", "File not found", "ERR_SVC_HINT_SERIAL_PATH_NOT_FOUND"),
            ("serial_bridge", "already in use", "ERR_SVC_HINT_SERIAL_NOT_FOUND"),
            ("mcp_server", "address already in use", "ERR_SVC_HINT_PORT_IN_USE"),
            ("mcp_server", "permission denied", "ERR_SVC_HINT_PERMISSION_DENIED"),
            ("mcp_server", "connection refused", "ERR_SVC_HINT_CONNECTION_REFUSED"),
            ("mcp_server", "timeout", "ERR_SVC_HINT_TIMEOUT"),
            ("mcp_server", "unknown error", "ERR_SVC_HINT_CHECK_CONFIG"),
        ],
    )
    def test_hints(self, manager, svc, err, exp):
        assert manager._get_start_error_hint(svc, RuntimeError(err)) == exp


class TestStartServiceInstance:
    """_start_service_instance 方法。"""

    async def test_mcp_noop(self, manager, mock_get_config, app_state):
        await manager._start_service_instance("mcp_server")
        assert manager._get_instance("mcp_server") is None

    async def test_grafana_noop(self, manager, mock_get_config, app_state):
        await manager._start_service_instance("grafana")
        assert manager._get_instance("grafana") is None

    async def test_mqtt(self, manager, app_state):
        cfg = MagicMock()
        cfg.mqtt_server = _sec(True, host="127.0.0.1", port=1888, ws_port=None, username="u", password="p")
        inst = MagicMock()
        inst.start = AsyncMock()
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch("edgelite.engine.mqtt_server.MqttServer", return_value=inst),
        ):
            await manager._start_service_instance("mqtt_server", {"host": "0.0.0.0"})
        sc = inst.start.call_args.args[0]
        assert sc["host"] == "0.0.0.0" and sc["port"] == 1888
        assert manager._get_instance("mqtt_server") is inst

    async def test_modbus(self, manager, app_state):
        cfg = MagicMock()
        cfg.modbus_slave = _sec(True, host="h", port=5020, holding_size=1000, input_size=1000)
        inst = MagicMock()
        inst.start = AsyncMock()
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch("edgelite.engine.modbus_slave.ModbusSlaveServer", return_value=inst),
        ):
            await manager._start_service_instance("modbus_slave", {"port": 5021})
        sc = inst.start.call_args.args[0]
        assert sc["port"] == 5021 and sc["holding_size"] == 1000
        assert manager._get_instance("modbus_slave") is inst

    async def test_serial_bridge(self, manager, app_state):
        cfg = MagicMock()
        cfg.serial_bridge = _sec(
            True, serial_port="/dev/ttyUSB0", baud_rate=9600, tcp_port=9000, ip_whitelist=["1.1.1.1"]
        )
        inst = MagicMock()
        inst.start = AsyncMock()
        with (
            patch("edgelite.services.service_manager.get_config", return_value=cfg),
            patch("edgelite.engine.serial_bridge.SerialTcpBridge", return_value=inst),
        ):
            await manager._start_service_instance("serial_bridge", {"tcp_port": 8888})
        sc = inst.start.call_args.args[0]
        assert sc["serial_port"] == "/dev/ttyUSB0" and sc["baudrate"] == 9600
        assert sc["tcp_port"] == 8888 and sc["allowed_ips"] == ["1.1.1.1"]
        assert manager._get_instance("serial_bridge") is inst


class TestStopServiceInstance:
    """_stop_service_instance 方法。"""

    async def test_no_instance(self, manager, app_state):
        with patch.object(manager, "_get_instance", return_value=None):
            await manager._stop_service_instance("mqtt_server")

    async def test_stop_success(self, manager, app_state):
        inst = MagicMock()
        inst.stop = AsyncMock()
        app_state.mqtt_server = inst
        with patch.object(manager, "_get_instance", return_value=inst):
            await manager._stop_service_instance("mqtt_server")
        inst.stop.assert_called_once()
        assert app_state.mqtt_server is None

    async def test_stop_exception_clears(self, manager, app_state):
        inst = MagicMock()
        inst.stop = AsyncMock(side_effect=RuntimeError("boom"))
        app_state.mqtt_server = inst
        with patch.object(manager, "_get_instance", return_value=inst):
            await manager._stop_service_instance("mqtt_server")
        assert app_state.mqtt_server is None

    async def test_no_stop_method(self, manager, app_state):
        inst = MagicMock(spec=[])
        app_state.mqtt_server = inst
        with patch.object(manager, "_get_instance", return_value=inst):
            await manager._stop_service_instance("mqtt_server")
        assert app_state.mqtt_server is None


class TestGetServiceManager:
    """get_service_manager 单例工厂。"""

    def test_singleton(self):
        import edgelite.services.service_manager as sm

        sm._service_manager = None
        m1 = get_service_manager()
        m2 = get_service_manager()
        assert m1 is m2
        assert isinstance(m1, ServiceManager)
        sm._service_manager = None
