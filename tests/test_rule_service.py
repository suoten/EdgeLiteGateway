"""规则管理服务测试 - CRUD/校验/评估/级联清理

覆盖 services/rule_service.py：
- RuleService: create/get/list/update/delete/enable/disable/test/evaluate
- _compare: 操作符比较与类型容错
- _get_create_lock: per-device 锁缓存
- 级联清理: 告警恢复/事件发布/静默清理/evaluator 缓存失效
- 错误路径: 设备不存在/规则上限/规则不存在/import 失败
"""

from __future__ import annotations

import sys

sys.path.insert(0, "src")

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from edgelite.services.rule_service import _MAX_RULES_PER_DEVICE, RuleService

# ── 辅助对象 ─────────────────────────────────────────────────────────────


class FakeAppState:
    """可控的 _app_state 替身。

    使用真实类而非 MagicMock，避免 MagicMock 自动属性导致 hasattr 误判
    （delete_rule 中 hasattr(_app_state, "_repos") 需精确控制）。
    """

    def __init__(self, evaluator=None, event_bus=None, repos=None):
        self.evaluator = evaluator
        self.event_bus = event_bus
        if repos is not None:
            self._repos = repos


def make_evaluator():
    """返回带 async 方法的 evaluator mock。"""
    ev = AsyncMock()
    ev.invalidate_cache = AsyncMock()
    ev.cleanup_duration_tracker = AsyncMock()
    ev._check_conditions = AsyncMock(return_value=True)
    return ev


@pytest.fixture
def rule_repo():
    repo = AsyncMock()
    repo.get = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value={"rule_id": "r1", "name": "rule1"})
    repo.list_all = AsyncMock(return_value=([], 0))
    repo.list_rules_by_ids = AsyncMock(return_value=[])
    repo.update = AsyncMock(return_value=None)
    repo.delete = AsyncMock(return_value=False)
    repo.toggle = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def device_repo():
    repo = AsyncMock()
    repo.get = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def svc(rule_repo, device_repo):
    return RuleService(rule_repo=rule_repo, device_repo=device_repo)


# ── __init__ / _get_create_lock ──────────────────────────────────────────


class TestInit:
    def test_init_creates_empty_locks(self, svc):
        """__init__ 应创建空锁字典与元锁"""
        assert svc._create_locks == {}
        assert isinstance(svc._create_locks_meta, asyncio.Lock)

    async def test_get_create_lock_caches_per_device(self, svc):
        """同一设备的锁应被缓存复用"""
        lock1 = await svc._get_create_lock("dev1")
        lock2 = await svc._get_create_lock("dev1")
        assert lock1 is lock2
        assert "dev1" in svc._create_locks

    async def test_get_create_lock_different_devices(self, svc):
        """不同设备应获得不同锁"""
        lock1 = await svc._get_create_lock("dev1")
        lock2 = await svc._get_create_lock("dev2")
        assert lock1 is not lock2
        assert len(svc._create_locks) == 2


# ── create_rule ──────────────────────────────────────────────────────────


