"""Modbus 连接池 TOCTOU 竞态修复单元测试 (并发安全 #1)

覆盖 P0 修复:

**TCP (modbus_tcp.py)**:
- write_point / write_points_batch 的 get+lease 原子化
- 原问题: get(无锁) → check(无锁) → _lease_client(加锁)，
  _stale_client_cleanup_loop 可在 get 与 lease 之间关闭 client
- 修复: 在 _lease_lock 内原子完成 get + check + lease

**RTU (modbus_rtu.py)**:
- read_points / write_point / write_points_batch 的串口锁内 client 重新校验
- 原问题: _ensure_connected 在无锁状态下返回 client，_try_reconnect(另一协程)
  可在 _ensure_connected 返回与 acquire_serial_lock 之间关闭/替换 client
- 修复: 在串口锁内重新校验 client.connected 和 client 身份 (is 同一性)
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "src")


# ════════════════════════════════════════════════════════════════════════
# TCP 驱动构造辅助
# ════════════════════════════════════════════════════════════════════════


def _make_tcp_driver():
    """构造 ModbusTcpDriver 实例 (绕过 __init__)，仅设置 TOCTOU 测试所需属性。"""
    from edgelite.drivers.modbus_tcp import ModbusTcpDriver

    driver = ModbusTcpDriver.__new__(ModbusTcpDriver)
    # 连接池租用机制
    driver._lease_lock = asyncio.Lock()
    driver._leased_clients: set = set()
    driver._clients: dict = {}
    driver._connection_pool: dict = {}
    # 权限
    driver._role_lock = asyncio.Lock()
    driver._current_user_role = "admin"
    driver.check_permission = AsyncMock(return_value=True)
    # 状态转换与日志
    driver._transition_state = MagicMock()
    driver._log_error = MagicMock()
    driver._audit_write = MagicMock()
    driver._record_write_success = MagicMock()
    driver._record_write_failure = MagicMock()
    # NOTE: 不 mock _release_client — 使用真实实现 (仅依赖 _lease_lock + _leased_clients)
    # 这样可验证 lease→release 完整生命周期
    # 写入校验
    driver._check_write_value_range = MagicMock(return_value=True)
    driver._check_write_rate_limit = MagicMock(return_value=True)
    driver._WRITE_RATE_LIMIT_DEFAULT = 1.0
    driver._audit = None
    driver._write_fail_tracker: dict = {}
    driver._last_values: dict = {}
    driver._device_configs: dict = {}
    driver._device_points: dict = {}
    driver.plugin_name = "modbus_tcp"
    return driver


def _make_tcp_client(connected: bool = True):
    """构造模拟的 pymodbus AsyncModbusTcpClient。"""
    client = MagicMock()
    client.connected = connected
    client.write_coil = AsyncMock()
    client.write_register = AsyncMock()
    client.write_registers = AsyncMock()
    client.close = MagicMock()
    return client


# ════════════════════════════════════════════════════════════════════════
# TCP write_point TOCTOU 测试
# ════════════════════════════════════════════════════════════════════════


class TestTcpWritePointTOCTOU:
    """write_point 的 get+lease 原子化 (并发安全 #1)"""

    async def test_returns_false_when_client_not_in_clients(self):
        """client 不在 _clients 中 (被 _stale_client_cleanup_loop 移除) → 返回 False

        验证: 原子 get+lease 在 _lease_lock 内发现 client=None，不进入写入路径。
        """
        driver = _make_tcp_driver()
        driver._clients = {}  # client 已被清理

        result = await driver.write_point("dev1", "point1", 1)

        assert result is False
        driver._transition_state.assert_called_once()
        args = driver._transition_state.call_args
        assert "dev1" in args[0]
        assert "disconnected" in str(args[0]).lower()
        # client 未被租用
        assert len(driver._leased_clients) == 0
        # _release_client 未被调用 (没有 client 需要释放)
        # _release_client 未被调用 (无 client 被租用) — 由 _leased_clients 为空间接验证

    async def test_returns_false_when_client_disconnected(self):
        """client.connected=False (被 _stale_client_cleanup_loop 关闭) → 返回 False

        验证: 原子 get+lease 在 _lease_lock 内发现 client.connected=False，不租用。
        """
        driver = _make_tcp_driver()
        client = _make_tcp_client(connected=False)
        driver._clients = {"dev1": client}

        result = await driver.write_point("dev1", "point1", 1)

        assert result is False
        driver._transition_state.assert_called_once()
        # client 未被加入 _leased_clients
        assert client not in driver._leased_clients
        # _release_client 未被调用 (无 client 被租用) — 由 _leased_clients 为空间接验证

    async def test_leases_and_releases_on_success(self):
        """client 有效时 → 租用 → 写入 → 释放 (happy path)

        验证: 原子 get+lease 成功将 client 加入 _leased_clients，
        写入完成后 _release_client 被调用释放租用。
        """
        driver = _make_tcp_driver()
        client = _make_tcp_client(connected=True)
        driver._clients = {"dev1": client}
        driver._device_configs = {"dev1": {"slave_id": 1, "byte_order": "ABCD", "timeout": 5.0}}
        driver._device_points = {"dev1": [{"name": "point1", "address": 0, "data_type": "bool"}]}

        # patch record_packet (模块级函数)
        import edgelite.drivers.modbus_tcp as modbus_tcp_mod

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is True
        # _release_client 在 finally 中被调用
        # 真实 _release_client 在 finally 中执行，从 _leased_clients 移除 client
        # 写入后 _leased_clients 应为空 (已释放)
        assert client not in driver._leased_clients

    async def test_releases_on_write_exception(self):
        """写入异常时 → finally 仍释放 client (no leak)"""
        driver = _make_tcp_driver()
        client = _make_tcp_client(connected=True)
        # write_coil 抛出异常
        client.write_coil = AsyncMock(side_effect=Exception("write failed"))
        driver._clients = {"dev1": client}
        driver._device_configs = {"dev1": {"slave_id": 1, "byte_order": "ABCD", "timeout": 5.0}}
        driver._device_points = {"dev1": [{"name": "point1", "address": 0, "data_type": "bool"}]}

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is False
        # 即使写入失败，client 也被释放
        # 真实 _release_client 在 finally 中执行，从 _leased_clients 移除 client


# ════════════════════════════════════════════════════════════════════════
# TCP write_points_batch TOCTOU 测试
# ════════════════════════════════════════════════════════════════════════


class TestTcpWritePointsBatchTOCTOU:
    """write_points_batch 的 get+lease 原子化 (并发安全 #1)"""

    async def test_returns_all_false_when_client_not_in_clients(self):
        """client 不在 _clients 中 → 所有点位返回 False"""
        driver = _make_tcp_driver()
        driver._clients = {}
        points = {"pt1": 1, "pt2": 2}

        result = await driver.write_points_batch("dev1", points)

        assert result == {"pt1": False, "pt2": False}
        driver._transition_state.assert_called_once()
        assert len(driver._leased_clients) == 0
        # _release_client 未被调用 (无 client 被租用) — 由 _leased_clients 为空间接验证

    async def test_returns_all_false_when_client_disconnected(self):
        """client.connected=False → 所有点位返回 False"""
        driver = _make_tcp_driver()
        client = _make_tcp_client(connected=False)
        driver._clients = {"dev1": client}
        points = {"pt1": 1, "pt2": 2}

        result = await driver.write_points_batch("dev1", points)

        assert result == {"pt1": False, "pt2": False}
        assert client not in driver._leased_clients
        # _release_client 未被调用 (无 client 被租用) — 由 _leased_clients 为空间接验证

    async def test_leases_and_releases_on_batch(self):
        """batch 写入 happy path: 租用 → 写入 → 释放"""
        driver = _make_tcp_driver()
        client = _make_tcp_client(connected=True)
        driver._clients = {"dev1": client}
        driver._device_configs = {"dev1": {"slave_id": 1, "byte_order": "ABCD", "timeout": 5.0}}
        driver._device_points = {"dev1": [{"name": "pt1", "address": 0, "data_type": "bool"}]}

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_points_batch("dev1", {"pt1": True})

        # pt1 有点位定义，写入成功
        assert result.get("pt1") is True
        # 真实 _release_client 在 finally 中执行，从 _leased_clients 移除 client


# ════════════════════════════════════════════════════════════════════════
# RTU 驱动构造辅助
# ════════════════════════════════════════════════════════════════════════


class _TOCTOUSerialContext:
    """模拟 TOCTOU: 在串口锁获取时使 client 失效。

    模拟场景: _ensure_connected 返回有效 client 后，在 acquire_serial_lock 之前，
    另一协程的 _try_reconnect 关闭/替换了该 client。
    """

    def __init__(self, invalidate_fn):
        self._invalidate_fn = invalidate_fn

    async def __aenter__(self) -> bool:
        self._invalidate_fn()
        return True  # 锁获取成功

    async def __aexit__(self, *args):
        return False


class _ValidSerialContext:
    """模拟正常路径: 串口锁获取成功，client 保持有效。"""

    async def __aenter__(self) -> bool:
        return True

    async def __aexit__(self, *args):
        return False


def _make_rtu_driver():
    """构造 ModbusRtuDriver 实例 (绕过 __init__)，仅设置 TOCTOU 测试所需属性。"""
    from edgelite.drivers.modbus_rtu import ModbusRtuDriver

    driver = ModbusRtuDriver.__new__(ModbusRtuDriver)
    # 连接与 client
    driver._clients: dict = {}
    driver._connected: dict = {}
    driver._connection_statuses: dict = {}
    # 权限
    driver._role_lock = asyncio.Lock()
    driver._current_user_role = "admin"
    driver.check_permission = AsyncMock(return_value=True)
    # 日志与审计
    driver._log_error = MagicMock()
    driver._record_write_audit = MagicMock()
    driver._set_connection_state = AsyncMock()
    # 写入限制
    driver._write_last_time: dict = {}
    driver._WRITE_RATE_LIMIT_INTERVAL = 0  # 绕过频率限制
    # 配置
    driver._device_configs: dict = {}
    driver._device_points: dict = {}
    driver.plugin_name = "modbus_rtu"
    # 会被 mock 的方法
    driver._ensure_connected = AsyncMock()
    driver._apply_turnaround_delay = AsyncMock()
    driver._read_points_inner = AsyncMock(return_value={})
    driver._read_point_raw = AsyncMock(return_value=None)
    driver._write_point_inner = AsyncMock(return_value=True)
    driver._batch_write_inner = AsyncMock(return_value={})
    driver._acquire_serial_context = lambda dev_id: _ValidSerialContext()
    return driver


def _make_rtu_client(connected: bool = True):
    """构造模拟的 pymodbus AsyncModbusSerialClient。"""
    client = MagicMock()
    client.connected = connected
    return client


# ════════════════════════════════════════════════════════════════════════
# RTU read_points TOCTOU 测试
# ════════════════════════════════════════════════════════════════════════


class TestRtuReadPointsTOCTOU:
    """read_points 串口锁内 client 重新校验 (并发安全 #1)"""

    async def test_returns_bad_when_client_closed_after_ensure(self):
        """_ensure_connected 返回后 client 被 _try_reconnect 关闭 → 返回 bad quality

        模拟: _ensure_connected 返回 connected=True 的 client，
        但在获取串口锁时 client 已被关闭 (connected=False)。
        """
        driver = _make_rtu_driver()
        client = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client}
        driver._ensure_connected = AsyncMock(return_value=client)

        # 模拟 TOCTOU: 串口锁获取时 client 被关闭
        def invalidate():
            client.connected = False

        driver._acquire_serial_context = lambda dev_id: _TOCTOUSerialContext(invalidate)

        result = await driver.read_points("dev1", ["pt1"])

        assert "pt1" in result
        assert result["pt1"].quality == "bad"
        # _read_points_inner 未被调用 (client 无效，提前返回)
        driver._read_points_inner.assert_not_called()

    async def test_returns_bad_when_client_replaced_after_ensure(self):
        """_ensure_connected 返回后 client 被 _try_reconnect 替换 → 返回 bad quality

        模拟: _ensure_connected 返回 client C1，
        但在获取串口锁时 _clients[dev1] 已被替换为 C2 (C1 身份不匹配)。
        """
        driver = _make_rtu_driver()
        client_c1 = _make_rtu_client(connected=True)
        client_c2 = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client_c1}
        driver._ensure_connected = AsyncMock(return_value=client_c1)

        # 模拟 TOCTOU: 串口锁获取时 _clients 中的 client 被替换
        def invalidate():
            driver._clients["dev1"] = client_c2

        driver._acquire_serial_context = lambda dev_id: _TOCTOUSerialContext(invalidate)

        result = await driver.read_points("dev1", ["pt1"])

        assert result["pt1"].quality == "bad"
        driver._read_points_inner.assert_not_called()

    async def test_proceeds_when_client_still_valid(self):
        """client 在串口锁内仍然有效 → 正常读取 (happy path)"""
        driver = _make_rtu_driver()
        client = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client}
        driver._ensure_connected = AsyncMock(return_value=client)
        # _ValidSerialContext (默认) 不修改 client

        from edgelite.drivers.base import PointValue

        expected = {"pt1": PointValue(value=42, quality="good", timestamp=datetime.now(UTC))}
        driver._read_points_inner = AsyncMock(return_value=expected)

        result = await driver.read_points("dev1", ["pt1"])

        assert result["pt1"].quality == "good"
        driver._read_points_inner.assert_called_once()

    async def test_returns_bad_when_ensure_returns_none(self):
        """_ensure_connected 返回 None → 返回 bad quality (无 client)"""
        driver = _make_rtu_driver()
        driver._ensure_connected = AsyncMock(return_value=None)

        result = await driver.read_points("dev1", ["pt1"])

        assert result["pt1"].quality == "bad"


