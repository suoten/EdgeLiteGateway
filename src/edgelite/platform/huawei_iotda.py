"""华为云IoTDA平台对接 - 通过MQTT协议对接华为云设备接入服务(IoTDA)

华为云IoTDA MQTT Topic体系:
- $oc/devices/{device_id}/sys/properties/report    - 属性上报
- $oc/devices/{device_id}/sys/commands/#            - 命令下发
- $oc/devices/{device_id}/sys/properties/set/#      - 属性设置
- $oc/devices/{device_id}/sys/events/report         - 事件上报
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import hmac
import hashlib
from typing import Any, Callable

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class HuaweiIoTDAHandler(PlatformHandler):
    """华为云IoTDA平台对接实现"""

    platform_name = "huawei_iotda"
    platform_version = "1.0.0"

    def __init__(self):
        self._connected = False
        self._client = None
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._subscribe_task: asyncio.Task | None = None
        self._running = False

    def _generate_password(self, device_id: str, secret: str, timestamp: str) -> str:
        """生成华为云IoTDA MQTT密码 (HMAC-SHA256)"""
        message = f"{timestamp}"
        hmac_code = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
        import base64
        return base64.b64encode(hmac_code).decode("utf-8")

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt未安装，请执行: pip install aiomqtt")

        self._config = config
        self._running = True

        broker = config.get("broker", config.get("host", ""))
        port = int(config.get("port", 8883))
        device_id = config.get("device_id", "")
        secret = config.get("secret", "")

        timestamp = str(int(time.time()))
        if secret:
            password = self._generate_password(device_id, secret, timestamp)
        else:
            password = config.get("password", "")

        username = f"{device_id}_{timestamp}"

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password, device_id),
            name="huawei-iotda-connect",
        )
        logger.info("华为云IoTDA平台对接启动: %s:%d", broker, port)

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
        logger.info("华为云IoTDA平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._connected or not self._client:
            return
        topic = f"$oc/devices/{device_id}/sys/properties/report"
        payload = json.dumps({
            "services": [{
                "service_id": "edgelite",
                "properties": data,
                "event_time": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            }]
        }, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("华为云IoTDA遥测上报失败: %s", e)

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._connected or not self._client:
            return
        topic = f"$oc/devices/{device_id}/sys/properties/report"
        payload = json.dumps({
            "services": [{
                "service_id": "edgelite_attrs",
                "properties": attrs,
                "event_time": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            }]
        }, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("华为云IoTDA属性上传失败: %s", e)

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._connected or not self._client:
            return
        topic = f"$oc/devices/{device_id}/sys/events/report"
        payload = json.dumps({
            "services": [{
                "service_id": "$device_status",
                "event_type": "device_status_change",
                "paras": {"status": "ONLINE" if online else "OFFLINE"},
                "event_time": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
            }]
        }, ensure_ascii=False, default=str)
        try:
            await self._client.publish(topic, payload.encode("utf-8"), qos=1)
        except Exception as e:
            logger.error("华为云IoTDA设备状态上报失败: %s", e)

    async def _connect_loop(self, broker: str, port: int, username: str, password: str, device_id: str) -> None:
        import aiomqtt

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username,
                    password=password,
                    keepalive=60,
                ) as client:
                    self._client = client
                    self._connected = True
                    logger.info("华为云IoTDA MQTT连接成功: %s:%d", broker, port)

                    await client.subscribe(f"$oc/devices/{device_id}/sys/commands/#", qos=1)
                    await client.subscribe(f"$oc/devices/{device_id}/sys/properties/set/#", qos=1)
                    self._subscribe_task = asyncio.create_task(
                        self._message_listen_loop(client, device_id),
                        name="huawei-iotda-msg-listen",
                    )

                    while self._running:
                        await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("华为云IoTDA MQTT连接异常: %s，5秒后重试", e)
                self._client = None
                self._connected = False
                await asyncio.sleep(5)

    async def _message_listen_loop(self, client: Any, device_id: str) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    payload = json.loads(message.payload.decode("utf-8"))

                    if "/sys/commands/" in topic:
                        cmd_name = payload.get("command_name", "")
                        paras = payload.get("paras", {})
                        if self._rpc_callback:
                            result = await self._rpc_callback(device_id, cmd_name, paras)

                    elif "/sys/properties/set/" in topic:
                        props = payload.get("properties", {})
                        if self._rpc_callback:
                            await self._rpc_callback(device_id, "set_properties", props)

                except Exception as e:
                    logger.error("华为云IoTDA消息处理异常: %s", e)
        except asyncio.CancelledError:
            pass
