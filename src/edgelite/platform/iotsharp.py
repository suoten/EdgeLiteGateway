"""IoTSharp平台对接 - 通过MQTT协议对接IoTSharp IoT平台

IoTSharp MQTT Topic体系:
- devices/{device_id}/telemetry   - 遥测数据上报
- devices/{device_id}/attributes  - 属性上传
- devices/{device_id}/rpc/request  - RPC请求（平台→网关）
- devices/{device_id}/rpc/response - RPC响应（网关→平台）
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

from edgelite.constants import _MQTT_QUEUE_MAXSIZE, _MQTT_KEEPALIVE, _MQTT_RECONNECT_DELAY, _QUEUE_POLL_TIMEOUT
from edgelite.platform.base import PlatformHandler
from edgelite.utils import timestamp_ms  # FIXED: 原问题-缺失导入导致NameError

logger = logging.getLogger(__name__)


class IoTSharpHandler(PlatformHandler):
    """IoTSharp平台对接实现"""

    platform_name = "iotsharp"
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

        broker = config.get("broker", "")
        if not broker:
            raise ValueError("broker is required")
        port = int(config.get("port", 1883))
        username = config.get("username", "")
        password = config.get("password", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password),
            name="iotsharp-connect",
        )
        logger.info("IoTSharp平台对接启动: %s:%d", broker, port)

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._connected = False
        logger.info("IoTSharp平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"devices/{device_id}/telemetry"
        payload = json.dumps(data, ensure_ascii=False, default=str)
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("IoTSharp发布队列已满，丢弃消息")

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"devices/{device_id}/attributes"
        payload = json.dumps(attrs, ensure_ascii=False, default=str)
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("IoTSharp发布队列已满，丢弃消息")

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"devices/{device_id}/attributes"
        payload = json.dumps({"online": online, "lastActivityTime": timestamp_ms()})  # FIXED: 原问题-直接调用int(time.time()*1000)，未使用统一工具函数
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("IoTSharp发布队列已满，丢弃消息")

    async def _connect_loop(self, broker: str, port: int, username: str, password: str) -> None:
        import aiomqtt

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username or None,
                    password=password or None,
                    keepalive=_MQTT_KEEPALIVE,  # FIXED: 原问题-keepalive=60魔法数字
                ) as client:
                    self._connected = True
                    logger.info("IoTSharp MQTT连接成功: %s:%d", broker, port)

                    await client.subscribe("devices/+/rpc/request", qos=1)

                    rpc_task = asyncio.create_task(
                        self._rpc_listen_loop(client),
                        name="iotsharp-rpc-listen",
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client),
                        name="iotsharp-publish",
                    )

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        for t in [rpc_task, pub_task]:
                            if not t.done():
                                t.cancel()
                        for t in [rpc_task, pub_task]:
                            with contextlib.suppress(asyncio.CancelledError):
                                await t
                        self._connected = False

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("IoTSharp MQTT连接异常: %s，5秒后重试", e)
                self._connected = False
                await asyncio.sleep(_MQTT_RECONNECT_DELAY)

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    topic, payload, qos = await asyncio.wait_for(self._pub_queue.get(), timeout=_QUEUE_POLL_TIMEOUT)  # FIXED: 原问题-timeout=1.0魔法数字
                except TimeoutError:
                    continue
                try:
                    await client.publish(topic, payload, qos=qos)
                except Exception as e:
                    logger.error("IoTSharp MQTT发布失败: %s", e)
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
                    if len(parts) >= 4 and parts[3] == "request":
                        device_id = parts[1]
                        # FIXED: 原问题-MQTT消息JSON解析无异常保护
                        try:
                            payload = json.loads(message.payload.decode("utf-8"))
                        except json.JSONDecodeError as e:
                            logger.warning("IoTSharp MQTT消息JSON解析失败: %s", e)
                            continue
                        method = payload.get("method", "")
                        params = payload.get("params", {})

                        if self._rpc_callback:
                            rpc_result = await self._rpc_callback(device_id, method, params)
                            response_topic = f"devices/{device_id}/rpc/response"
                            response_data = json.dumps(
                                {
                                    "method": method,
                                    "result": rpc_result,
                                }
                            )
                            await client.publish(
                                response_topic,
                                response_data.encode("utf-8"),
                                qos=1,
                            )
                except Exception as e:
                    logger.error("IoTSharp RPC处理异常: %s", e)
        except asyncio.CancelledError:
            pass
