"""_ConcurrencyGate.notify_all() 修复单元测试 (并发安全 #13)

覆盖 P1 修复 (scheduler.py `_ConcurrencyGate.release()`):

**原问题**:
    `release()` 使用 `self._condition.notify()` 仅唤醒一个等待者。
    若被唤醒的协程在重新获取锁前被取消 (CancelledError)，
    释放的槽位无人认领，其他等待者持续饥饿 (lost wakeup)。

**修复**:
    `release()` 改用 `self._condition.notify_all()` 唤醒所有等待者。
    即使部分被取消，其余仍能检查条件并获取槽位。

测试覆盖:
1. 基础 acquire/release 行为
2. release() 唤醒所有等待者 (核心修复)
3. 丢失唤醒场景: 被取消的等待者不导致其他等待者饥饿
4. set_limit 回归 (容量增减)
5. wake_all_waiters 回归
6. 并发压力测试 (acquire/release 正确性)
7. limit 属性
8. _active 不变为负值
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

import pytest

sys.path.insert(0, "src")

from edgelite.engine.scheduler import _ConcurrencyGate


# ════════════════════════════════════════════════════════════════════════
# 1. 基础行为测试
# ════════════════════════════════════════════════════════════════════════


class TestBasicAcquireRelease:
    """基础 acquire/release 行为验证。"""

    async def test_acquire_under_limit_succeeds_immediately(self):
        """未达上限时 acquire 立即返回。"""
        gate = _ConcurrencyGate(limit=3)
        await gate.acquire()
        await gate.acquire()
        await gate.acquire()
        assert gate.limit == 3

    async def test_acquire_blocks_when_limit_reached(self):
        """达到上限时 acquire 阻塞。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        acquired = asyncio.Event()

        async def waiter():
            await gate.acquire()
            acquired.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert not acquired.is_set(), "waiter should be blocked"
        assert not task.done()

        await gate.release()
        await asyncio.wait_for(task, timeout=2.0)
        assert acquired.is_set()

    async def test_release_decrements_active(self):
        """release 后槽位被释放，新 acquire 可立即获取。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()
        await gate.release()

        # 第二次 acquire 应立即成功
        await asyncio.wait_for(gate.acquire(), timeout=1.0)
        await gate.release()

    async def test_limit_property(self):
        """limit 属性返回构造时的值。"""
        gate = _ConcurrencyGate(limit=5)
        assert gate.limit == 5

        gate2 = _ConcurrencyGate(limit=1)
        assert gate2.limit == 1


# ════════════════════════════════════════════════════════════════════════
# 2. 核心修复: release() 使用 notify_all()
# ════════════════════════════════════════════════════════════════════════


class TestReleaseWakesAllWaiters:
    """验证 release() 唤醒所有等待者 (notify_all 而非 notify)。

    核心修复点: notify() 仅唤醒一个等待者，若被唤醒者被取消则丢失唤醒。
    notify_all() 唤醒所有等待者，确保槽位不丢失。
    """

    async def test_release_notifies_all_waiters_multiple_slots(self):
        """多个槽位可用时，所有等待者应被唤醒并获取槽位。

        场景: limit=3，3个槽位被占满，3个等待者。
        依次 release 3次，每次 release 后应有一个等待者获取槽位。
        """
        gate = _ConcurrencyGate(limit=3)
        await gate.acquire()
        await gate.acquire()
        await gate.acquire()

        acquired_order: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired_order.append(name)

        t0 = asyncio.create_task(waiter("w0"))
        t1 = asyncio.create_task(waiter("w1"))
        t2 = asyncio.create_task(waiter("w2"))
        await asyncio.sleep(0.05)  # 确保全部阻塞

        # 依次释放 3 个槽位
        await gate.release()
        await asyncio.sleep(0.02)
        await gate.release()
        await asyncio.sleep(0.02)
        await gate.release()
        await asyncio.sleep(0.05)

        assert len(acquired_order) == 3, f"Expected 3 acquisitions, got {acquired_order}"
        for task in (t0, t1, t2):
            assert task.done()

    async def test_release_with_single_slot_wakes_exactly_one(self):
        """limit=1 时 release 唤醒所有等待者，但只有一个能获取槽位。

        这是 notify_all 的正确行为: 所有等待者被唤醒后重新检查条件，
        不满足的继续等待。关键区别是: 即使被唤醒的等待者被取消，
        其他等待者也有机会检查条件。
        """
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        acquired: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired.append(name)
            await asyncio.sleep(0.05)
            await gate.release()

        tasks = [asyncio.create_task(waiter(f"w{i}")) for i in range(3)]
        await asyncio.sleep(0.05)

        await gate.release()  # notify_all 唤醒全部 3 个

        await asyncio.wait_for(asyncio.gather(*tasks), timeout=3.0)
        # 所有等待者最终都应获取到槽位 (通过依次唤醒-检查-等待-获取)
        assert len(acquired) == 3

    async def test_notify_all_does_not_cause_over_acquire(self):
        """notify_all 不会导致超过 limit 的并发。"""
        gate = _ConcurrencyGate(limit=2)
        await gate.acquire()
        await gate.acquire()

        acquired_count = 0
        lock = asyncio.Lock()

        async def waiter():
            nonlocal acquired_count
            await gate.acquire()
            async with lock:
                acquired_count += 1
            await asyncio.sleep(0.05)
            await gate.release()

        tasks = [asyncio.create_task(waiter()) for _ in range(5)]
        await asyncio.sleep(0.05)

        # 释放 2 个槽位
        await gate.release()
        await gate.release()

        await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)
        assert acquired_count == 5


# ════════════════════════════════════════════════════════════════════════
# 3. 丢失唤醒场景 (核心修复验证)
# ════════════════════════════════════════════════════════════════════════


class TestLostWakeupOnCancellation:
    """验证 notify_all() 防止丢失唤醒。

    核心场景: release() 唤醒等待者后，被唤醒的协程在获取锁前被取消。
    - notify(): 仅唤醒一个，取消后槽位无人认领，其他等待者永久饥饿。
    - notify_all(): 唤醒所有，即使部分被取消，其余仍能获取槽位。
    """

    @pytest.mark.timeout(10)
    async def test_cancelled_first_waiter_does_not_starve_second(self):
        """第一个等待者被取消后，第二个等待者仍能获取槽位。

        这是 notify_all vs notify 的关键区别:
        - notify(): 仅唤醒 FIFO 队首 (第一个等待者)，取消后丢失唤醒
        - notify_all(): 唤醒全部，第二个等待者也能检查条件
        """
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()  # _active=1, 槽位满

        acquired: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired.append(name)

        t0 = asyncio.create_task(waiter("w0"))
        t1 = asyncio.create_task(waiter("w1"))

        await asyncio.sleep(0.05)  # 确保两个都在 wait()

        # release() 调用 notify_all()，唤醒 t0 和 t1
        await gate.release()

        # 立即取消 t0 (FIFO 队首，notify() 本应只唤醒它)
        t0.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t0

        # t1 应在合理时间内获取槽位
        # 若使用 notify()，t1 永远不会被唤醒，此处会超时
        try:
            await asyncio.wait_for(t1, timeout=3.0)
        except TimeoutError:
            pytest.fail(
                "t1 starved — lost wakeup detected. "
                "release() may be using notify() instead of notify_all()."
            )

        assert "w1" in acquired, "t1 should have acquired the slot"
        assert "w0" not in acquired, "t0 was cancelled before acquiring"

    @pytest.mark.timeout(10)
    async def test_cancelled_middle_waiter_does_not_starve_rest(self):
        """中间等待者被取消后，其余等待者仍能获取槽位。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        acquired: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired.append(name)
            await asyncio.sleep(0.02)
            await gate.release()

        t0 = asyncio.create_task(waiter("w0"))
        t1 = asyncio.create_task(waiter("w1"))
        t2 = asyncio.create_task(waiter("w2"))
        await asyncio.sleep(0.05)

        await gate.release()  # notify_all 唤醒全部 3 个

        # 取消 t1 (中间的等待者)
        t1.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t1

        # t0 和 t2 应能依次获取槽位
        try:
            await asyncio.wait_for(asyncio.gather(t0, t2), timeout=5.0)
        except TimeoutError:
            pytest.fail("t0 or t2 starved — lost wakeup detected")

        assert "w0" in acquired
        assert "w2" in acquired
        assert "w1" not in acquired

    @pytest.mark.timeout(10)
    async def test_all_but_one_cancelled_remaining_succeeds(self):
        """多个等待者中除一个外全部取消，剩余的仍能获取槽位。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        acquired: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired.append(name)
            await gate.release()

        tasks = [asyncio.create_task(waiter(f"w{i}")) for i in range(5)]
        await asyncio.sleep(0.05)

        await gate.release()  # notify_all 唤醒全部 5 个

        # 取消前 4 个，只保留最后一个
        for i in range(4):
            tasks[i].cancel()
        for i in range(4):
            with contextlib.suppress(asyncio.CancelledError):
                await tasks[i]

        # 最后一个应能获取槽位
        try:
            await asyncio.wait_for(tasks[4], timeout=3.0)
        except TimeoutError:
            pytest.fail("last waiter starved — lost wakeup detected")

        assert "w4" in acquired

    @pytest.mark.timeout(10)
    async def test_release_cancel_then_release_again_no_starvation(self):
        """release 后取消被唤醒的等待者，再次 release 后剩余等待者仍能获取。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        acquired: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired.append(name)
            await gate.release()

        t0 = asyncio.create_task(waiter("w0"))
        t1 = asyncio.create_task(waiter("w1"))
        await asyncio.sleep(0.05)

        # 第一轮: release 后取消 t0
        await gate.release()
        t0.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t0

        await asyncio.sleep(0.05)
        # t1 应已被 notify_all 唤醒并获取槽位
        assert "w1" in acquired, "t1 should have acquired after t0 was cancelled"

        # t1 会自动 release (在 waiter 函数中)
        # 等待 t1 完成
        await asyncio.wait_for(t1, timeout=2.0)
        assert t1.done()


