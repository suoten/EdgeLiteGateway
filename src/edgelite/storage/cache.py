"""断网缓存管理"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from edgelite.constants import _CACHE_MAX_SIZE
from edgelite.models.db import CacheQueueORM

logger = logging.getLogger(__name__)

MAX_CACHE_SIZE = _CACHE_MAX_SIZE  # FIXED: 原问题-硬编码缓存上限，现引用constants.py


# FIXED: 原问题-json.loads无异常保护，数据库字段损坏导致整个查询崩溃
def _safe_json_loads(value: Any, default: Any = None) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return default
    return value


class CacheManager:
    """InfluxDB不可用时的数据缓存管理"""

    def __init__(self, database: Any):
        self._database = database

    async def add_to_cache(
        self,
        measurement: str,
        tags: dict,
        fields: dict,
        timestamp: str,
    ) -> None:
        # FIXED: 原问题-add_to_cache无try-except保护，缓存写入失败导致数据采集流程崩溃
        try:
            async with self._database.get_session() as session:
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
                    created_at=datetime.now(UTC),
                )
                session.add(orm)
                await session.commit()
        except Exception as e:
            logger.error("CacheManager.add_to_cache failed: %s", e)

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
                    }
                    for r in rows
                ]
        except Exception as e:
            logger.error("CacheManager.get_cached_records failed: %s", e)
            return []

    async def delete_cached(self, ids: list[int]) -> None:
        if not ids:
            return
        # FIXED: 原问题-delete_cached无try-except保护
        try:
            async with self._database.get_session() as session:
                await session.execute(sa_delete(CacheQueueORM).where(CacheQueueORM.id.in_(ids)))
                await session.commit()
        except Exception as e:
            logger.error("CacheManager.delete_cached failed: %s", e)

    async def increment_retry(self, ids: list[int]) -> None:
        if not ids:
            return
        # FIXED: 原问题-increment_retry无try-except保护
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
        # FIXED: 原问题-get_cache_count无try-except保护
        try:
            async with self._database.get_session() as session:
                result = await session.execute(select(func.count()).select_from(CacheQueueORM))
                return result.scalar() or 0
        except Exception as e:
            logger.error("CacheManager.get_cache_count failed: %s", e)
            return 0
