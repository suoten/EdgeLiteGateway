"""数据预处理模块扩展单元测试"""
import math
import sys
import time
from unittest.mock import patch

sys.path.insert(0, "src")
from edgelite.engine.preprocessor import (
    DataPreprocessor,
    SlidingWindowAggregator,
    downsample_average,
    downsample_lttb,
    downsample_minmax,
    interpolate_average,
    interpolate_linear,
    interpolate_next,
    interpolate_previous,
    interpolate_spline,
    transform_linearize,
    transform_round,
    transform_scale,
)


class TestInterpolationFunctions:
    """测试插值工具函数"""

    async def test_interpolate_linear_basic(self):
        """线性插值-基本计算"""
        assert interpolate_linear(10.0, 20.0, 0.5) == 15.0
        assert interpolate_linear(0.0, 100.0, 0.0) == 0.0
        assert interpolate_linear(0.0, 100.0, 1.0) == 100.0

    async def test_interpolate_linear_negative(self):
        """线性插值-负值与反向"""
        assert interpolate_linear(-10.0, 10.0, 0.5) == 0.0
        assert interpolate_linear(20.0, 10.0, 0.5) == 15.0

    async def test_interpolate_previous_found(self):
        """前值插值-找到前一个有效值"""
        values = [1.0, None, float("nan"), 4.0]
        assert interpolate_previous(values, 3) == 1.0

    async def test_interpolate_previous_skip_nan_none(self):
        """前值插值-跳过None和NaN"""
        values = [None, float("nan"), 5.0, None]
        assert interpolate_previous(values, 3) == 5.0

    async def test_interpolate_previous_not_found(self):
        """前值插值-前面无有效值返回None"""
        values = [None, float("nan"), 3.0]
        assert interpolate_previous(values, 0) is None
        assert interpolate_previous(values, 1) is None

    async def test_interpolate_next_found(self):
        """后值插值-找到后一个有效值"""
        values = [None, 2.0, None, 4.0]
        assert interpolate_next(values, 0) == 2.0

    async def test_interpolate_next_skip_nan_none(self):
        """后值插值-跳过None和NaN"""
        values = [None, float("nan"), None, 7.0]
        assert interpolate_next(values, 0) == 7.0

    async def test_interpolate_next_not_found(self):
        """后值插值-后面无有效值返回None"""
        values = [1.0, None, float("nan")]
        assert interpolate_next(values, 1) is None
        assert interpolate_next(values, 2) is None

    async def test_interpolate_average_basic(self):
        """均值插值-窗口内有效值均值"""
        values = [10.0, None, 20.0, None, 30.0]
        result = interpolate_average(values, 1, window=3)
        assert result is not None
        assert 0.0 < result < 40.0

    async def test_interpolate_average_window_param(self):
        """均值插值-不同窗口大小"""
        values = [1.0, 2.0, None, 4.0, 5.0]
        r1 = interpolate_average(values, 2, window=2)
        r2 = interpolate_average(values, 2, window=5)
        assert r1 is not None
        assert r2 is not None

    async def test_interpolate_average_no_nearby(self):
        """均值插值-邻近全为None返回None"""
        values = [None, None, None]
        assert interpolate_average(values, 1, window=3) is None

    async def test_interpolate_average_exclude_self(self):
        """均值插值-排除自身索引"""
        values = [10.0, 20.0, None, 40.0, 50.0]
        result = interpolate_average(values, 2, window=5)
        assert result is not None
        assert result == (10.0 + 20.0 + 40.0 + 50.0) / 4.0

    async def test_interpolate_spline_middle(self):
        """样条(分段线性)插值-中间区间"""
        values = [0.0, None, 20.0]
        result = interpolate_spline(values, 1)
        assert result is not None
        assert abs(result - 10.0) < 0.001

    async def test_interpolate_spline_below_range(self):
        """样条插值-低于最小索引返回首值"""
        values = [None, 10.0, 20.0]
        result = interpolate_spline(values, 0)
        assert result == 10.0

    async def test_interpolate_spline_above_range(self):
        """样条插值-高于最大索引返回末值"""
        values = [10.0, 20.0, None]
        result = interpolate_spline(values, 2)
        assert result == 20.0

    async def test_interpolate_spline_insufficient_points(self):
        """样条插值-有效点不足2个返回None"""
        values = [None, None, 5.0]
        assert interpolate_spline(values, 0) is None

    async def test_interpolate_spline_multi_segment(self):
        """样条插值-多段线性插值"""
        values = [0.0, None, 20.0, None, 40.0]
        r1 = interpolate_spline(values, 1)
        r2 = interpolate_spline(values, 3)
        assert abs(r1 - 10.0) < 0.001
        assert abs(r2 - 30.0) < 0.001


