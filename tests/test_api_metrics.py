"""Prometheus metrics endpoint unit tests - covers api/metrics.py"""

from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, "src")

from edgelite.api import metrics as metrics_module
from edgelite.api.metrics import (
    PrometheusExporter,
    _authenticate_metrics,
    _background_collect_loop,
    _collect_ai_metrics,
    _collect_alarm_metrics,
    _collect_all_metrics,
    _collect_cache_metrics,
    _collect_db_monitor_metrics,
    _collect_device_metrics,
    _collect_event_bus_metrics,
    _collect_points_metrics,
    _collect_prometheus_client_metrics,
    _collect_protocol_metrics,
    _collect_system_metrics,
    _escape_label,
    _format_metric,
    _root_metrics_router,
    get_exporter,
    router,
    start_background_collection,
    stop_background_collection,
)


class _AsyncCM:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


def _make_request(headers=None, cookies=None, client_host="1.2.3.4"):
    req = MagicMock()
    req.headers = headers or {}
    req.cookies = cookies or {}
    req.client = SimpleNamespace(host=client_host) if client_host else None
    return req


def _app_state_ns(**attrs):
    defaults = dict(
        database=None,
        scheduler=None,
        driver_registry=None,
        alarm_service=None,
        evaluator=None,
        ai_engine=None,
        cache_manager=None,
        influx_storage=None,
        mqtt_forwarder=None,
        event_bus=None,
        audit_service=None,
        start_time=None,
    )
    defaults.update(attrs)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _reset_module_globals():
    metrics_module._last_ai_stats.clear()
    metrics_module._last_protocol_stats.clear()
    metrics_module._last_collect_stats.clear()
    metrics_module._last_mqtt_forward_count = 0
    metrics_module._last_mqtt_error_count = 0
    metrics_module._exporter = None
    yield


class TestEscapeAndFormat:
    def test_escape_label(self):
        assert _escape_label("simple") == "simple"
        assert _escape_label('a"b') == 'a\\"b'
        assert _escape_label("a\\b") == "a\\\\b"
        assert _escape_label("a\nb") == "a\\nb"

    def test_format_metric_no_labels(self):
        assert _format_metric("m", {}, 1.0) == "m 1.0"

    def test_format_metric_with_labels(self):
        assert _format_metric("m", {"a": "1"}, 3.0) == 'm{a="1"} 3.0'

    def test_format_metric_with_timestamp(self):
        assert _format_metric("m", {}, 1.0, timestamp=1.5).endswith(" 1500")

    def test_format_metric_escapes_values(self):
        assert 'k="va\\"l"' in _format_metric("m", {"k": 'va"l'}, 1.0)


class TestPrometheusExporter:
    def test_gauge_sets_and_overwrites(self):
        exp = PrometheusExporter()
        exp.gauge("g1", 10.0, {"l": "v"}, "desc")
        assert exp._metrics["g1"]["type"] == "gauge"
        assert exp._metrics["g1"]["values"]["l=v"] == 10.0
        exp.gauge("g1", 20.0, {"l": "v"})
        assert exp._metrics["g1"]["values"]["l=v"] == 20.0

    def test_counter_accumulates(self):
        exp = PrometheusExporter()
        exp.counter("c1", 5.0)
        exp.counter("c1", 3.0, {"l": "v"})
        assert exp._metrics["c1"]["values"][""] == 5.0
        assert exp._metrics["c1"]["values"]["l=v"] == 3.0

    def test_counter_negative_clamped(self):
        exp = PrometheusExporter()
        exp.counter("c1", -5.0)
        assert exp._metrics["c1"]["values"][""] == 0.0

    def test_histogram_records_buckets(self):
        exp = PrometheusExporter()
        exp.histogram("h1", 0.05, {"d": "1"})
        data = exp._metrics["h1"]["values"]["d=1"]
        assert data["sum"] == 0.05 and data["count"] == 1
        assert data["buckets"][0.05] == 1 and data["buckets"][0.005] == 0

    def test_histogram_custom_buckets(self):
        exp = PrometheusExporter()
        exp.histogram("h1", 1.5, buckets=(1.0, 2.0))
        assert exp._metrics["h1"]["buckets"] == (1.0, 2.0)

    def test_summary_records_values(self):
        exp = PrometheusExporter()
        for v in (1.0, 2.0, 3.0, 4.0):
            exp.summary("s1", v)
        data = exp._metrics["s1"]["values"][""]
        assert data["sum"] == 10.0 and data["count"] == 4

    def test_labels_key_and_parse(self):
        assert PrometheusExporter._labels_key({"b": "2", "a": "1"}) == "a=1,b=2"
        assert PrometheusExporter._labels_key({}) == ""
        assert PrometheusExporter._parse_labels("a=1,b=2") == {"a": "1", "b": "2"}
        assert PrometheusExporter._parse_labels("") == {}
        assert PrometheusExporter._parse_labels("a=1,bad") == {"a": "1"}

    def test_reset_gauge(self):
        exp = PrometheusExporter()
        exp.gauge("g1", 1.0, {"l": "v"})
        exp.gauge("g1", 2.0, {"l": "w"})
        exp.reset_gauge("g1", {"l": "v"})
        assert "l=v" not in exp._metrics["g1"]["values"]
        assert "l=w" in exp._metrics["g1"]["values"]
        exp.reset_gauge_all("g1")
        assert exp._metrics["g1"]["values"] == {}
        exp.reset_gauge("nope", {"l": "v"})
        exp.reset_gauge_all("nope")

    def test_render_gauge_counter(self):
        exp = PrometheusExporter()
        exp.gauge("g1", 1.0, {"l": "v"}, "gauge desc")
        exp.counter("c1", 2.0, description="counter desc")
        out = exp.render()
        assert "# HELP g1 gauge desc" in out
        assert "# TYPE g1 gauge" in out and "# TYPE c1 counter" in out
        assert 'g1{l="v"}' in out and out.endswith("\n")

    def test_render_histogram(self):
        exp = PrometheusExporter()
        exp.histogram("h1", 0.05, {"d": "1"}, "hist desc")
        out = exp.render()
        assert "# TYPE h1 histogram" in out
        assert 'h1_bucket{d="1",le="0.05"}' in out
        assert 'h1_bucket{d="1",le="+Inf"}' in out
        assert "h1_sum" in out and "h1_count" in out

    def test_render_summary(self):
        exp = PrometheusExporter()
        for v in (1.0, 2.0, 3.0, 4.0):
            exp.summary("s1", v, description="sum desc")
        out = exp.render()
        assert "# TYPE s1 summary" in out
        assert 's1{quantile="0.5"}' in out and "s1_sum" in out

    def test_render_empty_and_no_desc(self):
        assert PrometheusExporter().render() == "\n"
        exp = PrometheusExporter()
        exp.gauge("g1", 1.0)
        assert "# HELP g1" not in exp.render()


class TestGetExporter:
    def test_singleton(self):
        assert get_exporter() is get_exporter()


class TestLogApiKeyUsage:
    async def test_no_audit_service_noop(self):
        with patch("edgelite.app._app_state", _app_state_ns(audit_service=None)):
            metrics_module._log_api_key_usage(_make_request(), "grafana.api_key", True)

    async def test_with_audit_service_creates_task(self):
        audit_svc = AsyncMock()
        audit_svc.log = AsyncMock(return_value=None)
        with patch("edgelite.app._app_state", _app_state_ns(audit_service=audit_svc)):
            metrics_module._log_api_key_usage(_make_request(), "grafana.api_key", True)
            await asyncio.sleep(0.01)
            assert audit_svc.log.called

    async def test_exception_handled(self):
        with patch("edgelite.app._app_state", _app_state_ns(audit_service=None)):
            metrics_module._log_api_key_usage(_make_request(), "k", True)


class TestAuthenticateMetrics:
    async def test_no_auth_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthErrors

        with pytest.raises(HTTPException) as exc:
            await _authenticate_metrics(_make_request())
        assert exc.value.status_code == 401
        assert exc.value.detail == AuthErrors.AUTH_REQUIRED

    async def test_jwt_valid_admin_success(self):
        payload = {"sub": "u1", "username": "admin", "role": "admin", "jti": "j1", "iat": 1000}
        mock_db = MagicMock()
        mock_db.get_session.return_value = _AsyncCM()
        mock_db.write_lock = MagicMock()
        repo = AsyncMock()
        repo.get_by_username = AsyncMock(return_value={"enabled": True, "role": "admin"})
        with (
            patch("edgelite.security.jwt.verify_token", return_value=payload),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.app._app_state", _app_state_ns(database=mock_db)),
            patch("edgelite.storage.sqlite_repo.UserRepo", return_value=repo),
        ):
            result = await _authenticate_metrics(_make_request(headers={"Authorization": "Bearer t"}))
        assert result["auth_type"] == "jwt" and result["role"] == "admin"

    async def test_jwt_db_none_skips_user_check(self):
        payload = {"sub": "u1", "username": "admin", "role": "admin", "jti": "j1"}
        with (
            patch("edgelite.security.jwt.verify_token", return_value=payload),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.app._app_state", _app_state_ns(database=None)),
        ):
            result = await _authenticate_metrics(_make_request(headers={"Authorization": "Bearer t"}))
        assert result["role"] == "admin"

    async def test_jwt_revoked_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthErrors

        payload = {"sub": "u1", "username": "admin", "role": "admin", "jti": "j1"}
        with (
            patch("edgelite.security.jwt.verify_token", return_value=payload),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=True),
            patch("edgelite.app._app_state", _app_state_ns(database=None)),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"Authorization": "Bearer t"}))
        assert exc.value.detail == AuthErrors.TOKEN_REVOKED

    async def test_jwt_user_disabled_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthErrors

        payload = {"sub": "u1", "username": "admin", "role": "admin", "jti": "j1"}
        mock_db = MagicMock()
        mock_db.get_session.return_value = _AsyncCM()
        mock_db.write_lock = MagicMock()
        repo = AsyncMock()
        repo.get_by_username = AsyncMock(return_value={"enabled": False, "role": "admin"})
        with (
            patch("edgelite.security.jwt.verify_token", return_value=payload),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.app._app_state", _app_state_ns(database=mock_db)),
            patch("edgelite.storage.sqlite_repo.UserRepo", return_value=repo),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"Authorization": "Bearer t"}))
        assert exc.value.detail == AuthErrors.USER_DISABLED

    async def test_jwt_user_not_found_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthErrors

        payload = {"sub": "u1", "username": "ghost", "role": "admin", "jti": "j1"}
        mock_db = MagicMock()
        mock_db.get_session.return_value = _AsyncCM()
        mock_db.write_lock = MagicMock()
        repo = AsyncMock()
        repo.get_by_username = AsyncMock(return_value=None)
        with (
            patch("edgelite.security.jwt.verify_token", return_value=payload),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.app._app_state", _app_state_ns(database=mock_db)),
            patch("edgelite.storage.sqlite_repo.UserRepo", return_value=repo),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"Authorization": "Bearer t"}))
        assert exc.value.detail == AuthErrors.USER_DISABLED

    async def test_jwt_password_changed_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthErrors

        payload = {"sub": "u1", "username": "admin", "role": "admin", "jti": "j1", "iat": 1000}
        mock_db = MagicMock()
        mock_db.get_session.return_value = _AsyncCM()
        mock_db.write_lock = MagicMock()
        repo = AsyncMock()
        repo.get_by_username = AsyncMock(
            return_value={"enabled": True, "role": "admin", "password_changed_at": "2026-01-01T00:00:00"}
        )
        with (
            patch("edgelite.security.jwt.verify_token", return_value=payload),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.app._app_state", _app_state_ns(database=mock_db)),
            patch("edgelite.storage.sqlite_repo.UserRepo", return_value=repo),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"Authorization": "Bearer t"}))
        assert exc.value.detail == AuthErrors.TOKEN_PASSWORD_CHANGED

    async def test_jwt_insufficient_permission_returns_403(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthzErrors

        payload = {"sub": "u1", "username": "guest", "role": "guest", "jti": "j1"}
        with (
            patch("edgelite.security.jwt.verify_token", return_value=payload),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.app._app_state", _app_state_ns(database=None)),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"Authorization": "Bearer t"}))
        assert exc.value.status_code == 403
        assert exc.value.detail == AuthzErrors.PERMISSION_DENIED

    async def test_jwt_verify_token_raises_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthErrors

        with patch("edgelite.security.jwt.verify_token", side_effect=Exception("bad")):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"Authorization": "Bearer bad"}))
        assert exc.value.detail == AuthErrors.TOKEN_INVALID

    async def test_cookie_fallback_success(self):
        payload = {"sub": "u1", "username": "admin", "role": "admin"}
        with (
            patch("edgelite.security.jwt.verify_token", return_value=payload),
            patch("edgelite.security.token_revocation.is_token_revoked", return_value=False),
            patch("edgelite.app._app_state", _app_state_ns(database=None)),
        ):
            result = await _authenticate_metrics(_make_request(cookies={"edgelite_access": "ct"}))
        assert result["auth_type"] == "jwt"

    async def test_api_key_grafana_success(self):
        config = SimpleNamespace(
            grafana=SimpleNamespace(api_key="g-key"),
            video=SimpleNamespace(pygbsentry=SimpleNamespace(api_key="v-key")),
            server=SimpleNamespace(webhook_api_key="s-key"),
        )
        with (
            patch("edgelite.config.get_config", return_value=config),
            patch("edgelite.app._app_state", _app_state_ns(audit_service=None)),
        ):
            result = await _authenticate_metrics(_make_request(headers={"X-API-Key": "g-key"}))
        assert result["auth_type"] == "api_key" and result["username"] == "grafana"

    async def test_api_key_video_success(self):
        config = SimpleNamespace(
            grafana=SimpleNamespace(api_key=""),
            video=SimpleNamespace(pygbsentry=SimpleNamespace(api_key="v-key")),
            server=SimpleNamespace(webhook_api_key="s-key"),
        )
        with (
            patch("edgelite.config.get_config", return_value=config),
            patch("edgelite.app._app_state", _app_state_ns(audit_service=None)),
        ):
            result = await _authenticate_metrics(_make_request(headers={"X-API-Key": "v-key"}))
        assert result["username"] == "video"

    async def test_api_key_webhook_returns_403(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthzErrors

        config = SimpleNamespace(
            grafana=SimpleNamespace(api_key=""),
            video=SimpleNamespace(pygbsentry=SimpleNamespace(api_key="")),
            server=SimpleNamespace(webhook_api_key="s-key"),
        )
        with (
            patch("edgelite.config.get_config", return_value=config),
            patch("edgelite.app._app_state", _app_state_ns(audit_service=None)),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"X-API-Key": "s-key"}))
        assert exc.value.status_code == 403 and exc.value.detail == AuthzErrors.PERMISSION_DENIED

    async def test_api_key_invalid_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import DeviceErrors

        config = SimpleNamespace(
            grafana=SimpleNamespace(api_key="g-key"),
            video=SimpleNamespace(pygbsentry=SimpleNamespace(api_key="v-key")),
            server=SimpleNamespace(webhook_api_key="s-key"),
        )
        with (
            patch("edgelite.config.get_config", return_value=config),
            patch("edgelite.app._app_state", _app_state_ns(audit_service=None)),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"X-API-Key": "wrong"}))
        assert exc.value.detail == DeviceErrors.API_KEY_INVALID

    async def test_api_key_config_none_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import CommonErrors

        with (
            patch("edgelite.config.get_config", return_value=None),
            patch("edgelite.app._app_state", _app_state_ns(audit_service=None)),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"X-API-Key": "k"}))
        assert exc.value.detail == CommonErrors.SERVICE_NOT_READY

    async def test_api_key_exception_returns_401(self):
        from fastapi import HTTPException

        from edgelite.api.error_codes import AuthErrors

        with (
            patch("edgelite.config.get_config", side_effect=RuntimeError("boom")),
            patch("edgelite.app._app_state", _app_state_ns(audit_service=None)),
        ):
            with pytest.raises(HTTPException) as exc:
                await _authenticate_metrics(_make_request(headers={"X-API-Key": "k"}))
        assert exc.value.detail == AuthErrors.AUTH_FAILED


