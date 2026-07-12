"""驱动熔断器测试 - 三态状态机/熔断阈值/半开恢复

覆盖 engine/circuit_breaker.py：
- CircuitState 枚举
- CircuitBreakerStats: failure_rate/current_state/默认值
- CircuitBreaker: CLOSED→OPEN→HALF_OPEN→CLOSED 状态转换
  - 失败阈值熔断/恢复超时半开/半开成功关闭/半开失败重开
  - excluded_exceptions 忽略
  - call() 成功/失败/降级/拒绝
  - reset()/get_status()
- CircuitBreakerManager: get_breaker/call_with_protection/get_all_status/reset/remove
- get_circuit_breaker_manager 单例
"""

from __future__ import annotations

import time

import pytest

from edgelite.engine.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerManager,
    CircuitBreakerStats,
    CircuitState,
    get_circuit_breaker_manager,
)

# --------------------------------------------------------------------------- #
# CircuitState
# --------------------------------------------------------------------------- #


class TestCircuitState:
    def test_values(self):
        assert CircuitState.CLOSED.value == "closed"
        assert CircuitState.OPEN.value == "open"
        assert CircuitState.HALF_OPEN.value == "half_open"


# --------------------------------------------------------------------------- #
# CircuitBreakerStats
# --------------------------------------------------------------------------- #


class TestCircuitBreakerStats:
    def test_defaults(self):
        stats = CircuitBreakerStats()
        assert stats.total_calls == 0
        assert stats.successful_calls == 0
        assert stats.failed_calls == 0
        assert stats.rejected_calls == 0
        assert stats.state_changes == 0
        assert stats.consecutive_failures == 0
        assert stats.consecutive_successes == 0
        assert stats.total_open_duration == 0.0
        assert stats.last_state_change is None
        assert stats.last_failure is None
        assert stats.last_success is None
        assert stats.opened_at is None

    def test_current_state_defaults_closed(self):
        stats = CircuitBreakerStats()
        assert stats.current_state == CircuitState.CLOSED

    def test_failure_rate_zero_when_no_calls(self):
        stats = CircuitBreakerStats()
        assert stats.failure_rate == 0.0

    def test_failure_rate_calculation(self):
        stats = CircuitBreakerStats(total_calls=10, failed_calls=3, rejected_calls=2)
        # (failed + rejected) / (total + rejected) = (3+2) / (10+2) = 5/12
        assert stats.failure_rate == 5 / 12

    def test_failure_rate_all_failed(self):
        stats = CircuitBreakerStats(total_calls=5, failed_calls=5)
        assert stats.failure_rate == 1.0


# --------------------------------------------------------------------------- #
# CircuitBreaker - 状态转换
# --------------------------------------------------------------------------- #


class TestCircuitBreakerStates:
    @pytest.mark.asyncio
    async def test_initial_state_closed(self):
        cb = CircuitBreaker("test")
        assert cb.state == CircuitState.CLOSED
        assert cb.is_closed() is True
        assert cb.is_open is False
        assert cb.is_half_open() is False

    @pytest.mark.asyncio
    async def test_closed_to_open_on_threshold(self):
        """连续失败达到阈值 → OPEN"""
        cb = CircuitBreaker("test", failure_threshold=3)
        await cb.record_failure(ValueError("e1"))
        await cb.record_failure(ValueError("e2"))
        assert cb.state == CircuitState.CLOSED  # 未达阈值
        await cb.record_failure(ValueError("e3"))
        assert cb.state == CircuitState.OPEN
        assert cb.is_open is True

    @pytest.mark.asyncio
    async def test_open_to_half_open_after_recovery_timeout(self):
        """OPEN 超过 recovery_timeout → HALF_OPEN（通过 call 触发）"""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)
        await cb.record_failure(ValueError("e"))
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        # call 触发 OPEN→HALF_OPEN 转换
        result = await cb.call(lambda: "ok")
        assert cb.state == CircuitState.HALF_OPEN
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_half_open_to_closed_on_success_threshold(self):
        """HALF_OPEN 累计成功达到阈值 → CLOSED"""
        cb = CircuitBreaker("test", failure_threshold=1, success_threshold=2, recovery_timeout=0.1)
        await cb.record_failure(ValueError("e"))
        assert cb.state == CircuitState.OPEN
        time.sleep(0.15)
        # 第一次成功
        await cb.call(lambda: "ok1")
        assert cb.state == CircuitState.HALF_OPEN  # 还未达 success_threshold
        # 第二次成功
        await cb.call(lambda: "ok2")
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_to_open_on_failure(self):
        """HALF_OPEN 时失败 → 立即回到 OPEN"""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1)
        await cb.record_failure(ValueError("e"))
        time.sleep(0.15)
        # 进入 HALF_OPEN
        await cb.call(lambda: "ok")
        assert cb.state == CircuitState.HALF_OPEN
        # 失败 → OPEN
        await cb.record_failure(ValueError("e2"))
        assert cb.state == CircuitState.OPEN


