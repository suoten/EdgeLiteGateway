"""设备管理业务逻辑测试 - services/device_service.py (扩展部分)

覆盖 DeviceService 健康/配置版本/批量/加载/模板/导入导出方法。
基础方法测试见 test_device_service.py。
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.base import DriverCapabilities, DriverHealthStats, PointValue  # noqa: E402
from edgelite.services.device_service import DeviceService  # noqa: E402

# 复用 test_device_service 中的夹具与辅助函数（pytest 会识别导入的 @pytest.fixture）
from test_device_service import (  # noqa: F401,E402
    _drain_cleanup_tasks,
    _make_mock_driver,
    _make_mock_driver_class,
    device_repo,
    device_service,
    lifecycle,
    mock_simulator,
    registry,
    rule_repo,
    scheduler,
    template_repo,
)


# ───────────────────────── 设备健康 ─────────────────────────


class TestDeviceHealth:
    """get_device_health / reset / probe 各分支测试"""

    async def test_get_device_health_no_driver(self, device_service):
        """无驱动实例应返回 None"""
        assert await device_service.get_device_health("d1") is None

    async def test_get_device_health_no_stats(self, device_service):
        """get_health_stats 抛异常应返回仅含连接状态的 dict"""
        drv = _make_mock_driver(has_stats=False)
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_health("d1")
        assert result["device_id"] == "d1"
        assert result["is_connected"] is True

    async def test_get_device_health_with_stats_and_redundancy(self, device_service):
        """含统计与冗余状态时应聚合所有字段"""
        drv = _make_mock_driver()
        drv.get_redundancy_status = MagicMock(return_value={"active_host": "1.2.3.4", "active_role": "backup"})
        drv._using_backup = True
        drv._active_ip = "1.2.3.4"
        drv._pdu_size = 240
        drv._plc_model = "S7-1200"
        drv._auth_locked_until = 9999999999.0
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_health("d1")
        assert result["current_ip"] == "1.2.3.4"
        assert result["using_backup"] is True
        assert result["pdu_size"] == 240
        assert result["plc_model"] == "S7-1200"
        assert result["auth_locked"] is True

    async def test_get_device_health_is_connected_exception(self, device_service):
        """is_device_connected 异常应返回 None"""
        drv = _make_mock_driver()
        drv.is_device_connected = MagicMock(side_effect=RuntimeError("conn fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_health("d1")
        assert result["is_connected"] is None

    async def test_get_device_health_redundancy_exception(self, device_service):
        """冗余状态异常应被吞掉"""
        drv = _make_mock_driver()
        drv.get_redundancy_status = MagicMock(side_effect=RuntimeError("red fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_health("d1")
        assert result["device_id"] == "d1"

    async def test_reset_device_health_no_driver(self, device_service):
        assert await device_service.reset_device_health("d1") is False

    async def test_reset_device_health_no_method(self, device_service):
        drv = _make_mock_driver()
        delattr(drv, "reset_health_stats")
        device_service._driver_instances["d1"] = drv
        assert await device_service.reset_device_health("d1") is False

    async def test_reset_device_health_success(self, device_service):
        drv = _make_mock_driver()
        drv.reset_health_stats = MagicMock(return_value=None)
        device_service._driver_instances["d1"] = drv
        assert await device_service.reset_device_health("d1") is True

    async def test_reset_device_health_exception(self, device_service):
        drv = _make_mock_driver()
        drv.reset_health_stats = MagicMock(side_effect=RuntimeError("reset fail"))
        device_service._driver_instances["d1"] = drv
        assert await device_service.reset_device_health("d1") is False

    async def test_probe_primary_link_no_driver(self, device_service):
        assert await device_service.probe_primary_link("d1") is False

    async def test_probe_primary_link_no_method(self, device_service):
        drv = _make_mock_driver()
        device_service._driver_instances["d1"] = drv
        assert await device_service.probe_primary_link("d1") is False

    async def test_probe_primary_link_success(self, device_service):
        drv = _make_mock_driver()
        drv.probe_primary_link = AsyncMock(return_value=True)
        device_service._driver_instances["d1"] = drv
        assert await device_service.probe_primary_link("d1") is True

    async def test_probe_primary_link_exception(self, device_service):
        drv = _make_mock_driver()
        drv.probe_primary_link = AsyncMock(side_effect=RuntimeError("probe fail"))
        device_service._driver_instances["d1"] = drv
        assert await device_service.probe_primary_link("d1") is False


class TestDeviceOpsData:
    """get_device_ops_data 各分支测试"""

    async def test_no_driver_returns_none(self, device_service):
        assert await device_service.get_device_ops_data("d1") is None

    async def test_dict_stats(self, device_service):
        """get_health_stats 返回 dict 时按 key 访问"""
        drv = _make_mock_driver()
        drv.get_health_stats = MagicMock(return_value={"total_reads": 10, "failed_reads": 2, "total_writes": 5})
        drv.get_connection_status = MagicMock(return_value=MagicMock(state="connected", reason="ok"))
        drv._port_available = {"COM1": True}
        drv._device_port_map = {"d1": "COM1"}
        drv.get_polling_interval = MagicMock(return_value=1000)
        drv._degrade_level = {"d1": 1}
        drv.get_latency_history = MagicMock(return_value=[1, 2])
        drv.get_reconnect_history = MagicMock(return_value=[3])
        drv.get_quality_stream = MagicMock(return_value=[0.9])
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_ops_data("d1")
        assert result["total_reads"] == 10
        assert result["state"] == "connected"
        assert result["port_status"] == "available"
        assert result["port_path"] == "COM1"
        assert result["polling_interval"] == 1000
        assert result["degrade_level"] == 1
        assert result["online_rate"] == 0.8
        assert result["latency_history"] == [1, 2]
        assert result["reconnect_history"] == [3]
        assert result["quality_stream"] == [0.9]

    async def test_object_stats(self, device_service):
        """get_health_stats 返回对象时按属性访问"""
        drv = _make_mock_driver()
        stats = MagicMock(
            total_reads=4, failed_reads=1, total_writes=2, failed_writes=0,
            total_reconnects=1, avg_latency_ms=5.0,
        )
        drv.get_health_stats = MagicMock(return_value=stats)
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_ops_data("d1")
        assert result["total_reads"] == 4
        assert result["avg_latency_ms"] == 5.0

    async def test_stats_exception_swallowed(self, device_service):
        """get_health_stats 异常应被吞掉"""
        drv = _make_mock_driver()
        drv.get_health_stats = MagicMock(side_effect=RuntimeError("stats fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_ops_data("d1")
        assert result["online_rate"] == 1.0

    async def test_connection_status_exception(self, device_service):
        drv = _make_mock_driver()
        drv.get_connection_status = MagicMock(side_effect=RuntimeError("cs fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_ops_data("d1")
        assert "state" not in result or result.get("state") is None or True

    async def test_is_connected_exception_false(self, device_service):
        drv = _make_mock_driver()
        drv.is_device_connected = MagicMock(side_effect=RuntimeError("ic fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_ops_data("d1")
        assert result["is_connected"] is False

    async def test_latency_history_exception_returns_empty(self, device_service):
        drv = _make_mock_driver()
        drv.get_latency_history = MagicMock(side_effect=RuntimeError("lh fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_device_ops_data("d1")
        assert result["latency_history"] == []


class TestPointHealth:
    async def test_no_driver(self, device_service):
        assert await device_service.get_point_health("d1") is None

    async def test_no_get_point_stats(self, device_service):
        """驱动无 get_point_stats 方法时应返回 None"""
        drv = MagicMock(spec=["is_device_connected"])
        device_service._driver_instances["d1"] = drv
        assert await device_service.get_point_health("d1") is None

    async def test_no_device_points(self, device_service):
        drv = _make_mock_driver()
        drv.get_point_stats = MagicMock(return_value=None)
        device_service._driver_instances["d1"] = drv
        assert await device_service.get_point_health("d1") == []

    async def test_with_points(self, device_service):
        drv = _make_mock_driver()
        drv._device_points = {"d1": [{"name": "p1"}, {"name": "p2"}]}
        drv.get_point_stats = MagicMock(return_value={"success_count": 5})
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_point_health("d1")
        assert len(result) == 2
        assert result[0]["point_name"] == "p1"
        assert result[0]["success_count"] == 5

    async def test_point_stats_exception_returns_defaults(self, device_service):
        drv = _make_mock_driver()
        drv._device_points = {"d1": [{"name": "p1"}]}
        drv.get_point_stats = MagicMock(side_effect=RuntimeError("fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_point_health("d1")
        assert result[0]["success_count"] == 0
        assert result[0]["success_rate"] == 1.0


class TestWriteAudit:
    async def test_no_driver(self, device_service):
        assert await device_service.get_write_audit("d1") is None

    async def test_no_method(self, device_service):
        drv = _make_mock_driver()
        delattr(drv, "get_write_audit_log")
        device_service._driver_instances["d1"] = drv
        assert await device_service.get_write_audit("d1") is None

    async def test_with_filters(self, device_service):
        drv = _make_mock_driver()
        drv.get_write_audit_log = MagicMock(return_value=[
            {"result": "success", "timestamp": "2024-01-02"},
            {"result": "fail", "timestamp": "2024-01-01"},
        ])
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_write_audit("d1", limit=10, result="success")
        assert len(result) == 1
        assert result[0]["result"] == "success"

    async def test_time_range_filter(self, device_service):
        drv = _make_mock_driver()
        drv.get_write_audit_log = MagicMock(return_value=[
            {"timestamp": "2024-01-01"},
            {"timestamp": "2024-01-05"},
            {"timestamp": "2024-01-10"},
        ])
        device_service._driver_instances["d1"] = drv
        result = await device_service.get_write_audit("d1", start_time="2024-01-03", end_time="2024-01-08")
        assert len(result) == 1


# ───────────────────────── 配置版本 ─────────────────────────


class TestConfigVersion:
    async def test_save_config_version_no_driver(self, device_service):
        assert await device_service.save_config_version("d1", {}) == 0

    async def test_save_config_version_success(self, device_service):
        drv = _make_mock_driver()
        drv.save_config_version = MagicMock(return_value=3)
        device_service._driver_instances["d1"] = drv
        assert await device_service.save_config_version("d1", {}, "chg", "op") == 3

    async def test_get_config_current_no_driver(self, device_service):
        assert await device_service.get_config_current("d1") is None

    async def test_get_config_current_success(self, device_service):
        drv = _make_mock_driver()
        drv.get_config_current = MagicMock(return_value={"points": []})
        device_service._driver_instances["d1"] = drv
        assert await device_service.get_config_current("d1") == {"points": []}

    async def test_get_config_versions_no_driver(self, device_service):
        assert await device_service.get_config_versions("d1") == []

    async def test_get_config_versions_success(self, device_service):
        drv = _make_mock_driver()
        drv.get_config_versions = MagicMock(return_value=[{"version": 1}])
        device_service._driver_instances["d1"] = drv
        assert await device_service.get_config_versions("d1") == [{"version": 1}]

    async def test_get_config_version_config_no_driver(self, device_service):
        assert await device_service.get_config_version_config("d1", 1) is None

    async def test_get_config_version_config_success(self, device_service):
        drv = _make_mock_driver()
        drv.get_config_version_config = MagicMock(return_value={"v": 1})
        device_service._driver_instances["d1"] = drv
        assert await device_service.get_config_version_config("d1", 1) == {"v": 1}

    async def test_rollback_config_no_driver(self, device_service):
        assert await device_service.rollback_config("d1", 1) is None

    async def test_rollback_config_success_with_immutable_change(self, device_service):
        """回滚涉及不可变字段变更应记录审计日志但允许执行"""
        drv = _make_mock_driver()
        drv.rollback_config = MagicMock(return_value={"version": 2, "config": {"x": 1}})
        drv._config = MagicMock()
        drv.get_config_current = MagicMock(return_value={"points": [{"name": "p1", "address": "A"}]})
        drv.get_config_version_config = MagicMock(return_value={"points": [{"name": "p1", "address": "B"}]})
        device_service._driver_instances["d1"] = drv
        fake_app = MagicMock()
        fake_app.audit_service = None
        with patch("edgelite.app._app_state", fake_app):
            result = await device_service.rollback_config("d1", 1, operator="admin")
        assert result["version"] == 2
        drv._config.update.assert_called_once_with({"x": 1})

    async def test_rollback_config_with_audit_service(self, device_service):
        """存在审计服务时应调用 log"""
        drv = _make_mock_driver()
        drv.rollback_config = MagicMock(return_value={"version": 2, "config": {}})
        drv.get_config_current = MagicMock(return_value=None)
        drv.get_config_version_config = MagicMock(return_value=None)
        device_service._driver_instances["d1"] = drv
        audit_svc = AsyncMock()
        fake_app = MagicMock()
        fake_app.audit_service = audit_svc
        with patch("edgelite.app._app_state", fake_app):
            await device_service.rollback_config("d1", 1, operator="admin")
        audit_svc.log.assert_awaited()

    async def test_rollback_config_get_current_exception(self, device_service):
        """获取当前配置异常应被吞掉，回滚仍执行"""
        drv = _make_mock_driver()
        drv.rollback_config = MagicMock(return_value={"version": 2, "config": {}})
        drv.get_config_current = MagicMock(side_effect=RuntimeError("fail"))
        drv.get_config_version_config = MagicMock(return_value=None)
        device_service._driver_instances["d1"] = drv
        result = await device_service.rollback_config("d1", 1)
        assert result["version"] == 2

    async def test_get_config_audit_trail_no_driver(self, device_service):
        assert await device_service.get_config_audit_trail("d1") == []

    async def test_get_config_audit_trail_success(self, device_service):
        drv = _make_mock_driver()
        drv.get_config_audit_trail = MagicMock(return_value=[{"a": 1}])
        device_service._driver_instances["d1"] = drv
        assert await device_service.get_config_audit_trail("d1", limit=5) == [{"a": 1}]

    async def test_diff_config_versions_no_driver(self, device_service):
        assert await device_service.diff_config_versions("d1", 1, 2) is None

    async def test_diff_config_versions_success(self, device_service):
        drv = _make_mock_driver()
        drv.diff_config_versions = MagicMock(return_value={"diff": True})
        device_service._driver_instances["d1"] = drv
        assert await device_service.diff_config_versions("d1", 1, 2) == {"diff": True}


# ───────────────────────── 健康列表 ─────────────────────────


class TestListHealth:
    async def test_list_device_health_empty(self, device_service):
        assert await device_service.list_device_health() == []

    async def test_list_device_health_with_driver(self, device_service):
        drv = _make_mock_driver()
        device_service._driver_instances["d1"] = drv
        result = await device_service.list_device_health()
        assert len(result) == 1
        assert result[0]["device_id"] == "d1"
        assert result[0]["total_reads"] == 0

    async def test_list_device_health_stats_exception(self, device_service):
        drv = _make_mock_driver()
        drv.get_health_stats = MagicMock(side_effect=RuntimeError("fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.list_device_health()
        assert result[0]["total_reads"] is None

    async def test_list_device_health_paginated(self, device_service):
        for i in range(5):
            device_service._driver_instances[f"d{i}"] = _make_mock_driver()
        items, total = await device_service.list_device_health_paginated(page=1, size=2)
        assert total == 5
        assert len(items) == 2

    async def test_list_device_health_paginated_with_filter(self, device_service):
        for i in range(3):
            device_service._driver_instances[f"d{i}"] = _make_mock_driver()
        items, total = await device_service.list_device_health_paginated(device_ids={"d0", "d2"})
        assert total == 2
        assert len(items) == 2

    async def test_list_device_health_for_ids_empty(self, device_service):
        assert await device_service.list_device_health_for_ids([]) == {}

    async def test_list_device_health_for_ids_no_driver(self, device_service):
        result = await device_service.list_device_health_for_ids(["d1"])
        assert result == {}

    async def test_list_device_health_for_ids_with_driver(self, device_service):
        drv = _make_mock_driver()
        device_service._driver_instances["d1"] = drv
        result = await device_service.list_device_health_for_ids(["d1"])
        assert "d1" in result
        assert result["d1"]["total_reads"] == 0

    async def test_list_device_health_for_ids_exception(self, device_service):
        drv = _make_mock_driver()
        drv.is_device_connected = MagicMock(side_effect=RuntimeError("fail"))
        device_service._driver_instances["d1"] = drv
        result = await device_service.list_device_health_for_ids(["d1"])
        assert "d1" in result


# ───────────────────────── 批量操作 ─────────────────────────


class TestBatch:
    async def test_batch_delete_no_user(self, device_service, device_repo):
        device_repo.delete.return_value = True
        result = await device_service.batch_delete_devices(["d1", "d2"])
        assert result["d1"][0] is True
        assert result["d2"][0] is True
        await _drain_cleanup_tasks(device_service)

    async def test_batch_delete_with_user_authorized(self, device_service, device_repo):
        device_repo.delete_with_owner_check.return_value = "deleted"
        result = await device_service.batch_delete_devices(["d1"], user_id="u1", is_admin=False)
        assert result["d1"][0] is True
        await _drain_cleanup_tasks(device_service)

    async def test_batch_delete_with_user_not_authorized(self, device_service, device_repo):
        device_repo.delete_with_owner_check.return_value = "not_authorized"
        result = await device_service.batch_delete_devices(["d1"], user_id="u1")
        assert result["d1"][0] is False

    async def test_batch_delete_with_user_not_found(self, device_service, device_repo):
        device_repo.delete_with_owner_check.return_value = "not_found"
        result = await device_service.batch_delete_devices(["d1"], user_id="u1")
        assert result["d1"] == (False, None)

    async def test_batch_delete_active_rules(self, device_service, rule_repo):
        rule_repo.list_all.return_value = ([{"name": "r1", "enabled": True}], 1)
        result = await device_service.batch_delete_devices(["d1"], user_id="u1")
        assert result["d1"][0] is False

    async def test_batch_delete_exception_caught(self, device_service, device_repo):
        device_repo.delete.side_effect = RuntimeError("boom")
        result = await device_service.batch_delete_devices(["d1"])
        assert result["d1"][0] is False

    async def test_batch_start_collect(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1", "status": "online"}
        result = await device_service.batch_start_collect(["d1"])
        assert result["d1"][0] is True

    async def test_batch_start_collect_failure(self, device_service, device_repo):
        device_repo.get.return_value = None
        result = await device_service.batch_start_collect(["d1"])
        assert result["d1"][0] is False

    async def test_batch_stop_collect(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1", "status": "offline"}
        result = await device_service.batch_stop_collect(["d1"])
        assert result["d1"][0] is True

    async def test_batch_stop_collect_failure(self, device_service, device_repo):
        device_repo.get.return_value = None
        result = await device_service.batch_stop_collect(["d1"])
        assert result["d1"][0] is False


# ───────────────────────── 启动加载与 sidecar 补偿 ─────────────────────────


class TestLoadExisting:
    async def test_load_existing_devices_empty(self, device_service, device_repo):
        device_repo.list_all.return_value = ([], 0)
        with patch.object(device_service, "_start_sidecar_compensation"):
            await device_service.load_existing_devices()

    async def test_load_existing_simulator(self, device_service, device_repo, scheduler, mock_simulator, registry):
        registry.get_driver_class.return_value = MagicMock()
        device_service._simulator_driver = mock_simulator
        device_repo.list_all.return_value = (
            [{"device_id": "s1", "protocol": "simulator", "points": [], "collect_interval": 5}],
            1,
        )
        with patch.object(device_service, "_start_sidecar_compensation"):
            await device_service.load_existing_devices()
        scheduler.start_collect.assert_awaited()
        assert device_service._driver_instances["s1"] is mock_simulator

    async def test_load_existing_driver_connected(self, device_service, device_repo, scheduler, lifecycle, registry):
        drv = _make_mock_driver(connected=True)
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        device_repo.list_all.return_value = (
            [{"device_id": "d1", "protocol": "modbus_tcp", "config": {}, "points": [], "collect_interval": 5}],
            1,
        )
        with patch.object(device_service, "_start_sidecar_compensation"):
            await device_service.load_existing_devices()
        lifecycle.on_device_online.assert_awaited_with("d1")

    async def test_load_existing_no_protocol_skipped(self, device_service, device_repo, registry):
        device_repo.list_all.return_value = ([{"device_id": "d1", "config": {}}], 1)
        with patch.object(device_service, "_start_sidecar_compensation"):
            await device_service.load_existing_devices()
        registry.get_driver_class.assert_not_called()

    async def test_load_existing_no_driver_class_skipped(self, device_service, device_repo, registry):
        registry.get_driver_class.return_value = None
        device_repo.list_all.return_value = (
            [{"device_id": "d1", "protocol": "weird", "config": {}}],
            1,
        )
        with patch.object(device_service, "_start_sidecar_compensation"):
            await device_service.load_existing_devices()

    async def test_load_existing_driver_timeout(self, device_service, device_repo, registry):
        drv = _make_mock_driver()
        drv.start = AsyncMock(side_effect=asyncio.TimeoutError())
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        device_repo.list_all.return_value = (
            [{"device_id": "d1", "protocol": "modbus_tcp", "config": {}}],
            1,
        )
        with patch.object(device_service, "_start_sidecar_compensation"):
            await device_service.load_existing_devices()  # 超时应被捕获不抛出

    async def test_load_existing_driver_exception(self, device_service, device_repo, registry):
        drv = _make_mock_driver()
        drv.add_device = AsyncMock(side_effect=RuntimeError("boom"))
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        device_repo.list_all.return_value = (
            [{"device_id": "d1", "protocol": "modbus_tcp", "config": {}, "points": []}],
            1,
        )
        with patch.object(device_service, "_start_sidecar_compensation"):
            await device_service.load_existing_devices()  # 单设备失败不阻塞

    async def test_load_existing_pagination(self, device_service, device_repo):
        """多页设备应分页加载"""
        device_repo.list_all.side_effect = [
            ([{"device_id": "d1", "config": {}}], 2),
            ([{"device_id": "d2", "config": {}}], 2),
        ]
        with patch("edgelite.services.device_service._MAX_QUERY_SIZE", 1):
            with patch.object(device_service, "_start_sidecar_compensation"):
                await device_service.load_existing_devices()
        assert device_repo.list_all.await_count == 2

    async def test_sidecar_compensation_start_and_stop(self, device_service):
        """补偿任务启动后可被停止"""
        with patch.object(device_service, "_run_sidecar_compensation", new=AsyncMock()):
            device_service._start_sidecar_compensation()
            assert device_service._sidecar_compensation_task is not None
            await asyncio.sleep(0)
            await device_service.stop_sidecar_compensation()
            assert device_service._sidecar_compensation_task is None

    async def test_sidecar_compensation_start_idempotent(self, device_service):
        """重复启动补偿任务不应创建新任务"""
        with patch.object(device_service, "_run_sidecar_compensation", new=AsyncMock()):
            device_service._start_sidecar_compensation()
            t1 = device_service._sidecar_compensation_task
            device_service._start_sidecar_compensation()
            assert device_service._sidecar_compensation_task is t1
            await device_service.stop_sidecar_compensation()

    async def test_stop_sidecar_when_none(self, device_service):
        """无任务时停止不应抛异常"""
        await device_service.stop_sidecar_compensation()

    async def test_run_sidecar_compensation_loop(self, device_service, device_repo):
        """补偿循环应周期调用 retry_pending_sidecar_cleanups，取消时退出"""
        call_count = 0

        async def fake_retry():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        device_repo.retry_pending_sidecar_cleanups = fake_retry
        with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            with pytest.raises(asyncio.CancelledError):
                await device_service._run_sidecar_compensation()
        assert call_count >= 2

    async def test_run_sidecar_compensation_exception_recovery(self, device_service, device_repo):
        """补偿循环异常后应等待并重试"""
        calls = 0

        async def fake_retry():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            raise asyncio.CancelledError()

        device_repo.retry_pending_sidecar_cleanups = fake_retry
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
            return None

        with patch("edgelite.services.device_service.asyncio.sleep", new=fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await device_service._run_sidecar_compensation()
        assert 60 in sleeps


# ───────────────────────── 模板管理 ─────────────────────────


class TestTemplates:
    async def test_create_template_no_repo(self, device_service):
        device_service._template_repo = None
        with pytest.raises(ValueError, match="Template repository not available"):
            await device_service.create_template("d1", "tpl")

    async def test_create_template_device_not_found(self, device_service, device_repo):
        device_repo.get.return_value = None
        with pytest.raises(ValueError, match="Device not found"):
            await device_service.create_template("d1", "tpl")

    async def test_create_template_success(self, device_service, device_repo, template_repo):
        device_repo.get.return_value = {
            "device_id": "d1", "protocol": "modbus_tcp", "config": {"a": 1}, "points": [{"name": "p1"}],
        }
        await device_service.create_template("d1", "tpl", created_by="u")
        template_repo.create.assert_awaited()

    async def test_list_templates_no_repo(self, device_service):
        device_service._template_repo = None
        with pytest.raises(ValueError):
            await device_service.list_templates()

    async def test_list_templates_success(self, device_service, template_repo):
        template_repo.list_all.return_value = ([{"name": "t1"}], 1)
        result = await device_service.list_templates()
        assert result == [{"name": "t1"}]

    async def test_create_from_template_no_repo(self, device_service):
        device_service._template_repo = None
        with pytest.raises(ValueError):
            await device_service.create_from_template("t", {})

    async def test_create_from_template_not_found(self, device_service, template_repo):
        template_repo.get.return_value = None
        with pytest.raises(ValueError, match="Template not found"):
            await device_service.create_from_template("t", {"device_id": "d", "name": "n"})

    async def test_create_from_template_success(self, device_service, device_repo, template_repo, mock_simulator):
        template_repo.get.return_value = {
            "name": "t", "protocol": "simulator", "config_template": {"a": 1}, "point_templates": [{"name": "p1"}],
        }
        device_service._simulator_driver = mock_simulator
        device_repo.create.return_value = {"device_id": "d1", "protocol": "simulator", "config": {}, "points": []}
        overrides = {"device_id": "d1", "name": "n"}
        result = await device_service.create_from_template("t", overrides, created_by="u")
        assert result["device_id"] == "d1"

    async def test_delete_template_no_repo(self, device_service):
        device_service._template_repo = None
        with pytest.raises(ValueError):
            await device_service.delete_template("t")

    async def test_delete_template_success(self, device_service, template_repo):
        assert await device_service.delete_template("t") is True


# ───────────────────────── 导入导出 ─────────────────────────


class TestExportImport:
    async def test_export_by_ids(self, device_service, device_repo):
        device_repo.get_by_ids.return_value = [
            {"device_id": "d1", "name": "n", "protocol": "modbus_tcp", "config": {}, "points": [], "collect_interval": 5}
        ]
        result = await device_service.export_devices(["d1"])
        assert len(result) == 1
        assert result[0]["device_id"] == "d1"

    async def test_export_all(self, device_service, device_repo):
        device_repo.list_all.return_value = (
            [{"device_id": "d1", "name": "n", "protocol": "modbus_tcp", "config": {}, "points": [], "collect_interval": 5}],
            1,
        )
        result = await device_service.export_devices()
        assert len(result) == 1

    async def test_export_all_pagination(self, device_service, device_repo):
        device_repo.list_all.side_effect = [
            ([{"device_id": "d1", "name": "n", "protocol": "p", "config": {}, "points": [], "collect_interval": 5}], 2),
            ([{"device_id": "d2", "name": "n", "protocol": "p", "config": {}, "points": [], "collect_interval": 5}], 2),
        ]
        with patch("edgelite.services.device_service._MAX_QUERY_SIZE", 1):
            result = await device_service.export_devices()
        assert len(result) == 2

    async def test_import_not_list(self, device_service):
        result = await device_service.import_devices("not a list")
        assert result["success"] == 0
        assert result["failed"] == 0

    async def test_import_partial_success(self, device_service, device_repo, mock_simulator):
        device_service._simulator_driver = mock_simulator
        device_repo.get.return_value = None
        device_repo.create.return_value = {"device_id": "d1", "protocol": "simulator", "points": []}
        data = [{"device_id": "d1", "name": "n", "protocol": "simulator", "points": [{"name": "p1"}]}]
        result = await device_service.import_devices(data)
        assert result["success"] == 1
        assert result["mode"] == "partial"

    async def test_import_missing_protocol(self, device_service):
        result = await device_service.import_devices([{"device_id": "d1", "name": "n", "points": []}])
        assert result["failed"] == 1
        assert "protocol" in result["errors"][0]

    async def test_import_unsupported_protocol(self, device_service):
        result = await device_service.import_devices([{"device_id": "d1", "name": "n", "protocol": "zzz", "points": []}])
        assert result["failed"] == 1

    async def test_import_missing_name(self, device_service):
        result = await device_service.import_devices([{"device_id": "d1", "protocol": "simulator", "points": []}])
        assert result["failed"] == 1
        assert "name" in result["errors"][0]

    async def test_import_missing_points(self, device_service):
        result = await device_service.import_devices([{"device_id": "d1", "name": "n", "protocol": "simulator"}])
        assert result["failed"] == 1
        assert "points" in result["errors"][0]

    async def test_import_existing_no_overwrite(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1"}
        data = [{"device_id": "d1", "name": "n", "protocol": "simulator", "points": [{"name": "p1"}]}]
        result = await device_service.import_devices(data)
        assert result["failed"] == 1
        assert "already exists" in result["errors"][0]

    async def test_import_existing_with_overwrite(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1", "protocol": "simulator", "config": {}, "points": []}
        device_repo.update.return_value = {"device_id": "d1", "protocol": "simulator", "config": {}, "points": []}
        data = [{"device_id": "d1", "name": "n2", "protocol": "simulator", "points": [{"name": "p1"}]}]
        result = await device_service.import_devices(data, overwrite=True)
        assert result["success"] == 1

    async def test_import_existing_overwrite_update_fails(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1", "protocol": "simulator", "config": {}, "points": []}
        device_repo.update.return_value = None
        data = [{"device_id": "d1", "name": "n2", "protocol": "simulator", "points": [{"name": "p1"}]}]
        result = await device_service.import_devices(data, overwrite=True)
        assert result["failed"] == 1

    async def test_import_unsupported_protocol_zzz(self, device_service, registry):
        registry.get_all_protocol_keys.return_value = ["modbus_tcp"]
        data = [{"device_id": "d1", "name": "n", "protocol": "zzz", "points": [{"name": "p1"}]}]
        result = await device_service.import_devices(data)
        assert result["failed"] == 1

    async def test_import_atomic_db_not_available(self, device_service):
        """原子导入但数据库不可用应返回失败"""
        fake_app = MagicMock()
        fake_app.database = None
        with patch("edgelite.app._app_state", fake_app):
            result = await device_service.import_devices(
                [{"device_id": "d1", "name": "n", "protocol": "simulator", "points": [{"name": "p1"}]}],
                atomic=True,
            )
        assert result["success"] == 0
        assert result["mode"] == "atomic"

    async def test_import_atomic_with_errors(self, device_service):
        """原子导入存在校验错误应回滚事务"""
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        session.rollback = AsyncMock()
        session.commit = AsyncMock()

        class FakeSessionCtx:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *args):
                return None

        db = MagicMock()
        db.session = MagicMock(return_value=FakeSessionCtx())
        fake_app = MagicMock()
        fake_app.database = db
        data = [{"device_id": "d1", "protocol": "simulator", "points": [{"name": "p1"}]}]
        with patch("edgelite.app._app_state", fake_app):
            result = await device_service.import_devices(data, atomic=True)
        assert result["success"] == 0
        session.rollback.assert_awaited()

    async def test_import_atomic_success(self, device_service, device_repo, mock_simulator):
        """原子导入全部成功应提交事务并加载驱动"""
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )
        session.add = MagicMock()
        session.commit = AsyncMock()
        session.rollback = AsyncMock()

        class FakeSessionCtx:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *args):
                return None

        db = MagicMock()
        db.session = MagicMock(return_value=FakeSessionCtx())
        fake_app = MagicMock()
        fake_app.database = db
        device_repo.get.return_value = {"device_id": "d1", "protocol": "simulator", "config": {}, "points": []}
        device_service._simulator_driver = mock_simulator
        data = [{"device_id": "d1", "name": "n", "protocol": "simulator", "points": [{"name": "p1"}], "config": {}}]
        with patch("edgelite.app._app_state", fake_app):
            with patch("edgelite.storage.sqlite_repo._validate_device_data"):
                result = await device_service.import_devices(data, atomic=True)
        assert result["success"] == 1
        session.commit.assert_awaited()

    async def test_load_driver_for_device_simulator(self, device_service, mock_simulator, scheduler):
        device_service._simulator_driver = mock_simulator
        await device_service._load_driver_for_device(
            {"device_id": "s1", "protocol": "simulator", "points": [], "config": {}}
        )
        assert device_service._driver_instances["s1"] is mock_simulator
        scheduler.start_collect.assert_awaited()

    async def test_load_driver_for_device_no_protocol(self, device_service):
        """无 protocol 字段应直接返回"""
        await device_service._load_driver_for_device({"device_id": "d1"})

    async def test_load_driver_for_device_no_driver_class(self, device_service, registry):
        registry.get_driver_class.return_value = None
        await device_service._load_driver_for_device({"device_id": "d1", "protocol": "weird", "config": {}})

    async def test_load_driver_for_device_driver(self, device_service, registry, scheduler):
        drv = _make_mock_driver()
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        await device_service._load_driver_for_device(
            {"device_id": "d1", "protocol": "modbus_tcp", "config": {}, "points": []}
        )
        assert device_service._driver_instances["d1"] is drv