class TestCollectSystemMetrics:
    def test_collects_all_system_metrics(self):
        exp = PrometheusExporter()
        mem = SimpleNamespace(total=100, available=40, used=60, percent=60.0)
        disk = SimpleNamespace(total=1000, free=500, used=500, percent=50.0)
        net = SimpleNamespace(bytes_recv=10, bytes_sent=20, packets_recv=1, packets_sent=2)
        with (
            patch("psutil.cpu_percent", side_effect=[55.0, [10.0, 20.0]]),
            patch("psutil.virtual_memory", return_value=mem),
            patch("psutil.disk_usage", return_value=disk),
            patch("psutil.net_io_counters", return_value=net),
            patch("edgelite.app._app_state", _app_state_ns(start_time=100.0)),
        ):
            _collect_system_metrics(exp)
        out = exp.render()
        assert "edgelite_system_cpu_usage_percent" in out
        assert "edgelite_system_memory_total_bytes" in out
        assert "edgelite_system_disk_usage_percent" in out
        assert "edgelite_system_network_bytes_total" in out
        assert "edgelite_system_uptime_seconds" in out

    def test_disk_exception_handled(self):
        exp = PrometheusExporter()
        mem = SimpleNamespace(total=100, available=40, used=60, percent=60.0)
        net = SimpleNamespace(bytes_recv=10, bytes_sent=20, packets_recv=1, packets_sent=2)
        with (
            patch("psutil.cpu_percent", side_effect=[55.0, [10.0]]),
            patch("psutil.virtual_memory", return_value=mem),
            patch("psutil.disk_usage", side_effect=OSError("no disk")),
            patch("psutil.net_io_counters", return_value=net),
            patch("edgelite.app._app_state", _app_state_ns(start_time=None)),
        ):
            _collect_system_metrics(exp)
        assert "edgelite_system_memory_total_bytes" in exp.render()


