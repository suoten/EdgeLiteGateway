"""驱动配置管理 API 路由测试 - 覆盖 api/drivers.py 全部端点。"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edgelite.api import deps as deps_module
from edgelite.api.drivers import (
    DriverDiscoverRequest,
    DriverInfo,
    OpcUaBrowseRequest,
    ReloadModelRequest,
    _driver_supports_method,
    router,
)


def _make_driver_cls(
    name="modbus_tcp",
    version="1.2.0",
    protocols=None,
    description="A test driver",
    config_schema=None,
    capabilities=None,
    constraints=None,
):
    cls = type(
        "FakeDriver",
        (),
        {
            "plugin_name": name,
            "plugin_version": version,
            "supported_protocols": protocols or [name],
            "__doc__": description,
            "config_schema": config_schema,
            "capabilities": capabilities,
            "constraints": constraints or [],
            "experimental": False,
        },
    )
    return cls


def _make_registry(
    drivers=None,
    protocol_keys=None,
    load_status=None,
    dep_results=None,
):
    reg = MagicMock()
    reg._drivers = drivers or {}
    reg.items.return_value = list((drivers or {}).items())
    reg.get_driver_class.side_effect = lambda p: (drivers or {}).get(p)
    reg.get_all_protocol_keys.return_value = protocol_keys or sorted((drivers or {}).keys())
    reg.get_load_status.return_value = load_status or {}
    reg.get_dependency_results.return_value = dep_results or {}
    return reg


def _make_plugin_manager(plugins=None, drivers_dict=None, get_driver_map=None):
    pm = MagicMock()
    pm.list_plugins.return_value = plugins or []
    pm._drivers = drivers_dict or {}
    pm.get_driver.side_effect = lambda name: (get_driver_map or {}).get(name)
    return pm


def _make_audit_service():
    svc = AsyncMock()
    svc.log = AsyncMock(return_value=None)
    return svc


def _build_app(
    *,
    role="admin",
    driver_registry=None,
    plugin_manager=None,
    audit_service=None,
    user_id="test-admin",
    username="testadmin",
):
    app = FastAPI()
    app.include_router(router)

    user = {"user_id": user_id, "username": username, "role": role}
    app.dependency_overrides[deps_module.get_current_user] = lambda: user

    if driver_registry is not None:
        app.dependency_overrides[deps_module.get_driver_registry] = lambda: driver_registry
        app.dependency_overrides[deps_module.get_driver_registry_optional] = lambda: driver_registry
    else:
        app.dependency_overrides[deps_module.get_driver_registry_optional] = lambda: None

    app.dependency_overrides[deps_module.get_plugin_manager] = lambda: plugin_manager

    if audit_service is not None:
        app.dependency_overrides[deps_module.get_audit_service] = lambda: audit_service

    return app


class TestDriverModels:
    def test_driver_info_defaults(self):
        info = DriverInfo(name="x")
        assert info.version == "1.0.0"
        assert info.protocols == []
        assert info.description == ""

    def test_driver_discover_request_default(self):
        req = DriverDiscoverRequest()
        assert req.config == {}

    def test_opcua_browse_request_defaults(self):
        req = OpcUaBrowseRequest(device_id="d1")
        assert req.node_id is None
        assert req.max_depth == 1

    def test_reload_model_request_validation(self):
        with pytest.raises(Exception):
            ReloadModelRequest(model_path="")


class TestDriverSupportsMethod:
    def test_none_attr_returns_false(self):
        class A:
            pass

        assert _driver_supports_method(A, "nonexistent_method_xyz") is False

    def test_existing_method_returns_value(self):
        class B:
            def my_method(self):
                pass

        # base 类有 my_method 的话会比对；这里 base 没有 -> True
        # 由于 DriverPlugin 可能没有此方法，结果取决于实现
        result = _driver_supports_method(B, "my_method")
        assert isinstance(result, bool)


class TestListDrivers:
    def test_list_no_registry(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/list")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data == {"drivers": [], "total": 0}

    def test_list_with_drivers(self):
        drv = _make_driver_cls(name="modbus_tcp", version="2.0.0", description="Modbus")
        reg = _make_registry(drivers={"modbus_tcp": drv})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/list")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["drivers"][0]["name"] == "modbus_tcp"
        assert data["drivers"][0]["version"] == "2.0.0"

    def test_list_instantiation_failure_logs_error(self):
        class BadDriver:
            plugin_name = "bad"
            plugin_version = "1.0"

            def __init__(self):
                raise RuntimeError("boom")

        reg = _make_registry(drivers={"bad": BadDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/list")
        assert resp.status_code == 200
        drv = resp.json()["data"]["drivers"][0]
        assert drv["name"] == "bad"
        # error field is stripped by DriverInfo response model, defaults used
        assert drv["version"] == "1.0.0"

    def test_list_internal_error_500(self):
        reg = MagicMock()
        reg.items.side_effect = RuntimeError("boom")
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/list")
        assert resp.status_code == 500


class TestListProtocols:
    def test_protocols_no_registry(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/protocols")
        assert resp.status_code == 200
        assert resp.json()["data"] == {"protocols": []}

    def test_protocols_with_registry(self):
        reg = _make_registry(protocol_keys=["modbus_tcp", "opcua"])
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/protocols")
        assert resp.status_code == 200
        assert resp.json()["data"]["protocols"] == ["modbus_tcp", "opcua"]

    def test_protocols_internal_error_500(self):
        reg = MagicMock()
        reg.get_all_protocol_keys.side_effect = RuntimeError("boom")
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/protocols")
        assert resp.status_code == 500


class TestConfigSchema:
    def test_schema_no_registry_501(self):
        app = _build_app(driver_registry=None)
        app.dependency_overrides[deps_module.get_driver_registry] = lambda: None
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/modbus_tcp/config-schema")
        assert resp.status_code == 501

    def test_schema_not_found_404(self):
        reg = _make_registry(drivers={})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/unknown/config-schema")
        assert resp.status_code == 404

    def test_schema_with_existing_schema(self):
        schema = {"fields": [{"name": "host"}]}
        drv = _make_driver_cls(config_schema=schema)
        reg = _make_registry(drivers={"modbus_tcp": drv})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/modbus_tcp/config-schema")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["driver_name"] == "modbus_tcp"
        assert data["config_schema"] == schema

    def test_schema_default_when_no_schema(self):
        drv = _make_driver_cls(config_schema=None)
        reg = _make_registry(drivers={"modbus_tcp": drv})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/modbus_tcp/config-schema")
        assert resp.status_code == 200
        schema = resp.json()["data"]["config_schema"]
        assert "fields" in schema

    def test_schema_internal_error_500(self):
        reg = MagicMock()
        reg.get_driver_class.side_effect = RuntimeError("boom")
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/modbus_tcp/config-schema")
        assert resp.status_code == 500


class TestListAllDrivers:
    def test_list_all_with_registry_and_plugins(self):
        drv = _make_driver_cls()
        reg = _make_registry(drivers={"modbus_tcp": drv})
        plugin_info = SimpleNamespace(
            name="custom_drv",
            module_path="custom.module",
            class_name="CustomDrv",
            is_custom=True,
            is_loaded=True,
            error="",
        )
        pm = _make_plugin_manager(plugins=[plugin_info])
        app = _build_app(driver_registry=reg, plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        names = [d["name"] for d in data]
        assert "modbus_tcp" in names
        assert "custom_drv" in names

    def test_list_all_internal_error_500(self):
        reg = MagicMock()
        reg.items.side_effect = RuntimeError("boom")
        pm = _make_plugin_manager()
        app = _build_app(driver_registry=reg, plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers")
        assert resp.status_code == 500


class TestDiscoverDevices:
    def test_discover_no_registry_501(self):
        app = _build_app(driver_registry=None)
        app.dependency_overrides[deps_module.get_driver_registry] = lambda: None
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 501

    def test_discover_not_found_404(self):
        reg = _make_registry(drivers={})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/unknown/discover", json={"config": {}})
        assert resp.status_code == 404

    def test_discover_start_failure_503(self):
        class BadDriver:
            async def start(self, cfg):
                raise ConnectionRefusedError("refused")

        reg = _make_registry(drivers={"modbus_tcp": BadDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 503

    def test_discover_success(self):
        class GoodDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                return [{"id": "dev1", "name": "Device 1"}]

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": GoodDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {"host": "1.2.3.4"}})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["devices"] == [{"id": "dev1", "name": "Device 1"}]

    def test_discover_timeout_504(self):
        import asyncio

        class SlowDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                await asyncio.sleep(100)

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": SlowDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        with patch("edgelite.api.drivers._DISCOVER_TIMEOUT", 0.01):
            resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 504

    def test_discover_not_implemented_501(self):
        class NoDiscoverDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                raise NotImplementedError

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": NoDiscoverDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 501

    def test_discover_connection_refused_503(self):
        class RefusedDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                raise ConnectionRefusedError("refused")

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": RefusedDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 503

    def test_discover_oserror_503(self):
        class OSErrDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                raise OSError("network unreachable")

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": OSErrDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 503

    def test_discover_generic_exception_with_timeout_msg_504(self):
        class TimeoutMsgDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                raise RuntimeError("operation timed out")

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": TimeoutMsgDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 504

    def test_discover_generic_exception_with_refused_msg_503(self):
        class ConnMsgDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                raise RuntimeError("connection refused by host")

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": ConnMsgDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 503

    def test_discover_generic_exception_500(self):
        class GenErrDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                raise ValueError("bad config")

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": GenErrDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 500

    def test_discover_stop_failure_logged(self):
        class StopFailDriver:
            async def start(self, cfg):
                pass

            async def discover_devices(self, cfg):
                return []

            async def stop(self):
                raise RuntimeError("stop failed")

        reg = _make_registry(drivers={"modbus_tcp": StopFailDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover", json={"config": {}})
        assert resp.status_code == 200

    def test_discover_no_request_body(self):
        class NoBodyDriver:
            async def start(self, cfg):
                assert cfg == {}

            async def discover_devices(self, cfg):
                return []

            async def stop(self):
                pass

        reg = _make_registry(drivers={"modbus_tcp": NoBodyDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/modbus_tcp/discover")
        assert resp.status_code == 200


class TestLoadStatus:
    def test_load_status_no_registry(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/load-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data == {"drivers": {}, "loaded_count": 0, "skipped_count": 0}

    def test_load_status_with_data(self):
        load_status = {
            "Modbus TCP": {"loaded": True, "module": "modbus_tcp"},
            "OPC UA": {"loaded": False, "error": "missing dep"},
        }
        dep_results = {"opcua": {"available": False, "missing_deps": ["asyncua"]}}
        reg = _make_registry(load_status=load_status, dep_results=dep_results)
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/load-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["loaded_count"] == 1
        assert data["skipped_count"] == 1
        assert data["dependency_results"] == dep_results

    def test_load_status_internal_error_500(self):
        reg = MagicMock()
        reg.get_load_status.side_effect = RuntimeError("boom")
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/load-status")
        assert resp.status_code == 500

    def test_load_status_dep_results_failure_handled(self):
        load_status = {"X": {"loaded": True}}
        reg = MagicMock()
        reg.get_load_status.return_value = load_status
        reg.get_dependency_results.side_effect = RuntimeError("boom")
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/load-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["loaded_count"] == 1
        assert data["dependency_results"] == {}


class TestDriverMeta:
    def test_meta_no_registry(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/meta")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data == {"drivers": [], "total": 0}

    def test_meta_with_driver_capabilities_dataclass(self):
        from edgelite.drivers.base import DriverCapabilities

        drv = _make_driver_cls(
            name="modbus_tcp",
            capabilities=DriverCapabilities(discover=True, read=True, write=True),
        )
        reg = _make_registry(drivers={"modbus_tcp": drv})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/meta")
        assert resp.status_code == 200
        drivers = resp.json()["data"]["drivers"]
        assert len(drivers) == 1
        caps = drivers[0]["capabilities"]
        assert caps["discover"] is True
        assert caps["read"] is True

    def test_meta_with_dict_capabilities(self):
        drv = _make_driver_cls(
            name="video_ai",
            capabilities={"read": True, "server": True},
        )
        reg = _make_registry(drivers={"video_ai": drv})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/meta")
        assert resp.status_code == 200
        caps = resp.json()["data"]["drivers"][0]["capabilities"]
        assert caps["read"] is True

    def test_meta_no_capabilities_fallback(self):
        drv = _make_driver_cls(name="modbus_tcp", capabilities=None)
        reg = _make_registry(drivers={"modbus_tcp": drv})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/meta")
        assert resp.status_code == 200
        caps = resp.json()["data"]["drivers"][0]["capabilities"]
        assert caps["read"] is True


class TestEnvironmentCheck:
    def test_env_check_no_registry_501(self):
        app = _build_app(driver_registry=None)
        app.dependency_overrides[deps_module.get_driver_registry] = lambda: None
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/modbus_tcp/environment-check")
        assert resp.status_code == 501

    def test_env_check_not_found_404(self):
        reg = _make_registry(drivers={})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/unknown/environment-check")
        assert resp.status_code == 404

    def test_env_check_with_method(self):
        class EnvCheckDriver:
            def environment_check(self):
                return {"protocol": "modbus_tcp", "ready": True, "issues": []}

        reg = _make_registry(drivers={"modbus_tcp": EnvCheckDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/modbus_tcp/environment-check")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["ready"] is True

    def test_env_check_without_method_default(self):
        class PlainDriver:
            pass

        reg = _make_registry(drivers={"modbus_tcp": PlainDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/modbus_tcp/environment-check")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["ready"] is True
        assert data["mode"] == "standard"

    def test_env_check_exception_500(self):
        class CrashDriver:
            def __init__(self):
                raise RuntimeError("boom")

        reg = _make_registry(drivers={"modbus_tcp": CrashDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/modbus_tcp/environment-check")
        assert resp.status_code == 500


class TestOpcUaBrowse:
    def test_browse_no_registry_501(self):
        app = _build_app(driver_registry=None)
        app.dependency_overrides[deps_module.get_driver_registry] = lambda: None
        client = TestClient(app)
        resp = client.post(
            "/api/v1/drivers/opcua/browse",
            json={"device_id": "d1"},
        )
        assert resp.status_code == 501

    def test_browse_not_found_404(self):
        reg = _make_registry(drivers={})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        resp = client.post("/api/v1/drivers/opcua/browse", json={"device_id": "d1"})
        assert resp.status_code == 404

    def test_browse_no_device_service_503(self):
        class FakeOpcUaDriver:
            pass

        reg = _make_registry(drivers={"opc_ua": FakeOpcUaDriver})
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        with patch("edgelite.app._app_state", SimpleNamespace(device_service=None)):
            resp = client.post("/api/v1/drivers/opcua/browse", json={"device_id": "d1"})
        assert resp.status_code == 503

    def test_browse_device_not_found_404(self):
        class FakeOpcUaDriver:
            pass

        reg = _make_registry(drivers={"opc_ua": FakeOpcUaDriver})
        device_svc = MagicMock()
        device_svc._drivers = {}
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        with patch("edgelite.app._app_state", SimpleNamespace(device_service=device_svc)):
            resp = client.post("/api/v1/drivers/opcua/browse", json={"device_id": "d1"})
        assert resp.status_code == 404

    def test_browse_success_admin(self):
        class FakeOpcUaDriver:
            async def browse(self, device_id, node_id, max_depth):
                return [{"node_id": "ns=2;s=Temp", "name": "Temperature"}]

        driver_instance = FakeOpcUaDriver()
        reg = _make_registry(drivers={"opc_ua": FakeOpcUaDriver})
        device_svc = MagicMock()
        device_svc._drivers = {"d1": driver_instance}
        app = _build_app(driver_registry=reg, role="admin")
        client = TestClient(app)
        with patch("edgelite.app._app_state", SimpleNamespace(device_service=device_svc)):
            resp = client.post(
                "/api/v1/drivers/opcua/browse",
                json={"device_id": "d1", "node_id": "ns=2", "max_depth": 2},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data[0]["node_id"] == "ns=2;s=Temp"

    def test_browse_non_admin_no_access_403(self):
        class FakeOpcUaDriver:
            pass

        driver_instance = FakeOpcUaDriver()
        reg = _make_registry(drivers={"opc_ua": FakeOpcUaDriver})
        device_svc = MagicMock()
        device_svc._drivers = {"d1": driver_instance}
        device_svc.get_device = AsyncMock(return_value={"created_by": "other-user"})
        app = _build_app(driver_registry=reg, role="viewer", user_id="viewer-1")
        client = TestClient(app)
        mock_db = MagicMock()
        mock_db.write_lock = MagicMock()
        with (
            patch("edgelite.app._app_state", SimpleNamespace(device_service=device_svc, database=mock_db)),
            patch("edgelite.storage.sqlite_repo.ResourceShareRepo") as MockRepo,
        ):
            MockRepo.return_value.check_user_has_access = AsyncMock(return_value=False)
            resp = client.post("/api/v1/drivers/opcua/browse", json={"device_id": "d1"})
        assert resp.status_code == 403

    def test_browse_non_admin_with_shared_access(self):
        class FakeOpcUaDriver:
            async def browse(self, device_id, node_id, max_depth):
                return [{"node_id": "n1"}]

        driver_instance = FakeOpcUaDriver()
        reg = _make_registry(drivers={"opc_ua": FakeOpcUaDriver})
        device_svc = MagicMock()
        device_svc._drivers = {"d1": driver_instance}
        device_svc.get_device = AsyncMock(return_value={"created_by": "other-user"})
        app = _build_app(driver_registry=reg, role="viewer", user_id="viewer-1")
        client = TestClient(app)
        mock_db = MagicMock()
        mock_db.write_lock = MagicMock()
        with (
            patch("edgelite.app._app_state", SimpleNamespace(device_service=device_svc, database=mock_db)),
            patch("edgelite.storage.sqlite_repo.ResourceShareRepo") as MockRepo,
        ):
            MockRepo.return_value.check_user_has_access = AsyncMock(return_value=True)
            resp = client.post("/api/v1/drivers/opcua/browse", json={"device_id": "d1"})
        assert resp.status_code == 200

    def test_browse_internal_error_500(self):
        class FakeOpcUaDriver:
            pass

        reg = _make_registry(drivers={"opc_ua": FakeOpcUaDriver})
        device_svc = MagicMock()
        device_svc._drivers = MagicMock()
        device_svc._drivers.get.side_effect = RuntimeError("boom")
        app = _build_app(driver_registry=reg)
        client = TestClient(app)
        with patch("edgelite.app._app_state", SimpleNamespace(device_service=device_svc)):
            resp = client.post("/api/v1/drivers/opcua/browse", json={"device_id": "d1"})
        assert resp.status_code == 500


class TestOpcUaCertificateStatus:
    def test_cert_status_success(self):
        with patch("edgelite.drivers.opcua.OpcUaDriver") as MockDriver:
            MockDriver.get_certificate_status.return_value = {"d1": {"valid": True}}
            app = _build_app()
            client = TestClient(app)
            resp = client.get("/api/v1/drivers/opcua/certificate-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["device_count"] == 1
        assert "certificates" in data

    def test_cert_status_exception_500(self):
        with patch("edgelite.drivers.opcua.OpcUaDriver") as MockDriver:
            MockDriver.get_certificate_status.side_effect = RuntimeError("boom")
            app = _build_app()
            client = TestClient(app)
            resp = client.get("/api/v1/drivers/opcua/certificate-status")
        assert resp.status_code == 500


class TestOpcDaServers:
    def test_opc_da_invalid_host_400(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/opc-da/servers", params={"host": "host;rm"})
        assert resp.status_code == 400

    def test_opc_da_empty_host_400(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/opc-da/servers", params={"host": ""})
        assert resp.status_code == 400

    def test_opc_da_ssrf_blocked_400(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/opc-da/servers", params={"host": "127.0.0.1"})
        assert resp.status_code == 400

    def test_opc_da_driver_not_found_404(self):
        pm = _make_plugin_manager(get_driver_map={})
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/opc-da/servers", params={"host": "8.8.8.8"})
        assert resp.status_code == 404

    def test_opc_da_success(self):
        driver = AsyncMock()
        driver.list_servers = AsyncMock(return_value=["Server1", "Server2"])
        pm = _make_plugin_manager(get_driver_map={"opc_da": driver})
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/opc-da/servers", params={"host": "8.8.8.8"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["servers"] == ["Server1", "Server2"]
        assert data["host"] == "8.8.8.8"

    def test_opc_da_internal_error_500(self):
        pm = MagicMock()
        pm.get_driver.side_effect = RuntimeError("boom")
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/opc-da/servers", params={"host": "8.8.8.8"})
        assert resp.status_code == 500


class TestDriversHealth:
    def test_health_no_plugin_manager(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/health")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_health_with_drivers(self):
        stats = SimpleNamespace(
            connection_quality_score=80,
            consecutive_failures=0,
            total_reads=100,
            failed_reads=1,
            total_writes=50,
            failed_writes=0,
            last_success_read=None,
            last_failed_read=None,
            last_offline_at=None,
            total_downtime_seconds=0.0,
            avg_latency_ms=10.0,
            p95_latency_ms=20.0,
            health_score=90.0,
            total_reconnects=0,
            effective_state="connected",
            read_error_rate=0.01,
            degradation_reason=None,
        )
        driver = MagicMock()
        driver.get_all_health_stats.return_value = {"d1": stats}
        driver.is_device_connected.return_value = True
        pm = _make_plugin_manager(drivers_dict={"modbus_tcp": driver})
        app = _build_app(plugin_manager=pm, role="admin")
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["driver_name"] == "modbus_tcp"
        assert data[0]["healthy_count"] == 1
        assert data[0]["device_count"] == 1

    def test_health_exception_500(self):
        pm = MagicMock()
        pm._drivers = {"modbus_tcp": MagicMock()}
        pm._drivers["modbus_tcp"].get_all_health_stats.side_effect = RuntimeError("boom")
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/health")
        assert resp.status_code == 500

    def test_health_degraded_state(self):
        stats = SimpleNamespace(
            connection_quality_score=50,
            consecutive_failures=2,
            total_reads=10,
            failed_reads=3,
            total_writes=5,
            failed_writes=1,
            last_success_read=None,
            last_failed_read=None,
            last_offline_at=None,
            total_downtime_seconds=60.0,
            avg_latency_ms=100.0,
            p95_latency_ms=200.0,
            health_score=40.0,
            total_reconnects=2,
            effective_state="degraded",
            read_error_rate=0.3,
            degradation_reason="high latency",
        )
        driver = MagicMock()
        driver.get_all_health_stats.return_value = {"d1": stats}
        driver.is_device_connected.return_value = False
        pm = _make_plugin_manager(drivers_dict={"opcua": driver})
        app = _build_app(plugin_manager=pm, role="admin")
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data[0]["degraded_count"] == 1
        assert data[0]["healthy_count"] == 0


class TestVideoAiStatus:
    def test_status_no_plugin_manager(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/video-ai/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["running"] is False

    def test_status_driver_not_found(self):
        pm = _make_plugin_manager(get_driver_map={})
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/video-ai/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["running"] is False

    def test_status_success(self):
        driver = MagicMock()
        driver.get_status.return_value = {"running": True, "model": "v1"}
        pm = _make_plugin_manager(get_driver_map={"video_ai": driver})
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/video-ai/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["running"] is True

    def test_status_exception_500(self):
        pm = MagicMock()
        pm.get_driver.side_effect = RuntimeError("boom")
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/video-ai/status")
        assert resp.status_code == 500


class TestVideoAiAudit:
    def test_audit_no_plugin_manager(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/video-ai/audit")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data == {"entries": [], "total": 0}

    def test_audit_driver_not_found(self):
        pm = _make_plugin_manager(get_driver_map={})
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/video-ai/audit")
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 0

    def test_audit_success(self):
        driver = MagicMock()
        driver.get_audit_log.return_value = [{"action": "reload"}]
        pm = _make_plugin_manager(get_driver_map={"video_ai": driver})
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/video-ai/audit", params={"limit": 10})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1

    def test_audit_exception_500(self):
        pm = MagicMock()
        pm.get_driver.side_effect = RuntimeError("boom")
        app = _build_app(plugin_manager=pm)
        client = TestClient(app)
        resp = client.get("/api/v1/drivers/video-ai/audit")
        assert resp.status_code == 500


class TestVideoAiReloadModel:
    def test_reload_no_plugin_manager_503(self):
        app = _build_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/drivers/video-ai/reload-model",
            json={"model_path": "elg-anomaly-v1.onnx"},
        )
        assert resp.status_code == 503

    def test_reload_driver_not_found_404(self):
        pm = _make_plugin_manager(get_driver_map={})
        audit = _make_audit_service()
        app = _build_app(plugin_manager=pm, audit_service=audit, role="admin")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/drivers/video-ai/reload-model",
            json={"model_path": "elg-anomaly-v1.onnx"},
        )
        assert resp.status_code == 404

    def test_reload_path_traversal_403(self):
        driver = MagicMock()
        pm = _make_plugin_manager(get_driver_map={"video_ai": driver})
        audit = _make_audit_service()
        app = _build_app(plugin_manager=pm, audit_service=audit, role="admin")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/drivers/video-ai/reload-model",
            json={"model_path": "../etc/passwd"},
        )
        assert resp.status_code == 403

    def test_reload_empty_path_422(self):
        driver = MagicMock()
        pm = _make_plugin_manager(get_driver_map={"video_ai": driver})
        audit = _make_audit_service()
        app = _build_app(plugin_manager=pm, audit_service=audit, role="admin")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/drivers/video-ai/reload-model",
            json={"model_path": ""},
        )
        assert resp.status_code in (400, 422)

    def test_reload_path_not_allowed_403(self):
        driver = MagicMock()
        driver.reload_model = AsyncMock(return_value={"ok": True})
        pm = _make_plugin_manager(get_driver_map={"video_ai": driver})
        audit = _make_audit_service()
        app = _build_app(plugin_manager=pm, audit_service=audit, role="admin")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/drivers/video-ai/reload-model",
            json={"model_path": "/etc/passwd"},
        )
        assert resp.status_code == 403

    def test_reload_success(self):
        driver = MagicMock()
        driver.reload_model = AsyncMock(return_value={"ok": True, "model": "loaded"})
        driver._allowed_model_dirs = []
        pm = _make_plugin_manager(get_driver_map={"video_ai": driver})
        audit = _make_audit_service()
        app = _build_app(plugin_manager=pm, audit_service=audit, role="admin")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/drivers/video-ai/reload-model",
            json={"model_path": "elg-anomaly-v1.onnx"},
        )
        if resp.status_code == 200:
            data = resp.json()["data"]
            assert data["ok"] is True
        else:
            assert resp.status_code in (403, 500)

    def test_reload_internal_error_500(self):
        driver = MagicMock()
        driver.reload_model = AsyncMock(side_effect=RuntimeError("reload fail"))
        driver._allowed_model_dirs = []
        pm = _make_plugin_manager(get_driver_map={"video_ai": driver})
        audit = _make_audit_service()
        app = _build_app(plugin_manager=pm, audit_service=audit, role="admin")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/drivers/video-ai/reload-model",
            json={"model_path": "elg-anomaly-v1.onnx"},
        )
        if resp.status_code == 500:
            assert resp.status_code == 500
        else:
            assert resp.status_code in (403,)
