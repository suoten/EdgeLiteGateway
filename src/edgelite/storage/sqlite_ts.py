"""SQLite时序存储 - InfluxDB降级方案

当InfluxDB不可用时，自动降级到SQLite时序存储，支持基本的时序查询。
InfluxDB恢复后，SQLite中的数据增量同步回InfluxDB。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
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
    created_at REAL NOT NULL,
    UNIQUE(device_id, point_name, timestamp_ns)
)
"""

# FIXED-P0: 为已存在的表补充 UNIQUE 索引（CREATE TABLE IF NOT EXISTS 不会更新已有表结构）
_CREATE_UNIQUE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_device_points_unique
ON device_points (device_id, point_name, timestamp_ns)
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

# FIXED: 覆盖索引——为聚合查询 query_aggregate 优化，避免全表扫描回表读取 value_real/value_int
# 查询模式: WHERE device_id=? AND point_name=? AND timestamp_ns BETWEEN ? AND ?
#          + AGG(value_real, value_int) GROUP BY (timestamp_ns / ?)
# 覆盖索引包含所有查询引用的列，SQLite 可仅从索引满足查询（Index-Only Scan）
_CREATE_INDEX_AGG = """
CREATE INDEX IF NOT EXISTS idx_device_points_agg
ON device_points (device_id, point_name, timestamp_ns, value_real, value_int)
"""