# --------------------------------------------------------------------------- #
# CircuitBreaker - call()
# --------------------------------------------------------------------------- #


class TestCircuitBreakerCall:
    @pytest.mark.asyncio
    async def test_call_success_returns_result(self):
        cb = CircuitBreaker("test")
        result = await cb.call(lambda: "success")
        assert result == "success"

    @pytest.mark.asyncio
    async def test_call_async_func(self):
        cb = CircuitBreaker("test")

        async def _async_func():
            return "async_result"

        result = await cb.call(_async_func)
        assert result == "async_result"

    @pytest.mark.asyncio
    async def test_call_failure_records_failure(self):
        cb = CircuitBreaker("test", failure_threshold=3)

        def _fail():
            raise ValueError("boom")

        result = await cb.call(_fail)
        assert result is None
        assert cb.stats.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_call_with_fallback_on_failure(self):
        cb = CircuitBreaker("test")

        def _fail():
            raise ValueError("boom")

        result = await cb.call(_fail, fallback=lambda: "fallback_value")
        assert result == "fallback_value"

    @pytest.mark.asyncio
    async def test_call_with_async_fallback(self):
        cb = CircuitBreaker("test")

        async def _async_fallback():
            return "async_fallback"

        def _fail():
            raise ValueError("boom")

        result = await cb.call(_fail, fallback=_async_fallback)
        assert result == "async_fallback"

    @pytest.mark.asyncio
    async def test_call_rejected_when_open(self):
        """OPEN 状态拒绝调用，返回 fallback 或 None"""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=100)
        await cb.record_failure(ValueError("e"))
        assert cb.state == CircuitState.OPEN
        result = await cb.call(lambda: "should_not_run")
        assert result is None
        assert cb.stats.rejected_calls >= 1

    @pytest.mark.asyncio
    async def test_call_rejected_with_fallback(self):
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=100)
        await cb.record_failure(ValueError("e"))
        result = await cb.call(lambda: "x", fallback=lambda: "fb")
        assert result == "fb"

    @pytest.mark.asyncio
    async def test_excluded_exceptions_not_counted(self):
        """排除的异常不计为失败"""
        cb = CircuitBreaker("test", failure_threshold=2, excluded_exceptions=(KeyError,))
        await cb.record_failure(KeyError("ignored"))
        await cb.record_failure(KeyError("ignored"))
        assert cb.state == CircuitState.CLOSED  # 未触发熔断
        assert cb.stats.consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_half_open_max_calls_limit(self):
        """HALF_OPEN 限制最大放行数"""
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=0.1, half_open_max_calls=1)
        await cb.record_failure(ValueError("e"))
        time.sleep(0.15)
        # 第一次调用放行
        await cb.call(lambda: "ok")
        assert cb.state == CircuitState.HALF_OPEN
        # 第二次调用被拒绝（超过 half_open_max_calls）
        result = await cb.call(lambda: "ok2")
        assert result is None  # 被拒绝


# --------------------------------------------------------------------------- #
# CircuitBreaker - reset / get_status
# --------------------------------------------------------------------------- #