class TestDownsampleFunctions:
    """测试降采样算法"""

    async def test_downsample_lttb_threshold_ge_n(self):
        """LTTB-阈值大于等于数据量时原样返回"""
        values = [(float(i), float(i)) for i in range(5)]
        result = downsample_lttb(values, threshold=10)
        assert result == values

    async def test_downsample_lttb_threshold_below_3(self):
        """LTTB-阈值小于3时原样返回"""
        values = [(float(i), float(i)) for i in range(10)]
        result = downsample_lttb(values, threshold=2)
        assert result == values

    async def test_downsample_lttb_normal(self):
        """LTTB-正常降采样"""
        values = [(float(i), float(i * 2)) for i in range(100)]
        result = downsample_lttb(values, threshold=10)
        assert len(result) == 10
        assert result[0] == values[0]
        assert result[-1] == values[-1]

    async def test_downsample_lttb_preserves_endpoints(self):
        """LTTB-保留首尾点"""
        values = [(float(i), math.sin(i)) for i in range(50)]
        result = downsample_lttb(values, threshold=5)
        assert len(result) == 5
        assert result[0][0] == values[0][0]
        assert result[-1][0] == values[-1][0]

    async def test_downsample_minmax_empty(self):
        """MinMax降采样-空数据"""
        assert downsample_minmax([], 5) == []

    async def test_downsample_minmax_small_bucket(self):
        """MinMax降采样-桶大小小于2原样返回"""
        values = [(0.0, 1.0), (1.0, 2.0)]
        assert downsample_minmax(values, 1) == values

    async def test_downsample_minmax_normal(self):
        """MinMax降采样-保留每桶最小最大值"""
        values = [(0.0, 5.0), (1.0, 1.0), (2.0, 8.0), (3.0, 3.0)]
        result = downsample_minmax(values, 2)
        assert len(result) == 4
        assert (1.0, 1.0) in result
        assert (0.0, 5.0) in result

    async def test_downsample_minmax_min_eq_max(self):
        """MinMax降采样-桶内min==max只保留一个"""
        values = [(0.0, 5.0), (1.0, 5.0)]
        result = downsample_minmax(values, 2)
        assert len(result) == 1

    async def test_downsample_average_empty(self):
        """均值降采样-空数据"""
        assert downsample_average([], 5) == []

    async def test_downsample_average_small_bucket(self):
        """均值降采样-桶大小小于1原样返回"""
        values = [(0.0, 1.0), (1.0, 2.0)]
        assert downsample_average(values, 0) == values

    async def test_downsample_average_normal(self):
        """均值降采样-每桶用均值替代"""
        values = [(0.0, 10.0), (1.0, 20.0), (2.0, 30.0), (3.0, 40.0)]
        result = downsample_average(values, 2)
        assert len(result) == 2
        assert abs(result[0][0] - 0.5) < 0.001
        assert abs(result[0][1] - 15.0) < 0.001
        assert abs(result[1][0] - 2.5) < 0.001
        assert abs(result[1][1] - 35.0) < 0.001


class TestTransformFunctions:
    """测试数据变换工具函数"""

    async def test_transform_scale_basic(self):
        """比例变换-y = x * scale + offset"""
        assert transform_scale(10.0, 2.0, 0.0) == 20.0
        assert transform_scale(10.0, 1.0, 5.0) == 15.0
        assert transform_scale(10.0, 0.0, 100.0) == 100.0

    async def test_transform_scale_negative(self):
        """比例变换-负系数"""
        assert transform_scale(10.0, -1.0, 0.0) == -10.0
        assert transform_scale(-5.0, -2.0, 3.0) == 13.0

    async def test_transform_linearize_empty_table(self):
        """线性化-空表返回原值"""
        assert transform_linearize(42.0, []) == 42.0

    async def test_transform_linearize_below_first(self):
        """线性化-低于首点返回首点映射"""
        table = [(0.0, 100.0), (10.0, 200.0)]
        assert transform_linearize(-5.0, table) == 100.0

    async def test_transform_linearize_above_last(self):
        """线性化-高于末点返回末点映射"""
        table = [(0.0, 100.0), (10.0, 200.0)]
        assert transform_linearize(15.0, table) == 200.0

    async def test_transform_linearize_middle(self):
        """线性化-中间区间分段插值"""
        table = [(0.0, 0.0), (10.0, 100.0), (20.0, 200.0)]
        assert abs(transform_linearize(5.0, table) - 50.0) < 0.001
        assert abs(transform_linearize(15.0, table) - 150.0) < 0.001

    async def test_transform_round_zero_decimals(self):
        """四舍五入-0位小数"""
        assert transform_round(2.4, 0) == 2.0
        assert transform_round(2.5, 0) == 3.0
        assert transform_round(2.6, 0) == 3.0

    async def test_transform_round_positive_decimals(self):
        """四舍五入-多位小数"""
        assert transform_round(2.567, 2) == 2.57
        assert transform_round(2.555, 1) == 2.6

    async def test_transform_round_negative_value(self):
        """四舍五入-负值处理"""
        assert transform_round(-2.4, 0) == -2.0
        assert transform_round(-2.6, 0) == -3.0


