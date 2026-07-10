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
import contextlib
import json
import logging
import random
import uuid
from collections.abc import Callable
from typing import Any

from edgelite.constants import (  # FIXED-P0: 补充_QUEUE_POLL_TIMEOUT导入
    _MQTT_KEEPALIVE,
    _MQTT_QUEUE_MAXSIZE,
    _MQTT_RECONNECT_DELAY,
    _PLATFORM_RECONNECT_MAX_BACKOFF,
    _QUEUE_POLL_TIMEOUT,
)
from edgelite.platform.base import PlatformHandler
from edgelite.utils import timestamp_ms  # FIXED: 原问题-缺失导入导致NameError

logger = logging.getLogger(__name__)


class ThingsPanelHandler(PlatformHandler):
    """ThingsPanel平台对接实现"""

    platform_name = "thingspanel"
    platform_version = "1.0.0"

    def __init__(self):
        super().__init__()  # FIXED-P2: 调用基类__init__，初始化离线队列和重连退避
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
        device_token = config.get("device_token", "")

        self._connect_task = asyncio.create_task(
            self._connect_loop(broker, port, username, password, device_token),
            name="thingspanel-connect",
        )
        logger.info("ThingsPanel platform started: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

    async def disconnect(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        if self._client:
            try:
                self._client.disconnect()
            except Exception as e:
                logger.debug("ThingsPanel disconnect failed: %s", e)  # FIXED-P3: 中文日志→英文
        self._connected = False
        logger.info("ThingsPanel platform disconnected")  # FIXED-P3: 中文日志→英文

    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        topic = "v1/gateway/telemetry"
        payload = {
            "ts": timestamp_ms(),
            "values": data,
        }
        payload_bytes = json.dumps({device_id: [payload]}, ensure_ascii=False).encode("utf-8")
        # FIXED-P0: 断线时不再直接return丢弃数据，改为缓存到离线队列，重连后由_flush_offline_queue刷出
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload_bytes, 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload_bytes, 1))
        except asyncio.QueueFull:
            # FIXED-P0: 队列满时同样缓存到离线队列，避免数据丢失
            self._enqueue_offline(topic, payload_bytes, 1)
            logger.warning("ThingsPanel pub queue full, enqueued offline")  # FIXED-P2: 原问题-中文日志

    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        topic = "v1/gateway/attributes"
        payload_bytes = json.dumps({device_id: attrs}, ensure_ascii=False).encode("utf-8")
        # FIXED-P0: 断线时不再直接return丢弃数据，改为缓存到离线队列，重连后由_flush_offline_queue刷出
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload_bytes, 1)
            return
        # FIXED-P1: 原问题-使用 await self._pub_queue.put() 会无限阻塞，队列满时导致整个发布协程卡死
        # 改为 put_nowait + QueueFull 降级，与其他发布方法保持一致
        try:
            self._pub_queue.put_nowait((topic, payload_bytes, 1))
        except asyncio.QueueFull:
            # FIXED-P0: 队列满时同样缓存到离线队列，避免数据丢失
            self._enqueue_offline(topic, payload_bytes, 1)
            logger.warning("ThingsPanel pub queue full, enqueued offline")  # FIXED-P2: 原问题-中文日志与其他模块不一致

    async def on_rpc_request(self, callback: Callable) -> None:
        self._rpc_callback = callback

    async def publish_device_status(self, device_id: str, online: bool) -> None:
        topic = "v1/gateway/connect" if online else "v1/gateway/disconnect"
        payload_bytes = json.dumps({"device": device_id}, ensure_ascii=False).encode("utf-8")
        # FIXED-P0: 断线时不再直接return丢弃数据，改为缓存到离线队列，重连后由_flush_offline_queue刷出
        if not self._connected or not self._pub_queue:
            self._enqueue_offline(topic, payload_bytes, 1)
            return
        try:
            self._pub_queue.put_nowait((topic, payload_bytes, 1))
        except asyncio.QueueFull:
            # FIXED-P0: 队列满时同样缓存到离线队列，避免数据丢失
            self._enqueue_offline(topic, payload_bytes, 1)
            logger.warning("ThingsPanel pub queue full, enqueued offline")  # FIXED-P2: 原问题-中文日志

    async def _connect_loop(self, broker: str, port: int, username: str, password: str, device_token: str) -> None:
        import aiomqtt

        backoff = 1
        max_backoff = _PLATFORM_RECONNECT_MAX_BACKOFF  # FIXED-P2: 原问题-硬编码 max_backoff=60，改为使用常量

        while self._running:
            try:
                async with aiomqtt.Client(
                    hostname=broker,
                    port=port,
                    username=username or None,
                    password=password or None,
                    keepalive=_MQTT_KEEPALIVE,
                    identifier=f"edgelite-thingspanel-{uuid.uuid4().hex[:8]}",
                ) as client:
                    self._client = client
                    self._connected = True
                    backoff = 1
                    logger.info("ThingsPanel connected: %s:%d", broker, port)  # FIXED-P3: 中文日志→英文

                    await client.subscribe("v1/devices/me/rpc/request/+")
                    await client.subscribe("v1/gateway/rpc/request/+")

                    # R7-S-06 修复(严重): 重连成功后刷新离线队列，将断连期间缓存的数据投递到 _pub_queue 待发送
                    # 配合 _publish_loop 中 R7-S-03 的离线缓存，确保断连期间数据不丢
                    await self._flush_offline_queue()

                    # R6-S-03 修复(严重): 原 gather 无 return_exceptions，_publish_loop 抛异常
                    # 会取消 _rpc_loop，导致正在处理的 RPC 请求被中断。
                    results = await asyncio.gather(
                        self._rpc_loop(client),
                        self._publish_loop(client),
                        return_exceptions=True,
                    )
                    for r in results:
                        if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                            logger.error("ThingsPanel loop error: %s", r)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.error("ThingsPanel connection error: %s, retrying in %ds", e, backoff)  # FIXED-P3: 中文日志→英文
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                backoff *= 0.5 + random.random() * 0.5  # FIXED-P4: 原问题-重连退避无抖动

        self._connected = False

    async def _rpc_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                try:
                    topic = str(message.topic)
                    request_id = topic.split("/")[-1]
                    # FIXED-P1: 原问题-RPC响应始终使用 v1/devices/me/rpc/response/，
                    # 但网关RPC请求(v1/gateway/rpc/request/)应使用 v1/gateway/rpc/response/
                    is_gateway_rpc = topic.startswith("v1/gateway/rpc/request/")
                    if is_gateway_rpc:
                        response_topic = f"v1/gateway/rpc/response/{request_id}"
                    else:
                        response_topic = f"v1/devices/me/rpc/response/{request_id}"
                    # FIXED: 原问题-MQTT消息JSON解析无异常保护
                    try:
                        payload = json.loads(message.payload.decode("utf-8"))
                    except json.JSONDecodeError as e:
                        logger.warning("ThingsPanel MQTT JSON parse failed: %s", e)  # FIXED-P3: 中文日志→英文
                        continue
                    method = payload.get("method", "")
                    params = payload.get("params", {})

                    if self._rpc_callback:
                        result = await self._rpc_callback("gateway", method, params)
                        response_payload = json.dumps(
                            {"result": result} if result is not None else {}, ensure_ascii=False
                        )
                        await client.publish(response_topic, response_payload)
                        logger.debug(
                            "ThingsPanel RPC response: %s -> %s", method, request_id
                        )  # FIXED-P3: 中文日志→英文
                except Exception as e:
                    logger.error("ThingsPanel RPC handler error: %s", e)  # FIXED-P3: 中文日志→英文
        except asyncio.CancelledError:
            pass

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    msg = await asyncio.wait_for(
                        self._pub_queue.get(), timeout=_QUEUE_POLL_TIMEOUT
                    )  # FIXED: 原问题-timeout=1.0魔法数字
                except TimeoutError:
                    continue
                # R7-S-04 修复(严重): 兼容直接发布的 dict 与离线队列刷新的 tuple (topic, payload, qos) 两种格式
                if isinstance(msg, tuple):
                    topic, payload, _q = msg
                else:
                    topic = msg["topic"]
                    payload = msg["payload"]
                    # FIXED-P1: dict 格式也提取 qos，避免下方回退时 _q 未定义；缺省为 1
                    _q = msg.get("qos", 1)
                try:
                    await client.publish(topic, payload)
                except Exception as e:
                    logger.error("ThingsPanel publish failed: %s", e)  # FIXED-P3: 中文日志→英文
                    # R7-S-03 修复(严重): publish 失败时将数据缓存到离线队列而非丢弃/重入 pub_queue
                    # 原问题: 断连时 publish 失败，数据仅重入 _pub_queue 且 QueueFull 时被静默丢弃；
                    #         修复为写入基类 _offline_queue，重连后由 _flush_offline_queue 刷出，保证不丢
                    payload_bytes = payload.encode("utf-8") if isinstance(payload, str) else payload
                    # FIXED-P1: 原问题-回退时硬编码 qos=1 丢失元组中原始 _q；修复-使用解析出的 _q 保留原始 QoS
                    self._enqueue_offline(topic, payload_bytes, _q)
                    raise
        # R7-S-05 修复(严重): CancelledError 应 raise 而非 pass/break，让上层 gather(return_exceptions=True)
        # 能感知任务被取消，避免 disconnect 时取消信号被吞没导致任务无法正确退出
        except asyncio.CancelledError:
            raise
