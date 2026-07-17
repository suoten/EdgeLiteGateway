"""采集调度器 (CollectScheduler) 综合单元测试。

覆盖 _ConcurrencyGate/DevicePriority/CollectStats/DeviceQualityStats/CollectScheduler
的所有公共方法与内部协程: 采集循环、统计更新、自适应频率、看门狗、缓存回写、AI推理。

所有外部依赖均被 mock，不产生真实网络或数据库调用。
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.base import PointValue  # noqa: E402
from edgelite.engine.scheduler import (  # noqa: E402
    _PRIORITY_INTERVAL_MULTIPLIER,
    _PRIORITY_SEMAPHORE_WEIGHT,
    DEFAULT_TIMEOUT,
    CollectScheduler,
    CollectStats,
    DevicePriority,
    DeviceQualityStats,
    _ConcurrencyGate,
)


def _patch_sleep_cancel(after: int = 1):
    """打补丁使 scheduler 模块内 asyncio.sleep 第 after 次调用抛出 CancelledError。"""
    real_sleep = asyncio.sleep
    counter = {"n": 0}

    async def fake_sleep(delay, *args, **kwargs):
        counter["n"] += 1
        if counter["n"] >= after:
            raise asyncio.CancelledError()
        await real_sleep(0)

    return patch("edgelite.engine.scheduler.asyncio.sleep", fake_sleep)


async def _run_collect_once(scheduler, device_id, driver, points, interval=5):
    """运行 _collect_loop 恰好一次迭代后通过 CancelledError 退出。"""
    with _patch_sleep_cancel(after=1):
        task = asyncio.create_task(scheduler._collect_loop(device_id, driver, points, interval))
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def mock_event_bus():
    bus = AsyncMock()
    bus.publish = AsyncMock()
    return bus


@pytest.fixture
def mock_influx():
    influx = AsyncMock()
    influx.write_points_batch = AsyncMock(return_value=True)
    influx.check_health = AsyncMock(return_value=True)
    return influx


@pytest.fixture
def mock_cache():
    cache = AsyncMock()
    cache.restore_from_sqlite = AsyncMock(return_value=0)
    cache.get_pending_from_ring_buffer = AsyncMock(return_value=[])
    cache.get_cached_records = AsyncMock(return_value=[])
    cache.get_ring_buffer_stats = MagicMock(return_value=None)
    cache.add_to_cache = AsyncMock()
    cache.mark_synced = AsyncMock()
    cache.mark_failed = AsyncMock()
    cache.delete_cached = AsyncMock()
    return cache


@pytest.fixture
def mock_circuit_manager():
    mgr = AsyncMock()
    mgr.get_all_status = AsyncMock(return_value=[])
    mgr.reset_device = AsyncMock(return_value=True)
    mgr.remove_breaker = AsyncMock(return_value=True)

    async def _cwp(device_id, func, *args, fallback=None, **kwargs):
        return await func()

    mgr.call_with_protection = _cwp
    return mgr


@pytest.fixture
def scheduler(mock_event_bus, mock_influx, mock_cache, mock_circuit_manager):
    with (
        patch("edgelite.engine.circuit_breaker.get_circuit_breaker_manager", return_value=mock_circuit_manager),
        patch("edgelite.config.get_config", side_effect=Exception("no config")),
    ):
        return CollectScheduler(mock_event_bus, mock_influx, mock_cache)


@pytest.fixture
def scheduler_no_cache(mock_event_bus, mock_influx, mock_circuit_manager):
    with (
        patch("edgelite.engine.circuit_breaker.get_circuit_breaker_manager", return_value=mock_circuit_manager),
        patch("edgelite.config.get_config", side_effect=Exception("no config")),
    ):
        return CollectScheduler(mock_event_bus, mock_influx, None)


def _make_driver(read_values=None, plugin_name="modbus", protocols=("modbus",)):
    driver = MagicMock()
    driver.plugin_name = plugin_name
    driver.supported_protocols = protocols
    driver.read_points = AsyncMock(return_value=read_values if read_values is not None else {})
    return driver


class TestConcurrencyGate:
    """_ConcurrencyGate 并发门控行为验证。"""

    async def test_acquire_release_basic(self):
        """未达上限时 acquire 立即返回。"""
        gate = _ConcurrencyGate(limit=2)
        await gate.acquire()
        await gate.acquire()
        await gate.release()
        await gate.acquire()
        await gate.release()
        await gate.release()

    async def test_acquire_blocks_when_limit_reached(self):
        """达到上限时 acquire 阻塞，release 后唤醒。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()
        acquired = asyncio.Event()

        async def waiter():
            await gate.acquire()
            acquired.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        assert not acquired.is_set()
        await gate.release()
        await asyncio.wait_for(task, timeout=2.0)
        assert acquired.is_set()
        await gate.release()

    async def test_release_notifies_all_waiters(self):
        """release 使用 notify_all 唤醒等待者。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()
        done = []

        async def waiter(name):
            await gate.acquire()
            done.append(name)

        tasks = [asyncio.create_task(waiter(f"w{i}")) for i in range(3)]
        await asyncio.sleep(0.02)
        assert done == []
        await gate.release()
        await asyncio.sleep(0.02)
        assert len(done) == 1
        for _ in range(2):
            await gate.release()
            await asyncio.sleep(0.01)
        for t in tasks:
            await asyncio.wait_for(t, timeout=2.0)
        assert len(done) == 3

    async def test_set_limit_increase(self):
        """set_limit 增大容量时唤醒等待者。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()
        freed = asyncio.Event()

        async def waiter():
            await gate.acquire()
            freed.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.02)
        assert not freed.is_set()
        await gate.set_limit(5)
        await asyncio.wait_for(task, timeout=2.0)
        assert freed.is_set()
        assert gate.limit == 5
        await gate.release()
        await gate.release()

    async def test_set_limit_decrease(self):
        """set_limit 减小容量时 limit 更新。"""
        gate = _ConcurrencyGate(limit=5)
        await gate.set_limit(1)
        assert gate.limit == 1

    async def test_set_limit_minimum_one(self):
        """set_limit 最小为1。"""
        gate = _ConcurrencyGate(limit=3)
        await gate.set_limit(0)
        assert gate.limit == 1
        await gate.set_limit(-5)
        assert gate.limit == 1

    async def test_wake_all_waiters(self):
        """wake_all_waiters 提高限制并唤醒等待者。"""
        gate = _ConcurrencyGate(limit=1)
        await gate.acquire()
        passed = []

        async def waiter(name):
            await gate.acquire()
            passed.append(name)

        task = asyncio.create_task(waiter("w0"))
        await asyncio.sleep(0.02)
        assert passed == []
        await gate.wake_all_waiters()
        await asyncio.wait_for(task, timeout=2.0)
        assert len(passed) == 1
        await gate.release()
        await gate.release()

    def test_limit_property(self):
        """limit 属性返回当前配置值。"""
        gate = _ConcurrencyGate(limit=7)
        assert gate.limit == 7