class TestCreateRule:
    async def test_missing_device_id_raises(self, svc):
        """缺少 device_id 应抛 ValueError"""
        with pytest.raises(ValueError, match="device_id"):
            await svc.create_rule({"name": "r"})

    async def test_device_not_found_raises(self, svc, device_repo):
        """设备不存在应抛 ValueError"""
        device_repo.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="Device not found"):
            await svc.create_rule({"device_id": "devX"})

    async def test_rule_limit_reached_raises(self, svc, rule_repo, device_repo):
        """规则数达上限应拒绝创建"""
        device_repo.get = AsyncMock(return_value={"device_id": "dev1"})
        rule_repo.list_all = AsyncMock(return_value=([], _MAX_RULES_PER_DEVICE))
        with pytest.raises(ValueError, match="Rule limit reached"):
            await svc.create_rule({"device_id": "dev1"})

    async def test_rule_limit_boundary_allowed(self, svc, rule_repo, device_repo):
        """count == MAX-1 应允许创建（边界 < MAX）"""
        device_repo.get = AsyncMock(return_value={"device_id": "dev1"})
        rule_repo.list_all = AsyncMock(return_value=([], _MAX_RULES_PER_DEVICE - 1))
        rule_repo.create = AsyncMock(return_value={"rule_id": "r1"})
        result = await svc.create_rule({"device_id": "dev1"})
        assert result["rule_id"] == "r1"

    async def test_success_without_evaluator(self, svc, rule_repo, device_repo):
        """无 evaluator 时正常创建"""
        device_repo.get = AsyncMock(return_value={"device_id": "dev1"})
        rule_repo.create = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.create_rule({"device_id": "dev1"})
        assert result["rule_id"] == "r1"
        rule_repo.create.assert_awaited_once()

    async def test_success_with_evaluator_invalidates(self, svc, rule_repo, device_repo):
        """有 evaluator 时应失效缓存"""
        device_repo.get = AsyncMock(return_value={"device_id": "dev1"})
        rule_repo.create = AsyncMock(return_value={"rule_id": "r1"})
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            await svc.create_rule({"device_id": "dev1"})
        ev.invalidate_cache.assert_awaited_once()

    async def test_create_passes_created_by(self, svc, rule_repo, device_repo):
        """created_by 应透传给 repo.create"""
        device_repo.get = AsyncMock(return_value={"device_id": "dev1"})
        rule_repo.create = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            await svc.create_rule({"device_id": "dev1"}, created_by="alice")
        rule_repo.create.assert_awaited_once_with({"device_id": "dev1"}, created_by="alice")

    async def test_import_error_skipped(self, svc, rule_repo, device_repo):
        """_app_state 不可用时（AttributeError）应跳过缓存失效"""
        device_repo.get = AsyncMock(return_value={"device_id": "dev1"})
        rule_repo.create = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=None):
            result = await svc.create_rule({"device_id": "dev1"})
        assert result["rule_id"] == "r1"

    async def test_list_all_called_with_device_filter(self, svc, rule_repo, device_repo):
        """校验数量时应按 device_id 过滤"""
        device_repo.get = AsyncMock(return_value={"device_id": "dev1"})
        rule_repo.create = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            await svc.create_rule({"device_id": "dev1"})
        rule_repo.list_all.assert_awaited_once_with(page=1, size=1, device_id="dev1")


# ── get_rule / list_rules_by_ids / list_rules ────────────────────────────


class TestReadOperations:
    async def test_get_rule_delegates(self, svc, rule_repo):
        """get_rule 应委托给 repo"""
        rule_repo.get = AsyncMock(return_value={"rule_id": "r1"})
        result = await svc.get_rule("r1")
        assert result == {"rule_id": "r1"}
        rule_repo.get.assert_awaited_once_with("r1")

    async def test_get_rule_returns_none(self, svc, rule_repo):
        """规则不存在返回 None"""
        rule_repo.get = AsyncMock(return_value=None)
        assert await svc.get_rule("nope") is None

    async def test_list_rules_by_ids(self, svc, rule_repo):
        """批量查询应委托给 repo"""
        rule_repo.list_rules_by_ids = AsyncMock(return_value=[{"rule_id": "r1"}, {"rule_id": "r2"}])
        result = await svc.list_rules_by_ids(["r1", "r2"])
        assert len(result) == 2
        rule_repo.list_rules_by_ids.assert_awaited_once_with(["r1", "r2"])

    async def test_list_rules_with_filters(self, svc, rule_repo):
        """list_rules 应透传所有过滤参数"""
        rule_repo.list_all = AsyncMock(return_value=([{"rule_id": "r1"}], 1))
        rules, total = await svc.list_rules(
            page=2,
            size=10,
            device_id="dev1",
            search="temp",
            severity="warning",
            created_by="bob",
        )
        assert total == 1
        assert len(rules) == 1
        rule_repo.list_all.assert_awaited_once_with(2, 10, "dev1", "temp", "warning", "bob")

    async def test_list_rules_defaults(self, svc, rule_repo):
        """list_rules 默认参数"""
        rule_repo.list_all = AsyncMock(return_value=([], 0))
        await svc.list_rules()
        rule_repo.list_all.assert_awaited_once_with(1, 20, None, None, None, None)