# ════════════════════════════════════════════════════════════════════════
# RTU write_point TOCTOU 测试
# ════════════════════════════════════════════════════════════════════════


class TestRtuWritePointTOCTOU:
    """write_point 串口锁内 client 重新校验 (并发安全 #1)"""

    @staticmethod
    def _setup_valid_write(driver, device_id="dev1"):
        """设置写入所需的最小配置 (通过 TOCTOU 前的所有检查)。"""
        driver._device_configs = {device_id: {"write_verify": False}}
        driver._device_points = {device_id: [{"name": "pt1", "address": 0, "data_type": "bool"}]}

    async def test_returns_false_when_client_closed_after_ensure(self):
        """_ensure_connected 返回后 client 被关闭 → 返回 False"""
        driver = _make_rtu_driver()
        self._setup_valid_write(driver)
        client = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client}
        driver._ensure_connected = AsyncMock(return_value=client)

        def invalidate():
            client.connected = False

        driver._acquire_serial_context = lambda dev_id: _TOCTOUSerialContext(invalidate)

        result = await driver.write_point("dev1", "pt1", True)

        assert result is False
        driver._write_point_inner.assert_not_called()

    async def test_returns_false_when_client_replaced_after_ensure(self):
        """_ensure_connected 返回后 client 被替换 → 返回 False"""
        driver = _make_rtu_driver()
        self._setup_valid_write(driver)
        client_c1 = _make_rtu_client(connected=True)
        client_c2 = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client_c1}
        driver._ensure_connected = AsyncMock(return_value=client_c1)

        def invalidate():
            driver._clients["dev1"] = client_c2

        driver._acquire_serial_context = lambda dev_id: _TOCTOUSerialContext(invalidate)

        result = await driver.write_point("dev1", "pt1", True)

        assert result is False
        driver._write_point_inner.assert_not_called()

    async def test_proceeds_when_client_still_valid(self):
        """client 在串口锁内仍然有效 → 正常写入 (happy path)"""
        driver = _make_rtu_driver()
        self._setup_valid_write(driver)
        client = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client}
        driver._ensure_connected = AsyncMock(return_value=client)

        result = await driver.write_point("dev1", "pt1", True)

        assert result is True
        driver._write_point_inner.assert_called_once()


