"""VideoProvider抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum


class DeviceStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class VideoProvider(ABC):
    """视频接入抽象接口，解耦具体视频平台实现"""

    @abstractmethod
    async def register_device(self, device_id: str, config: dict) -> bool:
        """注册视频设备"""

    @abstractmethod
    async def get_stream_url(self, device_id: str, channel_id: str) -> str:
        """获取视频流地址"""

    @abstractmethod
    async def ptz_control(self, device_id: str, channel_id: str, action: str, **kwargs) -> bool:
        """云台控制"""

    @abstractmethod
    async def get_device_status(self, device_id: str) -> DeviceStatus:
        """获取视频设备状态"""

    @abstractmethod
    async def on_alarm(self, callback: Callable) -> None:
        """注册告警回调"""