class TestPriorityConstants:
    """设备优先级枚举与权重/倍率常量验证。"""

    def test_priority_values(self):
        """P0-P3 优先级值递增。"""
        assert DevicePriority.P0.value == 0
        assert DevicePriority.P3.value == 3

    def test_interval_multiplier(self):
        """优先级倍率正确。"""
        assert _PRIORITY_INTERVAL_MULTIPLIER[DevicePriority.P0] == 0.5
        assert _PRIORITY_INTERVAL_MULTIPLIER[DevicePriority.P1] == 0.75
        assert _PRIORITY_INTERVAL_MULTIPLIER[DevicePriority.P2] == 1.0
        assert _PRIORITY_INTERVAL_MULTIPLIER[DevicePriority.P3] == 2.0

    def test_semaphore_weight_sum(self):
        """优先级信号量权重之和为1.0。"""
        assert abs(sum(_PRIORITY_SEMAPHORE_WEIGHT.values()) - 1.0) < 1e-9

    def test_all_priorities_have_weight(self):
        """每个优先级都有对应权重。"""
        for p in DevicePriority:
            assert p in _PRIORITY_SEMAPHORE_WEIGHT


class TestDataclasses:
    """数据类默认值验证。"""

    def test_collect_stats_defaults(self):
        """CollectStats 默认值正确。"""
        s = CollectStats()
        assert s.device_id == ""
        assert s.avg_latency_ms == 0.0
        assert s.total_calls == 0
        assert s.timeout_count == 0

    def test_device_quality_stats_defaults(self):
        """DeviceQualityStats 默认值正确。"""
        qs = DeviceQualityStats(device_id="dev1")
        assert qs.success_count == 0
        assert qs.error_count == 0
        assert qs.total_count == 0
        assert qs.error_rate == 0.0


class TestSchedulerInit:
    """CollectScheduler 初始化与配置加载验证。"""

    def test_init_defaults(self, scheduler):
        """无配置时使用默认值。"""
        assert scheduler._max_concurrent_collects == 50
        assert scheduler._error_rate_threshold == 0.1
        assert scheduler._WATCHDOG_INTERVAL == 30
        assert scheduler._WATCHDOG_STALE_CYCLES == 3
        assert scheduler._WATCHDOG_RESTART_CYCLES == 10
        assert scheduler._concurrency_gate is None
        assert scheduler._priority_semaphores == {}

    def test_init_no_cache(self, scheduler_no_cache):
        """无缓存管理器时 _cache 为 None。"""
        assert scheduler_no_cache._cache is None

    def test_init_loads_config(self, mock_event_bus, mock_influx, mock_cache, mock_circuit_manager):
        """配置存在时从 scheduler 配置加载参数。"""
        sc = SimpleNamespace(
            max_concurrent_collects=20,
            error_rate_threshold=0.2,
            watchdog_interval=15,
            watchdog_stale_cycles=5,
            watchdog_restart_cycles=20,
        )
        cfg = SimpleNamespace(scheduler=sc)
        with (
            patch("edgelite.engine.circuit_breaker.get_circuit_breaker_manager", return_value=mock_circuit_manager),
            patch("edgelite.config.get_config", return_value=cfg),
        ):
            s = CollectScheduler(mock_event_bus, mock_influx, mock_cache)
        assert s._max_concurrent_collects == 20
        assert s._error_rate_threshold == 0.2
        assert s._WATCHDOG_INTERVAL == 15

    def test_init_partial_config(self, mock_event_bus, mock_influx, mock_cache, mock_circuit_manager):
        """配置部分缺失时使用 getattr 默认值。"""
        cfg = SimpleNamespace(scheduler=SimpleNamespace(max_concurrent_collects=10))
        with (
            patch("edgelite.engine.circuit_breaker.get_circuit_breaker_manager", return_value=mock_circuit_manager),
            patch("edgelite.config.get_config", return_value=cfg),
        ):
            s = CollectScheduler(mock_event_bus, mock_influx, mock_cache)
        assert s._max_concurrent_collects == 10
        assert s._error_rate_threshold == 0.1


class TestStartStopCollect:
    """采集任务启动、停止与全量停止验证。"""

    async def test_start_collect_creates_task(self, scheduler):
        """start_collect 创建采集任务并初始化内部状态。"""
        driver = _make_driver({"temp": 25.0})
        await scheduler.start_collect("dev1", driver, [{"name": "temp"}], collect_interval=5)
        try:
            assert await scheduler.get_task_count() == 1
            assert "dev1" in await scheduler.get_active_devices()
            assert scheduler._device_priorities["dev1"] == DevicePriority.P2
            assert scheduler._concurrency_gate is not None
            assert len(scheduler._priority_semaphores) == 4
            assert scheduler._cache_flush_task is not None
            assert scheduler._watchdog_task is not None
        finally:
            await scheduler.stop_all()

    async def test_start_collect_priority_string(self, scheduler):
        """通过字符串指定优先级 P0。"""
        driver = _make_driver()
        await scheduler.start_collect("dev1", driver, [{"name": "p"}], priority="P0")
        try:
            assert scheduler._device_priorities["dev1"] == DevicePriority.P0
            assert scheduler._adaptive_state["dev1"]["priority_multiplier"] == 0.5
        finally:
            await scheduler.stop_all()

    async def test_start_collect_invalid_priority(self, scheduler):
        """无效优先级字符串回退为 P2。"""
        driver = _make_driver()
        await scheduler.start_collect("dev1", driver, [{"name": "p"}], priority="P9")
        try:
            assert scheduler._device_priorities["dev1"] == DevicePriority.P2
        finally:
            await scheduler.stop_all()

    async def test_start_collect_enum_priority(self, scheduler):
        """直接传入 DevicePriority 枚举。"""
        driver = _make_driver()
        await scheduler.start_collect("dev1", driver, [{"name": "p"}], collect_interval=10, priority=DevicePriority.P3)
        try:
            assert scheduler._device_priorities["dev1"] == DevicePriority.P3
            assert scheduler._adaptive_state["dev1"]["effective_interval"] == 20
        finally:
            await scheduler.stop_all()

    async def test_start_collect_replaces_existing(self, scheduler):
        """对已存在的设备再次 start_collect 会先停止旧任务。"""
        driver = _make_driver({"temp": 1.0})
        await scheduler.start_collect("dev1", driver, [{"name": "temp"}])
        old_task = scheduler._tasks["dev1"]
        await scheduler.start_collect("dev1", driver, [{"name": "temp"}])
        try:
            assert scheduler._tasks["dev1"] is not old_task
            assert old_task.done()
            assert await scheduler.get_task_count() == 1
        finally:
            await scheduler.stop_all()

    async def test_stop_collect_cleans_state(self, scheduler):
        """stop_collect 清理所有设备相关内部状态。"""
        driver = _make_driver()
        await scheduler.start_collect("dev1", driver, [{"name": "p"}])
        await scheduler.stop_collect("dev1")
        assert "dev1" not in scheduler._tasks
        assert "dev1" not in scheduler._device_info
        assert "dev1" not in scheduler._device_priorities
        assert "dev1" not in scheduler._adaptive_state
        assert await scheduler.get_task_count() == 0

    async def test_stop_collect_calls_remove_breaker(self, scheduler, mock_circuit_manager):
        """stop_collect 调用熔断器管理器移除设备熔断器。"""
        driver = _make_driver()
        await scheduler.start_collect("dev1", driver, [{"name": "p"}])
        await scheduler.stop_collect("dev1")
        mock_circuit_manager.remove_breaker.assert_awaited_with("dev1")

    async def test_stop_collect_nonexistent(self, scheduler):
        """停止不存在的设备不报错。"""
        await scheduler.stop_collect("nonexistent")

    async def test_stop_all(self, scheduler):
        """stop_all 停止所有任务并清理后台协程。"""
        driver = _make_driver()
        await scheduler.start_collect("dev1", driver, [{"name": "p"}])
        await scheduler.start_collect("dev2", driver, [{"name": "p"}])
        await scheduler.stop_all()
        assert await scheduler.get_task_count() == 0
        assert scheduler._watchdog_task is None
        assert scheduler._cache_flush_task is None


