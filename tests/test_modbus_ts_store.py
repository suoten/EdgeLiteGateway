"""modbus_ts_store 模块测试 - 时序存储纯函数与数据转换

覆盖 drivers/modbus_ts_store.py 的静态方法与构造器：
- _dt_to_ns: datetime→ns 时间戳，None→now，naive→UTC
- _extract_point_value: PointValue duck typing 提取 / 裸值回退
- _value_to_columns: bool/int/float/str/None → 四列拆分
- _row_to_dict: 查询行→结果 dict（值优先级还原）
- 构造器: retention_days 边界（max(1, int(...))）
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from edgelite.drivers.modbus_ts_store import ModbusTsStore


# --------------------------------------------------------------------------- #
# _dt_to_ns
# --------------------------------------------------------------------------- #


class TestDtToNs:
    """datetime → ns 时间戳转换"""

    def test_none_returns_current_time_ns(self):
        before = time.time_ns()
        result = ModbusTsStore._dt_to_ns(None)
        after = time.time_ns()
        assert before <= result <= after

    def test_naive_datetime_treated_as_utc(self):
        """无时区信息的 datetime 按 UTC 处理"""
        dt = datetime(2024, 1, 1, 0, 0, 0)  # naive
        result = ModbusTsStore._dt_to_ns(dt)
        expected = int(datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp() * 1_000_000_000)
        assert result == expected

    def test_aware_datetime(self):
        """有时区信息的 datetime 直接转换"""
        dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        result = ModbusTsStore._dt_to_ns(dt)
        expected = int(dt.timestamp() * 1_000_000_000)
        assert result == expected

    def test_aware_non_utc_datetime(self):
        """非 UTC 时区的 aware datetime 正确转换"""
        # UTC+8 的 2024-01-01 08:00:00 == UTC 2024-01-01 00:00:00
        from datetime import timezone

        dt = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone(timedelta(hours=8)))
        utc_dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert ModbusTsStore._dt_to_ns(dt) == ModbusTsStore._dt_to_ns(utc_dt)

    def test_result_is_int(self):
        dt = datetime(2024, 6, 15, 12, 30, 45, tzinfo=UTC)
        result = ModbusTsStore._dt_to_ns(dt)
        assert isinstance(result, int)

    def test_epoch_zero(self):
        """UTC 1970-01-01 00:00:00 → 0 ns"""
        dt = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert ModbusTsStore._dt_to_ns(dt) == 0


# --------------------------------------------------------------------------- #
# _extract_point_value
# --------------------------------------------------------------------------- #


class TestExtractPointValue:
    """从 PointValue 或裸值提取 (value, quality, timestamp_ns)"""

    def test_bare_int(self):
        val, quality, ts = ModbusTsStore._extract_point_value(42)
        assert val == 42
        assert quality == "good"
        assert ts > 0

    def test_bare_float(self):
        val, quality, ts = ModbusTsStore._extract_point_value(3.14)
        assert val == 3.14
        assert quality == "good"

    def test_bare_str(self):
        val, quality, _ = ModbusTsStore._extract_point_value("hello")
        assert val == "hello"
        assert quality == "good"

    def test_bare_bool(self):
        """bool 是 int 子类，走裸值分支"""
        val, quality, _ = ModbusTsStore._extract_point_value(True)
        assert val is True
        assert quality == "good"

    def test_bare_none(self):
        val, quality, _ = ModbusTsStore._extract_point_value(None)
        assert val is None
        assert quality == "good"

    def test_pointvalue_like_with_timestamp(self):
        """有 .value/.quality/.timestamp 的对象 → 提取三元组"""
        dt = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        pv = SimpleNamespace(value=99.5, quality="bad", timestamp=dt)
        val, quality, ts = ModbusTsStore._extract_point_value(pv)
        assert val == 99.5
        assert quality == "bad"
        expected_ns = int(dt.timestamp() * 1_000_000_000)
        assert ts == expected_ns

    def test_pointvalue_like_without_timestamp(self):
        """有 .value/.quality 但无 .timestamp → 用 now"""
        pv = SimpleNamespace(value=1, quality="uncertain")
        # 删除 timestamp 属性确保不存在
        pv = SimpleNamespace(value=1, quality="uncertain")
        val, quality, ts = ModbusTsStore._extract_point_value(pv)
        assert val == 1
        assert quality == "uncertain"
        assert ts > 0  # time.time_ns()

    def test_pointvalue_like_none_quality_defaults_good(self):
        """quality=None → 回退 'good'（`or 'good'`）"""
        pv = SimpleNamespace(value=1, quality=None, timestamp=None)
        val, quality, _ = ModbusTsStore._extract_point_value(pv)
        assert val == 1
        assert quality == "good"

    def test_pointvalue_like_empty_quality_defaults_good(self):
        """quality='' → 回退 'good'"""
        pv = SimpleNamespace(value=1, quality="", timestamp=None)
        val, quality, _ = ModbusTsStore._extract_point_value(pv)
        assert quality == "good"

    def test_pointvalue_like_non_datetime_timestamp(self):
        """timestamp 不是 datetime（如字符串）→ 用 now"""
        pv = SimpleNamespace(value=1, quality="good", timestamp="2024-01-01")
        val, quality, ts = ModbusTsStore._extract_point_value(pv)
        assert val == 1
        assert ts > 0  # 走 time.time_ns()

    def test_dict_not_treated_as_pointvalue(self):
        """dict 不触发 PointValue 分支（isinstance 排除 dict）"""
        val, quality, _ = ModbusTsStore._extract_point_value({"value": 1, "quality": "bad"})
        assert quality == "good"  # 走裸值分支

    def test_list_not_treated_as_pointvalue(self):
        val, quality, _ = ModbusTsStore._extract_point_value([1, 2, 3])
        assert quality == "good"

    def test_tuple_not_treated_as_pointvalue(self):
        val, quality, _ = ModbusTsStore._extract_point_value((1, 2))
        assert quality == "good"

    def test_bytes_not_treated_as_pointvalue(self):
        val, quality, _ = ModbusTsStore._extract_point_value(b"data")
        assert quality == "good"


# --------------------------------------------------------------------------- #
# _value_to_columns
# --------------------------------------------------------------------------- #


class TestValueToColumns:
    """值 → (value_real, value_int, value_str, value_bool) 列拆分"""

    def test_bool_true(self):
        r, i, s, b = ModbusTsStore._value_to_columns(True)
        assert r is None
        assert i is None
        assert s is None
        assert b == 1

    def test_bool_false(self):
        r, i, s, b = ModbusTsStore._value_to_columns(False)
        assert b == 0
        assert r is None and i is None and s is None

    def test_int(self):
        r, i, s, b = ModbusTsStore._value_to_columns(42)
        assert i == 42
        assert r is None and s is None and b is None

    def test_negative_int(self):
        r, i, s, b = ModbusTsStore._value_to_columns(-7)
        assert i == -7

    def test_float(self):
        r, i, s, b = ModbusTsStore._value_to_columns(3.14)
        assert r == 3.14
        assert i is None and s is None and b is None

    def test_float_zero(self):
        r, i, s, b = ModbusTsStore._value_to_columns(0.0)
        assert r == 0.0
        assert i is None  # 0.0 是 float 不是 int

    def test_str(self):
        r, i, s, b = ModbusTsStore._value_to_columns("active")
        assert s == "active"
        assert r is None and i is None and b is None

    def test_empty_str(self):
        r, i, s, b = ModbusTsStore._value_to_columns("")
        assert s == ""

    def test_none(self):
        r, i, s, b = ModbusTsStore._value_to_columns(None)
        assert r is None and i is None and s is None and b is None

    def test_bool_checked_before_int(self):
        """bool 是 int 子类，必须先匹配 bool 分支（True→b=1 而非 i=1）"""
        r, i, s, b = ModbusTsStore._value_to_columns(True)
        assert b == 1
        assert i is None  # 不走 int 分支


# --------------------------------------------------------------------------- #
# _row_to_dict
# --------------------------------------------------------------------------- #


class _FakeRow:
    """模拟 aiosqlite.Row，支持 __getitem__"""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


class TestRowToDict:
    """查询行 → 结果 dict（值优先级 value_real > value_int > value_str > value_bool）"""

    def test_value_real_priority(self):
        row = _FakeRow(
            {
                "timestamp_ns": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1e9),
                "value_real": 3.14,
                "value_int": 42,
                "value_str": "x",
                "value_bool": 1,
                "quality": "good",
            }
        )
        d = ModbusTsStore._row_to_dict(row)
        assert d["value"] == 3.14
        assert d["quality"] == "good"
        assert "T" in d["time"]  # ISO format

    def test_value_int_when_real_none(self):
        row = _FakeRow(
            {
                "timestamp_ns": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1e9),
                "value_real": None,
                "value_int": 42,
                "value_str": None,
                "value_bool": None,
                "quality": "good",
            }
        )
        d = ModbusTsStore._row_to_dict(row)
        assert d["value"] == 42

    def test_value_str_when_real_int_none(self):
        row = _FakeRow(
            {
                "timestamp_ns": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1e9),
                "value_real": None,
                "value_int": None,
                "value_str": "active",
                "value_bool": None,
                "quality": "good",
            }
        )
        d = ModbusTsStore._row_to_dict(row)
        assert d["value"] == "active"

    def test_value_bool_when_all_others_none(self):
        row = _FakeRow(
            {
                "timestamp_ns": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1e9),
                "value_real": None,
                "value_int": None,
                "value_str": None,
                "value_bool": 1,
                "quality": "good",
            }
        )
        d = ModbusTsStore._row_to_dict(row)
        assert d["value"] is True

    def test_all_none_value_is_none(self):
        row = _FakeRow(
            {
                "timestamp_ns": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1e9),
                "value_real": None,
                "value_int": None,
                "value_str": None,
                "value_bool": None,
                "quality": "good",
            }
        )
        d = ModbusTsStore._row_to_dict(row)
        assert d["value"] is None

    def test_quality_none_defaults_good(self):
        row = _FakeRow(
            {
                "timestamp_ns": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1e9),
                "value_real": 1.0,
                "value_int": None,
                "value_str": None,
                "value_bool": None,
                "quality": None,
            }
        )
        d = ModbusTsStore._row_to_dict(row)
        assert d["quality"] == "good"

    def test_bool_false_value(self):
        row = _FakeRow(
            {
                "timestamp_ns": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1e9),
                "value_real": None,
                "value_int": None,
                "value_str": None,
                "value_bool": 0,
                "quality": "good",
            }
        )
        d = ModbusTsStore._row_to_dict(row)
        assert d["value"] is False

    def test_time_is_iso_format(self):
        ts_ns = int(datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC).timestamp() * 1e9)
        row = _FakeRow(
            {
                "timestamp_ns": ts_ns,
                "value_real": 1.0,
                "value_int": None,
                "value_str": None,
                "value_bool": None,
                "quality": "good",
            }
        )
        d = ModbusTsStore._row_to_dict(row)
        assert d["time"].startswith("2024-06-15T12:00:00")


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #


class TestModbusTsStoreConstructor:
    """ModbusTsStore(retention_days) 边界测试"""

    def test_default_retention(self):
        store = ModbusTsStore()
        assert store._retention_days == 7

    def test_positive_retention(self):
        store = ModbusTsStore(retention_days=30)
        assert store._retention_days == 30

    def test_zero_retention_clamped_to_one(self):
        store = ModbusTsStore(retention_days=0)
        assert store._retention_days == 1  # max(1, 0) = 1

    def test_negative_retention_clamped_to_one(self):
        store = ModbusTsStore(retention_days=-5)
        assert store._retention_days == 1  # max(1, -5) = 1

    def test_float_retention_truncated_to_int(self):
        store = ModbusTsStore(retention_days=3.9)
        assert store._retention_days == 3  # int(3.9) = 3, max(1, 3) = 3

    def test_float_below_one_clamped(self):
        store = ModbusTsStore(retention_days=0.5)
        assert store._retention_days == 1  # int(0.5)=0, max(1,0)=1

    def test_string_numeric_retention(self):
        """int('7') = 7"""
        store = ModbusTsStore(retention_days="7")  # type: ignore[arg-type]
        assert store._retention_days == 7

    def test_default_attributes(self):
        store = ModbusTsStore()
        assert store._db_path == "data/modbus_ts.db"
        assert store._db is None
        assert store._write_count == 0
        assert store._pending_writes == 0
        assert store._max_pending == 200
        assert store._cleanup_every == 500

    def test_db_lock_is_asyncio_lock(self):
        import asyncio

        store = ModbusTsStore()
        assert isinstance(store._db_lock, asyncio.Lock)