# ── update_rule ──────────────────────────────────────────────────────────


class TestUpdateRule:
    async def test_result_none_no_invalidation(self, svc, rule_repo):
        """更新返回 None 时不触发缓存失效"""
        rule_repo.update = AsyncMock(return_value=None)
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.update_rule("r1", {"name": "new"})
        assert result is None
        ev.invalidate_cache.assert_not_awaited()

    async def test_success_with_evaluator_cleanup(self, svc, rule_repo):
        """更新成功应清理 duration_tracker 并失效缓存"""
        rule_repo.update = AsyncMock(return_value={"rule_id": "r1"})
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.update_rule("r1", {"name": "new"})
        assert result["rule_id"] == "r1"
        ev.cleanup_duration_tracker.assert_awaited_once_with("r1")
        ev.invalidate_cache.assert_awaited_once()

    async def test_success_evaluator_none(self, svc, rule_repo):
        """evaluator 为 None 时不报错"""
        rule_repo.update = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.update_rule("r1", {"name": "new"})
        assert result["rule_id"] == "r1"

    async def test_attribute_error_skipped(self, svc, rule_repo):
        """_app_state 不可用（None）应跳过"""
        rule_repo.update = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=None):
            result = await svc.update_rule("r1", {"name": "new"})
        assert result["rule_id"] == "r1"


# ── delete_rule ──────────────────────────────────────────────────────────


