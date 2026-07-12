"""断网缓存管理测试 - 集成环形缓冲区与增量同步

覆盖 storage/cache.py：
- _safe_json_loads: JSON 解析安全保护
- CacheManager: add_to_cache / get_cached_records / mark_syncing / mark_synced_records
- 计数器保护 (max(0, ...))、孤儿记录追踪
- check_watermark: 水位线检测
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from edgelite.models.db import Base, CacheQueueORM
from edgelite.storage.cache import CacheManager, _safe_json_loads


@pytest.fixture
async def db_engine(tmp_path):
    """创建临时 SQLite async engine 并建表"""
    db_path = tmp_path / "test_cache.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def fake_database(db_engine):
    """封装为带 get_session 的伪 database 对象"""

    class FakeDatabase:
        def __init__(self, engine):
            self._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            self.write_lock = None

        def get_session(self):
            return self._session_factory()

    return FakeDatabase(db_engine)


@pytest.fixture
async def cache_manager(fake_database):
    """创建 CacheManager 实例（禁用 RingBuffer 以简化测试）"""
    mgr = CacheManager(fake_database)
    # 禁用 RingBuffer 初始化，专注于测试 SQLite 路径
    mgr._ring_buffer_initialized = True
    mgr._ring_buffer = None
    yield mgr
    await mgr.close()


class TestSafeJsonLoads:
    def test_valid_json_string(self):
        """合法 JSON 字符串应解析为对象"""
        assert _safe_json_loads('{"a": 1}') == {"a": 1}

    def test_invalid_json_returns_default(self):
        """非法 JSON 应返回默认值"""
        assert _safe_json_loads("not json", default={}) == {}

    def test_non_string_returns_value(self):
        """非字符串应原样返回"""
        assert _safe_json_loads(123) == 123
        assert _safe_json_loads(None) is None
        assert _safe_json_loads({"a": 1}) == {"a": 1}

    def test_invalid_json_default_none(self):
        """默认 default=None"""
        assert _safe_json_loads("invalid") is None


class TestCacheManagerInit:
    @pytest.mark.asyncio
    async def test_initial_state(self, fake_database):
        """新实例初始状态"""
        mgr = CacheManager(fake_database)
        assert mgr._cache_count == 0
        assert mgr._ring_buffer is None
        assert mgr._ring_buffer_initialized is False
        assert mgr._orphan_sqlite_ids == set()
        await mgr.close()

    @pytest.mark.asyncio
    async def test_ensure_ring_buffer_disabled(self, cache_manager):
        """已标记初始化后 _ensure_ring_buffer 应为 no-op"""
        cache_manager._ensure_ring_buffer()
        assert cache_manager._ring_buffer is None


class TestAddToCache:
    @pytest.mark.asyncio
    async def test_add_to_cache_success(self, cache_manager):
        """成功写入应返回 sqlite_ok=True"""
        result = await cache_manager.add_to_cache("cpu", {"host": "h1"}, {"value": 50}, "2024-01-01T00:00:00")
        assert result["sqlite_ok"] is True
        assert result["ring_ok"] is False  # RingBuffer 未初始化
        assert cache_manager._cache_count == 1

    @pytest.mark.asyncio
    async def test_add_to_cache_empty_measurement(self, cache_manager):
        """空 measurement 应跳过写入"""
        result = await cache_manager.add_to_cache("", {}, {}, "")
        assert result["sqlite_ok"] is False
        assert result["ring_ok"] is False

    @pytest.mark.asyncio
    async def test_add_to_cache_increments_count(self, cache_manager):
        """多次写入应递增计数器"""
        for i in range(5):
            await cache_manager.add_to_cache("m", {}, {"v": i}, str(i))
        assert cache_manager._cache_count == 5

    @pytest.mark.asyncio
    async def test_add_to_cache_creates_orphan_on_ring_failure(self, cache_manager):
        """SQLite 成功 RingBuffer 失败时应记录孤儿 ID"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        # RingBuffer 未初始化（None），写入应失败，孤儿集合应非空
        assert len(cache_manager._orphan_sqlite_ids) == 1

    @pytest.mark.asyncio
    async def test_add_to_cache_persists_data(self, cache_manager, fake_database):
        """写入的数据应能从数据库查回"""
        await cache_manager.add_to_cache("temp", {"host": "h1"}, {"value": 25.5}, "2024-01-01")
        records = await cache_manager.get_cached_records()
        assert len(records) == 1
        assert records[0]["measurement"] == "temp"
        assert records[0]["tags"] == {"host": "h1"}
        assert records[0]["fields"] == {"value": 25.5}