class TestSlidingWindowAggregator:
    """测试 SlidingWindowAggregator"""

    async def test_add_window_sec_le_zero(self):
        """窗口大小<=0返回None"""
        agg = SlidingWindowAggregator()
        assert agg.add("p", 10.0, 100.0, window_sec=0) is None
        assert agg.add("p", 10.0, 100.0, window_sec=-5) is None

    async def test_add_tumbling_first_returns_none(self):
        """Tumbling窗口-首点不足2个返回None"""
        agg = SlidingWindowAggregator()
        assert agg.add("p", 10.0, 100.0, window_sec=60) is None

    async def test_add_tumbling_avg(self):
        """Tumbling窗口-返回均值"""
        agg = SlidingWindowAggregator()
        agg.add("p", 10.0, 100.0, window_sec=60)
        result = agg.add("p", 20.0, 110.0, window_sec=60)
        assert result is not None
        assert abs(result - 15.0) < 0.001

    async def test_add_tumbling_evicts_old(self):
        """Tumbling窗口-淘汰超时点"""
        agg = SlidingWindowAggregator()
        agg.add("p", 10.0, 100.0, window_sec=50)
        agg.add("p", 20.0, 110.0, window_sec=50)
        result = agg.add("p", 30.0, 200.0, window_sec=50)
        assert result is None

    async def test_add_sliding(self):
        """Sliding窗口类型"""
        agg = SlidingWindowAggregator()
        assert agg.add("p", 10.0, 100.0, window_sec=60, window_type="sliding") is None
        result = agg.add("p", 20.0, 110.0, window_sec=60, window_type="sliding")
        assert abs(result - 15.0) < 0.001

    async def test_add_session_no_gap(self):
        """Session窗口-间隔内不清理"""
        agg = SlidingWindowAggregator()
        agg.add("p", 10.0, 100.0, window_sec=120, window_type="session", session_gap_sec=30)
        result = agg.add("p", 20.0, 110.0, window_sec=120, window_type="session", session_gap_sec=30)
        assert abs(result - 15.0) < 0.001

    async def test_add_session_gap_clears(self):
        """Session窗口-超间隔清理窗口"""
        agg = SlidingWindowAggregator()
        agg.add("p", 10.0, 100.0, window_sec=120, window_type="session", session_gap_sec=30)
        result = agg.add("p", 20.0, 200.0, window_sec=120, window_type="session", session_gap_sec=30)
        assert result is None

    async def test_get_stats_unknown_point(self):
        """get_stats-未知点返回None"""
        agg = SlidingWindowAggregator()
        assert agg.get_stats("unknown") is None

    async def test_get_stats_with_data(self):
        """get_stats-返回窗口统计"""
        agg = SlidingWindowAggregator()
        agg.add("p", 10.0, 100.0, window_sec=60)
        agg.add("p", 20.0, 110.0, window_sec=60)
        stats = agg.get_stats("p")
        assert stats is not None
        assert "tumbling" in stats
        assert stats["tumbling"]["count"] == 2
        assert stats["tumbling"]["min"] == 10.0
        assert stats["tumbling"]["max"] == 20.0
        assert stats["tumbling"]["latest"] == 20.0

    async def test_clear_single_point(self):
        """clear-清除单个点"""
        agg = SlidingWindowAggregator()
        agg.add("p1", 10.0, 100.0, window_sec=60)
        agg.add("p2", 20.0, 100.0, window_sec=60)
        agg.clear("p1")
        assert agg.get_stats("p1") is None
        assert agg.get_stats("p2") is not None

    async def test_clear_all(self):
        """clear-清除所有"""
        agg = SlidingWindowAggregator()
        agg.add("p1", 10.0, 100.0, window_sec=60)
        agg.add("p2", 20.0, 100.0, window_sec=60)
        agg.clear()
        assert agg.get_stats("p1") is None
        assert agg.get_stats("p2") is None

    async def test_get_stats_only_nonempty_windows(self):
        """get_stats-仅返回非空窗口的统计"""
        agg = SlidingWindowAggregator()
        agg.add("p", 10.0, 100.0, window_sec=60, window_type="tumbling")
        stats = agg.get_stats("p")
        assert stats is not None
        assert "tumbling" in stats
        assert "sliding" not in stats
        assert "session" not in stats