class TestGettersSetters:
    """状态查询与参数设置方法验证。"""

    async def test_get_active_devices(self, scheduler):
        """get_active_devices 返回活跃设备ID列表。"""
        driver = _make_driver()
        await scheduler.start_collect("dev1", driver, [{"name": "p"}])
        try:
            assert await scheduler.get_active_devices() == ["dev1"]
        finally:
            await scheduler.stop_all()

    async def test_get_task_count(self, scheduler):
        """get_task_count 返回活跃任务数。"""
        assert await scheduler.get_task_count() == 0

    def test_set_preprocessor(self, scheduler):
        """set_preprocessor 设置预处理器。"""
        pp = MagicMock()
        scheduler.set_preprocessor(pp)
        assert scheduler._preprocessor is pp

    async def test_get_collect_stats_empty(self, scheduler):
        assert await scheduler.get_collect_stats() == {}

    async def test_get_last_values_empty(self, scheduler):
        assert await scheduler.get_last_values() == {}
        assert await scheduler.get_last_values("dev1") == {}

    async def test_get_device_quality_stats_empty(self, scheduler):
        assert await scheduler.get_device_quality_stats() == {}

    async def test_set_max_concurrent_rebuilds_semaphores(self, scheduler):
        """set_max_concurrent 动态调整限流并重建优先级信号量。"""
        driver = _make_driver()
        await scheduler.start_collect("dev1", driver, [{"name": "p"}])
        try:
            await scheduler.set_max_concurrent(100)
            assert scheduler._max_concurrent_collects == 100
            assert scheduler._concurrency_gate.limit == 100
            for p in DevicePriority:
                expected = max(1, int(100 * _PRIORITY_SEMAPHORE_WEIGHT[p]))
                assert scheduler._priority_semaphores[p].limit == expected
        finally:
            await scheduler.stop_all()

    async def test_set_max_concurrent_minimum_one(self, scheduler):
        await scheduler.set_max_concurrent(0)
        assert scheduler._max_concurrent_collects == 1

    async def test_set_max_concurrent_no_gate(self, scheduler_no_cache):
        await scheduler_no_cache.set_max_concurrent(10)
        assert scheduler_no_cache._max_concurrent_collects == 10

    def test_set_error_rate_threshold_clamp(self, scheduler):
        """set_error_rate_threshold 将阈值限制在 [0, 1]。"""
        scheduler.set_error_rate_threshold(0.5)
        assert scheduler._error_rate_threshold == 0.5
        scheduler.set_error_rate_threshold(-0.1)
        assert scheduler._error_rate_threshold == 0.0
        scheduler.set_error_rate_threshold(1.5)
        assert scheduler._error_rate_threshold == 1.0

    async def test_get_circuit_breaker_status(self, scheduler, mock_circuit_manager):
        mock_circuit_manager.get_all_status = AsyncMock(return_value=[{"state": "closed"}])
        assert await scheduler.get_circuit_breaker_status() == [{"state": "closed"}]

    async def test_reset_circuit_breaker(self, scheduler, mock_circuit_manager):
        mock_circuit_manager.reset_device = AsyncMock(return_value=True)
        assert await scheduler.reset_circuit_breaker("dev1") is True
        mock_circuit_manager.reset_device.assert_awaited_with("dev1")


class TestDriverHotPlug:
    """load_driver / unload_driver 驱动热插拔验证。"""

    async def test_load_driver_success(self, scheduler):
        """成功加载匹配协议的 DriverPlugin 子类。"""
        from edgelite.drivers.base import DriverPlugin

        mock_module = MagicMock()

        class FakePlugin(DriverPlugin):
            supported_protocols = ("opcua",)

        mock_module.FakePlugin = FakePlugin
        with patch("importlib.import_module", return_value=mock_module):
            result = await scheduler.load_driver("opcua", "edgelite.drivers.opcua")
        assert result is True
        assert scheduler._driver_classes["opcua"] is FakePlugin

    async def test_load_driver_no_match(self, scheduler):
        """模块中无匹配协议的驱动类时返回 False。"""
        mock_module = MagicMock()
        mock_module.SomeClass = type("SomeClass", (), {})
        with patch("importlib.import_module", return_value=mock_module):
            assert await scheduler.load_driver("unknown", "edgelite.drivers.unknown") is False

    async def test_load_driver_exception(self, scheduler):
        """importlib 抛出异常时返回 False。"""
        with patch("importlib.import_module", side_effect=ImportError("no module")):
            assert await scheduler.load_driver("x", "edgelite.drivers.x") is False

    async def test_unload_driver_stops_devices(self, scheduler):
        """unload_driver 停止使用该协议的设备并从注册表移除。"""
        driver = _make_driver(plugin_name="modbus", protocols=("modbus",))
        await scheduler.start_collect("dev1", driver, [{"name": "p"}])
        scheduler._driver_classes["modbus"] = type("Fake", (), {})
        assert await scheduler.get_task_count() == 1
        result = await scheduler.unload_driver("modbus")
        assert result is True
        assert "modbus" not in scheduler._driver_classes
        assert await scheduler.get_task_count() == 0

    async def test_unload_driver_not_found(self, scheduler):
        """卸载未注册的协议返回 False。"""
        assert await scheduler.unload_driver("nonexistent") is False


