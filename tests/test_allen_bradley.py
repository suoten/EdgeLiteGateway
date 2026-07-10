"""Allen-Bradley (EtherNet/IP) 驱动单元测试

覆盖 Task #15 两项 P1 修复:

1. CIP 会话续期 — watchdog 连续失败达阈值触发自动重连
   - 原问题: watchdog 仅标记 DEGRADED，不触发重连；若无读写操作则连接永久不可恢复
   - 修复: 新增 _watchdog_fail_count 计数器，连续 3 次失败触发 _try_reconnect

2. Large Forward Open 降级 — 自动启用 LFO 时若 ping 失败，回退到普通 Forward Open
   - 原问题: LFO 自动启用后不测试是否可用，旧固件 PLC 不支持 LFO 会导致连接失败且无降级
   - 修复: ping 失败 + LFO auto → 禁用 LFO 并重试 ping (在 start() 和 _try_reconnect() 两条路径)
"""

from __future__ import annotations

import asyncio
import sys
import threading
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "src")

from edgelite.drivers.allen_bradley import AbConnState, AllenBradleyDriver


def _make_driver() -> AllenBradleyDriver:
    """构造 AllenBradleyDriver 实例 (绕过 __init__ 的复杂依赖)，仅设置测试所需属性。

    _set_conn_state 依赖基类 _set_connection_state / _background_tasks，测试中替换为
    直接写 _conn_state 的轻量实现，避免引入事件总线等重依赖。
    """
    driver = AllenBradleyDriver.__new__(AllenBradleyDriver)
    driver._client = None
    driver._config: dict = {}
    driver._sync_lock = threading.RLock()
    driver._ab_conn_state_lock = threading.Lock()
    driver._reconnect_lock = asyncio.Lock()
    driver._device_clients_lock = asyncio.Lock()
    driver._device_clients: dict = {}
    driver._device_configs: dict = {}
    driver._device_locks: dict = {}
    driver._reconnect_count = 0
    driver._reconnect_cooldown_until = 0.0
    driver._reconnect_delay = 1.0
    driver._devices: dict = {}
    driver._watchdog_task = None
    driver._watchdog_interval = 10.0
    driver._watchdog_check_mode = driver._WATCHDOG_CHECK_MODE_PING
    driver._watchdog_fail_count = 0
    driver._WATCHDOG_RECONNECT_THRESHOLD = 3
    driver._running = False
    driver._conn_state = AbConnState.DISCONNECTED.value
    driver._offline_since: dict = {}
    driver._large_forward_open_auto = False
    driver._cip_security_enabled = False
    driver._primary_ip = ""
    driver._backup_ip = ""
    driver._active_ip = ""
    driver._using_backup = False
    driver._primary_fail_count = 0
    driver._failover_count = 0
    driver._failover_start_mono = 0.0
    driver._last_failover_time = ""
    driver._last_failover_duration_ms = 0.0
    driver._failover_probe_task = None
    driver._failover_mode_lock = asyncio.Lock()
    driver._auto_revert = True
    driver._audit = None
    driver._MAX_RECONNECT_ATTEMPTS = 3
    driver._RECONNECT_BASE_DELAY = 1.0
    driver._RECONNECT_MAX_DELAY = 60.0
    driver._JITTER_MAX_MS = 1000
    driver._FAILOVER_THRESHOLD = 3
    driver._FAILOVER_FAST_DELAY = 0.5
    driver._FAILOVER_PROBE_INTERVAL = 30.0
    driver._READ_TIMEOUT = 30
    driver._DEFAULT_TAG = "@cpu"
    driver._connection_timeout = 5.0  # _try_reconnect 中 self._client.SocketTimeout 赋值依赖
    driver._config_version_mgr = None
    driver._ota_mgr = None

    # 轻量 _set_conn_state: 仅更新 _conn_state，避免基类事件总线依赖
    def _light_set_conn_state(new_state: str, device_id: str = "", reason: str = "") -> None:
        driver._conn_state = new_state

    driver._set_conn_state = _light_set_conn_state  # type: ignore[assignment]
    return driver


