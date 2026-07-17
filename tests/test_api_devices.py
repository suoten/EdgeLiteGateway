"""设备管理 API 端点测试。"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import socket
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edgelite.api.deps import get_current_user, get_optional_current_user
from edgelite.api.devices import (
    _check_device_access,
    _check_device_owner,
    _get_accessible_device_ids,
    _is_host_safe_for_device_test,
    router,
)
from edgelite.models.db import StaleDataError

ADMIN = {"user_id": "admin-1", "username": "admin", "role": "admin"}
OPERATOR = {"user_id": "op-1", "username": "operator", "role": "operator"}
VIEWER = {"user_id": "viewer-1", "username": "viewer", "role": "viewer"}
_USERS = {"admin": ADMIN, "operator": OPERATOR, "viewer": VIEWER}

DEVICE = {
    "device_id": "dev-1",
    "name": "Test Device",
    "protocol": "modbus_tcp",
    "status": "online",
    "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
    "points": [{"name": "temp", "data_type": "float32", "address": "0", "access_mode": "rw"}],
    "collect_interval": 5,
    "created_by": "admin-1",
    "created_at": "2025-01-01T00:00:00",
    "updated_at": "2025-01-01T00:00:00",
    "version": 1,
}

TEMPLATE = {
    "name": "tpl-1",
    "protocol": "modbus_tcp",
    "config_template": {"host": "0.0.0.0", "port": 502},
    "point_templates": [{"name": "temp", "data_type": "float32", "address": "0"}],
    "created_at": "2025-01-01T00:00:00",
}

HEALTH = {
    "device_id": "dev-1",
    "status": "online",
    "last_check": "2025-01-01T00:00:00",
    "consecutive_failures": 0,
}


def _make_client(role="admin", optional_user="__none__", webhook_key=""):
    app = FastAPI()
    app.include_router(router)
    user = _USERS[role]
    app.dependency_overrides[get_current_user] = lambda: user
    if optional_user == "__none__":
        app.dependency_overrides[get_optional_current_user] = lambda: None
    else:
        app.dependency_overrides[get_optional_current_user] = lambda: optional_user
    svc = AsyncMock()
    svc._driver_instances = {}
    audit_svc = AsyncMock()
    audit_svc.log = AsyncMock(return_value=None)
    scheduler = AsyncMock()
    config = SimpleNamespace(server=SimpleNamespace(webhook_api_key=webhook_key))
    app.state.device_service = svc
    app.state.audit_service = audit_svc
    app.state.scheduler = scheduler
    app.state.config = config
    return SimpleNamespace(
        client=TestClient(app),
        svc=svc,
        audit=audit_svc,
        scheduler=scheduler,
        config=config,
        app=app,
    )


@pytest.fixture(autouse=True)
def _no_driver_registry():
    with patch("edgelite.drivers.registry.get_driver_registry", return_value=None):
        yield


@contextmanager
def _non_admin_state():
    mock_db = MagicMock()
    mock_db.write_lock = MagicMock()
    mock_state = SimpleNamespace(database=mock_db)
    with (
        patch("edgelite.app._app_state", mock_state),
        patch("edgelite.storage.sqlite_repo.ResourceShareRepo") as RepoMock,
    ):
        repo_instance = AsyncMock()
        repo_instance.check_user_has_access = AsyncMock(return_value=False)
        repo_instance.get_shared_resource_ids = AsyncMock(return_value=set())
        RepoMock.return_value = repo_instance
        yield repo_instance


class TestHostSafety:
    def test_empty_host(self):
        assert _is_host_safe_for_device_test("") is False

    def test_loopback_ipv4(self):
        assert _is_host_safe_for_device_test("127.0.0.1") is False

    def test_loopback_ipv6(self):
        assert _is_host_safe_for_device_test("::1") is False

    def test_link_local(self):
        assert _is_host_safe_for_device_test("169.254.1.1") is False

    def test_unspecified(self):
        assert _is_host_safe_for_device_test("0.0.0.0") is False

    def test_multicast(self):
        assert _is_host_safe_for_device_test("224.0.0.1") is False

    def test_reserved(self):
        assert _is_host_safe_for_device_test("240.0.0.1") is False

    def test_private_allowed(self):
        assert _is_host_safe_for_device_test("192.168.1.1") is True

    def test_public_allowed(self):
        assert _is_host_safe_for_device_test("8.8.8.8") is True

    def test_domain_loopback(self):
        with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 0))]):
            assert _is_host_safe_for_device_test("evil.example.com") is False

    def test_domain_safe(self):
        with patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("10.0.0.1", 0))]):
            assert _is_host_safe_for_device_test("good.example.com") is True

    def test_domain_unresolvable(self):
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("dns failed")):
            assert _is_host_safe_for_device_test("nonexistent.example.com") is False


class TestListDevices:
    def test_list_admin(self):
        t = _make_client("admin")
        t.svc.list_devices = AsyncMock(return_value=([DEVICE], 1))
        r = t.client.get("/api/v1/devices")
        assert r.status_code == 200
        assert r.json()["total"] == 1

    def test_list_with_filters(self):
        t = _make_client("admin")
        t.svc.list_devices = AsyncMock(return_value=([], 0))
        r = t.client.get("/api/v1/devices?status=online&protocol=modbus_tcp&search=test")
        assert r.status_code == 200

    def test_list_non_admin_with_shared(self):
        t = _make_client("operator")
        t.svc.list_devices = AsyncMock(return_value=([DEVICE], 1))
        t.svc.list_device_ids_by_owner = AsyncMock(return_value=["dev-1"])
        t.svc.list_devices_by_ids = AsyncMock(return_value=[])
        with _non_admin_state() as repo:
            repo.get_shared_resource_ids = AsyncMock(return_value={"dev-2"})
            r = t.client.get("/api/v1/devices")
        assert r.status_code == 200

    def test_list_invalid_status(self):
        t = _make_client("admin")
        r = t.client.get("/api/v1/devices?status=invalid")
        assert r.status_code == 422

    def test_list_error(self):
        t = _make_client("admin")
        t.svc.list_devices = AsyncMock(side_effect=RuntimeError("db down"))
        r = t.client.get("/api/v1/devices")
        assert r.status_code == 500


class TestCreateDevice:
    def test_create_success(self):
        t = _make_client("admin")
        t.svc.create_device = AsyncMock(return_value=DEVICE)
        r = t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "dev-1",
                "name": "Test",
                "protocol": "modbus_tcp",
                "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
                "points": [{"name": "temp"}],
                "collect_interval": 5,
            },
        )
        assert r.status_code == 201

    def test_create_already_exists(self):
        t = _make_client("admin")
        t.svc.create_device = AsyncMock(side_effect=ValueError("Device already exists"))
        r = t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "dev-1",
                "name": "Test",
                "protocol": "modbus_tcp",
                "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 409

    def test_create_unsupported_protocol(self):
        t = _make_client("admin")
        r = t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "dev-1",
                "name": "Test",
                "protocol": "unknown_proto",
                "config": {},
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 422

    def test_create_missing_required(self):
        t = _make_client("admin")
        t.svc.create_device = AsyncMock(side_effect=ValueError("missing required field: host"))
        r = t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "dev-1",
                "name": "Test",
                "protocol": "modbus_tcp",
                "config": {},
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 422

    def test_create_driver_start_failed(self):
        t = _make_client("admin")
        t.svc.create_device = AsyncMock(side_effect=ValueError("driver start failed: connection refused"))
        r = t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "dev-1",
                "name": "Test",
                "protocol": "modbus_tcp",
                "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 409

    def test_create_generic_value_error(self):
        t = _make_client("admin")
        t.svc.create_device = AsyncMock(side_effect=ValueError("some other error"))
        r = t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "dev-1",
                "name": "Test",
                "protocol": "modbus_tcp",
                "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 422

    def test_create_internal_error(self):
        t = _make_client("admin")
        t.svc.create_device = AsyncMock(side_effect=RuntimeError("unexpected"))
        r = t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "dev-1",
                "name": "Test",
                "protocol": "modbus_tcp",
                "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 500

    def test_create_invalid_device_id(self):
        t = _make_client("admin")
        r = t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "Invalid!",
                "name": "Test",
                "protocol": "modbus_tcp",
                "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 422

    def test_create_audit_log_called(self):
        t = _make_client("admin")
        t.svc.create_device = AsyncMock(return_value=DEVICE)
        t.client.post(
            "/api/v1/devices",
            json={
                "device_id": "dev-1",
                "name": "Test",
                "protocol": "modbus_tcp",
                "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
                "points": [{"name": "temp"}],
            },
        )
        t.audit.log.assert_called_once()


class TestCreateSimulator:
    def test_simulator_success(self):
        t = _make_client("admin")
        sim_device = {**DEVICE, "protocol": "simulator"}
        t.svc.create_simulator = AsyncMock(return_value=sim_device)
        r = t.client.post(
            "/api/v1/devices/simulator",
            json={
                "device_id": "sim-1",
                "name": "Sim",
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 201

    def test_simulator_failure(self):
        t = _make_client("admin")
        t.svc.create_simulator = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post(
            "/api/v1/devices/simulator",
            json={
                "device_id": "sim-1",
                "name": "Sim",
                "points": [{"name": "temp"}],
            },
        )
        assert r.status_code == 500


class TestDiscover:
    def test_discover_success(self):
        t = _make_client("admin")
        t.svc.discover_devices = AsyncMock(return_value=[{"host": "192.168.1.2"}])
        r = t.client.post(
            "/api/v1/devices/discover",
            json={
                "protocol": "modbus_tcp",
                "config": {"host": "192.168.1.1"},
            },
        )
        assert r.status_code == 200

    def test_discover_failure(self):
        t = _make_client("admin")
        t.svc.discover_devices = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post(
            "/api/v1/devices/discover",
            json={
                "protocol": "modbus_tcp",
                "config": {},
            },
        )
        assert r.status_code == 500

    def test_discover_viewer_forbidden(self):
        t = _make_client("viewer")
        r = t.client.post(
            "/api/v1/devices/discover",
            json={
                "protocol": "modbus_tcp",
                "config": {},
            },
        )
        assert r.status_code == 403


class TestConnection:
    def test_serial_protocol(self):
        t = _make_client("admin")
        r = t.client.post(
            "/api/v1/devices/test-connection",
            json={
                "protocol": "modbus_rtu",
                "config": {},
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["supported"] is False

    def test_no_host(self):
        t = _make_client("admin")
        r = t.client.post(
            "/api/v1/devices/test-connection",
            json={
                "protocol": "modbus_tcp",
                "config": {},
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["success"] is False

    def test_ssrf_blocked(self):
        t = _make_client("admin")
        r = t.client.post(
            "/api/v1/devices/test-connection",
            json={
                "protocol": "modbus_tcp",
                "config": {"host": "127.0.0.1", "port": 502},
            },
        )
        assert r.status_code == 200
        assert r.json()["data"]["success"] is False

    def test_connection_success(self):
        t = _make_client("admin")
        mock_writer = MagicMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = AsyncMock()
        with patch("edgelite.api.devices.asyncio.open_connection", AsyncMock(return_value=(MagicMock(), mock_writer))):
            r = t.client.post(
                "/api/v1/devices/test-connection",
                json={
                    "protocol": "modbus_tcp",
                    "config": {"host": "192.168.1.1", "port": 502},
                },
            )
        assert r.status_code == 200
        assert r.json()["data"]["success"] is True

    def test_connection_refused(self):
        t = _make_client("admin")
        with patch(
            "edgelite.api.devices.asyncio.open_connection", AsyncMock(side_effect=ConnectionRefusedError("refused"))
        ):
            r = t.client.post(
                "/api/v1/devices/test-connection",
                json={
                    "protocol": "modbus_tcp",
                    "config": {"host": "192.168.1.1", "port": 502},
                },
            )
        assert r.status_code == 200
        assert r.json()["data"]["success"] is False

    def test_connection_timeout(self):
        t = _make_client("admin")
        with patch("edgelite.api.devices.asyncio.open_connection", AsyncMock(side_effect=TimeoutError("timed out"))):
            r = t.client.post(
                "/api/v1/devices/test-connection",
                json={
                    "protocol": "modbus_tcp",
                    "config": {"host": "192.168.1.1", "port": 502},
                },
            )
        assert r.status_code == 200
        assert r.json()["data"]["success"] is False


class TestHealthAndStats:
    def test_health_all_admin(self):
        t = _make_client("admin")
        t.svc.list_device_health = AsyncMock(return_value=[HEALTH])
        r = t.client.get("/api/v1/devices/health/all")
        assert r.status_code == 200

    def test_health_all_pagination(self):
        t = _make_client("admin")
        t.svc.list_device_health = AsyncMock(return_value=[HEALTH, HEALTH, HEALTH])
        r = t.client.get("/api/v1/devices/health/all?limit=2&offset=1")
        assert r.status_code == 200
        body = r.json()["data"]
        assert body["total"] == 3
        assert len(body["items"]) == 2

    def test_health_all_non_admin(self):
        t = _make_client("operator")
        t.svc.list_device_health = AsyncMock(return_value=[HEALTH])
        t.svc.list_device_ids_by_owner = AsyncMock(return_value=["dev-1"])
        with _non_admin_state() as repo:
            repo.get_shared_resource_ids = AsyncMock(return_value=set())
            r = t.client.get("/api/v1/devices/health/all")
        assert r.status_code == 200

    def test_health_by_ids(self):
        t = _make_client("admin")
        t.svc.list_device_health_for_ids = AsyncMock(return_value=[HEALTH])
        r = t.client.get("/api/v1/devices/health?ids=dev-1&ids=dev-2")
        assert r.status_code == 200

    def test_collect_stats_admin(self):
        t = _make_client("admin")
        stat = SimpleNamespace(
            device_id="dev-1",
            avg_latency_ms=10.5,
            max_latency_ms=20.0,
            total_calls=100,
            timeout_count=2,
            last_collect_at="2025-01-01",
        )
        t.scheduler.get_collect_stats = AsyncMock(return_value={"dev-1": stat})
        r = t.client.get("/api/v1/devices/collect-stats")
        assert r.status_code == 200

    def test_collect_stats_non_admin(self):
        t = _make_client("operator")
        stat = SimpleNamespace(
            device_id="dev-1",
            avg_latency_ms=10.5,
            max_latency_ms=20.0,
            total_calls=100,
            timeout_count=2,
            last_collect_at="2025-01-01",
        )
        t.scheduler.get_collect_stats = AsyncMock(return_value={"dev-1": stat})
        t.svc.list_device_ids_by_owner = AsyncMock(return_value=["dev-1"])
        with _non_admin_state() as repo:
            repo.get_shared_resource_ids = AsyncMock(return_value=set())
            r = t.client.get("/api/v1/devices/collect-stats")
        assert r.status_code == 200

    def test_quality_stats_admin(self):
        t = _make_client("admin")
        stat = SimpleNamespace(
            device_id="dev-1",
            success_count=90,
            error_count=10,
            total_count=100,
            error_rate=0.1,
        )
        t.scheduler.get_device_quality_stats = AsyncMock(return_value={"dev-1": stat})
        r = t.client.get("/api/v1/devices/device-quality-stats")
        assert r.status_code == 200

    def test_health_all_error(self):
        t = _make_client("admin")
        t.svc.list_device_health = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.get("/api/v1/devices/health/all")
        assert r.status_code == 500

    def test_collect_stats_error(self):
        t = _make_client("admin")
        t.scheduler.get_collect_stats = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.get("/api/v1/devices/collect-stats")
        assert r.status_code == 500

    def test_quality_stats_error(self):
        t = _make_client("admin")
        t.scheduler.get_device_quality_stats = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.get("/api/v1/devices/device-quality-stats")
        assert r.status_code == 500


class TestBatchOps:
    def test_batch_delete_success(self):
        t = _make_client("admin")
        t.svc.batch_delete_devices = AsyncMock(
            return_value={
                "dev-1": (True, None),
                "dev-2": (False, "not found"),
            }
        )
        r = t.client.post("/api/v1/devices/batch/delete", json={"device_ids": ["dev-1", "dev-2"]})
        assert r.status_code == 200
        assert r.json()["data"]["success_count"] == 1

    def test_batch_delete_error(self):
        t = _make_client("admin")
        t.svc.batch_delete_devices = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post("/api/v1/devices/batch/delete", json={"device_ids": ["dev-1"]})
        assert r.status_code == 500

    def test_batch_delete_empty_ids(self):
        t = _make_client("admin")
        r = t.client.post("/api/v1/devices/batch/delete", json={"device_ids": []})
        assert r.status_code == 422

    def test_batch_start_collect(self):
        t = _make_client("admin")
        t.svc.batch_start_collect = AsyncMock(return_value={"dev-1": (True, None)})
        r = t.client.post("/api/v1/devices/batch/start-collect", json={"device_ids": ["dev-1"]})
        assert r.status_code == 200

    def test_batch_start_collect_error(self):
        t = _make_client("admin")
        t.svc.batch_start_collect = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post("/api/v1/devices/batch/start-collect", json={"device_ids": ["dev-1"]})
        assert r.status_code == 500

    def test_batch_stop_collect(self):
        t = _make_client("admin")
        t.svc.batch_stop_collect = AsyncMock(return_value={"dev-1": (True, None)})
        r = t.client.post("/api/v1/devices/batch/stop-collect", json={"device_ids": ["dev-1"]})
        assert r.status_code == 200

    def test_batch_stop_collect_error(self):
        t = _make_client("admin")
        t.svc.batch_stop_collect = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post("/api/v1/devices/batch/stop-collect", json={"device_ids": ["dev-1"]})
        assert r.status_code == 500

    def test_batch_deploy_success(self):
        t = _make_client("admin")
        template_dev = {
            **DEVICE,
            "points": [{"name": "temp", "address": "0", "data_type": "float32", "access_mode": "rw"}],
        }
        target_dev = {
            **DEVICE,
            "device_id": "dev-2",
            "points": [{"name": "temp", "address": "1", "data_type": "float32", "access_mode": "rw"}],
        }
        t.svc.get_device = AsyncMock(return_value=template_dev)
        t.svc.list_devices_by_ids = AsyncMock(return_value=[target_dev])
        t.svc.update_device = AsyncMock(return_value=target_dev)
        r = t.client.post(
            "/api/v1/devices/batch-deploy",
            json={
                "template_device_id": "dev-1",
                "target_device_ids": ["dev-2"],
            },
        )
        assert r.status_code == 200

    def test_batch_deploy_template_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=None)
        r = t.client.post(
            "/api/v1/devices/batch-deploy",
            json={
                "template_device_id": "dev-1",
                "target_device_ids": ["dev-2"],
            },
        )
        assert r.status_code == 404

    def test_batch_deploy_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post(
            "/api/v1/devices/batch-deploy",
            json={
                "template_device_id": "dev-1",
                "target_device_ids": ["dev-2"],
            },
        )
        assert r.status_code == 500

    def test_batch_deploy_too_many(self):
        t = _make_client("admin")
        ids = [f"dev-{i}" for i in range(101)]
        r = t.client.post(
            "/api/v1/devices/batch-deploy",
            json={
                "template_device_id": "dev-1",
                "target_device_ids": ids,
            },
        )
        assert r.status_code == 422


class TestTemplates:
    def test_create_template_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.create_template = AsyncMock(return_value=TEMPLATE)
        r = t.client.post(
            "/api/v1/devices/templates",
            json={
                "device_id": "dev-1",
                "template_name": "tpl-1",
            },
        )
        assert r.status_code == 201

    def test_create_template_device_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=None)
        r = t.client.post(
            "/api/v1/devices/templates",
            json={
                "device_id": "missing",
                "template_name": "tpl-1",
            },
        )
        assert r.status_code == 404

    def test_create_template_value_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.create_template = AsyncMock(side_effect=ValueError("exists"))
        r = t.client.post(
            "/api/v1/devices/templates",
            json={
                "device_id": "dev-1",
                "template_name": "tpl-1",
            },
        )
        assert r.status_code == 409

    def test_create_template_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.create_template = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post(
            "/api/v1/devices/templates",
            json={
                "device_id": "dev-1",
                "template_name": "tpl-1",
            },
        )
        assert r.status_code == 500

    def test_list_templates_admin(self):
        t = _make_client("admin")
        t.svc.list_templates = AsyncMock(return_value=[TEMPLATE])
        r = t.client.get("/api/v1/devices/templates")
        assert r.status_code == 200

    def test_list_templates_value_error(self):
        t = _make_client("admin")
        t.svc.list_templates = AsyncMock(side_effect=ValueError("fail"))
        r = t.client.get("/api/v1/devices/templates")
        assert r.status_code == 503

    def test_list_templates_error(self):
        t = _make_client("admin")
        t.svc.list_templates = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.get("/api/v1/devices/templates")
        assert r.status_code == 500

    def test_create_from_template_success(self):
        t = _make_client("admin")
        t.svc.create_from_template = AsyncMock(return_value=DEVICE)
        r = t.client.post(
            "/api/v1/devices/from-template",
            json={
                "template_name": "tpl-1",
                "device_id": "new-1",
                "name": "New",
            },
        )
        assert r.status_code == 201

    def test_create_from_template_value_error(self):
        t = _make_client("admin")
        t.svc.create_from_template = AsyncMock(side_effect=ValueError("bad"))
        r = t.client.post(
            "/api/v1/devices/from-template",
            json={
                "template_name": "tpl-1",
                "device_id": "new-1",
                "name": "New",
            },
        )
        assert r.status_code == 409

    def test_delete_template_success(self):
        t = _make_client("admin")
        t.svc.delete_template = AsyncMock(return_value=True)
        r = t.client.delete("/api/v1/devices/templates/tpl-1")
        assert r.status_code == 200

    def test_delete_template_not_found(self):
        t = _make_client("admin")
        t.svc.delete_template = AsyncMock(return_value=False)
        r = t.client.delete("/api/v1/devices/templates/tpl-1")
        assert r.status_code == 404


class TestImportExport:
    def test_export_success(self):
        t = _make_client("admin")
        t.svc.export_devices = AsyncMock(return_value=[DEVICE])
        r = t.client.post("/api/v1/devices/export", json={"device_ids": ["dev-1"]})
        assert r.status_code == 200

    def test_export_error(self):
        t = _make_client("admin")
        t.svc.export_devices = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post("/api/v1/devices/export", json={"device_ids": ["dev-1"]})
        assert r.status_code == 500

    def test_import_success(self):
        t = _make_client("admin")
        t.svc.import_devices = AsyncMock(
            return_value={
                "success": 1,
                "failed": 0,
                "errors": [],
            }
        )
        r = t.client.post(
            "/api/v1/devices/import",
            json={
                "data": [
                    {
                        "device_id": "dev-1",
                        "name": "Test",
                        "protocol": "modbus_tcp",
                        "config": {"host": "192.168.1.1", "port": 502, "slave_id": 1},
                        "points": [{"name": "temp"}],
                    }
                ],
            },
        )
        assert r.status_code == 200

    def test_import_atomic_failure(self):
        t = _make_client("admin")
        t.svc.import_devices = AsyncMock(
            return_value={
                "success": 0,
                "failed": 1,
                "errors": ["bad"],
            }
        )
        r = t.client.post(
            "/api/v1/devices/import",
            json={
                "data": [{"device_id": "dev-1"}],
                "atomic": True,
            },
        )
        assert r.status_code == 400

    def test_import_timeout(self):
        t = _make_client("admin")
        t.svc.import_devices = AsyncMock(side_effect=TimeoutError("slow"))
        r = t.client.post(
            "/api/v1/devices/import",
            json={
                "data": [{"device_id": "dev-1"}],
            },
        )
        assert r.status_code == 504

    def test_import_error(self):
        t = _make_client("admin")
        t.svc.import_devices = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post(
            "/api/v1/devices/import",
            json={
                "data": [{"device_id": "dev-1"}],
            },
        )
        assert r.status_code == 500

    def test_import_empty_data(self):
        t = _make_client("admin")
        r = t.client.post("/api/v1/devices/import", json={"data": []})
        assert r.status_code == 422

    def test_import_too_many(self):
        t = _make_client("admin")
        data = [{"device_id": f"dev-{i}"} for i in range(501)]
        r = t.client.post("/api/v1/devices/import", json={"data": data})
        assert r.status_code == 422


class TestDeviceCrud:
    def test_get_device_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        r = t.client.get("/api/v1/devices/dev-1")
        assert r.status_code == 200

    def test_get_device_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=None)
        r = t.client.get("/api/v1/devices/dev-1")
        assert r.status_code == 404

    def test_get_device_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.get("/api/v1/devices/dev-1")
        assert r.status_code == 500

    def test_get_device_non_admin_owner(self):
        t = _make_client("operator")
        dev = {**DEVICE, "created_by": "op-1"}
        t.svc.get_device = AsyncMock(return_value=dev)
        r = t.client.get("/api/v1/devices/dev-1")
        assert r.status_code == 200

    def test_get_device_non_admin_no_access(self):
        t = _make_client("operator")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        with _non_admin_state():
            r = t.client.get("/api/v1/devices/dev-1")
        assert r.status_code == 403

    def test_update_device_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        updated = {**DEVICE, "name": "Updated"}
        t.svc.update_device = AsyncMock(return_value=updated)
        r = t.client.put("/api/v1/devices/dev-1", json={"name": "Updated"})
        assert r.status_code == 200

    def test_update_device_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=None)
        r = t.client.put("/api/v1/devices/dev-1", json={"name": "Updated"})
        assert r.status_code == 404

    def test_update_device_stale_data(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.update_device = AsyncMock(side_effect=StaleDataError("version conflict"))
        r = t.client.put("/api/v1/devices/dev-1", json={"name": "Updated"})
        assert r.status_code == 409

    def test_update_device_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.update_device = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.put("/api/v1/devices/dev-1", json={"name": "Updated"})
        assert r.status_code == 500

    def test_update_device_returns_none(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.update_device = AsyncMock(return_value=None)
        r = t.client.put("/api/v1/devices/dev-1", json={"name": "Updated"})
        assert r.status_code == 404

    def test_update_device_strips_write_policy(self):
        t = _make_client("admin")
        dev = {**DEVICE, "config": {"host": "192.168.1.1", "write_verify": True}}
        t.svc.get_device = AsyncMock(return_value=dev)
        t.svc.update_device = AsyncMock(return_value=dev)
        r = t.client.put("/api/v1/devices/dev-1", json={"config": {"host": "10.0.0.1", "write_verify": False}})
        assert r.status_code == 200
        call_data = t.svc.update_device.call_args.args[1]
        cfg = call_data.get("config", {})
        assert cfg.get("write_verify") is True

    def test_update_device_rejected_keys(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.update_device = AsyncMock(return_value=DEVICE)
        r = t.client.put("/api/v1/devices/dev-1", json={"name": "Updated"})
        assert r.status_code == 200
        call_data = t.svc.update_device.call_args.args[1]
        assert "device_id" not in call_data

    def test_update_device_non_admin_immutable_point(self):
        t = _make_client("operator")
        dev = {
            **DEVICE,
            "created_by": "op-1",
            "points": [{"name": "temp", "address": "0", "data_type": "float32", "access_mode": "rw"}],
        }
        t.svc.get_device = AsyncMock(return_value=dev)
        r = t.client.put(
            "/api/v1/devices/dev-1",
            json={"points": [{"name": "temp", "address": "999", "data_type": "float32", "access_mode": "rw"}]},
        )
        assert r.status_code == 403

    def test_update_device_admin_can_change_immutable(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        updated = {**DEVICE, "points": [{"name": "temp", "address": "999"}]}
        t.svc.update_device = AsyncMock(return_value=updated)
        r = t.client.put(
            "/api/v1/devices/dev-1",
            json={"points": [{"name": "temp", "address": "999", "data_type": "float32", "access_mode": "rw"}]},
        )
        assert r.status_code == 200

    def test_update_write_policy_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.update_device = AsyncMock(return_value=DEVICE)
        r = t.client.put("/api/v1/devices/dev-1/write-policy", json={"write_verify": True})
        assert r.status_code == 200

    def test_update_write_policy_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=None)
        r = t.client.put("/api/v1/devices/dev-1/write-policy", json={"write_verify": True})
        assert r.status_code == 404

    def test_update_write_policy_stale(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.update_device = AsyncMock(side_effect=StaleDataError("conflict"))
        r = t.client.put("/api/v1/devices/dev-1/write-policy", json={"write_verify": True})
        assert r.status_code == 409

    def test_update_write_policy_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.update_device = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.put("/api/v1/devices/dev-1/write-policy", json={"write_verify": True})
        assert r.status_code == 500

    def test_delete_device_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.delete_device = AsyncMock(return_value=(True, None))
        r = t.client.delete("/api/v1/devices/dev-1")
        assert r.status_code == 200

    def test_delete_device_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=None)
        r = t.client.delete("/api/v1/devices/dev-1")
        assert r.status_code == 404

    def test_delete_device_conflict(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.delete_device = AsyncMock(return_value=(False, "in use"))
        r = t.client.delete("/api/v1/devices/dev-1")
        assert r.status_code == 409

    def test_delete_device_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.delete_device = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.delete("/api/v1/devices/dev-1")
        assert r.status_code == 500


class TestDevicePoints:
    def test_get_points_success(self):
        t = _make_client("admin")
        t.svc.read_points = AsyncMock(return_value={"temp": 25.5})
        r = t.client.get("/api/v1/devices/dev-1/points")
        assert r.status_code == 200

    def test_get_points_timeout(self):
        t = _make_client("admin")
        t.svc.read_points = AsyncMock(side_effect=TimeoutError("slow"))
        r = t.client.get("/api/v1/devices/dev-1/points")
        assert r.status_code == 200
        assert r.json()["data"] == {}

    def test_get_points_not_found_owner(self):
        t = _make_client("operator")
        t.svc.get_device = AsyncMock(return_value=None)
        r = t.client.get("/api/v1/devices/dev-1/points")
        assert r.status_code == 404

    def test_get_points_error(self):
        t = _make_client("admin")
        t.svc.read_points = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.get("/api/v1/devices/dev-1/points")
        assert r.status_code == 500

    def test_write_point_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.write_point = AsyncMock(return_value=True)
        r = t.client.post("/api/v1/devices/dev-1/points", json={"point": "temp", "value": 30.0})
        assert r.status_code == 200

    def test_write_point_failed(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.write_point = AsyncMock(return_value=False)
        r = t.client.post("/api/v1/devices/dev-1/points", json={"point": "temp", "value": 30.0})
        assert r.status_code == 400

    def test_write_point_not_found_owner(self):
        t = _make_client("operator")
        t.svc.get_device = AsyncMock(return_value=None)
        r = t.client.post("/api/v1/devices/dev-1/points", json={"point": "temp", "value": 30.0})
        assert r.status_code == 403

    def test_write_point_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.write_point = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post("/api/v1/devices/dev-1/points", json={"point": "temp", "value": 30.0})
        assert r.status_code == 500

    def test_write_point_empty_point_name(self):
        t = _make_client("admin")
        r = t.client.post("/api/v1/devices/dev-1/points", json={"point": "", "value": 30.0})
        assert r.status_code == 422


class TestPushData:
    def test_push_with_bearer_admin(self):
        t = _make_client("admin", optional_user=ADMIN)
        t.svc._driver_instances = {"dev-1": MagicMock()}
        t.svc._driver_instances["dev-1"].receive_data = AsyncMock(return_value=None)
        r = t.client.post("/api/v1/devices/dev-1/push", json={"data": {"temp": {"value": 25.0}}})
        assert r.status_code == 200

    def test_push_with_bearer_non_admin_owner(self):
        t = _make_client("operator", optional_user=OPERATOR)
        dev = {**DEVICE, "created_by": "op-1"}
        t.svc.get_device = AsyncMock(return_value=dev)
        t.svc._driver_instances = {"dev-1": MagicMock()}
        t.svc._driver_instances["dev-1"].receive_data = AsyncMock(return_value=None)
        r = t.client.post("/api/v1/devices/dev-1/push", json={"data": {"temp": {"value": 25.0}}})
        assert r.status_code == 200

    def test_push_with_bearer_non_admin_no_access(self):
        t = _make_client("operator", optional_user=OPERATOR)
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        with _non_admin_state():
            r = t.client.post("/api/v1/devices/dev-1/push", json={"data": {"temp": {"value": 25.0}}})
        assert r.status_code == 403

    def test_push_with_api_key_valid(self):
        t = _make_client("admin", optional_user="__none__", webhook_key="test-key-123")
        t.svc._driver_instances = {"dev-1": MagicMock()}
        t.svc._driver_instances["dev-1"].receive_data = AsyncMock(return_value=None)
        with patch("edgelite.security.rbac.has_api_key_permission", return_value=True):
            r = t.client.post(
                "/api/v1/devices/dev-1/push",
                json={"data": {"temp": {"value": 25.0}}},
                headers={"X-API-Key": "test-key-123"},
            )
        assert r.status_code == 200

    def test_push_with_api_key_invalid(self):
        t = _make_client("admin", optional_user="__none__", webhook_key="correct-key")
        with patch("edgelite.security.rbac.has_api_key_permission", return_value=True):
            r = t.client.post(
                "/api/v1/devices/dev-1/push",
                json={"data": {"temp": {"value": 25.0}}},
                headers={"X-API-Key": "wrong-key"},
            )
        assert r.status_code == 401

    def test_push_no_auth(self):
        t = _make_client("admin", optional_user="__none__", webhook_key="")
        r = t.client.post("/api/v1/devices/dev-1/push", json={"data": {"temp": {"value": 25.0}}})
        assert r.status_code == 401

    def test_push_driver_not_ready(self):
        t = _make_client("admin", optional_user=ADMIN)
        t.svc._driver_instances = {}
        r = t.client.post("/api/v1/devices/dev-1/push", json={"data": {"temp": {"value": 25.0}}})
        assert r.status_code == 400

    def test_push_empty_data(self):
        t = _make_client("admin", optional_user=ADMIN)
        r = t.client.post("/api/v1/devices/dev-1/push", json={"data": {}})
        assert r.status_code == 422

    def test_push_error(self):
        t = _make_client("admin", optional_user=ADMIN)
        t.svc._driver_instances = {"dev-1": MagicMock()}
        t.svc._driver_instances["dev-1"].receive_data = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post("/api/v1/devices/dev-1/push", json={"data": {"temp": {"value": 25.0}}})
        assert r.status_code == 500


class TestDeviceSubResources:
    def test_get_device_health_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_device_health = AsyncMock(return_value=HEALTH)
        r = t.client.get("/api/v1/devices/dev-1/health")
        assert r.status_code == 200

    def test_get_device_health_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_device_health = AsyncMock(return_value=None)
        r = t.client.get("/api/v1/devices/dev-1/health")
        assert r.status_code == 404

    def test_get_device_health_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_device_health = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.get("/api/v1/devices/dev-1/health")
        assert r.status_code == 500

    def test_reset_health_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.reset_device_health = AsyncMock(return_value=True)
        r = t.client.post("/api/v1/devices/dev-1/health/reset")
        assert r.status_code == 200

    def test_reset_health_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.reset_device_health = AsyncMock(return_value=False)
        r = t.client.post("/api/v1/devices/dev-1/health/reset")
        assert r.status_code == 404

    def test_get_ops_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_device_ops_data = AsyncMock(return_value={"ops": "data"})
        r = t.client.get("/api/v1/devices/dev-1/ops")
        assert r.status_code == 200

    def test_get_ops_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_device_ops_data = AsyncMock(return_value=None)
        r = t.client.get("/api/v1/devices/dev-1/ops")
        assert r.status_code == 404

    def test_probe_primary_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.probe_primary_link = AsyncMock(return_value=True)
        r = t.client.post("/api/v1/devices/dev-1/probe-primary")
        assert r.status_code == 200

    def test_probe_primary_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.probe_primary_link = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post("/api/v1/devices/dev-1/probe-primary")
        assert r.status_code == 500

    def test_get_point_health_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_point_health = AsyncMock(return_value={"temp": "good"})
        r = t.client.get("/api/v1/devices/dev-1/point-health")
        assert r.status_code == 200

    def test_get_point_health_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_point_health = AsyncMock(return_value=None)
        r = t.client.get("/api/v1/devices/dev-1/point-health")
        assert r.status_code == 404

    def test_get_write_audit_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_write_audit = AsyncMock(return_value=[{"point": "temp", "value": 1}])
        r = t.client.get("/api/v1/devices/dev-1/write-audit")
        assert r.status_code == 200

    def test_get_write_audit_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_write_audit = AsyncMock(return_value=None)
        r = t.client.get("/api/v1/devices/dev-1/write-audit")
        assert r.status_code == 404

    def test_get_metrics_success(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        r = t.client.get("/api/v1/devices/dev-1/metrics")
        assert r.status_code == 200
        assert "read_error_rate" in r.json()["data"]


class TestConfigVersions:
    def test_list_config_versions(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_config_versions = AsyncMock(return_value=[{"version": 1}])
        r = t.client.get("/api/v1/devices/dev-1/config-versions")
        assert r.status_code == 200

    def test_list_config_versions_value_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_config_versions = AsyncMock(side_effect=ValueError("bad"))
        r = t.client.get("/api/v1/devices/dev-1/config-versions")
        assert r.status_code == 400

    def test_list_config_versions_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_config_versions = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.get("/api/v1/devices/dev-1/config-versions")
        assert r.status_code == 500

    def test_get_config_current(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_config_current = AsyncMock(return_value={"host": "1.2.3.4"})
        r = t.client.get("/api/v1/devices/dev-1/config-versions/current")
        assert r.status_code == 200

    def test_get_config_current_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_config_current = AsyncMock(return_value=None)
        r = t.client.get("/api/v1/devices/dev-1/config-versions/current")
        assert r.status_code == 404

    def test_get_config_version_detail(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_config_version_config = AsyncMock(return_value={"version": 1})
        r = t.client.get("/api/v1/devices/dev-1/config-versions/1")
        assert r.status_code == 200

    def test_get_config_version_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.get_config_version_config = AsyncMock(return_value=None)
        r = t.client.get("/api/v1/devices/dev-1/config-versions/99")
        assert r.status_code == 404

    def test_save_config_version(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.save_config_version = AsyncMock(return_value=2)
        r = t.client.post(
            "/api/v1/devices/dev-1/config-versions",
            json={
                "config": {"host": "1.2.3.4"},
                "change_summary": "update",
                "operator": "admin",
            },
        )
        assert r.status_code == 200

    def test_save_config_version_failed(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.save_config_version = AsyncMock(return_value=0)
        r = t.client.post(
            "/api/v1/devices/dev-1/config-versions",
            json={
                "config": {"host": "1.2.3.4"},
                "change_summary": "update",
            },
        )
        assert r.status_code == 400

    def test_save_config_version_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.save_config_version = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post(
            "/api/v1/devices/dev-1/config-versions",
            json={
                "config": {"host": "1.2.3.4"},
            },
        )
        assert r.status_code == 500

    def test_rollback_config(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.rollback_config = AsyncMock(return_value={"version": 3})
        r = t.client.post(
            "/api/v1/devices/dev-1/config-versions/rollback",
            json={
                "target_version": 1,
                "operator": "admin",
            },
        )
        assert r.status_code == 200

    def test_rollback_config_not_found(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.rollback_config = AsyncMock(return_value=None)
        r = t.client.post(
            "/api/v1/devices/dev-1/config-versions/rollback",
            json={
                "target_version": 99,
            },
        )
        assert r.status_code == 404

    def test_rollback_config_error(self):
        t = _make_client("admin")
        t.svc.get_device = AsyncMock(return_value=DEVICE)
        t.svc.rollback_config = AsyncMock(side_effect=RuntimeError("fail"))
        r = t.client.post(
            "/api/v1/devices/dev-1/config-versions/rollback",
            json={
                "target_version": 1,
            },
        )
        assert r.status_code == 500


class TestAccessHelpers:
    async def test_check_device_owner_admin(self):
        svc = AsyncMock()
        await _check_device_owner(svc, "dev-1", ADMIN)

    async def test_check_device_owner_not_found(self):
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value=None)
        with pytest.raises(Exception) as exc_info:
            await _check_device_owner(svc, "dev-1", OPERATOR)
        assert exc_info.value.status_code == 404

    async def test_check_device_owner_owner(self):
        svc = AsyncMock()
        dev = {**DEVICE, "created_by": "op-1"}
        svc.get_device = AsyncMock(return_value=dev)
        await _check_device_owner(svc, "dev-1", OPERATOR)

    async def test_check_device_owner_shared(self):
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value=DEVICE)
        with _non_admin_state() as repo:
            repo.check_user_has_access = AsyncMock(return_value=True)
            await _check_device_owner(svc, "dev-1", OPERATOR)

    async def test_check_device_owner_denied(self):
        svc = AsyncMock()
        svc.get_device = AsyncMock(return_value=DEVICE)
        with _non_admin_state():
            with pytest.raises(Exception) as exc_info:
                await _check_device_owner(svc, "dev-1", OPERATOR)
            assert exc_info.value.status_code == 403

    async def test_check_device_access_admin(self):
        await _check_device_access(DEVICE, ADMIN)

    async def test_check_device_access_owner(self):
        dev = {**DEVICE, "created_by": "op-1"}
        await _check_device_access(dev, OPERATOR)

    async def test_check_device_access_shared(self):
        with _non_admin_state() as repo:
            repo.check_user_has_access = AsyncMock(return_value=True)
            await _check_device_access(DEVICE, OPERATOR)

    async def test_check_device_access_denied(self):
        with _non_admin_state():
            with pytest.raises(Exception) as exc_info:
                await _check_device_access(DEVICE, OPERATOR)
            assert exc_info.value.status_code == 403

    async def test_get_accessible_ids_admin(self):
        svc = AsyncMock()
        result = await _get_accessible_device_ids(svc, ADMIN)
        assert result is None

    async def test_get_accessible_ids_non_admin(self):
        svc = AsyncMock()
        svc.list_device_ids_by_owner = AsyncMock(return_value={"dev-1"})
        with _non_admin_state() as repo:
            repo.get_shared_resource_ids = AsyncMock(return_value={"dev-2"})
            result = await _get_accessible_device_ids(svc, OPERATOR)
        assert result == {"dev-1", "dev-2"}
