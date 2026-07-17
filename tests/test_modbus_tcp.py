"""Modbus TCP 驱动单元测试

覆盖 src/edgelite/drivers/modbus_tcp.py 的纯函数与类元数据：
- _slave_kwarg / _read_kwargs（TCP 模式：允许 slave_id=0 广播）
- _resolve_error_code（异常类型 → 错误码映射）
- _bad_pv（错误点值构造）
- ModbusTcpDriver 类元数据（plugin_name / supported_protocols / config_schema）

设计要点：
- 纯函数测试不依赖 pymodbus 连接，验证参数校验与返回格式
- TCP 与 RTU 的关键差异：allow_broadcast=True（slave_id=0 合法）
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from edgelite.api.error_codes import ModbusDriverErrors
from edgelite.drivers.base import PointValue
from edgelite.drivers.modbus_tcp import ModbusTcpDriver, _bad_pv, _read_kwargs, _resolve_error_code, _slave_kwarg

# ── _slave_kwarg（TCP: allow_broadcast=True）──


class TestSlaveKwargTcp:
    def test_broadcast_slave_id_zero_allowed(self):
        """TCP 模式允许 slave_id=0（广播写）。"""
        result = _slave_kwarg(0)
        # 返回 dict 包含 slave 参数名
        assert isinstance(result, dict)
        assert 0 in result.values()

    def test_normal_slave_id(self):
        result = _slave_kwarg(1)
        assert isinstance(result, dict)
        assert 1 in result.values()

    def test_max_slave_id(self):
        result = _slave_kwarg(247)
        assert 247 in result.values()

    def test_slave_id_too_large_raises(self):
        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(248)

    def test_negative_slave_id_raises(self):
        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(-1)


# ── _read_kwargs ──


class TestReadKwargsTcp:
    def test_returns_slave_and_count(self):
        result = _read_kwargs(count=10, slave_id=1)
        assert isinstance(result, dict)
        assert result.get("count") == 10
        assert 1 in result.values()

    def test_broadcast_read(self):
        result = _read_kwargs(count=5, slave_id=0)
        assert result.get("count") == 5
        assert 0 in result.values()

    def test_invalid_slave_raises(self):
        with pytest.raises(ValueError):
            _read_kwargs(count=1, slave_id=999)


# ── _resolve_error_code ──


class TestResolveErrorCode:
    def test_modbus_exception_maps_to_read_exception(self):
        from pymodbus.exceptions import ModbusException

        code = _resolve_error_code(ModbusException("test"))
        assert code == ModbusDriverErrors.READ_EXCEPTION

    def test_timeout_maps_to_read_timeout(self):
        code = _resolve_error_code(TimeoutError("timeout"))
        assert code == ModbusDriverErrors.READ_TIMEOUT

    def test_generic_exception_maps_to_read_failed(self):
        code = _resolve_error_code(RuntimeError("unknown"))
        assert code == ModbusDriverErrors.READ_FAILED

    def test_connection_error_maps_to_read_failed(self):
        code = _resolve_error_code(ConnectionError("refused"))
        assert code == ModbusDriverErrors.READ_FAILED


# ── _bad_pv ──


class TestBadPv:
    def test_returns_bad_quality_point_value(self):
        pv = _bad_pv("TEST_ERROR")
        assert isinstance(pv, PointValue)
        assert pv.quality == "bad"
        assert pv.value is None
        assert "TEST_ERROR" in pv.source

    def test_timestamp_is_recent(self):
        before = datetime.now(UTC)
        pv = _bad_pv("ERR")
        after = datetime.now(UTC)
        assert before <= pv.timestamp <= after


# ── ModbusTcpDriver 类元数据 ──


class TestModbusTcpDriverMetadata:
    def test_plugin_name(self):
        assert ModbusTcpDriver.plugin_name == "modbus_tcp"

    def test_supported_protocols(self):
        assert "modbus_tcp" in ModbusTcpDriver.supported_protocols
        assert "modbus-tcp" in ModbusTcpDriver.supported_protocols

    def test_required_dependencies(self):
        assert "pymodbus" in ModbusTcpDriver._required_dependencies

    def test_config_schema_has_required_fields(self):
        schema = ModbusTcpDriver.config_schema
        assert "host" in schema["required"]
        assert "port" in schema["required"]
        assert "slave_id" in schema["required"]

    def test_config_schema_properties(self):
        props = ModbusTcpDriver.config_schema["properties"]
        assert "host" in props
        assert "port" in props
        assert "slave_id" in props
        assert "timeout" in props
        assert "byte_order" in props

    def test_config_schema_byte_order_enum(self):
        props = ModbusTcpDriver.config_schema["properties"]
        assert set(props["byte_order"]["enum"]) == {"ABCD", "BADC", "CDAB", "DCBA"}

    def test_reconnect_attempts(self):
        assert ModbusTcpDriver._MAX_RECONNECT_ATTEMPTS >= 3
