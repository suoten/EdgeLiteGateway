"""Platform API endpoint unit tests covering api/platforms.py.

Covers:
- Platform CRUD: list / config-schema / status / dashboard / metrics
- Connection mgmt: connect / disconnect / test-connection / reload (with audit)
- Data queries: message-preview / broker-quality / tb series / shadow / logs / mapping
- Config import/export
- Templates & scripts: validate-topic / validate-advanced-template / preview-template /
  validate-script / test-script / mqtt-test-publish
- Error paths: 400/404/403/422/500/504, audit failure, path validation
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

# 北向适配器缺失模块的桩实现在 conftest.py 中统一设置，
# 此处无需重复注入——conftest 在所有测试文件之前执行。
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from edgelite.api.platforms import router  # noqa: E402

# -- helper: build a mock PlatformService instance ----------------------


def _make_mock_service() -> MagicMock:
    """Build a mock PlatformService with all sync/async method defaults."""
    svc = MagicMock()
    # async methods
    svc.connect = AsyncMock(return_value={"status": "connected", "platform": "mqtt"})
    svc.disconnect = AsyncMock(return_value={"status": "disconnected"})
    svc.test_connection = AsyncMock(return_value={"success": True, "latency_ms": 10})
    svc.get_dashboard_data = AsyncMock(return_value={"summary": {"total": 1}})
    svc.reload_config = AsyncMock(return_value={"status": "reloaded"})
    svc.test_script = AsyncMock(return_value={"result": "ok", "output": "x"})
    svc.mqtt_test_publish = AsyncMock(return_value={"published": True, "mid": 42})
    # sync methods
    svc.list_platforms = MagicMock(return_value=[{"name": "mqtt", "connected": True}])
    svc.list_supported = MagicMock(return_value=[{"name": "mqtt", "label": "MQTT", "description": "MQTT broker"}])
    svc.get_config_schema = MagicMock(return_value={"fields": [{"name": "broker"}]})
    svc.get_status = MagicMock(return_value={"connected": True, "state": "online"})
    svc.get_north_metrics = MagicMock(return_value="# HELP north_msgs total\n# TYPE north_msgs counter\n")
    svc.get_message_preview = MagicMock(return_value=[{"topic": "t", "payload": "p"}])
    svc.get_broker_quality = MagicMock(return_value={"quality": "good", "score": 99})
    svc.validate_topic_template = MagicMock(return_value={"valid": True, "topic": "a/b"})
    svc.get_tb_devices = MagicMock(return_value=[{"id": "d1", "name": "dev1"}])
    svc.get_tb_rpc_logs = MagicMock(return_value=[{"rpc": "x", "ts": 1}])
    svc.get_tb_alarm_records = MagicMock(return_value=[{"alarm": "a", "severity": "high"}])
    svc.get_tb_sync_status = MagicMock(return_value={"synced": True, "last": 1})
    svc.get_platform_shadow = MagicMock(return_value={"shadow": {"state": "on"}})
    svc.get_platform_command_logs = MagicMock(return_value=[{"cmd": "set", "ts": 1}])
    svc.get_platform_alarm_records = MagicMock(return_value=[{"alarm": "x", "ts": 1}])
    svc.get_platform_device_mapping = MagicMock(return_value=[{"device": "d", "topic": "t"}])
    svc.export_config = MagicMock(return_value={"platform": "mqtt", "config": {"broker": "h"}})
    svc.import_config = MagicMock(return_value={"imported": True, "count": 1})
    svc.get_broker_status = MagicMock(return_value=[{"broker": "h", "online": True}])
    svc.validate_advanced_template = MagicMock(return_value={"valid": True, "errors": []})
    svc.preview_template = MagicMock(return_value={"rendered": "result", "valid": True})
    svc.validate_script = MagicMock(return_value={"valid": True, "errors": []})
    return svc


# -- Fixtures ------------------------------------------------------------


@pytest.fixture
def mock_svc():
    """Patch module-level PlatformService so all instantiations return one mock."""
    with patch("edgelite.api.platforms.PlatformService") as mock_cls:
        svc = _make_mock_service()
        mock_cls.return_value = svc
        yield svc


@pytest.fixture
def audit_svc():
    """Accessible mock audit service (for asserting log calls)."""
    from conftest import make_mock_audit_service

    return make_mock_audit_service()


@pytest.fixture
def make_app_with(mock_svc, audit_svc):
    """Factory building a test app with the platforms router; role selectable."""
    from conftest import make_app

    def _make(role: str = "admin"):
        services = {
            "platform_handlers": {},
            "audit_service": audit_svc,
        }
        return make_app(router, role=role, services=services)

    return _make


@pytest.fixture
def client(make_app_with):
    """Default admin-role sync TestClient."""
    return TestClient(make_app_with("admin"))


# -- GET /list -----------------------------------------------------------


class TestListPlatforms:
    def test_list_ok(self, client, mock_svc):
        resp = client.get("/api/v1/platforms/list")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["platforms"] == [{"name": "mqtt", "connected": True}]
        assert body["data"]["supported"][0]["name"] == "mqtt"
        mock_svc.list_platforms.assert_called_once()
        mock_svc.list_supported.assert_called_once()

    def test_list_internal_error_returns_500(self, client, mock_svc):
        mock_svc.list_platforms.side_effect = RuntimeError("boom")
        resp = client.get("/api/v1/platforms/list")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_CONNECT_FAILED"


# -- GET /config-schema/{platform_name} ----------------------------------


class TestConfigSchema:
    def test_schema_ok(self, client, mock_svc):
        resp = client.get("/api/v1/platforms/config-schema/mqtt")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["platform_name"] == "mqtt"
        assert body["data"]["config_schema"] == {"fields": [{"name": "broker"}]}

    def test_schema_not_found_404(self, client, mock_svc):
        mock_svc.get_config_schema.return_value = None
        resp = client.get("/api/v1/platforms/config-schema/mqtt")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "ERR_PLATFORM_CONFIG_SCHEMA_NOT_FOUND"

    def test_schema_internal_error_500(self, client, mock_svc):
        mock_svc.get_config_schema.side_effect = RuntimeError("boom")
        resp = client.get("/api/v1/platforms/config-schema/mqtt")
        assert resp.status_code == 500

    def test_schema_invalid_name_422(self, client):
        resp = client.get("/api/v1/platforms/config-schema/bad%20name")
        assert resp.status_code == 422


# -- POST /connect/{platform_name} ---------------------------------------


class TestConnectPlatform:
    def test_connect_ok_logs_audit(self, client, mock_svc, audit_svc):
        resp = client.post(
            "/api/v1/platforms/connect/mqtt",
            json={"config": {"broker": "127.0.0.1", "password": "secret"}},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "connected"
        mock_svc.connect.assert_awaited_once_with("mqtt", {"broker": "127.0.0.1", "password": "secret"})
        audit_svc.log.assert_awaited_once()
        call_kwargs = audit_svc.log.await_args.kwargs
        after = call_kwargs["after_value"]
        assert after["config"]["password"] == "***"
        assert after["config"]["broker"] == "127.0.0.1"

    def test_connect_empty_config_400(self, client, mock_svc):
        resp = client.post("/api/v1/platforms/connect/mqtt", json={"config": {}})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "ERR_PLATFORM_MISSING_CONFIG"
        mock_svc.connect.assert_not_awaited()

    def test_connect_value_error_400(self, client, mock_svc, audit_svc):
        mock_svc.connect.side_effect = ValueError("bad config")
        resp = client.post(
            "/api/v1/platforms/connect/mqtt",
            json={"config": {"broker": "x"}},
        )
        assert resp.status_code == 400
        assert audit_svc.log.await_count == 1
        assert audit_svc.log.await_args.kwargs.get("status") == "failed"

    def test_connect_internal_error_500(self, client, mock_svc, audit_svc):
        mock_svc.connect.side_effect = RuntimeError("boom")
        resp = client.post(
            "/api/v1/platforms/connect/mqtt",
            json={"config": {"broker": "x"}},
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_CONNECT_FAILED"
        assert audit_svc.log.await_count == 1

    def test_connect_audit_failure_still_succeeds(self, client, mock_svc, audit_svc):
        audit_svc.log.side_effect = RuntimeError("audit down")
        resp = client.post(
            "/api/v1/platforms/connect/mqtt",
            json={"config": {"broker": "x"}},
        )
        assert resp.status_code == 200

    def test_connect_forbidden_for_viewer(self, make_app_with):
        c = TestClient(make_app_with("viewer"))
        resp = c.post(
            "/api/v1/platforms/connect/mqtt",
            json={"config": {"broker": "x"}},
        )
        assert resp.status_code == 403


# -- POST /disconnect/{platform_name} ------------------------------------


class TestDisconnectPlatform:
    def test_disconnect_ok_logs_audit(self, client, mock_svc, audit_svc):
        resp = client.post("/api/v1/platforms/disconnect/mqtt")
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "disconnected"
        mock_svc.disconnect.assert_awaited_once_with("mqtt")
        audit_svc.log.assert_awaited_once()

    def test_disconnect_key_error_404(self, client, mock_svc, audit_svc):
        mock_svc.disconnect.side_effect = KeyError("not found")
        resp = client.post("/api/v1/platforms/disconnect/mqtt")
        assert resp.status_code == 404
        assert audit_svc.log.await_args.kwargs.get("status") == "failed"

    def test_disconnect_internal_error_500(self, client, mock_svc, audit_svc):
        mock_svc.disconnect.side_effect = RuntimeError("boom")
        resp = client.post("/api/v1/platforms/disconnect/mqtt")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_DISCONNECT_FAILED"

    def test_disconnect_forbidden_for_viewer(self, make_app_with):
        c = TestClient(make_app_with("viewer"))
        assert c.post("/api/v1/platforms/disconnect/mqtt").status_code == 403


# -- POST /test-connection/{platform_name} -------------------------------


class TestTestConnection:
    def test_test_connection_ok(self, client, mock_svc, audit_svc):
        resp = client.post(
            "/api/v1/platforms/test-connection/mqtt",
            json={"config": {"broker": "127.0.0.1"}},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["success"] is True
        audit_svc.log.assert_awaited_once()

    def test_test_connection_value_error_400(self, client, mock_svc):
        mock_svc.test_connection.side_effect = ValueError("bad")
        resp = client.post(
            "/api/v1/platforms/test-connection/mqtt",
            json={"config": {"broker": "x"}},
        )
        assert resp.status_code == 400

    def test_test_connection_timeout_504(self, client, mock_svc, audit_svc):
        mock_svc.test_connection.side_effect = TimeoutError("slow")
        resp = client.post(
            "/api/v1/platforms/test-connection/mqtt",
            json={"config": {"broker": "x"}},
        )
        assert resp.status_code == 504
        assert audit_svc.log.await_args.kwargs.get("error_message") == "timeout"

    def test_test_connection_internal_error_500(self, client, mock_svc):
        mock_svc.test_connection.side_effect = RuntimeError("boom")
        resp = client.post(
            "/api/v1/platforms/test-connection/mqtt",
            json={"config": {"broker": "x"}},
        )
        assert resp.status_code == 500

    def test_test_connection_forbidden_for_viewer(self, make_app_with):
        c = TestClient(make_app_with("viewer"))
        resp = c.post(
            "/api/v1/platforms/test-connection/mqtt",
            json={"config": {"broker": "x"}},
        )
        assert resp.status_code == 403


# -- GET /status / /dashboard / /metrics ---------------------------------


class TestStatusDashboardMetrics:
    def test_status_ok(self, client, mock_svc):
        resp = client.get("/api/v1/platforms/status/mqtt")
        assert resp.status_code == 200
        assert resp.json()["data"]["connected"] is True
        mock_svc.get_status.assert_called_once_with("mqtt")

    def test_status_error_500(self, client, mock_svc):
        mock_svc.get_status.side_effect = RuntimeError("boom")
        resp = client.get("/api/v1/platforms/status/mqtt")
        assert resp.status_code == 500

    def test_dashboard_ok(self, client, mock_svc):
        resp = client.get("/api/v1/platforms/dashboard")
        assert resp.status_code == 200
        assert resp.json()["data"]["summary"]["total"] == 1
        mock_svc.get_dashboard_data.assert_awaited_once()

    def test_dashboard_error_500(self, client, mock_svc):
        mock_svc.get_dashboard_data.side_effect = RuntimeError("boom")
        resp = client.get("/api/v1/platforms/dashboard")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_DASHBOARD_FAILED"

    def test_metrics_ok_plain_text(self, client, mock_svc):
        resp = client.get("/api/v1/platforms/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "north_msgs" in resp.text
        mock_svc.get_north_metrics.assert_called_once()

    def test_metrics_error_returns_500_text(self, client, mock_svc):
        mock_svc.get_north_metrics.side_effect = RuntimeError("boom")
        resp = client.get("/api/v1/platforms/metrics")
        assert resp.status_code == 500
        assert "internal server error" in resp.text
        assert "boom" not in resp.text


# -- POST /reload/{platform_name} ----------------------------------------


class TestReloadConfig:
    def test_reload_ok_logs_audit(self, client, mock_svc, audit_svc):
        resp = client.post(
            "/api/v1/platforms/reload/mqtt",
            json={"config": {"broker": "h", "api_key": "k"}},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "reloaded"
        audit_svc.log.assert_awaited_once()
        after = audit_svc.log.await_args.kwargs["after_value"]
        assert after["config"]["api_key"] == "***"

    def test_reload_key_error_404(self, client, mock_svc):
        mock_svc.reload_config.side_effect = KeyError("nope")
        resp = client.post(
            "/api/v1/platforms/reload/mqtt",
            json={"config": {"broker": "h"}},
        )
        assert resp.status_code == 404

    def test_reload_value_error_400(self, client, mock_svc):
        mock_svc.reload_config.side_effect = ValueError("bad")
        resp = client.post(
            "/api/v1/platforms/reload/mqtt",
            json={"config": {"broker": "h"}},
        )
        assert resp.status_code == 400

    def test_reload_internal_error_500(self, client, mock_svc, audit_svc):
        mock_svc.reload_config.side_effect = RuntimeError("boom")
        resp = client.post(
            "/api/v1/platforms/reload/mqtt",
            json={"config": {"broker": "h"}},
        )
        assert resp.status_code == 500
        assert audit_svc.log.await_args.kwargs.get("status") == "failed"

    def test_reload_forbidden_for_viewer(self, make_app_with):
        c = TestClient(make_app_with("viewer"))
        resp = c.post(
            "/api/v1/platforms/reload/mqtt",
            json={"config": {"broker": "h"}},
        )
        assert resp.status_code == 403


# -- Simple data query endpoints (parametrized) -------------------------

_DATA_QUERY_ENDPOINTS = [
    ("/message-preview/{}", "get_message_preview"),
    ("/broker-quality/{}", "get_broker_quality"),
    ("/tb/devices/{}", "get_tb_devices"),
    ("/tb/rpc-logs/{}", "get_tb_rpc_logs"),
    ("/tb/alarms/{}", "get_tb_alarm_records"),
    ("/tb/sync-status/{}", "get_tb_sync_status"),
    ("/shadow/{}", "get_platform_shadow"),
    ("/command-logs/{}", "get_platform_command_logs"),
    ("/alarm-records/{}", "get_platform_alarm_records"),
    ("/device-mapping/{}", "get_platform_device_mapping"),
    ("/broker-status/{}", "get_broker_status"),
]


@pytest.mark.parametrize("suffix, method", _DATA_QUERY_ENDPOINTS)
class TestDataQueryEndpoints:
    def test_data_query_ok(self, client, mock_svc, suffix, method):
        url = "/api/v1/platforms" + suffix.format("mqtt")
        resp = client.get(url)
        assert resp.status_code == 200, resp.text
        getattr(mock_svc, method).assert_called_once_with("mqtt")

    def test_data_query_error_500(self, client, mock_svc, suffix, method):
        getattr(mock_svc, method).side_effect = RuntimeError("boom")
        url = "/api/v1/platforms" + suffix.format("mqtt")
        resp = client.get(url)
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_DATA_QUERY_FAILED"


# -- POST /validate-topic ------------------------------------------------


class TestValidateTopic:
    def test_validate_topic_ok(self, client, mock_svc):
        resp = client.post("/api/v1/platforms/validate-topic", json={"template": "a/{{b}}"})
        assert resp.status_code == 200
        assert resp.json()["data"]["valid"] is True
        mock_svc.validate_topic_template.assert_called_once_with("a/{{b}}")

    def test_validate_topic_error_500(self, client, mock_svc):
        mock_svc.validate_topic_template.side_effect = RuntimeError("boom")
        resp = client.post("/api/v1/platforms/validate-topic", json={"template": "a"})
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_TEMPLATE_FAILED"


# -- GET /export / POST /import ------------------------------------------


class TestExportImport:
    def test_export_ok(self, client, mock_svc):
        resp = client.get("/api/v1/platforms/export/mqtt")
        assert resp.status_code == 200
        assert resp.json()["data"]["platform"] == "mqtt"
        mock_svc.export_config.assert_called_once_with("mqtt")

    def test_export_error_500(self, client, mock_svc):
        mock_svc.export_config.side_effect = RuntimeError("boom")
        resp = client.get("/api/v1/platforms/export/mqtt")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_EXPORT_FAILED"

    def test_export_forbidden_for_viewer(self, make_app_with):
        c = TestClient(make_app_with("viewer"))
        assert c.get("/api/v1/platforms/export/mqtt").status_code == 403

    def test_import_ok(self, client, mock_svc):
        resp = client.post(
            "/api/v1/platforms/import/mqtt",
            json={"config_data": {"broker": "h"}},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["imported"] is True
        mock_svc.import_config.assert_called_once_with("mqtt", {"broker": "h"})

    def test_import_value_error_400(self, client, mock_svc):
        mock_svc.import_config.side_effect = ValueError("bad")
        resp = client.post(
            "/api/v1/platforms/import/mqtt",
            json={"config_data": {"broker": "h"}},
        )
        assert resp.status_code == 400

    def test_import_internal_error_500(self, client, mock_svc):
        mock_svc.import_config.side_effect = RuntimeError("boom")
        resp = client.post(
            "/api/v1/platforms/import/mqtt",
            json={"config_data": {"broker": "h"}},
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_IMPORT_FAILED"

    def test_import_forbidden_for_viewer(self, make_app_with):
        c = TestClient(make_app_with("viewer"))
        resp = c.post(
            "/api/v1/platforms/import/mqtt",
            json={"config_data": {"broker": "h"}},
        )
        assert resp.status_code == 403


# -- POST /validate-advanced-template / /preview-template ----------------


class TestAdvancedTemplates:
    def test_validate_advanced_template_ok(self, client, mock_svc):
        resp = client.post(
            "/api/v1/platforms/validate-advanced-template",
            json={"template": "{{x}}", "template_type": "payload"},
        )
        assert resp.status_code == 200
        mock_svc.validate_advanced_template.assert_called_once_with("{{x}}", "payload")

    def test_validate_advanced_template_default_type(self, client, mock_svc):
        resp = client.post(
            "/api/v1/platforms/validate-advanced-template",
            json={"template": "{{x}}"},
        )
        assert resp.status_code == 200

    def test_validate_advanced_template_value_error_422(self, client, mock_svc):
        mock_svc.validate_advanced_template.side_effect = ValueError("bad")
        resp = client.post(
            "/api/v1/platforms/validate-advanced-template",
            json={"template": "{{x}}"},
        )
        assert resp.status_code == 422

    def test_validate_advanced_template_error_500(self, client, mock_svc):
        mock_svc.validate_advanced_template.side_effect = RuntimeError("boom")
        resp = client.post(
            "/api/v1/platforms/validate-advanced-template",
            json={"template": "{{x}}"},
        )
        assert resp.status_code == 500

    def test_validate_advanced_template_invalid_type_422(self, client):
        resp = client.post(
            "/api/v1/platforms/validate-advanced-template",
            json={"template": "{{x}}", "template_type": "evil"},
        )
        assert resp.status_code == 422

    def test_preview_template_ok(self, client, mock_svc):
        resp = client.post(
            "/api/v1/platforms/preview-template",
            json={"template": "{{x}}", "test_data": {"x": "v"}, "template_type": "header"},
        )
        assert resp.status_code == 200
        mock_svc.preview_template.assert_called_once_with("{{x}}", {"x": "v"}, "header")

    def test_preview_template_value_error_422(self, client, mock_svc):
        mock_svc.preview_template.side_effect = ValueError("bad")
        resp = client.post(
            "/api/v1/platforms/preview-template",
            json={"template": "{{x}}", "test_data": {"x": "v"}},
        )
        assert resp.status_code == 422

    def test_preview_template_error_500(self, client, mock_svc):
        mock_svc.preview_template.side_effect = RuntimeError("boom")
        resp = client.post(
            "/api/v1/platforms/preview-template",
            json={"template": "{{x}}", "test_data": {"x": "v"}},
        )
        assert resp.status_code == 500


# -- POST /validate-script / /test-script --------------------------------


class TestScripts:
    def test_validate_script_ok(self, client, mock_svc):
        resp = client.post("/api/v1/platforms/validate-script", json={"script": "return 1"})
        assert resp.status_code == 200
        mock_svc.validate_script.assert_called_once_with("return 1")

    def test_validate_script_value_error_422(self, client, mock_svc):
        mock_svc.validate_script.side_effect = ValueError("bad")
        resp = client.post("/api/v1/platforms/validate-script", json={"script": "x"})
        assert resp.status_code == 422

    def test_validate_script_error_500(self, client, mock_svc):
        mock_svc.validate_script.side_effect = RuntimeError("boom")
        resp = client.post("/api/v1/platforms/validate-script", json={"script": "x"})
        assert resp.status_code == 500

    def test_test_script_ok(self, client, mock_svc):
        resp = client.post(
            "/api/v1/platforms/test-script",
            json={"script": "return 1", "test_payload": {"a": 1}},
        )
        assert resp.status_code == 200
        mock_svc.test_script.assert_awaited_once_with("return 1", {"a": 1}, None)

    def test_test_script_with_context(self, client, mock_svc):
        resp = client.post(
            "/api/v1/platforms/test-script",
            json={"script": "return 1", "test_payload": {"a": 1}, "test_context": {"k": "v"}},
        )
        assert resp.status_code == 200

    def test_test_script_value_error_422(self, client, mock_svc):
        mock_svc.test_script.side_effect = ValueError("bad")
        resp = client.post(
            "/api/v1/platforms/test-script",
            json={"script": "x", "test_payload": {"a": 1}},
        )
        assert resp.status_code == 422

    def test_test_script_error_500(self, client, mock_svc):
        mock_svc.test_script.side_effect = RuntimeError("boom")
        resp = client.post(
            "/api/v1/platforms/test-script",
            json={"script": "x", "test_payload": {"a": 1}},
        )
        assert resp.status_code == 500

    def test_test_script_forbidden_for_viewer(self, make_app_with):
        c = TestClient(make_app_with("viewer"))
        resp = c.post(
            "/api/v1/platforms/test-script",
            json={"script": "x", "test_payload": {"a": 1}},
        )
        assert resp.status_code == 403


# -- POST /mqtt-test-publish/{platform_name} -----------------------------


class TestMqttTestPublish:
    def test_publish_ok_default_qos(self, client, mock_svc):
        resp = client.post(
            "/api/v1/platforms/mqtt-test-publish/mqtt",
            json={"topic": "t/a", "payload": "hello"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["published"] is True
        mock_svc.mqtt_test_publish.assert_awaited_once_with("mqtt", "t/a", "hello", 0)

    def test_publish_ok_qos_2(self, client, mock_svc):
        resp = client.post(
            "/api/v1/platforms/mqtt-test-publish/mqtt",
            json={"topic": "t/a", "payload": "hello", "qos": 2},
        )
        assert resp.status_code == 200
        mock_svc.mqtt_test_publish.assert_awaited_once_with("mqtt", "t/a", "hello", 2)

    def test_publish_invalid_qos_422(self, client):
        resp = client.post(
            "/api/v1/platforms/mqtt-test-publish/mqtt",
            json={"topic": "t/a", "payload": "hello", "qos": 5},
        )
        assert resp.status_code == 422

    def test_publish_error_500(self, client, mock_svc):
        mock_svc.mqtt_test_publish.side_effect = RuntimeError("boom")
        resp = client.post(
            "/api/v1/platforms/mqtt-test-publish/mqtt",
            json={"topic": "t/a", "payload": "hello"},
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "ERR_PLATFORM_MQTT_PUBLISH_FAILED"

    def test_publish_forbidden_for_viewer(self, make_app_with):
        c = TestClient(make_app_with("viewer"))
        resp = c.post(
            "/api/v1/platforms/mqtt-test-publish/mqtt",
            json={"topic": "t/a", "payload": "hello"},
        )
        assert resp.status_code == 403


# -- Path parameter validation (generic) ---------------------------------


class TestPathValidation:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/platforms/status/bad%20name",
            "/api/v1/platforms/status/name%21",
            "/api/v1/platforms/status/with.dot",
        ],
    )
    def test_invalid_platform_name_rejected(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 422
