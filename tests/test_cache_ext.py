"""断网缓存管理扩展测试 - 补充 cache.py 未覆盖的分支"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from edgelite.models.db import Base
from edgelite.storage.cache import (
    MAX_CACHE_SIZE,
    CacheManager,
    _epoch_to_iso,
    _ts_to_epoch,
)
from edgelite.storage.ring_buffer import RingBuffer

sys.path.insert(0, "src")


@asynccontextmanager
async def _raising_session():
    raise RuntimeError("db error")
    yield  # pragma: no cover


@asynccontextmanager
async def _readonly_session():
    raise RuntimeError("attempt to write a readonly database")
    yield  # pragma: no cover


@asynccontextmanager
async def _orphan_fail_session():
    raise RuntimeError("orphan persist fail")
    yield  # pragma: no cover


@pytest.fixture
async def db_engine(tmp_path):
    db_path = tmp_path / "test_cache_ext.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def fake_database(db_engine):
    class FakeDatabase:
        def __init__(self, engine):
            self._session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            self.write_lock = None

        def get_session(self):
            return self._session_factory()

    return FakeDatabase(db_engine)


@pytest.fixture
async def cache_manager(fake_database):
    mgr = CacheManager(fake_database)
    mgr._ring_buffer_initialized = True
    mgr._ring_buffer = None
    yield mgr
    await mgr.close()


@pytest.fixture
async def cache_manager_rb(fake_database):
    mgr = CacheManager(fake_database)
    mgr._ring_buffer_initialized = True
    mgr._ring_buffer = RingBuffer(capacity=100, compress=False)
    yield mgr
    await mgr.close()


class TestTsToEpoch:
    def test_int(self):
        assert _ts_to_epoch(100) == 100.0

    def test_float(self):
        assert _ts_to_epoch(1.5) == 1.5

    def test_datetime(self):
        dt = datetime(2024, 1, 1, tzinfo=UTC)
        assert _ts_to_epoch(dt) == dt.timestamp()

    def test_iso_string(self):
        result = _ts_to_epoch("2024-01-01T00:00:00")
        assert isinstance(result, float)
        assert result > 0

    def test_iso_string_with_z(self):
        result = _ts_to_epoch("2024-01-01T00:00:00Z")
        assert isinstance(result, float)
        assert result > 0

    def test_invalid_returns_now(self):
        before = datetime.now(UTC).timestamp()
        result = _ts_to_epoch("not-a-date")
        assert abs(result - before) < 1.0


class TestEpochToIso:
    def test_valid(self):
        result = _epoch_to_iso(1700000000.0)
        assert "2023" in result
        assert "T" in result

    def test_invalid_string_returns_now(self):
        result = _epoch_to_iso("not-a-number")
        assert "T" in result

    def test_none_returns_now(self):
        result = _epoch_to_iso(None)
        assert "T" in result

    def test_oversized_float_returns_now(self):
        # 超大负时间戳在多数平台上触发 OSError: timestamp out of range
        result = _epoch_to_iso(-(2**40))
        assert "T" in result


class TestEnsureRingBuffer:
    async def test_initializes_with_config(self, fake_database):
        mgr = CacheManager(fake_database)
        cache_cfg = SimpleNamespace(
            incremental_sync_enabled=True,
            ring_buffer_capacity=50,
            ring_buffer_compress=False,
            high_watermark_pct=0.8,
            critical_watermark_pct=0.9,
        )
        config = SimpleNamespace(cache=cache_cfg)
        with patch("edgelite.config.get_config", return_value=config):
            mgr._ensure_ring_buffer()
        assert mgr._ring_buffer is not None
        assert mgr._ring_buffer_initialized is True
        await mgr.close()

    async def test_disabled_when_incremental_sync_disabled(self, fake_database):
        mgr = CacheManager(fake_database)
        cache_cfg = SimpleNamespace(incremental_sync_enabled=False)
        config = SimpleNamespace(cache=cache_cfg)
        with patch("edgelite.config.get_config", return_value=config):
            mgr._ensure_ring_buffer()
        assert mgr._ring_buffer is None
        await mgr.close()

    async def test_no_cache_config(self, fake_database):
        mgr = CacheManager(fake_database)
        config = SimpleNamespace(cache=None)
        with patch("edgelite.config.get_config", return_value=config):
            mgr._ensure_ring_buffer()
        assert mgr._ring_buffer is None
        await mgr.close()

    async def test_exception_falls_back_to_sqlite(self, fake_database):
        mgr = CacheManager(fake_database)
        with patch("edgelite.config.get_config", side_effect=RuntimeError("no config")):
            mgr._ensure_ring_buffer()
        assert mgr._ring_buffer is None
        assert mgr._ring_buffer_initialized is True
        await mgr.close()


class TestRestoreFromSqlite:
    async def test_no_ring_buffer_calibrates_count(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        await cache_manager.add_to_cache("m", {}, {"v": 2}, "2")
        cache_manager._cache_count = 0
        result = await cache_manager.restore_from_sqlite()
        assert result == 0
        assert cache_manager._cache_count == 2

    async def test_no_ring_buffer_empty_db(self, cache_manager):
        result = await cache_manager.restore_from_sqlite()
        assert result == 0
        assert cache_manager._cache_count == 0

    async def test_with_ring_buffer_loads_records(self, cache_manager_rb):
        await cache_manager_rb.add_to_cache("m", {"h": "1"}, {"v": 1}, "1")
        await cache_manager_rb.add_to_cache("m", {"h": "2"}, {"v": 2}, "2")
        cache_manager_rb._ring_buffer._buffer.clear()
        cache_manager_rb._ring_buffer._pending_count = 0
        result = await cache_manager_rb.restore_from_sqlite()
        assert result == 2

    async def test_restores_orphan_ids_from_sqlite(self, cache_manager):
        async with cache_manager._database.get_session() as session:
            from sqlalchemy import text

            await session.execute(text("CREATE TABLE IF NOT EXISTS _orphan_ids (id INTEGER PRIMARY KEY)"))
            await session.execute(text("INSERT INTO _orphan_ids (id) VALUES (999)"))
            await session.commit()
        cache_manager._orphan_persisted = False
        await cache_manager.restore_from_sqlite()
        assert 999 in cache_manager._orphan_sqlite_ids
        assert cache_manager._orphan_persisted is True

    async def test_orphan_restore_skipped_when_already_persisted(self, cache_manager):
        cache_manager._orphan_persisted = True
        cache_manager._orphan_sqlite_ids = set()
        await cache_manager.restore_from_sqlite()
        assert cache_manager._orphan_sqlite_ids == set()

    async def test_restore_with_ring_buffer_removes_restored_orphans(self, cache_manager_rb):
        mock_rb = AsyncMock()
        mock_rb.put = AsyncMock(return_value=False)
        mock_rb.load_from_records = AsyncMock(return_value=1)
        cache_manager_rb._ring_buffer = mock_rb
        await cache_manager_rb.add_to_cache("m", {}, {"v": 1}, "1")
        assert len(cache_manager_rb._orphan_sqlite_ids) == 1
        orphan_id = list(cache_manager_rb._orphan_sqlite_ids)[0]
        result = await cache_manager_rb.restore_from_sqlite()
        assert result == 1
        assert orphan_id not in cache_manager_rb._orphan_sqlite_ids

    async def test_restore_ring_buffer_load_exception(self, cache_manager_rb):
        await cache_manager_rb.add_to_cache("m", {}, {"v": 1}, "1")
        mock_rb = AsyncMock()
        mock_rb.load_from_records = AsyncMock(side_effect=RuntimeError("load fail"))
        cache_manager_rb._ring_buffer = mock_rb
        result = await cache_manager_rb.restore_from_sqlite()
        assert result == 0


class TestAddToCacheExtended:
    async def test_eviction_when_full(self, cache_manager):
        from edgelite.constants import _CACHE_EVICTION_RATIO

        cache_manager._cache_count = MAX_CACHE_SIZE
        result = await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        assert result["sqlite_ok"] is True
        delete_count = MAX_CACHE_SIZE // _CACHE_EVICTION_RATIO
        assert cache_manager._cache_count == MAX_CACHE_SIZE + 1 - delete_count

    async def test_readonly_database_exception(self, cache_manager):
        CacheManager._last_log_time = 0.0
        cache_manager._database.get_session = _readonly_session
        try:
            result = await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
            assert result["sqlite_ok"] is False
            assert result["ring_ok"] is False
        finally:
            CacheManager._last_log_time = 0.0

    async def test_generic_database_exception(self, cache_manager):
        CacheManager._last_log_time = 0.0
        cache_manager._database.get_session = _raising_session
        try:
            result = await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
            assert result["sqlite_ok"] is False
        finally:
            CacheManager._last_log_time = 0.0

    async def test_with_ring_buffer_success(self, cache_manager_rb):
        result = await cache_manager_rb.add_to_cache("m", {"h": "1"}, {"v": 1}, "1")
        assert result["sqlite_ok"] is True
        assert result["ring_ok"] is True
        assert cache_manager_rb._cache_count == 1

    async def test_ring_buffer_write_exception(self, cache_manager):
        mock_rb = AsyncMock()
        mock_rb.put = AsyncMock(side_effect=RuntimeError("ring fail"))
        cache_manager._ring_buffer = mock_rb
        result = await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        assert result["sqlite_ok"] is True
        assert result["ring_ok"] is False
        assert len(cache_manager._orphan_sqlite_ids) == 1

    async def test_ring_buffer_put_returns_false(self, cache_manager):
        mock_rb = AsyncMock()
        mock_rb.put = AsyncMock(return_value=False)
        cache_manager._ring_buffer = mock_rb
        result = await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        assert result["sqlite_ok"] is True
        assert result["ring_ok"] is False
        assert len(cache_manager._orphan_sqlite_ids) == 1

    async def test_orphan_persistence_exception(self, cache_manager):
        real_get_session = cache_manager._database.get_session
        sessions = [real_get_session(), _orphan_fail_session()]
        cache_manager._database.get_session = lambda: sessions.pop(0)
        result = await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        assert result["sqlite_ok"] is True
        assert result["ring_ok"] is False
        assert len(cache_manager._orphan_sqlite_ids) == 1


class TestQueryExceptionPaths:
    async def test_get_cached_records_exception(self, cache_manager):
        cache_manager._database.get_session = _raising_session
        assert await cache_manager.get_cached_records() == []

    async def test_get_cached_records_after_exception(self, cache_manager):
        cache_manager._database.get_session = _raising_session
        assert await cache_manager.get_cached_records_after() == []

    async def test_get_pending_records_returns_pending(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        await cache_manager.add_to_cache("m", {}, {"v": 2}, "2")
        records = await cache_manager.get_cached_records()
        await cache_manager.mark_syncing([records[0]["id"]])
        pending = await cache_manager.get_pending_records()
        assert len(pending) == 1
        assert pending[0]["status"] == "pending"

    async def test_get_pending_records_exception(self, cache_manager):
        cache_manager._database.get_session = _raising_session
        assert await cache_manager.get_pending_records() == []

    async def test_get_cache_count_exception(self, cache_manager):
        cache_manager._database.get_session = _raising_session
        assert await cache_manager.get_cache_count() == 0


class TestMutationExceptionPaths:
    async def test_mark_syncing_exception(self, cache_manager):
        cache_manager._database.get_session = _raising_session
        await cache_manager.mark_syncing([1, 2])

    async def test_mark_synced_records_exception(self, cache_manager):
        cache_manager._database.get_session = _raising_session
        await cache_manager.mark_synced_records([1, 2])

    async def test_reset_syncing_to_pending_exception(self, cache_manager):
        cache_manager._database.get_session = _raising_session
        assert await cache_manager.reset_syncing_to_pending() == 0

    async def test_delete_cached_exception(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        records = await cache_manager.get_cached_records()
        cache_manager._database.get_session = _raising_session
        await cache_manager.delete_cached([records[0]["id"]])

    async def test_delete_cached_count_negative_guard(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        records = await cache_manager.get_cached_records()
        cache_manager._cache_count = 0
        await cache_manager.delete_cached([records[0]["id"]])
        assert cache_manager._cache_count == 0

    async def test_increment_retry_exception(self, cache_manager):
        cache_manager._database.get_session = _raising_session
        await cache_manager.increment_retry([1, 2])


class TestOrphanExceptionPaths:
    async def test_get_orphan_records_exception(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        assert len(cache_manager._orphan_sqlite_ids) == 1
        cache_manager._database.get_session = _raising_session
        assert await cache_manager.get_orphan_records() == []

    async def test_clear_orphan_ids_persistence_exception(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        orphan_ids = list(cache_manager._orphan_sqlite_ids)
        cache_manager._database.get_session = _raising_session
        await cache_manager.clear_orphan_ids(orphan_ids)
        assert len(cache_manager._orphan_sqlite_ids) == 0

    async def test_clear_orphan_ids_empty(self, cache_manager):
        await cache_manager.clear_orphan_ids([])


class TestRingBufferIntegration:
    async def test_get_pending_from_ring_buffer_no_rb(self, cache_manager):
        assert await cache_manager.get_pending_from_ring_buffer() == []

    async def test_get_pending_from_ring_buffer_with_rb(self, cache_manager):
        mock_rb = AsyncMock()
        mock_rb.get_pending = AsyncMock(return_value=[{"_id": 1}])
        cache_manager._ring_buffer = mock_rb
        result = await cache_manager.get_pending_from_ring_buffer(limit=10)
        assert result == [{"_id": 1}]
        mock_rb.get_pending.assert_called_once_with(limit=10)

    async def test_get_pending_from_ring_buffer_exception(self, cache_manager):
        mock_rb = AsyncMock()
        mock_rb.get_pending = AsyncMock(side_effect=RuntimeError("fail"))
        cache_manager._ring_buffer = mock_rb
        assert await cache_manager.get_pending_from_ring_buffer() == []

    async def test_mark_synced_no_ring_buffer(self, cache_manager):
        count = await cache_manager.mark_synced([1, 2, 3])
        assert count == 0

    async def test_mark_synced_with_ring_buffer(self, cache_manager):
        mock_rb = AsyncMock()
        mock_rb.mark_synced = AsyncMock(return_value=3)
        cache_manager._ring_buffer = mock_rb
        count = await cache_manager.mark_synced([1, 2, 3])
        assert count == 3

    async def test_mark_synced_with_sqlite_ids(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        records = await cache_manager.get_cached_records()
        mock_rb = AsyncMock()
        mock_rb.mark_synced = AsyncMock(return_value=1)
        cache_manager._ring_buffer = mock_rb
        count = await cache_manager.mark_synced([1], sqlite_ids=[records[0]["id"]])
        assert count == 1
        assert await cache_manager.get_cached_records() == []

    async def test_mark_synced_ring_buffer_exception(self, cache_manager):
        mock_rb = AsyncMock()
        mock_rb.mark_synced = AsyncMock(side_effect=RuntimeError("fail"))
        cache_manager._ring_buffer = mock_rb
        count = await cache_manager.mark_synced([1, 2])
        assert count == 0

    async def test_mark_failed_no_ring_buffer(self, cache_manager):
        assert await cache_manager.mark_failed([1, 2, 3]) == 0

    async def test_mark_failed_with_ring_buffer(self, cache_manager):
        mock_rb = AsyncMock()
        mock_rb.mark_failed = AsyncMock(return_value=2)
        cache_manager._ring_buffer = mock_rb
        assert await cache_manager.mark_failed([1, 2, 3]) == 2

    async def test_mark_failed_exception(self, cache_manager):
        mock_rb = AsyncMock()
        mock_rb.mark_failed = AsyncMock(side_effect=RuntimeError("fail"))
        cache_manager._ring_buffer = mock_rb
        assert await cache_manager.mark_failed([1, 2, 3]) == 0

    def test_get_ring_buffer_stats_no_rb(self, cache_manager):
        assert cache_manager.get_ring_buffer_stats() is None

    def test_get_ring_buffer_stats_with_rb(self, cache_manager):
        mock_rb = MagicMock()
        mock_rb.get_stats = MagicMock(return_value={"size": 5})
        cache_manager._ring_buffer = mock_rb
        assert cache_manager.get_ring_buffer_stats() == {"size": 5}


class TestCalibration:
    async def test_start_and_stop_calibration(self, cache_manager):
        cache_manager._calibration_interval = 0.01
        await cache_manager.start_calibration()
        assert cache_manager._calibration_task is not None
        await asyncio.sleep(0.03)
        await cache_manager.stop_calibration()
        assert cache_manager._calibration_task is None

    async def test_start_calibration_idempotent(self, cache_manager):
        cache_manager._calibration_interval = 0.01
        await cache_manager.start_calibration()
        task1 = cache_manager._calibration_task
        await cache_manager.start_calibration()
        assert cache_manager._calibration_task is task1
        await cache_manager.stop_calibration()

    async def test_calibration_corrects_drift(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        cache_manager._cache_count = 999
        cache_manager._calibration_interval = 0.01
        await cache_manager.start_calibration()
        # Poll until calibration corrects the drift.
        for _ in range(100):
            await asyncio.sleep(0.01)
            if cache_manager._cache_count == 1:
                break
        await cache_manager.stop_calibration()
        assert cache_manager._cache_count == 1

    async def test_calibration_handles_exception(self, cache_manager):
        cache_manager._calibration_interval = 0.01
        cache_manager.get_cache_count = AsyncMock(side_effect=RuntimeError("fail"))
        await cache_manager.start_calibration()
        await asyncio.sleep(0.03)
        await cache_manager.stop_calibration()
        assert cache_manager._calibration_task is None

    async def test_stop_calibration_without_task(self, cache_manager):
        await cache_manager.stop_calibration()
        assert cache_manager._calibration_task is None


class TestOrphanCompaction:
    async def test_start_and_stop(self, cache_manager):
        cache_manager.start_orphan_compaction(interval=0.01)
        assert cache_manager._orphan_compaction_task is not None
        await asyncio.sleep(0.02)
        cache_manager.stop_orphan_compaction()
        assert cache_manager._orphan_compaction_task is None

    async def test_start_idempotent(self, cache_manager):
        cache_manager.start_orphan_compaction(interval=0.01)
        task1 = cache_manager._orphan_compaction_task
        cache_manager.start_orphan_compaction(interval=0.01)
        assert cache_manager._orphan_compaction_task is task1
        cache_manager.stop_orphan_compaction()

    async def test_compaction_reinjects_orphans(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        assert len(cache_manager._orphan_sqlite_ids) == 1
        mock_rb = AsyncMock()
        mock_rb.load_from_records = AsyncMock(return_value=1)
        cache_manager._ring_buffer = mock_rb
        cache_manager.start_orphan_compaction(interval=0.01)
        # Poll until the compaction clears the orphan, then stop and await the task.
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(cache_manager._orphan_sqlite_ids) == 0:
                break
        task = cache_manager._orphan_compaction_task
        cache_manager.stop_orphan_compaction()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(cache_manager._orphan_sqlite_ids) == 0
        mock_rb.load_from_records.assert_called_once()

    async def test_compaction_no_ring_buffer(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        cache_manager.start_orphan_compaction(interval=0.01)
        await asyncio.sleep(0.05)
        task = cache_manager._orphan_compaction_task
        cache_manager.stop_orphan_compaction()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(cache_manager._orphan_sqlite_ids) == 1

    async def test_compaction_partial_load(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        await cache_manager.add_to_cache("m", {}, {"v": 2}, "2")
        assert len(cache_manager._orphan_sqlite_ids) == 2
        mock_rb = AsyncMock()
        # Only the first compaction cycle loads 1 record; subsequent cycles
        # load 0 so exactly 1 orphan is cleared regardless of timing.
        load_calls = {"n": 0}

        async def _load_from_records(_records):
            load_calls["n"] += 1
            return 1 if load_calls["n"] == 1 else 0

        mock_rb.load_from_records = _load_from_records
        cache_manager._ring_buffer = mock_rb
        cache_manager.start_orphan_compaction(interval=0.01)
        # Poll until the first compaction cycle clears exactly 1 orphan.
        # Subsequent cycles load 0 records so no more orphans are cleared.
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(cache_manager._orphan_sqlite_ids) == 1:
                break
        # stop_orphan_compaction cancels but does not await the task; await it
        # explicitly to avoid a lingering task hanging the event loop teardown.
        task = cache_manager._orphan_compaction_task
        cache_manager.stop_orphan_compaction()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(cache_manager._orphan_sqlite_ids) == 1

    async def test_compaction_handles_exception(self, cache_manager):
        cache_manager.get_orphan_records = AsyncMock(side_effect=RuntimeError("fail"))
        cache_manager.start_orphan_compaction(interval=0.01)
        await asyncio.sleep(0.05)
        cache_manager.stop_orphan_compaction()
        assert cache_manager._orphan_compaction_task is None

    async def test_stop_without_task(self, cache_manager):
        cache_manager.stop_orphan_compaction()
        assert cache_manager._orphan_compaction_task is None


class TestCheckWatermarkExtended:
    async def test_pending_count_exception(self, cache_manager):
        cache_manager._cache_count = 10
        cache_manager._database.get_session = _raising_session
        result = await cache_manager.check_watermark()
        assert result["level"] == "normal"
        assert result["pending_count"] == 0
        assert result["cache_count"] == 10

    async def test_critical_with_pending(self, cache_manager):
        await cache_manager.add_to_cache("m", {}, {"v": 1}, "1")
        cache_manager._cache_count = int(MAX_CACHE_SIZE * 0.95)
        result = await cache_manager.check_watermark()
        assert result["level"] == "critical"
        assert result["pending_count"] >= 1

    async def test_zero_max_size(self, cache_manager):
        cache_manager._cache_count = 5
        with patch("edgelite.storage.cache.MAX_CACHE_SIZE", 0):
            result = await cache_manager.check_watermark()
        assert result["usage_pct"] == 0
        assert result["level"] == "normal"


class TestClose:
    async def test_close_stops_all_tasks(self, cache_manager):
        cache_manager._calibration_interval = 0.01
        await cache_manager.start_calibration()
        cache_manager.start_orphan_compaction(interval=0.01)
        assert cache_manager._calibration_task is not None
        assert cache_manager._orphan_compaction_task is not None
        await cache_manager.close()
        assert cache_manager._calibration_task is None
        assert cache_manager._orphan_compaction_task is None

    async def test_close_without_tasks(self, cache_manager):
        await cache_manager.close()
        assert cache_manager._calibration_task is None
        assert cache_manager._orphan_compaction_task is None
