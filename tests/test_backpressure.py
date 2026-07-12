"""流量背压控制器测试 - 配置校验/入队丢弃/槽位限流/状态机

覆盖 engine/backpressure.py：
- BackpressureState 枚举
- BackpressureConfig: 阈值范围校验/跨字段 ordering 校验
- QueueMetrics: 默认值
- BackpressureController: enqueue/dequeue/背压丢弃/acquire_slot/release_slot/set_priority/get_priority
"""

from __future__ import annotations

import pytest

from edgelite.engine.backpressure import (
    BackpressureConfig,
    BackpressureController,
    BackpressureState,
    QueueMetrics,
)


class TestBackpressureState:
    def test_values(self):
        assert BackpressureState.NORMAL.value == "normal"
        assert BackpressureState.WARNING.value == "warning"
        assert BackpressureState.BACKPRESSURE.value == "backpressure"
        assert BackpressureState.RECOVERING.value == "recovering"


class TestBackpressureConfig:
    def test_defaults(self):
        cfg = BackpressureConfig()
        assert cfg.max_queue_size == 1000
        assert cfg.warning_threshold == 0.7
        assert cfg.backpressure_threshold == 0.9
        assert cfg.recovery_threshold == 0.3
        assert cfg.max_concurrent_requests == 100

    def test_valid_custom_config(self):
        cfg = BackpressureConfig(
            warning_threshold=0.6,
            backpressure_threshold=0.8,
            recovery_threshold=0.2,
        )
        assert cfg.warning_threshold == 0.6

    def test_threshold_out_of_range_high(self):
        with pytest.raises(ValueError, match="必须在"):
            BackpressureConfig(warning_threshold=1.5)

    def test_threshold_out_of_range_negative(self):
        with pytest.raises(ValueError, match="必须在"):
            BackpressureConfig(recovery_threshold=-0.1)

    def test_recovery_must_be_less_than_warning(self):
        with pytest.raises(ValueError, match="阈值关系"):
            BackpressureConfig(recovery_threshold=0.7, warning_threshold=0.7, backpressure_threshold=0.9)

    def test_warning_must_be_less_than_backpressure(self):
        with pytest.raises(ValueError, match="阈值关系"):
            BackpressureConfig(recovery_threshold=0.3, warning_threshold=0.9, backpressure_threshold=0.9)

    def test_boundary_zero_and_one(self):
        """0 和 1 是合法边界值"""
        cfg = BackpressureConfig(recovery_threshold=0.0, warning_threshold=0.5, backpressure_threshold=1.0)
        assert cfg.recovery_threshold == 0.0
        assert cfg.backpressure_threshold == 1.0


class TestQueueMetrics:
    def test_defaults(self):
        m = QueueMetrics()
        assert m.depth == 0
        assert m.max_depth == 0
        assert m.enqueued_total == 0
        assert m.dequeued_total == 0
        assert m.dropped_total == 0
        assert m.backpressure_triggered == 0
        assert m.last_backpressure_at is None


class TestBackpressureControllerEnqueue:
    @pytest.mark.asyncio
    async def test_enqueue_creates_queue(self):
        ctrl = BackpressureController()
        result = await ctrl.enqueue("q1", "item1")
        assert result is True
        assert "q1" in ctrl._queues

    @pytest.mark.asyncio
    async def test_enqueue_success(self):
        ctrl = BackpressureController()
        assert await ctrl.enqueue("q1", "item1") is True
        assert await ctrl.enqueue("q1", "item2") is True

    @pytest.mark.asyncio
    async def test_enqueue_drop_on_backpressure(self):
        """队列满时入队被拒绝"""
        ctrl = BackpressureController(
            BackpressureConfig(max_queue_size=2, backpressure_threshold=0.9, warning_threshold=0.7)
        )
        await ctrl.enqueue("q1", "item1")  # depth=0 → ok
        await ctrl.enqueue("q1", "item2")  # depth=1, ratio=0.5 → ok
        result = await ctrl.enqueue("q1", "item3")  # depth=2, ratio=1.0 >= 0.9 → drop
        assert result is False
        metrics = ctrl._metrics["q1"]
        assert metrics.dropped_total >= 1

    @pytest.mark.asyncio
    async def test_dequeue_returns_item(self):
        ctrl = BackpressureController()
        await ctrl.enqueue("q1", "item1")
        item = await ctrl.dequeue("q1", timeout=0.5)
        assert item == "item1"

    @pytest.mark.asyncio
    async def test_dequeue_empty_returns_none(self):
        ctrl = BackpressureController()
        await ctrl.enqueue("q1", "item1")
        await ctrl.dequeue("q1", timeout=0.5)
        item = await ctrl.dequeue("q1", timeout=0.1)
        assert item is None

    @pytest.mark.asyncio
    async def test_dequeue_nonexistent_queue(self):
        ctrl = BackpressureController()
        assert await ctrl.dequeue("nonexistent", timeout=0.1) is None

    @pytest.mark.asyncio
    async def test_dequeue_all(self):
        ctrl = BackpressureController()
        await ctrl.enqueue("q1", "a")
        await ctrl.enqueue("q1", "b")
        await ctrl.enqueue("q1", "c")
        items = await ctrl.dequeue_all("q1")
        assert len(items) == 3
        assert set(items) == {"a", "b", "c"}


class TestBackpressureControllerSlots:
    @pytest.mark.asyncio
    async def test_acquire_and_release_slot(self):
        ctrl = BackpressureController(BackpressureConfig(max_concurrent_requests=2))
        await ctrl.start()
        try:
            s1 = await ctrl.acquire_slot()
            assert s1 is True
            s2 = await ctrl.acquire_slot()
            assert s2 is True
            # 第三个应该被拒绝或等待
            ctrl.release_slot()  # sync method
            ctrl.release_slot()
        finally:
            await ctrl.stop()

    @pytest.mark.asyncio
    async def test_release_without_acquire_ignored(self):
        """未获取槽位时 release 不应导致计数溢出"""
        ctrl = BackpressureController(BackpressureConfig(max_concurrent_requests=2))
        await ctrl.start()
        try:
            ctrl.release_slot()  # sync method, 不应抛异常
            assert ctrl._acquired_count == 0  # 不应为负
        finally:
            await ctrl.stop()


class TestBackpressureControllerPriority:
    @pytest.mark.asyncio
    async def test_set_and_get_priority(self):
        ctrl = BackpressureController()
        await ctrl.enqueue("q1", "item")
        await ctrl.set_priority("q1", 5)
        assert await ctrl.get_priority("q1") == 5

    @pytest.mark.asyncio
    async def test_get_priority_default_zero(self):
        ctrl = BackpressureController()
        assert await ctrl.get_priority("nonexistent") == 0
