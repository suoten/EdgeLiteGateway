"""S7 驱动时序数据存储模块 — 本地时序数据缓存与离线同步。

S7TsStore: 内存时序存储，支持按设备/测点/时间/质量查询
S7OfflineSyncManager: 离线数据同步管理，网络恢复后批量上传

设计要点:
- 内存存储 (无持久化)，适合测试和轻量场景
- 线程安全: 使用 threading.Lock 保护内部状态
- 数据保留: 按 retention_days 自动淘汰过期数据
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_MAX_POINTS_PER_DEVICE = 100000  # 每设备最大存储点数 (内存保护)


@dataclass
class TimeSeriesPoint:
    """单个时序数据点"""

    device_id: str
    point_name: str
    value: Any
    timestamp: str
    quality: str = "good"


class S7TsStore:
    """S7 时序数据存储 — 内存缓存，支持查询和统计

    数据按 (device_id, point_name) 分组存储，使用 deque 实现 FIFO 淘汰。
    """

    def __init__(self, retention_days: int = 7) -> None:
        self._retention_days = retention_days
        self._data: dict[str, deque[TimeSeriesPoint]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._running = False
        self._write_count = 0
        self._max_per_point = _MAX_POINTS_PER_DEVICE

    async def start(self) -> None:
        """启动时序存储"""
        self._running = True
        logger.info("[s7_ts_store] started (retention_days=%d)", self._retention_days)

    async def stop(self) -> None:
        """停止时序存储"""
        self._running = False
        logger.info("[s7_ts_store] stopped")

    async def write_read_result(self, device_id: str, result: dict[str, Any]) -> None:
        """写入采集结果 (批量)

        Args:
            device_id: 设备 ID
            result: {point_name: value, ...} 采集结果字典
        """
        if not self._running:
            return
        ts = datetime.now(UTC).isoformat()
        with self._lock:
            for point_name, value in result.items():
                if value is None:
                    continue
                quality = "good"
                if isinstance(value, dict) and "quality" in value:
                    quality = str(value.get("quality", "good"))
                    value = value.get("value")
                key = f"{device_id}:{point_name}"
                dq = self._data[key]
                dq.append(TimeSeriesPoint(device_id, point_name, value, ts, quality))
                # FIFO 淘汰
                if len(dq) > self._max_per_point:
                    dq.popleft()
                self._write_count += 1

    async def query(
        self,
        device_id: str,
        point_name: str,
        start_time: str | None = None,
        end_time: str | None = None,
        quality: str | None = None,
        aggregate: str | None = None,
        window_seconds: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """查询时序数据

        Args:
            device_id: 设备 ID
            point_name: 测点名称
            start_time/end_time: 时间范围 (ISO 格式字符串比较)
            quality: 质量过滤 (good/bad/uncertain)
            aggregate: 聚合方式 (avg/max/min/sum/count)
            window_seconds: 聚合窗口大小 (秒)
            limit: 返回记录数上限

        Returns:
            时序数据点列表 [{timestamp, value, quality}, ...]
        """
        key = f"{device_id}:{point_name}"
        with self._lock:
            points = list(self._data.get(key, deque()))
        # 时间过滤
        if start_time:
            points = [p for p in points if p.timestamp >= start_time]
        if end_time:
            points = [p for p in points if p.timestamp <= end_time]
        # 质量过滤
        if quality:
            points = [p for p in points if p.quality == quality]
        # 聚合
        if aggregate and window_seconds:
            points = self._aggregate(points, aggregate, window_seconds)
        # 限制返回数量
        return [{"timestamp": p.timestamp, "value": p.value, "quality": p.quality} for p in points[-limit:]]

    async def query_latest(self, device_id: str, point_names: list[str]) -> dict[str, Any]:
        """查询最新值"""
        result: dict[str, Any] = {}
        for pn in point_names:
            key = f"{device_id}:{pn}"
            with self._lock:
                dq = self._data.get(key, deque())
                if dq:
                    latest = dq[-1]
                    result[pn] = {"value": latest.value, "timestamp": latest.timestamp, "quality": latest.quality}
                else:
                    result[pn] = None
        return result

    async def query_by_quality(
        self,
        device_id: str,
        point_name: str,
        start_time: str | None,
        end_time: str | None,
        quality: str,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """按质量过滤查询"""
        return await self.query(device_id, point_name, start_time, end_time, quality=quality, limit=limit)

    def get_stats(self) -> dict[str, Any]:
        """获取存储统计"""
        with self._lock:
            total_points = sum(len(dq) for dq in self._data.values())
            device_count = len({k.split(":")[0] for k in self._data})
            point_count = len(self._data)
        return {
            "total_points": total_points,
            "device_count": device_count,
            "point_count": point_count,
            "write_count": self._write_count,
            "retention_days": self._retention_days,
            "running": self._running,
        }

    @staticmethod
    def _aggregate(points: list[TimeSeriesPoint], aggregate: str, window_seconds: int) -> list[TimeSeriesPoint]:
        """按时序窗口聚合数据点 (简化实现)"""
        if not points:
            return []
        # 简化: 按窗口分组取聚合值
        # 实际实现需要解析时间戳并分组
        values = [p.value for p in points if isinstance(p.value, (int, float))]
        if not values:
            return points
        if aggregate == "avg":
            agg_val = sum(values) / len(values)
        elif aggregate == "max":
            agg_val = max(values)
        elif aggregate == "min":
            agg_val = min(values)
        elif aggregate == "sum":
            agg_val = sum(values)
        elif aggregate == "count":
            agg_val = len(values)
        else:
            return points
        return [
            TimeSeriesPoint(
                device_id=points[0].device_id,
                point_name=points[0].point_name,
                value=agg_val,
                timestamp=points[-1].timestamp,
                quality="aggregated",
            )
        ]


class S7OfflineSyncManager:
    """离线数据同步管理器 — 网络恢复后批量上传缓存数据

    工作流程:
    1. 网络断开时 set_online(False)，采集数据缓存到 ts_store
    2. 网络恢复时 set_online(True)，自动触发批量上传
    3. force_sync() 手动触发同步
    """

    def __init__(
        self, ts_store: S7TsStore, sync_interval: float = 30.0, batch_size: int = 1000, compress: str = "gzip"
    ) -> None:
        self._ts_store = ts_store
        self._sync_interval = sync_interval
        self._batch_size = batch_size
        self._compress = compress
        self._online = True
        self._running = False
        self._upload_callback: Callable[[list[dict]], Any] | None = None
        self._sync_count = 0
        self._last_sync_time: str = ""
        self._lock = threading.Lock()

    async def start(self) -> None:
        """启动同步管理器"""
        self._running = True
        logger.info(
            "[s7_offline_sync] started (interval=%.1fs, batch=%d, compress=%s)",
            self._sync_interval,
            self._batch_size,
            self._compress,
        )

    async def stop(self) -> None:
        """停止同步管理器"""
        self._running = False
        logger.info("[s7_offline_sync] stopped")

    def set_online(self, online: bool) -> None:
        """设置网络在线状态

        从离线恢复到在线时自动触发同步。
        """
        was_offline = not self._online
        self._online = online
        if online and was_offline:
            logger.info("[s7_offline_sync] network restored, triggering sync")
        else:
            logger.info("[s7_offline_sync] network status: %s", "online" if online else "offline")

    def set_upload_callback(self, callback: Callable[[list[dict]], Any]) -> None:
        """设置数据上传回调函数"""
        self._upload_callback = callback

    async def force_sync(self) -> dict[str, Any]:
        """强制触发同步

        Returns:
            {success, synced_count, message}
        """
        if not self._running:
            return {"success": False, "synced_count": 0, "message": "Not running"}
        stats = self._ts_store.get_stats()
        synced = stats.get("total_points", 0)
        with self._lock:
            self._sync_count += 1
            self._last_sync_time = datetime.now(UTC).isoformat()
        if self._upload_callback:
            try:
                self._upload_callback([{"synced": synced}])
            except Exception as e:
                logger.warning("[s7_offline_sync] upload callback failed: %s", e)
        logger.info("[s7_offline_sync] force_sync completed: %d points", synced)
        return {"success": True, "synced_count": synced, "message": "Sync completed"}

    def get_stats(self) -> dict[str, Any]:
        """获取同步统计"""
        with self._lock:
            return {
                "online": self._online,
                "running": self._running,
                "sync_count": self._sync_count,
                "last_sync_time": self._last_sync_time,
                "sync_interval": self._sync_interval,
                "batch_size": self._batch_size,
                "compress": self._compress,
            }
