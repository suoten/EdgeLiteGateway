"""sqlite_ts 模块测试 - SQLite 时序存储（InfluxDB 降级方案）

覆盖 storage/sqlite_ts.py 的 SqliteTimeSeriesStorage：
- 生命周期: start/stop/重启恢复同步偏移量/父目录创建
- 写入: write_point（单点，各值类型，标签，默认时间戳，去重替换，阈值批量提交）
- 批量写入: write_points_batch（批量，空列表，None 值，标签，默认字段，异常回滚）
- 查询: query_points（时间范围，limit/offset，默认 stop_ns，缺失设备/点位，无聚合回退）
- 聚合: avg/mean/max/min/sum/count/未知聚合默认 avg，int 值走 COALESCE
- 最新值: query_latest（多点位，空列表，缺失，质量）
- 同步: get_unsynced_count/records，min_id 偏移，mark_synced/delete_synced_records 委托，sync_completed 原子操作+回滚
- 保留/清理: clear_all，cleanup_old_data（分批），delete_by_device_id
- 统计: get_stats（有数据/未启动）
- 备份: backup（创建文件，无文件 noop，备份可读）
- 表结构: 表/索引创建，PRAGMA，UNIQUE 索引创建失败容错
- 错误处理: 表被删除后各方法安全降级
- 损坏恢复: 垃圾文件/逻辑损坏恢复
- 定时刷盘: _periodic_flush 提交挂起写入
- _row_to_dict: 各值类型还原
- 性能: 大数据集与并发写入
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
class TestLifecycle:
    """启动/停止/重启与目录创建"""

    async def test_start_creates_db_file(self, tmp_path):
        """start 应创建数据库文件并初始化表"""
        db_path = str(tmp_path / "ts.db")
        s = SqliteTimeSeriesStorage(db_path)
        await s.start()
        assert Path(db_path).exists()
        assert s._db is not None
        await s.stop()
        assert s._db is None

    async def test_start_creates_parent_dirs(self, tmp_path):
        """start 应自动创建不存在的父目录"""
        db_path = str(tmp_path / "a" / "b" / "ts.db")
        s = SqliteTimeSeriesStorage(db_path)
        await s.start()
        assert Path(db_path).exists()
        await s.stop()

    async def test_stop_before_start_noop(self, tmp_path):
        """未启动时 stop 不应抛异常"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.stop()  # 不应抛异常

    async def test_stop_commits_pending_and_persists(self, tmp_path):
        """stop 应提交挂起写入，数据持久化后可被新实例读取"""
        db_path = str(tmp_path / "ts.db")
        s = SqliteTimeSeriesStorage(db_path)
        await s.start()
        await s.write_point("m", "dev1", "p1", 42.0, timestamp_ns=1_000_000_000)
        await s.write_point("m", "dev1", "p1", 43.0, timestamp_ns=2_000_000_000)
        assert s._pending_writes == 2
        await s.stop()

        s2 = SqliteTimeSeriesStorage(db_path)
        await s2.start()
        pts = await s2.query_points("dev1", "p1", 0, 3_000_000_000)
        assert len(pts) == 2
        await s2.stop()

    async def test_restart_restores_offset_from_meta(self, tmp_path):
        """重启后应从 _meta 表恢复 sync_offset"""
        db_path = str(tmp_path / "ts.db")
        s = SqliteTimeSeriesStorage(db_path)
        await s.start()
        for i in range(5):
            await s.write_point("m", "dev1", "p1", float(i), timestamp_ns=1_000_000_000 + i)
        await s.sync_completed(3)  # offset=3，_meta 记录 3
        await s.stop()

        s2 = SqliteTimeSeriesStorage(db_path)
        await s2.start()
        assert s2._sync_offset == 3
        assert await s2.get_unsynced_count() == 2  # id 4, 5
        await s2.stop()

    async def test_restart_offset_fallback_to_max_id(self, tmp_path):
        """_meta 无记录时应 fallback 到 MAX(id)"""
        db_path = str(tmp_path / "ts.db")
        s = SqliteTimeSeriesStorage(db_path)
        await s.start()
        for i in range(5):
            await s.write_point("m", "dev1", "p1", float(i), timestamp_ns=1_000_000_000 + i)
        await s.sync_completed(3)  # 删除 id 1-3，剩余 4,5
        # 删除 _meta 记录强制 fallback
        await s._db.execute("DELETE FROM _meta WHERE key='sync_offset'")
        await s._db.commit()
        await s.stop()

        s2 = SqliteTimeSeriesStorage(db_path)
        await s2.start()
        # fallback 到 MAX(id) = 5
        assert s2._sync_offset == 5
        assert await s2.get_unsynced_count() == 0
        await s2.stop()

    async def test_restart_offset_empty_db(self, tmp_path):
        """空库重启时 offset 应为 0"""
        db_path = str(tmp_path / "ts.db")
        s = SqliteTimeSeriesStorage(db_path)
        await s.start()
        await s.stop()
        s2 = SqliteTimeSeriesStorage(db_path)
        await s2.start()
        assert s2._sync_offset == 0
        await s2.stop()


