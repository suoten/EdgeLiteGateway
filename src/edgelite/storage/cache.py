"""断网缓存管理 - 集成环形缓冲区与增量同步"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from edgelite.constants import _CACHE_EVICTION_RATIO, _CACHE_MAX_SIZE
from edgelite.models.db import CacheQueueORM
from edgelite.storage.ring_buffer import RingBuffer

logger = logging.getLogger(__name__)

MAX_CACHE_SIZE = _CACHE_MAX_SIZE


# FIXED: 原问题-json.loads无异常保护，数据库字段损坏导致整个查询崩溃
def _safe_json_loads(value: Any, default: Any = None) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return value


# FIXED-P0: 原问题-CacheQueueORM.timestamp 列为 Float，但调用方(scheduler/influx_storage)
# 传入 ISO 字符串，导致 add_to_cache 始终抛 "could not convert string to float"，
# 断网缓存完全失效。同时消费方(scheduler._flush_from_ring_buffer)用 datetime.fromisoformat
# 期望 ISO 字符串。统一：存储时 ISO→epoch float（支持数值比较/索引），读取时 float→ISO 字符串。
def _ts_to_epoch(ts: Any) -> float:
    """将时间戳（ISO 字符串/数值/datetime）归一化为 epoch 浮点秒。"""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, datetime):
        return ts.timestamp()
    try:
        s = str(ts)
        # 兼容带 'Z' 结尾的 UTC ISO 时间
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()


def _epoch_to_iso(ts: Any) -> str:
    """将 epoch 浮点秒转回 ISO 字符串（消费方 datetime.fromisoformat 契约）。"""
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (ValueError, TypeError, OSError):
        return datetime.now(UTC).isoformat()


class CacheManager:
    """InfluxDB不可用时的数据缓存管理

    集成 RingBuffer 实现增量同步:
    - add_to_cache() 同时写入 SQLite（持久化）和 RingBuffer（内存）
    - get_pending_from_ring_buffer() 从 RingBuffer 获取待同步记录
    - mark_synced() / mark_failed() 更新 RingBuffer 同步状态
    - 进程重启后从 SQLite 恢复到 RingBuffer
    """

    _last_log_time: float = 0.0
    _LOG_INTERVAL = 60.0

    def __init__(self, database: Any):
        self._database = database
        self._ring_buffer: RingBuffer | None = None
        self._ring_buffer_initialized = False
        self._orphan_sqlite_ids: set[int] = set()
        self._orphan_persisted = False
        self._write_lock = asyncio.Lock()
        self._orphan_lock = asyncio.Lock()  # FIXED-P1: 孤儿补偿追踪加锁
        self._orphan_compaction_task: asyncio.Task | None = None  # FIXED-P1: 孤儿补偿定时任务
        self._cache_count: int = 0  # FIXED-P0: 内存计数器替代SELECT COUNT(*)，减少写锁临界区耗时
        self._calibration_task: asyncio.Task | None = None  # 定时校准任务
        self._calibration_interval: float = 300.0  # 校准间隔：5分钟

    def _ensure_ring_buffer(self) -> None:
        """延迟初始化 RingBuffer（需要配置可用）"""
        if self._ring_buffer_initialized:
            return
        self._ring_buffer_initialized = True
        try:
            from edgelite.config import get_config

            config = get_config()
            cache_cfg = getattr(config, "cache", None)
            if cache_cfg and getattr(cache_cfg, "incremental_sync_enabled", True):
                capacity = getattr(cache_cfg, "ring_buffer_capacity", 100000)
                compress = getattr(cache_cfg, "ring_buffer_compress", True)
                high_wm = getattr(cache_cfg, "high_watermark_pct", 0.8)
                critical_wm = getattr(cache_cfg, "critical_watermark_pct", 0.9)
                self._ring_buffer = RingBuffer(
                    capacity=capacity,
                    compress=compress,
                    high_watermark=high_wm,
                    critical_watermark=critical_wm,
                )
                logger.info(
                    "RingBuffer已初始化: capacity=%d, compress=%s",
                    capacity,
                    compress,
                )
        except Exception as e:
            logger.warning("RingBuffer初始化失败，仅使用SQLite: %s", e)

    async def restore_from_sqlite(self) -> int:
        """从 SQLite 恢复未同步记录到 RingBuffer（进程重启后调用）"""
        self._ensure_ring_buffer()
        if not self._orphan_persisted:  # FIXED-P1: 原问题-orphan集合纯内存，重启后丢失；从SQLite恢复orphan IDs
            try:
                async with self._database.get_session() as session:
                    from sqlalchemy import text

                    await session.execute(text("CREATE TABLE IF NOT EXISTS _orphan_ids (id INTEGER PRIMARY KEY)"))
                    result = await session.execute(text("SELECT id FROM _orphan_ids"))
                    rows = result.fetchall()
                    async with self._orphan_lock:
                        self._orphan_sqlite_ids = {row[0] for row in rows}
                        if self._orphan_sqlite_ids:
                            logger.info("从SQLite恢复%d个orphan ID", len(self._orphan_sqlite_ids))
            except Exception as e:
                logger.warning("从SQLite恢复orphan IDs失败: %s", e)
            self._orphan_persisted = True
        if not self._ring_buffer:
            # FIXED S-04: 即使没有RingBuffer，也通过实际DB数量校准计数器，不清零
            try:
                async with self._write_lock:
                    self._cache_count = await self.get_cache_count()
            except Exception as calib_err:
                logger.error("校准缓存计数器失败: %s", calib_err)
            return 0
        try:
            # PERF: 改为分批加载（每批1000条），使用游标分页 WHERE id > last_id，
            # 避免一次性 get_cached_records(limit=100000) 将全部记录加载到内存导致 OOM
            _RESTORE_BATCH_SIZE = 1000
            total_loaded = 0
            all_restored_ids: set[int] = set()
            last_id: int | None = None
            while True:
                records = await self.get_cached_records_after(after_id=last_id, limit=_RESTORE_BATCH_SIZE)
                if not records:
                    break
                ring_records = []
                batch_max_id: int | None = None
                for r in records:
                    sqlite_id = r.get("id")
                    ring_records.append(
                        {
                            "measurement": r.get("measurement", ""),
                            "tags": r.get("tags", {}),
                            "fields": r.get("fields", {}),
                            "timestamp": r.get("timestamp", ""),
                            "sqlite_id": sqlite_id,
                        }
                    )
                    if sqlite_id is not None:
                        all_restored_ids.add(sqlite_id)
                        if batch_max_id is None or sqlite_id > batch_max_id:
                            batch_max_id = sqlite_id
                loaded = await self._ring_buffer.load_from_records(ring_records)
                total_loaded += loaded
                # 游标推进：用本批最大 id 作为下一批起点
                if batch_max_id is not None:
                    last_id = batch_max_id
                # 不足一批说明已到末尾
                if len(records) < _RESTORE_BATCH_SIZE:
                    break
            if total_loaded > 0:
                logger.info("从SQLite恢复%d条记录到RingBuffer", total_loaded)
            async with self._orphan_lock:
                self._orphan_sqlite_ids -= all_restored_ids  # FIXED-P1: 恢复成功的记录从orphan集合移除
            return total_loaded
        except Exception as e:
            # FIXED S-04: 异常时记录ERROR日志，不清零计数器，由finally块重新校准
            logger.error("从SQLite恢复到RingBuffer失败: %s", e)
            return 0
        finally:
            # FIXED S-04: 无论成功还是异常，都通过实际DB数量重新校准计数器，确保与实际数据一致
            # FIXED-Bug12: 用实际 DB 行数初始化计数器，避免受限 fetch 导致计数器偏低、淘汰逻辑失效
            try:
                async with self._write_lock:
                    self._cache_count = await self.get_cache_count()
            except Exception as calib_err:
                logger.error("重新校准缓存计数器失败: %s", calib_err)

    async def add_to_cache(
        self,
        measurement: str,
        tags: dict,
        fields: dict,
        timestamp: str,
    ) -> dict[str, bool]:
        self._ensure_ring_buffer()
        if not measurement:
            logger.warning("CacheManager.add_to_cache: empty measurement, skipping")
            return {"sqlite_ok": False, "ring_ok": False}
        sqlite_ok = False
        sqlite_record_id: int | None = None
        async with self._write_lock:  # FIXED-P0: 原问题-count检查与insert非原子，并发可突破MAX_CACHE_SIZE
            self._ensure_ring_buffer()  # FIXED-P2: 懒初始化移入_write_lock内，消除并发创建多个RingBuffer的竞态
            try:
                async with self._database.get_session() as session:
                    evicted_count = 0
                    if (
                        self._cache_count >= MAX_CACHE_SIZE
                    ):  # FIXED-P0: 内存计数器替代SELECT COUNT(*)，减少写锁临界区耗时
                        delete_count = MAX_CACHE_SIZE // _CACHE_EVICTION_RATIO
                        subq = select(CacheQueueORM.id).order_by(CacheQueueORM.id.asc()).limit(delete_count)
                        await session.execute(sa_delete(CacheQueueORM).where(CacheQueueORM.id.in_(subq)))
                        evicted_count = delete_count
                        logger.warning("缓存已满，丢弃最旧%d条数据", delete_count)

                    orm = CacheQueueORM(
                        measurement=measurement,
                        tags=json.dumps(tags, ensure_ascii=False),
                        fields=json.dumps(fields, ensure_ascii=False),
                        timestamp=_ts_to_epoch(timestamp),  # FIXED-P0: ISO→epoch float
                        created_at=datetime.now(UTC),
                    )
                    session.add(orm)
                    await session.commit()
                    await session.refresh(orm)
                    sqlite_record_id = orm.id  # FIXED-P0: 记录SQLite记录ID，用于孤儿补偿
                    # FIXED S-03: 使用 max(0, ...) 保护，防止计数器变为负数
                    new_cache_count = self._cache_count + 1 - evicted_count
                    if new_cache_count < 0:
                        logger.warning(
                            "缓存计数器异常: _cache_count=%d, 1-evicted_count=%d, 计算结果=%d, 已重置为0",
                            self._cache_count,
                            1 - evicted_count,
                            new_cache_count,
                        )
                        new_cache_count = 0
                    self._cache_count = new_cache_count  # FIXED-P0: 提交后更新计数器，避免事务回滚导致计数漂移
                    sqlite_ok = True  # FIXED-P2: 移到_cache_count更新之后，确保commit+refresh都成功才标记成功
            except Exception as e:
                now = time.monotonic()
                if "readonly database" in str(e).lower():
                    if now - CacheManager._last_log_time >= CacheManager._LOG_INTERVAL:
                        logger.error("Cache数据库只读，请检查文件权限: %s", e)
                        CacheManager._last_log_time = now
                elif now - CacheManager._last_log_time >= CacheManager._LOG_INTERVAL:
                    logger.error("CacheManager.add_to_cache failed: %s", e)
                    CacheManager._last_log_time = now

        # 同时写入 RingBuffer
        ring_ok = False
        if self._ring_buffer:
            try:
                # FIXED-BugR4X: 原问题-add_to_cache写入RingBuffer的记录缺少sqlite_id字段，导致增量同步后无法删除SQLite源记录，SQLite无限增长  # noqa: E501
                # 修复-如果sqlite_record_id不为None，将其加入ring_record字典
                ring_record = {
                    "measurement": measurement,
                    "tags": tags,
                    "fields": fields,
                    "timestamp": timestamp,
                }
                if sqlite_record_id is not None:
                    ring_record["sqlite_id"] = sqlite_record_id
                ring_ok = await self._ring_buffer.put(ring_record)
            except Exception as e:
                logger.error("RingBuffer写入失败: %s", e)

        if sqlite_ok != ring_ok:  # FIXED-P1: 部分失败时记录warning
            logger.warning("CacheManager partial write: sqlite_ok=%s, ring_ok=%s", sqlite_ok, ring_ok)

        if (
            sqlite_ok and not ring_ok and sqlite_record_id is not None
        ):  # FIXED-P0: 原问题-SQLite写成功RingBuffer写失败时数据成为孤儿，记录ID供补偿同步
            # FIXED-P1: _orphan_sqlite_ids.add需加锁保护
            async with self._orphan_lock:
                self._orphan_sqlite_ids.add(sqlite_record_id)
            try:  # FIXED-P1: 原问题-orphan集合纯内存，重启后丢失；持久化到SQLite
                async with self._database.get_session() as session:
                    from sqlalchemy import text

                    await session.execute(text("CREATE TABLE IF NOT EXISTS _orphan_ids (id INTEGER PRIMARY KEY)"))
                    await session.execute(
                        text("INSERT OR IGNORE INTO _orphan_ids (id) VALUES (:id)"),
                        {"id": sqlite_record_id},
                    )
                    await session.commit()
            except Exception as e:
                logger.warning("Failed to persist orphan id %d: %s", sqlite_record_id, e)

        return {"sqlite_ok": sqlite_ok, "ring_ok": ring_ok}

    async def get_cached_records(self, limit: int = 1000) -> list[dict]:
        # FIXED: 原问题-get_cached_records无try-except保护
        try:
            async with self._database.get_session() as session:
                result = await session.execute(select(CacheQueueORM).order_by(CacheQueueORM.id.asc()).limit(limit))
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "measurement": r.measurement,
                        "tags": _safe_json_loads(r.tags, {}),
                        "fields": _safe_json_loads(r.fields, {}),
                        "timestamp": _epoch_to_iso(r.timestamp),  # FIXED-P0: float→ISO
                        "retry_count": r.retry_count,
                        "status": r.status,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("CacheManager.get_cached_records failed: %s", e)
            return []

    async def get_cached_records_after(self, after_id: int | None = None, limit: int = 1000) -> list[dict]:
        """游标分页获取缓存记录（WHERE id > after_id ORDER BY id LIMIT batch_size）。

        PERF: 供 restore_from_sqlite 分批加载使用，避免一次性加载 10 万条导致 OOM。
        """
        try:
            async with self._database.get_session() as session:
                stmt = select(CacheQueueORM).order_by(CacheQueueORM.id.asc()).limit(limit)
                if after_id is not None:
                    stmt = stmt.where(CacheQueueORM.id > after_id)
                result = await session.execute(stmt)
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "measurement": r.measurement,
                        "tags": _safe_json_loads(r.tags, {}),
                        "fields": _safe_json_loads(r.fields, {}),
                        "timestamp": _epoch_to_iso(r.timestamp),  # FIXED-P0: float→ISO
                        "retry_count": r.retry_count,
                        "status": r.status,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("CacheManager.get_cached_records_after failed: %s", e)
            return []

    async def get_pending_records(self, limit: int = 500) -> list[dict]:
        """获取状态为pending的缓存记录（增量同步入口）"""
        try:
            async with self._database.get_session() as session:
                result = await session.execute(
                    select(CacheQueueORM)
                    .where(CacheQueueORM.status == "pending")
                    .order_by(CacheQueueORM.id.asc())
                    .limit(limit)
                )
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "measurement": r.measurement,
                        "tags": _safe_json_loads(r.tags, {}),
                        "fields": _safe_json_loads(r.fields, {}),
                        "timestamp": _epoch_to_iso(r.timestamp),  # FIXED-P0: float→ISO
                        "retry_count": r.retry_count,
                        "status": r.status,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("CacheManager.get_pending_records failed: %s", e)
            return []

    async def mark_syncing(self, ids: list[int]) -> None:
        """将缓存记录状态标记为syncing（正在同步中）"""
        if not ids:
            return
        try:
            async with self._database.get_session() as session:
                await session.execute(
                    sa_update(CacheQueueORM).where(CacheQueueORM.id.in_(ids)).values(status="syncing")
                )
                await session.commit()
        except Exception as e:
            logger.error("CacheManager.mark_syncing failed: %s", e)

    async def mark_synced_records(self, ids: list[int]) -> None:
        """将缓存记录状态标记为synced并删除（同步完成）"""
        if not ids:
            return
        try:
            async with self._database.get_session() as session:
                await session.execute(sa_delete(CacheQueueORM).where(CacheQueueORM.id.in_(ids)))
                await session.commit()
                async with self._write_lock:  # FIXED-P1: _cache_count修改移入_write_lock，与add_to_cache互斥
                    # FIXED S-03: 使用 max(0, ...) 保护，防止计数器变为负数
                    new_cache_count = self._cache_count - len(ids)
                    if new_cache_count < 0:
                        logger.warning(
                            "缓存计数器异常: _cache_count=%d, 试图减少 %d, 结果=%d, 已重置为0",
                            self._cache_count,
                            len(ids),
                            new_cache_count,
                        )
                        new_cache_count = 0
                    self._cache_count = new_cache_count
        except Exception as e:
            logger.error("CacheManager.mark_synced_records failed: %s", e)

    async def reset_syncing_to_pending(self) -> int:
        """将所有syncing状态的记录重置为pending（进程重启后调用）"""
        try:
            async with self._database.get_session() as session:
                result = await session.execute(
                    sa_update(CacheQueueORM).where(CacheQueueORM.status == "syncing").values(status="pending")
                )
                await session.commit()
                return result.rowcount
        except Exception as e:
            logger.error("CacheManager.reset_syncing_to_pending failed: %s", e)
            return 0

    async def get_pending_from_ring_buffer(self, limit: int = 500) -> list[dict]:
        """从 RingBuffer 获取待同步记录（增量同步入口）"""
        self._ensure_ring_buffer()
        if not self._ring_buffer:
            return []
        try:
            return await self._ring_buffer.get_pending(limit=limit)
        except Exception as e:
            logger.error("RingBuffer.get_pending failed: %s", e)
            return []

    async def get_orphan_records(self) -> list[dict]:
        """FIXED-P0: 原问题-SQLite写成功RingBuffer写失败时数据成为孤儿
        获取孤儿SQLite记录供补偿同步，同步后自动清理孤儿集合"""
        # FIXED-P1: _orphan_sqlite_ids读取需加锁保护
        async with self._orphan_lock:
            if not self._orphan_sqlite_ids:
                return []
            ids = list(self._orphan_sqlite_ids)
        try:
            async with self._database.get_session() as session:
                result = await session.execute(select(CacheQueueORM).where(CacheQueueORM.id.in_(ids)))
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "measurement": r.measurement,
                        "tags": _safe_json_loads(r.tags, {}),
                        "fields": _safe_json_loads(r.fields, {}),
                        "timestamp": r.timestamp,
                        "status": "orphan",
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("CacheManager.get_orphan_records failed: %s", e)
            return []

    async def clear_orphan_ids(self, ids: list[int]) -> None:
        """FIXED-P0: 孤儿记录同步成功后清理孤儿集合
        FIXED-P1: 原问题-仅清理内存集合，未同步删除_orphan_ids持久化表，重启后重复恢复已补偿孤儿"""
        # FIXED-P1: _orphan_sqlite_ids修改需加锁保护
        async with self._orphan_lock:
            self._orphan_sqlite_ids -= set(ids)
        try:
            async with self._database.get_session() as session:
                from sqlalchemy import text

                for orphan_id in ids:
                    await session.execute(
                        text("DELETE FROM _orphan_ids WHERE id = :id"),
                        {"id": orphan_id},
                    )
                await session.commit()
        except Exception as e:
            logger.debug("Failed to clear orphan ids from persistent table: %s", e)

    async def mark_synced(self, record_ids: list[int], sqlite_ids: list[int] | None = None) -> int:
        """标记记录为已同步

        Args:
            record_ids: RingBuffer 中的 _id 列表
            sqlite_ids: 对应的 SQLite 记录 id 列表（可选）
        """
        count = 0
        if self._ring_buffer:
            try:
                count = await self._ring_buffer.mark_synced(record_ids)
            except Exception as e:
                logger.error("RingBuffer.mark_synced failed: %s", e)

        # 同步删除 SQLite 中对应记录
        if sqlite_ids:
            await self.delete_cached(sqlite_ids)

        return count

    async def mark_failed(self, record_ids: list[int]) -> int:
        """标记同步失败的记录回退为 pending"""
        if not self._ring_buffer:
            return 0
        try:
            return await self._ring_buffer.mark_failed(record_ids)
        except Exception as e:
            logger.error("RingBuffer.mark_failed failed: %s", e)
            return 0

    async def delete_cached(self, ids: list[int]) -> None:
        if not ids:
            return
        try:
            async with self._database.get_session() as session:
                await session.execute(sa_delete(CacheQueueORM).where(CacheQueueORM.id.in_(ids)))
                await session.commit()
                async with self._write_lock:  # FIXED-P1: _cache_count修改移入_write_lock，与add_to_cache互斥
                    # FIXED S-03: 使用 max(0, ...) 保护，防止计数器变为负数
                    new_cache_count = self._cache_count - len(ids)
                    if new_cache_count < 0:
                        logger.warning(
                            "缓存计数器异常: _cache_count=%d, 试图减少 %d, 结果=%d, 已重置为0",
                            self._cache_count,
                            len(ids),
                            new_cache_count,
                        )
                        new_cache_count = 0
                    self._cache_count = new_cache_count
        except Exception as e:
            logger.error("CacheManager.delete_cached failed: %s", e)

    async def increment_retry(self, ids: list[int]) -> None:
        if not ids:
            return
        try:
            async with self._database.get_session() as session:
                await session.execute(
                    sa_update(CacheQueueORM)
                    .where(CacheQueueORM.id.in_(ids))
                    .values(retry_count=CacheQueueORM.retry_count + 1)
                )
                await session.commit()
        except Exception as e:
            logger.error("CacheManager.increment_retry failed: %s", e)

    async def get_cache_count(self) -> int:
        try:
            async with self._database.get_session() as session:
                result = await session.execute(select(func.count()).select_from(CacheQueueORM))
                return result.scalar() or 0
        except Exception as e:
            logger.error("CacheManager.get_cache_count failed: %s", e)
            return 0

    async def start_calibration(self) -> None:
        """启动定时校准任务，每5分钟用 SELECT COUNT(*) 校准 _cache_count

        内存计数器在异常退出/并发竞争下可能漂移，定时校准确保计数准确。
        """
        if self._calibration_task is not None:
            return
        self._calibration_task = asyncio.create_task(self._calibration_loop())

    async def stop_calibration(self) -> None:
        """停止定时校准任务"""
        if self._calibration_task is not None:
            self._calibration_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._calibration_task
            self._calibration_task = None

    async def _calibration_loop(self) -> None:
        """定时校准 _cache_count 内存计数器"""
        while True:
            try:
                await asyncio.sleep(self._calibration_interval)
                actual_count = await self.get_cache_count()
                async with self._write_lock:
                    if self._cache_count != actual_count:
                        logger.info(
                            "缓存计数器校准: 内存=%d, 实际=%d, 已校正",
                            self._cache_count,
                            actual_count,
                        )
                        self._cache_count = actual_count
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("缓存计数器校准失败: %s", e)

    def get_ring_buffer_stats(self) -> dict | None:
        """获取 RingBuffer 统计信息"""
        if not self._ring_buffer:
            return None
        return self._ring_buffer.get_stats()

    async def check_watermark(self) -> dict:
        """检查缓存水位线，返回水位状态信息

        Returns:
            dict: {
                "level": "normal" | "high" | "critical",
                "usage_pct": float,
                "cache_count": int,
                "max_size": int,
                "pending_count": int,
            }
        """
        # FIXED-P2: 原问题-用过期的COUNT查询覆盖_cache_count；改为使用内存计数器，不覆盖
        async with self._write_lock:
            cache_count = self._cache_count
        usage_pct = round(cache_count / MAX_CACHE_SIZE * 100, 1) if MAX_CACHE_SIZE else 0

        pending_count = 0
        try:
            async with self._database.get_session() as session:
                result = await session.execute(
                    select(func.count()).select_from(CacheQueueORM).where(CacheQueueORM.status == "pending")
                )
                pending_count = result.scalar() or 0
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("获取缓存pending计数失败: %s", e)

        if usage_pct >= 90:
            level = "critical"
            logger.warning(
                "缓存水位告警: CRITICAL (usage=%.1f%%, count=%d/%d, pending=%d)",
                usage_pct,
                cache_count,
                MAX_CACHE_SIZE,
                pending_count,
            )
        elif usage_pct >= 80:
            level = "high"
            logger.warning(
                "缓存水位告警: HIGH (usage=%.1f%%, count=%d/%d, pending=%d)",
                usage_pct,
                cache_count,
                MAX_CACHE_SIZE,
                pending_count,
            )
        else:
            level = "normal"

        return {
            "level": level,
            "usage_pct": usage_pct,
            "cache_count": cache_count,
            "max_size": MAX_CACHE_SIZE,
            "pending_count": pending_count,
        }

    def start_orphan_compaction(self, interval: float = 60.0) -> None:
        """FIXED-P1: 原问题-孤儿补偿无自动定时任务，SQLite写成功RingBuffer写失败的孤儿数据永久不同步
        Start a background task that periodically attempts orphan record compaction."""
        if self._orphan_compaction_task is not None:
            return
        self._orphan_compaction_task = asyncio.create_task(
            self._orphan_compaction_loop(interval),
        )

    async def _orphan_compaction_loop(self, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                orphans = await self.get_orphan_records()
                if orphans:
                    logger.info("Orphan compaction: found %d orphan records", len(orphans))
                    # FIXED-P0: 原问题-孤儿补偿循环只清理追踪ID，不执行实际补偿同步
                    # 改为将孤儿记录重新写入RingBuffer，成功后才清理追踪ID
                    compensated_ids = []
                    if self._ring_buffer:
                        # FIXED-Bug11: 记录每个 ring_record 对应的原始 orphan id，用于精确清理
                        successfully_loaded_ids: list[int] = []
                        ring_records = []
                        for r in orphans:
                            if "id" not in r:
                                continue
                            ring_records.append(
                                {
                                    "measurement": r.get("measurement", ""),
                                    "tags": r.get("tags", {}),
                                    "fields": r.get("fields", {}),
                                    "timestamp": r.get("timestamp", ""),
                                    "sqlite_id": r["id"],
                                }
                            )
                            successfully_loaded_ids.append(r["id"])
                        if ring_records:
                            loaded = await self._ring_buffer.load_from_records(ring_records)
                            if loaded > 0:
                                # FIXED-Bug11: 仅清理实际装入 RingBuffer 的前 loaded 条孤儿 ID
                                # 之前：loaded>0 即清掉全部 orphans ID，未装入的记录变隐形导致静默数据丢失
                                compensated_ids = successfully_loaded_ids[:loaded]
                                logger.info("Orphan compaction: re-injected %d records into RingBuffer", loaded)
                    if compensated_ids:
                        await self.clear_orphan_ids(compensated_ids)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Orphan compaction loop error: %s", e)

    def stop_orphan_compaction(self) -> None:
        if self._orphan_compaction_task is not None:
            self._orphan_compaction_task.cancel()
            self._orphan_compaction_task = None

    async def close(self) -> None:
        """修复资源泄漏：关闭缓存管理器，停止所有后台任务"""
        await self.stop_calibration()
        self.stop_orphan_compaction()
