"""Alarms API endpoint tests - covers src/edgelite/api/alarms.py

Tests all public API endpoints via FastAPI TestClient with mocked services:
- GET /statistics, /trend, "" (list), /silence, /correlation, /{alarm_id}, /history/{rule_id}
- PUT /{alarm_id}/ack, /{alarm_id}/recover
- DELETE /{alarm_id}, /silence/{silence_id}
- POST /{alarm_id}/suppress, /silence
- Helper functions: _check_alarm_device_access, _get_accessible_device_ids_for_alarms,
  _parse_silence_end_time
- Pydantic models: SuppressRequest, SilenceCreateRequest
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

from datetime import UTC, datetime, timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from edgelite.api.alarms import (
    SilenceCreateRequest,
    SuppressRequest,
    _check_alarm_device_access,
    _get_accessible_device_ids_for_alarms,
    _parse_silence_end_time,
    router,
)
from edgelite.api.deps import (
    get_alarm_service,
    get_audit_service,
    get_current_user,
)
from edgelite.api.error_codes import AlarmErrors, AuthzErrors, CommonErrors, RepoErrors
from edgelite.models.db import StaleDataError
from edgelite.services.alarm_service import AlarmSuppressionRule

# ── Helpers ──


def _make_user(role="admin", user_id="u1", username="admin"):
    return {"user_id": user_id, "username": username, "role": role}


def _make_alarm(**overrides):
    """Return a complete alarm dict matching AlarmResponse fields."""
    base = {
        "alarm_id": "a1",
        "rule_id": "r1",
        "device_id": "d1",
        "severity": "warning",
        "status": "firing",
        "message": "temp high",
        "trigger_value": {"temp": 80},
        "trigger_count": 1,
        "fired_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _build_app(role="admin", alarm_svc=None, audit_svc=None):
    """Build a test FastAPI app with mocked dependencies."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: _make_user(role)
    app.dependency_overrides[get_alarm_service] = lambda: alarm_svc or AsyncMock()
    app.dependency_overrides[get_audit_service] = lambda: audit_svc or AsyncMock()
    return app


def _mock_silence_module(manager=None):
    """Create a fake edgelite.services.alarm_silence module."""
    mod = ModuleType("edgelite.services.alarm_silence")
    mgr = manager or MagicMock()
    mod.get_alarm_silence_manager = lambda: mgr
    return mod


def _mock_correlation_module(manager=None):
    """Create a fake edgelite.services.alarm_correlation module."""
    mod = ModuleType("edgelite.services.alarm_correlation")
    mgr = manager or MagicMock()
    mod.get_alarm_correlation_manager = lambda: mgr
    return mod


# ════════════════════════════════════════════════════════════
#  _parse_silence_end_time helper
# ════════════════════════════════════════════════════════════


