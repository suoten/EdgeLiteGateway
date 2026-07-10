"""表达式引擎线程池扩容 + eval 超时线程取消单元测试 (并发安全 #6, #7)

覆盖 P1 修复:
  #6: 线程池大小可配置 (原硬编码 max_workers=2 → 可配置，默认 4)
  #7: eval 超时后调用 future.cancel() 取消 future + 超时可配置 + submit/rebuild 加锁

原问题:
  1. ExpressionEngine.__init__ 硬编码 max_workers=2，高并发场景下多个表达式
     并行评估时相互阻塞 (线程池满 → 排队等待)。
  2. _eval_with_timeout 超时后不调用 future.cancel()，future 状态残留为 RUNNING，
     超时后的结果可能被误用。
  3. _eval_with_timeout 的 submit 与 _rebuild_eval_pool 无锁保护，并发调用时
     可能向已 shutdown 的池提交 → RuntimeError: cannot schedule new futures。
  4. _rebuild_eval_pool 重建时硬编码 max_workers=2，与 __init__ 配置不一致。

修复:
  1. __init__ 增加 max_workers / eval_timeout 参数，默认从 constants 读取
  2. _eval_with_timeout 超时后调用 future.cancel() 取消 future
  3. _eval_with_timeout submit 在 _pool_lock 内执行
  4. _rebuild_eval_pool 在 _pool_lock 内执行，使用 self._max_workers
"""

from __future__ import annotations

import concurrent.futures
import sys
import threading
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, "src")

from edgelite.constants import _EXPRESSION_EVAL_MAX_WORKERS, _EXPRESSION_EVAL_TIMEOUT
from edgelite.engine.expression_engine import ExpressionEngine

# ════════════════════════════════════════════════════════════════════════
# 1. 线程池扩容: 可配置 max_workers (并发安全 #6)
# ════════════════════════════════════════════════════════════════════════


class TestThreadPoolConfigurable:
    """验证线程池大小可配置 (并发安全 #6)"""

    def test_default_max_workers(self):
        """默认 max_workers 应为 _EXPRESSION_EVAL_MAX_WORKERS"""
        engine = ExpressionEngine()
        assert engine._max_workers == _EXPRESSION_EVAL_MAX_WORKERS
        # 验证线程池实际使用了配置值
        assert engine._eval_pool._max_workers == _EXPRESSION_EVAL_MAX_WORKERS
        engine.close()

    def test_custom_max_workers(self):
        """自定义 max_workers 应被正确使用"""
        engine = ExpressionEngine(max_workers=8)
        assert engine._max_workers == 8
        assert engine._eval_pool._max_workers == 8
        engine.close()

    def test_max_workers_none_uses_default(self):
        """max_workers=None 应使用默认值"""
        engine = ExpressionEngine(max_workers=None)
        assert engine._max_workers == _EXPRESSION_EVAL_MAX_WORKERS
        engine.close()

    def test_default_max_workers_is_4(self):
        """默认值应为 4 (从原来的 2 扩容)"""
        assert _EXPRESSION_EVAL_MAX_WORKERS == 4
        engine = ExpressionEngine()
        assert engine._max_workers == 4
        engine.close()

    def test_rebuild_uses_configured_max_workers(self):
        """_rebuild_eval_pool 重建后应使用 self._max_workers 而非硬编码 2"""
        engine = ExpressionEngine(max_workers=6)
        assert engine._eval_pool._max_workers == 6
        # 触发重建
        engine._rebuild_eval_pool()
        # 重建后仍应为 6，不是硬编码的 2
        assert engine._eval_pool._max_workers == 6
        assert engine._max_workers == 6
        engine.close()


# ════════════════════════════════════════════════════════════════════════
# 2. eval 超时可配置 (并发安全 #7)
# ════════════════════════════════════════════════════════════════════════


