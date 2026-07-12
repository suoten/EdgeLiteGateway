"""System management API endpoint tests - covers src/edgelite/api/system.py

Tests all public API endpoints via FastAPI TestClient with mocked services:
- GET/POST/DELETE /backup, POST /restore
- GET/POST /backup/schedule
- GET /cascade/topology, /cascade/neighbors, POST /cascade/config, DELETE /cascade/neighbors/{id}
- POST /config/reload, GET /config, PUT /config/{section}
- GET /quality/{device_id}, GET/POST /circuit-breakers
- GET /health/basic, /ready-status, /performance
- GET/PUT /retention, GET /cert, POST /cert/rotate
- GET/PUT /ntp
- GET /migration/status, POST /migration/retry, GET /migration/history
- GET /locks/status, GET /network
- Helper functions: _is_cascade_parent_host_safe, _notify_services_reload, _load/_save_ntp_config
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import ipaddress
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from edgelite.api.deps import (
    get_audit_service,
    get_current_user,
    get_scheduler,
    get_system_service,
)
from edgelite.api.system import (
    CascadeConfigUpdate,
    ConfigSectionUpdate,
    NtpConfigUpdate,
    RetentionPolicyUpdate,
    _is_cascade_parent_host_safe,
    _load_ntp_config,
    _notify_services_reload,
    _save_ntp_config,
    router,
)


# ── Helpers ──


def _make_user(role: str = "admin", user_id: str = "u1", username: str = "admin"):
    return {"user_id": user_id, "username": username, "role": role}


def _build_app(role: str = "admin", system_svc=None, audit_svc=None, scheduler=None):
    """Build a test FastAPI app with mocked dependencies."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    app.dependency_overrides[get_system_service] = lambda: system_svc or AsyncMock()
    app.dependency_overrides[get_audit_service] = lambda: audit_svc or AsyncMock()
    app.dependency_overrides[get_scheduler] = lambda: scheduler or AsyncMock()
    return app


# ── Fixtures ──


@pytest.fixture
def mock_system_svc():
    svc = AsyncMock()
    svc.get_status = AsyncMock(return_value={"device_total": 5, "device_online": 3})
    svc.collect_resources = AsyncMock(return_value={"cpu_percent": 50.0})
    svc.list_backups = AsyncMock(return_value=[])
    svc.create_backup = AsyncMock(return_value={"backup_id": "20260101_120000"})
    svc.restore_backup = AsyncMock(return_value=True)
    svc.delete_backup = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def mock_audit_svc():
    svc = AsyncMock()
    svc.log = AsyncMock(return_value=None)
    return svc


@pytest.fixture
def mock_scheduler():
    sched = AsyncMock()
    sched.calculate_quality_score = AsyncMock(return_value={"score": 95.5})
    sched.get_circuit_breaker_status = AsyncMock(return_value={})
    sched.reset_circuit_breaker = AsyncMock(return_value=True)
    return sched


@pytest.fixture
def app(mock_system_svc, mock_audit_svc, mock_scheduler):
    return _build_app(
        role="admin",
        system_svc=mock_system_svc,
        audit_svc=mock_audit_svc,
        scheduler=mock_scheduler,
    )


@pytest.fixture
def client(app):
    return TestClient(app)


# ════════════════════════════════════════════════════════════
#  _is_cascade_parent_host_safe (SSRF validation helper)
# ════════════════════════════════════════════════════════════


class TestIsCascadeParentHostSafe:
    def test_empty_host_returns_false(self):
        assert _is_cascade_parent_host_safe("") is False

    def test_none_host_returns_false(self):
        assert _is_cascade_parent_host_safe(None) is False

    def test_loopback_ipv4_blocked(self):
        assert _is_cascade_parent_host_safe("127.0.0.1") is False

    def test_loopback_ipv6_blocked(self):
        assert _is_cascade_parent_host_safe("::1") is False

    def test_link_local_blocked(self):
        assert _is_cascade_parent_host_safe("169.254.1.1") is False

    def test_unspecified_blocked(self):
        assert _is_cascade_parent_host_safe("0.0.0.0") is False

    def test_multicast_blocked(self):
        assert _is_cascade_parent_host_safe("224.0.0.1") is False

    def test_reserved_blocked(self):
        assert _is_cascade_parent_host_safe("240.0.0.1") is False

    def test_private_ip_allowed(self):
        assert _is_cascade_parent_host_safe("192.168.1.1") is True

    def test_public_ip_allowed(self):
        assert _is_cascade_parent_host_safe("8.8.8.8") is True

    def test_domain_resolves_to_safe_ip(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (0, 0, 0, "", ("93.184.216.34", 0)),
            ]
            assert _is_cascade_parent_host_safe("example.com") is True

    def test_domain_resolves_to_loopback_blocked(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (0, 0, 0, "", ("127.0.0.1", 0)),
            ]
            assert _is_cascade_parent_host_safe("evil.com") is False

    def test_domain_resolution_fails_returns_false(self):
        import socket

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("dns fail")):
            assert _is_cascade_parent_host_safe("nonexistent.invalid") is False

    def test_domain_empty_addrs_returns_false(self):
        with patch("socket.getaddrinfo", return_value=[]):
            assert _is_cascade_parent_host_safe("empty.com") is False

    def test_domain_one_unsafe_among_safe_returns_false(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (0, 0, 0, "", ("8.8.8.8", 0)),
                (0, 0, 0, "", ("127.0.0.1", 0)),
            ]
            assert _is_cascade_parent_host_safe("mixed.com") is False

    def test_domain_addr_value_error_skipped(self):
        with patch("socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [
                (0, 0, 0, "", ("not_an_ip", 0)),
                (0, 0, 0, "", ("8.8.8.8", 0)),
            ]
            assert _is_cascade_parent_host_safe("skipbad.com") is True


# ════════════════════════════════════════════════════════════
#  GET /status
# ════════════════════════════════════════════════════════════


def _full_status_data():
    """Return a complete status dict matching SystemStatusResponse fields."""
    return {
        "cpu_percent": 25.0,
        "memory_total": 1000,
        "memory_used": 500,
        "memory_percent": 50.0,
        "disk_total": 2000,
        "disk_used": 1000,
        "disk_percent": 50.0,
        "device_total": 5,
        "device_online": 3,
        "rule_total": 7,
        "rule_enabled": 4,
        "alarm_firing": 2,
        "collect_task_count": 5,
        "uptime": 100,
        "version": "1.0.0",
    }