class TestParseSilenceEndTime:
    def test_none_returns_none(self):
        assert _parse_silence_end_time(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_silence_end_time("") is None

    def test_valid_iso_with_tz(self):
        dt = _parse_silence_end_time("2026-01-15T10:30:00+00:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.tzinfo is not None

    def test_valid_iso_without_tz_adds_utc(self):
        dt = _parse_silence_end_time("2026-01-15T10:30:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)

    def test_invalid_string_returns_none(self):
        assert _parse_silence_end_time("not-a-date") is None

    def test_non_string_value_returns_none(self):
        assert _parse_silence_end_time(12345) is None

    def test_valid_iso_z_suffix(self):
        dt = _parse_silence_end_time("2026-01-15T10:30:00Z")
        assert dt is not None
        assert dt.year == 2026


# ════════════════════════════════════════════════════════════
#  _check_alarm_device_access helper
# ════════════════════════════════════════════════════════════


class TestCheckAlarmDeviceAccess:
    async def test_admin_returns_immediately(self):
        """Admin user should bypass all access checks."""
        user = _make_user("admin")
        await _check_alarm_device_access("d1", user)

    async def test_no_device_id_returns_immediately(self):
        """No device_id (None) should bypass access checks for non-admin."""
        user = _make_user("operator")
        await _check_alarm_device_access(None, user)

    async def test_empty_device_id_returns_immediately(self):
        """Empty device_id should bypass access checks for non-admin."""
        user = _make_user("operator")
        await _check_alarm_device_access("", user)

    async def test_owned_device_allowed(self):
        """Non-admin user accessing their own device should pass."""
        user = _make_user("operator", user_id="u2")
        mock_device_svc = AsyncMock()
        mock_device_svc.get_device = AsyncMock(return_value={"device_id": "d1", "created_by": "u2"})
        mock_state = SimpleNamespace(device_service=mock_device_svc, database=MagicMock())
        with (
            patch("edgelite.app._app_state", mock_state),
            patch("edgelite.storage.sqlite_repo.ResourceShareRepo"),
        ):
            await _check_alarm_device_access("d1", user)

    async def test_shared_device_allowed(self):
        """Non-admin user accessing a shared device should pass."""
        user = _make_user("operator", user_id="u2")
        mock_device_svc = AsyncMock()
        mock_device_svc.get_device = AsyncMock(return_value={"device_id": "d1", "created_by": "other"})
        mock_share_repo = AsyncMock()
        mock_share_repo.check_user_has_access = AsyncMock(return_value=True)
        mock_state = SimpleNamespace(
            device_service=mock_device_svc,
            database=MagicMock(write_lock=MagicMock()),
        )
        with (
            patch("edgelite.app._app_state", mock_state),
            patch(
                "edgelite.storage.sqlite_repo.ResourceShareRepo",
                return_value=mock_share_repo,
            ),
        ):
            await _check_alarm_device_access("d1", user)

    async def test_no_access_raises_403(self):
        """Non-admin user without ownership or share should get 403."""
        user = _make_user("operator", user_id="u2")
        mock_device_svc = AsyncMock()
        mock_device_svc.get_device = AsyncMock(return_value={"device_id": "d1", "created_by": "other"})
        mock_share_repo = AsyncMock()
        mock_share_repo.check_user_has_access = AsyncMock(return_value=False)
        mock_state = SimpleNamespace(
            device_service=mock_device_svc,
            database=MagicMock(write_lock=MagicMock()),
        )
        with (
            patch("edgelite.app._app_state", mock_state),
            patch(
                "edgelite.storage.sqlite_repo.ResourceShareRepo",
                return_value=mock_share_repo,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _check_alarm_device_access("d1", user)
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == AuthzErrors.RESOURCE_OWNERSHIP_DENIED

    async def test_device_not_found_raises_403(self):
        """Non-admin user accessing non-existent device should get 403."""
        user = _make_user("operator", user_id="u2")
        mock_device_svc = AsyncMock()
        mock_device_svc.get_device = AsyncMock(return_value=None)
        mock_share_repo = AsyncMock()
        mock_share_repo.check_user_has_access = AsyncMock(return_value=False)
        mock_state = SimpleNamespace(
            device_service=mock_device_svc,
            database=MagicMock(write_lock=MagicMock()),
        )
        with (
            patch("edgelite.app._app_state", mock_state),
            patch(
                "edgelite.storage.sqlite_repo.ResourceShareRepo",
                return_value=mock_share_repo,
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await _check_alarm_device_access("d1", user)
        assert exc_info.value.status_code == 403


# ════════════════════════════════════════════════════════════
#  _get_accessible_device_ids_for_alarms helper
# ════════════════════════════════════════════════════════════


class TestGetAccessibleDeviceIds:
    async def test_admin_returns_none(self):
        """Admin user should get None (no filtering needed)."""
        user = _make_user("admin")
        result = await _get_accessible_device_ids_for_alarms(user)
        assert result is None

    async def test_non_admin_returns_owned_and_shared(self):
        """Non-admin should get union of owned and shared device IDs."""
        user = _make_user("operator", user_id="u2")
        mock_device_svc = AsyncMock()
        mock_device_svc.list_device_ids_by_owner = AsyncMock(return_value=["d1", "d2"])
        mock_share_repo = AsyncMock()
        mock_share_repo.get_shared_resource_ids = AsyncMock(return_value={"d3", "d4"})
        mock_state = SimpleNamespace(
            device_service=mock_device_svc,
            database=MagicMock(write_lock=MagicMock()),
        )
        with (
            patch("edgelite.app._app_state", mock_state),
            patch(
                "edgelite.storage.sqlite_repo.ResourceShareRepo",
                return_value=mock_share_repo,
            ),
        ):
            result = await _get_accessible_device_ids_for_alarms(user)
        assert result == {"d1", "d2", "d3", "d4"}

    async def test_non_admin_no_devices_returns_empty_set(self):
        """Non-admin with no owned or shared devices should get empty set."""
        user = _make_user("operator", user_id="u2")
        mock_device_svc = AsyncMock()
        mock_device_svc.list_device_ids_by_owner = AsyncMock(return_value=[])
        mock_share_repo = AsyncMock()
        mock_share_repo.get_shared_resource_ids = AsyncMock(return_value=set())
        mock_state = SimpleNamespace(
            device_service=mock_device_svc,
            database=MagicMock(write_lock=MagicMock()),
        )
        with (
            patch("edgelite.app._app_state", mock_state),
            patch(
                "edgelite.storage.sqlite_repo.ResourceShareRepo",
                return_value=mock_share_repo,
            ),
        ):
            result = await _get_accessible_device_ids_for_alarms(user)
        assert result == set()


# ════════════════════════════════════════════════════════════
#  GET /statistics
# ════════════════════════════════════════════════════════════


class TestGetAlarmStatistics:
    def test_admin_success(self):
        svc = AsyncMock()
        svc.get_statistics_summary = AsyncMock(return_value={"total": 5, "firing": 2})
        svc.get_trend = AsyncMock(return_value=[{"hour": "10", "count": 1}])
        svc.get_top_alarms = AsyncMock(return_value={"top_devices": [{"device_id": "d1"}], "top_rules": []})
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/statistics")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["summary"]["total"] == 5
        assert len(data["trend"]) == 1
        assert len(data["top_devices"]) == 1
        assert data["top_rules"] == []

    def test_admin_with_days_param(self):
        svc = AsyncMock()
        svc.get_statistics_summary = AsyncMock(return_value={})
        svc.get_trend = AsyncMock(return_value=[])
        svc.get_top_alarms = AsyncMock(return_value={})
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/statistics?days=7")
        assert resp.status_code == 200
        # days=7 → hours=168
        svc.get_trend.assert_called_once_with(hours=168, device_ids=None)
        svc.get_top_alarms.assert_called_once_with(hours=168, device_ids=None, limit=10)

    def test_days_below_minimum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/statistics?days=0")
        assert resp.status_code == 422

    def test_days_above_maximum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/statistics?days=366")
        assert resp.status_code == 422

    def test_non_admin_with_accessible_devices(self):
        svc = AsyncMock()
        svc.get_statistics_summary = AsyncMock(return_value={"total": 1})
        svc.get_trend = AsyncMock(return_value=[])
        svc.get_top_alarms = AsyncMock(return_value={})
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value={"d1", "d2"},
        ):
            resp = client.get("/api/v1/alarms/statistics")
        assert resp.status_code == 200

    def test_non_admin_no_accessible_devices_returns_empty(self):
        svc = AsyncMock()
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value=set(),
        ):
            resp = client.get("/api/v1/alarms/statistics")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["summary"] == {}
        assert data["trend"] == []
        # service should not be called
        svc.get_statistics_summary.assert_not_called()

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.get_statistics_summary = AsyncMock(side_effect=RuntimeError("db down"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/statistics")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.LIST_FAILED


# ════════════════════════════════════════════════════════════
#  GET /trend
# ════════════════════════════════════════════════════════════


class TestGetAlarmTrend:
    def test_admin_success(self):
        svc = AsyncMock()
        svc.get_trend = AsyncMock(return_value=[{"hour": "10", "count": 5}, {"hour": "11", "count": 3}])
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/trend")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2

    def test_admin_with_hours_param(self):
        svc = AsyncMock()
        svc.get_trend = AsyncMock(return_value=[])
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/trend?hours=48")
        assert resp.status_code == 200
        svc.get_trend.assert_called_once_with(48, device_ids=None)

    def test_hours_below_minimum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/trend?hours=0")
        assert resp.status_code == 422

    def test_hours_above_maximum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/trend?hours=721")
        assert resp.status_code == 422

    def test_non_admin_with_accessible_devices(self):
        svc = AsyncMock()
        svc.get_trend = AsyncMock(return_value=[{"hour": "10"}])
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value={"d1"},
        ):
            resp = client.get("/api/v1/alarms/trend")
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

    def test_non_admin_no_accessible_devices_returns_empty_list(self):
        svc = AsyncMock()
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value=set(),
        ):
            resp = client.get("/api/v1/alarms/trend")
        assert resp.status_code == 200
        assert resp.json()["data"] == []
        svc.get_trend.assert_not_called()

    def test_service_returns_none_yields_empty_list(self):
        svc = AsyncMock()
        svc.get_trend = AsyncMock(return_value=None)
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/trend")
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.get_trend = AsyncMock(side_effect=RuntimeError("fail"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/trend")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.LIST_FAILED


# ════════════════════════════════════════════════════════════
#  GET "" (list alarms)
# ════════════════════════════════════════════════════════════


class TestListAlarms:
    def test_admin_success_no_filters(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([_make_alarm(alarm_id="a1")], 1))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["data"]) == 1
        assert body["page"] == 1
        assert body["size"] == 20

    def test_admin_with_status_filter(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([], 0))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms?status=firing")
        assert resp.status_code == 200
        call_kwargs = svc.list_alarms.call_args
        assert call_kwargs.args[2] == "firing"

    def test_admin_with_severity_filter(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([], 0))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms?severity=critical")
        assert resp.status_code == 200
        call_kwargs = svc.list_alarms.call_args
        assert call_kwargs.args[3] == "critical"

    def test_admin_with_device_id_filter(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([], 0))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms?device_id=d1")
        assert resp.status_code == 200
        call_kwargs = svc.list_alarms.call_args
        assert call_kwargs.args[4] == "d1"

    def test_admin_with_search_filter(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([], 0))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms?search=temp")
        assert resp.status_code == 200
        call_kwargs = svc.list_alarms.call_args
        assert call_kwargs.args[5] == "temp"

    def test_admin_with_pagination(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([], 0))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms?page=2&size=50")
        assert resp.status_code == 200
        call_kwargs = svc.list_alarms.call_args
        assert call_kwargs.args[0] == 2
        assert call_kwargs.args[1] == 50

    def test_invalid_status_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms?status=invalid")
        assert resp.status_code == 422

    def test_invalid_severity_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms?severity=invalid")
        assert resp.status_code == 422

    def test_non_admin_with_accessible_device(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([_make_alarm()], 1))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value={"d1", "d2"},
        ):
            resp = client.get("/api/v1/alarms?device_id=d1")
        assert resp.status_code == 200

    def test_non_admin_device_not_accessible_returns_403(self):
        svc = AsyncMock()
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value={"d1"},
        ):
            resp = client.get("/api/v1/alarms?device_id=d2")
        assert resp.status_code == 403
        assert resp.json()["detail"] == AuthzErrors.RESOURCE_OWNERSHIP_DENIED

    def test_non_admin_no_device_filter_passes_device_ids(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([], 0))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value={"d1", "d2"},
        ):
            resp = client.get("/api/v1/alarms")
        assert resp.status_code == 200
        call_kwargs = svc.list_alarms.call_args
        assert call_kwargs.kwargs.get("device_ids") == {"d1", "d2"}

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(side_effect=RuntimeError("fail"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.LIST_FAILED

    def test_viewer_allowed(self):
        """Viewer has ALARM_READ permission."""
        svc = AsyncMock()
        svc.list_alarms = AsyncMock(return_value=([], 0))
        app = _build_app("viewer", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value={"d1"},
        ):
            resp = client.get("/api/v1/alarms")
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════
#  GET /silence (list alarm silences)
# ════════════════════════════════════════════════════════════


class TestListAlarmSilences:
    def test_admin_success_no_filters(self):
        mgr = MagicMock()
        mgr.list_silences = MagicMock(return_value=[{"id": "s1", "device_id": "d1", "end_time": None}])
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/silence")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert len(body["data"]) == 1

    def test_status_active_sets_active_only_true(self):
        mgr = MagicMock()
        mgr.list_silences = MagicMock(return_value=[])
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/silence?status=active")
        assert resp.status_code == 200
        mgr.list_silences.assert_called_once()
        assert mgr.list_silences.call_args.kwargs.get("active_only") is True

    def test_status_expired_filters_past_end_time(self):
        mgr = MagicMock()
        past_time = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        future_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        mgr.list_silences = MagicMock(
            return_value=[
                {"id": "s1", "end_time": past_time},
                {"id": "s2", "end_time": future_time},
                {"id": "s3", "end_time": None},
            ]
        )
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/silence?status=expired")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["data"][0]["id"] == "s1"

    def test_status_cancelled_filters_cancelled_at(self):
        mgr = MagicMock()
        mgr.list_silences = MagicMock(
            return_value=[
                {"id": "s1", "cancelled_at": "2026-01-01T00:00:00Z"},
                {"id": "s2", "cancelled_at": None},
            ]
        )
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/silence?status=cancelled")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["data"][0]["id"] == "s1"

    def test_non_admin_filters_by_accessible_devices(self):
        mgr = MagicMock()
        mgr.list_silences = MagicMock(
            return_value=[
                {"id": "s1", "device_id": "d1"},
                {"id": "s2", "device_id": "d2"},
                {"id": "s3", "device_id": ""},
            ]
        )
        app = _build_app("operator")
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
            ),
            patch(
                "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
                return_value={"d1"},
            ),
        ):
            resp = client.get("/api/v1/alarms/silence")
        assert resp.status_code == 200
        body = resp.json()
        # d1 (accessible) and "" (global, kept because not s.get("device_id") is falsy)
        assert body["total"] == 2
        ids = {item["id"] for item in body["data"]}
        assert ids == {"s1", "s3"}

    def test_device_id_filter_passed_to_manager(self):
        mgr = MagicMock()
        mgr.list_silences = MagicMock(return_value=[])
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/silence?device_id=d1")
        assert resp.status_code == 200
        mgr.list_silences.assert_called_once_with(device_id="d1", rule_id="", active_only=False)

    def test_rule_id_filter_passed_to_manager(self):
        mgr = MagicMock()
        mgr.list_silences = MagicMock(return_value=[])
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/silence?rule_id=r1")
        assert resp.status_code == 200
        assert mgr.list_silences.call_args.kwargs.get("rule_id") == "r1"

    def test_pagination_slices_results(self):
        mgr = MagicMock()
        mgr.list_silences = MagicMock(return_value=[{"id": f"s{i}"} for i in range(25)])
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/silence?page=2&size=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 25
        assert len(body["data"]) == 10
        assert body["page"] == 2

    def test_service_error_returns_500(self):
        mgr = MagicMock()
        mgr.list_silences = MagicMock(side_effect=RuntimeError("fail"))
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/silence")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.LIST_FAILED


# ════════════════════════════════════════════════════════════
#  GET /correlation
# ════════════════════════════════════════════════════════════


class TestGetAlarmCorrelations:
    def test_admin_success(self):
        mgr = MagicMock()
        mgr.get_groups = MagicMock(return_value=[{"group_id": "g1", "root_device_id": "d1"}])
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_correlation": _mock_correlation_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/correlation")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data["groups"]) == 1
        assert data["limit"] == 50
        assert data["offset"] == 0

    def test_with_limit_and_offset(self):
        mgr = MagicMock()
        mgr.get_groups = MagicMock(return_value=[])
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_correlation": _mock_correlation_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/correlation?limit=10&offset=5")
        assert resp.status_code == 200
        mgr.get_groups.assert_called_once_with(limit=10, offset=5)

    def test_limit_below_minimum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/correlation?limit=0")
        assert resp.status_code == 422

    def test_limit_above_maximum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/correlation?limit=201")
        assert resp.status_code == 422

    def test_offset_below_minimum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/correlation?offset=-1")
        assert resp.status_code == 422

    def test_non_admin_filters_by_accessible_devices(self):
        mgr = MagicMock()
        mgr.get_groups = MagicMock(
            return_value=[
                {"group_id": "g1", "root_device_id": "d1"},
                {"group_id": "g2", "root_device_id": "d2"},
                {"group_id": "g3", "root_device_id": ""},
            ]
        )
        app = _build_app("operator")
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_correlation": _mock_correlation_module(mgr)},
            ),
            patch(
                "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
                return_value={"d1"},
            ),
        ):
            resp = client.get("/api/v1/alarms/correlation")
        assert resp.status_code == 200
        data = resp.json()["data"]
        # d1 (accessible) and "" (global, kept because not root_device_id is falsy)
        assert len(data["groups"]) == 2

    def test_service_error_returns_500(self):
        mgr = MagicMock()
        mgr.get_groups = MagicMock(side_effect=RuntimeError("fail"))
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_correlation": _mock_correlation_module(mgr)},
        ):
            resp = client.get("/api/v1/alarms/correlation")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.LIST_FAILED