class TestCollectDeviceMetrics:
    async def test_no_scheduler_returns(self):
        exp = PrometheusExporter()
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=None)):
            await _collect_device_metrics(exp)
        assert exp._metrics == {}

    async def test_collects_device_stats(self):
        exp = PrometheusExporter()
        stat = SimpleNamespace(avg_latency_ms=10.0, max_latency_ms=20.0, total_calls=5, timeout_count=0)
        scheduler = AsyncMock()
        scheduler.get_collect_stats = AsyncMock(return_value={"d1": stat})
        scheduler.get_active_devices = AsyncMock(return_value=["d1"])
        driver = MagicMock()
        driver.get_all_health_stats.return_value = {"d1": object()}
        registry = MagicMock()
        registry.list_drivers.return_value = ["modbus"]
        registry.get_driver.return_value = driver
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler, driver_registry=registry)):
            await _collect_device_metrics(exp)
        out = exp.render()
        assert "edgelite_devices_total" in out and "edgelite_devices_online" in out
        assert "edgelite_collect_avg_latency_ms" in out

    async def test_no_protocol_counts_uses_unknown(self):
        exp = PrometheusExporter()
        stat = SimpleNamespace(avg_latency_ms=10.0, max_latency_ms=20.0, total_calls=5, timeout_count=1)
        scheduler = AsyncMock()
        scheduler.get_collect_stats = AsyncMock(return_value={"d1": stat})
        scheduler.get_active_devices = AsyncMock(return_value=["d1"])
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler, driver_registry=None)):
            await _collect_device_metrics(exp)
        out = exp.render()
        assert 'protocol="unknown"' in out and "edgelite_devices_offline" in out

    async def test_exception_handled(self):
        exp = PrometheusExporter()
        scheduler = AsyncMock()
        scheduler.get_collect_stats = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler)):
            await _collect_device_metrics(exp)


class TestCollectPointsMetrics:
    async def test_no_scheduler_returns(self):
        exp = PrometheusExporter()
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=None)):
            await _collect_points_metrics(exp)
        assert exp._metrics == {}

    async def test_collects_points(self):
        exp = PrometheusExporter()
        stat = SimpleNamespace(total_calls=10, timeout_count=2)
        scheduler = AsyncMock()
        scheduler.get_collect_stats = AsyncMock(return_value={"d1": stat})
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler)):
            await _collect_points_metrics(exp)
        assert exp._metrics["edgelite_points_collected_total"]["values"][""] == 10
        assert exp._metrics["edgelite_collection_errors_total"]["values"][""] == 2

    async def test_exception_handled(self):
        exp = PrometheusExporter()
        scheduler = AsyncMock()
        scheduler.get_collect_stats = AsyncMock(side_effect=RuntimeError("x"))
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler)):
            await _collect_points_metrics(exp)


