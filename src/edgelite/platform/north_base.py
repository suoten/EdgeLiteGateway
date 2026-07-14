"""北向平台适配器抽象基类

BaseNorthAdapter 是北向平台对接的抽象基类，定义了适配器的生命周期管理
（启动/停止/连接检查）和数据查询接口（消息预览/Broker质量/设备列表/仪表盘数据）。

与 PlatformHandler 的区别：
- PlatformHandler 面向 MQTT 协议的同步/异步发布，侧重数据上报。
- BaseNorthAdapter 面向完整的北向适配器生命周期管理，支持 QoS、去重、压缩等高级特性。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from edgelite.models.north import NorthConfig

logger = logging.getLogger(__name__)


@dataclass
class AdapterMetrics:
    """适配器运行指标"""

    messages_total: int = 0
    errors_total: int = 0
    dedup_dropped: int = 0
    compressed_total: int = 0


@dataclass
class AdapterQueue:
    """适配器内部消息队列"""

    _items: deque = field(default_factory=lambda: deque(maxlen=10000))

    @property
    def size(self) -> int:
        return len(self._items)

    def put(self, item: Any) -> None:
        self._items.append(item)

    def get(self) -> Any | None:
        return self._items.popleft() if self._items else None

    def peek(self, n: int = 10) -> list[Any]:
        return list(self._items)[:n]


class BaseNorthAdapter(ABC):
    """北向平台适配器抽象基类

    所有北向平台适配器（如 ThingsBoard、ThingsCloud、Huawei IoTDA 等）
    继承此类并实现 start/stop 抽象方法。
    """

    platform_name: str = ""
    platform_version: str = "1.0.0"

    def __init__(self) -> None:
        self._connected: bool = False
        self._state: str = "disconnected"  # disconnected | connecting | connected | error
        self._queue: AdapterQueue = AdapterQueue()
        self._metrics: AdapterMetrics = AdapterMetrics()
        self._last_heartbeat: float = 0.0
        self._config: NorthConfig | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

    @abstractmethod
    async def start(self, config: NorthConfig) -> None:
        """启动适配器并连接到北向平台

        Args:
            config: 北向平台完整配置
        """
        self._config = config
        self._state = "connecting"

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器并断开连接"""
        self._state = "disconnected"
        self._connected = False

    async def is_connected(self) -> bool:
        """检查适配器是否已连接"""
        return self._connected

    def get_message_preview(self) -> list[dict[str, Any]]:
        """获取待发送消息预览（最近10条）"""
        return [
            {"topic": getattr(msg, "topic", ""), "payload_size": len(str(getattr(msg, "payload", "")))}
            for msg in self._queue.peek(10)
        ]

    def get_broker_quality(self) -> dict[str, Any]:
        """获取 Broker 连接质量指标"""
        return {
            "connected": self._connected,
            "state": self._state,
            "last_heartbeat": self._last_heartbeat,
            "messages_total": self._metrics.messages_total,
            "errors_total": self._metrics.errors_total,
            "dedup_dropped": self._metrics.dedup_dropped,
            "compressed_total": self._metrics.compressed_total,
            "queue_size": self._queue.size,
        }

    def get_device_list(self) -> list[dict[str, Any]]:
        """获取已注册设备列表（子类可覆盖）"""
        return []

    async def get_dashboard_data(self) -> dict[str, Any]:
        """获取仪表盘数据（子类可覆盖）"""
        return {
            "platform": self.platform_name,
            "version": self.platform_version,
            "connected": self._connected,
            "state": self._state,
            "queue_size": self._queue.size,
            "metrics": {
                "messages_total": self._metrics.messages_total,
                "errors_total": self._metrics.errors_total,
                "dedup_dropped": self._metrics.dedup_dropped,
                "compressed_total": self._metrics.compressed_total,
            },
            "last_heartbeat": self._last_heartbeat,
        }

    def _update_heartbeat(self) -> None:
        """更新心跳时间戳"""
        self._last_heartbeat = time.time()