class TestGetSystemStatus:
    def test_status_success(self, client, mock_system_svc):
        mock_system_svc.get_status = AsyncMock(return_value=_full_status_data())
        resp = client.get("/api/v1/system/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["device_total"] == 5
        assert data["device_online"] == 3

    def test_status_service_error_returns_500(self, client, mock_system_svc):
        mock_system_svc.get_status = AsyncMock(side_effect=RuntimeError("db down"))
        resp = client.get("/api/v1/system/status")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SYS_STATUS_FAILED"

    def test_status_viewer_allowed(self, mock_system_svc, mock_audit_svc, mock_scheduler):
        """viewer has SYSTEM_READ permission, should succeed."""
        app = _build_app("viewer", mock_system_svc, mock_audit_svc, mock_scheduler)
        mock_system_svc.get_status = AsyncMock(return_value=_full_status_data())
        client = TestClient(app)
        resp = client.get("/api/v1/system/status")
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════
#  GET /resources
# ════════════════════════════════════════════════════════════


class TestGetSystemResources:
    def test_resources_success(self, client, mock_system_svc):
        mock_system_svc.collect_resources = AsyncMock(
            return_value={"cpu_percent": 42.0, "memory_percent": 60.0}
        )
        resp = client.get("/api/v1/system/resources")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["cpu_percent"] == 42.0

    def test_resources_error_returns_500(self, client, mock_system_svc):
        mock_system_svc.collect_resources = AsyncMock(side_effect=Exception("psutil fail"))
        resp = client.get("/api/v1/system/resources")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  GET /backup (list)
# ════════════════════════════════════════════════════════════


class TestListBackups:
    def test_list_success(self, client, mock_system_svc):
        mock_system_svc.list_backups = AsyncMock(
            return_value=[{"backup_id": "b1", "size": 1024}]
        )
        resp = client.get("/api/v1/system/backup")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

    def test_list_error_returns_500(self, client, mock_system_svc):
        mock_system_svc.list_backups = AsyncMock(side_effect=RuntimeError("io"))
        resp = client.get("/api/v1/system/backup")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SYS_BACKUP_LIST_FAILED"

    def test_list_viewer_forbidden(self, mock_system_svc, mock_audit_svc, mock_scheduler):
        """list_backups requires SYSTEM_MANAGE, viewer should get 403."""
        app = _build_app("viewer", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        resp = client.get("/api/v1/system/backup")
        assert resp.status_code == 403


# ════════════════════════════════════════════════════════════
#  POST /backup (create)
# ════════════════════════════════════════════════════════════


class TestCreateBackup:
    def test_create_success(self, client, mock_system_svc, mock_audit_svc):
        mock_system_svc.create_backup = AsyncMock(
            return_value={"backup_id": "20260101_120000", "db_file": "/tmp/backup.db"}
        )
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/backup")
        assert resp.status_code == 201
        assert resp.json()["data"]["backup_id"] == "20260101_120000"
        mock_audit_svc.log.assert_awaited_once()

    def test_create_service_error_returns_500(self, client, mock_system_svc, mock_audit_svc):
        mock_system_svc.create_backup = AsyncMock(side_effect=RuntimeError("disk full"))
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/backup")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SYS_BACKUP_CREATE_FAILED"
        # audit log should be called with status="failed"
        mock_audit_svc.log.assert_awaited()
        call_kwargs = mock_audit_svc.log.call_args
        assert call_kwargs.kwargs.get("status") == "failed"

    def test_create_backup_non_dict_result(self, client, mock_system_svc, mock_audit_svc):
        mock_system_svc.create_backup = AsyncMock(return_value="backup_done")
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/backup")
        assert resp.status_code == 201

    def test_create_audit_log_failure_swallowed(self, client, mock_system_svc, mock_audit_svc):
        mock_system_svc.create_backup = AsyncMock(return_value={"backup_id": "b1"})
        mock_audit_svc.log = AsyncMock(side_effect=RuntimeError("audit down"))
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/backup")
        # Should still succeed even if audit log fails
        assert resp.status_code == 201


# ════════════════════════════════════════════════════════════
#  POST /restore
# ════════════════════════════════════════════════════════════


class TestRestoreBackup:
    def test_restore_success(self, client, mock_system_svc, mock_audit_svc):
        mock_system_svc.restore_backup = AsyncMock(return_value=True)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post(
                "/api/v1/system/restore",
                json={"backup_id": "backup_20260101", "confirm": True},
            )
        assert resp.status_code == 200
        mock_audit_svc.log.assert_awaited()

    def test_restore_not_confirmed_returns_400(self, client):
        resp = client.post(
            "/api/v1/system/restore",
            json={"backup_id": "backup_20260101", "confirm": False},
        )
        assert resp.status_code == 400

    def test_restore_invalid_backup_id_returns_400(self, client):
        resp = client.post(
            "/api/v1/system/restore",
            json={"backup_id": "../../etc/passwd", "confirm": True},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "ERR_SYS_INVALID_BACKUP_ID"

    def test_restore_not_found_returns_404(self, client, mock_system_svc, mock_audit_svc):
        mock_system_svc.restore_backup = AsyncMock(return_value=False)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post(
                "/api/v1/system/restore",
                json={"backup_id": "backup_missing", "confirm": True},
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_SYS_BACKUP_NOT_FOUND"

    def test_restore_service_error_returns_500(self, client, mock_system_svc):
        mock_system_svc.restore_backup = AsyncMock(side_effect=RuntimeError("corrupt"))
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post(
                "/api/v1/system/restore",
                json={"backup_id": "backup_20260101", "confirm": True},
            )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_SYS_RESTORE_FAILED"


# ════════════════════════════════════════════════════════════
#  DELETE /backup/{backup_id}
# ════════════════════════════════════════════════════════════


class TestDeleteBackup:
    def test_delete_success(self, client, mock_system_svc, mock_audit_svc):
        mock_system_svc.delete_backup = AsyncMock(return_value=True)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.delete("/api/v1/system/backup/backup_20260101")
        assert resp.status_code == 200
        mock_audit_svc.log.assert_awaited()

    def test_delete_invalid_id_returns_400(self, client):
        resp = client.delete("/api/v1/system/backup/backup!@%23")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "ERR_SYS_INVALID_BACKUP_ID"

    def test_delete_not_found_returns_404(self, client, mock_system_svc):
        mock_system_svc.delete_backup = AsyncMock(return_value=False)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.delete("/api/v1/system/backup/backup_missing")
        assert resp.status_code == 404

    def test_delete_service_error_returns_500(self, client, mock_system_svc):
        mock_system_svc.delete_backup = AsyncMock(side_effect=RuntimeError("io"))
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.delete("/api/v1/system/backup/backup_20260101")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  GET /backup/schedule
# ════════════════════════════════════════════════════════════


class TestGetBackupSchedule:
    def test_schedule_success(self, client):
        mock_status = SimpleNamespace(
            enabled=True,
            interval_seconds=3600,
            retain_days=7,
            is_running=True,
            last_backup_time="2026-01-01T12:00:00Z",
            last_backup_duration_ms=500,
            backup_count=3,
            total_backup_size_bytes=10240,
        )
        mock_sched = MagicMock()
        mock_sched.status = mock_status
        mock_sched.get_backup_list = MagicMock(return_value=[{"id": "b1"}])
        with patch(
            "edgelite.services.backup_scheduler.get_backup_scheduler",
            return_value=mock_sched,
        ):
            resp = client.get("/api/v1/system/backup/schedule")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["enabled"] is True
        assert data["interval_seconds"] == 3600
        assert data["backups"] == [{"id": "b1"}]

    def test_schedule_error_returns_500(self, client):
        with patch(
            "edgelite.services.backup_scheduler.get_backup_scheduler",
            side_effect=RuntimeError("init fail"),
        ):
            resp = client.get("/api/v1/system/backup/schedule")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  POST /backup/schedule/trigger
# ════════════════════════════════════════════════════════════


class TestTriggerBackup:
    def test_trigger_success(self, client, mock_audit_svc):
        mock_result = SimpleNamespace(
            component="main_db",
            success=True,
            backup_path="/tmp/backup.db",
            error=None,
            duration_ms=250,
        )
        mock_sched = AsyncMock()
        mock_sched.run_backup = AsyncMock(return_value=[mock_result])
        with patch(
            "edgelite.services.backup_scheduler.get_backup_scheduler",
            return_value=mock_sched,
        ), patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/backup/schedule/trigger")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "results" in data
        assert data["results"][0]["component"] == "main_db"
        mock_audit_svc.log.assert_awaited()

    def test_trigger_value_error_returns_422(self, client, mock_audit_svc):
        mock_sched = AsyncMock()
        mock_sched.run_backup = AsyncMock(side_effect=ValueError("bad config"))
        with patch(
            "edgelite.services.backup_scheduler.get_backup_scheduler",
            return_value=mock_sched,
        ), patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/backup/schedule/trigger")
        assert resp.status_code == 422

    def test_trigger_generic_error_returns_500(self, client):
        mock_sched = AsyncMock()
        mock_sched.run_backup = AsyncMock(side_effect=RuntimeError("io"))
        with patch(
            "edgelite.services.backup_scheduler.get_backup_scheduler",
            return_value=mock_sched,
        ), patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/backup/schedule/trigger")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  GET /cascade/topology
# ════════════════════════════════════════════════════════════


class TestGetCascadeTopology:
    def test_topology_standalone_when_no_manager(self, client):
        with patch("edgelite.api.system._get_cascade_manager", return_value=None):
            resp = client.get("/api/v1/system/cascade/topology")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "standalone"
        assert data["children"] == []

    def test_topology_with_manager(self, client):
        mock_mgr = MagicMock()
        mock_neighbor = SimpleNamespace(
            neighbor_id="n1", host="192.168.1.10", port=8080, role="parent", last_seen="2026-01-01"
        )
        mock_topology = SimpleNamespace(
            local_id="gw1",
            status="child",
            parent_id="parent_gw",
            children=["child1"],
            peers=[mock_neighbor],
            updated_at="2026-01-01T12:00:00Z",
        )
        mock_mgr.build_topology = MagicMock(return_value=mock_topology)
        with patch("edgelite.api.system._get_cascade_manager", return_value=mock_mgr):
            resp = client.get("/api/v1/system/cascade/topology")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["local_id"] == "gw1"
        assert data["status"] == "child"
        assert len(data["peers"]) == 1
        assert data["peers"][0]["neighbor_id"] == "n1"

    def test_topology_error_returns_500(self, client):
        with patch(
            "edgelite.api.system._get_cascade_manager",
            side_effect=RuntimeError("init fail"),
        ):
            resp = client.get("/api/v1/system/cascade/topology")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  GET /cascade/neighbors
# ════════════════════════════════════════════════════════════


class TestGetCascadeNeighbors:
    def test_neighbors_empty_when_no_manager(self, client):
        with patch("edgelite.api.system._get_cascade_manager", return_value=None):
            resp = client.get("/api/v1/system/cascade/neighbors")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_neighbors_with_manager(self, client):
        mock_mgr = MagicMock()
        mock_neighbor = SimpleNamespace(
            neighbor_id="n1",
            host="192.168.1.10",
            port=8080,
            role="peer",
            properties={"version": "1.0"},
            last_seen="2026-01-01",
        )
        mock_mgr.neighbors = [mock_neighbor]
        with patch("edgelite.api.system._get_cascade_manager", return_value=mock_mgr):
            resp = client.get("/api/v1/system/cascade/neighbors")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["neighbor_id"] == "n1"
        assert data[0]["properties"] == {"version": "1.0"}


# ════════════════════════════════════════════════════════════
#  POST /cascade/config
# ════════════════════════════════════════════════════════════


class TestUpdateCascadeConfig:
    def test_config_update_success(self, client):
        mock_mgr = AsyncMock()
        mock_mgr.update_config = AsyncMock(return_value=None)
        with patch("edgelite.api.system._get_cascade_manager", return_value=mock_mgr):
            resp = client.post(
                "/api/v1/system/cascade/config",
                json={"parent_host": "192.168.1.100", "parent_port": 8080, "role": "child"},
            )
        assert resp.status_code == 200
        mock_mgr.update_config.assert_awaited_once()

    def test_config_empty_body_returns_400(self, client):
        resp = client.post("/api/v1/system/cascade/config", json={})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "ERR_CASCADE_INVALID_CONFIG"

    def test_config_ssrf_blocked(self, client):
        resp = client.post(
            "/api/v1/system/cascade/config",
            json={"parent_host": "127.0.0.1", "parent_port": 8080},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "ERR_SSRF_BLOCKED"

    def test_config_no_manager_returns_503(self, client):
        with patch("edgelite.api.system._get_cascade_manager", return_value=None):
            resp = client.post(
                "/api/v1/system/cascade/config",
                json={"parent_host": "192.168.1.100", "parent_port": 8080},
            )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "ERR_CASCADE_NOT_ENABLED"

    def test_config_update_error_returns_500(self, client):
        mock_mgr = AsyncMock()
        mock_mgr.update_config = AsyncMock(side_effect=RuntimeError("update fail"))
        with patch("edgelite.api.system._get_cascade_manager", return_value=mock_mgr):
            resp = client.post(
                "/api/v1/system/cascade/config",
                json={"parent_host": "192.168.1.100", "parent_port": 8080},
            )
        assert resp.status_code == 500

    def test_config_extra_field_rejected(self, client):
        resp = client.post(
            "/api/v1/system/cascade/config",
            json={"parent_host": "192.168.1.100", "unknown_field": "bad"},
        )
        assert resp.status_code == 422  # Pydantic extra=forbid

    def test_config_invalid_port_validation(self, client):
        resp = client.post(
            "/api/v1/system/cascade/config",
            json={"parent_port": 70000},  # > 65535
        )
        assert resp.status_code == 422


# ════════════════════════════════════════════════════════════
#  DELETE /cascade/neighbors/{neighbor_id}
# ════════════════════════════════════════════════════════════


class TestRemoveCascadeNeighbor:
    def test_remove_success(self, client):
        mock_mgr = AsyncMock()
        mock_mgr.remove_neighbor = AsyncMock(return_value=True)
        with patch("edgelite.api.system._get_cascade_manager", return_value=mock_mgr):
            resp = client.delete("/api/v1/system/cascade/neighbors/n1")
        assert resp.status_code == 200

    def test_remove_not_found_returns_404(self, client):
        mock_mgr = AsyncMock()
        mock_mgr.remove_neighbor = AsyncMock(return_value=False)
        with patch("edgelite.api.system._get_cascade_manager", return_value=mock_mgr):
            resp = client.delete("/api/v1/system/cascade/neighbors/n1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_CASCADE_NEIGHBOR_NOT_FOUND"

    def test_remove_no_manager_returns_503(self, client):
        with patch("edgelite.api.system._get_cascade_manager", return_value=None):
            resp = client.delete("/api/v1/system/cascade/neighbors/n1")
        assert resp.status_code == 503


# ════════════════════════════════════════════════════════════
#  POST /config/reload
# ════════════════════════════════════════════════════════════


class TestReloadConfig:
    def test_reload_success(self, client, mock_audit_svc):
        mock_config = SimpleNamespace(_config_version=42)
        with patch("edgelite.config.reload_config", return_value=(mock_config, [])), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.post("/api/v1/system/config/reload")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["config_version"] == 42
        mock_audit_svc.log.assert_awaited()

    def test_reload_with_changed_keys(self, client):
        mock_config = SimpleNamespace(_config_version=43)
        with patch(
            "edgelite.config.reload_config",
            return_value=(mock_config, ["mqtt.host", "influxdb.url"]),
        ), patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/config/reload")
        assert resp.status_code == 200
        assert "mqtt.host" in resp.json()["data"]["changed_sensitive_keys"]

    def test_reload_error_returns_500(self, client, mock_audit_svc):
        with patch("edgelite.config.reload_config", side_effect=RuntimeError("parse fail")), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.post("/api/v1/system/config/reload")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_CONFIG_RELOAD_FAILED"


# ════════════════════════════════════════════════════════════
#  GET /config
# ════════════════════════════════════════════════════════════


class TestGetCurrentConfig:
    def test_get_config_success(self, client):
        with patch("edgelite.config.get_sanitized_config", return_value={"server": {"port": 8000}}):
            resp = client.get("/api/v1/system/config")
        assert resp.status_code == 200
        assert resp.json()["data"]["server"]["port"] == 8000

    def test_get_config_error_returns_500(self, client):
        with patch("edgelite.config.get_sanitized_config", side_effect=RuntimeError("fail")):
            resp = client.get("/api/v1/system/config")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  PUT /config/{section}
# ════════════════════════════════════════════════════════════


class TestUpdateConfigSection:
    def test_update_success(self, client, mock_audit_svc):
        mock_config = MagicMock()
        mock_config.scheduler = SimpleNamespace(model_dump=lambda: {"interval": 60})
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "edgelite.config.update_config_section"
        ) as mock_update, patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.put(
                "/api/v1/system/config/scheduler",
                json={"config": {"interval": 120}},
            )
        assert resp.status_code == 200
        mock_update.assert_called_once_with("scheduler", {"interval": 120})
        mock_audit_svc.log.assert_awaited()

    def test_update_denied_section_returns_403(self, client):
        resp = client.put(
            "/api/v1/system/config/security",
            json={"config": {"secret_key": "hacked"}},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "ERR_CONFIG_SECTION_NOT_ALLOWED"

    def test_update_section_not_found_returns_404(self, client):
        mock_config = MagicMock(spec=[])  # no attributes
        with patch("edgelite.config.get_config", return_value=mock_config):
            resp = client.put(
                "/api/v1/system/config/nonexistent",
                json={"config": {"key": "val"}},
            )
        assert resp.status_code == 404

    def test_update_database_denied(self, client):
        resp = client.put(
            "/api/v1/system/config/database",
            json={"config": {"path": "/tmp/x"}},
        )
        assert resp.status_code == 403

    def test_update_influxdb_denied(self, client):
        resp = client.put(
            "/api/v1/system/config/influxdb",
            json={"config": {"url": "http://x"}},
        )
        assert resp.status_code == 403

    def test_update_update_error_returns_500(self, client, mock_audit_svc):
        mock_config = MagicMock()
        mock_config.scheduler = SimpleNamespace(model_dump=lambda: {})
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "edgelite.config.update_config_section",
            side_effect=RuntimeError("write fail"),
        ), patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.put(
                "/api/v1/system/config/scheduler",
                json={"config": {"interval": 120}},
            )
        assert resp.status_code == 500

    def test_update_sensitive_keys_masked_in_audit(self, client, mock_audit_svc):
        mock_config = MagicMock()
        mock_config.scheduler = SimpleNamespace(model_dump=lambda: {})
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "edgelite.config.update_config_section"
        ), patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.put(
                "/api/v1/system/config/scheduler",
                json={"config": {"api_key": "secret123", "normal": "ok"}},
            )
        assert resp.status_code == 200
        # Check audit log was called with masked values
        call_kwargs = mock_audit_svc.log.call_args
        after_value = call_kwargs.kwargs.get("after_value", {})
        assert after_value.get("api_key") == "***"
        assert after_value.get("normal") == "ok"


# ════════════════════════════════════════════════════════════
#  GET /quality/{device_id}
# ════════════════════════════════════════════════════════════


class TestGetDeviceQuality:
    def test_quality_admin_success(self, client, mock_scheduler):
        mock_scheduler.calculate_quality_score = AsyncMock(return_value={"score": 95.0})
        resp = client.get("/api/v1/system/quality/dev1")
        assert resp.status_code == 200
        assert resp.json()["data"]["score"] == 95.0

    def test_quality_non_admin_owned_device(self, mock_system_svc, mock_audit_svc, mock_scheduler):
        app = _build_app("operator", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        mock_scheduler.calculate_quality_score = AsyncMock(return_value={"score": 80.0})
        mock_device_svc = AsyncMock()
        mock_device_svc.get_device = AsyncMock(
            return_value={"device_id": "dev1", "created_by": "u1"}
        )
        mock_app_state = SimpleNamespace(device_service=mock_device_svc)
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/quality/dev1")
        assert resp.status_code == 200

    def test_quality_non_admin_not_found(self, mock_system_svc, mock_audit_svc, mock_scheduler):
        app = _build_app("operator", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        mock_device_svc = AsyncMock()
        mock_device_svc.get_device = AsyncMock(return_value=None)
        mock_app_state = SimpleNamespace(device_service=mock_device_svc)
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/quality/dev1")
        assert resp.status_code == 404

    def test_quality_non_admin_not_owner_no_share(self, mock_system_svc, mock_audit_svc, mock_scheduler):
        app = _build_app("operator", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        mock_device_svc = AsyncMock()
        mock_device_svc.get_device = AsyncMock(
            return_value={"device_id": "dev1", "created_by": "other_user"}
        )
        mock_db = MagicMock()
        mock_share_repo = AsyncMock()
        mock_share_repo.check_user_has_access = AsyncMock(return_value=False)
        mock_app_state = SimpleNamespace(
            device_service=mock_device_svc, database=mock_db
        )
        with patch("edgelite.app._app_state", mock_app_state), patch(
            "edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_share_repo
        ):
            resp = client.get("/api/v1/system/quality/dev1")
        assert resp.status_code == 403

    def test_quality_non_admin_shared_device(self, mock_system_svc, mock_audit_svc, mock_scheduler):
        app = _build_app("operator", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        mock_scheduler.calculate_quality_score = AsyncMock(return_value={"score": 70.0})
        mock_device_svc = AsyncMock()
        mock_device_svc.get_device = AsyncMock(
            return_value={"device_id": "dev1", "created_by": "other_user"}
        )
        mock_db = MagicMock()
        mock_share_repo = AsyncMock()
        mock_share_repo.check_user_has_access = AsyncMock(return_value=True)
        mock_app_state = SimpleNamespace(
            device_service=mock_device_svc, database=mock_db
        )
        with patch("edgelite.app._app_state", mock_app_state), patch(
            "edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_share_repo
        ):
            resp = client.get("/api/v1/system/quality/dev1")
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════
#  GET /circuit-breakers
# ════════════════════════════════════════════════════════════


class TestGetCircuitBreakerStatus:
    def test_circuit_breakers_admin_success(self, client, mock_scheduler):
        mock_scheduler.get_circuit_breaker_status = AsyncMock(
            return_value={"dev1": "closed", "dev2": "open"}
        )
        resp = client.get("/api/v1/system/circuit-breakers")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "dev1" in data

    def test_circuit_breakers_non_admin_filtered(
        self, mock_system_svc, mock_audit_svc, mock_scheduler
    ):
        app = _build_app("operator", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        mock_scheduler.get_circuit_breaker_status = AsyncMock(
            return_value={"dev1": "closed", "dev2": "open"}
        )
        mock_device_svc = AsyncMock()
        mock_device_svc.list_device_ids_by_owner = AsyncMock(return_value=["dev1"])
        mock_db = MagicMock()
        mock_share_repo = AsyncMock()
        mock_share_repo.get_shared_resource_ids = AsyncMock(return_value=set())
        mock_app_state = SimpleNamespace(
            device_service=mock_device_svc, database=mock_db
        )
        with patch("edgelite.app._app_state", mock_app_state), patch(
            "edgelite.storage.sqlite_repo.ResourceShareRepo", return_value=mock_share_repo
        ):
            resp = client.get("/api/v1/system/circuit-breakers")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "dev1" in data
        assert "dev2" not in data

    def test_circuit_breakers_empty_statuses(self, client, mock_scheduler):
        mock_scheduler.get_circuit_breaker_status = AsyncMock(return_value={})
        resp = client.get("/api/v1/system/circuit-breakers")
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════
#  POST /circuit-breakers/{device_id}/reset
# ════════════════════════════════════════════════════════════


class TestResetCircuitBreaker:
    def test_reset_success(self, client, mock_scheduler, mock_audit_svc):
        mock_scheduler.reset_circuit_breaker = AsyncMock(return_value=True)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/circuit-breakers/dev1/reset")
        assert resp.status_code == 200
        mock_audit_svc.log.assert_awaited()

    def test_reset_not_found_returns_404(self, client, mock_scheduler, mock_audit_svc):
        mock_scheduler.reset_circuit_breaker = AsyncMock(return_value=False)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/system/circuit-breakers/dev1/reset")
        assert resp.status_code == 404

    def test_reset_operator_forbidden(self, mock_system_svc, mock_audit_svc, mock_scheduler):
        """reset_circuit_breaker requires SYSTEM_MANAGE; operator doesn't have it."""
        app = _build_app("operator", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        # Operator lacks SYSTEM_MANAGE, so gets 403 before ownership check
        resp = client.post("/api/v1/system/circuit-breakers/dev1/reset")
        assert resp.status_code == 403


# ════════════════════════════════════════════════════════════
#  GET /health/basic (no auth required)
# ════════════════════════════════════════════════════════════


class TestHealthCheckBasic:
    def test_health_basic_success(self, client):
        with patch("psutil.cpu_percent", return_value=50.0), patch(
            "psutil.virtual_memory"
        ) as mock_mem, patch("psutil.disk_usage") as mock_disk:
            mock_mem.return_value = SimpleNamespace(percent=60.0)
            mock_disk.return_value = SimpleNamespace(percent=70.0)
            resp = client.get("/api/v1/system/health/basic")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "healthy"
        assert len(data["components"]) == 3

    def test_health_basic_degraded_high_cpu(self, client):
        with patch("psutil.cpu_percent", return_value=95.0), patch(
            "psutil.virtual_memory"
        ) as mock_mem, patch("psutil.disk_usage") as mock_disk:
            mock_mem.return_value = SimpleNamespace(percent=50.0)
            mock_disk.return_value = SimpleNamespace(percent=50.0)
            resp = client.get("/api/v1/system/health/basic")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "degraded"

    def test_health_basic_psutil_unavailable(self, client):
        # When psutil raises an exception, it should return unknown component
        with patch("psutil.cpu_percent", side_effect=ImportError("no psutil")):
            resp = client.get("/api/v1/system/health/basic")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # Should have at least the "system" component with status "unknown"
        assert any(c["status"] == "unknown" for c in data["components"])


# ════════════════════════════════════════════════════════════
#  GET /ready-status (no auth required)
# ════════════════════════════════════════════════════════════


class TestReadinessCheck:
    def test_ready_true(self, client, mock_system_svc):
        mock_system_svc.get_status = AsyncMock(return_value=_full_status_data())
        resp = client.get("/api/v1/system/ready-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["ready"] is True
        assert data["status"] == "ready"

    def test_ready_false_when_negative(self, client, mock_system_svc):
        status = _full_status_data()
        status["device_total"] = -1
        mock_system_svc.get_status = AsyncMock(return_value=status)
        resp = client.get("/api/v1/system/ready-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["ready"] is False

    def test_ready_false_on_exception(self, client, mock_system_svc):
        mock_system_svc.get_status = AsyncMock(side_effect=RuntimeError("fail"))
        resp = client.get("/api/v1/system/ready-status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["ready"] is False
        assert data["status"] == "not_ready"


# ════════════════════════════════════════════════════════════
#  GET /performance
# ════════════════════════════════════════════════════════════


class TestGetPerformance:
    def test_performance_success(self, client):
        with patch("psutil.cpu_percent", return_value=45.0), patch(
            "psutil.virtual_memory"
        ) as mock_mem, patch("psutil.disk_usage") as mock_disk, patch(
            "psutil.net_io_counters"
        ) as mock_net:
            mock_mem.return_value = SimpleNamespace(
                percent=55.0, used=1048576, total=2097152
            )
            mock_disk.return_value = SimpleNamespace(percent=65.0, used=1073741824, total=2147483648)
            mock_net.return_value = SimpleNamespace(bytes_sent=1024, bytes_recv=2048)
            resp = client.get("/api/v1/system/performance")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["cpu_percent"] == 45.0

    def test_performance_psutil_import_error(self, client):
        # Need to make the import fail inside the endpoint
        import sys as _sys

        original_psutil = _sys.modules.get("psutil")
        _sys.modules["psutil"] = None  # This will cause ImportError on `import psutil`
        try:
            resp = client.get("/api/v1/system/performance")
        finally:
            if original_psutil is not None:
                _sys.modules["psutil"] = original_psutil
            else:
                _sys.modules.pop("psutil", None)
        assert resp.status_code == 200
        assert "error" in resp.json()["data"]

    def test_performance_psutil_exception(self, client):
        with patch("psutil.cpu_percent", side_effect=RuntimeError("fail")):
            resp = client.get("/api/v1/system/performance")
        assert resp.status_code == 200
        assert "error" in resp.json()["data"]


# ════════════════════════════════════════════════════════════
#  GET /retention
# ════════════════════════════════════════════════════════════


class TestGetRetentionPolicy:
    def test_retention_success(self, client):
        mock_config = SimpleNamespace(influxdb=SimpleNamespace(retention_days=60))
        with patch("edgelite.config.get_config", return_value=mock_config):
            resp = client.get("/api/v1/system/retention")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["history_retention_days"] == 60
        assert data["alarm_retention_days"] == 365

    def test_retention_default_when_no_attr(self, client):
        mock_config = SimpleNamespace(influxdb=SimpleNamespace())
        with patch("edgelite.config.get_config", return_value=mock_config):
            resp = client.get("/api/v1/system/retention")
        assert resp.status_code == 200
        assert resp.json()["data"]["history_retention_days"] == 30

    def test_retention_error_returns_500(self, client):
        with patch("edgelite.config.get_config", side_effect=RuntimeError("fail")):
            resp = client.get("/api/v1/system/retention")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  PUT /retention
# ════════════════════════════════════════════════════════════


class TestUpdateRetentionPolicy:
    def test_update_history_success(self, client, mock_audit_svc):
        mock_config = SimpleNamespace(influxdb=SimpleNamespace(retention_days=30))
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "edgelite.config.update_config_section"
        ) as mock_update, patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.put(
                "/api/v1/system/retention",
                json={"history_retention_days": 90},
            )
        assert resp.status_code == 200
        mock_update.assert_called_once_with("influxdb", {"retention_days": 90})
        mock_audit_svc.log.assert_awaited()

    def test_update_alarm_only(self, client, mock_audit_svc):
        mock_config = SimpleNamespace(influxdb=SimpleNamespace(retention_days=30))
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.put(
                "/api/v1/system/retention",
                json={"alarm_retention_days": 180},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["alarm_retention_days"] == 180

    def test_update_both_none(self, client, mock_audit_svc):
        mock_config = SimpleNamespace(influxdb=SimpleNamespace(retention_days=30))
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.put("/api/v1/system/retention", json={})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["history_retention_days"] == 30
        assert data["alarm_retention_days"] == 365

    def test_update_error_returns_500(self, client, mock_audit_svc):
        with patch("edgelite.config.get_config", side_effect=RuntimeError("fail")), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.put(
                "/api/v1/system/retention",
                json={"history_retention_days": 90},
            )
        assert resp.status_code == 500

    def test_update_invalid_days_validation(self, client):
        # Pydantic ge=1, le=3650 should reject 0
        resp = client.put(
            "/api/v1/system/retention",
            json={"history_retention_days": 0},
        )
        assert resp.status_code == 422

    def test_update_days_too_large_validation(self, client):
        resp = client.put(
            "/api/v1/system/retention",
            json={"history_retention_days": 5000},
        )
        assert resp.status_code == 422


# ════════════════════════════════════════════════════════════
#  GET /cert
# ════════════════════════════════════════════════════════════


class TestGetCertInfo:
    def test_cert_no_cert_configured(self, client):
        mock_config = SimpleNamespace(server=SimpleNamespace(ssl_cert=None, ssl_key=None))
        with patch("edgelite.config.get_config", return_value=mock_config):
            resp = client.get("/api/v1/system/cert")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["has_cert"] is False
        assert data["cert_path"] is None

    def test_cert_has_cert_paths(self, client):
        mock_config = SimpleNamespace(
            server=SimpleNamespace(ssl_cert="/path/cert.pem", ssl_key="/path/key.pem")
        )
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "os.path.exists", return_value=False
        ):
            resp = client.get("/api/v1/system/cert")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["has_cert"] is True
        assert data["cert_path"] == "/path/cert.pem"
        assert data["expiry"] is None

    def test_cert_with_valid_cert_file(self, client):
        mock_config = SimpleNamespace(
            server=SimpleNamespace(ssl_cert="/path/cert.pem", ssl_key="/path/key.pem")
        )
        mock_cert = MagicMock()
        mock_cert.not_valid_after.isoformat.return_value = "2026-12-31T23:59:59"
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "os.path.exists", return_value=True
        ), patch("edgelite.api.system._read_file_sync", return_value=b"cert data"), patch(
            "cryptography.x509.load_pem_x509_certificate", return_value=mock_cert
        ), patch(
            "cryptography.hazmat.backends.default_backend"
        ):
            resp = client.get("/api/v1/system/cert")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["has_cert"] is True
        assert data["expiry"] == "2026-12-31T23:59:59"

    def test_cert_error_returns_500(self, client):
        with patch("edgelite.config.get_config", side_effect=RuntimeError("fail")):
            resp = client.get("/api/v1/system/cert")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  POST /cert/rotate
# ════════════════════════════════════════════════════════════


class TestRotateCert:
    def test_rotate_no_cert_paths(self, client):
        mock_config = SimpleNamespace(mqtt_server=SimpleNamespace(tls=None))
        with patch("edgelite.config.get_config", return_value=mock_config):
            resp = client.post("/api/v1/system/cert/rotate")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["certificates"] == {}

    def test_rotate_missing_cert_file(self, client):
        mock_config = SimpleNamespace(
            mqtt_server=SimpleNamespace(
                tls=SimpleNamespace(ca_path="/tmp/ca.pem", cert_path="/tmp/cert.pem")
            )
        )
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "pathlib.Path.exists", return_value=False
        ):
            resp = client.post("/api/v1/system/cert/rotate")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["certificates"]["mqtt_ca"]["status"] == "missing"

    def test_rotate_valid_cert(self, client):
        mock_config = SimpleNamespace(
            mqtt_server=SimpleNamespace(
                tls=SimpleNamespace(ca_path="/tmp/ca.pem", cert_path=None)
            )
        )
        mock_cert_mgr = MagicMock()
        mock_cert_mgr.validate_cert = MagicMock(
            return_value={"valid": True, "not_after": "2026-12-31", "days_remaining": 180}
        )
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "pathlib.Path.exists", return_value=True
        ), patch("edgelite.engine.tls_security.CertManager", return_value=mock_cert_mgr):
            resp = client.post("/api/v1/system/cert/rotate")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["certificates"]["mqtt_ca"]["status"] == "valid"
        assert data["certificates"]["mqtt_ca"]["days_remaining"] == 180

    def test_rotate_cert_error(self, client):
        mock_config = SimpleNamespace(
            mqtt_server=SimpleNamespace(
                tls=SimpleNamespace(ca_path="/tmp/ca.pem", cert_path=None)
            )
        )
        mock_cert_mgr = MagicMock()
        mock_cert_mgr.validate_cert = MagicMock(side_effect=RuntimeError("cert error"))
        with patch("edgelite.config.get_config", return_value=mock_config), patch(
            "pathlib.Path.exists", return_value=True
        ), patch("edgelite.engine.tls_security.CertManager", return_value=mock_cert_mgr):
            resp = client.post("/api/v1/system/cert/rotate")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["certificates"]["mqtt_ca"]["status"] == "error"


# ════════════════════════════════════════════════════════════
#  NTP config helpers
# ════════════════════════════════════════════════════════════


class TestNtpConfigHelpers:
    def test_load_ntp_config_file_not_exists(self, tmp_path):
        with patch("edgelite.api.system._NTP_CONFIG_FILE", str(tmp_path / "nonexistent.json")):
            result = _load_ntp_config()
        assert result == {"enabled": False, "server": "pool.ntp.org"}

    def test_load_ntp_config_valid_file(self, tmp_path):
        config_file = tmp_path / "ntp.json"
        config_file.write_text(json.dumps({"enabled": True, "server": "time.google.com"}))
        with patch("edgelite.api.system._NTP_CONFIG_FILE", str(config_file)):
            result = _load_ntp_config()
        assert result["enabled"] is True
        assert result["server"] == "time.google.com"

    def test_load_ntp_config_corrupt_file(self, tmp_path):
        config_file = tmp_path / "ntp.json"
        config_file.write_text("{invalid json")
        with patch("edgelite.api.system._NTP_CONFIG_FILE", str(config_file)):
            result = _load_ntp_config()
        assert result == {"enabled": False, "server": "pool.ntp.org"}

    def test_load_ntp_config_defaults_when_missing_keys(self, tmp_path):
        config_file = tmp_path / "ntp.json"
        config_file.write_text(json.dumps({}))
        with patch("edgelite.api.system._NTP_CONFIG_FILE", str(config_file)):
            result = _load_ntp_config()
        assert result["enabled"] is False
        assert result["server"] == "pool.ntp.org"

    def test_save_ntp_config_success(self, tmp_path):
        config_file = tmp_path / "ntp.json"
        with patch("edgelite.api.system._NTP_CONFIG_FILE", str(config_file)):
            _save_ntp_config({"enabled": True, "server": "time.cloudflare.com"})
        data = json.loads(config_file.read_text())
        assert data["enabled"] is True
        assert data["server"] == "time.cloudflare.com"

    def test_save_ntp_config_creates_dir(self, tmp_path):
        config_file = tmp_path / "subdir" / "ntp.json"
        with patch("edgelite.api.system._NTP_CONFIG_FILE", str(config_file)):
            _save_ntp_config({"enabled": False, "server": "pool.ntp.org"})
        assert config_file.exists()

    def test_save_ntp_config_error_raises(self, tmp_path):
        with patch("edgelite.api.system._NTP_CONFIG_FILE", "/nonexistent/path/ntp.json"), patch(
            "os.makedirs", side_effect=OSError("permission denied")
        ):
            with pytest.raises(OSError):
                _save_ntp_config({"enabled": True, "server": "x"})


# ════════════════════════════════════════════════════════════
#  GET /ntp
# ════════════════════════════════════════════════════════════


class TestGetNtpConfig:
    def test_ntp_get_success(self, client):
        with patch("edgelite.api.system._load_ntp_config", return_value={
            "enabled": True,
            "server": "time.google.com",
        }), patch("edgelite.api.system._get_ntp_sync_status", return_value="synced"):
            resp = client.get("/api/v1/system/ntp")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["enabled"] is True
        assert data["server"] == "time.google.com"
        assert data["sync_status"] == "synced"
        assert "current_time" in data

    def test_ntp_get_error_returns_500(self, client):
        with patch("edgelite.api.system._load_ntp_config", side_effect=RuntimeError("fail")):
            resp = client.get("/api/v1/system/ntp")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  PUT /ntp
# ════════════════════════════════════════════════════════════


class TestUpdateNtpConfig:
    def test_ntp_update_success(self, client, mock_audit_svc):
        with patch("edgelite.api.system._load_ntp_config", return_value={
            "enabled": False,
            "server": "old.pool.ntp.org",
        }), patch("edgelite.api.system._save_ntp_config"), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.put(
                "/api/v1/system/ntp",
                json={"enabled": True, "server": "time.google.com"},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["enabled"] is True
        assert data["server"] == "time.google.com"
        mock_audit_svc.log.assert_awaited()

    def test_ntp_update_ip_server(self, client, mock_audit_svc):
        with patch("edgelite.api.system._save_ntp_config"), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.put(
                "/api/v1/system/ntp",
                json={"enabled": True, "server": "192.168.1.1"},
            )
        assert resp.status_code == 200

    def test_ntp_update_empty_server_returns_400(self, client):
        resp = client.put(
            "/api/v1/system/ntp",
            json={"enabled": True, "server": "  "},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "ERR_CONFIG_VALIDATION_FAILED"

    def test_ntp_update_invalid_server_returns_400(self, client):
        resp = client.put(
            "/api/v1/system/ntp",
            json={"enabled": True, "server": "invalid server!"},
        )
        assert resp.status_code == 400

    def test_ntp_update_missing_server_returns_422(self, client):
        resp = client.put(
            "/api/v1/system/ntp",
            json={"enabled": True},
        )
        assert resp.status_code == 422

    def test_ntp_update_save_error_returns_500(self, client, mock_audit_svc):
        with patch("edgelite.api.system._save_ntp_config", side_effect=OSError("disk")), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.put(
                "/api/v1/system/ntp",
                json={"enabled": True, "server": "time.google.com"},
            )
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  GET /migration/status
# ════════════════════════════════════════════════════════════


class TestGetMigrationStatus:
    def test_migration_status_admin_full_error(self, client):
        mock_app_state = SimpleNamespace(_migration_status={
            "current_status": "failed",
            "last_updated": "2026-01-01T12:00:00Z",
            "last_failure": {
                "timestamp": "2026-01-01T12:00:00Z",
                "error": "Migration failed: column already exists",
            },
        })
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/migration/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["current_status"] == "failed"
        assert data["last_failure"]["error"] == "Migration failed: column already exists"

    def test_migration_status_non_admin_truncated_error(
        self, mock_system_svc, mock_audit_svc, mock_scheduler
    ):
        app = _build_app("viewer", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        long_error = "x" * 300
        mock_app_state = SimpleNamespace(_migration_status={
            "current_status": "failed",
            "last_updated": "2026-01-01T12:00:00Z",
            "last_failure": {
                "timestamp": "2026-01-01T12:00:00Z",
                "error": long_error,
            },
        })
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/migration/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["last_failure"]["error"].endswith("...")

    def test_migration_status_no_failure(self, client):
        mock_app_state = SimpleNamespace(_migration_status={
            "current_status": "success",
            "last_updated": "2026-01-01T12:00:00Z",
        })
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/migration/status")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["current_status"] == "success"
        assert "last_failure" not in data

    def test_migration_status_error_returns_500(self, client):
        class ExplodingState:
            def __getattr__(self, name):
                raise RuntimeError("state fail")

        with patch("edgelite.app._app_state", ExplodingState()):
            resp = client.get("/api/v1/system/migration/status")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  POST /migration/retry
# ════════════════════════════════════════════════════════════


class TestRetryMigration:
    def test_retry_non_admin_forbidden(self, mock_system_svc, mock_audit_svc, mock_scheduler):
        app = _build_app("operator", mock_system_svc, mock_audit_svc, mock_scheduler)
        client = TestClient(app)
        resp = client.post("/api/v1/system/migration/retry")
        assert resp.status_code == 403

    def test_retry_no_app_state_returns_503(self, client, mock_audit_svc):
        with patch("edgelite.app._app_state", None), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.post("/api/v1/system/migration/retry")
        assert resp.status_code == 503

    def test_retry_no_database_returns_503(self, client, mock_audit_svc):
        mock_app_state = SimpleNamespace(database=None)
        with patch("edgelite.app._app_state", mock_app_state), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.post("/api/v1/system/migration/retry")
        assert resp.status_code == 503

    def test_retry_success(self, client, mock_audit_svc):
        mock_db = AsyncMock()
        mock_db._update_migration_status = AsyncMock()
        mock_db._migrate = AsyncMock(return_value=True)
        mock_conn_cm = AsyncMock()
        mock_conn_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_conn_cm.__aexit__ = AsyncMock(return_value=False)
        mock_db.engine.begin = MagicMock(return_value=mock_conn_cm)
        mock_app_state = SimpleNamespace(database=mock_db)
        with patch("edgelite.app._app_state", mock_app_state), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.post("/api/v1/system/migration/retry")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "success"
        mock_audit_svc.log.assert_awaited()

    def test_retry_migration_returns_false(self, client, mock_audit_svc):
        mock_db = AsyncMock()
        mock_db._update_migration_status = AsyncMock()
        mock_db._migrate = AsyncMock(return_value=False)
        mock_conn_cm = AsyncMock()
        mock_conn_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_conn_cm.__aexit__ = AsyncMock(return_value=False)
        mock_db.engine.begin = MagicMock(return_value=mock_conn_cm)
        mock_app_state = SimpleNamespace(database=mock_db)
        with patch("edgelite.app._app_state", mock_app_state), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.post("/api/v1/system/migration/retry")
        assert resp.status_code == 500

    def test_retry_value_error_returns_422(self, client, mock_audit_svc):
        mock_db = AsyncMock()
        mock_db._update_migration_status = AsyncMock()
        mock_db._migrate = AsyncMock(side_effect=ValueError("bad schema"))
        mock_conn_cm = AsyncMock()
        mock_conn_cm.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_conn_cm.__aexit__ = AsyncMock(return_value=False)
        mock_db.engine.begin = MagicMock(return_value=mock_conn_cm)
        mock_app_state = SimpleNamespace(database=mock_db)
        with patch("edgelite.app._app_state", mock_app_state), patch(
            "edgelite.api.auth._get_client_ip", return_value="10.0.0.1"
        ):
            resp = client.post("/api/v1/system/migration/retry")
        assert resp.status_code == 422


# ════════════════════════════════════════════════════════════
#  GET /migration/history
# ════════════════════════════════════════════════════════════


class TestGetMigrationHistory:
    def test_history_with_entries(self, client):
        mock_app_state = SimpleNamespace(_migration_status={
            "current_status": "success",
            "last_updated": "2026-01-01T12:00:00Z",
            "last_failure": {
                "timestamp": "2025-12-31T10:00:00Z",
                "error": "column exists",
            },
        })
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/migration/history")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["history"]) == 2
        # Sorted by timestamp descending
        assert data["history"][0]["timestamp"] == "2026-01-01T12:00:00Z"

    def test_history_empty(self, client):
        mock_app_state = SimpleNamespace(_migration_status={})
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/migration/history")
        assert resp.status_code == 200
        assert resp.json()["data"]["history"] == []

    def test_history_long_error_truncated(self, client):
        long_error = "x" * 200
        mock_app_state = SimpleNamespace(_migration_status={
            "last_failure": {
                "timestamp": "2026-01-01T12:00:00Z",
                "error": long_error,
            },
        })
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/migration/history")
        assert resp.status_code == 200
        data = resp.json()["data"]
        error_preview = data["history"][0]["error_preview"]
        assert error_preview.endswith("...")

    def test_history_error_returns_500(self, client):
        class ExplodingState:
            def __getattr__(self, name):
                raise RuntimeError("state fail")

        with patch("edgelite.app._app_state", ExplodingState()):
            resp = client.get("/api/v1/system/migration/history")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  GET /locks/status
# ════════════════════════════════════════════════════════════


class TestGetLockStatus:
    def test_locks_success(self, client):
        mock_db = MagicMock()
        mock_db.get_lock_status = MagicMock(return_value={"table_locks": [], "global_locks": []})
        mock_app_state = SimpleNamespace(database=mock_db)
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/locks/status")
        assert resp.status_code == 200

    def test_locks_no_app_state_returns_503(self, client):
        with patch("edgelite.app._app_state", None):
            resp = client.get("/api/v1/system/locks/status")
        assert resp.status_code == 503

    def test_locks_no_database_returns_503(self, client):
        mock_app_state = SimpleNamespace(database=None)
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/locks/status")
        assert resp.status_code == 503

    def test_locks_error_returns_500(self, client):
        mock_db = MagicMock()
        mock_db.get_lock_status = MagicMock(side_effect=RuntimeError("fail"))
        mock_app_state = SimpleNamespace(database=mock_db)
        with patch("edgelite.app._app_state", mock_app_state):
            resp = client.get("/api/v1/system/locks/status")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  GET /network
# ════════════════════════════════════════════════════════════


class TestGetNetworkInfo:
    def test_network_success(self, client):
        with patch("socket.gethostname", return_value="test-host"), patch(
            "socket.gethostbyname", return_value="192.168.1.100"
        ), patch("psutil.net_if_addrs") as mock_addrs, patch(
            "psutil.net_if_stats"
        ) as mock_stats:
            mock_addrs.return_value = {
                "eth0": [
                    SimpleNamespace(
                        family=SimpleNamespace(name="AF_INET"),
                        address="192.168.1.100",
                        netmask="255.255.255.0",
                        broadcast="192.168.1.255",
                    )
                ]
            }
            mock_stats.return_value = {"eth0": SimpleNamespace(isup=True)}
            resp = client.get("/api/v1/system/network")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["hostname"] == "test-host"
        assert data["local_ip"] == "192.168.1.100"
        assert len(data["interfaces"]) == 1
        assert data["interfaces"][0]["name"] == "eth0"

    def test_network_skips_loopback(self, client):
        with patch("socket.gethostname", return_value="test-host"), patch(
            "socket.gethostbyname", return_value="192.168.1.100"
        ), patch("psutil.net_if_addrs") as mock_addrs, patch(
            "psutil.net_if_stats"
        ) as mock_stats:
            mock_addrs.return_value = {
                "lo": [
                    SimpleNamespace(
                        family=SimpleNamespace(name="AF_INET"),
                        address="127.0.0.1",
                        netmask="255.0.0.0",
                        broadcast="",
                    )
                ],
                "eth0": [
                    SimpleNamespace(
                        family=SimpleNamespace(name="AF_INET"),
                        address="192.168.1.100",
                        netmask="255.255.255.0",
                        broadcast="192.168.1.255",
                    )
                ],
            }
            mock_stats.return_value = {
                "lo": SimpleNamespace(isup=True),
                "eth0": SimpleNamespace(isup=True),
            }
            resp = client.get("/api/v1/system/network")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # loopback should be skipped
        assert len(data["interfaces"]) == 1
        assert data["interfaces"][0]["address"] == "192.168.1.100"

    def test_network_gethostbyname_fails(self, client):
        with patch("socket.gethostname", return_value="test-host"), patch(
            "socket.gethostbyname", side_effect=Exception("dns fail")
        ), patch("psutil.net_if_addrs", return_value={}), patch(
            "psutil.net_if_stats", return_value={}
        ):
            resp = client.get("/api/v1/system/network")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["local_ip"] == "127.0.0.1"

    def test_network_error_returns_500(self, client):
        with patch("socket.gethostname", side_effect=RuntimeError("fail")):
            resp = client.get("/api/v1/system/network")
        assert resp.status_code == 500


# ════════════════════════════════════════════════════════════
#  _notify_services_reload (helper function)
# ════════════════════════════════════════════════════════════


class TestNotifyServicesReload:
    async def test_notify_mqtt(self):
        mock_mqtt = AsyncMock()
        mock_mqtt.on_config_changed = AsyncMock()
        mock_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mqtt_forwarder=mock_mqtt)))
        await _notify_services_reload(mock_request, ["mqtt.host"])
        mock_mqtt.on_config_changed.assert_awaited_once()

    async def test_notify_mqtt_no_forwarder(self):
        mock_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        await _notify_services_reload(mock_request, ["mqtt.host"])

    async def test_notify_mqtt_no_on_config_changed(self):
        mock_mqtt = MagicMock()  # no on_config_changed attr
        mock_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mqtt_forwarder=mock_mqtt)))
        await _notify_services_reload(mock_request, ["mqtt.host"])

    async def test_notify_mqtt_on_config_changed_error(self):
        mock_mqtt = AsyncMock()
        mock_mqtt.on_config_changed = AsyncMock(side_effect=RuntimeError("fail"))
        mock_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mqtt_forwarder=mock_mqtt)))
        await _notify_services_reload(mock_request, ["mqtt.host"])

    async def test_notify_influxdb(self):
        mock_influx = AsyncMock()
        mock_influx.on_config_changed = AsyncMock()
        mock_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(influx_storage=mock_influx))
        )
        await _notify_services_reload(mock_request, ["influxdb.url"])
        mock_influx.on_config_changed.assert_awaited_once()

    async def test_notify_database(self):
        mock_device_svc = AsyncMock()
        mock_device_svc.on_config_changed = AsyncMock()
        mock_request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(device_service=mock_device_svc))
        )
        await _notify_services_reload(mock_request, ["database.path"])
        mock_device_svc.on_config_changed.assert_awaited_once()

    async def test_notify_security_just_logs(self):
        mock_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        await _notify_services_reload(mock_request, ["security.secret_key"])

    async def test_notify_no_related_keys(self):
        mock_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        await _notify_services_reload(mock_request, ["other.key"])