class TestGetCachedRecords:
    @pytest.mark.asyncio
    async def test_get_cached_records_empty(self, cache_manager):
        """空缓存应返回空列表"""
        assert await cache_manager.get_cached_records() == []

    @pytest.mark.asyncio
    async def test_get_cached_records_limit(self, cache_manager):
        """应支持 limit 参数"""
        for i in range(10):
            await cache_manager.add_to_cache("m", {}, {"v": i}, str(i))
        records = await cache_manager.get_cached_records(limit=5)
        assert len(records) == 5

    @pytest.mark.asyncio
    async def test_get_cached_records_after(self, cache_manager):
        """游标分页应正确过滤"""
        for i in range(5):
            await cache_manager.add_to_cache("m", {}, {"v": i}, str(i))
        first_batch = await cache_manager.get_cached_records_after(limit=2)
        assert len(first_batch) == 2
        after_id = first_batch[-1]["id"]
        second_batch = await cache_manager.get_cached_records_after(after_id=after_id, limit=2)
        assert len(second_batch) == 2
        assert second_batch[0]["id"] > after_id


class TestMarkSyncing:
    @pytest.mark.asyncio
    async def test_mark_syncing_updates_status(self, cache_manager):
        """mark_syncing 应将状态改为 syncing"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        records = await cache_manager.get_cached_records()
        rid = records[0]["id"]
        await cache_manager.mark_syncing([rid])
        pending = await cache_manager.get_pending_records()
        assert len(pending) == 0  # 已标记 syncing，不再是 pending

    @pytest.mark.asyncio
    async def test_mark_syncing_empty_list(self, cache_manager):
        """空列表应为 no-op"""
        await cache_manager.mark_syncing([])


class TestMarkSyncedRecords:
    @pytest.mark.asyncio
    async def test_mark_synced_deletes_records(self, cache_manager):
        """mark_synced_records 应删除已同步记录"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        await cache_manager.add_to_cache("m", {}, {"v": 2}, "2")
        records = await cache_manager.get_cached_records()
        rids = [r["id"] for r in records]
        await cache_manager.mark_synced_records(rids)
        assert cache_manager._cache_count == 0
        assert await cache_manager.get_cached_records() == []

    @pytest.mark.asyncio
    async def test_mark_synced_decrements_count(self, cache_manager):
        """同步后计数器应递减"""
        for i in range(5):
            await cache_manager.add_to_cache("m", {}, {"v": i}, str(i))
        records = await cache_manager.get_cached_records()
        await cache_manager.mark_synced_records([records[0]["id"]])
        assert cache_manager._cache_count == 4

    @pytest.mark.asyncio
    async def test_mark_synced_count_never_negative(self, cache_manager):
        """计数器不应变为负数"""
        # 手动设置一个较低的计数器
        cache_manager._cache_count = 1
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        records = await cache_manager.get_cached_records()
        # 删除超过计数器的数量
        await cache_manager.mark_synced_records([records[0]["id"]])
        assert cache_manager._cache_count >= 0

    @pytest.mark.asyncio
    async def test_mark_synced_empty_list(self, cache_manager):
        """空列表应为 no-op"""
        await cache_manager.mark_synced_records([])