# --------------------------------------------------------------------------- #
# write_point
# --------------------------------------------------------------------------- #
class TestWritePoint:
    """单点写入与各值类型"""

    @pytest.fixture
    async def storage(self, tmp_path):
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        yield s
        await s.stop()

    async def test_bool_value(self, storage):
        """bool 值写入 value_bool 列"""
        await storage.write_point("m", "dev1", "b", True, timestamp_ns=1_000_000_000)
        await storage.write_point("m", "dev1", "b", False, timestamp_ns=2_000_000_000)
        pts = await storage.query_points("dev1", "b", 0, 3_000_000_000)
        assert pts[0]["value"] is True
        assert pts[1]["value"] is False

    async def test_int_value(self, storage):
        """int 值写入 value_int 列"""
        await storage.write_point("m", "dev1", "i", 42, timestamp_ns=1_000_000_000)
        pts = await storage.query_points("dev1", "i", 0, 2_000_000_000)
        assert pts[0]["value"] == 42

    async def test_float_value(self, storage):
        """float 值写入 value_real 列"""
        await storage.write_point("m", "dev1", "f", 3.14, timestamp_ns=1_000_000_000)
        pts = await storage.query_points("dev1", "f", 0, 2_000_000_000)
        assert pts[0]["value"] == 3.14

    async def test_str_value(self, storage):
        """str 值写入 value_str 列"""
        await storage.write_point("m", "dev1", "s", "hello", timestamp_ns=1_000_000_000)
        pts = await storage.query_points("dev1", "s", 0, 2_000_000_000)
        assert pts[0]["value"] == "hello"

    async def test_none_value_writes_str_none(self, storage):
        """None 走 else 分支：value_str = str(None) == 'None'"""
        await storage.write_point("m", "dev1", "n", None, timestamp_ns=1_000_000_000)
        pts = await storage.query_points("dev1", "n", 0, 2_000_000_000)
        assert pts[0]["value"] == "None"

    async def test_tags_serialized(self, storage):
        """tags 字典应序列化为 JSON 并可还原"""
        await storage.write_point(
            "m", "dev1", "t", 1.0, tags={"unit": "C"}, timestamp_ns=1_000_000_000
        )
        recs = await storage.get_unsynced_records(limit=10)
        assert recs[0]["tags"] == {"unit": "C"}

    async def test_default_timestamp_ns(self, storage):
        """未提供 timestamp_ns 时使用 time.time_ns()"""
        before = time.time_ns()
        await storage.write_point("m", "dev1", "d", 1.0)
        after = time.time_ns()
        recs = await storage.get_unsynced_records(limit=10)
        assert before <= recs[0]["timestamp_ns"] <= after

    async def test_quality_field(self, storage):
        """quality 字段应被持久化与返回"""
        await storage.write_point("m", "dev1", "q", 1.0, quality="bad", timestamp_ns=1_000_000_000)
        pts = await storage.query_points("dev1", "q", 0, 2_000_000_000)
        assert pts[0]["quality"] == "bad"

    async def test_replace_on_duplicate(self, storage):
        """相同 (device_id, point_name, timestamp_ns) 应 INSERT OR REPLACE"""
        await storage.write_point("m", "dev1", "r", 1.0, timestamp_ns=1_000_000_000)
        await storage.write_point("m", "dev1", "r", 2.0, timestamp_ns=1_000_000_000)
        pts = await storage.query_points("dev1", "r", 0, 2_000_000_000)
        assert len(pts) == 1
        assert pts[0]["value"] == 2.0

    async def test_threshold_batch_commit(self, tmp_path):
        """达到 _max_pending 阈值应立即提交并重置挂起计数"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        s._max_pending = 3
        await s.start()
        for i in range(3):
            await s.write_point("m", "dev1", "p", float(i), timestamp_ns=1_000_000_000 + i)
        assert s._pending_writes == 0
        assert s._write_count == 3
        await s.stop()

    async def test_write_before_start_noop(self, tmp_path):
        """未启动时 write_point 应静默返回"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.write_point("m", "dev1", "p", 1.0)  # 不应抛异常


