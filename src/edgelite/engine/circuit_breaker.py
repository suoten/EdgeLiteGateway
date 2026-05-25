"""驱动熔断器模式 - 防止故障传播，实现故障隔离"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, UTC
from enum import Enum
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """熔断器状态"""

    CLOSED = "closed"  # 正常状态，请求通过
    OPEN = "open"  # 熔断状态，请求被拒绝
    HALF_OPEN = "half_open"  # 半开状态，尝试放行部分请求


@dataclass
class CircuitBreakerStats:
    """熔断器统计"""

    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    rejected_calls: int = 0
    state_changes: int = 0
    last_state_change: datetime | None = None
    last_failure: datetime | None = None
    last_success: datetime | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    total_open_duration: float = 0.0  # 累计熔断时长（秒）
    opened_at: datetime | None = None

    @property
    def failure_rate(self) -> float:
        """失败率 (0-1)"""
        if self.total_calls == 0:
            return 0.0
        return self.failed_calls / self.total_calls

    @property
    def current_state(self) -> CircuitState:
        return CircuitState.CLOSED


class CircuitBreaker:
    """驱动熔断器

    实现 Circuit Breaker 模式，用于防止故障在系统间传播。

    状态机:
        CLOSED ──(failure >= threshold)──> OPEN
        OPEN ──(timeout)──> HALF_OPEN
        HALF_OPEN ──(failure)──> OPEN
        HALF_OPEN ──(success)──> CLOSED

    配置参数:
        failure_threshold: 失败次数阈值，超过此值则开启熔断 (默认: 5)
        success_threshold: 半开状态下成功次数阈值 (默认: 2)
        recovery_timeout: 恢复超时（秒），熔断后等待此时间进入半开状态 (默认: 30)
        half_open_max_calls: 半开状态下最大放行调用数 (默认: 3)
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        success_threshold: int = 2,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 3,
        excluded_exceptions: tuple[type[Exception], ...] = (),
    ):
        self.name = name
        self._failure_threshold = failure_threshold
        self._success_threshold = success_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_max_calls = half_open_max_calls
        self._excluded_exceptions = excluded_exceptions

        self._state: CircuitState = CircuitState.CLOSED
        self._stats = CircuitBreakerStats()
        self._failure_count: int = 0
        self._success_count: int = 0
        self._half_open_calls: int = 0
        self._opened_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        """获取当前状态"""
        return self._state

    @property
    def stats(self) -> CircuitBreakerStats:
        """获取统计信息"""
        return self._stats

    def is_closed(self) -> bool:
        """是否处于关闭状态（正常）"""
        return self._state == CircuitState.CLOSED

    def is_open(self) -> bool:
        """是否处于熔断状态"""
        return self._state == CircuitState.OPEN

    def is_half_open(self) -> bool:
        """是否处于半开状态"""
        return self._state == CircuitState.HALF_OPEN

    async def call(
        self,
        func: Callable[..., T],
        *args: Any,
        fallback: Callable[..., T] | None = None,
        **kwargs: Any,
    ) -> T | None:
        """带熔断保护的调用

        Args:
            func: 要调用的异步函数
            fallback: 熔断时的降级函数
            *args, **kwargs: 传递给 func 的参数

        Returns:
            函数执行结果，或 fallback 的返回值，或 None
        """
        if not await self._can_execute():
            self._stats.rejected_calls += 1
            logger.warning(
                "Circuit breaker [%s] rejected call (state=%s)",
                self.name,
                self._state.value,
            )
            if fallback:
                try:
                    if asyncio.iscoroutine_function(fallback):
                        return await fallback(*args, **kwargs)
                    return fallback(*args, **kwargs)
                except Exception as e:
                    logger.error("Fallback failed: %s", e)
            return None

        try:
            result = await self._execute(func, *args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            await self._on_failure(e)
            if fallback:
                try:
                    if asyncio.iscoroutine_function(fallback):
                        return await fallback(*args, **kwargs)
                    return fallback(*args, **kwargs)
                except Exception as fallback_error:
                    logger.error("Fallback failed: %s", fallback_error)
            return None

    async def _can_execute(self) -> bool:
        """检查是否可以执行请求"""
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                # 检查是否超时
                if self._opened_at and (time.time() - self._opened_at) >= self._recovery_timeout:
                    await self._transition_to_half_open()
                    return True
                return False

            if self._state == CircuitState.HALF_OPEN:
                # 半开状态限制并发数
                if self._half_open_calls < self._half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False

            return False

    async def _execute(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """执行函数"""
        self._stats.total_calls += 1
        if asyncio.iscoroutine_function(func):
            return await func(*args, **kwargs)
        return func(*args, **kwargs)

    async def _on_success(self) -> None:
        """记录成功"""
        async with self._lock:
            self._failure_count = 0
            self._success_count += 1
            self._stats.consecutive_failures = 0
            self._stats.consecutive_successes += 1
            self._stats.successful_calls += 1
            self._stats.last_success = datetime.now(UTC)

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self._success_threshold:
                    await self._transition_to_closed()

    async def _on_failure(self, exception: Exception) -> None:
        """记录失败"""
        # 检查是否排除的异常
        if isinstance(exception, self._excluded_exceptions):
            logger.debug(
                "Circuit breaker [%s] ignoring excluded exception: %s",
                self.name,
                exception,
            )
            return

        async with self._lock:
            self._failure_count += 1
            self._success_count = 0
            self._stats.consecutive_failures += 1
            self._stats.consecutive_successes = 0
            self._stats.failed_calls += 1
            self._stats.last_failure = datetime.now(UTC)

            if self._state == CircuitState.CLOSED:
                if self._failure_count >= self._failure_threshold:
                    await self._transition_to_open()
            elif self._state == CircuitState.HALF_OPEN:
                await self._transition_to_open()

    async def _transition_to_open(self) -> None:
        """转换到 OPEN 状态"""
        if self._state == CircuitState.OPEN:
            return

        self._state = CircuitState.OPEN
        self._opened_at = time.time()
        self._stats.opened_at = datetime.now(UTC)
        self._stats.state_changes += 1
        self._stats.last_state_change = datetime.now(UTC)

        logger.warning(
            "Circuit breaker [%s] OPENED (failures=%d >= threshold=%d)",
            self.name,
            self._failure_count,
            self._failure_threshold,
        )

    async def _transition_to_half_open(self) -> None:
        """转换到 HALF_OPEN 状态"""
        self._state = CircuitState.HALF_OPEN
        self._half_open_calls = 0
        self._success_count = 0
        self._failure_count = 0
        self._stats.state_changes += 1
        self._stats.last_state_change = datetime.now(UTC)

        logger.info(
            "Circuit breaker [%s] HALF_OPEN (recovery_timeout=%.1fs)",
            self.name,
            self._recovery_timeout,
        )

    async def _transition_to_closed(self) -> None:
        """转换到 CLOSED 状态"""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._half_open_calls = 0
        self._opened_at = None
        self._stats.state_changes += 1
        self._stats.last_state_change = datetime.now(UTC)

        # 累计熔断时长
        if self._stats.opened_at:
            self._stats.total_open_duration += (datetime.now(UTC) - self._stats.opened_at).total_seconds()
            self._stats.opened_at = None

        logger.info("Circuit breaker [%s] CLOSED", self.name)

    async def reset(self) -> None:
        """手动重置熔断器"""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._success_count = 0
            self._half_open_calls = 0
            self._opened_at = None
            self._stats.state_changes += 1
            self._stats.last_state_change = datetime.now(UTC)
            logger.info("Circuit breaker [%s] manually reset", self.name)

    def get_status(self) -> dict:
        """获取熔断器状态"""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_threshold": self._failure_threshold,
            "recovery_timeout": self._recovery_timeout,
            "total_calls": self._stats.total_calls,
            "successful_calls": self._stats.successful_calls,
            "failed_calls": self._stats.failed_calls,
            "rejected_calls": self._stats.rejected_calls,
            "failure_rate": round(self._stats.failure_rate, 4),
            "consecutive_failures": self._stats.consecutive_failures,
            "state_changes": self._stats.state_changes,
            "last_state_change": self._stats.last_state_change.isoformat() if self._stats.last_state_change else None,
            "last_failure": self._stats.last_failure.isoformat() if self._stats.last_failure else None,
            "last_success": self._stats.last_success.isoformat() if self._stats.last_success else None,
        }


class CircuitBreakerManager:
    """熔断器管理器 - 管理多个设备的熔断器"""

    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    def get_breaker(
        self,
        device_id: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> CircuitBreaker:
        """获取或创建设备的熔断器"""
        if device_id not in self._breakers:
            self._breakers[device_id] = CircuitBreaker(
                name=device_id,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
            )
        return self._breakers[device_id]

    async def call_with_protection(
        self,
        device_id: str,
        func: Callable[..., T],
        *args: Any,
        fallback: Callable[..., T] | None = None,
        **kwargs: Any,
    ) -> T | None:
        """带熔断保护的调用"""
        breaker = self.get_breaker(device_id)
        return await breaker.call(func, *args, fallback=fallback, **kwargs)

    def get_all_status(self) -> list[dict]:
        """获取所有熔断器状态"""
        return [breaker.get_status() for breaker in self._breakers.values()]

    def get_device_status(self, device_id: str) -> dict | None:
        """获取指定设备熔断器状态"""
        breaker = self._breakers.get(device_id)
        return breaker.get_status() if breaker else None

    async def reset_device(self, device_id: str) -> bool:
        """重置指定设备的熔断器"""
        breaker = self._breakers.get(device_id)
        if breaker:
            await breaker.reset()
            return True
        return False

    async def reset_all(self) -> int:
        """重置所有熔断器，返回重置数量"""
        count = len(self._breakers)
        for breaker in self._breakers.values():
            await breaker.reset()
        return count

    def remove_breaker(self, device_id: str) -> bool:
        """移除设备的熔断器"""
        if device_id in self._breakers:
            del self._breakers[device_id]
            return True
        return False


# 全局熔断器管理器实例
_circuit_breaker_manager: CircuitBreakerManager | None = None


def get_circuit_breaker_manager() -> CircuitBreakerManager:
    """获取全局熔断器管理器"""
    global _circuit_breaker_manager
    if _circuit_breaker_manager is None:
        _circuit_breaker_manager = CircuitBreakerManager()
    return _circuit_breaker_manager
