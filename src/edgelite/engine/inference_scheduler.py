"""AI 推理调度器 - 优先级队列 + 并发限制 + 503 溢出

提供 AI 推理请求的统一调度入口：
- 优先级队列: USER_QUERY > SCHEDULED > BACKGROUND
- Semaphore(4) 限制最大并发推理数
- 队列超限返回 503 (Service Unavailable)
- 单次推理耗时测量和统计
- 模型级推理函数注册/注销

使用方式:
    scheduler = InferenceScheduler(max_concurrent=4, max_queue_size=100)
    await scheduler.start()
    await scheduler.register_model("elg-anomaly-v1", infer_fn)
    result = await scheduler.submit_and_wait("elg-anomaly-v1", input_data, InferencePriority.USER_QUERY)
    await scheduler.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any

logger = logging.getLogger(__name__)


class InferencePriority(IntEnum):
    """推理请求优先级（数值越大优先级越高）"""

    BACKGROUND = 0  # 后台批量推理
    SCHEDULED = 1  # 定时调度推理
    USER_QUERY = 2  # 用户实时查询（最高优先级）


@dataclass
class _InferenceRequest:
    """内部推理请求"""

    model_id: str
    input_data: list[float]
    priority: InferencePriority
    future: asyncio.Future
    enqueue_time: float = field(default_factory=time.monotonic)


class InferenceScheduler:
    """AI 推理调度器

    线程安全：
    - _queue_lock (asyncio.Lock): 保护 _pending_queue 的并发访问
    - _registry_lock (asyncio.Lock): 保护 _model_infer_fn 的并发注册/注销
    """

    def __init__(
        self,
        max_concurrent: int = 4,
        max_queue_size: int = 100,
    ):
        self._max_concurrent = max_concurrent
        self._max_queue_size = max_queue_size
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._model_infer_fn: dict[str, Any] = {}
        self._registry_lock = asyncio.Lock()
        self._queue_lock = asyncio.Lock()
        self._pending_queue: deque[_InferenceRequest] = deque()
        self._dispatcher_task: asyncio.Task | None = None
        self._running = False

        # 统计
        self._total_submitted = 0
        self._total_completed = 0
        self._total_errors = 0
        self._total_rejected = 0  # 因队列满被拒绝
        self._total_latency_ms = 0
        self._per_model_stats: dict[str, dict[str, Any]] = {}

    async def start(self) -> None:
        """启动调度器"""
        if self._running:
            logger.warning("InferenceScheduler already started")
            return
        self._running = True
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop(), name="inference-dispatcher")
        logger.info(
            "InferenceScheduler started (max_concurrent=%d, max_queue=%d)",
            self._max_concurrent,
            self._max_queue_size,
        )

    async def stop(self) -> None:
        """停止调度器"""
        self._running = False
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
            import contextlib

            with contextlib.suppress(asyncio.CancelledError):
                await self._dispatcher_task
        # 拒绝所有待处理请求
        async with self._queue_lock:
            while self._pending_queue:
                req = self._pending_queue.popleft()
                if not req.future.done():
                    req.future.set_exception(RuntimeError("InferenceScheduler shutting down"))
        logger.info(
            "InferenceScheduler stopped (submitted=%d, completed=%d, errors=%d, rejected=%d)",
            self._total_submitted,
            self._total_completed,
            self._total_errors,
            self._total_rejected,
        )

    async def register_model(self, model_id: str, infer_fn: Any) -> None:
        """注册模型的推理函数

        Args:
            model_id: 模型 ID
            infer_fn: 异步推理函数 async fn(input_data: list[float]) -> Any
        """
        async with self._registry_lock:
            self._model_infer_fn[model_id] = infer_fn
        logger.info("Model registered to scheduler: %s", model_id)

    async def unregister_model(self, model_id: str) -> None:
        """注销模型"""
        async with self._registry_lock:
            self._model_infer_fn.pop(model_id, None)
        logger.info("Model unregistered from scheduler: %s", model_id)

    async def submit_and_wait(
        self,
        model_id: str,
        input_data: list[float],
        priority: InferencePriority = InferencePriority.USER_QUERY,
        timeout: float = 30.0,
    ) -> Any:
        """提交推理请求并等待结果

        Args:
            model_id: 模型 ID
            input_data: 输入数据
            priority: 请求优先级
            timeout: 超时秒数

        Returns:
            推理结果对象

        Raises:
            RuntimeError: 模型未注册或调度器未启动
            TimeoutError: 推理超时
        """
        if not self._running:
            raise RuntimeError("InferenceScheduler is not running")

        async with self._registry_lock:
            if model_id not in self._model_infer_fn:
                raise RuntimeError(f"Model not registered in scheduler: {model_id}")

        # 检查队列是否已满
        async with self._queue_lock:
            if len(self._pending_queue) >= self._max_queue_size:
                self._total_rejected += 1
                raise RuntimeError(
                    f"Inference queue full (max={self._max_queue_size}), rejecting request for model {model_id}"
                )

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        req = _InferenceRequest(
            model_id=model_id,
            input_data=input_data,
            priority=priority,
            future=future,
        )

        async with self._queue_lock:
            self._pending_queue.append(req)
            self._total_submitted += 1

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            raise TimeoutError(f"Inference request timed out after {timeout}s for model {model_id}") from None

    async def _dispatch_loop(self) -> None:
        """调度循环：从队列取请求，通过 Semaphore 限制并发执行"""
        while self._running:
            try:
                # 从队列取最高优先级请求
                req = None
                async with self._queue_lock:
                    if self._pending_queue:
                        # 按 priority 排序（高优先级先出队）
                        self._pending_queue = deque(sorted(self._pending_queue, key=lambda r: -r.priority))
                        req = self._pending_queue.popleft()

                if req is None:
                    await asyncio.sleep(0.001)
                    continue

                # 获取信号量限制并发
                await self._semaphore.acquire()
                # 启动推理任务（不等待完成，立即处理下一个请求）
                asyncio.create_task(self._execute_inference(req), name=f"infer-{req.model_id}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Dispatch loop error: %s", e)
                await asyncio.sleep(0.01)

    async def _execute_inference(self, req: _InferenceRequest) -> None:
        """执行单个推理请求（已持有信号量）"""
        start = time.perf_counter()
        try:
            async with self._registry_lock:
                infer_fn = self._model_infer_fn.get(req.model_id)
            if infer_fn is None:
                raise RuntimeError(f"Model not registered: {req.model_id}")

            result = await infer_fn(req.input_data)
            latency_ms = int((time.perf_counter() - start) * 1000)

            self._total_completed += 1
            self._total_latency_ms += latency_ms
            self._update_model_stats(req.model_id, latency_ms, True)

            if not req.future.done():
                req.future.set_result(result)

            logger.debug(
                "Inference completed: model=%s, priority=%s, latency=%dms, queue_wait=%dms",
                req.model_id,
                req.priority.name,
                latency_ms,
                int((start - req.enqueue_time) * 1000),
            )
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            self._total_errors += 1
            self._update_model_stats(req.model_id, latency_ms, False)

            if not req.future.done():
                req.future.set_exception(e)

            logger.error(
                "Inference failed: model=%s, latency=%dms, error=%s",
                req.model_id,
                latency_ms,
                e,
            )
        finally:
            self._semaphore.release()

    def _update_model_stats(self, model_id: str, latency_ms: int, success: bool) -> None:
        """更新模型级统计"""
        if model_id not in self._per_model_stats:
            self._per_model_stats[model_id] = {
                "call_count": 0,
                "error_count": 0,
                "total_latency_ms": 0,
                "max_latency_ms": 0,
                "min_latency_ms": 0,
            }
        stats = self._per_model_stats[model_id]
        stats["call_count"] += 1
        stats["total_latency_ms"] += latency_ms
        if latency_ms > stats["max_latency_ms"]:
            stats["max_latency_ms"] = latency_ms
        if stats["min_latency_ms"] == 0 or latency_ms < stats["min_latency_ms"]:
            stats["min_latency_ms"] = latency_ms
        if not success:
            stats["error_count"] += 1

    async def get_model_metrics(self, model_id: str) -> dict | None:
        """获取模型级推理指标"""
        stats = self._per_model_stats.get(model_id)
        if stats is None:
            return None
        calls = stats["call_count"]
        return {
            "model_id": model_id,
            "call_count": calls,
            "error_count": stats["error_count"],
            "avg_latency_ms": stats["total_latency_ms"] // calls if calls > 0 else 0,
            "max_latency_ms": stats["max_latency_ms"],
            "min_latency_ms": stats["min_latency_ms"],
        }

    async def get_stats(self) -> dict:
        """获取调度器全局统计"""
        avg_latency = self._total_latency_ms // self._total_completed if self._total_completed > 0 else 0
        model_distribution = {mid: s["call_count"] for mid, s in self._per_model_stats.items()}
        return {
            "total_submitted": self._total_submitted,
            "total_completed": self._total_completed,
            "total_errors": self._total_errors,
            "total_rejected": self._total_rejected,
            "avg_latency_ms": avg_latency,
            "pending_queue_size": len(self._pending_queue),
            "max_concurrent": self._max_concurrent,
            "model_distribution": model_distribution,
        }

    @property
    def queue_size(self) -> int:
        """当前队列大小"""
        return len(self._pending_queue)