class TestCollectAlarmMetrics:
    def test_no_alarm_service_returns(self):
        exp = PrometheusExporter()
        with patch("edgelite.app._app_state", _app_state_ns(alarm_service=None)):
            _collect_alarm_metrics(exp)
        assert exp._metrics == {}

    def test_collects_via_repo_count(self):
        exp = PrometheusExporter()
        alarm_repo = MagicMock()
        alarm_repo.count_active_by_severity.return_value = {"critical": 3, "warning": 5}
        with patch("edgelite.app._app_state", _app_state_ns(alarm_service=SimpleNamespace(_repo=alarm_repo))):
            _collect_alarm_metrics(exp)
        out = exp.render()
        assert "edgelite_alarms_active" in out and 'severity="all"' in out

    def test_falls_back_to_evaluator_counts(self):
        exp = PrometheusExporter()
        with patch(
            "edgelite.app._app_state",
            _app_state_ns(
                alarm_service=SimpleNamespace(_repo=SimpleNamespace()),
                evaluator=SimpleNamespace(_alarm_counts={"high": 2}),
            ),
        ):
            _collect_alarm_metrics(exp)
        assert "edgelite_alarms_active" in exp.render()

    def test_no_repo_returns(self):
        exp = PrometheusExporter()
        with patch("edgelite.app._app_state", _app_state_ns(alarm_service=SimpleNamespace())):
            _collect_alarm_metrics(exp)
        assert exp._metrics == {}


class TestCollectAiMetrics:
    def test_no_ai_engine_returns(self):
        exp = PrometheusExporter()
        with patch("edgelite.app._app_state", _app_state_ns(ai_engine=None)):
            _collect_ai_metrics(exp)
        assert exp._metrics == {}

    def test_collects_ai_stats_with_delta(self):
        ai_engine = MagicMock()
        ai_engine.get_loaded_models.return_value = {"m1": SimpleNamespace(status="active")}
        ai_engine.get_model_stats.return_value = {"call_count": 10, "error_count": 1, "avg_latency_ms": 5.0}
        with patch("edgelite.app._app_state", _app_state_ns(ai_engine=ai_engine)):
            exp = PrometheusExporter()
            _collect_ai_metrics(exp)
            assert "edgelite_ai_inferences_total" in exp.render()
            assert "edgelite_ai_active_models" in exp.render()
            exp2 = PrometheusExporter()
            _collect_ai_metrics(exp2)
        assert exp2._metrics["edgelite_ai_inferences_total"]["values"]["model=m1,status=active"] == 0

    def test_stale_model_cleaned(self):
        metrics_module._last_ai_stats["old_model"] = {"inference_count": 5, "error_count": 0}
        ai_engine = MagicMock()
        ai_engine.get_loaded_models.return_value = {"m1": SimpleNamespace(status="active")}
        ai_engine.get_model_stats.return_value = {"call_count": 1, "error_count": 0, "avg_latency_ms": 1.0}
        with patch("edgelite.app._app_state", _app_state_ns(ai_engine=ai_engine)):
            _collect_ai_metrics(PrometheusExporter())
        assert "old_model" not in metrics_module._last_ai_stats

    def test_exception_handled(self):
        ai_engine = MagicMock()
        ai_engine.get_loaded_models.side_effect = RuntimeError("x")
        with patch("edgelite.app._app_state", _app_state_ns(ai_engine=ai_engine)):
            _collect_ai_metrics(PrometheusExporter())


class TestCollectCacheMetrics:
    async def test_no_services_returns(self):
        exp = PrometheusExporter()
        with patch("edgelite.app._app_state", _app_state_ns(cache_manager=None, influx_storage=None)):
            await _collect_cache_metrics(exp)
        assert exp._metrics == {}

    async def test_collects_cache_and_influx(self):
        exp = PrometheusExporter()
        cache_manager = MagicMock()
        cache_manager.size.return_value = 42
        influx_storage = AsyncMock()
        influx_storage.available = AsyncMock(return_value=True)
        influx_storage.using_fallback = AsyncMock(return_value=False)
        influx_storage._sqlite_ts = SimpleNamespace(_buffer=[1, 2, 3])
        with patch(
            "edgelite.app._app_state", _app_state_ns(cache_manager=cache_manager, influx_storage=influx_storage)
        ):
            await _collect_cache_metrics(exp)
        out = exp.render()
        assert "edgelite_cache_size" in out and "edgelite_ring_buffer_usage" in out
        assert "edgelite_influxdb_available" in out and "edgelite_influxdb_using_fallback" in out

    async def test_cache_manager_with_cache_attr(self):
        exp = PrometheusExporter()
        with patch(
            "edgelite.app._app_state",
            _app_state_ns(
                cache_manager=SimpleNamespace(_cache={"a": 1, "b": 2}),
                influx_storage=None,
            ),
        ):
            await _collect_cache_metrics(exp)
        assert exp._metrics["edgelite_cache_size"]["values"][""] == 2

    async def test_exception_handled(self):
        exp = PrometheusExporter()
        cm = MagicMock()
        cm.size.side_effect = RuntimeError("x")
        with patch("edgelite.app._app_state", _app_state_ns(cache_manager=cm, influx_storage=None)):
            await _collect_cache_metrics(exp)


