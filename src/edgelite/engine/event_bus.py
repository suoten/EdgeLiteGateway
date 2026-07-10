"""事件总线 - 基于asyncio.Queue的进程内事件总线"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edgelite.constants import _EVENT_BUS_MAX_QUEUE

logger = logging.getLogger(__name__)

MAX_QUEUE_SIZE = _EVENT_BUS_MAX_QUEUE  # FIXED: 原问题-硬编码队列上限，现引用constants.py

# FIXED: 告警事件持久化 outbox 默认路径（进程崩溃兜底）[2026-06-29]
_DEFAULT_ALARM_OUTBOX_PATH = "data/alarm_outbox.db"


# ─── 事件定义 ───


@dataclass
class Event:
    """事件基类

    R7-S-01: 增加 event_id 字段用于事件去重。
    event_id 为空字符串时表示不去重（向后兼容）；
    设置非空值时，EventBus.publish 会基于该值去重，防止同一逻辑事件被重复发布。
    """

    event_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class PointUpdateEvent(Event):
    """测点值更新事件"""

    device_id: str = ""
    point_name: str = ""
    value: float | None = None  # FIXED: 原问题-默认0.0掩盖数据缺失，None表示无值
    quality: str = "unknown"  # FIXED: 原问题-默认"good"掩盖数据缺失，unknown表示未确认


@dataclass
class DeviceStatusEvent(Event):
    """设备状态变更事件"""

    device_id: str = ""
    old_status: str = ""
    new_status: str = ""


@dataclass
class AlarmEvent(Event):
    """告警事件

    R7-S-01: 自动生成 event_id 用于 EventBus 去重，防止同一告警的同一动作被重复发布。
    """

    alarm_id: str = ""
    rule_id: str = ""
    rule_name: str = ""
    device_id: str = ""
    device_name: str = ""
    severity: str = ""
    action: str = "firing"  # firing / recovered
    trigger_value: dict = field(default_factory=dict)
    rule_type: str = ""

    def __post_init__(self):
        # R7-S-01: 未显式设置 event_id 时，基于 alarm_id+action 自动生成，用于去重
        if not self.event_id:
            self.event_id = f"alarm:{self.alarm_id}:{self.action}"


@dataclass
class InfluxDBFallbackEvent(Event):
    """InfluxDB降级/恢复事件"""

    action: str = ""  # "degraded" / "recovered"
    reason: str = ""
    cached_count: int = 0


@dataclass
class MqttForwardEvent(Event):  # FIXED-P0: device_linkage mqtt_publish需要Event子类
    """MQTT转发事件"""

    topic: str = ""
    payload: dict = field(default_factory=dict)


@dataclass
class StreamResultEvent(Event):
    """FIXED-P0: 流计算结果事件 - stream_compute._publish_result 发布专用"""

    result_type: str = ""  # "window" / "pattern"
    window_id: str = ""
    window_type: str = ""
    point_name: str = ""
    aggregate: str = ""
    value: float = 0.0
    count: int = 0
    pattern_id: str = ""
    matched: bool = False
    confidence: float = 0.0
    details: dict = field(default_factory=dict)


# ─── 事件总线 ───


class EventBus:
    """进程内事件总线，解耦各模块"""

    def __init__(self, max_queue_size: int = MAX_QUEUE_SIZE):
        self._max_size = max_queue_size
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._handlers: dict[str, list[Callable]] = {}
        self._subscribers_lock = asyncio.Lock()  # FIXED-P1: _subscribers并发修改保护
        self._handlers_lock = threading.Lock()  # FIXED-P0: _handlers并发修改保护，防止register/unregister与publish竞态
        # FIXED-P1: 限制 publish 失败重试的并发 task 数量，防止无界 task 导致内存溢出
        self._retry_semaphore = asyncio.Semaphore(50)
        # FIXED-P1: 背压机制 - 丢弃计数器，统计因队列满而被丢弃的事件数
        # FIXED-P2: 计数器读-改-写使用 threading.Lock 保护（与 _handlers_lock 一致，
        # 同时兼容 sync get/reset 方法和 async publish 调用；
        # 锁内仅做 int 运算无 await，不阻塞事件循环）
        self._dropped_count: int = 0
        self._dropped_lock = threading.Lock()
        # FIXED-P1: handler 执行超时（秒），防止单个 handler 阻塞整个 publish 流程
        self._handler_timeout: float = 10.0
        # S-01修复: handler_loop 协程的停止事件，unsubscribe 时设置以唤醒阻塞的 queue.get()
        self._handler_stop_events: dict[str, asyncio.Event] = {}
        # S-01修复: handler_loop 协程对应的 Task，便于 shutdown 时统一取消
        self._handler_tasks: dict[str, asyncio.Task] = {}
        # 并发安全: 跟踪重试任务，shutdown 时统一取消，防止 fire-and-forget 任务泄漏
        self._retry_tasks: set[asyncio.Task] = set()
        # 并发安全: shutdown 标志，publish 开头检查，防止 shutdown 与 publish 竞态
        self._shutting_down: bool = False
        # R7-S-01: event_id 去重缓存，使用 OrderedDict 实现 FIFO 淘汰，防止内存无限增长
        self._seen_event_ids: OrderedDict[str, None] = OrderedDict()
        self._max_dedup_size = 10000
        # FIXED: 告警事件持久化 outbox（进程崩溃兜底），None 表示未启用 [2026-06-29]
        self._alarm_outbox: Any = None

    def enable_alarm_persistence(self, db_path: str = _DEFAULT_ALARM_OUTBOX_PATH) -> None:
        """启用告警事件持久化兜底（进程崩溃后重启可重放未投递告警）。

        best-effort: DB 不可用时仅记录日志，不影响 EventBus 主流程。
        应在应用启动早期（bootstrap）调用，随后调用 replay_pending_alarms() 重放历史。
        """
        from edgelite.engine.alarm_outbox import AlarmOutbox

        self._alarm_outbox = AlarmOutbox(db_path)
        if self._alarm_outbox._conn is not None:
            logger.info("Alarm outbox persistence enabled: %s", db_path)

    async def replay_pending_alarms(self) -> int:
        """重启后重放 outbox 中未投递的告警事件。

        Returns: 重放的告警数量
        """
        if self._alarm_outbox is None:
            return 0
        loop = asyncio.get_running_loop()

        def _sync_publish(event: Any) -> None:
            # 在 executor 线程中同步调用异步 publish: 创建任务并等待
            try:
                future = asyncio.run_coroutine_threadsafe(self.publish(event), loop)
                future.result(timeout=2.0)
            except Exception as e:
                logger.warning("AlarmOutbox replay publish failed: %s", e)

        return await loop.run_in_executor(None, self._alarm_outbox.replay_and_clear, _sync_publish)

    async def subscribe(self, name: str) -> asyncio.Queue:
        """订阅事件，返回独立队列"""
        async with self._subscribers_lock:  # FIXED-P1: subscribe与publish并发保护
            # FIXED-BugR4X: 原问题-重名订阅静默覆盖导致前一个订阅者队列丢失且事件丢失；修复-重名订阅时抛出ValueError
            if name in self._subscribers:
                raise ValueError(f"Event bus subscriber name already exists: {name}")
            queue = asyncio.Queue(maxsize=self._max_size)
            self._subscribers[name] = queue
        logger.info("Event bus subscriber registered: %s", name)
        return queue

    async def unsubscribe(self, name: str) -> bool:
        """FIXED-P0: 取消订阅，移除订阅者队列

        Args:
            name: 订阅者名称

        Returns:
            是否成功移除（False 表示该名称不存在）
        """
        async with self._subscribers_lock:
            if name in self._subscribers:
                del self._subscribers[name]
                logger.info("Event bus subscriber removed: %s", name)
                # S-01修复: 设置停止事件，唤醒可能阻塞在 queue.get() 上的 handler_loop 协程
                stop_event = self._handler_stop_events.get(name)
                if stop_event is not None:
                    stop_event.set()
                return True
            return False

    def register_handler(self, event_type: str, handler: Callable) -> None:
        """注册事件处理器"""
        with self._handlers_lock:  # FIXED-P0: 加锁防止与publish并发修改导致竞态
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)

    def unregister_handler(self, event_type: str, handler: Callable) -> None:
        """注销事件处理器"""
        with self._handlers_lock:  # FIXED-P0: 加锁防止与publish并发修改导致竞态
            if event_type in self._handlers:
                self._handlers[event_type] = [h for h in self._handlers[event_type] if h is not handler]
                if not self._handlers[event_type]:
                    del self._handlers[event_type]

    def unregister_all(self) -> None:
        """注销所有事件处理器"""
        with self._handlers_lock:  # FIXED-P0: 加锁保护
            self._handlers.clear()

    def get_dropped_count(self) -> int:
        """FIXED-P1: 获取因队列满被丢弃的事件总数（背压监控指标）"""
        # FIXED-P2: 读操作也走锁保护，避免与 publish 中的 += 1 竞态
        with self._dropped_lock:
            return self._dropped_count

    def reset_dropped_count(self) -> int:
        """FIXED-P1: 重置丢弃计数器并返回重置前的值"""
        # FIXED-P2: 读-改-写在锁内完成，防止并发 publish 同时 += 1 导致计数丢失
        with self._dropped_lock:
            old = self._dropped_count
            self._dropped_count = 0
            return old

    async def publish(self, event: Event) -> None:
        """发布事件到所有订阅者"""
        # 并发安全: shutdown 期间拒绝发布，防止与 shutdown 竞态导致事件丢失或异常
        if self._shutting_down:
            logger.warning("Event bus is shutting down, event dropped: type=%s", type(event).__name__)
            return
        # R7-S-01: event_id 去重，防止同一逻辑事件被重复发布到订阅者
        # event_id 为空时不去重（向后兼容），仅对显式设置 event_id 的事件生效
        event_id = getattr(event, "event_id", "")
        if event_id:
            if event_id in self._seen_event_ids:
                logger.debug("Event deduplicated: id=%s type=%s", event_id, type(event).__name__)
                return
            self._seen_event_ids[event_id] = None
            if len(self._seen_event_ids) > self._max_dedup_size:
                self._seen_event_ids.popitem(last=False)  # FIFO 淘汰最旧条目
        # FIXED-P0: AlarmEvent优先保留，队列满时优先丢弃非告警事件
        is_alarm = isinstance(event, AlarmEvent)
        # FIXED-P0 (并发安全#4): AlarmEvent 持久化兜底 — 先落盘后投递，维护 outbox 一致性
        # 原问题: persist (best-effort) 吞掉异常/超时后仍继续投递，进程崩溃后 outbox 中无此事件
        #         但已投递给订阅者，重启重放时遗漏 (outbox 无记录) 或重复 (outbox 有记录但已投递过)。
        # 修复: persist 返回 False / 超时 / 抛异常 → 不投递 (return)，
        #       确保已投递的事件一定在 outbox 中有记录 (可通过 replay 重放)。
        if is_alarm and self._alarm_outbox is not None:
            persisted = False
            try:
                persisted = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, self._alarm_outbox.persist, event),
                    timeout=0.5,
                )
            except TimeoutError:
                logger.warning("Alarm outbox persist timed out, event not delivered to maintain outbox consistency")
                return
            except Exception as e:
                logger.warning("Alarm outbox persist failed, event not delivered to maintain outbox consistency: %s", e)
                return
            if not persisted:
                logger.warning(
                    "Alarm outbox persist returned False, event not delivered to maintain outbox consistency"
                )
                return
        # FIXED-P1: 整个发布过程在锁内进行，防止subscribe/disconnect并发修改
        async with self._subscribers_lock:
            subscriber_items = list(self._subscribers.items())
        for name, queue in subscriber_items:
            if is_alarm:
                # S-02修复: AlarmEvent 为关键告警事件，使用阻塞式 put 确保零丢失
                # 并发安全: 加入 1s 超时，防止队列满且消费者卡死时卡死采集调度循环
                try:
                    await asyncio.wait_for(queue.put(event), timeout=1.0)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:  # FIXED-UP041: asyncio.TimeoutError → TimeoutError
                    # 超时后降级为 put_nowait 尝试非阻塞写入（可能丢弃）
                    logger.critical(
                        "Alarm event enqueue timed out after 1s, subscriber=%s type=%s — degrading to put_nowait",
                        name,
                        type(event).__name__,
                    )
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        logger.critical(
                            "Alarm event DROPPED (queue full after timeout): subscriber=%s type=%s",
                            name,
                            type(event).__name__,
                        )
                except Exception as e:
                    logger.error(
                        "Failed to enqueue alarm event: subscriber=%s type=%s error=%s",
                        name,
                        type(event).__name__,
                        e,
                    )
            else:
                # S-02修复: 非关键事件（如 metrics、status）队列满时丢弃并计数
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                    # BUG-006: 非告警事件队列满时put_nowait可能再次失败，增加二次保护
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        # S-02修复: 非关键事件丢弃，递增丢弃计数器，记录 WARNING 日志便于监控
                        # FIXED-P2: 计数器读-改-写在锁内完成，防止并发 publish 丢计数
                        with self._dropped_lock:
                            self._dropped_count += 1
                            dropped_total = self._dropped_count
                        logger.warning(
                            "Event queue full, non-critical event dropped: subscriber=%s type=%s dropped_total=%d",
                            name,
                            type(event).__name__,
                            dropped_total,
                        )

        # 调用注册的处理器
        event_type = type(event).__name__
        with self._handlers_lock:  # FIXED-P0: 加锁创建快照，防止与register/unregister并发修改竞态
            handlers = list(self._handlers.get(event_type, []))
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    # FIXED-P1: handler 执行加 asyncio.wait_for(timeout=10s)，防止单个 handler 阻塞 publish
                    await asyncio.wait_for(handler(event), timeout=self._handler_timeout)
                else:
                    handler(event)
            except TimeoutError:  # FIXED-UP041: asyncio.TimeoutError → TimeoutError
                # FIXED-P1: handler 超时记录 warning，不重试（超时通常意味着 handler 死锁或卡死）
                logger.warning(
                    "Event handler timed out after %.1fs: %s - %s",
                    self._handler_timeout,
                    event_type,
                    getattr(handler, "__name__", repr(handler)),
                )
            except Exception as e:
                logger.error("Event handler execution failed: %s - %s", event_type, e)

                # FIXED-P2: 原问题-handler失败后sleep(0.1)重试阻塞publish流程，改为fire-and-forget后台重试
                # FIXED-P2: 原问题-重试task无done_callback，异常被吞没且可能task泄漏
                # FIXED-P1: 使用 Semaphore(50) 限制并发重试 task 数量，防止无界 task 导致内存溢出
                async def _retry_handler(_handler=handler, _event=event, _et=event_type):
                    async with self._retry_semaphore:
                        try:
                            await asyncio.sleep(0.1)
                            if asyncio.iscoroutinefunction(_handler):
                                await _handler(_event)
                            else:
                                _handler(_event)
                        except Exception as retry_e:
                            logger.error(
                                "Event handler retry also failed: %s - %s",
                                _et,
                                retry_e,
                            )

                retry_task = asyncio.create_task(_retry_handler(), name=f"event_retry_{event_type}")
                # 并发安全: 跟踪重试任务，shutdown 时统一取消，防止 fire-and-forget 任务泄漏
                self._retry_tasks.add(retry_task)

                # BUG-005: 重试Task异常记录日志，而非静默吞没
                def _on_retry_done(t: asyncio.Task, _tasks=self._retry_tasks):
                    # 并发安全: 任务完成后从跟踪集合移除
                    _tasks.discard(t)
                    if t.cancelled():
                        return
                    exc = t.exception()
                    if exc:
                        logger.error("Event handler retry task failed: %s", exc)

                retry_task.add_done_callback(_on_retry_done)

    async def start_handler_loop(self, subscriber_name: str, handler: Callable) -> None:
        """启动订阅者处理循环

        S-01修复: 为每个 handler_loop 关联停止事件（asyncio.Event），unsubscribe 时
        设置停止事件以唤醒阻塞在 queue.get() 上的协程，避免协程永久阻塞泄漏。
        协程退出时清理 _handler_stop_events 和 _handler_tasks 中的自身引用。
        """
        queue = self._subscribers.get(subscriber_name)
        if queue is None:
            logger.error("Subscriber not found: %s", subscriber_name)  # FIXED-P3: 中文日志→英文
            return

        # S-01修复: 创建停止事件并注册到 _handler_stop_events，便于 unsubscribe 时唤醒
        stop_event = asyncio.Event()
        self._handler_stop_events[subscriber_name] = stop_event
        # S-01修复: 跟踪当前协程对应的 Task，便于 shutdown 时统一取消
        current_task = asyncio.current_task()
        if current_task is not None:
            self._handler_tasks[subscriber_name] = current_task

        logger.info("Starting event handler loop: %s", subscriber_name)  # FIXED-P3: 中文日志→英文
        try:
            while not stop_event.is_set():
                # S-01修复: 使用 asyncio.wait 竞争 queue.get() 与 stop_event.wait()，
                # 避免协程永久阻塞在 queue.get() 上导致泄漏
                get_task = asyncio.ensure_future(queue.get())
                stop_task = asyncio.ensure_future(stop_event.wait())
                try:
                    done, pending = await asyncio.wait(
                        {get_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                except asyncio.CancelledError:
                    # 外部取消（如 shutdown），清理未完成任务并向上抛出
                    get_task.cancel()
                    stop_task.cancel()
                    raise

                if stop_task in done:
                    # S-01修复: 收到停止信号，取消 get_task 并退出循环
                    get_task.cancel()
                    break

                # stop_task 未完成，取消它以避免任务残留
                stop_task.cancel()

                # get_task 完成，取出事件并处理
                try:
                    event = get_task.result()
                except asyncio.CancelledError:
                    break

                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event)
                    else:
                        handler(event)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        "Event handler loop exception: %s - %s",
                        subscriber_name,
                        e,
                    )  # FIXED-P3: 中文日志→英文
        except asyncio.CancelledError:
            logger.info(
                "Event handler loop cancelled: %s",
                subscriber_name,
            )
        finally:
            # S-01修复: 协程退出时清理资源，从字典移除自身引用，避免内存泄漏
            self._handler_stop_events.pop(subscriber_name, None)
            self._handler_tasks.pop(subscriber_name, None)
            logger.info("Event handler loop exited: %s", subscriber_name)

    async def shutdown(self) -> None:
        """S-01修复: 关闭所有 handler_loop 协程，便于在应用 shutdown 时统一取消

        设置所有停止事件并取消所有 handler_loop Task，等待协程退出。
        """
        # 并发安全: 获取 _subscribers_lock 并设置 _shutting_down 标志，防止 shutdown 与 publish 竞态
        async with self._subscribers_lock:
            self._shutting_down = True
        # 并发安全: 取消所有重试任务，防止 fire-and-forget 任务在 shutdown 后继续运行
        retry_tasks = list(self._retry_tasks)
        for task in retry_tasks:
            if not task.done():
                task.cancel()
        if retry_tasks:
            await asyncio.gather(*retry_tasks, return_exceptions=True)
        self._retry_tasks.clear()
        # 设置所有停止事件，唤醒阻塞的 queue.get()
        for stop_event in self._handler_stop_events.values():
            stop_event.set()
        # 取消所有 handler_loop Task
        tasks = list(self._handler_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        # 等待所有 Task 退出（忽略 CancelledError）
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._handler_stop_events.clear()
        self._handler_tasks.clear()
        # FIXED: 关闭告警持久化 outbox 连接，防止句柄泄漏 [2026-06-29]
        if self._alarm_outbox is not None:
            try:
                self._alarm_outbox.close()
            except Exception as e:
                logger.warning("Alarm outbox close failed (best-effort): %s", e)
            finally:
                self._alarm_outbox = None
        logger.info("All event handler loops shut down")

    def get_handler_loop_count(self) -> int:
        """S-01修复: 获取当前活跃的 handler_loop 协程数量，用于监控"""
        return len(self._handler_tasks)
