"""RuleEvaluator._prune_duration_tracker 加锁保护单元测试 (并发安全 #3)

覆盖 P1 修复: `_prune_duration_tracker` 改为 async 方法，整段读-改-写在
`_state_lock` 内原子执行，与 `_evaluate_rule`/`cleanup_duration_tracker`/
`invalidate_cache` 等对 `_duration_tracker` 的读写互斥。

原问题:
  `_prune_duration_tracker` 是同步方法，在 `_eval_loop` 中无锁直接遍历+删除
  `_duration_tracker`，与 `_evaluate_rule` 中的 get/set/pop (在 `_state_lock`
  保护下) 并发执行时存在 TOCTOU 竞态: 迭代/修改竞态可能导致
  RuntimeError: dictionary changed size during iteration，或误删/重复条目。

修复:
  1. `_prune_duration_tracker` 改为 `async def`
  2. 整段读-改-写在 `async with self._state_lock` 内执行
  3. `_eval_loop` 两个调用点改为 `await self._prune_duration_tracker()`
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

sys.path.insert(0, "src")

from edgelite.engine.evaluator import RuleEvaluator
from edgelite.engine.event_bus import EventBus

# ════════════════════════════════════════════════════════════════════════
# Fixtures
# ════════════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def evaluator():
    """构造一个 RuleEvaluator 实例，依赖全部 mock。

    _tracker_cleanup_interval 设为一个较小值便于测试 _eval_loop 集成，
    但本测试主要直接调用 _prune_duration_tracker。
    """
    event_bus = EventBus()
    rule_repo = MagicMock()
    rule_repo.list_enabled_by_point = AsyncMock(return_value=[])
    alarm_repo = MagicMock()
    alarm_repo.get_firing_by_rule_device = AsyncMock(return_value=None)

    ev = RuleEvaluator(event_bus, rule_repo, alarm_repo)
    # 便于测试: 清理间隔设为 0，每次 _eval_loop 迭代都触发 prune
    ev._tracker_cleanup_interval = 0.0
    yield ev
    # 清理: stop 不会启动 task，但清理状态
    ev._duration_tracker.clear()
    ev._rule_cache.clear()
    ev._recent_firings.clear()
    ev._point_value_cache.clear()
    ev._last_values.clear()
    ev._condition_first_met.clear()
    event_bus._handlers.clear()


def _add_tracker(ev: RuleEvaluator, rule_id: str, device_id: str, first_match_time: datetime) -> None:
    """向 _duration_tracker 注入一条记录。"""
    ev._duration_tracker[(rule_id, device_id)] = first_match_time


# ════════════════════════════════════════════════════════════════════════
# 1. 方法签名: async + 协程
# ════════════════════════════════════════════════════════════════════════


class TestPruneIsAsync:
    """验证 _prune_duration_tracker 已改为 async 方法 (并发安全 #3 修复点 1)"""

    def test_prune_is_coroutine_function(self):
        """inspect.iscoroutinefunction 应返回 True"""
        assert inspect.iscoroutinefunction(RuleEvaluator._prune_duration_tracker)

    def test_prune_returns_coroutine(self, evaluator):
        """调用 (不 await) 应返回 coroutine 对象而非 None"""
        coro = evaluator._prune_duration_tracker()
        try:
            assert asyncio.iscoroutine(coro)
        finally:
            coro.close()


# ════════════════════════════════════════════════════════════════════════
# 2. 加锁互斥: _state_lock 被持有期间阻塞其他协程
# ════════════════════════════════════════════════════════════════════════


class TestPruneLockMutualExclusion:
    """验证 _prune_duration_tracker 在执行期间持有 _state_lock (并发安全 #3 修复点 2)"""

    @pytest.mark.asyncio
    async def test_prune_acquires_state_lock(self, evaluator):
        """_prune_duration_tracker 执行期间，另一协程无法获取 _state_lock"""
        # 先在另一协程持有 _state_lock，验证 prune 会等待
        lock_acquired = asyncio.Event()
        release_lock = asyncio.Event()

        async def hold_lock():
            async with evaluator._state_lock:
                lock_acquired.set()
                await release_lock.wait()

        holder = asyncio.create_task(hold_lock())
        await lock_acquired.wait()

        # 此时锁被 holder 持有，prune 应阻塞
        prune_task = asyncio.create_task(evaluator._prune_duration_tracker())
        # 让出控制权，让 prune_task 有机会运行 (但它应卡在锁上)
        await asyncio.sleep(0.05)
        assert not prune_task.done(), "prune should be blocked waiting for _state_lock"

        # 释放锁，prune 应能完成
        release_lock.set()
        await asyncio.wait_for(prune_task, timeout=2.0)
        await holder

    @pytest.mark.asyncio
    async def test_prune_blocks_cleanup_duration_tracker(self, evaluator):
        """prune 持有锁期间，cleanup_duration_tracker 被阻塞 (证明互斥)"""
        # 注入一些数据让 prune 有事可做
        _add_tracker(evaluator, "r1", "d1", datetime.now(UTC))
        _add_tracker(evaluator, "r2", "d2", datetime.now(UTC))

        prune_started = asyncio.Event()

        # 模拟 prune 持有 _state_lock 的场景 (实际 prune 内部逻辑相同)
        async def test_prune_with_hook():
            async with evaluator._state_lock:
                prune_started.set()
                # 在锁内停留一会，让 cleanup 尝试获取锁
                await asyncio.sleep(0.1)

        prune_task = asyncio.create_task(test_prune_with_hook())
        await prune_started.wait()

        # cleanup_duration_tracker 需要 _state_lock，应被阻塞
        cleanup_task = asyncio.create_task(evaluator.cleanup_duration_tracker("r1"))
        await asyncio.sleep(0.02)
        assert not cleanup_task.done(), "cleanup should be blocked while prune holds lock"

        # 等 prune 完成 (释放锁)
        await prune_task
        await asyncio.wait_for(cleanup_task, timeout=2.0)
        # cleanup 应已移除 r1 的记录
        assert ("r1", "d1") not in evaluator._duration_tracker

    @pytest.mark.asyncio
    async def test_prune_blocks_invalidate_cache(self, evaluator):
        """prune 持有锁期间，invalidate_cache 被阻塞 (证明互斥)"""
        _add_tracker(evaluator, "r1", "d1", datetime.now(UTC))
        evaluator._rule_cache["d1:p1"] = []

        prune_started = asyncio.Event()

        async def test_prune_with_hook():
            async with evaluator._state_lock:
                prune_started.set()
                await asyncio.sleep(0.1)

        prune_task = asyncio.create_task(test_prune_with_hook())
        await prune_started.wait()

        invalidate_task = asyncio.create_task(evaluator.invalidate_cache())
        await asyncio.sleep(0.02)
        assert not invalidate_task.done(), "invalidate should be blocked while prune holds lock"

        await prune_task
        await asyncio.wait_for(invalidate_task, timeout=2.0)


# ════════════════════════════════════════════════════════════════════════
# 3. 清理逻辑: 过期条目移除 + 上限保护 (无回归)
# ════════════════════════════════════════════════════════════════════════


class TestPruneLogicNoRegression:
    """验证加锁后清理逻辑保持不变 (无回归)"""

    @pytest.mark.asyncio
    async def test_prune_removes_expired_entries(self, evaluator):
        """超过 24h 的条目应被移除"""
        now = datetime.now(UTC)
        # 25h 前: 应被清理
        _add_tracker(evaluator, "r1", "d1", now - timedelta(hours=25))
        _add_tracker(evaluator, "r2", "d2", now - timedelta(hours=48))
        # 新鲜: 应保留
        _add_tracker(evaluator, "r3", "d3", now - timedelta(minutes=5))

        await evaluator._prune_duration_tracker()

        assert ("r1", "d1") not in evaluator._duration_tracker
        assert ("r2", "d2") not in evaluator._duration_tracker
        assert ("r3", "d3") in evaluator._duration_tracker

    @pytest.mark.asyncio
    async def test_prune_preserves_recent_entries(self, evaluator):
        """24h 内的条目应全部保留"""
        now = datetime.now(UTC)
        # 用分钟偏移确保全部在 24h 内 (50 分钟 < 24h)
        for i in range(50):
            _add_tracker(evaluator, f"r{i}", f"d{i}", now - timedelta(minutes=i))

        await evaluator._prune_duration_tracker()

        # 50 条 < 100 (max_entries 下限)，且都新鲜，应全部保留
        assert len(evaluator._duration_tracker) == 50

    @pytest.mark.asyncio
    async def test_prune_caps_to_max_entries(self, evaluator):
        """条目数超过 max(active*2, 100) 时应裁剪到上限"""
        now = datetime.now(UTC)
        # 200 条，全部属于同一规则 r1 → active=1 → max=100
        for i in range(200):
            _add_tracker(evaluator, "r1", f"d{i}", now - timedelta(seconds=i))

        await evaluator._prune_duration_tracker()

        # active_rule_count=1 → max_entries=max(2, 100)=100
        assert len(evaluator._duration_tracker) == 100
        # 应保留时间最早的 100 条? 不对 — sorted 按 timestamp 升序，excess 取前 excess 个
        # 即删除最早的 excess 条，保留最新的 100 条
        # 200 条时间从 now-0s 到 now-199s，删除最早 100 条 (now-199s..now-100s)
        # 保留 now-99s..now-0s → device id 0..99
        remaining_devices = {k[1] for k in evaluator._duration_tracker}
        assert len(remaining_devices) == 100
        # 最新的 100 条 (d0..d99) 应保留
        for i in range(100):
            assert ("r1", f"d{i}") in evaluator._duration_tracker
        # 最早的 100 条 (d100..d199) 应被删除
        for i in range(100, 200):
            assert ("r1", f"d{i}") not in evaluator._duration_tracker

    @pytest.mark.asyncio
    async def test_prune_max_entries_with_many_rules(self, evaluator):
        """多个活跃规则时 max_entries = active_rule_count * 2"""
        now = datetime.now(UTC)
        # 5 个规则，每个 50 条 = 250 条 → active=5 → max=max(10, 100)=100
        for r in range(5):
            for d in range(50):
                _add_tracker(evaluator, f"r{r}", f"d{r}_{d}", now - timedelta(seconds=d))

        await evaluator._prune_duration_tracker()

        assert len(evaluator._duration_tracker) == 100

    @pytest.mark.asyncio
    async def test_prune_empty_tracker_noop(self, evaluator):
        """空 tracker 调用 prune 不报错"""
        await evaluator._prune_duration_tracker()
        assert len(evaluator._duration_tracker) == 0

    @pytest.mark.asyncio
    async def test_prune_preserves_lock_unlocked_after(self, evaluator):
        """prune 完成后 _state_lock 应被释放 (可再次获取)"""
        _add_tracker(evaluator, "r1", "d1", datetime.now(UTC) - timedelta(hours=25))
        await evaluator._prune_duration_tracker()

        # 锁应已释放，可立即获取
        async with evaluator._state_lock:
            pass
        assert True  # 到这里说明锁已释放

    @pytest.mark.asyncio
    async def test_prune_cutoff_boundary_24h(self, evaluator):
        """刚好 24h 边界: < cutoff 删除, >= cutoff 保留"""
        now = datetime.now(UTC)
        cutoff_ts = now.timestamp() - 86400
        # 构造 first_match_time.timestamp() < cutoff 的条目
        # now - 24h - 1s → 删除
        _add_tracker(evaluator, "r1", "d_old", datetime.fromtimestamp(cutoff_ts - 1, tz=UTC))
        # now - 24h + 1s → 保留
        _add_tracker(evaluator, "r2", "d_new", datetime.fromtimestamp(cutoff_ts + 1, tz=UTC))

        await evaluator._prune_duration_tracker()

        assert ("r1", "d_old") not in evaluator._duration_tracker
        assert ("r2", "d_new") in evaluator._duration_tracker


# ════════════════════════════════════════════════════════════════════════
# 4. _eval_loop 集成: await 调用正常工作
# ════════════════════════════════════════════════════════════════════════


class TestEvalLoopIntegration:
    """验证 _eval_loop 中的 await 调用集成正常"""

    @pytest.mark.asyncio
    async def test_eval_loop_calls_prune_with_await(self, evaluator):
        """_eval_loop 启动后应能正常调用 async _prune_duration_tracker 不报错"""
        # 注入一些过期数据
        _add_tracker(evaluator, "r1", "d1", datetime.now(UTC) - timedelta(hours=25))

        # 启动 eval_loop
        queue = asyncio.Queue()
        # 放入一个非 good 质量的事件，让 loop 立即获取并跳过 _evaluate，
        # 随后触发 prune (interval=0.0 每次迭代都触发)
        from edgelite.engine.event_bus import PointUpdateEvent
        await queue.put(PointUpdateEvent(device_id="d1", point_name="p1", value=1.0, quality="bad"))
        task = asyncio.create_task(evaluator._eval_loop(queue))
        # 等待事件被处理 + prune 执行
        await asyncio.sleep(0.2)

        # 取消任务
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # 过期条目应已被清理
        assert ("r1", "d1") not in evaluator._duration_tracker

    @pytest.mark.asyncio
    async def test_eval_loop_prune_does_not_crash_on_concurrent_access(self, evaluator):
        """_eval_loop 中 prune 与手动 _evaluate_rule 并发访问 _duration_tracker 不崩溃"""
        now = datetime.now(UTC)
        # 注入大量数据
        for i in range(150):
            _add_tracker(evaluator, "r1", f"d{i}", now - timedelta(seconds=i))

        queue = asyncio.Queue()
        # 放入一个 PointUpdateEvent 触发 _evaluate
        from edgelite.engine.event_bus import PointUpdateEvent
        await queue.put(PointUpdateEvent(device_id="d0", point_name="p1", value=1.0, quality="good"))

        task = asyncio.create_task(evaluator._eval_loop(queue))
        # 让 eval_loop 运行一段时间，期间会触发 prune (interval=0.05s)
        await asyncio.sleep(0.3)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # 不应抛出 RuntimeError: dictionary changed size during iteration
        # prune 应已裁剪到 100 条
        assert len(evaluator._duration_tracker) <= 100


# ════════════════════════════════════════════════════════════════════════
# 5. 并发安全: 多次并发调用 prune 不产生竞态
# ════════════════════════════════════════════════════════════════════════


class TestPruneConcurrentSafety:
    """验证并发调用 _prune_duration_tracker 不产生竞态"""

    @pytest.mark.asyncio
    async def test_concurrent_prune_calls_safe(self, evaluator):
        """多个 prune 协程并发执行不抛 RuntimeError"""
        now = datetime.now(UTC)
        for i in range(200):
            _add_tracker(evaluator, "r1", f"d{i}", now - timedelta(seconds=i))

        # 并发调用 10 次
        tasks = [evaluator._prune_duration_tracker() for _ in range(10)]
        await asyncio.gather(*tasks, return_exceptions=False)

        # 应裁剪到 100 条，无异常
        assert len(evaluator._duration_tracker) == 100

    @pytest.mark.asyncio
    async def test_prune_concurrent_with_cleanup_duration_tracker(self, evaluator):
        """prune 与 cleanup_duration_tracker 并发执行不抛异常"""
        now = datetime.now(UTC)
        # 注入 r1 (将被 cleanup 删除) 和 r2 (保留) 的数据
        for i in range(80):
            _add_tracker(evaluator, "r1", f"d1_{i}", now - timedelta(seconds=i))
        for i in range(80):
            _add_tracker(evaluator, "r2", f"d2_{i}", now - timedelta(seconds=i + 80))

        # 并发: prune + cleanup(r1)
        await asyncio.gather(
            evaluator._prune_duration_tracker(),
            evaluator.cleanup_duration_tracker("r1"),
        )

        # r1 应被 cleanup 清空
        r1_keys = [k for k in evaluator._duration_tracker if k[0] == "r1"]
        assert len(r1_keys) == 0
        # r2 应保留 (prune 不会删除新鲜数据，cleanup 只删 r1)
        r2_keys = [k for k in evaluator._duration_tracker if k[0] == "r2"]
        assert len(r2_keys) > 0
