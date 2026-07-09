"""环形缓冲区 - 支持增量同步的内存环形缓冲区，用于断网续传"""

from __future__ import annotations

import asyncio
import copy
import gzip
import json
import logging
import threading
import time
from collections import deque

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
        self._lock = threading.Lock()  # FIXED-P2: asyncio.Lock与threading.Lock双锁竞态→统一threading.Lock
        self._total_written = 0
        self._total_synced = 0
        self._total_dropped = 0
        self._high_watermark = high_watermark
        self._critical_watermark = critical_watermark
        # PERF: 维护 pending/syncing 增量计数器，避免 get_stats() 的 O(2n) 全量遍历
        self._pending_count = 0
        self._syncing_count = 0

    async def put(self, record: dict) -> bool:
        """添加记录到缓冲区。缓冲区满时自动覆盖最旧数据并告警。"""
        # FIXED-P1: 原问题-async方法直接with threading.Lock，若锁被线程池持有则阻塞事件循环；
        # 改为asyncio.to_thread与get_pending/mark_synced等一致
        def _put_sync():
            with self._lock:
                record_copy = dict(record)
                record_copy["_id"] = self._total_written
                record_copy["_status"] = "pending"
                record_copy["_created_at"] = time.monotonic()

                if self._compress and "payload" in record_copy:
                    payload_str = json.dumps(record_copy["payload"], ensure_ascii=False)
                    record_copy["_payload_compressed"] = gzip.compress(payload_str.encode("utf-8"))
                    del record_copy["payload"]

                # FIXED-P1: 原问题-满时丢弃新数据；改为让deque maxlen生效，满时自动覆盖最旧数据并告警
                if len(self._buffer) >= self._capacity:
                    self._total_dropped += 1
                    # PERF: deque maxlen 会自动覆盖最旧记录，先获取其状态以更新计数器
                    oldest = self._buffer[0]
                    if oldest.get("_status") == "pending":
                        self._pending_count = max(0, self._pending_count - 1)
                    elif oldest.get("_status") == "syncing":
                        self._syncing_count = max(0, self._syncing_count - 1)
                    logger.warning(
                        "[ring_buffer] code=BUFFER_FULL_OVERWRITE msg=Buffer full (%d/%d), overwriting oldest record, dropped_total=%d",
                        len(self._buffer), self._capacity, self._total_dropped,
                    )

                self._buffer.append(record_copy)
                self._total_written += 1
                self._pending_count += 1

                usage = len(self._buffer) / self._capacity
                if usage >= self._critical_watermark:
                    logger.warning(
                        "[ring_buffer] code=CRITICAL_WATERMARK msg=Usage %.1f%% (%d/%d)",
                        usage * 100, len(self._buffer), self._capacity,
                    )
                elif usage >= self._high_watermark:
                    logger.warning(
                        "[ring_buffer] code=HIGH_WATERMARK msg=Usage %.1f%% (%d/%d)",
                        usage * 100, len(self._buffer), self._capacity,
                    )
                return True
        return await asyncio.to_thread(_put_sync)

    async def get_pending(self, limit: int = 500, priority: str | None = None) -> list[dict]:
        """获取待同步记录，可选按优先级过滤

        返回的记录状态会被标记为 syncing。
        """
        def _get_pending_sync():
            with self._lock:
                result = []
                for record in self._buffer:
                    if record.get("_status") == "pending":
                        if priority and record.get("priority") != priority:
                            continue
                        record["_status"] = "syncing"
                        # PERF: 增量更新计数器，避免 get_stats() 全量遍历
                        self._pending_count = max(0, self._pending_count - 1)
                        self._syncing_count += 1
                        item = copy.deepcopy(record)  # FIXED-P2: 原问题-浅拷贝导致嵌套dict被调用方意外修改；改为深拷贝
                        self._decompress_record(item)  # FIXED-P0: 原问题-压缩模式下get_pending返回_payload_compressed(bytes)而非payload(dict)，下游无法解析
                        result.append(item)
                        if len(result) >= limit:
                            break
                return result
        return await asyncio.to_thread(_get_pending_sync)  # FIXED-P2: O(n)操作移至线程池，避免阻塞事件循环

    async def mark_synced(self, record_ids: list[int]) -> int:
        """将记录标记为已同步，并从缓冲区移除"""
        def _mark_synced_sync():
            with self._lock:
                id_set = set(record_ids)
                count = 0
                # FIXED-P3: 原问题-全量popleft drain+rebuild；改为遍历过滤后clear+extend，减少内存分配
                retained = []
                for record in self._buffer:
                    if record.get("_id") in id_set:
                        # PERF: 根据原状态减计数器
                        status = record.get("_status")
                        if status == "pending":
                            self._pending_count = max(0, self._pending_count - 1)
                        elif status == "syncing":
                            self._syncing_count = max(0, self._syncing_count - 1)
                        record["_status"] = "synced"
                        self._total_synced += 1
                        count += 1
                    else:
                        retained.append(record)
                self._buffer.clear()
                self._buffer.extend(retained)
                return count
        return await asyncio.to_thread(_mark_synced_sync)  # FIXED-P2: O(n)操作移至线程池，避免阻塞事件循环

    async def mark_failed(self, record_ids: list[int]) -> int:
        """将 syncing 状态的记录回退为 pending（同步失败）"""
        def _mark_failed_sync():
            with self._lock:
                id_set = set(record_ids)
                count = 0
                for record in self._buffer:
                    if record.get("_id") in id_set and record.get("_status") == "syncing":
                        record["_status"] = "pending"
                        # PERF: syncing 回退为 pending，增量更新计数器
                        self._syncing_count = max(0, self._syncing_count - 1)
                        self._pending_count += 1
                        count += 1
                return count
        return await asyncio.to_thread(_mark_failed_sync)  # FIXED-P2: 原问题-mark_failed同步O(n)遍历阻塞事件循环，改为asyncio.to_thread与get_pending/mark_synced一致

    async def load_from_records(self, records: list[dict]) -> int:
        """从外部记录列表恢复到缓冲区（如从 SQLite 恢复）

        用于进程重启后从持久化存储恢复数据。
        """
        # FIXED-P2: 原问题-O(n)遍历+threading.Lock在async方法中直接使用，恢复大量记录时阻塞事件循环；
        # 改为asyncio.to_thread与get_pending/mark_synced一致
        def _load_sync():
            with self._lock:
                loaded = 0
                for rec in records:
                    # FIXED-P2: 原问题-不检查容量且直接修改输入记录；改为检查容量并拷贝dict
                    if len(self._buffer) >= self._capacity:
                        logger.warning(
                            "[ring_buffer] load_from_records buffer full (%d/%d), stopping load at %d records",
                            len(self._buffer), self._capacity, loaded,
                        )
                        break
                    rec_copy = dict(rec)
                    rec_copy["_id"] = self._total_written
                    rec_copy["_status"] = "pending"
                    rec_copy["_created_at"] = time.monotonic()
                    self._buffer.append(rec_copy)
                    self._total_written += 1
                    self._pending_count += 1
                    loaded += 1
                return loaded
        return await asyncio.to_thread(_load_sync)

    def put_sync(self, record: dict) -> bool:
        """同步版本的 put，用于从非 async 上下文写入"""
        with self._lock:
            # FIXED-P1: 与 put() 语义一致，满时覆盖最旧数据（deque maxlen 自动淘汰）
            if len(self._buffer) >= self._capacity:
                self._total_dropped += 1
                # PERF: deque maxlen 会自动覆盖最旧记录，先获取其状态以更新计数器
                oldest = self._buffer[0]
                if oldest.get("_status") == "pending":
                    self._pending_count = max(0, self._pending_count - 1)
                elif oldest.get("_status") == "syncing":
                    self._syncing_count = max(0, self._syncing_count - 1)
                logger.warning(
                    "[ring_buffer] code=BUFFER_FULL_OVERWRITE "
                    "msg=Buffer full (%d/%d), overwriting oldest, "
                    "dropped_total=%d",
                    len(self._buffer), self._capacity, self._total_dropped,
                )

            # FIXED-P1: 复制record避免修改传入字典
            record = dict(record)
            record["_id"] = self._total_written
            record["_status"] = "pending"
            record["_created_at"] = time.monotonic()

            if self._compress and "payload" in record:
                payload_str = json.dumps(record["payload"], ensure_ascii=False)
                record["_payload_compressed"] = gzip.compress(payload_str.encode("utf-8"))
                del record["payload"]

            self._buffer.append(record)
            self._total_written += 1
            self._pending_count += 1

            usage = len(self._buffer) / self._capacity
            if usage >= self._critical_watermark:
                logger.warning(
                    "[ring_buffer] code=CRITICAL_WATERMARK msg=Usage %.1f%% (%d/%d)",
                    usage * 100, len(self._buffer), self._capacity,
                )
            return True

    def get_stats(self) -> dict:
        """获取缓冲区统计信息"""
        # PERF: 原实现 O(2n) 全量遍历 _buffer 计算 pending/syncing；
        # 改为维护增量计数器，此处直接返回 O(1)
        with self._lock:
            return {
                "capacity": self._capacity,
                "size": len(self._buffer),
                "pending": max(0, self._pending_count),
                "syncing": max(0, self._syncing_count),
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
