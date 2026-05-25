"""SQLite时序存储 - InfluxDB降级方案

当InfluxDB不可用时，自动降级到SQLite时序存储，支持基本的时序查询。
InfluxDB恢复后，SQLite中的数据增量同步回InfluxDB。
"""

from __future__ import annotations

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
    measurement TEXT NOT NULL,
    device_id TEXT NOT NULL,
    point_name TEXT NOT NULL,
    quality TEXT NOT NULL DEFAULT 'good',
    value_real REAL,
    value_int INTEGER,
    value_str TEXT,
    value_bool INTEGER,
    tags_json TEXT,
    timestamp_ns INTEGER NOT NULL,
    created_at REAL NOT NULL
)
"""

_CREATE_INDEX_DEVICE_POINT_TS = """
CREATE INDEX IF NOT EXISTS idx_device_point_ts
ON device_points (device_id, point_name, timestamp_ns)
"""

_CREATE_INDEX_TS = """
CREATE INDEX IF NOT EXISTS idx_timestamp_ns
ON device_points (timestamp_ns)
"""

_CREATE_INDEX_ID = """
CREATE INDEX IF NOT EXISTS idx_id
ON device_points (id)
"""


class SqliteTimeSeriesStorage:
    """SQLite-based time series storage as InfluxDB fallback"""

    def __init__(self, db_path: str = "data/edgelite_ts.db"):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_count = 0
        self._sync_offset = 0

    async def start(self) -> None:
        """Initialize database and create tables"""
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row

        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")

        await self._db.execute(_CREATE_TABLE_SQL)
        await self._db.execute(_CREATE_INDEX_DEVICE_POINT_TS)
        await self._db.execute(_CREATE_INDEX_TS)
        await self._db.execute(_CREATE_INDEX_ID)
        await self._db.commit()

        # 恢复同步偏移量
        await self._restore_sync_offset()

        logger.info("SQLite时序存储已启动: %s", self._db_path)

    async def _restore_sync_offset(self) -> None:
        """从数据库恢复同步偏移量"""
        if not self._db:
            return
        try:
            cursor = await self._db.execute(
                "SELECT COALESCE(MAX(id), 0) FROM device_points"
            )
            row = await cursor.fetchone()
            if row:
                self._sync_offset = row[0]
        except Exception as e:
            logger.debug("恢复同步偏移量失败: %s", e)
            self._sync_offset = 0

    async def stop(self) -> None:
        """Close database connection"""
        if self._db:
            await self._db.close()
            self._db = None
        logger.info("SQLite时序存储已关闭")

    async def write_point(
        self,
        measurement: str,
        device_id: str,
        point_name: str,
        value: Any,
        quality: str = "good",
        timestamp_ns: int | None = None,
        tags: dict | None = None,
    ) -> None:
        """Write a single data point"""
        if not self._db:
            return

        if timestamp_ns is None:
            timestamp_ns = time.time_ns()

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
            value_str = str(value)

        tags_json = None
        if tags:
            import json
            tags_json = json.dumps(tags, ensure_ascii=False)

        try:
            await self._db.execute(
                """INSERT INTO device_points
                   (measurement, device_id, point_name, quality,
                    value_real, value_int, value_str, value_bool,
                    tags_json, timestamp_ns, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    measurement,
                    device_id,
                    point_name,
                    quality,
                    value_real,
                    value_int,
                    value_str,
                    value_bool,
                    tags_json,
                    timestamp_ns,
                    time.time(),
                ),
            )
            self._write_count += 1
            if self._write_count % 100 == 0:
                await self._db.commit()
        except Exception as e:
            logger.error("SQLite写入失败: %s", e)

    async def write_points_batch(self, points: list[dict]) -> None:
        """Batch write data points"""
        if not self._db or not points:
            return

        rows = []
        for p in points:
            value = p.get("value")
            timestamp_ns = p.get("timestamp_ns") or time.time_ns()

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

            tags = p.get("tags")
            tags_json = None
            if tags:
                import json
                tags_json = json.dumps(tags, ensure_ascii=False)

            rows.append(
                (
                    p.get("measurement", "device_points"),
                    p.get("device_id", ""),
                    p.get("point_name", ""),
                    p.get("quality", "good"),
                    value_real,
                    value_int,
                    value_str,
                    value_bool,
                    tags_json,
                    timestamp_ns,
                    time.time(),
                )
            )

        try:
            await self._db.executemany(
                """INSERT INTO device_points
                   (measurement, device_id, point_name, quality,
                    value_real, value_int, value_str, value_bool,
                    tags_json, timestamp_ns, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
            await self._db.commit()
            self._write_count += len(rows)
        except Exception as e:
            logger.error("SQLite批量写入失败: %s", e)

    async def query_points(
        self,
        device_id: str,
        point_name: str,
        start_ns: int,
        stop_ns: int | None = None,
        aggregate: str | None = None,
        window_seconds: int | None = None,
        limit: int = 10000,
    ) -> list[dict]:
        """Query time series data with optional aggregation"""
        if not self._db:
            return []

        if stop_ns is None:
            stop_ns = time.time_ns()

        if aggregate and window_seconds:
            return await self._query_aggregate(
                device_id, point_name, start_ns, stop_ns, aggregate, window_seconds, limit
            )

        try:
            cursor = await self._db.execute(
                """SELECT timestamp_ns, value_real, value_int, value_str, value_bool, quality
                   FROM device_points
                   WHERE device_id = ? AND point_name = ?
                     AND timestamp_ns BETWEEN ? AND ?
                   ORDER BY timestamp_ns ASC
                   LIMIT ?""",
                (device_id, point_name, start_ns, stop_ns, limit),
            )
            rows = await cursor.fetchall()
            return [self._row_to_dict(row) for row in rows]
        except Exception as e:
            logger.error("SQLite查询失败: %s", e)
            return []

    async def _query_aggregate(
        self,
        device_id: str,
        point_name: str,
        start_ns: int,
        stop_ns: int,
        aggregate: str,
        window_seconds: int,
        limit: int,
    ) -> list[dict]:
        """Aggregated query using SQL aggregate functions"""
        if not self._db:
            return []

        window_ns = window_seconds * 1_000_000_000
        agg_map = {
            "mean": "AVG(value_real)",
            "avg": "AVG(value_real)",
            "max": "MAX(value_real)",
            "min": "MIN(value_real)",
            "sum": "SUM(value_real)",
            "count": "COUNT(value_real)",
        }
        agg_expr = agg_map.get(aggregate.lower(), "AVG(value_real)")

        try:
            cursor = await self._db.execute(
                f"""SELECT
                       (timestamp_ns / ?) * ? AS window_start,
                       {agg_expr} AS agg_value,
                       quality
                   FROM device_points
                   WHERE device_id = ? AND point_name = ?
                     AND timestamp_ns BETWEEN ? AND ?
                     AND value_real IS NOT NULL
                   GROUP BY (timestamp_ns / ?)
                   ORDER BY window_start ASC
                   LIMIT ?""",
                (window_ns, window_ns, device_id, point_name, start_ns, stop_ns, window_ns, limit),
            )
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                window_start = row[0]
                ts = datetime.fromtimestamp(window_start / 1e9, tz=UTC).isoformat()
                results.append({
                    "time": ts,
                    "value": row[1],
                    "quality": row[2] or "good",
                })
            return results
        except Exception as e:
            logger.error("SQLite聚合查询失败: %s", e)
            return []

    async def query_latest(
        self, device_id: str, point_names: list[str]
    ) -> dict[str, dict]:
        """Query latest values for points"""
        if not self._db or not point_names:
            return {}

        placeholders = ",".join("?" for _ in point_names)
        try:
            cursor = await self._db.execute(
                f"""SELECT point_name, timestamp_ns, value_real, value_int,
                           value_str, value_bool, quality
                   FROM device_points
                   WHERE device_id = ? AND point_name IN ({placeholders})
                     AND id IN (
                       SELECT MAX(id) FROM device_points
                       WHERE device_id = ? AND point_name IN ({placeholders})
                       GROUP BY point_name
                     )""",
                (device_id, *point_names, device_id, *point_names),
            )
            rows = await cursor.fetchall()
            result = {}
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
                result[pn] = {
                    "time": ts,
                    "value": value,
                    "quality": row[6] or "good",
                }
            return result
        except Exception as e:
            logger.error("SQLite最新值查询失败: %s", e)
            return {}

    async def get_unsynced_count(self) -> int:
        """Get count of records not yet synced to InfluxDB"""
        if not self._db:
            return 0
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) FROM device_points WHERE id > ?", (self._sync_offset,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
        except Exception as e:
            logger.error("SQLite获取未同步计数失败: %s", e)
            return 0

    async def get_unsynced_records(self, limit: int = 1000) -> list[dict]:
        """Get unsynced records for incremental sync"""
        if not self._db:
            return []
        try:
            cursor = await self._db.execute(
                """SELECT id, measurement, device_id, point_name, quality,
                          value_real, value_int, value_str, value_bool,
                          tags_json, timestamp_ns
                   FROM device_points
                   WHERE id > ?
                   ORDER BY id ASC
                   LIMIT ?""",
                (self._sync_offset, limit),
            )
            rows = await cursor.fetchall()
            results = []
            for row in rows:
                value = row[5] if row[5] is not None else (
                    row[6] if row[6] is not None else (
                        row[7] if row[7] is not None else (
                            bool(row[8]) if row[8] is not None else None
                        )
                    )
                )
                tags = None
                if row[9]:
                    import json
                    try:
                        tags = json.loads(row[9])
                    except (json.JSONDecodeError, TypeError):
                        tags = None

                results.append({
                    "id": row[0],
                    "measurement": row[1],
                    "device_id": row[2],
                    "point_name": row[3],
                    "quality": row[4],
                    "value": value,
                    "tags": tags,
                    "timestamp_ns": row[10],
                })
            return results
        except Exception as e:
            logger.error("SQLite获取未同步记录失败: %s", e)
            return []

    async def mark_synced(self, max_id: int) -> None:
        """Mark records as synced up to given ID"""
        self._sync_offset = max_id

    async def cleanup_old_data(self, retention_days: int = 30) -> int:
        """Delete data older than retention period"""
        if not self._db:
            return 0
        cutoff_ns = int((time.time() - retention_days * 86400) * 1e9)
        try:
            cursor = await self._db.execute(
                "DELETE FROM device_points WHERE timestamp_ns < ?", (cutoff_ns,)
            )
            await self._db.commit()
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info("SQLite清理旧数据: 删除%d条 (retention=%d天)", deleted, retention_days)
            return deleted
        except Exception as e:
            logger.error("SQLite清理旧数据失败: %s", e)
            return 0

    async def get_stats(self) -> dict:
        """Get storage statistics"""
        if not self._db:
            return {
                "total_records": 0,
                "db_size_bytes": 0,
                "oldest_record": None,
                "newest_record": None,
                "unsynced_count": 0,
            }
        try:
            cursor = await self._db.execute("SELECT COUNT(*) FROM device_points")
            row = await cursor.fetchone()
            total = row[0] if row else 0

            db_size = 0
            if os.path.exists(self._db_path):
                db_size = os.path.getsize(self._db_path)

            cursor = await self._db.execute(
                "SELECT MIN(timestamp_ns), MAX(timestamp_ns) FROM device_points"
            )
            row = await cursor.fetchone()
            oldest = None
            newest = None
            if row and row[0]:
                oldest = datetime.fromtimestamp(row[0] / 1e9, tz=UTC).isoformat()
            if row and row[1]:
                newest = datetime.fromtimestamp(row[1] / 1e9, tz=UTC).isoformat()

            unsynced = await self.get_unsynced_count()

            return {
                "total_records": total,
                "db_size_bytes": db_size,
                "oldest_record": oldest,
                "newest_record": newest,
                "unsynced_count": unsynced,
            }
        except Exception as e:
            logger.error("SQLite获取统计信息失败: %s", e)
            return {
                "total_records": 0,
                "db_size_bytes": 0,
                "oldest_record": None,
                "newest_record": None,
                "unsynced_count": 0,
            }

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict:
        """Convert a query row to a result dict"""
        ts_ns = row[0]
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=UTC).isoformat()
        value = row[1] if row[1] is not None else (
            row[2] if row[2] is not None else (
                row[3] if row[3] is not None else (
                    bool(row[4]) if row[4] is not None else None
                )
            )
        )
        return {
            "time": ts,
            "value": value,
            "quality": row[5] or "good",
        }