class TestPreprocessorFilters:
    """测试各类滤波器"""

    async def test_kalman_filter_first_point(self):
        """Kalman滤波-首点返回原值"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "kalman"})
        val, report = pp.process("p", 10.0, 100.0)
        assert val == 10.0
        assert report is True

    async def test_kalman_filter_second_point(self):
        """Kalman滤波-后续点平滑值介于估计与测量之间"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "kalman", "kalman_process_noise": 0.001, "kalman_measurement_noise": 0.01})
        pp.process("p", 10.0, 100.0)
        val, _ = pp.process("p", 12.0, 110.0)
        assert val is not None
        assert 10.0 < val < 12.0

    async def test_kalman_custom_noise(self):
        """Kalman滤波-自定义噪声参数收敛"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "kalman", "kalman_process_noise": 0.1, "kalman_measurement_noise": 1.0})
        pp.process("p", 100.0, 100.0)
        val, _ = pp.process("p", 100.0, 110.0)
        assert val is not None
        assert abs(val - 100.0) < 1.0

    async def test_ema_filter_first_point(self):
        """EMA滤波-首点返回原值"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "ema", "ema_alpha": 0.3})
        val, report = pp.process("p", 10.0, 100.0)
        assert val == 10.0
        assert report is True

    async def test_ema_filter_smoothing(self):
        """EMA滤波-指数平滑计算"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "ema", "ema_alpha": 0.3})
        pp.process("p", 10.0, 100.0)
        val, _ = pp.process("p", 20.0, 110.0)
        assert abs(val - 13.0) < 0.001

    async def test_ema_invalid_alpha_defaults(self):
        """EMA滤波-非法alpha使用默认0.3"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "ema", "ema_alpha": 5.0})
        pp.process("p", 10.0, 100.0)
        val, _ = pp.process("p", 20.0, 110.0)
        assert abs(val - 13.0) < 0.001

    async def test_moving_average_filter(self):
        """滑动均值滤波"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "moving_avg", "filter_window": 3})
        pp.process("p", 10.0, 100.0)
        val2, _ = pp.process("p", 20.0, 110.0)
        assert abs(val2 - 15.0) < 0.001
        val3, _ = pp.process("p", 30.0, 120.0)
        assert abs(val3 - 20.0) < 0.001

    async def test_moving_average_window_too_small(self):
        """滑动均值滤波-window<1返回原值"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "moving_avg", "filter_window": 0})
        val, _ = pp.process("p", 42.0, 100.0)
        assert val == 42.0

    async def test_median_filter_5(self):
        """中值滤波-median_5窗口"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "median_5", "filter_window": 5})
        pp.process("p", 10.0, 100.0)
        pp.process("p", 20.0, 110.0)
        # window=[10,20,200] len=3 -> 中值=20.0
        val3, _ = pp.process("p", 200.0, 120.0)
        assert val3 == 20.0
        # window=[10,20,200,30] len=4 -> sorted=[10,20,30,200] 中值=30.0
        val4, _ = pp.process("p", 30.0, 130.0)
        assert val4 == 30.0

    async def test_median_filter_window_too_small(self):
        """中值滤波-window<1返回原值"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "median_3", "filter_window": 0})
        val, _ = pp.process("p", 99.0, 100.0)
        assert val == 99.0

    async def test_median_filter_window_change(self):
        """中值滤波-窗口大小变更时重建deque"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "median_3", "filter_window": 3})
        pp.process("p", 10.0, 100.0)
        pp.process("p", 20.0, 110.0)
        pp.process("p", 30.0, 120.0)
        pp.configure("p", {"filter": "median_5", "filter_window": 5})
        val, _ = pp.process("p", 40.0, 130.0)
        assert val is not None

    async def test_moving_average_window_change(self):
        """滑动均值-窗口大小变更时重建deque"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "moving_avg", "filter_window": 2})
        pp.process("p", 10.0, 100.0)
        pp.process("p", 20.0, 110.0)
        pp.configure("p", {"filter": "moving_avg", "filter_window": 4})
        val, _ = pp.process("p", 30.0, 120.0)
        assert val is not None

    async def test_unknown_filter_passthrough(self):
        """未知滤波类型-原值透传"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "unknown_filter"})
        val, _ = pp.process("p", 42.0, 100.0)
        assert val == 42.0

    async def test_no_filter_passthrough(self):
        """无滤波配置-原值透传"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband": 100.0})
        val, _ = pp.process("p", 42.0, 100.0)
        assert val == 42.0


class TestPreprocessorTransform:
    """测试 process 中的数据变换步骤"""

    async def test_transform_scale_only(self):
        """比例变换-only scale"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 2.0, "transform_offset": 0.0})
        val, _ = pp.process("p", 10.0, 100.0)
        assert val == 20.0

    async def test_transform_offset_only(self):
        """比例变换-only offset"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 1.0, "transform_offset": 5.0})
        val, _ = pp.process("p", 10.0, 100.0)
        assert val == 15.0

    async def test_transform_scale_and_offset(self):
        """比例变换-scale + offset"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 3.0, "transform_offset": 1.0})
        val, _ = pp.process("p", 10.0, 100.0)
        assert val == 31.0

    async def test_transform_round(self):
        """四舍五入变换"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_round": 1})
        val, _ = pp.process("p", 2.567, 100.0)
        assert abs(val - 2.6) < 0.001

    async def test_transform_scale_and_round(self):
        """比例变换+四舍五入组合"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 0.1, "transform_offset": 0.0, "transform_round": 2})
        val, _ = pp.process("p", 25.678, 100.0)
        assert abs(val - 2.57) < 0.001

    async def test_transform_defaults_passthrough(self):
        """变换默认值(scale=1,offset=0,无round)-原值透传"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 1.0, "transform_offset": 0.0})
        val, _ = pp.process("p", 42.0, 100.0)
        assert val == 42.0


