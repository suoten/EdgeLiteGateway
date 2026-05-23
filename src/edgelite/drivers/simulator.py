"""模拟器驱动 - 无需外部连接，纯内存实现"""

from __future__ import annotations

import logging
import math
import random
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class SimulatorDriver(DriverPlugin):
    """模拟器驱动，生成模拟测点数据"""

    plugin_name = "simulator"
    plugin_version = "0.1.0"
    supported_protocols = ["simulator"]

    def __init__(self):
        self._running = False
        # device_id -> {point_name -> point_config}
        self._devices: dict[str, dict[str, dict]] = {}
        # 随机游走状态
        self._walk_state: dict[str, float] = {}
        # 正弦波相位
        self._sine_phase: dict[str, float] = {}

    async def start(self, config: dict) -> None:
        """启动模拟器"""
        self._running = True
        logger.info("模拟器驱动启动")

    async def stop(self) -> None:
        """停止模拟器"""
        self._running = False
        logger.info("模拟器驱动停止")

    def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加模拟设备"""
        if points is None:
            points = []
        self._devices[device_id] = {}
        for pt in points:
            name = pt.get("name")  # FIXED: 原问题-pt["name"]硬访问
            if name is None:
                continue
            self._devices[device_id][name] = pt
            # 初始化随机游走状态
            mid = (pt.get("min", 0) + pt.get("max", 100)) / 2
            self._walk_state[f"{device_id}:{name}"] = mid
            self._sine_phase[f"{device_id}:{name}"] = random.uniform(0, 2 * math.pi)

    def remove_device(self, device_id: str) -> None:
        """移除模拟设备"""
        self._devices.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """生成模拟测点值"""
        if device_id not in self._devices:
            return {}

        result = {}
        device_points = self._devices[device_id]
        for point_name in points:
            pt_config = device_points.get(point_name)
            if pt_config is None:
                continue

            value = self._generate_value(device_id, point_name, pt_config)
            result[point_name] = value

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """模拟写入（固定值模式）"""
        key = f"{device_id}:{point}"
        self._walk_state[key] = float(value)
        return True

    def _generate_value(self, device_id: str, point_name: str, config: dict) -> float:
        """根据模式生成模拟值"""
        mode = config.get("mode", "random")
        min_val = config.get("min", 0.0)
        max_val = config.get("max", 100.0)
        key = f"{device_id}:{point_name}"

        if mode == "fixed":
            return (min_val + max_val) / 2

        elif mode == "sine":
            phase = self._sine_phase.get(key, 0)
            collect_interval = config.get("collect_interval", 1.0)  # FIXED-P2: 正弦波相位步长与调用频率耦合，采集间隔非1秒时周期错误，现根据实际间隔计算步长
            phase += 2 * math.pi * collect_interval / 60  # 周期60秒，步长=2π*interval/60
            self._sine_phase[key] = phase
            mid = (min_val + max_val) / 2
            amp = (max_val - min_val) / 2
            return mid + amp * math.sin(phase)

        elif mode == "random_walk":
            # 随机游走
            current: float = self._walk_state.get(key, (min_val + max_val) / 2)
            step = (max_val - min_val) * 0.02  # 步长为范围的2%
            current += random.gauss(0, step)
            # 限制在范围内
            current = max(min_val, min(max_val, current))
            self._walk_state[key] = current
            return current

        else:  # random
            return random.uniform(min_val, max_val)