class TestResetSyncingToPending:
    @pytest.mark.asyncio
    async def test_reset_syncing_to_pending(self, cache_manager):
        """syncing 状态应能重置为 pending"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        records = await cache_manager.get_cached_records()
        await cache_manager.mark_syncing([records[0]["id"]])
        count = await cache_manager.reset_syncing_to_pending()
        assert count == 1
        pending = await cache_manager.get_pending_records()
        assert len(pending) == 1


class TestGetCacheCount:
    @pytest.mark.asyncio
    async def test_get_cache_count_empty(self, cache_manager):
        """空缓存计数为 0"""
        assert await cache_manager.get_cache_count() == 0

    @pytest.mark.asyncio
    async def test_get_cache_count_after_adds(self, cache_manager):
        """计数应与实际记录数一致"""
        for i in range(5):
            await cache_manager.add_to_cache("m", {}, {"v": i}, str(i))
        assert await cache_manager.get_cache_count() == 5


class TestCheckWatermark:
    @pytest.mark.asyncio
    async def test_normal_level(self, cache_manager):
        """低使用率应为 normal"""
        cache_manager._cache_count = 10
        result = await cache_manager.check_watermark()
        assert result["level"] == "normal"
        assert result["cache_count"] == 10
        assert "usage_pct" in result
        assert "max_size" in result

    @pytest.mark.asyncio
    async def test_critical_level(self, cache_manager):
        """>=90% 应为 critical"""
        from edgelite.storage.cache import MAX_CACHE_SIZE

        cache_manager._cache_count = int(MAX_CACHE_SIZE * 0.95)
        result = await cache_manager.check_watermark()
        assert result["level"] == "critical"

    @pytest.mark.asyncio
    async def test_high_level(self, cache_manager):
        """>=80% 应为 high"""
        from edgelite.storage.cache import MAX_CACHE_SIZE

        cache_manager._cache_count = int(MAX_CACHE_SIZE * 0.85)
        result = await cache_manager.check_watermark()
        assert result["level"] == "high"


class TestGetOrphanRecords:
    @pytest.mark.asyncio
    async def test_no_orphans(self, cache_manager):
        """无孤儿时应返回空列表"""
        assert await cache_manager.get_orphan_records() == []

    @pytest.mark.asyncio
    async def test_returns_orphan_records(self, cache_manager):
        """应返回孤儿 SQLite 记录"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        # add_to_cache 成功但 RingBuffer 失败，应已记录孤儿
        orphans = await cache_manager.get_orphan_records()
        assert len(orphans) == 1
        assert orphans[0]["status"] == "orphan"


class TestClearOrphanIds:
    @pytest.mark.asyncio
    async def test_clear_orphan_ids(self, cache_manager):
        """应从孤儿集合中清除已同步的 ID"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        orphan_ids = list(cache_manager._orphan_sqlite_ids)
        assert len(orphan_ids) == 1
        await cache_manager.clear_orphan_ids(orphan_ids)
        assert len(cache_manager._orphan_sqlite_ids) == 0

    @pytest.mark.asyncio
    async def test_clear_orphan_ids_partial(self, cache_manager):
        """应仅清除指定的 ID"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        await cache_manager.add_to_cache("m", {}, {"v": 2}, "2")
        all_ids = list(cache_manager._orphan_sqlite_ids)
        assert len(all_ids) == 2
        await cache_manager.clear_orphan_ids([all_ids[0]])
        assert len(cache_manager._orphan_sqlite_ids) == 1


class TestDeleteCached:
    @pytest.mark.asyncio
    async def test_delete_cached(self, cache_manager):
        """应删除指定 ID 的记录"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        records = await cache_manager.get_cached_records()
        await cache_manager.delete_cached([records[0]["id"]])
        assert await cache_manager.get_cached_records() == []
        assert cache_manager._cache_count == 0

    @pytest.mark.asyncio
    async def test_delete_cached_empty_list(self, cache_manager):
        """空列表应为 no-op"""
        await cache_manager.delete_cached([])


class TestIncrementRetry:
    @pytest.mark.asyncio
    async def test_increment_retry(self, cache_manager):
        """应递增记录的重试计数"""
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        records = await cache_manager.get_cached_records()
        rid = records[0]["id"]
        await cache_manager.increment_retry([rid])
        records = await cache_manager.get_cached_records()
        assert records[0]["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_increment_retry_empty_list(self, cache_manager):
        """空列表应为 no-op"""
        await cache_manager.increment_retry([])