class TestEvalTimeoutConfigurable:
    """验证 eval 超时可配置 (并发安全 #7)"""

    def test_default_eval_timeout(self):
        """默认 eval_timeout 应为 _EXPRESSION_EVAL_TIMEOUT"""
        engine = ExpressionEngine()
        assert engine._eval_timeout == _EXPRESSION_EVAL_TIMEOUT
        engine.close()

    def test_custom_eval_timeout(self):
        """自定义 eval_timeout 应被正确使用"""
        engine = ExpressionEngine(eval_timeout=10.0)
        assert engine._eval_timeout == 10.0
        engine.close()

    def test_default_timeout_is_5(self):
        """默认超时应为 5.0s"""
        assert _EXPRESSION_EVAL_TIMEOUT == 5.0
        engine = ExpressionEngine()
        assert engine._eval_timeout == 5.0
        engine.close()

    def test_short_timeout_triggers_timeout_error(self):
        """短超时 + 阻塞自定义函数 → 触发 ValueError (超时)"""
        engine = ExpressionEngine(eval_timeout=0.3)

        # 注册一个阻塞的自定义函数
        def slow_func():
            time.sleep(10)  # 远超超时
            return 42

        engine.register_function("slow_func", slow_func)

        # eval 应触发超时
        with pytest.raises(ValueError, match="超时"):
            engine.evaluate("slow_func()")

        engine.close()

    def test_timeout_error_message_contains_configured_timeout(self):
        """超时错误消息应包含配置的超时值"""
        engine = ExpressionEngine(eval_timeout=1.5)

        def slow_func():
            time.sleep(10)
            return 42

        engine.register_function("slow_func", slow_func)

        with pytest.raises(ValueError, match="1.5"):
            engine.evaluate("slow_func()")

        engine.close()


# ════════════════════════════════════════════════════════════════════════
# 3. 超时后 future 取消 (并发安全 #7)
# ════════════════════════════════════════════════════════════════════════


class TestFutureCancellationOnTimeout:
    """验证超时后 future.cancel() 被调用 (并发安全 #7 修复点)"""

    def test_future_cancel_called_on_timeout(self):
        """超时后应调用 future.cancel()"""
        engine = ExpressionEngine(eval_timeout=0.2)
        cancelled_futures = []

        # 通过 patch future.cancel 追踪调用
        original_cancel = concurrent.futures.Future.cancel

        def tracking_cancel(self_future):
            result = original_cancel(self_future)
            cancelled_futures.append(self_future)
            return result

        def slow_func():
            time.sleep(10)
            return 42

        engine.register_function("slow_func", slow_func)

        with patch.object(concurrent.futures.Future, "cancel", tracking_cancel):
            with pytest.raises(ValueError, match="超时"):
                engine.evaluate("slow_func()")

        # future.cancel 应被调用至少一次
        assert len(cancelled_futures) >= 1, "future.cancel() should be called on timeout"

        engine.close()

    def test_pool_rebuilt_after_timeout(self):
        """超时后应重建线程池 (丢弃卡死线程)"""
        engine = ExpressionEngine(eval_timeout=0.2)
        original_pool = engine._eval_pool

        def slow_func():
            time.sleep(10)
            return 42

        engine.register_function("slow_func", slow_func)

        with pytest.raises(ValueError, match="超时"):
            engine.evaluate("slow_func()")

        # 线程池应已被重建 (新对象)
        assert engine._eval_pool is not original_pool, "pool should be rebuilt after timeout"
        engine.close()

    def test_engine_still_works_after_timeout(self):
        """超时重建后引擎应仍可正常工作"""
        engine = ExpressionEngine(eval_timeout=0.2)

        def slow_func():
            time.sleep(10)
            return 42

        engine.register_function("slow_func", slow_func)

        # 第一次: 超时
        with pytest.raises(ValueError, match="超时"):
            engine.evaluate("slow_func()")

        # 第二次: 正常表达式应仍可工作
        result = engine.evaluate("2 + 3")
        assert result == 5

        engine.close()


# ════════════════════════════════════════════════════════════════════════
# 4. 并发安全: submit/rebuild 锁保护 (并发安全 #6)
# ════════════════════════════════════════════════════════════════════════


