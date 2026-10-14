"""离线数据队列测试 - 入队/出队/重试/确认/清理/批量重传

覆盖 storage/offline_queue.py：
- OfflineQueue: enqueue/dequeue_batch/increment_retry/acknowledge/count
- purge_expired/purge_max_retries
- flush: 批量重传 + callback
- close: 连接关闭
"""

from __future__ import annotations

import pytest

from edgelite.storage.offline_queue import OfflineQueue


@pytest.fixture
async def queue(tmp_path):
    """创建临时 OfflineQueue 实例"""
    q = OfflineQueue(db_path=str(tmp_path / "offline.db"), max_size_mb=10)
    yield q
    await q.close()


class TestOfflineQueueEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_returns_id(self, queue):
        rid = await queue.enqueue("topic1", {"v": 1})
        assert rid > 0

    @pytest.mark.asyncio
    async def test_enqueue_increments_count(self, queue):
        await queue.enqueue("topic1", {"v": 1})
        await queue.enqueue("topic1", {"v": 2})
        assert await queue.count() == 2

    @pytest.mark.asyncio
    async def test_enqueue_string_payload(self, queue):
        rid = await queue.enqueue("topic1", "plain string payload")
        assert rid > 0
        batch = await queue.dequeue_batch()
        assert batch[0]["payload"] == "plain string payload"


class TestOfflineQueueDequeue:
    @pytest.mark.asyncio
    async def test_dequeue_returns_fifo_order(self, queue):
        await queue.enqueue("t1", {"v": 1})
        await queue.enqueue("t1", {"v": 2})
        await queue.enqueue("t1", {"v": 3})
        batch = await queue.dequeue_batch()
        assert len(batch) == 3
        assert batch[0]["payload"]["v"] == 1
        assert batch[1]["payload"]["v"] == 2
        assert batch[2]["payload"]["v"] == 3

    @pytest.mark.asyncio
    async def test_dequeue_respects_size(self, queue):
        for i in range(5):
            await queue.enqueue("t1", {"v": i})
        batch = await queue.dequeue_batch(size=2)
        assert len(batch) == 2

    @pytest.mark.asyncio
    async def test_dequeue_empty(self, queue):
        batch = await queue.dequeue_batch()
        assert batch == []

    @pytest.mark.asyncio
    async def test_dequeue_preserves_topic(self, queue):
        await queue.enqueue("topic_a", {"v": 1})
        await queue.enqueue("topic_b", {"v": 2})
        batch = await queue.dequeue_batch()
        assert batch[0]["topic"] == "topic_a"
        assert batch[1]["topic"] == "topic_b"

    @pytest.mark.asyncio
    async def test_dequeue_initial_retries_zero(self, queue):
        await queue.enqueue("t1", {"v": 1})
        batch = await queue.dequeue_batch()
        assert batch[0]["retries"] == 0


class TestOfflineQueueIncrementRetry:
    @pytest.mark.asyncio
    async def test_increment_retry(self, queue):
        rid = await queue.enqueue("t1", {"v": 1})
        await queue.increment_retry([rid], "send failed")
        batch = await queue.dequeue_batch()
        assert batch[0]["retries"] == 1

    @pytest.mark.asyncio
    async def test_increment_retry_multiple(self, queue):
        rid = await queue.enqueue("t1", {"v": 1})
        await queue.increment_retry([rid], "fail 1")
        await queue.increment_retry([rid], "fail 2")
        await queue.increment_retry([rid], "fail 3")
        batch = await queue.dequeue_batch()
        assert batch[0]["retries"] == 3

    @pytest.mark.asyncio
    async def test_increment_retry_empty_list(self, queue):
        """空列表不应抛异常"""
        await queue.increment_retry([], "reason")


class TestOfflineQueueAcknowledge:
    @pytest.mark.asyncio
    async def test_acknowledge_deletes_records(self, queue):
        rid1 = await queue.enqueue("t1", {"v": 1})
        rid2 = await queue.enqueue("t1", {"v": 2})
        await queue.acknowledge([rid1])
        assert await queue.count() == 1
        batch = await queue.dequeue_batch()
        assert batch[0]["id"] == rid2

    @pytest.mark.asyncio
    async def test_acknowledge_multiple(self, queue):
        r1 = await queue.enqueue("t1", {"v": 1})
        r2 = await queue.enqueue("t1", {"v": 2})
        r3 = await queue.enqueue("t1", {"v": 3})
        await queue.acknowledge([r1, r2, r3])
        assert await queue.count() == 0

    @pytest.mark.asyncio
    async def test_acknowledge_empty_list(self, queue):
        await queue.acknowledge([])


class TestOfflineQueuePurge:
    @pytest.mark.asyncio
    async def test_purge_max_retries(self, queue):
        r1 = await queue.enqueue("t1", {"v": 1})
        await queue.enqueue("t1", {"v": 2})
        # r1 重试 5 次
        for _ in range(5):
            await queue.increment_retry([r1], "fail")
        deleted = await queue.purge_max_retries(max_retries=5)
        assert deleted == 1
        assert await queue.count() == 1

    @pytest.mark.asyncio
    async def test_purge_max_retries_none_expired(self, queue):
        await queue.enqueue("t1", {"v": 1})
        deleted = await queue.purge_max_retries(max_retries=10)
        assert deleted == 0


