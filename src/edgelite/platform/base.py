"""北向平台对接抽象基类"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from typing import Any

from edgelite.constants import _PLATFORM_RECONNECT_MAX_BACKOFF

logger = logging.getLogger(__name__)


class PlatformHandler(ABC):
    """北向IoT平台对接抽象基类

    所有平台对接实现必须继承此类并实现所有抽象方法。
    平台对接通过MQTT协议与云端平台通信，支持：
    - 遥测数据上报
    - 属性上传
    - RPC反向控制（从平台下发指令到设备）
    - 设备上下线通知
    """

    platform_name: str = ""
    platform_version: str = "1.0.0"
    config_schema: dict = {}

    def __init__(self):
        self._connected = False
        self._offline_queue: deque[tuple[str, bytes, int]] = deque()  # FIXED-P2: 离线数据缓存队列，断线时缓存待上报数据
        self._offline_queue_max: int = 10000  # 离线队列最大条目数
        self._reconnect_backoff: float = 1.0  # FIXED-P2: 重连退避基数秒数

    @abstractmethod
    async def connect(self, config: dict) -> None:
        """连接到平台MQTT Broker

        Args:
            config: 平台连接配置，包含broker/port/username/password等
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """断开平台连接"""

    @abstractmethod
    async def publish_telemetry(self, device_id: str, data: dict[str, Any]) -> None:
        """上报设备遥测数据

        Args:
            device_id: 设备ID
            data: 遥测数据键值对，如 {"temperature": 25.6, "humidity": 60}
        """

    @abstractmethod
    async def publish_attributes(self, device_id: str, attrs: dict[str, Any]) -> None:
        """上传设备属性

        Args:
            device_id: 设备ID
            attrs: 属性键值对，如 {"model": "S7-1200", "location": "workshop-1"}
        """

    @abstractmethod
    async def on_rpc_request(self, callback: Callable) -> None:
        """注册RPC请求回调

        当平台下发RPC请求时，调用callback处理

        Args:
            callback: RPC请求处理函数，
                签名为 async def callback(device_id, method, params) -> result
        """

    @abstractmethod
    async def publish_device_status(self, device_id: str, online: bool) -> None:
        """上报设备上下线状态

        Args:
            device_id: 设备ID
            online: True=上线，False=下线
        """

    @property
    def is_connected(self) -> bool:
        """平台是否已连接"""
        return self._connected

    async def reconnect_with_backoff(self, config: dict) -> None:
        """FIXED-P2: 带指数退避的重连方法

        断线后按指数退避策略重连，避免频繁重连导致 broker 压力。
        各适配器在 _connect_loop 中断线时应调用此方法。
        """
        backoff = self._reconnect_backoff
        max_backoff = _PLATFORM_RECONNECT_MAX_BACKOFF
        while not self._connected:
            try:
                logger.info("Platform %s reconnecting (backoff=%.1fs)...", self.platform_name, backoff)
                await self.connect(config)
                if self._connected:
                    self._reconnect_backoff = 1.0  # 重连成功后重置退避
                    try:
                        await self._flush_offline_queue()
                    except Exception as flush_err:
                        # FIXED-P2: 原问题-_flush_offline_queue 异常被外层 except 静默吞没
                        logger.warning("Platform %s offline queue flush failed: %s", self.platform_name, flush_err)
                    return
            except Exception as e:
                logger.error("Platform %s reconnect failed: %s", self.platform_name, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            backoff *= 0.5 + random.random() * 0.5  # FIXED-P2: 原问题-重连退避无抖动

    def _enqueue_offline(self, topic: str, payload: bytes, qos: int = 1) -> None:
        """FIXED-P2: 离线时缓存待上报数据

        Args:
            topic: MQTT topic
            payload: 消息负载
            qos: QoS级别
        """
        if len(self._offline_queue) >= self._offline_queue_max:
            self._offline_queue.popleft()  # 丢弃最旧的
            logger.debug("Offline queue full, dropped oldest entry for platform %s", self.platform_name)
        self._offline_queue.append((topic, payload, qos))

    async def _flush_offline_queue(self) -> None:
        """FIXED-P2: 重连成功后刷出离线缓存队列"""
        if not self._offline_queue:
            return
        logger.info("Flushing %d offline queued messages for platform %s", len(self._offline_queue), self.platform_name)
        queue = list(self._offline_queue)
        self._offline_queue.clear()
        flushed = 0
        for topic, payload, qos in queue:
            try:
                if hasattr(self, '_pub_queue') and self._pub_queue:
                    self._pub_queue.put_nowait((topic, payload, qos))
                    flushed += 1
                else:
                    logger.debug("No pub_queue available, skipping offline flush entry")
            except asyncio.QueueFull:
                # FIXED-P1: 原问题-QueueFull 时 break 导致剩余条目永久丢失
                # 改为将未刷出的条目重新放回离线队列，下次重连时重试
                remaining = queue[flushed:]
                for item in remaining:
                    self._enqueue_offline(item[0], item[1], item[2])
                logger.warning(
                    "Pub queue full during offline flush, re-queued %d entries for next reconnect",
                    len(remaining),
                )
                break