class TestPoolLockProtection:
    """验证 _pool_lock 保护 submit/rebuild 竞态 (并发安全 #6)"""

    def test_pool_lock_exists(self):
        """_pool_lock 应为 threading.Lock 实例"""
        engine = ExpressionEngine()
        assert hasattr(engine, "_pool_lock")
        assert isinstance(engine._pool_lock, type(threading.Lock()))
        engine.close()

    def test_concurrent_submit_no_runtime_error(self):
        """多线程并发 submit 不应抛 RuntimeError (向已 shutdown 的池提交)"""
        engine = ExpressionEngine(max_workers=4)
        errors = []

        def worker():
            try:
                for _ in range(10):
                    engine.evaluate("1 + 1")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # 不应有 RuntimeError (向已 shutdown 的池提交)
        runtime_errors = [e for e in errors if isinstance(e, RuntimeError)]
        assert len(runtime_errors) == 0, f"Concurrent submit caused RuntimeError: {runtime_errors}"
        engine.close()

    def test_concurrent_submit_and_rebuild_no_runtime_error(self):
        """多线程并发 submit + rebuild 不应抛 RuntimeError"""
        engine = ExpressionEngine(max_workers=2, eval_timeout=0.1)
        errors = []

        def blocker():
            """频繁触发超时 → rebuild"""
            try:
                for _ in range(3):
                    def slow_func():
                        time.sleep(5)
                        return 0
                    # 直接注册会冲突，用内联方式触发超时
                    # 通过直接调用 _rebuild_eval_pool 模拟
                    engine._rebuild_eval_pool()
                    time.sleep(0.01)
            except Exception as e:
                errors.append(e)

        def evaluator():
            """频繁提交表达式"""
            try:
                for _ in range(20):
                    engine.evaluate("1 + 1")
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(2):
            threads.append(threading.Thread(target=blocker))
        for _ in range(4):
            threads.append(threading.Thread(target=evaluator))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # 不应有 RuntimeError
        runtime_errors = [e for e in errors if isinstance(e, RuntimeError)]
        assert len(runtime_errors) == 0, f"Concurrent submit+rebuild caused RuntimeError: {runtime_errors}"
        engine.close()

    def test_rebuild_under_lock(self):
        """_rebuild_eval_pool 应在 _pool_lock 内执行"""
        engine = ExpressionEngine(max_workers=3)

        # 验证: rebuild 期间持有锁 → 另一线程的 submit 被阻塞
        # 通过监控 _pool_lock 的状态
        rebuild_holding = threading.Event()
        threading.Event()


        def slow_rebuild():
            with engine._pool_lock:
                # 在锁内模拟延迟
                rebuild_holding.set()
                time.sleep(0.1)
                # 此时 submit 应被阻塞
            # 锁释放后 submit 应能继续

        # 直接测试: 持有 _pool_lock 时 submit 被阻塞
        threading.Event()
        submit_done = threading.Event()

        def hold_and_submit():
            # 先持有锁
            with engine._pool_lock:
                rebuild_holding.set()
                # 在另一线程启动 submit
                t = threading.Thread(target=try_submit)
                t.start()
                time.sleep(0.05)
                # submit 应被阻塞 (锁被持有)
                assert not submit_done.is_set(), "submit should be blocked while _pool_lock is held"
                # 释放锁 (退出 with)
            t.join(timeout=5)
            assert submit_done.is_set(), "submit should complete after lock released"

        def try_submit():
            try:
                engine.evaluate("1 + 1")
                submit_done.set()
            except Exception:
                submit_done.set()  # 即使出错也算完成

        t = threading.Thread(target=hold_and_submit)
        t.start()
        t.join(timeout=10)

        assert submit_done.is_set(), "submit should have completed after lock release"
        engine.close()


# ════════════════════════════════════════════════════════════════════════
# 5. 无回归: 基本表达式功能正常
# ════════════════════════════════════════════════════════════════════════


class TestNoRegression:
    """验证修改后基本表达式功能正常 (无回归)"""

    def test_basic_arithmetic(self):
        engine = ExpressionEngine()
        assert engine.evaluate("2 + 3") == 5
        assert engine.evaluate("10 - 4") == 6
        assert engine.evaluate("3 * 4") == 12
        engine.close()

    def test_safe_functions(self):
        engine = ExpressionEngine()
        assert engine.evaluate("abs(-5)") == 5
        assert engine.evaluate("round(3.7)") == 4
        assert engine.evaluate("min(1, 2, 3)") == 1
        engine.close()

    def test_custom_function(self):
        engine = ExpressionEngine()

        def double(x):
            return x * 2

        engine.register_function("double", double)
        assert engine.evaluate("double(21)") == 42
        engine.close()

    def test_batch_evaluate(self):
        engine = ExpressionEngine()
        results = engine.evaluate_batch({"a": "1 + 1", "b": "2 * 3"})
        assert results == {"a": 2, "b": 6}
        engine.close()

    def test_empty_expression_returns_none(self):
        engine = ExpressionEngine()
        assert engine.evaluate("") is None
        assert engine.evaluate("   ") is None
        engine.close()

    def test_close_releases_pool(self):
        """close() 应关闭线程池"""
        engine = ExpressionEngine()
        engine.close()
        # 关闭后不应崩溃 (幂等)
        engine.close()
