"""MCP service unit tests.

Covers src/edgelite/services/mcp_service.py:
- MCPToolService: tool registration/properties/call/validation
- MCPAuthManager: key load/save/list/create/delete/verify
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from edgelite.services.mcp_service import MCPAuthManager, MCPToolService


@pytest.fixture
def service():
    return MCPToolService()


@pytest.fixture
def auth_manager(tmp_path, monkeypatch):
    store_file = tmp_path / "mcp_keys.json"
    monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store_file)
    return MCPAuthManager()


def _make_device_service():
    ds = AsyncMock()
    ds.list_devices = AsyncMock(return_value=([{"id": "d1"}], 1))
    ds.get_device = AsyncMock(return_value={"id": "d1", "name": "device1"})
    ds.read_points = AsyncMock(return_value=[{"name": "p1", "value": 1}])
    ds.write_point = AsyncMock(return_value=None)
    ds._driver_instances = {}
    return ds


def _make_alarm_service():
    al = AsyncMock()
    al.list_alarms = AsyncMock(return_value=([{"id": "a1"}], 1))
    return al


def _make_system_service():
    ss = AsyncMock()
    ss.get_status = AsyncMock(return_value={"cpu": 50})
    return ss


def _make_rule_service():
    rs = AsyncMock()
    rs.list_rules = AsyncMock(return_value=([{"id": "r1"}], 1))
    return rs


class TestMCPToolServiceRegistration:
    def test_init_registers_all_tools(self, service):
        expected = {
            "list_devices",
            "get_device_status",
            "read_device_points",
            "write_device_point",
            "list_alarms",
            "get_system_status",
            "list_rules",
            "ai_inference",
            "ai_model_status",
            "ai_anomaly_history",
            "ai_submit_feedback",
        }
        assert set(service.tools.keys()) == expected

    def test_init_registers_all_resources(self, service):
        expected = {
            "devices",
            "alarms/active",
            "system/status",
            "ai/models",
            "ai/inference/recent",
            "ai/anomalies",
        }
        assert set(service.resources.keys()) == expected

    def test_init_registers_all_prompts(self, service):
        assert set(service.prompts.keys()) == {"analyze_device", "alarm_summary"}

    def test_tools_property_returns_dict(self, service):
        tools = service.tools
        assert isinstance(tools, dict)
        assert tools["list_devices"]["name"] == "list_devices"
        assert "inputSchema" in tools["list_devices"]

    def test_resources_property_returns_dict(self, service):
        resources = service.resources
        assert resources["devices"]["uri"] == "edgelite://devices"
        assert resources["devices"]["mimeType"] == "application/json"

    def test_prompts_property_returns_dict(self, service):
        prompts = service.prompts
        assert prompts["analyze_device"]["arguments"][0]["required"] is True
        assert prompts["alarm_summary"]["arguments"][0]["required"] is False

    def test_register_tools_idempotent(self, service):
        service._tools.clear()
        service._register_tools()
        assert len(service._tools) == 11

    def test_register_resources_idempotent(self, service):
        service._resources.clear()
        service._register_resources()
        assert len(service._resources) == 6

    def test_register_prompts_idempotent(self, service):
        service._prompts.clear()
        service._register_prompts()
        assert len(service._prompts) == 2

    def test_tool_input_schemas_have_required(self, service):
        assert service.tools["get_device_status"]["inputSchema"]["required"] == ["device_id"]
        assert service.tools["write_device_point"]["inputSchema"]["required"] == [
            "device_id",
            "point_name",
            "value",
        ]
        assert service.tools["ai_inference"]["inputSchema"]["required"] == [
            "model_id",
            "input_data",
        ]
        assert service.tools["ai_submit_feedback"]["inputSchema"]["required"] == [
            "model_id",
            "feedback_type",
        ]

    def test_initial_ai_dependencies_none(self, service):
        assert service._ai_scheduler is None
        assert service._anomaly_learner is None
        assert service._threshold_learner is None


class TestSetAIDependencies:
    def test_set_all_dependencies(self, service):
        sched = MagicMock()
        anomaly = MagicMock()
        threshold = MagicMock()
        service.set_ai_dependencies(sched, anomaly, threshold)
        assert service._ai_scheduler is sched
        assert service._anomaly_learner is anomaly
        assert service._threshold_learner is threshold

    def test_set_partial_dependencies_keeps_existing(self, service):
        sched = MagicMock()
        service.set_ai_dependencies(ai_scheduler=sched)
        assert service._ai_scheduler is sched
        assert service._anomaly_learner is None

    def test_set_none_does_not_overwrite(self, service):
        sched = MagicMock()
        service.set_ai_dependencies(ai_scheduler=sched)
        service.set_ai_dependencies(ai_scheduler=None)
        assert service._ai_scheduler is sched

    def test_set_empty_call_no_change(self, service):
        service.set_ai_dependencies()
        assert service._ai_scheduler is None


class TestValidateToolCall:
    def test_unknown_tool_raises(self, service):
        with pytest.raises(HTTPException) as exc:
            service.validate_tool_call("not_a_tool", {})
        assert exc.value.status_code == 400

    def test_valid_no_required(self, service):
        errors = service.validate_tool_call("list_devices", None)
        assert errors == []

    def test_missing_required_param(self, service):
        errors = service.validate_tool_call("get_device_status", {})
        assert len(errors) == 1
        assert "device_id" in errors[0]

    def test_unknown_param(self, service):
        errors = service.validate_tool_call("get_device_status", {"device_id": "d1", "extra": 1})
        assert any("extra" in e for e in errors)

    def test_multiple_missing_required(self, service):
        errors = service.validate_tool_call("write_device_point", {})
        assert len(errors) == 3

    def test_all_valid(self, service):
        errors = service.validate_tool_call(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": 1},
        )
        assert errors == []

    def test_arguments_none_treated_as_empty(self, service):
        errors = service.validate_tool_call("list_alarms", None)
        assert errors == []

    def test_tool_def_without_input_schema(self, service):
        service._tools["custom"] = {"name": "custom"}
        errors = service.validate_tool_call("custom", {"x": 1})
        assert errors == []


class TestCallToolDevices:
    async def test_list_devices_success(self, service):
        ds = _make_device_service()
        result = await service.call_tool("list_devices", {}, device_service=ds)
        assert result == {"devices": [{"id": "d1"}], "total": 1}
        ds.list_devices.assert_awaited_once()
        assert ds.list_devices.call_args.kwargs["page"] == 1

    async def test_list_devices_no_service(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("list_devices", {})
        assert exc.value.status_code == 503

    async def test_get_device_status_success(self, service):
        ds = _make_device_service()
        result = await service.call_tool(
            "get_device_status", {"device_id": "d1"}, device_service=ds
        )
        assert result == {"id": "d1", "name": "device1"}

    async def test_get_device_status_missing_id(self, service):
        ds = _make_device_service()
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("get_device_status", {}, device_service=ds)
        assert exc.value.status_code == 400

    async def test_get_device_status_no_service(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("get_device_status", {"device_id": "d1"})
        assert exc.value.status_code == 503

    async def test_get_device_status_not_found(self, service):
        ds = _make_device_service()
        ds.get_device = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "get_device_status", {"device_id": "missing"}, device_service=ds
            )
        assert exc.value.status_code == 404

    async def test_read_device_points_success(self, service):
        ds = _make_device_service()
        result = await service.call_tool(
            "read_device_points", {"device_id": "d1"}, device_service=ds
        )
        assert result == {"device_id": "d1", "points": [{"name": "p1", "value": 1}]}

    async def test_read_device_points_missing_id(self, service):
        ds = _make_device_service()
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("read_device_points", {}, device_service=ds)
        assert exc.value.status_code == 400

    async def test_read_device_points_no_service(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("read_device_points", {"device_id": "d1"})
        assert exc.value.status_code == 503


class TestCallToolWritePoint:
    async def test_write_device_point_int_value(self, service):
        ds = _make_device_service()
        audit = AsyncMock()
        result = await service.call_tool(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": 5},
            device_service=ds,
            user={"username": "alice", "role": "admin", "user_id": "u1"},
            audit_svc=audit,
        )
        assert result["success"] is True
        assert result["value"] == 5
        ds.write_point.assert_awaited_once()
        mcp_user = ds.write_point.call_args.kwargs["user"]
        assert mcp_user["username"] == "mcp:alice"
        assert mcp_user["source"] == "mcp"
        audit.log.assert_awaited_once()

    async def test_write_device_point_no_user_defaults_to_system(self, service):
        ds = _make_device_service()
        audit = AsyncMock()
        await service.call_tool(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": 1},
            device_service=ds,
            audit_svc=audit,
        )
        mcp_user = ds.write_point.call_args.kwargs["user"]
        assert mcp_user["username"] == "mcp:system"
        assert mcp_user["role"] == "admin"

    async def test_write_device_point_string_int(self, service):
        ds = _make_device_service()
        result = await service.call_tool(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": "5"},
            device_service=ds,
            audit_svc=AsyncMock(),
        )
        assert result["value"] == 5
        assert isinstance(result["value"], int)

    async def test_write_device_point_string_float(self, service):
        ds = _make_device_service()
        result = await service.call_tool(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": "1.5"},
            device_service=ds,
            audit_svc=AsyncMock(),
        )
        assert result["value"] == 1.5

    async def test_write_device_point_string_true(self, service):
        ds = _make_device_service()
        result = await service.call_tool(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": "true"},
            device_service=ds,
            audit_svc=AsyncMock(),
        )
        assert result["value"] is True

    async def test_write_device_point_string_false(self, service):
        ds = _make_device_service()
        result = await service.call_tool(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": "off"},
            device_service=ds,
            audit_svc=AsyncMock(),
        )
        assert result["value"] is False

    async def test_write_device_point_invalid_string(self, service):
        ds = _make_device_service()
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "write_device_point",
                {"device_id": "d1", "point_name": "p1", "value": "abc"},
                device_service=ds,
                audit_svc=AsyncMock(),
            )
        assert exc.value.status_code == 400

    async def test_write_device_point_missing_params(self, service):
        ds = _make_device_service()
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "write_device_point",
                {"device_id": "", "point_name": "", "value": 1},
                device_service=ds,
            )
        assert exc.value.status_code == 400

    async def test_write_device_point_no_service(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "write_device_point",
                {"device_id": "d1", "point_name": "p1", "value": 1},
            )
        assert exc.value.status_code == 503

    async def test_write_device_point_driver_blocks_write(self, service):
        ds = _make_device_service()
        driver = MagicMock()
        driver.check_write_allowed = MagicMock(return_value=False)
        ds._driver_instances = {"d1": driver}
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "write_device_point",
                {"device_id": "d1", "point_name": "p1", "value": 1},
                device_service=ds,
                audit_svc=AsyncMock(),
            )
        assert exc.value.status_code == 403

    async def test_write_device_point_driver_allows_write(self, service):
        ds = _make_device_service()
        driver = MagicMock()
        driver.check_write_allowed = MagicMock(return_value=True)
        ds._driver_instances = {"d1": driver}
        result = await service.call_tool(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": 1},
            device_service=ds,
            audit_svc=AsyncMock(),
        )
        assert result["success"] is True
        driver.check_write_allowed.assert_called_once_with("d1", "p1")

    async def test_write_device_point_driver_check_raises(self, service):
        ds = _make_device_service()
        driver = MagicMock()
        driver.check_write_allowed = MagicMock(side_effect=RuntimeError("boom"))
        ds._driver_instances = {"d1": driver}
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "write_device_point",
                {"device_id": "d1", "point_name": "p1", "value": 1},
                device_service=ds,
                audit_svc=AsyncMock(),
            )
        assert exc.value.status_code == 403

    async def test_write_device_point_audit_log_failure_swallowed(self, service):
        ds = _make_device_service()
        audit = AsyncMock()
        audit.log = AsyncMock(side_effect=RuntimeError("audit fail"))
        result = await service.call_tool(
            "write_device_point",
            {"device_id": "d1", "point_name": "p1", "value": 1},
            device_service=ds,
            audit_svc=audit,
        )
        assert result["success"] is True

    async def test_write_device_point_audit_via_app_state(self, service):
        ds = _make_device_service()
        audit = AsyncMock()
        mock_state = SimpleNamespace(audit_service=audit)
        with patch("edgelite.app._app_state", mock_state):
            result = await service.call_tool(
                "write_device_point",
                {"device_id": "d1", "point_name": "p1", "value": 1},
                device_service=ds,
            )
        assert result["success"] is True
        audit.log.assert_awaited_once()

    async def test_write_device_point_no_audit_available(self, service):
        ds = _make_device_service()
        mock_state = SimpleNamespace(audit_service=None)
        with patch("edgelite.app._app_state", mock_state):
            result = await service.call_tool(
                "write_device_point",
                {"device_id": "d1", "point_name": "p1", "value": 1},
                device_service=ds,
            )
        assert result["success"] is True
        ds.write_point.assert_awaited_once()


class TestCallToolAlarmSystemRule:
    async def test_list_alarms_success(self, service):
        al = _make_alarm_service()
        result = await service.call_tool("list_alarms", {"severity": "critical"}, alarm_service=al)
        assert result == {"alarms": [{"id": "a1"}], "total": 1}
        assert al.list_alarms.call_args.kwargs["severity"] == "critical"

    async def test_list_alarms_no_severity(self, service):
        al = _make_alarm_service()
        await service.call_tool("list_alarms", {}, alarm_service=al)
        assert al.list_alarms.call_args.kwargs["severity"] is None

    async def test_list_alarms_no_service(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("list_alarms", {})
        assert exc.value.status_code == 503

    async def test_get_system_status_success(self, service):
        ss = _make_system_service()
        result = await service.call_tool("get_system_status", {}, system_service=ss)
        assert result == {"cpu": 50}

    async def test_get_system_status_no_service(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("get_system_status", {})
        assert exc.value.status_code == 503

    async def test_list_rules_success(self, service):
        rs = _make_rule_service()
        result = await service.call_tool("list_rules", {}, rule_service=rs)
        assert result == {"rules": [{"id": "r1"}], "total": 1}

    async def test_list_rules_no_service(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("list_rules", {})
        assert exc.value.status_code == 503


class TestCallToolAI:
    async def test_ai_inference_success_with_dict(self, service):
        sched = AsyncMock()
        result_obj = SimpleNamespace(score=0.9, label="anomaly", _private="x")
        sched.submit_and_wait = AsyncMock(return_value=result_obj)
        service.set_ai_dependencies(ai_scheduler=sched)
        result = await service.call_tool(
            "ai_inference",
            {"model_id": "elg-anomaly-v1", "input_data": [1, 2, 3]},
        )
        assert result["model_id"] == "elg-anomaly-v1"
        assert "_private" not in result["result"]
        assert result["result"]["score"] == 0.9

    async def test_ai_inference_success_plain_result(self, service):
        sched = AsyncMock()
        sched.submit_and_wait = AsyncMock(return_value={"plain": True})
        service.set_ai_dependencies(ai_scheduler=sched)
        result = await service.call_tool(
            "ai_inference",
            {"model_id": "elg-anomaly-v1", "input_data": [1]},
        )
        assert result["result"] == {"plain": True}

    async def test_ai_inference_missing_model_id(self, service):
        sched = AsyncMock()
        service.set_ai_dependencies(ai_scheduler=sched)
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("ai_inference", {"model_id": "", "input_data": []})
        assert exc.value.status_code == 400

    async def test_ai_inference_no_scheduler(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "ai_inference",
                {"model_id": "elg-anomaly-v1", "input_data": []},
            )
        assert exc.value.status_code == 503

    async def test_ai_model_status_all(self, service):
        sched = AsyncMock()
        sched.get_stats = AsyncMock(return_value={"models": 3})
        service.set_ai_dependencies(ai_scheduler=sched)
        result = await service.call_tool("ai_model_status", {})
        assert result == {"models": 3}
        sched.get_stats.assert_awaited_once()

    async def test_ai_model_status_specific_found(self, service):
        sched = AsyncMock()
        sched.get_model_metrics = AsyncMock(return_value={"accuracy": 0.95})
        service.set_ai_dependencies(ai_scheduler=sched)
        result = await service.call_tool(
            "ai_model_status", {"model_id": "elg-anomaly-v1"}
        )
        assert result == {"accuracy": 0.95}

    async def test_ai_model_status_specific_not_found(self, service):
        sched = AsyncMock()
        sched.get_model_metrics = AsyncMock(return_value=None)
        service.set_ai_dependencies(ai_scheduler=sched)
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "ai_model_status", {"model_id": "missing-model"}
            )
        assert exc.value.status_code == 404

    async def test_ai_model_status_no_scheduler(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("ai_model_status", {})
        assert exc.value.status_code == 503

    async def test_ai_anomaly_history_no_learner(self, service):
        result = await service.call_tool("ai_anomaly_history", {})
        assert result == {"anomalies": [], "total": 0}

    async def test_ai_anomaly_history_all(self, service):
        learner = MagicMock()
        learner.get_dashboard = MagicMock(
            return_value={"recent_anomalies": [{"device_id": "d1"}, {"device_id": "d2"}]}
        )
        service.set_ai_dependencies(anomaly_learner=learner)
        result = await service.call_tool("ai_anomaly_history", {})
        assert len(result["anomalies"]) == 2
        assert result["total"] == 2

    async def test_ai_anomaly_history_filtered_by_device(self, service):
        learner = MagicMock()
        learner.get_dashboard = MagicMock(
            return_value={
                "recent_anomalies": [
                    {"device_id": "d1"},
                    {"device_id": "d2"},
                    {"device_id": "d1"},
                ]
            }
        )
        service.set_ai_dependencies(anomaly_learner=learner)
        result = await service.call_tool(
            "ai_anomaly_history", {"device_id": "d1"}
        )
        assert len(result["anomalies"]) == 2
        assert all(a["device_id"] == "d1" for a in result["anomalies"])

    async def test_ai_anomaly_history_limit(self, service):
        learner = MagicMock()
        learner.get_dashboard = MagicMock(
            return_value={"recent_anomalies": [{"i": i} for i in range(10)]}
        )
        service.set_ai_dependencies(anomaly_learner=learner)
        result = await service.call_tool("ai_anomaly_history", {"limit": 3})
        assert len(result["anomalies"]) == 3
        assert result["total"] == 10

    async def test_ai_anomaly_history_no_dashboard_method(self, service):
        learner = MagicMock(spec=[])
        service.set_ai_dependencies(anomaly_learner=learner)
        result = await service.call_tool("ai_anomaly_history", {})
        assert result == {"anomalies": [], "total": 0}

    async def test_ai_submit_feedback_anomaly(self, service):
        learner = AsyncMock()
        learner.submit_feedback = AsyncMock(return_value={"ok": True})
        service.set_ai_dependencies(anomaly_learner=learner)
        result = await service.call_tool(
            "ai_submit_feedback",
            {"model_id": "elg-anomaly-v1", "feedback_type": "confirmed"},
        )
        assert result["status"] == "ok"
        learner.submit_feedback.assert_awaited_once()
        kwargs = learner.submit_feedback.call_args.kwargs
        assert kwargs["feedback"] == "confirmed"
        assert kwargs["is_anomaly"] is False

    async def test_ai_submit_feedback_threshold(self, service):
        learner = AsyncMock()
        learner.submit_feedback = AsyncMock(return_value={"ok": True})
        service.set_ai_dependencies(threshold_learner=learner)
        result = await service.call_tool(
            "ai_submit_feedback",
            {"model_id": "elg-threshold-v1", "feedback_type": "too_sensitive"},
        )
        assert result["status"] == "ok"
        kwargs = learner.submit_feedback.call_args.kwargs
        assert kwargs["feedback_type"] == "too_sensitive"

    async def test_ai_submit_feedback_missing_params(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("ai_submit_feedback", {"model_id": ""})
        assert exc.value.status_code == 400

    async def test_ai_submit_feedback_no_learner(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "ai_submit_feedback",
                {"model_id": "elg-anomaly-v1", "feedback_type": "confirmed"},
            )
        assert exc.value.status_code == 404

    async def test_ai_submit_feedback_unknown_model(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool(
                "ai_submit_feedback",
                {"model_id": "unknown-model", "feedback_type": "confirmed"},
            )
        assert exc.value.status_code == 404


class TestCallToolErrors:
    async def test_unknown_tool_raises_400(self, service):
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("not_a_tool", {})
        assert exc.value.status_code == 400

    async def test_generic_exception_becomes_500(self, service):
        ds = _make_device_service()
        ds.list_devices = AsyncMock(side_effect=ValueError("boom"))
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("list_devices", {}, device_service=ds)
        assert exc.value.status_code == 500

    async def test_http_exception_propagates(self, service):
        ds = _make_device_service()
        ds.list_devices = AsyncMock(side_effect=HTTPException(status_code=418, detail="teapot"))
        with pytest.raises(HTTPException) as exc:
            await service.call_tool("list_devices", {}, device_service=ds)
        assert exc.value.status_code == 418

    async def test_call_with_none_arguments(self, service):
        ds = _make_device_service()
        result = await service.call_tool("list_devices", None, device_service=ds)
        assert result["total"] == 1


class TestMCPAuthManagerLoadSave:
    def test_init_no_file(self, auth_manager):
        assert auth_manager.enabled is False
        assert auth_manager.list_keys() == []

    def test_load_existing_file(self, tmp_path, monkeypatch):
        store = tmp_path / "mcp_keys.json"
        data = {
            "keys": {
                "k1": {
                    "name": "test-key",
                    "scopes": ["read"],
                    "key_hash": "abc123",
                    "created_at": "2026-01-01",
                }
            },
            "enabled": True,
        }
        store.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store)
        mgr = MCPAuthManager()
        assert mgr.enabled is True
        keys = mgr.list_keys()
        assert len(keys) == 1
        assert keys[0]["name"] == "test-key"

    def test_load_broken_file_resets_to_empty(self, tmp_path, monkeypatch):
        store = tmp_path / "mcp_keys.json"
        store.write_text("not-json{", encoding="utf-8")
        monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store)
        mgr = MCPAuthManager()
        assert mgr.enabled is False
        assert mgr.list_keys() == []

    def test_load_file_missing_keys_field(self, tmp_path, monkeypatch):
        store = tmp_path / "mcp_keys.json"
        store.write_text(json.dumps({"enabled": True}), encoding="utf-8")
        monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store)
        mgr = MCPAuthManager()
        assert mgr.enabled is True
        assert mgr.list_keys() == []

    def test_save_persists_with_hash(self, auth_manager):
        result = auth_manager.create_key("mykey", ["read"])
        store_file = MCPAuthManager._STORE_FILE
        assert store_file.exists()
        data = json.loads(store_file.read_text(encoding="utf-8"))
        stored = data["keys"][result["id"]]
        assert "key" not in stored
        assert "key_hash" in stored
        assert data["enabled"] is True

    def test_save_failure_swallowed(self, tmp_path, monkeypatch):
        store = tmp_path / "subdir" / "mcp_keys.json"
        monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store)
        mgr = MCPAuthManager()
        with patch("builtins.open", side_effect=OSError("denied")):
            mgr._save()

    def test_save_entries_without_key_field(self, tmp_path, monkeypatch):
        """从文件加载的条目只有 key_hash 无 key，_save 时应跳过哈希直接保留"""
        store = tmp_path / "mcp_keys.json"
        data = {
            "keys": {
                "k1": {
                    "name": "hash-only",
                    "scopes": ["read"],
                    "key_hash": "abc123",
                    "created_at": "2026-01-01",
                }
            },
            "enabled": True,
        }
        store.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store)
        mgr = MCPAuthManager()
        mgr._save()
        saved = json.loads(store.read_text(encoding="utf-8"))
        assert "key_hash" in saved["keys"]["k1"]
        assert "key" not in saved["keys"]["k1"]


class TestMCPAuthManagerKeys:
    def test_create_key(self, auth_manager):
        result = auth_manager.create_key("test", ["read", "write"])
        assert result["name"] == "test"
        assert result["scopes"] == ["read", "write"]
        assert result["key"].startswith("mcp_")
        assert result["id"]
        assert auth_manager.enabled is True

    def test_create_key_enables_auth(self, auth_manager):
        assert auth_manager.enabled is False
        auth_manager.create_key("k", ["read"])
        assert auth_manager.enabled is True

    def test_list_keys_masks_key(self, auth_manager):
        auth_manager.create_key("test-key-name", ["read"])
        keys = auth_manager.list_keys()
        assert len(keys) == 1
        masked = keys[0]["key"]
        assert masked.endswith("****")
        assert len(masked) == 8

    def test_list_keys_returns_metadata(self, auth_manager):
        auth_manager.create_key("named", ["read"])
        keys = auth_manager.list_keys()
        assert keys[0]["name"] == "named"
        assert keys[0]["scopes"] == ["read"]
        assert keys[0]["created_at"]
        assert "id" in keys[0]

    def test_delete_key_existing(self, auth_manager):
        result = auth_manager.create_key("k", ["read"])
        assert auth_manager.delete_key(result["id"]) is True
        assert auth_manager.list_keys() == []
        assert auth_manager.enabled is False

    def test_delete_key_nonexistent(self, auth_manager):
        assert auth_manager.delete_key("nonexistent") is False

    def test_delete_key_keeps_enabled_if_others_remain(self, auth_manager):
        r1 = auth_manager.create_key("k1", ["read"])
        auth_manager.create_key("k2", ["read"])
        auth_manager.delete_key(r1["id"])
        assert auth_manager.enabled is True
        assert len(auth_manager.list_keys()) == 1

    def test_mask_key_long(self):
        assert MCPAuthManager._mask_key("mcp_abcdefghijklmnop") == "mcp_****"

    def test_mask_key_short(self):
        assert MCPAuthManager._mask_key("short") == "****"

    def test_mask_key_empty(self):
        assert MCPAuthManager._mask_key("") == "****"

    def test_mask_key_exactly_8(self):
        assert MCPAuthManager._mask_key("12345678") == "****"


class TestMCPAuthManagerVerify:
    def test_verify_plaintext_key(self, auth_manager):
        result = auth_manager.create_key("test", ["read", "write"])
        verified = auth_manager.verify_key(result["key"])
        assert verified is not None
        assert verified["name"] == "test"
        assert verified["scopes"] == ["read", "write"]
        assert "id" in verified

    def test_verify_wrong_key(self, auth_manager):
        auth_manager.create_key("test", ["read"])
        assert auth_manager.verify_key("mcp_wrong_key") is None

    def test_verify_empty_keys(self, auth_manager):
        assert auth_manager.verify_key("any") is None

    def test_verify_hash_key_after_reload(self, tmp_path, monkeypatch):
        store = tmp_path / "mcp_keys.json"
        monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store)
        mgr1 = MCPAuthManager()
        created = mgr1.create_key("reload-test", ["read"])
        api_key = created["key"]
        mgr2 = MCPAuthManager()
        verified = mgr2.verify_key(api_key)
        assert verified is not None
        assert verified["name"] == "reload-test"

    def test_verify_skips_entries_without_name_or_scopes(self, tmp_path, monkeypatch):
        store = tmp_path / "mcp_keys.json"
        data = {
            "keys": {
                "bad1": {"scopes": ["read"], "key": "abc"},
                "bad2": {"name": "x", "key": "abc"},
            },
            "enabled": True,
        }
        store.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store)
        mgr = MCPAuthManager()
        assert mgr.verify_key("abc") is None

    def test_verify_key_hash_mismatch(self, tmp_path, monkeypatch):
        store = tmp_path / "mcp_keys.json"
        data = {
            "keys": {
                "k1": {
                    "name": "test",
                    "scopes": ["read"],
                    "key_hash": "0" * 64,
                    "created_at": "2026-01-01",
                }
            },
            "enabled": True,
        }
        store.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(MCPAuthManager, "_STORE_FILE", store)
        mgr = MCPAuthManager()
        assert mgr.verify_key("mcp_something") is None


class TestMCPAuthManagerConcurrency:
    def test_concurrent_create_and_list(self, auth_manager):
        errors = []

        def creator():
            try:
                for i in range(20):
                    auth_manager.create_key(f"k{i}", ["read"])
            except Exception as e:
                errors.append(e)

        def lister():
            try:
                for _ in range(20):
                    auth_manager.list_keys()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=creator)
        t2 = threading.Thread(target=lister)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == []
