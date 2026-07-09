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
import random
import time
from collections.abc import Callable
from typing import Any

from edgelite.constants import (  # FIXED-P0: 补充_QUEUE_POLL_TIMEOUT导入
    _MQTT_KEEPALIVE,
    _MQTT_QUEUE_MAXSIZE,
    _MQTT_RECONNECT_DELAY,
    _NORTH_RETRY_MAX_ATTEMPTS,
    _QUEUE_POLL_TIMEOUT,
)
from edgelite.platform.base import PlatformHandler
from edgelite.utils import timestamp_ms  # FIXED: 原问题-缺失导入导致NameError

logger = logging.getLogger(__name__)


class ThingsBoardHandler(PlatformHandler):
    """ThingsBoard平台对接实现（网关MQTT协议）"""

    platform_name = "thingsboard"
    platform_version = "1.0.0"

    def __init__(self):
        super().__init__()  # FIXED-P2: 调用基类__init__，初始化离线队列和重连退避
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._running = False
        self._pub_queue: asyncio.Queue | None = None
        self._consecutive_failures: int = 0  # FIXED-P2: 原问题-重连固定延迟无退避，添加指数退避计数

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt not installed, run: pip install aiomqtt") from None

        self._config = config
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)  # FIXED: 原问题-硬编码队列大小

        broker = config.get("broker", "")
        if not broker:
            raise ValueError("broker is required")
        port = int(config.get("port", 1883))
        token = config.get("token", config.get("username", ""))
        password = config.get("password", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, token, password),
            name="thingsboard-connect",
        )
        logger.info("ThingsBoard platform started: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._connected = False
        logger.info("ThingsBoard platform disconnected")  # FIXED-P3: 中文日志→英文

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
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
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1, 0))
        except asyncio.QueueFull:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)  # FIXED-P2#26: 队列满时入离线队列
            logger.warning("ThingsBoard pub queue full, enqueued offline")  # FIXED-P3: 中文日志→英文

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        topic = "v1/gateway/attributes"
        payload = json.dumps({device_id: attrs}, ensure_ascii=False, default=str)
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1, 0))
        except asyncio.QueueFull:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)  # FIXED-P2#26
            logger.warning("ThingsBoard pub queue full, enqueued offline")  # FIXED-P3: 中文日志→英文

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        topic = "v1/gateway/connect" if online else "v1/gateway/disconnect"
        payload = json.dumps([device_id])
        # FIXED-P2#26: 断连时入离线队列而非静默丢弃
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1, 0))
        except asyncio.QueueFull:
            self._enqueue_offline(topic, payload.encode("utf-8"), 1)  # FIXED-P2#26
            logger.warning("ThingsBoard pub queue full, enqueued offline")  # FIXED-P3: 中文日志→英文

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
                    self._consecutive_failures = 0  # FIXED-P2: 连接成功重置退避计数
                    logger.info("ThingsBoard MQTT connected: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

                    # FIXED-P2#26: 重连成功后刷出离线缓存队列
                    await self._flush_offline_queue()

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
                self._consecutive_failures += 1
                delay = min(5 * (2 ** (self._consecutive_failures - 1)), 60)
                delay *= 0.5 + random.random() * 0.5  # FIXED-P2: 原问题-重连固定延迟无退避无抖动，改为指数退避+jitter
                logger.error("ThingsBoard MQTT connection error: %s, retrying in %.1fs (failures=%d)", e, delay, self._consecutive_failures)  # FIXED-P3: 中文日志→英文
                self._connected = False
                await asyncio.sleep(delay)

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    item = await asyncio.wait_for(self._pub_queue.get(), timeout=_QUEUE_POLL_TIMEOUT)  # FIXED: 原问题-timeout=1.0魔法数字
                except TimeoutError:
                    continue
                # 兼容4元组(topic, payload, qos, retry_count)和3元组(topic, payload, qos)
                if len(item) == 4:
                    topic, payload, qos, retry_count = item
                else:
                    topic, payload, qos = item
                    retry_count = 0
                try:
                    await client.publish(topic, payload, qos=qos)
                except Exception as e:
                    logger.error("ThingsBoard MQTT publish failed: %s", e)  # FIXED-P3: 中文日志→英文
                    # 重入队列带重试计数，超过阈值后丢弃
                    retry_count += 1
                    if retry_count > _NORTH_RETRY_MAX_ATTEMPTS:
                        # FIXED-P2: 超过重试上限时写入离线队列而非直接丢弃，原问题-消息直接丢弃无持久化
                        # 修复方案: 添加_offline_queue引用，publish失败时enqueue，网络恢复后重试
                        self._enqueue_offline(topic, payload, qos)
                        logger.warning("ThingsBoard message enqueued offline after %d retries: %s", retry_count, topic)
                    elif self._pub_queue is not None:
                        try:
                            self._pub_queue.put_nowait((topic, payload, qos, retry_count))
                        except Exception as qe:
                            # FIXED-P2: 重入队列失败时写入离线队列，避免消息丢失
                            self._enqueue_offline(topic, payload, qos)
                            logger.error("ThingsBoard re-enqueue failed, enqueued offline: %s", qe)  # FIXED-P2: 原问题-重入队列失败时pass吞没异常
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
                        logger.warning("ThingsBoard MQTT JSON parse failed: %s", e)  # FIXED-P3: 中文日志→英文
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
                        logger.info("ThingsBoard attribute request: %s", payload)  # FIXED-P3: 中文日志→英文

                except Exception as e:
                    logger.error("ThingsBoard message handler error: %s", e)  # FIXED-P3: 中文日志→英文
        except asyncio.CancelledError:
            pass
