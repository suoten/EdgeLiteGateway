"""环形缓冲区测试 - 写入/同步状态/覆盖/压缩/统计

覆盖 storage/ring_buffer.py：
- RingBuffer: put/put_sync/get_pending/mark_synced/mark_failed/load_from_records/get_stats
- 压缩模式: gzip payload + 解压
- 容量覆盖: 满时自动覆盖最旧记录
- 水位线: high/critical watermark
- 增量计数器: pending/syncing 统计
"""

from __future__ import annotations

import gzip
import json

import pytest

from edgelite.storage.ring_buffer import RingBuffer


class TestRingBufferConstructor:
    def test_defaults(self):
        rb = RingBuffer()
        assert rb._capacity == 100000
        assert rb._compress is False
        assert rb._high_watermark == 0.8
        assert rb._critical_watermark == 0.9
        assert rb._total_written == 0
        assert rb._total_synced == 0
        assert rb._total_dropped == 0

    def test_custom_params(self):
        rb = RingBuffer(capacity=100, compress=True, high_watermark=0.5, critical_watermark=0.8)
        assert rb._capacity == 100
        assert rb._compress is True
        assert rb._high_watermark == 0.5
        assert rb._critical_watermark == 0.8


class TestRingBufferPut:
    @pytest.mark.asyncio
    async def test_put_assigns_metadata(self):
        rb = RingBuffer(capacity=10)
        await rb.put({"value": 1})
        record = rb._buffer[0]
        assert record["_id"] == 0
        assert record["_status"] == "pending"
        assert "_created_at" in record
        assert record["value"] == 1

    @pytest.mark.asyncio
    async def test_put_returns_true(self):
        rb = RingBuffer(capacity=10)
        assert await rb.put({"v": 1}) is True

    @pytest.mark.asyncio
    async def test_put_increments_written(self):
        rb = RingBuffer(capacity=10)
        await rb.put({"v": 1})
        await rb.put({"v": 2})
        assert rb._total_written == 2

    @pytest.mark.asyncio
    async def test_put_does_not_mutate_input(self):
        """put 不应修改传入的 dict"""
        rb = RingBuffer(capacity=10)
        record = {"value": 1}
        await rb.put(record)
        assert "_id" not in record  # 输入 dict 不应被修改
        assert "_status" not in record

    @pytest.mark.asyncio
    async def test_put_overwrite_when_full(self):
        """缓冲区满时覆盖最旧记录"""
        rb = RingBuffer(capacity=2)
        await rb.put({"v": 1})
        await rb.put({"v": 2})
        await rb.put({"v": 3})  # 覆盖 v=1
        assert len(rb._buffer) == 2
        assert rb._total_dropped == 1
        # 最旧的 v=1 被覆盖
        assert rb._buffer[0]["v"] == 2
        assert rb._buffer[1]["v"] == 3

    @pytest.mark.asyncio
    async def test_put_with_compress(self):
        """压缩模式：payload 被压缩为 _payload_compressed"""
        rb = RingBuffer(capacity=10, compress=True)
        payload = {"temp": 25.5, "hum": 60}
        await rb.put({"payload": payload})
        record = rb._buffer[0]
        assert "_payload_compressed" in record
        assert "payload" not in record
        # 解压验证
        decompressed = json.loads(gzip.decompress(record["_payload_compressed"]).decode("utf-8"))
        assert decompressed == payload


class TestRingBufferPutSync:
    def test_put_sync_works(self):
        rb = RingBuffer(capacity=10)
        assert rb.put_sync({"v": 1}) is True
        assert rb._total_written == 1
        assert rb._buffer[0]["v"] == 1

    def test_put_sync_overwrite_when_full(self):
        rb = RingBuffer(capacity=2)
        rb.put_sync({"v": 1})
        rb.put_sync({"v": 2})
        rb.put_sync({"v": 3})
        assert rb._total_dropped == 1
        assert len(rb._buffer) == 2