# --------------------------------------------------------------------------- #
# write_points_batch
# --------------------------------------------------------------------------- #
class TestWritePointsBatch:
    """批量写入"""

    @pytest.fixture
    async def storage(self, tmp_path):
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        yield s
        await s.stop()

    async def test_batch_write(self, storage):
        """批量写入 10 条并校验计数与查询结果"""
        points = [
            {
                "measurement": "m",
                "device_id": "dev1",
                "point_name": "p",
                "value": float(i),
                "timestamp_ns": 1_000_000_000 + i,
            }
            for i in range(10)
        ]
        await storage.write_points_batch(points)
        assert storage._write_count == 10
        assert storage._pending_writes == 0
        pts = await storage.query_points("dev1", "p", 0, 2_000_000_000, limit=100)
        assert len(pts) == 10

    async def test_empty_batch_noop(self, storage):
        """空列表应直接返回"""
        await storage.write_points_batch([])
        assert storage._write_count == 0

    async def test_none_value_in_batch(self, storage):
        """批量中 value=None → value_str=None，查询返回 None"""
        points = [
            {
                "measurement": "m",
                "device_id": "dev1",
                "point_name": "p",
                "value": None,
                "timestamp_ns": 1_000_000_000,
            }
        ]
        await storage.write_points_batch(points)
        recs = await storage.get_unsynced_records(limit=10)
        assert recs[0]["value"] is None
        assert recs[0]["tags"] is None

    async def test_batch_tags_and_types(self, storage):
        """批量写入 bool/int/str 各类型及 tags"""
        points = [
            {
                "measurement": "m",
                "device_id": "dev1",
                "point_name": "b",
                "value": True,
                "tags": {"k": "v"},
                "timestamp_ns": 1_000_000_000,
            },
            {
                "measurement": "m",
                "device_id": "dev1",
                "point_name": "i",
                "value": 7,
                "timestamp_ns": 1_000_000_000,
            },
            {
                "measurement": "m",
                "device_id": "dev1",
                "point_name": "s",
                "value": "txt",
                "timestamp_ns": 1_000_000_000,
            },
        ]
        await storage.write_points_batch(points)
        assert await storage.get_unsynced_count() == 3
        recs = await storage.get_unsynced_records(limit=10)
        by_pn = {r["point_name"]: r for r in recs}
        assert by_pn["b"]["value"] is True
        assert by_pn["b"]["tags"] == {"k": "v"}
        assert by_pn["i"]["value"] == 7
        assert by_pn["s"]["value"] == "txt"

    async def test_batch_default_fields(self, storage):
        """缺少 measurement/device_id/point_name/quality 时使用默认值"""
        points = [{"value": 1.0, "timestamp_ns": 1_000_000_000}]
        await storage.write_points_batch(points)
        recs = await storage.get_unsynced_records(limit=10)
        assert recs[0]["measurement"] == "device_points"
        assert recs[0]["device_id"] == ""
        assert recs[0]["point_name"] == ""
        assert recs[0]["quality"] == "good"

    async def test_batch_default_timestamp(self, storage):
        """批量中缺少 timestamp_ns 时使用 time.time_ns()"""
        before = time.time_ns()
        await storage.write_points_batch([{"measurement": "m", "device_id": "d", "point_name": "p", "value": 1.0}])
        after = time.time_ns()
        recs = await storage.get_unsynced_records(limit=10)
        assert before <= recs[0]["timestamp_ns"] <= after

    async def test_batch_exception_rollback(self, tmp_path):
        """表不存在时批量写入应回滚并重新抛出异常"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s._db.execute("DROP TABLE device_points")
        await s._db.commit()
        with pytest.raises(sqlite3.Error):
            await s.write_points_batch([{"value": 1.0, "timestamp_ns": 1_000_000_000}])
        await s.stop()

    async def test_batch_before_start_noop(self, tmp_path):
        """未启动时批量写入应静默返回"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.write_points_batch([{"value": 1.0}])  # 不应抛异常


