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
import json
import logging
import time
from typing import Any, Callable

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class ThingsBoardHandler(PlatformHandler):
    """ThingsBoard平台对接实现（网关MQTT协议）"""

    platform_name = "thingsboard"
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
        """连接ThingsBoard MQTT Broker

        ThingsBoard使用设备Token作为username进行认证
        """
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt未安装，请执行: pip install aiomqtt")

        self._config = config
        self._running = True

        broker = config.get("broker", "localhost")
        port = int(config.get("port", 1883))
        # ThingsBoard使用设备Token作为username
        token = config.get("token", config.get("username", ""))
        password = config.get("password", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, token, password),
            name="thingsboard-connect",
        )
        logger.info("ThingsBoard平台对接启动: %s:%d", broker, port)

    async def disconnect(self) -> None:
        """断开ThingsBoard连接"""
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
        logger.info("ThingsBoard平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        """上报遥测数据到ThingsBoard（网关协议格式）

        ThingsBoard网关遥测格式:
        {
            "device_id": {
                "ts": timestamp,
                "values": {"key": value, ...}
            }
        }
        """
        if not self._connected or not self._client:
            return
        topic = "v1/gateway/telemetry"
        payload = json.dumps({
            device_id: [{
                "ts": int(time.time() * 1000),
                "values": data,
            }]
        }, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("ThingsBoard遥测上报失败: %s", e)

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        """上传设备属性到ThingsBoard（网关协议格式）

        ThingsBoard网关属性格式:
        {
            "device_id": {"key": value, ...}
        }
        """
        if not self._connected or not self._client:
            return
        topic = "v1/gateway/attributes"
        payload = json.dumps({device_id: attrs}, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("ThingsBoard属性上传失败: %s", e)

    async def on_rpc_request(self, callback: Callable) -> None:
        """注册RPC请求回调"""
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        """上报设备上下线状态"""
        if not self._connected or not self._client:
            return
        topic = "v1/gateway/connect" if online else "v1/gateway/disconnect"
        payload = json.dumps(device_id)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("ThingsBoard设备状态上报失败: %s", e)

    async def _connect_loop(self, broker: str, port: int, token: str, password: str) -> None:
        """MQTT连接循环"""
        import aiomqtt

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=token or None,
                    password=password or None,
                    keepalive=60,
                ) as client:
                    self._client = client
                    self._connected = True
                    logger.info("ThingsBoard MQTT连接成功: %s:%d", broker, port)

                    # 订阅RPC请求和属性请求主题
                    await client.subscribe("v1/gateway/rpc", qos=1)
                    await client.subscribe("v1/gateway/attributes/request", qos=1)
                    self._subscribe_task = asyncio.create_task(
                        self._message_listen_loop(client),
                        name="thingsboard-msg-listen",
                    )

                    while self._running:
                        await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("ThingsBoard MQTT连接异常: %s，5秒后重试", e)
                self._client = None
                self._connected = False
                await asyncio.sleep(5)

    async def _message_listen_loop(self, client: Any) -> None:
        """监听平台下发的消息（RPC请求、属性请求）"""
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    payload = json.loads(message.payload.decode("utf-8"))

                    if topic == "v1/gateway/rpc":
                        # RPC请求: {"device": "device_id", "data": {"id": 1, "method": "setName", "params": {...}}}
                        device_id = payload.get("device", "")
                        rpc_data = payload.get("data", {})
                        method = rpc_data.get("method", "")
                        params = rpc_data.get("params", {})
                        request_id = rpc_data.get("id", "")

                        if self._rpc_callback:
                            result = await self._rpc_callback(device_id, method, params)
                            # 发送RPC响应
                            response_topic = "v1/gateway/rpc"
                            response_data = json.dumps({
                                "device": device_id,
                                "id": request_id,
                                "data": result,
                            })
                            await client.publish(
                                response_topic,
                                response_data.encode("utf-8"),
                                qos=1,
                            )

                    elif topic == "v1/gateway/attributes/request":
                        # 属性请求: {"id": 1, "device": "device_id", "client": true, "key": "attrKey"}
                        logger.info("ThingsBoard属性请求: %s", payload)

                except Exception as e:
                    logger.error("ThingsBoard消息处理异常: %s", e)
        except asyncio.CancelledError:
            pass
