"""规则评估器 - 订阅测点更新事件，评估规则条件"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime

from edgelite.constants import _POINT_VALUE_CACHE_MAX, _POINT_VALUE_CACHE_TTL, _RULE_CACHE_TTL
from edgelite.engine.event_bus import AlarmEvent, EventBus, PointUpdateEvent
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

    def __init__(
        self, event_bus: EventBus, rule_repo: RuleRepo, alarm_repo: AlarmRepo, ai_engine=None, device_repo=None
    ):
        self._event_bus = event_bus
        self._rule_repo = rule_repo
        self._alarm_repo = alarm_repo
        self._ai_engine = ai_engine
        self._device_repo = device_repo
        self._device_name_cache: dict[str, str] = {}
        # 持续时间窗口追踪: (rule_id, device_id) -> first_match_time
        self._duration_tracker: dict[tuple[str, str], datetime] = {}
        # 规则缓存: cache_key -> rules
        self._rule_cache: dict[str, list] = {}
        self._cache_time: float = 0.0
        self._cache_ttl: float = _RULE_CACHE_TTL
        self._cache_generation: int = (
            0  # FIXED-BugR4X: 缓存世代标记，invalidate_cache递增，回填时校验以避免in-flight查询回填陈旧数据
        )
        self._task: asyncio.Task | None = None
        self._point_value_cache: dict[str, tuple[float, float]] = {}
        self._point_cache_ttl: float = _POINT_VALUE_CACHE_TTL
        self._point_cache_max_size: int = _POINT_VALUE_CACHE_MAX
        self._tracker_cleanup_interval: float = 600.0  # FIXED: P2-3 duration_tracker无限增长，每10分钟清理过期条目
        self._recent_firings: dict[str, float] = {}  # FIXED-P0: 规则循环触发保护，记录rule_id最近触发时间
        self._recent_firings_max: int = 10000  # FIXED-P1: _recent_firings上限，防止长期运行内存泄漏
        self._min_firing_interval: float = 5.0  # 同一规则两次触发的最小间隔秒数
        self._last_values: dict[str, float] = {}  # 死区过滤: point_key -> last_value
        self._condition_first_met: dict[str, float] = {}  # 条件持续时间追踪: condition_key -> first_met_time
        self._state_lock = asyncio.Lock()  # FIXED-P1: 共享状态并发保护（_duration_tracker/_rule_cache/_recent_firings/_last_values/_condition_first_met/_point_value_cache）  # noqa: E501

    # FIXED(一般): 原问题-同步版_get_device_name是死代码且实现有缺陷（同步上下文无法await）;
    # 修复-删除同步版，统一使用异步版_resolve_device_name

    async def _resolve_device_name(self, device_id: str) -> str:
        """异步获取设备名称，带缓存"""
        if device_id in self._device_name_cache:
            return self._device_name_cache[device_id]
        if self._device_repo:
            try:
                # FIXED-BugR4X: 原问题-DeviceRepo只有get方法没有get_by_id，调用会抛AttributeError；修复-改为get
                device = await self._device_repo.get(device_id)
                if device and device.get("name"):
                    self._device_name_cache[device_id] = device["name"]
                    return device["name"]
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("异步获取设备名称失败: %s", e)
        return device_id

    async def start(self) -> None:
        """启动评估器"""
        queue = await self._event_bus.subscribe("rule_evaluator")
        self._task = asyncio.create_task(self._eval_loop(queue), name="rule-evaluator")
        accel = "Cython accelerated" if _HAS_CYTHON else "pure Python"
        logger.info("Rule evaluator started (%s)", accel)  # FIXED-P3: 中文日志→英文

    async def _get_rules_for_point(self, device_id: str, point_name: str) -> list:
        now = time.time()
        cache_key = f"{device_id}:{point_name}"

        async with self._state_lock:  # FIXED-P1: _rule_cache读写加锁，与invalidate_cache互斥
            if (now - self._cache_time) >= self._cache_ttl:
                self._rule_cache.clear()
                self._cache_time = now

            if cache_key in self._rule_cache:
                return self._rule_cache[cache_key]
            # FIXED-BugR4X: 原问题-invalidate_cache后已in-flight的DB查询结果会回填陈旧数据；修复-记录当前世代，回填时校验世代是否变化  # noqa: E501
            generation = self._cache_generation

        try:
            rules = await self._rule_repo.list_enabled_by_point(device_id, point_name)
        except Exception as e:
            logger.error("Query rules failed %s/%s: %s", device_id, point_name, e)
            rules = []
        async with self._state_lock:  # FIXED-P1: _rule_cache写入加锁
            # FIXED-BugR4X: 校验世代，若DB查询期间invalidate_cache已递增世代则丢弃结果，避免竞态回填陈旧数据
            if generation == self._cache_generation:
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
        self._last_values.clear()
        self._condition_first_met.clear()
        self._recent_firings.clear()  # FIXED-P2: 清理recent_firings防止重启后状态残留
        self._point_value_cache.clear()  # FIXED-P2: 清理point_value_cache
        logger.info("Rule evaluator stopped")  # FIXED-P3: 中文日志→英文

    async def cleanup_duration_tracker(
        self, rule_id: str
    ) -> None:  # FIXED-P1: 改为async加锁，_duration_tracker并发修改保护
        async with self._state_lock:
            keys_to_remove = [k for k in self._duration_tracker if k[0] == rule_id]
            for k in keys_to_remove:
                del self._duration_tracker[k]
            # R6-S-06: 同时清理 _condition_first_met 中以 rule_id: 为前缀的键，防止规则删除后内存泄漏
            prefix = f"{rule_id}:"
            cond_keys_to_remove = [k for k in self._condition_first_met if k.startswith(prefix)]
            for k in cond_keys_to_remove:
                del self._condition_first_met[k]

    async def invalidate_cache(
        self, device_id: str | None = None, point_name: str | None = None
    ) -> None:  # FIXED-P1: 改为async加锁，_rule_cache并发修改保护
        async with self._state_lock:
            # FIXED-BugR4X: 原问题-invalidate_cache后已in-flight的DB查询结果会回填陈旧数据；修复-递增缓存世代，使回填时校验失败而丢弃  # noqa: E501
            self._cache_generation += 1
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
                    await self._prune_duration_tracker()
                    last_tracker_cleanup = now
            except TimeoutError:
                now = time.monotonic()
                if now - last_tracker_cleanup >= self._tracker_cleanup_interval:
                    await self._prune_duration_tracker()
                    last_tracker_cleanup = now
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Eval loop error: %s", e)

    async def _prune_duration_tracker(self) -> None:
        """FIXED: P2-3 清理过期的duration_tracker条目，防止内存无限增长

        Async method with lock protection to prevent TOCTOU race conditions
        with concurrent access from _evaluate_rule/_eval_loop.
        """
        async with self._state_lock:
            cutoff = datetime.now(UTC).timestamp() - 86400
            keys_to_remove = []
            for (rule_id, device_id), first_match_time in list(self._duration_tracker.items()):
                if first_match_time.timestamp() < cutoff:
                    keys_to_remove.append((rule_id, device_id))
            for key in keys_to_remove:
                del self._duration_tracker[key]
            # FIXED-P2: 确保条目数不超过当前活跃规则数的2倍
            active_rule_count = len({k[0] for k in self._duration_tracker})
            max_entries = max(active_rule_count * 2, 100)  # 至少保留100条
            if len(self._duration_tracker) > max_entries:
                sorted_keys = sorted(
                    self._duration_tracker.items(),
                    key=lambda x: x[1].timestamp(),
                )
                excess = len(self._duration_tracker) - max_entries
                for key, _ in sorted_keys[:excess]:
                    del self._duration_tracker[key]
                logger.debug("Pruned %d excess duration_tracker entries (limit=%d)", excess, max_entries)
            if keys_to_remove:
                logger.debug("Pruned %d expired duration_tracker entries", len(keys_to_remove))

    async def _evaluate(self, event: PointUpdateEvent) -> None:
        """评估单个测点更新事件"""
        # FIXED-P1: 加 asyncio.wait_for(timeout=5.0) 防止规则评估长时间阻塞事件循环
        try:
            await asyncio.wait_for(self._evaluate_inner(event), timeout=5.0)
        except TimeoutError:
            logger.warning(
                "[evaluator] code=EVAL_TIMEOUT msg=Rule evaluation timed out after 5.0s for device=%s point=%s",
                event.device_id,
                event.point_name,
            )

    async def _evaluate_inner(self, event: PointUpdateEvent) -> None:
        """评估单个测点更新事件（实际实现）"""
        # 查找关联该设备+测点的所有启用规则
        rules = await self._get_rules_for_point(event.device_id, event.point_name)
        # 按优先级排序（数值越高优先级越高）
        # FIXED: 使用 sorted() 返回新列表，避免原地排序修改 _rule_cache 中的缓存列表
        rules = sorted(rules, key=lambda r: r.get("priority", 0), reverse=True)

        for rule in rules:
            try:
                await self._evaluate_rule(rule, event)
            except Exception as e:
                logger.error(
                    "Rule eval failed: %s - %s", rule.get("rule_id", "?"), e
                )  # FIXED: 原问题-rule["rule_id"]硬访问

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
            logger.warning(
                "Incomplete rule data, skipping: rule_id=%s, device_id=%s", rule_id, device_id
            )  # FIXED-P3: 中文日志→英文
            return

        now = time.time()

        point_values = {event.point_name: event.value}
        cache_key = f"{event.device_id}:{event.point_name}"
        async with self._state_lock:  # FIXED-P1: _point_value_cache并发修改保护
            self._point_value_cache[cache_key] = (event.value, now)
            if len(self._point_value_cache) > self._point_cache_max_size:
                expired_keys = [k for k, (_, t) in self._point_value_cache.items() if (now - t) > self._point_cache_ttl]
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
                async with self._state_lock:  # FIXED-P1: _point_value_cache并发读取保护
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
                                    async with self._state_lock:  # FIXED-P1: _point_value_cache并发修改保护
                                        self._point_value_cache[cond_cache_key] = (latest_val, now)
                    except Exception as e:
                        logger.debug(
                            "Failed to get point value from InfluxDB %s.%s: %s", event.device_id, cond_point, e
                        )  # FIXED-P3: 中文日志→英文

        # 评估条件
        if rule_type == "script" and script:
            # FIXED-P0: 沙箱执行改为 asyncio.to_thread + wait_for(timeout=3s) + setrlimit
            matched = await self._eval_script(script, point_values)
        elif rule_type == "ai_inference":
            matched = await self._evaluate_ai_conditions(conditions, point_values, logic, event.device_id)
        else:
            matched = await self._check_conditions(conditions, point_values, logic, device_id, rule_id)

        tracker_key = (rule_id, device_id)

        if matched:
            if duration > 0:
                fire_alarm = False  # FIXED-P1: 锁内判断，锁外触发，避免_state_lock与event_bus嵌套
                async with self._state_lock:
                    first_time = self._duration_tracker.get(tracker_key)
                    if first_time is None:
                        self._duration_tracker[tracker_key] = datetime.now(UTC)
                    else:
                        elapsed = (datetime.now(UTC) - first_time).total_seconds()
                        if elapsed >= duration:
                            self._duration_tracker.pop(tracker_key, None)
                            fire_alarm = True
                if fire_alarm:
                    await self._fire_alarm(rule, point_values)
            else:
                await self._fire_alarm(rule, point_values)
        else:
            async with self._state_lock:  # FIXED-P1: _duration_tracker并发修改保护
                if tracker_key in self._duration_tracker:
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
                    logger.warning(
                        "Firing alarm missing alarm_id, skip recovery: %s", firing_alarm
                    )  # FIXED-P3: 中文日志→英文
                    return
                await self._recover_alarm(alarm_id, rule)

    async def _check_conditions(
        self,
        conditions: list[dict],
        point_values: dict[str, float],
        logic: str,
        device_id: str = "",
        rule_id: str = "",
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
                # AI推理/死区/持续时间条件需要异步处理，跳过Cython快速路径
                if (
                    cond.get("source") == "ai_inference"
                    or cond.get("dead_zone", 0) > 0
                    or cond.get("duration_seconds", 0) > 0
                ):
                    all_available = False
                    break
                value = point_values.get(point)
                if value is None or not point:
                    all_available = False
                    break
                fast_conds.append({"operator": cond.get("operator", ">"), "threshold": cond.get("threshold", 0)})
            if all_available and fast_conds:
                if logic == "NOT":
                    # FIXED-Bug7: 与非 Cython 路径保持一致：NOT 语义为 not all(results)
                    # 之前：仅取 conditions[0] 单独评估并取反，忽略后续条件
                    values = [point_values[c.get("point", "")] for c in conditions if c.get("point")]
                    for actual, fc in zip(values, fast_conds, strict=False):
                        if not check_condition_fast(actual, fc.get("operator", ">"), fc.get("threshold", 0)):
                            # 任一条件不满足 → all()=False → not all()=True
                            return True
                    # 所有条件均满足 → all()=True → not all()=False
                    return False
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
        for _i, cond in enumerate(conditions):  # FIXED(P3): 原问题-B007循环变量i未使用; 修复-改为_i
            # FIXED: 原问题-cond硬访问可能KeyError，改为.get()
            point = cond.get("point")
            operator = cond.get("operator", ">")
            threshold = cond.get("threshold", 0)
            window_seconds = cond.get("window_seconds", 0)
            aggregate = cond.get("aggregate", "")

            # AI推理结果作为条件输入
            if cond.get("source") == "ai_inference":
                ai_result = await self._get_latest_ai_result(cond.get("model_id", ""))
                value = ai_result.get(cond.get("field", "anomaly_score"), 0)
            # 窗口聚合条件：从流计算引擎或InfluxDB获取聚合值
            elif window_seconds > 0 and aggregate and device_id:
                value = await self._get_window_aggregate(device_id, point, window_seconds, aggregate)
                if value is None:
                    results.append(False)
                    continue
            else:
                value = point_values.get(point)

            if value is None or not point:
                results.append(False)
                continue

            # 死区过滤：变化量小于死区则跳过
            dead_zone = cond.get("dead_zone", 0)
            if dead_zone > 0:
                point_key = f"{device_id}:{point}"
                async with self._state_lock:  # FIXED-P1: _last_values并发读写保护
                    last_value = self._last_values.get(point_key)
                    # FIXED-BugR4X: 原问题-死区内results.append(False)并continue，导致稳定超限值(如温度持续105°C阈值100°C死区5)第二次评估差值0<5被误判为条件不满足；修复-死区内只跳过last_value更新，仍正常评估条件  # noqa: E501
                    if last_value is None or abs(value - last_value) >= dead_zone:
                        self._last_values[point_key] = value

            result = self._compare(value, operator, threshold)

            # 条件持续时间/防抖：条件需持续满足N秒才视为匹配
            if result:
                duration_seconds = cond.get("duration_seconds", 0)
                if duration_seconds > 0 and rule_id:
                    condition_key = f"{rule_id}:{point}:{operator}:{threshold}"  # FIXED-BugR4X: 原问题-用下标i作key，条件顺序变更后防抖状态错乱；修复-改用point:operator:threshold作key  # noqa: E501
                    async with self._state_lock:  # FIXED-P1: _condition_first_met并发修改保护
                        first_met = self._condition_first_met.get(condition_key)
                        if first_met is None:
                            self._condition_first_met[condition_key] = time.monotonic()
                            result = False
                        elif time.monotonic() - first_met < duration_seconds:
                            result = False
            elif rule_id:
                condition_key = f"{rule_id}:{point}:{operator}:{threshold}"  # FIXED-BugR4X: 原问题-用下标i作key，条件顺序变更后防抖状态错乱；修复-改用point:operator:threshold作key  # noqa: E501
                async with self._state_lock:  # FIXED-P1: _condition_first_met并发修改保护
                    self._condition_first_met.pop(condition_key, None)

            results.append(result)

        if not results:
            return False

        if logic == "NOT":
            # FIXED-P2: 原问题-仅取反results[0]忽略后续条件；NOT语义为"所有条件的组合取反"，改为not all(results)
            return not all(results)
        elif logic == "AND":
            return all(results)
        else:  # OR
            return any(results)

    async def _evaluate_ai_conditions(
        self,
        conditions: list[dict],
        point_values: dict[str, float],
        logic: str,
        device_id: str,
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
        # FIXED-Bug8: NOT 语义为 not all(results)，之前误用 any(results)
        if logic == "NOT":
            return not all(results)
        if logic == "AND":
            return all(results)
        return any(results)

    async def _get_latest_ai_result(self, model_id: str) -> dict:
        """获取最新AI推理结果"""
        if not self._ai_engine:
            return {}
        try:
            wrapper = self._ai_engine.get_model(model_id)
            if not wrapper or wrapper.status != "active":
                return {}
            return getattr(wrapper, "last_result", {}) or {}
        except Exception as e:
            logger.debug("Failed to get latest AI result for model %s: %s", model_id, e)
            return {}

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
        elif operator == "==" or operator == "=":
            # FIXED(P2): 原问题-EdgeRuleOperator枚举使用"="表示相等，但纯Python分支仅处理"==";
            # 修复-补充"="分支，与Cython路径(check_condition_fast)行为一致
            return abs(value - threshold) < 1e-9
        elif operator == "!=":
            return abs(value - threshold) >= 1e-9
        return False

    async def _get_window_aggregate(
        self,
        device_id: str,
        point_name: str,
        window_seconds: int,
        aggregate: str,
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
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.debug("从流计算引擎获取窗口聚合值失败: %s", e)

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
                device_id,
                point_name,
                e,
            )

        return None

    @staticmethod
    async def _eval_script(script: str, point_values: dict[str, float]) -> bool:
        """在安全沙箱中执行脚本规则。

        FIXED-P0: 原实现使用 RestrictedPython+exec() 同步阻塞事件循环，无超时和资源限制，
                 恶意脚本可写 while True: pass 卡死整个评估循环。
                 修复：抽取公共沙箱执行器 edgelite.engine.sandbox.run_script_safely，
                 使用 asyncio.to_thread + asyncio.wait_for(timeout=3s) + resource.setrlimit。
        """
        namespace = {"point_values": point_values, "result": False}
        try:
            from edgelite.engine.sandbox import run_script_safely

            result = await run_script_safely(
                script,
                namespace,
                timeout=3.0,
                filename="<rule_script>",
            )
            return bool(result)
        except TimeoutError:
            logger.warning("Script rule execution timed out (3s)")
            return False
        except Exception as e:
            logger.warning("Script rule execution failed: %s", e)
            return False

    async def _fire_alarm(self, rule: dict, trigger_value: dict) -> None:
        """触发告警"""
        rule_id = rule.get("rule_id")
        device_id = rule.get("device_id")
        severity = rule.get("severity", "warning")

        if not rule_id or not device_id:
            logger.warning(
                "Alarm fire failed: incomplete rule data rule_id=%s, device_id=%s", rule_id, device_id
            )  # FIXED-P3: 中文日志→英文
            return

        # FIXED-P0: 规则循环触发保护，同一规则在最小间隔内不重复触发
        now = time.time()
        async with self._state_lock:  # FIXED-P1: _recent_firings并发读写保护+上限控制
            last_fire = self._recent_firings.get(rule_id, 0)
            if now - last_fire < self._min_firing_interval:
                logger.debug("Rule %s in firing cooldown (within %.1fs), skipping", rule_id, self._min_firing_interval)
                return
            self._recent_firings[rule_id] = now
            if len(self._recent_firings) > self._recent_firings_max:  # FIXED-P1: 超限时淘汰最旧条目
                oldest_key = min(self._recent_firings, key=self._recent_firings.get)
                del self._recent_firings[oldest_key]

        # FIXED: 原问题-告警收敛查询和创建无异常保护，数据库异常导致评估循环崩溃
        try:
            existing = await self._alarm_repo.get_firing_by_rule_device(rule_id, device_id)
            if existing:
                # FIXED: 原问题-existing["alarm_id"]硬访问可能KeyError，改为.get()
                existing_alarm_id = existing.get("alarm_id")
                if existing_alarm_id:
                    await self._alarm_repo.update_trigger_count(existing_alarm_id, trigger_value)
                    logger.debug(
                        "Alarm dedup: %s already has firing alarm, updating trigger count", rule_id
                    )  # FIXED-P3: 中文日志→英文
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
        except asyncio.CancelledError:
            # R6-S-23: 超时取消(CancelledError)继承自 BaseException，不被 except Exception 捕获，
            # 需独立处理并回滚 _recent_firings，否则该规则在 _min_firing_interval 内的后续告警被错误丢弃
            logger.warning("Alarm fire cancelled (timeout) %s/%s, rolling back _recent_firings", rule_id, device_id)
            async with self._state_lock:
                if self._recent_firings.get(rule_id) == now:
                    del self._recent_firings[rule_id]
            raise
        except Exception as e:
            # FIXED(严重): 原问题-DB 失败时 _recent_firings[rule_id] 已设置但未回滚，
            # 导致该规则在 _min_firing_interval（5秒）内的后续告警被错误丢弃，形成告警黑洞
            # 修复：DB 失败时回滚 _recent_firings，仅当仍是本次设置的值才回滚避免误删后续成功的标记
            logger.error("Alarm create/update failed %s/%s: %s", rule_id, device_id, e, exc_info=True)
            async with self._state_lock:
                if self._recent_firings.get(rule_id) == now:
                    del self._recent_firings[rule_id]
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
            rule_name=rule.get("name", rule_id),
            device_id=device_id,
            device_name=await self._resolve_device_name(device_id),
            severity=severity,
            action="firing",
            trigger_value=trigger_value,
            rule_type=rule.get("rule_type", "threshold"),
        )
        # FIXED(严重): 原问题-超时取消(CancelledError)若发生在 create 之后、publish 之前，
        # 告警已写入 DB 但事件未发布，导致无通知/无升级/无统计的孤儿告警
        # 修复-捕获 CancelledError，用 asyncio.shield 保护 publish 完成，然后重新抛出
        try:
            await self._event_bus.publish(alarm_event)
        except asyncio.CancelledError:
            logger.warning("Evaluator cancelled after alarm %s created, shielded publish attempt", alarm_id)
            # FIXED(严重): 原问题-contextlib.suppress(Exception) 吞掉所有异常，
            # shield 内 publish 失败时形成孤儿告警（DB 有记录但无通知/无升级/无统计）
            # 修复：记录错误日志便于后续补偿，不再静默吞没
            try:
                await asyncio.shield(self._event_bus.publish(alarm_event))
            except Exception as shield_err:
                logger.error(
                    "Shielded publish also failed for alarm %s, orphan alarm risk "
                    "(manual intervention may be required): %s",
                    alarm_id,
                    shield_err,
                    exc_info=True,
                )
            raise
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
                rule_name=rule.get("name", rule.get("rule_id", "")),
                device_id=rule.get("device_id", ""),
                device_name=await self._resolve_device_name(rule.get("device_id", "")),
                severity=rule.get("severity", "info"),
                action="recovered",
                rule_type=rule.get("rule_type", "threshold"),
            )
            await self._event_bus.publish(alarm_event)
            logger.info("Alarm recovered: %s", alarm_id)  # FIXED-P3: 中文日志→英文
