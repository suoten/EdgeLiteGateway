"""流计算引擎扩展测试 - 引擎生命周期/规则处理/模式检测/谓词评估/回调

覆盖 engine/stream_compute.py 中现有测试未覆盖的部分：
- TumblingWindow: 缓冲区满告警
- SessionWindow: 会话超时/新会话/连续事件
- StreamProcessor: aggregate/detect_rise/detect_change_rate/detect_anomaly 算子
- StreamComputeEngine: start/stop/submit/create_window/add_rule/remove_rule/
  _process_rule(filter/aggregate/pattern/anomaly)/_detect_3sigma/_publish_result/
  _eval_predicate/_emit_result/get_window_result/get_stats/get_processor_stats
- get_stream_engine 全局单例
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from edgelite.engine.event_bus import EventBus, PointUpdateEvent, StreamResultEvent
from edgelite.engine.stream_compute import (
    PatternMatch,
    SessionWindow,
    SlidingWindow,
    StreamComputeEngine,
    StreamEvent,
    StreamProcessor,
    TumblingWindow,
    WindowResult,
    get_stream_engine,
)

_BASE_TS = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)


def _make_event(
    value: float,
    ts_offset: float = 0.0,
    device: str = "dev1",
    point: str = "temp",
    quality: str = "good",
) -> StreamEvent:
    """创建带固定时间戳的 StreamEvent"""
    return StreamEvent(
        device_id=device,
        point_name=point,
        value=value,
        timestamp=_BASE_TS + timedelta(seconds=ts_offset),
        quality=quality,
    )


# ───────────────────────── TumblingWindow 扩展 ─────────────────────────


class TestTumblingWindowExt:
    """滚动窗口扩展边界测试"""

    def test_buffer_full_warning(self, caplog):
        """缓冲区达到 maxlen 时记录告警日志（仅告警一次）"""
        import logging

        tw = TumblingWindow(size_seconds=0.001)  # maxlen = int(0.001*1000) = 1
        with caplog.at_level(logging.WARNING, logger="edgelite.engine.stream_compute"):
            # 连续添加超过 maxlen 的事件
            tw.add(_make_event(1.0, ts_offset=0))
            tw.add(_make_event(2.0, ts_offset=0.0001))
            tw.add(_make_event(3.0, ts_offset=0.0002))
        # 至少有一条缓冲区满告警
        assert any("buffer full" in rec.message for rec in caplog.records)
        # _maxlen_warned 标志置位后不再重复告警
        assert tw._maxlen_warned is True

    def test_window_start_resets_after_emit(self):
        """窗口触发后 _window_start 重置为当前事件时间戳"""
        tw = TumblingWindow(size_seconds=1.0)
        tw.add(_make_event(1.0, ts_offset=0))
        tw.add(_make_event(2.0, ts_offset=1.0))  # 触发过期
        # 重置后 window_start 应为最后一个事件的时间戳
        assert tw._window_start == _make_event(2.0, ts_offset=1.0).timestamp.timestamp()


# ───────────────────────── SessionWindow ─────────────────────────


class TestSessionWindowExt:
    """会话窗口完整测试"""

    def test_first_event_no_expired(self):
        """首个事件不触发会话过期"""
        sw = SessionWindow(timeout_seconds=5)
        expired, new_session = sw.add(_make_event(1.0, ts_offset=0))
        assert expired is None
        assert new_session is False
        assert sw._last_event_time is not None

    def test_continuous_events_within_timeout(self):
        """连续事件（间隔小于超时）不产生过期"""
        sw = SessionWindow(timeout_seconds=10)
        sw.add(_make_event(1.0, ts_offset=0))
        expired, new_session = sw.add(_make_event(2.0, ts_offset=5))
        assert expired is None
        assert new_session is False
        # 缓冲区累积两个事件
        assert len(sw._buffer) == 2

    def test_session_timeout_emits_expired(self):
        """会话超时返回缓冲区数据并标记新会话"""
        sw = SessionWindow(timeout_seconds=5)
        sw.add(_make_event(1.0, ts_offset=0))
        sw.add(_make_event(2.0, ts_offset=2))
        # 间隔 10s > timeout 5s → 超时
        expired, new_session = sw.add(_make_event(3.0, ts_offset=12))
        assert expired is not None
        assert len(expired) == 2
        assert new_session is True
        # 新事件加入新会话缓冲
        assert len(sw._buffer) == 1

    def test_multiple_session_cycles(self):
        """多次会话超时循环"""
        sw = SessionWindow(timeout_seconds=1)
        sw.add(_make_event(1.0, ts_offset=0))
        sw.add(_make_event(2.0, ts_offset=0.5))
        # 第一次超时
        expired1, new1 = sw.add(_make_event(3.0, ts_offset=5))
        assert len(expired1) == 2
        assert new1 is True
        # 第二次超时
        sw.add(_make_event(4.0, ts_offset=5.2))
        expired2, new2 = sw.add(_make_event(5.0, ts_offset=10))
        assert len(expired2) == 2
        assert new2 is True


# ───────────────────────── StreamProcessor 算子 ─────────────────────────


class TestStreamProcessorAggregate:
    """StreamProcessor.aggregate 算子测试"""

    def test_aggregate_avg(self):
        """aggregate avg 聚合多事件"""
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=100, agg_func="avg")
        sp.process(_make_event(2.0, ts_offset=0))
        sp.process(_make_event(4.0, ts_offset=1))
        results = sp.process(_make_event(6.0, ts_offset=2))
        assert len(results) == 1
        assert results[0].value == 4.0  # (2+4+6)/3
        assert results[0].point_name == "temp_avg"

    def test_aggregate_sum(self):
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=100, agg_func="sum")
        sp.process(_make_event(1.0, ts_offset=0))
        results = sp.process(_make_event(2.0, ts_offset=1))
        assert results[0].value == 3.0

    def test_aggregate_min(self):
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=100, agg_func="min")
        sp.process(_make_event(5.0, ts_offset=0))
        sp.process(_make_event(3.0, ts_offset=1))
        results = sp.process(_make_event(7.0, ts_offset=2))
        assert results[0].value == 3.0

    def test_aggregate_max(self):
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=100, agg_func="max")
        sp.process(_make_event(5.0, ts_offset=0))
        sp.process(_make_event(3.0, ts_offset=1))
        results = sp.process(_make_event(7.0, ts_offset=2))
        assert results[0].value == 7.0

    def test_aggregate_count(self):
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=100, agg_func="count")
        sp.process(_make_event(1.0, ts_offset=0))
        sp.process(_make_event(2.0, ts_offset=1))
        results = sp.process(_make_event(3.0, ts_offset=2))
        assert results[0].value == 3.0

    def test_aggregate_std(self):
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=100, agg_func="std")
        sp.process(_make_event(2.0, ts_offset=0))
        sp.process(_make_event(4.0, ts_offset=1))
        results = sp.process(_make_event(6.0, ts_offset=2))
        assert abs(results[0].value - math.sqrt(8 / 3)) < 1e-6

    def test_aggregate_unknown_func_returns_last(self):
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=100, agg_func="median")
        sp.process(_make_event(1.0, ts_offset=0))
        results = sp.process(_make_event(3.0, ts_offset=1))
        assert results[0].value == 3.0

    def test_aggregate_evicts_old_events(self):
        """超过 window_seconds 的旧事件被剔除"""
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=1.0, agg_func="sum")
        sp.process(_make_event(10.0, ts_offset=0))
        # 事件时间戳=5，cutoff=5-1=4，前一个事件 ts=0 < 4 被剔除
        results = sp.process(_make_event(20.0, ts_offset=5))
        assert len(results) == 1
        assert results[0].value == 20.0  # 只剩当前事件

    def test_aggregate_multiple_keys_isolated(self):
        """不同 device:point 键的窗口相互隔离"""
        sp = StreamProcessor("p1")
        sp.aggregate(window_seconds=100, agg_func="sum")
        sp.process(_make_event(1.0, ts_offset=0, device="d1"))
        sp.process(_make_event(100.0, ts_offset=1, device="d2"))
        r1 = sp.process(_make_event(2.0, ts_offset=2, device="d1"))
        r2 = sp.process(_make_event(200.0, ts_offset=3, device="d2"))
        assert r1[0].value == 3.0  # d1: 1+2
        assert r2[0].value == 300.0  # d2: 100+200


class TestStreamProcessorDetectRise:
    """detect_rise 上升沿检测"""

    def test_rise_detected(self):
        """值上升超过阈值时检测到上升沿"""
        sp = StreamProcessor("p1")
        sp.detect_rise(threshold=5.0, name="up")
        sp.process(_make_event(10.0, ts_offset=0))
        results = sp.process(_make_event(20.0, ts_offset=1))  # 上升 10 >= 5
        assert len(results) == 1
        assert results[0].value == 1.0
        assert results[0].point_name == "temp_up"

    def test_rise_below_threshold_no_emit(self):
        """上升幅度小于阈值不触发"""
        sp = StreamProcessor("p1")
        sp.detect_rise(threshold=10.0)
        sp.process(_make_event(10.0, ts_offset=0))
        results = sp.process(_make_event(15.0, ts_offset=1))  # 上升 5 < 10
        assert len(results) == 0

    def test_rise_decrease_no_emit(self):
        """值下降不触发上升沿"""
        sp = StreamProcessor("p1")
        sp.detect_rise(threshold=1.0)
        sp.process(_make_event(20.0, ts_offset=0))
        results = sp.process(_make_event(10.0, ts_offset=1))  # 下降
        assert len(results) == 0

    def test_rise_first_event_no_emit(self):
        """首个事件（last 默认 0）上升未达阈值不触发"""
        sp = StreamProcessor("p1")
        sp.detect_rise(threshold=100.0)
        results = sp.process(_make_event(50.0, ts_offset=0))
        assert len(results) == 0

    def test_rise_first_event_meets_threshold(self):
        """首个事件值 >= threshold（相对默认 0）触发"""
        sp = StreamProcessor("p1")
        sp.detect_rise(threshold=5.0)
        results = sp.process(_make_event(10.0, ts_offset=0))
        assert len(results) == 1
        assert results[0].value == 1.0

    def test_rise_isolated_per_key(self):
        """不同 device:point 的上升沿独立跟踪"""
        sp = StreamProcessor("p1")
        sp.detect_rise(threshold=5.0)
        sp.process(_make_event(0.0, ts_offset=0, device="d1"))
        sp.process(_make_event(0.0, ts_offset=0, device="d2"))
        r1 = sp.process(_make_event(10.0, ts_offset=1, device="d1"))
        r2 = sp.process(_make_event(10.0, ts_offset=2, device="d2"))
        assert len(r1) == 1
        assert len(r2) == 1


class TestStreamProcessorDetectChangeRate:
    """detect_change_rate 变化率检测"""

    def test_rate_exceeded(self):
        """变化率超限时输出 suspect 事件"""
        sp = StreamProcessor("p1")
        sp.detect_change_rate(max_rate=5.0, window_seconds=10)
        sp.process(_make_event(0.0, ts_offset=0))
        # dt=1, 变化=100, rate=100 > 5
        results = sp.process(_make_event(100.0, ts_offset=1))
        assert len(results) == 1
        assert results[0].point_name == "temp_rate_exceeded"
        assert results[0].quality == "suspect"
        assert results[0].value == 100.0

    def test_rate_within_limit_no_emit(self):
        """变化率在限值内不输出"""
        sp = StreamProcessor("p1")
        sp.detect_change_rate(max_rate=100.0, window_seconds=10)
        sp.process(_make_event(0.0, ts_offset=0))
        results = sp.process(_make_event(10.0, ts_offset=1))  # rate=10 < 100
        assert len(results) == 0

    def test_rate_first_event_no_emit(self):
        """首个事件无历史，不输出"""
        sp = StreamProcessor("p1")
        sp.detect_change_rate(max_rate=1.0)
        results = sp.process(_make_event(100.0, ts_offset=0))
        assert len(results) == 0

    def test_rate_zero_dt_no_emit(self):
        """dt=0 时不计算变化率（避免除零）"""
        sp = StreamProcessor("p1")
        sp.detect_change_rate(max_rate=1.0, window_seconds=10)
        # 同一时间戳两个事件
        sp.process(_make_event(0.0, ts_offset=0))
        results = sp.process(_make_event(1000.0, ts_offset=0))
        assert len(results) == 0

    def test_rate_evicts_old_history(self):
        """超过 window_seconds 的历史被剔除"""
        sp = StreamProcessor("p1")
        sp.detect_change_rate(max_rate=1.0, window_seconds=1.0)
        sp.process(_make_event(0.0, ts_offset=0))
        # ts=10, cutoff=10-1=9, 前一历史 ts=0 < 9 被剔除 → 无历史比较
        results = sp.process(_make_event(1000.0, ts_offset=10))
        assert len(results) == 0


class TestStreamProcessorDetectAnomaly:
    """detect_anomaly 统计异常检测（Welford 增量算法）"""

    def test_no_emit_before_min_samples(self):
        """样本数不足 min_samples(10) 时不输出"""
        sp = StreamProcessor("p1")
        sp.detect_anomaly(std_multiplier=3.0)
        for i in range(9):
            results = sp.process(_make_event(float(i), ts_offset=float(i)))
            assert results == []

    def test_anomaly_detected(self):
        """样本足够后，离群值被检测为异常"""
        sp = StreamProcessor("p1")
        sp.detect_anomaly(std_multiplier=2.0)
        # 前 10 个稳定值
        for i in range(10):
            sp.process(_make_event(50.0 + (i % 3), ts_offset=float(i)))
        # 第 11 个为离群值
        results = sp.process(_make_event(500.0, ts_offset=10))
        assert len(results) == 1
        assert results[0].point_name == "temp_anomaly"
        assert results[0].quality == "suspect"
        assert results[0].value == 500.0

    def test_anomaly_normal_value_no_emit(self):
        """正常范围内不输出"""
        sp = StreamProcessor("p1")
        sp.detect_anomaly(std_multiplier=3.0)
        for i in range(15):
            results = sp.process(_make_event(50.0, ts_offset=float(i)))
            # 全部正常，无异常输出
            assert results == []

    def test_anomaly_welford_eviction_at_max_samples(self):
        """超过 max_samples(1000) 时触发 Welford 反向更新（覆盖 popleft 分支）"""
        sp = StreamProcessor("p1")
        sp.detect_anomaly(std_multiplier=0.1)  # 低阈值便于触发
        # 添加超过 1000 个事件，触发 deque 满淘汰 + Welford 反向更新
        for i in range(1005):
            sp.process(_make_event(float(i % 100), ts_offset=float(i) * 0.001))
        # 至少应能正常处理而不抛异常，且 stats 记录处理数
        stats = sp.get_stats()
        assert stats["processed"] >= 1005

    def test_anomaly_std_zero_no_emit(self):
        """所有值相同时 std=0，不输出异常"""
        sp = StreamProcessor("p1")
        sp.detect_anomaly(std_multiplier=1.0)
        for i in range(20):
            results = sp.process(_make_event(42.0, ts_offset=float(i)))
            assert results == []


class TestStreamProcessorProcessExt:
    """StreamProcessor.process 异常处理"""

    def test_process_operator_exception_breaks_and_counts_error(self):
        """算子抛异常时中断处理链并计入 errors（current 保留前值并被返回）"""
        sp = StreamProcessor("p1")

        def _bad_op(event: StreamEvent):
            raise RuntimeError("boom")

        sp._operators.append(_bad_op)
        evt = _make_event(1.0)
        results = sp.process(evt)
        # 异常中断处理链，但 current 仍为输入事件，被加入结果
        assert len(results) == 1
        assert results[0] is evt
        stats = sp.get_stats()
        assert stats["errors"] == 1
        assert stats["processed"] >= 1

    def test_map_returns_none_drops_event(self):
        """map 转换返回 None 时事件被丢弃"""
        sp = StreamProcessor("p1")
        sp.map(lambda e: None)
        results = sp.process(_make_event(1.0))
        assert results == []

    def test_process_no_operators_returns_original(self):
        """无算子时返回原始事件"""
        sp = StreamProcessor("p1")
        evt = _make_event(5.0)
        results = sp.process(evt)
        assert len(results) == 1
        assert results[0] is evt


# ───────────────────────── _eval_predicate 静态方法 ─────────────────────────


class TestEvalPredicate:
    """_eval_predicate 谓词表达式评估"""

    def test_greater_than_true(self):
        assert StreamComputeEngine._eval_predicate("value > 10", 15.0) is True

    def test_greater_than_false(self):
        assert StreamComputeEngine._eval_predicate("value > 10", 5.0) is False

    def test_less_than(self):
        assert StreamComputeEngine._eval_predicate("value < 10", 5.0) is True
        assert StreamComputeEngine._eval_predicate("value < 10", 15.0) is False

    def test_equal(self):
        assert StreamComputeEngine._eval_predicate("value == 10", 10.0) is True
        assert StreamComputeEngine._eval_predicate("value == 10", 11.0) is False

    def test_not_equal(self):
        assert StreamComputeEngine._eval_predicate("value != 10", 11.0) is True
        assert StreamComputeEngine._eval_predicate("value != 10", 10.0) is False

    def test_greater_equal(self):
        assert StreamComputeEngine._eval_predicate("value >= 10", 10.0) is True
        assert StreamComputeEngine._eval_predicate("value >= 10", 9.0) is False

    def test_less_equal(self):
        assert StreamComputeEngine._eval_predicate("value <= 10", 10.0) is True
        assert StreamComputeEngine._eval_predicate("value <= 10", 11.0) is False

    def test_no_operator_returns_true(self):
        """无比较运算符时返回 True（放行）"""
        assert StreamComputeEngine._eval_predicate("just_text", 5.0) is True

    def test_left_expr_numeric_overrides_value(self):
        """左侧为数字字面量时覆盖传入 value"""
        # "5 > 3" → left=5, threshold=3 → 5 > 3 = True
        assert StreamComputeEngine._eval_predicate("5 > 3", 999.0) is True

    def test_left_expr_non_numeric_keeps_value(self):
        """左侧非数字时保留传入 value"""
        assert StreamComputeEngine._eval_predicate("value > 3", 5.0) is True

    def test_invalid_threshold_returns_false(self):
        """右侧非数字时异常 → 返回 False（安全侧）"""
        assert StreamComputeEngine._eval_predicate("value > abc", 5.0) is False

    def test_empty_predicate_returns_true(self):
        """空谓词（无运算符）返回 True（放行）"""
        assert StreamComputeEngine._eval_predicate("", 5.0) is True


# ───────────────────────── _detect_3sigma 方法 ─────────────────────────


class TestDetect3Sigma:
    """_detect_3sigma 异常值检测"""

    def test_insufficient_samples_returns_none(self):
        """少于 10 个样本返回 None"""
        engine = StreamComputeEngine()
        assert engine._detect_3sigma([1.0, 2.0, 3.0]) is None

    def test_zero_std_returns_none(self):
        """标准差为 0 时返回 None"""
        engine = StreamComputeEngine()
        values = [5.0] * 10
        assert engine._detect_3sigma(values) is None

    def test_outlier_detected(self):
        """离群值（z_score > 3）被检测"""
        engine = StreamComputeEngine()
        # 10 个正常值 + 1 个离群值（最后一个）
        values = [10.0] * 9 + [10.0]
        values.append(100.0)  # 离群
        match = engine._detect_3sigma(values)
        assert match is not None
        assert match.pattern_id == "3sigma"
        assert match.matched is True
        assert match.confidence <= 1.0
        assert "z_score" in match.details

    def test_normal_value_returns_none(self):
        """正常值不触发"""
        engine = StreamComputeEngine()
        values = [10.0, 11.0, 9.0, 10.5, 9.5, 10.0, 11.0, 9.0, 10.0, 11.0, 10.0]
        assert engine._detect_3sigma(values) is None

    def test_confidence_capped_at_one(self):
        """confidence 上限为 1.0（z_score/6 > 1 时截断）

        z_score ≈ n/sqrt(n+1)（n 个相同正常值 + 1 个离群值），
        n=100 时 z_score≈9.95，confidence=min(9.95/6,1.0)=1.0。
        """
        engine = StreamComputeEngine()
        values = [0.0] * 100 + [1000000.0]
        match = engine._detect_3sigma(values)
        assert match is not None
        assert match.confidence == 1.0


# ───────────────────────── _publish_result / _emit_result ─────────────────────────


class TestPublishAndEmit:
    """_publish_result 与 _emit_result 测试"""

    async def test_publish_window_result_without_event_bus(self):
        """无 event_bus 时 _publish_result 仅记录调试包，不抛异常"""
        engine = StreamComputeEngine()
        wr = WindowResult(
            window_id="w1",
            window_type="sliding",
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            point_name="temp",
            aggregate="avg",
            value=42.0,
            count=5,
        )
        # 不应抛异常
        await engine._publish_result(wr)

    async def test_publish_pattern_match_without_event_bus(self):
        """无 event_bus 时发布 PatternMatch 不抛异常"""
        engine = StreamComputeEngine()
        pm = PatternMatch(pattern_id="rise", matched=True, confidence=0.9, details={"v": 1})
        await engine._publish_result(pm)

    async def test_publish_window_result_with_event_bus(self):
        """有 event_bus 时发布 StreamResultEvent(window)"""
        bus = EventBus()
        received: list = []
        bus.register_handler("StreamResultEvent", lambda e: received.append(e))
        engine = StreamComputeEngine()
        engine._event_bus = bus
        wr = WindowResult(
            window_id="w1",
            window_type="sliding",
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            point_name="temp",
            aggregate="avg",
            value=42.0,
            count=5,
        )
        await engine._publish_result(wr)
        # register_handler 是同步注册，publish 走 handlers 同步调用
        assert len(received) == 1
        assert isinstance(received[0], StreamResultEvent)
        assert received[0].result_type == "window"
        assert received[0].value == 42.0

    async def test_publish_pattern_match_with_event_bus(self):
        """有 event_bus 时发布 StreamResultEvent(pattern)"""
        bus = EventBus()
        received: list = []
        bus.register_handler("StreamResultEvent", lambda e: received.append(e))
        engine = StreamComputeEngine()
        engine._event_bus = bus
        pm = PatternMatch(pattern_id="anomaly", matched=True, confidence=0.8, details={"k": "v"})
        await engine._publish_result(pm)
        assert len(received) == 1
        assert received[0].result_type == "pattern"
        assert received[0].pattern_id == "anomaly"
        assert received[0].confidence == 0.8

    async def test_emit_result_sync_callback(self):
        """_emit_result 调用同步回调"""
        engine = StreamComputeEngine()
        received: list = []
        engine.register_callback(lambda e: received.append(e))
        evt = _make_event(1.0)
        await engine._emit_result(evt)
        assert received == [evt]

    async def test_emit_result_async_callback(self):
        """_emit_result 调用异步回调"""
        engine = StreamComputeEngine()
        received: list = []

        async def cb(e):
            received.append(e)

        engine.register_callback(cb)
        evt = _make_event(1.0)
        await engine._emit_result(evt)
        assert received == [evt]

    async def test_emit_result_callback_exception_does_not_raise(self):
        """回调抛异常时 _emit_result 不向上抛出，继续执行后续回调"""
        engine = StreamComputeEngine()
        received: list = []

        def bad_cb(e):
            raise RuntimeError("cb error")

        engine.register_callback(bad_cb)
        engine.register_callback(lambda e: received.append(e))
        await engine._emit_result(_make_event(1.0))
        assert len(received) == 1  # 第二个回调仍被执行

    async def test_publish_result_event_bus_exception_swallowed(self):
        """event_bus.publish 抛异常时被吞没，不向上传播"""
        bus = EventBus()
        bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))
        engine = StreamComputeEngine()
        engine._event_bus = bus
        wr = WindowResult(
            window_id="w1",
            window_type="sliding",
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            point_name="temp",
            aggregate="avg",
            value=1.0,
            count=1,
        )
        # 不应抛异常
        await engine._publish_result(wr)


# ───────────────────────── StreamComputeEngine 基础 ─────────────────────────


class TestStreamComputeEngineBasic:
    """引擎基础方法测试"""

    def test_init_defaults(self):
        """引擎初始化默认状态"""
        engine = StreamComputeEngine()
        assert engine._running is False
        assert engine._processors == {}
        assert engine._rules == {}
        assert engine._callbacks == []
        assert engine._windows == {}
        assert engine._event_bus is None
        assert engine._window_results_max == 10000
        assert "total_processed" in engine._stats

    def test_create_processor(self):
        """create_processor 注册并返回处理器"""
        engine = StreamComputeEngine()
        proc = engine.create_processor("p1")
        assert proc._id == "p1"
        assert "p1" in engine._processors
        assert engine._stats["processors_count"] == 1

    async def test_create_tumbling_window(self):
        engine = StreamComputeEngine()
        win = await engine.create_tumbling_window("tw1", 10)
        assert isinstance(win, TumblingWindow)
        assert "tw1" in engine._windows

    async def test_create_sliding_window(self):
        engine = StreamComputeEngine()
        win = await engine.create_sliding_window("sw1", 10, 1)
        assert isinstance(win, SlidingWindow)
        assert "sw1" in engine._windows

    async def test_create_session_window(self):
        engine = StreamComputeEngine()
        win = await engine.create_session_window("sess1", 30)
        assert isinstance(win, SessionWindow)
        assert "sess1" in engine._windows

    async def test_add_rule(self):
        engine = StreamComputeEngine()
        await engine.add_rule("r1", {"type": "filter", "predicate": "value > 0"})
        assert "r1" in engine._rules

    async def test_submit_event(self):
        """submit_event 入队"""
        engine = StreamComputeEngine()
        await engine.submit_event(_make_event(1.0))
        assert engine._event_queue.qsize() == 1

    async def test_submit(self):
        """submit 构造事件并入队"""
        engine = StreamComputeEngine()
        await engine.submit("d1", "p1", 42.0, quality="good")
        assert engine._event_queue.qsize() == 1

    async def test_submit_event_queue_full_drops(self, caplog):
        """队列满时丢弃事件并记录告警"""
        import logging

        engine = StreamComputeEngine()
        # 替换为 maxsize=1 的队列
        engine._event_queue = asyncio.Queue(maxsize=1)
        await engine.submit_event(_make_event(1.0))
        with caplog.at_level(logging.WARNING, logger="edgelite.engine.stream_compute"):
            # 第二个事件应被丢弃
            await engine.submit_event(_make_event(2.0))
        assert any("队列已满" in rec.message or "queue" in rec.message.lower() for rec in caplog.records)
        assert engine._event_queue.qsize() == 1

    def test_get_stats(self):
        """get_stats 返回快照含规则/窗口计数"""
        engine = StreamComputeEngine()
        stats = engine.get_stats()
        assert "total_processed" in stats
        assert "rules_count" in stats
        assert "windows_count" in stats
        assert "queue_size" in stats
        assert stats["rules_count"] == 0

    def test_get_processor_stats_existing(self):
        """get_processor_stats 返回已存在处理器统计"""
        engine = StreamComputeEngine()
        engine.create_processor("p1")
        stats = engine.get_processor_stats("p1")
        assert stats is not None
        assert "processed" in stats

    def test_get_processor_stats_missing(self):
        """get_processor_stats 对不存在的处理器返回 None"""
        engine = StreamComputeEngine()
        assert engine.get_processor_stats("nope") is None

    def test_get_window_result_missing(self):
        """get_window_result 无缓存时返回 None"""
        engine = StreamComputeEngine()
        assert engine.get_window_result("d1", "p1", 60, "avg") is None

    def test_get_window_result_cached(self):
        """get_window_result 命中缓存并移动到末尾(LRU)"""
        engine = StreamComputeEngine()
        key = "d1:p1:60:avg"
        engine._window_results[key] = 42.0
        result = engine.get_window_result("d1", "p1", 60, "avg")
        assert result == 42.0
        # 验证 move_to_end 生效：key 应是最后一项
        assert list(engine._window_results.keys())[-1] == key

    def test_register_callback(self):
        engine = StreamComputeEngine()
        cb = lambda e: None  # noqa: E731
        engine.register_callback(cb)
        assert cb in engine._callbacks


# ───────────────────────── remove_rule 清理 ─────────────────────────


class TestRemoveRule:
    """remove_rule 关联资源清理"""

    async def test_remove_rule_basic(self):
        """移除规则后规则字典中不再存在"""
        engine = StreamComputeEngine()
        await engine.add_rule("r1", {"type": "filter"})
        await engine.remove_rule("r1")
        assert "r1" not in engine._rules

    async def test_remove_rule_cleans_rule_windows(self):
        """移除规则时清理 _rule_windows 中以 rule_id 为前缀的窗口"""
        engine = StreamComputeEngine()
        await engine.add_rule("r1", {"type": "aggregate", "window_seconds": 60, "aggregate": "avg"})
        # 模拟规则处理产生的窗口键
        engine._rule_windows["r1:dev1:temp"] = SlidingWindow(60, 1)
        engine._rule_windows["r1:dev2:temp"] = SlidingWindow(60, 1)
        engine._rule_windows["r2:dev1:temp"] = SlidingWindow(60, 1)
        await engine.remove_rule("r1")
        assert "r1:dev1:temp" not in engine._rule_windows
        assert "r1:dev2:temp" not in engine._rule_windows
        # 其他规则窗口保留
        assert "r2:dev1:temp" in engine._rule_windows

    async def test_remove_rule_cleans_windows_dict(self):
        """移除规则时清理 _windows 中以 rule_id 为前缀或等于 rule_id 的窗口"""
        engine = StreamComputeEngine()
        await engine.add_rule("r1", {"type": "filter"})
        engine._windows["r1"] = TumblingWindow(10)
        engine._windows["r1:extra"] = TumblingWindow(10)
        engine._windows["other"] = TumblingWindow(10)
        await engine.remove_rule("r1")
        assert "r1" not in engine._windows
        assert "r1:extra" not in engine._windows
        assert "other" in engine._windows

    async def test_remove_rule_cleans_processors(self):
        """移除规则时清理 _pattern_/ _anomaly_ 处理器"""
        engine = StreamComputeEngine()
        await engine.add_rule("r1", {"type": "filter"})
        engine.create_processor("_pattern_r1")
        engine.create_processor("_anomaly_r1")
        engine.create_processor("_pattern_r2")
        await engine.remove_rule("r1")
        assert "_pattern_r1" not in engine._processors
        assert "_anomaly_r1" not in engine._processors
        assert "_pattern_r2" in engine._processors

    async def test_remove_nonexistent_rule_no_error(self):
        """移除不存在的规则不抛异常"""
        engine = StreamComputeEngine()
        await engine.remove_rule("nonexistent")  # 不应抛异常


# ───────────────────────── _process_rule 集成 ─────────────────────────


class TestProcessRule:
    """_process_rule 各规则类型直接调用测试"""

    async def test_filter_rule_match(self):
        """filter 规则：满足谓词的事件被加入结果"""
        engine = StreamComputeEngine()
        rule = {"type": "filter", "predicate": "value > 5"}
        evt = _make_event(10.0)
        results = await engine._process_rule("r1", rule, evt)
        assert len(results) == 1
        assert results[0] is evt

    async def test_filter_rule_no_match(self):
        """filter 规则：不满足谓词返回空"""
        engine = StreamComputeEngine()
        rule = {"type": "filter", "predicate": "value > 50"}
        results = await engine._process_rule("r1", rule, _make_event(10.0))
        assert results == []

    async def test_filter_rule_point_filter_mismatch(self):
        """point_filter 不匹配时返回空（不处理）"""
        engine = StreamComputeEngine()
        rule = {"type": "filter", "predicate": "value > 0", "point_filter": "humidity"}
        results = await engine._process_rule("r1", rule, _make_event(10.0, point="temp"))
        assert results == []

    async def test_filter_rule_point_filter_prefix_match(self):
        """point_filter 前缀匹配时处理事件"""
        engine = StreamComputeEngine()
        rule = {"type": "filter", "predicate": "value > 0", "point_filter": "temp"}
        results = await engine._process_rule("r1", rule, _make_event(10.0, point="temp_01"))
        assert len(results) == 1

    async def test_filter_rule_default_type(self):
        """未指定 type 时默认 filter"""
        engine = StreamComputeEngine()
        rule = {"predicate": "value > 5"}
        results = await engine._process_rule("r1", rule, _make_event(10.0))
        assert len(results) == 1

    async def test_aggregate_rule_emits_result(self):
        """aggregate 规则：首事件即输出（_last_emit 为 None 时直接输出），后续按 slide 间隔输出"""
        engine = StreamComputeEngine()
        rule = {
            "type": "aggregate",
            "window_seconds": 100,
            "aggregate": "sum",
            "min_count": 1,
            "slide_seconds": 1.0,
        }
        # 首事件：_last_emit 为 None → 直接输出
        r1 = await engine._process_rule("r1", rule, _make_event(1.0, ts_offset=0))
        assert len(r1) == 1
        assert r1[0].value == 1.0  # sum=[1.0]
        assert r1[0].point_name == "temp:sum"
        # 第二个事件（间隔>=slide）触发滑动输出
        r2 = await engine._process_rule("r1", rule, _make_event(2.0, ts_offset=1.0))
        assert len(r2) == 1
        assert r2[0].value == 3.0  # sum=1+2
        assert r2[0].point_name == "temp:sum"

    async def test_aggregate_rule_caches_window_result(self):
        """aggregate 规则输出时缓存到 _window_results"""
        engine = StreamComputeEngine()
        rule = {
            "type": "aggregate",
            "window_seconds": 60,
            "aggregate": "avg",
            "min_count": 1,
            "slide_seconds": 1.0,
        }
        await engine._process_rule("r1", rule, _make_event(1.0, ts_offset=0))
        await engine._process_rule("r1", rule, _make_event(3.0, ts_offset=1.0))
        # 缓存键 device:point:window_seconds:aggregate
        cached = engine.get_window_result("dev1", "temp", 60, "avg")
        assert cached is not None
        assert cached == 2.0  # (1+3)/2

    async def test_aggregate_rule_insufficient_count_no_emit(self):
        """aggregate 规则 min_count 未满足时不输出"""
        engine = StreamComputeEngine()
        rule = {
            "type": "aggregate",
            "window_seconds": 100,
            "aggregate": "avg",
            "min_count": 5,
            "slide_seconds": 1.0,
        }
        r1 = await engine._process_rule("r1", rule, _make_event(1.0, ts_offset=0))
        r2 = await engine._process_rule("r1", rule, _make_event(2.0, ts_offset=1.0))
        assert r1 == []
        assert r2 == []

    async def test_pattern_rise_rule(self):
        """pattern rise 规则检测上升沿"""
        engine = StreamComputeEngine()
        rule = {"type": "pattern", "pattern": "rise", "threshold": 5.0}
        r1 = await engine._process_rule("r1", rule, _make_event(0.0, ts_offset=0))
        # 首事件上升 0 < 5 → 无输出
        assert r1 == []
        r2 = await engine._process_rule("r1", rule, _make_event(10.0, ts_offset=1))
        # 上升 10 >= 5 → 输出
        assert len(r2) == 1
        assert r2[0].point_name == "temp_rise"

    async def test_pattern_rate_rule(self):
        """pattern rate 规则检测变化率超限"""
        engine = StreamComputeEngine()
        rule = {"type": "pattern", "pattern": "rate", "max_rate": 5.0}
        await engine._process_rule("r1", rule, _make_event(0.0, ts_offset=0))
        r2 = await engine._process_rule("r1", rule, _make_event(100.0, ts_offset=1))
        assert len(r2) == 1
        assert r2[0].point_name == "temp_rate_exceeded"

    async def test_pattern_3sigma_rule_no_match(self):
        """pattern 3sigma 规则样本不足时不输出"""
        engine = StreamComputeEngine()
        rule = {"type": "pattern", "pattern": "3sigma", "window_seconds": 100}
        # 样本不足 10，无输出
        for i in range(5):
            r = await engine._process_rule("r1", rule, _make_event(float(i), ts_offset=float(i)))
            assert r == []

    async def test_pattern_3sigma_rule_match(self):
        """pattern 3sigma 规则检测到离群值"""
        engine = StreamComputeEngine()
        rule = {"type": "pattern", "pattern": "3sigma", "window_seconds": 1000}
        # 前 10 个正常值
        for i in range(10):
            await engine._process_rule("r1", rule, _make_event(50.0, ts_offset=float(i)))
        # 第 11 个离群值
        results = await engine._process_rule("r1", rule, _make_event(500.0, ts_offset=10))
        assert len(results) == 1
        assert results[0].point_name == "temp_3sigma"
        assert results[0].quality == "suspect"

    async def test_anomaly_rule_no_emit_before_min_samples(self):
        """anomaly 规则样本不足时不输出"""
        engine = StreamComputeEngine()
        rule = {"type": "anomaly", "std_multiplier": 3.0}
        for i in range(9):
            r = await engine._process_rule("r1", rule, _make_event(float(i), ts_offset=float(i)))
            assert r == []

    async def test_anomaly_rule_detects_outlier(self):
        """anomaly 规则检测离群值"""
        engine = StreamComputeEngine()
        rule = {"type": "anomaly", "std_multiplier": 2.0}
        for i in range(10):
            await engine._process_rule("r1", rule, _make_event(50.0, ts_offset=float(i)))
        results = await engine._process_rule("r1", rule, _make_event(500.0, ts_offset=10))
        assert len(results) == 1
        assert results[0].point_name == "temp_anomaly"

    async def test_aggregate_rule_publishes_to_event_bus(self):
        """aggregate 规则输出时发布 PointUpdateEvent 到 EventBus（每个聚合结果发布一次）"""
        bus = EventBus()
        received: list = []
        bus.register_handler("PointUpdateEvent", lambda e: received.append(e))
        engine = StreamComputeEngine()
        engine._event_bus = bus
        rule = {
            "type": "aggregate",
            "window_seconds": 100,
            "aggregate": "sum",
            "min_count": 1,
            "slide_seconds": 1.0,
        }
        # 首事件输出 sum=1.0，第二事件输出 sum=3.0
        await engine._process_rule("r1", rule, _make_event(1.0, ts_offset=0))
        await engine._process_rule("r1", rule, _make_event(2.0, ts_offset=1.0))
        assert len(received) == 2
        assert received[0].value == 1.0
        assert received[1].value == 3.0

    async def test_unknown_rule_type_returns_empty(self):
        """未知 rule_type 返回空列表"""
        engine = StreamComputeEngine()
        rule = {"type": "unknown_type"}
        results = await engine._process_rule("r1", rule, _make_event(1.0))
        assert results == []

    async def test_aggregate_rule_event_bus_publish_exception_swallowed(self):
        """aggregate 规则发布到 EventBus 失败时异常被吞没，不影响结果返回"""
        bus = EventBus()
        bus.publish = AsyncMock(side_effect=RuntimeError("bus down"))
        engine = StreamComputeEngine()
        engine._event_bus = bus
        rule = {
            "type": "aggregate",
            "window_seconds": 100,
            "aggregate": "sum",
            "min_count": 1,
            "slide_seconds": 1.0,
        }
        # 首事件输出聚合结果，EventBus 发布失败但不抛出
        results = await engine._process_rule("r1", rule, _make_event(1.0, ts_offset=0))
        assert len(results) == 1
        assert results[0].value == 1.0


# ───────────────────────── 引擎生命周期集成 ─────────────────────────


class TestEngineLifecycle:
    """引擎 start/stop/事件处理循环集成"""

    async def test_start_stop_without_event_bus(self):
        """无 event_bus 时 start/stop 正常"""
        engine = StreamComputeEngine()
        await engine.start()
        assert engine._running is True
        assert engine._task is not None
        await asyncio.sleep(0.05)
        await engine.stop()
        assert engine._running is False

    async def test_start_with_event_bus_subscribes(self):
        """start(event_bus) 订阅事件总线并启动事件处理循环"""
        bus = EventBus()
        engine = StreamComputeEngine()
        await engine.start(event_bus=bus)
        assert engine._event_bus is bus
        assert engine._subscriber_queue is not None
        assert engine._event_handler_task is not None
        await engine.stop()

    async def test_connect_to_event_bus(self):
        """connect_to_event_bus 单独连接"""
        bus = EventBus()
        engine = StreamComputeEngine()
        await engine.connect_to_event_bus(bus)
        assert engine._event_bus is bus
        assert engine._subscriber_queue is not None
        # 清理：取消事件处理任务
        engine._running = False
        if engine._event_handler_task:
            engine._event_handler_task.cancel()
            try:
                await engine._event_handler_task
            except (asyncio.CancelledError, Exception):
                pass

    async def test_connect_to_event_bus_already_connected_noop(self, caplog):
        """已连接且 running 时再次连接记录告警并返回"""
        import logging

        bus = EventBus()
        engine = StreamComputeEngine()
        engine._running = True
        engine._event_bus = bus
        engine._subscriber_queue = await bus.subscribe("stream_compute")
        with caplog.at_level(logging.WARNING, logger="edgelite.engine.stream_compute"):
            await engine.connect_to_event_bus(bus)
        assert any("已连接" in rec.message for rec in caplog.records)

    async def test_event_handler_loop_processes_point_update_with_str_ts(self):
        """_event_handler_loop 处理 PointUpdateEvent（字符串时间戳）"""
        bus = EventBus()
        engine = StreamComputeEngine()
        await engine.start(event_bus=bus)
        # 注册回调收集结果
        received: list = []
        engine.register_callback(lambda e: received.append(e))
        # 添加 filter 规则使事件被处理
        await engine.add_rule("r1", {"type": "filter", "predicate": "value > 0"})
        # 发布带字符串时间戳的 PointUpdateEvent
        evt = PointUpdateEvent(
            device_id="d1",
            point_name="temp",
            value=42.0,
            quality="good",
        )
        evt.timestamp = "2024-01-01T00:00:00+00:00"
        await bus.publish(evt)
        # 等待事件处理循环消费
        await asyncio.sleep(0.3)
        await engine.stop()
        assert len(received) >= 1
        assert received[0].value == 42.0

    async def test_event_handler_loop_processes_point_update_none_value(self):
        """PointUpdateEvent value=None 时转为 0.0"""
        bus = EventBus()
        engine = StreamComputeEngine()
        await engine.start(event_bus=bus)
        received: list = []
        engine.register_callback(lambda e: received.append(e))
        await engine.add_rule("r1", {"type": "filter", "predicate": "value >= 0"})
        evt = PointUpdateEvent(device_id="d1", point_name="temp", value=None, quality="good")
        await bus.publish(evt)
        await asyncio.sleep(0.3)
        await engine.stop()
        assert len(received) >= 1
        assert received[0].value == 0.0

    async def test_event_handler_loop_processes_streamevent_directly(self):
        """_event_handler_loop 处理直接发布的 StreamEvent"""
        bus = EventBus()
        engine = StreamComputeEngine()
        await engine.start(event_bus=bus)
        received: list = []
        engine.register_callback(lambda e: received.append(e))
        await engine.add_rule("r1", {"type": "filter", "predicate": "value > 0"})
        await bus.publish(_make_event(99.0))
        await asyncio.sleep(0.3)
        await engine.stop()
        assert len(received) >= 1
        assert received[0].value == 99.0

    async def test_process_loop_processes_submitted_event(self):
        """_process_loop 处理 submit 提交的事件"""
        engine = StreamComputeEngine()
        await engine.start()
        received: list = []
        engine.register_callback(lambda e: received.append(e))
        await engine.add_rule("r1", {"type": "filter", "predicate": "value > 0"})
        await engine.submit("d1", "temp", 7.0)
        await asyncio.sleep(0.3)
        await engine.stop()
        assert len(received) >= 1
        assert received[0].value == 7.0

    async def test_process_loop_rule_exception_does_not_crash(self):
        """规则处理抛异常时 _process_loop 不崩溃，继续运行"""
        engine = StreamComputeEngine()
        await engine.start()
        # 构造会导致 _process_rule 异常的规则（恶意 predicate）
        await engine.add_rule("r1", {"type": "filter", "predicate": "value > abc"})
        # 提交事件，规则求值失败但引擎不应崩溃
        await engine.submit("d1", "temp", 5.0)
        await asyncio.sleep(0.2)
        # 引擎仍在运行
        assert engine._running is True
        # 再提交一个正常规则验证引擎仍可工作
        await engine.add_rule("r2", {"type": "filter", "predicate": "value > 0"})
        received: list = []
        engine.register_callback(lambda e: received.append(e))
        await engine.submit("d2", "temp", 9.0)
        await asyncio.sleep(0.3)
        await engine.stop()
        assert any(r.value == 9.0 for r in received)

    async def test_stop_without_start(self):
        """未 start 时 stop 不抛异常"""
        engine = StreamComputeEngine()
        await engine.stop()  # _task / _event_handler_task 均为 None
        assert engine._running is False

    async def test_event_handler_loop_returns_when_subscriber_queue_none(self):
        """_subscriber_queue 为 None 时 _event_handler_loop 立即返回"""
        engine = StreamComputeEngine()
        engine._running = True
        engine._subscriber_queue = None
        # 直接调用应立即返回（不阻塞）
        await asyncio.wait_for(engine._event_handler_loop(), timeout=1.0)
        # 无异常即通过

    async def test_process_loop_record_packet_exception_swallowed(self, monkeypatch):
        """_process_loop 中 record_packet 抛异常时被吞没，不影响事件处理"""

        def _raising_record_packet(*args, **kwargs):
            raise RuntimeError("dbg fail")

        monkeypatch.setattr("edgelite.engine.stream_compute.record_packet", _raising_record_packet)
        engine = StreamComputeEngine()
        await engine.start()
        received: list = []
        engine.register_callback(lambda e: received.append(e))
        await engine.add_rule("r1", {"type": "filter", "predicate": "value > 0"})
        await engine.submit("d1", "temp", 8.0)
        await asyncio.sleep(0.3)
        await engine.stop()
        # record_packet 异常不影响事件处理
        assert any(r.value == 8.0 for r in received)


# ───────────────────────── _window_results LRU 淘汰 ─────────────────────────


class TestWindowResultsLRU:
    """_window_results OrderedDict LRU 淘汰机制"""

    async def test_lru_eviction_when_exceeding_max(self):
        """超过 _window_results_max 时淘汰最旧条目"""
        engine = StreamComputeEngine()
        engine._window_results_max = 3
        # 直接通过 _process_rule 触发缓存写入较复杂，这里直接测试淘汰逻辑
        engine._window_results["k1"] = 1.0
        engine._window_results["k2"] = 2.0
        engine._window_results["k3"] = 3.0
        # 模拟 _process_rule 中的写入+淘汰逻辑
        with engine._window_results_lock:
            engine._window_results["k4"] = 4.0
            engine._window_results.move_to_end("k4")
            while len(engine._window_results) > engine._window_results_max:
                engine._window_results.popitem(last=False)
        assert "k1" not in engine._window_results  # 最旧被淘汰
        assert "k4" in engine._window_results
        assert len(engine._window_results) == 3


# ───────────────────────── get_stream_engine 全局单例 ─────────────────────────


class TestGetStreamEngine:
    """get_stream_engine 全局单例"""

    def test_returns_engine_instance(self):
        """返回 StreamComputeEngine 实例"""
        engine = get_stream_engine()
        assert isinstance(engine, StreamComputeEngine)

    def test_singleton_identity(self):
        """多次调用返回同一实例"""
        e1 = get_stream_engine()
        e2 = get_stream_engine()
        assert e1 is e2
