"""FANUC CNC驱动 - 基于pyfanuc库，通过FOCAS库连接FANUC数控机床

FANUC是工业数控系统市场占有率最高的品牌（约60%），
FOCAS (FANUC Open CNC API Specifications) 是其开放API。
支持0i-D/0i-F/16i/18i/30i/31i/32i等系列。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class FanucCncDriver(DriverPlugin):
    """FANUC CNC驱动

    配置参数:
        ip: CNC控制器IP地址
        port: FOCAS端口 (默认8193)
        timeout: 连接超时秒 (默认10)
    """

    plugin_name = "fanuc_cnc"
    plugin_version = "1.0.0"
    supported_protocols = ["fanuc", "focas"]
    config_schema = {
        "description": "FANUC CNC FOCAS protocol, supports reading machine status and coordinates",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {"name": "host", "type": "string", "label": "IP Address", "description": "CNC controller IP address", "default": "192.168.1.1", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "port", "type": "integer", "label": "Port", "description": "FOCAS port, default 8193", "default": 8193},  # FIXED: 原问题-中文硬编码label/description
            {"name": "timeout", "type": "integer", "label": "Timeout (s)", "description": "Connection timeout", "default": 5},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        self._running = False
        self._handle = None
        self._config: dict = {}
        self._lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        """启动FANUC CNC连接"""
        try:
            from pyfanuc import FocasClient
        except ImportError:
            raise ImportError(
                "pyfanuc未安装，请执行: pip install pyfanuc。"
                "注意：FOCAS库需要FANUC官方提供的fwlib32.dll/fwlib64.dll"
            ) from None

        self._config = config
        ip = config.get("ip", "")
        port = int(config.get("port", 8193))
        timeout = int(config.get("timeout", 10))

        if not ip:
            raise ValueError("FANUC CNC驱动配置缺少ip参数")

        try:
            self._handle = FocasClient(ip, port=port, timeout=timeout)
            self._handle.connect()
            self._running = True
            logger.info("FANUC CNC连接成功: %s:%d", ip, port)
        except Exception as e:
            logger.error("FANUC CNC连接失败: %s - %s", ip, e)
            raise

    async def stop(self) -> None:
        """停止FANUC CNC驱动"""
        self._running = False
        if self._handle:
            try:
                await asyncio.to_thread(self._handle.disconnect)
            except Exception as e:
                logger.warning("FANUC CNC断开异常: %s", e)
            self._handle = None
        logger.info("FANUC CNC驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取FANUC CNC数据

        预定义测点名称:
            - cnc_status: CNC运行状态
            - cnc_position: 各轴当前位置
            - cnc_speed: 各轴速度
            - cnc_feedrate: 进给速度
            - cnc_spindle_speed: 主轴转速
            - cnc_program: 当前程序号
            - cnc_alarm: 报警信息
            - cnc_tool: 当前刀号
        或使用自定义地址: "axis.X.pos" (X轴位置), "param.100" (参数号100)
        """
        if not self._running or not self._handle:
            return {}

        result = {}
        async with self._lock:
            for point_name in points:
                try:
                    value = await asyncio.to_thread(self._read_point, point_name)
                    result[point_name] = value
                except Exception as e:
                    logger.warning("FANUC读取失败 %s: %s", point_name, e)
                    result[point_name] = None
        return result

    def _read_point(self, name: str) -> Any:
        """同步读取单个CNC数据"""
        name_lower = name.lower()

        if name_lower == "cnc_status":
            return self._handle.read_cnc_status()
        elif name_lower == "cnc_position":
            return self._handle.read_cnc_position()
        elif name_lower == "cnc_speed":
            return self._handle.read_cnc_speed()
        elif name_lower == "cnc_feedrate":
            return self._handle.read_cnc_feedrate()
        elif name_lower == "cnc_spindle_speed":
            return self._handle.read_cnc_spindle_speed()
        elif name_lower == "cnc_program":
            return self._handle.read_cnc_program()
        elif name_lower == "cnc_alarm":
            return self._handle.read_cnc_alarm()
        elif name_lower == "cnc_tool":
            return self._handle.read_cnc_tool()
        elif name_lower.startswith("axis."):
            # axis.X.pos / axis.X.speed / axis.X.load
            parts = name.split(".")
            if len(parts) >= 3:
                axis = parts[1].upper()
                prop = parts[2].lower()
                pos_data = self._handle.read_cnc_position()
                axis_idx = ord(axis) - ord("X")
                if prop == "pos" and axis_idx < len(pos_data):
                    return pos_data[axis_idx]
                elif prop == "speed":
                    speed_data = self._handle.read_cnc_speed()
                    if axis_idx < len(speed_data):
                        return speed_data[axis_idx]
        elif name_lower.startswith("param."):
            param_num = int(name.split(".")[1])
            return self._handle.read_cnc_parameter(param_num)

        raise ValueError(f"未知的FANUC测点: {name}")

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入FANUC CNC参数（谨慎使用）"""
        if not self._running or not self._handle:
            return False

        try:
            async with self._lock:
                if point.lower().startswith("param."):
                    param_num = int(point.split(".")[1])
                    await asyncio.to_thread(self._handle.write_cnc_parameter, param_num, int(value))
                    return True
                logger.warning("FANUC CNC仅支持写入参数: %s", point)
                return False
        except Exception as e:
            logger.error("FANUC写入失败 %s: %s", point, e)
            return False

    def _read_points_batch(self, points: list[str]) -> dict[str, Any]:
        """同步批量读取（单次to_thread调用，减少线程切换开销）"""
        result = {}
        for p in points:
            try:
                result[p] = self._read_point(p)
            except Exception:
                result[p] = None
        return result

    async def discover_devices(self, config: dict) -> list[dict]:
        """FANUC CNC不支持自动发现"""
        return []