class TestQualityScore:
    """calculate_quality_score 数据质量评分验证。"""

    async def test_no_stats(self, scheduler):
        """无统计时返回0分/N/A等级。"""
        result = await scheduler.calculate_quality_score("dev1")
        assert result == {"score": 0, "grade": "N/A", "details": {}}

    async def test_zero_total(self, scheduler):
        """total_count 为0时返回0分。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(device_id="dev1")
        result = await scheduler.calculate_quality_score("dev1")
        assert result["score"] == 0
        assert result["grade"] == "N/A"

    async def test_grade_a(self, scheduler):
        """100%成功率 + 低延迟 -> A级。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(device_id="dev1", success_count=10, total_count=10)
        scheduler._collect_stats["dev1"] = CollectStats(device_id="dev1", avg_latency_ms=50.0, total_calls=10)
        result = await scheduler.calculate_quality_score("dev1")
        assert result["grade"] == "A"
        assert result["score"] >= 90

    async def test_grade_b(self, scheduler):
        """50%成功率 + 中等延迟 -> B级。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(
            device_id="dev1", success_count=5, error_count=5, total_count=10, error_rate=0.5
        )
        scheduler._collect_stats["dev1"] = CollectStats(device_id="dev1", avg_latency_ms=200.0, total_calls=10)
        assert (await scheduler.calculate_quality_score("dev1"))["grade"] == "B"

    async def test_grade_c(self, scheduler):
        """低成功率 + 较高延迟 -> C级。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(
            device_id="dev1", success_count=3, error_count=7, total_count=10, error_rate=0.7
        )
        scheduler._collect_stats["dev1"] = CollectStats(device_id="dev1", avg_latency_ms=600.0, total_calls=10)
        assert (await scheduler.calculate_quality_score("dev1"))["grade"] == "C"

    async def test_grade_d(self, scheduler):
        """0%成功率 + 高延迟 -> D级。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(
            device_id="dev1", success_count=0, error_count=10, total_count=10, error_rate=1.0
        )
        scheduler._collect_stats["dev1"] = CollectStats(device_id="dev1", avg_latency_ms=2000.0, total_calls=10)
        result = await scheduler.calculate_quality_score("dev1")
        assert result["grade"] == "D"
        assert result["score"] >= 40

    async def test_grade_f(self, scheduler):
        """0%成功率 + 极高延迟 -> F级。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(
            device_id="dev1", success_count=0, error_count=10, total_count=10, error_rate=1.0
        )
        scheduler._collect_stats["dev1"] = CollectStats(device_id="dev1", avg_latency_ms=6000.0, total_calls=10)
        result = await scheduler.calculate_quality_score("dev1")
        assert result["grade"] == "F"
        assert result["score"] < 40

    async def test_latency_tier_500ms(self, scheduler):
        """延迟<=500ms 得25分。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(device_id="dev1", success_count=10, total_count=10)
        scheduler._collect_stats["dev1"] = CollectStats(device_id="dev1", avg_latency_ms=500.0, total_calls=10)
        assert (await scheduler.calculate_quality_score("dev1"))["details"]["latency_score"] == 25

    async def test_latency_tier_1000ms(self, scheduler):
        """延迟<=1000ms 得20分。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(device_id="dev1", success_count=10, total_count=10)
        scheduler._collect_stats["dev1"] = CollectStats(device_id="dev1", avg_latency_ms=1000.0, total_calls=10)
        assert (await scheduler.calculate_quality_score("dev1"))["details"]["latency_score"] == 20

    async def test_no_collect_stats(self, scheduler):
        """无采集延迟统计时 avg_latency 按0计算。"""
        scheduler._device_quality_stats["dev1"] = DeviceQualityStats(device_id="dev1", success_count=10, total_count=10)
        assert (await scheduler.calculate_quality_score("dev1"))["grade"] == "A"