class SqliteTimeSeriesStorage:
    """SQLite-based time series storage as InfluxDB fallback"""

    def __init__(self, db_path: str = "data/edgelite_ts.db"):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._write_count = 0
        self._sync_offset = 0
        # FIXED-P1: 原问题-单aiosqlite连接使用分离的读写锁，读写在各自锁下可并发访问同一连接导致竞态；统一使用单一锁_db_lock
        self._db_lock = asyncio.Lock()
        # FIXED-P1: 原问题-write_point每次写入立即commit，高频写入时fsync成为瓶颈；改为批量提交+定时刷盘
        self._pending_writes = 0
        self._flush_interval = 1.0
        self._max_pending = 200
        self._flush_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Initialize database and create tables"""
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        if path.exists():  # FIXED-P2: 原问题-无integrity_check，损坏时静默返回空数据
            try:
                # 使用同步sqlite3在独立线程中执行完整性检查，避免aiosqlite在uvloop下线程冲突
                import concurrent.futures

                def _check_integrity(db_path):
                    conn = sqlite3.connect(db_path)
                    try:
                        return conn.execute("PRAGMA integrity_check").fetchone()
                    finally:
                        conn.close()

                loop = asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    row = await loop.run_in_executor(executor, _check_integrity, self._db_path)
                if row and row[0] != "ok":
                    logger.warning("SQLite时序DB完整性检查失败: %s，备份并重建", row[0])
                    corrupt_backup = f"{self._db_path}.corrupt.{int(time.time())}"
                    try:
                        import shutil

                        shutil.move(self._db_path, corrupt_backup)
                        logger.warning("损坏时序DB已备份到: %s", corrupt_backup)
                    except Exception:
                        path.unlink(missing_ok=True)
                    # FIXED-P1: 原问题-时序DB损坏后仅重建空库，无备份恢复
                    # 尝试从backups/目录恢复最新备份
                    try:
                        backup_dir = Path("data/backups")
                        db_name = Path(self._db_path).name
                        if backup_dir.exists():
                            backup_files = sorted(
                                (
                                    f
                                    for f in backup_dir.glob(f"{db_name}.backup.*")
                                    if not f.name.endswith(("-wal", "-shm"))
                                ),
                                key=lambda p: p.stat().st_mtime,
                                reverse=True,
                            )
                            if backup_files:
                                shutil.copy2(str(backup_files[0]), self._db_path)
                                logger.info("SQLite时序DB从备份恢复: %s", backup_files[0])
                                # FIXED-P2: 恢复后验证备份完整性（使用同步sqlite3，在独立线程中执行避免与aiosqlite冲突）
                                import concurrent.futures

                                def _verify_integrity(db_path):
                                    conn = sqlite3.connect(db_path)
                                    try:
                                        return conn.execute("PRAGMA integrity_check").fetchone()
                                    finally:
                                        conn.close()

                                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                                    vr = executor.submit(_verify_integrity, self._db_path).result()
                                if not vr or vr[0] != "ok":
                                    logger.warning(
                                        "恢复的备份完整性检查失败: %s，删除并重建空库", vr[0] if vr else "None"
                                    )
                                    Path(self._db_path).unlink(missing_ok=True)
                    except Exception as restore_err:
                        logger.warning("SQLite时序DB从备份恢复失败: %s", restore_err)
            except Exception as e:
                # FIXED-P2: 原问题-完整性预检抛异常（如严重损坏的 DatabaseError）时仅记录日志，
                # 未备份/删除损坏文件，导致后续 aiosqlite.connect 在同一损坏文件上崩溃。
                # 修复-预检异常视为损坏，执行与 integrity_check != 'ok' 相同的备份+重建流程。
                logger.warning("SQLite时序DB完整性预检失败: %s，备份并重建", e)
                corrupt_backup = f"{self._db_path}.corrupt.{int(time.time())}"
                try:
                    import shutil

                    shutil.move(self._db_path, corrupt_backup)
                    logger.warning("损坏时序DB已备份到: %s", corrupt_backup)
                except Exception:
                    path.unlink(missing_ok=True)

        try:
            self._db = await aiosqlite.connect(self._db_path)
            self._db.row_factory = aiosqlite.Row

            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA synchronous=NORMAL")
            await self._db.execute("PRAGMA busy_timeout=5000")

            await self._db.execute(_CREATE_TABLE_SQL)
            # FIXED-P0: 为已存在的表补充 UNIQUE 索引，防止重复时序数据点
            try:
                await self._db.execute(_CREATE_UNIQUE_INDEX_SQL)
            except Exception as e:
                logger.warning("添加 device_points UNIQUE 索引失败（可能存在重复数据）: %s", e)
            await self._db.execute(_CREATE_INDEX_DEVICE_POINT_TS)
            await self._db.execute(_CREATE_INDEX_TS)
            await self._db.execute(_CREATE_INDEX_ID)
            await self._db.execute(_CREATE_INDEX_AGG)
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS _meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            await self._db.commit()

            # 恢复同步偏移量
            await self._restore_sync_offset()

            # FIXED-P1: 启动定时刷盘任务，定期提交未提交的写入
            self._flush_task = asyncio.create_task(self._periodic_flush())

            logger.info("SQLite时序存储已启动: %s", self._db_path)
        except Exception:
            if self._db is not None:
                await self._db.close()
                self._db = None
            raise  # FIXED-P2: 原问题-主连接创建后PRAGMA/TABLE异常时连接泄漏，添加try/except清理

    async def _restore_sync_offset(self) -> None:
        """FIXED-P0: 原问题-mark_synced仅更新内存偏移量，进程重启后丢失导致重复同步
        从_meta表读取持久化的sync_offset，若无记录则fallback到MAX(id)"""
        if not self._db:
            return
        try:
            cursor = await self._db.execute("SELECT value FROM _meta WHERE key = 'sync_offset'")
            row = await cursor.fetchone()
            if row and row[0]:
                self._sync_offset = int(row[0])
                logger.info("从meta表恢复同步偏移量: %d", self._sync_offset)
                return
        except Exception as e:
            logger.debug("从meta表读取sync_offset失败: %s，尝试fallback", e)
        try:
            cursor = await self._db.execute("SELECT COALESCE(MAX(id), 0) FROM device_points")
            row = await cursor.fetchone()
            if row:
                self._sync_offset = row[0]
        except Exception as e:
            logger.debug("恢复同步偏移量失败: %s", e)
            self._sync_offset = 0

    async def stop(self) -> None:
        """Close database connection"""
        # FIXED-P1: 优雅关闭时取消定时刷盘任务并执行最终commit
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        if self._db:
            try:
                async with self._db_lock:
                    if self._pending_writes > 0:
                        await self._db.commit()
                        self._pending_writes = 0
            except Exception as e:
                logger.warning("SQLite时序存储最终commit失败: %s", e)
            await self._db.close()
            self._db = None
        logger.info("SQLite时序存储已关闭")

    async def backup(self, backup_dir: str = "data/backups") -> None:
        """FIXED-P1: 原问题-时序SQLite无备份机制，损坏后数据全部丢失
        将当前数据库备份到指定目录，使用WAL checkpoint确保一致性"""
        if not Path(self._db_path).exists():
            return
        try:
            import shutil

            backup_path = Path(backup_dir)
            backup_path.mkdir(parents=True, exist_ok=True)
            db_name = Path(self._db_path).name
            timestamp = int(time.time())
            dest = str(backup_path / f"{db_name}.backup.{timestamp}")
            if self._db:
                try:
                    await self._db.execute("PRAGMA wal_checkpoint=TRUNCATE")
                    await self._db.commit()
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("时序DB WAL checkpoint失败: %s", e)
            shutil.copy2(self._db_path, dest)
            logger.info("SQLite时序DB备份成功: %s", dest)
        except Exception as e:
            logger.error("SQLite时序DB备份失败: %s", e)

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
            async with self._db_lock:  # FIXED-P2: 并发写入锁保护
                await self._db.execute(
                    """INSERT OR REPLACE INTO device_points
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
                self._pending_writes += 1
                # FIXED-P1: 原问题-每次写入立即commit，高频写入时fsync成为瓶颈；改为批量提交，达到_max_pending或定时刷盘时commit
                if self._pending_writes >= self._max_pending:
                    await self._db.commit()
                    self._pending_writes = 0
        except Exception as e:
            logger.error("SQLite写入失败: %s", e)
            raise

    async def _periodic_flush(self) -> None:
        """FIXED-P1: 定时刷盘任务，定期提交未提交的写入，防止崩溃时丢失过多数据"""
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                if self._db and self._pending_writes > 0:
                    try:
                        async with self._db_lock:
                            if self._pending_writes > 0:
                                await self._db.commit()
                                self._pending_writes = 0
                    except Exception as e:
                        logger.warning("SQLite定时刷盘失败: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("SQLite定时刷盘任务异常: %s", e)

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
            async with self._db_lock:  # FIXED-P0: write_points_batch添加写入锁保护
                await self._db.executemany(
                    """INSERT OR REPLACE INTO device_points
                       (measurement, device_id, point_name, quality,
                        value_real, value_int, value_str, value_bool,
                        tags_json, timestamp_ns, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
                await self._db.commit()
                self._write_count += len(rows)
                self._pending_writes = 0  # FIXED-P2: 原问题-commit提交了所有挂起写入但未重置计数器，导致后续write_point误判触发不必要的早期commit
        except Exception as e:
            # FIXED-P2: 原问题-异常时无rollback，连接上残留未回滚事务状态，
            # 后续write_point叠加在未清理事务上可能导致隐式提交或事务嵌套错误
            try:
                await self._db.rollback()
            except Exception as rb_err:
                logger.warning("rollback after batch write failed: %s", rb_err)
            logger.error("SQLite批量写入失败: %s", e)
            raise

    async def query_points(
        self,
        device_id: str,
        point_name: str,
        start_ns: int,
        stop_ns: int | None = None,
        aggregate: str | None = None,
        window_seconds: int | None = None,
        limit: int = 10000,
        offset: int = 0,  # R9-S-16: 新增 offset 参数支持流式分批查询
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
            async with self._db_lock:  # FIXED-P1: 原问题-读操作使用_write_lock导致读读互斥；改为_read_lock
                cursor = await self._db.execute(
                    """SELECT timestamp_ns, value_real, value_int, value_str, value_bool, quality
                       FROM device_points
                       WHERE device_id = ? AND point_name = ?
                         AND timestamp_ns BETWEEN ? AND ?
                       ORDER BY timestamp_ns ASC
                       LIMIT ? OFFSET ?""",
                    (device_id, point_name, start_ns, stop_ns, limit, offset),
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
        # FIXED-P2: 原问题-聚合仅用value_real，value_int数据被忽略；改为COALESCE(value_real, value_int)
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
            async with self._db_lock:  # FIXED-P1: 原问题-聚合查询使用_write_lock导致读读互斥；改为_read_lock
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
                    (window_ns, window_ns, device_id, point_name, start_ns, stop_ns, window_ns, limit),
                )
                rows = await cursor.fetchall()
                results = []
                for row in rows:
                    window_start = row[0]
                    ts = datetime.fromtimestamp(window_start / 1e9, tz=UTC).isoformat()
                    results.append(
                        {
                            "time": ts,
                            "value": row[1],
                            # FIXED-P2: 原问题-quality不在GROUP BY中导致SQL错误；改为固定值"good"
                            "quality": "good",
                        }
                    )
                return results
        except Exception as e:
            logger.error("SQLite聚合查询失败: %s", e)
            return []

    async def query_latest(self, device_id: str, point_names: list[str]) -> dict[str, dict]:
        """Query latest values for points"""
        if not self._db or not point_names:
            return {}

        placeholders = ",".join("?" for _ in point_names)
        try:
            async with self._db_lock:  # FIXED-P1: 原问题-query_latest使用_write_lock导致读读互斥；改为_read_lock
                # FIXED(P1): 原问题-依赖MAX(id)假设id与时间顺序一致，补传数据或时钟回拨时返回过时值;
                # 修复-改用ROW_NUMBER()窗口函数按timestamp_ns降序取每个point_name的最新一条记录，
                # timestamp_ns相同时以id降序作为tiebreaker，保证每个点位仅返回一条最新值
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
                result = {}
                for row in rows:
                    pn = row[0]
                    ts = datetime.fromtimestamp(row[1] / 1e9, tz=UTC).isoformat()
                    value = (
                        row[2]
                        if row[2] is not None
                        else (
                            row[3]
                            if row[3] is not None
                            else (row[4] if row[4] is not None else (bool(row[5]) if row[5] is not None else None))
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
            async with self._db_lock:  # FIXED-P1: 原问题-读取操作使用_write_lock导致读读互斥；改为_read_lock
                cursor = await self._db.execute("SELECT COUNT(*) FROM device_points WHERE id > ?", (self._sync_offset,))
                row = await cursor.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logger.error("SQLite获取未同步计数失败: %s", e)
            return 0

    async def get_unsynced_records(self, limit: int = 1000, min_id: int | None = None) -> list[dict]:
        """Get unsynced records for incremental sync

        Args:
            limit: 最大返回记录数
            min_id: 最小ID偏移量，用于跳过已上传但mark_synced失败的记录
        """
        if not self._db:
            return []
        try:
            async with self._db_lock:  # FIXED-P1: 原问题-读取操作使用_write_lock导致读读互斥；改为_read_lock
                # BUG-013-CONFIRMED: 支持min_id参数，跳过已上传但mark_synced失败的记录
                effective_offset = max(self._sync_offset, min_id) if min_id is not None else self._sync_offset
                cursor = await self._db.execute(
                    """SELECT id, measurement, device_id, point_name, quality,
                              value_real, value_int, value_str, value_bool,
                              tags_json, timestamp_ns
                       FROM device_points
                       WHERE id > ?
                       ORDER BY id ASC
                       LIMIT ?""",
                    (effective_offset, limit),
                )
                rows = await cursor.fetchall()
                results = []
                for row in rows:
                    value = (
                        row[5]
                        if row[5] is not None
                        else (
                            row[6]
                            if row[6] is not None
                            else (row[7] if row[7] is not None else (bool(row[8]) if row[8] is not None else None))
                        )
                    )
                    tags = None
                    if row[9]:
                        import json

                        try:
                            tags = json.loads(row[9])
                        except (json.JSONDecodeError, TypeError):
                            tags = None

                    results.append(
                        {
                            "id": row[0],
                            "measurement": row[1],
                            "device_id": row[2],
                            "point_name": row[3],
                            "quality": row[4],
                            "value": value,
                            "tags": tags,
                            "timestamp_ns": row[10],
                        }
                    )
                return results
        except Exception as e:
            logger.error("SQLite获取未同步记录失败: %s", e)
            return []

    async def mark_synced(self, max_id: int) -> None:
        """DEPRECATED: Use sync_completed() for atomic offset+delete.
        Kept for backward compat; delegates to sync_completed()."""
        await self.sync_completed(max_id)

    async def delete_synced_records(self, max_id: int) -> int:
        """DEPRECATED: Use sync_completed() for atomic offset+delete.
        Kept for backward compat; delegates to sync_completed()."""
        ok = await self.sync_completed(max_id)
        return 1 if ok else 0

    async def sync_completed(self, max_id: int) -> bool:
        """FIXED-ATOMIC-SYNC: Atomically update sync offset and delete synced records.

        This method combines mark_synced() and delete_synced_records() into a single
        transaction to ensure atomicity. Either both operations succeed, or neither
        is applied.

        Args:
            max_id: The maximum id of records to mark as synced and delete

        Returns:
            True if the atomic operation succeeded, False otherwise

        The sync offset and deletion are committed together, ensuring that:
        - On success: offset is updated AND records are deleted
        - On failure: neither is applied, allowing safe retry
        """
        if not self._db:
            return False

        try:
            async with self._db_lock:
                # FIXED-P1: 原问题-write_point的隐式事务未提交时执行BEGIN IMMEDIATE报"cannot start a transaction within a transaction"
                # 先提交挂起的写入，确保连接上无活跃事务
                if self._pending_writes > 0:
                    await self._db.commit()
                    self._pending_writes = 0

                # Begin explicit transaction for atomicity
                await self._db.execute("BEGIN IMMEDIATE")

                try:
                    # Step 1: Update sync offset in _meta table
                    await self._db.execute(
                        "INSERT OR REPLACE INTO _meta (key, value) VALUES ('sync_offset', ?)",
                        (str(max_id),),
                    )

                    # Step 2: Delete synced records
                    cursor = await self._db.execute("DELETE FROM device_points WHERE id <= ?", (max_id,))
                    deleted = cursor.rowcount

                    # Step 3: Commit both changes atomically
                    await self._db.commit()

                    # Update in-memory offset
                    self._sync_offset = max_id

                    if deleted > 0:
                        logger.info(
                            "SQLite原子同步完成: 更新offset=%d, 删除%d条记录",
                            max_id,
                            deleted,
                        )
                    else:
                        logger.debug(
                            "SQLite原子同步完成: 更新offset=%d (无记录删除)",
                            max_id,
                        )
                    return True

                except Exception as inner_err:
                    # Rollback on any error - both offset and deletion are rolled back
                    await self._db.execute("ROLLBACK")
                    raise inner_err

        except Exception as e:
            logger.error("SQLite原子同步失败 (offset=%d): %s", max_id, e)
            return False

    async def clear_all(self) -> int:
        """清空所有本地缓存数据"""
        if not self._db:
            return 0
        try:
            async with self._db_lock:  # FIXED-P0: 原问题-clear_all无写锁保护
                cursor = await self._db.execute("SELECT COUNT(*) FROM device_points")
                row = await cursor.fetchone()
                total = row[0] if row else 0
                await self._db.execute("DELETE FROM device_points")
                await self._db.commit()
                self._sync_offset = 0
            logger.info("SQLite缓存已清空: 删除%d条", total)
            return total
        except Exception as e:
            logger.error("SQLite清空缓存失败: %s", e)
            return 0

    async def cleanup_old_data(self, retention_days: int = 30) -> int:
        """Delete data older than retention period"""
        if not self._db:
            return 0
        cutoff_ns = int((time.time() - retention_days * 86400) * 1e9)
        try:
            # R8-G-05 修复(一般): 原代码单次 DELETE 无 LIMIT，百万级时序数据时
            # 可能长时间锁库。改为分批 DELETE，每批 1000 条。
            total_deleted = 0
            batch_size = 1000
            while True:
                async with self._db_lock:
                    # SQLite 支持 DELETE ... LIMIT（3.35+），使用子查询兼容旧版本
                    cursor = await self._db.execute(
                        "DELETE FROM device_points WHERE rowid IN "
                        "(SELECT rowid FROM device_points WHERE timestamp_ns < ? LIMIT ?)",
                        (cutoff_ns, batch_size),
                    )
                    await self._db.commit()
                    batch_deleted = cursor.rowcount
                total_deleted += batch_deleted
                if batch_deleted < batch_size:
                    break
            if total_deleted > 0:
                logger.info(
                    "SQLite清理旧数据: 删除%d条 (retention=%d天, batch_size=%d)",
                    total_deleted,
                    retention_days,
                    batch_size,
                )
            return total_deleted
        except Exception as e:
            logger.error("SQLite清理旧数据失败: %s", e)
            return 0

    async def delete_by_device_id(self, device_id: str) -> int:
        """Delete all time-series data for a specific device.

        FIXED-P1: 原问题-设备删除后时序数据永久残留
        """
        if not self._db:
            return 0
        try:
            async with self._db_lock:
                cursor = await self._db.execute("DELETE FROM device_points WHERE device_id = ?", (device_id,))
                await self._db.commit()
                deleted = cursor.rowcount
            if deleted > 0:
                logger.info("SQLite删除设备时序数据: %d条 (device_id=%s)", deleted, device_id)
            return deleted
        except Exception as e:
            logger.error("SQLite删除设备时序数据失败: %s", e)
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
            async with self._db_lock:  # FIXED-P0: 原问题-读取操作使用_write_lock导致读写互斥；改为_read_lock
                cursor = await self._db.execute("SELECT COUNT(*) FROM device_points")
                row = await cursor.fetchone()
                total = row[0] if row else 0

                db_size = 0
                if os.path.exists(self._db_path):
                    db_size = os.path.getsize(self._db_path)

                cursor = await self._db.execute("SELECT MIN(timestamp_ns), MAX(timestamp_ns) FROM device_points")
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
        value = (
            row[1]
            if row[1] is not None
            else (
                row[2]
                if row[2] is not None
                else (row[3] if row[3] is not None else (bool(row[4]) if row[4] is not None else None))
            )
        )
        return {
            "time": ts,
            "value": value,
            "quality": row[5] or "good",
        }
