"""规则评估器 - 订阅测点更新事件，评估规则条件"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

from edgelite.engine.event_bus import AlarmEvent, EventBus, PointUpdateEvent
from edgelite.constants import _RULE_CACHE_TTL, _POINT_VALUE_CACHE_TTL, _POINT_VALUE_CACHE_MAX
from edgelite.storage.sqlite_repo import AlarmRepo, RuleRepo

try:
    from edgelite._cython import check_condition_fast

    _HAS_CYTHON = True
except ImportError:
    _HAS_CYTHON = False
    check_condition_fast = None

logger = logging.getLogger(__name__)


class RuleEvaluator:
    """规则评估器，订阅PointUpdateEvent评估规则"""

    def __init__(self, event_bus: EventBus, rule_repo: RuleRepo, alarm_repo: AlarmRepo):
        self._event_bus = event_bus
        self._rule_repo = rule_repo
        self._alarm_repo = alarm_repo
        # 持续时间窗口追踪: (rule_id, device_id) -> first_match_time
        self._duration_tracker: dict[tuple[str, str], datetime] = {}
        # 规则缓存: cache_key -> rules
        self._rule_cache: dict[str, list] = {}
        self._cache_time: float = 0.0
        self._cache_ttl: float = _RULE_CACHE_TTL  # FIXED: 原问题-硬编码缓存TTL，现引用constants.py
        self._task: asyncio.Task | None = None
        self._point_value_cache: dict[str, tuple[float, float]] = {}
        self._point_cache_ttl: float = _POINT_VALUE_CACHE_TTL
        self._point_cache_max_size: int = _POINT_VALUE_CACHE_MAX

    async def start(self) -> None:
        """启动评估器"""
        queue = self._event_bus.subscribe("rule_evaluator")
        self._task = asyncio.create_task(self._eval_loop(queue), name="rule-evaluator")
        accel = "Cython加速" if _HAS_CYTHON else "纯Python"
        logger.info("规则评估器启动 (%s)", accel)

    async def _get_rules_for_point(self, device_id: str, point_name: str) -> list:
        now = time.time()
        cache_key = f"{device_id}:{point_name}"

        # 缓存过期时清空整个缓存，防止无限增长
        if (now - self._cache_time) >= self._cache_ttl:
            self._rule_cache.clear()
            self._cache_time = now

        # 缓存命中
        if cache_key in self._rule_cache:
            return self._rule_cache[cache_key]

        # 查询数据库并更新缓存
        # FIXED: 原问题-数据库查询无异常保护，异常导致评估循环崩溃
        try:
            rules = await self._rule_repo.list_enabled_by_point(device_id, point_name)
        except Exception as e:
            logger.error("查询规则失败 %s/%s: %s", device_id, point_name, e)
            rules = []
        self._rule_cache[cache_key] = rules
        return rules

    async def stop(self) -> None:
        """停止评估器"""
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._rule_cache.clear()
        self._duration_tracker.clear()
        logger.info("规则评估器停止")

    def cleanup_duration_tracker(self, rule_id: str) -> None:
        """清理已删除规则的持续时间追踪器"""
        keys_to_remove = [k for k in self._duration_tracker if k[0] == rule_id]
        for k in keys_to_remove:
            del self._duration_tracker[k]

    def invalidate_cache(self, device_id: str | None = None, point_name: str | None = None) -> None:
        """主动失效规则缓存"""
        if device_id and point_name:
            cache_key = f"{device_id}:{point_name}"
            self._rule_cache.pop(cache_key, None)
        else:
            self._rule_cache.clear()
            self._cache_time = 0.0

    async def _eval_loop(self, queue: asyncio.Queue) -> None:
        """评估循环"""
        while True:
            try:
                event = await queue.get()
                if isinstance(event, PointUpdateEvent) and event.quality == "good":
                    await self._evaluate(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("规则评估异常: %s", e)

    async def _evaluate(self, event: PointUpdateEvent) -> None:
        """评估单个测点更新事件"""
        # 查找关联该设备+测点的所有启用规则
        rules = await self._get_rules_for_point(event.device_id, event.point_name)

        for rule in rules:
            try:
                await self._evaluate_rule(rule, event)
            except Exception as e:
                logger.error("规则评估失败: %s - %s", rule["rule_id"], e)

    async def _evaluate_rule(self, rule: dict, event: PointUpdateEvent) -> None:
        """评估单条规则"""
        rule_id = rule["rule_id"]
        device_id = rule["device_id"]
        conditions = rule["conditions"]
        logic = rule["logic"]
        duration = rule["duration"]
        rule_type = rule.get("rule_type", "threshold")
        script = rule.get("script", "")

        now = time.time()

        point_values = {event.point_name: event.value}
        cache_key = f"{event.device_id}:{event.point_name}"
        self._point_value_cache[cache_key] = (event.value, now)
        if len(self._point_value_cache) > self._point_cache_max_size:
            expired_keys = [
                k for k, (_, t) in self._point_value_cache.items()
                if (now - t) > self._point_cache_ttl
            ]
            for k in expired_keys:
                del self._point_value_cache[k]
            if len(self._point_value_cache) > self._point_cache_max_size:
                oldest_keys = sorted(
                    self._point_value_cache.keys(),
                    key=lambda k: self._point_value_cache[k][1],
                )
                for k in oldest_keys[: len(self._point_value_cache) - self._point_cache_max_size // 2]:
                    del self._point_value_cache[k]

        for cond in conditions:
            cond_point = cond["point"]
            if cond_point not in point_values:
                cond_cache_key = f"{event.device_id}:{cond_point}"
                cached = self._point_value_cache.get(cond_cache_key)
                if cached and (now - cached[1]) < self._point_cache_ttl:
                    point_values[cond_point] = cached[0]
                else:
                    try:
                        from edgelite.app import _app_state

                        influx = _app_state.influx_storage
                        if influx:
                            latest_dict = await influx.query_latest(event.device_id, [cond_point])
                            if latest_dict and cond_point in latest_dict:
                                latest_val = latest_dict[cond_point]
                                if isinstance(latest_val, dict):
                                    latest_val = latest_val.get("value")
                                if isinstance(latest_val, (int, float)):
                                    point_values[cond_point] = latest_val
                                    self._point_value_cache[cond_cache_key] = (latest_val, now)
                    except Exception as e:
                        logger.debug(
                            "从InfluxDB获取测点值失败 %s.%s: %s", event.device_id, cond_point, e
                        )

        # 评估条件
        if rule_type == "script" and script:
            matched = self._eval_script(script, point_values)
        else:
            matched = self._check_conditions(conditions, point_values, logic)

        tracker_key = (rule_id, device_id)

        if matched:
            # 条件满足
            if duration > 0:
                # 需要持续满足
                first_time = self._duration_tracker.get(tracker_key)
                if first_time is None:
                    # 首次满足，记录时间
                    self._duration_tracker[tracker_key] = datetime.now(UTC)
                else:
                    # 检查是否持续满足到时间窗口
                    elapsed = (datetime.now(UTC) - first_time).total_seconds()
                    if elapsed >= duration:
                        # 持续窗口到期，触发告警
                        await self._fire_alarm(rule, point_values)
                        # 清除追踪
                        self._duration_tracker.pop(tracker_key, None)
            else:
                # 无持续时间要求，立即触发
                await self._fire_alarm(rule, point_values)
        else:
            # 条件不满足
            if tracker_key in self._duration_tracker:
                # 清除持续时间追踪
                self._duration_tracker.pop(tracker_key, None)

            # 检查是否有firing告警需要恢复
            # FIXED: 原问题-数据库查询无异常保护，异常导致评估循环崩溃
            try:
                firing_alarm = await self._alarm_repo.get_firing_by_rule_device(rule_id, device_id)
            except Exception as e:
                logger.error("查询firing告警失败 %s/%s: %s", rule_id, device_id, e)
                firing_alarm = None
            if firing_alarm:
                await self._recover_alarm(firing_alarm["alarm_id"], rule)

    def _check_conditions(
        self, conditions: list[dict], point_values: dict[str, float], logic: str
    ) -> bool:
        """检查条件组合"""
        if _HAS_CYTHON:
            fast_conds = []
            all_available = True
            for cond in conditions:
                point = cond["point"]
                value = point_values.get(point)
                if value is None:
                    all_available = False
                    break
                fast_conds.append({"operator": cond["operator"], "threshold": cond["threshold"]})
            if all_available and fast_conds:
                values = [point_values[c["point"]] for c in conditions]
                for actual, fc in zip(values, fast_conds, strict=False):
                    if not check_condition_fast(actual, fc["operator"], fc["threshold"]):
                        if logic == "AND":
                            return False
                    else:
                        if logic == "OR":
                            return True
                return logic == "AND"

        results = []
        for cond in conditions:
            point = cond["point"]
            operator = cond["operator"]
            threshold = cond["threshold"]

            value = point_values.get(point)
            if value is None:
                results.append(False)
                continue

            result = self._compare(value, operator, threshold)
            results.append(result)

        if not results:
            return False

        if logic == "AND":
            return all(results)
        else:  # OR
            return any(results)

    @staticmethod
    def _compare(value: float, operator: str, threshold: float) -> bool:
        """比较值与阈值"""
        if _HAS_CYTHON:
            return check_condition_fast(value, operator, threshold)
        if operator == ">":
            return value > threshold
        elif operator == ">=":
            return value >= threshold
        elif operator == "<":
            return value < threshold
        elif operator == "<=":
            return value <= threshold
        elif operator == "==":
            return abs(value - threshold) < 1e-9
        elif operator == "!=":
            return abs(value - threshold) >= 1e-9
        return False

    @staticmethod
    def _eval_script(script: str, point_values: dict[str, float]) -> bool:
        """在安全沙箱中执行脚本规则"""
        try:
            from RestrictedPython import compile_restricted, safe_globals
            from RestrictedPython.Eval import default_guarded_getitem
            from RestrictedPython.Guards import safer_getattr

            safe_locals = {
                "__builtins__": safe_globals,
                "_getitem_": default_guarded_getitem,
                "_getattr_": safer_getattr,
                "_getiter_": iter,
                "abs": abs,
                "min": min,
                "max": max,
                "sum": sum,
                "len": len,
                "round": round,
                "sorted": sorted,
                "point_values": point_values,
            }

            code = compile_restricted(script, filename="<rule_script>", mode="exec")
            if code is None:
                return False

            exec(code, safe_locals, safe_locals)
            result = safe_locals.get("result", False)
            return bool(result)
        except ImportError:
            logger.error(
                "RestrictedPython未安装，脚本规则不可用，请执行: pip install RestrictedPython"
            )
            return False
        except Exception as e:
            logger.warning("脚本规则执行失败: %s", e)
            return False

    async def _fire_alarm(self, rule: dict, trigger_value: dict) -> None:
        """触发告警"""
        rule_id = rule["rule_id"]
        device_id = rule["device_id"]
        severity = rule["severity"]

        # FIXED: 原问题-告警收敛查询和创建无异常保护，数据库异常导致评估循环崩溃
        try:
            existing = await self._alarm_repo.get_firing_by_rule_device(rule_id, device_id)
            if existing:
                await self._alarm_repo.update_trigger_count(existing["alarm_id"], trigger_value)
                logger.debug("告警收敛: %s 已有firing告警，更新触发次数", rule_id)
                return

            alarm = await self._alarm_repo.create(
                {
                    "rule_id": rule_id,
                    "device_id": device_id,
                    "severity": severity,
                    "trigger_value": trigger_value,
                }
            )
        except Exception as e:
            logger.error("告警创建/更新失败 %s/%s: %s", rule_id, device_id, e)
            return

        # 发布告警事件
        alarm_event = AlarmEvent(
            alarm_id=alarm["alarm_id"],
            rule_id=rule_id,
            device_id=device_id,
            severity=severity,
            action="firing",
            trigger_value=trigger_value,
        )
        await self._event_bus.publish(alarm_event)
        logger.info(
            "告警触发: %s (规则=%s, 设备=%s, 级别=%s)",
            alarm["alarm_id"],
            rule_id,
            device_id,
            severity,
        )

    async def _recover_alarm(self, alarm_id: str, rule: dict) -> None:
        """恢复告警"""
        # FIXED: 原问题-告警恢复数据库操作无异常保护
        try:
            alarm = await self._alarm_repo.recover(alarm_id)
        except Exception as e:
            logger.error("告警恢复失败 %s: %s", alarm_id, e)
            return
        if alarm:
            alarm_event = AlarmEvent(
                alarm_id=alarm_id,
                rule_id=rule["rule_id"],
                device_id=rule["device_id"],
                severity=rule["severity"],
                action="recovered",
            )
            await self._event_bus.publish(alarm_event)
            logger.info("告警恢复: %s", alarm_id)
