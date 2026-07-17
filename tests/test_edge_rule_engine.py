"""边缘规则引擎单元测试

覆盖 src/edgelite/drivers/edge_rule_engine.py：
- EdgeRuleType / EdgeRuleOperator 枚举
- _OP_FUNCS 运算符映射（GT/GTE/LT/LTE/EQ/NEQ）
- EdgeRule / AlarmRecord 数据类 to_dict()
- ModbusEdgeRuleEngine: add_rule / get_rule / evaluate_point / _check_condition
  / remove_rule / update_rule / get_stats / cooldown / duration / deadband / recovery

设计要点：
- 规则引擎是纯内存逻辑，无需 mock 外部 IO
- evaluate_point 为 async，验证阈值触发/恢复/冷却期/持续时长/死区
"""

from __future__ import annotations

import pytest

from edgelite.drivers.edge_rule_engine import (
    _OP_FUNCS,
    AlarmRecord,
    EdgeRule,
    EdgeRuleOperator,
    EdgeRuleType,
    ModbusEdgeRuleEngine,
)

# ── 枚举 ──


class TestEnums:
    def test_rule_type_values(self):
        assert EdgeRuleType.THRESHOLD == "threshold"
        assert EdgeRuleType.RATE_OF_CHANGE == "rate_of_change"
        assert EdgeRuleType.STATE == "state"
        assert EdgeRuleType.EXPRESSION == "expression"

    def test_operator_values(self):
        assert EdgeRuleOperator.GT == ">"
        assert EdgeRuleOperator.GTE == ">="
        assert EdgeRuleOperator.LT == "<"
        assert EdgeRuleOperator.LTE == "<="
        assert EdgeRuleOperator.EQ == "=="
        assert EdgeRuleOperator.NEQ == "!="


# ── _OP_FUNCS ──


class TestOpFuncs:
    def test_gt(self):
        assert _OP_FUNCS[EdgeRuleOperator.GT](5, 3) is True
        assert _OP_FUNCS[EdgeRuleOperator.GT](3, 3) is False
        assert _OP_FUNCS[EdgeRuleOperator.GT](2, 3) is False

    def test_gte(self):
        assert _OP_FUNCS[EdgeRuleOperator.GTE](5, 3) is True
        assert _OP_FUNCS[EdgeRuleOperator.GTE](3, 3) is True
        assert _OP_FUNCS[EdgeRuleOperator.GTE](2, 3) is False

    def test_lt(self):
        assert _OP_FUNCS[EdgeRuleOperator.LT](2, 3) is True
        assert _OP_FUNCS[EdgeRuleOperator.LT](3, 3) is False

    def test_lte(self):
        assert _OP_FUNCS[EdgeRuleOperator.LTE](2, 3) is True
        assert _OP_FUNCS[EdgeRuleOperator.LTE](3, 3) is True
        assert _OP_FUNCS[EdgeRuleOperator.LTE](4, 3) is False

    def test_eq_float_tolerance(self):
        assert _OP_FUNCS[EdgeRuleOperator.EQ](3.0, 3.0) is True
        assert _OP_FUNCS[EdgeRuleOperator.EQ](3.0000000001, 3.0) is True  # 容差内
        assert _OP_FUNCS[EdgeRuleOperator.EQ](3.1, 3.0) is False

    def test_neq(self):
        assert _OP_FUNCS[EdgeRuleOperator.NEQ](3.1, 3.0) is True
        assert _OP_FUNCS[EdgeRuleOperator.NEQ](3.0, 3.0) is False


# ── EdgeRule 数据类 ──


class TestEdgeRule:
    def test_to_dict_contains_all_fields(self):
        rule = EdgeRule(
            rule_id="r1",
            device_id="dev1",
            point_name="temp",
            threshold=50.0,
            severity="critical",
        )
        d = rule.to_dict()
        assert d["rule_id"] == "r1"
        assert d["device_id"] == "dev1"
        assert d["point_name"] == "temp"
        assert d["threshold"] == 50.0
        assert d["severity"] == "critical"
        assert d["enabled"] is True
        assert d["rule_type"] == "threshold"
        assert d["operator"] == ">"

    def test_defaults(self):
        rule = EdgeRule(rule_id="r2")
        assert rule.device_id == ""
        assert rule.threshold == 0.0
        assert rule.severity == "major"
        assert rule.enabled is True
        assert rule.cooldown_ms == 5000.0
        assert rule.duration_ms == 0.0
        assert rule.deadband == 0.0
        assert rule.actions == []


