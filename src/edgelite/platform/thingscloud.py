"""ThingsCloud平台对接 - 通过MQTT协议对接ThingsCloud物联网平台

ThingsCloud MQTT Topic体系:
- things/{device_id}/properties/report  - 属性上报
- things/{device_id}/command/receive    - 命令下发
- things/{device_id}/event/report       - 事件上报
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random  # R7-S-01 修复(严重): 指数退避重连需要随机抖动，避免多客户端同步重连(thundering herd)
import time
from collections.abc import Callable
from typing import Any

from edgelite.constants import (  # FIXED-P0: 补充_QUEUE_POLL_TIMEOUT导入
    _MQTT_KEEPALIVE,
    _MQTT_QUEUE_MAXSIZE,
    _PLATFORM_RECONNECT_MAX_BACKOFF,  # R7-S-01 修复(严重): 指数退避上限常量(60s)
    _QUEUE_POLL_TIMEOUT,
)
from edgelite.platform.base import PlatformHandler
from edgelite.utils import timestamp_ms

logger = logging.getLogger(__name__)


class ThingsCloudHandler(PlatformHandler):
    """ThingsCloud平台对接实现"""

    platform_name = "thingscloud"
    platform_version = "1.0.0"

    def __init__(self):
        super().__init__()  # FIXED-P2: 调用基类__init__，初始化离线队列和重连退避
        self._config: dict = {}
        self._rpc_callback: Callable | None = None
        self._connect_task: asyncio.Task | None = None
        self._running = False
        self._pub_queue: asyncio.Queue | None = None

    async def connect(self, config: dict) -> None:
        try:
            import aiomqtt
        except ImportError:
            raise ImportError("aiomqtt not installed, run: pip install aiomqtt") from None

        self._config = config
        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)  # FIXED: 原问题-maxsize=1000魔法数字

        broker = config.get("broker", config.get("host", ""))
        if not broker:
            raise ValueError("broker is required")
        port = int(config.get("port", 1883))
        username = config.get("username", config.get("access_key", ""))
        password = config.get("password", config.get("access_secret", ""))

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password),
            name="thingscloud-connect",
        )
        logger.info("ThingsCloud platform started: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._connected = False
        logger.info("ThingsCloud platform disconnected")  # FIXED-P3: 中文日志→英文

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        if not self._pub_queue:
            return
        topic = f"things/{device_id}/properties/report"
        payload = json.dumps(
            {
                "properties": data,
                # FIXED: 原问题-int(time.time()*1000)重复模式，改为timestamp_ms()
                "timestamp": timestamp_ms(),
            },
            ensure_ascii=False,
            default=str,
        )
        payload_bytes = payload.encode("utf-8")
        if not self._connected:
            # FIXED-P1: 原问题-断线时直接 return 丢弃数据；改为缓存到离线队列，重连后刷出
            self._enqueue_offline(topic, payload_bytes, 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload_bytes, 1))
        except asyncio.QueueFull:
            logger.warning("ThingsCloud pub queue full, dropping message")  # FIXED-P3: 中文日志→英文

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        if not self._pub_queue:
            return
        topic = f"things/{device_id}/attributes/report"
        payload = json.dumps(attrs, ensure_ascii=False, default=str)
        payload_bytes = payload.encode("utf-8")
        if not self._connected:
            # FIXED-P1: 原问题-断线时直接 return 丢弃数据；改为缓存到离线队列
            self._enqueue_offline(topic, payload_bytes, 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload_bytes, 1))
        except asyncio.QueueFull:
            logger.warning("ThingsCloud pub queue full, dropping message")  # FIXED-P3: 中文日志→英文

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        if not self._pub_queue:
            return
        topic = f"things/{device_id}/event/report"
        payload = json.dumps(
            {
                "event": "device_status",
                "data": {"online": online},
                # FIXED: 原问题-int(time.time()*1000)重复模式，改为timestamp_ms()
                "timestamp": timestamp_ms(),
            },
            ensure_ascii=False,
            default=str,
        )
        payload_bytes = payload.encode("utf-8")
        if not self._connected:
            # FIXED-P1: 原问题-断线时直接 return 丢弃数据；改为缓存到离线队列
            self._enqueue_offline(topic, payload_bytes, 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload_bytes, 1))
        except asyncio.QueueFull:
            logger.warning("ThingsCloud pub queue full, dropping message")  # FIXED-P3: 中文日志→英文

    async def _connect_loop(self, broker: str, port: int, username: str, password: str) -> None:
        import aiomqtt

        # R7-S-01 修复(严重): 重连使用指数退避(1s,2s,4s...上限60s)+随机抖动，替代固定延迟 _MQTT_RECONNECT_DELAY
        # 原问题: 断线后固定等待 _MQTT_RECONNECT_DELAY(5s) 重连，broker 持续故障时高频重连加剧服务端压力
        # 修复方案: 复用基类 PlatformHandler.reconnect_with_backoff 的退避策略——_reconnect_backoff 基数
        #           + _PLATFORM_RECONNECT_MAX_BACKOFF(60s) 上限 + 随机抖动，与 thingspanel 实现保持一致
        # 注: 因 connect() 创建后台循环任务，无法直接调用 reconnect_with_backoff，故复用其退避参数与公式
        backoff = self._reconnect_backoff
        max_backoff = _PLATFORM_RECONNECT_MAX_BACKOFF

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
                    # R7-S-01: 连接成功后重置退避基数(局部变量与基类字段同步)
                    backoff = 1
                    self._reconnect_backoff = 1.0
                    logger.info("ThingsCloud MQTT connected: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

                    await client.subscribe("things/+/command/receive", qos=1)

                    # R7-S-02 修复(严重): 重连成功后刷新离线队列，避免断连期间缓存数据丢失
                    # 原问题: 断线时 publish_telemetry 等通过 _enqueue_offline 缓存到 _offline_queue，
                    #         但重连后未调用 _flush_offline_queue，缓存数据永久滞留无法投递
                    await self._flush_offline_queue()

                    msg_task = asyncio.create_task(
                        self._message_listen_loop(client),
                        name="thingscloud-msg-listen",
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client),
                        name="thingscloud-publish",
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
                self._connected = False
                logger.error(
                    "ThingsCloud MQTT connection error: %s, retrying in %.1fs", e, backoff
                )  # FIXED-P3: 中文日志→英文
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                backoff *= 0.5 + random.random() * 0.5  # R7-S-01: 随机抖动，避免重连风暴

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    topic, payload, qos = await asyncio.wait_for(
                        self._pub_queue.get(), timeout=_QUEUE_POLL_TIMEOUT
                    )  # FIXED: 原问题-timeout=1.0魔法数字
                except TimeoutError:
                    continue
                try:
                    await client.publish(topic, payload, qos=qos)
                except Exception as e:
                    logger.error("ThingsCloud MQTT publish failed: %s", e)  # FIXED-P3: 中文日志→英文
        except asyncio.CancelledError:
            pass

    async def _message_listen_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    # FIXED: 原问题-json.loads无JSONDecodeError专项捕获，恶意MQTT消息导致处理中断
                    try:
                        payload = json.loads(message.payload.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError) as e:
                        logger.warning("ThingsCloud invalid message payload: %s", e)
                        continue

                    if "/command/receive" in topic:
                        parts = topic.split("/")
                        device_id = parts[1] if len(parts) >= 2 else ""
                        method = payload.get("command", payload.get("method", ""))
                        params = payload.get("params", payload.get("data", {}))

                        if self._rpc_callback:
                            await self._rpc_callback(device_id, method, params)

                except Exception as e:
                    logger.error("ThingsCloud message handler error: %s", e)  # FIXED-P3: 中文日志→英文
        except asyncio.CancelledError:
            pass
