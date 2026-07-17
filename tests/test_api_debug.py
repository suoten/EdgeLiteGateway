"""Comprehensive unit tests for edgelite.api.debug module."""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from edgelite.api.debug import (
    SimulateParams,
    _check_debug_ip_whitelist,
    _get_buffer,
    _get_real_client_ip,
    _normalize_field_type,
    _packet_buffers,
    _simulate_abb_rws,
    _simulate_allen_bradley,
    _simulate_fins,
    _simulate_generic,
    _simulate_http_webhook,
    _simulate_mc,
    _simulate_modbus,
    _simulate_mqtt,
    _simulate_onvif,
    _simulate_opc_da,
    _simulate_opcua,
    _simulate_s7,
    _simulate_serial,
    _simulate_simulator,
    record_packet,
    router,
)
from edgelite.api.error_codes import DebugErrors

# ── Fixtures ──


@pytest.fixture(autouse=True)
def _clear_buffers():
    _packet_buffers.clear()
    yield
    _packet_buffers.clear()


@pytest.fixture
def mock_app_state(monkeypatch):
    state = SimpleNamespace(device_service=None, plugin_manager=None, driver_registry=None, audit_service=None)
    monkeypatch.setattr("edgelite.app._app_state", state)
    return state


@pytest.fixture
def app(mock_app_state, monkeypatch):
    # Bypass IP whitelist for endpoint tests (tested directly in TestCheckIpWhitelist)
    monkeypatch.setattr("edgelite.api.debug._check_debug_ip_whitelist", lambda request: None)
    from edgelite.api.deps import get_current_user

    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_current_user] = lambda: {"user_id": "t", "username": "t", "role": "admin"}
    return application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def allowed_config(monkeypatch):
    """Configure IP whitelist to allow the test client (bypass the check).

    The ``app`` fixture already bypasses ``_check_debug_ip_whitelist``; this
    fixture keeps that bypass in place so endpoint tests reach the handler.
    """
    monkeypatch.setattr("edgelite.api.debug._check_debug_ip_whitelist", lambda request: None)
    return None


@pytest.fixture
def blocked_config(monkeypatch):
    """Configure IP whitelist to block all requests (raise 403).

    Overrides the ``app`` fixture's bypass so the endpoint rejects requests.
    """

    def _block(request):
        raise HTTPException(status_code=403, detail=DebugErrors.IP_NOT_ALLOWED)

    monkeypatch.setattr("edgelite.api.debug._check_debug_ip_whitelist", _block)
    return None