class TestCollectProtocolMetrics:
    async def test_no_scheduler_returns(self):
        exp = PrometheusExporter()
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=None)):
            await _collect_protocol_metrics(exp)
        assert exp._metrics == {}

    async def test_collects_protocol_stats(self):
        exp = PrometheusExporter()
        health = SimpleNamespace(total_reads=10, failed_reads=1, total_writes=5, failed_writes=0)
        driver = SimpleNamespace(plugin_name="modbus")
        driver.get_all_health_stats = MagicMock(return_value={"d1": health})
        scheduler = MagicMock()
        scheduler._device_info = {"d1": (driver,)}
        scheduler.get_collect_stats = AsyncMock(return_value={"d1": SimpleNamespace(avg_latency_ms=12.0)})
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler)):
            await _collect_protocol_metrics(exp)
        out = exp.render()
        assert "edgelite_protocol_requests_total" in out
        assert "edgelite_protocol_reads_total" in out
        assert "edgelite_protocol_latency_ms" in out

    async def test_delta_and_stale_cleanup(self):
        metrics_module._last_protocol_stats["old_proto"] = {"requests_total": 1, "errors_total": 0}
        health = SimpleNamespace(total_reads=10, failed_reads=1, total_writes=5, failed_writes=0)
        driver = SimpleNamespace(plugin_name="modbus")
        driver.get_all_health_stats = MagicMock(return_value={"d1": health})
        scheduler = MagicMock()
        scheduler._device_info = {"d1": (driver,)}
        scheduler.get_collect_stats = AsyncMock(return_value={})
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler)):
            exp1 = PrometheusExporter()
            await _collect_protocol_metrics(exp1)
            exp2 = PrometheusExporter()
            await _collect_protocol_metrics(exp2)
        assert exp2._metrics["edgelite_protocol_requests_total"]["values"]["protocol=modbus"] == 0
        assert "old_proto" not in metrics_module._last_protocol_stats

    async def test_non_tuple_info_skipped(self):
        exp = PrometheusExporter()
        scheduler = MagicMock()
        scheduler._device_info = {"d1": "not-a-tuple"}
        scheduler.get_collect_stats = AsyncMock(return_value={})
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler)):
            await _collect_protocol_metrics(exp)
        assert "edgelite_protocol_requests_total" not in exp._metrics

    async def test_exception_handled(self):
        exp = PrometheusExporter()
        scheduler = MagicMock()
        scheduler._device_info = None
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=scheduler)):
            await _collect_protocol_metrics(exp)


class TestCollectEventBusMetrics:
    def test_no_event_bus_returns(self):
        exp = PrometheusExporter()
        with patch("edgelite.app._app_state", _app_state_ns(event_bus=None)):
            _collect_event_bus_metrics(exp)
        assert exp._metrics == {}

    def test_collects_event_bus_stats(self):
        exp = PrometheusExporter()
        event_bus = SimpleNamespace(
            get_dropped_count=lambda: 5,
            get_handler_loop_count=lambda: 3,
            _subscribers={"a": 1, "b": 2},
        )
        with patch("edgelite.app._app_state", _app_state_ns(event_bus=event_bus)):
            _collect_event_bus_metrics(exp)
        out = exp.render()
        assert "edgelite_event_bus_dropped_total" in out
        assert "edgelite_event_bus_handler_loops" in out
        assert "edgelite_event_bus_subscribers" in out

    def test_exception_handled(self):
        with patch("edgelite.app._app_state", _app_state_ns(event_bus=SimpleNamespace())):
            _collect_event_bus_metrics(PrometheusExporter())


class TestCollectDbMonitorMetrics:
    def test_collects_db_monitor_stats(self):
        exp = PrometheusExporter()
        monitor = MagicMock()
        monitor.get_pool_stats.return_value = {
            "active_connections": 3,
            "idle_connections": 2,
            "waiting_count": 1,
        }
        monitor.get_slow_query_count.return_value = 5
        with patch("edgelite.services.db_monitor.get_db_monitor", return_value=monitor):
            _collect_db_monitor_metrics(exp)
        out = exp.render()
        assert "edgelite_db_pool_active_connections" in out
        assert "edgelite_db_slow_queries_total" in out

    def test_exception_handled(self):
        exp = PrometheusExporter()
        with patch("edgelite.services.db_monitor.get_db_monitor", side_effect=RuntimeError("x")):
            _collect_db_monitor_metrics(exp)
        assert exp._metrics == {}


