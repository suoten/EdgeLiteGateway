"""流量控制与背压策略模块"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class BackpressureState(Enum):
    """背压状态"""

    NORMAL = "normal"  # 正常状态
    WARNING = "warning"  # 警告状态（接近阈值）
    BACKPRESSURE = "backpressure"  # 背压状态（触发限流）
    RECOVERING = "recovering"  # 恢复中


@dataclass
class BackpressureConfig:
    """背压配置"""

    max_queue_size: int = 1000  # 最大队列大小
    warning_threshold: float = 0.7  # 警告阈值（比例）
    backpressure_threshold: float = 0.9  # 背压阈值（比例）
    recovery_threshold: float = 0.3  # 恢复阈值（比例）
    check_interval: float = 1.0  # 检查间隔（秒）
    max_concurrent_requests: int = 100  # 最大并发请求数

    def __post_init__(self) -> None:
        # R8-S-13: 原问题-三阈值无 ge/le 约束且无跨字段校验，可能配置出 recovery >= warning 等无效关系；
        # 修复-校验三阈值在 [0,1] 范围内且满足 recovery < warning < backpressure
        for name, value in (
            ("warning_threshold", self.warning_threshold),
            ("backpressure_threshold", self.backpressure_threshold),
            ("recovery_threshold", self.recovery_threshold),
        ):
            if not (0 <= value <= 1):
                raise ValueError(f"{name} 必须在 [0, 1] 范围内，当前值为 {value}")
        if not (self.recovery_threshold < self.warning_threshold < self.backpressure_threshold):
            raise ValueError(
                f"阈值关系必须满足 recovery < warning < backpressure，"
                f"当前 recovery={self.recovery_threshold}, warning={self.warning_threshold}, "
                f"backpressure={self.backpressure_threshold}"
            )


@dataclass
class QueueMetrics:
    """队列指标"""

    depth: int = 0
    max_depth: int = 0
    enqueued_total: int = 0
    dequeued_total: int = 0
    dropped_total: int = 0
    backpressure_triggered: int = 0
    last_backpressure_at: datetime | None = None


class BackpressureController:
    """流量控制与背压策略控制器

    功能：
    - 请求队列管理
    - 背压触发/恢复
    - 采集优先级配置
    - 动态频率调整

    使用方式：
        controller = BackpressureController()
        await controller.enqueue(device_id, data, priority=1)
        # 或使用装饰器
        @backpressure_controller.limit
        async def my_request():
            ...
    """

    def __init__(self, config: BackpressureConfig | None = None):
        self._config = config or BackpressureConfig()
        self._queues: dict[str, asyncio.Queue] = {}
        self._priorities: dict[str, int] = {}
        self._metrics: dict[str, QueueMetrics] = {}
        self._state = BackpressureState.NORMAL
        self._state_lock = asyncio.Lock()
        self._running = False
        self._task: asyncio.Task | None = None
        self._callbacks: list[Callable] = []
        self._semaphore: asyncio.Semaphore | None = None
        # FIXED-P0: 原问题-release_slot无获取验证，未配对调用会导致信号量计数溢出；
        # 使用 _acquired_count 计数器跟踪当前已获取的槽位，release_slot 校验后才释放
        self._acquired_count: int = 0

    async def start(self) -> None:
        """启动背压控制器"""
        # FIXED-P0: 原问题-start()可重复调用，每次创建新的 _task 监控任务，
        # 导致多个监控循环并发运行，状态被重复更新；检查现有任务是否在运行，避免重复启动
        if self._task is not None and not self._task.done():
            logger.warning("Backpressure controller 已在运行，忽略重复start调用")
            return

        self._running = True
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent_requests)
        self._acquired_count = 0
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "Backpressure controller started (max_queue=%d, max_concurrent=%d)",
            self._config.max_queue_size,
            self._config.max_concurrent_requests,
        )

    async def stop(self) -> None:
        """停止背压控制器"""
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for queue in self._queues.values():
            if hasattr(queue, "close"):  # FIXED-P2: Queue.close()仅Python 3.12+存在
                queue.close()
        self._queues.clear()
        # FIXED-P0: 原问题-stop()不释放已占用的信号量槽位，_semaphore 引用残留；
        # 重置 _semaphore = None，release_slot 检查 _semaphore 是否为 None 避免对已释放信号量操作
        self._semaphore = None
        # FIXED(P0): 原问题-stop()调用self._in_use.clear()但_in_use从未在__init__中定义，导致AttributeError;
        # 修复-删除该行，并重置_acquired_count=0（_in_use从未被使用，_acquired_count已通过_semaphore=None间接失效）
        self._acquired_count = 0
        logger.info("Backpressure controller stopped")

    async def register_queue(
        self, queue_id: str, max_size: int | None = None
    ) -> None:  # FIXED-P2: 改为async加锁保护_queues/_metrics
        """注册队列"""
        async with self._state_lock:
            if queue_id not in self._queues:
                max_size = max_size or self._config.max_queue_size
                self._queues[queue_id] = asyncio.Queue(maxsize=max_size)
                self._metrics[queue_id] = QueueMetrics(max_depth=max_size)

    async def unregister_queue(self, queue_id: str) -> None:  # FIXED-P2: 改为async加锁保护_queues/_metrics
        """取消注册队列"""
        async with self._state_lock:
            queue = self._queues.pop(queue_id, None)
            if queue and hasattr(queue, "close"):
                queue.close()
            self._metrics.pop(queue_id, None)

    async def set_priority(self, queue_id: str, priority: int) -> None:  # FIXED-P2: 改为async加锁保护_priorities
        """设置队列优先级（数字越大优先级越高）"""
        async with self._state_lock:
            self._priorities[queue_id] = priority

    async def get_priority(self, queue_id: str) -> int:  # FIXED-P2: 改为async加锁保护_priorities
        """获取队列优先级"""
        async with self._state_lock:
            return self._priorities.get(queue_id, 0)

    async def enqueue(
        self,
        queue_id: str,
        item: Any,
        priority: int = 0,
        timeout: float = 5.0,
    ) -> bool:
        """入队操作

        Args:
            queue_id: 队列ID
            item: 要入队的项
            priority: 优先级（数字越大优先级越高）
            timeout: 入队超时时间

        Returns:
            True 表示入队成功，False 表示被拒绝（背压）
        """
        async with self._state_lock:  # FIXED-P2: _queues/_metrics读取加锁
            if queue_id not in self._queues:
                max_size = self._config.max_queue_size
                self._queues[queue_id] = asyncio.Queue(maxsize=max_size)
                self._metrics[queue_id] = QueueMetrics(max_depth=max_size)
            queue = self._queues[queue_id]
            if queue_id not in self._metrics:
                self._metrics[queue_id] = QueueMetrics()
            metrics = self._metrics[queue_id]
            # FIXED-P2: 原问题-qsize()在锁外检查，释放锁后队列状态可能已变化导致背压判断基于过时数据；
            # 将qsize检查和背压判断移入锁内，确保检查与判断的原子性
            current_size = queue.qsize()
            queue_ratio = current_size / metrics.max_depth if metrics.max_depth > 0 else 0
            should_drop = queue_ratio >= self._config.backpressure_threshold
            # FIXED-P1: 原问题-两次锁获取间metrics可因unregister_queue变为悬空引用；
            # 将metrics更新合并到同一临界区内，避免释放锁后引用失效
            if should_drop:
                metrics.dropped_total += 1
                metrics.backpressure_triggered += 1
                metrics.last_backpressure_at = datetime.now(UTC)
            else:
                # FIXED-P1: 原问题-背压检查与queue.put非原子，释放锁后队列可能已满导致误入队或误丢弃；
                # 将put_nowait移入锁内，确保检查与入队的原子性，QueueFull时视为背压
                try:
                    queue.put_nowait(item)
                    metrics.depth = queue.qsize()
                    metrics.enqueued_total += 1
                except asyncio.QueueFull:
                    should_drop = True
                    metrics.dropped_total += 1
                    metrics.backpressure_triggered += 1
                    metrics.last_backpressure_at = datetime.now(UTC)

        if should_drop:
            # 触发背压，丢弃请求
            await self._check_and_update_state()
            await self._notify_callbacks(queue_id, "dropped", item)

            logger.warning(
                "Backpressure: dropping item for queue %s (depth=%d/%d, ratio=%.2f)",
                queue_id,
                current_size,
                metrics.max_depth,
                queue_ratio,
            )
            return False

        # put_succeeded is True
        # 更新优先级
        if priority > 0:
            await self.set_priority(queue_id, priority)

        # 检查警告状态
        if queue_ratio >= self._config.warning_threshold:
            await self._notify_callbacks(queue_id, "warning", item)

        return True

    async def dequeue(self, queue_id: str, timeout: float = 1.0) -> Any | None:
        """出队操作

        Args:
            queue_id: 队列ID
            timeout: 出队超时时间

        Returns:
            出队的项，超时返回 None
        """
        # FIXED-P0: 锁内读取队列引用，防止队列在迭代期间被删除
        async with self._state_lock:
            if queue_id not in self._queues:
                return None
            queue = self._queues[queue_id]
            metrics = self._metrics.get(queue_id)

        try:
            item = await asyncio.wait_for(queue.get(), timeout=timeout)
            async with self._state_lock:  # FIXED-P0: 统计更新在锁内
                if metrics:
                    metrics.depth = queue.qsize()
                    metrics.dequeued_total += 1
            return item
        except TimeoutError:
            return None

    async def dequeue_all(self, queue_id: str, max_items: int = 100) -> list[Any]:
        """批量出队

        Args:
            queue_id: 队列ID
            max_items: 最大出队数量

        Returns:
            出队的项列表
        """
        items = []
        for _ in range(max_items):
            item = await self.dequeue(queue_id, timeout=0.01)
            if item is None:
                break
            items.append(item)
        return items

    async def acquire_slot(self, timeout: float = 5.0) -> bool:
        """获取执行槽位（用于限流）

        Args:
            timeout: 获取超时时间

        Returns:
            True 表示获取成功，False 表示被限流
        """
        # FIXED-P0: 原问题-stop()后 _semaphore 为 None，acquire_slot 仍尝试获取；
        # 检查 _semaphore 是否为 None，避免对已释放信号量操作
        if self._semaphore is None:
            return False

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            # FIXED-P0: 使用计数器跟踪当前已获取的槽位，release_slot 校验后才释放
            self._acquired_count += 1
            return True
        except TimeoutError:
            logger.warning("Backpressure: concurrent request limit reached")
            return False

    def release_slot(self) -> None:
        """释放执行槽位"""
        if self._semaphore is None:
            return
        if self._acquired_count <= 0:
            logger.warning("Backpressure: release_slot called without matching acquire_slot, ignored")
            return
        self._acquired_count -= 1
        self._semaphore.release()

    async def _notify_callbacks(self, queue_id: str, event_type: str, item: Any) -> None:
        """通知队列事件回调（dropped/warning）。

        FIXED(严重): 原问题-enqueue 调用 self._notify_callbacks 但该方法从未定义，
        导致背压触发时抛 AttributeError 使入队操作崩溃。
        修复-新增占位实现。当前 register_callback 注册的是状态变更回调
        (签名为 old_state/new_state/ratio)，与此处 per-event 通知语义不同，
        故此处保留接口但不触发状态回调，避免签名不匹配；为未来 per-event
        回调注册预留扩展点。
        """
        # 预留扩展点：未来可维护独立的 per-event 回调列表
        logger.debug(
            "Backpressure event: queue=%s type=%s (per-event callbacks not registered)",
            queue_id,
            event_type,
        )

    @property
    def state(self) -> BackpressureState:
        """获取当前背压状态"""
        return self._state

    async def get_queue_depth(self, queue_id: str) -> int:
        """获取队列深度"""
        # FIXED-P2: _queues读取需加锁保护
        async with self._state_lock:
            queue = self._queues.get(queue_id)
            return queue.qsize() if queue else 0

    async def get_queue_ratio(self, queue_id: str) -> float:
        """获取队列使用比例"""
        # FIXED-P2: _queues/_metrics读取需加锁保护
        async with self._state_lock:
            queue = self._queues.get(queue_id)
            metrics = self._metrics.get(queue_id)
            if not queue or not metrics or metrics.max_depth == 0:
                return 0.0
            return queue.qsize() / metrics.max_depth

    async def get_metrics(self, queue_id: str | None = None) -> dict:
        """获取背压指标"""
        if queue_id:
            async with self._state_lock:
                metrics = self._metrics.get(queue_id)
                if not metrics:
                    return {}
                return {
                    "queue_id": queue_id,
                    "depth": metrics.depth,
                    "max_depth": metrics.max_depth,
                    "ratio": metrics.depth / metrics.max_depth if metrics.max_depth > 0 else 0,
                    "enqueued_total": metrics.enqueued_total,
                    "dequeued_total": metrics.dequeued_total,
                    "dropped_total": metrics.dropped_total,
                    "backpressure_triggered": metrics.backpressure_triggered,
                    "last_backpressure_at": metrics.last_backpressure_at.isoformat()
                    if metrics.last_backpressure_at
                    else None,
                }

        async with self._state_lock:
            queue_ids = list(self._queues.keys())
            metrics_map = {}
            for qid in queue_ids:
                m = self._metrics.get(qid)
                if m:
                    metrics_map[qid] = {
                        "depth": m.depth,
                        "max_depth": m.max_depth,
                        "ratio": m.depth / m.max_depth if m.max_depth > 0 else 0,
                        "enqueued_total": m.enqueued_total,
                        "dequeued_total": m.dequeued_total,
                        "dropped_total": m.dropped_total,
                        "backpressure_triggered": m.backpressure_triggered,
                        "last_backpressure_at": m.last_backpressure_at.isoformat() if m.last_backpressure_at else None,
                    }
            return {
                "state": self._state.value,
                "queues": metrics_map,
                "total_queues": len(queue_ids),
                "max_concurrent_requests": self._config.max_concurrent_requests,
            }

    async def _monitor_loop(self) -> None:
        """监控循环"""
        while self._running:
            try:
                await asyncio.sleep(self._config.check_interval)
                await self._check_and_update_state()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Backpressure monitor error: %s", e)

    async def _check_and_update_state(self) -> None:
        """检查并更新背压状态"""
        # FIXED-P0: 锁内读取_queues/_metrics快照，防止迭代期间被修改
        async with self._state_lock:
            if not self._queues:
                return
            queues_snapshot = dict(self._queues)
            metrics_snapshot = dict(self._metrics)

        # 计算总体队列使用情况（在锁外，避免长时间持锁）
        total_depth = sum(queue.qsize() for queue in queues_snapshot.values())
        max_total = sum(m.max_depth for m in metrics_snapshot.values())
        overall_ratio = total_depth / max_total if max_total > 0 else 0

        # 确定状态
        new_state = BackpressureState.NORMAL
        if overall_ratio >= self._config.backpressure_threshold:
            new_state = BackpressureState.BACKPRESSURE
        elif overall_ratio >= self._config.warning_threshold:
            new_state = BackpressureState.WARNING
        elif overall_ratio <= self._config.recovery_threshold:
            new_state = BackpressureState.RECOVERING

        # 状态变更
        # FIXED-P3: 状态判断+更新在同一锁内，防止并发判断导致状态回退
        async with self._state_lock:
            if new_state != self._state:
                old_state = self._state
                self._state = new_state
            else:
                old_state = None

        if old_state is not None:
            logger.info(
                "Backpressure state changed: %s -> %s (ratio=%.2f)",
                old_state.value,
                new_state.value,
                overall_ratio,
            )

            # FIXED-P0: 锁内读取_callbacks快照，与register/unregister互斥
            async with self._state_lock:
                callbacks_snapshot = list(self._callbacks)
            for callback in callbacks_snapshot:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(old_state, new_state, overall_ratio)
                    else:
                        callback(old_state, new_state, overall_ratio)
                except Exception as e:
                    logger.warning("Backpressure callback error: %s", e)

    async def register_callback(self, callback: Callable) -> None:
        """注册背压状态变更回调"""
        async with self._state_lock:  # FIXED-P0: _callbacks写入加锁，与_check_and_update_state读取互斥
            self._callbacks.append(callback)

    async def unregister_callback(self, callback: Callable) -> None:
        """取消注册回调"""
        async with self._state_lock:  # FIXED-P0: _callbacks写入加锁，与_check_and_update_state读取互斥
            with contextlib.suppress(ValueError):
                self._callbacks.remove(callback)

    async def get_sorted_queues(self) -> list[str]:
        """获取按优先级排序的队列ID列表"""
        async with self._state_lock:
            return sorted(
                list(self._queues.keys()),
                key=lambda qid: self._priorities.get(qid, 0),
                reverse=True,
            )


class BackpressureLimit:
    """背压限制装饰器

    使用方式：
        @backpressure_limit.limit(requests_per_second=10)
        async def my_request():
            ...
    """

    def __init__(self, controller: BackpressureController | None = None):
        self._controller = controller or _global_controller

    def limit(
        self,
        requests_per_second: float = 0,
        max_concurrent: int = 0,
    ):
        """限流装饰器

        Args:
            requests_per_second: 每秒请求数限制（0表示不限）
            max_concurrent: 最大并发数限制（0表示不限）
        """

        def decorator(func: Callable):
            semaphore = asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None
            rate_limiter = asyncio.Semaphore(int(requests_per_second)) if requests_per_second > 0 else None
            last_call = [0.0]
            rate_lock = asyncio.Lock()  # FIXED-P0: 原问题-last_call无锁保护，并发调用都读取相同值并sleep，实际QPS超限

            async def wrapper(*args, **kwargs):
                # 限速
                if rate_limiter:
                    async with rate_lock:  # FIXED-P0: 加锁保护last_call的读-改-写操作
                        now = asyncio.get_running_loop().time()
                        elapsed = now - last_call[0]
                        if elapsed < 1.0 / requests_per_second:
                            await asyncio.sleep(1.0 / requests_per_second - elapsed)
                        last_call[0] = asyncio.get_running_loop().time()

                # 并发限制
                if semaphore:
                    async with semaphore:
                        return await func(*args, **kwargs)
                return await func(*args, **kwargs)

            return wrapper

        return decorator


# 全局背压控制器
_global_controller: BackpressureController | None = None
_global_controller_lock = threading.Lock()  # FIXED-P2: 全局单例初始化竞态保护


def get_backpressure_controller() -> BackpressureController:
    """获取全局背压控制器"""
    global _global_controller
    with _global_controller_lock:  # FIXED-P2: 全局单例初始化竞态保护
        if _global_controller is None:
            _global_controller = BackpressureController()
        return _global_controller


def init_backpressure_controller(config: BackpressureConfig) -> BackpressureController:
    """初始化全局背压控制器"""
    global _global_controller
    with _global_controller_lock:  # FIXED-P2: 全局单例初始化竞态保护
        _global_controller = BackpressureController(config)
        return _global_controller