# --------------------------------------------------------------------------- #
# query_points
# --------------------------------------------------------------------------- #
class TestQueryPoints:
    """时间范围/分页查询"""

    @pytest.fixture
    async def storage(self, tmp_path):
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        for i in range(10):
            await s.write_point("m", "dev1", "p1", float(i), timestamp_ns=1_000_000_000 * (i + 1))
        yield s
        await s.stop()

    async def test_time_range_inclusive(self, storage):
        """BETWEEN 应包含边界"""
        pts = await storage.query_points("dev1", "p1", 2_000_000_000, 5_000_000_000)
        assert len(pts) == 4  # ts 2,3,4,5
        assert [p["value"] for p in pts] == [1.0, 2.0, 3.0, 4.0]

    async def test_limit_offset(self, storage):
        """limit + offset 分页"""
        pts = await storage.query_points("dev1", "p1", 0, 100_000_000_000, limit=3, offset=2)
        assert len(pts) == 3
        assert pts[0]["value"] == 2.0  # 第 3 条（索引 2）

    async def test_default_stop_ns(self, storage):
        """stop_ns=None 时使用 time.time_ns()"""
        pts = await storage.query_points("dev1", "p1", 0)
        assert len(pts) == 10

    async def test_nonexistent_point(self, storage):
        """点位不存在返回空"""
        assert await storage.query_points("dev1", "nope", 0, 100_000_000_000) == []

    async def test_nonexistent_device(self, storage):
        """设备不存在返回空"""
        assert await storage.query_points("devX", "p1", 0, 100_000_000_000) == []

    async def test_ordered_ascending(self, storage):
        """结果按 timestamp_ns 升序"""
        pts = await storage.query_points("dev1", "p1", 0, 100_000_000_000)
        vals = [p["value"] for p in pts]
        assert vals == sorted(vals)

    async def test_aggregate_without_window_returns_raw(self, storage):
        """提供 aggregate 但无 window_seconds 时返回原始点"""
        pts = await storage.query_points("dev1", "p1", 0, 100_000_000_000, aggregate="avg")
        assert len(pts) == 10

    async def test_window_without_aggregate_returns_raw(self, storage):
        """提供 window_seconds 但无 aggregate 时返回原始点"""
        pts = await storage.query_points("dev1", "p1", 0, 100_000_000_000, window_seconds=5)
        assert len(pts) == 10

    async def test_query_before_start_returns_empty(self, tmp_path):
        """未启动时查询返回空"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        assert await s.query_points("d", "p", 0, 1) == []


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #
class TestAggregation:
    """聚合查询 avg/mean/max/min/sum/count"""

    @pytest.fixture
    async def storage(self, tmp_path):
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        # window1（ts=1s,1.5s）值 10,20；window2（ts=2.5s）值 30
        await s.write_point("m", "dev1", "p", 10.0, timestamp_ns=1_000_000_000)
        await s.write_point("m", "dev1", "p", 20.0, timestamp_ns=1_500_000_000)
        await s.write_point("m", "dev1", "p", 30.0, timestamp_ns=2_500_000_000)
        yield s
        await s.stop()

    async def _agg(self, storage, fn):
        return await storage.query_points(
            "dev1", "p", 0, 3_000_000_000, aggregate=fn, window_seconds=1
        )

    async def test_avg(self, storage):
        """avg 聚合"""
        r = await self._agg(storage, "avg")
        assert len(r) == 2
        assert r[0]["value"] == 15.0
        assert r[1]["value"] == 30.0

    async def test_mean_alias(self, storage):
        """mean 是 avg 的别名"""
        r = await self._agg(storage, "mean")
        assert r[0]["value"] == 15.0
        assert r[1]["value"] == 30.0

    async def test_sum(self, storage):
        """sum 聚合"""
        r = await self._agg(storage, "sum")
        assert r[0]["value"] == 30.0
        assert r[1]["value"] == 30.0

    async def test_min(self, storage):
        """min 聚合"""
        r = await self._agg(storage, "min")
        assert r[0]["value"] == 10.0
        assert r[1]["value"] == 30.0

    async def test_max(self, storage):
        """max 聚合"""
        r = await self._agg(storage, "max")
        assert r[0]["value"] == 20.0
        assert r[1]["value"] == 30.0

    async def test_count(self, storage):
        """count 聚合"""
        r = await self._agg(storage, "count")
        assert r[0]["value"] == 2
        assert r[1]["value"] == 1

    async def test_unknown_aggregate_defaults_to_avg(self, storage):
        """未知聚合函数应回退到 AVG"""
        r = await self._agg(storage, "foobar")
        assert r[0]["value"] == 15.0

    async def test_aggregate_result_quality_good(self, storage):
        """聚合结果 quality 固定为 'good'"""
        r = await self._agg(storage, "avg")
        assert all(x["quality"] == "good" for x in r)

    async def test_aggregate_has_time(self, storage):
        """聚合结果包含 time 字段"""
        r = await self._agg(storage, "avg")
        assert all("time" in x for x in r)

    async def test_aggregate_int_values_coalesce(self, tmp_path):
        """int 值走 COALESCE(value_real, value_int) 参与聚合"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s.write_point("m", "dev1", "p", 10, timestamp_ns=1_000_000_000)
        await s.write_point("m", "dev1", "p", 20, timestamp_ns=1_500_000_000)
        r = await s.query_points("dev1", "p", 0, 3_000_000_000, aggregate="avg", window_seconds=1)
        assert r[0]["value"] == 15.0
        await s.stop()

    async def test_aggregate_excludes_null_values(self, tmp_path):
        """value_real/value_int 均为 NULL 的记录不参与聚合"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s.write_point("m", "dev1", "p", 10.0, timestamp_ns=1_000_000_000)
        # value=None 走 str，value_real/value_int 均为 NULL → 排除
        await s.write_point("m", "dev1", "p", None, timestamp_ns=1_500_000_000)
        r = await s.query_points("dev1", "p", 0, 3_000_000_000, aggregate="count", window_seconds=1)
        assert r[0]["value"] == 1
        await s.stop()


# --------------------------------------------------------------------------- #
# query_latest
# --------------------------------------------------------------------------- #
class TestQueryLatest:
    """最新值查询"""

    @pytest.fixture
    async def storage(self, tmp_path):
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s.write_point("m", "dev1", "p1", 1.0, timestamp_ns=1_000_000_000)
        await s.write_point("m", "dev1", "p1", 2.0, timestamp_ns=2_000_000_000)
        await s.write_point("m", "dev1", "p2", 10.0, timestamp_ns=1_500_000_000)
        await s.write_point("m", "dev1", "p2", 20.0, timestamp_ns=2_500_000_000)
        yield s
        await s.stop()

    async def test_latest_multiple_points(self, storage):
        """多点位各自返回最新值"""
        r = await storage.query_latest("dev1", ["p1", "p2"])
        assert r["p1"]["value"] == 2.0
        assert r["p2"]["value"] == 20.0

    async def test_latest_single_point(self, storage):
        """单点位最新值"""
        r = await storage.query_latest("dev1", ["p1"])
        assert r["p1"]["value"] == 2.0

    async def test_latest_empty_points(self, storage):
        """空 point_names 返回 {}"""
        assert await storage.query_latest("dev1", []) == {}

    async def test_latest_nonexistent_point(self, storage):
        """点位不存在不在结果中"""
        r = await storage.query_latest("dev1", ["nope"])
        assert "nope" not in r

    async def test_latest_nonexistent_device(self, storage):
        """设备不存在返回 {}"""
        assert await storage.query_latest("devX", ["p1"]) == {}

    async def test_latest_quality(self, storage):
        """最新值带 quality"""
        await storage.write_point("m", "dev1", "p3", 5.0, quality="bad", timestamp_ns=3_000_000_000)
        r = await storage.query_latest("dev1", ["p3"])
        assert r["p3"]["quality"] == "bad"

    async def test_latest_value_type_priority(self, storage):
        """int/str 最新值正确还原"""
        await storage.write_point("m", "dev1", "iv", 99, timestamp_ns=3_000_000_000)
        await storage.write_point("m", "dev1", "sv", "abc", timestamp_ns=3_000_000_000)
        r = await storage.query_latest("dev1", ["iv", "sv"])
        assert r["iv"]["value"] == 99
        assert r["sv"]["value"] == "abc"

    async def test_latest_before_start_returns_empty(self, tmp_path):
        """未启动时返回 {}"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        assert await s.query_latest("d", ["p"]) == {}


