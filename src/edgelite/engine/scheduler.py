"""采集调度器 - 基于asyncio的定时采集任务调度"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from typing import Any

from edgelite.engine.event_bus import EventBus, PointUpdateEvent
from edgelite.engine.preprocessor import DataPreprocessor
from edgelite.storage.cache import CacheManager
from edgelite.storage.influx_storage import InfluxDBStorage

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0


class CollectScheduler:
    """采集调度器，为每个在线设备创建独立的采集协程"""

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
        # device_id -> asyncio.Task
        self._tasks: dict[str, asyncio.Task] = {}
        # device_id -> (driver_instance, points, collect_interval)
        self._device_info: dict[str, tuple] = {}

    async def start_collect(
        self,
        device_id: str,
        driver: Any,
        points: list[dict],
        collect_interval: int = 5,
    ) -> None:
        """为设备启动采集任务"""
        if device_id in self._tasks:
            await self.stop_collect(device_id)

        self._device_info[device_id] = (driver, points, collect_interval)
        task = asyncio.create_task(
            self._collect_loop(device_id, driver, points, collect_interval),
            name=f"collect-{device_id}",
        )
        self._tasks[device_id] = task
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

    async def _collect_loop(
        self,
        device_id: str,
        driver: Any,
        points: list[dict],
        collect_interval: int,
    ) -> None:
        """采集循环协程"""
        point_names = [p["name"] for p in points]
        timeout = DEFAULT_TIMEOUT

        while True:
            try:
                # 读取测点值（带超时）
                values = await asyncio.wait_for(
                    driver.read_points(device_id, point_names),
                    timeout=timeout,
                )

                if values:
                    now = datetime.now(UTC)
                    # 批量写入InfluxDB（一次API调用替代N次）
                    records = []
                    for point_name, value in values.items():
                        v = float(value) if not isinstance(value, bool) else value
                        # 数据预处理
                        if self._preprocessor:
                            processed_value, should_report = self._preprocessor.process(
                                f"{device_id}.{point_name}", v
                            )
                            if not should_report:
                                continue
                            if processed_value is not None:
                                v = processed_value
                        # 发布事件
                        event = PointUpdateEvent(
                            device_id=device_id,
                            point_name=point_name,
                            value=v,
                            quality="good",
                        )
                        await self._event_bus.publish(event)
                        records.append(
                            {
                                "device_id": device_id,
                                "point_name": point_name,
                                "value": v,
                                "timestamp": now,
                                "quality": "good",
                            }
                        )

                    # 批量写入时序数据库
                    success = await self._influx.write_points_batch(records)

                    # InfluxDB不可用时逐条缓存
                    if not success and self._cache:
                        for rec in records:
                            await self._cache.add_to_cache(
                                measurement="device_points",
                                tags={
                                    "device_id": rec["device_id"],
                                    "point_name": rec["point_name"],
                                    "quality": rec["quality"],
                                },
                                fields={"value": rec["value"]},
                                timestamp=now.isoformat(),
                            )

            except TimeoutError:
                logger.warning("采集超时: %s (%.1fs)", device_id, timeout)
                # 发布超时事件
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
                logger.error("采集异常: %s - %s", device_id, e)

            # 等待下一个采集周期
            await asyncio.sleep(collect_interval)
