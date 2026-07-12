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
