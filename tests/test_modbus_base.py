"""modbus_base 共享模块单元测试

覆盖 Task #17 P2 修复:
1. _MODBUS_EXCEPTION_CODES — 11 个标准 Modbus 异常码完整映射
2. _detect_slave_kwarg_name — pymodbus 2.x/3.0-3.6/3.7+ 版本检测
3. _slave_kwarg — allow_broadcast 差异化 (TCP 允许 0, RTU 禁止 0)
4. _set_client_slave_id — 兼容保留函数
5. _read_kwargs — count + slave 参数组合
6. _parse_modbus_exception — 已知码/未知码/非异常 (FIXED-P2: 未知码返回描述而非 None)
7. REGISTER_TYPES / DATA_TYPE_REGS / _BYTE_ORDER_FMT — 常量字典结构完整性

同时验证 modbus_tcp.py 和 modbus_rtu.py 正确集成了 modbus_base:
- TCP 的 _slave_kwarg 允许 slave_id=0 (广播写)
- RTU 的 _slave_kwarg 禁止 slave_id=0
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, "src")

from edgelite.drivers import modbus_base
from edgelite.drivers.modbus_base import (
    _BYTE_ORDER_FMT,
    _MODBUS_EXCEPTION_CODES,
    _PYMODBUS_MAJOR,
    _PYMODBUS_MINOR,
    DATA_TYPE_REGS,
    REGISTER_TYPES,
    _detect_slave_kwarg_name,
    _parse_modbus_exception,
    _read_kwargs,
    _set_client_slave_id,
    _slave_kwarg,
)

# ════════════════════════════════════════════════════════════════════════
# 1. _MODBUS_EXCEPTION_CODES 完整性
# ════════════════════════════════════════════════════════════════════════


class TestModbusExceptionCodes:
    """验证 Modbus 异常码映射表覆盖协议规范 §6 的全部 11 个标准异常码"""

    def test_all_11_standard_codes_present(self):
        """Modbus 协议定义了 11 个标准异常码 (0x01-0x0B, 其中 0x09 保留)"""
        assert len(_MODBUS_EXCEPTION_CODES) == 11

    def test_exception_code_values(self):
        """每个异常码键 = 功能码 | 0x80, 值为非空描述字符串"""
        for code, desc in _MODBUS_EXCEPTION_CODES.items():
            assert isinstance(code, int)
            assert code & 0x80 == 0x80, f"异常码 0x{code:02X} 缺少 0x80 标志位"
            assert isinstance(desc, str) and len(desc) > 0
            # 描述应包含十六进制异常码 (如 "Illegal Data Address (0x02)")
            assert "0x" in desc, f"异常码 0x{code:02X} 描述缺少十六进制标注: {desc}"

    @pytest.mark.parametrize("exc_code,expected_keyword", [
        (0x81, "Illegal Function"),
        (0x82, "Illegal Data Address"),
        (0x83, "Illegal Data Value"),
        (0x84, "Server Device Failure"),
        (0x85, "Acknowledge"),
        (0x86, "Server Device Busy"),
        (0x87, "Negative Acknowledge"),
        (0x88, "Memory Parity Error"),
        (0x8A, "Gateway Path Unavailable"),
        (0x8B, "Gateway Target Device Failed"),
        (0xAB, "Extended Exception"),
    ])
    def test_specific_exception_description(self, exc_code, expected_keyword):
        desc = _MODBUS_EXCEPTION_CODES[exc_code]
        assert expected_keyword in desc


# ════════════════════════════════════════════════════════════════════════
# 2. _detect_slave_kwarg_name 版本检测
# ════════════════════════════════════════════════════════════════════════


class TestDetectSlaveKwargName:
    """验证 pymodbus 版本到 slave 参数名的映射"""

    def test_returns_str_or_none(self):
        result = _detect_slave_kwarg_name()
        assert result is None or isinstance(result, str)

    def test_known_pymodbus_version_mapping(self):
        """基于当前安装的 pymodbus 版本验证返回值"""
        result = _detect_slave_kwarg_name()
        if _PYMODBUS_MAJOR < 3:
            assert result == "slave"
        elif _PYMODBUS_MAJOR == 3 and _PYMODBUS_MINOR < 7:
            assert result == "unit"
        else:
            assert result == "slave"

    def test_result_is_valid_kwarg_name(self):
        result = _detect_slave_kwarg_name()
        if result is not None:
            assert result in ("slave", "unit")


# ════════════════════════════════════════════════════════════════════════
# 3. _slave_kwarg — allow_broadcast 差异化
# ════════════════════════════════════════════════════════════════════════


class TestSlaveKwargBroadcast:
    """FIXED-P2: TCP (allow_broadcast=True) 允许 slave_id=0, RTU (默认) 禁止"""

    def test_broadcast_allowed_zero(self):
        """allow_broadcast=True 时 slave_id=0 应被接受"""
        kw = _slave_kwarg(0, allow_broadcast=True)
        # 应返回包含 slave 或 unit 的字典
        assert len(kw) == 1
        assert 0 in kw.values()

    def test_broadcast_rejected_zero(self):
        """allow_broadcast=False (RTU 默认) 时 slave_id=0 应抛 ValueError"""
        with pytest.raises(ValueError, match="1-247"):
            _slave_kwarg(0, allow_broadcast=False)

    def test_valid_slave_id_1(self):
        kw = _slave_kwarg(1, allow_broadcast=False)
        assert 1 in kw.values()

    def test_valid_slave_id_247(self):
        kw = _slave_kwarg(247, allow_broadcast=False)
        assert 247 in kw.values()

    def test_invalid_slave_id_248(self):
        with pytest.raises(ValueError, match="1-247"):
            _slave_kwarg(248, allow_broadcast=False)

    def test_invalid_slave_id_negative(self):
        with pytest.raises(ValueError):
            _slave_kwarg(-1, allow_broadcast=False)

    def test_broadcast_valid_247(self):
        """allow_broadcast=True 时 1-247 仍然有效"""
        kw = _slave_kwarg(247, allow_broadcast=True)
        assert 247 in kw.values()

    def test_broadcast_invalid_248(self):
        """allow_broadcast=True 时 248 仍然无效 (0 是唯一的广播地址)"""
        with pytest.raises(ValueError, match="0-247"):
            _slave_kwarg(248, allow_broadcast=True)

    def test_returns_dict_with_correct_key(self):
        """返回的字典键应为 'slave' 或 'unit' (取决于 pymodbus 版本)"""
        kw = _slave_kwarg(1, allow_broadcast=False)
        assert len(kw) == 1
        key = next(iter(kw))
        assert key in ("slave", "unit")


# ════════════════════════════════════════════════════════════════════════
# 4. _set_client_slave_id
# ════════════════════════════════════════════════════════════════════════


class TestSetClientSlaveId:
    """验证兼容保留函数 _set_client_slave_id

    注: _set_client_slave_id 仅在 _SLAVE_KWARG_NAME 未缓存 (pymodbus 2.x 场景) 时
    设置 client.slave_id。pymodbus 3.7+ per-call 模式下 _SLAVE_KWARG_NAME 已缓存,
    此函数为 no-op。测试需重置缓存以覆盖两条路径。
    """

    def test_sets_slave_id_when_kwarg_name_uncached(self):
        """_SLAVE_KWARG_NAME 为 None (未缓存) 时, 设置 client.slave_id"""
        import edgelite.drivers.modbus_base as _mb
        saved = _mb._SLAVE_KWARG_NAME
        _mb._SLAVE_KWARG_NAME = None
        try:
            client = MagicMock()
            client.slave_id = 0
            _set_client_slave_id(client, 42)
            assert client.slave_id == 42
        finally:
            _mb._SLAVE_KWARG_NAME = saved

    def test_noop_when_kwarg_name_cached(self):
        """_SLAVE_KWARG_NAME 已缓存 (3.7+ per-call 模式) 时不设置 client.slave_id"""
        client = MagicMock()
        client.slave_id = 0
        # _SLAVE_KWARG_NAME 已被之前的测试缓存为 "slave" 或 "unit"
        _set_client_slave_id(client, 42)
        assert client.slave_id == 0  # 未被修改

    def test_no_error_when_client_lacks_attr(self):
        """如果 client 没有 slave_id 属性, 不应抛异常"""
        import edgelite.drivers.modbus_base as _mb
        saved = _mb._SLAVE_KWARG_NAME
        _mb._SLAVE_KWARG_NAME = None
        try:
            client = MagicMock(spec=[])  # 空规格, 无任何属性
            _set_client_slave_id(client, 42)
            # 不抛异常即可
        finally:
            _mb._SLAVE_KWARG_NAME = saved


# ════════════════════════════════════════════════════════════════════════
# 5. _read_kwargs
# ════════════════════════════════════════════════════════════════════════


class TestReadKwargs:
    """验证 _read_kwargs 返回 count + slave 参数"""

    def test_contains_count(self):
        kw = _read_kwargs(5, 1, allow_broadcast=False)
        assert kw["count"] == 5

    def test_contains_slave(self):
        kw = _read_kwargs(5, 7, allow_broadcast=False)
        assert 7 in kw.values()

    def test_broadcast_zero_allowed(self):
        kw = _read_kwargs(3, 0, allow_broadcast=True)
        assert kw["count"] == 3
        assert 0 in kw.values()

    def test_rejects_zero_without_broadcast(self):
        with pytest.raises(ValueError):
            _read_kwargs(3, 0, allow_broadcast=False)

    def test_count_always_present(self):
        """FIXED: count 始终传递, 防止旧版本默认 count=1 导致 float32 等读取不完整"""
        for count in (1, 2, 4, 10, 100):
            kw = _read_kwargs(count, 1, allow_broadcast=False)
            assert kw["count"] == count


# ════════════════════════════════════════════════════════════════════════
# 6. _parse_modbus_exception — FIXED-P2 核心改进
# ════════════════════════════════════════════════════════════════════════


class TestParseModbusException:
    """FIXED-P2: 未映射异常码返回 "Unknown Exception (0xNN)" 而非 None"""

    def test_none_input_returns_none(self):
        assert _parse_modbus_exception(None) is None

    @pytest.mark.parametrize("exc_code,expected_desc", [
        (0x81, "Illegal Function"),
        (0x82, "Illegal Data Address"),
        (0x83, "Illegal Data Value"),
        (0x84, "Server Device Failure"),
        (0x86, "Server Device Busy"),
        (0x8A, "Gateway Path Unavailable"),
    ])
    def test_known_exception_from_raw_bytes(self, exc_code, expected_desc):
        """通过 raw bytes (Modbus 异常响应帧) 解析已知异常码"""
        # 异常响应帧: [功能码|0x80, 异常码]
        func_code = exc_code & 0x7F
        mock_result = MagicMock()
        mock_result.raw = bytes([func_code | 0x80, exc_code & 0x7F])
        mock_result.value = None
        desc = _parse_modbus_exception(mock_result)
        assert desc is not None
        assert expected_desc in desc

    def test_unknown_exception_code_returns_description(self):
        """FIXED-P2: 未映射的异常码 (如 0x09, 保留码) 应返回 "Unknown Exception (0x09)" 而非 None"""
        mock_result = MagicMock()
        mock_result.raw = bytes([0x83 | 0x80, 0x09])  # 功能码 0x03 + 保留异常码 0x09
        mock_result.value = None
        desc = _parse_modbus_exception(mock_result)
        assert desc is not None
        assert "Unknown Exception" in desc
        assert "0x09" in desc

    def test_unknown_exception_code_0x0c(self):
        """另一个未映射异常码 0x0C"""
        mock_result = MagicMock()
        mock_result.raw = bytes([0x83 | 0x80, 0x0C])
        mock_result.value = None
        desc = _parse_modbus_exception(mock_result)
        assert desc is not None
        assert "Unknown Exception" in desc

    def test_string_match_known_exception(self):
        """通过字符串匹配已知异常码 (当 raw 不可用时)"""
        mock_result = MagicMock()
        mock_result.raw = None
        mock_result.value = None
        mock_result.__str__ = lambda self: "Modbus error: Illegal Data Address (0x02)"
        desc = _parse_modbus_exception(mock_result)
        assert desc is not None
        assert "Illegal Data Address" in desc

    def test_string_with_hex_code_match(self):
        """通过十六进制字符串匹配"""
        mock_result = MagicMock()
        mock_result.raw = None
        mock_result.value = None
        mock_result.__str__ = lambda self: "Exception code 0x86"
        desc = _parse_modbus_exception(mock_result)
        assert desc is not None
        assert "Server Device Busy" in desc

    def test_string_with_unknown_exception_keyword(self):
        """FIXED-P2: 字符串包含 "exception" 但未匹配已知码 → 标记为 Unknown"""
        mock_result = MagicMock()
        mock_result.raw = None
        mock_result.value = None
        mock_result.__str__ = lambda self: "Modbus exception response code 0x99"
        desc = _parse_modbus_exception(mock_result)
        assert desc is not None
        assert "Unknown Exception" in desc

    def test_short_raw_returns_none(self):
        """raw 长度 < 2 字节时无法解析异常码"""
        mock_result = MagicMock()
        mock_result.raw = bytes([0x83])
        mock_result.value = None
        # 单字节 raw 不包含异常码, 且 __str__ 无异常关键字 → 返回 None
        mock_result.__str__ = lambda self: ""
        desc = _parse_modbus_exception(mock_result)
        assert desc is None

    def test_empty_raw_no_string_match_returns_none(self):
        """非异常响应 (无 raw, 无异常字符串) 返回 None"""
        mock_result = MagicMock()
        mock_result.raw = None
        mock_result.value = None
        mock_result.__str__ = lambda self: "success"
        desc = _parse_modbus_exception(mock_result)
        assert desc is None

    def test_raw_as_list(self):
        """raw 为 list 类型时也能正确解析"""
        mock_result = MagicMock()
        mock_result.raw = [0x83, 0x02]  # list 形式
        mock_result.value = None
        desc = _parse_modbus_exception(mock_result)
        assert desc is not None
        assert "Illegal Data Address" in desc

    def test_value_attribute_used_when_raw_none(self):
        """raw 为 None 时回退到 value 属性"""
        mock_result = MagicMock()
        mock_result.raw = None
        mock_result.value = bytes([0x83, 0x03])
        desc = _parse_modbus_exception(mock_result)
        assert desc is not None
        assert "Illegal Data Value" in desc


# ════════════════════════════════════════════════════════════════════════
# 7. 常量字典结构
# ════════════════════════════════════════════════════════════════════════


class TestConstantDictionaries:
    """验证寄存器类型/数据类型/字节序常量字典的完整性"""

    def test_register_types_has_4_entries(self):
        assert len(REGISTER_TYPES) == 4
        for name in ("coil", "discrete", "holding", "input"):
            assert name in REGISTER_TYPES

    def test_register_types_values(self):
        """每个值为 (功能码, 每寄存器字节数)"""
        assert REGISTER_TYPES["coil"] == (0, 1)
        assert REGISTER_TYPES["discrete"] == (1, 1)
        assert REGISTER_TYPES["holding"] == (3, 2)
        assert REGISTER_TYPES["input"] == (4, 2)

    def test_data_type_regs_has_8_entries(self):
        assert len(DATA_TYPE_REGS) == 8
        for name in ("bool", "int16", "uint16", "int32", "uint32", "float32", "float64", "string"):
            assert name in DATA_TYPE_REGS

    def test_data_type_regs_values(self):
        assert DATA_TYPE_REGS["bool"] == 1
        assert DATA_TYPE_REGS["int16"] == 1
        assert DATA_TYPE_REGS["uint16"] == 1
        assert DATA_TYPE_REGS["int32"] == 2
        assert DATA_TYPE_REGS["uint32"] == 2
        assert DATA_TYPE_REGS["float32"] == 2
        assert DATA_TYPE_REGS["float64"] == 4
        assert DATA_TYPE_REGS["string"] == 1

    def test_byte_order_fmt_has_4_entries(self):
        assert len(_BYTE_ORDER_FMT) == 4
        for name in ("ABCD", "BADC", "CDAB", "DCBA"):
            assert name in _BYTE_ORDER_FMT

    def test_byte_order_fmt_values_are_tuples(self):
        for _name, (reg_pack, val_unpack) in _BYTE_ORDER_FMT.items():
            assert reg_pack in (">", "<")
            assert val_unpack in (">", "<")


# ════════════════════════════════════════════════════════════════════════
# 8. modbus_tcp / modbus_rtu 集成验证
# ════════════════════════════════════════════════════════════════════════


class TestTcpRtuIntegration:
    """验证 modbus_tcp.py 和 modbus_rtu.py 正确从 modbus_base 导入共享代码,
    且 TCP/RTU 的 _slave_kwarg 差异化 (allow_broadcast) 生效"""

    def test_tcp_slave_kwarg_allows_broadcast_zero(self):
        """TCP 模式允许 slave_id=0 (广播写)"""
        from edgelite.drivers import modbus_tcp
        kw = modbus_tcp._slave_kwarg(0)
        assert 0 in kw.values()

    def test_tcp_slave_kwarg_rejects_248(self):
        from edgelite.drivers import modbus_tcp
        with pytest.raises(ValueError, match="0-247"):
            modbus_tcp._slave_kwarg(248)

    def test_tcp_read_kwargs_allows_broadcast_zero(self):
        from edgelite.drivers import modbus_tcp
        kw = modbus_tcp._read_kwargs(2, 0)
        assert kw["count"] == 2
        assert 0 in kw.values()

    def test_tcp_imports_shared_constants(self):
        """TCP 模块应使用 modbus_base 的常量 (同一对象)"""
        from edgelite.drivers import modbus_tcp
        assert modbus_tcp.REGISTER_TYPES is REGISTER_TYPES
        assert modbus_tcp.DATA_TYPE_REGS is DATA_TYPE_REGS
        assert modbus_tcp._BYTE_ORDER_FMT is _BYTE_ORDER_FMT
        assert modbus_tcp._MODBUS_EXCEPTION_CODES is _MODBUS_EXCEPTION_CODES

    def test_tcp_uses_improved_parse_exception(self):
        """TCP 应使用 modbus_base 改进后的 _parse_modbus_exception (未知码返回描述)"""
        from edgelite.drivers import modbus_tcp
        assert modbus_tcp._parse_modbus_exception is _parse_modbus_exception

    def test_rtu_slave_kwarg_rejects_zero(self):
        """RTU 模式禁止 slave_id=0"""
        from edgelite.drivers import modbus_rtu
        with pytest.raises(ValueError, match="1-247"):
            modbus_rtu._slave_kwarg(0)

    def test_rtu_slave_kwarg_accepts_1(self):
        from edgelite.drivers import modbus_rtu
        kw = modbus_rtu._slave_kwarg(1)
        assert 1 in kw.values()

    def test_rtu_read_kwargs_rejects_zero(self):
        from edgelite.drivers import modbus_rtu
        with pytest.raises(ValueError):
            modbus_rtu._read_kwargs(2, 0)

    def test_rtu_imports_shared_constants(self):
        """RTU 模块应使用 modbus_base 的常量 (同一对象)"""
        from edgelite.drivers import modbus_rtu
        assert modbus_rtu.REGISTER_TYPES is REGISTER_TYPES
        assert modbus_rtu.DATA_TYPE_REGS is DATA_TYPE_REGS
        assert modbus_rtu._BYTE_ORDER_FMT is _BYTE_ORDER_FMT
        assert modbus_rtu._MODBUS_EXCEPTION_CODES is _MODBUS_EXCEPTION_CODES

    def test_rtu_uses_improved_parse_exception(self):
        """RTU 应使用 modbus_base 改进后的 _parse_modbus_exception"""
        from edgelite.drivers import modbus_rtu
        assert modbus_rtu._parse_modbus_exception is _parse_modbus_exception

    def test_tcp_rtu_no_duplicate_modbus_exception_codes(self):
        """FIXED-P2: 确认 _MODBUS_EXCEPTION_CODES 不再在 TCP/RTU 中重复定义,
        而是统一引用 modbus_base 的同一字典对象"""
        from edgelite.drivers import modbus_rtu, modbus_tcp
        # 三个模块引用同一个字典对象 (is 同一性检查)
        assert modbus_tcp._MODBUS_EXCEPTION_CODES is modbus_rtu._MODBUS_EXCEPTION_CODES
        assert modbus_tcp._MODBUS_EXCEPTION_CODES is modbus_base._MODBUS_EXCEPTION_CODES

    def test_tcp_rtu_no_duplicate_register_types(self):
        """FIXED-P2: REGISTER_TYPES / DATA_TYPE_REGS / _BYTE_ORDER_FMT 也不重复定义"""
        from edgelite.drivers import modbus_rtu, modbus_tcp
        assert modbus_tcp.REGISTER_TYPES is modbus_rtu.REGISTER_TYPES
        assert modbus_tcp.DATA_TYPE_REGS is modbus_rtu.DATA_TYPE_REGS
        assert modbus_tcp._BYTE_ORDER_FMT is modbus_rtu._BYTE_ORDER_FMT