class TestCollectLoop:
    """_collect_loop 采集循环各场景验证。"""

    async def test_successful_collect(self, scheduler, mock_influx, mock_event_bus):
        """成功采集: 写入InfluxDB + 发布事件 + 更新统计。"""
        driver = _make_driver({"temp": 25.5})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "temp"}])
        mock_influx.write_points_batch.assert_awaited_once()
        records = mock_influx.write_points_batch.call_args[0][0]
        assert len(records) == 1
        assert records[0]["value"] == 25.5
        mock_event_bus.publish.assert_awaited()
        stats = await scheduler.get_collect_stats()
        assert stats["dev1"].total_calls == 1
        assert (await scheduler.get_last_values("dev1"))["temp"] == 25.5

    async def test_collect_with_point_value(self, scheduler, mock_influx):
        """采集值为 PointValue 时正确提取 value 和 quality。"""
        pv = PointValue(value=42.0, quality="good")
        driver = _make_driver({"sensor": pv})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "sensor"}])
        records = mock_influx.write_points_batch.call_args[0][0]
        assert records[0]["value"] == 42.0
        assert records[0]["quality"] == "good"

    async def test_collect_bad_quality_none_value(self, scheduler, mock_influx):
        """PointValue quality=bad 且 value=None 时记录 bad 质量测点。"""
        pv = PointValue(value=None, quality="bad")
        driver = _make_driver({"sensor": pv})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "sensor"}])
        records = mock_influx.write_points_batch.call_args[0][0]
        assert records[0]["value"] is None
        assert records[0]["quality"] == "bad"

    async def test_collect_empty_values(self, scheduler, mock_influx):
        """采集返回空字典时不写入 InfluxDB。"""
        driver = _make_driver({})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "p"}])
        mock_influx.write_points_batch.assert_not_awaited()

    async def test_collect_circuit_fallback_sentinel(self, scheduler, mock_circuit_manager):
        """熔断器 fallback 命中哨兵时标记为采集错误。"""
        from edgelite.engine.scheduler import _CIRCUIT_FALLBACK_SENTINEL

        mock_circuit_manager.call_with_protection = AsyncMock(return_value=_CIRCUIT_FALLBACK_SENTINEL)
        driver = _make_driver()
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "p"}])
        assert (await scheduler.get_collect_stats())["dev1"].timeout_count == 1

    async def test_collect_timeout(self, scheduler, mock_circuit_manager):
        """采集超时时发布 timeout 质量事件。"""

        async def _cp(device_id, func, *args, fallback=None, **kwargs):
            raise TimeoutError()

        mock_circuit_manager.call_with_protection = _cp
        driver = _make_driver()
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "p1"}, {"name": "p2"}])
        assert (await scheduler.get_collect_stats())["dev1"].timeout_count == 1
        published = scheduler._event_bus.publish.call_args_list
        assert any(c.args[0].quality == "timeout" for c in published)

    async def test_collect_timeout_summary(self, scheduler, mock_circuit_manager):
        """超时且测点数>10时发布 __summary__ 事件。"""

        async def _cp(device_id, func, *args, fallback=None, **kwargs):
            raise TimeoutError()

        mock_circuit_manager.call_with_protection = _cp
        driver = _make_driver()
        points = [{"name": f"p{i}"} for i in range(15)]
        await _run_collect_once(scheduler, "dev1", driver, points)
        published = scheduler._event_bus.publish.call_args_list
        assert any(getattr(c.args[0], "point_name", "") == "__summary__" for c in published)

    async def test_collect_generic_error(self, scheduler, mock_circuit_manager):
        """采集发生通用异常时发布 bad 质量事件。"""

        async def _cp(device_id, func, *args, fallback=None, **kwargs):
            raise RuntimeError("driver error")

        mock_circuit_manager.call_with_protection = _cp
        driver = _make_driver()
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "p1"}])
        assert (await scheduler.get_collect_stats())["dev1"].timeout_count == 1
        published = scheduler._event_bus.publish.call_args_list
        assert any(c.args[0].quality == "bad" for c in published)

    async def test_collect_publish_failure_breaks(self, scheduler, mock_event_bus):
        """publish 失败时 break 避免异常逃出 except 块。"""
        mock_event_bus.publish = AsyncMock(side_effect=RuntimeError("queue full"))

        async def _cp(device_id, func, *args, fallback=None, **kwargs):
            raise RuntimeError("err")

        scheduler._circuit_breaker_manager.call_with_protection = _cp
        driver = _make_driver()
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "p1"}, {"name": "p2"}])
        assert (await scheduler.get_collect_stats())["dev1"].total_calls == 1

    async def test_collect_value_conversion_failure(self, scheduler, mock_influx):
        """值无法转为 float 时跳过该测点。"""
        driver = _make_driver({"good": 1.0, "bad": object()})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "good"}, {"name": "bad"}])
        records = mock_influx.write_points_batch.call_args[0][0]
        assert len(records) == 1
        assert records[0]["point_name"] == "good"

    async def test_collect_jump_threshold(self, scheduler, mock_event_bus):
        """值跳变超过阈值时质量标记为 suspect。"""
        driver = _make_driver({"temp": 100.0})
        points = [{"name": "temp", "jump_threshold": 5.0}]
        await _run_collect_once(scheduler, "dev1", driver, points)
        driver2 = _make_driver({"temp": 200.0})
        await _run_collect_once(scheduler, "dev1", driver2, points)
        published = mock_event_bus.publish.call_args_list
        assert any(getattr(c.args[0], "quality", "") == "suspect" for c in published)

    async def test_collect_out_of_range(self, scheduler, mock_event_bus):
        """值超出范围时质量标记为 out_of_range。"""
        driver = _make_driver({"temp": 200.0})
        points = [{"name": "temp", "min_value": 0.0, "max_value": 100.0}]
        await _run_collect_once(scheduler, "dev1", driver, points)
        published = mock_event_bus.publish.call_args_list
        assert any(getattr(c.args[0], "quality", "") == "out_of_range" for c in published)

    async def test_collect_preprocessor_skip(self, scheduler, mock_influx):
        """preprocessor 返回 should_report=False 时跳过测点(写入空记录列表)。"""
        pp = MagicMock()
        pp.process = MagicMock(return_value=(None, False))
        scheduler.set_preprocessor(pp)
        driver = _make_driver({"temp": 25.0})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "temp"}])
        mock_influx.write_points_batch.assert_awaited_once()
        assert len(mock_influx.write_points_batch.call_args[0][0]) == 0

    async def test_collect_preprocessor_transform(self, scheduler, mock_influx):
        """preprocessor 返回变换后的值时使用变换值。"""
        pp = MagicMock()
        pp.process = MagicMock(return_value=(99.0, True))
        scheduler.set_preprocessor(pp)
        driver = _make_driver({"temp": 25.0})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "temp"}])
        assert mock_influx.write_points_batch.call_args[0][0][0]["value"] == 99.0

    async def test_collect_cache_fallback(self, scheduler, mock_influx, mock_cache):
        """InfluxDB写入失败时数据写入缓存。"""
        mock_influx.write_points_batch = AsyncMock(return_value=False)
        driver = _make_driver({"temp": 25.0})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "temp"}])
        mock_cache.add_to_cache.assert_awaited()

    async def test_collect_cache_add_failure_isolated(self, scheduler, mock_influx, mock_cache):
        """缓存写入单条失败不影响其他记录。"""
        mock_influx.write_points_batch = AsyncMock(return_value=False)
        mock_cache.add_to_cache = AsyncMock(side_effect=RuntimeError("cache error"))
        driver = _make_driver({"a": 1.0, "b": 2.0})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "a"}, {"name": "b"}])
        assert mock_cache.add_to_cache.await_count == 2

    async def test_collect_bool_value_preserved(self, scheduler, mock_influx):
        """布尔类型值不被 round 转换。"""
        driver = _make_driver({"flag": True})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "flag"}])
        assert mock_influx.write_points_batch.call_args[0][0][0]["value"] is True

    async def test_collect_updates_quality_stats(self, scheduler):
        """采集成功后更新设备质量统计。"""
        driver = _make_driver({"temp": 25.0})
        await _run_collect_once(scheduler, "dev1", driver, [{"name": "temp"}])
        qs = await scheduler.get_device_quality_stats()
        assert qs["dev1"].total_count == 1
        assert qs["dev1"].success_count == 1


class TestUpdateStats:
    """_update_collect_stats / _update_device_quality_stats 验证。"""

    async def test_update_collect_stats(self, scheduler):
        """采集统计正确累计延迟与调用次数。"""
        await scheduler._update_collect_stats("dev1", 100.0, False)
        await scheduler._update_collect_stats("dev1", 300.0, True)
        stats = (await scheduler.get_collect_stats())["dev1"]
        assert stats.total_calls == 2
        assert stats.avg_latency_ms == 200.0
        assert stats.max_latency_ms == 300.0
        assert stats.timeout_count == 1
        assert stats.last_collect_at != ""

    async def test_quality_stats_success(self, scheduler):
        """成功采集递增 success_count。"""
        await scheduler._update_device_quality_stats("dev1", False)
        qs = (await scheduler.get_device_quality_stats())["dev1"]
        assert qs.success_count == 1
        assert qs.error_rate == 0.0

    async def test_quality_stats_error(self, scheduler):
        """错误采集递增 error_count。"""
        await scheduler._update_device_quality_stats("dev1", True)
        qs = (await scheduler.get_device_quality_stats())["dev1"]
        assert qs.error_count == 1
        assert qs.error_rate == 1.0

    async def test_quality_stats_alarm_triggered(self, scheduler):
        """错误率超阈值时发布告警事件。"""
        fake_state = SimpleNamespace(event_bus=AsyncMock())
        with patch("edgelite.app._app_state", fake_state):
            for _ in range(5):
                await scheduler._update_device_quality_stats("dev1", False)
            for _ in range(5):
                await scheduler._update_device_quality_stats("dev1", True)
            await asyncio.sleep(0.05)
        assert fake_state.event_bus.publish.await_count >= 1

    async def test_quality_stats_no_alarm(self, scheduler):
        """错误率未超阈值时不告警。"""
        fake_state = SimpleNamespace(event_bus=AsyncMock())
        with patch("edgelite.app._app_state", fake_state):
            for _ in range(10):
                await scheduler._update_device_quality_stats("dev1", False)
            await asyncio.sleep(0.02)
        fake_state.event_bus.publish.assert_not_awaited()