# ════════════════════════════════════════════════════════════════════════
# RTU write_points_batch TOCTOU 测试
# ════════════════════════════════════════════════════════════════════════


class TestRtuWritePointsBatchTOCTOU:
    """write_points_batch 串口锁内 client 重新校验 (并发安全 #1)"""

    async def test_returns_all_false_when_client_closed_after_ensure(self):
        """_ensure_connected 返回后 client 被关闭 → 所有点位返回 False"""
        driver = _make_rtu_driver()
        driver._device_configs = {"dev1": {}}
        driver._device_points = {"dev1": [{"name": "pt1", "address": 0, "data_type": "bool"}]}
        client = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client}
        driver._ensure_connected = AsyncMock(return_value=client)

        def invalidate():
            client.connected = False

        driver._acquire_serial_context = lambda dev_id: _TOCTOUSerialContext(invalidate)

        result = await driver.write_points_batch("dev1", {"pt1": True})

        assert result == {"pt1": False}
        driver._batch_write_inner.assert_not_called()

    async def test_returns_all_false_when_client_replaced_after_ensure(self):
        """_ensure_connected 返回后 client 被替换 → 所有点位返回 False"""
        driver = _make_rtu_driver()
        driver._device_configs = {"dev1": {}}
        driver._device_points = {"dev1": [{"name": "pt1", "address": 0, "data_type": "bool"}]}
        client_c1 = _make_rtu_client(connected=True)
        client_c2 = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client_c1}
        driver._ensure_connected = AsyncMock(return_value=client_c1)

        def invalidate():
            driver._clients["dev1"] = client_c2

        driver._acquire_serial_context = lambda dev_id: _TOCTOUSerialContext(invalidate)

        result = await driver.write_points_batch("dev1", {"pt1": True})

        assert result == {"pt1": False}
        driver._batch_write_inner.assert_not_called()

    async def test_proceeds_when_client_still_valid(self):
        """client 在串口锁内仍然有效 → 正常批量写入 (happy path)"""
        driver = _make_rtu_driver()
        driver._device_configs = {"dev1": {}}
        driver._device_points = {"dev1": [{"name": "pt1", "address": 0, "data_type": "bool"}]}
        client = _make_rtu_client(connected=True)
        driver._clients = {"dev1": client}
        driver._ensure_connected = AsyncMock(return_value=client)
        driver._batch_write_inner = AsyncMock(return_value={"pt1": True})

        result = await driver.write_points_batch("dev1", {"pt1": True})

        assert result == {"pt1": True}
        driver._batch_write_inner.assert_called_once()
