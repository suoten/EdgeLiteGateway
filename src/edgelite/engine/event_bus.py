"""事件总线 - 基于asyncio.Queue的进程内事件总线"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_QUEUE_SIZE = 10000


# ─── 事件定义 ───


@dataclass
class Event:
    """事件基类"""

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class PointUpdateEvent(Event):
    """测点值更新事件"""

    device_id: str = ""
    point_name: str = ""
    value: float = 0.0
    quality: str = "good"


@dataclass
class DeviceStatusEvent(Event):
    """设备状态变更事件"""

    device_id: str = ""
    old_status: str = ""
    new_status: str = ""


@dataclass
class AlarmEvent(Event):
    """告警事件"""

    alarm_id: str = ""
    rule_id: str = ""
    device_id: str = ""
    severity: str = ""
    action: str = "firing"  # firing / recovered
    trigger_value: dict = field(default_factory=dict)


# ─── 事件总线 ───


class EventBus:
    """进程内事件总线，解耦各模块"""

    def __init__(self, max_queue_size: int = MAX_QUEUE_SIZE):
        self._max_size = max_queue_size
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._handlers: dict[str, list[Callable]] = {}

    def subscribe(self, name: str) -> asyncio.Queue:
        """订阅事件，返回独立队列"""
        queue = asyncio.Queue(maxsize=self._max_size)
        self._subscribers[name] = queue
        logger.info("事件总线订阅者注册: %s", name)
        return queue

    def register_handler(self, event_type: str, handler: Callable) -> None:
        """注册事件处理器"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    async def publish(self, event: Event) -> None:
        """发布事件到所有订阅者"""
        for name, queue in self._subscribers.items():
            try:
                # 非阻塞放入，队列满时丢弃最旧事件
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 丢弃最旧事件
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(event)
                logger.debug("事件队列满，丢弃最旧事件: subscriber=%s", name)

        # 调用注册的处理器
        event_type = type(event).__name__
        handlers = self._handlers.get(event_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error("事件处理器执行失败: %s - %s", event_type, e)

    async def start_handler_loop(self, subscriber_name: str, handler: Callable) -> None:
        """启动订阅者处理循环"""
        queue = self._subscribers.get(subscriber_name)
        if queue is None:
            logger.error("订阅者不存在: %s", subscriber_name)
            return

        logger.info("启动事件处理循环: %s", subscriber_name)
        while True:
            try:
                event = await queue.get()
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except asyncio.CancelledError:
                logger.info("事件处理循环取消: %s", subscriber_name)
                break
            except Exception as e:
                logger.error("事件处理循环异常: %s - %s", subscriber_name, e)