# ════════════════════════════════════════════════════════════
#  _get_cascade_manager (helper function)
# ════════════════════════════════════════════════════════════


class TestGetCascadeManager:
    def test_returns_cascade_manager(self):
        from edgelite.api.system import _get_cascade_manager

        mock_mgr = MagicMock()
        mock_app_state = SimpleNamespace(cascade_manager=mock_mgr)
        with patch("edgelite.app._app_state", mock_app_state):
            result = _get_cascade_manager()
        assert result is mock_mgr

    def test_no_cascade_manager_attr_returns_none(self):
        from edgelite.api.system import _get_cascade_manager

        mock_app_state = SimpleNamespace()
        with patch("edgelite.app._app_state", mock_app_state):
            result = _get_cascade_manager()
        assert result is None

    def test_generic_exception_returns_none(self):
        from edgelite.api.system import _get_cascade_manager

        class ExplodingState:
            def __getattr__(self, name):
                raise RuntimeError("fail")

        with patch("edgelite.app._app_state", ExplodingState()):
            result = _get_cascade_manager()
        assert result is None


# ════════════════════════════════════════════════════════════
#  Pydantic model validation
# ════════════════════════════════════════════════════════════


class TestPydanticModels:
    def test_config_section_update_none_config(self):
        model = ConfigSectionUpdate(config=None)
        assert model.config is None

    def test_config_section_update_with_config(self):
        model = ConfigSectionUpdate(config={"key": "value"})
        assert model.config == {"key": "value"}

    def test_retention_policy_update_all_none(self):
        model = RetentionPolicyUpdate()
        assert model.history_retention_days is None
        assert model.alarm_retention_days is None

    def test_retention_policy_update_with_values(self):
        model = RetentionPolicyUpdate(history_retention_days=30, alarm_retention_days=365)
        assert model.history_retention_days == 30
        assert model.alarm_retention_days == 365

    def test_retention_policy_invalid_days_below_1(self):
        with pytest.raises(ValueError):
            RetentionPolicyUpdate(history_retention_days=0)

    def test_retention_policy_invalid_days_above_3650(self):
        with pytest.raises(ValueError):
            RetentionPolicyUpdate(history_retention_days=4000)

    def test_ntp_config_update_defaults(self):
        model = NtpConfigUpdate(server="pool.ntp.org")
        assert model.enabled is False
        assert model.server == "pool.ntp.org"

    def test_ntp_config_update_empty_server_rejected(self):
        with pytest.raises(ValueError):
            NtpConfigUpdate(server="")

    def test_ntp_config_update_server_too_long(self):
        with pytest.raises(ValueError):
            NtpConfigUpdate(server="x" * 256)

    def test_cascade_config_update_all_none(self):
        model = CascadeConfigUpdate()
        assert model.parent_host is None
        assert model.parent_port is None
        assert model.role is None
        assert model.enabled is None
        assert model.auth_key is None

    def test_cascade_config_update_with_values(self):
        model = CascadeConfigUpdate(
            parent_host="192.168.1.1",
            parent_port=8080,
            role="child",
            enabled=True,
            auth_key="secret",
        )
        assert model.parent_host == "192.168.1.1"
        assert model.parent_port == 8080
        assert model.role == "child"

    def test_cascade_config_extra_field_rejected(self):
        with pytest.raises(ValueError):
            CascadeConfigUpdate(unknown_field="bad")

    def test_cascade_config_invalid_port(self):
        with pytest.raises(ValueError):
            CascadeConfigUpdate(parent_port=70000)

    def test_cascade_config_invalid_role(self):
        with pytest.raises(ValueError):
            CascadeConfigUpdate(role="invalid_role")