class TestDeleteRule:
    async def test_rule_not_found_delete_fails(self, svc, rule_repo):
        """规则不存在且删除失败返回 False"""
        rule_repo.get = AsyncMock(return_value=None)
        rule_repo.delete = AsyncMock(return_value=False)
        assert await svc.delete_rule("r1") is False

    async def test_delete_returns_false_no_cleanup(self, svc, rule_repo):
        """repo.delete 返回 False 时直接返回，不触碰关联数据"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=False)
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            assert await svc.delete_rule("r1") is False
        ev.invalidate_cache.assert_not_awaited()

    async def test_success_no_repos(self, svc, rule_repo):
        """删除成功但无 _repos 时仅清理 evaluator 缓存"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.delete_rule("r1")
        assert result is True
        ev.cleanup_duration_tracker.assert_awaited_once_with("r1")
        ev.invalidate_cache.assert_awaited_once()

    async def test_success_rule_none_dict(self, svc, rule_repo):
        """rule 为 None 但删除成功时正常流程（device_id_for_lock=None）"""
        rule_repo.get = AsyncMock(return_value=None)
        rule_repo.delete = AsyncMock(return_value=True)
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.delete_rule("r1")
        assert result is True

    async def test_success_with_alarm_recovery_and_events(self, svc, rule_repo, device_repo):
        """完整恢复路径：恢复告警 + 解析设备名 + 发布事件"""
        rule_repo.get = AsyncMock(return_value={"name": "rule1", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        device_repo.get = AsyncMock(return_value={"name": "Device1"})
        alarm_repo = AsyncMock()
        alarm_repo.recover_active_by_rule = AsyncMock(
            return_value=[
                {"alarm_id": "a1", "device_id": "dev1", "severity": "warning"},
                {"alarm_id": "a2", "device_id": "dev1", "severity": "critical"},
            ]
        )
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock()
        ev = make_evaluator()
        state = FakeAppState(evaluator=ev, event_bus=event_bus, repos={"alarm": alarm_repo})
        with patch("edgelite.app._app_state", new=state):
            result = await svc.delete_rule("r1")
        assert result is True
        alarm_repo.recover_active_by_rule.assert_awaited_once_with("r1")
        assert event_bus.publish.await_count == 2
        ev.cleanup_duration_tracker.assert_awaited_once_with("r1")

    async def test_recovery_empty_no_events(self, svc, rule_repo):
        """恢复列表为空时不发布事件"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        alarm_repo = AsyncMock()
        alarm_repo.recover_active_by_rule = AsyncMock(return_value=[])
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock()
        state = FakeAppState(evaluator=None, event_bus=event_bus, repos={"alarm": alarm_repo})
        with patch("edgelite.app._app_state", new=state):
            await svc.delete_rule("r1")
        event_bus.publish.assert_not_awaited()

    async def test_recovery_no_event_bus(self, svc, rule_repo):
        """无 event_bus 时恢复告警但不发布事件"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        alarm_repo = AsyncMock()
        alarm_repo.recover_active_by_rule = AsyncMock(
            return_value=[{"alarm_id": "a1", "device_id": "dev1", "severity": "info"}]
        )
        state = FakeAppState(evaluator=None, event_bus=None, repos={"alarm": alarm_repo})
        with patch("edgelite.app._app_state", new=state):
            result = await svc.delete_rule("r1")
        assert result is True

    async def test_recovery_empty_device_id(self, svc, rule_repo, device_repo):
        """恢复告警无 device_id 时跳过设备名查询"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        alarm_repo = AsyncMock()
        alarm_repo.recover_active_by_rule = AsyncMock(
            return_value=[{"alarm_id": "a1", "device_id": "", "severity": "info"}]
        )
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock()
        state = FakeAppState(evaluator=None, event_bus=event_bus, repos={"alarm": alarm_repo})
        with patch("edgelite.app._app_state", new=state):
            await svc.delete_rule("r1")
        device_repo.get.assert_not_awaited()
        event_bus.publish.assert_awaited_once()

    async def test_device_lookup_failure(self, svc, rule_repo, device_repo):
        """设备名查询失败时应捕获异常，device_name 保持空"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        device_repo.get = AsyncMock(side_effect=RuntimeError("db down"))
        alarm_repo = AsyncMock()
        alarm_repo.recover_active_by_rule = AsyncMock(
            return_value=[{"alarm_id": "a1", "device_id": "dev1", "severity": "info"}]
        )
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock()
        state = FakeAppState(evaluator=None, event_bus=event_bus, repos={"alarm": alarm_repo})
        with patch("edgelite.app._app_state", new=state):
            result = await svc.delete_rule("r1")
        assert result is True
        event_bus.publish.assert_awaited_once()

    async def test_event_bus_publish_failure(self, svc, rule_repo):
        """事件发布失败应记录但不中断"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        alarm_repo = AsyncMock()
        alarm_repo.recover_active_by_rule = AsyncMock(
            return_value=[{"alarm_id": "a1", "device_id": "dev1", "severity": "info"}]
        )
        event_bus = AsyncMock()
        event_bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))
        state = FakeAppState(evaluator=None, event_bus=event_bus, repos={"alarm": alarm_repo})
        with patch("edgelite.app._app_state", new=state):
            result = await svc.delete_rule("r1")
        assert result is True

    async def test_recover_raises_exception(self, svc, rule_repo):
        """recover_active_by_rule 抛异常应被外层 except Exception 捕获"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        alarm_repo = AsyncMock()
        alarm_repo.recover_active_by_rule = AsyncMock(side_effect=RuntimeError("recover failed"))
        ev = make_evaluator()
        state = FakeAppState(evaluator=ev, event_bus=None, repos={"alarm": alarm_repo})
        with patch("edgelite.app._app_state", new=state):
            result = await svc.delete_rule("r1")
        assert result is True
        # evaluator 缓存仍应被清理（在独立 try 块中）
        ev.invalidate_cache.assert_awaited_once()

    async def test_silence_cleanup_success(self, svc, rule_repo):
        """静默清理成功路径（注入 fake alarm_silence 模块）"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        fake_mgr = MagicMock()
        fake_mgr.delete_silences_by_rule = MagicMock(return_value=2)
        fake_module = MagicMock()
        fake_module.get_alarm_silence_manager = MagicMock(return_value=fake_mgr)
        with patch.dict(sys.modules, {"edgelite.services.alarm_silence": fake_module}):
            with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
                result = await svc.delete_rule("r1")
        assert result is True
        fake_mgr.delete_silences_by_rule.assert_called_once_with("r1")

    async def test_silence_cleanup_failure(self, svc, rule_repo):
        """静默清理抛异常应被捕获不中断"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        fake_mgr = MagicMock()
        fake_mgr.delete_silences_by_rule = MagicMock(side_effect=RuntimeError("fail"))
        fake_module = MagicMock()
        fake_module.get_alarm_silence_manager = MagicMock(return_value=fake_mgr)
        with patch.dict(sys.modules, {"edgelite.services.alarm_silence": fake_module}):
            with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
                result = await svc.delete_rule("r1")
        assert result is True

    async def test_lock_cleanup_on_delete(self, svc, rule_repo):
        """删除成功后应清理对应设备的 per-device 锁"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        # 预先创建锁
        await svc._get_create_lock("dev1")
        assert "dev1" in svc._create_locks
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            await svc.delete_rule("r1")
        assert "dev1" not in svc._create_locks

    async def test_attribute_error_on_app_state(self, svc, rule_repo):
        """_app_state 为 None 时告警清理块应捕获 AttributeError"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        with patch("edgelite.app._app_state", new=None):
            result = await svc.delete_rule("r1")
        assert result is True

    async def test_repos_none_triggers_attribute_error(self, svc, rule_repo):
        """_repos 存在但为 None 时，None.get() 抛 AttributeError 被捕获（覆盖 line 159）"""
        from types import SimpleNamespace

        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        # _repos=None → hasattr True，但 None.get("alarm") 抛 AttributeError
        state = SimpleNamespace(evaluator=None, event_bus=None, _repos=None)
        with patch("edgelite.app._app_state", new=state):
            result = await svc.delete_rule("r1")
        assert result is True

    async def test_silence_cleanup_zero_count(self, svc, rule_repo):
        """静默清理返回 0 时不记录 info 日志（覆盖 falsy 分支）"""
        rule_repo.get = AsyncMock(return_value={"name": "r", "device_id": "dev1"})
        rule_repo.delete = AsyncMock(return_value=True)
        fake_mgr = MagicMock()
        fake_mgr.delete_silences_by_rule = MagicMock(return_value=0)
        fake_module = MagicMock()
        fake_module.get_alarm_silence_manager = MagicMock(return_value=fake_mgr)
        with patch.dict(sys.modules, {"edgelite.services.alarm_silence": fake_module}):
            with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
                result = await svc.delete_rule("r1")
        assert result is True
        fake_mgr.delete_silences_by_rule.assert_called_once_with("r1")


# ── enable_rule / disable_rule ───────────────────────────────────────────


class TestToggleRule:
    async def test_enable_result_none(self, svc, rule_repo):
        """enable 返回 None 时不触发缓存失效"""
        rule_repo.toggle = AsyncMock(return_value=None)
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.enable_rule("r1")
        assert result is None
        ev.invalidate_cache.assert_not_awaited()

    async def test_enable_success_with_evaluator(self, svc, rule_repo):
        """enable 成功应清理 tracker 并失效缓存"""
        rule_repo.toggle = AsyncMock(return_value={"rule_id": "r1", "enabled": True})
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.enable_rule("r1")
        assert result["enabled"] is True
        rule_repo.toggle.assert_awaited_once_with("r1", True)
        ev.cleanup_duration_tracker.assert_awaited_once_with("r1")
        ev.invalidate_cache.assert_awaited_once()

    async def test_enable_evaluator_none(self, svc, rule_repo):
        """evaluator 为 None 时 enable 不报错"""
        rule_repo.toggle = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.enable_rule("r1")
        assert result["rule_id"] == "r1"

    async def test_enable_attribute_error_skipped(self, svc, rule_repo):
        """_app_state 为 None 时 enable 应跳过"""
        rule_repo.toggle = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=None):
            result = await svc.enable_rule("r1")
        assert result["rule_id"] == "r1"

    async def test_disable_result_none(self, svc, rule_repo):
        """disable 返回 None 时不触发缓存失效"""
        rule_repo.toggle = AsyncMock(return_value=None)
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.disable_rule("r1")
        assert result is None
        ev.invalidate_cache.assert_not_awaited()

    async def test_disable_success_with_evaluator(self, svc, rule_repo):
        """disable 成功应清理 tracker 并失效缓存"""
        rule_repo.toggle = AsyncMock(return_value={"rule_id": "r1", "enabled": False})
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.disable_rule("r1")
        assert result["enabled"] is False
        rule_repo.toggle.assert_awaited_once_with("r1", False)
        ev.cleanup_duration_tracker.assert_awaited_once_with("r1")
        ev.invalidate_cache.assert_awaited_once()

    async def test_disable_evaluator_none(self, svc, rule_repo):
        """evaluator 为 None 时 disable 不报错"""
        rule_repo.toggle = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.disable_rule("r1")
        assert result["rule_id"] == "r1"

    async def test_disable_attribute_error_skipped(self, svc, rule_repo):
        """_app_state 为 None 时 disable 应跳过缓存失效（覆盖 except 分支）"""
        rule_repo.toggle = AsyncMock(return_value={"rule_id": "r1"})
        with patch("edgelite.app._app_state", new=None):
            result = await svc.disable_rule("r1")
        assert result["rule_id"] == "r1"


# ── test_rule ────────────────────────────────────────────────────────────


class TestTestRule:
    async def test_rule_not_found_raises(self, svc, rule_repo):
        """规则不存在应抛 ValueError"""
        rule_repo.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="Rule not found"):
            await svc.test_rule("r1", {"temp": 80})

    async def test_no_conditions(self, svc, rule_repo):
        """无条件时返回 all_matched=False"""
        rule_repo.get = AsyncMock(return_value={"rule_id": "r1", "conditions": [], "logic": "AND"})
        result = await svc.test_rule("r1", {"temp": 80})
        assert result["all_matched"] is False
        assert result["condition_results"] == []
        assert result["logic"] == "AND"

    async def test_no_conditions_none_field(self, svc, rule_repo):
        """conditions 为 None 时应视为空"""
        rule_repo.get = AsyncMock(return_value={"rule_id": "r1", "conditions": None, "logic": "OR"})
        result = await svc.test_rule("r1", {"temp": 80})
        assert result["all_matched"] is False

    async def test_condition_missing_fields(self, svc, rule_repo):
        """条件缺少 point/operator/threshold 时 matched=False"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [{"point": "temp"}],  # 缺 operator/threshold
                "logic": "AND",
            }
        )
        result = await svc.test_rule("r1", {"temp": 80})
        assert result["condition_results"][0]["matched"] is False
        assert result["condition_results"][0]["actual_value"] is None

    async def test_point_value_none(self, svc, rule_repo):
        """点位值缺失时 matched=False"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [{"point": "temp", "operator": ">", "threshold": 50}],
                "logic": "AND",
            }
        )
        result = await svc.test_rule("r1", {"other": 10})
        assert result["condition_results"][0]["matched"] is False
        assert result["condition_results"][0]["actual_value"] is None

    async def test_and_logic_all_match(self, svc, rule_repo):
        """AND 逻辑全部匹配为 True"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [
                    {"point": "temp", "operator": ">", "threshold": 50},
                    {"point": "hum", "operator": "<", "threshold": 80},
                ],
                "logic": "AND",
            }
        )
        result = await svc.test_rule("r1", {"temp": 80, "hum": 60})
        assert result["all_matched"] is True

    async def test_and_logic_partial(self, svc, rule_repo):
        """AND 逻辑部分匹配为 False"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [
                    {"point": "temp", "operator": ">", "threshold": 50},
                    {"point": "hum", "operator": "<", "threshold": 80},
                ],
                "logic": "AND",
            }
        )
        result = await svc.test_rule("r1", {"temp": 80, "hum": 90})
        assert result["all_matched"] is False

    async def test_or_logic_any_match(self, svc, rule_repo):
        """OR 逻辑任一匹配为 True"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [
                    {"point": "temp", "operator": ">", "threshold": 50},
                    {"point": "hum", "operator": "<", "threshold": 80},
                ],
                "logic": "OR",
            }
        )
        result = await svc.test_rule("r1", {"temp": 80, "hum": 90})
        assert result["all_matched"] is True

    async def test_or_logic_none_match(self, svc, rule_repo):
        """OR 逻辑全不匹配为 False"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [
                    {"point": "temp", "operator": ">", "threshold": 50},
                ],
                "logic": "OR",
            }
        )
        result = await svc.test_rule("r1", {"temp": 10})
        assert result["all_matched"] is False

    async def test_not_logic_some_match(self, svc, rule_repo):
        """NOT 逻辑：非全部匹配 -> True"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [
                    {"point": "temp", "operator": ">", "threshold": 50},
                    {"point": "hum", "operator": "<", "threshold": 80},
                ],
                "logic": "NOT",
            }
        )
        result = await svc.test_rule("r1", {"temp": 80, "hum": 90})
        assert result["all_matched"] is True

    async def test_not_logic_all_match(self, svc, rule_repo):
        """NOT 逻辑：全部匹配 -> False"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [
                    {"point": "temp", "operator": ">", "threshold": 50},
                    {"point": "hum", "operator": "<", "threshold": 80},
                ],
                "logic": "NOT",
            }
        )
        result = await svc.test_rule("r1", {"temp": 80, "hum": 60})
        assert result["all_matched"] is False

    async def test_zero_value_is_valid(self, svc, rule_repo):
        """值为 0 时应正常比较（不应被 is None 误判）"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [{"point": "temp", "operator": ">=", "threshold": 0}],
                "logic": "AND",
            }
        )
        result = await svc.test_rule("r1", {"temp": 0})
        assert result["all_matched"] is True
        assert result["condition_results"][0]["actual_value"] == 0


# ── evaluate_rule ────────────────────────────────────────────────────────


class TestEvaluateRule:
    async def test_rule_not_found_raises(self, svc, rule_repo):
        """规则不存在应抛 ValueError"""
        rule_repo.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="Rule not found"):
            await svc.evaluate_rule("r1")

    async def test_no_conditions(self, svc, rule_repo):
        """无条件时返回 all_matched=False"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [],
                "logic": "AND",
                "priority": 5,
            }
        )
        result = await svc.evaluate_rule("r1", {"temp": 80})
        assert result["all_matched"] is False
        assert result["priority"] == 5
        assert result["condition_results"] == []

    async def test_with_evaluator_and_point_values(self, svc, rule_repo):
        """有 evaluator 且有点位值时使用 _check_conditions"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [{"point": "temp", "operator": ">", "threshold": 50}],
                "logic": "AND",
                "priority": 3,
                "duration_seconds": 10,
                "device_id": "dev1",
            }
        )
        ev = make_evaluator()
        ev._check_conditions = AsyncMock(return_value=True)
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.evaluate_rule("r1", {"temp": 80})
        assert result["all_matched"] is True
        assert result["priority"] == 3
        assert result["duration_seconds"] == 10
        # evaluator 路径不返回 condition_results
        assert "condition_results" not in result
        ev._check_conditions.assert_awaited_once()

    async def test_evaluator_but_no_point_values_fallback(self, svc, rule_repo):
        """有 evaluator 但无点位值时回退到简单评估"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [{"point": "temp", "operator": ">", "threshold": 50}],
                "logic": "AND",
                "priority": 1,
                "device_id": "dev1",
            }
        )
        ev = make_evaluator()
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=ev)):
            result = await svc.evaluate_rule("r1", None)
        assert result["all_matched"] is False
        assert "condition_results" in result
        ev._check_conditions.assert_not_awaited()

    async def test_fallback_and_logic(self, svc, rule_repo):
        """回退评估 AND 逻辑"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [{"point": "temp", "operator": ">", "threshold": 50}],
                "logic": "AND",
                "priority": 0,
            }
        )
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.evaluate_rule("r1", {"temp": 80})
        assert result["all_matched"] is True

    async def test_fallback_or_logic(self, svc, rule_repo):
        """回退评估 OR 逻辑"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [
                    {"point": "temp", "operator": ">", "threshold": 50},
                    {"point": "hum", "operator": "<", "threshold": 80},
                ],
                "logic": "OR",
                "priority": 0,
            }
        )
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.evaluate_rule("r1", {"temp": 10, "hum": 60})
        assert result["all_matched"] is True

    async def test_fallback_not_logic(self, svc, rule_repo):
        """回退评估 NOT 逻辑（非全部匹配 -> True）"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [
                    {"point": "temp", "operator": ">", "threshold": 50},
                    {"point": "hum", "operator": "<", "threshold": 80},
                ],
                "logic": "NOT",
                "priority": 0,
            }
        )
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.evaluate_rule("r1", {"temp": 80, "hum": 90})
        assert result["all_matched"] is True

    async def test_fallback_condition_missing_fields(self, svc, rule_repo):
        """回退评估条件字段缺失时 matched=False"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [{"point": "temp"}],
                "logic": "AND",
                "priority": 0,
            }
        )
        with patch("edgelite.app._app_state", new=FakeAppState(evaluator=None)):
            result = await svc.evaluate_rule("r1", {"temp": 80})
        assert result["all_matched"] is False
        assert result["condition_results"][0]["matched"] is False

    async def test_evaluate_import_error_fallback(self, svc, rule_repo):
        """edgelite.app import fail -> fallback (covers except ImportError)"""
        rule_repo.get = AsyncMock(
            return_value={
                "rule_id": "r1",
                "conditions": [{"point": "temp", "operator": ">", "threshold": 50}],
                "logic": "AND",
                "priority": 0,
            }
        )
        with patch.dict(sys.modules, {"edgelite.app": None}):
            result = await svc.evaluate_rule("r1", {"temp": 80})
        assert result["all_matched"] is True
        assert "condition_results" in result