class _FakePlcClient:
    """模拟 pylogix.PLC 客户端 — 仅暴露 LargeForwardOpen 属性。"""

    def __init__(self):
        self.LargeForwardOpen = True
        self.SocketTimeout = None


# ══════════════════════════════════════════════════════════════════════
# 1. Watchdog 失败计数 — CIP 会话续期核心逻辑
# ══════════════════════════════════════════════════════════════════════


class TestWatchdogFailCount:
    """watchdog 连续失败计数递增/重置 (Task #15 修复点 1)"""

    @staticmethod
    def _make_ping_fail_driver() -> AllenBradleyDriver:
        """构造一个 watchdog 始终 ping 失败的驱动实例。"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001  # 极短间隔加速测试
        driver._client = MagicMock()  # truthy，通过 if not self._client 检查
        driver._devices = {"dev1": {}}
        driver._conn_state = AbConnState.CONNECTED.value
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore

        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            # 失败 2 次后停止循环 (低于阈值，不触发重连)
            if call_count >= 2:
                driver._running = False
            return False

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        return driver

    async def test_fail_count_increments_on_failure(self):
        """ping 失败 → _watchdog_fail_count 递增"""
        driver = self._make_ping_fail_driver()
        await driver._watchdog_loop()
        assert driver._watchdog_fail_count == 2

    async def test_fail_count_resets_on_success(self):
        """ping 成功 → _watchdog_fail_count 重置为 0"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001
        driver._client = MagicMock()
        driver._devices = {"dev1": {}}
        driver._conn_state = AbConnState.DEGRADED.value  # 已降级，验证恢复
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore

        # 预置失败计数，验证成功后清零
        driver._watchdog_fail_count = 2
        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                driver._running = False
            return True  # ping 成功

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        await driver._watchdog_loop()
        assert driver._watchdog_fail_count == 0

    async def test_success_clears_offline_since(self):
        """ping 成功 → 清除 _offline_since 记录"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001
        driver._client = MagicMock()
        driver._devices = {"dev1": {}, "dev2": {}}
        driver._conn_state = AbConnState.CONNECTED.value
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        # 预置离线时间戳
        driver._offline_since = {"dev1": "ts1", "dev2": "ts2"}

        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                driver._running = False
            return True

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        await driver._watchdog_loop()
        assert driver._offline_since == {}

    async def test_failure_records_offline_since(self):
        """ping 失败 → 记录 _offline_since 时间戳"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001
        driver._client = MagicMock()
        driver._devices = {"dev1": {}}
        driver._conn_state = AbConnState.CONNECTED.value
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore

        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                driver._running = False
            return False

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        await driver._watchdog_loop()
        assert "dev1" in driver._offline_since


