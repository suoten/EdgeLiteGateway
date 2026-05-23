"""驱动插件抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any


class DriverPlugin(ABC):
    """协议驱动插件基类"""

    plugin_name: str = ""
    plugin_version: str = "0.1.0"
    supported_protocols: list[str] = []
    config_schema: dict = {}

    def __init__(self) -> None:
        self._running: bool = False  # FIXED-P2: 基类初始化_running和_data_callback，避免子类访问未定义属性抛AttributeError
        self._data_callback: Callable | None = None

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

    # FIXED: 原问题-add_device使用NotImplementedError而非@abstractmethod，子类未实现时不在实例化阶段报错
    # add_device 保持为可选方法（非 abstractmethod），但改用更明确的文档说明
    async def add_device(
        self, device_id: str, config: dict, points: list[dict] | None = None
    ) -> None:
        """添加设备到驱动实例（可选实现）。未实现时抛出 NotImplementedError。"""
        raise NotImplementedError(f"{self.__class__.__name__} does not implement add_device")

    def is_device_connected(self, device_id: str) -> bool:
        """检查设备是否已连接（可选实现）"""
        return False

    def on_data(self, callback: Callable) -> None:
        """注册数据回调（可选，用于推送型协议如MQTT）。子类如需支持推送，应覆盖此方法保存callback。"""
        self._data_callback = callback

    @property
    def is_running(self) -> bool:
        """驱动是否运行中"""
        return getattr(self, "_running", False)