class TestCollectAllMetrics:
    async def test_calls_all_collectors(self):
        exp = PrometheusExporter()
        with (
            patch("edgelite.api.metrics._collect_system_metrics") as sys_m,
            patch("edgelite.api.metrics._collect_device_metrics", new=AsyncMock()) as dev_m,
            patch("edgelite.api.metrics._collect_points_metrics", new=AsyncMock()) as pts_m,
            patch("edgelite.api.metrics._collect_alarm_metrics") as alm_m,
            patch("edgelite.api.metrics._collect_ai_metrics") as ai_m,
            patch("edgelite.api.metrics._collect_cache_metrics", new=AsyncMock()) as cache_m,
            patch("edgelite.api.metrics._collect_protocol_metrics", new=AsyncMock()) as proto_m,
            patch("edgelite.api.metrics._collect_db_monitor_metrics") as db_m,
            patch("edgelite.api.metrics._collect_event_bus_metrics") as eb_m,
        ):
            await _collect_all_metrics(exp)
        for m in (sys_m, dev_m, pts_m, alm_m, ai_m, cache_m, proto_m, db_m, eb_m):
            m.assert_called_once_with(exp)

    async def test_system_import_error_swallowed(self):
        exp = PrometheusExporter()
        with (
            patch("edgelite.api.metrics._collect_system_metrics", side_effect=ImportError("no psutil")),
            patch("edgelite.api.metrics._collect_device_metrics", new=AsyncMock()),
            patch("edgelite.api.metrics._collect_points_metrics", new=AsyncMock()),
            patch("edgelite.api.metrics._collect_alarm_metrics"),
            patch("edgelite.api.metrics._collect_ai_metrics"),
            patch("edgelite.api.metrics._collect_cache_metrics", new=AsyncMock()),
            patch("edgelite.api.metrics._collect_protocol_metrics", new=AsyncMock()),
            patch("edgelite.api.metrics._collect_db_monitor_metrics"),
            patch("edgelite.api.metrics._collect_event_bus_metrics"),
        ):
            await _collect_all_metrics(exp)


class TestCollectPrometheusClientMetrics:
    async def test_not_available_returns(self):
        with patch("edgelite.api.metrics._PROMETHEUS_CLIENT_AVAILABLE", False):
            await _collect_prometheus_client_metrics()

    async def test_collects_all_sections(self):
        stat = SimpleNamespace(total_calls=10, timeout_count=1, avg_latency_ms=50.0)
        scheduler = AsyncMock()
        scheduler.get_active_devices = AsyncMock(return_value=["d1"])
        scheduler.get_collect_stats = AsyncMock(return_value={"d1": stat})
        driver = MagicMock()
        driver.get_all_health_stats.return_value = {"d1": object()}
        registry = MagicMock()
        registry.list_drivers.return_value = ["modbus"]
        registry.get_driver.return_value = driver
        evaluator = SimpleNamespace(_rules={"r1": SimpleNamespace(enabled=True)})
        alarm_repo = MagicMock()
        alarm_repo.count_active_by_severity.return_value = {"critical": 2}
        influx_storage = AsyncMock()
        influx_storage.using_fallback = AsyncMock(return_value=True)
        with patch(
            "edgelite.app._app_state",
            _app_state_ns(
                scheduler=scheduler,
                driver_registry=registry,
                evaluator=evaluator,
                alarm_service=SimpleNamespace(_repo=alarm_repo),
                mqtt_forwarder=SimpleNamespace(_forward_count=5, _error_count=1),
                influx_storage=influx_storage,
            ),
        ):
            await _collect_prometheus_client_metrics()
        from prometheus_client import generate_latest as _gen

        out = _gen(metrics_module._registry).decode("utf-8")
        assert "edgelite_devices_online" in out
        assert "edgelite_rules_active" in out
        assert "edgelite_influxdb_fallback_mode" in out

    async def test_no_scheduler_skips_device_section(self):
        with patch("edgelite.app._app_state", _app_state_ns(scheduler=None)):
            await _collect_prometheus_client_metrics()


class TestBackgroundCollection:
    def test_stop_with_no_task_noop(self):
        metrics_module._background_task = None
        stop_background_collection()

    def test_stop_cancels_existing_task(self):
        task = MagicMock()
        task.done.return_value = False
        metrics_module._background_task = task
        with patch.object(task, "cancel") as cancel_mock:
            stop_background_collection()
        cancel_mock.assert_called_once()
        assert metrics_module._background_task is None

    def test_start_with_existing_running_task_returns(self):
        task = MagicMock()
        task.done.return_value = False
        metrics_module._background_task = task
        loop = MagicMock()
        with patch("asyncio.get_running_loop", return_value=loop):
            start_background_collection()
        loop.create_task.assert_not_called()

    def test_start_without_running_loop_passes(self):
        metrics_module._background_task = None
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
            start_background_collection()
        assert metrics_module._background_task is None

    async def test_start_creates_task_with_running_loop(self):
        metrics_module._background_task = None
        start_background_collection()
        await asyncio.sleep(0.01)
        assert metrics_module._background_task is not None
        stop_background_collection()

    async def test_background_loop_cancels(self):
        with patch("edgelite.api.metrics.asyncio.sleep", side_effect=asyncio.CancelledError()):
            await _background_collect_loop()

    async def test_background_loop_collects_then_cancels(self):
        n = {"i": 0}

        async def fake_sleep(_):
            n["i"] += 1
            if n["i"] == 1:
                return
            raise asyncio.CancelledError()

        with (
            patch("edgelite.api.metrics.asyncio.sleep", side_effect=fake_sleep),
            patch("edgelite.api.metrics.get_exporter", return_value=PrometheusExporter()) as ge,
            patch("edgelite.api.metrics._collect_all_metrics", new=AsyncMock()) as coll,
        ):
            await _background_collect_loop()
        coll.assert_called_once()
        ge.assert_called()

    async def test_background_loop_exception_continues(self):
        n = {"i": 0}

        async def fake_sleep(_):
            n["i"] += 1
            if n["i"] == 1:
                return
            raise asyncio.CancelledError()

        with (
            patch("edgelite.api.metrics.asyncio.sleep", side_effect=fake_sleep),
            patch("edgelite.api.metrics.get_exporter", return_value=PrometheusExporter()),
            patch("edgelite.api.metrics._collect_all_metrics", new=AsyncMock(side_effect=RuntimeError("boom"))),
        ):
            await _background_collect_loop()


