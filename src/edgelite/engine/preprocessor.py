"""边缘数据预处理模块 - 死区/滤波/聚合/插值/滑动窗口/降采样/变换"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Literal

from edgelite.constants import _PREPROCESSOR_MAX_POINTS

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# 数据插值策略
# ─────────────────────────────────────────────────────────────


def interpolate_linear(v1: float, v2: float, ratio: float) -> float:
    """线性插值"""
    return v1 + (v2 - v1) * ratio


def interpolate_previous(values: list[float], idx: int) -> float | None:
    """用前一个已知值填充"""
    for i in range(idx - 1, -1, -1):
        if values[i] is not None and not math.isnan(values[i]):
            return values[i]
    return None


def interpolate_next(values: list[float], idx: int) -> float | None:
    """用后一个已知值填充"""
    for i in range(idx + 1, len(values)):
        if values[i] is not None and not math.isnan(values[i]):
            return values[i]
    return None


def interpolate_average(values: list[float], idx: int, window: int = 3) -> float | None:
    """滑动窗口均值填充：前后各window/2个点的均值"""
    left_start = max(0, idx - window // 2)
    right_end = min(len(values), idx + window // 2 + 1)
    nearby = [
        values[i]
        for i in range(left_start, right_end)
        if i != idx and values[i] is not None and not math.isnan(values[i])
    ]
    if nearby:
        return sum(nearby) / len(nearby)
    return None


def interpolate_spline(values: list[float], idx: int) -> float | None:
    """分段线性插值（非三次样条，保留函数名以兼容调用方）"""
    valid_pairs: list[tuple[int, float]] = [(i, v) for i, v in enumerate(values) if v is not None and not math.isnan(v)]
    if len(valid_pairs) < 2:
        return None
    xs = [p[0] for p in valid_pairs]
    ys = [p[1] for p in valid_pairs]

    if idx <= xs[0]:
        return ys[0]
    if idx >= xs[-1]:
        return ys[-1]

    for i in range(len(xs) - 1):
        if xs[i] <= idx <= xs[i + 1]:
            t = (idx - xs[i]) / (xs[i + 1] - xs[i])
            return interpolate_linear(ys[i], ys[i + 1], t)
    return None


# ─────────────────────────────────────────────────────────────
# 降采样算法
# ─────────────────────────────────────────────────────────────


def downsample_lttb(values: list[tuple[float, float]], threshold: int) -> list[tuple[float, float]]:
    """Largest-Triangle-Three-Buckets (LTTB) 降采样

    Args:
        values: list of (x, y) tuples (time, value)
        threshold: target number of points after downsampling

    Returns:
        Downsampled list of (x, y) tuples
    """
    n = len(values)
    if threshold >= n or threshold < 3:
        return values

    sampled: list[tuple[float, float]] = []
    sampled.append(values[0])

    bucket_size = (n - 2) / (threshold - 2)

    a = 0
    for i in range(threshold - 2):
        avg_start = int((i + 1) * bucket_size) + 1
        avg_end = int((i + 2) * bucket_size) + 1
        avg_end = min(avg_end, n)

        avg_x, avg_y = 0.0, 0.0
        for j in range(avg_start, avg_end):
            avg_x += values[j][0]
            avg_y += values[j][1]
        count = avg_end - avg_start
        if count <= 0:  # FIXED-P2: 防止整数截断导致空桶时除以零
            avg_start = max(avg_start, 0)
            avg_x = values[avg_start][0] if avg_start < n else values[-1][0]
            avg_y = values[avg_start][1] if avg_start < n else values[-1][1]
        else:
            avg_x /= count
            avg_y /= count

        range_start = int(i * bucket_size) + 1
        range_end = int((i + 1) * bucket_size) + 1

        max_area = -1.0
        max_idx = range_start
        for j in range(range_start, min(range_end, n)):
            area = abs(
                (values[a][0] - avg_x) * (values[j][1] - values[a][1])
                - (values[a][0] - values[j][0]) * (avg_y - values[a][1])
            )
            if area > max_area:
                max_area = area
                max_idx = j

        sampled.append(values[max_idx])
        a = max_idx

    sampled.append(values[-1])
    return sampled


def downsample_minmax(values: list[tuple[float, float]], bucket_size: int) -> list[tuple[float, float]]:
    """Min-Max 降采样：每个桶保留最小值和最大值"""
    if not values or bucket_size < 2:
        return values

    result: list[tuple[float, float]] = []
    i = 0
    n = len(values)

    while i < n:
        bucket_end = min(i + bucket_size, n)
        bucket = values[i:bucket_end]
        min_pt = min(bucket, key=lambda p: p[1])
        max_pt = max(bucket, key=lambda p: p[1])
        result.append(min_pt)
        if min_pt != max_pt:
            result.append(max_pt)
        i = bucket_end

    return result


def downsample_average(values: list[tuple[float, float]], bucket_size: int) -> list[tuple[float, float]]:
    """均值降采样：每个桶用均值替代"""
    if not values or bucket_size < 1:
        return values

    result: list[tuple[float, float]] = []
    i = 0
    n = len(values)

    while i < n:
        bucket_end = min(i + bucket_size, n)
        bucket = values[i:bucket_end]
        avg_x = sum(p[0] for p in bucket) / len(bucket)
        avg_y = sum(p[1] for p in bucket) / len(bucket)
        result.append((avg_x, avg_y))
        i = bucket_end

    return result


# ─────────────────────────────────────────────────────────────
# 数据变换
# ─────────────────────────────────────────────────────────────


def transform_scale(value: float, scale: float, offset: float) -> float:
    """比例变换: y = x * scale + offset"""
    return value * scale + offset


def transform_linearize(value: float, table: list[tuple[float, float]]) -> float:
    """分段线性化：查表插值"""
    if not table:
        return value
    if value <= table[0][0]:
        return table[0][1]
    if value >= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        if table[i][0] <= value <= table[i + 1][0]:
            t = (value - table[i][0]) / (table[i + 1][0] - table[i][0])
            return interpolate_linear(table[i][1], table[i + 1][1], t)
    return value


def transform_round(value: float, decimals: int) -> float:
    """四舍五入到指定小数位"""
    mult = 10**decimals
    return math.floor(value * mult + 0.5) / mult


# ─────────────────────────────────────────────────────────────
# 滑动窗口聚合
# ─────────────────────────────────────────────────────────────


class SlidingWindowAggregator:
    """滑动窗口聚合器 - 支持 Tumbling / Sliding / Session 三种窗口"""

    def __init__(self, max_points: int = _PREPROCESSOR_MAX_POINTS):
        self._windows: dict[str, dict] = {}  # point_key -> window state
        self._max_points = max_points

    def add(
        self,
        point_key: str,
        value: float,
        timestamp: float,
        window_sec: int,
        window_type: Literal["tumbling", "sliding", "session"] = "tumbling",
        session_gap_sec: int = 60,
    ) -> float | None:
        """添加数据点，返回聚合结果（无聚合结果返回None）

        Args:
            point_key: 测点标识
            value: 数据值
            timestamp: Unix时间戳（秒）
            window_sec: 窗口大小（秒）
            window_type: 窗口类型
            session_gap_sec: Session窗口的间隔阈值（秒）

        Returns:
            聚合值，或None（窗口数据不足）
        """
        if window_sec <= 0:
            return None

        if point_key not in self._windows:
            self._windows[point_key] = {
                "tumbling": deque(maxlen=self._max_points),
                "sliding": deque(maxlen=self._max_points),
                "session": deque(maxlen=self._max_points),
                "session_last_ts": 0.0,
            }

        state = self._windows[point_key]
        window = state[window_type]

        if window_type == "session":
            if state["session_last_ts"] > 0 and (timestamp - state["session_last_ts"]) > session_gap_sec:
                window.clear()
            state["session_last_ts"] = timestamp

        cutoff = timestamp - window_sec
        while window and window[0][0] < cutoff:
            window.popleft()

        window.append((timestamp, value))

        if len(window) < 2:
            return None

        values = [v for _, v in window]
        return self._aggregate(values, window_type)

    def _aggregate(self, values: list[float], window_type: str) -> float:
        """计算聚合值 - 每个窗口类型可自定义聚合函数"""
        return sum(values) / len(values)

    def get_stats(self, point_key: str) -> dict | None:
        """获取当前窗口统计信息"""
        if point_key not in self._windows:
            return None
        state = self._windows[point_key]
        stats = {}
        for wtype in ("tumbling", "sliding", "session"):
            window = state[wtype]
            if window:
                vals = [v for _, v in window]
                stats[wtype] = {
                    "count": len(vals),
                    "avg": sum(vals) / len(vals),
                    "min": min(vals),
                    "max": max(vals),
                    "latest": vals[-1],
                }
        return stats

    def clear(self, point_key: str | None = None) -> None:
        """清除窗口数据"""
        if point_key:
            self._windows.pop(point_key, None)
        else:
            self._windows.clear()


# ─────────────────────────────────────────────────────────────
# 主预处理类
# ─────────────────────────────────────────────────────────────


class DataPreprocessor:
    """数据预处理管道

    完整处理顺序：
    原始值 → 缺失值插值 → 滑动窗口统计 → 线性变换 → 中值滤波 → 死区过滤 → 时间窗口聚合

    新增能力：
    - 数据插值：linear / previous / next / average / spline
    - 滑动窗口聚合：tumbling / sliding / session
    - 降采样：LTTB / minmax / average
    - 数据变换：scale+offset / table linearization / round
    - 复杂滤波：moving_average / exponential_smoothing / kalman_filter
    """

    def __init__(self):
        self._filter_windows: dict[str, deque] = {}
        self._last_values: dict[str, float] = {}
        self._aggregate_windows: dict[str, deque] = {}
        self._configs: dict[str, dict] = {}
        self._sliding_aggregator = SlidingWindowAggregator()
        self._transform_state: dict[str, float] = {}
        self._ema_state: dict[str, float] = {}
        self._kalman_state: dict[str, tuple[float, float]] = {}  # (estimate, variance)
        # FIXED(一般): 原问题-self.preprocess_skipped 实例变量在并发调用时会被覆盖
        # 修复-移除实例变量，process() 返回值已包含足够信息，无需共享状态

    # ──────────────────────────────────────────────────────
    # 配置
    # ──────────────────────────────────────────────────────
    def configure(self, point_key: str, config: dict) -> None:
        """配置测点预处理参数

        config: {
            "deadband": float or None,
            "deadband_percent": float or None,
            "filter": str or None,               # "median_3", "median_5", "moving_avg", "ema", "kalman"
            "filter_window": int or None,
            "aggregate": str or None,            # "avg", "max", "min", "sum", "last"
            "aggregate_window_sec": int or None,
            # ── 新增配置 ──
            "interpolate": str or None,          # "linear", "previous", "next", "average", "spline"
            "interpolate_window": int,            # 插值窗口大小（用于average插值）
            "transform_scale": float,             # scale factor (y = x * scale + offset)
            "transform_offset": float,             # offset factor
            "transform_round": int or None,       # 四舍五入位数，None=不处理
            "sliding_window_sec": int,            # 滑动窗口大小（秒）
            "sliding_window_type": str,           # "tumbling", "sliding", "session"
            "sliding_agg": str,                  # 滑动窗口内聚合函数
            "downsample_method": str,            # "lttb", "minmax", "average"
            "downsample_threshold": int,          # 降采样后的点数
            "kalman_process_noise": float,        # Kalman filter process noise (Q)
            "kalman_measurement_noise": float,    # Kalman filter measurement noise (R)
        }
        """
        self._configs[point_key] = config

    # ──────────────────────────────────────────────────────
    # 主处理入口
    # ──────────────────────────────────────────────────────
    def process(self, point_key: str, value: float, timestamp: float | None = None) -> tuple[float | None, bool]:
        """处理数据值，返回(处理后的值, 是否应上报)
        如果死区过滤判定不需要上报，返回(None, False)
        异常容错：每个步骤try/except包裹，异常时跳过当前步骤继续流转
        """
        config = self._configs.get(point_key, {})
        if not config:
            return value, True

        ts = (
            time.time() if timestamp is None else timestamp
        )  # FIXED-P2: 原代码 `timestamp or time.time()` 会将合法的 0 时间戳误判为 falsy
        processed = value

        # 1. Kalman filter (先验滤波)
        if config.get("filter") == "kalman":
            try:
                processed = self._apply_kalman(point_key, processed, config)
            except Exception as e:
                logger.warning("Preprocessor Kalman滤波异常(point=%s): %s，跳过", point_key, e)

        # 2. 数据变换：比例 + 线性化
        try:
            transformed = self._apply_transform(point_key, processed, config)
            if transformed is None:
                return None, False
            processed = transformed
        except Exception as e:
            logger.warning("Preprocessor数据变换异常(point=%s): %s，跳过", point_key, e)

        # 3. 滑动窗口聚合（独立，不依赖滤波链）
        try:
            sliding_result = self._apply_sliding_window(point_key, processed, ts, config)
        except Exception as e:
            logger.warning("Preprocessor滑动窗口异常(point=%s): %s，跳过", point_key, e)
            sliding_result = None
        if sliding_result is not None:
            # 滑动窗口结果作为并行统计替换 processed，继续后续滤波/死区/聚合处理
            processed = sliding_result

        # 4. 中值/EMA/MovingAvg 滤波
        try:
            filtered = self._apply_filter(point_key, processed, config)
            if filtered is None:
                filtered = processed
        except Exception as e:
            logger.warning("Preprocessor滤波异常(point=%s): %s，跳过", point_key, e)
            filtered = processed

        # 5. 死区过滤
        try:
            should_report = self._apply_deadband(point_key, filtered, config)
        except Exception as e:
            logger.warning("Preprocessor死区过滤异常(point=%s): %s，跳过(默认上报)", point_key, e)
            should_report = True
        if not should_report:
            return None, False

        # 6. 时间窗口聚合
        try:
            aggregated = self._apply_aggregation(point_key, filtered, ts, config)
        except Exception as e:
            logger.warning("Preprocessor时间窗口聚合异常(point=%s): %s，跳过", point_key, e)
            aggregated = None
        if aggregated is not None:
            return aggregated, True

        return filtered, True

    def process_batch(
        self, point_key: str, values: list[float], timestamps: list[float]
    ) -> list[tuple[float | None, bool]]:
        """批量处理多个数据点（带插值）

        Args:
            point_key: 测点标识
            values: 原始值列表
            timestamps: 对应时间戳列表

        Returns:
            处理结果列表 [(值, 是否上报), ...]
        """
        config = self._configs.get(point_key, {})

        # 缺失值插值
        if config.get("interpolate"):
            values = self._interpolate_missing(point_key, values, config)

        if len(values) != len(timestamps):
            logger.error(
                "Preprocessor process_batch 输入长度不一致(point=%s): values=%d, timestamps=%d",
                point_key,
                len(values),
                len(timestamps),
            )
            return []

        results = []
        for _i, (v, ts) in enumerate(zip(values, timestamps, strict=True)):
            result = self.process(point_key, v, ts)
            results.append(result)
        return results

    # ──────────────────────────────────────────────────────
    # 插值
    # ──────────────────────────────────────────────────────
    def _interpolate_missing(self, point_key: str, values: list[float], config: dict) -> list[float]:
        """对批量数据进行缺失值插值"""
        interp_method = config.get("interpolate")
        if not interp_method or not values:
            return values

        result = list(values)
        for i, v in enumerate(values):
            if v is None or (isinstance(v, float) and math.isnan(v)):
                filled = None
                if interp_method == "linear":
                    filled = self._interp_linear(result, i)
                elif interp_method == "previous":
                    filled = interpolate_previous(result, i)
                elif interp_method == "next":
                    filled = interpolate_next(result, i)
                elif interp_method == "average":
                    filled = interpolate_average(result, i, config.get("interpolate_window", 3))
                elif interp_method == "spline":
                    filled = interpolate_spline(result, i)
                result[i] = filled if filled is not None else config.get("interpolate_fallback")  # type: ignore[assignment]
        return result

    def _interp_linear(self, values: list[float], idx: int) -> float | None:
        """线性插值：找前后最近已知点插值"""
        prev_val, prev_idx = None, None
        next_val, next_idx = None, None
        for i in range(idx - 1, -1, -1):
            v = values[i]
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                prev_val, prev_idx = v, i
                break
        for i in range(idx + 1, len(values)):
            v = values[i]
            if v is not None and not (isinstance(v, float) and math.isnan(v)):
                next_val, next_idx = v, i
                break
        if prev_val is None or next_val is None or prev_idx is None or next_idx is None:
            return None
        ratio = (idx - prev_idx) / (next_idx - prev_idx)
        return interpolate_linear(prev_val, next_val, ratio)

    # ──────────────────────────────────────────────────────
    # 滑动窗口
    # ──────────────────────────────────────────────────────
    def _apply_sliding_window(self, point_key: str, value: float, ts: float, config: dict) -> float | None:
        """滑动窗口聚合"""
        window_sec = config.get("sliding_window_sec", 0)
        if not window_sec or window_sec <= 0:
            return None

        window_type = config.get("sliding_window_type", "tumbling")
        session_gap = config.get("session_gap_sec", 60)

        return self._sliding_aggregator.add(
            point_key=point_key,
            value=value,
            timestamp=ts,
            window_sec=window_sec,
            window_type=window_type,
            session_gap_sec=session_gap,
        )

    def get_sliding_stats(self, point_key: str) -> dict | None:
        """获取滑动窗口统计信息"""
        return self._sliding_aggregator.get_stats(point_key)

    # ──────────────────────────────────────────────────────
    # 数据变换
    # ──────────────────────────────────────────────────────
    def _apply_transform(self, point_key: str, value: float, config: dict) -> float | None:
        """应用数据变换"""
        scale = config.get("transform_scale", 1.0)
        offset = config.get("transform_offset", 0.0)
        rounded = config.get("transform_round")

        result = value
        if scale != 1.0 or offset != 0.0:
            result = transform_scale(result, scale, offset)

        if rounded is not None:
            result = transform_round(result, rounded)

        return result

    # ──────────────────────────────────────────────────────
    # 滤波
    # ──────────────────────────────────────────────────────
    def _apply_filter(self, point_key: str, value: float, config: dict) -> float | None:
        """应用滤波（中值/EMA/MovingAvg）"""
        filter_type = config.get("filter")
        if not filter_type or filter_type == "kalman":
            return value  # Kalman已在上层处理

        if filter_type.startswith("median_"):
            return self._apply_median_filter(point_key, value, config)
        elif filter_type == "moving_avg":
            return self._apply_moving_average(point_key, value, config)
        elif filter_type == "ema":
            return self._apply_ema(point_key, value, config)
        return value

    def _apply_median_filter(self, point_key: str, value: float, config: dict) -> float | None:
        """中值滤波"""
        parts = config.get("filter", "median_3").split("_")
        window_size = config.get("filter_window", int(parts[1]) if len(parts) > 1 else 5)
        if window_size < 1:  # FIXED-P2: 防止 maxlen=0 的 deque 丢弃所有数据
            return value

        if point_key not in self._filter_windows:
            self._filter_windows[point_key] = deque(maxlen=window_size)
        elif self._filter_windows[point_key].maxlen != window_size:
            old_data = list(self._filter_windows[point_key])
            self._filter_windows[point_key] = deque(old_data[-window_size:], maxlen=window_size)

        window = self._filter_windows[point_key]
        window.append(value)

        if len(window) < 3:
            return value

        sorted_vals = sorted(window)
        return sorted_vals[len(sorted_vals) // 2]

    def _apply_moving_average(self, point_key: str, value: float, config: dict) -> float | None:
        """滑动均值滤波"""
        window_size = config.get("filter_window", 5)
        if window_size < 1:
            return value

        if point_key not in self._filter_windows:
            self._filter_windows[point_key] = deque(maxlen=window_size)
        elif self._filter_windows[point_key].maxlen != window_size:
            old_data = list(self._filter_windows[point_key])
            self._filter_windows[point_key] = deque(old_data[-window_size:], maxlen=window_size)

        window = self._filter_windows[point_key]
        window.append(value)
        return sum(window) / len(window)

    def _apply_ema(self, point_key: str, value: float, config: dict) -> float | None:
        """指数移动平均滤波"""
        alpha = config.get("ema_alpha", 0.3)  # 平滑系数，默认0.3
        if not (0 < alpha <= 1):
            alpha = 0.3

        last = self._ema_state.get(point_key)
        if last is None:
            self._ema_state[point_key] = value
            return value

        ema = alpha * value + (1 - alpha) * last
        self._ema_state[point_key] = ema
        return ema

    def _apply_kalman(self, point_key: str, value: float, config: dict) -> float:
        """一维Kalman滤波"""
        q = config.get("kalman_process_noise", 0.001)  # 过程噪声
        r = config.get("kalman_measurement_noise", 0.01)  # 测量噪声

        if point_key not in self._kalman_state:
            self._kalman_state[point_key] = (value, 1.0)
            return value

        estimate, variance = self._kalman_state[point_key]

        # Prediction
        pred_estimate = estimate
        pred_variance = variance + q

        # Update
        k = pred_variance / (pred_variance + r)  # Kalman gain
        estimate = pred_estimate + k * (value - pred_estimate)
        variance = (1 - k) * pred_variance

        self._kalman_state[point_key] = (estimate, variance)
        return estimate

    # ──────────────────────────────────────────────────────
    # 死区过滤
    # ──────────────────────────────────────────────────────
    def _apply_deadband(self, point_key: str, value: float, config: dict) -> bool:
        """死区过滤，返回是否应上报"""
        deadband = config.get("deadband")
        deadband_pct = config.get("deadband_percent")

        last = self._last_values.get(point_key)
        if last is None:
            self._last_values[point_key] = value
            return True

        if deadband is not None and deadband > 0 and abs(value - last) < deadband:
            return False

        if deadband_pct is not None and deadband_pct > 0:
            if abs(last) < 1e-6:
                if abs(value - last) < deadband_pct:
                    return False
            else:
                if abs(value - last) / abs(last) * 100 < deadband_pct:
                    return False

        self._last_values[point_key] = value
        return True

    # ──────────────────────────────────────────────────────
    # 时间窗口聚合
    # ──────────────────────────────────────────────────────
    MAX_AGGREGATE_POINTS = _PREPROCESSOR_MAX_POINTS

    def _apply_aggregation(self, point_key: str, value: float, ts: float, config: dict) -> float | None:
        """时间窗口聚合"""
        agg = config.get("aggregate")
        agg_window = config.get("aggregate_window_sec")
        if not agg or not agg_window or agg_window <= 0:
            return None

        if point_key not in self._aggregate_windows:
            self._aggregate_windows[point_key] = deque(maxlen=_PREPROCESSOR_MAX_POINTS)

        window = self._aggregate_windows[point_key]
        window.append((ts, value))

        # FIXED-P2: 移除冗余的手动清理逻辑 - deque 已设置 maxlen=_PREPROCESSOR_MAX_POINTS，
        # 会自动淘汰旧数据，手动 popleft 不仅多余还浪费 CPU
        cutoff = ts - agg_window
        while window and window[0][0] < cutoff:
            window.popleft()

        if len(window) < 2:
            return None

        values = [v for _, v in window]
        if agg.startswith("avg"):
            return sum(values) / len(values)
        elif agg.startswith("max"):
            return max(values)
        elif agg.startswith("min"):
            return min(values)
        elif agg.startswith("sum"):
            return sum(values)
        elif agg.startswith("last"):
            return values[-1]
        return None

    # ──────────────────────────────────────────────────────
    # 降采样（静态工具方法）
    # ──────────────────────────────────────────────────────
    @staticmethod
    def downsample(
        values: list[tuple[float, float]],
        method: Literal["lttb", "minmax", "average"] = "lttb",
        threshold: int | None = None,
        bucket_size: int | None = None,
    ) -> list[tuple[float, float]]:
        """对批量时序数据进行降采样

        Args:
            values: list of (timestamp, value) tuples
            method: 降采样算法
            threshold: LTTB的目标点数
            bucket_size: minmax/average的桶大小

        Returns:
            降采样后的数据
        """
        if not values or len(values) <= 3:
            return values

        if method == "lttb":
            return downsample_lttb(values, threshold or max(3, len(values) // 10))
        elif method == "minmax":
            return downsample_minmax(values, bucket_size or max(2, len(values) // 100))
        elif method == "average":
            return downsample_average(values, bucket_size or max(2, len(values) // 100))
        return values

    # ──────────────────────────────────────────────────────
    # 清理
    # ──────────────────────────────────────────────────────
    def remove_config(self, point_key: str) -> None:
        """移除测点配置"""
        self._configs.pop(point_key, None)
        self._filter_windows.pop(point_key, None)
        self._last_values.pop(point_key, None)
        self._aggregate_windows.pop(point_key, None)
        self._transform_state.pop(point_key, None)
        self._ema_state.pop(point_key, None)
        self._kalman_state.pop(point_key, None)
        self._sliding_aggregator.clear(point_key)

    def clear_all(self) -> None:
        """清除所有状态"""
        self._configs.clear()
        self._filter_windows.clear()
        self._last_values.clear()
        self._aggregate_windows.clear()
        self._transform_state.clear()
        self._ema_state.clear()
        self._kalman_state.clear()
        self._sliding_aggregator.clear()

    # ──────────────────────────────────────────────────────
    # 诊断
    # ──────────────────────────────────────────────────────
    def get_status(self, point_key: str) -> dict:
        """获取测点预处理状态"""
        config = self._configs.get(point_key, {})
        return {
            "point_key": point_key,
            "config_keys": list(config.keys()),
            "has_filter_window": point_key in self._filter_windows,
            "has_last_value": point_key in self._last_values,
            "has_aggregate_window": point_key in self._aggregate_windows,
            "has_ema_state": point_key in self._ema_state,
            "has_kalman_state": point_key in self._kalman_state,
            "sliding_stats": self._sliding_aggregator.get_stats(point_key),
        }
