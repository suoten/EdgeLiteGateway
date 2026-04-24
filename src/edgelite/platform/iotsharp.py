"""IoTSharp平台对接 - 通过MQTT协议对接IoTSharp IoT平台

IoTSharp MQTT Topic体系:
- devices/{device_id}/telemetry   - 遥测数据上报
- devices/{device_id}/attributes  - 属性上传
- devices/{device_id}/rpc/request  - RPC请求（平台→网关）
- devices/{device_id}/rpc/response - RPC响应（网关→平台）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class IoTSharpHandler(PlatformHandler):
    """IoTSharp平台对接实现"""

    platform_name = "iotsharp"
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
        """连接IoTSharp MQTT Broker"""
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt未安装，请执行: pip install aiomqtt")

        self._config = config
        self._running = True

        broker = config.get("broker", "localhost")
        port = int(config.get("port", 1883))
        username = config.get("username", "")
        password = config.get("password", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password),
            name="iotsharp-connect",
        )
        logger.info("IoTSharp平台对接启动: %s:%d", broker, port)

    async def disconnect(self) -> None:
        """断开IoTSharp连接"""
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
        logger.info("IoTSharp平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        """上报遥测数据到IoTSharp"""
        if not self._connected or not self._client:
            return
        topic = f"devices/{device_id}/telemetry"
        payload = json.dumps(data, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("IoTSharp遥测上报失败: %s", e)

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        """上传设备属性到IoTSharp"""
        if not self._connected or not self._client:
            return
        topic = f"devices/{device_id}/attributes"
        payload = json.dumps(attrs, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("IoTSharp属性上传失败: %s", e)

    async def on_rpc_request(self, callback: Callable) -> None:
        """注册RPC请求回调"""
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        """上报设备上下线状态"""
        if not self._connected or not self._client:
            return
        topic = f"devices/{device_id}/attributes"
        payload = json.dumps({"online": online, "lastActivityTime": int(time.time() * 1000)})
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("IoTSharp设备状态上报失败: %s", e)

    async def _connect_loop(self, broker: str, port: int, username: str, password: str) -> None:
        """MQTT连接循环"""
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
                    logger.info("IoTSharp MQTT连接成功: %s:%d", broker, port)

                    # 订阅RPC请求主题
                    await client.subscribe("devices/+/rpc/request", qos=1)
                    self._subscribe_task = asyncio.create_task(
                        self._rpc_listen_loop(client),
                        name="iotsharp-rpc-listen",
                    )

                    while self._running:
                        await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("IoTSharp MQTT连接异常: %s，5秒后重试", e)
                self._client = None
                self._connected = False
                await asyncio.sleep(5)

    async def _rpc_listen_loop(self, client: Any) -> None:
        """监听RPC请求"""
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    # 解析 devices/{device_id}/rpc/request
                    parts = topic.split("/")
                    if len(parts) >= 4 and parts[3] == "request":
                        device_id = parts[1]
                        payload = json.loads(message.payload.decode("utf-8"))
                        method = payload.get("method", "")
                        params = payload.get("params", {})

                        if self._rpc_callback:
                            result = await self._rpc_callback(device_id, method, params)
                            # 发送RPC响应
                            response_topic = f"devices/{device_id}/rpc/response"
                            response_data = json.dumps({
                                "method": method,
                                "result": result,
                            })
                            await client.publish(
                                response_topic,
                                response_data.encode("utf-8"),
                                qos=1,
                            )
                except Exception as e:
                    logger.error("IoTSharp RPC处理异常: %s", e)
        except asyncio.CancelledError:
            pass
