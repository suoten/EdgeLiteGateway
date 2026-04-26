"""EdgeLite v1.0 数据回传管理器 - 基于v1.0的EventBus(register_handler)接口"""

import asyncio
import logging
import time
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)


class BackhaulManager:
    def __init__(
        self,
        event_bus: Any,
        endpoint: Any,
        device_filter: list[str] | None = None,
        point_filter: list[str] | None = None,
        change_threshold: float = 0.0,
        rate_limit: float = 10.0,
        buffer_size: int = 1000,
    ):
        self._event_bus = event_bus
        self._endpoint = endpoint
        self._device_filter = set(device_filter or [])
        self._point_filter = set(point_filter or [])
        self._change_threshold = change_threshold
        self._rate_limit = rate_limit
        self._buffer_size = buffer_size
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._last_values: dict[str, float] = {}
        self._last_send_time: dict[str, float] = {}
        self._running = False

    async def start(self) -> None:
        self._running = True
        if self._event_bus:
            self._event_bus.register_handler("PointUpdateEvent", self._on_point_update)
            self._event_bus.register_handler("DeviceStatusEvent", self._on_device_status)
            self._event_bus.register_handler("AlarmEvent", self._on_alarm)
        logger.info("BackhaulManager started")

    async def stop(self) -> None:
        self._running = False
        if self._event_bus:
            self._event_bus.unregister_handler("PointUpdateEvent", self._on_point_update)
            self._event_bus.unregister_handler("DeviceStatusEvent", self._on_device_status)
            self._event_bus.unregister_handler("AlarmEvent", self._on_alarm)
        logger.info("BackhaulManager stopped")

    async def _on_point_update(self, event: Any) -> None:
        device_id = getattr(event, "device_id", "")
        point_name = getattr(event, "point_name", "")
        value = getattr(event, "value", 0.0)

        if self._device_filter and device_id not in self._device_filter:
            return
        if self._point_filter and point_name not in self._point_filter:
            return

        key = f"{device_id}.{point_name}"
        if self._change_threshold > 0:
            last = self._last_values.get(key)
            if last is not None and abs(value - last) < self._change_threshold:
                return
            self._last_values[key] = value

        now = time.time()
        min_interval = 1.0 / self._rate_limit if self._rate_limit > 0 else 0
        last_send = self._last_send_time.get(device_id, 0)
        if now - last_send < min_interval:
            return
        self._last_send_time[device_id] = now

        await self._send_or_buffer({
            "type": "point_data",
            "timestamp": now,
            "payload": {"device_id": device_id, "point_name": point_name, "value": value,
                        "quality": getattr(event, "quality", "good")},
        })

    async def _on_device_status(self, event: Any) -> None:
        await self._send_or_buffer({
            "type": "device_status_changed",
            "timestamp": time.time(),
            "payload": {"device_id": getattr(event, "device_id", ""),
                        "new_status": getattr(event, "new_status", ""),
                        "old_status": getattr(event, "old_status", "")},
        })

    async def _on_alarm(self, event: Any) -> None:
        action = getattr(event, "action", "firing")
        await self._send_or_buffer({
            "type": "alarm_fired" if action == "firing" else "alarm_recovered",
            "timestamp": time.time(),
            "payload": {"alarm_id": getattr(event, "alarm_id", ""),
                        "rule_id": getattr(event, "rule_id", ""),
                        "device_id": getattr(event, "device_id", ""),
                        "severity": getattr(event, "severity", ""),
                        "action": action},
        })

    async def _send_or_buffer(self, message: dict[str, Any]) -> None:
        if self._endpoint and self._endpoint._connections:
            sent = await self._endpoint.broadcast(message)
            if sent > 0:
                return
        self._buffer.append(message)

    async def flush_buffer(self) -> int:
        if not self._buffer or not self._endpoint or not self._endpoint._connections:
            return 0
        count = 0
        while self._buffer:
            msg = self._buffer.popleft()
            sent = await self._endpoint.broadcast(msg)
            if sent > 0:
                count += 1
            else:
                self._buffer.appendleft(msg)
                break
        return count
