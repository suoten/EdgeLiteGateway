"""告警服务扩展测试 - 覆盖 alarm_service.py 未覆盖行

补充 test_alarm_service.py：名称解析/事件分发/告警触发/恢复/确认/升级/
统计摘要/趋势查询/数据清理/公共 API（ack/clear/delete/trigger）。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.engine.event_bus import AlarmEvent
from edgelite.services.alarm_service import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    AlarmService,
    AlarmSuppressionRule,
)

@pytest.fixture
def alarm_repo():
    """Fake AlarmRepo"""
    repo = AsyncMock()
    repo.get = AsyncMock(return_value=None)
    repo.create = AsyncMock(return_value=None)
    repo.create_with_id = AsyncMock(return_value=None)
    repo.update_severity = AsyncMock()
    repo.count_by_status_and_severity = AsyncMock(return_value={})
    repo.list_all = AsyncMock(return_value=([], 0))
    repo.query_trend_data = AsyncMock(return_value={"top_devices": []})
    repo.get_top_alarms = AsyncMock(return_value={})
    repo.cleanup_old_alarms = AsyncMock(return_value=0)
    repo.delete = AsyncMock(return_value=True)
    repo.ack = AsyncMock(return_value=None)
    repo.recover = AsyncMock(return_value=None)
    repo.get_alarm_history = AsyncMock(return_value=[])
    return repo

@pytest.fixture
def notification_manager():
    """Fake NotificationManager"""
    mgr = AsyncMock()
    mgr.send_notification = AsyncMock(return_value={"email": True})
    mgr.test_channel = AsyncMock(return_value=(True, "ok"))
    mgr.list_channels = MagicMock(return_value=[])
    mgr.set_channel_enabled = MagicMock(return_value=True)
    return mgr

@pytest.fixture
def event_bus():
    """Fake EventBus"""
    bus = AsyncMock()
    bus.register_handler = MagicMock()
    bus.publish = AsyncMock()
    return bus

@pytest.fixture
def alarm_svc(alarm_repo, notification_manager):
    """AlarmService 实例（无 EventBus）"""
    return AlarmService(alarm_repo=alarm_repo, event_bus=None, notification_manager=notification_manager)

@pytest.fixture
def alarm_svc_with_bus(alarm_repo, notification_manager, event_bus):
    """带 EventBus 的 AlarmService 实例"""
    return AlarmService(alarm_repo=alarm_repo, event_bus=event_bus, notification_manager=notification_manager)

def _make_event(
    alarm_id="a1", action="firing", rule_id="r1", device_id="d1",
    severity=SEVERITY_INFO, rule_name="Rule", device_name="Device",
):
    """构造 AlarmEvent"""
    return AlarmEvent(
        alarm_id=alarm_id, rule_id=rule_id, rule_name=rule_name,
        device_id=device_id, device_name=device_name, severity=severity,
        action=action, trigger_value={}, rule_type="threshold",
    )

def _alarm(alarm_id="a1", status="firing", severity=SEVERITY_INFO, **kw):
    """构造告警字典（减少重复）"""
    d = {"alarm_id": alarm_id, "rule_id": "r1", "device_id": "d1", "severity": severity, "status": status}
    d.update(kw)
    return d

class TestResolveNames:
    async def test_no_resolver_returns_ids(self, alarm_svc):
        """无 resolver 时回退为 id"""
        rn, dn = await alarm_svc._resolve_names("r1", "d1")
        assert rn == "r1"
        assert dn == "d1"

    async def test_resolver_success(self, alarm_repo, notification_manager):
        """resolver 成功返回解析名称"""

        async def _resolve(rule_id, device_id):
            if rule_id:
                return ("MyRule", "")
            return ("", "MyDevice")

        resolver = MagicMock()
        resolver.resolve = _resolve
        svc = AlarmService(alarm_repo, None, notification_manager, name_resolver=resolver)
        rn, dn = await svc._resolve_names("r1", "d1")
        assert rn == "MyRule"
        assert dn == "MyDevice"

    async def test_resolver_empty_falls_back(self, alarm_repo, notification_manager):

        async def _resolve(rule_id, device_id):
            return ("", "")

        resolver = MagicMock()
        resolver.resolve = _resolve
        svc = AlarmService(alarm_repo, None, notification_manager, name_resolver=resolver)
        rn, dn = await svc._resolve_names("r1", "d1")
        assert rn == "r1"
        assert dn == "d1"

    async def test_resolver_exception_falls_back(self, alarm_repo, notification_manager):
        resolver = MagicMock()
        resolver.resolve = AsyncMock(side_effect=RuntimeError("db down"))
        svc = AlarmService(alarm_repo, None, notification_manager, name_resolver=resolver)
        rn, dn = await svc._resolve_names("r1", "d1")
        assert rn == "r1"
        assert dn == "d1"

    async def test_resolver_without_resolve_attr(self, alarm_repo, notification_manager):
        resolver = MagicMock(spec=[])
        svc = AlarmService(alarm_repo, None, notification_manager, name_resolver=resolver)
        rn, dn = await svc._resolve_names("r1", "d1")
        assert rn == "r1"
        assert dn == "d1"

class TestStartStop:
    async def test_start_with_event_bus(self, alarm_svc_with_bus):
        await alarm_svc_with_bus.start()
        bus = alarm_svc_with_bus._event_bus
        bus.register_handler.assert_called_once_with(
            "AlarmEvent", alarm_svc_with_bus._handle_alarm_event
        )
        assert alarm_svc_with_bus._cleanup_task is not None
        await alarm_svc_with_bus.stop()

    async def test_start_without_event_bus(self, alarm_svc):
        await alarm_svc.start()
        assert alarm_svc._cleanup_task is not None
        await alarm_svc.stop()

    async def test_stop_cancels_escalation_tasks(self, alarm_svc):

        async def _long():
            await asyncio.sleep(100)

        t1 = asyncio.create_task(_long())
        t2 = asyncio.create_task(_long())
        alarm_svc._escalation_tasks["a1"] = t1
        alarm_svc._escalation_tasks["a2"] = t2
        alarm_svc._cleanup_task = None
        await alarm_svc.stop()
        assert (t1.cancelled() or t1.done()) is True
        assert len(alarm_svc._escalation_tasks) == 0

    async def test_stop_cancels_cleanup_task(self, alarm_svc):
        await alarm_svc.start()
        cleanup = alarm_svc._cleanup_task
        await alarm_svc.stop()
        assert (cleanup.cancelled() or cleanup.done()) is True

class TestHandleAlarmEventDispatch:
    async def test_dispatch_firing(self, alarm_svc):
        event = _make_event("a-fire", action="firing")
        alarm_svc.handle_alarm_event = AsyncMock()
        await alarm_svc._handle_alarm_event(event)
        alarm_svc.handle_alarm_event.assert_called_once_with(event)

    async def test_dispatch_recovered(self, alarm_svc):
        event = _make_event("a-rec", action="recovered")
        alarm_svc._handle_recovery = AsyncMock()
        await alarm_svc._handle_alarm_event(event)
        alarm_svc._handle_recovery.assert_called_once_with(event)

    async def test_dispatch_acknowledged(self, alarm_svc):
        event = _make_event("a-ack", action="acknowledged")
        alarm_svc._handle_acknowledgment = AsyncMock()
        await alarm_svc._handle_alarm_event(event)
        alarm_svc._handle_acknowledgment.assert_called_once_with(event)

    async def test_dispatch_escalated(self, alarm_svc):
        event = _make_event("a-esc", action="escalated")
        alarm_svc._handle_escalation = AsyncMock()
        await alarm_svc._handle_alarm_event(event)
        alarm_svc._handle_escalation.assert_called_once_with(event)

    async def test_dispatch_unknown_action(self, alarm_svc):
        """未知动作不应抛异常"""
        event = _make_event("a-unknown", action="unknown")
        await alarm_svc._handle_alarm_event(event)

    async def test_dispatch_exception_swallowed(self, alarm_svc):
        """处理异常应被捕获不传播"""
        event = _make_event("a-err", action="firing")
        alarm_svc.handle_alarm_event = AsyncMock(side_effect=RuntimeError("boom"))
        await alarm_svc._handle_alarm_event(event)

class TestGetAlarmLock:
    async def test_creates_and_reuses_lock(self, alarm_svc):
        """同一 alarm_id 应返回同一把锁"""
        l1 = await alarm_svc._get_alarm_lock("a1")
        l2 = await alarm_svc._get_alarm_lock("a1")
        assert l1 is l2

    async def test_different_ids_different_locks(self, alarm_svc):
        """不同 alarm_id 应返回不同锁"""
        l1 = await alarm_svc._get_alarm_lock("a1")
        l2 = await alarm_svc._get_alarm_lock("a2")
        assert l1 is not l2

class TestHandleAlarmEvent:
    async def test_duplicate_skipped(self, alarm_svc, alarm_repo):
        """同一 alarm_id 第二次应被跳过"""
        alarm_repo.get = AsyncMock(return_value=_alarm())
        event = _make_event("a-dup", action="firing")
        await alarm_svc.handle_alarm_event(event)
        assert alarm_svc._stats.total_count == 1
        await alarm_svc.handle_alarm_event(event)
        assert alarm_svc._stats.total_count == 1

    async def test_suppressed_alarm(self, alarm_svc, notification_manager):
        """被抑制规则匹配的告警应跳过且清理 _handled_alarm_ids"""
        alarm_svc.add_suppression_rule(AlarmSuppressionRule(rule_id="rs", name="sup", device_ids=["d1"]))
        event = _make_event("a-sup", action="firing", device_id="d1")
        await alarm_svc.handle_alarm_event(event)
        assert alarm_svc._stats.total_count == 0
        notification_manager.send_notification.assert_not_called()
        assert "a-sup" not in alarm_svc._handled_alarm_ids

    async def test_silenced_alarm(self, alarm_svc, notification_manager):
        """被静默规则拦截的告警应跳过"""
        fake_module = ModuleType("edgelite.services.alarm_silence")
        fake_mgr = MagicMock()
        fake_mgr.is_silenced = MagicMock(return_value=True)
        fake_module.get_alarm_silence_manager = lambda: fake_mgr
        with patch.dict(sys.modules, {"edgelite.services.alarm_silence": fake_module}):
            event = _make_event("a-silenced", action="firing")
            await alarm_svc.handle_alarm_event(event)
        assert alarm_svc._stats.total_count == 0
        notification_manager.send_notification.assert_not_called()
        assert "a-silenced" not in alarm_svc._handled_alarm_ids

    async def test_silence_check_failure_continues(self, alarm_svc, alarm_repo):
        """静默检查失败应不阻断告警处理"""
        fake_module = ModuleType("edgelite.services.alarm_silence")
        fake_mgr = MagicMock()
        fake_mgr.is_silenced = MagicMock(side_effect=RuntimeError("db"))
        fake_module.get_alarm_silence_manager = lambda: fake_mgr
        alarm_repo.get = AsyncMock(return_value=_alarm())
        with patch.dict(sys.modules, {"edgelite.services.alarm_silence": fake_module}):
            event = _make_event("a-silfail", action="firing")
            await alarm_svc.handle_alarm_event(event)
        assert alarm_svc._stats.total_count == 1

    async def test_alarm_in_db(self, alarm_svc, alarm_repo, notification_manager):
        """告警已在 DB 中时应直接发送通知"""
        alarm_repo.get = AsyncMock(return_value=_alarm())
        event = _make_event("a-indb", action="firing")
        await alarm_svc.handle_alarm_event(event)
        notification_manager.send_notification.assert_called_once()
        assert alarm_svc._stats.total_count == 1
        assert alarm_svc._stats.firing_count == 1

    async def test_create_from_event_success(self, alarm_svc, alarm_repo):
        """告警不在 DB 时应从事件创建"""
        alarm_repo.get = AsyncMock(return_value=None)
        alarm_repo.create_with_id = AsyncMock(return_value=_alarm())
        event = _make_event("a-create", action="firing")
        await alarm_svc.handle_alarm_event(event)
        alarm_repo.create_with_id.assert_called_once()
        assert alarm_svc._stats.total_count == 1

    async def test_create_fails_retry_get_success(self, alarm_svc, alarm_repo):
        """create_with_id 失败后应重试 get"""
        alarm_repo.get = AsyncMock(side_effect=[None, _alarm()])
        alarm_repo.create_with_id = AsyncMock(side_effect=RuntimeError("exists"))
        event = _make_event("a-retry", action="firing")
        await alarm_svc.handle_alarm_event(event)
        assert alarm_svc._stats.total_count == 1

    async def test_create_and_get_both_fail(self, alarm_svc, alarm_repo):
        """create 和 retry get 均失败时应提前返回"""
        alarm_repo.get = AsyncMock(return_value=None)
        alarm_repo.create_with_id = AsyncMock(side_effect=RuntimeError("exists"))
        event = _make_event("a-bothfail", action="firing")
        await alarm_svc.handle_alarm_event(event)
        assert alarm_svc._stats.total_count == 0

    async def test_notification_failure_logs(self, alarm_svc, alarm_repo, notification_manager):
        """通知部分渠道失败应记录但不阻断"""
        alarm_repo.get = AsyncMock(return_value=_alarm())
        notification_manager.send_notification = AsyncMock(return_value={"email": True, "webhook": False})
        event = _make_event("a-notifyfail", action="firing")
        await alarm_svc.handle_alarm_event(event)
        assert alarm_svc._stats.total_count == 1

    async def test_stats_mtbf_and_first_alarm_time(self, alarm_svc, alarm_repo):
        """连续告警应计算 MTBF 间隔并设置 _first_alarm_time"""
        alarm_repo.get = AsyncMock(return_value=_alarm())
        alarm_svc._last_fire_time = datetime.now(UTC) - timedelta(seconds=30)
        event = _make_event("a-mtbf", action="firing")
        await alarm_svc.handle_alarm_event(event)
        assert len(alarm_svc._stats.failure_intervals) == 1
        assert alarm_svc._first_alarm_time is not None

    async def test_failure_intervals_trimmed_to_100(self, alarm_svc, alarm_repo):
        """故障间隔列表应限制在 100 条"""
        alarm_repo.get = AsyncMock(return_value=_alarm())
        alarm_svc._stats.failure_intervals = [1.0] * 105
        alarm_svc._last_fire_time = datetime.now(UTC) - timedelta(seconds=1)
        event = _make_event("a-trim", action="firing")
        await alarm_svc.handle_alarm_event(event)
        assert len(alarm_svc._stats.failure_intervals) == 100

class TestHandleRecovery:
    async def test_full_recovery_with_severity_restore(self, alarm_svc, alarm_repo, notification_manager):
        """恢复时应还原原始严重度、计算时长、发送通知、更新统计"""
        alarm_svc._original_severities["a1"] = SEVERITY_MINOR
        alarm_svc._alarm_start_times["a1"] = datetime.now(UTC) - timedelta(seconds=60)
        alarm_svc._stats.firing_count = 1
        alarm_repo.get = AsyncMock(return_value=_alarm(status=None))
        alarm_repo.update_severity = AsyncMock()
        event = _make_event("a1", action="recovered")
        await alarm_svc._handle_recovery(event)
        alarm_repo.update_severity.assert_called_once_with("a1", SEVERITY_MINOR)
        notification_manager.send_notification.assert_called_once()
        assert alarm_svc._stats.recovered_count == 1
        assert alarm_svc._stats.firing_count == 0
        assert len(alarm_svc._stats.recovery_times) == 1
        assert "a1" not in alarm_svc._handled_alarm_ids
        assert "a1" not in alarm_svc._alarm_locks

    async def test_recovery_severity_update_fails(self, alarm_svc, alarm_repo):
        """还原严重度失败应不阻断恢复流程"""
        alarm_svc._original_severities["a1"] = SEVERITY_MINOR
        alarm_repo.update_severity = AsyncMock(side_effect=RuntimeError("db"))
        alarm_repo.get = AsyncMock(return_value=None)
        event = _make_event("a1", action="recovered")
        await alarm_svc._handle_recovery(event)
        assert alarm_svc._stats.recovered_count == 1

    async def test_recovery_no_start_time(self, alarm_svc, alarm_repo):
        """无 start_time 时时长为 0"""
        alarm_repo.get = AsyncMock(return_value=None)
        event = _make_event("a1", action="recovered")
        await alarm_svc._handle_recovery(event)
        assert alarm_svc._stats.recovered_count == 1
        assert len(alarm_svc._stats.recovery_times) == 0

    async def test_recovery_no_alarm_in_db(self, alarm_svc, alarm_repo, notification_manager):
        """DB 中无告警时不应发送通知"""
        alarm_repo.get = AsyncMock(return_value=None)
        event = _make_event("a-noalarm", action="recovered")
        await alarm_svc._handle_recovery(event)
        notification_manager.send_notification.assert_not_called()
        assert alarm_svc._stats.recovered_count == 1

    async def test_recovery_trims_recovery_times(self, alarm_svc, alarm_repo):
        """恢复时间列表应限制在 100 条"""
        alarm_svc._alarm_start_times["a1"] = datetime.now(UTC) - timedelta(seconds=1)
        alarm_svc._stats.recovery_times = [1.0] * 105
        alarm_repo.get = AsyncMock(return_value=None)
        event = _make_event("a1", action="recovered")
        await alarm_svc._handle_recovery(event)
        assert len(alarm_svc._stats.recovery_times) == 100

class TestHandleAcknowledgment:
    async def test_with_alarm(self, alarm_svc, alarm_repo, notification_manager):
        """有告警记录时应发送确认通知"""
        alarm_repo.get = AsyncMock(return_value=_alarm(status=None, acknowledged_by="user1"))
        event = _make_event("a1", action="acknowledged")
        await alarm_svc._handle_acknowledgment(event)
        notification_manager.send_notification.assert_called_once()
        assert alarm_svc._stats.acknowledged_count == 1

    async def test_without_alarm(self, alarm_svc, alarm_repo, notification_manager):
        """无告警记录时不应发送通知"""
        alarm_repo.get = AsyncMock(return_value=None)
        event = _make_event("a-noack", action="acknowledged")
        await alarm_svc._handle_acknowledgment(event)
        notification_manager.send_notification.assert_not_called()
        assert alarm_svc._stats.acknowledged_count == 1

class TestHandleEscalationEvent:
    async def test_with_alarm(self, alarm_svc, alarm_repo, notification_manager):
        """有告警记录时应发送升级通知"""
        alarm_repo.get = AsyncMock(return_value=_alarm(status=None))
        event = _make_event("a1", action="escalated")
        await alarm_svc._handle_escalation(event)
        notification_manager.send_notification.assert_called_once()
        assert alarm_svc._stats.escalated_count == 1

    async def test_without_alarm(self, alarm_svc, alarm_repo, notification_manager):
        """无告警记录时不应发送通知但仍更新统计"""
        alarm_repo.get = AsyncMock(return_value=None)
        event = _make_event("a-noesc", action="escalated")
        await alarm_svc._handle_escalation(event)
        notification_manager.send_notification.assert_not_called()
        assert alarm_svc._stats.escalated_count == 1

class TestEscalationScheduling:
    async def test_schedule_no_config_returns_early(self, alarm_svc):
        """无升级配置（如 INFO）时应直接返回"""
        await alarm_svc._schedule_escalation("a1", SEVERITY_INFO, {"alarm_id": "a1"})
        assert "a1" not in alarm_svc._escalation_tasks

    async def test_schedule_creates_and_cancels_old(self, alarm_svc):
        """应创建新任务并取消旧任务"""
        alarm_svc.configure_escalation(SEVERITY_CRITICAL, threshold_seconds=300)

        async def _long():
            await asyncio.sleep(100)

        old = asyncio.create_task(_long())
        alarm_svc._escalation_tasks["a1"] = old
        await alarm_svc._schedule_escalation("a1", SEVERITY_CRITICAL, {"alarm_id": "a1"})
        assert (old.cancelled() or old.done()) is True
        assert "a1" in alarm_svc._escalation_tasks
        for t in list(alarm_svc._escalation_tasks.values()):
            t.cancel()
        alarm_svc._escalation_tasks.clear()

    async def test_do_escalate_alarm_not_firing(self, alarm_svc, alarm_repo):
        """告警已恢复时 _do_escalate 应跳过升级"""
        alarm_svc.configure_escalation(SEVERITY_CRITICAL, threshold_seconds=0)
        alarm_repo.get = AsyncMock(return_value={"alarm_id": "a1", "status": "recovered"})
        alarm_repo.update_severity = AsyncMock()
        await alarm_svc._schedule_escalation("a1", SEVERITY_CRITICAL, _alarm(status=None))
        await asyncio.sleep(0.2)
        alarm_repo.update_severity.assert_not_called()
        await alarm_svc.stop()

    async def test_do_escalate_alarm_not_in_db(self, alarm_svc, alarm_repo):
        """告警不在 DB 时 _do_escalate 应跳过升级"""
        alarm_svc.configure_escalation(SEVERITY_CRITICAL, threshold_seconds=0)
        alarm_repo.get = AsyncMock(return_value=None)
        alarm_repo.update_severity = AsyncMock()
        await alarm_svc._schedule_escalation("a1", SEVERITY_CRITICAL, _alarm(status=None))
        await asyncio.sleep(0.2)
        alarm_repo.update_severity.assert_not_called()
        await alarm_svc.stop()

    async def test_do_escalate_fires(self, alarm_svc, alarm_repo, notification_manager):
        """告警仍 firing 时 _do_escalate 应执行升级"""
        alarm_svc.configure_escalation(SEVERITY_CRITICAL, threshold_seconds=0)
        alarm_repo.get = AsyncMock(return_value=_alarm())
        alarm_repo.update_severity = AsyncMock()
        await alarm_svc._schedule_escalation("a1", SEVERITY_CRITICAL, _alarm(status=None))
        await asyncio.sleep(0.3)
        alarm_repo.update_severity.assert_called_once_with("a1", SEVERITY_CRITICAL)
        await alarm_svc.stop()

    async def test_cancel_escalation_existing_task(self, alarm_svc):
        """_cancel_escalation 应取消已存在任务"""

        async def _long():
            await asyncio.sleep(100)

        task = asyncio.create_task(_long())
        alarm_svc._escalation_tasks["a1"] = task
        await alarm_svc._cancel_escalation("a1")
        assert (task.cancelled() or task.done()) is True
        assert "a1" not in alarm_svc._escalation_tasks

    async def test_cancel_escalation_no_task(self, alarm_svc):
        """无任务时 _cancel_escalation 不应抛异常"""
        await alarm_svc._cancel_escalation("nonexistent")

    async def test_escalate_alarm_with_event_bus(self, alarm_svc_with_bus, alarm_repo):
        """有 EventBus 时应发布升级事件（critical 目标不调度下一轮）"""
        alarm_repo.update_severity = AsyncMock()
        alarm = {"alarm_id": "a1", "rule_id": "r1", "device_id": "d1", "severity": SEVERITY_MAJOR}
        await alarm_svc_with_bus._escalate_alarm("a1", SEVERITY_MAJOR, SEVERITY_CRITICAL, alarm)
        alarm_repo.update_severity.assert_called_once_with("a1", SEVERITY_CRITICAL)
        alarm_svc_with_bus._event_bus.publish.assert_called_once()

    async def test_escalate_alarm_publish_failure(self, alarm_svc_with_bus, alarm_repo):
        """EventBus 发布失败应不阻断升级流程"""
        alarm_repo.update_severity = AsyncMock()
        alarm_svc_with_bus._event_bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))
        alarm = {"alarm_id": "a1", "rule_id": "r1", "device_id": "d1", "severity": SEVERITY_MAJOR}
        await alarm_svc_with_bus._escalate_alarm("a1", SEVERITY_MAJOR, SEVERITY_CRITICAL, alarm)
        alarm_repo.update_severity.assert_called_once()

    async def test_escalate_alarm_without_event_bus_fallback(self, alarm_svc, alarm_repo, notification_manager):
        """无 EventBus 时应走 fallback 直接发送通知"""
        alarm_repo.update_severity = AsyncMock()
        alarm = {"alarm_id": "a1", "rule_id": "r1", "device_id": "d1", "severity": SEVERITY_MAJOR}
        await alarm_svc._escalate_alarm("a1", SEVERITY_MAJOR, SEVERITY_CRITICAL, alarm)
        alarm_repo.update_severity.assert_called_once_with("a1", SEVERITY_CRITICAL)
        notification_manager.send_notification.assert_called_once()
        assert alarm["original_severity"] == SEVERITY_MAJOR

    async def test_escalate_alarm_schedules_next_round(self, alarm_svc, alarm_repo):
        """非 critical 目标应调度下一轮升级"""
        alarm_svc.configure_escalation(SEVERITY_MAJOR, threshold_seconds=999)
        alarm_repo.update_severity = AsyncMock()
        alarm = {"alarm_id": "a1", "rule_id": "r1", "device_id": "d1", "severity": SEVERITY_MINOR}
        await alarm_svc._escalate_alarm("a1", SEVERITY_MINOR, SEVERITY_MAJOR, alarm)
        assert "a1" in alarm_svc._escalation_tasks
        await alarm_svc.stop()

    async def test_escalate_alarm_preserves_original_severity(self, alarm_svc, alarm_repo):
        """多次升级应保留最原始严重度"""
        alarm_repo.update_severity = AsyncMock()
        alarm = {"alarm_id": "a1", "rule_id": "r1", "device_id": "d1", "severity": SEVERITY_MINOR}
        await alarm_svc._escalate_alarm("a1", SEVERITY_MINOR, SEVERITY_MAJOR, alarm)
        assert alarm_svc._original_severities["a1"] == SEVERITY_MINOR
        await alarm_svc.stop()

class TestConfigureEscalationExtended:
    def test_configure_with_explicit_escalate_to(self, alarm_svc):
        """应支持显式指定 escalate_to"""
        alarm_svc.configure_escalation("custom", threshold_seconds=120, escalate_to=SEVERITY_CRITICAL)
        cfg = alarm_svc._escalation_configs["custom"]
        assert cfg.threshold_seconds == 120
        assert cfg.escalate_to == SEVERITY_CRITICAL

class TestIsSuppressedTimeRange:
    async def test_time_range_match(self, alarm_svc):
        """时间范围匹配时应抑制（00:00-23:59 总是匹配）"""
        alarm_svc.add_suppression_rule(
            AlarmSuppressionRule(rule_id="r1", name="allday", time_range_start="00:00", time_range_end="23:59")
        )
        assert await alarm_svc._is_suppressed("r1", "d1", SEVERITY_INFO) is True

    def test_is_in_time_range_invalid_format(self):
        """非法时间格式（int 解析失败）应返回 False"""
        assert AlarmService._is_in_time_range("12:00", "ab:cd", "17:00") is False

    def test_is_in_time_range_invalid_current(self):
        """current 非法时应返回 False"""
        assert AlarmService._is_in_time_range("bad", "09:00", "17:00") is False

class TestStatsPropertySetters:
    def test_recovery_times_setter(self, alarm_svc):
        """_recovery_times setter 应写入 _stats"""
        alarm_svc._recovery_times = [1.0, 2.0]
        assert alarm_svc._stats.recovery_times == [1.0, 2.0]

    def test_failure_intervals_setter(self, alarm_svc):
        """_failure_intervals setter 应写入 _stats"""
        alarm_svc._failure_intervals = [3.0, 4.0]
        assert alarm_svc._stats.failure_intervals == [3.0, 4.0]

    def test_recovery_times_getter(self, alarm_svc):
        """_recovery_times getter 应返回 _stats.recovery_times"""
        alarm_svc._stats.recovery_times = [5.0]
        assert alarm_svc._recovery_times == [5.0]

    def test_failure_intervals_getter(self, alarm_svc):
        """_failure_intervals getter 应返回 _stats.failure_intervals"""
        alarm_svc._stats.failure_intervals = [6.0]
        assert alarm_svc._failure_intervals == [6.0]

class TestStatisticsSummary:
    async def test_summary_no_filter(self, alarm_svc, alarm_repo):
        """无 device_ids 过滤的统计摘要"""
        alarm_repo.count_by_status_and_severity = AsyncMock(
            return_value={
                ("firing", "critical"): 2,
                ("acknowledged", "major"): 1,
                ("recovered", "minor"): 3,
                ("other", "info"): 5,
            }
        )
        summary = await alarm_svc.get_statistics_summary()
        assert summary["total_alarms"] == 6
        assert summary["firing_alarms"] == 2
        assert summary["acknowledged_alarms"] == 1
        assert summary["recovered_alarms"] == 3
        assert summary["by_severity"]["critical"] == 2
        assert "mttr_formatted" in summary
        assert "mtbf_formatted" in summary
        assert "alarm_rate_per_hour" in summary

    async def test_summary_with_device_ids(self, alarm_svc, alarm_repo):
        """有 device_ids 过滤时应下推到 repo"""
        alarm_repo.count_by_status_and_severity = AsyncMock(return_value={})
        await alarm_svc.get_statistics_summary(device_ids={"d1"})
        call_kwargs = alarm_repo.count_by_status_and_severity.call_args.kwargs
        assert call_kwargs.get("device_ids") == ["d1"]

    async def test_summary_empty_counts(self, alarm_svc, alarm_repo):
        """空计数应返回全零摘要"""
        alarm_repo.count_by_status_and_severity = AsyncMock(return_value={})
        summary = await alarm_svc.get_statistics_summary()
        assert summary["total_alarms"] == 0
        assert summary["firing_alarms"] == 0

class TestCalculateAlarmRateEdge:
    def test_zero_total_count(self, alarm_svc):
        """total_count 为 0 时速率应为 0"""
        alarm_svc._first_alarm_time = datetime.now(UTC)
        assert alarm_svc._calculate_alarm_rate() == 0.0

    def test_nonzero_returns_float(self, alarm_svc):
        """有数据时应返回 float"""
        alarm_svc._first_alarm_time = datetime.now(UTC) - timedelta(hours=2)
        alarm_svc._stats.total_count = 10
        rate = alarm_svc._calculate_alarm_rate()
        assert isinstance(rate, float)
        assert rate > 0

class TestTrendAndTop:
    async def test_get_trend_no_filter(self, alarm_svc, alarm_repo):
        """无 device_ids 过滤的趋势查询"""
        alarm_repo.query_trend_data = AsyncMock(return_value={"top_devices": [{"device_id": "d1"}, {"device_id": "d2"}]}
        )
        trend = await alarm_svc.get_trend()
        assert len(trend["top_devices"]) == 2

    async def test_get_trend_with_device_ids(self, alarm_svc, alarm_repo):
        """有 device_ids 过滤时应筛选 top_devices"""
        alarm_repo.query_trend_data = AsyncMock(return_value={"top_devices": [{"device_id": "d1"}, {"device_id": "d2"}]}
        )
        trend = await alarm_svc.get_trend(device_ids={"d1"})
        assert len(trend["top_devices"]) == 1
        assert trend["top_devices"][0]["device_id"] == "d1"

    async def test_get_top_alarms_no_filter(self, alarm_svc, alarm_repo):
        """无 device_ids 过滤的 Top 查询"""
        alarm_repo.get_top_alarms = AsyncMock(return_value={"devices": [{"device_id": "d1"}]})
        result = await alarm_svc.get_top_alarms()
        assert "devices" in result

    async def test_get_top_alarms_with_device_ids(self, alarm_svc, alarm_repo):
        """有 device_ids 过滤时应下推到 repo"""
        alarm_repo.get_top_alarms = AsyncMock(return_value={})
        await alarm_svc.get_top_alarms(hours=12, device_ids={"d1", "d2"}, limit=5)
        call_kwargs = alarm_repo.get_top_alarms.call_args.kwargs
        assert call_kwargs.get("hours") == 12
        assert call_kwargs.get("limit") == 5
        assert set(call_kwargs.get("device_ids")) == {"d1", "d2"}

class TestCleanup:
    async def test_cleanup_old_data_trims_and_cleans(self, alarm_svc, alarm_repo):
        """_cleanup_old_data 应裁剪列表并清理过期 start_times"""
        alarm_svc._stats.recovery_times = [1.0] * 150
        alarm_svc._stats.failure_intervals = [1.0] * 150
        alarm_svc._alarm_start_times["stale"] = datetime.now(UTC) - timedelta(days=10)
        alarm_svc._alarm_start_times["fresh"] = datetime.now(UTC)
        alarm_repo.cleanup_old_alarms = AsyncMock(return_value=5)
        await alarm_svc._cleanup_old_data()
        assert len(alarm_svc._stats.recovery_times) == 100
        assert len(alarm_svc._stats.failure_intervals) == 100
        assert "stale" not in alarm_svc._alarm_start_times
        assert "fresh" in alarm_svc._alarm_start_times

    async def test_cleanup_old_data_repo_failure(self, alarm_svc, alarm_repo):
        """repo 清理失败应不阻断"""
        alarm_repo.cleanup_old_alarms = AsyncMock(side_effect=RuntimeError("db"))
        await alarm_svc._cleanup_old_data()

    async def test_cleanup_old_data_no_trim_needed(self, alarm_svc, alarm_repo):
        """列表未超 100 时不应裁剪"""
        alarm_svc._stats.recovery_times = [1.0] * 50
        alarm_repo.cleanup_old_alarms = AsyncMock(return_value=0)
        await alarm_svc._cleanup_old_data()
        assert len(alarm_svc._stats.recovery_times) == 50

    async def test_cleanup_loop_cancel(self, alarm_svc, alarm_repo):
        """_cleanup_loop 应在取消时正常退出"""
        alarm_repo.cleanup_old_alarms = AsyncMock(return_value=0)
        task = asyncio.create_task(alarm_svc._cleanup_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.sleep(0.05)
        assert task.done() is True

class TestPublicAPI:
    async def test_get_alarm(self, alarm_svc, alarm_repo):
        """get_alarm 应委托给 repo.get"""
        alarm_repo.get = AsyncMock(return_value={"alarm_id": "a1"})
        result = await alarm_svc.get_alarm("a1")
        assert result == {"alarm_id": "a1"}

    async def test_get_alarm_history(self, alarm_svc, alarm_repo):
        """get_alarm_history 应委托给 repo"""
        alarm_repo.get_alarm_history = AsyncMock(return_value=[{"alarm_id": "a1"}])
        result = await alarm_svc.get_alarm_history("r1", days=3)
        assert len(result) == 1
        alarm_repo.get_alarm_history.assert_called_once_with("r1", days=3)

    async def test_delete_alarm(self, alarm_svc, alarm_repo):
        """delete_alarm 应清理内存状态并删除 DB 记录"""
        alarm_svc._alarm_start_times["a1"] = datetime.now(UTC)
        alarm_svc._original_severities["a1"] = SEVERITY_MINOR
        alarm_svc._handled_alarm_ids.add("a1")
        alarm_repo.delete = AsyncMock(return_value=True)
        result = await alarm_svc.delete_alarm("a1")
        assert result is True
        assert "a1" not in alarm_svc._alarm_start_times
        assert "a1" not in alarm_svc._original_severities
        assert "a1" not in alarm_svc._handled_alarm_ids

    async def test_delete_alarm_false(self, alarm_svc, alarm_repo):
        """delete_alarm 删除失败应返回 False"""
        alarm_repo.delete = AsyncMock(return_value=False)
        result = await alarm_svc.delete_alarm("a1")
        assert result is False

class TestAckAlarm:
    async def test_ack_with_event_bus(self, alarm_svc_with_bus, alarm_repo):
        """有 EventBus 时应发布 acknowledged 事件"""
        alarm_repo.ack = AsyncMock(return_value=_alarm(severity=SEVERITY_MAJOR, status=None))
        result = await alarm_svc_with_bus.ack_alarm("a1", "user1")
        assert result is not None
        alarm_svc_with_bus._event_bus.publish.assert_called_once()

    async def test_ack_without_event_bus(self, alarm_svc, alarm_repo, notification_manager):
        """无 EventBus 时应直接调用 _handle_acknowledgment"""
        alarm_repo.ack = AsyncMock(return_value=_alarm(severity=SEVERITY_MAJOR, status=None))
        alarm_repo.get = AsyncMock(return_value=_alarm(status=None))
        await alarm_svc.ack_alarm("a1", "user1")
        notification_manager.send_notification.assert_called_once()

    async def test_ack_status_conflict(self, alarm_svc, alarm_repo, notification_manager):
        """状态冲突时应返回告警但不发布事件"""
        alarm_repo.ack = AsyncMock(return_value={"alarm_id": "a1", "_status_conflict": True, "rule_id": "r1"})
        result = await alarm_svc.ack_alarm("a1", "user1")
        assert result is not None
        assert "_status_conflict" not in result
        notification_manager.send_notification.assert_not_called()

    async def test_ack_not_found(self, alarm_svc, alarm_repo, notification_manager):
        """告警不存在时应返回 None"""
        alarm_repo.ack = AsyncMock(return_value=None)
        result = await alarm_svc.ack_alarm("a1", "user1")
        assert result is None
        notification_manager.send_notification.assert_not_called()

    async def test_ack_with_name_resolver(self, alarm_repo, notification_manager, event_bus):
        """ack_alarm 应通过 name_resolver 解析名称"""

        async def _resolve(rule_id, device_id):
            if rule_id:
                return ("MyRule", "")
            return ("", "MyDevice")

        resolver = MagicMock()
        resolver.resolve = _resolve
        svc = AlarmService(alarm_repo, event_bus, notification_manager, name_resolver=resolver)
        alarm_repo.ack = AsyncMock(return_value=_alarm(severity=SEVERITY_MAJOR, status=None))
        await svc.ack_alarm("a1", "user1")
        published_event = event_bus.publish.call_args[0][0]
        assert published_event.rule_name == "MyRule"
        assert published_event.device_name == "MyDevice"

class TestClearAlarm:
    async def test_clear_with_event_bus(self, alarm_svc_with_bus, alarm_repo):
        """有 EventBus 时应发布 recovered 事件"""
        alarm_repo.recover = AsyncMock(return_value=_alarm(severity=SEVERITY_MAJOR, status=None))
        result = await alarm_svc_with_bus.clear_alarm("a1")
        assert result is not None
        alarm_svc_with_bus._event_bus.publish.assert_called_once()

    async def test_clear_without_event_bus(self, alarm_svc, alarm_repo, notification_manager):
        """无 EventBus 时应直接调用 _handle_recovery"""
        alarm_repo.recover = AsyncMock(return_value=_alarm(severity=SEVERITY_MAJOR, status=None))
        alarm_repo.get = AsyncMock(return_value=_alarm(status=None))
        await alarm_svc.clear_alarm("a1")
        notification_manager.send_notification.assert_called_once()

    async def test_clear_status_conflict(self, alarm_svc, alarm_repo, notification_manager):
        """状态冲突时应返回告警但不发布事件"""
        alarm_repo.recover = AsyncMock(return_value={"alarm_id": "a1", "_status_conflict": True, "rule_id": "r1"})
        result = await alarm_svc.clear_alarm("a1")
        assert result is not None
        assert "_status_conflict" not in result
        notification_manager.send_notification.assert_not_called()

    async def test_clear_not_found(self, alarm_svc, alarm_repo, notification_manager):
        """告警不存在时应返回 None"""
        alarm_repo.recover = AsyncMock(return_value=None)
        result = await alarm_svc.clear_alarm("a1")
        assert result is None
        notification_manager.send_notification.assert_not_called()

class TestTriggerAlarmExtended:
    async def test_trigger_notification_failure(self, alarm_svc, alarm_repo, notification_manager):
        """通知失败时告警仍应持久化"""
        alarm_repo.create = AsyncMock(return_value={"alarm_id": "a1", "severity": SEVERITY_INFO})
        notification_manager.send_notification = AsyncMock(return_value={"email": False, "webhook": False})
        result = await alarm_svc.trigger_alarm("r1", "Rule", "d1", "Dev", SEVERITY_INFO, "msg", {}, channels=["email"])
        assert result is not None
        assert result["alarm_id"] == "a1"

    async def test_trigger_with_channels(self, alarm_svc, alarm_repo, notification_manager):
        """应将 channels 传递给通知管理器"""
        alarm_repo.create = AsyncMock(return_value={"alarm_id": "a1", "severity": SEVERITY_INFO})
        await alarm_svc.trigger_alarm(
            "r1", "Rule", "d1", "Dev", SEVERITY_INFO, "msg", {},
            channels=["email", "webhook"],
        )
        call_args = notification_manager.send_notification.call_args
        assert call_args[0][1] == ["email", "webhook"]

class TestListAlarmsExtended:
    async def test_list_with_all_params(self, alarm_svc, alarm_repo):
        """list_alarms 应传递所有参数"""
        alarm_repo.list_all = AsyncMock(return_value=([{"alarm_id": "a1"}], 1))
        alarms, total = await alarm_svc.list_alarms(
            page=2, size=10, status="firing",
            severity="critical", device_id="d1", search="temp",
        )
        assert total == 1
        call_args = alarm_repo.list_all.call_args
        assert call_args.args == (2, 10, "firing", "critical", "d1", "temp")