@pytest.fixture
def metrics_app():
    app = FastAPI()
    app.include_router(router)
    app.include_router(_root_metrics_router)
    app.dependency_overrides[_authenticate_metrics] = lambda: {
        "auth_type": "jwt",
        "username": "admin",
        "role": "admin",
    }
    return app


@contextmanager
def _patch_endpoint_collectors():
    with ExitStack() as stack:
        stack.enter_context(patch("edgelite.api.metrics._collect_prometheus_client_metrics", new=AsyncMock()))
        stack.enter_context(patch("edgelite.api.metrics._collect_system_metrics"))
        stack.enter_context(patch("edgelite.api.metrics._collect_ai_metrics"))
        stack.enter_context(patch("edgelite.api.metrics._collect_cache_metrics", new=AsyncMock()))
        stack.enter_context(patch("edgelite.api.metrics._collect_protocol_metrics", new=AsyncMock()))
        stack.enter_context(patch("edgelite.api.metrics._collect_event_bus_metrics"))
        stack.enter_context(patch("edgelite.api.metrics._collect_db_monitor_metrics"))
        stack.enter_context(patch("edgelite.api.metrics.start_background_collection"))
        yield


class TestMetricsEndpoint:
    def test_metrics_returns_200_prometheus_format(self, metrics_app):
        with (
            patch("edgelite.api.metrics._PROMETHEUS_CLIENT_AVAILABLE", True),
            patch("edgelite.api.metrics.generate_latest", return_value=b"# prom content\n"),
            _patch_endpoint_collectors(),
        ):
            client = TestClient(metrics_app)
            resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200
        assert "prom content" in resp.text
        assert "text/plain" in resp.headers["content-type"]

    def test_metrics_unavailable_uses_fallback_exporter(self, metrics_app):
        with (
            patch("edgelite.api.metrics._PROMETHEUS_CLIENT_AVAILABLE", False),
            patch("edgelite.api.metrics._collect_all_metrics", new=AsyncMock()),
            _patch_endpoint_collectors(),
        ):
            client = TestClient(metrics_app)
            resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200

    def test_metrics_json_returns_200(self, metrics_app):
        exp = get_exporter()
        exp.gauge("test_gauge", 1.0, {"l": "v"}, "desc")
        with (
            patch("edgelite.api.metrics._PROMETHEUS_CLIENT_AVAILABLE", True),
            patch("edgelite.api.metrics.generate_latest", return_value=b""),
            _patch_endpoint_collectors(),
        ):
            client = TestClient(metrics_app)
            resp = client.get("/api/v1/metrics.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "metrics" in data and "timestamp" in data
        assert "test_gauge" in [m["name"] for m in data["metrics"]]

    def test_metrics_json_empty(self, metrics_app):
        with (
            patch("edgelite.api.metrics._PROMETHEUS_CLIENT_AVAILABLE", True),
            patch("edgelite.api.metrics.generate_latest", return_value=b""),
            _patch_endpoint_collectors(),
        ):
            client = TestClient(metrics_app)
            resp = client.get("/api/v1/metrics.json")
        assert resp.status_code == 200
        assert resp.json()["metrics"] == []

    def test_root_metrics_returns_200(self, metrics_app):
        with (
            patch("edgelite.api.metrics._PROMETHEUS_CLIENT_AVAILABLE", True),
            patch("edgelite.api.metrics.generate_latest", return_value=b"# root content\n"),
            patch(
                "edgelite.api.metrics._authenticate_metrics",
                new=AsyncMock(return_value={"auth_type": "jwt", "username": "admin", "role": "admin"}),
            ),
            _patch_endpoint_collectors(),
        ):
            client = TestClient(metrics_app)
            resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "root content" in resp.text

    def test_root_metrics_unavailable_uses_fallback(self, metrics_app):
        with (
            patch("edgelite.api.metrics._PROMETHEUS_CLIENT_AVAILABLE", False),
            patch("edgelite.api.metrics._collect_all_metrics", new=AsyncMock()),
            patch(
                "edgelite.api.metrics._authenticate_metrics",
                new=AsyncMock(return_value={"auth_type": "jwt", "username": "admin", "role": "admin"}),
            ),
            _patch_endpoint_collectors(),
        ):
            client = TestClient(metrics_app)
            resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_requires_auth_when_no_override(self):
        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 401
