"""ThingsPanel平台对接 - 通过MQTT协议对接ThingsPanel开源物联网平台

ThingsPanel MQTT Topic体系:
- v1/devices/me/telemetry       - 遥测数据上报
- v1/devices/me/attributes      - 属性上报
- v1/devices/me/rpc/request/{id} - RPC请求下发
- v1/devices/me/rpc/response/{id} - RPC响应上报
- v1/gateway/connect            - 网关连接
- v1/gateway/disconnect         - 网关断开
- v1/gateway/telemetry          - 网关子设备遥测上报
- v1/gateway/attributes         - 网关子设备属性上报
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class ThingsPanelHandler(PlatformHandler):
    """ThingsPanel平台对接实现"""

    platform_name = "thingspanel"
    platform_version = "1.0.0"

    def __init__(self):
        self._connected = False
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._running = False
        self._pub_queue: asyncio.Queue | None = None
        self._client: Any = None

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt未安装，请执行: pip install aiomqtt")

        self._config = config
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=1000)

        broker = config.get("broker", config.get("host", "localhost"))
        port = int(config.get("port", 1883))
        username = config.get("username", config.get("access_key", ""))
        password = config.get("password", config.get("access_secret", ""))
        device_token = config.get("device_token", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password, device_token),
            name="thingspanel-connect",
        )
        logger.info("ThingsPanel平台对接启动: %s:%d", broker, port)

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                self._client.disconnect()
            except Exception as e:
                logger.debug("ThingsPanel断开连接失败: %s", e)
        self._connected = False
        logger.info("ThingsPanel平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._pub_queue:
            return
        payload = {
            "ts": int(time.time() * 1000),
            "values": data,
        }
        await self._pub_queue.put({
            "topic": "v1/gateway/telemetry",
            "payload": json.dumps({device_id: [payload]}, ensure_ascii=False),
        })

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._pub_queue:
            return
        await self._pub_queue.put({
            "topic": "v1/gateway/attributes",
            "payload": json.dumps({device_id: attrs}, ensure_ascii=False),
        })

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._pub_queue:
            return
        if online:
            payload = {"device": device_id}
            await self._pub_queue.put({
                "topic": "v1/gateway/connect",
                "payload": json.dumps(payload, ensure_ascii=False),
            })
        else:
            payload = {"device": device_id}
            await self._pub_queue.put({
                "topic": "v1/gateway/disconnect",
                "payload": json.dumps(payload, ensure_ascii=False),
            })

    async def _connect_loop(
        self, broker: str, port: int, username: str, password: str, device_token: str
    ) -> None:
        import aiomqtt

        backoff = 1
        max_backoff = 60

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username or None,
                    password=password or None,
                    keepalive=60,
                    clean_session=True,
                    client_id=f"edgelite-thingspanel-{uuid.uuid4().hex[:8]}",
                ) as client:
                    self._client = client
                    self._connected = True
                    backoff = 1
                    logger.info("ThingsPanel已连接: %s:%d", broker, port)

                    await client.subscribe("v1/devices/me/rpc/request/+")
                    await client.subscribe("v1/gateway/rpc/request/+")

                    await asyncio.gather(
                        self._rpc_loop(client),
                        self._publish_loop(client),
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.error("ThingsPanel连接异常: %s，%ds后重连", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        self._connected = False

    async def _rpc_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    request_id = topic.split("/")[-1]
                    payload = json.loads(message.payload.decode())
                    method = payload.get("method", "")
                    params = payload.get("params", {})

                    if self._rpc_callback:
                        result = await self._rpc_callback("gateway", method, params)
                        response_topic = f"v1/devices/me/rpc/response/{request_id}"
                        response_payload = json.dumps({"result": result} if result is not None else {}, ensure_ascii=False)
                        await client.publish(response_topic, response_payload)
                        logger.debug("ThingsPanel RPC响应: %s -> %s", method, request_id)
                except Exception as e:
                    logger.error("ThingsPanel RPC处理异常: %s", e)
        except asyncio.CancelledError:
            pass

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                try:
                    msg = await asyncio.wait_for(self._pub_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                try:
                    await client.publish(msg["topic"], msg["payload"])
                except Exception as e:
                    logger.error("ThingsPanel发布失败: %s", e)
                    if self._pub_queue:
                        try:
                            self._pub_queue.put_nowait(msg)
                        except asyncio.QueueFull:
                            pass
                    raise
        except asyncio.CancelledError:
            pass