# ── _compare ────────────────────────────────────────────────────────────


class TestCompare:
    def test_gt(self):
        assert RuleService._compare(10, ">", 5) is True
        assert RuleService._compare(5, ">", 10) is False

    def test_gte(self):
        assert RuleService._compare(5, ">=", 5) is True
        assert RuleService._compare(4, ">=", 5) is False

    def test_lt(self):
        assert RuleService._compare(3, "<", 5) is True
        assert RuleService._compare(5, "<", 5) is False

    def test_lte(self):
        assert RuleService._compare(5, "<=", 5) is True
        assert RuleService._compare(6, "<=", 5) is False

    def test_eq_single(self):
        assert RuleService._compare(5, "=", 5) is True
        assert RuleService._compare(5, "=", 6) is False

    def test_eq_double(self):
        assert RuleService._compare(5, "==", 5) is True
        assert RuleService._compare(5, "==", 6) is False

    def test_ne(self):
        assert RuleService._compare(5, "!=", 6) is True
        assert RuleService._compare(5, "!=", 5) is False

    def test_unknown_operator(self):
        """未知操作符返回 False"""
        assert RuleService._compare(5, "UNKNOWN", 5) is False

    def test_string_values_converted(self):
        """字符串数值应能转换为 float 后比较"""
        assert RuleService._compare("10", ">", "5") is True
        assert RuleService._compare("5.5", ">=", 5) is True

    def test_invalid_value_type(self):
        """无法转换为 float 的值返回 False"""
        assert RuleService._compare("abc", ">", 5) is False

    def test_invalid_threshold_type(self):
        """无法转换的 threshold 返回 False"""
        assert RuleService._compare(10, ">", "abc") is False

    def test_none_value(self):
        """None 值返回 False"""
        assert RuleService._compare(None, ">", 5) is False

    def test_float_equality_precision(self):
        """浮点相等使用 1e-9 容差"""
        assert RuleService._compare(0.1 + 0.2, "=", 0.3) is True
