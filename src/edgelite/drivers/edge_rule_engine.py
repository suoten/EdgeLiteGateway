"""边缘规则引擎 - 在驱动侧实现实时阈值/规则计算与告警触发。

提供轻量级、进程内的规则求值，避免每个协议驱动重复实现告警逻辑。
支持能力:
- 阈值规则 (threshold): 比较 测点值 与 阈值
- 死区 (deadband): 防止在阈值附近抖动反复触发
- 冷却期 (cooldown_ms): 两次告警之间的最小间隔
- 持续时长 (duration_ms): 条件需持续满足一段时间才触发
- 动作回调: 规则命中时通过注册的回调执行设备写入 / MQTT 发布等动作

设计要点:
- ``ModbusEdgeRuleEngine`` 名称保留历史命名（虽被多协议共用），变更会破坏既有导入。
- 驱动代码直接访问 ``_rules`` / ``_alarm_history`` 私有属性，必须保持为真实 dict / list。
- ``evaluate_point`` / ``remove_rule`` / ``update_rule`` 为协程；其余方法同步。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from edgelite.engine.event_bus import EventBus

logger = logging.getLogger(__name__)


class EdgeRuleType(StrEnum):
    THRESHOLD = "threshold"
    RATE_OF_CHANGE = "rate_of_change"
    STATE = "state"
    EXPRESSION = "expression"


class EdgeRuleOperator(StrEnum):
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "=="
    NEQ = "!="


_OP_FUNCS: dict[EdgeRuleOperator, Callable[[float, float], bool]] = {
    EdgeRuleOperator.GT: lambda a, b: a > b,
    EdgeRuleOperator.GTE: lambda a, b: a >= b,
    EdgeRuleOperator.LT: lambda a, b: a < b,
    EdgeRuleOperator.LTE: lambda a, b: a <= b,
    EdgeRuleOperator.EQ: lambda a, b: abs(a - b) < 1e-9,
    EdgeRuleOperator.NEQ: lambda a, b: abs(a - b) >= 1e-9,
}


@dataclass
class EdgeRule:
    rule_id: str
    device_id: str = ""
    point_name: str = ""
    rule_type: EdgeRuleType = EdgeRuleType.THRESHOLD
    operator: EdgeRuleOperator = EdgeRuleOperator.GT
    threshold: float = 0.0
    severity: str = "major"
    enabled: bool = True
    cooldown_ms: float = 5000.0
    duration_ms: float = 0.0
    deadband: float = 0.0
    actions: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "device_id": self.device_id,
            "point_name": self.point_name,
            "rule_type": self.rule_type.value,
            "operator": self.operator.value,
            "threshold": self.threshold,
            "severity": self.severity,
            "enabled": self.enabled,
            "cooldown_ms": self.cooldown_ms,
            "duration_ms": self.duration_ms,
            "deadband": self.deadband,
            "actions": list(self.actions),
        }


@dataclass
class AlarmRecord:
    alarm_id: str
    rule_id: str
    device_id: str
    point_name: str
    action: str
    trigger_value: float
    threshold: float
    severity: str
    latency_ms: float
    timestamp: datetime
    actions: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "alarm_id": self.alarm_id,
            "rule_id": self.rule_id,
            "device_id": self.device_id,
            "point_name": self.point_name,
            "action": self.action,
            "trigger_value": self.trigger_value,
            "threshold": self.threshold,
            "severity": self.severity,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "actions": list(self.actions),
        }


class ModbusEdgeRuleEngine:
    def __init__(self, event_bus: EventBus | None = None) -> None:
        self._rules: dict[str, EdgeRule] = {}
        self._alarm_history: list[AlarmRecord] = []
        self._active_alarms: dict[str, AlarmRecord] = {}
        self._last_fire_ts: dict[str, float] = {}
        self._breach_start_ts: dict[str, float] = {}
        self._last_value: dict[str, float] = {}
        self._on_action_callback: Callable[[AlarmRecord], Awaitable[None] | None] | None = None
        self._event_bus = event_bus
        self._stats = {"evaluations": 0, "fires": 0, "recoveries": 0, "active": 0, "rules": 0}

    def set_on_action_callback(self, callback: Callable[[AlarmRecord], Awaitable[None] | None]) -> None:
        self._on_action_callback = callback

    def add_rule(self, rule: EdgeRule) -> None:
        self._rules[rule.rule_id] = rule
        self._stats["rules"] = len(self._rules)

    def get_rule(self, rule_id: str) -> EdgeRule | None:
        return self._rules.get(rule_id)

    def get_rules_for_device(self, device_id: str) -> list[EdgeRule]:
        return [r for r in self._rules.values() if r.enabled and (not r.device_id or r.device_id == device_id)]

    def get_all_rules(self) -> list[dict]:
        return [r.to_dict() for r in self._rules.values()]

    async def evaluate_point(self, device_id: str, point_name: str, value: float, quality: str) -> list[AlarmRecord]:
        self._stats["evaluations"] += 1
        produced: list[AlarmRecord] = []
        now_mono = time.monotonic()
        for rule in list(self._rules.values()):
            if not rule.enabled:
                continue
            if rule.device_id and rule.device_id != device_id:
                continue
            if rule.point_name and rule.point_name != point_name:
                continue
            is_breached = False
            if quality == "good" and value is not None:
                try:
                    is_breached = self._check_condition(rule, float(value))
                except (TypeError, ValueError) as e:
                    logger.warning("rule %s condition check failed: %s", rule.rule_id, e)
            alarm = await self._process_rule(rule, device_id, point_name, value, is_breached, now_mono)
            if alarm is not None:
                produced.append(alarm)
        self._stats["active"] = len(self._active_alarms)
        return produced

    def _check_condition(self, rule: EdgeRule, value: float) -> bool:
        func = _OP_FUNCS.get(rule.operator)
        if func is None:
            logger.warning("unknown operator %s for rule %s", rule.operator, rule.rule_id)
            return False
        return func(value, rule.threshold)

    async def _process_rule(self, rule: EdgeRule, device_id: str, point_name: str, value: float, is_breached: bool, now_mono: float) -> AlarmRecord | None:
        rule_id = rule.rule_id
        was_active = rule_id in self._active_alarms
        if is_breached:
            if rule.deadband > 0 and rule_id in self._last_value:
                if abs(value - self._last_value[rule_id]) < rule.deadband:
                    return None
            if rule.duration_ms > 0:
                if rule_id not in self._breach_start_ts:
                    self._breach_start_ts[rule_id] = now_mono
                    return None
                if (now_mono - self._breach_start_ts[rule_id]) * 1000 < rule.duration_ms:
                    return None
            if rule_id in self._last_fire_ts:
                elapsed_ms = (now_mono - self._last_fire_ts[rule_id]) * 1000
                if elapsed_ms < rule.cooldown_ms:
                    return None
            self._last_fire_ts[rule_id] = now_mono
            self._last_value[rule_id] = value
            self._breach_start_ts.pop(rule_id, None)
            record = AlarmRecord(str(uuid.uuid4()), rule_id, device_id, point_name, "firing", value, rule.threshold, rule.severity, 0.0, datetime.now(UTC), list(rule.actions))
            self._active_alarms[rule_id] = record
            self._alarm_history.append(record)
            self._stats["fires"] += 1
            await self._fire_callback(record)
            return record
        if was_active:
            self._active_alarms.pop(rule_id, None)
            self._last_value.pop(rule_id, None)
            self._breach_start_ts.pop(rule_id, None)
            record = AlarmRecord(str(uuid.uuid4()), rule_id, device_id, point_name, "recovered", value if value is not None else 0.0, rule.threshold, rule.severity, 0.0, datetime.now(UTC), list(rule.actions))
            self._alarm_history.append(record)
            self._stats["recoveries"] += 1
            await self._fire_callback(record)
            return record
        return None

    async def _fire_callback(self, record: AlarmRecord) -> None:
        if self._on_action_callback is None:
            return
        try:
            result = self._on_action_callback(record)
            if hasattr(result, "__await__"):
                await result
        except Exception as e:
            logger.error("rule action callback failed for %s: %s", record.rule_id, e)

    async def remove_rule(self, rule_id: str) -> EdgeRule | None:
        rule = self._rules.pop(rule_id, None)
        if rule is None:
            return None
        self._active_alarms.pop(rule_id, None)
        self._last_fire_ts.pop(rule_id, None)
        self._last_value.pop(rule_id, None)
        self._breach_start_ts.pop(rule_id, None)
        self._stats["rules"] = len(self._rules)
        self._stats["active"] = len(self._active_alarms)
        return rule

    async def update_rule(self, rule_id: str, updates: dict) -> bool:
        rule = self._rules.get(rule_id)
        if rule is None:
            return False
        for key, val in updates.items():
            if key == "rule_id":
                continue
            if hasattr(rule, key):
                if key == "rule_type" and not isinstance(val, EdgeRuleType):
                    val = EdgeRuleType(val)
                elif key == "operator" and not isinstance(val, EdgeRuleOperator):
                    val = EdgeRuleOperator(val)
                elif key in ("threshold", "cooldown_ms", "duration_ms", "deadband"):
                    val = float(val)
                elif key == "enabled":
                    val = bool(val)
                setattr(rule, key, val)
        return True

    def get_active_alarms(self) -> list[dict]:
        return [r.to_dict() for r in self._active_alarms.values()]

    def get_alarm_history(self, limit: int = 100) -> list[AlarmRecord]:
        if limit <= 0:
            return list(self._alarm_history)
        return list(self._alarm_history[-limit:])

    def get_stats(self) -> dict:
        stats = dict(self._stats)
        stats["rules"] = len(self._rules)
        stats["active"] = len(self._active_alarms)
        stats["history"] = len(self._alarm_history)
        return stats


__all__ = ["AlarmRecord", "EdgeRule", "EdgeRuleOperator", "EdgeRuleType", "ModbusEdgeRuleEngine"]