class TestAdaptiveInterval:
    """_adjust_adaptive_interval 自适应频率调整验证。"""

    async def test_no_state_returns_base(self, scheduler):
        """无自适应状态时返回基础间隔。"""
        assert await scheduler._adjust_adaptive_interval("dev1", False, 10) == 10

    async def test_three_successes_speed_up(self, scheduler):
        """连续3次成功后间隔缩短。"""
        scheduler._adaptive_state["dev1"] = {
            "consecutive_successes": 0,
            "consecutive_failures": 0,
            "base_interval": 10,
            "effective_interval": 10,
            "priority_multiplier": 1.0,
        }
        scheduler._device_priorities["dev1"] = DevicePriority.P2
        for _ in range(3):
            await scheduler._adjust_adaptive_interval("dev1", False, 10)
        assert scheduler._adaptive_state["dev1"]["effective_interval"] == 8

    async def test_three_failures_slow_down(self, scheduler):
        """连续3次失败后间隔翻倍。"""
        scheduler._adaptive_state["dev1"] = {
            "consecutive_successes": 0,
            "consecutive_failures": 0,
            "base_interval": 10,
            "effective_interval": 10,
            "priority_multiplier": 1.0,
        }
        scheduler._device_priorities["dev1"] = DevicePriority.P2
        for _ in range(3):
            await scheduler._adjust_adaptive_interval("dev1", True, 10)
        assert scheduler._adaptive_state["dev1"]["effective_interval"] == 20

    async def test_recovery_resets_to_base(self, scheduler):
        """失败后恢复成功时重置为基础间隔。"""
        scheduler._adaptive_state["dev1"] = {
            "consecutive_successes": 0,
            "consecutive_failures": 0,
            "base_interval": 10,
            "effective_interval": 20,
            "priority_multiplier": 1.0,
        }
        scheduler._device_priorities["dev1"] = DevicePriority.P2
        result = await scheduler._adjust_adaptive_interval("dev1", False, 10)
        assert result == 10
        assert scheduler._adaptive_state["dev1"]["effective_interval"] == 10

    async def test_failure_resets_success_streak(self, scheduler):
        """失败时重置连续成功计数。"""
        scheduler._adaptive_state["dev1"] = {
            "consecutive_successes": 2,
            "consecutive_failures": 0,
            "base_interval": 10,
            "effective_interval": 10,
            "priority_multiplier": 1.0,
        }
        scheduler._device_priorities["dev1"] = DevicePriority.P2
        await scheduler._adjust_adaptive_interval("dev1", True, 10)
        assert scheduler._adaptive_state["dev1"]["consecutive_successes"] == 0
        assert scheduler._adaptive_state["dev1"]["consecutive_failures"] == 1

    async def test_priority_applied_to_base(self, scheduler):
        """P0 优先级的 priority_base 减半。"""
        scheduler._adaptive_state["dev1"] = {
            "consecutive_successes": 0,
            "consecutive_failures": 0,
            "base_interval": 10,
            "effective_interval": 5,
            "priority_multiplier": 0.5,
        }
        scheduler._device_priorities["dev1"] = DevicePriority.P0
        for _ in range(3):
            await scheduler._adjust_adaptive_interval("dev1", False, 10)
        assert scheduler._adaptive_state["dev1"]["effective_interval"] == 4


class TestWatchdog:
    """_watchdog_loop 看门狗协程验证。"""

    async def test_watchdog_stale_warning(self, scheduler, caplog):
        """stale_cycles 在 STALE 和 RESTART 之间时记录 stale 警告。"""
        import logging
        import time

        scheduler._WATCHDOG_INTERVAL = 0
        scheduler._WATCHDOG_STALE_CYCLES = 3
        scheduler._WATCHDOG_RESTART_CYCLES = 10
        scheduler._device_info["dev1"] = (_make_driver(), [{"name": "p"}], 1)
        scheduler._last_collect_time["dev1"] = time.monotonic() - 5
        scheduler._tasks["dev1"] = MagicMock(done=MagicMock(return_value=True))
        with _patch_sleep_cancel(after=2):
            with caplog.at_level(logging.WARNING):
                with contextlib.suppress(asyncio.CancelledError):
                    await scheduler._watchdog_loop()
        assert any("marked stale" in r.message for r in caplog.records)

    async def test_watchdog_restart_stale_task(self, scheduler):
        """stale_cycles >= RESTART_CYCLES 时重启采集任务。"""
        import time

        scheduler._WATCHDOG_INTERVAL = 0
        scheduler._WATCHDOG_STALE_CYCLES = 3
        scheduler._WATCHDOG_RESTART_CYCLES = 10
        scheduler._device_info["dev1"] = (_make_driver(), [{"name": "p"}], 1)
        scheduler._last_collect_time["dev1"] = time.monotonic() - 100
        old_task = MagicMock()
        old_task.done = MagicMock(return_value=False)
        old_task.cancel = MagicMock()
        scheduler._tasks["dev1"] = old_task
        with (
            _patch_sleep_cancel(after=2),
            patch("edgelite.engine.scheduler.asyncio.wait_for", new=AsyncMock()),
        ):
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler._watchdog_loop()
        old_task.cancel.assert_called()

    async def test_watchdog_no_last_time_skips(self, scheduler):
        """无 last_collect_time 的设备被跳过。"""
        scheduler._WATCHDOG_INTERVAL = 0
        scheduler._device_info["dev1"] = (_make_driver(), [{"name": "p"}], 1)
        scheduler._tasks["dev1"] = MagicMock(done=MagicMock(return_value=True))
        with _patch_sleep_cancel(after=2):
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler._watchdog_loop()


