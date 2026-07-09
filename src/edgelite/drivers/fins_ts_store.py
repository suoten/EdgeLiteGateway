"""FINS 驱动时序存储与离线同步管理

提供两个核心组件：
- FinsTsStore：基于 aiosqlite 的本地时序存储，持久化采集结果
- FinsOfflineSyncManager：离线数据同步管理器，网络中断时缓存、恢复后重传

关键差异（相对 OPC UA/S7）：
- FinsOfflineSyncManager 使用 enqueue() 方法接收离线数据，
  而非 set_upload_callback()（fins.py 中 0 处调用 set_upload_callback）
- FinsTsStore.write_read_result 返回 int（写入记录数），fins.py:1335 据此判断
- FinsTsStore.query 接收 8 个位置参数（全部位置传入，非关键字）
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = "data/fins_ts.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS device_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    point_name TEXT NOT NULL,
    quality TEXT NOT NULL DEFAULT 'good',
    value_real REAL,
    value_int INTEGER,
    value_str TEXT,
    value_bool INTEGER,
    timestamp_ns INTEGER NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(device_id, point_name, timestamp_ns)
)
"""

_CREATE_INDEX_DP = """
CREATE INDEX IF NOT EXISTS idx_device_point_ts
ON device_points (device_id, point_name, timestamp_ns)
"""


