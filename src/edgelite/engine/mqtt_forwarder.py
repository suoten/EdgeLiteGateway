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
        self._client = None
        self._connected = False
        self._connect_task: asyncio.Task | None = None
        self._message_queue: asyncio.Queue | None = None
        self._publish_task: asyncio.Task | None = None

    async def start(self, event_bus: Any = None) -> None:
        """启动MQTT转发器"""
        config = get_config()
        if not config.mqtt.broker:
            logger.info("MQTT Broker未配置，北向转发不启动")
            return

        self._running = True
        self._message_queue = asyncio.Queue(maxsize=10000)

        # 订阅EventBus事件
        if event_bus:
            event_bus.register_handler("point_update", self._on_point_update)
            event_bus.register_handler("alarm", self._on_alarm_event)
            event_bus.register_handler("device_status", self._on_device_status)
            logger.info("MQTT转发器已订阅EventBus事件")

        # 启动连接和发布任务
        self._connect_task = asyncio.create_task(self._connect_loop(), name="mqtt-forward-connect")
        self._publish_task = asyncio.create_task(self._publish_loop(), name="mqtt-forward-publish")
        logger.info("MQTT北向转发器启动")

    async def stop(self) -> None:
        """停止转发器"""
        self._running = False

        for task in [self._connect_task, self._publish_task]:
            if task and not task.done():
                task.cancel()
        for task in [self._connect_task, self._publish_task]:
            if task:
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

        logger.info("MQTT北向转发器停止")

    async def _on_point_update(self, event: Any) -> None:
        """处理测点更新事件"""
        if not self._message_queue:
            return
        try:
            data = {
                "type": "point_update",
                "device_id": getattr(event, "device_id", ""),
                "points": getattr(event, "points", {}),
                "timestamp": time.time(),
            }
            self._message_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("MQTT转发队列已满，丢弃消息")

    async def _on_alarm_event(self, event: Any) -> None:
        """处理告警事件"""
        if not self._message_queue:
            return
        try:
            data = {
                "type": "alarm",
                "alarm_id": getattr(event, "alarm_id", ""),
                "device_id": getattr(event, "device_id", ""),
                "severity": getattr(event, "severity", ""),
                "status": getattr(event, "status", ""),
                "timestamp": time.time(),
            }
            self._message_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("MQTT转发队列已满，丢弃消息")

    async def _on_device_status(self, event: Any) -> None:
        """处理设备状态变更事件"""
        if not self._message_queue:
            return
        try:
            data = {
                "type": "device_status",
                "device_id": getattr(event, "device_id", ""),
                "status": getattr(event, "status", ""),
                "timestamp": time.time(),
            }
            self._message_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("MQTT转发队列已满，丢弃消息")

    async def _connect_loop(self) -> None:
        """MQTT连接循环"""
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
                    self._client = client
                    self._connected = True
                    logger.info("MQTT转发器连接成功: %s:%d", config.mqtt.broker, config.mqtt.port)

                    # 保持连接
                    while self._running:
                        await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except ImportError:
                logger.error("aiomqtt未安装，MQTT转发不可用")
                await asyncio.sleep(30)
            except Exception as e:
                logger.error("MQTT转发器连接异常: %s，5秒后重试", e)
                self._client = None
                self._connected = False
                await asyncio.sleep(5)

    async def _publish_loop(self) -> None:
        """消息发布循环"""
        while self._running:
            if not self._message_queue:
                await asyncio.sleep(1)
                continue

            try:
                data = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

            if not self._connected or not self._client:
                # 未连接时丢弃消息（可改为缓存）
                continue

            try:
                config = get_config()
                topic_prefix = config.mqtt.topic_prefix

                # 根据消息类型确定主题
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
                await self._client.publish(topic, payload.encode("utf-8"), qos=1)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("MQTT发布失败: %s", e)

    @property
    def is_connected(self) -> bool:
        return self._connected