class TestCacheFlush:
    """缓存回写各路径验证。"""

    async def test_flush_ring_buffer_success(self, scheduler, mock_cache, mock_influx):
        """RingBuffer 批量回写成功时标记已同步。"""
        mock_cache.get_pending_from_ring_buffer = AsyncMock(
            return_value=[
                {
                    "_id": 1,
                    "sqlite_id": 10,
                    "tags": {"device_id": "dev1", "point_name": "temp", "quality": "good"},
                    "fields": {"value": 25.0},
                    "timestamp": "2026-01-01T00:00:00+00:00",
                }
            ]
        )
        mock_influx.write_points_batch = AsyncMock(return_value=True)
        await scheduler._flush_from_ring_buffer()
        mock_influx.write_points_batch.assert_awaited_once()
        mock_cache.mark_synced.assert_awaited_with([1], [10])

    async def test_flush_ring_buffer_empty(self, scheduler, mock_cache, mock_influx):
        """RingBuffer 无记录时直接返回。"""
        mock_cache.get_pending_from_ring_buffer = AsyncMock(return_value=[])
        await scheduler._flush_from_ring_buffer()
        mock_influx.write_points_batch.assert_not_awaited()

    async def test_flush_ring_buffer_write_failure(self, scheduler, mock_cache, mock_influx):
        """RingBuffer 回写失败时标记失败。"""
        mock_cache.get_pending_from_ring_buffer = AsyncMock(
            return_value=[
                {
                    "_id": 1,
                    "sqlite_id": None,
                    "tags": {"device_id": "dev1", "point_name": "temp"},
                    "fields": {"value": 25.0},
                    "timestamp": None,
                }
            ]
        )
        mock_influx.write_points_batch = AsyncMock(return_value=False)
        await scheduler._flush_from_ring_buffer()
        mock_cache.mark_failed.assert_awaited_with([1])

    async def test_flush_ring_buffer_invalid(self, scheduler, mock_cache, mock_influx):
        """无效记录被跳过。"""
        mock_cache.get_pending_from_ring_buffer = AsyncMock(
            return_value=[
                {
                    "_id": 1,
                    "tags": {"device_id": "", "point_name": "temp"},
                    "fields": {"value": 25.0},
                    "timestamp": None,
                }
            ]
        )
        await scheduler._flush_from_ring_buffer()
        mock_influx.write_points_batch.assert_not_awaited()

    async def test_flush_sqlite_success(self, scheduler, mock_cache, mock_influx):
        """SQLite 回写成功时删除缓存记录。"""
        mock_cache.get_cached_records = AsyncMock(
            return_value=[
                {
                    "id": 5,
                    "tags": {"device_id": "dev1", "point_name": "temp", "quality": "good"},
                    "fields": {"value": 25.0},
                    "timestamp": "2026-01-01T00:00:00+00:00",
                }
            ]
        )
        mock_influx.write_points_batch = AsyncMock(return_value=True)
        await scheduler._flush_from_sqlite()
        mock_cache.delete_cached.assert_awaited_with([5])

    async def test_flush_sqlite_empty(self, scheduler, mock_cache, mock_influx):
        """SQLite 无记录时直接返回。"""
        mock_cache.get_cached_records = AsyncMock(return_value=[])
        await scheduler._flush_from_sqlite()
        mock_influx.write_points_batch.assert_not_awaited()

    async def test_flush_sqlite_write_failure(self, scheduler, mock_cache, mock_influx):
        """SQLite 回写失败时不删除缓存记录。"""
        mock_cache.get_cached_records = AsyncMock(
            return_value=[
                {
                    "id": 5,
                    "tags": {"device_id": "dev1", "point_name": "temp"},
                    "fields": {"value": 25.0},
                    "timestamp": None,
                }
            ]
        )
        mock_influx.write_points_batch = AsyncMock(return_value=False)
        await scheduler._flush_from_sqlite()
        mock_cache.delete_cached.assert_not_awaited()

    async def test_flush_sqlite_exception(self, scheduler, mock_cache, mock_influx):
        """SQLite 回写抛出异常时被捕获不传播。"""
        mock_cache.get_cached_records = AsyncMock(
            return_value=[
                {
                    "id": 5,
                    "tags": {"device_id": "dev1", "point_name": "temp"},
                    "fields": {"value": 25.0},
                    "timestamp": None,
                }
            ]
        )
        mock_influx.write_points_batch = AsyncMock(side_effect=RuntimeError("err"))
        await scheduler._flush_from_sqlite()

    async def test_cache_flush_loop_ring_buffer(self, scheduler, mock_cache, mock_influx):
        """_cache_flush_loop 走 RingBuffer 路径。"""
        mock_cache.get_ring_buffer_stats = MagicMock(return_value={"pending": 1})
        mock_cache.get_pending_from_ring_buffer = AsyncMock(return_value=[])
        mock_influx.check_health = AsyncMock(return_value=True)
        with _patch_sleep_cancel(after=2):
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler._cache_flush_loop()
        mock_cache.get_pending_from_ring_buffer.assert_awaited()

    async def test_cache_flush_loop_sqlite_fallback(self, scheduler, mock_cache, mock_influx):
        """_cache_flush_loop 无 RingBuffer 时走 SQLite 路径。"""
        mock_cache.get_ring_buffer_stats = MagicMock(return_value=None)
        mock_cache.get_cached_records = AsyncMock(return_value=[])
        mock_influx.check_health = AsyncMock(return_value=True)
        with _patch_sleep_cancel(after=2):
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler._cache_flush_loop()
        mock_cache.get_cached_records.assert_awaited()

    async def test_cache_flush_loop_health_fail(self, scheduler, mock_cache, mock_influx):
        """InfluxDB 不可用时跳过回写。"""
        mock_influx.check_health = AsyncMock(return_value=False)
        with _patch_sleep_cancel(after=2):
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler._cache_flush_loop()
        mock_cache.get_pending_from_ring_buffer.assert_not_awaited()

    async def test_cache_flush_loop_restore(self, scheduler, mock_cache):
        """_cache_flush_loop 启动时从 SQLite 恢复。"""
        mock_cache.get_ring_buffer_stats = MagicMock(return_value=None)
        mock_cache.get_cached_records = AsyncMock(return_value=[])
        with _patch_sleep_cancel(after=2):
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler._cache_flush_loop()
        mock_cache.restore_from_sqlite.assert_awaited()