class TestPreprocessorDeadband:
    """测试死区过滤边界分支"""

    async def test_deadband_first_value_always_reports(self):
        """死区-首值始终上报"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband": 0.5})
        val, report = pp.process("p", 25.0, 100.0)
        assert report is True
        assert val == 25.0

    async def test_deadband_percent_near_zero_last(self):
        """百分比死区-last接近0时按绝对值比较"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband_percent": 5.0})
        pp.process("p", 0.0, 100.0)
        _, report = pp.process("p", 3.0, 110.0)
        assert report is False
        val, report = pp.process("p", 6.0, 120.0)
        assert report is True

    async def test_deadband_absolute_zero_deadband(self):
        """绝对死区-deadband=0始终上报"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband": 0})
        pp.process("p", 10.0, 100.0)
        _, report = pp.process("p", 10.0, 110.0)
        assert report is True

    async def test_deadband_percent_zero_pct(self):
        """百分比死区-pct=0始终上报"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband_percent": 0})
        pp.process("p", 100.0, 100.0)
        _, report = pp.process("p", 100.0, 110.0)
        assert report is True

    async def test_deadband_both_configured(self):
        """绝对+百分比死区同时配置-绝对先判断,百分比后判断"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband": 10.0, "deadband_percent": 5.0})
        pp.process("p", 1000.0, 100.0)
        # 变化12: abs 12>=10 通过绝对死区, pct 1.2%<5% 被百分比死区拦截
        _, report = pp.process("p", 1012.0, 110.0)
        assert report is False
        # 变化100: abs 100>=10 通过, pct 10%>=5% 通过 -> 上报
        _, report = pp.process("p", 1100.0, 120.0)
        assert report is True


class TestPreprocessorAggregation:
    """测试时间窗口聚合各类型"""

    async def test_aggregation_max(self):
        """时间窗口聚合-max"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "max", "aggregate_window_sec": 10})
        now = time.time()
        pp.process("p", 20.0, now - 5)
        val, _ = pp.process("p", 30.0, now)
        assert val == 30.0

    async def test_aggregation_min(self):
        """时间窗口聚合-min"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "min", "aggregate_window_sec": 10})
        now = time.time()
        pp.process("p", 20.0, now - 5)
        val, _ = pp.process("p", 30.0, now)
        assert val == 20.0

    async def test_aggregation_sum(self):
        """时间窗口聚合-sum"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "sum", "aggregate_window_sec": 10})
        now = time.time()
        pp.process("p", 20.0, now - 5)
        val, _ = pp.process("p", 30.0, now)
        assert val == 50.0

    async def test_aggregation_last(self):
        """时间窗口聚合-last"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "last", "aggregate_window_sec": 10})
        now = time.time()
        pp.process("p", 20.0, now - 5)
        val, _ = pp.process("p", 30.0, now)
        assert val == 30.0

    async def test_aggregation_unknown_type(self):
        """时间窗口聚合-未知类型返回None(透传滤波值)"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "xyz", "aggregate_window_sec": 10})
        now = time.time()
        pp.process("p", 20.0, now - 5)
        val, report = pp.process("p", 30.0, now)
        assert report is True
        assert val == 30.0

    async def test_aggregation_window_eviction(self):
        """时间窗口聚合-超时点淘汰"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "avg", "aggregate_window_sec": 5})
        now = time.time()
        pp.process("p", 100.0, now - 100)
        val, report = pp.process("p", 20.0, now)
        assert report is True
        assert val == 20.0

    async def test_aggregation_invalid_window(self):
        """时间窗口聚合-窗口<=0不聚合"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "avg", "aggregate_window_sec": 0})
        val, report = pp.process("p", 20.0, 100.0)
        assert report is True
        assert val == 20.0

    async def test_aggregation_no_agg_config(self):
        """时间窗口聚合-无aggregate配置不聚合"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate_window_sec": 10})
        val, report = pp.process("p", 20.0, 100.0)
        assert report is True
        assert val == 20.0

    async def test_aggregation_single_point_returns_none(self):
        """时间窗口聚合-仅1个点返回None透传"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "avg", "aggregate_window_sec": 10})
        val, report = pp.process("p", 20.0, 100.0)
        assert report is True
        assert val == 20.0


class TestPreprocessorSlidingWindow:
    """测试 process 中的滑动窗口步骤"""

    async def test_sliding_window_first_returns_original(self):
        """滑动窗口-首点返回原值(无聚合)"""
        pp = DataPreprocessor()
        pp.configure("p", {"sliding_window_sec": 60, "sliding_window_type": "tumbling"})
        val, report = pp.process("p", 10.0, 100.0)
        assert report is True
        assert val == 10.0

    async def test_sliding_window_second_returns_avg(self):
        """滑动窗口-第二点返回均值替换"""
        pp = DataPreprocessor()
        pp.configure("p", {"sliding_window_sec": 60, "sliding_window_type": "tumbling"})
        pp.process("p", 10.0, 100.0)
        val, _ = pp.process("p", 20.0, 110.0)
        assert abs(val - 15.0) < 0.001

    async def test_sliding_window_disabled(self):
        """滑动窗口-window_sec<=0不启用"""
        pp = DataPreprocessor()
        pp.configure("p", {"sliding_window_sec": 0})
        val, _ = pp.process("p", 42.0, 100.0)
        assert val == 42.0

    async def test_sliding_window_session_type(self):
        """滑动窗口-session类型"""
        pp = DataPreprocessor()
        pp.configure("p", {"sliding_window_sec": 120, "sliding_window_type": "session", "session_gap_sec": 30})
        pp.process("p", 10.0, 100.0)
        val, _ = pp.process("p", 20.0, 110.0)
        assert abs(val - 15.0) < 0.001

    async def test_get_sliding_stats(self):
        """get_sliding_stats-获取统计"""
        pp = DataPreprocessor()
        pp.configure("p", {"sliding_window_sec": 60})
        pp.process("p", 10.0, 100.0)
        pp.process("p", 20.0, 110.0)
        stats = pp.get_sliding_stats("p")
        assert stats is not None
        assert stats["tumbling"]["count"] == 2

    async def test_get_sliding_stats_unknown(self):
        """get_sliding_stats-未知点返回None"""
        pp = DataPreprocessor()
        assert pp.get_sliding_stats("unknown") is None


