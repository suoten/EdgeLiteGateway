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

    def __init__(self, event_bus: EventBus, rule_repo: RuleRepo, alarm_repo: AlarmRepo, ai_engine=None):
        self._event_bus = event_bus
        self._rule_repo = rule_repo
        self._alarm_repo = alarm_repo
        self._ai_engine = ai_engine
        # 持续时间窗口追踪: (rule_id, device_id) -> first_match_time
        self._duration_tracker: dict[tuple[str, str], datetime] = {}
        # 规则缓存: cache_key -> rules
        self._rule_cache: dict[str, list] = {}
        self._cache_time: float = 0.0
        self._cache_ttl: float = _RULE_CACHE_TTL
        self._task: asyncio.Task | None = None
        self._point_value_cache: dict[str, tuple[float, float]] = {}
        self._point_cache_ttl: float = _POINT_VALUE_CACHE_TTL
        self._point_cache_max_size: int = _POINT_VALUE_CACHE_MAX
        self._tracker_cleanup_interval: float = 600.0  # FIXED: P2-3 duration_tracker无限增长，每10分钟清理过期条目
        self._recent_firings: dict[str, float] = {}  # FIXED-P0: 规则循环触发保护，记录rule_id最近触发时间
        self._min_firing_interval: float = 5.0  # 同一规则两次触发的最小间隔秒数

    async def start(self) -> None:
        """启动评估器"""
        queue = self._event_bus.subscribe("rule_evaluator")
        self._task = asyncio.create_task(self._eval_loop(queue), name="rule-evaluator")
        accel = "Cython accelerated" if _HAS_CYTHON else "pure Python"
        logger.info("Rule evaluator started (%s)", accel)  # FIXED-P3: 中文日志→英文

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
            logger.error("Query rules failed %s/%s: %s", device_id, point_name, e)  # FIXED-P3: 中文日志→英文
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
        logger.info("Rule evaluator stopped")  # FIXED-P3: 中文日志→英文

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
        last_tracker_cleanup = time.monotonic()
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                if isinstance(event, PointUpdateEvent) and event.quality == "good":
                    await self._evaluate(event)
                # FIXED: P2-3 定期清理duration_tracker，防止随运行时间无限增长
                now = time.monotonic()
                if now - last_tracker_cleanup >= self._tracker_cleanup_interval:
                    self._prune_duration_tracker()
                    last_tracker_cleanup = now
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now - last_tracker_cleanup >= self._tracker_cleanup_interval:
                    self._prune_duration_tracker()
                    last_tracker_cleanup = now
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Eval loop error: %s", e)

    def _prune_duration_tracker(self) -> None:
        """FIXED: P2-3 清理过期的duration_tracker条目，防止内存无限增长"""
        cutoff = datetime.now(UTC).timestamp() - 3600
        keys_to_remove = []
        for (rule_id, device_id), first_match_time in list(self._duration_tracker.items()):
            if first_match_time.timestamp() < cutoff:
                keys_to_remove.append((rule_id, device_id))
        for key in keys_to_remove:
            del self._duration_tracker[key]
        if keys_to_remove:
            logger.debug("Pruned %d expired duration_tracker entries", len(keys_to_remove))

    async def _evaluate(self, event: PointUpdateEvent) -> None:
        """评估单个测点更新事件"""
        # 查找关联该设备+测点的所有启用规则
        rules = await self._get_rules_for_point(event.device_id, event.point_name)

        for rule in rules:
            try:
                await self._evaluate_rule(rule, event)
            except Exception as e:
                logger.error("Rule eval failed: %s - %s", rule.get("rule_id", "?"), e)  # FIXED: 原问题-rule["rule_id"]硬访问

    async def _evaluate_rule(self, rule: dict, event: PointUpdateEvent) -> None:
        """评估单条规则"""
        # FIXED: 原问题-字典硬访问rule["key"]可能KeyError，改为.get()加校验
        rule_id = rule.get("rule_id")
        device_id = rule.get("device_id")
        conditions = rule.get("conditions", [])
        logic = rule.get("logic", "AND")
        duration = rule.get("duration", 0)
        rule_type = rule.get("rule_type", "threshold")
        script = rule.get("script", "")

        if not rule_id or not device_id or not conditions:
            logger.warning("Incomplete rule data, skipping: rule_id=%s, device_id=%s", rule_id, device_id)  # FIXED-P3: 中文日志→英文
            return

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
            # FIXED: 原问题-cond["point"]硬访问可能KeyError，改为.get()
            cond_point = cond.get("point")
            if not cond_point:
                continue
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
                            "Failed to get point value from InfluxDB %s.%s: %s", event.device_id, cond_point, e
                        )  # FIXED-P3: 中文日志→英文

        # 评估条件
        if rule_type == "script" and script:
            matched = self._eval_script(script, point_values)
        elif rule_type == "ai_inference":
            matched = await self._evaluate_ai_conditions(conditions, point_values, logic, event.device_id)
        else:
            matched = await self._check_conditions(conditions, point_values, logic, device_id)

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
                logger.error("Query firing alarm failed %s/%s: %s", rule_id, device_id, e)  # FIXED-P3: 中文日志→英文
                firing_alarm = None
            if firing_alarm:
                alarm_id = firing_alarm.get("alarm_id")  # FIXED: 原问题-硬访问alarm_id可能KeyError
                if alarm_id is None:
                    logger.warning("Firing alarm missing alarm_id, skip recovery: %s", firing_alarm)  # FIXED-P3: 中文日志→英文
                    return
                await self._recover_alarm(alarm_id, rule)

    async def _check_conditions(
        self, conditions: list[dict], point_values: dict[str, float], logic: str,
        device_id: str = "",
    ) -> bool:
        """检查条件组合

        Args:
            conditions: 条件列表
            point_values: 当前测点值映射
            logic: 逻辑组合 (AND/OR)
            device_id: 设备ID，用于窗口聚合查询
        """
        if _HAS_CYTHON:
            fast_conds = []
            all_available = True
            for cond in conditions:
                # FIXED: 原问题-cond硬访问可能KeyError，改为.get()
                point = cond.get("point")
                # 窗口聚合条件需要异步查询，跳过Cython快速路径
                if cond.get("window_seconds", 0) > 0 and cond.get("aggregate"):
                    all_available = False
                    break
                value = point_values.get(point)
                if value is None or not point:
                    all_available = False
                    break
                fast_conds.append({"operator": cond.get("operator", ">"), "threshold": cond.get("threshold", 0)})
            if all_available and fast_conds:
                values = [point_values[c.get("point", "")] for c in conditions if c.get("point")]
                for actual, fc in zip(values, fast_conds, strict=False):
                    if not check_condition_fast(actual, fc.get("operator", ">"), fc.get("threshold", 0)):
                        if logic == "AND":
                            return False
                    else:
                        if logic == "OR":
                            return True
                return logic == "AND"

        results = []
        for cond in conditions:
            # FIXED: 原问题-cond硬访问可能KeyError，改为.get()
            point = cond.get("point")
            operator = cond.get("operator", ">")
            threshold = cond.get("threshold", 0)
            window_seconds = cond.get("window_seconds", 0)
            aggregate = cond.get("aggregate", "")

            # 窗口聚合条件：从流计算引擎或InfluxDB获取聚合值
            if window_seconds > 0 and aggregate and device_id:
                value = await self._get_window_aggregate(
                    device_id, point, window_seconds, aggregate
                )
                if value is None:
                    results.append(False)
                    continue
            else:
                value = point_values.get(point)

            if value is None or not point:
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

    async def _evaluate_ai_conditions(
        self, conditions: list[dict], point_values: dict[str, float], logic: str, device_id: str,
    ) -> bool:
        """评估AI推理条件"""
        if not self._ai_engine:
            logger.warning("AI engine unavailable, skipping AI condition evaluation")
            return False
        results = []
        for cond in conditions:
            model_id = cond.get("model_id")
            ai_threshold = cond.get("ai_threshold", 0.5)
            if not model_id:
                results.append(False)
                continue
            try:
                # 获取模型期望的输入维度，将point_values填充到所需长度
                wrapper = self._ai_engine.get_model(model_id)
                if not wrapper or wrapper.status != "active":
                    results.append(False)
                    continue

                input_values = list(point_values.values())
                if not input_values:
                    results.append(False)
                    continue

                # 根据模型输入schema填充/截断数据到期望长度
                input_shape = wrapper.input_schema.get("shape", [1, -1])
                expected_len = input_shape[-1] if len(input_shape) >= 2 and input_shape[-1] > 0 else -1
                if expected_len > 0:
                    if len(input_values) >= expected_len:
                        input_values = input_values[:expected_len]
                    else:
                        # 数据不足时，用最后一个值重复填充
                        last_val = input_values[-1]
                        input_values = input_values + [last_val] * (expected_len - len(input_values))

                result = await self._ai_engine.infer(model_id, input_values)
                if result.status == "success":
                    score = result.output_data.get("output_0", [0])
                    if isinstance(score, list) and score:
                        score = score[0]
                    results.append(score > ai_threshold)
                else:
                    results.append(False)
            except Exception as e:
                logger.warning("AI condition evaluation failed: model_id=%s, %s", model_id, e)
                results.append(False)
        if not results:
            return False
        return all(results) if logic == "AND" else any(results)

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

    async def _get_window_aggregate(
        self, device_id: str, point_name: str, window_seconds: int, aggregate: str,
    ) -> float | None:
        """获取窗口聚合值（优先从流计算引擎缓存，回退到InfluxDB）

        Args:
            device_id: 设备ID
            point_name: 测点名称
            window_seconds: 滑动窗口秒数
            aggregate: 聚合函数 (avg/sum/min/max/count/std)

        Returns:
            聚合值，无结果时返回 None
        """
        # 优先从流计算引擎获取缓存结果
        try:
            from edgelite.engine.stream_compute import get_stream_engine
            engine = get_stream_engine()
            result = engine.get_window_result(device_id, point_name, window_seconds, aggregate)
            if result is not None:
                return result
        except Exception:
            pass

        # 回退到InfluxDB查询
        try:
            from edgelite.app import _app_state
            if _app_state.influx_storage:
                data = await _app_state.influx_storage.query_points(
                    device_id=device_id,
                    point_name=point_name,
                    start=f"-{window_seconds}s",
                    aggregate=f"{window_seconds}s",
                )
                if data:
                    last_item = data[-1]
                    val = last_item.get("value")
                    if isinstance(val, (int, float)):
                        return float(val)
        except Exception as e:
            logger.debug(
                "Failed to get window aggregate from InfluxDB %s.%s: %s",
                device_id, point_name, e,
            )

        return None

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
                "RestrictedPython not installed, script rules unavailable, run: pip install RestrictedPython"
            )  # FIXED-P3: 中文日志→英文
            return False
        except Exception as e:
            logger.warning("Script rule execution failed: %s", e)  # FIXED-P3: 中文日志→英文
            return False

    async def _fire_alarm(self, rule: dict, trigger_value: dict) -> None:
        """触发告警"""
        rule_id = rule.get("rule_id")
        device_id = rule.get("device_id")
        severity = rule.get("severity", "warning")

        if not rule_id or not device_id:
            logger.warning("Alarm fire failed: incomplete rule data rule_id=%s, device_id=%s", rule_id, device_id)  # FIXED-P3: 中文日志→英文
            return

        # FIXED-P0: 规则循环触发保护，同一规则在最小间隔内不重复触发
        now = time.time()
        last_fire = self._recent_firings.get(rule_id, 0)
        if now - last_fire < self._min_firing_interval:
            logger.debug("Rule %s in firing cooldown (within %.1fs), skipping", rule_id, self._min_firing_interval)  # FIXED-P3: 中文日志→英文
            return
        self._recent_firings[rule_id] = now

        # FIXED: 原问题-告警收敛查询和创建无异常保护，数据库异常导致评估循环崩溃
        try:
            existing = await self._alarm_repo.get_firing_by_rule_device(rule_id, device_id)
            if existing:
                # FIXED: 原问题-existing["alarm_id"]硬访问可能KeyError，改为.get()
                existing_alarm_id = existing.get("alarm_id")
                if existing_alarm_id:
                    await self._alarm_repo.update_trigger_count(existing_alarm_id, trigger_value)
                    logger.debug("Alarm dedup: %s already has firing alarm, updating trigger count", rule_id)  # FIXED-P3: 中文日志→英文
                return

            alarm = await self._alarm_repo.create(
                {
                    "rule_id": rule_id,
                    "device_id": device_id,
                    "severity": severity,
                    "trigger_value": trigger_value,
                    "rule_type": rule.get("rule_type", "threshold"),
                }
            )
        except Exception as e:
            logger.error("Alarm create/update failed %s/%s: %s", rule_id, device_id, e)  # FIXED-P3: 中文日志→英文
            return

        # 发布告警事件
        # FIXED: 原问题-alarm可能为None或缺少alarm_id，加空值保护
        if not alarm:
            logger.warning("Alarm create returned None: rule_id=%s", rule_id)  # FIXED-P3: 中文日志→英文
            return
        alarm_id = alarm.get("alarm_id")
        if not alarm_id:
            logger.warning("Alarm create result missing alarm_id: rule_id=%s", rule_id)  # FIXED-P3: 中文日志→英文
            return

        alarm_event = AlarmEvent(
            alarm_id=alarm_id,
            rule_id=rule_id,
            device_id=device_id,
            severity=severity,
            action="firing",
            trigger_value=trigger_value,
            rule_type=rule.get("rule_type", "threshold"),
        )
        await self._event_bus.publish(alarm_event)
        logger.info(
            "Alarm fired: %s (rule=%s, device=%s, severity=%s)",
            alarm_id,
            rule_id,
            device_id,
            severity,
        )  # FIXED-P3: 中文日志→英文

    async def _recover_alarm(self, alarm_id: str, rule: dict) -> None:
        """恢复告警"""
        # FIXED: 原问题-告警恢复数据库操作无异常保护
        try:
            alarm = await self._alarm_repo.recover(alarm_id)
        except Exception as e:
            logger.error("Alarm recovery failed %s: %s", alarm_id, e)  # FIXED-P3: 中文日志→英文
            return
        if alarm:
            alarm_event = AlarmEvent(
                alarm_id=alarm_id,
                rule_id=rule.get("rule_id", ""),
                device_id=rule.get("device_id", ""),
                severity=rule.get("severity", "info"),
                action="recovered",
                rule_type=rule.get("rule_type", "threshold"),
            )
            await self._event_bus.publish(alarm_event)
            logger.info("Alarm recovered: %s", alarm_id)  # FIXED-P3: 中文日志→英文