class TestAIInference:
    """AI 推理与异常告警验证。"""

    async def test_run_ai_inference_timeout(self, scheduler):
        """AI 推理超时时不抛出异常。"""

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise TimeoutError()

        with patch("edgelite.engine.scheduler.asyncio.wait_for", new=fake_wait_for):
            await scheduler._run_ai_inference("dev1", {"temp": 25.0})

    async def test_inner_no_ai_service(self, scheduler):
        """无 ai_service 时直接返回。"""
        with patch("edgelite.app._app_state", SimpleNamespace(ai_service=None, ai_engine=MagicMock())):
            await scheduler._run_ai_inference_inner("dev1", {"temp": 25.0})

    async def test_inner_no_ai_engine(self, scheduler):
        """无 ai_engine 时直接返回。"""
        with patch("edgelite.app._app_state", SimpleNamespace(ai_service=AsyncMock(), ai_engine=None)):
            await scheduler._run_ai_inference_inner("dev1", {"temp": 25.0})

    async def test_inner_cooldown(self, scheduler):
        """冷却期内跳过推理。"""
        import time

        ai_engine = MagicMock()
        ai_engine.get_loaded_models = MagicMock(return_value={})
        fake_state = SimpleNamespace(ai_service=AsyncMock(), ai_engine=ai_engine)
        with patch("edgelite.app._app_state", fake_state):
            scheduler._last_ai_inference_time["dev1"] = time.monotonic()
            await scheduler._run_ai_inference_inner("dev1", {"temp": 25.0})
        fake_state.ai_service.inference.assert_not_awaited()

    async def test_inner_no_active_models(self, scheduler):
        """无活跃模型时跳过推理。"""
        ai_engine = MagicMock()
        ai_engine.get_loaded_models = MagicMock(return_value={})
        fake_state = SimpleNamespace(ai_service=AsyncMock(), ai_engine=ai_engine)
        with patch("edgelite.app._app_state", fake_state):
            await scheduler._run_ai_inference_inner("dev1", {"temp": 25.0})
        fake_state.ai_service.inference.assert_not_awaited()

    async def test_inner_no_numeric_input(self, scheduler):
        """无数值输入时跳过推理。"""
        mw = MagicMock()
        mw.status = "active"
        mw.model_name = "model1"
        ai_engine = MagicMock()
        ai_engine.get_loaded_models = MagicMock(return_value={"m1": mw})
        fake_state = SimpleNamespace(ai_service=AsyncMock(), ai_engine=ai_engine)
        with patch("edgelite.app._app_state", fake_state):
            await scheduler._run_ai_inference_inner("dev1", {"flag": True})
        fake_state.ai_service.inference.assert_not_awaited()

    async def test_inner_success(self, scheduler, mock_event_bus):
        """成功推理时发布虚拟测点事件。"""
        mw = MagicMock()
        mw.status = "active"
        mw.model_name = "anomaly_model"
        ai_engine = MagicMock()
        ai_engine.get_loaded_models = MagicMock(return_value={"m1": mw})
        ai_service = AsyncMock()
        ai_service.inference = AsyncMock(
            return_value={"status": "success", "output_data": {"score": 0.3}, "latency_ms": 5.0}
        )
        fake_state = SimpleNamespace(ai_service=ai_service, ai_engine=ai_engine, alarm_service=None)
        with patch("edgelite.app._app_state", fake_state):
            await scheduler._run_ai_inference_inner("dev1", {"temp": 25.0})
        ai_service.inference.assert_awaited_once()
        assert mock_event_bus.publish.await_count >= 2

    async def test_inner_inference_failure(self, scheduler):
        """推理抛出异常时不传播。"""
        mw = MagicMock()
        mw.status = "active"
        mw.model_name = "model1"
        ai_engine = MagicMock()
        ai_engine.get_loaded_models = MagicMock(return_value={"m1": mw})
        ai_service = AsyncMock()
        ai_service.inference = AsyncMock(side_effect=RuntimeError("model error"))
        fake_state = SimpleNamespace(ai_service=ai_service, ai_engine=ai_engine)
        with patch("edgelite.app._app_state", fake_state):
            await scheduler._run_ai_inference_inner("dev1", {"temp": 25.0})

    async def test_publish_virtual_points_list(self, scheduler, mock_event_bus):
        """输出值为列表时取首元素。"""
        await scheduler._publish_ai_virtual_points("m1", "model", "dev1", {"out": [0.5]}, 10.0)
        published = mock_event_bus.publish.call_args_list
        point_events = [c for c in published if getattr(c.args[0], "point_name", "") == "out"]
        assert len(point_events) == 1
        assert point_events[0].args[0].value == 0.5

    async def test_publish_virtual_points_scalar(self, scheduler, mock_event_bus):
        """输出值为标量时直接使用。"""
        await scheduler._publish_ai_virtual_points("m1", "model", "dev1", {"out": 0.7}, 10.0)
        published = mock_event_bus.publish.call_args_list
        latency_events = [c for c in published if getattr(c.args[0], "point_name", "") == "inference_latency_ms"]
        assert len(latency_events) == 1
        assert latency_events[0].args[0].value == 10.0

    async def test_publish_virtual_points_other(self, scheduler, mock_event_bus):
        """输出值为其他类型时使用0.0。"""
        await scheduler._publish_ai_virtual_points("m1", "model", "dev1", {"out": "text"}, 5.0)
        published = mock_event_bus.publish.call_args_list
        point_events = [c for c in published if getattr(c.args[0], "point_name", "") == "out"]
        assert point_events[0].args[0].value == 0.0

    async def test_anomaly_no_anomaly(self, scheduler):
        """异常分数低于阈值时不触发告警。"""
        alarm_service = AsyncMock()
        with patch("edgelite.app._app_state", SimpleNamespace(alarm_service=alarm_service)):
            await scheduler._check_ai_anomaly("m1", "model", "dev1", {"score": 0.3}, 5.0)
        alarm_service.trigger_alarm.assert_not_awaited()

    async def test_anomaly_critical(self, scheduler):
        """异常分数>0.95 触发 critical 告警。"""
        alarm_service = AsyncMock()
        with patch("edgelite.app._app_state", SimpleNamespace(alarm_service=alarm_service)):
            await scheduler._check_ai_anomaly("m1", "model", "dev1", {"score": 0.96}, 5.0)
        assert alarm_service.trigger_alarm.call_args.kwargs["severity"] == "critical"

    async def test_anomaly_major(self, scheduler):
        """异常分数0.85-0.95 触发 major 告警。"""
        alarm_service = AsyncMock()
        with patch("edgelite.app._app_state", SimpleNamespace(alarm_service=alarm_service)):
            await scheduler._check_ai_anomaly("m1", "model", "dev1", {"score": 0.9}, 5.0)
        assert alarm_service.trigger_alarm.call_args.kwargs["severity"] == "major"

    async def test_anomaly_minor(self, scheduler):
        """异常分数0.8-0.85 触发 minor 告警。"""
        alarm_service = AsyncMock()
        with patch("edgelite.app._app_state", SimpleNamespace(alarm_service=alarm_service)):
            await scheduler._check_ai_anomaly("m1", "model", "dev1", {"score": 0.82}, 5.0)
        assert alarm_service.trigger_alarm.call_args.kwargs["severity"] == "minor"

    async def test_anomaly_no_alarm_service(self, scheduler):
        """无 alarm_service 时不报错。"""
        with patch("edgelite.app._app_state", SimpleNamespace(alarm_service=None)):
            await scheduler._check_ai_anomaly("m1", "model", "dev1", {"score": 0.96}, 5.0)

    async def test_anomaly_list_score(self, scheduler):
        """异常分数为列表时取首元素判断。"""
        alarm_service = AsyncMock()
        with patch("edgelite.app._app_state", SimpleNamespace(alarm_service=alarm_service)):
            await scheduler._check_ai_anomaly("m1", "model", "dev1", {"score": [0.9]}, 5.0)
        alarm_service.trigger_alarm.assert_awaited_once()

    async def test_anomaly_exception_caught(self, scheduler):
        """异常告警检查抛出异常时被捕获不传播。"""
        alarm_service = AsyncMock()
        alarm_service.trigger_alarm = AsyncMock(side_effect=RuntimeError("alarm fail"))
        with patch("edgelite.app._app_state", SimpleNamespace(alarm_service=alarm_service)):
            await scheduler._check_ai_anomaly("m1", "model", "dev1", {"score": 0.96}, 5.0)


class TestIntegration:
    """多组件协作的集成场景验证。"""

    async def test_full_collect_lifecycle(self, scheduler, mock_influx):
        """完整采集生命周期: 启动->采集->停止。"""
        driver = _make_driver({"temp": 25.0, "humi": 60.0})
        await scheduler.start_collect("dev1", driver, [{"name": "temp"}, {"name": "humi"}])
        await asyncio.sleep(0.1)
        await scheduler.stop_all()
        assert mock_influx.write_points_batch.await_count >= 1

    async def test_get_last_values_multiple_devices(self, scheduler):
        """get_last_values 返回多设备最近值。"""
        scheduler._last_values["dev1"] = {"temp": 25.0}
        scheduler._last_values["dev2"] = {"humi": 60.0}
        result = await scheduler.get_last_values()
        assert result["dev1"]["temp"] == 25.0
        assert result["dev2"]["humi"] == 60.0

    def test_default_timeout_constant(self):
        """DEFAULT_TIMEOUT 为5.0秒。"""
        assert DEFAULT_TIMEOUT == 5.0