class TestPreprocessorBatchInterpolation:
    """测试 process_batch 与缺失值插值"""

    async def test_process_batch_basic(self):
        """批量处理-无插值配置"""
        pp = DataPreprocessor()
        results = pp.process_batch("p", [10.0, 20.0, 30.0], [100.0, 110.0, 120.0])
        assert len(results) == 3
        for _val, report in results:
            assert report is True

    async def test_process_batch_length_mismatch(self):
        """批量处理-长度不一致返回空"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "linear"})
        results = pp.process_batch("p", [1.0, 2.0, 3.0], [100.0, 110.0])
        assert results == []

    async def test_process_batch_empty(self):
        """批量处理-空输入"""
        pp = DataPreprocessor()
        results = pp.process_batch("p", [], [])
        assert results == []

    async def test_interpolate_linear_in_batch(self):
        """批量插值-线性插值填充None"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "linear"})
        values = [10.0, None, 20.0]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        assert len(results) == 3
        assert results[1][0] is not None

    async def test_interpolate_linear_nan_in_batch(self):
        """批量插值-线性插值填充NaN"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "linear"})
        values = [10.0, float("nan"), 20.0]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        assert len(results) == 3
        assert results[1][0] is not None

    async def test_interpolate_previous_in_batch(self):
        """批量插值-previous方法"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "previous"})
        values = [10.0, None, None]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        assert len(results) == 3

    async def test_interpolate_next_in_batch(self):
        """批量插值-next方法"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "next"})
        values = [None, None, 30.0]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        assert len(results) == 3

    async def test_interpolate_average_in_batch(self):
        """批量插值-average方法"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "average", "interpolate_window": 5})
        values = [10.0, None, 30.0]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        assert len(results) == 3

    async def test_interpolate_spline_in_batch(self):
        """批量插值-spline方法"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "spline"})
        values = [0.0, None, 20.0]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        assert len(results) == 3

    async def test_interpolate_with_fallback(self):
        """批量插值-无法填充时使用fallback"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "linear", "interpolate_fallback": -1.0})
        values = [None, 20.0, 30.0]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        assert len(results) == 3

    async def test_interpolate_no_fallback_returns_none(self):
        """批量插值-无法填充且无fallback返回None导致process返回None"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "linear"})
        values = [None, 20.0, 30.0]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        assert len(results) == 3
        assert results[0] == (None, False)

    async def test_interpolate_empty_values(self):
        """批量插值-空values原样返回"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "linear"})
        results = pp.process_batch("p", [], [])
        assert results == []

    async def test_interp_linear_no_neighbors(self):
        """线性插值-无前后邻居返回None"""
        pp = DataPreprocessor()
        pp.configure("p", {"interpolate": "linear"})
        values = [None, None, None]
        timestamps = [100.0, 110.0, 120.0]
        results = pp.process_batch("p", values, timestamps)
        for val, report in results:
            assert val is None
            assert report is False


class TestPreprocessorDownsample:
    """测试 DataPreprocessor.downsample 静态方法"""

    async def test_downsample_empty(self):
        """降采样-空数据"""
        assert DataPreprocessor.downsample([]) == []

    async def test_downsample_small_data(self):
        """降采样-数据量<=3原样返回"""
        values = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
        assert DataPreprocessor.downsample(values) == values

    async def test_downsample_lttb_default(self):
        """降采样-LTTB默认阈值"""
        values = [(float(i), float(i)) for i in range(100)]
        result = DataPreprocessor.downsample(values, method="lttb")
        assert len(result) <= 100
        assert result[0] == values[0]
        assert result[-1] == values[-1]

    async def test_downsample_lttb_custom_threshold(self):
        """降采样-LTTB自定义阈值"""
        values = [(float(i), float(i)) for i in range(100)]
        result = DataPreprocessor.downsample(values, method="lttb", threshold=20)
        assert len(result) == 20

    async def test_downsample_minmax_default(self):
        """降采样-MinMax默认桶大小"""
        values = [(float(i), float(i)) for i in range(200)]
        result = DataPreprocessor.downsample(values, method="minmax")
        assert len(result) <= len(values)
        assert len(result) > 0

    async def test_downsample_minmax_custom_bucket(self):
        """降采样-MinMax自定义桶大小"""
        values = [(float(i), float(i)) for i in range(20)]
        result = DataPreprocessor.downsample(values, method="minmax", bucket_size=5)
        assert len(result) <= len(values)

    async def test_downsample_average_default(self):
        """降采样-均值默认桶大小"""
        values = [(float(i), float(i)) for i in range(200)]
        result = DataPreprocessor.downsample(values, method="average")
        assert len(result) <= len(values)
        assert len(result) > 0

    async def test_downsample_average_custom_bucket(self):
        """降采样-均值自定义桶大小"""
        values = [(float(i), float(i)) for i in range(20)]
        result = DataPreprocessor.downsample(values, method="average", bucket_size=5)
        assert len(result) == 4

    async def test_downsample_unknown_method(self):
        """降采样-未知方法原样返回"""
        values = [(float(i), float(i)) for i in range(100)]
        result = DataPreprocessor.downsample(values, method="unknown")
        assert result == values