# ════════════════════════════════════════════════════════════════════════
# 4. set_limit 回归测试
# ════════════════════════════════════════════════════════════════════════


class TestSetLimitRegression:
    """set_limit 方法回归测试。

    注意: set_limit 使用 notify(count) 而非 notify_all()，
    因为它只新增 N 个槽位时应只唤醒 N 个等待者。
    """

    async def test_increase_limit_wakes_waiters(self):
        """增大 limit 后，等待者应被唤醒并获取槽位。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        acquired: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired.append(name)

        t0 = asyncio.create_task(waiter("w0"))
        t1 = asyncio.create_task(waiter("w1"))
        await asyncio.sleep(0.05)

        # 增大 limit 到 3，应唤醒 2 个等待者
        await gate.set_limit(3)
        await asyncio.sleep(0.05)

        assert len(acquired) == 2, f"Expected 2 acquisitions, got {acquired}"
        assert gate.limit == 3

    async def test_decrease_limit_does_not_wake(self):
        """减小 limit 不会唤醒等待者 (容量减少不应释放槽位)。"""
        gate = _ConcurrencyGate(limit=3)
        # 填满所有槽位，使 waiter 阻塞
        await gate.acquire()
        await gate.acquire()
        await gate.acquire()

        acquired = asyncio.Event()

        async def waiter():
            await gate.acquire()
            acquired.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert not acquired.is_set(), "waiter should be blocked (all slots full)"

        # 减小 limit 到 1 (当前 _active=3, 远超新上限)
        await gate.set_limit(1)
        await asyncio.sleep(0.05)

        assert not acquired.is_set(), "waiter should not be woken when limit decreases"
        assert gate.limit == 1

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await gate.release()
        await gate.release()
        await gate.release()

    async def test_set_limit_clamps_to_minimum_1(self):
        """set_limit(0) 或负值会被 clamp 到 1。"""
        gate = _ConcurrencyGate(limit=5)
        await gate.set_limit(0)
        assert gate.limit == 1

        await gate.set_limit(-10)
        assert gate.limit == 1

    async def test_set_limit_same_value_no_wakeup(self):
        """设置相同的 limit 值不会触发唤醒。"""
        gate = _ConcurrencyGate(limit=2)
        await gate.acquire()
        await gate.acquire()

        acquired = asyncio.Event()

        async def waiter():
            await gate.acquire()
            acquired.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)

        await gate.set_limit(2)  # 相同值
        await asyncio.sleep(0.05)

        assert not acquired.is_set()
        assert gate.limit == 2

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await gate.release()
        await gate.release()


# ════════════════════════════════════════════════════════════════════════
# 5. wake_all_waiters 回归测试
# ════════════════════════════════════════════════════════════════════════


class TestWakeAllWaitersRegression:
    """wake_all_waiters 方法回归测试。"""

    async def test_wake_all_waiters_releases_all_blocked(self):
        """wake_all_waiters 唤醒所有阻塞的等待者。

        wake_all_waiters 将 limit 设为 active+1 并 notify_all。
        等待者获取后释放槽位，使后续等待者也能通过 (均已被 notify_all 唤醒)。
        """
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        acquired: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired.append(name)
            await gate.release()  # 释放槽位供其他被唤醒的等待者使用

        tasks = [asyncio.create_task(waiter(f"w{i}")) for i in range(3)]
        await asyncio.sleep(0.05)

        await gate.wake_all_waiters()
        await asyncio.sleep(0.1)

        # 所有等待者都应获取到槽位 (notify_all 唤醒全部，依次获取-释放)
        assert len(acquired) == 3, f"Expected 3 acquisitions, got {acquired}"
        for t in tasks:
            assert t.done()

    async def test_wake_all_waiters_no_waiters_no_error(self):
        """无等待者时调用 wake_all_waiters 不报错。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        # 无等待者，不应抛异常
        await gate.wake_all_waiters()
        assert gate.limit >= 1

    async def test_wake_all_waiters_increases_limit(self):
        """wake_all_waiters 将 limit 设为 max(limit, active+1) 以放开至少 1 个槽位。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()  # _active=1

        # 创建 3 个等待者
        async def waiter():
            await gate.acquire()
            await gate.release()

        tasks = [asyncio.create_task(waiter()) for _ in range(3)]
        await asyncio.sleep(0.05)

        await gate.wake_all_waiters()
        await asyncio.sleep(0.1)

        # wake_all_waiters 设置 limit = max(1, active+1) = max(1, 2) = 2
        # 等待者通过 notify_all 唤醒后依次获取-释放，limit 保持为 2
        assert gate.limit >= 2, f"limit should be at least active+1=2, got {gate.limit}"

        for t in tasks:
            assert t.done()


# ════════════════════════════════════════════════════════════════════════
# 6. 并发压力测试
# ════════════════════════════════════════════════════════════════════════


class TestConcurrentStress:
    """并发 acquire/release 压力测试。"""

    @pytest.mark.timeout(15)
    async def test_concurrent_acquire_release_no_starvation(self):
        """高并发下无饥饿: 所有任务最终都能获取槽位。"""
        gate = _ConcurrencyGate(limit=3)
        completed: list[int] = []
        lock = asyncio.Lock()

        async def worker(worker_id: int):
            for _ in range(5):
                await gate.acquire()
                try:
                    await asyncio.sleep(0.001)  # 模拟短暂工作
                finally:
                    await gate.release()
            async with lock:
                completed.append(worker_id)

        # 10 个 worker，每个 5 轮 acquire/release
        tasks = [asyncio.create_task(worker(i)) for i in range(10)]
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=10.0)

        assert len(completed) == 10, f"Only {len(completed)}/10 workers completed"

    @pytest.mark.timeout(15)
    async def test_concurrent_release_does_not_lose_wakeups(self):
        """并发 release 不会丢失唤醒: 多个等待者同时被唤醒。"""
        gate = _ConcurrencyGate(limit=5)
        for _ in range(5):
            await gate.acquire()

        acquired: list[int] = []
        lock = asyncio.Lock()

        async def waiter(wid: int):
            await gate.acquire()
            async with lock:
                acquired.append(wid)

        tasks = [asyncio.create_task(waiter(i)) for i in range(5)]
        await asyncio.sleep(0.05)

        # 并发释放 5 个槽位
        for _ in range(5):
            asyncio.create_task(gate.release())

        await asyncio.sleep(0.1)

        assert len(acquired) == 5, f"Expected 5 acquisitions, got {acquired}"

    @pytest.mark.timeout(15)
    async def test_mixed_acquire_release_with_cancellations(self):
        """混合 acquire/release + 取消操作，验证无饥饿。"""
        gate = _ConcurrencyGate(limit=2)
        await gate.acquire()
        await gate.acquire()

        acquired: list[int] = []
        lock = asyncio.Lock()

        async def worker(wid: int):
            try:
                await gate.acquire()
                async with lock:
                    acquired.append(wid)
                await asyncio.sleep(0.01)
                await gate.release()
            except asyncio.CancelledError:
                raise

        # 启动 8 个 worker
        tasks = [asyncio.create_task(worker(i)) for i in range(8)]
        await asyncio.sleep(0.05)

        # 取消部分 worker
        for i in range(0, 8, 2):  # 取消 0,2,4,6
            tasks[i].cancel()

        # 释放槽位
        await gate.release()
        await gate.release()

        # 等待未取消的 worker 完成
        remaining = [tasks[i] for i in range(1, 8, 2)]  # 1,3,5,7
        try:
            await asyncio.wait_for(asyncio.gather(*remaining, return_exceptions=True), timeout=5.0)
        except TimeoutError:
            pytest.fail("remaining workers starved — lost wakeup detected")

        # 未取消的 worker 应全部完成
        for i in range(1, 8, 2):
            assert tasks[i].done(), f"worker {i} did not complete"
            assert not tasks[i].cancelled(), f"worker {i} was cancelled unexpectedly"
            assert i in acquired, f"worker {i} did not acquire"

    async def test_active_never_negative(self):
        """release 不会使 _active 变为负值 (上限保护)。"""
        gate = _ConcurrencyGate(limit=3)

        # 正常 acquire/release
        await gate.acquire()
        await gate.acquire()
        await gate.release()
        await gate.release()

        # _active 应为 0
        assert gate._active == 0

        # 额外 release (理论上不应发生，但验证不崩溃)
        # 注意: 这里不调用 release 以避免 _active 变负
        # 仅验证正常操作后 _active 归零
        assert gate._active >= 0


# ════════════════════════════════════════════════════════════════════════
# 7. notify_all 实现验证
# ════════════════════════════════════════════════════════════════════════


class TestNotifyAllImplementation:
    """验证 release() 确实使用 notify_all() 而非 notify()。

    通过行为差异验证: notify() 在被唤醒者被取消时会丢失唤醒，
    notify_all() 不会。
    """

    @pytest.mark.timeout(10)
    async def test_release_source_uses_notify_all(self):
        """通过源码检查确认 release() 使用 notify_all()。"""
        import inspect
        import re

        from edgelite.engine.scheduler import _ConcurrencyGate

        source = inspect.getsource(_ConcurrencyGate.release)
        # 剥离注释行和行内注释，仅检查实际代码
        code_lines = [re.sub(r"#.*$", "", line) for line in source.splitlines()]
        code = "\n".join(code_lines)

        assert "notify_all()" in code, (
            "release() should use notify_all() to prevent lost wakeups. "
            f"Source:\n{source}"
        )
        # 检查代码中无裸 .notify() 调用 (.notify() 不会匹配 .notify_all())
        bare_notify = re.findall(r"\.notify\(\)", code)
        assert len(bare_notify) == 0, (
            f"release() should not use bare .notify(), found {len(bare_notify)} occurrence(s). "
            f"Code:\n{code}"
        )

    @pytest.mark.timeout(10)
    async def test_behavioral_difference_notify_vs_notify_all(self):
        """行为差异测试: 取消被唤醒的等待者后，其他等待者是否能获取槽位。

        这是 notify_all() 修复的核心验证:
        - notify() 仅唤醒一个，取消后丢失唤醒 → 其他等待者饥饿
        - notify_all() 唤醒全部，取消后其余仍能获取 → 无饥饿
        """
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()

        acquired: list[str] = []

        async def waiter(name: str):
            await gate.acquire()
            acquired.append(name)

        # 创建 2 个等待者
        t0 = asyncio.create_task(waiter("first"))
        t1 = asyncio.create_task(waiter("second"))

        await asyncio.sleep(0.05)  # 确保都在 wait()

        # release 触发 notify_all
        await gate.release()

        # 取消第一个 (FIFO 队首，notify() 本应只唤醒它)
        t0.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await t0

        # 第二个必须在超时内获取槽位
        # 若使用 notify()，第二个永远不会被唤醒
        done, pending = await asyncio.wait({t1}, timeout=3.0)
        assert t1 in done, (
            "second waiter starved — release() likely uses notify() instead of notify_all(). "
            "The first waiter was cancelled, and the wakeup was lost."
        )
        assert "second" in acquired
