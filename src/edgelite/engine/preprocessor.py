"""边缘数据预处理模块 - 死区/滤波/聚合"""

from __future__ import annotations
import logging
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


class DataPreprocessor:
    """数据预处理管道：原始值 → 中值滤波 → 死区过滤 → 时间窗口聚合"""

    def __init__(self):
        self._filter_windows: dict[str, deque] = {}
        self._last_values: dict[str, float] = {}
        self._aggregate_windows: dict[str, deque] = {}
        self._configs: dict[str, dict] = {}

    def configure(self, point_key: str, config: dict) -> None:
        """配置测点预处理参数
        config: {
            "deadband": float or None,
            "deadband_percent": float or None,
            "filter": str or None,               # "median_3", "median_5"
            "filter_window": int or None,
            "aggregate": str or None,            # "avg_Ns", "max_Ns", "min_Ns"
            "aggregate_window_sec": int or None,
        }
        """
        self._configs[point_key] = config

    def process(self, point_key: str, value: float, timestamp: Optional[float] = None) -> tuple[Optional[float], bool]:
        """处理数据值，返回(处理后的值, 是否应上报)
        如果死区过滤判定不需要上报，返回(None, False)
        """
        config = self._configs.get(point_key, {})
        if not config:
            return value, True

        ts = timestamp or time.time()

        filtered = self._apply_filter(point_key, value, config)
        if filtered is None:
            return value, True

        should_report = self._apply_deadband(point_key, filtered, config)
        if not should_report:
            return None, False

        aggregated = self._apply_aggregation(point_key, filtered, ts, config)
        if aggregated is not None:
            return aggregated, True

        return filtered, True

    def _apply_filter(self, point_key: str, value: float, config: dict) -> Optional[float]:
        """中值滤波"""
        filter_type = config.get("filter")
        if not filter_type:
            return value

        if filter_type.startswith("median_"):
            window_size = config.get("filter_window", int(filter_type.split("_")[1]))
        else:
            return value

        if point_key not in self._filter_windows:
            self._filter_windows[point_key] = deque(maxlen=window_size)
        elif self._filter_windows[point_key].maxlen != window_size:
            del self._filter_windows[point_key]
            self._filter_windows[point_key] = deque(maxlen=window_size)

        window = self._filter_windows[point_key]
        window.append(value)

        if len(window) < 3:
            return value

        sorted_vals = sorted(window)
        return sorted_vals[len(sorted_vals) // 2]

    def _apply_deadband(self, point_key: str, value: float, config: dict) -> bool:
        """死区过滤，返回是否应上报"""
        deadband = config.get("deadband")
        deadband_pct = config.get("deadband_percent")

        last = self._last_values.get(point_key)
        if last is None:
            self._last_values[point_key] = value
            return True

        if deadband is not None and deadband > 0:
            if abs(value - last) < deadband:
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

    MAX_AGGREGATE_POINTS = 10000

    def _apply_aggregation(self, point_key: str, value: float, ts: float, config: dict) -> Optional[float]:
        """时间窗口聚合"""
        agg = config.get("aggregate")
        agg_window = config.get("aggregate_window_sec")
        if not agg or not agg_window or agg_window <= 0:
            return None

        if point_key not in self._aggregate_windows:
            self._aggregate_windows[point_key] = deque()

        window = self._aggregate_windows[point_key]
        window.append((ts, value))

        if len(window) > self.MAX_AGGREGATE_POINTS:
            del window[:self.MAX_AGGREGATE_POINTS // 2]

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

    def remove_config(self, point_key: str) -> None:
        """移除测点配置"""
        self._configs.pop(point_key, None)
        self._filter_windows.pop(point_key, None)
        self._last_values.pop(point_key, None)
        self._aggregate_windows.pop(point_key, None)

    def clear_all(self) -> None:
        self._configs.clear()
        self._filter_windows.clear()
        self._last_values.clear()
        self._aggregate_windows.clear()
