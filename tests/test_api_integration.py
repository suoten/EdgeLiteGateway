"""联调集成API端点测试

覆盖 src/edgelite/api/integration.py 全部端点与 _validate_push_device 校验逻辑:
- POST /api/v1/integration/push-device
- POST /api/v1/integration/handshake
- GET  /api/v1/integration/status
- POST /api/v1/integration/rpc/execute
- GET  /api/v1/integration/rpc/history
- GET  /api/v1/integration/health
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from edgelite.api import integration as integration_module
from edgelite.api.deps import (
    get_audit_service,
    get_current_user,
    get_integration_endpoint,
)
from edgelite.api.integration import (
    PushDeviceRequest,
    _validate_push_device,
    router,
)
from edgelite.engine.integration.backhaul import RpcResult

ADMIN_USER = {"user_id": "test-admin", "username": "testadmin", "role": "admin"}


# -- 辅助构造 -------------------------------------------------------------


def _make_endpoint(device_service=None, backhaul=None, sessions=None, connections=None):
    ep = MagicMock()
    ep._device_service = device_service
    ep._backhaul = backhaul
    ep._sessions = sessions if sessions is not None else {}
    ep._connections = connections if connections is not None else {}
    ep.handle_handshake = AsyncMock(return_value={"session_id": "sess-1", "ok": True})
    return ep


def _build_app(endpoint=None, audit_svc=None, user=ADMIN_USER):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_integration_endpoint] = lambda: endpoint
    app.dependency_overrides[get_audit_service] = lambda: audit_svc
    return app


def _valid_push_payload(**overrides):
    payload = {
        "device_id": "dev-001",
        "name": "test-device",
        "protocol": "modbus_rtu",
        "points": [{"point_name": "p1", "address": "40001", "data_type": "float"}],
        "collect_interval": 5,
    }
    payload.update(overrides)
    return payload


def _rpc_payload(**overrides):
    payload = {
        "method": "write_point",
        "device_id": "dev-001",
        "params": {"point_name": "p1", "value": 1},
        "timeout": 10.0,
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _mock_driver_registry():
    mock_reg = MagicMock()
    mock_reg.get_driver_class.return_value = MagicMock()
    with patch("edgelite.drivers.registry.get_driver_registry", return_value=mock_reg):
        yield


@pytest.fixture
def audit_svc():
    svc = AsyncMock()
    svc.log = AsyncMock(return_value=None)
    return svc


# -- _validate_push_device 直接测试 ----------------------------------------


class TestValidatePushDevice:
    def test_valid_request_no_errors(self):
        req = PushDeviceRequest(**_valid_push_payload())
        assert _validate_push_device(req) == []

    def test_invalid_device_id_uppercase(self):
        req = PushDeviceRequest(**_valid_push_payload(device_id="UPPER"))
        errors = _validate_push_device(req)
        assert any("device_id format invalid" in e for e in errors)

    def test_invalid_device_id_too_short(self):
        req = PushDeviceRequest(**_valid_push_payload(device_id="a"))
        errors = _validate_push_device(req)
        assert any("device_id format invalid" in e for e in errors)

    def test_name_empty(self):
        req = PushDeviceRequest(**_valid_push_payload(name=""))
        errors = _validate_push_device(req)
        assert any("name must be non-empty" in e for e in errors)

    def test_name_too_long(self):
        req = PushDeviceRequest(**_valid_push_payload(name="x" * 65))
        errors = _validate_push_device(req)
        assert any("name must be non-empty" in e for e in errors)

    def test_protocol_empty(self):
        req = PushDeviceRequest(**_valid_push_payload(protocol=""))
        errors = _validate_push_device(req)
        assert any("protocol is required" in e for e in errors)

    def test_protocol_not_registered(self):
        mock_reg = MagicMock()
        mock_reg.get_driver_class.return_value = None
        with patch("edgelite.drivers.registry.get_driver_registry", return_value=mock_reg):
            req = PushDeviceRequest(**_valid_push_payload(protocol="unknown_proto"))
            errors = _validate_push_device(req)
        assert any("not registered" in e for e in errors)

    def test_protocol_video_skips_registry_check(self):
        mock_reg = MagicMock()
        mock_reg.get_driver_class.return_value = None
        with patch("edgelite.drivers.registry.get_driver_registry", return_value=mock_reg):
            req = PushDeviceRequest(**_valid_push_payload(protocol="video"))
            errors = _validate_push_device(req)
        assert not any("not registered" in e for e in errors)

    def test_registry_exception_logged_no_protocol_error(self):
        with patch(
            "edgelite.drivers.registry.get_driver_registry",
            side_effect=RuntimeError("registry boom"),
        ):
            req = PushDeviceRequest(**_valid_push_payload(protocol="modbus_tcp"))
            errors = _validate_push_device(req)
        assert not any("not registered" in e for e in errors)

    def test_empty_points(self):
        req = PushDeviceRequest(**_valid_push_payload(points=[]))
        errors = _validate_push_device(req)
        assert any("points must be a non-empty list" in e for e in errors)

    def test_collect_interval_below_one(self):
        req = PushDeviceRequest(**_valid_push_payload(collect_interval=0))
        errors = _validate_push_device(req)
        assert any("collect_interval must be >= 1" in e for e in errors)

    def test_multiple_errors_collected(self):
        req = PushDeviceRequest(
            device_id="UP",
            name="",
            protocol="",
            points=[],
            collect_interval=0,
        )
        errors = _validate_push_device(req)
        assert len(errors) >= 5


# -- POST /push-device ----------------------------------------------------


class TestPushDevice:
    def test_validation_errors_return_422(self, audit_svc):
        app = _build_app(endpoint=_make_endpoint(), audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/integration/push-device",
            json=_valid_push_payload(device_id="UP"),
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "ERR_DEVICE_CONFIG_INVALID"

    def test_device_service_none_returns_503(self, audit_svc):
        ep = _make_endpoint(device_service=None)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 503
        assert resp.json()["detail"] == "ERR_INTEG_BACKHAUL_NOT_READY"

    def test_success_returns_device(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(return_value={"device_id": "dev-001", "name": "test"})
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 200
        assert resp.json()["data"]["device_id"] == "dev-001"

    def test_value_error_already_exists_409(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(side_effect=ValueError("device already exists"))
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 409
        assert resp.json()["detail"] == "ERR_DEVICE_ALREADY_EXISTS"

    def test_value_error_duplicate_409(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(side_effect=ValueError("duplicate device id"))
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 409

    def test_value_error_unsupported_protocol_422(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(side_effect=ValueError("unsupported protocol xyz"))
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 422
        assert resp.json()["detail"] == "ERR_DEVICE_DRIVER_UNAVAILABLE"

    def test_value_error_driver_start_failed_409(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(side_effect=ValueError("driver start failed: timeout"))
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 409
        assert resp.json()["detail"]["error_code"] == "ERR_DEVICE_CREATE_FAILED"

    def test_value_error_connection_409(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(side_effect=ValueError("connection refused"))
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 409

    def test_value_error_other_422_config_invalid(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(side_effect=ValueError("some weird error"))
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 422
        assert resp.json()["detail"]["error_code"] == "ERR_DEVICE_CONFIG_INVALID"

    def test_http_exception_passthrough(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(side_effect=HTTPException(status_code=404, detail="not found"))
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 404

    def test_generic_exception_500(self, audit_svc):
        ds = AsyncMock()
        ds.create_device = AsyncMock(side_effect=RuntimeError("kaboom"))
        ep = _make_endpoint(device_service=ds)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/push-device", json=_valid_push_payload())
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_DEVICE_CREATE_FAILED"


# -- POST /handshake ------------------------------------------------------


class TestHandshake:
    def test_success(self, audit_svc):
        ep = _make_endpoint()
        ep.handle_handshake = AsyncMock(return_value={"session_id": "s1"})
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post(
            "/api/v1/integration/handshake",
            json={"cloud_url": "http://c", "protocol_version": "1.0"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["session_id"] == "s1"

    def test_http_exception_passthrough(self, audit_svc):
        ep = _make_endpoint()
        ep.handle_handshake = AsyncMock(side_effect=HTTPException(status_code=400, detail="bad"))
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/handshake", json={})
        assert resp.status_code == 400

    def test_generic_exception_500(self, audit_svc):
        ep = _make_endpoint()
        ep.handle_handshake = AsyncMock(side_effect=RuntimeError("boom"))
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/handshake", json={})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_INTEG_HANDSHAKE_FAILED"


# -- GET /status ----------------------------------------------------------


class TestGetIntegrationStatus:
    def test_with_sessions(self, audit_svc):
        ep = _make_endpoint(sessions={"s1": "x", "s2": "y"})
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.get("/api/v1/integration/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["connected"] is True
        assert data["sessions"] == 2
        assert data["session_id"] == "s1"
        assert data["session_ids"] == ["s1", "s2"]

    def test_without_sessions(self, audit_svc):
        ep = _make_endpoint(sessions={})
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.get("/api/v1/integration/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["connected"] is False
        assert data["session_id"] is None
        assert data["sessions"] == 0

    def test_http_exception_passthrough(self, audit_svc):
        ep = _make_endpoint()
        ep._sessions = MagicMock()
        ep._sessions.keys.side_effect = HTTPException(status_code=418, detail="teapot")
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.get("/api/v1/integration/status")
        assert resp.status_code == 418

    def test_generic_exception_500(self, audit_svc):
        ep = _make_endpoint()
        ep._sessions = MagicMock()
        ep._sessions.keys.side_effect = RuntimeError("boom")
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.get("/api/v1/integration/status")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_INTEG_STATUS_FAILED"


# -- POST /rpc/execute ----------------------------------------------------


class TestExecuteRpcCommand:
    def test_success(self, audit_svc):
        bh = AsyncMock()
        bh.handle_rpc_command = AsyncMock(
            return_value=RpcResult(
                command_id="cmd-1",
                success=True,
                result={"v": 1},
                error=None,
                elapsed_ms=5.0,
            )
        )
        ep = _make_endpoint(backhaul=bh)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/rpc/execute", json=_rpc_payload())
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["command_id"] == "cmd-1"
        assert data["success"] is True
        assert data["elapsed_ms"] == 5.0
        audit_svc.log.assert_awaited_once()

    def test_success_audit_failure_logged(self):
        bh = AsyncMock()
        bh.handle_rpc_command = AsyncMock(
            return_value=RpcResult(
                command_id="cmd-2",
                success=True,
                result=None,
                error=None,
                elapsed_ms=3.0,
            )
        )
        ep = _make_endpoint(backhaul=bh)
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        app = _build_app(endpoint=ep, audit_svc=audit)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/rpc/execute", json=_rpc_payload())
        assert resp.status_code == 200
        assert resp.json()["data"]["command_id"] == "cmd-2"

    def test_backhaul_none_503(self, audit_svc):
        ep = _make_endpoint(backhaul=None)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/rpc/execute", json=_rpc_payload())
        assert resp.status_code == 503
        assert resp.json()["detail"] == "ERR_INTEG_BACKHAUL_NOT_READY"

    def test_http_exception_passthrough(self, audit_svc):
        bh = AsyncMock()
        bh.handle_rpc_command = AsyncMock(side_effect=HTTPException(status_code=408, detail="timeout"))
        ep = _make_endpoint(backhaul=bh)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/rpc/execute", json=_rpc_payload())
        assert resp.status_code == 408

    def test_generic_exception_500_with_audit(self, audit_svc):
        bh = AsyncMock()
        bh.handle_rpc_command = AsyncMock(side_effect=RuntimeError("rpc boom"))
        ep = _make_endpoint(backhaul=bh)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/rpc/execute", json=_rpc_payload())
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_INTEG_RPC_EXECUTE_FAILED"
        audit_svc.log.assert_awaited_once()

    def test_generic_exception_audit_also_fails(self):
        bh = AsyncMock()
        bh.handle_rpc_command = AsyncMock(side_effect=RuntimeError("rpc boom"))
        ep = _make_endpoint(backhaul=bh)
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit boom"))
        app = _build_app(endpoint=ep, audit_svc=audit)
        client = TestClient(app)
        resp = client.post("/api/v1/integration/rpc/execute", json=_rpc_payload())
        assert resp.status_code == 500


# -- GET /rpc/history -----------------------------------------------------


class TestGetRpcHistory:
    def test_endpoint_none_returns_empty(self, audit_svc):
        app = _build_app(endpoint=None, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.get("/api/v1/integration/rpc/history")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_backhaul_none_returns_empty(self, audit_svc):
        ep = _make_endpoint(backhaul=None)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.get("/api/v1/integration/rpc/history")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_success_returns_history(self, audit_svc):
        bh = AsyncMock()
        bh.get_rpc_history = AsyncMock(return_value=[{"command_id": "c1"}, {"command_id": "c2"}])
        ep = _make_endpoint(backhaul=bh)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.get("/api/v1/integration/rpc/history?limit=10")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 2
        bh.get_rpc_history.assert_awaited_once_with(limit=10)

    def test_exception_500(self, audit_svc):
        bh = AsyncMock()
        bh.get_rpc_history = AsyncMock(side_effect=RuntimeError("hist boom"))
        ep = _make_endpoint(backhaul=bh)
        app = _build_app(endpoint=ep, audit_svc=audit_svc)
        client = TestClient(app)
        resp = client.get("/api/v1/integration/rpc/history")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_INTEG_RPC_HISTORY_FAILED"


# -- GET /health ----------------------------------------------------------


class TestIntegrationHealthCheck:
    @pytest.fixture
    def health_app(self):
        app = FastAPI()
        app.include_router(router)
        return app

    def _set_endpoint(self, endpoint):
        import edgelite.app as app_mod

        app_mod._app_state.integration_endpoint = endpoint

    def test_unauthenticated_minimal(self, health_app):
        self._set_endpoint(None)
        with patch.object(integration_module, "get_optional_user", new=AsyncMock(return_value=None)):
            client = TestClient(health_app)
            resp = client.get("/api/v1/integration/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "status" in data
        assert "active_sessions" not in data

    def test_unauthenticated_degraded_when_no_endpoint(self, health_app):
        self._set_endpoint(None)
        with patch.object(integration_module, "get_optional_user", new=AsyncMock(return_value=None)):
            client = TestClient(health_app)
            resp = client.get("/api/v1/integration/health")
        assert resp.json()["data"]["status"] == "degraded"

    def test_authenticated_healthy_full_detail(self, health_app):
        bh = MagicMock()
        bh._buffer = [1, 2, 3]
        ep = MagicMock()
        ep._sessions = {"s1": 1}
        ep._connections = {"c1": 1}
        ep._backhaul = bh
        self._set_endpoint(ep)
        with patch.object(
            integration_module,
            "get_optional_user",
            new=AsyncMock(return_value=ADMIN_USER),
        ):
            client = TestClient(health_app)
            resp = client.get("/api/v1/integration/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "healthy"
        assert data["endpoint_initialized"] is True
        assert data["active_sessions"] == 1
        assert data["active_connections"] == 1
        assert data["backhaul_available"] is True
        assert data["buffer_backlog"] == 3
        assert data["rpc_available"] is True

    def test_authenticated_degraded_no_backhaul(self, health_app):
        ep = MagicMock()
        ep._sessions = {}
        ep._connections = {}
        ep._backhaul = None
        self._set_endpoint(ep)
        with patch.object(
            integration_module,
            "get_optional_user",
            new=AsyncMock(return_value=ADMIN_USER),
        ):
            client = TestClient(health_app)
            resp = client.get("/api/v1/integration/health")
        data = resp.json()["data"]
        assert data["status"] == "degraded"
        assert data["backhaul_available"] is False
        assert data["rpc_available"] is False

    def test_authenticated_no_endpoint_degraded(self, health_app):
        self._set_endpoint(None)
        with patch.object(
            integration_module,
            "get_optional_user",
            new=AsyncMock(return_value=ADMIN_USER),
        ):
            client = TestClient(health_app)
            resp = client.get("/api/v1/integration/health")
        data = resp.json()["data"]
        assert data["status"] == "degraded"
        assert data["endpoint_initialized"] is False

    def test_sessions_access_exception_logged(self, health_app):
        ep = MagicMock()
        bad_sessions = MagicMock()
        bad_sessions.__len__ = MagicMock(side_effect=RuntimeError("sessions boom"))
        ep._sessions = bad_sessions
        ep._connections = {}
        ep._backhaul = None
        self._set_endpoint(ep)
        with patch.object(
            integration_module,
            "get_optional_user",
            new=AsyncMock(return_value=ADMIN_USER),
        ):
            client = TestClient(health_app)
            resp = client.get("/api/v1/integration/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["active_sessions"] == 0

    def test_backhaul_buffer_exception_logged(self, health_app):
        bh = MagicMock()
        bad_buffer = MagicMock()
        bad_buffer.__len__ = MagicMock(side_effect=RuntimeError("buffer boom"))
        bh._buffer = bad_buffer
        ep = MagicMock()
        ep._sessions = {}
        ep._connections = {}
        ep._backhaul = bh
        self._set_endpoint(ep)
        with patch.object(
            integration_module,
            "get_optional_user",
            new=AsyncMock(return_value=ADMIN_USER),
        ):
            client = TestClient(health_app)
            resp = client.get("/api/v1/integration/health")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["buffer_backlog"] == 0
        assert data["rpc_available"] is False
