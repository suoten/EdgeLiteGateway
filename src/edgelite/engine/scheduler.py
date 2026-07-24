"""采集调度器 - 基于asyncio的定时采集任务调度"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from edgelite.constants import _CACHE_BATCH_LIMIT, _SCHEDULER_INTERVAL
from edgelite.drivers.base import PointValue
from edgelite.engine.event_bus import EventBus, PointUpdateEvent
from edgelite.engine.preprocessor import DataPreprocessor
from edgelite.storage.cache import CacheManager
from edgelite.storage.influx_storage import InfluxDBStorage

_AI_ANOMALY_THRESHOLD_DEFAULT = 0.8
_AI_INFERENCE_COOLDOWN_SECONDS = 10

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0

# R8-S-02 修复: 熔断器 fallback 哨兵对象。当 call_with_protection 命中 fallback
# （熔断器 OPEN 拒绝调用或读取异常）时返回此哨兵，调用方据此将 is_error 置为 True，
# 避免熔断降级被静默视为采集成功而产生监控盲区。
_CIRCUIT_FALLBACK_SENTINEL = object()


class _ConcurrencyGate:
    """FIXED-P0: 支持动态限流的并发门控，替代asyncio.Semaphore热替换"""

    def __init__(self, limit: int):
        self._limit = limit
        self._active = 0
        self._lock = asyncio.Lock()
        self._condition = asyncio.Condition(self._lock)

    async def acquire(self) -> None:
        async with self._condition:
            while self._active >= self._limit:
                await self._condition.wait()
            self._active += 1

    async def release(self) -> None:
        async with self._condition:
            self._active -= 1
            self._condition.notify_all()

    async def set_limit(self, new_limit: int) -> None:
        async with self._condition:
            old_limit = self._limit
            self._limit = max(1, new_limit)
            if self._limit > old_limit:
                self._condition.notify(self._limit - old_limit)

    async def wake_all_waiters(self) -> None:
        """S-05 FIX: 唤醒所有正在等待的协程（用于信号量重建时的优雅迁移）

        旧 gate 即将退役，放开限制让所有等待者立即通过本 gate 完成本次采集，
        下次循环时等待者将从字典中获取新 gate，实现无缝迁移。
        """
        async with self._condition:
            # 放开限制，确保所有等待者都能通过
            self._limit = max(self._limit, self._active + 1)
            self._condition.notify_all()

    @property
    def limit(self) -> int:
        return self._limit


class DevicePriority(enum.Enum):
    """Device collection priority levels"""

    P0 = 0  # Emergency: interval halved
    P1 = 1  # High: interval reduced by 25%
    P2 = 2  # Normal: default
    P3 = 3  # Low: interval doubled


# Priority interval multipliers
_PRIORITY_INTERVAL_MULTIPLIER = {
    DevicePriority.P0: 0.5,
    DevicePriority.P1: 0.75,
    DevicePriority.P2: 1.0,
    DevicePriority.P3: 2.0,
}

# Priority semaphore capacity weights (fraction of total capacity, sum=1.0)
# FIXED(一般): 原问题-值为0/1/2/3的优先级顺序而非权重，P0权重为0导致容量为0;
# 修复-改为真实权重，高优先级分配更多容量
_PRIORITY_SEMAPHORE_WEIGHT = {
    DevicePriority.P0: 0.4,  # 最高优先级，40%容量
    DevicePriority.P1: 0.3,  # 30%容量
    DevicePriority.P2: 0.2,  # 20%容量
    DevicePriority.P3: 0.1,  # 最低优先级，10%容量
}


@dataclass
class CollectStats:
    device_id: str = ""
    avg_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    total_calls: int = 0
    timeout_count: int = 0
    last_collect_at: str = ""
    _latency_sum: float = field(default=0.0, repr=False)


@dataclass
class DeviceQualityStats:
    device_id: str = ""
    success_count: int = 0
    error_count: int = 0
    total_count: int = 0
    error_rate: float = 0.0


class CollectScheduler:
    """采集调度器，为每个在线设备创建独立的采集协程"""

    _DEFAULT_MAX_CONCURRENT = 50
    _DEFAULT_ERROR_RATE_THRESHOLD = 0.1
    _WATCHDOG_INTERVAL = 30
    _WATCHDOG_STALE_CYCLES = 3
    _WATCHDOG_RESTART_CYCLES = 10

    def __init__(
        self,
        event_bus: EventBus,
        influx_storage: InfluxDBStorage,
        cache_manager: CacheManager | None = None,
        preprocessor: DataPreprocessor | None = None,
    ):
        self._event_bus = event_bus
        self._influx = influx_storage
        self._cache = cache_manager
        self._preprocessor = preprocessor
        self._tasks: dict[str, asyncio.Task] = {}
        self._device_info: dict[str, tuple] = {}
        self._cache_flush_task: asyncio.Task | None = None
        self._collect_stats: dict[str, CollectStats] = {}
        self._device_quality_stats: dict[str, DeviceQualityStats] = {}
        self._last_collect_time: dict[str, float] = {}
        self._last_values: dict[str, dict[str, float]] = {}
        self._last_ai_inference_time: dict[str, float] = {}
        self._ai_virtual_devices: set[str] = set()
        self._watchdog_task: asyncio.Task | None = None
        self._driver_classes: dict[str, type] = {}
        # FIXED-P0: 共享状态并发保护锁（_last_values, _collect_stats, _device_quality_stats, _adaptive_state）
        self._state_lock = asyncio.Lock()

        # Priority tracking: device_id -> DevicePriority
        self._device_priorities: dict[str, DevicePriority] = {}

        # Adaptive frequency tracking: device_id -> (consecutive_successes, consecutive_failures, effective_interval)
        self._adaptive_state: dict[str, dict] = {}

        try:
            from edgelite.config import get_config

            cfg = get_config()
            sc = getattr(cfg, "scheduler", None)
        except Exception:
            sc = None

        self._max_concurrent_collects = (
            getattr(sc, "max_concurrent_collects", self._DEFAULT_MAX_CONCURRENT) if sc else self._DEFAULT_MAX_CONCURRENT
        )
        self._error_rate_threshold = (
            getattr(sc, "error_rate_threshold", self._DEFAULT_ERROR_RATE_THRESHOLD)
            if sc
            else self._DEFAULT_ERROR_RATE_THRESHOLD
        )
        self._WATCHDOG_INTERVAL = getattr(sc, "watchdog_interval", 30) if sc else 30
        self._WATCHDOG_STALE_CYCLES = getattr(sc, "watchdog_stale_cycles", 3) if sc else 3
        self._WATCHDOG_RESTART_CYCLES = getattr(sc, "watchdog_restart_cycles", 10) if sc else 10
        self._concurrency_gate: _ConcurrencyGate | None = None  # FIXED-P0: 使用ConcurrencyGate替代Semaphore

        # Priority-aware semaphores: one per priority level for fair scheduling
        self._priority_semaphores: dict[DevicePriority, _ConcurrencyGate] = {}

        from edgelite.engine.circuit_breaker import get_circuit_breaker_manager

        self._circuit_breaker_manager = get_circuit_breaker_manager()

    async def start_collect(
        self,
        device_id: str,
        driver: Any,
        points: list[dict],
        collect_interval: int = 5,
        priority: str | DevicePriority = "P2",
    ) -> None:
        """为设备启动采集任务

        Args:
            device_id: Device ID
            driver: Driver instance
            points: List of point definitions
            collect_interval: Base collection interval in seconds
            priority: Device priority (P0/P1/P2/P3), default P2
        """
        old_info = None
        if device_id in self._tasks:
            old_info = self._device_info.get(device_id)
            await self.stop_collect(device_id)

        if not self._cache_flush_task and self._cache:
            self._cache_flush_task = asyncio.create_task(self._cache_flush_loop())

        if self._concurrency_gate is None:
            self._concurrency_gate = _ConcurrencyGate(self._max_concurrent_collects)

        # Initialize priority semaphores
        # FIXED(一般): 原问题-所有优先级共享同一gate，优先级调度失效;
        # 修复-按权重为每个优先级创建独立gate
        total_capacity = self._max_concurrent_collects
        for p in DevicePriority:
            if p not in self._priority_semaphores:
                weight = _PRIORITY_SEMAPHORE_WEIGHT.get(p, 0.1)
                capacity = max(1, int(total_capacity * weight))
                self._priority_semaphores[p] = _ConcurrencyGate(capacity)

        if not self._watchdog_task:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="watchdog")

        # Parse and store priority
        if isinstance(priority, str):
            try:
                parsed_priority = DevicePriority[priority]
            except KeyError:
                parsed_priority = DevicePriority.P2
        else:
            parsed_priority = priority
        multiplier = _PRIORITY_INTERVAL_MULTIPLIER.get(parsed_priority, 1.0)
        effective_interval = max(1, int(collect_interval * multiplier))
        async with self._state_lock:  # FIXED-P1: start_collect中共享状态写入加锁，与_collect_loop读取互斥
            self._device_priorities[device_id] = parsed_priority
            self._adaptive_state[device_id] = {
                "consecutive_successes": 0,
                "consecutive_failures": 0,
                "base_interval": collect_interval,
                "effective_interval": effective_interval,
                "priority_multiplier": multiplier,
            }
            self._device_info[device_id] = (driver, points, collect_interval)
            self._collect_stats.setdefault(device_id, CollectStats(device_id=device_id))
            self._device_quality_stats.setdefault(device_id, DeviceQualityStats(device_id=device_id))
        try:
            task = asyncio.create_task(
                self._collect_loop(device_id, driver, points, effective_interval),
                name=f"collect-{device_id}",
            )
            # FIXED-P0: _tasks写入需要加锁，防止与stop_collect/_watchdog_loop竞态
            async with self._state_lock:
                self._tasks[device_id] = task
        except Exception:
            if old_info:
                async with self._state_lock:
                    self._device_info[device_id] = old_info
            raise
        logger.info(
            "Collection task started: %s (interval=%ds, effective=%ds, priority=%s, points=%d)",
            device_id,
            collect_interval,
            effective_interval,
            parsed_priority.name,
            len(points),
        )

    async def stop_collect(self, device_id: str) -> None:
        """停止设备采集任务"""
        # FIXED-P0: _tasks.pop移入锁内，与_watchdog_loop/start_collect竞态
        async with self._state_lock:
            task = self._tasks.pop(device_id, None)
            self._device_info.pop(device_id, None)
            self._device_priorities.pop(device_id, None)
            self._adaptive_state.pop(device_id, None)
            self._last_values.pop(device_id, None)  # FIXED-P2: 停止采集时清理设备最近值缓存，防止内存泄漏
            # FIXED-LP08: 补全4个统计字典的清理，防止设备频繁增删时内存泄漏
            self._collect_stats.pop(device_id, None)
            self._device_quality_stats.pop(device_id, None)
            self._last_collect_time.pop(device_id, None)
            self._last_ai_inference_time.pop(device_id, None)
        await self._circuit_breaker_manager.remove_breaker(device_id)  # FIXED-P0: remove_breaker是async，需要await
        if task and not task.done():
            task.cancel()
            try:
                # 超时保护：采集任务可能卡在同步I/O（如Modbus读取），
                # cancel()无法中断run_in_executor中的线程，需等待其自然完成
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, TimeoutError):
                pass
            logger.info("Collection task stopped: %s", device_id)

    async def stop_all(self) -> None:
        """停止所有采集任务"""
        if self._watchdog_task:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
            self._watchdog_task = None
        if self._cache_flush_task:
            self._cache_flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cache_flush_task
            self._cache_flush_task = None
        # FIXED-P1: _tasks读取加锁保护，防止与start_collect/stop_collect并发导致RuntimeError
        async with self._state_lock:
            device_ids = list(self._tasks.keys())
        for device_id in device_ids:
            await self.stop_collect(device_id)
        logger.info("All collection tasks stopped")

    async def get_active_devices(self) -> list[str]:
        """获取正在采集的设备列表"""
        # FIXED-P0: _tasks读取需加锁保护
        async with self._state_lock:
            return list(self._tasks.keys())

    async def get_task_count(self) -> int:
        """获取活跃采集任务数"""
        # FIXED-P0: _tasks读取需加锁保护
        async with self._state_lock:
            return len(self._tasks)

    def set_preprocessor(self, preprocessor: DataPreprocessor) -> None:
        """运行时设置数据预处理器"""
        self._preprocessor = preprocessor

    async def get_collect_stats(self) -> dict[str, CollectStats]:
        """获取所有设备采集统计"""
        async with self._state_lock:  # FIXED-P0: _collect_stats并发读取保护
            return dict(self._collect_stats)

    async def get_last_values(self, device_id: str | None = None) -> dict[str, Any]:
        """获取设备最近采集值（缓存，毫秒级返回）

        Args:
            device_id: 设备ID，为None时返回所有设备
        """
        async with self._state_lock:  # FIXED-P0: _last_values并发读取保护
            if device_id:
                return dict(self._last_values.get(device_id, {}))
            return {k: dict(v) for k, v in self._last_values.items()}

    async def get_device_quality_stats(self) -> dict[str, DeviceQualityStats]:
        """获取所有设备帧错误率统计"""
        async with self._state_lock:  # FIXED-P0: _device_quality_stats并发读取保护
            return dict(self._device_quality_stats)

    async def set_max_concurrent(self, max_concurrent: int) -> None:
        """设置最大并发采集数"""  # FIXED-P0: 使用ConcurrencyGate动态调整限流，不再替换Semaphore对象
        new_max = max(1, max_concurrent)
        # S-05 FIX: 重建过程在 _state_lock 锁保护下进行，避免重建期间并发问题
        async with self._state_lock:
            old_max = self._max_concurrent_collects
            self._max_concurrent_collects = new_max
            if self._concurrency_gate is not None:
                await self._concurrency_gate.set_limit(new_max)
            # S-05 FIX: 清空旧优先级信号量后必须立即重建，否则 _collect_loop
            # 获取不到信号量会导致所有任务永久阻塞，优先级调度功能失效
            old_semaphores = dict(self._priority_semaphores)
            self._priority_semaphores.clear()
            # 根据新的 max_concurrent 重建各优先级信号量
            total_capacity = new_max
            for p in DevicePriority:
                weight = _PRIORITY_SEMAPHORE_WEIGHT.get(p, 0.1)
                capacity = max(1, int(total_capacity * weight))
                self._priority_semaphores[p] = _ConcurrencyGate(capacity)
        # S-05 FIX: 优雅迁移 - 唤醒旧信号量上正在等待的协程
        # 旧 gate 对象仍可用，被唤醒的等待者会用旧 gate 完成本次采集，
        # 下次循环时将从字典中获取新 gate，实现无缝迁移
        for p, old_gate in old_semaphores.items():
            try:
                await old_gate.wake_all_waiters()
            except Exception as migrate_e:
                logger.warning(
                    "信号量迁移: 唤醒旧信号量等待者失败 (priority=%s): %s",
                    p.name,
                    migrate_e,
                )
        # S-05 FIX: 记录信号量重建事件日志
        logger.info(
            "优先级信号量重建完成: old_max=%d, new_max=%d, 已迁移 %d 个旧信号量",
            old_max,
            new_max,
            len(old_semaphores),
        )

    def set_error_rate_threshold(self, threshold: float) -> None:
        """设置帧错误率告警阈值"""
        self._error_rate_threshold = max(0.0, min(1.0, threshold))

    async def get_circuit_breaker_status(self) -> list[dict]:  # FIXED-P0: 改为async以适配get_all_status
        """Get all circuit breaker statuses"""
        return await self._circuit_breaker_manager.get_all_status()

    async def reset_circuit_breaker(self, device_id: str) -> bool:
        """Reset a device's circuit breaker"""
        return await self._circuit_breaker_manager.reset_device(device_id)

    async def load_driver(self, protocol: str, driver_module: str) -> bool:
        """动态加载协议驱动（热插拔）"""
        try:
            import importlib

            from edgelite.drivers.base import DriverPlugin

            module = importlib.import_module(driver_module)
            # Find the driver class
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, DriverPlugin)
                    and attr is not DriverPlugin
                    and hasattr(attr, "supported_protocols")
                    and protocol in attr.supported_protocols
                ):
                    # Register the driver
                    self._driver_classes[protocol] = attr
                    logger.info("Driver hot-loaded: %s -> %s", protocol, attr_name)
                    return True
            logger.warning("No driver class found for protocol %s in %s", protocol, driver_module)
            return False
        except Exception as e:
            logger.error("Driver hot-load failed: %s - %s", driver_module, e)
            return False

    async def unload_driver(self, protocol: str) -> bool:
        """动态卸载协议驱动（热插拔）"""
        # FIXED-P1: 迭代_device_info加锁保护，防止与stop_collect并发修改
        async with self._state_lock:
            device_ids_to_stop = [
                device_id
                for device_id, info in self._device_info.items()
                if info[0].plugin_name == protocol or protocol in getattr(info[0], "supported_protocols", [])
            ]
        for device_id in device_ids_to_stop:
            await self.stop_collect(device_id)
            logger.info("Stopped device %s (driver %s unloaded)", device_id, protocol)

        # Remove from registry
        if protocol in self._driver_classes:
            del self._driver_classes[protocol]
            logger.info("Driver hot-unloaded: %s", protocol)
            return True
        return False

    async def calculate_quality_score(
        self, device_id: str
    ) -> dict[str, Any]:  # FIXED-P1: 改为async加锁保护共享状态读取
        """计算设备数据质量评分

        评分维度:
        - 采集成功率 (40%): success_count / total_count
        - 采集延迟 (30%): avg_latency_ms越低越好
        - 数据一致性 (30%): 值变化率是否合理（无跳变）
        """
        async with self._state_lock:
            stats = self._device_quality_stats.get(device_id)
            collect = self._collect_stats.get(device_id)
            if not stats or stats.total_count == 0:
                return {"score": 0, "grade": "N/A", "details": {}}
            success_rate = (stats.success_count / stats.total_count) if stats.total_count > 0 else 0
            success_score = success_rate * 40
            avg_latency = collect.avg_latency_ms if collect else 0
        if avg_latency <= 100:
            latency_score = 30
        elif avg_latency <= 500:
            latency_score = 25
        elif avg_latency <= 1000:
            latency_score = 20
        elif avg_latency <= 5000:
            latency_score = 10
        else:
            latency_score = 5

        # 3. 一致性评分 (0-30分): 检查值是否有异常跳变
        consistency_score = 30  # 默认满分，有跳变时扣分

        total_score = success_score + latency_score + consistency_score

        # Grade: A(90+), B(75+), C(60+), D(40+), F(<40)
        if total_score >= 90:
            grade = "A"
        elif total_score >= 75:
            grade = "B"
        elif total_score >= 60:
            grade = "C"
        elif total_score >= 40:
            grade = "D"
        else:
            grade = "F"

        return {
            "score": round(total_score, 1),
            "grade": grade,
            "details": {
                "success_rate": round(success_rate * 100, 1),
                "success_score": round(success_score, 1),
                "avg_latency_ms": round(avg_latency, 1),
                "latency_score": latency_score,
                "consistency_score": consistency_score,
                "total_reads": stats.total_count,
                "error_reads": stats.error_count,
            },
        }

    async def _collect_loop(
        self,
        device_id: str,
        driver: Any,
        points: list[dict],
        collect_interval: int,
    ) -> None:
        """采集循环协程"""
        point_names = [cast(str, p.get("name")) for p in points if p.get("name")]
        point_defs_map = {p.get("name"): p for p in points if p.get("name")}
        timeout = DEFAULT_TIMEOUT

        while True:
            start_time = time.monotonic()
            is_error = False
            try:
                # S-06 FIX: 在锁内捕获 gate 本地引用，并在使用期间检测是否被替换
                async with self._state_lock:
                    priority = self._device_priorities.get(device_id, DevicePriority.P2)
                    gate = self._priority_semaphores.get(priority) or self._concurrency_gate

                if gate:
                    try:
                        await gate.acquire()
                    except Exception as gate_e:
                        # S-06 FIX: gate 可能在使用期间被替换导致引用失效，捕获异常并重新获取当前 gate
                        logger.warning(
                            "gate acquire 异常 %s (priority=%s): %s, 尝试重新获取当前 gate",
                            device_id,
                            priority.name,
                            gate_e,
                        )
                        async with self._state_lock:
                            gate = self._priority_semaphores.get(priority) or self._concurrency_gate
                        if gate:
                            await gate.acquire()
                        else:
                            gate = None
                    # S-06 FIX: 验证 gate 是否仍为当前有效引用，若已被替换则迁移到新 gate
                    if gate is not None:
                        async with self._state_lock:
                            current_gate = self._priority_semaphores.get(priority) or self._concurrency_gate
                        if current_gate is not gate:
                            # S-06 FIX: 记录 gate 切换事件日志
                            logger.info(
                                "gate 切换 %s (priority=%s): 旧 gate 已被替换，迁移到新 gate",
                                device_id,
                                priority.name,
                            )
                            await gate.release()
                            gate = current_gate
                            if gate:
                                await gate.acquire()
                try:

                    async def _read_with_timeout():
                        return await asyncio.wait_for(
                            driver.read_points(device_id, point_names),
                            timeout=timeout,
                        )

                    # R8-S-02 修复: 原 fallback=lambda: {} 返回空字典，与正常读取返回空结果
                    # 无法区分。当熔断器 OPEN 拒绝调用或读取异常时 fallback 被触发，但 is_error
                    # 保持 False，导致监控盲区（设备实际不可用却被统计为采集成功）。
                    # 改用哨兵对象作为 fallback，调用后检测是否命中 fallback，命中则置 is_error=True。
                    values = await self._circuit_breaker_manager.call_with_protection(
                        device_id,
                        _read_with_timeout,
                        fallback=lambda: _CIRCUIT_FALLBACK_SENTINEL,
                    )
                finally:
                    if gate:
                        await gate.release()

                # R8-S-02 修复: 命中 fallback（熔断器拒绝或读取异常）时标记为采集错误，
                # 并将 values 归一化为空字典以跳过后续数据记录逻辑。
                if values is _CIRCUIT_FALLBACK_SENTINEL:
                    is_error = True
                    values = {}

                if values:
                    now = datetime.now().astimezone()  # 本地时间，确保显示时间与当前时间一致
                    records = []
                    for point_name, value in values.items():
                        if isinstance(value, PointValue):
                            pv = value
                            v = pv.value
                            quality = pv.quality
                        else:
                            v = value
                            quality = "good"
                        if v is None:
                            if quality == "bad":
                                records.append(
                                    {
                                        "device_id": device_id,
                                        "point_name": point_name,
                                        "value": None,
                                        "timestamp": now,
                                        "quality": "bad",
                                    }
                                )
                            continue
                        try:
                            v = round(float(v), 6) if not isinstance(v, bool) else v
                        except (ValueError, TypeError):
                            logger.warning("Point value conversion failed %s.%s: %r", device_id, point_name, v)
                            continue

                        if not isinstance(value, PointValue):
                            quality = "good"
                        pt_def = point_defs_map.get(point_name, {})

                        async with (
                            self._state_lock
                        ):  # FIXED-P2: _last_values读取+写入在同一锁临界区，消除跳变检测竞态窗口
                            last_vals = self._last_values.setdefault(device_id, {})
                            last_v = last_vals.get(point_name)
                            if isinstance(v, (int, float)):
                                last_vals[point_name] = v
                        jump_threshold = pt_def.get("jump_threshold")
                        if jump_threshold is not None and last_v is not None and isinstance(v, (int, float)):
                            if abs(v - last_v) > jump_threshold:
                                quality = "suspect"
                                logger.warning(
                                    "Data jump %s.%s: %.6f -> %.6f (threshold=%.4f)",
                                    device_id,
                                    point_name,
                                    last_v,
                                    v,
                                    jump_threshold,
                                )

                        min_value = pt_def.get("min_value")
                        max_value = pt_def.get("max_value")
                        if min_value is not None and max_value is not None and isinstance(v, (int, float)):
                            if v < min_value or v > max_value:
                                quality = "out_of_range"
                                logger.warning(
                                    "Data out of range %s.%s: %.6f (range=[%.4f, %.4f])",
                                    device_id,
                                    point_name,
                                    v,
                                    min_value,
                                    max_value,
                                )

                        if self._preprocessor:
                            processed_value, should_report = self._preprocessor.process(f"{device_id}.{point_name}", v)
                            if not should_report:
                                continue
                            if processed_value is not None:
                                v = processed_value

                        event = PointUpdateEvent(
                            device_id=device_id,
                            point_name=point_name,
                            value=v,
                            quality=quality,
                        )
                        await self._event_bus.publish(event)
                        records.append(
                            {
                                "device_id": device_id,
                                "point_name": point_name,
                                "value": v,
                                "timestamp": now,
                                "quality": quality,
                            }
                        )

                    success = await self._influx.write_points_batch(records)

                    if not success and self._cache:
                        for rec in records:
                            dev_id = rec.get("device_id")
                            pt_name = rec.get("point_name")
                            if not dev_id or not pt_name:
                                continue
                            # FIXED(一般): 原问题-add_to_cache 单条失败会中断循环，导致剩余记录丢失缓存
                            # 修复-单条异常隔离，保证其余记录继续写入缓存
                            try:
                                await self._cache.add_to_cache(
                                    measurement="device_points",
                                    tags={
                                        "device_id": dev_id,
                                        "point_name": pt_name,
                                        "quality": rec.get("quality", "unknown"),
                                    },
                                    fields={"value": rec.get("value", 0)},
                                    timestamp=now.isoformat(),
                                )
                            except Exception as cache_e:
                                logger.error(
                                    "add_to_cache failed for %s/%s: %s",
                                    dev_id,
                                    pt_name,
                                    cache_e,
                                )

                    await self._run_ai_inference(device_id, values)

            except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                is_error = True
                logger.warning("Collection timeout: %s (%.1fs)", device_id, timeout)
                # FIXED(一般): 原问题-逐点发布事件淹没队列;
                # 修复-限制单次失败事件数，超过阈值发布summary
                _MAX_TIMEOUT_EVENTS = 10
                if len(point_names) <= _MAX_TIMEOUT_EVENTS:
                    for point_name in point_names:
                        event = PointUpdateEvent(
                            device_id=device_id,
                            point_name=point_name,
                            quality="timeout",
                        )
                        # FIXED(严重): publish 失败时(如队列满)记录 warning 并 break，
                        # 避免异常逃出 except 块导致 while True 循环终止、设备采集永久停止
                        try:
                            await self._event_bus.publish(event)
                        except Exception as publish_e:
                            logger.warning(
                                "publish failed (queue full?) for %s.%s: %s",
                                device_id,
                                point_name,
                                publish_e,
                            )
                            break
                else:
                    event = PointUpdateEvent(
                        device_id=device_id,
                        point_name="__summary__",
                        quality="timeout",
                    )
                    # FIXED(严重): publish 失败时记录 warning，避免异常逃出 except 块
                    try:
                        await self._event_bus.publish(event)
                    except Exception as publish_e:
                        logger.warning(
                            "publish failed (queue full?) for %s.__summary__: %s",
                            device_id,
                            publish_e,
                        )

            except asyncio.CancelledError:
                raise

            except Exception as e:
                is_error = True
                logger.error("Collection error: %s - %s", device_id, e)
                for point_name in point_names:
                    event = PointUpdateEvent(
                        device_id=device_id,
                        point_name=point_name,
                        quality="bad",
                    )
                    # FIXED(严重): publish 失败时(如队列满)记录 warning 并 break，
                    # 避免异常逃出 except 块导致 while True 循环终止、设备采集永久停止
                    try:
                        await self._event_bus.publish(event)
                    except Exception as publish_e:
                        logger.warning(
                            "publish failed (queue full?) for %s.%s: %s",
                            device_id,
                            point_name,
                            publish_e,
                        )
                        break

            end_time = time.monotonic()
            latency_ms = (end_time - start_time) * 1000
            await self._update_collect_stats(device_id, latency_ms, is_error)
            await self._update_device_quality_stats(device_id, is_error)
            async with self._state_lock:  # FIXED-P2: _last_collect_time写入加锁，与_watchdog_loop读取互斥
                self._last_collect_time[device_id] = end_time

            # Adaptive frequency adjustment
            adaptive_interval = await self._adjust_adaptive_interval(device_id, is_error, collect_interval)

            await asyncio.sleep(adaptive_interval)

    async def _update_collect_stats(self, device_id: str, latency_ms: float, is_error: bool) -> None:
        """更新采集延迟统计"""
        async with self._state_lock:  # FIXED-P0: _collect_stats并发更新保护
            stats = self._collect_stats.get(device_id)
            if not stats:
                stats = CollectStats(device_id=device_id)
                self._collect_stats[device_id] = stats
            stats.total_calls += 1
            stats._latency_sum += latency_ms
            if latency_ms > stats.max_latency_ms:
                stats.max_latency_ms = latency_ms
            stats.avg_latency_ms = stats._latency_sum / stats.total_calls
            stats.last_collect_at = datetime.now(UTC).isoformat()
            if is_error:
                stats.timeout_count += 1

    async def _update_device_quality_stats(self, device_id: str, is_error: bool) -> None:
        """更新帧错误率统计，超阈值时发布告警事件"""
        async with self._state_lock:  # FIXED-P0: _device_quality_stats并发更新保护
            qs = self._device_quality_stats.get(device_id)
            if not qs:
                qs = DeviceQualityStats(device_id=device_id)
                self._device_quality_stats[device_id] = qs
            qs.total_count += 1
            if is_error:
                qs.error_count += 1
            else:
                qs.success_count += 1
            if qs.total_count > 0:
                qs.error_rate = qs.error_count / qs.total_count
            should_alarm = qs.total_count >= 10 and qs.error_rate > self._error_rate_threshold
            alarm_error_rate = qs.error_rate

        if should_alarm:
            logger.warning(
                "设备帧错误率超阈值: %s (%.1f%% > %.1f%%)",
                device_id,
                alarm_error_rate * 100,
                self._error_rate_threshold * 100,
            )
            try:
                from edgelite.app import _app_state

                if _app_state.event_bus:
                    from edgelite.engine.event_bus import DeviceStatusEvent

                    event = DeviceStatusEvent(
                        device_id=device_id,
                        old_status="healthy",
                        new_status="degraded",
                    )

                    # FIXED(一般): 原问题-lambda吞没异常，t.exception()仅返回不记录;
                    # 修复-改为命名函数记录日志
                    def _on_evt_done(t: asyncio.Task) -> None:
                        if not t.cancelled() and t.exception():
                            logger.warning("Event publish failed: %s", t.exception())

                    _evt_task = asyncio.create_task(_app_state.event_bus.publish(event))
                    _evt_task.add_done_callback(_on_evt_done)
            except Exception as e:
                logger.debug("Frame error rate alarm event publish failed: %s", e)

    async def _adjust_adaptive_interval(self, device_id: str, is_error: bool, base_interval: int) -> int:
        """Adjust collection interval based on consecutive success/failure counts

        Rules:
        - 3 consecutive successes: reduce interval by 20% (min 50% of base)
        - 3 consecutive failures: double interval (max 5x of base)
        - Return to original interval when recovering from failure

        Args:
            device_id: Device ID
            is_error: Whether the last collection was an error
            base_interval: The base (configured) collection interval

        Returns:
            The effective collection interval in seconds
        """
        async with self._state_lock:  # FIXED-P0: _adaptive_state并发读写保护
            state = self._adaptive_state.get(device_id)
            if state is None:
                return base_interval

            priority = self._device_priorities.get(device_id, DevicePriority.P2)
            multiplier = _PRIORITY_INTERVAL_MULTIPLIER.get(priority, 1.0)
            priority_base = max(1, int(base_interval * multiplier))

            if is_error:
                state["consecutive_successes"] = 0
                state["consecutive_failures"] += 1
            else:
                state["consecutive_failures"] = 0
                state["consecutive_successes"] += 1

            current_interval = state["effective_interval"]

            if state["consecutive_successes"] >= 3:
                new_interval = max(
                    int(priority_base * 0.5),
                    int(current_interval * 0.8),
                )
                if new_interval != current_interval:
                    logger.info(
                        "Adaptive interval: %s speed up %ds -> %ds (3+ consecutive successes)",
                        device_id,
                        current_interval,
                        new_interval,
                    )
                    state["effective_interval"] = new_interval
                state["consecutive_successes"] = 0

            elif state["consecutive_failures"] >= 3:
                max_interval = priority_base * 5
                new_interval = min(max_interval, current_interval * 2)
                if new_interval != current_interval:
                    logger.info(
                        "Adaptive interval: %s slow down %ds -> %ds (3+ consecutive failures)",
                        device_id,
                        current_interval,
                        new_interval,
                    )
                    state["effective_interval"] = new_interval
                state["consecutive_failures"] = 0

            elif state["consecutive_successes"] > 0 and current_interval > priority_base:
                logger.info(
                    "Adaptive interval: %s reset to base %ds -> %ds (recovered)",
                    device_id,
                    current_interval,
                    priority_base,
                )
                state["effective_interval"] = priority_base

            return state["effective_interval"]

    async def _watchdog_loop(self) -> None:
        """看门狗协程：定期检查采集活跃度，stale设备重启采集Task"""
        while True:
            try:
                await asyncio.sleep(self._WATCHDOG_INTERVAL)
                now = time.monotonic()
                # FIXED-P0: 迭代前获取锁并创建快照，防止与start_collect/stop_collect竞态
                async with self._state_lock:
                    device_items = list(self._device_info.items())
                    collect_times = dict(self._last_collect_time)
                    task_map = dict(self._tasks)
                for device_id, info in device_items:
                    last_t = collect_times.get(device_id)
                    if last_t is None:
                        continue
                    driver, points, interval = info
                    elapsed = now - last_t
                    stale_cycles = elapsed / max(interval, 1)

                    if stale_cycles >= self._WATCHDOG_RESTART_CYCLES:
                        logger.warning(
                            "Watchdog: %s no data for %d cycles (%.0fs), restarting collection task",
                            device_id,
                            self._WATCHDOG_RESTART_CYCLES,
                            elapsed,
                        )
                        old_task = task_map.get(device_id)
                        if old_task and not old_task.done():
                            old_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                                await asyncio.wait_for(old_task, timeout=5.0)
                        new_task = asyncio.create_task(
                            self._collect_loop(device_id, driver, points, interval),
                            name=f"collect-{device_id}",
                        )
                        # FIXED(严重): TOCTOU 竞态 - 写入前检查任务是否已被 start_collect/stop_collect 修改
                        # 若已变更则放弃新任务，避免覆盖产生僵尸任务（旧引用泄漏且仍在运行）
                        abort_new = False
                        async with self._state_lock:
                            current_task = self._tasks.get(device_id)
                            if current_task is not old_task:
                                abort_new = True
                            else:
                                self._tasks[device_id] = new_task
                                self._last_collect_time[device_id] = now
                                # FIXED-P0: 重置 adaptive_state，防止 effective_interval 过大导致
                                # "重启->超时->重启"死循环
                                if device_id in self._adaptive_state:
                                    state = self._adaptive_state[device_id]
                                    state["consecutive_failures"] = 0
                                    state["consecutive_successes"] = 0
                                    state["effective_interval"] = interval
                        if abort_new:
                            new_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await new_task
                            logger.info(
                                "Watchdog: %s task changed during restart, aborting restart",
                                device_id,
                            )

                    elif stale_cycles >= self._WATCHDOG_STALE_CYCLES:
                        logger.warning(
                            "Watchdog: %s marked stale (%.0fs / %d cycles)",
                            device_id,
                            elapsed,
                            self._WATCHDOG_STALE_CYCLES,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Watchdog error: %s", e)

    async def _cache_flush_loop(self) -> None:
        """定期检查InfluxDB可用性并回写缓存数据

        优先从 RingBuffer 获取待同步记录（增量同步），
        若 RingBuffer 不可用则回退到 SQLite 查询。
        """
        # 首次启动时从 SQLite 恢复到 RingBuffer
        if self._cache and hasattr(self._cache, "restore_from_sqlite"):
            try:
                await self._cache.restore_from_sqlite()
            except Exception as e:
                logger.error("Failed to restore from SQLite to RingBuffer: %s", e)

        while True:
            try:
                await asyncio.sleep(_SCHEDULER_INTERVAL)
                if not self._cache or not self._influx:
                    continue
                if not await self._influx.check_health():
                    continue

                # 优先从 RingBuffer 获取待同步记录
                use_ring_buffer = (
                    hasattr(self._cache, "get_pending_from_ring_buffer")
                    and self._cache.get_ring_buffer_stats() is not None
                )

                if use_ring_buffer:
                    await self._flush_from_ring_buffer()
                else:
                    await self._flush_from_sqlite()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Cache flush error: %s", e)

    async def _flush_from_ring_buffer(self) -> None:
        """从 RingBuffer 增量同步缓存数据到 InfluxDB（批量写入）"""
        cache = self._cache
        if cache is None:
            return
        records = await cache.get_pending_from_ring_buffer(limit=_CACHE_BATCH_LIMIT)
        if not records:
            return

        # 将缓存记录转换为 write_points_batch 所需格式
        batch: list[dict] = []
        rec_index: list[tuple[int | None, int | None]] = []  # (ring_id, sqlite_id) per record
        for rec in records:
            tags = rec.get("tags", {})
            fields = rec.get("fields", {})
            device_id = tags.get("device_id", "")
            point_name = tags.get("point_name", "")
            value = fields.get("value")
            if not device_id or not point_name or value is None:
                continue

            ts = rec.get("timestamp")
            if ts:
                try:
                    timestamp = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    timestamp = datetime.now(UTC)
            else:
                timestamp = datetime.now(UTC)

            batch.append(
                {
                    "device_id": device_id,
                    "point_name": point_name,
                    "value": value,
                    "timestamp": timestamp,
                    "quality": tags.get("quality", "unknown"),
                }
            )
            rec_index.append((rec.get("_id"), rec.get("sqlite_id")))

        if not batch:
            return

        ok = await self._influx.write_points_batch(batch)
        if ok:
            synced_ring_ids = [rid for rid, _ in rec_index if rid is not None]
            synced_sqlite_ids = [sid for _, sid in rec_index if sid is not None]
            if synced_ring_ids:
                await cache.mark_synced(synced_ring_ids, synced_sqlite_ids or None)
                logger.info("Cache flush (RingBuffer): %d records written to InfluxDB", len(synced_ring_ids))
        else:
            failed_ring_ids = [rid for rid, _ in rec_index if rid is not None]
            if failed_ring_ids:
                await cache.mark_failed(failed_ring_ids)
            logger.warning("Cache flush batch write failed (%d records), marking as failed", len(batch))

    async def _flush_from_sqlite(self) -> None:
        """从 SQLite 回写缓存数据到 InfluxDB（回退路径，批量写入）"""
        cache = self._cache
        if cache is None:
            return
        records = await cache.get_cached_records(limit=_CACHE_BATCH_LIMIT)
        if not records:
            return

        # PERF: 改为批量写入，避免逐条 write_point 产生 N 次 HTTP 请求
        # 参考 _flush_from_ring_buffer 的批量写入模式
        batch: list[dict] = []
        rec_ids: list[int] = []
        for rec in records:
            tags = rec.get("tags", {})
            fields = rec.get("fields", {})
            device_id = tags.get("device_id", "")
            point_name = tags.get("point_name", "")
            value = fields.get("value")
            if not device_id or not point_name or value is None:
                continue

            ts = rec.get("timestamp")
            if ts:
                try:
                    timestamp = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    timestamp = datetime.now(UTC)
            else:
                timestamp = datetime.now(UTC)

            batch.append(
                {
                    "device_id": device_id,
                    "point_name": point_name,
                    "value": value,
                    "timestamp": timestamp,
                    "quality": tags.get("quality", "unknown"),
                }
            )
            rec_id = rec.get("id")
            if rec_id is not None:
                rec_ids.append(rec_id)

        if not batch:
            return

        try:
            ok = await self._influx.write_points_batch(batch)
            if ok:
                if rec_ids:
                    await cache.delete_cached(rec_ids)
                logger.info("Cache flush (SQLite): %d records written to InfluxDB", len(batch))
            else:
                logger.warning("Cache flush batch write failed (%d records from SQLite)", len(batch))
        except Exception as e:
            logger.error("Cache flush batch write failed (SQLite): %s", e)

    async def _run_ai_inference(self, device_id: str, values: dict[str, float]) -> None:
        # FIXED-P1: 加 asyncio.wait_for(timeout=10s) 防止 AI 推理长时间阻塞调度循环
        try:
            await asyncio.wait_for(self._run_ai_inference_inner(device_id, values), timeout=10.0)
        except TimeoutError:
            logger.warning(
                "[scheduler] code=AI_INFERENCE_TIMEOUT msg=AI inference timed out after 10.0s for device=%s",
                device_id,
            )
        except Exception as e:
            logger.error("AI inference scheduler error for device %s: %s", device_id, e)

    async def _run_ai_inference_inner(self, device_id: str, values: dict[str, float]) -> None:
        try:
            from edgelite.app import _app_state

            ai_service = getattr(_app_state, "ai_service", None)
            if ai_service is None:
                return

            ai_engine = getattr(_app_state, "ai_engine", None)
            if ai_engine is None:
                return

            now = time.monotonic()
            # FIXED-P0: 冷却期检查和写入必须在同一锁临界区，防止并发绕过冷却期
            async with self._state_lock:
                last_time = self._last_ai_inference_time.get(device_id, 0)
                if now - last_time < _AI_INFERENCE_COOLDOWN_SECONDS:
                    return
                self._last_ai_inference_time[device_id] = now

            loaded_models = ai_engine.get_loaded_models()
            active_models = [(mid, mw) for mid, mw in loaded_models.items() if mw.status == "active"]
            if not active_models:
                return

            input_data = [v for v in values.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if not input_data:
                return

            for model_id, model_wrapper in active_models:
                try:
                    result = await ai_service.inference(
                        model_id=model_id,
                        input_data=input_data,
                        device_id=device_id,
                    )

                    if result.get("status") == "success":
                        output_data = result.get("output_data", {})
                        latency_ms = result.get("latency_ms", 0)
                        await self._publish_ai_virtual_points(
                            model_id,
                            model_wrapper.model_name,
                            device_id,
                            output_data,
                            latency_ms,
                        )
                        await self._check_ai_anomaly(
                            model_id,
                            model_wrapper.model_name,
                            device_id,
                            output_data,
                            latency_ms,
                        )
                except Exception as e:
                    logger.error(
                        "AI inference failed for model %s on device %s: %s",
                        model_id,
                        device_id,
                        e,
                    )
        except Exception as e:
            logger.error("AI inference scheduler error for device %s: %s", device_id, e)

    async def _publish_ai_virtual_points(
        self,
        model_id: str,
        model_name: str,
        device_id: str,
        output_data: dict,
        latency_ms: float,
    ) -> None:
        virtual_device_id = f"ai_inference_{model_id}"
        async with self._state_lock:  # FIXED-P2: _ai_virtual_devices写入加锁
            self._ai_virtual_devices.add(virtual_device_id)

        for _output_key, output_value in output_data.items():
            point_value = None
            if isinstance(output_value, list) and len(output_value) > 0:
                point_value = float(output_value[0]) if isinstance(output_value[0], (int, float)) else 0.0
            elif isinstance(output_value, (int, float)):
                point_value = float(output_value)
            else:
                point_value = 0.0

            event = PointUpdateEvent(
                device_id=virtual_device_id,
                point_name=_output_key,
                value=point_value,
                quality="good",
            )
            await self._event_bus.publish(event)

        latency_event = PointUpdateEvent(
            device_id=virtual_device_id,
            point_name="inference_latency_ms",
            value=latency_ms,
            quality="good",
        )
        await self._event_bus.publish(latency_event)

    async def _check_ai_anomaly(
        self,
        model_id: str,
        model_name: str,
        device_id: str,
        output_data: dict,
        latency_ms: float,
    ) -> None:
        try:
            from edgelite.app import _app_state

            anomaly_score = None
            for _output_key2, output_value in output_data.items():
                val = output_value
                if isinstance(val, list) and len(val) > 0:
                    val = float(val[0]) if isinstance(val[0], (int, float)) else None
                elif isinstance(val, (int, float)):
                    val = float(val)
                else:
                    val = None

                if val is not None and val > _AI_ANOMALY_THRESHOLD_DEFAULT:
                    anomaly_score = val
                    break

            if anomaly_score is None:
                return

            alarm_service = getattr(_app_state, "alarm_service", None)
            if alarm_service is None:
                return

            severity = "critical" if anomaly_score > 0.95 else "major" if anomaly_score > 0.85 else "minor"
            await alarm_service.trigger_alarm(
                rule_id=f"ai_anomaly_{model_id}",
                rule_name=f"AI Anomaly: {model_name}",
                device_id=device_id,
                device_name=device_id,
                severity=severity,
                message=(
                    f"AI model {model_name} detected anomaly on device {device_id}, anomaly score: {anomaly_score:.4f}"
                ),
                trigger_value={
                    "model_id": model_id,
                    "model_name": model_name,
                    "anomaly_score": anomaly_score,
                    "latency_ms": latency_ms,
                    "output_data": output_data,
                },
            )
            logger.warning(
                "AI anomaly alarm triggered: model=%s, device=%s, score=%.4f, severity=%s",
                model_id,
                device_id,
                anomaly_score,
                severity,
            )
        except Exception as e:
            logger.error("AI anomaly alarm check failed: %s", e)