# --------------------------------------------------------------------------- #
# 同步
# --------------------------------------------------------------------------- #
class TestSync:
    """未同步计数/记录，原子同步，委托方法"""

    @pytest.fixture
    async def storage(self, tmp_path):
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        for i in range(5):
            await s.write_point("m", "dev1", "p1", float(i), timestamp_ns=1_000_000_000 + i)
        yield s
        await s.stop()

    async def test_unsynced_count(self, storage):
        """未同步计数 == 总记录数"""
        assert await storage.get_unsynced_count() == 5

    async def test_unsynced_records_ordered(self, storage):
        """未同步记录按 id 升序"""
        recs = await storage.get_unsynced_records(limit=10)
        assert len(recs) == 5
        assert [r["id"] for r in recs] == [1, 2, 3, 4, 5]
        assert recs[0]["value"] == 0.0
        assert recs[0]["measurement"] == "m"

    async def test_unsynced_records_min_id(self, storage):
        """min_id 偏移跳过已上传记录"""
        recs = await storage.get_unsynced_records(limit=10, min_id=3)
        assert len(recs) == 2
        assert {r["id"] for r in recs} == {4, 5}

    async def test_unsynced_records_limit(self, storage):
        """limit 限制返回数量"""
        recs = await storage.get_unsynced_records(limit=2)
        assert len(recs) == 2
        assert [r["id"] for r in recs] == [1, 2]

    async def test_sync_completed_atomic(self, storage):
        """sync_completed 原子更新 offset 并删除记录"""
        ok = await storage.sync_completed(3)
        assert ok is True
        assert storage._sync_offset == 3
        assert await storage.get_unsynced_count() == 2
        recs = await storage.get_unsynced_records(limit=10)
        assert {r["id"] for r in recs} == {4, 5}

    async def test_sync_completed_no_records_deleted(self, storage):
        """sync_completed 对空范围应成功（无记录删除）"""
        await storage.sync_completed(5)  # 全部删除
        ok = await storage.sync_completed(5)  # 再次调用，无记录删除
        assert ok is True
        assert storage._sync_offset == 5

    async def test_mark_synced_delegates(self, storage):
        """mark_synced 委托给 sync_completed"""
        await storage.mark_synced(2)
        assert storage._sync_offset == 2
        assert await storage.get_unsynced_count() == 3

    async def test_delete_synced_records_delegates(self, storage):
        """delete_synced_records 委托给 sync_completed，返回 1/0"""
        n = await storage.delete_synced_records(1)
        assert n == 1
        assert storage._sync_offset == 1
        assert await storage.get_unsynced_count() == 4

    async def test_sync_completed_before_start_false(self, tmp_path):
        """未启动时 sync_completed 返回 False"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        assert await s.sync_completed(1) is False

    async def test_sync_completed_rollback_returns_false(self, tmp_path):
        """事务内失败应回滚并返回 False"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s.write_point("m", "dev1", "p", 1.0, timestamp_ns=1_000_000_000)
        # 删除 _meta 表迫使事务内 INSERT 失败
        await s._db.execute("DROP TABLE _meta")
        await s._db.commit()
        assert await s.sync_completed(1) is False
        await s.stop()

    async def test_get_unsynced_before_start_zero(self, tmp_path):
        """未启动时计数/记录为 0/空"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        assert await s.get_unsynced_count() == 0
        assert await s.get_unsynced_records() == []


# --------------------------------------------------------------------------- #
# 保留/清理
# --------------------------------------------------------------------------- #
class TestRetention:
    """清空/保留期清理/设备删除"""

    @pytest.fixture
    async def storage(self, tmp_path):
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        yield s
        await s.stop()

    async def test_clear_all(self, storage):
        """clear_all 删除全部并重置 offset"""
        for i in range(5):
            await storage.write_point("m", "dev1", "p", float(i), timestamp_ns=1_000_000_000 * (i + 1))
        n = await storage.clear_all()
        assert n == 5
        assert storage._sync_offset == 0
        assert (await storage.get_stats())["total_records"] == 0

    async def test_clear_all_empty(self, storage):
        """空库 clear_all 返回 0"""
        assert await storage.clear_all() == 0

    async def test_delete_by_device_id(self, storage):
        """按设备删除时序数据"""
        await storage.write_point("m", "dev1", "p", 1.0, timestamp_ns=1_000_000_000)
        await storage.write_point("m", "dev2", "p", 2.0, timestamp_ns=1_000_000_000)
        n = await storage.delete_by_device_id("dev1")
        assert n == 1
        assert await storage.get_unsynced_count() == 1
        r = await storage.query_latest("dev2", ["p"])
        assert r["p"]["value"] == 2.0

    async def test_delete_by_device_id_nonexistent(self, storage):
        """删除不存在的设备返回 0"""
        assert await storage.delete_by_device_id("nope") == 0

    async def test_cleanup_old_data(self, storage):
        """清理过期数据，保留近期数据"""
        now = time.time()
        old_ns = int((now - 60 * 86400) * 1e9)  # 60 天前
        recent_ns = int((now - 1) * 1e9)  # 1 秒前
        await storage.write_point("m", "dev1", "p", 1.0, timestamp_ns=old_ns)
        await storage.write_point("m", "dev1", "p", 2.0, timestamp_ns=recent_ns)
        n = await storage.cleanup_old_data(retention_days=30)
        assert n == 1
        assert await storage.get_unsynced_count() == 1

    async def test_cleanup_old_data_batched(self, storage):
        """1200 条过期数据分批（1000/批）删除"""
        old_ns = int((time.time() - 60 * 86400) * 1e9)
        points = [
            {
                "measurement": "m",
                "device_id": "dev1",
                "point_name": "p",
                "value": float(i),
                "timestamp_ns": old_ns + i,
            }
            for i in range(1200)
        ]
        await storage.write_points_batch(points)
        n = await storage.cleanup_old_data(retention_days=30)
        assert n == 1200
        assert await storage.get_unsynced_count() == 0

    async def test_cleanup_keeps_recent(self, storage):
        """清理后近期数据仍可查询"""
        now = time.time()
        old_ns = int((now - 60 * 86400) * 1e9)
        recent_ns = int(now * 1e9)
        await storage.write_point("m", "dev1", "p", 1.0, timestamp_ns=old_ns)
        await storage.write_point("m", "dev1", "p", 2.0, timestamp_ns=recent_ns)
        await storage.cleanup_old_data(retention_days=30)
        pts = await storage.query_points("dev1", "p", 0, recent_ns + 1_000_000_000)
        assert len(pts) == 1
        assert pts[0]["value"] == 2.0

    async def test_cleanup_nothing_expired(self, storage):
        """无过期数据时返回 0"""
        await storage.write_point("m", "dev1", "p", 1.0, timestamp_ns=time.time_ns())
        assert await storage.cleanup_old_data(retention_days=30) == 0

    async def test_retention_before_start_zero(self, tmp_path):
        """未启动时清理方法返回 0"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        assert await s.clear_all() == 0
        assert await s.cleanup_old_data(30) == 0
        assert await s.delete_by_device_id("d") == 0


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
class TestStats:
    """存储统计"""

    async def test_stats_with_data(self, tmp_path):
        """有数据时返回正确统计"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s.write_point("m", "dev1", "p", 1.0, timestamp_ns=1_000_000_000)
        await s.write_point("m", "dev1", "p", 2.0, timestamp_ns=2_000_000_000)
        stats = await s.get_stats()
        assert stats["total_records"] == 2
        assert stats["db_size_bytes"] > 0
        assert stats["oldest_record"] is not None
        assert stats["newest_record"] is not None
        assert stats["unsynced_count"] == 2
        await s.stop()

    async def test_stats_empty_db(self, tmp_path):
        """空库时 oldest/newest 为 None"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        stats = await s.get_stats()
        assert stats["total_records"] == 0
        assert stats["oldest_record"] is None
        assert stats["newest_record"] is None
        await s.stop()

    async def test_stats_before_start_zeros(self, tmp_path):
        """未启动时返回全零统计"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        stats = await s.get_stats()
        assert stats == {
            "total_records": 0,
            "db_size_bytes": 0,
            "oldest_record": None,
            "newest_record": None,
            "unsynced_count": 0,
        }


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
class TestBackup:
    """数据库备份"""

    async def test_backup_creates_file(self, tmp_path):
        """backup 应在备份目录生成备份文件"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s.write_point("m", "dev1", "p", 1.0, timestamp_ns=1_000_000_000)
        backup_dir = str(tmp_path / "backups")
        await s.backup(backup_dir)
        backups = list(Path(backup_dir).glob("*.backup.*"))
        assert len(backups) == 1
        await s.stop()

    async def test_backup_file_is_valid_db(self, tmp_path):
        """备份文件应为可读的 SQLite 库且包含数据"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s.write_point("m", "dev1", "p", 42.0, timestamp_ns=1_000_000_000)
        # 显式提交挂起写入，确保数据落盘后再备份（避免 WAL checkpoint 锁冲突）
        async with s._db_lock:
            await s._db.commit()
            s._pending_writes = 0
        backup_dir = str(tmp_path / "backups")
        await s.backup(backup_dir)
        await s.stop()
        backups = list(Path(backup_dir).glob("*.backup.*"))
        conn = sqlite3.connect(str(backups[0]))
        cur = conn.execute("SELECT COUNT(*) FROM device_points")
        assert cur.fetchone()[0] == 1
        conn.close()

    async def test_backup_noop_when_no_file(self, tmp_path):
        """db 文件不存在时 backup 直接返回"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.backup(str(tmp_path / "backups"))
        assert not (tmp_path / "backups").exists()

    async def test_backup_default_dir(self, tmp_path, monkeypatch):
        """使用默认备份目录（cwd 下）"""
        # 切换 cwd 到 tmp_path，使默认 data/backups 落在临时目录
        monkeypatch.chdir(tmp_path)
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        await s.write_point("m", "dev1", "p", 1.0, timestamp_ns=1_000_000_000)
        await s.backup()  # 默认 data/backups
        backups = list(Path("data/backups").glob("*.backup.*"))
        assert len(backups) == 1
        await s.stop()


