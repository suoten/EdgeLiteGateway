"""HTTP/Webhook接入驱动 - 接收外部HTTP推送数据"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from edgelite.constants import _HTTP_TIMEOUT
from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class HttpWebhookDriver(DriverPlugin):
    """HTTP Webhook驱动，设备通过HTTP POST推送数据到EdgeLite"""

    plugin_name = "http_webhook"
    plugin_version = "0.1.0"
    supported_protocols = ["http", "webhook"]

    def __init__(self):
        self._running = False
        self._device_configs: dict[str, dict] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._latest_values: dict[str, dict[str, Any]] = {}
        self._last_receive: dict[str, float] = {}
        self._data_callback: Callable | None = None
        self._http_client: Any = None  # FIXED-P2: 复用httpx客户端而非每次write_point创建新实例

    async def start(self, config: dict) -> None:
        """启动驱动（HTTP Webhook不需要主动连接）"""
        try:
            import httpx
            self._http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        except ImportError:
            self._http_client = None
        self._running = True
        logger.info("HTTP Webhook驱动启动")

    async def stop(self) -> None:
        """停止驱动"""
        self._running = False
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        logger.info("HTTP Webhook驱动停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加HTTP设备"""
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}
        self._last_receive[device_id] = 0  # 尚未收到数据，初始为0
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
            if not self._http_client:
                import httpx
                self._http_client = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
            resp = await self._http_client.post(
                push_url,
                json={"point": point, "value": value},
                timeout=_HTTP_TIMEOUT,
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
        try:  # FIXED: 原问题-update和回调无try-catch保护
            self._latest_values.setdefault(device_id, {}).update(processed)  # FIXED: 原问题-硬访问device_id键可能不存在
            self._last_receive[device_id] = time.time()

            # 触发数据回调
            if self._data_callback:
                await self._data_callback(device_id, processed)
        except Exception as e:
            logger.error("HTTP接收数据处理失败: %s - %s", device_id, e)

    def _transform_data(self, device_id: str, data: dict) -> dict[str, Any]:
        """数据格式转换"""
        points = self._device_points.get(device_id, [])
        point_names = {p.get("name") for p in points if p.get("name") is not None}  # FIXED: 原问题-p["name"]硬访问

        # 如果数据是扁平的键值对，直接使用
        result = {}
        for key, value in data.items():
            if key in point_names or not point_names:
                # 类型转换
                point_def = next((p for p in points if p.get("name") == key), None)  # FIXED: 原问题-p["name"]硬访问
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

    # ─── 连通性判定 ─────────────────────────────────────────

    _OFFLINE_TIMEOUT = 60  # 超过60秒未收到数据视为离线

    def is_device_connected(self, device_id: str) -> bool:
        """HTTP Webhook 设备连通性判定：设备已注册即视为在线（被动协议，等待外部推送）；
        超过 OFFLINE_TIMEOUT 未收到数据则视为离线。
        """
        if device_id not in self._device_configs:
            return False
        last = self._last_receive.get(device_id, 0)
        # 设备已注册但尚未收到数据 → 视为在线（等待推送中）
        if last == 0:
            return True
        # 收到过数据，检查是否超时
        return (time.time() - last) < self._OFFLINE_TIMEOUT
