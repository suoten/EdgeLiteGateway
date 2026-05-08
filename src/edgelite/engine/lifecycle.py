"""设备生命周期管理"""

from __future__ import annotations

import logging

from edgelite.engine.event_bus import DeviceStatusEvent, EventBus

logger = logging.getLogger(__name__)


class DeviceLifecycleManager:
    """设备生命周期管理，处理设备上下线"""

    def __init__(self, event_bus: EventBus):
        self._event_bus = event_bus
        # device_id -> current_status
        self._status_map: dict[str, str] = {}

    async def on_device_online(self, device_id: str) -> None:
        """设备上线处理"""
        old_status = self._status_map.get(device_id, "offline")
        if old_status == "online":
            return

        self._status_map[device_id] = "online"
        event = DeviceStatusEvent(
            device_id=device_id,
            old_status=old_status,
            new_status="online",
        )
        await self._event_bus.publish(event)
        logger.info("设备上线: %s (%s -> online)", device_id, old_status)

    async def on_device_offline(self, device_id: str) -> None:
        """设备下线处理"""
        old_status = self._status_map.get(device_id, "online")
        if old_status == "offline":
            return

        self._status_map[device_id] = "offline"
        event = DeviceStatusEvent(
            device_id=device_id,
            old_status=old_status,
            new_status="offline",
        )
        await self._event_bus.publish(event)
        logger.info("设备下线: %s (%s -> offline)", device_id, old_status)

    async def on_device_unknown(self, device_id: str) -> None:
        """设备状态未知"""
        old_status = self._status_map.get(device_id, "offline")
        self._status_map[device_id] = "unknown"
        event = DeviceStatusEvent(
            device_id=device_id,
            old_status=old_status,
            new_status="unknown",
        )
        await self._event_bus.publish(event)
        logger.info("设备状态未知: %s (%s -> unknown)", device_id, old_status)

    def get_status(self, device_id: str) -> str:
        """获取设备当前状态"""
        return self._status_map.get(device_id, "offline")

    def remove_device(self, device_id: str) -> None:
        """移除设备状态记录"""
        self._status_map.pop(device_id, None)
