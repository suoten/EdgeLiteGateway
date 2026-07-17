"""视频接入业务逻辑

视频驱动 (edgelite.drivers.video) 已在社区版中移除。
本模块保留为接口存根，保持 bootstrap 和 API 层兼容性。
所有方法返回安全默认值并记录 warning 日志。
"""

from __future__ import annotations

import logging
from enum import Enum

from edgelite.engine.event_bus import AlarmEvent, EventBus

logger = logging.getLogger(__name__)


class DeviceStatus(str, Enum):
    """视频设备状态枚举（存根实现，替代已删除的 edgelite.drivers.video.provider.DeviceStatus）"""

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class VideoService:
    """视频接入业务逻辑（存根实现）

    视频驱动已在社区版中移除，所有操作返回安全默认值。
    保留 EventBus 依赖以维持接口兼容性，handle_webhook 仍可发布告警事件。
    """

    def __init__(self, event_bus: EventBus):
        self._event_bus = event_bus

    async def init_provider(self) -> None:
        """初始化视频提供者（存根：视频驱动已移除，记录日志即可）"""
        logger.info("Video driver has been removed in community edition, VideoService running in stub mode")

    async def close(self) -> None:
        """关闭视频提供者（存根：无资源需清理）"""
        pass

    async def register_video_device(self, device_id: str, config: dict) -> bool:
        """注册视频设备（存根：返回 False 表示功能不可用）"""
        logger.warning("Video driver removed: register_video_device is not available (device=%s)", device_id)
        return False

    async def get_stream_url(self, device_id: str, channel_id: str = "1") -> str:
        """获取视频流地址（存根：返回空字符串）"""
        logger.warning("Video driver removed: get_stream_url is not available (device=%s)", device_id)
        return ""

    async def ptz_control(self, device_id: str, channel_id: str, action: str, **kwargs) -> bool:
        """云台控制（存根：返回 False 表示功能不可用）"""
        logger.warning("Video driver removed: ptz_control is not available (device=%s)", device_id)
        return False

    async def get_device_status(self, device_id: str) -> DeviceStatus:
        """获取视频设备状态（存根：返回 UNKNOWN）"""
        return DeviceStatus.UNKNOWN

    async def handle_webhook(self, event_data: dict) -> None:
        """处理视频 Webhook 回调

        视频驱动已移除，无法调用 provider 处理 webhook。
        但仍支持将告警事件发布到 EventBus，保持告警链路兼容。
        """
        event_type = event_data.get("type", "")
        if "alarm" in event_type.lower():
            alarm_event = AlarmEvent(
                alarm_id=event_data.get(
                    "alarm_id", f"video_{event_data.get('device_id', '')}_{event_type}"
                ),
                rule_id=event_data.get("rule_id", "video_alarm"),
                device_id=event_data.get("device_id", ""),
                severity=event_data.get("severity", "warning"),
                action="firing",
                trigger_value=event_data,
                rule_type="video",
            )
            await self._event_bus.publish(alarm_event)
        else:
            logger.debug("Video webhook received but video driver is removed (type=%s)", event_type)
