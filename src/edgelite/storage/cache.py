"""断网缓存管理"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)

MAX_CACHE_SIZE = 100_000


class CacheManager:
    """InfluxDB不可用时的数据缓存管理"""

    def __init__(self, conn: aiosqlite.Connection):
        self.conn = conn

    async def add_to_cache(
        self,
        measurement: str,
        tags: dict,
        fields: dict,
        timestamp: str,
    ) -> None:
        """添加缓存条目"""
        # 检查缓存大小
        cursor = await self.conn.execute("SELECT COUNT(*) FROM cache_queue")
        count = (await cursor.fetchone())[0]

        if count >= MAX_CACHE_SIZE:
            # 丢弃最旧的10%数据
            delete_count = MAX_CACHE_SIZE // 10
            await self.conn.execute(
                f"DELETE FROM cache_queue WHERE id IN (SELECT id FROM cache_queue ORDER BY id ASC LIMIT {delete_count})"
            )
            logger.warning("缓存已满，丢弃最旧%d条数据", delete_count)

        await self.conn.execute(
            "INSERT INTO cache_queue (measurement, tags, fields, timestamp, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                measurement,
                json.dumps(tags, ensure_ascii=False),
                json.dumps(fields, ensure_ascii=False),
                timestamp,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await self.conn.commit()

    async def get_cached_records(self, limit: int = 1000) -> list[dict]:
        """获取缓存记录"""
        cursor = await self.conn.execute(
            "SELECT id, measurement, tags, fields, timestamp, retry_count FROM cache_queue ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            results.append({
                "id": row[0],
                "measurement": row[1],
                "tags": json.loads(row[2]),
                "fields": json.loads(row[3]),
                "timestamp": row[4],
                "retry_count": row[5],
            })
        return results

    async def delete_cached(self, ids: list[int]) -> None:
        """删除已成功写入的缓存记录"""
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self.conn.execute(f"DELETE FROM cache_queue WHERE id IN ({placeholders})", ids)
        await self.conn.commit()

    async def increment_retry(self, ids: list[int]) -> None:
        """增加重试计数"""
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self.conn.execute(
            f"UPDATE cache_queue SET retry_count = retry_count + 1 WHERE id IN ({placeholders})", ids
        )
        await self.conn.commit()

    async def get_cache_count(self) -> int:
        """获取缓存条数"""
        cursor = await self.conn.execute("SELECT COUNT(*) FROM cache_queue")
        return (await cursor.fetchone())[0]
