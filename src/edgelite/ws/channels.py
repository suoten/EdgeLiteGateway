"""WebSocket频道定义与事件处理"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from edgelite.engine.event_bus import AlarmEvent, DeviceStatusEvent, EventBus, PointUpdateEvent
from edgelite.ws.manager import ConnectionManager

logger = logging.getLogger(__name__)


class WebSocketChannels:
    """WebSocket频道管理，订阅EventBus并广播到前端"""

    def __init__(self, event_bus: EventBus, manager: ConnectionManager):
        self._event_bus = event_bus
        self._manager = manager
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        """启动所有频道的EventBus订阅"""
        # realtime频道：测点值更新
        realtime_queue = self._event_bus.subscribe("ws_realtime")
        self._tasks.append(
            asyncio.create_task(
                self._channel_loop("realtime", realtime_queue, self._format_point_update),
                name="ws-realtime",
            )
        )

        # alarm频道：告警事件
        alarm_queue = self._event_bus.subscribe("ws_alarm")
        self._tasks.append(
            asyncio.create_task(
                self._channel_loop("alarm", alarm_queue, self._format_alarm),
                name="ws-alarm",
            )
        )

        # device频道：设备状态变更
        device_queue = self._event_bus.subscribe("ws_device")
        self._tasks.append(
            asyncio.create_task(
                self._channel_loop("device", device_queue, self._format_device_status),
                name="ws-device",
            )
        )

        # integration频道：北向集成事件
        integration_queue = self._event_bus.subscribe("ws_integration")
        self._tasks.append(
            asyncio.create_task(
                self._channel_loop("integration", integration_queue, self._format_integration),
                name="ws-integration",
            )
        )

        logger.info("WebSocket频道启动完成")

    async def stop(self) -> None:
        """停止所有频道"""
        for task in self._tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._tasks.clear()
        logger.info("WebSocket频道停止")

    async def _channel_loop(self, channel: str, queue: asyncio.Queue, formatter) -> None:
        """频道事件循环"""
        while True:
            try:
                event = await queue.get()
                data = formatter(event)
                if data:
                    await self._manager.broadcast(channel, data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("WebSocket频道异常: %s - %s", channel, e)

    @staticmethod
    def _format_point_update(event) -> dict | None:
        if not isinstance(event, PointUpdateEvent):
            return None
        return {
            "type": "point_update",
            "device_id": event.device_id,
            "point_name": event.point_name,
            "value": event.value,
            "quality": event.quality,
            "timestamp": event.timestamp,
        }

    @staticmethod
    def _format_alarm(event) -> dict | None:
        if not isinstance(event, AlarmEvent):
            return None
        return {
            "type": "alarm",
            "alarm_id": event.alarm_id,
            "rule_id": event.rule_id,
            "device_id": event.device_id,
            "severity": event.severity,
            "action": event.action,
            "timestamp": event.timestamp,
        }

    @staticmethod
    def _format_device_status(event) -> dict | None:
        if not isinstance(event, DeviceStatusEvent):
            return None
        return {
            "type": "device_status",
            "device_id": event.device_id,
            "old_status": event.old_status,
            "new_status": event.new_status,
            "timestamp": event.timestamp,
        }

    @staticmethod
    def _format_integration(event) -> dict | None:
        if isinstance(event, dict):
            return event
        if hasattr(event, "model_dump"):
            return event.model_dump()
        return None
