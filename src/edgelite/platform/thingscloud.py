"""ThingsCloud平台对接 - 通过MQTT协议对接ThingsCloud物联网平台

ThingsCloud MQTT Topic体系:
- things/{device_id}/properties/report  - 属性上报
- things/{device_id}/command/receive    - 命令下发
- things/{device_id}/event/report       - 事件上报
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class ThingsCloudHandler(PlatformHandler):
    """ThingsCloud平台对接实现"""

    platform_name = "thingscloud"
    platform_version = "1.0.0"

    def __init__(self):
        self._connected = False
        self._client = None
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._subscribe_task: asyncio.Task | None = None
        self._running = False

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt未安装，请执行: pip install aiomqtt")

        self._config = config
        self._running = True

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
        for task in [self._connect_task, self._subscribe_task]:
            if task and not task.done():
                task.cancel()
        for task in [self._connect_task, self._subscribe_task]:
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
        self._connected = False
        logger.info("ThingsCloud平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._connected or not self._client:
            return
        topic = f"things/{device_id}/properties/report"
        payload = json.dumps({
            "properties": data,
            "timestamp": int(time.time() * 1000),
        }, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("ThingsCloud遥测上报失败: %s", e)

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._connected or not self._client:
            return
        topic = f"things/{device_id}/properties/report"
        payload = json.dumps({
            "properties": attrs,
            "timestamp": int(time.time() * 1000),
        }, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("ThingsCloud属性上传失败: %s", e)

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._connected or not self._client:
            return
        topic = f"things/{device_id}/event/report"
        payload = json.dumps({
            "event": "device_status",
            "data": {"online": online},
            "timestamp": int(time.time() * 1000),
        }, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("ThingsCloud设备状态上报失败: %s", e)

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
                    self._client = client
                    self._connected = True
                    logger.info("ThingsCloud MQTT连接成功: %s:%d", broker, port)

                    await client.subscribe("things/+/command/receive", qos=1)
                    self._subscribe_task = asyncio.create_task(
                        self._message_listen_loop(client),
                        name="thingscloud-msg-listen",
                    )

                    while self._running:
                        await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("ThingsCloud MQTT连接异常: %s，5秒后重试", e)
                self._client = None
                self._connected = False
                await asyncio.sleep(5)

    async def _message_listen_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    payload = json.loads(message.payload.decode("utf-8"))

                    if "/command/receive" in topic:
                        parts = topic.split("/")
                        device_id = parts[1] if len(parts) >= 2 else ""
                        method = payload.get("command", payload.get("method", ""))
                        params = payload.get("params", payload.get("data", {}))

                        if self._rpc_callback:
                            result = await self._rpc_callback(device_id, method, params)

                except Exception as e:
                    logger.error("ThingsCloud消息处理异常: %s", e)
        except asyncio.CancelledError:
            pass
