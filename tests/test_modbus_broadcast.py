"""Modbus 广播模式 slave_id 校验修正单元测试 (并发安全 #2)

覆盖 P1 修复 (modbus_tcp.py write_point):

**原问题**:
    broadcast_enabled = config.get("broadcast", False)
    if slave_id == 0 or broadcast_enabled:
        # 走广播写入路径 (slave_id=0)

当 `broadcast_enabled=True` 时，即使 slave_id 是特定设备 (1-247)，
也会走广播写入路径 (slave_id=0)，导致写入被发送到总线上所有设备 —
安全/正确性隐患。

**修复**:
    broadcast 仅由 slave_id==0 决定；broadcast_enabled 作为"允许广播"开关。
    - slave_id=0 + broadcast_enabled=True  → 广播写入 (slave_id=0)
    - slave_id=0 + broadcast_enabled=False → 拒绝写入 (BCAST_NOT_ENABLED)
    - slave_id=1-247 (任意 broadcast_enabled) → 正常单播写入

本测试通过 mock _broadcast_write / client.write_coil 等方法验证调用路径。
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, "src")


# ════════════════════════════════════════════════════════════════════════
# 驱动构造辅助
# ════════════════════════════════════════════════════════════════════════


def _make_tcp_driver():
    """构造 ModbusTcpDriver 实例 (绕过 __init__)，仅设置广播测试所需属性。"""
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
    # _broadcast_write 默认 mock 为返回 True (成功)
    driver._broadcast_write = AsyncMock(return_value=True)
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


def _setup_device(driver, device_id: str = "dev1", slave_id: int = 1,
                  broadcast: bool = False, point: str = "point1",
                  data_type: str = "bool", address: int = 0):
    """配置一个设备 + 一个点位，返回 mock client。"""
    client = _make_tcp_client(connected=True)
    driver._clients = {device_id: client}
    driver._device_configs = {
        device_id: {
            "slave_id": slave_id,
            "broadcast": broadcast,
            "byte_order": "ABCD",
            "timeout": 5.0,
        }
    }
    driver._device_points = {
        device_id: [{"name": point, "address": address, "data_type": data_type}]
    }
    return client


# ════════════════════════════════════════════════════════════════════════
# 测试组 1: slave_id=0 广播路径
# ════════════════════════════════════════════════════════════════════════


class TestSlaveIdZeroBroadcastPath:
    """slave_id=0 时的广播写入路径测试"""

    async def test_slave_id_zero_with_broadcast_enabled_calls_broadcast_write(self):
        """slave_id=0 + broadcast=True → 调用 _broadcast_write (slave_id=0)"""
        driver = _make_tcp_driver()
        _setup_device(driver, slave_id=0, broadcast=True)
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is True
        driver._broadcast_write.assert_awaited_once()
        # 验证 audit 记录为 ok
        audit_args = driver._audit_write.call_args
        assert audit_args[0][4] == "ok"  # result 参数位置

    async def test_slave_id_zero_with_broadcast_disabled_rejects(self):
        """slave_id=0 + broadcast=False → 拒绝写入 (BCAST_NOT_ENABLED)"""
        driver = _make_tcp_driver()
        _setup_device(driver, slave_id=0, broadcast=False)
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is False
        # 不应调用广播写入
        driver._broadcast_write.assert_not_awaited()
        # 应记录 BCAST_NOT_ENABLED 错误
        error_calls = driver._log_error.call_args_list
        assert any("BCAST_NOT_ENABLED" in str(c) for c in error_calls), \
            f"Expected BCAST_NOT_ENABLED error, got: {error_calls}"
        # 应记录 rejected audit
        audit_args = driver._audit_write.call_args
        assert audit_args[0][4] == "rejected"
        assert "broadcast not enabled" in str(audit_args[0][5])

    async def test_slave_id_zero_with_broadcast_missing_defaults_to_reject(self):
        """slave_id=0 + 配置中无 broadcast 字段 → 默认 False → 拒绝写入

        防止误配置 slave_id=0 但忘记启用 broadcast 时静默走广播路径。
        """
        driver = _make_tcp_driver()
        client = _make_tcp_client(connected=True)
        driver._clients = {"dev1": client}
        # 故意不设置 broadcast 字段
        driver._device_configs = {
            "dev1": {"slave_id": 0, "byte_order": "ABCD", "timeout": 5.0}
        }
        driver._device_points = {"dev1": [{"name": "point1", "address": 0, "data_type": "bool"}]}
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is False
        driver._broadcast_write.assert_not_awaited()
        error_calls = driver._log_error.call_args_list
        assert any("BCAST_NOT_ENABLED" in str(c) for c in error_calls)

    async def test_slave_id_zero_broadcast_failure_returns_false(self):
        """slave_id=0 + broadcast=True + _broadcast_write 返回 False → 返回 False"""
        driver = _make_tcp_driver()
        _setup_device(driver, slave_id=0, broadcast=True)
        driver._broadcast_write = AsyncMock(return_value=False)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is False
        driver._broadcast_write.assert_awaited_once()
        audit_args = driver._audit_write.call_args
        assert audit_args[0][4] == "failed"

    async def test_slave_id_zero_broadcast_timeout_returns_false(self):
        """slave_id=0 + broadcast=True + _broadcast_write 超时 → 返回 False + WRITE_TIMEOUT 日志"""
        driver = _make_tcp_driver()
        _setup_device(driver, slave_id=0, broadcast=True)

        async def _hang(*args, **kwargs):
            await asyncio.sleep(100)  # 模拟超时

        driver._broadcast_write = _hang

        # 设置极短超时触发 TimeoutError
        driver._device_configs["dev1"]["timeout"] = 0.05

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is False
        error_calls = driver._log_error.call_args_list
        assert any("WRITE_TIMEOUT" in str(c) for c in error_calls), \
            f"Expected WRITE_TIMEOUT error, got: {error_calls}"


# ════════════════════════════════════════════════════════════════════════
# 测试组 2: slave_id 1-247 单播路径 (核心回归: 不应走广播)
# ════════════════════════════════════════════════════════════════════════


class TestUnicastPathNotBroadcast:
    """slave_id=1-247 时即使 broadcast_enabled=True 也走单播路径 (核心修复)"""

    async def test_slave_id_one_with_broadcast_enabled_uses_unicast(self):
        """slave_id=1 + broadcast=True → 走单播写入 (调用 client.write_coil, 不调用 _broadcast_write)

        这是核心回归测试: 原代码 `if slave_id == 0 or broadcast_enabled` 会错误走广播路径。
        """
        driver = _make_tcp_driver()
        client = _setup_device(driver, slave_id=1, broadcast=True, data_type="bool")
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is True
        # 关键断言: 不应走广播路径
        driver._broadcast_write.assert_not_awaited()
        # 应走单播路径: 调用 client.write_coil
        client.write_coil.assert_awaited_once()

    async def test_slave_id_247_with_broadcast_enabled_uses_unicast(self):
        """slave_id=247 (最大单播地址) + broadcast=True → 走单播"""
        driver = _make_tcp_driver()
        client = _setup_device(driver, slave_id=247, broadcast=True, data_type="bool")
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is True
        driver._broadcast_write.assert_not_awaited()
        client.write_coil.assert_awaited_once()

    async def test_slave_id_one_with_broadcast_disabled_uses_unicast(self):
        """slave_id=1 + broadcast=False → 走单播 (正常配置)"""
        driver = _make_tcp_driver()
        client = _setup_device(driver, slave_id=1, broadcast=False, data_type="bool")
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is True
        driver._broadcast_write.assert_not_awaited()
        client.write_coil.assert_awaited_once()

    async def test_unicast_uses_correct_slave_id_not_zero(self):
        """单播路径使用配置的 slave_id, 不使用 0 (广播地址)

        通过检查 client.write_coil 调用参数中的 slave 参数验证。
        """
        driver = _make_tcp_driver()
        client = _setup_device(driver, slave_id=42, broadcast=True, data_type="bool")
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            await driver.write_point("dev1", "point1", True)

        # 检查 write_coil 调用参数 — slave 应为 42, 不是 0
        call_kwargs = client.write_coil.call_args.kwargs
        # pymodbus 使用 slave 或 slave_id 参数
        slave_value = call_kwargs.get("slave") or call_kwargs.get("slave_id")
        assert slave_value == 42, f"Expected slave_id=42, got {slave_value}"

    async def test_unicast_float32_write(self):
        """单播 float32 写入路径 (验证非 bool 数据类型也走单播)"""
        driver = _make_tcp_driver()
        client = _setup_device(driver, slave_id=10, broadcast=True,
                               data_type="float32", address=100)
        driver._broadcast_write = AsyncMock(return_value=True)
        driver._encode_value = MagicMock(return_value=[0, 0])  # mock 编码

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", 3.14)

        assert result is True
        driver._broadcast_write.assert_not_awaited()
        client.write_registers.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════════
# 测试组 3: 边界与回归
# ════════════════════════════════════════════════════════════════════════


class TestBroadcastBoundaryAndRegression:
    """边界条件和回归保护"""

    async def test_broadcast_enabled_no_slave_id_uses_default_unicast(self):
        """broadcast=True + 配置中无 slave_id → 默认 slave_id=1 → 走单播"""
        driver = _make_tcp_driver()
        client = _make_tcp_client(connected=True)
        driver._clients = {"dev1": client}
        # 故意不设置 slave_id (默认 1)
        driver._device_configs = {"dev1": {"broadcast": True, "byte_order": "ABCD", "timeout": 5.0}}
        driver._device_points = {"dev1": [{"name": "point1", "address": 0, "data_type": "bool"}]}
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            result = await driver.write_point("dev1", "point1", True)

        assert result is True
        # 默认 slave_id=1 → 走单播, 不走广播
        driver._broadcast_write.assert_not_awaited()
        client.write_coil.assert_awaited_once()

    async def test_audit_recorded_on_broadcast_reject(self):
        """slave_id=0 + broadcast=False 拒绝写入时记录 audit (rejected)"""
        driver = _make_tcp_driver()
        _setup_device(driver, slave_id=0, broadcast=False)
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            await driver.write_point("dev1", "point1", 42)

        driver._audit_write.assert_called_once()
        args = driver._audit_write.call_args[0]
        # args: (device_id, point_name, old_value, new_value, result, error_msg)
        assert args[0] == "dev1"
        assert args[1] == "point1"
        assert args[3] == 42  # new_value
        assert args[4] == "rejected"
        assert "broadcast not enabled" in args[5]

    async def test_audit_recorded_on_broadcast_success(self):
        """slave_id=0 + broadcast=True 广播成功时记录 audit (ok)"""
        driver = _make_tcp_driver()
        _setup_device(driver, slave_id=0, broadcast=True)
        driver._broadcast_write = AsyncMock(return_value=True)

        import edgelite.drivers.modbus_tcp as modbus_tcp_mod
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(modbus_tcp_mod, "record_packet", MagicMock())
            await driver.write_point("dev1", "point1", 42)

        driver._audit_write.assert_called_once()
        args = driver._audit_write.call_args[0]
        assert args[4] == "ok"

    async def test_no_client_returns_false_before_broadcast_check(self):
        """无连接 client 时 → 在广播检查前就返回 False (不调用 _broadcast_write)

        验证: client=None 的早期返回先于广播逻辑执行。
        """
        driver = _make_tcp_driver()
        driver._clients = {}  # 无 client
        driver._device_configs = {"dev1": {"slave_id": 0, "broadcast": True}}
        driver._device_points = {"dev1": [{"name": "point1", "address": 0, "data_type": "bool"}]}
        driver._broadcast_write = AsyncMock(return_value=True)

        result = await driver.write_point("dev1", "point1", True)

        assert result is False
        driver._broadcast_write.assert_not_awaited()
        driver._transition_state.assert_called_once()

    async def test_point_not_found_returns_false(self):
        """点位不存在 → 返回 False (在广播检查前)"""
        driver = _make_tcp_driver()
        client = _make_tcp_client(connected=True)
        driver._clients = {"dev1": client}
        driver._device_configs = {"dev1": {"slave_id": 0, "broadcast": True}}
        driver._device_points = {"dev1": [{"name": "other_point", "address": 0, "data_type": "bool"}]}
        driver._broadcast_write = AsyncMock(return_value=True)

        result = await driver.write_point("dev1", "point1", True)

        assert result is False
        driver._broadcast_write.assert_not_awaited()


# ════════════════════════════════════════════════════════════════════════
# 测试组 4: 条件逻辑单元测试 (无需驱动实例)
# ════════════════════════════════════════════════════════════════════════


class TestBroadcastConditionLogic:
    """直接验证条件逻辑 (无需 asyncio)

    复现修复后的条件判断逻辑，验证所有组合的正确性。
    """

    @staticmethod
    def _should_broadcast(slave_id: int, broadcast_enabled: bool) -> tuple[bool, bool]:
        """复现修复后的 write_point 广播判断逻辑。

        返回 (走广播路径, 拒绝写入)。
        - 走广播路径=True + 拒绝=False → 广播写入
        - 走广播路径=False + 拒绝=False → 正常单播
        - 拒绝=True → 拒绝写入 (slave_id=0 但 broadcast 未启用)
        """
        if slave_id == 0:
            if not broadcast_enabled:
                return False, True  # 拒绝
            return True, False  # 广播写入
        return False, False  # 正常单播

    def test_slave_zero_broadcast_enabled_goes_broadcast(self):
        """slave_id=0 + broadcast=True → 走广播, 不拒绝"""
        goes_broadcast, rejected = self._should_broadcast(0, True)
        assert goes_broadcast is True
        assert rejected is False

    def test_slave_zero_broadcast_disabled_rejected(self):
        """slave_id=0 + broadcast=False → 拒绝"""
        goes_broadcast, rejected = self._should_broadcast(0, False)
        assert goes_broadcast is False
        assert rejected is True

    def test_slave_one_broadcast_enabled_goes_unicast(self):
        """slave_id=1 + broadcast=True → 单播 (核心修复点)"""
        goes_broadcast, rejected = self._should_broadcast(1, True)
        assert goes_broadcast is False
        assert rejected is False

    def test_slave_one_broadcast_disabled_goes_unicast(self):
        """slave_id=1 + broadcast=False → 单播"""
        goes_broadcast, rejected = self._should_broadcast(1, False)
        assert goes_broadcast is False
        assert rejected is False

    def test_slave_247_broadcast_enabled_goes_unicast(self):
        """slave_id=247 + broadcast=True → 单播"""
        goes_broadcast, rejected = self._should_broadcast(247, True)
        assert goes_broadcast is False
        assert rejected is False

    @pytest.mark.parametrize("slave_id", [1, 2, 100, 247])
    def test_all_valid_unicast_ids_never_broadcast(self, slave_id):
        """参数化: 所有合法单播 id (1-247) 永远不走广播, 无论 broadcast_enabled"""
        for broadcast_enabled in (True, False):
            goes_broadcast, rejected = self._should_broadcast(slave_id, broadcast_enabled)
            assert goes_broadcast is False, \
                f"slave_id={slave_id} broadcast={broadcast_enabled} should not broadcast"
            assert rejected is False, \
                f"slave_id={slave_id} broadcast={broadcast_enabled} should not reject"

    @pytest.mark.parametrize("broadcast_enabled", [True, False])
    def test_slave_zero_always_enteres_broadcast_branch(self, broadcast_enabled):
        """参数化: slave_id=0 永远进入广播分支 (但 broadcast_enabled=False 时拒绝)"""
        goes_broadcast, rejected = self._should_broadcast(0, broadcast_enabled)
        if broadcast_enabled:
            assert goes_broadcast is True and rejected is False
        else:
            assert goes_broadcast is False and rejected is True
