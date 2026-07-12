"""Modbus RTU 驱动单元测试

覆盖 src/edgelite/drivers/modbus_rtu.py 的纯函数与类元数据：
- _slave_kwarg / _read_kwargs（RTU 模式：禁止 slave_id=0 广播）
- ModbusRtuDriver 类元数据（plugin_name / supported_protocols / config_schema）
- _CRCReconnectNeeded 异常

设计要点：
- RTU 与 TCP 的关键差异：allow_broadcast=False（slave_id=0 非法）
- 纯函数测试不依赖串口/pymodbus 连接
"""

from __future__ import annotations

import pytest

from edgelite.drivers.modbus_rtu import ModbusRtuDriver, _CRCReconnectNeeded, _read_kwargs, _slave_kwarg


# ── _slave_kwarg（RTU: allow_broadcast=False）──


class TestSlaveKwargRtu:
    def test_normal_slave_id(self):
        result = _slave_kwarg(1)
        assert isinstance(result, dict)
        assert 1 in result.values()

    def test_max_slave_id(self):
        result = _slave_kwarg(247)
        assert 247 in result.values()

    def test_broadcast_slave_id_zero_rejected(self):
        """RTU 模式禁止 slave_id=0（广播无响应且与单主总线语义冲突）。"""
        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(0)

    def test_slave_id_too_large_raises(self):
        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(248)

    def test_negative_slave_id_raises(self):
        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(-1)


# ── _read_kwargs ──


class TestReadKwargsRtu:
    def test_returns_slave_and_count(self):
        result = _read_kwargs(count=10, slave_id=1)
        assert isinstance(result, dict)
        assert result.get("count") == 10
        assert 1 in result.values()

    def test_broadcast_read_rejected(self):
        """RTU 模式禁止 slave_id=0 读取。"""
        with pytest.raises(ValueError):
            _read_kwargs(count=5, slave_id=0)

    def test_invalid_slave_raises(self):
        with pytest.raises(ValueError):
            _read_kwargs(count=1, slave_id=999)


# ── _CRCReconnectNeeded ──


class TestCRCReconnectNeeded:
    def test_message_stored(self):
        exc = _CRCReconnectNeeded("CRC mismatch")
        assert str(exc) == "CRC mismatch"

    def test_partial_result_default_empty(self):
        exc = _CRCReconnectNeeded("CRC mismatch")
        assert exc.partial_result == {}

    def test_partial_result_provided(self):
        partial = {"point1": 42}
        exc = _CRCReconnectNeeded("CRC mismatch", partial_result=partial)
        assert exc.partial_result == partial

    def test_is_exception(self):
        exc = _CRCReconnectNeeded("test")
        assert isinstance(exc, Exception)


# ── ModbusRtuDriver 类元数据 ──


class TestModbusRtuDriverMetadata:
    def test_plugin_name(self):
        assert ModbusRtuDriver.plugin_name == "modbus_rtu"

    def test_supported_protocols(self):
        assert "modbus_rtu" in ModbusRtuDriver.supported_protocols
        assert "modbus-rtu" in ModbusRtuDriver.supported_protocols

    def test_required_dependencies(self):
        assert "pymodbus" in ModbusRtuDriver._required_dependencies
        assert "serial" in ModbusRtuDriver._required_dependencies

    def test_config_schema_has_required_fields(self):
        schema = ModbusRtuDriver.config_schema
        assert "port" in schema["required"]
        assert "baudrate" in schema["required"]
        assert "unit_id" in schema["required"]

    def test_config_schema_properties(self):
        props = ModbusRtuDriver.config_schema["properties"]
        assert "port" in props
        assert "baudrate" in props
        assert "unit_id" in props

    def test_reconnect_attempts(self):
        assert ModbusRtuDriver._MAX_RECONNECT_ATTEMPTS >= 3

    def test_failover_threshold(self):
        assert ModbusRtuDriver._FAILOVER_FAIL_THRESHOLD >= 1

    def test_degrade_levels_is_tuple(self):
        """FIXED(P2): 可变默认值改为 tuple。"""
        assert isinstance(ModbusRtuDriver._DEGRADE_LEVELS, tuple)