class TestCircuitBreakerResetStatus:
    @pytest.mark.asyncio
    async def test_reset_returns_to_closed(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        await cb.record_failure(ValueError("e"))
        assert cb.state == CircuitState.OPEN
        await cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0

    @pytest.mark.asyncio
    async def test_reset_increments_state_changes(self):
        cb = CircuitBreaker("test", failure_threshold=1)
        await cb.record_failure(ValueError("e"))
        changes_before = cb.stats.state_changes
        await cb.reset()
        assert cb.stats.state_changes == changes_before + 1

    @pytest.mark.asyncio
    async def test_get_status_returns_dict(self):
        cb = CircuitBreaker("test", failure_threshold=5, recovery_timeout=30.0)
        status = cb.get_status()
        assert status["name"] == "test"
        assert status["state"] == "closed"
        assert status["failure_threshold"] == 5
        assert status["recovery_timeout"] == 30.0
        assert status["total_calls"] == 0
        assert status["failure_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_get_status_after_failures(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        await cb.record_failure(ValueError("e1"))
        await cb.record_failure(ValueError("e2"))
        status = cb.get_status()
        assert status["consecutive_failures"] == 2
        assert status["failed_calls"] == 2
        assert status["last_failure"] is not None

    @pytest.mark.asyncio
    async def test_record_success_increments_stats(self):
        cb = CircuitBreaker("test")
        await cb.record_success()
        assert cb.stats.successful_calls == 1
        assert cb.stats.consecutive_successes == 1
        assert cb.stats.last_success is not None


# --------------------------------------------------------------------------- #
# CircuitBreakerManager
# --------------------------------------------------------------------------- #


class TestCircuitBreakerManager:
    @pytest.mark.asyncio
    async def test_get_breaker_creates_new(self):
        mgr = CircuitBreakerManager()
        cb = await mgr.get_breaker("device_1")
        assert cb.name == "device_1"
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_get_breaker_returns_same_instance(self):
        mgr = CircuitBreakerManager()
        cb1 = await mgr.get_breaker("device_1")
        cb2 = await mgr.get_breaker("device_1")
        assert cb1 is cb2

    @pytest.mark.asyncio
    async def test_get_breaker_different_devices(self):
        mgr = CircuitBreakerManager()
        cb1 = await mgr.get_breaker("dev_a")
        cb2 = await mgr.get_breaker("dev_b")
        assert cb1 is not cb2

    @pytest.mark.asyncio
    async def test_call_with_protection(self):
        mgr = CircuitBreakerManager()
        result = await mgr.call_with_protection("dev_1", lambda: "protected_result")
        assert result == "protected_result"

    @pytest.mark.asyncio
    async def test_get_all_status(self):
        mgr = CircuitBreakerManager()
        await mgr.get_breaker("dev_a")
        await mgr.get_breaker("dev_b")
        statuses = await mgr.get_all_status()
        assert len(statuses) == 2
        names = {s["name"] for s in statuses}
        assert names == {"dev_a", "dev_b"}

    @pytest.mark.asyncio
    async def test_get_device_status(self):
        mgr = CircuitBreakerManager()
        await mgr.get_breaker("dev_a")
        status = await mgr.get_device_status("dev_a")
        assert status is not None
        assert status["name"] == "dev_a"

    @pytest.mark.asyncio
    async def test_get_device_status_nonexistent(self):
        mgr = CircuitBreakerManager()
        status = await mgr.get_device_status("nonexistent")
        assert status is None

    @pytest.mark.asyncio
    async def test_reset_device(self):
        mgr = CircuitBreakerManager()
        cb = await mgr.get_breaker("dev_a", failure_threshold=1)
        await cb.record_failure(ValueError("e"))
        assert cb.state == CircuitState.OPEN
        result = await mgr.reset_device("dev_a")
        assert result is True
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_reset_device_nonexistent(self):
        mgr = CircuitBreakerManager()
        result = await mgr.reset_device("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_reset_all(self):
        mgr = CircuitBreakerManager()
        await mgr.get_breaker("dev_a")
        await mgr.get_breaker("dev_b")
        count = await mgr.reset_all()
        assert count == 2

    @pytest.mark.asyncio
    async def test_remove_breaker(self):
        mgr = CircuitBreakerManager()
        await mgr.get_breaker("dev_a")
        result = await mgr.remove_breaker("dev_a")
        assert result is True
        status = await mgr.get_device_status("dev_a")
        assert status is None

    @pytest.mark.asyncio
    async def test_remove_breaker_nonexistent(self):
        mgr = CircuitBreakerManager()
        result = await mgr.remove_breaker("nonexistent")
        assert result is False


# --------------------------------------------------------------------------- #
# get_circuit_breaker_manager 单例
# --------------------------------------------------------------------------- #


class TestGetCircuitBreakerManager:
    def test_singleton_returns_same_instance(self):
        m1 = get_circuit_breaker_manager()
        m2 = get_circuit_breaker_manager()
        assert m1 is m2
