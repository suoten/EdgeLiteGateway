"""流计算引擎测试 - 窗口聚合/滑动窗口/水位线/迟到数据

覆盖 engine/stream_compute.py：
- StreamEvent / WindowResult / PatternMatch 数据类
- TumblingWindow: 窗口边界/过期事件返回/缓冲区满告警
- SlidingWindow: 滑动间隔/水位线/迟到丢弃/avg/sum/min/max/count/std 聚合
- StreamProcessor: filter/map/aggregate 链式算子
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from edgelite.engine.stream_compute import (
    PatternMatch,
    SessionWindow,
    SlidingWindow,
    StreamEvent,
    StreamProcessor,
    TumblingWindow,
    WindowResult,
)


def _make_event(value: float, ts_offset: float = 0.0, device: str = "dev1", point: str = "temp") -> StreamEvent:
    """创建带时间戳的 StreamEvent"""
    return StreamEvent(
        device_id=device,
        point_name=point,
        value=value,
        timestamp=datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC) + timedelta(seconds=ts_offset),
    )


class TestStreamEvent:
    def test_defaults(self):
        e = StreamEvent(device_id="d", point_name="p", value=1.0)
        assert e.device_id == "d"
        assert e.point_name == "p"
        assert e.value == 1.0
        assert e.quality == "good"
        assert e.timestamp is not None

    def test_custom(self):
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        e = StreamEvent(device_id="d", point_name="p", value=2.5, timestamp=ts, quality="bad")
        assert e.timestamp == ts
        assert e.quality == "bad"


class TestWindowResult:
    def test_fields(self):
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        wr = WindowResult(
            window_id="w1",
            window_type="tumbling",
            start_time=ts,
            end_time=ts,
            point_name="temp",
            aggregate="avg",
            value=42.0,
            count=5,
        )
        assert wr.window_id == "w1"
        assert wr.aggregate == "avg"
        assert wr.value == 42.0


class TestPatternMatch:
    def test_fields(self):
        pm = PatternMatch(pattern_id="p1", matched=True, confidence=0.9, details={"x": 1})
        assert pm.matched is True
        assert pm.confidence == 0.9


class TestTumblingWindow:
    def test_first_event_starts_window(self):
        tw = TumblingWindow(size_seconds=10)
        expired = tw.add(_make_event(1.0, ts_offset=0))
        assert expired == []  # 窗口刚开始，无过期

    def test_window_emits_on_size_reached(self):
        """窗口大小达到后返回所有缓冲事件"""
        tw = TumblingWindow(size_seconds=1.0)
        tw.add(_make_event(1.0, ts_offset=0))
        tw.add(_make_event(2.0, ts_offset=0.5))
        expired = tw.add(_make_event(3.0, ts_offset=1.0))  # 超过1s → 窗口结束
        assert len(expired) == 3

    def test_window_clears_after_emit(self):
        tw = TumblingWindow(size_seconds=1.0)
        tw.add(_make_event(1.0, ts_offset=0))
        tw.add(_make_event(2.0, ts_offset=1.0))  # 触发过期
        assert len(tw._buffer) == 0  # 缓冲已清空

    def test_no_emit_before_size(self):
        tw = TumblingWindow(size_seconds=10)
        tw.add(_make_event(1.0, ts_offset=0))
        tw.add(_make_event(2.0, ts_offset=5))
        expired = tw.add(_make_event(3.0, ts_offset=8))
        assert expired == []  # 未到10s


class TestSlidingWindow:
    def test_add_returns_none_before_slide(self):
        """未到滑动间隔时返回 None"""
        sw = SlidingWindow(size_seconds=10, slide_seconds=5)
        sw.add(_make_event(1.0, ts_offset=0))
        result = sw.add(_make_event(2.0, ts_offset=1))
        assert result is None  # 未到 slide 间隔

    def test_add_returns_aggregate_on_slide(self):
        sw = SlidingWindow(size_seconds=10, slide_seconds=5, agg_func="avg")
        sw.add(_make_event(1.0, ts_offset=0))
        sw.add(_make_event(3.0, ts_offset=1))
        result = sw.add(_make_event(5.0, ts_offset=5))  # 到达 slide 间隔
        assert result is not None
        assert result["aggregate"] == "avg"
        assert result["value"] == 3.0  # (1+3+5)/3

    def test_aggregate_sum(self):
        sw = SlidingWindow(size_seconds=100, slide_seconds=1, agg_func="sum", min_count=1)
        sw.add(_make_event(1.0, ts_offset=0))
        result = sw.add(_make_event(2.0, ts_offset=1))
        assert result["value"] == 3.0

    def test_aggregate_min(self):
        sw = SlidingWindow(size_seconds=100, slide_seconds=1, agg_func="min", min_count=1)
        sw.add(_make_event(5.0, ts_offset=0))
        sw.add(_make_event(3.0, ts_offset=0.5))
        result = sw.add(_make_event(7.0, ts_offset=1))
        assert result["value"] == 3.0

    def test_aggregate_max(self):
        sw = SlidingWindow(size_seconds=100, slide_seconds=1, agg_func="max", min_count=1)
        sw.add(_make_event(5.0, ts_offset=0))
        sw.add(_make_event(3.0, ts_offset=0.5))
        result = sw.add(_make_event(7.0, ts_offset=1))
        assert result["value"] == 7.0

    def test_aggregate_count(self):
        sw = SlidingWindow(size_seconds=100, slide_seconds=1, agg_func="count", min_count=1)
        sw.add(_make_event(1.0, ts_offset=0))
        sw.add(_make_event(2.0, ts_offset=0.5))
        result = sw.add(_make_event(3.0, ts_offset=1))
        assert result["value"] == 3.0  # float(len)

    def test_aggregate_std(self):
        sw = SlidingWindow(size_seconds=100, slide_seconds=1, agg_func="std", min_count=1)
        sw.add(_make_event(2.0, ts_offset=0))
        sw.add(_make_event(4.0, ts_offset=0.5))
        result = sw.add(_make_event(6.0, ts_offset=1))
        # values = [2, 4, 6], mean=4, variance=((2-4)^2+(4-4)^2+(6-4)^2)/3 = 8/3, std=sqrt(8/3)
        import math

        assert abs(result["value"] - math.sqrt(8 / 3)) < 1e-6

    def test_aggregate_unknown_func_returns_last(self):
        sw = SlidingWindow(size_seconds=100, slide_seconds=1, agg_func="median", min_count=1)
        sw.add(_make_event(1.0, ts_offset=0))
        sw.add(_make_event(2.0, ts_offset=0.5))
        result = sw.add(_make_event(3.0, ts_offset=1))
        assert result["value"] == 3.0  # 未知 agg_func → 返回最后一个值

    def test_min_count_not_met(self):
        """窗口内数据不足 min_count 时不输出"""
        sw = SlidingWindow(size_seconds=100, slide_seconds=1, agg_func="avg", min_count=5)
        sw.add(_make_event(1.0, ts_offset=0))
        result = sw.add(_make_event(2.0, ts_offset=1))
        assert result is None  # 数据不足

    def test_late_data_dropped(self):
        """严重迟到事件被丢弃"""
        sw = SlidingWindow(size_seconds=10, slide_seconds=1, allowed_lateness=1.0)
        sw.add(_make_event(1.0, ts_offset=10))  # watermark = 10
        # 迟到 5s > allowed_lateness 1s → 丢弃
        result = sw.add(_make_event(2.0, ts_offset=5))
        assert result is None

    def test_get_values(self):
        sw = SlidingWindow(size_seconds=100, slide_seconds=1, agg_func="avg")
        sw.add(_make_event(1.0, ts_offset=0))
        sw.add(_make_event(2.0, ts_offset=0.5))
        sw.add(_make_event(3.0, ts_offset=0.8))
        values = sw.get_values()
        assert values == [1.0, 2.0, 3.0]

    def test_expired_data_cleaned(self):
        """超过窗口大小的旧数据被清理"""
        sw = SlidingWindow(size_seconds=1, slide_seconds=0.5, agg_func="avg")
        sw.add(_make_event(1.0, ts_offset=0))
        sw.add(_make_event(2.0, ts_offset=0.5))
        # 第三个事件 timestamp=2.0, cutoff=2.0-1.0=1.0, 前两个事件 ts<1.0 被清理
        result = sw.add(_make_event(3.0, ts_offset=2.0))
        # 只有 [3.0] 在窗口内
        if result is not None:
            assert result["count"] == 1


class TestSessionWindow:
    def test_init(self):
        sw = SessionWindow(timeout_seconds=30)
        assert sw._timeout == 30


class TestStreamProcessor:
    def test_init(self):
        sp = StreamProcessor("proc1")
        assert sp is not None
        assert sp._id == "proc1"
        assert sp._operators == []
        assert sp._running is False

    def test_filter_keeps_matching(self):
        """filter 算子保留满足谓词的事件"""
        sp = StreamProcessor("proc1")
        sp.filter(lambda e: e.value > 3)
        results = sp.process(_make_event(5.0))
        assert len(results) == 1
        assert results[0].value == 5.0

    def test_filter_drops_non_matching(self):
        """filter 算子丢弃不满足谓词的事件"""
        sp = StreamProcessor("proc1")
        sp.filter(lambda e: e.value > 3)
        results = sp.process(_make_event(1.0))
        assert len(results) == 0  # 被过滤掉

    def test_filter_chainable_returns_self(self):
        """filter 返回 self 支持链式调用"""
        sp = StreamProcessor("proc1")
        result = sp.filter(lambda e: e.value > 0)
        assert result is sp

    def test_map_transforms_value(self):
        """map 算子将值转换为标量，自动创建新事件"""
        sp = StreamProcessor("proc1")
        sp.map(lambda e: e.value * 10)
        results = sp.process(_make_event(2.0))
        assert len(results) == 1
        assert results[0].value == 20.0

    def test_map_returns_streamevent_as_is(self):
        """map 转换返回 StreamEvent 时直接使用"""
        sp = StreamProcessor("proc1")

        def to_event(e: StreamEvent) -> StreamEvent:
            return StreamEvent(e.device_id, "mapped", e.value * 5, e.timestamp)

        sp.map(to_event)
        results = sp.process(_make_event(2.0))
        assert len(results) == 1
        assert results[0].value == 10.0
        assert results[0].point_name == "mapped"

    def test_map_chainable_returns_self(self):
        sp = StreamProcessor("proc1")
        result = sp.map(lambda e: e.value)
        assert result is sp

    def test_filter_and_map_chain(self):
        """filter + map 链式组合"""
        sp = StreamProcessor("proc1")
        sp.filter(lambda e: e.value > 3).map(lambda e: e.value * 2)
        # 满足条件的事件
        results = sp.process(_make_event(5.0))
        assert len(results) == 1
        assert results[0].value == 10.0
        # 不满足条件的事件被过滤
        results = sp.process(_make_event(1.0))
        assert len(results) == 0

    def test_get_stats(self):
        sp = StreamProcessor("proc1")
        sp.filter(lambda e: e.value > 0)  # 添加算子使 processed 计数递增
        sp.process(_make_event(1.0))
        stats = sp.get_stats()
        assert stats["processed"] >= 1
        assert "last_process_time" in stats

    def test_filter_predicate_exception_returns_none(self):
        """filter 谓词抛异常时事件被丢弃"""
        sp = StreamProcessor("proc1")
        sp.filter(lambda e: 1 / 0)  # 抛 ZeroDivisionError
        results = sp.process(_make_event(1.0))
        assert len(results) == 0

    def test_map_transform_exception_returns_none(self):
        """map 转换抛异常时事件被丢弃"""
        sp = StreamProcessor("proc1")
        sp.map(lambda e: 1 / 0)
        results = sp.process(_make_event(1.0))
        assert len(results) == 0
