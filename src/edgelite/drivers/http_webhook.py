"""HTTP/Webhook接入驱动 - 接收外部HTTP推送数据"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class HttpWebhookDriver(DriverPlugin):
    """HTTP Webhook驱动，设备通过HTTP POST推送数据到EdgeLite"""

    plugin_name = "http_webhook"
    plugin_version = "0.1.0"
    supported_protocols = ["http", "webhook"]

    def __init__(self):
        self._running = False
        # device_id -> config
        self._device_configs: dict[str, dict] = {}
        # device_id -> points定义
        self._device_points: dict[str, list[dict]] = {}
        # device_id -> latest_values
        self._latest_values: dict[str, dict[str, Any]] = {}
        # device_id -> last_receive_time
        self._last_receive: dict[str, float] = {}
        # 数据回调
        self._data_callback: Callable | None = None

    async def start(self, config: dict) -> None:
        """启动驱动（HTTP Webhook不需要主动连接）"""
        self._running = True
        logger.info("HTTP Webhook驱动启动")

    async def stop(self) -> None:
        """停止驱动"""
        self._running = False
        logger.info("HTTP Webhook驱动停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加HTTP设备"""
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}
        self._last_receive[device_id] = time.time()
        logger.info("HTTP Webhook设备注册: %s", device_id)

    async def remove_device(self, device_id: str) -> None:
        """移除设备"""
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)
        self._last_receive.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值（返回最新缓存值）"""
        values = self._latest_values.get(device_id, {})
        return {p: values.get(p) for p in points if p in values}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值（通过HTTP POST推送到设备）"""
        config = self._device_configs.get(device_id, {})
        push_url = config.get("push_url")

        if not push_url:
            logger.warning("HTTP设备 %s 未配置push_url", device_id)
            return False

        try:
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    push_url,
                    json={"point": point, "value": value},
                    timeout=10.0,
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error("HTTP推送失败: %s - %s", device_id, e)
            return False

    def on_data(self, callback: Callable) -> None:
        """注册数据回调"""
        self._data_callback = callback

    async def receive_data(self, device_id: str, data: dict[str, Any]) -> None:
        """接收外部HTTP推送的数据（由API层调用）"""
        if device_id not in self._device_configs:
            logger.warning("未注册的HTTP设备: %s", device_id)
            return

        # 数据转换：支持多种格式
        processed = self._transform_data(device_id, data)
        self._latest_values[device_id].update(processed)
        self._last_receive[device_id] = time.time()

        # 触发数据回调
        if self._data_callback:
            await self._data_callback(device_id, processed)

    def _transform_data(self, device_id: str, data: dict) -> dict[str, Any]:
        """数据格式转换"""
        points = self._device_points.get(device_id, [])
        point_names = {p["name"] for p in points}

        # 如果数据是扁平的键值对，直接使用
        result = {}
        for key, value in data.items():
            if key in point_names or not point_names:
                # 类型转换
                point_def = next((p for p in points if p["name"] == key), None)
                if point_def:
                    value = self._cast_value(value, point_def.get("data_type", "float32"))
                result[key] = value

        return result

    @staticmethod
    def _cast_value(value: Any, data_type: str) -> Any:
        """值类型转换"""
        try:
            if data_type in ("int16", "int32", "uint16", "uint32", "int", "integer"):
                return int(float(value))
            elif data_type in ("float32", "float64", "float", "double"):
                return float(value)
            elif data_type in ("bool", "boolean"):
                if isinstance(value, str):
                    return value.lower() in ("true", "1", "yes")
                return bool(value)
            return value
        except (ValueError, TypeError):
            return value

    def get_last_receive_time(self, device_id: str) -> float:
        """获取设备最后数据接收时间"""
        return self._last_receive.get(device_id, 0)
