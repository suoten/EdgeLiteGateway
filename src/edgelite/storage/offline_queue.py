"""离线数据队列 - 基于 SQLite 的可靠异步队列。

当上行链路（InfluxDB / 上行服务器）不可达时，将时序记录暂存到本地 SQLite，
链路恢复后批量重传。提供重试计数、最大重试清理、ack 确认等能力，
保证数据"至少一次"送达（at-least-once）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "data/edgelite_offline.db"
_DEFAULT_MAX_SIZE_MB = 200


class OfflineQueue:
    """异步离线队列（aiosqlite 后端）

    Args:
        db_path: SQLite 数据库路径
        max_size_mb: 最大占用空间（MB），超出时丢弃最旧记录
    """

    def __init__(self, db_path: str = _DEFAULT_DB_PATH, max_size_mb: int = _DEFAULT_MAX_SIZE_MB) -> None:
        self._db_path = db_path
        self._max_size_bytes = int(max_size_mb) * 1024 * 1024
        self._db: aiosqlite.Connection | None = None
        self._started = False

    @property
    def _db_path_attr(self) -> str:
        return self._db_path

    async def _ensure_started(self) -> None:
        if self._started:
            return
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS offline_records (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, payload TEXT NOT NULL, retries INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_offline_topic ON offline_records(topic)")
        await self._db.execute("CREATE INDEX IF NOT EXISTS idx_offline_retries ON offline_records(retries)")
        await self._db.commit()
        self._started = True

    async def enqueue(self, topic: str, payload: Any = None) -> int:
        """入队一条记录。返回记录 id。"""
        await self._ensure_started()
        payload_json = json.dumps(payload, ensure_ascii=False, default=str) if not isinstance(payload, str) else payload
        now = _now_iso()
        cur = await self._db.execute(
            "INSERT INTO offline_records(topic, payload, created_at, updated_at) VALUES(?,?,?,?)",
            (topic, payload_json, now, now),
        )
        await self._db.commit()
        await self._enforce_max_size()
        return cur.lastrowid or 0

    async def dequeue_batch(self, size: int = 1000) -> list[dict]:
        """批量取出最早入队的记录（不删除，待 acknowledge 确认后删除）。"""
        await self._ensure_started()
        _cur = await self._db.execute(
            "SELECT id, topic, payload, retries FROM offline_records ORDER BY id ASC LIMIT ?",
            (int(size),),
        )
        rows = await _cur.fetchall()
        result = []
        for rid, topic, payload, retries in rows:
            try:
                payload_obj = json.loads(payload)
            except (TypeError, ValueError):
                payload_obj = payload
            result.append({"id": rid, "topic": topic, "payload": payload_obj, "retries": retries})
        return result

    async def increment_retry(self, ids: list[int], reason: str = "") -> None:
        """对未确认的记录增加重试计数"""
        if not ids:
            return
        await self._ensure_started()
        now = _now_iso()
        placeholders = ",".join("?" * len(ids))
        await self._db.execute(
            f"UPDATE offline_records SET retries = retries + 1, last_error = ?, updated_at = ? WHERE id IN ({placeholders})",
            (reason, now, *ids),
        )
        await self._db.commit()

    async def acknowledge(self, ids: list[int]) -> None:
        """确认成功送达，删除记录"""
        if not ids:
            return
        await self._ensure_started()
        placeholders = ",".join("?" * len(ids))
        await self._db.execute(f"DELETE FROM offline_records WHERE id IN ({placeholders})", tuple(ids))
        await self._db.commit()

    async def flush(self, send_callback: Callable[[list[dict]], Awaitable[Any]] | None = None) -> int:
        """批量重传全部待发记录。返回成功送达数量。"""
        if send_callback is None:
            return 0
        await self._ensure_started()
        sent = 0
        while True:
            batch = await self.dequeue_batch(1000)
            if not batch:
                break
            success_ids: list[int] = []
            try:
                result = await send_callback(batch)
                if isinstance(result, list):
                    success_ids = [int(x) for x in result if x is not None]
                elif isinstance(result, bool) and result:
                    success_ids = [r["id"] for r in batch]
                elif isinstance(result, int):
                    success_ids = [r["id"] for r in batch[:result]]
            except Exception as e:
                logger.warning("flush send_callback failed: %s", e)
            if success_ids:
                await self.acknowledge(success_ids)
                sent += len(success_ids)
            failed_ids = [r["id"] for r in batch if r["id"] not in success_ids]
            if failed_ids:
                await self.increment_retry(failed_ids, "flush send returned failure")
            if len(batch) < 1000:
                break
        return sent

    async def purge_expired(self, max_age_days: int = 7) -> int:
        """删除超过最大保留天数的记录，防止离线队列无限增长"""
        await self._ensure_started()
        cutoff = (datetime.now(UTC).timestamp()) - (max_age_days * 86400)
        cur = await self._db.execute(
            "DELETE FROM offline_records WHERE unixepoch(created_at) < ?",
            (cutoff,),
        )
        await self._db.commit()
        return cur.rowcount or 0

    async def purge_max_retries(self, max_retries: int = 10) -> int:
        """删除超过最大重试次数的记录，防止毒丸消息堆积"""
        await self._ensure_started()
        cur = await self._db.execute("DELETE FROM offline_records WHERE retries >= ?", (int(max_retries),))
        await self._db.commit()
        return cur.rowcount or 0

    async def count(self) -> int:
        await self._ensure_started()
        _cur = await self._db.execute("SELECT COUNT(*) FROM offline_records")
        row = await _cur.fetchone()
        return row[0] if row else 0

    async def _enforce_max_size(self) -> None:
        """超过容量上限时按 FIFO 丢弃最旧记录"""
        try:
            size = os.path.getsize(self._db_path)
        except OSError:
            return
        if size <= self._max_size_bytes:
            return
        excess = size - self._max_size_bytes
        await self._db.execute(
            "DELETE FROM offline_records WHERE id IN (SELECT id FROM offline_records ORDER BY id ASC LIMIT ?)",
            (max(1, excess // 512),),
        )
        await self._db.commit()

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            try:
                await self._db.close()
            except Exception as e:
                logger.warning("offline queue close error: %s", e)
            self._db = None
        self._started = False


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = ["OfflineQueue"]