# ── AlarmRecord 数据类 ──


class TestAlarmRecord:
    def test_to_dict_contains_all_fields(self):
        from datetime import UTC, datetime

        ts = datetime.now(UTC)
        record = AlarmRecord(
            alarm_id="a1",
            rule_id="r1",
            device_id="dev1",
            point_name="temp",
            action="firing",
            trigger_value=55.0,
            threshold=50.0,
            severity="major",
            latency_ms=1.5,
            timestamp=ts,
        )
        d = record.to_dict()
        assert d["alarm_id"] == "a1"
        assert d["action"] == "firing"
        assert d["trigger_value"] == 55.0
        assert d["threshold"] == 50.0
        assert d["timestamp"] == ts.isoformat()


# ── ModbusEdgeRuleEngine 规则管理 ──


class TestRuleManagement:
    @pytest.fixture
    def engine(self):
        return ModbusEdgeRuleEngine()

    def test_add_rule(self, engine):
        rule = EdgeRule(rule_id="r1", device_id="dev1")
        engine.add_rule(rule)
        assert engine.get_rule("r1") is rule

    def test_get_rule_not_found(self, engine):
        assert engine.get_rule("nonexistent") is None

    def test_get_rules_for_device(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1"))
        engine.add_rule(EdgeRule(rule_id="r2", device_id="dev2"))
        engine.add_rule(EdgeRule(rule_id="r3", device_id=""))  # 全局规则
        rules = engine.get_rules_for_device("dev1")
        rule_ids = {r.rule_id for r in rules}
        assert rule_ids == {"r1", "r3"}  # dev1 专属 + 全局

    def test_get_rules_for_device_excludes_disabled(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", enabled=False))
        rules = engine.get_rules_for_device("dev1")
        assert rules == []

    def test_get_all_rules(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1"))
        engine.add_rule(EdgeRule(rule_id="r2"))
        all_rules = engine.get_all_rules()
        assert len(all_rules) == 2
        assert all(isinstance(r, dict) for r in all_rules)

    @pytest.mark.asyncio
    async def test_remove_rule(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1"))
        removed = await engine.remove_rule("r1")
        assert removed is not None
        assert engine.get_rule("r1") is None

    @pytest.mark.asyncio
    async def test_remove_rule_not_found(self, engine):
        removed = await engine.remove_rule("nonexistent")
        assert removed is None

    @pytest.mark.asyncio
    async def test_remove_rule_clears_state(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1"))
        engine._active_alarms["r1"] = "fake"
        engine._last_fire_ts["r1"] = 123
        await engine.remove_rule("r1")
        assert "r1" not in engine._active_alarms
        assert "r1" not in engine._last_fire_ts

    @pytest.mark.asyncio
    async def test_update_rule(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", threshold=50.0))
        updated = await engine.update_rule("r1", {"threshold": 75.0, "severity": "critical"})
        assert updated is True
        assert engine.get_rule("r1").threshold == 75.0
        assert engine.get_rule("r1").severity == "critical"

    @pytest.mark.asyncio
    async def test_update_rule_not_found(self, engine):
        updated = await engine.update_rule("nonexistent", {"threshold": 75.0})
        assert updated is False

    @pytest.mark.asyncio
    async def test_update_rule_ignores_rule_id(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1"))
        await engine.update_rule("r1", {"rule_id": "r2"})
        assert engine.get_rule("r1") is not None  # 原规则仍在
        assert engine.get_rule("r2") is None  # rule_id 未被修改


# ── _check_condition ──


class TestCheckCondition:
    @pytest.fixture
    def engine(self):
        return ModbusEdgeRuleEngine()

    def test_gt_condition(self, engine):
        rule = EdgeRule(rule_id="r1", operator=EdgeRuleOperator.GT, threshold=50)
        assert engine._check_condition(rule, 55) is True
        assert engine._check_condition(rule, 45) is False

    def test_lte_condition(self, engine):
        rule = EdgeRule(rule_id="r1", operator=EdgeRuleOperator.LTE, threshold=50)
        assert engine._check_condition(rule, 50) is True
        assert engine._check_condition(rule, 51) is False

    def test_eq_condition(self, engine):
        rule = EdgeRule(rule_id="r1", operator=EdgeRuleOperator.EQ, threshold=50)
        assert engine._check_condition(rule, 50) is True
        assert engine._check_condition(rule, 51) is False


# ── evaluate_point（阈值触发与恢复）──


class TestEvaluatePoint:
    @pytest.fixture
    def engine(self):
        return ModbusEdgeRuleEngine()

    @pytest.mark.asyncio
    async def test_threshold_triggers_alarm(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        alarms = await engine.evaluate_point("dev1", "temp", 55, "good")
        assert len(alarms) == 1
        assert alarms[0].action == "firing"
        assert alarms[0].trigger_value == 55

    @pytest.mark.asyncio
    async def test_no_trigger_below_threshold(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        alarms = await engine.evaluate_point("dev1", "temp", 45, "good")
        assert len(alarms) == 0

    @pytest.mark.asyncio
    async def test_recovery_after_breach(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        # 触发
        await engine.evaluate_point("dev1", "temp", 55, "good")
        # 恢复
        alarms = await engine.evaluate_point("dev1", "temp", 45, "good")
        assert len(alarms) == 1
        assert alarms[0].action == "recovered"

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_fire(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50, cooldown_ms=10000))
        # 第一次触发
        alarms1 = await engine.evaluate_point("dev1", "temp", 55, "good")
        assert len(alarms1) == 1
        # 冷却期内不应再次触发
        alarms2 = await engine.evaluate_point("dev1", "temp", 60, "good")
        assert len(alarms2) == 0

    @pytest.mark.asyncio
    async def test_bad_quality_skips_evaluation(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        alarms = await engine.evaluate_point("dev1", "temp", 55, "bad")
        assert len(alarms) == 0

    @pytest.mark.asyncio
    async def test_disabled_rule_skipped(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50, enabled=False))
        alarms = await engine.evaluate_point("dev1", "temp", 55, "good")
        assert len(alarms) == 0

    @pytest.mark.asyncio
    async def test_device_filter(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        alarms = await engine.evaluate_point("dev2", "temp", 55, "good")
        assert len(alarms) == 0

    @pytest.mark.asyncio
    async def test_point_filter(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        alarms = await engine.evaluate_point("dev1", "pressure", 55, "good")
        assert len(alarms) == 0

    @pytest.mark.asyncio
    async def test_action_callback_called(self, engine):
        callback_calls = []

        def callback(record):
            callback_calls.append(record)

        engine.set_on_action_callback(callback)
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        await engine.evaluate_point("dev1", "temp", 55, "good")
        assert len(callback_calls) == 1
        assert callback_calls[0].action == "firing"


# ── get_stats / get_active_alarms / get_alarm_history ──


class TestStatsAndHistory:
    @pytest.fixture
    def engine(self):
        return ModbusEdgeRuleEngine()

    @pytest.mark.asyncio
    async def test_stats_after_fire(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        await engine.evaluate_point("dev1", "temp", 55, "good")
        stats = engine.get_stats()
        assert stats["evaluations"] == 1
        assert stats["fires"] == 1
        assert stats["active"] == 1
        assert stats["rules"] == 1

    @pytest.mark.asyncio
    async def test_stats_recovery(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        await engine.evaluate_point("dev1", "temp", 55, "good")
        await engine.evaluate_point("dev1", "temp", 45, "good")
        stats = engine.get_stats()
        assert stats["fires"] == 1
        assert stats["recoveries"] == 1
        assert stats["active"] == 0

    @pytest.mark.asyncio
    async def test_get_active_alarms(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50))
        await engine.evaluate_point("dev1", "temp", 55, "good")
        active = engine.get_active_alarms()
        assert len(active) == 1
        assert active[0]["rule_id"] == "r1"

    @pytest.mark.asyncio
    async def test_get_alarm_history_limit(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50, cooldown_ms=0))
        # 多次触发+恢复
        for _ in range(5):
            await engine.evaluate_point("dev1", "temp", 55, "good")
            await engine.evaluate_point("dev1", "temp", 45, "good")
        history = engine.get_alarm_history(limit=3)
        assert len(history) == 3

    @pytest.mark.asyncio
    async def test_get_alarm_history_all(self, engine):
        engine.add_rule(EdgeRule(rule_id="r1", device_id="dev1", point_name="temp", threshold=50, cooldown_ms=0))
        await engine.evaluate_point("dev1", "temp", 55, "good")
        await engine.evaluate_point("dev1", "temp", 45, "good")
        history = engine.get_alarm_history(limit=0)
        assert len(history) == 2  # firing + recovered