def _ac(app):
    """Create an AsyncClient context manager for ad-hoc app configs."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── _normalize_field_type ──


class TestNormalizeFieldType:
    @pytest.mark.parametrize(
        "inp,exp",
        [
            ("string", "text"),
            ("integer", "number"),
            ("number", "number"),
            ("boolean", "select"),
            ("array", "textarea"),
            ("select", "select"),
        ],
    )
    def test_known(self, inp, exp):
        assert _normalize_field_type(inp) == exp

    def test_unknown(self):
        assert _normalize_field_type("xyz") == "text"


# ── SimulateParams ──


class TestSimulateParams:
    def test_defaults(self):
        p = SimulateParams()
        assert p.function_code is None and p.start_address is None

    def test_valid_fc(self):
        assert SimulateParams(function_code="03").function_code == "03"

    @pytest.mark.parametrize("fc", ["99", "ZZ", "00", "17"])
    def test_invalid_fc(self, fc):
        with pytest.raises(ValidationError):
            SimulateParams(function_code=fc)

    @pytest.mark.parametrize(
        "field,val",
        [
            ("start_address", -1),
            ("start_address", 65536),
            ("quantity", 0),
            ("quantity", 126),
            ("slave_id", 0),
            ("slave_id", 248),
            ("qos", 3),
            ("qos", -1),
        ],
    )
    def test_bounds(self, field, val):
        with pytest.raises(ValidationError):
            SimulateParams(**{field: val})

    def test_extra_allowed(self):
        p = SimulateParams(custom="x")
        assert p.model_dump()["custom"] == "x"

    def test_write_value_types(self):
        SimulateParams(write_value=42)
        SimulateParams(write_value="s")
        SimulateParams(write_value=3.14)


# ── _get_real_client_ip ──


class TestGetRealClientIp:
    def _req(self, host, headers=None):
        r = MagicMock()
        r.client = SimpleNamespace(host=host) if host else None
        r.headers = headers or {}
        return r

    def _cfg(self, monkeypatch, proxies=None, allowed=None):
        cfg = SimpleNamespace(server=SimpleNamespace(trusted_proxies=proxies or [], debug_api_allowed_ips=allowed))
        monkeypatch.setattr("edgelite.config.get_config", lambda: cfg)

    def test_no_proxy(self, monkeypatch):
        self._cfg(monkeypatch)
        assert _get_real_client_ip(self._req("1.2.3.4")) == "1.2.3.4"

    def test_client_none(self, monkeypatch):
        self._cfg(monkeypatch)
        assert _get_real_client_ip(self._req(None)) is None

    def test_trusted_exact(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["10.0.0.1"])
        assert _get_real_client_ip(self._req("10.0.0.1", {"X-Forwarded-For": "203.0.113.5"})) == "203.0.113.5"

    def test_trusted_cidr(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["10.0.0.0/8"])
        assert _get_real_client_ip(self._req("10.1.2.3", {"X-Forwarded-For": "192.0.2.1"})) == "192.0.2.1"

    def test_untrusted(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["10.0.0.1"])
        assert _get_real_client_ip(self._req("6.6.6.6", {"X-Forwarded-For": "203.0.113.5"})) == "6.6.6.6"

    def test_xff_invalid_fallback_real_ip(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["10.0.0.1"])
        r = self._req("10.0.0.1", {"X-Forwarded-For": "bad", "X-Real-IP": "198.51.100.2"})
        assert _get_real_client_ip(r) == "198.51.100.2"

    def test_xff_multi(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["10.0.0.1"])
        r = self._req("10.0.0.1", {"X-Forwarded-For": "203.0.113.5, 10.0.0.1"})
        assert _get_real_client_ip(r) == "203.0.113.5"

    def test_no_headers(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["10.0.0.1"])
        assert _get_real_client_ip(self._req("10.0.0.1")) == "10.0.0.1"

    def test_config_exc(self, monkeypatch):
        monkeypatch.setattr("edgelite.config.get_config", lambda: (_ for _ in ()).throw(RuntimeError()))
        assert _get_real_client_ip(self._req("1.2.3.4")) == "1.2.3.4"

    def test_proxy_whitespace(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["  10.0.0.1  "])
        assert _get_real_client_ip(self._req("10.0.0.1", {"X-Forwarded-For": "203.0.113.5"})) == "203.0.113.5"

    def test_empty_proxy_entry(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["", "10.0.0.1"])
        assert _get_real_client_ip(self._req("10.0.0.1", {"X-Forwarded-For": "203.0.113.5"})) == "203.0.113.5"

    def test_invalid_cidr(self, monkeypatch):
        self._cfg(monkeypatch, proxies=["bad", "10.0.0.1"])
        assert _get_real_client_ip(self._req("10.0.0.1", {"X-Forwarded-For": "203.0.113.5"})) == "203.0.113.5"


# ── _check_debug_ip_whitelist ──


class TestCheckIpWhitelist:
    def _setup(self, monkeypatch, allowed):
        cfg = SimpleNamespace(server=SimpleNamespace(debug_api_allowed_ips=allowed, trusted_proxies=[]))
        monkeypatch.setattr("edgelite.config.get_config", lambda: cfg)
        r = MagicMock()
        r.client = SimpleNamespace(host="1.2.3.4")
        r.headers = {}
        return r

    def test_allowed(self, monkeypatch):
        _check_debug_ip_whitelist(self._setup(monkeypatch, ["1.2.3.4"]))

    def test_empty_rejects(self, monkeypatch):
        with pytest.raises(HTTPException) as e:
            _check_debug_ip_whitelist(self._setup(monkeypatch, []))
        assert e.value.status_code == 403 and e.value.detail == DebugErrors.IP_NOT_ALLOWED

    def test_none_rejects(self, monkeypatch):
        with pytest.raises(HTTPException) as e:
            _check_debug_ip_whitelist(self._setup(monkeypatch, None))
        assert e.value.status_code == 403

    def test_disallowed(self, monkeypatch):
        with pytest.raises(HTTPException) as e:
            _check_debug_ip_whitelist(self._setup(monkeypatch, ["9.9.9.9"]))
        assert e.value.status_code == 403


# ── Packet buffer & record_packet ──


class TestPacketBuffer:
    def test_get_creates(self):
        b = _get_buffer("mb")
        assert b is _packet_buffers["mb"]

    def test_get_default(self):
        assert _get_buffer() is _packet_buffers["__all__"]

    def test_reuses(self):
        assert _get_buffer("x") is _get_buffer("x")

    def test_record_str(self):
        record_packet("tx", "mb", "d1", "ABC")
        p = _get_buffer("mb")[0]
        assert p["content"] == "ABC" and p["content_type"] == "ascii" and p["seq"] >= 1

    def test_record_bytes(self):
        record_packet("rx", "opc", "d2", b"\x01\x02")
        p = _get_buffer("opc")[0]
        assert p["content"] == "0102" and p["content_type"] == "hex"

    def test_record_metadata(self):
        record_packet("tx", "s7", "d3", "x", metadata={"k": "v"})
        assert _get_buffer("s7")[0]["metadata"] == {"k": "v"}

    def test_record_no_metadata(self):
        record_packet("tx", "s7", "d3", "x")
        assert _get_buffer("s7")[0]["metadata"] == {}

    def test_record_all_buffer(self):
        record_packet("tx", "mb", "d1", "x")
        assert len(_get_buffer("__all__")) == 1

    def test_seq_monotonic(self):
        record_packet("tx", "p", "d", "a")
        record_packet("tx", "p", "d", "b")
        assert _get_buffer("p")[0]["seq"] < _get_buffer("p")[1]["seq"]

    def test_bounded(self):
        from edgelite.api.debug import _MAX_PACKET_BUFFER

        for i in range(_MAX_PACKET_BUFFER + 50):
            record_packet("tx", "bd", "d", f"m{i}")
        assert len(_get_buffer("bd")) == _MAX_PACKET_BUFFER


# ── list_debug_protocols endpoint ──


class TestProtocolsEndpoint:
    async def test_list(self, client):
        r = await client.get("/api/v1/debug/protocols")
        assert r.status_code == 200
        protos = r.json()["data"]["protocols"]
        assert len(protos) > 0
        assert "modbus_tcp" in {p["key"] for p in protos}
        for p in protos:
            assert "schema" in p and "fields" in p["schema"]

    async def test_blocked(self, app, blocked_config, mock_app_state):
        async with _ac(app) as c:
            assert (await c.get("/api/v1/debug/protocols")).status_code == 403


# ── packets endpoints ──


class TestPacketsEndpoints:
    async def test_empty(self, client):
        r = await client.get("/api/v1/debug/packets")
        assert r.json()["data"]["total"] == 0

    async def test_with_data(self, client):
        record_packet("tx", "mb", "d1", "A")
        record_packet("rx", "mb", "d1", "B")
        assert (await client.get("/api/v1/debug/packets")).json()["data"]["total"] == 2

    async def test_by_protocol(self, client):
        record_packet("tx", "mb", "d1", "A")
        record_packet("tx", "opc", "d2", "B")
        assert (await client.get("/api/v1/debug/packets", params={"protocol": "mb"})).json()["data"]["total"] == 1

    async def test_by_device(self, client):
        record_packet("tx", "mb", "d1", "A")
        record_packet("rx", "mb", "d2", "B")
        assert (await client.get("/api/v1/debug/packets", params={"device_id": "d1"})).json()["data"]["total"] == 1

    async def test_limit(self, client):
        for i in range(10):
            record_packet("tx", "mb", "d1", f"m{i}")
        assert (await client.get("/api/v1/debug/packets", params={"limit": 3})).json()["data"]["total"] == 3

    async def test_limit_invalid(self, client):
        assert (await client.get("/api/v1/debug/packets", params={"limit": 0})).status_code == 422
        assert (await client.get("/api/v1/debug/packets", params={"limit": 1001})).status_code == 422

    async def test_clear_all(self, client):
        record_packet("tx", "mb", "d1", "A")
        record_packet("rx", "mb", "d1", "B")
        assert (await client.delete("/api/v1/debug/packets")).json()["data"]["cleared"] == 2
        assert (await client.get("/api/v1/debug/packets")).json()["data"]["total"] == 0

    async def test_clear_protocol(self, client):
        record_packet("tx", "mb", "d1", "A")
        record_packet("tx", "opc", "d2", "B")
        assert (await client.delete("/api/v1/debug/packets", params={"protocol": "mb"})).json()["data"]["cleared"] == 1

    async def test_clear_nonexistent(self, client):
        assert (await client.delete("/api/v1/debug/packets", params={"protocol": "nope"})).json()["data"][
            "cleared"
        ] == 0


# ── list_debug_devices endpoint ──


class TestDevicesEndpoint:
    async def test_no_service(self, app, allowed_config, monkeypatch):
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=None))
        async with _ac(app) as c:
            assert (await c.get("/api/v1/debug/devices")).status_code == 503

    async def test_no_attr(self, app, allowed_config, monkeypatch):
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace())
        async with _ac(app) as c:
            assert (await c.get("/api/v1/debug/devices")).status_code == 503

    async def test_success(self, app, allowed_config, monkeypatch):
        svc = AsyncMock()
        svc.list_devices = AsyncMock(
            return_value=(
                [
                    {"device_id": "d1", "name": "D1", "protocol": "modbus_tcp", "status": "online"},
                    {"device_id": "d2", "name": "D2", "protocol": "opcua", "status": "offline"},
                ],
                2,
            )
        )
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=svc))
        async with _ac(app) as c:
            r = await c.get("/api/v1/debug/devices")
            body = r.json()
            assert len(body["data"]["devices"]) == 2

    async def test_protocol_filter(self, app, allowed_config, monkeypatch):
        svc = AsyncMock()
        svc.list_devices = AsyncMock(
            return_value=(
                [
                    {"device_id": "d1", "name": "D1", "protocol": "modbus_tcp", "status": "online"},
                    {"device_id": "d2", "name": "D2", "protocol": "opcua", "status": "offline"},
                ],
                2,
            )
        )
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=svc))
        async with _ac(app) as c:
            r = await c.get("/api/v1/debug/devices", params={"protocol": "opcua"})
            devs = r.json()["data"]["devices"]
            assert len(devs) == 1 and devs[0]["protocol"] == "opcua"

    async def test_exception(self, app, allowed_config, monkeypatch):
        svc = AsyncMock()
        svc.list_devices = AsyncMock(side_effect=RuntimeError("db"))
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=svc))
        async with _ac(app) as c:
            assert (await c.get("/api/v1/debug/devices")).status_code == 500


# ── debug_read endpoint ──


class TestDebugReadEndpoint:
    async def test_no_pm(self, app, allowed_config, monkeypatch):
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=None))
        async with _ac(app) as c:
            assert (
                await c.post("/api/v1/debug/read", params={"protocol": "modbus_tcp", "device_id": "d1"})
            ).status_code == 503

    async def test_no_driver(self, app, allowed_config, monkeypatch):
        pm = MagicMock()
        pm.get_driver.return_value = None
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=pm))
        async with _ac(app) as c:
            assert (
                await c.post("/api/v1/debug/read", params={"protocol": "modbus_tcp", "device_id": "d1"})
            ).status_code == 404

    async def test_success(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.read_points = AsyncMock(return_value={"p1": 42})
        pm = MagicMock()
        pm.get_driver.return_value = drv
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=pm))
        async with _ac(app) as c:
            r = await c.post(
                "/api/v1/debug/read", params={"protocol": "modbus_tcp", "device_id": "d1", "points": ["p1"]}
            )
            assert r.json()["data"]["values"] == {"p1": 42}

    async def test_failure(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.read_points = AsyncMock(side_effect=RuntimeError("fail"))
        pm = MagicMock()
        pm.get_driver.return_value = drv
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=pm))
        async with _ac(app) as c:
            assert (
                await c.post("/api/v1/debug/read", params={"protocol": "modbus_tcp", "device_id": "d1"})
            ).status_code == 500


# ── debug_write endpoint ──


class TestDebugWriteEndpoint:
    async def test_no_pm(self, app, allowed_config, monkeypatch):
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=None))
        async with _ac(app) as c:
            assert (
                await c.post(
                    "/api/v1/debug/write", params={"protocol": "mb", "device_id": "d1", "point": "p1", "value": "1"}
                )
            ).status_code == 503

    async def test_no_driver(self, app, allowed_config, monkeypatch):
        pm = MagicMock()
        pm.get_driver.return_value = None
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=pm))
        async with _ac(app) as c:
            assert (
                await c.post(
                    "/api/v1/debug/write", params={"protocol": "mb", "device_id": "d1", "point": "p1", "value": "1"}
                )
            ).status_code == 404

    async def test_via_device_service(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        svc = AsyncMock()
        svc._driver_instances = {"d1": drv}
        svc.write_point = AsyncMock(return_value=True)
        pm = MagicMock()
        pm.get_driver.return_value = drv
        monkeypatch.setattr(
            "edgelite.app._app_state",
            SimpleNamespace(plugin_manager=pm, device_service=svc, audit_service=AsyncMock(log=AsyncMock())),
        )
        async with _ac(app) as c:
            r = await c.post(
                "/api/v1/debug/write", params={"protocol": "mb", "device_id": "d1", "point": "p1", "value": "42"}
            )
            assert r.json()["data"]["success"] is True

    async def test_via_driver_direct(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.write_point = AsyncMock(return_value=True)
        drv.check_write_allowed = MagicMock(return_value=True)
        svc = AsyncMock()
        svc._driver_instances = {}
        pm = MagicMock()
        pm.get_driver.return_value = drv
        monkeypatch.setattr(
            "edgelite.app._app_state", SimpleNamespace(plugin_manager=pm, device_service=svc, audit_service=None)
        )
        async with _ac(app) as c:
            r = await c.post(
                "/api/v1/debug/write", params={"protocol": "mb", "device_id": "d1", "point": "p1", "value": "42"}
            )
            assert r.json()["data"]["success"] is True

    async def test_not_allowed(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.check_write_allowed = MagicMock(return_value=False)
        svc = AsyncMock()
        svc._driver_instances = {}
        pm = MagicMock()
        pm.get_driver.return_value = drv
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=pm, device_service=svc))
        async with _ac(app) as c:
            assert (
                await c.post(
                    "/api/v1/debug/write", params={"protocol": "mb", "device_id": "d1", "point": "p1", "value": "1"}
                )
            ).status_code == 403

    async def test_check_raises(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.check_write_allowed = MagicMock(side_effect=RuntimeError("e"))
        svc = AsyncMock()
        svc._driver_instances = {}
        pm = MagicMock()
        pm.get_driver.return_value = drv
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=pm, device_service=svc))
        async with _ac(app) as c:
            assert (
                await c.post(
                    "/api/v1/debug/write", params={"protocol": "mb", "device_id": "d1", "point": "p1", "value": "1"}
                )
            ).status_code == 403

    async def test_write_fail(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.check_write_allowed = MagicMock(return_value=True)
        drv.write_point = AsyncMock(side_effect=RuntimeError("e"))
        svc = AsyncMock()
        svc._driver_instances = {}
        pm = MagicMock()
        pm.get_driver.return_value = drv
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(plugin_manager=pm, device_service=svc))
        async with _ac(app) as c:
            assert (
                await c.post(
                    "/api/v1/debug/write", params={"protocol": "mb", "device_id": "d1", "point": "p1", "value": "1"}
                )
            ).status_code == 500

    async def test_async_set_role(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.check_write_allowed = MagicMock(return_value=True)
        drv.set_user_role = AsyncMock()
        drv.write_point = AsyncMock(return_value=True)
        svc = AsyncMock()
        svc._driver_instances = {}
        pm = MagicMock()
        pm.get_driver.return_value = drv
        monkeypatch.setattr(
            "edgelite.app._app_state", SimpleNamespace(plugin_manager=pm, device_service=svc, audit_service=None)
        )
        async with _ac(app) as c:
            assert (
                await c.post(
                    "/api/v1/debug/write", params={"protocol": "mb", "device_id": "d1", "point": "p1", "value": "1"}
                )
            ).status_code == 200


# ── simulate_signal endpoint ──


class TestSimulateEndpoint:
    async def test_blocked(self, app, blocked_config, mock_app_state):
        async with _ac(app) as c:
            assert (
                await c.post("/api/v1/debug/simulate", params={"protocol": "modbus_tcp", "device_id": "d1"})
            ).status_code == 403

    async def test_no_service(self, app, allowed_config, monkeypatch):
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=None))
        async with _ac(app) as c:
            assert (
                await c.post("/api/v1/debug/simulate", params={"protocol": "modbus_tcp", "device_id": "d1"})
            ).status_code == 503

    async def test_device_not_found(self, app, allowed_config, monkeypatch):
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value=None)
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=svc))
        async with _ac(app) as c:
            assert (
                await c.post("/api/v1/debug/simulate", params={"protocol": "modbus_tcp", "device_id": "d1"})
            ).status_code == 404

    async def test_device_exc(self, app, allowed_config, monkeypatch):
        svc = AsyncMock()
        svc.get_device = AsyncMock(side_effect=RuntimeError("nf"))
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=svc))
        async with _ac(app) as c:
            assert (
                await c.post("/api/v1/debug/simulate", params={"protocol": "modbus_tcp", "device_id": "d1"})
            ).status_code == 404

    async def test_modbus_read(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.read = AsyncMock(return_value=[1, 2, 3])
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value={"device_id": "d1", "protocol": "modbus_tcp", "config": {}})
        monkeypatch.setattr(
            "edgelite.app._app_state",
            SimpleNamespace(device_service=svc, driver_registry={"modbus_tcp": lambda c: drv}),
        )
        async with _ac(app) as c:
            r = await c.post(
                "/api/v1/debug/simulate",
                params={"protocol": "modbus_tcp", "device_id": "d1", "operation": "read"},
                json={"function_code": "03", "start_address": 0, "quantity": 3},
            )
            assert r.json()["data"]["values"] == [1, 2, 3]

    async def test_modbus_write(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.write = AsyncMock()
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value={"device_id": "d1", "protocol": "modbus_tcp", "config": {}})
        monkeypatch.setattr(
            "edgelite.app._app_state",
            SimpleNamespace(device_service=svc, driver_registry={"modbus_tcp": lambda c: drv}),
        )
        async with _ac(app) as c:
            r = await c.post(
                "/api/v1/debug/simulate",
                params={"protocol": "modbus_tcp", "device_id": "d1", "operation": "write"},
                json={"function_code": "06", "start_address": 10, "write_value": 99},
            )
            assert r.json()["data"]["values"] == {"written": 99}

    async def test_mqtt(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.publish = AsyncMock()
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value={"device_id": "d1", "protocol": "mqtt_client", "config": {}})
        monkeypatch.setattr(
            "edgelite.app._app_state",
            SimpleNamespace(device_service=svc, driver_registry={"mqtt_client": lambda c: drv}),
        )
        async with _ac(app) as c:
            r = await c.post(
                "/api/v1/debug/simulate",
                params={"protocol": "mqtt", "device_id": "d1"},
                json={"topic": "t", "payload": "p", "qos": 1},
            )
            assert r.json()["data"]["values"] == {"published": True}

    async def test_http(self, app, allowed_config, monkeypatch):
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value={"device_id": "d1", "protocol": "http_webhook", "config": {}})
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=svc, driver_registry={}))
        async with _ac(app) as c:
            r = await c.post(
                "/api/v1/debug/simulate",
                params={"protocol": "http", "device_id": "d1"},
                json={"method": "POST", "url": "http://x", "body": "{}"},
            )
            assert r.json()["data"]["message"] is not None

    async def test_records_packets(self, app, allowed_config, monkeypatch):
        drv = AsyncMock()
        drv.read = AsyncMock(return_value=[1])
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value={"device_id": "d1", "protocol": "modbus_tcp", "config": {}})
        monkeypatch.setattr(
            "edgelite.app._app_state",
            SimpleNamespace(device_service=svc, driver_registry={"modbus_tcp": lambda c: drv}),
        )
        async with _ac(app) as c:
            await c.post(
                "/api/v1/debug/simulate",
                params={"protocol": "modbus_tcp", "device_id": "d1"},
                json={"function_code": "03", "start_address": 0, "quantity": 1},
            )
        assert len(_get_buffer("__all__")) >= 2

    async def test_elapsed_ms(self, app, allowed_config, monkeypatch):
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value={"device_id": "d1", "protocol": "http_webhook", "config": {}})
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=svc, driver_registry={}))
        async with _ac(app) as c:
            r = await c.post("/api/v1/debug/simulate", params={"protocol": "http", "device_id": "d1"})
            assert r.json()["data"]["elapsed_ms"] >= 0

    async def test_op_test_becomes_connect(self, app, allowed_config, monkeypatch):
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value={"device_id": "d1", "protocol": "http_webhook", "config": {}})
        monkeypatch.setattr("edgelite.app._app_state", SimpleNamespace(device_service=svc, driver_registry={}))
        async with _ac(app) as c:
            r = await c.post(
                "/api/v1/debug/simulate", params={"protocol": "http", "device_id": "d1", "operation": "test"}
            )
            assert r.json()["data"]["operation"] == "connect"


# ── _simulate_modbus / opcua / mqtt / s7 (direct) ──


class TestSimulateDrivers:
    async def test_modbus_read(self):
        d = AsyncMock()
        d.read = AsyncMock(return_value=[10, 20])
        c = SimpleNamespace(driver_registry={"modbus_tcp": lambda cfg: d})
        r = await _simulate_modbus(c, {"protocol": "modbus_tcp"}, "read", {"start_address": 0, "quantity": 2})
        assert r["values"] == [10, 20]

    async def test_modbus_write(self):
        d = AsyncMock()
        d.write = AsyncMock()
        c = SimpleNamespace(driver_registry={"modbus_tcp": lambda cfg: d})
        r = await _simulate_modbus(
            c, {"protocol": "modbus_tcp"}, "write", {"function_code": "06", "start_address": 5, "write_value": 42}
        )
        assert r["values"] == {"written": 42}

    async def test_modbus_write_no_value(self):
        d = AsyncMock()
        c = SimpleNamespace(driver_registry={"modbus_tcp": lambda cfg: d})
        r = await _simulate_modbus(c, {"protocol": "modbus_tcp"}, "write", {"function_code": "06", "start_address": 5})
        assert "write_value required" in r["error"]

    async def test_modbus_write_exc(self):
        d = AsyncMock()
        d.write = AsyncMock(side_effect=RuntimeError("e"))
        c = SimpleNamespace(driver_registry={"modbus_tcp": lambda cfg: d})
        assert (
            await _simulate_modbus(c, {"protocol": "modbus_tcp"}, "write", {"function_code": "06", "write_value": 1})
        )["error"] == "simulate_failed"

    async def test_modbus_read_exc(self):
        d = AsyncMock()
        d.read = AsyncMock(side_effect=RuntimeError("e"))
        c = SimpleNamespace(driver_registry={"modbus_tcp": lambda cfg: d})
        assert (await _simulate_modbus(c, {"protocol": "modbus_tcp"}, "read", {}))["error"] == "simulate_failed"

    async def test_modbus_no_driver(self):
        assert "not available" in (await _simulate_modbus(SimpleNamespace(driver_registry={}), {}, "read", {}))["error"]

    async def test_modbus_no_registry(self):
        assert "not available" in (await _simulate_modbus(SimpleNamespace(), {}, "read", {}))["error"]

    async def test_modbus_fc_triggers_write(self):
        d = AsyncMock()
        d.write = AsyncMock()
        c = SimpleNamespace(driver_registry={"modbus_tcp": lambda cfg: d})
        r = await _simulate_modbus(c, {"protocol": "modbus_tcp"}, "read", {"function_code": "05", "write_value": 1})
        assert r["values"] == {"written": 1}

    async def test_opcua_read(self):
        d = AsyncMock()
        d.read = AsyncMock(return_value=42)
        c = SimpleNamespace(driver_registry={"opcua": lambda cfg: d})
        assert (await _simulate_opcua(c, {}, "read", {}))["values"] == 42

    async def test_opcua_write(self):
        d = AsyncMock()
        d.write = AsyncMock()
        c = SimpleNamespace(driver_registry={"opcua": lambda cfg: d})
        assert (await _simulate_opcua(c, {}, "write", {"write_value": 100}))["values"] == {"written": 100}

    async def test_opcua_browse(self):
        d = AsyncMock()
        d.browse = AsyncMock(return_value=["n1", "n2"])
        c = SimpleNamespace(driver_registry={"opcua": lambda cfg: d})
        assert len((await _simulate_opcua(c, {}, "browse", {}))["values"]) == 2

    async def test_opcua_browse_single(self):
        d = AsyncMock()
        d.browse = AsyncMock(return_value="x")
        c = SimpleNamespace(driver_registry={"opcua": lambda cfg: d})
        assert "1 items" in (await _simulate_opcua(c, {}, "browse", {}))["response_raw"]

    async def test_opcua_exc(self):
        d = AsyncMock()
        d.read = AsyncMock(side_effect=RuntimeError("e"))
        c = SimpleNamespace(driver_registry={"opcua": lambda cfg: d})
        assert (await _simulate_opcua(c, {}, "read", {}))["error"] == "simulate_failed"

    async def test_opcua_no_driver(self):
        assert "not available" in (await _simulate_opcua(SimpleNamespace(driver_registry={}), {}, "read", {}))["error"]

    async def test_mqtt_publish(self):
        d = AsyncMock()
        d.publish = AsyncMock()
        c = SimpleNamespace(driver_registry={"mqtt_client": lambda cfg: d})
        assert (await _simulate_mqtt(c, {}, "publish", {}))["values"] == {"published": True}

    async def test_mqtt_no_driver(self):
        assert (
            "not available" in (await _simulate_mqtt(SimpleNamespace(driver_registry={}), {}, "publish", {}))["error"]
        )

    async def test_mqtt_no_publish(self):
        d = AsyncMock(spec=[])
        c = SimpleNamespace(driver_registry={"mqtt_client": lambda cfg: d})
        assert "not available" in (await _simulate_mqtt(c, {}, "publish", {}))["error"]

    async def test_mqtt_exc(self):
        d = AsyncMock()
        d.publish = AsyncMock(side_effect=RuntimeError("e"))
        c = SimpleNamespace(driver_registry={"mqtt_client": lambda cfg: d})
        assert (await _simulate_mqtt(c, {}, "publish", {}))["error"] == "simulate_failed"

    async def test_s7_read(self):
        d = AsyncMock()
        d.read = AsyncMock(return_value=[1, 2])
        c = SimpleNamespace(driver_registry={"s7": lambda cfg: d})
        assert (await _simulate_s7(c, {}, "read", {}))["values"] == [1, 2]

    async def test_s7_no_driver(self):
        assert "not available" in (await _simulate_s7(SimpleNamespace(driver_registry={}), {}, "read", {}))["error"]

    async def test_s7_exc(self):
        d = AsyncMock()
        d.read = AsyncMock(side_effect=RuntimeError("e"))
        c = SimpleNamespace(driver_registry={"s7": lambda cfg: d})
        assert (await _simulate_s7(c, {}, "read", {}))["error"] == "simulate_failed"


# ── _simulate_mc / fins / allen_bradley / abb_rws (pure, no driver) ──


class TestSimulatePureProtocols:
    @pytest.mark.parametrize(
        "op,params,check",
        [
            ("read", {"device_type": "D", "address": 0, "count": 3}, lambda r: len(r["data"]["values"]) == 3),
            ("read_bit", {"count": 2}, lambda r: "MC Read" in r["message"]),
            ("write", {"write_value": 55}, lambda r: r["data"]["value"] == 55),
            ("other", {}, lambda r: "MC other" in r["message"]),
        ],
    )
    async def test_mc(self, op, params, check):
        assert check(await _simulate_mc(None, {}, op, params))

    @pytest.mark.parametrize(
        "op,params,check",
        [
            ("read", {"count": 2}, lambda r: "FINS Memory Read" in r["message"]),
            ("write", {"write_value": 99}, lambda r: "FINS Memory Write" in r["message"]),
            ("fill", {}, lambda r: "FINS Memory Fill" in r["message"]),
            ("read_multiple", {"count": 1}, lambda r: "FINS Read Multiple" in r["message"]),
            ("other", {}, lambda r: "FINS other" in r["message"]),
        ],
    )
    async def test_fins(self, op, params, check):
        assert check(await _simulate_fins(None, {}, op, params))

    @pytest.mark.parametrize(
        "op,params,check",
        [
            ("read", {"tag_name": "T"}, lambda r: r["data"]["value"] == 42),
            ("write", {"value": 100}, lambda r: "Write Tag" in r["message"]),
            ("read_pccc", {}, lambda r: "PCCC Typed Read" in r["message"]),
            ("write_pccc", {"value": 50}, lambda r: "PCCC Typed Write" in r["message"]),
            ("discover_tags", {}, lambda r: r["data"]["count"] == 3),
            ("other", {}, lambda r: "AB other" in r["message"]),
        ],
    )
    async def test_allen_bradley(self, op, params, check):
        assert check(await _simulate_allen_bradley(None, {}, op, params))

    @pytest.mark.parametrize(
        "op,check",
        [
            ("read_joints", lambda r: "axis_1" in r["data"]),
            ("read_motion", lambda r: "motion" in r["message"]),
            ("read_status", lambda r: r["data"]["motor_on"] is True),
            ("read_rapid", lambda r: "Read RAPID" in r["message"]),
            ("write_rapid", lambda r: "Write RAPID" in r["message"]),
            ("start_program", lambda r: "start_program" in r["message"]),
            ("stop_program", lambda r: "stop_program" in r["message"]),
            ("reset_program", lambda r: "reset_program" in r["message"]),
            ("other", lambda r: "other executed" in r["message"]),
        ],
    )
    async def test_abb_rws(self, op, check):
        assert check(await _simulate_abb_rws(None, {}, op, {"rapid_path": "T:x", "write_value": 5}))


# ── _simulate_onvif / http_webhook / serial / simulator / opc_da ──


class TestSimulateMoreProtocols:
    @pytest.mark.parametrize(
        "op,params,check",
        [
            ("discover", {}, lambda r: r["data"]["devices"] == []),
            ("get_rtsp", {}, lambda r: "GetStreamUri" in r["message"]),
            ("get_snapshot", {}, lambda r: "GetSnapshotUri" in r["message"]),
            ("ptz_continuous", {"pan": 0.5}, lambda r: r["data"]["pan"] == 0.5),
            ("preset_set", {"preset_name": "home"}, lambda r: "Preset preset_set" in r["message"]),
            ("subscribe_events", {}, lambda r: "PullPoint" in r["message"]),
            ("other", {}, lambda r: "other executed" in r["message"]),
        ],
    )
    async def test_onvif(self, op, params, check):
        assert check(await _simulate_onvif(None, {}, op, params))

    @pytest.mark.parametrize(
        "op,params,check",
        [
            ("send", {"method": "POST", "url": "http://x", "body": "{}"}, lambda r: r["data"]["status_code"] == 200),
            ("test_auth", {"url": "http://x"}, lambda r: r["data"]["auth_valid"] is True),
            ("other", {}, lambda r: "HTTP other" in r["message"]),
        ],
    )
    async def test_http_webhook(self, op, params, check):
        assert check(await _simulate_http_webhook(None, {}, op, params))

    @pytest.mark.parametrize(
        "op,params,check",
        [
            ("send", {"data": "hi", "encoding": "ascii"}, lambda r: r["data"]["sent"] == "hi"),
            ("send_hex", {"data": "0102"}, lambda r: "Hex" in r["message"]),
            ("read", {}, lambda r: "Serial RX" in r["message"]),
            ("other", {}, lambda r: "Serial other" in r["message"]),
        ],
    )
    async def test_serial(self, op, params, check):
        assert check(await _simulate_serial(None, {}, op, params))

    @pytest.mark.parametrize(
        "op,params,check",
        [
            ("read", {"point_name": "temp"}, lambda r: r["data"]["value"] == 42.5),
            ("write", {"value": 50}, lambda r: "Simulator Write" in r["message"]),
            ("set_fault", {"fault_mode": "timeout"}, lambda r: r["data"]["status"] == "active"),
            ("set_fault", {"fault_mode": "none"}, lambda r: r["data"]["status"] == "cleared"),
            ("other", {}, lambda r: "Simulator other" in r["message"]),
        ],
    )
    async def test_simulator(self, op, params, check):
        assert check(await _simulate_simulator(None, {}, op, params))

    @pytest.mark.parametrize(
        "op,params,check",
        [
            ("read", {"item_id": "S.R"}, lambda r: r["data"]["quality"] == "Good"),
            ("write", {"item_id": "S.R", "value": "42"}, lambda r: "OPC DA Write" in r["message"]),
            ("browse", {}, lambda r: len(r["data"]["items"]) == 3),
            ("list_servers", {}, lambda r: len(r["data"]["servers"]) == 2),
            ("other", {}, lambda r: "OPC DA other" in r["message"]),
        ],
    )
    async def test_opc_da(self, op, params, check):
        assert check(await _simulate_opc_da(None, {}, op, params))


# ── _simulate_generic ──


class TestSimulateGeneric:
    async def test_connect_health_check(self):
        d = AsyncMock()
        d.health_check = AsyncMock(return_value=True)
        svc = AsyncMock()
        svc._driver_instances = {"d1": d}
        r = await _simulate_generic(SimpleNamespace(device_service=svc), {"device_id": "d1"}, "connect", {})
        assert r["values"] == {"connected": True}

    async def test_connect_no_health_check(self):
        d = AsyncMock(spec=[])
        svc = AsyncMock()
        svc._driver_instances = {"d1": d}
        r = await _simulate_generic(SimpleNamespace(device_service=svc), {"device_id": "d1"}, "connect", {})
        assert "not available" in r["error"]

    async def test_connect_no_instance(self):
        svc = AsyncMock()
        svc._driver_instances = {}
        r = await _simulate_generic(SimpleNamespace(device_service=svc), {"device_id": "d1"}, "connect", {})
        assert "not available" in r["error"]

    async def test_discover_success(self):
        d = AsyncMock()
        d.start = AsyncMock()
        d.stop = AsyncMock()
        d.discover_devices = AsyncMock(return_value=[{"id": "x"}])
        reg = MagicMock()
        reg.get_driver_class.return_value = lambda: d
        r = await _simulate_generic(
            SimpleNamespace(driver_registry=reg), {"protocol": "custom"}, "discover", {"config": {}}
        )
        assert "Discover OK" in r["response_raw"]

    async def test_discover_no_driver(self):
        reg = MagicMock()
        reg.get_driver_class.return_value = None
        r = await _simulate_generic(SimpleNamespace(driver_registry=reg), {"protocol": "x"}, "discover", {})
        assert "not available" in r["error"]

    async def test_read_with_points(self):
        svc = AsyncMock()
        svc.read_points = AsyncMock(return_value={"p1": 1})
        r = await _simulate_generic(
            SimpleNamespace(device_service=svc), {"device_id": "d1"}, "read", {"points": ["p1"]}
        )
        assert "Read OK" in r["response_raw"]

    async def test_read_device_points(self):
        svc = AsyncMock()
        svc.read_points = AsyncMock(return_value={"p1": 1})
        r = await _simulate_generic(
            SimpleNamespace(device_service=svc), {"device_id": "d1", "points": [{"name": "p1"}]}, "read", {}
        )
        assert "Read OK" in r["response_raw"]

    async def test_read_no_points(self):
        svc = AsyncMock()
        r = await _simulate_generic(SimpleNamespace(device_service=svc), {"device_id": "d1"}, "read", {})
        assert "No points" in r["error"]

    async def test_write_single(self):
        svc = AsyncMock()
        svc.write_point = AsyncMock()
        r = await _simulate_generic(
            SimpleNamespace(device_service=svc), {"device_id": "d1"}, "write", {"point": "p1", "value": 42}
        )
        assert "Write OK" in r["response_raw"]

    async def test_write_map(self):
        svc = AsyncMock()
        svc.write_point = AsyncMock()
        r = await _simulate_generic(
            SimpleNamespace(device_service=svc), {"device_id": "d1"}, "write", {"points": {"p1": 1, "p2": 2}}
        )
        assert "Write OK" in r["response_raw"]

    async def test_write_no_params(self):
        r = await _simulate_generic(SimpleNamespace(device_service=AsyncMock()), {"device_id": "d1"}, "write", {})
        assert "No write parameters" in r["error"]

    async def test_unsupported_op(self):
        r = await _simulate_generic(SimpleNamespace(device_service=AsyncMock()), {"device_id": "d1"}, "unknown", {})
        assert "Unsupported operation" in r["error"]

    async def test_exc(self):
        svc = AsyncMock()
        svc.read_points = AsyncMock(side_effect=RuntimeError("e"))
        r = await _simulate_generic(
            SimpleNamespace(device_service=svc), {"device_id": "d1"}, "read", {"points": ["p1"]}
        )
        assert r["error"] == "simulate_failed"


# ── WebSocket monitor ──


class TestDebugMonitorWs:
    def _ws_connect(self, app):
        from starlette.testclient import TestClient

        return TestClient(app)

    def test_no_auth_frame(self, app, allowed_config, mock_app_state):
        with self._ws_connect(app) as tc:
            with tc.websocket_connect("/api/v1/debug/monitor") as ws:
                ws.send_json({"type": "not_auth"})
                with pytest.raises(Exception):
                    ws.receive_json()

    def test_invalid_json(self, app, allowed_config, mock_app_state):
        with self._ws_connect(app) as tc:
            with tc.websocket_connect("/api/v1/debug/monitor") as ws:
                ws.send_text("not json")
                with pytest.raises(Exception):
                    ws.receive_json()

    def test_no_token(self, app, allowed_config, mock_app_state):
        with self._ws_connect(app) as tc:
            with tc.websocket_connect("/api/v1/debug/monitor") as ws:
                ws.send_json({"type": "auth"})
                with pytest.raises(Exception):
                    ws.receive_json()

    def test_invalid_token(self, app, allowed_config, mock_app_state):
        with self._ws_connect(app) as tc:
            with tc.websocket_connect("/api/v1/debug/monitor") as ws:
                ws.send_json({"type": "auth", "token": "bad"})
                with pytest.raises(Exception):
                    ws.receive_json()
