"""断网缓存管理 - 集成环形缓冲区与增量同步"""

from __future__ import annotations

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
                    "RingBuffer已初始化: capacity=%d, compress=%s", capacity, compress,
                )
        except Exception as e:
            logger.warning("RingBuffer初始化失败，仅使用SQLite: %s", e)

    async def restore_from_sqlite(self) -> int:
        """从 SQLite 恢复未同步记录到 RingBuffer（进程重启后调用）"""
        self._ensure_ring_buffer()
        if not self._ring_buffer:
            return 0
        try:
            records = await self.get_cached_records(limit=100000)
            if not records:
                return 0
            ring_records = []
            for r in records:
                ring_records.append({
                    "measurement": r.get("measurement", ""),
                    "tags": r.get("tags", {}),
                    "fields": r.get("fields", {}),
                    "timestamp": r.get("timestamp", ""),
                    "sqlite_id": r.get("id"),
                })
            loaded = await self._ring_buffer.load_from_records(ring_records)
            if loaded > 0:
                logger.info("从SQLite恢复%d条记录到RingBuffer", loaded)
            return loaded
        except Exception as e:
            logger.error("从SQLite恢复到RingBuffer失败: %s", e)
            return 0

    async def add_to_cache(
        self,
        measurement: str,
        tags: dict,
        fields: dict,
        timestamp: str,
    ) -> bool:
        # FIXED: W5 原问题-缓存写入异常被静默吞掉，调用方无法感知数据是否成功缓存
        self._ensure_ring_buffer()
        sqlite_ok = False
        try:
            async with self._database.get_session() as session:
                count_result = await session.execute(
                    select(func.count()).select_from(CacheQueueORM)
                )
                count = count_result.scalar() or 0

                if count >= MAX_CACHE_SIZE:
                    delete_count = MAX_CACHE_SIZE // _CACHE_EVICTION_RATIO
                    subq = (
                        select(CacheQueueORM.id)
                        .order_by(CacheQueueORM.id.asc())
                        .limit(delete_count)
                    )
                    await session.execute(
                        sa_delete(CacheQueueORM).where(CacheQueueORM.id.in_(subq))
                    )
                    logger.warning("缓存已满，丢弃最旧%d条数据", delete_count)

                orm = CacheQueueORM(
                    measurement=measurement,
                    tags=json.dumps(tags, ensure_ascii=False),
                    fields=json.dumps(fields, ensure_ascii=False),
                    timestamp=timestamp,
                    created_at=datetime.now(UTC),
                )
                session.add(orm)
                await session.commit()
            sqlite_ok = True
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
                ring_ok = await self._ring_buffer.put({
                    "measurement": measurement,
                    "tags": tags,
                    "fields": fields,
                    "timestamp": timestamp,
                })
            except Exception as e:
                logger.error("RingBuffer写入失败: %s", e)

        return sqlite_ok or ring_ok

    async def get_cached_records(self, limit: int = 1000) -> list[dict]:
        # FIXED: 原问题-get_cached_records无try-except保护
        try:
            async with self._database.get_session() as session:
                result = await session.execute(
                    select(CacheQueueORM).order_by(CacheQueueORM.id.asc()).limit(limit)
                )
                rows = result.scalars().all()
                return [
                    {
                        "id": r.id,
                        "measurement": r.measurement,
                        "tags": _safe_json_loads(r.tags, {}),
                        "fields": _safe_json_loads(r.fields, {}),
                        "timestamp": r.timestamp,
                        "retry_count": r.retry_count,
                        "status": r.status,
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("CacheManager.get_cached_records failed: %s", e)
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
                        "timestamp": r.timestamp,
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
                    sa_update(CacheQueueORM)
                    .where(CacheQueueORM.id.in_(ids))
                    .values(status="syncing")
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
                await session.execute(
                    sa_delete(CacheQueueORM).where(CacheQueueORM.id.in_(ids))
                )
                await session.commit()
        except Exception as e:
            logger.error("CacheManager.mark_synced_records failed: %s", e)

    async def reset_syncing_to_pending(self) -> int:
        """将所有syncing状态的记录重置为pending（进程重启后调用）"""
        try:
            async with self._database.get_session() as session:
                result = await session.execute(
                    sa_update(CacheQueueORM)
                    .where(CacheQueueORM.status == "syncing")
                    .values(status="pending")
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
        cache_count = await self.get_cache_count()
        usage_pct = round(cache_count / MAX_CACHE_SIZE * 100, 1) if MAX_CACHE_SIZE else 0

        pending_count = 0
        try:
            async with self._database.get_session() as session:
                result = await session.execute(
                    select(func.count())
                    .select_from(CacheQueueORM)
                    .where(CacheQueueORM.status == "pending")
                )
                pending_count = result.scalar() or 0
        except Exception:
            pass

        if usage_pct >= 90:
            level = "critical"
            logger.warning(
                "缓存水位告警: CRITICAL (usage=%.1f%%, count=%d/%d, pending=%d)",
                usage_pct, cache_count, MAX_CACHE_SIZE, pending_count,
            )
        elif usage_pct >= 80:
            level = "high"
            logger.warning(
                "缓存水位告警: HIGH (usage=%.1f%%, count=%d/%d, pending=%d)",
                usage_pct, cache_count, MAX_CACHE_SIZE, pending_count,
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
