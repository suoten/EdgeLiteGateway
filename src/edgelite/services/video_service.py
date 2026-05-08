"""视频接入业务逻辑"""

from __future__ import annotations

import logging

from edgelite.drivers.video.provider import DeviceStatus
from edgelite.drivers.video.pygbsentry import PyGBSentryProvider
from edgelite.engine.event_bus import AlarmEvent, EventBus

logger = logging.getLogger(__name__)


class VideoService:
    """视频接入业务逻辑"""

    def __init__(self, event_bus: EventBus):
        self._event_bus = event_bus
        self._provider: PyGBSentryProvider | None = None

    async def init_provider(self) -> None:
        """初始化视频提供者"""
        try:
            self._provider = PyGBSentryProvider()
            await self._provider.connect()
        except Exception as e:
            logger.error("视频提供者初始化失败: %s", e)
            self._provider = None

    async def close(self) -> None:
        if self._provider:
            await self._provider.close()

    async def register_video_device(self, device_id: str, config: dict) -> bool:
        """注册视频设备"""
        if not self._provider:
            return False
        return await self._provider.register_device(device_id, config)

    async def get_stream_url(self, device_id: str, channel_id: str = "1") -> str:
        """获取视频流地址"""
        if not self._provider:
            return ""
        return await self._provider.get_stream_url(device_id, channel_id)

    async def ptz_control(self, device_id: str, channel_id: str, action: str, **kwargs) -> bool:
        """云台控制"""
        if not self._provider:
            return False
        return await self._provider.ptz_control(device_id, channel_id, action, **kwargs)

    async def get_device_status(self, device_id: str) -> DeviceStatus:
        """获取视频设备状态"""
        if not self._provider:
            return DeviceStatus.UNKNOWN
        return await self._provider.get_device_status(device_id)

    async def handle_webhook(self, event_data: dict) -> None:
        """处理PyGBSentry Webhook回调"""
        if not self._provider:
            return
        await self._provider.handle_webhook(event_data)

        # 如果是告警事件，发布到EventBus
        event_type = event_data.get("type", "")
        if "alarm" in event_type.lower():
            alarm_event = AlarmEvent(
                device_id=event_data.get("device_id", ""),
                severity=event_data.get("severity", "warning"),
                action="firing",
                trigger_value=event_data,
            )
            await self._event_bus.publish(alarm_event)