class TestPreprocessorLifecycle:
    """测试配置管理与状态诊断"""

    async def test_remove_config(self):
        """remove_config-移除后透传"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband": 0.5})
        pp.process("p", 10.0, 100.0)
        pp.remove_config("p")
        val, report = pp.process("p", 10.1, 110.0)
        assert val == 10.1
        assert report is True

    async def test_remove_config_clears_state(self):
        """remove_config-清除所有相关状态"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "ema", "deadband": 0.5})
        pp.process("p", 10.0, 100.0)
        pp.remove_config("p")
        status = pp.get_status("p")
        assert status["has_filter_window"] is False
        assert status["has_last_value"] is False
        assert status["has_ema_state"] is False
        assert status["has_kalman_state"] is False

    async def test_remove_config_unknown_point(self):
        """remove_config-未知点不报错"""
        pp = DataPreprocessor()
        pp.remove_config("nonexistent")

    async def test_clear_all(self):
        """clear_all-清除所有状态"""
        pp = DataPreprocessor()
        pp.configure("p1", {"deadband": 0.5})
        pp.configure("p2", {"filter": "ema"})
        pp.process("p1", 10.0, 100.0)
        pp.process("p2", 20.0, 100.0)
        pp.clear_all()
        val1, _ = pp.process("p1", 99.0, 110.0)
        val2, _ = pp.process("p2", 88.0, 110.0)
        assert val1 == 99.0
        assert val2 == 88.0

    async def test_get_status_no_config(self):
        """get_status-无配置"""
        pp = DataPreprocessor()
        status = pp.get_status("p")
        assert status["point_key"] == "p"
        assert status["config_keys"] == []
        assert status["has_filter_window"] is False
        assert status["sliding_stats"] is None

    async def test_get_status_with_config(self):
        """get_status-有配置和状态"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "median_3", "filter_window": 3, "deadband": 0.5})
        pp.process("p", 10.0, 100.0)
        status = pp.get_status("p")
        assert "filter" in status["config_keys"]
        assert status["has_filter_window"] is True
        assert status["has_last_value"] is True

    async def test_get_status_kalman_state(self):
        """get_status-kalman状态"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "kalman"})
        pp.process("p", 10.0, 100.0)
        status = pp.get_status("p")
        assert status["has_kalman_state"] is True

    async def test_get_status_aggregate_window(self):
        """get_status-聚合窗口状态"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "avg", "aggregate_window_sec": 10})
        pp.process("p", 10.0, 100.0)
        status = pp.get_status("p")
        assert status["has_aggregate_window"] is True

    async def test_configure_overrides(self):
        """configure-覆盖旧配置"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband": 0.5})
        pp.configure("p", {"deadband": 100.0})
        pp.process("p", 10.0, 100.0)
        _, report = pp.process("p", 15.0, 110.0)
        assert report is False


