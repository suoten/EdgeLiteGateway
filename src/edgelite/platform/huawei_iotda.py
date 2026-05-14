"""华为云IoTDA平台对接 - 通过MQTT协议对接华为云设备接入服务(IoTDA)

华为云IoTDA MQTT Topic体系:
- $oc/devices/{device_id}/sys/properties/report    - 属性上报
- $oc/devices/{device_id}/sys/commands/#            - 命令下发
- $oc/devices/{device_id}/sys/properties/set/#      - 属性设置
- $oc/devices/{device_id}/sys/events/report         - 事件上报
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class HuaweiIoTDAHandler(PlatformHandler):
    """华为云IoTDA平台对接实现"""

    platform_name = "huawei_iotda"
    platform_version = "1.0.0"

    def __init__(self):
        self._connected = False
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._running = False
        self._pub_queue: asyncio.Queue | None = None

    def _generate_password(self, device_id: str, secret: str, timestamp: str) -> str:
        message = f"{timestamp}"
        hmac_code = hmac.new(
            secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt未安装，请执行: pip install aiomqtt") from None

        self._config = config
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)  # FIXED: 原问题-硬编码队列大小

        broker = config.get("broker", config.get("host", ""))
        port = int(config.get("port", 8883))
        device_id = config.get("device_id", "")
        secret = config.get("secret", "")

        timestamp = str(timestamp_ms())  # FIXED: 原问题-直接调用int(time.time()*1000)，未使用统一工具函数
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
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._connected = False
        logger.info("华为云IoTDA平台对接已断开")

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"$oc/devices/{device_id}/sys/properties/report"
        payload = json.dumps(
            {
                "services": [
                    {
                        "service_id": "edgelite",
                        "properties": data,
                        "event_time": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                    }
                ]
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("华为云IoTDA发布队列已满，丢弃消息")

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"$oc/devices/{device_id}/sys/properties/report"
        payload = json.dumps(
            {
                "services": [
                    {
                        "service_id": "edgelite_attrs",
                        "properties": attrs,
                        "event_time": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                    }
                ]
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("华为云IoTDA发布队列已满，丢弃消息")

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"$oc/devices/{device_id}/sys/events/report"
        payload = json.dumps(
            {
                "services": [
                    {
                        "service_id": "$device_status",
                        "event_type": "device_status_change",
                        "paras": {"status": "ONLINE" if online else "OFFLINE"},
                        "event_time": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                    }
                ]
            },
            ensure_ascii=False,
            default=str,
        )
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("华为云IoTDA发布队列已满，丢弃消息")

    async def _connect_loop(
        self, broker: str, port: int, username: str, password: str, device_id: str
    ) -> None:
        import aiomqtt

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username,
                    password=password,
                    keepalive=_MQTT_KEEPALIVE,
                ) as client:
                    self._connected = True
                    logger.info("华为云IoTDA MQTT连接成功: %s:%d", broker, port)

                    await client.subscribe(f"$oc/devices/{device_id}/sys/commands/#", qos=1)
                    await client.subscribe(f"$oc/devices/{device_id}/sys/properties/set/#", qos=1)

                    msg_task = asyncio.create_task(
                        self._message_listen_loop(client, device_id),
                        name="huawei-iotda-msg-listen",
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client),
                        name="huawei-iotda-publish",
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
                logger.error("华为云IoTDA MQTT连接异常: %s，5秒后重试", e)
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
                    logger.error("华为云IoTDA MQTT发布失败: %s", e)
        except asyncio.CancelledError:
            pass

    async def _message_listen_loop(self, client: Any, device_id: str) -> None:
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
                        logger.warning("华为云IoTDA MQTT消息JSON解析失败: %s", e)
                        continue

                    if "/sys/commands/" in topic:
                        cmd_name = payload.get("command_name", "")
                        paras = payload.get("paras", {})
                        if self._rpc_callback:
                            await self._rpc_callback(device_id, cmd_name, paras)

                    elif "/sys/properties/set/" in topic:
                        props = payload.get("properties", {})
                        if self._rpc_callback:
                            await self._rpc_callback(device_id, "set_properties", props)

                except Exception as e:
                    logger.error("华为云IoTDA消息处理异常: %s", e)
        except asyncio.CancelledError:
            pass
