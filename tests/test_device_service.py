"""设备管理业务逻辑测试 - services/device_service.py (基础部分)

覆盖 DeviceService 构造、访问器、创建、查询、更新、删除、读取、写入、发现、采集启停。
完整健康/配置版本/批量/加载/模板/导入导出测试见 test_device_service_ext.py。
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.base import DriverCapabilities, DriverHealthStats, PointValue  # noqa: E402
from edgelite.services.device_service import DeviceService, _current_write_user_var  # noqa: E402

# ───────────────────────── 辅助 ─────────────────────────


async def _drain_cleanup_tasks(svc: DeviceService) -> None:
    """等待 service 中所有后台清理任务完成，确保 _cleanup 内部逻辑被执行到。"""
    tasks = list(svc._cleanup_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _make_mock_driver(
    *,
    connected: bool = True,
    has_stats: bool = True,
    write_capable: bool = True,
    discover: bool = False,
) -> MagicMock:
    """构造一个具备常用异步方法的 mock 驱动实例。"""
    drv = MagicMock()
    drv.start = AsyncMock(return_value=None)
    drv.stop = AsyncMock(return_value=None)
    drv.add_device = AsyncMock(return_value=None)
    drv.remove_device = AsyncMock(return_value=None)
    drv.read_points = AsyncMock(return_value={})
    drv.write_point = AsyncMock(return_value=True)
    drv.is_device_connected = MagicMock(return_value=connected)
    drv.capabilities = DriverCapabilities(read=True, write=write_capable, discover=discover)
    if has_stats:
        drv.get_health_stats = MagicMock(return_value=DriverHealthStats(device_id="d1"))
    else:
        drv.get_health_stats = MagicMock(side_effect=RuntimeError("boom"))
    return drv


def _make_mock_driver_class(driver_instance: MagicMock | None = None) -> MagicMock:
    """构造一个调用后返回指定 mock 驱动实例的"驱动类"。"""
    inst = driver_instance if driver_instance is not None else _make_mock_driver()
    return MagicMock(return_value=inst)


# ───────────────────────── 夹具 ─────────────────────────


@pytest.fixture
def device_repo():
    repo = AsyncMock()
    repo.create = AsyncMock(return_value={"device_id": "dev1", "protocol": "modbus_tcp", "config": {}, "points": []})
    repo.get = AsyncMock(return_value=None)
    repo.get_by_ids = AsyncMock(return_value=[])
    repo.list_all = AsyncMock(return_value=([], 0))
    repo.list_device_ids_by_owner = AsyncMock(return_value=[])
    repo.list_devices_by_ids = AsyncMock(return_value=[])
    repo.get_status_counts = AsyncMock(return_value={"online": 0, "offline": 0})
    repo.update = AsyncMock(return_value=None)
    repo.update_status = AsyncMock(return_value=None)
    repo.delete = AsyncMock(return_value=True)
    repo.delete_with_owner_check = AsyncMock(return_value="deleted")
    repo.cleanup_sidecar_data = AsyncMock(return_value=None)
    repo.retry_pending_sidecar_cleanups = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def rule_repo():
    repo = AsyncMock()
    repo.list_all = AsyncMock(return_value=([], 0))
    return repo


@pytest.fixture
def scheduler():
    sch = AsyncMock()
    sch.start_collect = AsyncMock(return_value=None)
    sch.stop_collect = AsyncMock(return_value=None)
    sch.get_last_values = AsyncMock(return_value={})
    return sch


@pytest.fixture
def lifecycle():
    lc = AsyncMock()
    lc.on_device_online = AsyncMock(return_value=None)
    lc.on_device_offline = AsyncMock(return_value=None)
    lc.remove_device = AsyncMock(return_value=None)
    return lc


@pytest.fixture
def template_repo():
    repo = AsyncMock()
    repo.create = AsyncMock(return_value={"name": "tpl"})
    repo.list_all = AsyncMock(return_value=([], 0))
    repo.get = AsyncMock(return_value=None)
    repo.delete = AsyncMock(return_value=True)
    return repo


@pytest.fixture
def registry():
    reg = MagicMock()
    reg.get_driver_class = MagicMock(return_value=None)
    reg.get_all_protocol_keys = MagicMock(return_value=["modbus_tcp"])
    return reg


@pytest.fixture
def device_service(device_repo, rule_repo, scheduler, lifecycle, template_repo, registry):
    """构造一个所有外部依赖均被 mock 的 DeviceService 实例。"""
    with patch("edgelite.services.device_service.get_driver_registry", return_value=registry):
        svc = DeviceService(
            device_repo=device_repo,
            rule_repo=rule_repo,
            scheduler=scheduler,
            lifecycle=lifecycle,
            template_repo=template_repo,
        )
    return svc


@pytest.fixture
def mock_simulator():
    """预构造的 simulator 驱动 mock，用于注入 _simulator_driver 避免真实实例化。"""
    drv = MagicMock()
    drv.start = AsyncMock(return_value=None)
    drv.add_device = AsyncMock(return_value=None)
    drv.remove_device = AsyncMock(return_value=None)
    drv.read_points = AsyncMock(return_value={})
    drv.write_point = AsyncMock(return_value=True)
    drv.is_device_connected = MagicMock(return_value=True)
    drv.capabilities = DriverCapabilities(read=True, write=True)
    drv.get_health_stats = MagicMock(return_value=DriverHealthStats(device_id="d1"))
    return drv


# ───────────────────────── 构造与访问器 ─────────────────────────


class TestAccessors:
    """私有属性访问器测试"""

    async def test_get_driver_instance_returns_existing(self, device_service):
        """已存在的驱动实例应被返回"""
        drv = MagicMock()
        device_service._driver_instances["d1"] = drv
        assert await device_service.get_driver_instance("d1") is drv

    async def test_get_driver_instance_missing_returns_none(self, device_service):
        """不存在的设备返回 None"""
        assert await device_service.get_driver_instance("nope") is None

    async def test_remove_driver_instance_returns_and_removes(self, device_service):
        """移除访问器应弹出并返回旧实例"""
        drv = MagicMock()
        device_service._driver_instances["d1"] = drv
        assert await device_service.remove_driver_instance("d1") is drv
        assert "d1" not in device_service._driver_instances

    async def test_remove_driver_instance_missing_returns_none(self, device_service):
        assert await device_service.remove_driver_instance("nope") is None

    async def test_get_lifecycle_returns_injected(self, device_service, lifecycle):
        assert await device_service.get_lifecycle() is lifecycle

    async def test_get_repo_returns_injected(self, device_service, device_repo):
        assert await device_service.get_repo() is device_repo

    async def test_get_simulator_driver_creates_singleton(self, device_service):
        """_get_simulator_driver 应创建并缓存模拟器驱动实例"""
        with patch("edgelite.services.device_service.SimulatorDriver") as SimCls:
            inst = MagicMock()
            inst.start = AsyncMock(return_value=None)
            SimCls.return_value = inst
            r1 = await device_service._get_simulator_driver()
            r2 = await device_service._get_simulator_driver()
            assert r1 is r2
            inst.start.assert_awaited_once_with({})


# ───────────────────────── 创建设备 ─────────────────────────


class TestCreateDevice:
    """create_device 各路径测试"""

    async def test_missing_protocol_raises(self, device_service):
        """缺少 protocol 字段应抛 ValueError"""
        with pytest.raises(ValueError, match="Missing required field: protocol"):
            await device_service.create_device({"name": "n"})

    async def test_unsupported_protocol_raises(self, device_service, registry):
        """未注册且非 simulator 的协议应抛 ValueError"""
        registry.get_driver_class.return_value = None
        with pytest.raises(ValueError, match="Unsupported protocol"):
            await device_service.create_device({"protocol": "unknown_proto"})

    async def test_create_simulator_device(self, device_service, device_repo, scheduler, lifecycle, mock_simulator):
        """simulator 协议创建设备应走模拟器路径并置 online"""
        device_service._simulator_driver = mock_simulator
        device_repo.create.return_value = {"device_id": "sim1", "protocol": "simulator", "points": []}
        data = {"protocol": "simulator", "points": []}
        result = await device_service.create_device(data, created_by="u1")
        assert result["device_id"] == "sim1"
        mock_simulator.add_device.assert_awaited()
        lifecycle.on_device_online.assert_awaited_with("sim1")
        device_repo.update_status.assert_any_call("sim1", "online")
        scheduler.start_collect.assert_awaited()
        assert device_service._driver_instances["sim1"] is mock_simulator

    async def test_create_driver_connected_online(self, device_service, device_repo, scheduler, lifecycle, registry):
        """普通驱动连接成功应置 online"""
        drv = _make_mock_driver(connected=True)
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        device_repo.create.return_value = {"device_id": "d2", "protocol": "modbus_tcp", "config": {}, "points": []}
        result = await device_service.create_device({"protocol": "modbus_tcp", "config": {}, "points": []})
        assert result["device_id"] == "d2"
        lifecycle.on_device_online.assert_awaited_with("d2")
        device_repo.update_status.assert_any_call("d2", "online")
        assert device_service._driver_instances["d2"] is drv

    async def test_create_driver_disconnected_offline(
        self, device_service, device_repo, scheduler, lifecycle, registry
    ):
        """普通驱动未连接应置 offline"""
        drv = _make_mock_driver(connected=False)
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        device_repo.create.return_value = {"device_id": "d3", "protocol": "modbus_tcp", "config": {}, "points": []}
        await device_service.create_device({"protocol": "modbus_tcp", "config": {}, "points": []})
        device_repo.update_status.assert_any_call("d3", "offline")
        lifecycle.on_device_online.assert_not_awaited()

    async def test_create_driver_start_failure_rolls_back(self, device_service, device_repo, scheduler, registry):
        """普通驱动 start 抛异常应回滚 DB 记录并停止 driver"""
        drv = _make_mock_driver()
        drv.start = AsyncMock(side_effect=RuntimeError("start fail"))
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        device_repo.create.return_value = {"device_id": "d4", "protocol": "modbus_tcp", "config": {}, "points": []}
        with pytest.raises(ValueError, match="Device driver start failed"):
            await device_service.create_device({"protocol": "modbus_tcp", "config": {}, "points": []})
        device_repo.delete.assert_awaited_with("d4")
        scheduler.stop_collect.assert_awaited()
        drv.stop.assert_awaited()
        assert "d4" not in device_service._driver_instances

    async def test_create_simulator_add_failure_rolls_back(
        self, device_service, device_repo, scheduler, mock_simulator
    ):
        """simulator add_device 失败应回滚 DB 记录（驱动实例尚未存入 _driver_instances）"""
        mock_simulator.add_device = AsyncMock(side_effect=RuntimeError("add fail"))
        device_service._simulator_driver = mock_simulator
        device_repo.create.return_value = {"device_id": "sim2", "protocol": "simulator", "points": []}
        with pytest.raises(ValueError, match="Device driver start failed"):
            await device_service.create_device({"protocol": "simulator", "points": []})
        device_repo.delete.assert_awaited_with("sim2")
        scheduler.stop_collect.assert_awaited()

    async def test_create_simulator_later_failure_rolls_back(
        self, device_service, device_repo, scheduler, mock_simulator
    ):
        """simulator 后续步骤(start_collect)失败应回滚并调用 remove_device 移除设备映射"""
        scheduler.start_collect = AsyncMock(side_effect=RuntimeError("collect fail"))
        device_service._simulator_driver = mock_simulator
        device_repo.create.return_value = {"device_id": "sim3", "protocol": "simulator", "points": []}
        with pytest.raises(ValueError, match="Device driver start failed"):
            await device_service.create_device({"protocol": "simulator", "points": []})
        device_repo.delete.assert_awaited_with("sim3")
        mock_simulator.remove_device.assert_called_with("sim3")
        assert "sim3" not in device_service._driver_instances


# ───────────────────────── 查询类方法 ─────────────────────────


class TestQueries:
    async def test_get_device(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1"}
        assert await device_service.get_device("d1") == {"device_id": "d1"}

    async def test_list_devices(self, device_service, device_repo):
        device_repo.list_all.return_value = ([{"device_id": "d1"}], 1)
        items, total = await device_service.list_devices(page=2, size=10, status="online", created_by="u")
        assert items == [{"device_id": "d1"}]
        assert total == 1
        device_repo.list_all.assert_awaited()

    async def test_list_device_ids_by_owner(self, device_service, device_repo):
        device_repo.list_device_ids_by_owner.return_value = ["d1", "d2"]
        assert await device_service.list_device_ids_by_owner("u") == ["d1", "d2"]

    async def test_list_devices_by_ids(self, device_service, device_repo):
        device_repo.list_devices_by_ids.return_value = [{"device_id": "d1"}]
        assert await device_service.list_devices_by_ids(["d1"]) == [{"device_id": "d1"}]

    async def test_get_status_counts(self, device_service, device_repo):
        device_repo.get_status_counts.return_value = {"online": 3}
        assert await device_service.get_status_counts(["d1"]) == {"online": 3}


# ───────────────────────── 更新与重载 ─────────────────────────


class TestUpdateDevice:
    async def test_update_device_not_found_returns_none(self, device_service, device_repo):
        device_repo.update.return_value = None
        assert await device_service.update_device("d1", {"name": "n"}) is None

    async def test_update_no_config_change(self, device_service, device_repo):
        """config/points 未变化时不触发重载"""
        device_repo.get.return_value = {"device_id": "d1", "config": {"a": 1}, "points": []}
        device_repo.update.return_value = {"device_id": "d1", "config": {"a": 1}, "points": []}
        result = await device_service.update_device("d1", {"name": "new"})
        assert result["device_id"] == "d1"

    async def test_update_config_change_triggers_reload(self, device_service, device_repo, registry, scheduler):
        """config 变化且有驱动实例时应触发重载"""
        old = {"device_id": "d1", "protocol": "modbus_tcp", "config": {"a": 1}, "points": []}
        new = {"device_id": "d1", "protocol": "modbus_tcp", "config": {"a": 2}, "points": []}
        device_repo.get.side_effect = [old, new, new]
        device_repo.update.return_value = new
        drv = _make_mock_driver()
        device_service._driver_instances["d1"] = drv
        registry.get_driver_class.return_value = _make_mock_driver_class(_make_mock_driver())
        await device_service.update_device("d1", {"config": {"a": 2}})
        scheduler.stop_collect.assert_awaited()

    async def test_reload_failure_rolls_back_to_old(self, device_service, device_repo, registry):
        """重载失败应回滚到旧配置并重启旧驱动"""
        old = {"device_id": "d1", "protocol": "modbus_tcp", "config": {"a": 1}, "points": []}
        new = {"device_id": "d1", "protocol": "modbus_tcp", "config": {"a": 2}, "points": []}
        device_repo.get.side_effect = [old, new, old, old]
        device_repo.update.return_value = new
        drv = _make_mock_driver()
        device_service._driver_instances["d1"] = drv
        bad_drv = _make_mock_driver()
        bad_drv.start = AsyncMock(side_effect=RuntimeError("reload fail"))
        registry.get_driver_class.return_value = _make_mock_driver_class(bad_drv)
        await device_service.update_device("d1", {"config": {"a": 2}})
        device_repo.update.assert_any_call("d1", {"config": {"a": 1}, "points": []})

    async def test_reload_new_device_none_returns(self, device_service, device_repo, registry):
        """重载时新设备记录为 None 应直接返回"""
        old = {"device_id": "d1", "protocol": "modbus_tcp", "config": {"a": 1}, "points": []}
        device_repo.get.side_effect = [old, None]
        device_repo.update.return_value = {
            "device_id": "d1",
            "protocol": "modbus_tcp",
            "config": {"a": 2},
            "points": [],
        }
        drv = _make_mock_driver()
        device_service._driver_instances["d1"] = drv
        await device_service._reload_driver_for_device("d1", old)


# ───────────────────────── 删除 ─────────────────────────


class TestDeleteDevice:
    async def test_delete_blocked_by_active_rules(self, device_service, rule_repo):
        """存在启用规则时应返回失败"""
        rule_repo.list_all.return_value = ([{"name": "r1", "enabled": True}], 1)
        ok, err = await device_service.delete_device("d1")
        assert ok is False
        assert "r1" in err

    async def test_delete_db_failure(self, device_service, device_repo):
        """DB 删除失败应返回失败"""
        device_repo.delete.return_value = False
        ok, err = await device_service.delete_device("d1")
        assert ok is False
        assert err is not None

    async def test_delete_success_schedules_cleanup(self, device_service, device_repo):
        """成功删除应调度后台清理"""
        ok, err = await device_service.delete_device("d1")
        assert ok is True
        assert err is None
        await _drain_cleanup_tasks(device_service)

    async def test_cleanup_removes_matching_driver(self, device_service, device_repo):
        """清理任务应移除与 expected_driver 相同实例的驱动"""
        drv = MagicMock()
        drv.stop = AsyncMock(return_value=None)
        device_service._driver_instances["d1"] = drv
        await device_service.delete_device("d1")
        await _drain_cleanup_tasks(device_service)
        assert "d1" not in device_service._driver_instances

    async def test_cleanup_skips_recreated_driver(self, device_service, device_repo):
        """清理时若 driver 实例已被替换为新实例应跳过清理"""
        old_drv = MagicMock()
        new_drv = MagicMock()
        device_service._driver_instances["d1"] = old_drv
        await device_service.delete_device("d1")  # 记录 old_drv 为 expected
        device_service._driver_instances["d1"] = new_drv  # 模拟删除后用相同 id 创建新设备
        await _drain_cleanup_tasks(device_service)
        assert device_service._driver_instances.get("d1") is new_drv

    async def test_cleanup_simulator_driver(self, device_service, device_repo):
        """清理 simulator 驱动应调用 remove_device 而非 stop"""
        from edgelite.drivers.simulator import SimulatorDriver

        with patch("edgelite.services.device_service.SimulatorDriver", SimulatorDriver):
            drv = MagicMock(spec=SimulatorDriver)
            drv.remove_device = AsyncMock(return_value=None)
            device_service._driver_instances["d1"] = drv
            await device_service.delete_device("d1")
            await _drain_cleanup_tasks(device_service)
            drv.remove_device.assert_awaited_with("d1")

    async def test_schedule_cleanup_handles_runtime_error(self, device_service):
        """无运行事件循环时 _schedule_cleanup 应吞掉 RuntimeError"""
        with patch("asyncio.get_running_loop", side_effect=RuntimeError("no loop")):
            device_service._schedule_cleanup("d1", None)  # 不应抛异常


# ───────────────────────── read_points ─────────────────────────


class TestReadPoints:
    async def test_read_points_from_cache(self, device_service, scheduler):
        """调度器缓存命中应包装为 PointValue 返回"""
        scheduler.get_last_values.return_value = {"p1": 42}
        result = await device_service.read_points("d1")
        assert "p1" in result
        assert isinstance(result["p1"], PointValue)
        assert result["p1"].source == "cache"

    async def test_read_points_cache_pointvalue_passthrough(self, device_service, scheduler):
        """缓存中已是 PointValue 应原样返回"""
        pv = PointValue(value=1, source="device")
        scheduler.get_last_values.return_value = {"p1": pv}
        result = await device_service.read_points("d1")
        assert result["p1"] is pv

    async def test_read_points_no_driver_returns_empty(self, device_service, scheduler):
        """缓存空且无驱动应返回空 dict"""
        scheduler.get_last_values.return_value = {}
        assert await device_service.read_points("d1") == {}

    async def test_read_points_device_missing_returns_empty(self, device_service, scheduler, device_repo):
        """缓存空、有驱动但设备记录缺失应返回空"""
        scheduler.get_last_values.return_value = {}
        device_service._driver_instances["d1"] = _make_mock_driver()
        device_repo.get.return_value = None
        assert await device_service.read_points("d1") == {}

    async def test_read_points_driver_realtime(self, device_service, scheduler, device_repo):
        """缓存空时走驱动实时读取"""
        scheduler.get_last_values.return_value = {}
        drv = _make_mock_driver()
        drv.read_points = AsyncMock(return_value={"p1": 10})
        device_service._driver_instances["d1"] = drv
        device_repo.get.return_value = {"device_id": "d1", "points": [{"name": "p1"}]}
        result = await device_service.read_points("d1")
        assert isinstance(result["p1"], PointValue)
        assert result["p1"].source == "device"

    async def test_read_points_driver_exception_returns_empty(self, device_service, scheduler, device_repo):
        """驱动读取异常应返回空而非抛出"""
        scheduler.get_last_values.return_value = {}
        drv = _make_mock_driver()
        drv.read_points = AsyncMock(side_effect=RuntimeError("read fail"))
        device_service._driver_instances["d1"] = drv
        device_repo.get.return_value = {"device_id": "d1", "points": [{"name": "p1"}]}
        assert await device_service.read_points("d1") == {}


# ───────────────────────── write_point ─────────────────────────


class TestWritePoint:
    async def test_write_no_driver_returns_false(self, device_service):
        assert await device_service.write_point("d1", "p1", 1) is False

    async def test_write_capability_not_supported_raises(self, device_service):
        drv = _make_mock_driver(write_capable=False)
        device_service._driver_instances["d1"] = drv
        with pytest.raises(ValueError, match="ERR_DEVICE_CAPABILITY_NOT_SUPPORTED"):
            await device_service.write_point("d1", "p1", 1)

    async def test_write_with_user_sets_context(self, device_service):
        drv = _make_mock_driver()
        drv.set_user_role = AsyncMock(return_value=None)
        device_service._driver_instances["d1"] = drv
        ok = await device_service.write_point("d1", "p1", 1, user={"username": "alice", "role": "operator"})
        assert ok is True
        drv.set_user_role.assert_awaited_with("operator")
        assert _current_write_user_var.get() == "alice"
        assert drv._current_write_user == "alice"

    async def test_write_with_sync_set_user_role(self, device_service):
        """set_user_role 为同步方法时也应被调用"""
        drv = _make_mock_driver()
        drv.set_user_role = MagicMock(return_value=None)
        device_service._driver_instances["d1"] = drv
        await device_service.write_point("d1", "p1", 1, user={"username": "bob", "role": "admin"})
        drv.set_user_role.assert_called_with("admin")

    async def test_write_set_user_role_failure_non_fatal(self, device_service):
        """set_user_role 抛异常不应中断写入"""
        drv = _make_mock_driver()
        drv.set_user_role = AsyncMock(side_effect=RuntimeError("role fail"))
        device_service._driver_instances["d1"] = drv
        ok = await device_service.write_point("d1", "p1", 1, user={"username": "x", "role": "op"})
        assert ok is True


# ───────────────────────── discover / simulator ─────────────────────────


class TestDiscoverAndSimulator:
    async def test_discover_unsupported_protocol(self, device_service, registry):
        registry.get_driver_class.return_value = None
        with pytest.raises(ValueError, match="Unsupported protocol for discovery"):
            await device_service.discover_devices("unknown", {})

    async def test_discover_no_discover_support(self, device_service, registry):
        drv = _make_mock_driver(discover=False)
        delattr(drv, "discover_devices")
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        with pytest.raises(ValueError, match="does not support device discovery"):
            await device_service.discover_devices("modbus_tcp", {})

    async def test_discover_success(self, device_service, registry):
        drv = _make_mock_driver()
        drv.discover_devices = AsyncMock(return_value=[{"id": "x"}])
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        result = await device_service.discover_devices("modbus_tcp", {})
        assert result == [{"id": "x"}]
        drv.stop.assert_awaited()

    async def test_discover_stop_failure_swallowed(self, device_service, registry):
        drv = _make_mock_driver()
        drv.discover_devices = AsyncMock(return_value=[])
        drv.stop = AsyncMock(side_effect=RuntimeError("stop fail"))
        registry.get_driver_class.return_value = _make_mock_driver_class(drv)
        assert await device_service.discover_devices("modbus_tcp", {}) == []

    async def test_create_simulator_sets_protocol_and_config(self, device_service, mock_simulator):
        """create_simulator 应强制 protocol=simulator 并补默认 config"""
        device_service._simulator_driver = mock_simulator
        with patch.object(device_service, "create_device", new=AsyncMock(return_value={"device_id": "s1"})) as cd:
            await device_service.create_simulator({"device_id": "s1", "name": "n"}, created_by="u")
            args = cd.call_args
            assert args.args[0]["protocol"] == "simulator"
            assert args.args[0]["config"] == {"timeout": 5.0}


# ───────────────────────── 采集启停 ─────────────────────────


class TestCollect:
    async def test_start_collect_device_not_found(self, device_service, device_repo):
        device_repo.get.return_value = None
        with pytest.raises(ValueError, match="Device not found"):
            await device_service.start_collect("d1")

    async def test_start_collect_already_online(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1", "status": "online"}
        assert await device_service.start_collect("d1") is True

    async def test_start_collect_no_driver(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1", "status": "offline"}
        with pytest.raises(ValueError, match="Device driver not found"):
            await device_service.start_collect("d1")

    async def test_start_collect_success(self, device_service, device_repo, scheduler, lifecycle):
        device_repo.get.return_value = {"device_id": "d1", "status": "offline", "points": [], "collect_interval": 5}
        device_service._driver_instances["d1"] = _make_mock_driver()
        assert await device_service.start_collect("d1") is True
        scheduler.start_collect.assert_awaited()
        lifecycle.on_device_online.assert_awaited_with("d1")
        device_repo.update_status.assert_awaited_with("d1", "online")

    async def test_stop_collect_device_not_found(self, device_service, device_repo):
        device_repo.get.return_value = None
        with pytest.raises(ValueError, match="Device not found"):
            await device_service.stop_collect("d1")

    async def test_stop_collect_already_offline(self, device_service, device_repo):
        device_repo.get.return_value = {"device_id": "d1", "status": "offline"}
        assert await device_service.stop_collect("d1") is True

    async def test_stop_collect_success(self, device_service, device_repo, scheduler, lifecycle):
        device_repo.get.return_value = {"device_id": "d1", "status": "online"}
        assert await device_service.stop_collect("d1") is True
        scheduler.stop_collect.assert_awaited_with("d1")
        lifecycle.on_device_offline.assert_awaited_with("d1")
        device_repo.update_status.assert_awaited_with("d1", "offline")