class TestRingBufferGetPending:
    @pytest.mark.asyncio
    async def test_get_pending_returns_and_marks_syncing(self):
        rb = RingBuffer(capacity=10)
        await rb.put({"v": 1})
        await rb.put({"v": 2})
        pending = await rb.get_pending()
        assert len(pending) == 2
        # 返回的记录应标记为 syncing
        for item in pending:
            assert item["_status"] == "syncing"
        # 原缓冲区中的记录也应标记为 syncing
        assert rb._buffer[0]["_status"] == "syncing"

    @pytest.mark.asyncio
    async def test_get_pending_respects_limit(self):
        rb = RingBuffer(capacity=10)
        for i in range(5):
            await rb.put({"v": i})
        pending = await rb.get_pending(limit=2)
        assert len(pending) == 2

    @pytest.mark.asyncio
    async def test_get_pending_priority_filter(self):
        rb = RingBuffer(capacity=10)
        await rb.put({"v": 1, "priority": "high"})
        await rb.put({"v": 2, "priority": "low"})
        pending = await rb.get_pending(priority="high")
        assert len(pending) == 1
        assert pending[0]["v"] == 1

    @pytest.mark.asyncio
    async def test_get_pending_empty(self):
        rb = RingBuffer(capacity=10)
        pending = await rb.get_pending()
        assert pending == []

    @pytest.mark.asyncio
    async def test_get_pending_decompresses(self):
        """压缩模式下 get_pending 返回解压后的 payload"""
        rb = RingBuffer(capacity=10, compress=True)
        payload = {"temp": 25.5}
        await rb.put({"payload": payload})
        pending = await rb.get_pending()
        assert len(pending) == 1
        assert pending[0]["payload"] == payload
        assert "_payload_compressed" not in pending[0]

    @pytest.mark.asyncio
    async def test_get_pending_returns_deepcopy(self):
        """get_pending 返回深拷贝，修改不影响原缓冲区"""
        rb = RingBuffer(capacity=10)
        await rb.put({"nested": {"v": 1}})
        pending = await rb.get_pending()
        pending[0]["nested"]["v"] = 999
        assert rb._buffer[0]["nested"]["v"] == 1  # 原数据不受影响


class TestRingBufferMarkSynced:
    @pytest.mark.asyncio
    async def test_mark_synced_removes_records(self):
        rb = RingBuffer(capacity=10)
        await rb.put({"v": 1})
        await rb.put({"v": 2})
        pending = await rb.get_pending()
        ids = [p["_id"] for p in pending]
        count = await rb.mark_synced(ids)
        assert count == 2
        assert len(rb._buffer) == 0  # synced 的记录被移除
        assert rb._total_synced == 2

    @pytest.mark.asyncio
    async def test_mark_synced_empty_list(self):
        rb = RingBuffer(capacity=10)
        count = await rb.mark_synced([])
        assert count == 0

    @pytest.mark.asyncio
    async def test_mark_synced_nonexistent_ids(self):
        rb = RingBuffer(capacity=10)
        await rb.put({"v": 1})
        count = await rb.mark_synced([999])
        assert count == 0
        assert len(rb._buffer) == 1  # 记录未被移除


class TestRingBufferMarkFailed:
    @pytest.mark.asyncio
    async def test_mark_failed_reverts_to_pending(self):
        rb = RingBuffer(capacity=10)
        await rb.put({"v": 1})
        pending = await rb.get_pending()  # 标记为 syncing
        ids = [p["_id"] for p in pending]
        count = await rb.mark_failed(ids)
        assert count == 1
        assert rb._buffer[0]["_status"] == "pending"

    @pytest.mark.asyncio
    async def test_mark_failed_only_syncing_records(self):
        """mark_failed 只回退 syncing 状态的记录"""
        rb = RingBuffer(capacity=10)
        await rb.put({"v": 1})  # status=pending
        count = await rb.mark_failed([0])
        assert count == 0  # pending 状态不被回退


class TestRingBufferLoadFromRecords:
    @pytest.mark.asyncio
    async def test_load_from_records(self):
        rb = RingBuffer(capacity=100)
        records = [{"v": i} for i in range(5)]
        loaded = await rb.load_from_records(records)
        assert loaded == 5
        assert len(rb._buffer) == 5
        assert rb._total_written == 5

    @pytest.mark.asyncio
    async def test_load_respects_capacity(self):
        rb = RingBuffer(capacity=3)
        records = [{"v": i} for i in range(10)]
        loaded = await rb.load_from_records(records)
        assert loaded == 3
        assert len(rb._buffer) == 3

    @pytest.mark.asyncio
    async def test_load_does_not_mutate_input(self):
        rb = RingBuffer(capacity=100)
        records = [{"v": 1}]
        await rb.load_from_records(records)
        assert "_id" not in records[0]


class TestRingBufferStats:
    @pytest.mark.asyncio
    async def test_get_stats_empty(self):
        rb = RingBuffer(capacity=100)
        stats = rb.get_stats()
        assert stats["capacity"] == 100
        assert stats["size"] == 0
        assert stats["pending"] == 0
        assert stats["syncing"] == 0
        assert stats["usage_pct"] == 0.0
        assert stats["total_written"] == 0
        assert stats["total_synced"] == 0
        assert stats["total_dropped"] == 0

    @pytest.mark.asyncio
    async def test_get_stats_after_operations(self):
        rb = RingBuffer(capacity=100)
        await rb.put({"v": 1})
        await rb.put({"v": 2})
        await rb.get_pending()  # 2 syncing
        stats = rb.get_stats()
        assert stats["size"] == 2
        assert stats["pending"] == 0
        assert stats["syncing"] == 2
        assert stats["total_written"] == 2

    @pytest.mark.asyncio
    async def test_get_stats_usage_pct(self):
        rb = RingBuffer(capacity=10)
        for i in range(5):
            await rb.put({"v": i})
        stats = rb.get_stats()
        assert stats["usage_pct"] == 50.0