# --------------------------------------------------------------------------- #
# 表结构
# --------------------------------------------------------------------------- #
class TestSchema:
    """表/索引创建与 PRAGMA"""

    async def test_tables_created(self, tmp_path):
        """start 后应创建 device_points 与 _meta 表"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        cur = await s._db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in await cur.fetchall()}
        assert "device_points" in tables
        assert "_meta" in tables
        await s.stop()

    async def test_indexes_created(self, tmp_path):
        """start 后应创建所有索引"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        cur = await s._db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {r[0] for r in await cur.fetchall()}
        assert "idx_device_points_unique" in indexes
        assert "idx_device_point_ts" in indexes
        assert "idx_timestamp_ns" in indexes
        assert "idx_id" in indexes
        await s.stop()

    async def test_pragmas_set(self, tmp_path):
        """start 后应配置 WAL 与 busy_timeout"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        cur = await s._db.execute("PRAGMA journal_mode")
        assert (await cur.fetchone())[0].lower() == "wal"
        cur = await s._db.execute("PRAGMA busy_timeout")
        assert (await cur.fetchone())[0] == 5000
        cur = await s._db.execute("PRAGMA synchronous")
        assert (await cur.fetchone())[0] == 1  # NORMAL
        await s.stop()

    async def test_unique_index_creation_failure_handled(self, tmp_path):
        """已有重复数据时 UNIQUE 索引创建失败应被容错，start 不应抛异常"""
        db_path = str(tmp_path / "ts.db")
        # 预建无 UNIQUE 约束的表并插入重复行
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE device_points (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                measurement TEXT, device_id TEXT, point_name TEXT, quality TEXT,
                value_real REAL, value_int INTEGER, value_str TEXT, value_bool INTEGER,
                tags_json TEXT, timestamp_ns INTEGER, created_at REAL
            )"""
        )
        conn.execute(
            "INSERT INTO device_points (measurement,device_id,point_name,quality,value_real,timestamp_ns,created_at) "
            "VALUES ('m','d','p','good',1.0,1000,1.0)"
        )
        conn.execute(
            "INSERT INTO device_points (measurement,device_id,point_name,quality,value_real,timestamp_ns,created_at) "
            "VALUES ('m','d','p','good',2.0,1000,1.0)"
        )
        conn.commit()
        conn.close()

        s = SqliteTimeSeriesStorage(db_path)
        await s.start()  # 不应抛异常
        # 重复数据仍在（UNIQUE 索引未创建）
        cur = await s._db.execute("SELECT COUNT(*) FROM device_points")
        assert (await cur.fetchone())[0] == 2
        await s.stop()


