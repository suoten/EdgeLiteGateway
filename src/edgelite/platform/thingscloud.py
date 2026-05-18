"""ThingsCloud平台对接 - 通过MQTT协议对接ThingsCloud物联网平台

ThingsCloud MQTT Topic体系:
- things/{device_id}/properties/report  - 属性上报
- things/{device_id}/command/receive    - 命令下发
- things/{device_id}/event/report       - 事件上报
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from edgelite.constants import _MQTT_QUEUE_MAXSIZE, _MQTT_KEEPALIVE, _MQTT_RECONNECT_DELAY
from edgelite.utils import timestamp_ms
from collections.abc import Callable
from typing import Any

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class ThingsCloudHandler(PlatformHandler):
    """ThingsCloud平台对接实现"""

    platform_name = "thingscloud"
    platform_version = "1.0.0"

    def __init__(self):
        self._connected = False
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._running = False
        self._pub_queue: asyncio.Queue | None = None

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt未安装，请执行: pip install aiomqtt") from None

        self._config = config
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)  # FIXED: 原问题-maxsize=1000魔法数字

        broker = config.get("broker", config.get("host", "localhost"))
        port = int(config.get("port", 1883))
        username = config.get("username", config.get("access_key", ""))
        password = config.get("password", config.get("access_secret", ""))

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password),
            name="thingscloud-connect",
        )
        logger.info("ThingsCloud平台对接启动: %s:%d", broker, port)

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._connected = False
        logger.info("ThingsCloud平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"things/{device_id}/properties/report"
        payload = json.dumps(
            {
                "properties": data,
                # FIXED: 原问题-int(time.time()*1000)重复模式，改为timestamp_ms()
                "timestamp": timestamp_ms(),
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("ThingsCloud发布队列已满，丢弃消息")

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"things/{device_id}/attributes/report"
        payload = json.dumps(attrs, ensure_ascii=False, default=str)
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("ThingsCloud发布队列已满，丢弃消息")

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"things/{device_id}/event/report"
        payload = json.dumps(
            {
                "event": "device_status",
                "data": {"online": online},
                # FIXED: 原问题-int(time.time()*1000)重复模式，改为timestamp_ms()
                "timestamp": timestamp_ms(),
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("ThingsCloud发布队列已满，丢弃消息")

    async def _connect_loop(self, broker: str, port: int, username: str, password: str) -> None:
        import aiomqtt

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username or None,
                    password=password or None,
                    keepalive=_MQTT_KEEPALIVE,
                ) as client:
                    self._connected = True
                    logger.info("ThingsCloud MQTT连接成功: %s:%d", broker, port)

                    await client.subscribe("things/+/command/receive", qos=1)

                    msg_task = asyncio.create_task(
                        self._message_listen_loop(client),
                        name="thingscloud-msg-listen",
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client),
                        name="thingscloud-publish",
                    )

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        for t in [msg_task, pub_task]:
                            if not t.done():
                                t.cancel()
                        for t in [msg_task, pub_task]:
                            with contextlib.suppress(asyncio.CancelledError):
                                await t
                        self._connected = False

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("ThingsCloud MQTT连接异常: %s，5秒后重试", e)
                self._connected = False
                await asyncio.sleep(5)

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    topic, payload, qos = await asyncio.wait_for(self._pub_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
                try:
                    await client.publish(topic, payload, qos=qos)
                except Exception as e:
                    logger.error("ThingsCloud MQTT发布失败: %s", e)
        except asyncio.CancelledError:
            pass

    async def _message_listen_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    # FIXED: 原问题-json.loads无JSONDecodeError专项捕获，恶意MQTT消息导致处理中断
                    try:
                        payload = json.loads(message.payload.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        logger.warning("ThingsCloud invalid message payload: %s", e)
                        continue

                    if "/command/receive" in topic:
                        parts = topic.split("/")
                        device_id = parts[1] if len(parts) >= 2 else ""
                        method = payload.get("command", payload.get("method", ""))
                        params = payload.get("params", payload.get("data", {}))

                        if self._rpc_callback:
                            await self._rpc_callback(device_id, method, params)

                except Exception as e:
                    logger.error("ThingsCloud消息处理异常: %s", e)
        except asyncio.CancelledError:
            pass