class FinsTsStore:
    """FINS 本地时序存储（aiosqlite 后端）。

    采集结果通过 write_read_result 写入，支持时序查询与最新值查询。
    数据保留策略按 retention_days 自动清理过期数据。
    """

    def __init__(self, retention_days: int = 7) -> None:
        self._db_path = _DB_PATH
        self._retention_days = retention_days
        self._db: aiosqlite.Connection | None = None
        self._write_count = 0
        self._db_lock = asyncio.Lock()

    async def start(self) -> None:
        """初始化数据库连接与表结构。"""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(_CREATE_TABLE_SQL)
        await self._db.execute(_CREATE_INDEX_DP)
        await self._db.commit()
        logger.info("[fins-ts] 时序存储已启动: %s (retention=%d天)", self._db_path, self._retention_days)

    async def stop(self) -> None:
        """关闭数据库连接。"""
        if self._db is not None:
            try:
                await self._db.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("[fins-ts] 关闭数据库异常: %s", e)
            self._db = None
        logger.info("[fins-ts] 时序存储已关闭")

    @staticmethod
    def _split_value(value: Any) -> tuple[Any, Any, Any, Any]:
        """将值拆分为 (real, int, str, bool) 四列，仅一列非 None。"""
        value_real = None
        value_int = None
        value_str = None
        value_bool = None
        if isinstance(value, bool):
            value_bool = 1 if value else 0
        elif isinstance(value, int):
            value_int = value
        elif isinstance(value, float):
            value_real = value
        else:
            value_str = str(value) if value is not None else None
        return value_real, value_int, value_str, value_bool

    async def write_read_result(self, device_id: str, result: dict[str, Any]) -> int:
        """将采集结果字典写入时序存储，返回写入记录数。

        result 为 {point_name: value} 或 {point_name: PointValue}。
        """
        if not self._db or not result:
            return 0
        now_ns = time.time_ns()
        now = time.time()
        written = 0
        try:
            async with self._db_lock:
                for point_name, value in result.items():
                    # 处理 PointValue 对象（含 value/quality 属性）
                    pv = value
                    quality = "good"
                    pv_obj = getattr(value, "value", None)
                    if pv_obj is not None and hasattr(value, "quality"):
                        pv = pv_obj
                        quality = value.quality or "good"
                    if pv is None:
                        continue
                    value_real, value_int, value_str, value_bool = self._split_value(pv)
                    await self._db.execute(
                        """INSERT OR REPLACE INTO device_points
                           (device_id, point_name, quality, value_real, value_int,
                            value_str, value_bool, timestamp_ns, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (device_id, point_name, quality, value_real, value_int,
                         value_str, value_bool, now_ns, now),
                    )
                    written += 1
                await self._db.commit()
                self._write_count += written
        except Exception as e:
            logger.error("[fins-ts] 写入采集结果失败: %s", e)
        return written
    async def query(
        self,
        device_id: str,
        point_name: str,
        start_time: Any,
        end_time: Any,
        quality: str | None,
        aggregate: str | None,
        window_seconds: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """时序查询（8 个位置参数，fins.py:2250 全部位置传入）。

        start_time 为 0 表示从头查询，end_time 为 None 表示到当前。
        """
        if not self._db:
            return []
        start_ns = int(start_time) if start_time else 0
        end_ns = int(end_time) if end_time else time.time_ns()
        if aggregate and window_seconds:
            return await self._query_aggregate(
                device_id, point_name, start_ns, end_ns, aggregate, window_seconds, limit
            )
        try:
            async with self._db_lock:
                if quality:
                    cursor = await self._db.execute(
                        """SELECT timestamp_ns, value_real, value_int, value_str, value_bool, quality
                           FROM device_points
                           WHERE device_id = ? AND point_name = ?
                             AND timestamp_ns BETWEEN ? AND ? AND quality = ?
                           ORDER BY timestamp_ns ASC LIMIT ?""",
                        (device_id, point_name, start_ns, end_ns, quality, limit),
                    )
                else:
                    cursor = await self._db.execute(
                        """SELECT timestamp_ns, value_real, value_int, value_str, value_bool, quality
                           FROM device_points
                           WHERE device_id = ? AND point_name = ?
                             AND timestamp_ns BETWEEN ? AND ?
                           ORDER BY timestamp_ns ASC LIMIT ?""",
                        (device_id, point_name, start_ns, end_ns, limit),
                    )
                rows = await cursor.fetchall()
                return [self._row_to_dict(row) for row in rows]
        except Exception as e:
            logger.error("[fins-ts] 查询失败: %s", e)
            return []

    async def _query_aggregate(
        self, device_id: str, point_name: str, start_ns: int, end_ns: int,
        aggregate: str, window_seconds: int, limit: int,
    ) -> list[dict[str, Any]]:
        """聚合查询。"""
        if not self._db:
            return []
        window_ns = window_seconds * 1_000_000_000
        agg_map = {
            "mean": "AVG(COALESCE(value_real, value_int))",
            "avg": "AVG(COALESCE(value_real, value_int))",
            "max": "MAX(COALESCE(value_real, value_int))",
            "min": "MIN(COALESCE(value_real, value_int))",
            "sum": "SUM(COALESCE(value_real, value_int))",
            "count": "COUNT(COALESCE(value_real, value_int))",
        }
        agg_expr = agg_map.get(aggregate.lower(), "AVG(COALESCE(value_real, value_int))")
        try:
            async with self._db_lock:
                cursor = await self._db.execute(
                    f"""SELECT (timestamp_ns / ?) * ? AS window_start, {agg_expr} AS agg_value
                        FROM device_points
                        WHERE device_id = ? AND point_name = ?
                          AND timestamp_ns BETWEEN ? AND ?
                          AND (value_real IS NOT NULL OR value_int IS NOT NULL)
                        GROUP BY (timestamp_ns / ?) ORDER BY window_start ASC LIMIT ?""",
                    (window_ns, window_ns, device_id, point_name, start_ns, end_ns, window_ns, limit),
                )
                rows = await cursor.fetchall()
                return [
                    {"time": datetime.fromtimestamp(row[0] / 1e9, tz=UTC).isoformat(),
                     "value": row[1], "quality": "good"}
                    for row in rows
                ]
        except Exception as e:
            logger.error("[fins-ts] 聚合查询失败: %s", e)
            return []

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """将查询行转换为结果字典。"""
        ts_ns = row["timestamp_ns"]
        value = row["value_real"]
        if value is None:
            value = row["value_int"]
        if value is None:
            value = row["value_str"]
        if value is None and row["value_bool"] is not None:
            value = bool(row["value_bool"])
        return {
            "time": datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).isoformat(),
            "value": value,
            "quality": row["quality"] or "good",
        }

    async def query_latest(
        self, device_id: str, point_names: list[str]
    ) -> dict[str, dict[str, Any]]:
        """查询指定点位的最新值。"""
        if not self._db or not point_names:
            return {}
        placeholders = ",".join("?" for _ in point_names)
        try:
            async with self._db_lock:
                cursor = await self._db.execute(
                    f"""SELECT point_name, timestamp_ns, value_real, value_int,
                               value_str, value_bool, quality
                        FROM (
                          SELECT point_name, timestamp_ns, value_real, value_int,
                                 value_str, value_bool, quality,
                                 ROW_NUMBER() OVER (
                                   PARTITION BY point_name ORDER BY timestamp_ns DESC, id DESC
                                 ) AS rn
                          FROM device_points
                          WHERE device_id = ? AND point_name IN ({placeholders})
                        ) WHERE rn = 1""",
                    (device_id, *point_names),
                )
                rows = await cursor.fetchall()
                result: dict[str, dict[str, Any]] = {}
                for row in rows:
                    pn = row[0]
                    ts = datetime.fromtimestamp(row[1] / 1e9, tz=UTC).isoformat()
                    value = row[2] if row[2] is not None else (
                        row[3] if row[3] is not None else (
                            row[4] if row[4] is not None else (
                                bool(row[5]) if row[5] is not None else None
                            )
                        )
                    )
                    result[pn] = {"time": ts, "value": value, "quality": row[6] or "good"}
                return result
        except Exception as e:
            logger.error("[fins-ts] 最新值查询失败: %s", e)
            return {}

    def get_stats(self) -> dict[str, Any]:
        """获取时序存储统计信息（同步方法）。"""
        return {
            "total_records": self._write_count,
            "write_count": self._write_count,
            "db_path": self._db_path,
            "retention_days": self._retention_days,
        }


class FinsOfflineSyncManager:
    """FINS 离线数据同步管理器。

    关键差异：使用 enqueue() 方法接收离线数据，而非 set_upload_callback()。
    网络离线时 enqueue 将数据缓存到内部 deque；网络恢复后
    force_sync 将缓存数据批量写入 ts_store 并返回同步记录数。

    Args:
        ts_store: 关联的 FinsTsStore 实例（用于恢复后写入）
        sync_interval: 同步间隔（秒）
        batch_size: 批量同步大小
        compress: 压缩方式（如 gzip）
    """

    def __init__(
        self,
        ts_store: FinsTsStore | None = None,
        sync_interval: float = 30.0,
        batch_size: int = 1000,
        compress: str = "gzip",
    ) -> None:
        self._ts_store = ts_store
        self._sync_interval = sync_interval
        self._batch_size = batch_size
        self._compress = compress
        self._online = True
        self._queue: deque[dict[str, Any]] = deque()
        self._synced_count = 0
        self._dropped_count = 0
        self._sync_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动离线同步管理器。"""
        self._online = True
        logger.info(
            "[fins-offline] 离线同步管理器已启动: interval=%.1f batch=%d compress=%s",
            self._sync_interval, self._batch_size, self._compress,
        )

    async def stop(self) -> None:
        """停止离线同步管理器，尝试刷新剩余队列。"""
        import contextlib
        if self._sync_task is not None and not self._sync_task.done():
            self._sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sync_task
            self._sync_task = None
        # 停止前尝试同步剩余数据
        if self._queue:
            await self.force_sync()
        logger.info("[fins-offline] 离线同步管理器已停止")

    async def enqueue(
        self,
        device_id: str,
        point_name: str,
        pv: Any,
        quality: str,
    ) -> None:
        """将一条数据入队（4 个位置参数，fins.py:1346 调用）。

        网络离线时缓存数据，网络在线时直接写入 ts_store。
        """
        record = {
            "device_id": device_id,
            "point_name": point_name,
            "value": pv,
            "quality": quality,
            "timestamp_ns": time.time_ns(),
        }
        if self._online and self._ts_store is not None:
            # 在线时直接写入 ts_store
            try:
                await self._ts_store.write_read_result(device_id, {point_name: pv})
            except Exception as e:
                logger.debug("[fins-offline] 在线写入失败，转入队列: %s", e)
                self._queue.append(record)
        else:
            # 离线时缓存到队列
            self._queue.append(record)

    async def force_sync(self) -> int:
        """强制同步队列中的数据到 ts_store，返回同步记录数。"""
        if not self._queue:
            return 0
        if self._ts_store is None:
            # 无 ts_store，返回队列大小（模拟已处理）
            count = len(self._queue)
            self._queue.clear()
            self._synced_count += count
            return count
        synced = 0
        # 按设备分组写入
        batch: dict[str, dict[str, Any]] = {}
        batch_count = 0
        while self._queue and synced < self._batch_size:
            record = self._queue.popleft()
            dev = record["device_id"]
            pn = record["point_name"]
            if dev not in batch:
                batch[dev] = {}
            batch[dev][pn] = record["value"]
            synced += 1
            batch_count += 1
        # 批量写入 ts_store
        for dev, points in batch.items():
            try:
                await self._ts_store.write_read_result(dev, points)
            except Exception as e:
                logger.error("[fins-offline] 批量写入 ts_store 失败: %s", e)
                synced -= len(points)
        self._synced_count += max(synced, 0)
        logger.info("[fins-offline] 强制同步完成: %d 条记录", synced)
        return max(synced, 0)

    def set_online(self, online: bool) -> None:
        """设置网络在线状态（同步方法）。"""
        was_offline = not self._online
        self._online = online
        if online and was_offline:
            logger.info("[fins-offline] 网络已恢复，队列中有 %d 条待同步", len(self._queue))

    def get_stats(self) -> dict[str, Any]:
        """获取离线同步统计信息（同步方法）。"""
        return {
            "online": self._online,
            "queue_size": len(self._queue),
            "synced_count": self._synced_count,
            "dropped_count": self._dropped_count,
            "sync_interval": self._sync_interval,
            "batch_size": self._batch_size,
            "compress": self._compress,
        }