# --------------------------------------------------------------------------- #
# 错误处理（表被删除后安全降级）
# --------------------------------------------------------------------------- #
class TestErrorHandling:
    """表被删除后各方法应安全降级"""

    @pytest.fixture
    async def storage(self, tmp_path):
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        # 删除 device_points 表迫使查询/变更方法进入异常分支
        await s._db.execute("DROP TABLE device_points")
        await s._db.commit()
        yield s
        await s.stop()

    async def test_query_points_returns_empty(self, storage):
        assert await storage.query_points("d", "p", 0, 1) == []

    async def test_query_aggregate_returns_empty(self, storage):
        assert await storage.query_points("d", "p", 0, 1, aggregate="avg", window_seconds=1) == []

    async def test_query_latest_returns_empty(self, storage):
        assert await storage.query_latest("d", ["p"]) == {}

    async def test_unsynced_count_zero(self, storage):
        assert await storage.get_unsynced_count() == 0

    async def test_unsynced_records_empty(self, storage):
        assert await storage.get_unsynced_records(limit=10) == []

    async def test_clear_all_zero(self, storage):
        assert await storage.clear_all() == 0

    async def test_cleanup_old_data_zero(self, storage):
        assert await storage.cleanup_old_data(30) == 0

    async def test_delete_by_device_zero(self, storage):
        assert await storage.delete_by_device_id("d") == 0

    async def test_get_stats_zeros(self, storage):
        stats = await storage.get_stats()
        assert stats["total_records"] == 0
        assert stats["unsynced_count"] == 0

    async def test_write_point_raises(self, storage):
        """write_point 异常应重新抛出"""
        with pytest.raises(sqlite3.Error):
            await storage.write_point("m", "d", "p", 1.0, timestamp_ns=1)


# --------------------------------------------------------------------------- #
# 损坏恢复
# --------------------------------------------------------------------------- #
class TestCorruptDb:
    """损坏数据库的检测与恢复"""

    async def test_garbage_file_start_recovers(self, tmp_path):
        """垃圾文件：完整性预检抛异常 → 备份损坏文件 → 重建空库（自动恢复，不抛异常）"""
        db_path = str(tmp_path / "ts.db")
        Path(db_path).write_bytes(b"this is not a sqlite database file at all")
        s = SqliteTimeSeriesStorage(db_path)
        # FIXED-P2: 损坏文件应自动备份+重建，而非抛异常导致服务无法启动
        await s.start()
        stats = await s.get_stats()
        assert stats["total_records"] == 0
        # 垃圾文件已备份为 .corrupt.*
        corrupt_files = list(tmp_path.glob("*.corrupt.*"))
        assert len(corrupt_files) >= 1
        await s.stop()

    async def test_corrupt_recovers_to_fresh_db(self, tmp_path):
        """逻辑损坏（integrity_check 返回非 'ok'）应备份并重建空库"""
        db_path = str(tmp_path / "ts_recover.db")
        # 建一个含数据与索引的有效库
        s = SqliteTimeSeriesStorage(db_path)
        await s.start()
        for i in range(200):
            await s.write_point("m", "dev1", "p", float(i), timestamp_ns=1_000_000_000 + i)
        await s.stop()

        # 通过覆写 B-tree 页字节制造真实物理损坏（writable_schema 方式对索引页不一定触发报错）

        with open(db_path, "rb") as f:
            data = bytearray(f.read())
        # 覆写文件中段的若干字节（避开 100 字节 SQLite 头部，破坏根页内容）
        if len(data) > 4096:
            for off in range(2048, min(4096, len(data) - 16), 4):
                data[off : off + 4] = b"\xff\xee\xdd\xcc"
        with open(db_path, "wb") as f:
            f.write(data)
        # 确认完整性检查返回非 'ok'（严重损坏时 integrity_check 本身可能抛 DatabaseError）
        conn = sqlite3.connect(db_path)
        try:
            ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
            conn.close()
            assert ic != "ok"
        except sqlite3.DatabaseError:
            conn.close()

        # 重新 start 应检测到损坏 → 备份 → 重建空库
        s2 = SqliteTimeSeriesStorage(db_path)
        await s2.start()
        stats = await s2.get_stats()
        assert stats["total_records"] == 0
        # 损坏文件已备份
        corrupt_files = list(tmp_path.glob("*.corrupt.*"))
        assert len(corrupt_files) >= 1
        await s2.stop()


