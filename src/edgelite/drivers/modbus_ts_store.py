"""Modbus 时序数据存储模块（仅 TCP 使用）

基于 aiosqlite 的轻量时序存储，参考 edgelite.storage.sqlite_ts 的实现模式：
- WAL 模式 + synchronous=NORMAL + busy_timeout=5000
- 批量写入 + 单一 asyncio.Lock 保护并发
- 支持 retention_days 过期数据清理

被 modbus_tcp.py 导入：from edgelite.drivers.modbus_ts_store import ModbusTsStore
TCP 调用：ModbusTsStore(retention_days=ts_retention)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


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

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_device_point_ts
ON device_points (device_id, point_name, timestamp_ns)
"""

_CREATE_INDEX_QUALITY_SQL = """
CREATE INDEX IF NOT EXISTS idx_device_quality
ON device_points (device_id, point_name, quality, timestamp_ns)
"""


class ModbusTsStore:
    """Modbus 时序数据存储（aiosqlite，异步）"""

    def __init__(self, retention_days: int = 7) -> None:
        self._db_path = "data/modbus_ts.db"
        self._retention_days = max(1, int(retention_days))
        self._db: aiosqlite.Connection | None = None
        self._db_lock = asyncio.Lock()
        self._write_count = 0
        self._pending_writes = 0
        self._max_pending = 200
        self._cleanup_every = 500  # 每 500 次写入触发一次过期清理
        # FIX: 后台写入队列，避免 SQLite 写入阻塞采集循环导致 asyncio.wait_for 超时
        self._write_queue: asyncio.Queue[tuple[str, list[tuple]] | None] = asyncio.Queue(maxsize=1000)
        self._writer_task: asyncio.Task | None = None

    async def start(self) -> None:
        """初始化数据库连接和表"""
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row

            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute("PRAGMA busy_timeout=5000")

            await self._db.execute(_CREATE_TABLE_SQL)
            await self._db.execute(_CREATE_INDEX_SQL)
            await self._db.execute(_CREATE_INDEX_QUALITY_SQL)
            await self._db.commit()

            logger.info(
                "[modbus_ts_store] 已启动: %s retention=%dd",
                self._db_path,
                self._retention_days,
            )
        except Exception:
            if self._db is not None:
                await self._db.close()
                self._db = None
            raise
        # FIX: 启动后台写入协程，将 SQLite 写入从采集循环中解耦
        self._writer_task = asyncio.create_task(self._writer_loop(), name="modbus-ts-writer")

    async def stop(self) -> None:
        """关闭数据库连接"""
        # FIX: 先停止后台写入任务
        if self._writer_task:
            await self._write_queue.put(None)  # 发送停止信号
            try:
                await asyncio.wait_for(self._writer_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._writer_task.cancel()
            self._writer_task = None
        if self._db is not None:
            try:
                async with self._db_lock:
                    if self._pending_writes > 0:
                        await self._db.commit()
                        self._pending_writes = 0
            except Exception as e:
                logger.warning("[modbus_ts_store] 最终 commit 失败: %s", e)
            await self._db.close()
            self._db = None
        logger.info("[modbus_ts_store] 已关闭")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    @staticmethod
    def _dt_to_ns(dt: datetime | None) -> int:
        """datetime 转 ns 时间戳；None 用当前时间"""
        if dt is None:
            return time.time_ns()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1_000_000_000)

    @staticmethod
    def _extract_point_value(value: Any) -> tuple[Any, str, int]:
        """从 PointValue 或裸值提取 (value, quality, timestamp_ns)

        PointValue 具有 .value/.quality/.timestamp 属性；
        裸值（int/float/str/bool/None）直接使用，quality 默认 good，timestamp 用 now。
        """
        # duck typing 识别 PointValue-like 对象
        if (
            hasattr(value, "value")
            and hasattr(value, "quality")
            and not isinstance(value, (int, float, str, bool, bytes, type(None), dict, list, tuple))
        ):
            raw = value.value
            quality = getattr(value, "quality", "good") or "good"
            ts_dt = getattr(value, "timestamp", None)
            ts_ns = ModbusTsStore._dt_to_ns(ts_dt) if isinstance(ts_dt, datetime) else time.time_ns()
            return raw, quality, ts_ns
        return value, "good", time.time_ns()

    @staticmethod
    def _value_to_columns(value: Any) -> tuple[Any, Any, Any, Any]:
        """将值拆分为 (value_real, value_int, value_str, value_bool) 列"""
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

    async def write_read_result(self, device_id: str, result: dict[str, Any]) -> None:
        """写入一次读取结果（非阻塞，放入后台队列）

        result 是 dict[str, PointValue | 裸值]，key 为 point_name。
        TCP 调用：await self._ts_store.write_read_result(device_id, result)

        FIX: 原实现在 read_points 内同步调用 executemany，当 SQLite 写入慢时
        会被调度器的 asyncio.wait_for(timeout=5s) 取消，导致 CancelledError。
        改为将行数据放入后台队列，由 _writer_loop 异步消费，不阻塞采集循环。
        """
        if not self._db or not result:
            return

        rows = []
        now_created = time.time()
        for point_name, value in result.items():
            # 跳过非点位字段（如 rule_engine/active_alarms 等统计字段）
            if not isinstance(point_name, str) or not point_name:
                continue
            raw, quality, ts_ns = self._extract_point_value(value)
            v_real, v_int, v_str, v_bool = self._value_to_columns(raw)
            rows.append((device_id, point_name, quality, v_real, v_int, v_str, v_bool, ts_ns, now_created))

        if not rows:
            return

        try:
            self._write_queue.put_nowait((device_id, rows))
        except asyncio.QueueFull:
            # 队列满时丢弃最旧数据，防止内存溢出
            logger.warning("[modbus_ts_store] 写入队列已满，丢弃数据")
            try:
                self._write_queue.get_nowait()
                self._write_queue.put_nowait((device_id, rows))
            except asyncio.QueueEmpty:
                pass

    async def _writer_loop(self) -> None:
        """FIX: 后台写入协程，从队列消费数据并写入 SQLite。

        将 SQLite executemany 从采集循环中解耦：
        - 采集循环只需 put_nowait 到队列，立即返回
        - 本协程在后台逐批消费，即使 SQLite 慢也不阻塞采集
        """
        while True:
            item = await self._write_queue.get()
            if item is None:
                # 停止信号
                break
            device_id, rows = item
            try:
                async with self._db_lock:
                    await self._db.executemany(
                        """INSERT OR REPLACE INTO device_points
                           (device_id, point_name, quality, value_real, value_int,
                            value_str, value_bool, timestamp_ns, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        rows,
                    )
                    self._write_count += len(rows)
                    self._pending_writes += len(rows)
                    if self._pending_writes >= self._max_pending:
                        await self._db.commit()
                        self._pending_writes = 0
                # 周期性清理过期数据
                if self._write_count % self._cleanup_every < len(rows):
                    await self._cleanup_old()
            except asyncio.CancelledError:
                # 优雅退出：尝试提交未写入的数据
                try:
                    async with self._db_lock:
                        if self._pending_writes > 0:
                            await self._db.commit()
                            self._pending_writes = 0
                except Exception:
                    pass
                raise
            except Exception as e:
                logger.error("[modbus_ts_store] 后台写入失败: %s", e)
                try:
                    async with self._db_lock:
                        await self._db.rollback()
                except Exception as rollback_err:
                    logger.debug("[modbus_ts_store] rollback failed: %s", rollback_err)

    async def _cleanup_old(self) -> None:
        """清理超过 retention_days 的过期数据"""
        if not self._db:
            return
        cutoff_ns = int((time.time() - self._retention_days * 86400) * 1_000_000_000)
        try:
            async with self._db_lock:
                cursor = await self._db.execute(
                    "DELETE FROM device_points WHERE timestamp_ns < ?",
                    (cutoff_ns,),
                )
                deleted = cursor.rowcount if cursor else 0
                await self._db.commit()
            if deleted > 0:
                logger.debug("[modbus_ts_store] 清理过期数据 %d 条", deleted)
        except Exception as e:
            logger.warning("[modbus_ts_store] 清理过期数据失败: %s", e)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """将查询行转为结果 dict"""
        ts_ns = row["timestamp_ns"]
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).isoformat()
        v_real = row["value_real"]
        v_int = row["value_int"]
        v_str = row["value_str"]
        v_bool = row["value_bool"]
        value = (
            v_real
            if v_real is not None
            else (
                v_int
                if v_int is not None
                else (v_str if v_str is not None else (bool(v_bool) if v_bool is not None else None))
            )
        )
        return {
            "time": ts,
            "value": value,
            "quality": row["quality"] or "good",
        }

    async def query(
        self,
        device_id: str,
        point_name: str,
        start_time: datetime,
        end_time: datetime | None = None,
        quality: str | None = None,
        aggregate: str | None = None,
        window_seconds: int | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """按时序查询点位数据，支持质量过滤与聚合"""
        if not self._db:
            return []

        start_ns = self._dt_to_ns(start_time)
        end_ns = self._dt_to_ns(end_time) if end_time is not None else time.time_ns()

        # 聚合查询
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
                             AND timestamp_ns BETWEEN ? AND ?
                             AND quality = ?
                           ORDER BY timestamp_ns ASC
                           LIMIT ?""",
                        (device_id, point_name, start_ns, end_ns, quality, limit),
                    )
                else:
                    cursor = await self._db.execute(
                        """SELECT timestamp_ns, value_real, value_int, value_str, value_bool, quality
                           FROM device_points
                           WHERE device_id = ? AND point_name = ?
                             AND timestamp_ns BETWEEN ? AND ?
                           ORDER BY timestamp_ns ASC
                           LIMIT ?""",
                        (device_id, point_name, start_ns, end_ns, limit),
                    )
                rows = await cursor.fetchall()
                return [self._row_to_dict(r) for r in rows]
        except Exception as e:
            logger.error("[modbus_ts_store] 查询失败: %s", e)
            return []

    async def _query_aggregate(
        self,
        device_id: str,
        point_name: str,
        start_ns: int,
        end_ns: int,
        aggregate: str,
        window_seconds: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        """聚合查询（mean/avg/max/min/sum/count）"""
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
        agg_expr = agg_map.get(str(aggregate).lower(), "AVG(COALESCE(value_real, value_int))")
        try:
            async with self._db_lock:
                cursor = await self._db.execute(
                    f"""SELECT
                           (timestamp_ns / ?) * ? AS window_start,
                           {agg_expr} AS agg_value
                       FROM device_points
                       WHERE device_id = ? AND point_name = ?
                         AND timestamp_ns BETWEEN ? AND ?
                         AND (value_real IS NOT NULL OR value_int IS NOT NULL)
                       GROUP BY (timestamp_ns / ?)
                       ORDER BY window_start ASC
                       LIMIT ?""",
                    (window_ns, window_ns, device_id, point_name, start_ns, end_ns, window_ns, limit),
                )
                rows = await cursor.fetchall()
                results = []
                for row in rows:
                    ts = datetime.fromtimestamp(row[0] / 1e9, tz=UTC).isoformat()
                    results.append({"time": ts, "value": row[1], "quality": "good"})
                return results
        except Exception as e:
            logger.error("[modbus_ts_store] 聚合查询失败: %s", e)
            return []

    async def query_latest(self, device_id: str, point_names: list[str]) -> dict[str, dict[str, Any]]:
        """查询多个点位的最新值"""
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
                                  PARTITION BY point_name
                                  ORDER BY timestamp_ns DESC, id DESC
                                ) AS rn
                         FROM device_points
                         WHERE device_id = ? AND point_name IN ({placeholders})
                       ) WHERE rn = 1""",
                    (device_id, *point_names),
                )
                rows = await cursor.fetchall()
                result: dict[str, dict[str, Any]] = {}
                for row in rows:
                    pn = row["point_name"]
                    ts = datetime.fromtimestamp(row["timestamp_ns"] / 1e9, tz=UTC).isoformat()
                    v_real = row["value_real"]
                    v_int = row["value_int"]
                    v_str = row["value_str"]
                    v_bool = row["value_bool"]
                    value = (
                        v_real
                        if v_real is not None
                        else (
                            v_int
                            if v_int is not None
                            else (v_str if v_str is not None else (bool(v_bool) if v_bool is not None else None))
                        )
                    )
                    result[pn] = {"time": ts, "value": value, "quality": row["quality"] or "good"}
                return result
        except Exception as e:
            logger.error("[modbus_ts_store] 最新值查询失败: %s", e)
            return {}

    async def query_by_quality(
        self,
        device_id: str,
        point_name: str,
        start_time: datetime,
        end_time: datetime | None = None,
        quality: str = "bad",
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """按质量等级查询点位数据"""
        if not self._db:
            return []
        start_ns = self._dt_to_ns(start_time)
        end_ns = self._dt_to_ns(end_time) if end_time is not None else time.time_ns()
        try:
            async with self._db_lock:
                cursor = await self._db.execute(
                    """SELECT timestamp_ns, value_real, value_int, value_str, value_bool, quality
                       FROM device_points
                       WHERE device_id = ? AND point_name = ?
                         AND timestamp_ns BETWEEN ? AND ?
                         AND quality = ?
                       ORDER BY timestamp_ns ASC
                       LIMIT ?""",
                    (device_id, point_name, start_ns, end_ns, quality, limit),
                )
                rows = await cursor.fetchall()
                return [self._row_to_dict(r) for r in rows]
        except Exception as e:
            logger.error("[modbus_ts_store] 按质量查询失败: %s", e)
            return []

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def get_stats(self) -> dict[str, Any]:
        """返回存储统计信息（同步）

        注意：此方法为同步，不访问 aiosqlite 连接（异步连接不能在同步上下文使用），
        仅返回内存中累计的写入计数与文件大小。
        """
        db_size = 0
        if os.path.exists(self._db_path):
            try:
                db_size = os.path.getsize(self._db_path)
            except OSError:
                db_size = 0
        return {
            "total_writes": self._write_count,
            "pending_writes": self._pending_writes,
            "db_path": self._db_path,
            "db_size_bytes": db_size,
            "retention_days": self._retention_days,
            "connected": self._db is not None,
        }
