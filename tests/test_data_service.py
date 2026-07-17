"""数据查询业务逻辑测试

覆盖 services/data_service.py：
- _sanitize_csv_cell: CSV 注入防护
- DataService: query_timeseries / get_latest_points / export_data
- stream_export_data: 流式导出（CSV/JSON 分批）
- query_trend / query_correlation / get_statistics / query_multi_point
- historical_service 延迟加载
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "src")

from edgelite.services.data_service import DataService, _sanitize_csv_cell
from edgelite.services.historical_data import HistoricalDataService, QueryOptions

# ──────────────────────── fixtures ────────────────────────


@pytest.fixture
def influx():
    store = AsyncMock()
    store.query_points = AsyncMock(return_value=[])
    store.query_latest = AsyncMock(return_value={})
    return store


@pytest.fixture
def device_repo():
    return MagicMock()


@pytest.fixture
def svc(influx, device_repo):
    return DataService(influx, device_repo)


@pytest.fixture
def historical_svc():
    """Mock HistoricalDataService，避免真实 InfluxDB 查询"""
    h = AsyncMock()
    h.query_trend = AsyncMock(return_value={"trend": "up"})
    h.query_correlation = AsyncMock(return_value={"correlation": 0.8})
    h.query = AsyncMock()
    h.query_multi_point = AsyncMock(return_value={})
    return h


def _make_result(device_id="dev1", point_name="temp", count=5, stats=None, data_points=None):
    """构造 QueryResult 兼容对象"""
    return SimpleNamespace(
        device_id=device_id,
        point_name=point_name,
        count=count,
        statistics=stats or {"mean": 25.0},
        data_points=data_points or [],
    )


# ──────────────────────── _sanitize_csv_cell ────────────────────────


class TestSanitizeCsvCell:
    def test_equals_sign(self):
        assert _sanitize_csv_cell("=cmd|calc") == "'=cmd|calc"

    def test_plus_sign(self):
        assert _sanitize_csv_cell("+1+2") == "'+1+2"

    def test_minus_sign(self):
        assert _sanitize_csv_cell("-1-2") == "'-1-2"

    def test_at_sign(self):
        assert _sanitize_csv_cell("@sum(A1)") == "'@sum(A1)"

    def test_normal_string(self):
        assert _sanitize_csv_cell("normal") == "normal"

    def test_empty_string(self):
        assert _sanitize_csv_cell("") == ""

    def test_non_string_int(self):
        assert _sanitize_csv_cell(123) == 123

    def test_non_string_none(self):
        assert _sanitize_csv_cell(None) is None

    def test_non_string_float(self):
        assert _sanitize_csv_cell(3.14) == 3.14

    def test_single_equals(self):
        assert _sanitize_csv_cell("=") == "'="

    def test_single_dash(self):
        assert _sanitize_csv_cell("-") == "'-"

    def test_no_modifier_in_middle(self):
        """仅开头的 =/+/-/@ 触发，中间出现不触发"""
        assert _sanitize_csv_cell("a=b") == "a=b"


# ──────────────────────── __init__ ────────────────────────


class TestDataServiceInit:
    def test_stores_deps(self, svc, influx, device_repo):
        assert svc._influx is influx
        assert svc._device_repo is device_repo

    def test_historical_svc_initially_none(self, svc):
        assert svc._historical_svc is None


class TestHistoricalServiceProperty:
    def test_lazy_load_creates_instance(self, svc, influx):
        """首次访问创建 HistoricalDataService"""
        hs = svc.historical_service
        assert isinstance(hs, HistoricalDataService)
        assert hs._influx is influx

    def test_lazy_load_caches(self, svc):
        """重复访问返回同一实例"""
        hs1 = svc.historical_service
        hs2 = svc.historical_service
        assert hs1 is hs2


# ──────────────────────── query_timeseries ────────────────────────


class TestQueryTimeseries:
    async def test_basic_query(self, svc, influx):
        influx.query_points.return_value = [{"time": "t1", "value": 1}]
        result = await svc.query_timeseries("dev1", "temp", "-1h")
        assert result == [{"time": "t1", "value": 1}]
        influx.query_points.assert_awaited_once()
        call_args = influx.query_points.await_args
        assert call_args.args == ("dev1", "temp", "-1h", None, None)
        assert call_args.kwargs == {"max_points": 10000, "agg_fn": None}

    async def test_with_stop_and_aggregate(self, svc, influx):
        influx.query_points.return_value = []
        await svc.query_timeseries("dev1", "temp", "-1h", stop="now", aggregate="5m")
        call_args = influx.query_points.await_args
        assert call_args.args == ("dev1", "temp", "-1h", "now", "5m")

    async def test_with_agg_fn(self, svc, influx):
        influx.query_points.return_value = []
        await svc.query_timeseries("dev1", "temp", "-1h", agg_fn="max")
        assert influx.query_points.await_args.kwargs["agg_fn"] == "max"

    async def test_fetch_limit_includes_offset(self, svc, influx):
        """max_points 应为 limit + offset"""
        influx.query_points.return_value = []
        await svc.query_timeseries("dev1", "temp", "-1h", limit=100, offset=50)
        assert influx.query_points.await_args.kwargs["max_points"] == 150

    async def test_offset_slices_data(self, svc, influx):
        """offset > 0 时切片掉前 offset 条"""
        influx.query_points.return_value = [{"v": 1}, {"v": 2}, {"v": 3}]
        result = await svc.query_timeseries("dev1", "temp", "-1h", limit=10, offset=1)
        assert result == [{"v": 2}, {"v": 3}]

    async def test_zero_offset_no_slice(self, svc, influx):
        influx.query_points.return_value = [{"v": 1}, {"v": 2}]
        result = await svc.query_timeseries("dev1", "temp", "-1h", limit=10, offset=0)
        assert result == [{"v": 1}, {"v": 2}]

    async def test_default_limit(self, svc, influx):
        influx.query_points.return_value = []
        await svc.query_timeseries("dev1", "temp", "-1h")
        assert influx.query_points.await_args.kwargs["max_points"] == 10000


# ──────────────────────── get_latest_points ────────────────────────


class TestGetLatestPoints:
    async def test_without_point_names(self, svc, influx):
        influx.query_latest.return_value = {"temp": 25.0}
        result = await svc.get_latest_points("dev1")
        assert result == {"temp": 25.0}
        influx.query_latest.assert_awaited_once_with("dev1", None)

    async def test_with_point_names(self, svc, influx):
        influx.query_latest.return_value = {"temp": 25.0}
        result = await svc.get_latest_points("dev1", ["temp", "hum"])
        assert result == {"temp": 25.0}
        influx.query_latest.assert_awaited_once_with("dev1", ["temp", "hum"])

    async def test_empty_result(self, svc, influx):
        influx.query_latest.return_value = {}
        result = await svc.get_latest_points("dev1")
        assert result == {}


# ──────────────────────── export_data ────────────────────────


class TestExportData:
    async def test_csv_basic(self, svc, influx):
        influx.query_points.return_value = [
            {"time": "t1", "device_id": "dev1", "point_name": "temp", "value": 25, "quality": "good"},
        ]
        result = await svc.export_data("dev1", "temp", "-1h")
        assert "time,device_id,point_name,value,quality" in result
        assert "t1,dev1,temp,25,good" in result

    async def test_csv_injection_sanitized(self, svc, influx):
        influx.query_points.return_value = [
            {"time": "=evil", "device_id": "dev1", "point_name": "temp", "value": 25, "quality": "good"},
        ]
        result = await svc.export_data("dev1", "temp", "-1h")
        assert "'=evil" in result
        assert ",=evil," not in result

    async def test_csv_empty_data(self, svc, influx):
        influx.query_points.return_value = []
        result = await svc.export_data("dev1", "temp", "-1h")
        assert "time,device_id,point_name,value,quality" in result
        # 只有表头，无数据行
        lines = result.strip().split("\n")
        assert len(lines) == 1

    async def test_csv_missing_fields_default_empty(self, svc, influx):
        """缺失字段使用空字符串"""
        influx.query_points.return_value = [{"time": "t1"}]
        result = await svc.export_data("dev1", "temp", "-1h")
        assert "t1,,,," in result

    async def test_csv_max_points_passed(self, svc, influx):
        influx.query_points.return_value = []
        await svc.export_data("dev1", "temp", "-1h", limit=500)
        assert influx.query_points.await_args.kwargs["max_points"] == 500

    async def test_json_format(self, svc, influx):
        influx.query_points.return_value = [{"time": "t1", "value": 25}]
        result = await svc.export_data("dev1", "temp", "-1h", fmt="json")
        parsed = json.loads(result)
        assert parsed == [{"time": "t1", "value": 25}]

    async def test_json_empty_data(self, svc, influx):
        influx.query_points.return_value = []
        result = await svc.export_data("dev1", "temp", "-1h", fmt="json")
        assert json.loads(result) == []

    async def test_json_ensure_ascii_false(self, svc, influx):
        """JSON 导出中文不转义"""
        influx.query_points.return_value = [{"name": "温度"}]
        result = await svc.export_data("dev1", "temp", "-1h", fmt="json")
        assert "温度" in result

    async def test_with_stop_param(self, svc, influx):
        influx.query_points.return_value = []
        await svc.export_data("dev1", "temp", "-1h", stop="now")
        assert influx.query_points.await_args.args[3] == "now"


# ──────────────────────── stream_export_data ────────────────────────


class TestStreamExportData:
    async def _collect(self, gen):
        chunks = []
        async for chunk in gen:
            chunks.append(chunk)
        return chunks

    async def test_csv_single_batch_with_header(self, svc, influx):
        influx.query_points.return_value = [
            {"time": "t1", "device_id": "dev1", "point_name": "temp", "value": 25, "quality": "good"},
        ]
        chunks = await self._collect(svc.stream_export_data("dev1", "temp", "-1h", fmt="csv"))
        assert len(chunks) == 1
        text = chunks[0].decode("utf-8")
        assert "time,device_id,point_name,value,quality" in text
        assert "t1,dev1,temp,25,good" in text

    async def test_csv_multiple_batches_header_once(self, svc, influx):
        batch1 = [{"time": "t1", "device_id": "d", "point_name": "p", "value": 1, "quality": "g"}]
        batch2 = [{"time": "t2", "device_id": "d", "point_name": "p", "value": 2, "quality": "g"}]
        influx.query_points.side_effect = [batch1, batch2, []]
        chunks = await self._collect(svc.stream_export_data("dev1", "temp", "-1h", fmt="csv", batch_size=1, limit=10))
        full = b"".join(chunks).decode("utf-8")
        # 表头只出现一次
        assert full.count("time,device_id,point_name,value,quality") == 1
        assert "t1" in full
        assert "t2" in full

    async def test_csv_empty_data(self, svc, influx):
        influx.query_points.return_value = []
        chunks = await self._collect(svc.stream_export_data("dev1", "temp", "-1h", fmt="csv"))
        # 无数据时不应输出任何 chunk（首批即空 -> break）
        assert chunks == []

    async def test_csv_injection_sanitized(self, svc, influx):
        influx.query_points.return_value = [
            {"time": "=evil", "device_id": "d", "point_name": "p", "value": 1, "quality": "g"},
        ]
        chunks = await self._collect(svc.stream_export_data("dev1", "temp", "-1h", fmt="csv"))
        text = chunks[0].decode("utf-8")
        assert "'=evil" in text

    async def test_csv_stops_when_partial_batch(self, svc, influx):
        """返回数据少于请求量时停止（说明已无更多数据）"""
        influx.query_points.return_value = [{"time": "t1"}]
        chunks = await self._collect(
            svc.stream_export_data("dev1", "temp", "-1h", fmt="csv", batch_size=100, limit=1000)
        )
        assert len(chunks) == 1
        assert influx.query_points.await_count == 1

    async def test_csv_limit_respected(self, svc, influx):
        """limit 限制总记录数"""
        influx.query_points.return_value = [{"time": "t1"}]
        await self._collect(svc.stream_export_data("dev1", "temp", "-1h", fmt="csv", batch_size=1, limit=1))
        assert influx.query_points.await_count == 1

    async def test_json_single_batch(self, svc, influx):
        influx.query_points.return_value = [{"time": "t1", "value": 25}]
        chunks = await self._collect(svc.stream_export_data("dev1", "temp", "-1h", fmt="json"))
        full = b"".join(chunks).decode("utf-8")
        assert full.startswith("[")
        assert full.endswith("]")
        parsed = json.loads(full)
        assert parsed == [{"time": "t1", "value": 25}]

    async def test_json_multiple_batches(self, svc, influx):
        batch1 = [{"time": "t1", "value": 1}]
        batch2 = [{"time": "t2", "value": 2}]
        influx.query_points.side_effect = [batch1, batch2, []]
        chunks = await self._collect(svc.stream_export_data("dev1", "temp", "-1h", fmt="json", batch_size=1, limit=10))
        full = b"".join(chunks).decode("utf-8")
        parsed = json.loads(full)
        assert parsed == [{"time": "t1", "value": 1}, {"time": "t2", "value": 2}]

    async def test_json_empty_data(self, svc, influx):
        """无数据时 JSON 返回空数组 []"""
        influx.query_points.return_value = []
        chunks = await self._collect(svc.stream_export_data("dev1", "temp", "-1h", fmt="json"))
        full = b"".join(chunks).decode("utf-8")
        assert json.loads(full) == []

    async def test_json_partial_batch_stops(self, svc, influx):
        influx.query_points.return_value = [{"time": "t1"}]
        chunks = await self._collect(
            svc.stream_export_data("dev1", "temp", "-1h", fmt="json", batch_size=100, limit=1000)
        )
        full = b"".join(chunks).decode("utf-8")
        assert json.loads(full) == [{"time": "t1"}]
        assert influx.query_points.await_count == 1

    async def test_default_fmt_is_csv(self, svc, influx):
        influx.query_points.return_value = [{"time": "t1"}]
        chunks = await self._collect(svc.stream_export_data("dev1", "temp", "-1h"))
        assert chunks  # csv produces output
        assert b"time,device_id" in chunks[0]


# ──────────────────────── query_trend / query_correlation ────────────────────────


class TestQueryTrend:
    async def test_calls_historical_service(self, svc, historical_svc):
        svc._historical_svc = historical_svc
        result = await svc.query_trend("dev1", "temp")
        assert result == {"trend": "up"}
        historical_svc.query_trend.assert_awaited_once_with("dev1", "temp", start="-24h", stop="", bucket_size="1h")

    async def test_custom_params(self, svc, historical_svc):
        svc._historical_svc = historical_svc
        await svc.query_trend("dev1", "temp", start="-7d", stop="now", bucket_size="6h")
        historical_svc.query_trend.assert_awaited_once_with("dev1", "temp", start="-7d", stop="now", bucket_size="6h")

    async def test_stop_none_becomes_empty(self, svc, historical_svc):
        svc._historical_svc = historical_svc
        await svc.query_trend("dev1", "temp")
        assert historical_svc.query_trend.await_args.kwargs["stop"] == ""


class TestQueryCorrelation:
    async def test_calls_historical_service(self, svc, historical_svc):
        svc._historical_svc = historical_svc
        result = await svc.query_correlation("dev1", "temp", "hum")
        assert result == {"correlation": 0.8}
        historical_svc.query_correlation.assert_awaited_once_with("dev1", "temp", "hum", start="-24h", stop="")

    async def test_custom_params(self, svc, historical_svc):
        svc._historical_svc = historical_svc
        await svc.query_correlation("dev1", "temp", "hum", start="-1h", stop="now")
        historical_svc.query_correlation.assert_awaited_once_with("dev1", "temp", "hum", start="-1h", stop="now")


# ──────────────────────── get_statistics ────────────────────────


class TestGetStatistics:
    async def test_returns_statistics_dict(self, svc, historical_svc):
        historical_svc.query.return_value = _make_result(
            device_id="dev1", point_name="temp", count=10, stats={"mean": 25.0, "max": 30.0}
        )
        svc._historical_svc = historical_svc
        result = await svc.get_statistics("dev1", "temp", start="-1h")
        assert result == {
            "device_id": "dev1",
            "point_name": "temp",
            "count": 10,
            "statistics": {"mean": 25.0, "max": 30.0},
        }

    async def test_stop_none_becomes_empty(self, svc, historical_svc):
        historical_svc.query.return_value = _make_result()
        svc._historical_svc = historical_svc
        await svc.get_statistics("dev1", "temp")
        call_args = historical_svc.query.await_args
        options = call_args.args[2]
        assert isinstance(options, QueryOptions)
        assert options.stop == ""
        assert options.aggregate == ""

    async def test_aggregate_passed(self, svc, historical_svc):
        historical_svc.query.return_value = _make_result()
        svc._historical_svc = historical_svc
        await svc.get_statistics("dev1", "temp", start="-1h", stop="now", aggregate="5m")
        options = historical_svc.query.await_args.args[2]
        assert options.aggregate == "5m"
        assert options.stop == "now"

    async def test_query_options_start(self, svc, historical_svc):
        historical_svc.query.return_value = _make_result()
        svc._historical_svc = historical_svc
        await svc.get_statistics("dev1", "temp", start="-12h")
        options = historical_svc.query.await_args.args[2]
        assert options.start == "-12h"


# ──────────────────────── query_multi_point ────────────────────────


class TestQueryMultiPoint:
    async def test_basic_query(self, svc, historical_svc):
        historical_svc.query_multi_point.return_value = {
            "temp": _make_result("dev1", "temp", 5, {"mean": 25}, [{"t": "t1"}]),
            "hum": _make_result("dev1", "hum", 3, {"mean": 60}, []),
        }
        svc._historical_svc = historical_svc
        result = await svc.query_multi_point("dev1", ["temp", "hum"], start="-1h")
        assert "temp" in result
        assert result["temp"]["device_id"] == "dev1"
        assert result["temp"]["point_name"] == "temp"
        assert result["temp"]["count"] == 5
        assert result["temp"]["data_points"] == [{"t": "t1"}]
        assert result["temp"]["statistics"] == {"mean": 25}
        assert result["hum"]["count"] == 3

    async def test_truncates_over_100_points(self, svc, historical_svc):
        """point_names 超过 100 个时截断"""
        historical_svc.query_multi_point.return_value = {}
        svc._historical_svc = historical_svc
        names = [f"p{i}" for i in range(150)]
        await svc.query_multi_point("dev1", names)
        call_args = historical_svc.query_multi_point.await_args
        passed_names = call_args.args[1]
        assert len(passed_names) == 100

    async def test_exactly_100_not_truncated(self, svc, historical_svc):
        historical_svc.query_multi_point.return_value = {}
        svc._historical_svc = historical_svc
        names = [f"p{i}" for i in range(100)]
        await svc.query_multi_point("dev1", names)
        passed_names = historical_svc.query_multi_point.await_args.args[1]
        assert len(passed_names) == 100

    async def test_stop_none_becomes_empty(self, svc, historical_svc):
        historical_svc.query_multi_point.return_value = {}
        svc._historical_svc = historical_svc
        await svc.query_multi_point("dev1", ["temp"])
        options = historical_svc.query_multi_point.await_args.args[2]
        assert options.stop == ""
        assert options.aggregate == ""

    async def test_aggregate_passed(self, svc, historical_svc):
        historical_svc.query_multi_point.return_value = {}
        svc._historical_svc = historical_svc
        await svc.query_multi_point("dev1", ["temp"], start="-1h", stop="now", aggregate="5m")
        options = historical_svc.query_multi_point.await_args.args[2]
        assert options.aggregate == "5m"
        assert options.stop == "now"

    async def test_empty_result(self, svc, historical_svc):
        historical_svc.query_multi_point.return_value = {}
        svc._historical_svc = historical_svc
        result = await svc.query_multi_point("dev1", ["temp"])
        assert result == {}