# ════════════════════════════════════════════════════════════
#  GET /{alarm_id}
# ════════════════════════════════════════════════════════════


class TestGetAlarm:
    def test_admin_success(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm())
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/a1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["alarm_id"] == "a1"

    def test_not_found_returns_404(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=None)
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == AlarmErrors.NOT_FOUND

    def test_non_admin_with_access(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            return_value=None,
        ) as mock_check:
            resp = client.get("/api/v1/alarms/a1")
        assert resp.status_code == 200
        mock_check.assert_awaited_once()

    def test_non_admin_without_access_returns_403(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            side_effect=HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED),
        ):
            resp = client.get("/api/v1/alarms/a1")
        assert resp.status_code == 403

    def test_non_admin_alarm_no_device_id_skips_access_check(self):
        """Alarm with no device_id should skip access check for non-admin."""
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id=None))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
        ) as mock_check:
            resp = client.get("/api/v1/alarms/a1")
        assert resp.status_code == 200
        mock_check.assert_not_awaited()

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(side_effect=RuntimeError("db fail"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/a1")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.GET_FAILED


# ════════════════════════════════════════════════════════════
#  GET /history/{rule_id}
# ════════════════════════════════════════════════════════════


class TestGetAlarmHistory:
    def test_admin_success(self):
        svc = AsyncMock()
        svc.get_alarm_history = AsyncMock(return_value=[_make_alarm(alarm_id="a1"), _make_alarm(alarm_id="a2")])
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/history/r1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 2
        svc.get_alarm_history.assert_called_once_with("r1", days=7)

    def test_with_days_param(self):
        svc = AsyncMock()
        svc.get_alarm_history = AsyncMock(return_value=[])
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/history/r1?days=30")
        assert resp.status_code == 200
        svc.get_alarm_history.assert_called_once_with("r1", days=30)

    def test_days_below_minimum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/history/r1?days=0")
        assert resp.status_code == 422

    def test_days_above_maximum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/history/r1?days=91")
        assert resp.status_code == 422

    def test_non_admin_filters_by_accessible_devices(self):
        svc = AsyncMock()
        svc.get_alarm_history = AsyncMock(
            return_value=[
                _make_alarm(alarm_id="a1", device_id="d1"),
                _make_alarm(alarm_id="a2", device_id="d2"),
            ]
        )
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._get_accessible_device_ids_for_alarms",
            return_value={"d1"},
        ):
            resp = client.get("/api/v1/alarms/history/r1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["alarm_id"] == "a1"

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.get_alarm_history = AsyncMock(side_effect=RuntimeError("fail"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.get("/api/v1/alarms/history/r1")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.GET_FAILED


# ════════════════════════════════════════════════════════════
#  PUT /{alarm_id}/ack
# ════════════════════════════════════════════════════════════


class TestAckAlarm:
    def test_admin_success(self):
        svc = AsyncMock()
        svc.ack_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged", acknowledged_by="admin"))
        audit = AsyncMock()
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["status"] == "acknowledged"
        audit.log.assert_awaited_once()

    def test_not_found_returns_404(self):
        svc = AsyncMock()
        svc.ack_alarm = AsyncMock(return_value=None)
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 404
        assert resp.json()["detail"] == AlarmErrors.NOT_FOUND

    def test_already_acked_by_other_returns_409(self):
        svc = AsyncMock()
        svc.ack_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged", acknowledged_by="other_user"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 409
        assert resp.json()["detail"] == AlarmErrors.ALREADY_ACKNOWLEDGED

    def test_already_acked_by_same_user_succeeds(self):
        svc = AsyncMock()
        svc.ack_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged", acknowledged_by="admin"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 200

    def test_non_admin_with_access(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1"))
        svc.ack_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged", acknowledged_by="admin"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            return_value=None,
        ):
            resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 200

    def test_non_admin_without_access_returns_403(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            side_effect=HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED),
        ):
            resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 403

    def test_viewer_forbidden(self):
        """Viewer lacks ALARM_ACK permission."""
        app = _build_app("viewer")
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 403

    def test_audit_log_failure_swallowed(self):
        svc = AsyncMock()
        svc.ack_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged", acknowledged_by="admin"))
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 200

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.ack_alarm = AsyncMock(side_effect=RuntimeError("db fail"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.ACK_FAILED

    def test_non_admin_alarm_no_device_skips_access_check(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id=None))
        svc.ack_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged", acknowledged_by="admin"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
        ) as mock_check:
            resp = client.put("/api/v1/alarms/a1/ack")
        assert resp.status_code == 200
        mock_check.assert_not_awaited()


# ════════════════════════════════════════════════════════════
#  PUT /{alarm_id}/recover
# ════════════════════════════════════════════════════════════


class TestRecoverAlarm:
    def test_admin_success(self):
        svc = AsyncMock()
        svc.clear_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged"))
        audit = AsyncMock()
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 200
        audit.log.assert_awaited_once()

    def test_not_found_returns_404(self):
        svc = AsyncMock()
        svc.clear_alarm = AsyncMock(return_value=None)
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 404
        assert resp.json()["detail"] == AlarmErrors.NOT_FOUND

    def test_already_recovered_returns_409(self):
        svc = AsyncMock()
        svc.clear_alarm = AsyncMock(return_value=_make_alarm(status="recovered"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 409
        assert resp.json()["detail"] == AlarmErrors.ALREADY_RECOVERED

    def test_stale_data_error_returns_409(self):
        svc = AsyncMock()
        svc.clear_alarm = AsyncMock(side_effect=StaleDataError("version conflict"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 409
        assert resp.json()["detail"] == RepoErrors.STALE_DATA_ERROR

    def test_non_admin_with_access(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1"))
        svc.clear_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            return_value=None,
        ):
            resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 200

    def test_non_admin_without_access_returns_403(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            side_effect=HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED),
        ):
            resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 403

    def test_viewer_forbidden(self):
        app = _build_app("viewer")
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 403

    def test_audit_log_failure_swallowed(self):
        svc = AsyncMock()
        svc.clear_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged"))
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 200

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.clear_alarm = AsyncMock(side_effect=RuntimeError("db fail"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.ACK_FAILED

    def test_non_admin_alarm_no_device_skips_access_check(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id=None))
        svc.clear_alarm = AsyncMock(return_value=_make_alarm(status="acknowledged"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
        ) as mock_check:
            resp = client.put("/api/v1/alarms/a1/recover")
        assert resp.status_code == 200
        mock_check.assert_not_awaited()


# ════════════════════════════════════════════════════════════
#  DELETE /{alarm_id}
# ════════════════════════════════════════════════════════════


class TestDeleteAlarm:
    def test_admin_success(self):
        svc = AsyncMock()
        svc.delete_alarm = AsyncMock(return_value=True)
        audit = AsyncMock()
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/a1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["deleted"] is True
        # audit should be called: pending + success
        assert audit.log.await_count >= 2

    def test_not_found_returns_404(self):
        svc = AsyncMock()
        svc.delete_alarm = AsyncMock(return_value=False)
        audit = AsyncMock()
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/a1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == AlarmErrors.NOT_FOUND
        # audit should have pending + failed
        assert audit.log.await_count >= 2

    def test_audit_pending_failure_returns_500(self):
        svc = AsyncMock()
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/a1")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.DELETE_FAILED

    def test_operator_forbidden(self):
        """ALARM_DELETE is admin-only; operator should get 403."""
        app = _build_app("operator")
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/a1")
        assert resp.status_code == 403

    def test_viewer_forbidden(self):
        app = _build_app("viewer")
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/a1")
        assert resp.status_code == 403

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.delete_alarm = AsyncMock(side_effect=RuntimeError("db fail"))
        audit = AsyncMock()
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/a1")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.DELETE_FAILED

    def test_audit_success_log_failure_swallowed(self):
        svc = AsyncMock()
        svc.delete_alarm = AsyncMock(return_value=True)
        audit = AsyncMock()
        # First call (pending) succeeds, second call (success) fails
        audit.log = AsyncMock(side_effect=[None, RuntimeError("audit down")])
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/a1")
        assert resp.status_code == 200

    def test_audit_failed_log_failure_swallowed(self):
        svc = AsyncMock()
        svc.delete_alarm = AsyncMock(return_value=False)
        audit = AsyncMock()
        # First call (pending) succeeds, second call (failed) fails
        audit.log = AsyncMock(side_effect=[None, RuntimeError("audit down")])
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/a1")
        assert resp.status_code == 404


# ════════════════════════════════════════════════════════════
#  POST /{alarm_id}/suppress
# ════════════════════════════════════════════════════════════


class TestSuppressAlarm:
    def test_admin_success(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm())
        svc._suppression_rules = []
        audit = AsyncMock()
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post(
                "/api/v1/alarms/a1/suppress",
                json={"duration_seconds": 3600, "reason": "testing"},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["suppressed"] is True
        assert data["duration_seconds"] == 3600
        assert len(svc._suppression_rules) == 1
        audit.log.assert_awaited_once()

    def test_not_found_returns_404(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=None)
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 404
        assert resp.json()["detail"] == AlarmErrors.NOT_FOUND

    def test_no_suppression_rules_attr_returns_503(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm())
        # _suppression_rules attribute is missing (getattr returns None)
        svc._suppression_rules = None
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 503
        assert resp.json()["detail"] == CommonErrors.SERVICE_NOT_READY

    def test_non_admin_with_access(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1"))
        svc._suppression_rules = []
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            return_value=None,
        ):
            resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 200

    def test_non_admin_without_access_returns_403(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1"))
        app = _build_app("operator", alarm_svc=svc)
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            side_effect=HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED),
        ):
            resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 403

    def test_viewer_forbidden(self):
        app = _build_app("viewer")
        client = TestClient(app)
        resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 403

    def test_duration_below_minimum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/alarms/a1/suppress",
            json={"duration_seconds": 30},
        )
        assert resp.status_code == 422

    def test_duration_above_maximum_returns_422(self):
        app = _build_app("admin")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/alarms/a1/suppress",
            json={"duration_seconds": 100000},
        )
        assert resp.status_code == 422

    def test_expired_rules_cleaned_up(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm())
        # Add an expired rule and a valid rule
        expired_rule = AlarmSuppressionRule(
            rule_id="old",
            name="old",
            expires_at=datetime.now(UTC) - timedelta(hours=1),
        )
        valid_rule = AlarmSuppressionRule(
            rule_id="valid",
            name="valid",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        svc._suppression_rules = [expired_rule, valid_rule]
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 200
        # expired rule should be cleaned, new rule + valid rule remain
        assert len(svc._suppression_rules) == 2
        rule_ids = [r.rule_id for r in svc._suppression_rules]
        assert "old" not in rule_ids
        assert "valid" in rule_ids
        assert "suppress_a1" in rule_ids

    def test_audit_log_failure_swallowed(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm())
        svc._suppression_rules = []
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        app = _build_app("admin", alarm_svc=svc, audit_svc=audit)
        client = TestClient(app)
        with patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"):
            resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 200

    def test_service_error_returns_500(self):
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(side_effect=RuntimeError("db fail"))
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.ACK_FAILED

    def test_default_duration_used(self):
        """When duration_seconds not provided, default 3600 should be used."""
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm())
        svc._suppression_rules = []
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        with patch("edgelite.api.auth._get_client_ip", return_value=""):
            resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["duration_seconds"] == 3600

    def test_suppression_rule_uses_alarm_attributes(self):
        """The suppression rule should use the alarm's device_id/rule_id/severity."""
        svc = AsyncMock()
        svc.get_alarm = AsyncMock(return_value=_make_alarm(device_id="d1", rule_id="r1", severity="critical"))
        svc._suppression_rules = []
        app = _build_app("admin", alarm_svc=svc)
        client = TestClient(app)
        with patch("edgelite.api.auth._get_client_ip", return_value=""):
            resp = client.post("/api/v1/alarms/a1/suppress", json={})
        assert resp.status_code == 200
        rule = svc._suppression_rules[0]
        assert rule.device_ids == ["d1"]
        assert rule.rule_ids == ["r1"]
        assert rule.severities == ["critical"]


# ════════════════════════════════════════════════════════════
#  POST /silence (create alarm silence)
# ════════════════════════════════════════════════════════════


class TestCreateAlarmSilence:
    def test_admin_success(self):
        mgr = MagicMock()
        mgr.create_silence = MagicMock(return_value={"id": "s1", "device_id": "d1"})
        audit = AsyncMock()
        app = _build_app("admin", audit_svc=audit)
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
            ),
            patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"),
        ):
            resp = client.post(
                "/api/v1/alarms/silence",
                json={"device_id": "d1", "reason": "maintenance"},
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["id"] == "s1"
        audit.log.assert_awaited_once()

    def test_admin_global_silence_allowed(self):
        """Admin can create global silence (empty device_id)."""
        mgr = MagicMock()
        mgr.create_silence = MagicMock(return_value={"id": "s1"})
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.post(
                "/api/v1/alarms/silence",
                json={"device_id": "", "reason": "global"},
            )
        assert resp.status_code == 200

    def test_non_admin_global_silence_returns_403(self):
        """Non-admin cannot create global silence (empty device_id)."""
        app = _build_app("operator")
        client = TestClient(app)
        resp = client.post(
            "/api/v1/alarms/silence",
            json={"device_id": ""},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == AuthzErrors.RESOURCE_OWNERSHIP_DENIED

    def test_non_admin_with_device_access(self):
        mgr = MagicMock()
        mgr.create_silence = MagicMock(return_value={"id": "s1"})
        app = _build_app("operator")
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
            ),
            patch(
                "edgelite.api.alarms._check_alarm_device_access",
                return_value=None,
            ),
        ):
            resp = client.post(
                "/api/v1/alarms/silence",
                json={"device_id": "d1"},
            )
        assert resp.status_code == 200

    def test_non_admin_without_device_access_returns_403(self):
        app = _build_app("operator")
        client = TestClient(app)
        with patch(
            "edgelite.api.alarms._check_alarm_device_access",
            side_effect=HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED),
        ):
            resp = client.post(
                "/api/v1/alarms/silence",
                json={"device_id": "d1"},
            )
        assert resp.status_code == 403

    def test_viewer_forbidden(self):
        app = _build_app("viewer")
        client = TestClient(app)
        resp = client.post("/api/v1/alarms/silence", json={})
        assert resp.status_code == 403

    def test_value_error_returns_422(self):
        mgr = MagicMock()
        mgr.create_silence = MagicMock(side_effect=ValueError("bad time"))
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.post("/api/v1/alarms/silence", json={"device_id": "d1"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == CommonErrors.VALIDATION_FAILED

    def test_service_error_returns_500(self):
        mgr = MagicMock()
        mgr.create_silence = MagicMock(side_effect=RuntimeError("fail"))
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.post("/api/v1/alarms/silence", json={"device_id": "d1"})
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.SILENCE_CREATE_FAILED

    def test_audit_log_failure_swallowed(self):
        mgr = MagicMock()
        mgr.create_silence = MagicMock(return_value={"id": "s1"})
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        app = _build_app("admin", audit_svc=audit)
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
            ),
            patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"),
        ):
            resp = client.post("/api/v1/alarms/silence", json={"device_id": "d1"})
        assert resp.status_code == 200

    def test_create_silence_passed_all_params(self):
        mgr = MagicMock()
        mgr.create_silence = MagicMock(return_value={"id": "s1"})
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.post(
                "/api/v1/alarms/silence",
                json={
                    "device_id": "d1",
                    "rule_id": "r1",
                    "start_time": "2026-01-01T00:00:00Z",
                    "end_time": "2026-01-02T00:00:00Z",
                    "reason": "test",
                },
            )
        assert resp.status_code == 200
        mgr.create_silence.assert_called_once_with(
            device_id="d1",
            rule_id="r1",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
            reason="test",
            operator="admin",
        )


# ════════════════════════════════════════════════════════════
#  DELETE /silence/{silence_id}
# ════════════════════════════════════════════════════════════


class TestDeleteAlarmSilence:
    def test_admin_success(self):
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value={"id": "s1", "device_id": "d1", "reason": "test"})
        mgr.delete_silence = MagicMock(return_value=True)
        audit = AsyncMock()
        app = _build_app("admin", audit_svc=audit)
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
            ),
            patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"),
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["deleted"] is True
        audit.log.assert_awaited_once()

    def test_not_found_returns_404(self):
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value=None)
        mgr.delete_silence = MagicMock(return_value=False)
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.delete("/api/v1/alarms/silence/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == AlarmErrors.SILENCE_NOT_FOUND

    def test_delete_returns_false_returns_404(self):
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value={"id": "s1", "device_id": "d1"})
        mgr.delete_silence = MagicMock(return_value=False)
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 404

    def test_non_admin_not_found_returns_404(self):
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value=None)
        app = _build_app("operator")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 404
        assert resp.json()["detail"] == AlarmErrors.SILENCE_NOT_FOUND

    def test_non_admin_global_silence_returns_403(self):
        """Non-admin cannot delete global silence (empty device_id)."""
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value={"id": "s1", "device_id": ""})
        app = _build_app("operator")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 403
        assert resp.json()["detail"] == AlarmErrors.SILENCE_GLOBAL_ADMIN_REQUIRED

    def test_non_admin_with_device_access(self):
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value={"id": "s1", "device_id": "d1"})
        mgr.delete_silence = MagicMock(return_value=True)
        app = _build_app("operator")
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
            ),
            patch(
                "edgelite.api.alarms._check_alarm_device_access",
                return_value=None,
            ),
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 200

    def test_non_admin_without_device_access_returns_403(self):
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value={"id": "s1", "device_id": "d1"})
        app = _build_app("operator")
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
            ),
            patch(
                "edgelite.api.alarms._check_alarm_device_access",
                side_effect=HTTPException(status_code=403, detail=AuthzErrors.RESOURCE_OWNERSHIP_DENIED),
            ),
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 403

    def test_viewer_forbidden(self):
        app = _build_app("viewer")
        client = TestClient(app)
        resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 403

    def test_service_error_returns_500(self):
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(side_effect=RuntimeError("fail"))
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 500
        assert resp.json()["detail"] == AlarmErrors.SILENCE_UPDATE_FAILED

    def test_audit_log_failure_swallowed(self):
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value={"id": "s1", "device_id": "d1"})
        mgr.delete_silence = MagicMock(return_value=True)
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit down"))
        app = _build_app("admin", audit_svc=audit)
        client = TestClient(app)
        with (
            patch.dict(
                sys.modules,
                {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
            ),
            patch("edgelite.api.auth._get_client_ip", return_value="10.0.0.1"),
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 200

    def test_admin_silence_not_found_in_get_silence_by_id(self):
        """Admin: get_silence_by_id returns None but delete_silence returns True.
        The _silence_info will be None but deletion succeeds."""
        mgr = MagicMock()
        mgr.get_silence_by_id = MagicMock(return_value=None)
        mgr.delete_silence = MagicMock(return_value=True)
        app = _build_app("admin")
        client = TestClient(app)
        with patch.dict(
            sys.modules,
            {"edgelite.services.alarm_silence": _mock_silence_module(mgr)},
        ):
            resp = client.delete("/api/v1/alarms/silence/s1")
        assert resp.status_code == 200


# ════════════════════════════════════════════════════════════
#  Pydantic models
# ════════════════════════════════════════════════════════════


class TestSuppressRequestModel:
    def test_defaults(self):
        req = SuppressRequest()
        assert req.duration_seconds == 3600
        assert req.reason == ""
        assert req.tag_match == {}

    def test_valid_values(self):
        req = SuppressRequest(
            duration_seconds=60,
            reason="maintenance",
            tag_match={"env": "prod"},
        )
        assert req.duration_seconds == 60
        assert req.reason == "maintenance"
        assert req.tag_match == {"env": "prod"}

    def test_duration_below_minimum_raises(self):
        with pytest.raises(ValidationError):
            SuppressRequest(duration_seconds=59)

    def test_duration_above_maximum_raises(self):
        with pytest.raises(ValidationError):
            SuppressRequest(duration_seconds=86401)

    def test_duration_at_minimum(self):
        req = SuppressRequest(duration_seconds=60)
        assert req.duration_seconds == 60

    def test_duration_at_maximum(self):
        req = SuppressRequest(duration_seconds=86400)
        assert req.duration_seconds == 86400


class TestSilenceCreateRequestModel:
    def test_defaults(self):
        req = SilenceCreateRequest()
        assert req.device_id == ""
        assert req.rule_id == ""
        assert req.start_time == ""
        assert req.end_time == ""
        assert req.reason == ""

    def test_valid_values(self):
        req = SilenceCreateRequest(
            device_id="d1",
            rule_id="r1",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
            reason="maintenance",
        )
        assert req.device_id == "d1"
        assert req.reason == "maintenance"