class TestPreprocessorErrorHandling:
    """测试 process 各步骤异常容错"""

    async def test_kalman_exception_skipped(self):
        """Kalman异常-跳过继续处理"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "kalman"})
        with patch.object(pp, "_apply_kalman", side_effect=RuntimeError("boom")):
            val, report = pp.process("p", 42.0, 100.0)
        assert report is True
        assert val == 42.0

    async def test_transform_exception_skipped(self):
        """数据变换异常-跳过继续处理"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 2.0})
        with patch.object(pp, "_apply_transform", side_effect=RuntimeError("boom")):
            val, report = pp.process("p", 42.0, 100.0)
        assert report is True
        assert val == 42.0

    async def test_sliding_window_exception_skipped(self):
        """滑动窗口异常-跳过继续处理"""
        pp = DataPreprocessor()
        pp.configure("p", {"sliding_window_sec": 60})
        with patch.object(pp, "_apply_sliding_window", side_effect=RuntimeError("boom")):
            val, report = pp.process("p", 42.0, 100.0)
        assert report is True
        assert val == 42.0

    async def test_filter_exception_skipped(self):
        """滤波异常-使用原值继续处理"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "median_3", "filter_window": 3})
        with patch.object(pp, "_apply_filter", side_effect=RuntimeError("boom")):
            val, report = pp.process("p", 42.0, 100.0)
        assert report is True
        assert val == 42.0

    async def test_deadband_exception_defaults_report(self):
        """死区异常-默认上报"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband": 0.5})
        with patch.object(pp, "_apply_deadband", side_effect=RuntimeError("boom")):
            val, report = pp.process("p", 42.0, 100.0)
        assert report is True
        assert val == 42.0

    async def test_aggregation_exception_skipped(self):
        """聚合异常-跳过返回滤波值"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "avg", "aggregate_window_sec": 10})
        with patch.object(pp, "_apply_aggregation", side_effect=RuntimeError("boom")):
            val, report = pp.process("p", 42.0, 100.0)
        assert report is True
        assert val == 42.0

    async def test_transform_returns_none_skips(self):
        """数据变换返回None-process返回(None,False)"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 2.0})
        with patch.object(pp, "_apply_transform", return_value=None):
            val, report = pp.process("p", 42.0, 100.0)
        assert val is None
        assert report is False

    async def test_filter_returns_none_uses_processed(self):
        """滤波返回None-使用processed值"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "median_3", "filter_window": 3})
        with patch.object(pp, "_apply_filter", return_value=None):
            val, report = pp.process("p", 42.0, 100.0)
        assert report is True
        assert val == 42.0


class TestPreprocessorEdgeCases:
    """测试边界值与特殊场景"""

    async def test_timestamp_zero(self):
        """时间戳为0-不被误判为falsy"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "avg", "aggregate_window_sec": 100})
        val, report = pp.process("p", 10.0, 0)
        assert report is True
        assert val == 10.0

    async def test_timestamp_none_uses_time(self):
        """时间戳为None-使用time.time()"""
        pp = DataPreprocessor()
        pp.configure("p", {"aggregate": "avg", "aggregate_window_sec": 100})
        val, report = pp.process("p", 10.0, None)
        assert report is True
        assert val == 10.0

    async def test_negative_value(self):
        """负值处理"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 2.0, "transform_offset": 1.0})
        val, _ = pp.process("p", -5.0, 100.0)
        assert val == -9.0

    async def test_zero_value(self):
        """零值处理"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 2.0, "transform_offset": 5.0})
        val, _ = pp.process("p", 0.0, 100.0)
        assert val == 5.0

    async def test_large_value(self):
        """大值处理"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 1.0, "transform_offset": 0.0})
        val, _ = pp.process("p", 1e15, 100.0)
        assert val == 1e15

    async def test_small_value(self):
        """极小值处理"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 1.0, "transform_offset": 0.0})
        val, _ = pp.process("p", 1e-10, 100.0)
        assert abs(val - 1e-10) < 1e-15

    async def test_nan_value_passthrough(self):
        """NaN值-无配置透传"""
        pp = DataPreprocessor()
        val, report = pp.process("p", float("nan"), 100.0)
        assert report is True
        assert math.isnan(val)

    async def test_inf_value_transform(self):
        """Infinity值变换"""
        pp = DataPreprocessor()
        pp.configure("p", {"transform_scale": 2.0, "transform_offset": 0.0})
        val, _ = pp.process("p", float("inf"), 100.0)
        assert math.isinf(val)

    async def test_multiple_points_isolated(self):
        """多测点-状态独立隔离"""
        pp = DataPreprocessor()
        pp.configure("p1", {"deadband": 0.5})
        pp.configure("p2", {"deadband": 100.0})
        pp.process("p1", 10.0, 100.0)
        _, r1 = pp.process("p1", 10.3, 110.0)
        assert r1 is False
        _, r2 = pp.process("p2", 50.0, 110.0)
        assert r2 is True

    async def test_full_pipeline_all_features(self):
        """完整管道-所有特性组合"""
        pp = DataPreprocessor()
        pp.configure(
            "p",
            {
                "filter": "median_3",
                "filter_window": 3,
                "transform_scale": 2.0,
                "transform_offset": 1.0,
                "deadband": 0.1,
                "aggregate": "avg",
                "aggregate_window_sec": 100,
            },
        )
        now = time.time()
        results = []
        for i in range(5):
            r = pp.process("p", float(i), now + i)
            results.append(r)
        reported = [r for r in results if r[1]]
        assert len(reported) >= 1

    async def test_deadband_does_not_update_last_when_filtered(self):
        """死区不上报时不更新last值"""
        pp = DataPreprocessor()
        pp.configure("p", {"deadband": 1.0})
        pp.process("p", 10.0, 100.0)
        pp.process("p", 10.5, 110.0)
        _, report = pp.process("p", 11.5, 120.0)
        assert report is True

    async def test_kalman_then_deadband_chain(self):
        """Kalman滤波后接死区-完整链路"""
        pp = DataPreprocessor()
        pp.configure("p", {"filter": "kalman", "deadband": 0.001})
        pp.process("p", 10.0, 100.0)
        val, report = pp.process("p", 20.0, 110.0)
        assert report is True
        assert val is not None
