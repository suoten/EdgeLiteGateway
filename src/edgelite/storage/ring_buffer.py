"""环形缓冲区 - 支持增量同步的内存环形缓冲区，用于断网续传"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class RingBuffer:
    """基于内存的环形缓冲区，支持增量同步

    特性:
    - 固定容量，满时自动覆盖最旧数据
    - 每条记录有同步状态: pending / syncing / synced
    - 增量同步流程: get_pending -> mark_syncing -> mark_synced
    - 可选 gzip 压缩大 payload
    - 水位线告警: 80% 警告, 90% 严重
    """

    def __init__(
        self,
        capacity: int = 100000,
        compress: bool = False,
        high_watermark: float = 0.8,
        critical_watermark: float = 0.9,
    ):
        self._buffer: deque = deque(maxlen=capacity)
        self._capacity = capacity
        self._compress = compress
        self._lock = asyncio.Lock()
        self._total_written = 0
        self._total_synced = 0
        self._total_dropped = 0
        self._high_watermark = high_watermark
        self._critical_watermark = critical_watermark

    async def put(self, record: dict) -> bool:
        """添加记录到缓冲区。达到严重水位线时返回 False。"""
        async with self._lock:
            record["_id"] = self._total_written
            record["_status"] = "pending"
            record["_created_at"] = time.monotonic()

            # 可选压缩 payload
            if self._compress and "payload" in record:
                payload_str = json.dumps(record["payload"], ensure_ascii=False)
                record["_payload_compressed"] = gzip.compress(payload_str.encode("utf-8"))
                del record["payload"]

            if len(self._buffer) >= self._capacity:
                self._total_dropped += 1

            self._buffer.append(record)
            self._total_written += 1

            # 检查水位线
            usage = len(self._buffer) / self._capacity
            if usage >= self._critical_watermark:
                logger.warning(
                    "[ring_buffer] code=CRITICAL_WATERMARK msg=Usage %.1f%% (%d/%d)",
                    usage * 100, len(self._buffer), self._capacity,
                )
                return False
            elif usage >= self._high_watermark:
                logger.warning(
                    "[ring_buffer] code=HIGH_WATERMARK msg=Usage %.1f%% (%d/%d)",
                    usage * 100, len(self._buffer), self._capacity,
                )
            return True

    async def get_pending(self, limit: int = 500, priority: str | None = None) -> list[dict]:
        """获取待同步记录，可选按优先级过滤

        返回的记录状态会被标记为 syncing。
        """
        async with self._lock:
            result = []
            for record in self._buffer:
                if record.get("_status") == "pending":
                    # 优先级过滤
                    if priority and record.get("priority") != priority:
                        continue
                    record["_status"] = "syncing"
                    result.append(record)
                    if len(result) >= limit:
                        break
            return result

    async def mark_synced(self, record_ids: list[int]) -> int:
        """将记录标记为已同步，并从缓冲区移除"""
        async with self._lock:
            id_set = set(record_ids)
            count = 0
            new_buffer = deque(maxlen=self._capacity)
            for record in self._buffer:
                if record.get("_id") in id_set:
                    record["_status"] = "synced"
                    self._total_synced += 1
                    count += 1
                    # 不加入 new_buffer = 从缓冲区移除
                else:
                    new_buffer.append(record)
            self._buffer = new_buffer
            return count

    async def mark_failed(self, record_ids: list[int]) -> int:
        """将 syncing 状态的记录回退为 pending（同步失败）"""
        async with self._lock:
            id_set = set(record_ids)
            count = 0
            for record in self._buffer:
                if record.get("_id") in id_set and record.get("_status") == "syncing":
                    record["_status"] = "pending"
                    count += 1
            return count

    async def load_from_records(self, records: list[dict]) -> int:
        """从外部记录列表恢复到缓冲区（如从 SQLite 恢复）

        用于进程重启后从持久化存储恢复数据。
        """
        async with self._lock:
            loaded = 0
            for rec in records:
                rec["_id"] = self._total_written
                rec["_status"] = "pending"
                rec["_created_at"] = time.monotonic()
                self._buffer.append(rec)
                self._total_written += 1
                loaded += 1
            return loaded

    def put_sync(self, record: dict) -> bool:
        """同步版本的 put，用于从非 async 上下文写入

        注意：此方法不加锁，仅适用于单线程场景（如 SQLite 回调或同步初始化）。
        """
        record["_id"] = self._total_written
        record["_status"] = "pending"
        record["_created_at"] = time.monotonic()

        if self._compress and "payload" in record:
            payload_str = json.dumps(record["payload"], ensure_ascii=False)
            record["_payload_compressed"] = gzip.compress(payload_str.encode("utf-8"))
            del record["payload"]

        if len(self._buffer) >= self._capacity:
            self._total_dropped += 1

        self._buffer.append(record)
        self._total_written += 1

        usage = len(self._buffer) / self._capacity
        if usage >= self._critical_watermark:
            logger.warning(
                "[ring_buffer] code=CRITICAL_WATERMARK msg=Usage %.1f%% (%d/%d)",
                usage * 100, len(self._buffer), self._capacity,
            )
            return False
        return True

    def get_stats(self) -> dict:
        """获取缓冲区统计信息"""
        pending = sum(1 for r in self._buffer if r.get("_status") == "pending")
        syncing = sum(1 for r in self._buffer if r.get("_status") == "syncing")
        return {
            "capacity": self._capacity,
            "size": len(self._buffer),
            "pending": pending,
            "syncing": syncing,
            "usage_pct": round(len(self._buffer) / self._capacity * 100, 1) if self._capacity else 0,
            "total_written": self._total_written,
            "total_synced": self._total_synced,
            "total_dropped": self._total_dropped,
        }

    def _decompress_record(self, record: dict) -> dict:
        """解压缩 payload（如果已压缩）"""
        if "_payload_compressed" in record:
            record["payload"] = json.loads(
                gzip.decompress(record["_payload_compressed"]).decode("utf-8")
            )
            del record["_payload_compressed"]
        return record
