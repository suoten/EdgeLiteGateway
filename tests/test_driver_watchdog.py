"""驱动心跳看门狗测试 - 心跳检测/状态统计/注册管理/连接摘要

覆盖 engine/driver_watchdog.py：
- HeartbeatResult 数据类
- DriverWatchdog: 构造参数/注册注销/心跳检测(sync/async/异常)/批量检测/连接摘要
- get_offline_history / get_connection_summary / get_connection_summary_async
- start/stop 生命周期
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from edgelite.engine.driver_watchdog import DriverWatchdog, HeartbeatResult


class FakeStats:
    """模拟 DriverHealthStats"""

    def __init__(
        self,
        consecutive_failures: int = 0,
        total_downtime_seconds: float = 0.0,
        connection_quality_score: float = 100.0,
    ):
        self.consecutive_failures = consecutive_failures
        self.total_downtime_seconds = total_downtime_seconds
        self.connection_quality_score = connection_quality_score


class FakeDriver:
    """模拟驱动插件（同步 health_check）"""

    def __init__(
        self,
        healthy: bool = True,
        stats: FakeStats | None = None,
        raise_exc: Exception | None = None,
    ):
        self._healthy = healthy
        self._stats = stats
        self._raise = raise_exc

    def health_check(self, device_id: str) -> bool:
        if self._raise:
            raise self._raise
        return self._healthy

    def get_health_stats(self, device_id: str):
        return self._stats


class FakeAsyncDriver:
    """模拟驱动插件（异步 health_check，asyncio.iscoroutinefunction 返回 True）"""

    def __init__(
        self,
        healthy: bool = True,
        stats: FakeStats | None = None,
        raise_exc: Exception | None = None,
    ):
        self._healthy = healthy
        self._stats = stats
        self._raise = raise_exc

    async def health_check(self, device_id: str) -> bool:
        if self._raise:
            raise self._raise
        return self._healthy

    def get_health_stats(self, device_id: str):
        return self._stats


class TestHeartbeatResult:
    def test_defaults(self):
        r = HeartbeatResult(device_id="d1", success=True, latency_ms=12.5)
        assert r.device_id == "d1"
        assert r.success is True
        assert r.latency_ms == 12.5
        assert r.error_message is None
        assert r.timestamp is None

    def test_failure_with_error(self):
        r = HeartbeatResult(
            device_id="d1",
            success=False,
            latency_ms=0,
            error_message="timeout",
        )
        assert r.success is False
        assert r.error_message == "timeout"


class TestDriverWatchdogConstructor:
    def test_defaults(self):
        wd = DriverWatchdog()
        assert wd._check_interval == 30.0
        assert wd._max_offline_duration == 300.0
        assert wd._auto_reconnect is True
        assert wd._max_reconnect_attempts == 3
        assert wd._stale_cycles == 3
        assert wd._restart_cycles == 10
        assert wd._running is False
        assert wd._task is None
        assert wd._drivers == {}
        assert wd._event_bus is None
        assert wd._on_status_change is None

    def test_custom_params(self):
        wd = DriverWatchdog(
            check_interval=5.0,
            max_offline_duration=60.0,
            auto_reconnect=False,
            max_reconnect_attempts=5,
            stale_cycles=2,
            restart_cycles=8,
        )
        assert wd._check_interval == 5.0
        assert wd._max_offline_duration == 60.0
        assert wd._auto_reconnect is False
        assert wd._max_reconnect_attempts == 5
        assert wd._stale_cycles == 2
        assert wd._restart_cycles == 8

    def test_set_event_bus(self):
        wd = DriverWatchdog()
        bus = SimpleNamespace()
        wd.set_event_bus(bus)
        assert wd._event_bus is bus

    def test_set_status_change_callback(self):
        wd = DriverWatchdog()
        cb = lambda dev_id, status: None  # noqa: E731
        wd.set_status_change_callback(cb)
        assert wd._on_status_change is cb


class TestDriverWatchdogRegister:
    @pytest.mark.asyncio
    async def test_register_driver(self):
        wd = DriverWatchdog(auto_reconnect=False)
        driver = FakeDriver(healthy=True, stats=None)
        await wd.register_driver("dev1", driver, {"ip": "127.0.0.1"})
        assert "dev1" in wd._drivers
        assert wd._device_configs["dev1"] == {"ip": "127.0.0.1"}
        assert "dev1" in wd._offline_history

    @pytest.mark.asyncio
    async def test_unregister_driver(self):
        wd = DriverWatchdog(auto_reconnect=False)
        driver = FakeDriver()
        await wd.register_driver("dev1", driver, {})
        assert "dev1" in wd._drivers
        await wd.unregister_driver("dev1")
        assert "dev1" not in wd._drivers
        assert "dev1" not in wd._device_configs
        # 各种计数器/集合也应被清理
        assert "dev1" not in wd._stale_counters
        assert "dev1" not in wd._stale_devices
        assert "dev1" not in wd._reconnect_attempts

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_no_error(self):
        wd = DriverWatchdog()
        # 注销不存在的驱动不应抛异常
        await wd.unregister_driver("nonexistent")


class TestDriverWatchdogCheckDevice:
    @pytest.mark.asyncio
    async def test_check_unregistered_device(self):
        wd = DriverWatchdog()
        result = await wd.check_device("nonexistent")
        assert result.success is False
        assert "not registered" in result.error_message.lower()
        assert result.device_id == "nonexistent"

    @pytest.mark.asyncio
    async def test_check_device_sync_success(self):
        wd = DriverWatchdog(auto_reconnect=False)
        driver = FakeDriver(healthy=True, stats=None)
        await wd.register_driver("dev1", driver, {})
        result = await wd.check_device("dev1")
        assert result.success is True
        assert result.error_message is None
        assert result.timestamp is not None
        assert result.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_check_device_sync_failure(self):
        wd = DriverWatchdog(auto_reconnect=False)
        driver = FakeDriver(healthy=False, stats=None)
        await wd.register_driver("dev1", driver, {})
        result = await wd.check_device("dev1")
        assert result.success is False
        assert "Health check failed" in result.error_message

    @pytest.mark.asyncio
    async def test_check_device_async_success(self):
        wd = DriverWatchdog(auto_reconnect=False)
        driver = FakeAsyncDriver(healthy=True, stats=None)
        await wd.register_driver("dev1", driver, {})
        result = await wd.check_device("dev1")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_check_device_async_failure(self):
        wd = DriverWatchdog(auto_reconnect=False)
        driver = FakeAsyncDriver(healthy=False, stats=None)
        await wd.register_driver("dev1", driver, {})
        result = await wd.check_device("dev1")
        assert result.success is False
        assert "Health check failed" in result.error_message

    @pytest.mark.asyncio
    async def test_check_device_exception(self):
        wd = DriverWatchdog(auto_reconnect=False)
        driver = FakeDriver(raise_exc=ConnectionError("network unreachable"), stats=None)
        await wd.register_driver("dev1", driver, {})
        result = await wd.check_device("dev1")
        assert result.success is False
        assert "network unreachable" in result.error_message

    @pytest.mark.asyncio
    async def test_check_device_async_exception(self):
        wd = DriverWatchdog(auto_reconnect=False)
        driver = FakeAsyncDriver(
            raise_exc=TimeoutError("async timeout"),
            stats=None,
        )
        await wd.register_driver("dev1", driver, {})
        result = await wd.check_device("dev1")
        assert result.success is False
        assert "async timeout" in result.error_message


class TestDriverWatchdogCheckAll:
    @pytest.mark.asyncio
    async def test_check_all_devices(self):
        wd = DriverWatchdog(auto_reconnect=False)
        await wd.register_driver("dev1", FakeDriver(healthy=True, stats=None), {})
        await wd.register_driver("dev2", FakeDriver(healthy=False, stats=None), {})
        results = await wd.check_all_devices()
        assert set(results.keys()) == {"dev1", "dev2"}
        assert results["dev1"].success is True
        assert results["dev2"].success is False

    @pytest.mark.asyncio
    async def test_check_all_devices_empty(self):
        wd = DriverWatchdog()
        results = await wd.check_all_devices()
        assert results == {}


class TestDriverWatchdogStaleAndRecovery:
    @pytest.mark.asyncio
    async def test_stale_counter_increments_on_failure(self):
        """单次失败递增 stale 计数器（未达阈值不发布事件）"""
        wd = DriverWatchdog(auto_reconnect=False, stale_cycles=3)
        driver = FakeDriver(healthy=False, stats=None)
        await wd.register_driver("dev1", driver, {})
        await wd.check_device("dev1")
        assert wd._stale_counters["dev1"] == 1
        assert "dev1" not in wd._stale_devices  # 未达阈值

    @pytest.mark.asyncio
    async def test_device_marked_stale_after_threshold(self):
        """连续失败达到 stale_cycles 阈值后标记为 stale"""
        wd = DriverWatchdog(auto_reconnect=False, stale_cycles=2)
        driver = FakeDriver(healthy=False, stats=None)
        await wd.register_driver("dev1", driver, {})
        await wd.check_device("dev1")  # stale=1
        await wd.check_device("dev1")  # stale=2 >= threshold
        assert "dev1" in wd._stale_devices

    @pytest.mark.asyncio
    async def test_stale_recovery_on_success(self):
        """stale 设备心跳恢复后从 stale 集合移除"""
        wd = DriverWatchdog(auto_reconnect=False, stale_cycles=1)
        # 先失败一次使其 stale
        driver = FakeDriver(healthy=False, stats=None)
        await wd.register_driver("dev1", driver, {})
        await wd.check_device("dev1")  # stale=1 >= 1 → marked stale
        assert "dev1" in wd._stale_devices
        # 恢复成功
        driver._healthy = True
        await wd.check_device("dev1")
        assert "dev1" not in wd._stale_devices


class TestDriverWatchdogSummary:
    @pytest.mark.asyncio
    async def test_get_offline_history_empty(self):
        wd = DriverWatchdog()
        assert wd.get_offline_history("nonexistent") == []

    @pytest.mark.asyncio
    async def test_get_connection_summary_empty(self):
        wd = DriverWatchdog()
        summary = wd.get_connection_summary()
        assert summary["total_devices"] == 0
        assert summary["online"] == 0
        assert summary["health_rate"] == 100.0

    @pytest.mark.asyncio
    async def test_get_connection_summary_with_devices(self):
        wd = DriverWatchdog(auto_reconnect=False)
        await wd.register_driver("dev1", FakeDriver(healthy=True, stats=FakeStats(consecutive_failures=0)), {})
        await wd.register_driver(
            "dev2",
            FakeDriver(healthy=False, stats=FakeStats(consecutive_failures=5, connection_quality_score=50)),
            {},
        )
        summary = wd.get_connection_summary()
        assert summary["total_devices"] == 2
        assert summary["online"] == 1  # dev1
        assert summary["offline"] == 1  # dev2
        assert summary["degraded"] == 1  # dev2 quality < 80

    @pytest.mark.asyncio
    async def test_get_connection_summary_stats_none_counts_online(self):
        """stats 为 None 的设备计入 online"""
        wd = DriverWatchdog(auto_reconnect=False)
        await wd.register_driver("dev1", FakeDriver(healthy=True, stats=None), {})
        summary = wd.get_connection_summary()
        assert summary["online"] == 1
        assert summary["offline"] == 0

    @pytest.mark.asyncio
    async def test_get_connection_summary_async(self):
        wd = DriverWatchdog(auto_reconnect=False)
        await wd.register_driver("dev1", FakeDriver(healthy=True, stats=FakeStats(consecutive_failures=0)), {})
        summary = await wd.get_connection_summary_async()
        assert summary["total_devices"] == 1
        assert summary["online"] == 1


class TestDriverWatchdogLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        """启动后立即停止，不应抛异常"""
        wd = DriverWatchdog(check_interval=999.0)  # 长间隔避免循环执行
        await wd.start()
        assert wd._running is True
        assert wd._task is not None
        await wd.stop()
        assert wd._running is False
        assert wd._task is None

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        """未启动直接停止不应抛异常"""
        wd = DriverWatchdog()
        await wd.stop()
        assert wd._running is False
