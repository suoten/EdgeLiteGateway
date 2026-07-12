"""告警服务测试 - 通知/升级/抑制/统计

覆盖 services/alarm_service.py：
- AlarmEscalationConfig / AlarmSuppressionRule / AlarmStatistics 数据类
- AlarmService: 升级配置、抑制规则、统计计算
- _is_suppressed: 抑制规则匹配（设备/规则/严重度/时间范围/过期）
- _is_in_time_range: 时间范围判断（含跨夜）
- _format_duration: 时长格式化
- _calculate_alarm_rate: 告警速率计算
- 统计更新与 MTTR/MTBF 计算
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from edgelite.services.alarm_service import (
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    SEVERITY_WARNING,
    AlarmEscalationConfig,
    AlarmService,
    AlarmStatistics,
    AlarmSuppressionRule,
)


@pytest.fixture
def alarm_repo():
    """Fake AlarmRepo"""
    repo = AsyncMock()
    repo.get = AsyncMock(return_value=None)
    repo.create_with_id = AsyncMock(return_value=None)
    repo.update_severity = AsyncMock()
    repo.count_by_status_and_severity = AsyncMock(return_value={})
    repo.list_all = AsyncMock(return_value=([], 0))
    repo.query_trend_data = AsyncMock(return_value={"top_devices": []})
    repo.get_top_alarms = AsyncMock(return_value={})
    repo.cleanup_old_alarms = AsyncMock(return_value=0)
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
def alarm_svc(alarm_repo, notification_manager):
    """创建 AlarmService 实例（无 EventBus）"""
    svc = AlarmService(
        alarm_repo=alarm_repo,
        event_bus=None,
        notification_manager=notification_manager,
    )
    return svc


class TestSeverityConstants:
    def test_severity_order(self):
        """严重度顺序应为 INFO < WARNING < MINOR < MAJOR < CRITICAL"""
        assert SEVERITY_INFO == "info"
        assert SEVERITY_WARNING == "warning"
        assert SEVERITY_MINOR == "minor"
        assert SEVERITY_MAJOR == "major"
        assert SEVERITY_CRITICAL == "critical"


class TestAlarmEscalationConfig:
    def test_required_fields(self):
        """升级配置必填字段"""
        cfg = AlarmEscalationConfig(
            severity=SEVERITY_CRITICAL,
            threshold_seconds=300,
            escalate_to=SEVERITY_CRITICAL,
        )
        assert cfg.severity == SEVERITY_CRITICAL
        assert cfg.threshold_seconds == 300
        assert cfg.notify_channels == []


class TestAlarmSuppressionRule:
    def test_defaults(self):
        """抑制规则默认值"""
        rule = AlarmSuppressionRule(rule_id="r1", name="test")
        assert rule.rule_id == "r1"
        assert rule.enabled is True
        assert rule.expires_at is None
        assert rule.device_ids == []

    def test_with_filters(self):
        """带过滤器的抑制规则"""
        rule = AlarmSuppressionRule(
            rule_id="r1",
            name="suppress critical",
            device_ids=["dev1", "dev2"],
            severities=[SEVERITY_CRITICAL],
        )
        assert rule.device_ids == ["dev1", "dev2"]
        assert rule.severities == [SEVERITY_CRITICAL]


class TestAlarmStatistics:
    def test_defaults(self):
        """统计默认值"""
        stats = AlarmStatistics()
        assert stats.total_count == 0
        assert stats.firing_count == 0
        assert stats.escalated_count == 0
        assert stats.mttr_seconds == 0.0
        assert stats.mtbf_seconds == 0.0


class TestConfigureEscalation:
    def test_configure_escalation(self, alarm_svc):
        """应能配置升级阈值"""
        alarm_svc.configure_escalation(SEVERITY_CRITICAL, threshold_seconds=600)
        cfg = alarm_svc._escalation_configs[SEVERITY_CRITICAL]
        assert cfg.threshold_seconds == 600
        assert cfg.escalate_to == SEVERITY_CRITICAL

    def test_default_escalation_configs(self, alarm_svc):
        """应有默认升级配置"""
        assert SEVERITY_CRITICAL in alarm_svc._escalation_configs
        assert SEVERITY_MAJOR in alarm_svc._escalation_configs
        assert SEVERITY_MINOR in alarm_svc._escalation_configs
        assert SEVERITY_WARNING in alarm_svc._escalation_configs

    def test_critical_escalates_to_critical(self, alarm_svc):
        """CRITICAL 应升级到 CRITICAL（不再升级）"""
        cfg = alarm_svc._escalation_configs[SEVERITY_CRITICAL]
        assert cfg.escalate_to == SEVERITY_CRITICAL

    def test_major_escalates_to_critical(self, alarm_svc):
        """MAJOR 应升级到 CRITICAL"""
        cfg = alarm_svc._escalation_configs[SEVERITY_MAJOR]
        assert cfg.escalate_to == SEVERITY_CRITICAL


class TestSuppressionRules:
    def test_add_suppression_rule(self, alarm_svc):
        """应能添加抑制规则"""
        rule = AlarmSuppressionRule(rule_id="r1", name="test")
        alarm_svc.add_suppression_rule(rule)
        rules = alarm_svc.list_suppression_rules()
        assert len(rules) == 1

    def test_remove_suppression_rule(self, alarm_svc):
        """应能删除抑制规则"""
        rule = AlarmSuppressionRule(rule_id="r1", name="test")
        alarm_svc.add_suppression_rule(rule)
        assert alarm_svc.remove_suppression_rule("r1") is True
        assert len(alarm_svc.list_suppression_rules()) == 0

    def test_remove_nonexistent_rule(self, alarm_svc):
        """删除不存在的规则应返回 False"""
        assert alarm_svc.remove_suppression_rule("nonexistent") is False

    def test_list_suppression_rules_returns_copy(self, alarm_svc):
        """list 应返回副本，修改不影响内部状态"""
        rule = AlarmSuppressionRule(rule_id="r1", name="test")
        alarm_svc.add_suppression_rule(rule)
        rules = alarm_svc.list_suppression_rules()
        rules.clear()
        assert len(alarm_svc.list_suppression_rules()) == 1


class TestIsSuppressed:
    @pytest.mark.asyncio
    async def test_no_rules_not_suppressed(self, alarm_svc):
        """无规则时不应抑制"""
        assert await alarm_svc._is_suppressed("rule1", "dev1", SEVERITY_WARNING) is False

    @pytest.mark.asyncio
    async def test_device_filter_match(self, alarm_svc):
        """设备过滤器匹配应抑制"""
        rule = AlarmSuppressionRule(
            rule_id="r1",
            name="suppress dev1",
            device_ids=["dev1"],
            enabled=True,
        )
        alarm_svc.add_suppression_rule(rule)
        assert await alarm_svc._is_suppressed("rule1", "dev1", SEVERITY_WARNING) is True

    @pytest.mark.asyncio
    async def test_device_filter_no_match(self, alarm_svc):
        """设备过滤器不匹配不应抑制"""
        rule = AlarmSuppressionRule(
            rule_id="r1",
            name="suppress dev1",
            device_ids=["dev1"],
        )
        alarm_svc.add_suppression_rule(rule)
        assert await alarm_svc._is_suppressed("rule1", "dev2", SEVERITY_WARNING) is False

    @pytest.mark.asyncio
    async def test_severity_filter(self, alarm_svc):
        """严重度过滤器"""
        rule = AlarmSuppressionRule(
            rule_id="r1",
            name="suppress critical",
            severities=[SEVERITY_CRITICAL],
        )
        alarm_svc.add_suppression_rule(rule)
        assert await alarm_svc._is_suppressed("r1", "dev1", SEVERITY_CRITICAL) is True
        assert await alarm_svc._is_suppressed("r1", "dev1", SEVERITY_WARNING) is False

    @pytest.mark.asyncio
    async def test_disabled_rule_not_applied(self, alarm_svc):
        """禁用的规则不应生效"""
        rule = AlarmSuppressionRule(
            rule_id="r1",
            name="disabled",
            device_ids=["dev1"],
            enabled=False,
        )
        alarm_svc.add_suppression_rule(rule)
        assert await alarm_svc._is_suppressed("r1", "dev1", SEVERITY_WARNING) is False

    @pytest.mark.asyncio
    async def test_expired_rule_not_applied(self, alarm_svc):
        """过期的规则不应生效"""
        rule = AlarmSuppressionRule(
            rule_id="r1",
            name="expired",
            device_ids=["dev1"],
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        alarm_svc.add_suppression_rule(rule)
        assert await alarm_svc._is_suppressed("r1", "dev1", SEVERITY_WARNING) is False

    @pytest.mark.asyncio
    async def test_rule_filter(self, alarm_svc):
        """规则 ID 过滤器"""
        rule = AlarmSuppressionRule(
            rule_id="r1",
            name="suppress rule1",
            rule_ids=["rule1"],
        )
        alarm_svc.add_suppression_rule(rule)
        assert await alarm_svc._is_suppressed("rule1", "dev1", SEVERITY_WARNING) is True
        assert await alarm_svc._is_suppressed("rule2", "dev1", SEVERITY_WARNING) is False


class TestIsInTimeRange:
    def test_normal_range(self):
        """正常时间范围"""
        assert AlarmService._is_in_time_range("10:00", "09:00", "17:00") is True
        assert AlarmService._is_in_time_range("08:00", "09:00", "17:00") is False

    def test_overnight_range(self):
        """跨夜时间范围"""
        assert AlarmService._is_in_time_range("23:00", "22:00", "06:00") is True
        assert AlarmService._is_in_time_range("03:00", "22:00", "06:00") is True
        assert AlarmService._is_in_time_range("12:00", "22:00", "06:00") is False

    def test_normalized_time(self):
        """应规范化时间格式"""
        assert AlarmService._is_in_time_range("9:00", "9:00", "17:00") is True
        assert AlarmService._is_in_time_range("9:00", "09:00", "17:00") is True

    def test_invalid_time(self):
        """非法时间应返回 False"""
        assert AlarmService._is_in_time_range("invalid", "09:00", "17:00") is False


class TestFormatDuration:
    def test_seconds(self):
        """<60s 应显示秒"""
        assert AlarmService._format_duration(30) == "30s"

    def test_minutes(self):
        """<3600s 应显示分钟"""
        assert AlarmService._format_duration(90) == "1.5m"

    def test_hours(self):
        """<86400s 应显示小时"""
        assert AlarmService._format_duration(7200) == "2.0h"

    def test_days(self):
        """>=86400s 应显示天"""
        assert AlarmService._format_duration(90000) == "1.0d"


class TestCalculateAlarmRate:
    def test_no_alarms_zero_rate(self, alarm_svc):
        """无告警时速率为 0"""
        assert alarm_svc._calculate_alarm_rate() == 0.0

    def test_with_alarms(self, alarm_svc):
        """有告警时应计算非零速率"""
        alarm_svc._first_alarm_time = datetime.now(UTC) - timedelta(hours=1)
        alarm_svc._stats.total_count = 10
        rate = alarm_svc._calculate_alarm_rate()
        assert rate > 0
        # 10 alarms / 1 hour = 10/hour
        assert 9 <= rate <= 11


class TestUpdateMttrMtbf:
    def test_empty_stats(self, alarm_svc):
        """无数据时 MTTR/MTBF 为 0"""
        alarm_svc._update_mttr_mtbf()
        assert alarm_svc._stats.mttr_seconds == 0.0
        assert alarm_svc._stats.mtbf_seconds == 0.0

    def test_with_recovery_times(self, alarm_svc):
        """有恢复时间应计算 MTTR"""
        alarm_svc._stats.recovery_times = [60.0, 120.0, 180.0]
        alarm_svc._update_mttr_mtbf()
        assert alarm_svc._stats.mttr_seconds == 120.0  # 平均

    def test_with_failure_intervals(self, alarm_svc):
        """有故障间隔应计算 MTBF"""
        alarm_svc._stats.failure_intervals = [300.0, 600.0]
        alarm_svc._update_mttr_mtbf()
        assert alarm_svc._stats.mtbf_seconds == 450.0  # 平均


class TestGetStatistics:
    def test_returns_stats(self, alarm_svc):
        """应返回 AlarmStatistics"""
        stats = alarm_svc.get_statistics()
        assert isinstance(stats, AlarmStatistics)
        assert stats.total_count == 0


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_no_event_bus(self, alarm_svc):
        """无 EventBus 时 start 不应抛异常"""
        await alarm_svc.start()
        # 清理：stop() 内部会 cancel 并 suppress CancelledError
        # （CancelledError 继承 BaseException，except Exception 无法捕获）
        await alarm_svc.stop()

    @pytest.mark.asyncio
    async def test_stop_cleans_tasks(self, alarm_svc):
        """stop 应清理所有任务"""
        await alarm_svc.start()
        await alarm_svc.stop()


class TestTriggerAlarm:
    @pytest.mark.asyncio
    async def test_trigger_alarm_suppressed(self, alarm_svc, alarm_repo):
        """被抑制的告警应返回 None"""
        rule = AlarmSuppressionRule(
            rule_id="rule1",
            name="suppress",
            device_ids=["dev1"],
        )
        alarm_svc.add_suppression_rule(rule)
        result = await alarm_svc.trigger_alarm(
            rule_id="rule1",
            rule_name="Test",
            device_id="dev1",
            device_name="Device1",
            severity=SEVERITY_WARNING,
            message="test",
            trigger_value={},
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_trigger_alarm_success(self, alarm_svc, alarm_repo, notification_manager):
        """成功触发告警"""
        alarm_repo.create = AsyncMock(return_value={"alarm_id": "a1", "severity": SEVERITY_WARNING})
        result = await alarm_svc.trigger_alarm(
            rule_id="rule1",
            rule_name="Test",
            device_id="dev1",
            device_name="Device1",
            severity=SEVERITY_WARNING,
            message="temperature high",
            trigger_value={"temp": 80},
        )
        assert result is not None
        assert result["alarm_id"] == "a1"
        notification_manager.send_notification.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_alarm_create_failure(self, alarm_svc, alarm_repo):
        """创建失败应返回 None"""
        alarm_repo.create = AsyncMock(side_effect=RuntimeError("db error"))
        result = await alarm_svc.trigger_alarm(
            rule_id="rule1",
            rule_name="Test",
            device_id="dev1",
            device_name="Device1",
            severity=SEVERITY_WARNING,
            message="test",
            trigger_value={},
        )
        assert result is None


class TestListAlarms:
    @pytest.mark.asyncio
    async def test_list_alarms_no_filter(self, alarm_svc, alarm_repo):
        """无过滤查询"""
        alarm_repo.list_all = AsyncMock(return_value=([{"alarm_id": "a1"}], 1))
        alarms, total = await alarm_svc.list_alarms()
        assert total == 1
        assert len(alarms) == 1

    @pytest.mark.asyncio
    async def test_list_alarms_with_device_ids(self, alarm_svc, alarm_repo):
        """按设备 ID 集合查询"""
        alarm_repo.list_all = AsyncMock(return_value=([{"alarm_id": "a1"}], 1))
        alarms, total = await alarm_svc.list_alarms(device_ids={"dev1", "dev2"})
        assert total == 1
        # 验证 device_ids 被下推到 repo（set→list 顺序非确定，按集合比较）
        call_args = alarm_repo.list_all.call_args
        assert set(call_args.kwargs.get("device_ids")) == {"dev1", "dev2"}


class TestNotificationChannelManagement:
    def test_list_channels(self, alarm_svc, notification_manager):
        """应列出通知渠道"""
        notification_manager.list_channels = MagicMock(return_value=[{"id": "ch1"}])
        channels = alarm_svc.list_notification_channels()
        assert len(channels) == 1

    def test_set_channel_enabled(self, alarm_svc, notification_manager):
        """应能启用/禁用渠道"""
        notification_manager.set_channel_enabled = MagicMock(return_value=True)
        assert alarm_svc.set_notification_channel_enabled("ch1", False) is True

    @pytest.mark.asyncio
    async def test_test_notification_channel(self, alarm_svc, notification_manager):
        """应能测试通知渠道"""
        notification_manager.test_channel = AsyncMock(return_value=(True, "ok"))
        success, msg = await alarm_svc.test_notification_channel("ch1")
        assert success is True
        assert msg == "ok"
