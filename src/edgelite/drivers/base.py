"""驱动插件抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable


class DriverPlugin(ABC):
    """协议驱动插件基类"""

    plugin_name: str = ""
    plugin_version: str = "0.1.0"
    supported_protocols: list[str] = []

    @abstractmethod
    async def start(self, config: dict) -> None:
        """启动驱动"""

    @abstractmethod
    async def stop(self) -> None:
        """停止驱动"""

    @abstractmethod
    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值，返回 {point_name: value}"""

    @abstractmethod
    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""

    async def discover_devices(self, config: dict) -> list[dict]:
        """发现设备（可选实现）"""
        return []

    def on_data(self, callback: Callable) -> None:
        """注册数据回调（可选，用于推送型协议如MQTT）。子类如需支持推送，应覆盖此方法保存callback。"""
        self._data_callback = callback

    @property
    def is_running(self) -> bool:
        """驱动是否运行中"""
        return getattr(self, "_running", False)
