"""采集调度器 - 基于asyncio的定时采集任务调度"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edgelite.engine.event_bus import EventBus, PointUpdateEvent
from edgelite.engine.preprocessor import DataPreprocessor
from edgelite.constants import _CACHE_BATCH_LIMIT, _SCHEDULER_INTERVAL  # FIXED: 原问题-魔法数字limit=500
from edgelite.storage.cache import CacheManager
from edgelite.storage.influx_storage import InfluxDBStorage

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0


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
        self._watchdog_task: asyncio.Task | None = None

        try:
            from edgelite.config import get_config
            cfg = get_config()
            sc = getattr(cfg, "scheduler", None)
        except Exception:
            sc = None

        self._max_concurrent_collects = getattr(sc, "max_concurrent_collects", self._DEFAULT_MAX_CONCURRENT) if sc else self._DEFAULT_MAX_CONCURRENT
        self._error_rate_threshold = getattr(sc, "error_rate_threshold", self._DEFAULT_ERROR_RATE_THRESHOLD) if sc else self._DEFAULT_ERROR_RATE_THRESHOLD
        self._WATCHDOG_INTERVAL = getattr(sc, "watchdog_interval", 30) if sc else 30
        self._WATCHDOG_STALE_CYCLES = getattr(sc, "watchdog_stale_cycles", 3) if sc else 3
        self._WATCHDOG_RESTART_CYCLES = getattr(sc, "watchdog_restart_cycles", 10) if sc else 10
        self._semaphore: asyncio.Semaphore | None = None

    async def start_collect(
        self,
        device_id: str,
        driver: Any,
        points: list[dict],
        collect_interval: int = 5,
    ) -> None:
        """为设备启动采集任务"""
        old_info = None
        if device_id in self._tasks:
            old_info = self._device_info.get(device_id)
            await self.stop_collect(device_id)

        if not self._cache_flush_task and self._cache:
            self._cache_flush_task = asyncio.create_task(self._cache_flush_loop())

        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrent_collects)

        if not self._watchdog_task:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="watchdog")

        self._device_info[device_id] = (driver, points, collect_interval)
        self._collect_stats.setdefault(device_id, CollectStats(device_id=device_id))
        self._device_quality_stats.setdefault(device_id, DeviceQualityStats(device_id=device_id))
        try:
            task = asyncio.create_task(
                self._collect_loop(device_id, driver, points, collect_interval),
                name=f"collect-{device_id}",
            )
            self._tasks[device_id] = task
        except Exception:
            if old_info:
                self._device_info[device_id] = old_info
            raise
        logger.info(
            "采集任务启动: %s (间隔=%ds, 测点=%d)", device_id, collect_interval, len(points)
        )

    async def stop_collect(self, device_id: str) -> None:
        """停止设备采集任务"""
        task = self._tasks.pop(device_id, None)
        self._device_info.pop(device_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info("采集任务停止: %s", device_id)

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
        device_ids = list(self._tasks.keys())
        for device_id in device_ids:
            await self.stop_collect(device_id)
        logger.info("所有采集任务已停止")

    def get_active_devices(self) -> list[str]:
        """获取正在采集的设备列表"""
        return list(self._tasks.keys())

    def get_task_count(self) -> int:
        """获取活跃采集任务数"""
        return len(self._tasks)

    def set_preprocessor(self, preprocessor: DataPreprocessor) -> None:
        """运行时设置数据预处理器"""
        self._preprocessor = preprocessor

    def get_collect_stats(self) -> dict[str, CollectStats]:
        """获取所有设备采集统计"""
        return dict(self._collect_stats)

    def get_device_quality_stats(self) -> dict[str, DeviceQualityStats]:
        """获取所有设备帧错误率统计"""
        return dict(self._device_quality_stats)

    def set_max_concurrent(self, max_concurrent: int) -> None:
        """设置最大并发采集数"""
        self._max_concurrent_collects = max(1, max_concurrent)
        self._semaphore = asyncio.Semaphore(self._max_concurrent_collects)

    def set_error_rate_threshold(self, threshold: float) -> None:
        """设置帧错误率告警阈值"""
        self._error_rate_threshold = max(0.0, min(1.0, threshold))

    async def _collect_loop(
        self,
        device_id: str,
        driver: Any,
        points: list[dict],
        collect_interval: int,
    ) -> None:
        """采集循环协程"""
        point_names = [p.get("name") for p in points if p.get("name")]
        point_defs_map = {p.get("name"): p for p in points if p.get("name")}
        timeout = DEFAULT_TIMEOUT

        while True:
            start_time = time.monotonic()
            is_error = False
            try:
                if self._semaphore:
                    await self._semaphore.acquire()
                try:
                    values = await asyncio.wait_for(
                        driver.read_points(device_id, point_names),
                        timeout=timeout,
                    )
                finally:
                    if self._semaphore:
                        self._semaphore.release()

                if values:
                    now = datetime.now(UTC)
                    records = []
                    for point_name, value in values.items():
                        try:
                            v = round(float(value), 6) if not isinstance(value, bool) else value
                        except (ValueError, TypeError):
                            logger.warning("测点值转换失败 %s.%s: %r", device_id, point_name, value)
                            continue

                        quality = "good"
                        pt_def = point_defs_map.get(point_name, {})

                        last_vals = self._last_values.setdefault(device_id, {})
                        last_v = last_vals.get(point_name)
                        jump_threshold = pt_def.get("jump_threshold")
                        if jump_threshold is not None and last_v is not None and isinstance(v, (int, float)):
                            if abs(v - last_v) > jump_threshold:
                                quality = "suspect"
                                logger.warning(
                                    "数据跳变 %s.%s: %.6f -> %.6f (threshold=%.4f)",
                                    device_id, point_name, last_v, v, jump_threshold,
                                )

                        min_value = pt_def.get("min_value")
                        max_value = pt_def.get("max_value")
                        if min_value is not None and max_value is not None and isinstance(v, (int, float)):
                            if v < min_value or v > max_value:
                                quality = "out_of_range"
                                logger.warning(
                                    "数据越界 %s.%s: %.6f (range=[%.4f, %.4f])",
                                    device_id, point_name, v, min_value, max_value,
                                )

                        if isinstance(v, (int, float)):
                            last_vals[point_name] = v

                        if self._preprocessor:
                            processed_value, should_report = self._preprocessor.process(
                                f"{device_id}.{point_name}", v
                            )
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

            except TimeoutError:
                is_error = True
                logger.warning("采集超时: %s (%.1fs)", device_id, timeout)
                for point_name in point_names:
                    event = PointUpdateEvent(
                        device_id=device_id,
                        point_name=point_name,
                        quality="timeout",
                    )
                    await self._event_bus.publish(event)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                is_error = True
                logger.error("采集异常: %s - %s", device_id, e)

            end_time = time.monotonic()
            latency_ms = (end_time - start_time) * 1000
            self._update_collect_stats(device_id, latency_ms, is_error)
            self._update_device_quality_stats(device_id, is_error)
            self._last_collect_time[device_id] = end_time

            await asyncio.sleep(collect_interval)

    def _update_collect_stats(self, device_id: str, latency_ms: float, is_error: bool) -> None:
        """更新采集延迟统计"""
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

    def _update_device_quality_stats(self, device_id: str, is_error: bool) -> None:
        """更新帧错误率统计，超阈值时发布告警事件"""
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

        if qs.total_count >= 10 and qs.error_rate > self._error_rate_threshold:
            logger.warning(
                "设备帧错误率超阈值: %s (%.1f%% > %.1f%%)",
                device_id, qs.error_rate * 100, self._error_rate_threshold * 100,
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
                    asyncio.create_task(_app_state.event_bus.publish(event))
            except Exception as e:
                logger.debug("帧错误率告警事件发布失败: %s", e)

    async def _watchdog_loop(self) -> None:
        """看门狗协程：定期检查采集活跃度，stale设备重启采集Task"""
        while True:
            try:
                await asyncio.sleep(self._WATCHDOG_INTERVAL)
                now = time.monotonic()
                for device_id, info in list(self._device_info.items()):
                    last_t = self._last_collect_time.get(device_id)
                    if last_t is None:
                        continue
                    driver, points, interval = info
                    elapsed = now - last_t
                    stale_cycles = elapsed / max(interval, 1)

                    if stale_cycles >= self._WATCHDOG_RESTART_CYCLES:
                        logger.warning(
                            "看门狗: %s 超过%d周期无数据(%.0fs)，重启采集Task",
                            device_id, self._WATCHDOG_RESTART_CYCLES, elapsed,
                        )
                        task = self._tasks.get(device_id)
                        if task and not task.done():
                            task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await task
                        new_task = asyncio.create_task(
                            self._collect_loop(device_id, driver, points, interval),
                            name=f"collect-{device_id}",
                        )
                        self._tasks[device_id] = new_task
                        self._last_collect_time[device_id] = now

                    elif stale_cycles >= self._WATCHDOG_STALE_CYCLES:
                        logger.warning(
                            "看门狗: %s 标记stale (%.0fs / %d周期)",
                            device_id, elapsed, self._WATCHDOG_STALE_CYCLES,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("看门狗异常: %s", e)

    async def _cache_flush_loop(self) -> None:
        """定期检查InfluxDB可用性并回写缓存数据"""
        while True:
            try:
                await asyncio.sleep(_SCHEDULER_INTERVAL)  # FIXED: 原问题-魔法数字，提取为命名常量
                if not self._cache or not self._influx:
                    continue
                if not await self._influx.check_health():
                    continue
                records = await self._cache.get_cached_records(limit=_CACHE_BATCH_LIMIT)  # FIXED: 原问题-魔法数字limit=500
                if not records:
                    continue
                success_count = 0
                for rec in records:
                    try:
                        ts = rec.get("timestamp")
                        if ts:
                            from datetime import datetime as dt
                            try:
                                timestamp = dt.fromisoformat(ts)
                            except (ValueError, TypeError):
                                timestamp = dt.now(UTC)
                        else:
                            timestamp = dt.now(UTC)
                        ok = await self._influx.write_point(
                            measurement=rec.get("measurement", "device_points"),
                            tags=rec.get("tags", {}),
                            fields=rec.get("fields", {}),
                            timestamp=timestamp,
                        )
                        if ok:
                            rec_id = rec.get("id")  # FIXED: 原问题-硬访问id可能KeyError
                            if rec_id is None:
                                continue
                            await self._cache.delete_cached([rec_id])  # FIXED: 原问题-传入int但签名要求list[int]，导致TypeError
                            success_count += 1
                    except Exception as e:  # FIXED: 原问题-except Exception:break无日志，InfluxDB写入失败后缓存回写永久停止且无告警
                        logger.error("缓存回写失败，停止回写循环: %s", e)
                        break
                if success_count > 0:
                    logger.info("缓存回写: %d 条记录已写入InfluxDB", success_count)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("缓存回写异常: %s", e)
