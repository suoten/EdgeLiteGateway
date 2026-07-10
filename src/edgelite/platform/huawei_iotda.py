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
import random
import time
from collections.abc import Callable
from typing import Any

from edgelite.constants import (  # FIXED-P0: 补充_QUEUE_POLL_TIMEOUT导入
    _MQTT_KEEPALIVE,
    _MQTT_QUEUE_MAXSIZE,
    _MQTT_RECONNECT_DELAY,
    _NORTH_RETRY_MAX_ATTEMPTS,  # FIXED-P0: 导入重试上限常量，用于_publish_loop重试计数
    _QUEUE_POLL_TIMEOUT,
)
from edgelite.platform.base import PlatformHandler
from edgelite.utils import timestamp_ms  # FIXED: 原问题-缺失导入导致NameError

logger = logging.getLogger(__name__)


class HuaweiIoTDAHandler(PlatformHandler):
    """华为云IoTDA平台对接实现"""

    platform_name = "huawei_iotda"
    platform_version = "1.0.0"

    def __init__(self):
        super().__init__()  # FIXED-P2: 调用基类__init__，初始化离线队列和重连退避
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._running = False
        self._pub_queue: asyncio.Queue | None = None
        self._consecutive_failures: int = 0  # FIXED-P2: 原问题-重连固定延迟无退避，添加指数退避计数

    def _generate_password(self, device_id: str, secret: str, timestamp: str) -> str:
        # FIXED-P0: 原问题-HMAC的key/message顺序错误。华为官方规范要求：
        # base64.b64encode(hmac.new(timestamp.encode(), secret.encode(), hashlib.sha256).digest()).decode()
        # 即 key=timestamp, message=secret。原代码 key=secret, message=timestamp 导致认证失败。
        # 时间戳使用毫秒级 str(timestamp_ms())，由调用方传入。
        hmac_code = hmac.new(timestamp.encode("utf-8"), secret.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(hmac_code).decode("utf-8")

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt not installed, run: pip install aiomqtt") from None

        self._config = config
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)  # FIXED: 原问题-硬编码队列大小

        broker = config.get("broker", config.get("host", ""))
        if not broker:
            raise ValueError("broker is required")
        port = int(config.get("port", 8883))
        device_id = config.get("device_id", "")
        secret = config.get("secret", "")

        # FIXED-P1#17: 存储 device_id/secret/broker/port，每次重连重新生成带时间戳的密码
        self._huawei_broker = broker
        self._huawei_port = port
        self._huawei_device_id = device_id
        self._huawei_secret = secret
        self._huawei_password_fallback = config.get("password", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, device_id, secret),
            name="huawei-iotda-connect",
        )
        logger.info("Huawei IoTDA platform started: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._connected = False
        logger.info("Huawei IoTDA platform disconnected")  # FIXED-P3: 中文日志→英文

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
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
        # FIXED-P2#26: 断连时入离线队列而非静默丢弃
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)  # FIXED-P2#26: 队列满时入离线队列
            logger.warning("Huawei IoTDA pub queue full, enqueued offline")  # FIXED-P3: 中文日志→英文

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
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
        # FIXED-P2#26: 断连时入离线队列而非静默丢弃
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)  # FIXED-P2#26
            logger.warning("Huawei IoTDA pub queue full, enqueued offline")  # FIXED-P3: 中文日志→英文

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
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
        # FIXED-P2#26: 断连时入离线队列而非静默丢弃
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)  # FIXED-P2#26
            logger.warning("Huawei IoTDA pub queue full, enqueued offline")  # FIXED-P3: 中文日志→英文

    async def _connect_loop(self, broker: str, port: int, device_id: str, secret: str) -> None:
        import aiomqtt

        while self._running:
            try:
                # FIXED-P1#17: 每次重连重新生成带时间戳的密码，避免密码过期导致认证失败
                timestamp = str(timestamp_ms())
                if secret:
                    password = self._generate_password(device_id, secret, timestamp)
                else:
                    password = self._huawei_password_fallback
                username = f"{device_id}_{timestamp}"

                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username,
                    password=password,
                    keepalive=_MQTT_KEEPALIVE,
                ) as client:
                    self._connected = True
                    self._consecutive_failures = 0  # FIXED-P2: 连接成功重置退避计数
                    logger.info("Huawei IoTDA MQTT connected: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

                    # FIXED-P2#26: 重连成功后刷出离线缓存队列
                    await self._flush_offline_queue()

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
                self._consecutive_failures += 1
                delay = min(5 * (2 ** (self._consecutive_failures - 1)), 60)
                delay *= 0.5 + random.random() * 0.5  # FIXED-P2: 原问题-重连固定延迟无退避无抖动，改为指数退避+jitter
                logger.error(
                    "Huawei IoTDA MQTT connection error: %s, retrying in %.1fs (failures=%d)",
                    e,
                    delay,
                    self._consecutive_failures,
                )  # FIXED-P3: 中文日志→英文
                self._connected = False
                await asyncio.sleep(delay)

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    item = await asyncio.wait_for(
                        self._pub_queue.get(), timeout=_QUEUE_POLL_TIMEOUT
                    )  # FIXED: 原问题-timeout=1.0魔法数字
                except TimeoutError:
                    continue
                # FIXED-P0: 兼容4元组(topic, payload, qos, retry_count)和3元组(topic, payload, qos)
                if len(item) == 4:
                    topic, payload, qos, retry_count = item
                else:
                    topic, payload, qos = item
                    retry_count = 0
                try:
                    await client.publish(topic, payload, qos=qos)
                except Exception as e:
                    logger.error("Huawei IoTDA MQTT publish failed: %s", e)  # FIXED-P3: 中文日志→英文
                    # FIXED-P0: 原问题-失败消息无限重入队导致队列积压；增加retry_count计数，
                    # 超过_NORTH_RETRY_MAX_ATTEMPTS后丢弃，参考thingsboard.py的4元组实现
                    retry_count += 1
                    if retry_count > _NORTH_RETRY_MAX_ATTEMPTS:
                        # FIXED-P1: 超过重试上限时写入离线队列而非直接丢弃，原问题-消息直接丢弃无持久化
                        self._enqueue_offline(topic, payload, qos)
                        logger.warning("Huawei IoTDA message enqueued offline after %d retries: %s", retry_count, topic)
                    elif self._pub_queue is not None:
                        try:
                            self._pub_queue.put_nowait((topic, payload, qos, retry_count))
                        except Exception as qe:
                            # FIXED-P1: 原问题-重入队列失败时仅log不入离线队列，消息永久丢失
                            # 修复：重入队列失败时调用_enqueue_offline缓存到离线队列，网络恢复后重试
                            self._enqueue_offline(topic, payload, qos)
                            logger.error(
                                "Huawei IoTDA re-enqueue failed, enqueued offline: %s", qe
                            )  # FIXED-P2: 原问题-重入队列失败时pass吞没异常，消息永久丢失无感知
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
                        logger.warning("Huawei IoTDA MQTT JSON parse failed: %s", e)  # FIXED-P3: 中文日志→英文
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
                    logger.error("Huawei IoTDA message handler error: %s", e)  # FIXED-P3: 中文日志→英文
        except asyncio.CancelledError:
            pass
