"""自定义MQTT平台对接 - 通用MQTT数据转发

支持自定义Topic前缀，适用于任何MQTT兼容平台
Topic体系:
- {topic_prefix}/{device_id}/telemetry   - 遥测数据上报
- {topic_prefix}/{device_id}/attributes  - 属性上传
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class CustomMqttHandler(PlatformHandler):
    """自定义MQTT平台对接实现"""

    platform_name = "custom"
    platform_version = "1.0.0"

    def __init__(self):
        self._connected = False
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._running = False
        self._pub_queue: asyncio.Queue | None = None
        self._topic_prefix = "edgelite"

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt未安装，请执行: pip install aiomqtt")

        self._config = config
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=1000)
        self._topic_prefix = config.get("topic_prefix", "edgelite")

        broker = config.get("broker", "localhost")
        port = int(config.get("port", 1883))
        username = config.get("username", "")
        password = config.get("password", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password),
            name="custom-mqtt-connect",
        )
        logger.info("自定义MQTT平台对接启动: %s:%d, 前缀: %s", broker, port, self._topic_prefix)

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        self._connected = False
        logger.info("自定义MQTT平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"{self._topic_prefix}/{device_id}/telemetry"
        payload = json.dumps(data, ensure_ascii=False, default=str)
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("自定义MQTT发布队列已满，丢弃消息")

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"{self._topic_prefix}/{device_id}/attributes"
        payload = json.dumps(attrs, ensure_ascii=False, default=str)
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("自定义MQTT发布队列已满，丢弃消息")

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"{self._topic_prefix}/{device_id}/status"
        payload = json.dumps({"online": online})
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("自定义MQTT发布队列已满，丢弃消息")

    async def _connect_loop(self, broker: str, port: int, username: str, password: str) -> None:
        import aiomqtt

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username or None,
                    password=password or None,
                    keepalive=60,
                ) as client:
                    self._connected = True
                    logger.info("自定义MQTT连接成功: %s:%d", broker, port)

                    await client.subscribe(f"{self._topic_prefix}/+/rpc/request", qos=1)

                    rpc_task = asyncio.create_task(
                        self._rpc_listen_loop(client),
                        name="custom-mqtt-rpc-listen",
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client),
                        name="custom-mqtt-publish",
                    )

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        for t in [rpc_task, pub_task]:
                            if not t.done():
                                t.cancel()
                        for t in [rpc_task, pub_task]:
                            try:
                                await t
                            except asyncio.CancelledError:
                                pass
                        self._connected = False

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("自定义MQTT连接异常: %s，5秒后重试", e)
                self._connected = False
                await asyncio.sleep(5)

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                try:
                    topic, payload, qos = await asyncio.wait_for(
                        self._pub_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                try:
                    await client.publish(topic, payload, qos=qos)
                except Exception as e:
                    logger.error("自定义MQTT发布失败: %s", e)
        except asyncio.CancelledError:
            pass

    async def _rpc_listen_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    parts = topic.split("/")
                    if len(parts) >= 4 and parts[-1] == "request":
                        device_id = parts[-3] if len(parts) >= 4 else "unknown"
                        payload = json.loads(message.payload.decode("utf-8"))
                        method = payload.get("method", "")
                        params = payload.get("params", {})

                        if self._rpc_callback:
                            result = await self._rpc_callback(device_id, method, params)
                            response_topic = f"{self._topic_prefix}/{device_id}/rpc/response"
                            response_data = json.dumps({"method": method, "result": result})
                            await client.publish(
                                response_topic,
                                response_data.encode("utf-8"),
                                qos=1,
                            )
                except Exception as e:
                    logger.error("自定义MQTT RPC处理异常: %s", e)
        except asyncio.CancelledError:
            pass
