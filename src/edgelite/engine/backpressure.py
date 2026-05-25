"""流量控制与背压策略模块"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any, Callable

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

    async def start(self) -> None:
        """启动背压控制器"""
        self._running = True
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent_requests)
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
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for queue in self._queues.values():
            queue.close()
        self._queues.clear()
        logger.info("Backpressure controller stopped")

    def register_queue(self, queue_id: str, max_size: int | None = None) -> None:
        """注册队列"""
        if queue_id not in self._queues:
            max_size = max_size or self._config.max_queue_size
            self._queues[queue_id] = asyncio.Queue(maxsize=max_size)
            self._metrics[queue_id] = QueueMetrics(max_depth=max_size)

    def unregister_queue(self, queue_id: str) -> None:
        """取消注册队列"""
        queue = self._queues.pop(queue_id, None)
        if queue:
            queue.close()
        self._metrics.pop(queue_id, None)

    def set_priority(self, queue_id: str, priority: int) -> None:
        """设置队列优先级（数字越大优先级越高）"""
        self._priorities[queue_id] = priority

    def get_priority(self, queue_id: str) -> int:
        """获取队列优先级"""
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
        if queue_id not in self._queues:
            self.register_queue(queue_id)

        queue = self._queues[queue_id]
        metrics = self._metrics.get(queue_id, QueueMetrics())

        # 检查队列状态
        current_size = queue.qsize()
        queue_ratio = current_size / metrics.max_depth if metrics.max_depth > 0 else 0

        if queue_ratio >= self._config.backpressure_threshold:
            # 触发背压，丢弃请求
            metrics.dropped_total += 1
            metrics.backpressure_triggered += 1
            metrics.last_backpressure_at = datetime.now(UTC)

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

        try:
            await asyncio.wait_for(queue.put(item), timeout=timeout)
            metrics.depth = queue.qsize()
            metrics.enqueued_total += 1

            # 更新优先级
            if priority > 0:
                self.set_priority(queue_id, priority)

            # 检查警告状态
            if queue_ratio >= self._config.warning_threshold:
                await self._notify_callbacks(queue_id, "warning", item)

            return True

        except asyncio.TimeoutError:
            metrics.dropped_total += 1
            logger.warning("Queue %s enqueue timeout, item dropped", queue_id)
            return False

    async def dequeue(self, queue_id: str, timeout: float = 1.0) -> Any | None:
        """出队操作

        Args:
            queue_id: 队列ID
            timeout: 出队超时时间

        Returns:
            出队的项，超时返回 None
        """
        if queue_id not in self._queues:
            return None

        queue = self._queues[queue_id]
        metrics = self._metrics.get(queue_id)

        try:
            item = await asyncio.wait_for(queue.get(), timeout=timeout)
            if metrics:
                metrics.depth = queue.qsize()
                metrics.dequeued_total += 1
            return item
        except asyncio.TimeoutError:
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
        if not self._semaphore:
            return True

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Backpressure: concurrent request limit reached")
            return False

    def release_slot(self) -> None:
        """释放执行槽位"""
        if self._semaphore:
            self._semaphore.release()

    @property
    def state(self) -> BackpressureState:
        """获取当前背压状态"""
        return self._state

    def get_queue_depth(self, queue_id: str) -> int:
        """获取队列深度"""
        queue = self._queues.get(queue_id)
        return queue.qsize() if queue else 0

    def get_queue_ratio(self, queue_id: str) -> float:
        """获取队列使用比例"""
        queue = self._queues.get(queue_id)
        metrics = self._metrics.get(queue_id)
        if not queue or not metrics or metrics.max_depth == 0:
            return 0.0
        return queue.qsize() / metrics.max_depth

    def get_metrics(self, queue_id: str | None = None) -> dict:
        """获取背压指标"""
        if queue_id:
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
                "last_backpressure_at": metrics.last_backpressure_at.isoformat() if metrics.last_backpressure_at else None,
            }

        return {
            "state": self._state.value,
            "queues": {qid: self.get_metrics(qid) for qid in self._queues.keys()},
            "total_queues": len(self._queues),
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
        if not self._queues:
            return

        # 计算总体队列使用情况
        total_depth = sum(queue.qsize() for queue in self._queues.values())
        max_total = sum(m.max_depth for m in self._metrics.values())
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
        if new_state != self._state:
            old_state = self._state
            async with self._state_lock:
                self._state = new_state

            logger.info(
                "Backpressure state changed: %s -> %s (ratio=%.2f)",
                old_state.value,
                new_state.value,
                overall_ratio,
            )

            # 触发回调
            for callback in self._callbacks:
                try:
                    if asyncio.iscoroutine_function(callback):
                        await callback(old_state, new_state, overall_ratio)
                    else:
                        callback(old_state, new_state, overall_ratio)
                except Exception as e:
                    logger.warning("Backpressure callback error: %s", e)

    def register_callback(self, callback: Callable) -> None:
        """注册背压状态变更回调"""
        self._callbacks.append(callback)

    def unregister_callback(self, callback: Callable) -> None:
        """取消注册回调"""
        try:
            self._callbacks.remove(callback)
        except ValueError:
            pass

    def get_sorted_queues(self) -> list[str]:
        """获取按优先级排序的队列ID列表"""
        return sorted(
            self._queues.keys(),
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
            semaphore = (
                asyncio.Semaphore(max_concurrent) if max_concurrent > 0 else None
            )
            rate_limiter = (
                asyncio.Semaphore(int(requests_per_second))
                if requests_per_second > 0
                else None
            )
            last_call = [0.0]

            async def wrapper(*args, **kwargs):
                # 限速
                if rate_limiter:
                    now = asyncio.get_event_loop().time()
                    elapsed = now - last_call[0]
                    if elapsed < 1.0 / requests_per_second:
                        await asyncio.sleep(1.0 / requests_per_second - elapsed)
                    last_call[0] = asyncio.get_event_loop().time()

                # 并发限制
                if semaphore:
                    async with semaphore:
                        return await func(*args, **kwargs)
                return await func(*args, **kwargs)

            return wrapper

        return decorator


# 全局背压控制器
_global_controller: BackpressureController | None = None


def get_backpressure_controller() -> BackpressureController:
    """获取全局背压控制器"""
    global _global_controller
    if _global_controller is None:
        _global_controller = BackpressureController()
    return _global_controller


def init_backpressure_controller(config: BackpressureConfig) -> BackpressureController:
    """初始化全局背压控制器"""
    global _global_controller
    _global_controller = BackpressureController(config)
    return _global_controller