class TestOfflineQueueFlush:
    @pytest.mark.asyncio
    async def test_flush_with_callback_success(self, queue):
        await queue.enqueue("t1", {"v": 1})
        await queue.enqueue("t1", {"v": 2})

        async def callback(batch):
            return [r["id"] for r in batch]  # 全部成功

        sent = await queue.flush(callback)
        assert sent == 2
        assert await queue.count() == 0  # 全部确认删除

    @pytest.mark.asyncio
    async def test_flush_with_callback_partial_failure(self, queue):
        r1 = await queue.enqueue("t1", {"v": 1})
        await queue.enqueue("t1", {"v": 2})

        async def callback(batch):
            return [r1]  # 只成功第一个

        sent = await queue.flush(callback)
        assert sent == 1
        # 失败的记录重试计数应增加
        batch = await queue.dequeue_batch()
        assert len(batch) == 1
        assert batch[0]["retries"] == 1

    @pytest.mark.asyncio
    async def test_flush_no_callback_returns_zero(self, queue):
        await queue.enqueue("t1", {"v": 1})
        sent = await queue.flush(None)
        assert sent == 0

    @pytest.mark.asyncio
    async def test_flush_callback_bool_true(self, queue):
        await queue.enqueue("t1", {"v": 1})

        async def callback(batch):
            return True  # 全部成功

        sent = await queue.flush(callback)
        assert sent == 1
        assert await queue.count() == 0

    @pytest.mark.asyncio
    async def test_flush_callback_exception(self, queue):
        await queue.enqueue("t1", {"v": 1})

        async def callback(batch):
            raise RuntimeError("network error")

        sent = await queue.flush(callback)
        assert sent == 0
        # 记录仍在队列中，且重试计数增加
        assert await queue.count() == 1


class TestOfflineQueueClose:
    @pytest.mark.asyncio
    async def test_close_resets_state(self, tmp_path):
        q = OfflineQueue(db_path=str(tmp_path / "offline.db"))
        await q.enqueue("t1", {"v": 1})
        assert q._started is True
        await q.close()
        assert q._started is False
        assert q._db is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self, tmp_path):
        q = OfflineQueue(db_path=str(tmp_path / "offline.db"))
        await q.enqueue("t1", {"v": 1})
        await q.close()
        # 再次 close 不应抛异常
        await q.close()


class TestFlushInfiniteLoopGuard:
    """P1-1 回归: 满批且本轮零成功时应 break，防止死循环"""

    @pytest.mark.asyncio
    async def test_full_batch_zero_success_breaks(self, queue, caplog):
        # 灌入超过一批(1000)的记录，callback 始终失败
        for i in range(2500):
            await queue.enqueue(f"t{i}", {"v": i})
        calls = []

        async def failing_callback(batch):
            calls.append(len(batch))
            return False  # bool False → 全部失败

        with caplog.at_level("WARNING", logger="edgelite.storage.offline_queue"):
            sent = await queue.flush(failing_callback)

        assert sent == 0
        # 满批零成功只允许一次满批尝试，不能无限循环重取同一批
        assert len(calls) == 1, f"expected single full-batch attempt, got {len(calls)} calls: {calls}"
        assert calls[0] == 1000
        # 记录已 increment_retry，未被删除
        assert await queue.count() == 2500
        assert any("zero successes" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_partial_success_keeps_progressing(self, queue):
        # 部分成功（仅最早 3 条固定失败）→ 有进度，应继续处理剩余记录直到不满批
        for i in range(1200):
            await queue.enqueue("t", {"v": i})
        first_ids = [r["id"] for r in await queue.dequeue_batch(3)]

        async def mostly_success_callback(batch):
            return [r["id"] for r in batch if r["id"] not in first_ids]  # 仅最早 3 条失败

        sent = await queue.flush(mostly_success_callback)
        assert sent == 1197
        assert await queue.count() == 3

    @pytest.mark.asyncio
    async def test_small_batch_unchanged_semantics(self, queue):
        # 不满批即停的原语义保持
        for i in range(5):
            await queue.enqueue("t", {"v": i})

        async def failing_callback(batch):
            return False

        sent = await queue.flush(failing_callback)
        assert sent == 0
        assert await queue.count() == 5


class TestEnforceMaxSizeByCount:
    """P1-2 回归: 淘汰按 SELECT COUNT(*) 计数，不依赖文件大小"""

    @pytest.mark.asyncio
    async def test_eviction_by_record_count(self, tmp_path):
        # max_records = 1MB // 512 = 2048 → 灌入 2100 条应淘汰 52 条
        q = OfflineQueue(db_path=str(tmp_path / "offline.db"), max_size_mb=1)
        try:
            for i in range(2100):
                await q.enqueue("t", {"v": i})
            count = await q.count()
            assert count == 2048
        finally:
            await q.close()

    @pytest.mark.asyncio
    async def test_no_eviction_below_limit(self, queue):
        for i in range(10):
            await queue.enqueue("t", {"v": i})
        assert await queue.count() == 10

    @pytest.mark.asyncio
    async def test_oversized_file_does_not_trigger_false_eviction(self, tmp_path):
        # P1-2 原问题: 文件因 WAL/空闲页远大于阈值但记录数很少时，旧实现会 perpetual 误删。
        # 新实现只看记录数 → 记录数低于 max_records 时不淘汰。
        q = OfflineQueue(db_path=str(tmp_path / "offline.db"), max_size_mb=1)
        try:
            for i in range(10):
                await q.enqueue("t", {"v": i})
            # 模拟文件膨胀（不实际改文件，直接断言淘汰逻辑基于计数）
            excess_expected = 10 - (1 * 1024 * 1024 // 512)
            assert excess_expected < 0  # 记录数远低于上限
            await q._enforce_max_size()
            assert await q.count() == 10
        finally:
            await q.close()
