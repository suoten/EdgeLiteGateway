"""ThingsBoard平台对接 - 通过MQTT网关协议对接ThingsBoard IoT平台

ThingsBoard Gateway MQTT Topic体系:
- v1/gateway/telemetry         - 网关遥测数据上报
- v1/gateway/attributes        - 网关属性上传
- v1/gateway/connect           - 设备连接通知
- v1/gateway/disconnect        - 设备断开通知
- v1/gateway/rpc               - RPC请求（平台→网关）
- v1/gateway/attributes/request - 属性请求（平台→网关）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from edgelite.constants import _MQTT_KEEPALIVE
from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class ThingsBoardHandler(PlatformHandler):
    """ThingsBoard平台对接实现（网关MQTT协议）"""

    platform_name = "thingsboard"
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
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)  # FIXED: 原问题-硬编码队列大小

        broker = config.get("broker", "localhost")
        port = int(config.get("port", 1883))
        token = config.get("token", config.get("username", ""))
        password = config.get("password", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, token, password),
            name="thingsboard-connect",
        )
        logger.info("ThingsBoard平台对接启动: %s:%d", broker, port)

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._connected = False
        logger.info("ThingsBoard平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = "v1/gateway/telemetry"
        payload = json.dumps(
            {
                device_id: [
                    {
                        # FIXED: 原问题-int(time.time()*1000)重复模式，改为timestamp_ms()
                        "ts": timestamp_ms(),
                        "values": data,
                    }
                ]
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("ThingsBoard发布队列已满，丢弃消息")

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = "v1/gateway/attributes"
        payload = json.dumps({device_id: attrs}, ensure_ascii=False, default=str)
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("ThingsBoard发布队列已满，丢弃消息")

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = "v1/gateway/connect" if online else "v1/gateway/disconnect"
        payload = json.dumps([device_id])
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("ThingsBoard发布队列已满，丢弃消息")

    async def _connect_loop(self, broker: str, port: int, token: str, password: str) -> None:
        import aiomqtt

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=token or None,
                    password=password or None,
                    keepalive=_MQTT_KEEPALIVE,  # FIXED: 原问题-keepalive=60魔法数字
                ) as client:
                    self._connected = True
                    logger.info("ThingsBoard MQTT连接成功: %s:%d", broker, port)

                    await client.subscribe("v1/gateway/rpc", qos=1)
                    await client.subscribe("v1/gateway/attributes/request", qos=1)

                    msg_task = asyncio.create_task(
                        self._message_listen_loop(client),
                        name="thingsboard-msg-listen",
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client),
                        name="thingsboard-publish",
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
                logger.error("ThingsBoard MQTT连接异常: %s，5秒后重试", e)
                self._connected = False
                await asyncio.sleep(_MQTT_RECONNECT_DELAY)

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
                    logger.error("ThingsBoard MQTT发布失败: %s", e)
        except asyncio.CancelledError:
            pass

    async def _message_listen_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    # FIXED: 原问题-MQTT消息JSON解析无异常保护
                    try:
                        payload = json.loads(message.payload.decode("utf-8"))
                    except json.JSONDecodeError as e:
                        logger.warning("ThingsBoard MQTT消息JSON解析失败: %s", e)
                        continue

                    if topic == "v1/gateway/rpc":
                        device_id = payload.get("device", "")
                        rpc_data = payload.get("data", {})
                        method = rpc_data.get("method", "")
                        params = rpc_data.get("params", {})
                        request_id = rpc_data.get("id", "")

                        if self._rpc_callback:
                            result = await self._rpc_callback(device_id, method, params)
                            response_topic = "v1/gateway/rpc"
                            response_data = json.dumps(
                                {
                                    "device": device_id,
                                    "id": request_id,
                                    "data": result,
                                }
                            )
                            await client.publish(
                                response_topic,
                                response_data.encode("utf-8"),
                                qos=1,
                            )

                    elif topic == "v1/gateway/attributes/request":
                        logger.info("ThingsBoard属性请求: %s", payload)

                except Exception as e:
                    logger.error("ThingsBoard消息处理异常: %s", e)
        except asyncio.CancelledError:
            pass