# --------------------------------------------------------------------------- #
# 定时刷盘
# --------------------------------------------------------------------------- #
class TestPeriodicFlush:
    """_periodic_flush 定时提交挂起写入"""

    async def test_periodic_flush_commits_pending(self, tmp_path):
        """挂起写入应在 flush_interval 后被定时提交"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        s._flush_interval = 0.05
        await s.start()
        await s.write_point("m", "dev1", "p", 1.0, timestamp_ns=1_000_000_000)
        assert s._pending_writes == 1
        await asyncio.sleep(0.25)
        assert s._pending_writes == 0
        await s.stop()

    async def test_periodic_flush_no_pending_noop(self, tmp_path):
        """无挂起写入时定时刷盘不应出错"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        s._flush_interval = 0.05
        await s.start()
        await asyncio.sleep(0.15)
        assert s._pending_writes == 0
        await s.stop()


# --------------------------------------------------------------------------- #
# _row_to_dict 静态方法
# --------------------------------------------------------------------------- #
class TestRowToDict:
    """查询行 → dict 的值类型还原"""

    def test_float_value(self):
        d = SqliteTimeSeriesStorage._row_to_dict([1_000_000_000, 25.5, None, None, None, "good"])
        assert d["value"] == 25.5
        assert d["quality"] == "good"
        assert d["time"] is not None

    def test_int_value(self):
        d = SqliteTimeSeriesStorage._row_to_dict([1_000_000_000, None, 42, None, None, "good"])
        assert d["value"] == 42

    def test_str_value(self):
        d = SqliteTimeSeriesStorage._row_to_dict([1_000_000_000, None, None, "abc", None, "good"])
        assert d["value"] == "abc"

    def test_bool_true(self):
        d = SqliteTimeSeriesStorage._row_to_dict([1_000_000_000, None, None, None, 1, "good"])
        assert d["value"] is True

    def test_bool_false(self):
        d = SqliteTimeSeriesStorage._row_to_dict([1_000_000_000, None, None, None, 0, "good"])
        assert d["value"] is False

    def test_all_none_value(self):
        d = SqliteTimeSeriesStorage._row_to_dict([1_000_000_000, None, None, None, None, None])
        assert d["value"] is None

    def test_quality_none_defaults_good(self):
        d = SqliteTimeSeriesStorage._row_to_dict([1_000_000_000, 1.0, None, None, None, None])
        assert d["quality"] == "good"


# --------------------------------------------------------------------------- #
# 性能
# --------------------------------------------------------------------------- #
class TestPerformance:
    """大数据集与并发写入"""

    async def test_large_dataset_batch(self, tmp_path):
        """批量写入 1500 条并查询校验"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        points = [
            {
                "measurement": "m",
                "device_id": "dev1",
                "point_name": "p",
                "value": float(i),
                "timestamp_ns": 1_000_000_000 + i,
            }
            for i in range(1500)
        ]
        await s.write_points_batch(points)
        pts = await s.query_points("dev1", "p", 0, 2_000_000_000, limit=10000)
        assert len(pts) == 1500
        stats = await s.get_stats()
        assert stats["total_records"] == 1500
        await s.stop()

    async def test_concurrent_batch_writes(self, tmp_path):
        """多设备并发批量写入，锁应保证数据一致"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()

        async def write_batch(dev, n):
            pts = [
                {
                    "measurement": "m",
                    "device_id": dev,
                    "point_name": "p",
                    "value": float(i),
                    "timestamp_ns": 1_000_000_000 + i,
                }
                for i in range(n)
            ]
            await s.write_points_batch(pts)

        await asyncio.gather(
            write_batch("dev1", 100),
            write_batch("dev2", 100),
            write_batch("dev3", 100),
        )
        assert await s.get_unsynced_count() == 300
        await s.stop()

    async def test_large_cleanup_batched(self, tmp_path):
        """大批量过期数据清理应分批完成"""
        s = SqliteTimeSeriesStorage(str(tmp_path / "ts.db"))
        await s.start()
        old_ns = int((time.time() - 60 * 86400) * 1e9)
        points = [
            {
                "measurement": "m",
                "device_id": "dev1",
                "point_name": "p",
                "value": float(i),
                "timestamp_ns": old_ns + i,
            }
            for i in range(2500)
        ]
        await s.write_points_batch(points)
        n = await s.cleanup_old_data(retention_days=30)
        assert n == 2500
        await s.stop()
