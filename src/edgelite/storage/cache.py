"""断网缓存管理"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, delete as sa_delete, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from edgelite.models.db import CacheQueueORM

logger = logging.getLogger(__name__)

MAX_CACHE_SIZE = 100_000


class CacheManager:
    """InfluxDB不可用时的数据缓存管理"""

    def __init__(self, database: Any):
        self._database = database

    def _session(self) -> AsyncSession:
        return self._database.get_session()

    async def add_to_cache(
        self,
        measurement: str,
        tags: dict,
        fields: dict,
        timestamp: str,
    ) -> None:
        session = self._session()
        count_result = await session.execute(select(func.count()).select_from(CacheQueueORM))
        count = count_result.scalar() or 0

        if count >= MAX_CACHE_SIZE:
            delete_count = MAX_CACHE_SIZE // 10
            subq = select(CacheQueueORM.id).order_by(CacheQueueORM.id.asc()).limit(delete_count)
            await session.execute(sa_delete(CacheQueueORM).where(CacheQueueORM.id.in_(subq)))
            logger.warning("缓存已满，丢弃最旧%d条数据", delete_count)

        orm = CacheQueueORM(
            measurement=measurement,
            tags=json.dumps(tags, ensure_ascii=False),
            fields=json.dumps(fields, ensure_ascii=False),
            timestamp=timestamp,
            created_at=datetime.now(timezone.utc),
        )
        session.add(orm)
        await session.commit()

    async def get_cached_records(self, limit: int = 1000) -> list[dict]:
        session = self._session()
        result = await session.execute(
            select(CacheQueueORM).order_by(CacheQueueORM.id.asc()).limit(limit)
        )
        rows = result.scalars().all()
        return [
            {
                "id": r.id,
                "measurement": r.measurement,
                "tags": json.loads(r.tags) if isinstance(r.tags, str) else r.tags,
                "fields": json.loads(r.fields) if isinstance(r.fields, str) else r.fields,
                "timestamp": r.timestamp,
                "retry_count": r.retry_count,
            }
            for r in rows
        ]

    async def delete_cached(self, ids: list[int]) -> None:
        if not ids:
            return
        session = self._session()
        await session.execute(sa_delete(CacheQueueORM).where(CacheQueueORM.id.in_(ids)))
        await session.commit()

    async def increment_retry(self, ids: list[int]) -> None:
        if not ids:
            return
        session = self._session()
        await session.execute(
            sa_update(CacheQueueORM)
            .where(CacheQueueORM.id.in_(ids))
            .values(retry_count=CacheQueueORM.retry_count + 1)
        )
        await session.commit()

    async def get_cache_count(self) -> int:
        session = self._session()
        result = await session.execute(select(func.count()).select_from(CacheQueueORM))
        return result.scalar() or 0
