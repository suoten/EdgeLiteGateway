"""自定义MQTT平台对接 - 通用MQTT数据转发

支持自定义Topic前缀，适用于任何MQTT兼容平台
Topic体系:
- {topic_prefix}/{device_id}/telemetry   - 遥测数据上报
- {topic_prefix}/{device_id}/attributes  - 属性上传
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Callable
from typing import Any

from edgelite.constants import (
    _MQTT_KEEPALIVE,
    _MQTT_QUEUE_MAXSIZE,
    _MQTT_RECONNECT_DELAY,
    _NORTH_RETRY_MAX_ATTEMPTS,
    _PLATFORM_RECONNECT_MAX_BACKOFF,
    _QUEUE_POLL_TIMEOUT,
)
from edgelite.platform.base import PlatformHandler

logger = logging.getLogger(__name__)


class CustomMqttHandler(PlatformHandler):
    """自定义MQTT平台对接实现"""

    platform_name = "custom"
    platform_version = "1.0.0"

    def __init__(self):
        super().__init__()  # FIXED-P2: 调用基类__init__，初始化离线队列和重连退避
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._running = False
        self._pub_queue: asyncio.Queue | None = None
        self._topic_prefix = "edgelite"
        self._topic_template: str = ""  # FIXED-P2: 自定义MQTT topic模板
        self._payload_template: str = ""  # FIXED-P2: 自定义MQTT payload模板
        self._consecutive_failures: int = 0  # 重连指数退避计数

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt not installed, run: pip install aiomqtt") from None

        self._config = config
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)  # FIXED: 原问题-硬编码队列大小
        self._topic_prefix = config.get("topic_prefix", "edgelite")
        self._topic_template = config.get("topic_template", "")  # FIXED-P2: 读取自定义topic模板
        self._payload_template = config.get("payload_template", "")  # FIXED-P2: 读取自定义payload模板

        broker = config.get("broker", "")
        if not broker:
            raise ValueError("broker is required")
        port = int(config.get("port", 1883))
        username = config.get("username", "")
        password = config.get("password", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password),
            name="custom-mqtt-connect",
        )
        logger.info(
            "Custom MQTT platform started: %s:%d, prefix: %s", broker, port, self._topic_prefix
        )  # FIXED-P3: 中文日志→英文

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            try:
                await self._connect_task
            except asyncio.CancelledError:
                logger.debug("Custom MQTT connect task cancelled")  # FIXED-P3: 中文日志→英文
        self._connected = False
        logger.info("Custom MQTT platform disconnected")  # FIXED-P3: 中文日志→英文

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            if not self._connected:
                self._enqueue_offline(
                    self._render_topic("telemetry", device_id),
                    self._render_payload(data, device_id).encode("utf-8"),
                    1,
                )  # FIXED-P2: 断线时缓存到离线队列
            return
        topic = self._render_topic("telemetry", device_id)
        payload = self._render_payload(data, device_id)
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("Custom MQTT pub queue full, dropping message")  # FIXED-P3: 中文日志→英文

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._connected or not self._pub_queue:
            if not self._connected:
                self._enqueue_offline(
                    self._render_topic("attributes", device_id),
                    self._render_payload(attrs, device_id).encode("utf-8"),
                    1,
                )  # FIXED-P2: 断线时缓存到离线队列
            return
        topic = self._render_topic("attributes", device_id)
        payload = self._render_payload(attrs, device_id)
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("Custom MQTT pub queue full, dropping message")  # FIXED-P3: 中文日志→英文

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._connected or not self._pub_queue:
            return
        topic = f"{self._topic_prefix}/{device_id}/status"
        payload = json.dumps({"online": online})
        try:
            self._pub_queue.put_nowait((topic, payload.encode("utf-8"), 1))
        except asyncio.QueueFull:
            logger.warning("Custom MQTT pub queue full, dropping message")  # FIXED-P3: 中文日志→英文

    async def _connect_loop(self, broker: str, port: int, username: str, password: str) -> None:
        import aiomqtt

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username or None,
                    password=password or None,
                    keepalive=_MQTT_KEEPALIVE,
                ) as client:
                    self._connected = True
                    self._consecutive_failures = 0  # 连接成功重置退避计数
                    logger.info("Custom MQTT connected: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

                    # FIXED-P2: 重连成功后刷出离线缓存队列，确保publish失败的消息重试
                    await self._flush_offline_queue()

                    await client.subscribe(f"{self._topic_prefix}/+/rpc/request", qos=1)

                    rpc_task = asyncio.create_task(
                        self._rpc_listen_loop(client),
                        name="custom-mqtt-rpc-listen",
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client),
                        name="custom-mqtt-publish",
                    )

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        for t in [rpc_task, pub_task]:
                            if not t.done():
                                t.cancel()
                        for t in [rpc_task, pub_task]:
                            try:
                                await t
                            except asyncio.CancelledError:
                                logger.debug("Custom MQTT sub-task cancelled")  # FIXED-P3: 中文日志→英文
                        self._connected = False

            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._consecutive_failures += 1
                delay = min(
                    _MQTT_RECONNECT_DELAY * (2 ** (self._consecutive_failures - 1)), _PLATFORM_RECONNECT_MAX_BACKOFF
                )
                delay *= 0.5 + random.random() * 0.5  # 指数退避+jitter
                logger.error(
                    "Custom MQTT connection error: %s, retrying in %.1fs (failures=%d)",
                    e,
                    delay,
                    self._consecutive_failures,
                )
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
                # 兼容4元组(topic, payload, qos, retry_count)和3元组(topic, payload, qos)
                if len(item) == 4:
                    topic, payload, qos, retry_count = item
                else:
                    topic, payload, qos = item
                    retry_count = 0
                try:
                    await client.publish(topic, payload, qos=qos)
                except Exception as e:
                    logger.error("Custom MQTT publish failed: %s", e)  # FIXED-P3: 中文日志→英文
                    # publish失败时将消息重入队列（带重试计数限制）
                    retry_count += 1
                    if retry_count > _NORTH_RETRY_MAX_ATTEMPTS:
                        # FIXED-P2: 超过重试上限时写入离线队列而非直接丢弃，原问题-消息直接丢弃无持久化
                        # 修复方案: 添加_offline_queue引用，publish失败时enqueue，网络恢复后重试
                        self._enqueue_offline(topic, payload, qos)
                        logger.warning("Custom MQTT message enqueued offline after %d retries: %s", retry_count, topic)
                    elif self._pub_queue is not None:
                        try:
                            self._pub_queue.put_nowait((topic, payload, qos, retry_count))
                        except Exception as qe:
                            # FIXED-P2: 重入队列失败时写入离线队列，避免消息丢失
                            self._enqueue_offline(topic, payload, qos)
                            logger.error("Custom MQTT re-enqueue failed, enqueued offline: %s", qe)
        except asyncio.CancelledError:
            logger.debug("Custom MQTT publish loop cancelled")  # FIXED-P3: 中文日志→英文

    async def _rpc_listen_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    parts = topic.split("/")
                    if len(parts) >= 4 and parts[-1] == "request":
                        device_id = parts[-3] if len(parts) >= 4 else "unknown"
                        # FIXED: 原问题-json.loads无JSONDecodeError专项捕获，恶意MQTT消息导致RPC监听循环中断
                        try:
                            payload = json.loads(message.payload.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError) as e:
                            logger.warning("CustomMQTT invalid RPC payload: %s", e)
                            continue
                        method = payload.get("method", "")
                        params = payload.get("params", {})

                        if self._rpc_callback:
                            result = await self._rpc_callback(device_id, method, params)
                            response_topic = f"{self._topic_prefix}/{device_id}/rpc/response"
                            response_data = json.dumps({"method": method, "result": result})
                            await client.publish(
                                response_topic,
                                response_data.encode("utf-8"),
                                qos=1,
                            )
                except Exception as e:
                    logger.error("Custom MQTT RPC handler error: %s", e)  # FIXED-P3: 中文日志→英文
        except asyncio.CancelledError:
            logger.debug("Custom MQTT RPC listen loop cancelled")  # FIXED-P3: 中文日志→英文

    def _render_topic(self, suffix: str, device_id: str) -> str:
        """FIXED-P2: 渲染topic模板，支持 {prefix}/{device_id}/{suffix} 和自定义模板"""
        if self._topic_template:
            try:
                return self._topic_template.format(
                    prefix=self._topic_prefix,
                    device_id=device_id,
                    suffix=suffix,
                )
            except (KeyError, IndexError):
                pass
        return f"{self._topic_prefix}/{device_id}/{suffix}"

    def _render_payload(self, data: dict[str, Any], device_id: str) -> str:
        """FIXED-P2: 渲染payload模板，支持 {device_id} 占位符和自定义JSON模板"""
        if self._payload_template:
            try:
                rendered = self._payload_template.format(
                    device_id=device_id,
                    data=json.dumps(data, ensure_ascii=False, default=str),
                )
                return rendered
            except (KeyError, IndexError):
                pass
        return json.dumps(data, ensure_ascii=False, default=str)
