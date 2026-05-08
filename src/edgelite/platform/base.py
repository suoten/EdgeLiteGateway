"""北向平台对接抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any


class PlatformHandler(ABC):
    """北向IoT平台对接抽象基类

    所有平台对接实现必须继承此类并实现所有抽象方法。
    平台对接通过MQTT协议与云端平台通信，支持：
    - 遥测数据上报
    - 属性上传
    - RPC反向控制（从平台下发指令到设备）
    - 设备上下线通知
    """

    platform_name: str = ""
    platform_version: str = "1.0.0"

    @abstractmethod
    async def connect(self, config: dict) -> None:
        """连接到平台MQTT Broker

        Args:
            config: 平台连接配置，包含broker/port/username/password等
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """断开平台连接"""

    @abstractmethod
    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        """上报设备遥测数据

        Args:
            device_id: 设备ID
            data: 遥测数据键值对，如 {"temperature": 25.6, "humidity": 60}
        """

    @abstractmethod
    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        """上传设备属性

        Args:
            device_id: 设备ID
            attrs: 属性键值对，如 {"model": "S7-1200", "location": "workshop-1"}
        """

    @abstractmethod
    async def on_rpc_request(self, callback: Callable) -> None:
        """注册RPC请求回调

        当平台下发RPC请求时，调用callback处理

        Args:
            callback: RPC请求处理函数，
                签名为 async def callback(device_id, method, params) -> result
        """

    @abstractmethod
    async def publish_device_status(self, device_id: str, online: bool) -> None:
        """上报设备上下线状态

        Args:
            device_id: 设备ID
            online: True=上线，False=下线
        """

    @property
    def is_connected(self) -> bool:
        """平台是否已连接"""
        return getattr(self, "_connected", False)
