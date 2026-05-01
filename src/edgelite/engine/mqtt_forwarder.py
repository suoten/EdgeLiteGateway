"""北向MQTT数据转发 - 将采集数据/告警事件转发到MQTT Broker"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from edgelite.config import get_config

logger = logging.getLogger(__name__)


class MqttForwarder:
    """北向MQTT数据转发器，订阅EventBus事件并转发到MQTT"""

    def __init__(self):
        self._running = False
        self._connected = False
        self._connect_task: asyncio.Task | None = None
        self._pub_queue: asyncio.Queue | None = None

    async def start(self, event_bus: Any = None) -> None:
        config = get_config()
        if not config.mqtt.broker:
            logger.info("MQTT Broker未配置，北向转发不启动")
            return

        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=10000)

        if event_bus:
            event_bus.register_handler("PointUpdateEvent", self._on_point_update)
            event_bus.register_handler("AlarmEvent", self._on_alarm_event)
            event_bus.register_handler("DeviceStatusEvent", self._on_device_status)
            logger.info("MQTT转发器已订阅EventBus事件")

        self._connect_task = asyncio.create_task(self._connect_loop(), name="mqtt-forward-connect")
        logger.info("MQTT北向转发器启动")

    async def stop(self) -> None:
        self._running = False

        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass

        self._connected = False
        logger.info("MQTT北向转发器停止")

    async def _on_point_update(self, event: Any) -> None:
        if not self._pub_queue:
            return
        try:
            data = {
                "type": "point_update",
                "device_id": getattr(event, "device_id", ""),
                "point_name": getattr(event, "point_name", ""),
                "value": getattr(event, "value", 0),
                "quality": getattr(event, "quality", "good"),
                "timestamp": time.time(),
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("MQTT转发队列已满，丢弃消息")

    async def _on_alarm_event(self, event: Any) -> None:
        if not self._pub_queue:
            return
        try:
            data = {
                "type": "alarm",
                "alarm_id": getattr(event, "alarm_id", ""),
                "device_id": getattr(event, "device_id", ""),
                "severity": getattr(event, "severity", ""),
                "action": getattr(event, "action", "firing"),
                "timestamp": time.time(),
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("MQTT转发队列已满，丢弃消息")

    async def _on_device_status(self, event: Any) -> None:
        if not self._pub_queue:
            return
        try:
            data = {
                "type": "device_status",
                "device_id": getattr(event, "device_id", ""),
                "old_status": getattr(event, "old_status", ""),
                "new_status": getattr(event, "new_status", ""),
                "timestamp": time.time(),
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("MQTT转发队列已满，丢弃消息")

    async def _connect_loop(self) -> None:
        config = get_config()

        while self._running:
            try:
                import aiomqtt

                async with aiomqtt.Client(
                    hostname=config.mqtt.broker,
                    port=config.mqtt.port,
                    username=config.mqtt.username or None,
                    password=config.mqtt.password or None,
                    keepalive=60,
                ) as client:
                    self._connected = True
                    logger.info("MQTT转发器连接成功: %s:%d", config.mqtt.broker, config.mqtt.port)

                    pub_task = asyncio.create_task(
                        self._publish_loop(client), name="mqtt-forward-publish"
                    )

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        if not pub_task.done():
                            pub_task.cancel()
                        try:
                            await pub_task
                        except asyncio.CancelledError:
                            pass
                        self._connected = False

            except asyncio.CancelledError:
                raise
            except ImportError:
                logger.error("aiomqtt未安装，MQTT转发不可用")
                await asyncio.sleep(30)
            except Exception as e:
                logger.error("MQTT转发器连接异常: %s，5秒后重试", e)
                self._connected = False
                await asyncio.sleep(5)

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                try:
                    data = await asyncio.wait_for(self._pub_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                try:
                    if not self._connected:
                        await asyncio.sleep(0.5)
                        continue

                    config = get_config()
                    topic_prefix = config.mqtt.topic_prefix

                    msg_type = data.get("type", "unknown")
                    device_id = data.get("device_id", "")

                    if msg_type == "point_update":
                        topic = f"{topic_prefix}/data/{device_id}"
                    elif msg_type == "alarm":
                        topic = f"{topic_prefix}/alarm/{device_id}"
                    elif msg_type == "device_status":
                        topic = f"{topic_prefix}/status/{device_id}"
                    else:
                        topic = f"{topic_prefix}/misc"

                    payload = json.dumps(data, ensure_ascii=False, default=str)
                    await client.publish(topic, payload.encode("utf-8"), qos=1)

                except Exception as e:
                    err_str = str(e)
                    if "not currently connected" in err_str:
                        self._connected = False
                        logger.warning("MQTT连接已断开，等待重连...")
                    else:
                        logger.error("MQTT发布失败: %s", e)
        except asyncio.CancelledError:
            pass

    @property
    def is_connected(self) -> bool:
        return self._connected