class TestWatchdogAutoReconnect:
    """watchdog 连续失败达阈值触发自动重连 (Task #15 修复点 1 核心)"""

    async def test_threshold_triggers_reconnect(self):
        """连续失败达阈值 (3 次) → 触发 _try_reconnect"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001
        driver._client = MagicMock()
        driver._devices = {"dev1": {}}
        driver._conn_state = AbConnState.CONNECTED.value
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        driver._try_reconnect = AsyncMock()  # type: ignore

        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            # 失败 3 次触发重连，第 4 次停止
            if call_count >= 4:
                driver._running = False
            return False

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        await driver._watchdog_loop()
        # 让 create_task 创建的重连任务有机会执行
        await asyncio.sleep(0.01)
        driver._try_reconnect.assert_called_once_with("dev1")

    async def test_fail_count_resets_after_threshold(self):
        """达阈值触发重连后 → _watchdog_fail_count 重置为 0 (避免重复触发)"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001
        driver._client = MagicMock()
        driver._devices = {"dev1": {}}
        driver._conn_state = AbConnState.CONNECTED.value
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        driver._try_reconnect = AsyncMock()  # type: ignore

        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                driver._running = False
            return False

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        await driver._watchdog_loop()
        await asyncio.sleep(0.01)
        # 3 次失败后触发重连并重置，第 4 次失败计数为 1
        assert driver._watchdog_fail_count == 1

    async def test_below_threshold_no_reconnect(self):
        """失败次数低于阈值 → 不触发 _try_reconnect"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001
        driver._client = MagicMock()
        driver._devices = {"dev1": {}}
        driver._conn_state = AbConnState.CONNECTED.value
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        driver._try_reconnect = AsyncMock()  # type: ignore

        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            # 仅失败 2 次 (低于阈值 3)
            if call_count >= 2:
                driver._running = False
            return False

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        await driver._watchdog_loop()
        await asyncio.sleep(0.01)
        driver._try_reconnect.assert_not_called()

    async def test_reconnect_with_empty_devices_uses_empty_did(self):
        """设备字典为空时 → 不触发重连 (安全防护: did 为空字符串)"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001
        driver._client = MagicMock()
        driver._devices = {}  # 空设备字典
        driver._conn_state = AbConnState.CONNECTED.value
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        driver._try_reconnect = AsyncMock()  # type: ignore

        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                driver._running = False
            return False

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        await driver._watchdog_loop()
        await asyncio.sleep(0.01)
        # did 为空字符串时 if did: 为 False，不调用 _try_reconnect
        driver._try_reconnect.assert_not_called()

    async def test_degraded_state_set_on_failure(self):
        """ping 失败且当前为 CONNECTED → 状态转为 DEGRADED"""
        driver = _make_driver()
        driver._running = True
        driver._watchdog_interval = 0.001
        driver._client = MagicMock()
        driver._devices = {"dev1": {}}
        driver._conn_state = AbConnState.CONNECTED.value
        driver._watchdog_check_mode = "ping"
        driver._handle_watchdog_exception = MagicMock(return_value=True)  # type: ignore
        driver._try_reconnect = AsyncMock()  # type: ignore

        state_transitions: list[str] = []

        def tracking_set_state(new_state: str, device_id: str = "", reason: str = "") -> None:
            state_transitions.append(new_state)
            driver._conn_state = new_state

        driver._set_conn_state = tracking_set_state  # type: ignore[assignment]

        call_count = 0

        async def fake_run_in_thread(func, *args, timeout: float = 30.0, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                driver._running = False
            return False

        driver._run_in_thread = fake_run_in_thread  # type: ignore[assignment]
        await driver._watchdog_loop()
        await asyncio.sleep(0.01)
        assert AbConnState.DEGRADED.value in state_transitions


class TestWatchdogResetOnReconnect:
    """重连成功后重置 watchdog 失败计数 (Task #15 修复点 1 配套 — allen_bradley.py:1689)"""

    async def test_reconnect_success_resets_fail_count(self):
        """_try_reconnect 成功路径 → _watchdog_fail_count 重置为 0

        验证 allen_bradley.py 第 1689 行: self._watchdog_fail_count = 0
        """
        driver = _make_driver()
        driver._config = {"port": 44818, "slot": 0, "large_forward_open": False}
        driver._active_ip = "192.168.1.100"
        driver._using_backup = False
        driver._backup_ip = ""
        driver._client = None  # 初始无 client，跳过 _forward_close
        driver._watchdog_fail_count = 3  # 预置失败计数
        driver._reconnect_count = 0
        driver._reconnect_cooldown_until = 0.0
        driver._device_clients = {}
        driver._device_configs = {}
        driver._conn_state = AbConnState.CONNECTING.value
        driver._calc_backoff_delay = MagicMock(return_value=0.0)  # type: ignore

        fake_plc = _FakePlcClient()

        with patch("pylogix.PLC", return_value=fake_plc):
            # _sync_ping 第一次返回 True (LFO 启用后 ping 成功，不走降级)
            with patch.object(driver, "_sync_ping", return_value=True):
                await driver._try_reconnect("dev1")

        assert driver._watchdog_fail_count == 0
        assert driver._running is True


# ══════════════════════════════════════════════════════════════════════
# 2. Large Forward Open 降级 — _try_reconnect 路径
# ══════════════════════════════════════════════════════════════════════


class TestLargeForwardOpenFallback:
    """Large Forward Open 降级逻辑 (Task #15 修复点 2)

    场景: 旧固件 PLC 不支持 LFO，自动启用 LFO 后 ping 失败 → 禁用 LFO → 重试 ping
    """

    def _setup_reconnect_driver(self) -> AllenBradleyDriver:
        """构造用于测试 _try_reconnect LFO 降级的驱动实例。"""
        driver = _make_driver()
        driver._config = {"port": 44818, "slot": 0, "large_forward_open": False}
        driver._active_ip = "192.168.1.100"
        driver._using_backup = False
        driver._backup_ip = ""
        driver._client = None
        driver._reconnect_count = 0
        driver._reconnect_cooldown_until = 0.0
        driver._device_clients = {}
        driver._device_configs = {}
        driver._conn_state = AbConnState.CONNECTING.value
        driver._calc_backoff_delay = MagicMock(return_value=0.0)  # type: ignore
        driver._forward_close = AsyncMock()  # type: ignore
        return driver

    async def test_lfo_auto_disabled_on_ping_fail(self):
        """LFO auto=True + ping 失败 → 禁用 LFO (LargeForwardOpen=False, _large_forward_open_auto=False)"""
        driver = self._setup_reconnect_driver()
        fake_plc = _FakePlcClient()

        ping_results = iter([False, True])  # 第一次 ping 失败，降级后第二次成功

        with patch("pylogix.PLC", return_value=fake_plc):
            with patch.object(driver, "_sync_ping", side_effect=lambda: next(ping_results)):
                await driver._try_reconnect("dev1")

        # LFO 被禁用
        assert driver._large_forward_open_auto is False
        assert fake_plc.LargeForwardOpen is False

    async def test_lfo_fallback_retries_ping(self):
        """LFO 降级后 → 再次执行 _sync_ping (验证重试)"""
        driver = self._setup_reconnect_driver()
        fake_plc = _FakePlcClient()

        ping_call_count = 0

        def fake_ping():
            nonlocal ping_call_count
            ping_call_count += 1
            return ping_call_count == 2  # 第一次 False，第二次 True

        with patch("pylogix.PLC", return_value=fake_plc):
            with patch.object(driver, "_sync_ping", side_effect=fake_ping):
                await driver._try_reconnect("dev1")

        assert ping_call_count == 2  # 降级后重试了一次

    async def test_lfo_fallback_success_connects(self):
        """LFO 降级 + 重试 ping 成功 → 标记 CONNECTED + _running=True"""
        driver = self._setup_reconnect_driver()
        fake_plc = _FakePlcClient()

        state_transitions: list[str] = []

        def tracking_set_state(new_state: str, device_id: str = "", reason: str = "") -> None:
            state_transitions.append(new_state)
            driver._conn_state = new_state

        driver._set_conn_state = tracking_set_state  # type: ignore[assignment]

        ping_results = iter([False, True])

        with patch("pylogix.PLC", return_value=fake_plc):
            with patch.object(driver, "_sync_ping", side_effect=lambda: next(ping_results)):
                await driver._try_reconnect("dev1")

        assert driver._running is True
        assert AbConnState.CONNECTED.value in state_transitions

    async def test_lfo_fallback_both_fail_stays_disconnected(self):
        """LFO 降级 + 重试 ping 仍失败 → 标记 DISCONNECTED + _client 清空"""
        driver = self._setup_reconnect_driver()
        fake_plc = _FakePlcClient()

        state_transitions: list[str] = []

        def tracking_set_state(new_state: str, device_id: str = "", reason: str = "") -> None:
            state_transitions.append(new_state)
            driver._conn_state = new_state

        driver._set_conn_state = tracking_set_state  # type: ignore[assignment]

        ping_results = iter([False, False])  # 两次都失败

        with patch("pylogix.PLC", return_value=fake_plc):
            with patch.object(driver, "_sync_ping", side_effect=lambda: next(ping_results)):
                await driver._try_reconnect("dev1")

        assert driver._running is False
        assert AbConnState.DISCONNECTED.value in state_transitions
        assert driver._client is None  # 清理失败连接

    async def test_lfo_not_triggered_when_auto_false(self):
        """_large_forward_open_auto=False + ping 失败 → 不触发 LFO 降级 (不重试 ping)"""
        driver = self._setup_reconnect_driver()
        # config 中 large_forward_open=True → 走显式 LFO 路径，不设 auto
        driver._config["large_forward_open"] = True
        fake_plc = _FakePlcClient()

        ping_call_count = 0

        def fake_ping():
            nonlocal ping_call_count
            ping_call_count += 1
            return False  # 始终失败

        with patch("pylogix.PLC", return_value=fake_plc):
            with patch.object(driver, "_sync_ping", side_effect=fake_ping):
                await driver._try_reconnect("dev1")

        # auto 未启用，不触发降级重试，仅 ping 一次
        assert ping_call_count == 1
        assert driver._large_forward_open_auto is False

    async def test_lfo_fallback_resets_fail_count(self):
        """LFO 降级成功后 → _watchdog_fail_count 重置 (与重连成功路径一致)"""
        driver = self._setup_reconnect_driver()
        driver._watchdog_fail_count = 3
        fake_plc = _FakePlcClient()

        ping_results = iter([False, True])

        with patch("pylogix.PLC", return_value=fake_plc):
            with patch.object(driver, "_sync_ping", side_effect=lambda: next(ping_results)):
                await driver._try_reconnect("dev1")

        assert driver._watchdog_fail_count == 0


class TestLargeForwardOpenAttributeInit:
    """LFO auto 标志初始化逻辑验证 (Task #15 修复点 2 前置条件)"""

    async def test_lfo_auto_set_when_not_explicit_and_no_backup(self):
        """config 未指定 large_forward_open + 无 backup → 自动启用 LFO (_large_forward_open_auto=True)"""
        driver = _make_driver()
        driver._config = {"port": 44818, "slot": 0, "large_forward_open": False}
        driver._active_ip = "192.168.1.100"
        driver._using_backup = False
        driver._backup_ip = ""
        driver._client = None
        driver._reconnect_count = 0
        driver._reconnect_cooldown_until = 0.0
        driver._device_clients = {}
        driver._device_configs = {}
        driver._conn_state = AbConnState.CONNECTING.value
        driver._calc_backoff_delay = MagicMock(return_value=0.0)  # type: ignore
        driver._forward_close = AsyncMock()  # type: ignore

        fake_plc = _FakePlcClient()

        with patch("pylogix.PLC", return_value=fake_plc):
            with patch.object(driver, "_sync_ping", return_value=True):
                await driver._try_reconnect("dev1")

        # 未显式指定 LFO + 非 backup → auto 启用
        assert driver._large_forward_open_auto is True
        assert fake_plc.LargeForwardOpen is True

    async def test_lfo_auto_not_set_when_using_backup(self):
        """使用 backup IP 时 → 不自动启用 LFO (_large_forward_open_auto 保持 False)"""
        driver = _make_driver()
        driver._config = {"port": 44818, "slot": 0, "large_forward_open": False}
        driver._active_ip = "192.168.1.200"  # backup IP
        driver._using_backup = True
        driver._backup_ip = "192.168.1.200"
        driver._client = None
        driver._reconnect_count = 0
        driver._reconnect_cooldown_until = 0.0
        driver._device_clients = {}
        driver._device_configs = {}
        driver._conn_state = AbConnState.CONNECTING.value
        driver._calc_backoff_delay = MagicMock(return_value=0.0)  # type: ignore
        driver._forward_close = AsyncMock()  # type: ignore

        fake_plc = _FakePlcClient()

        with patch("pylogix.PLC", return_value=fake_plc):
            with patch.object(driver, "_sync_ping", return_value=True):
                await driver._try_reconnect("dev1")

        # backup 模式下不自动启用 LFO
        assert driver._large_forward_open_auto is False
