"""历史数据查询服务测试

覆盖 src/edgelite/services/historical_data.py：
- AggregationType / TimeRangeFormat 枚举
- QueryOptions / QueryResult / Statistics 数据类
- HistoricalDataService:
  - query: 基础查询、超时、quality 过滤、统计、非数值过滤
  - _calculate_statistics: 全量统计指标
  - _percentile: 百分位计算
  - query_multi_point: 并发多点查询、部分失败
  - query_aggregated: 窗口聚合
  - export_data / _export_json / _export_csv: 导出
  - query_trend: 线性回归趋势
  - query_correlation: 皮尔逊相关性
- DeviceShadowService: 影子状态、回调、离线命令、LRU 淘汰
- get_historical_service / get_shadow_service: 单例获取
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from edgelite.services import historical_data as hd
from edgelite.services.historical_data import (
    AggregationType,
    DeviceShadowService,
    HistoricalDataService,
    QueryOptions,
    QueryResult,
    Statistics,
    TimeRangeFormat,
)

# ──────────────────────────────────────────────────────────────────────
# 辅助与夹具
# ──────────────────────────────────────────────────────────────────────


def _point(time: str, value, quality: str = "good"):
    """构造一个数据点 dict。"""
    return {"time": time, "value": value, "quality": quality}


def _series(n: int, start: float = 0.0, step: float = 1.0, quality: str = "good"):
    """构造 n 个等差数据点。"""
    return [_point(f"2026-07-10T10:{i:02d}:00Z", start + i * step, quality) for i in range(n)]


@pytest.fixture
def mock_influx():
    """模拟 InfluxDBStorage，query_points/query_latest 默认返回空。"""
    storage = MagicMock()
    storage.query_points = AsyncMock(return_value=[])
    storage.query_latest = AsyncMock(return_value={})
    return storage


@pytest.fixture
def svc(mock_influx):
    return HistoricalDataService(mock_influx)


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch):
    """每个测试前后重置模块级单例，防止跨测试污染。"""
    hd._historical_service = None
    hd._shadow_service = None
    yield
    hd._historical_service = None
    hd._shadow_service = None


# ══════════════════════════════════════════════════════════════════════
# 枚举与数据类
# ══════════════════════════════════════════════════════════════════════


class TestEnums:
    """枚举值与成员。"""

    def test_aggregation_type_members(self):
        assert AggregationType.MEAN.value == "mean"
        assert AggregationType.SUM.value == "sum"
        assert AggregationType.MAX.value == "max"
        assert AggregationType.MIN.value == "min"
        assert AggregationType.COUNT.value == "count"
        assert AggregationType.FIRST.value == "first"
        assert AggregationType.LAST.value == "last"
        assert AggregationType.MEDIAN.value == "median"
        assert AggregationType.STDDEV.value == "stddev"
        assert AggregationType.VARIANCE.value == "variance"
        assert AggregationType.PERCENTILE_90.value == "percentile_90"
        assert AggregationType.PERCENTILE_95.value == "percentile_95"
        assert AggregationType.PERCENTILE_99.value == "percentile_99"

    def test_time_range_format_members(self):
        assert TimeRangeFormat.RELATIVE.value == "relative"
        assert TimeRangeFormat.ABSOLUTE.value == "absolute"
        assert TimeRangeFormat.DURATION.value == "duration"


class TestDataclasses:
    """数据类默认值与字段。"""

    def test_query_options_defaults(self):
        opts = QueryOptions()
        assert opts.start == "-1h"
        assert opts.stop == ""
        assert opts.aggregate == ""
        assert opts.aggregation == AggregationType.MEAN
        assert opts.fill == "null"
        assert opts.limit == 10000
        assert opts.filter_quality == ""
        assert opts.interpolation == "linear"

    def test_query_result_defaults(self):
        r = QueryResult()
        assert r.device_id == ""
        assert r.count == 0
        assert r.data_points == []
        assert r.statistics == {}
        assert r.query_ms == 0.0

    def test_statistics_defaults(self):
        s = Statistics()
        assert s.count == 0
        assert s.sum == 0.0
        assert s.mean == 0.0

    def test_query_result_is_independent(self):
        """每个 QueryResult 实例的 data_points 应独立（default_factory）。"""
        a = QueryResult()
        b = QueryResult()
        a.data_points.append(_point("t", 1))
        assert b.data_points == []


# ══════════════════════════════════════════════════════════════════════
# _percentile
# ══════════════════════════════════════════════════════════════════════


class TestPercentile:
    """百分位计算静态方法。"""

    def test_empty_returns_zero(self):
        assert HistoricalDataService._percentile([], 50) == 0.0

    def test_single_value(self):
        assert HistoricalDataService._percentile([42.0], 50) == 42.0
        assert HistoricalDataService._percentile([42.0], 90) == 42.0

    def test_median_of_even_count(self):
        """偶数个值的中位数插值。"""
        vals = [1.0, 2.0, 3.0, 4.0]
        # index for 50 = 0.5*(4-1)=1.5 -> 2.0*0.5 + 3.0*0.5 = 2.5
        assert HistoricalDataService._percentile(vals, 50) == 2.5

    def test_median_of_odd_count(self):
        vals = [1.0, 2.0, 3.0]
        # index for 50 = 0.5*2 = 1.0 -> vals[1] = 2.0
        assert HistoricalDataService._percentile(vals, 50) == 2.0

    def test_percentile_90(self):
        vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        # index for 90 = 0.9*9 = 8.1 -> vals[8]*0.9 + vals[9]*0.1 = 9*0.9+10*0.1=9.1
        assert HistoricalDataService._percentile(vals, 90) == pytest.approx(9.1)

    def test_percentile_0_returns_min(self):
        # _percentile 接收已排序的值
        vals = [1.0, 3.0, 5.0, 8.0]
        assert HistoricalDataService._percentile(vals, 0) == 1.0

    def test_percentile_100_returns_max(self):
        vals = [1.0, 3.0, 5.0, 8.0]
        assert HistoricalDataService._percentile(vals, 100) == 8.0


# ══════════════════════════════════════════════════════════════════════
# _calculate_statistics
# ══════════════════════════════════════════════════════════════════════


class TestCalculateStatistics:
    """统计指标计算。"""

    def test_empty_values_returns_empty(self, svc):
        assert svc._calculate_statistics([], []) == {}

    def test_basic_aggregations(self, svc):
        """count/sum/mean/min/max。"""
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        data = [_point(f"t{i}", v) for i, v in enumerate(values)]
        stats = svc._calculate_statistics(values, data)
        assert stats["count"] == 5
        assert stats["sum"] == 15.0
        assert stats["mean"] == 3.0
        assert stats["min"] == 1.0
        assert stats["max"] == 5.0

    def test_variance_stddev(self, svc):
        """方差与标准差（样本方差 n-1）。"""
        values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
        data = [_point(f"t{i}", v) for i, v in enumerate(values)]
        stats = svc._calculate_statistics(values, data)
        # 样本方差（n-1）= 32/7 ~ 4.5714
        assert stats["variance"] == pytest.approx(4.571429, rel=1e-3)
        assert stats["stddev"] == pytest.approx(2.138, rel=1e-3)

    def test_single_value_no_variance(self, svc):
        """单值时方差/标准差为 0（n>1 才计算）。"""
        values = [5.0]
        data = [_point("t0", 5.0)]
        stats = svc._calculate_statistics(values, data)
        assert stats["variance"] == 0.0
        assert stats["stddev"] == 0.0

    def test_first_last_values_and_times(self, svc):
        values = [10.0, 20.0, 30.0]
        data = [_point("t0", 10.0), _point("t1", 20.0), _point("t2", 30.0)]
        stats = svc._calculate_statistics(values, data)
        assert stats["first_value"] == 10.0
        assert stats["last_value"] == 30.0
        assert stats["first_time"] == "t0"
        assert stats["last_time"] == "t2"

    def test_percentiles_in_result(self, svc):
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        data = [_point(f"t{i}", v) for i, v in enumerate(values)]
        stats = svc._calculate_statistics(values, data)
        assert "median" in stats
        assert "percentile_90" in stats
        assert "percentile_95" in stats
        assert "percentile_99" in stats
        assert stats["median"] == pytest.approx(5.5)

    def test_values_are_rounded(self, svc):
        """统计值应四舍五入到 6 位小数。"""
        values = [1.123456789, 2.987654321]
        data = [_point("t0", values[0]), _point("t1", values[1])]
        stats = svc._calculate_statistics(values, data)
        assert stats["sum"] == round(4.111111110, 6)
        assert stats["mean"] == round(2.055555555, 6)


# ══════════════════════════════════════════════════════════════════════
# query
# ══════════════════════════════════════════════════════════════════════


class TestQuery:
    """历史数据查询。"""

    async def test_basic_query_returns_data(self, svc, mock_influx):
        """基础查询应返回数据点与统计。"""
        mock_influx.query_points.return_value = _series(5, 1.0, 1.0)
        result = await svc.query("dev1", "temperature")
        assert result.device_id == "dev1"
        assert result.point_name == "temperature"
        assert result.count == 5
        assert len(result.data_points) == 5
        assert result.start_time == "-1h"
        assert "mean" in result.statistics
        assert result.query_ms >= 0

    async def test_query_passes_options_to_storage(self, svc, mock_influx):
        """查询选项应正确传递给 influx.query_points。"""
        opts = QueryOptions(start="-24h", stop="now", aggregate="5m", limit=500)
        await svc.query("dev1", "temp", opts)
        mock_influx.query_points.assert_awaited_once_with(
            device_id="dev1",
            point_name="temp",
            start="-24h",
            stop="now",
            aggregate="5m",
            max_points=500,
        )

    async def test_query_default_stop_is_none(self, svc, mock_influx):
        """stop 为空时应传 None。"""
        opts = QueryOptions(start="-1h", stop="")
        await svc.query("dev1", "temp", opts)
        assert mock_influx.query_points.call_args.kwargs["stop"] is None

    async def test_query_empty_data_no_statistics(self, svc, mock_influx):
        """无数据时统计为空。"""
        mock_influx.query_points.return_value = []
        result = await svc.query("dev1", "temp")
        assert result.count == 0
        assert result.statistics == {}

    async def test_query_filter_quality(self, svc, mock_influx):
        """filter_quality 应过滤非匹配质量的数据点。"""
        data = [
            _point("t0", 1.0, "good"),
            _point("t1", 2.0, "bad"),
            _point("t2", 3.0, "good"),
        ]
        mock_influx.query_points.return_value = data
        result = await svc.query("dev1", "temp", QueryOptions(filter_quality="good"))
        assert result.count == 2
        assert all(d["quality"] == "good" for d in result.data_points)

    async def test_query_filters_non_numeric_values(self, svc, mock_influx):
        """非数值 value 应被统计过滤（不抛异常）。"""
        data = [
            _point("t0", 1.0),
            _point("t1", "not_a_number"),
            _point("t2", None),
            _point("t3", 3.0),
        ]
        mock_influx.query_points.return_value = data
        result = await svc.query("dev1", "temp")
        assert result.count == 4  # data_points 保留全部
        assert result.statistics["count"] == 2  # 仅数值参与统计

    async def test_query_timeout_returns_empty(self, svc, mock_influx, monkeypatch):
        """InfluxDB 查询超时应返回空结果而非挂起。"""

        async def slow_query(**kwargs):
            await asyncio.sleep(60)
            return []

        mock_influx.query_points = slow_query

        async def fast_wait_for(coro, timeout):
            # 关闭未使用的协程，避免 "coroutine never awaited" 警告
            coro.close()
            raise TimeoutError

        monkeypatch.setattr(hd.asyncio, "wait_for", fast_wait_for)
        result = await svc.query("dev1", "temp")
        assert result.count == 0
        assert result.data_points == []

    async def test_query_uses_default_options_when_none(self, svc, mock_influx):
        """options=None 时应使用默认 QueryOptions。"""
        await svc.query("dev1", "temp", None)
        assert mock_influx.query_points.call_args.kwargs["start"] == "-1h"
        assert mock_influx.query_points.call_args.kwargs["max_points"] == 10000


# ══════════════════════════════════════════════════════════════════════
# query_multi_point
# ══════════════════════════════════════════════════════════════════════


class TestQueryMultiPoint:
    """多点并发查询。"""

    async def test_returns_results_per_point(self, svc, mock_influx):
        """每个点名应返回独立结果。"""
        mock_influx.query_points.return_value = _series(3, 1.0, 1.0)
        results = await svc.query_multi_point("dev1", ["temp", "humid"])
        assert set(results.keys()) == {"temp", "humid"}
        assert results["temp"].device_id == "dev1"
        assert results["humid"].point_name == "humid"

    async def test_partial_failure_skipped(self, svc, mock_influx, monkeypatch):
        """单个点查询异常应被跳过，不影响其他点。"""
        call_count = {"n": 0}

        original_query = svc.query

        async def patched_query(device_id, point_name, options=None):
            call_count["n"] += 1
            if point_name == "bad":
                raise RuntimeError("boom")
            return await original_query(device_id, point_name, options)

        monkeypatch.setattr(svc, "query", patched_query)
        mock_influx.query_points.return_value = _series(2)
        results = await svc.query_multi_point("dev1", ["good", "bad"])
        assert "good" in results
        assert "bad" not in results

    async def test_empty_point_list_returns_empty(self, svc):
        results = await svc.query_multi_point("dev1", [])
        assert results == {}

    async def test_concurrent_not_serial(self, svc, mock_influx):
        """多点查询应并发而非串行。"""
        import time

        async def slow_query_points(**kwargs):
            await asyncio.sleep(0.05)
            return _series(1)

        mock_influx.query_points = slow_query_points
        start = time.monotonic()
        await svc.query_multi_point("dev1", ["a", "b", "c"])
        elapsed = time.monotonic() - start
        assert elapsed < 0.12, f"multi-point not concurrent: {elapsed:.3f}s"


# ══════════════════════════════════════════════════════════════════════
# query_aggregated
# ══════════════════════════════════════════════════════════════════════


class TestQueryAggregated:
    """窗口聚合查询。"""

    async def test_returns_data_points_list(self, svc, mock_influx):
        mock_influx.query_points.return_value = _series(3, 10.0, 5.0)
        points = await svc.query_aggregated("dev1", "temp", window="1h")
        assert isinstance(points, list)
        assert len(points) == 3

    async def test_passes_window_as_aggregate(self, svc, mock_influx):
        await svc.query_aggregated("dev1", "temp", window="5m", start="-12h", stop="now")
        call_kwargs = mock_influx.query_points.call_args.kwargs
        assert call_kwargs["aggregate"] == "5m"
        assert call_kwargs["start"] == "-12h"
        assert call_kwargs["stop"] == "now"

    async def test_default_aggregation_is_mean(self, svc, mock_influx):
        await svc.query_aggregated("dev1", "temp", window="1h")
        # 默认 aggregation=MEAN，仅验证不抛异常且调用发生
        mock_influx.query_points.assert_awaited_once()

    async def test_custom_aggregation_type(self, svc, mock_influx):
        await svc.query_aggregated("dev1", "temp", window="1h", aggregation=AggregationType.MAX)
        mock_influx.query_points.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════
# export_data / _export_json / _export_csv
# ══════════════════════════════════════════════════════════════════════


class TestExport:
    """数据导出。"""

    async def test_export_json_valid(self, svc, mock_influx):
        mock_influx.query_points.return_value = _series(3, 1.0, 1.0)
        output = await svc.export_data("dev1", "temp", format="json")
        parsed = json.loads(output)
        assert parsed["device_id"] == "dev1"
        assert parsed["point_name"] == "temp"
        assert parsed["count"] == 3
        assert len(parsed["data_points"]) == 3
        assert "statistics" in parsed

    async def test_export_csv_has_header(self, svc, mock_influx):
        mock_influx.query_points.return_value = _series(3, 1.0, 1.0)
        output = await svc.export_data("dev1", "temp", format="csv")
        first_line = output.strip().splitlines()[0]
        assert "time" in first_line
        assert "value" in first_line
        assert "quality" in first_line

    async def test_export_csv_rows_count(self, svc, mock_influx):
        mock_influx.query_points.return_value = _series(5, 1.0, 1.0)
        output = await svc.export_data("dev1", "temp", format="csv")
        lines = output.strip().splitlines()
        assert len(lines) == 6  # 1 header + 5 rows

    async def test_export_csv_empty_returns_empty_string(self, svc, mock_influx):
        mock_influx.query_points.return_value = []
        output = await svc.export_data("dev1", "temp", format="csv")
        assert output == ""

    def test_export_json_includes_all_fields(self, svc):
        """_export_json 应包含全部导出字段。"""
        result = QueryResult(
            device_id="dev1",
            point_name="temp",
            start_time="-1h",
            end_time="now",
            count=2,
            data_points=[_point("t0", 1.0)],
            statistics={"mean": 1.0},
        )
        out = svc._export_json(result)
        parsed = json.loads(out)
        for key in ["device_id", "point_name", "start_time", "end_time", "count", "statistics", "data_points"]:
            assert key in parsed

    def test_export_csv_uses_safe_defaults(self, svc):
        """缺失 value/quality 的数据点应使用空字符串而非抛异常。"""
        result = QueryResult(
            device_id="dev1",
            point_name="temp",
            data_points=[{"time": "t0"}],  # 无 value/quality
        )
        out = svc._export_csv(result)
        lines = out.strip().splitlines()
        assert len(lines) == 2
        row = lines[1].split(",")
        assert row[0] == "t0"

    async def test_export_json_ensure_ascii_false(self, svc, mock_influx):
        """JSON 导出应保留 Unicode（ensure_ascii=False）。"""
        mock_influx.query_points.return_value = _series(1)
        output = await svc.export_data("dev1", "temp", format="json")
        # ensure_ascii=False 时中文等字符不转义；这里验证导出可解析
        assert json.loads(output) is not None

    async def test_export_default_format_is_json(self, svc, mock_influx):
        mock_influx.query_points.return_value = _series(1)
        output = await svc.export_data("dev1", "temp")
        assert json.loads(output) is not None


# ══════════════════════════════════════════════════════════════════════
# query_trend
# ══════════════════════════════════════════════════════════════════════


class TestQueryTrend:
    """趋势分析（线性回归）。"""

    async def test_increasing_trend(self, svc, mock_influx):
        """单调递增序列应判定为 increasing，slope > 0。"""
        mock_influx.query_points.return_value = _series(10, 10.0, 2.0)
        result = await svc.query_trend("dev1", "temp")
        assert result["trend"] == "increasing"
        assert result["slope"] > 0
        assert result["data_points"] == 10

    async def test_decreasing_trend(self, svc, mock_influx):
        """单调递减序列应判定为 decreasing，slope < 0。"""
        data = [_point(f"t{i}", 100.0 - i * 5.0) for i in range(10)]
        mock_influx.query_points.return_value = data
        result = await svc.query_trend("dev1", "temp")
        assert result["trend"] == "decreasing"
        assert result["slope"] < 0

    async def test_stable_trend(self, svc, mock_influx):
        """恒定序列应判定为 stable（|slope| < 0.001）。"""
        mock_influx.query_points.return_value = _series(10, 50.0, 0.0)
        result = await svc.query_trend("dev1", "temp")
        assert result["trend"] == "stable"
        assert result["slope"] == 0

    async def test_unknown_when_no_data(self, svc, mock_influx):
        mock_influx.query_points.return_value = []
        result = await svc.query_trend("dev1", "temp")
        assert result["trend"] == "unknown"
        assert result["slope"] == 0
        assert result["data"] == []

    async def test_insufficient_data_single_point(self, svc, mock_influx):
        """仅一个数据点应判定为 insufficient_data。"""
        mock_influx.query_points.return_value = _series(1, 5.0)
        result = await svc.query_trend("dev1", "temp")
        assert result["trend"] == "insufficient_data"
        assert result["slope"] == 0

    async def test_trend_ignores_none_values(self, svc, mock_influx):
        """None value 应被安全过滤，不参与回归。"""
        data = [_point("t0", 1.0), _point("t1", None), _point("t2", 3.0)]
        mock_influx.query_points.return_value = data
        result = await svc.query_trend("dev1", "temp")
        assert result["trend"] == "increasing"

    async def test_trend_passes_bucket_size(self, svc, mock_influx, monkeypatch):
        """bucket_size 应作为 window 传递给 query_aggregated。"""
        captured = {}

        async def fake_query_aggregated(device_id, point_name, window, aggregation, start, stop):
            captured["window"] = window
            return _series(3)

        monkeypatch.setattr(svc, "query_aggregated", fake_query_aggregated)
        await svc.query_trend("dev1", "temp", bucket_size="30m")
        assert captured["window"] == "30m"


# ══════════════════════════════════════════════════════════════════════
# query_correlation
# ══════════════════════════════════════════════════════════════════════


class TestQueryCorrelation:
    """皮尔逊相关性分析。"""

    async def test_strong_positive_correlation(self, svc, mock_influx):
        """完全正相关序列 correlation ≈ 1.0，强度 strong。"""
        data = _series(10, 1.0, 1.0)
        # 两个点使用相同序列 -> 完全正相关
        mock_influx.query_points.return_value = data
        result = await svc.query_correlation("dev1", "p1", "p2")
        assert result["correlation"] == pytest.approx(1.0, abs=1e-3)
        assert "strong" in result["interpretation"]
        assert "positive" in result["interpretation"]
        assert result["common_points"] == 10

    async def test_strong_negative_correlation(self, svc, mock_influx):
        """完全负相关序列 correlation ≈ -1.0。"""
        data1 = _series(10, 1.0, 1.0)
        data2 = [_point(f"2026-07-10T10:{i:02d}:00Z", 100.0 - i * 1.0) for i in range(10)]
        # query 依次为 point1, point2
        mock_influx.query_points.side_effect = [data1, data2]
        result = await svc.query_correlation("dev1", "p1", "p2")
        assert result["correlation"] == pytest.approx(-1.0, abs=1e-3)
        assert "negative" in result["interpretation"]

    async def test_insufficient_data_when_empty(self, svc, mock_influx):
        mock_influx.query_points.return_value = []
        result = await svc.query_correlation("dev1", "p1", "p2")
        assert result["correlation"] is None
        assert result["reason"] == "insufficient_data"

    async def test_insufficient_common_points(self, svc, mock_influx):
        """时间戳不重合应返回 insufficient_common_points。"""
        data1 = _series(3, 1.0, 1.0)  # t00, t01, t02
        data2 = [
            _point("2026-07-10T11:00:00Z", 1.0),
            _point("2026-07-10T11:01:00Z", 2.0),
            _point("2026-07-10T11:02:00Z", 3.0),
        ]
        mock_influx.query_points.side_effect = [data1, data2]
        result = await svc.query_correlation("dev1", "p1", "p2")
        assert result["correlation"] is None
        assert result["reason"] == "insufficient_common_points"

    async def test_too_few_common_points(self, svc, mock_influx):
        """少于 3 个公共时间戳应返回 insufficient_common_points。"""
        data1 = [_point("t0", 1.0), _point("t1", 2.0), _point("t2", 3.0)]
        data2 = [_point("t0", 2.0), _point("t1", 4.0), _point("tx", 9.0)]
        mock_influx.query_points.side_effect = [data1, data2]
        result = await svc.query_correlation("dev1", "p1", "p2")
        assert result["correlation"] is None
        assert result["reason"] == "insufficient_common_points"

    async def test_duplicate_timestamps_averaged(self, svc, mock_influx):
        """相同时间戳应取均值避免覆盖。"""
        data1 = [
            _point("t0", 1.0),
            _point("t0", 3.0),  # 均值 2.0
            _point("t1", 2.0),
            _point("t1", 4.0),  # 均值 3.0
            _point("t2", 3.0),
            _point("t2", 5.0),  # 均值 4.0
        ]
        data2 = [
            _point("t0", 2.0),
            _point("t1", 3.0),
            _point("t2", 4.0),
        ]
        mock_influx.query_points.side_effect = [data1, data2]
        result = await svc.query_correlation("dev1", "p1", "p2")
        # 完全相关
        assert result["correlation"] == pytest.approx(1.0, abs=1e-3)

    async def test_correlation_rounded_to_4_decimals(self, svc, mock_influx):
        mock_influx.query_points.return_value = _series(10, 1.0, 1.0)
        result = await svc.query_correlation("dev1", "p1", "p2")
        # 1.0 经 round(...,4) 仍为 1.0
        assert result["correlation"] == round(result["correlation"], 4)


# ══════════════════════════════════════════════════════════════════════
# DeviceShadowService
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def shadow_svc(mock_influx):
    return DeviceShadowService(mock_influx)


class TestDeviceShadowGet:
    """设备影子获取。"""

    async def test_get_shadow_from_influx(self, shadow_svc, mock_influx):
        """缓存未命中时应从 influx 查询并构建影子。"""
        mock_influx.query_latest.return_value = {
            "temp": {"value": 25.5, "time": "t0", "quality": "good"},
            "humid": {"value": 60.0, "time": "t0", "quality": "good"},
        }
        shadow = await shadow_svc.get_shadow("dev1")
        assert shadow["device_id"] == "dev1"
        assert shadow["state"]["reported"]["temp"] == 25.5
        assert shadow["state"]["reported"]["humid"] == 60.0
        assert shadow["state"]["metadata"]["temp"]["quality"] == "good"
        assert shadow["version"] == 1

    async def test_get_shadow_caches_after_query(self, shadow_svc, mock_influx):
        """首次查询后应缓存，第二次不再访问 influx。"""
        mock_influx.query_latest.return_value = {"temp": {"value": 1.0, "time": "t0"}}
        await shadow_svc.get_shadow("dev1")
        await shadow_svc.get_shadow("dev1")
        assert mock_influx.query_latest.await_count == 1

    async def test_get_shadow_returns_none_when_no_data(self, shadow_svc, mock_influx):
        mock_influx.query_latest.return_value = {}
        assert await shadow_svc.get_shadow("dev1") is None

    async def test_get_shadow_metadata_unknown_quality(self, shadow_svc, mock_influx):
        """无 quality 字段时应默认 'unknown'。"""
        mock_influx.query_latest.return_value = {"temp": {"value": 1.0, "time": "t0"}}
        shadow = await shadow_svc.get_shadow("dev1")
        assert shadow["state"]["metadata"]["temp"]["quality"] == "unknown"


class TestDeviceShadowUpdate:
    """设备影子更新与回调。"""

    async def test_update_reported_state_creates_shadow(self, shadow_svc):
        """无缓存时更新应创建新影子。"""
        shadow = await shadow_svc.update_reported_state("dev1", {"temp": 25.0})
        assert shadow["state"]["reported"]["temp"] == 25.0
        assert shadow["version"] == 1
        assert shadow["state"]["metadata"]["temp"]["quality"] == "reported"

    async def test_update_reported_increments_version(self, shadow_svc):
        """多次更新应递增 version。"""
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        shadow = await shadow_svc.update_reported_state("dev1", {"temp": 2.0})
        assert shadow["version"] == 2
        assert shadow["state"]["reported"]["temp"] == 2.0

    async def test_update_reported_triggers_callback(self, shadow_svc):
        """更新应触发已注册的回调。"""
        called = []

        async def cb(device_id, shadow):
            called.append((device_id, shadow["version"]))

        await shadow_svc.register_update_callback(cb)
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        assert len(called) == 1
        assert called[0][0] == "dev1"
        assert called[0][1] == 1

    async def test_callback_receives_deepcopy(self, shadow_svc):
        """回调应收到影子深拷贝，修改不影响内部状态。"""
        received = []

        async def cb(device_id, shadow):
            shadow["state"]["reported"]["injected"] = True
            received.append(shadow)

        await shadow_svc.register_update_callback(cb)
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        # 内部影子不应被污染
        internal = await shadow_svc.get_shadow("dev1")
        assert "injected" not in internal["state"]["reported"]

    async def test_callback_exception_swallowed(self, shadow_svc):
        """回调异常不应影响更新返回值。"""

        async def bad_cb(device_id, shadow):
            raise RuntimeError("cb fail")

        sync_called = []

        def sync_cb(device_id, shadow):
            sync_called.append(device_id)

        await shadow_svc.register_update_callback(bad_cb)
        await shadow_svc.register_update_callback(sync_cb)
        shadow = await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        assert shadow["version"] == 1
        assert len(sync_called) == 1

    async def test_sync_callback_supported(self, shadow_svc):
        """同步回调也应被支持。"""
        called = []

        def sync_cb(device_id, shadow):
            called.append(device_id)

        await shadow_svc.register_update_callback(sync_cb)
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        assert called == ["dev1"]

    async def test_unregister_callback(self, shadow_svc):
        called = []

        async def cb(device_id, shadow):
            called.append(device_id)

        await shadow_svc.register_update_callback(cb)
        await shadow_svc.unregister_update_callback(cb)
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        assert called == []

    async def test_update_desired_state(self, shadow_svc):
        """更新 desired 状态应合并到影子。"""
        shadow = await shadow_svc.update_desired_state("dev1", {"setpoint": 50.0})
        assert shadow["state"]["desired"]["setpoint"] == 50.0
        assert shadow["version"] == 1

    async def test_update_desired_increments_version(self, shadow_svc):
        await shadow_svc.update_desired_state("dev1", {"a": 1.0})
        shadow = await shadow_svc.update_desired_state("dev1", {"b": 2.0})
        assert shadow["version"] == 2
        assert shadow["state"]["desired"]["a"] == 1.0
        assert shadow["state"]["desired"]["b"] == 2.0


class TestDeviceShadowDelta:
    """reported/desired 差异计算。"""

    async def test_get_delta_when_mismatch(self, shadow_svc):
        """reported 与 desired 不一致时应返回 delta。"""
        await shadow_svc.update_reported_state("dev1", {"temp": 20.0})
        await shadow_svc.update_desired_state("dev1", {"temp": 25.0})
        delta = await shadow_svc.get_delta("dev1")
        assert delta is not None
        assert delta["temp"]["desired"] == 25.0
        assert delta["temp"]["reported"] == 20.0

    async def test_get_delta_none_when_matched(self, shadow_svc):
        """reported 与 desired 一致时应返回 None。"""
        await shadow_svc.update_reported_state("dev1", {"temp": 25.0})
        await shadow_svc.update_desired_state("dev1", {"temp": 25.0})
        assert await shadow_svc.get_delta("dev1") is None

    async def test_get_delta_none_when_no_shadow(self, shadow_svc, mock_influx):
        mock_influx.query_latest.return_value = {}
        assert await shadow_svc.get_delta("dev1") is None


class TestOfflineCommands:
    """离线命令缓存。"""

    async def test_cache_and_get_commands(self, shadow_svc):
        cmd = {"action": "reboot"}
        await shadow_svc.cache_offline_command("dev1", cmd)
        pending = await shadow_svc.get_pending_commands("dev1")
        assert len(pending) == 1
        assert pending[0]["command"] == cmd
        assert pending[0]["retries"] == 0
        assert "timestamp" in pending[0]

    async def test_get_pending_empty_for_unknown_device(self, shadow_svc):
        pending = await shadow_svc.get_pending_commands("unknown")
        assert pending == []

    async def test_clear_pending_returns_count(self, shadow_svc):
        await shadow_svc.cache_offline_command("dev1", {"a": 1})
        await shadow_svc.cache_offline_command("dev1", {"b": 2})
        count = await shadow_svc.clear_pending_commands("dev1")
        assert count == 2
        assert await shadow_svc.get_pending_commands("dev1") == []

    async def test_clear_pending_zero_for_unknown(self, shadow_svc):
        assert await shadow_svc.clear_pending_commands("unknown") == 0

    async def test_cache_enforces_size_limit(self, shadow_svc, monkeypatch):
        """超出每设备上限应丢弃最旧命令。"""
        monkeypatch.setattr(shadow_svc, "_max_offline_commands_per_device", 3)
        for i in range(5):
            await shadow_svc.cache_offline_command("dev1", {"i": i})
        pending = await shadow_svc.get_pending_commands("dev1")
        assert len(pending) == 3
        # 应保留最新的 3 个（i=2,3,4）
        assert [p["command"]["i"] for p in pending] == [2, 3, 4]


class TestShadowCacheManagement:
    """影子缓存管理与 LRU 淘汰。"""

    async def test_get_all_shadows(self, shadow_svc):
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        await shadow_svc.update_reported_state("dev2", {"temp": 2.0})
        all_shadows = await shadow_svc.get_all_shadows()
        assert len(all_shadows) == 2
        ids = {s["device_id"] for s in all_shadows}
        assert ids == {"dev1", "dev2"}

    async def test_clear_shadow_existing(self, shadow_svc):
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        assert await shadow_svc.clear_shadow("dev1") is True
        assert await shadow_svc.get_all_shadows() == []

    async def test_clear_shadow_nonexistent(self, shadow_svc):
        assert await shadow_svc.clear_shadow("unknown") is False

    async def test_lru_eviction_when_full(self, shadow_svc, monkeypatch):
        """超过 _MAX_SHADOW_DEVICES 应淘汰最久未活跃设备。"""
        monkeypatch.setattr(DeviceShadowService, "_MAX_SHADOW_DEVICES", 2)
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        await shadow_svc.update_reported_state("dev2", {"temp": 2.0})
        await shadow_svc.update_reported_state("dev3", {"temp": 3.0})
        all_shadows = await shadow_svc.get_all_shadows()
        ids = {s["device_id"] for s in all_shadows}
        # dev1 应被淘汰
        assert "dev1" not in ids
        assert "dev2" in ids
        assert "dev3" in ids

    async def test_update_reported_touches_lru_order(self, shadow_svc, monkeypatch):
        """写入影子应更新 LRU 顺序，使最近写入的设备避免被淘汰。"""
        monkeypatch.setattr(DeviceShadowService, "_MAX_SHADOW_DEVICES", 2)
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        await shadow_svc.update_reported_state("dev2", {"temp": 2.0})
        # 再次更新 dev1，使其变为最近使用（move_to_end）
        await shadow_svc.update_reported_state("dev1", {"temp": 1.5})
        await shadow_svc.update_reported_state("dev3", {"temp": 3.0})
        all_shadows = await shadow_svc.get_all_shadows()
        ids = {s["device_id"] for s in all_shadows}
        # dev2 现在是最久未使用，应被淘汰
        assert "dev1" in ids
        assert "dev3" in ids
        assert "dev2" not in ids

    async def test_get_shadow_cache_hit_does_not_touch_lru(self, shadow_svc, monkeypatch):
        """缓存命中读取不更新 LRU 顺序（与写入行为区分）。"""
        monkeypatch.setattr(DeviceShadowService, "_MAX_SHADOW_DEVICES", 2)
        await shadow_svc.update_reported_state("dev1", {"temp": 1.0})
        await shadow_svc.update_reported_state("dev2", {"temp": 2.0})
        # 缓存命中读取 dev1，不改变 LRU 顺序
        await shadow_svc.get_shadow("dev1")
        await shadow_svc.update_reported_state("dev3", {"temp": 3.0})
        all_shadows = await shadow_svc.get_all_shadows()
        ids = {s["device_id"] for s in all_shadows}
        # dev1 仍是最久未使用（读未刷新），应被淘汰
        assert "dev2" in ids
        assert "dev3" in ids
        assert "dev1" not in ids


# ══════════════════════════════════════════════════════════════════════
# 单例获取函数
# ══════════════════════════════════════════════════════════════════════


class TestSingletonGetters:
    """get_historical_service / get_shadow_service 单例行为。"""

    def test_get_historical_service_creates_singleton(self, mock_influx):
        svc1 = hd.get_historical_service(mock_influx)
        assert svc1 is not None
        svc2 = hd.get_historical_service(mock_influx)
        assert svc1 is svc2

    def test_get_shadow_service_creates_singleton(self, mock_influx):
        s1 = hd.get_shadow_service(mock_influx)
        assert s1 is not None
        s2 = hd.get_shadow_service(mock_influx)
        assert s1 is s2

    def test_get_historical_service_none_when_no_storage(self):
        """无 influx_storage 且 _app_state 无该属性时应返回 None。"""
        # _app_state 在 conftest 中被隔离，默认无 influx_storage 属性
        assert hd.get_historical_service() is None

    def test_get_shadow_service_none_when_no_storage(self):
        assert hd.get_shadow_service() is None

    def test_get_historical_service_uses_app_state_storage(self, mock_influx):
        """无显式 storage 时应从 _app_state.influx_storage 获取。"""
        from edgelite import app as app_module

        app_module._app_state.influx_storage = mock_influx
        try:
            svc = hd.get_historical_service()
            assert svc is not None
            assert isinstance(svc, HistoricalDataService)
        finally:
            del app_module._app_state.influx_storage
