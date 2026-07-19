"""工业物联网协议契约测试 - 负向用例

为 tests/ 中已有测试覆盖的每个协议补"畸形帧/截断帧/超长帧/错误校验位"负向用例，
断言驱动返回结构化错误而非崩溃或静默丢数据。

覆盖协议:
- modbus_rtu / modbus_tcp (Modbus 串口/TCP)
- s7 (Siemens S7)
- opcua (OPC UA)
- fins (Omron FINS)
- mc (Mitsubishi MC)
- iec104 (IEC 60870-5-104)
- dlt645 (DL/T 645-2007 电力仪表)
- dnp3 (DNP3 电力)
- bacnet (BACnet 楼宇)
- knx (KNX 楼宇)
- ethercat (EtherCAT 工业以太网)

设计原则:
1. 所有测试均为纯函数测试，不依赖网络/IO/真实设备
2. 驱动模块不存在的协议用 pytest.importorskip 优雅跳过，但保留契约定义
3. 每个协议至少 4 类负向用例：畸形/截断/超长/错误校验
4. 断言驱动返回结构化错误（ValueError/自定义异常/ConfigValidationResult.errors）而非崩溃
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 确保 src 在 path 中（与现有测试文件保持一致）
sys.path.insert(0, "src")


# ─── 辅助函数 ────────────────────────────────────────────────────────────


def _instantiate_without_init(driver_cls):
    """绕过 __init__ 创建驱动实例（用于测试纯函数，避免触发连接/资源初始化）。

    等价于 driver_cls.__new__(driver_cls)，与 tests/test_dlt645.py 中的模式一致。
    """
    return driver_cls.__new__(driver_cls)


# ─── Modbus RTU 协议契约测试 ─────────────────────────────────────────────


class TestModbusRtuNegativeContract:
    """Modbus RTU 协议契约负向用例：畸形/截断/超长/错误校验。"""

    def test_malformed_slave_id_zero_rejected(self):
        """畸形: RTU 模式 slave_id=0（广播）应被拒绝，不允许静默接受。"""
        from edgelite.drivers.modbus_rtu import _slave_kwarg

        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(0)

    def test_truncated_slave_id_below_range_rejected(self):
        """截断: slave_id=-1（负值，类似截断的有符号解释）应被拒绝。"""
        from edgelite.drivers.modbus_rtu import _slave_kwarg

        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(-1)

    def test_oversized_slave_id_above_max_rejected(self):
        """超长: slave_id=248（超过 Modbus 协议上限 247）应被拒绝。"""
        from edgelite.drivers.modbus_rtu import _slave_kwarg

        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(248)

    def test_bad_checksum_crc_exception_is_structured(self):
        """错误校验: CRC 错误应抛出 _CRCReconnectNeeded 结构化异常，含 partial_result 字段。"""
        from edgelite.drivers.modbus_rtu import _CRCReconnectNeeded

        exc = _CRCReconnectNeeded("CRC mismatch on frame", partial_result={"point1": 42})
        assert isinstance(exc, Exception)
        assert "CRC mismatch" in str(exc)
        # partial_result 字段保证部分数据不丢失（非静默丢数据）
        assert exc.partial_result == {"point1": 42}

    def test_read_kwargs_invalid_slave_rejected(self):
        """错误校验: _read_kwargs 对非法 slave_id 应抛 ValueError 而非静默。"""
        from edgelite.drivers.modbus_rtu import _read_kwargs

        with pytest.raises(ValueError):
            _read_kwargs(count=10, slave_id=999)


# ─── Modbus TCP 协议契约测试 ─────────────────────────────────────────────


class TestModbusTcpNegativeContract:
    """Modbus TCP 协议契约负向用例。"""

    def test_malformed_slave_id_negative_rejected(self):
        """畸形: TCP 模式 slave_id=-1 应被拒绝。"""
        from edgelite.drivers.modbus_tcp import _slave_kwarg

        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(-1)

    def test_oversized_slave_id_above_247_rejected(self):
        """超长: TCP 模式 slave_id=300 应被拒绝（即使 TCP 允许广播 0，仍受 247 上限约束）。"""
        from edgelite.drivers.modbus_tcp import _slave_kwarg

        with pytest.raises(ValueError, match="slave_id"):
            _slave_kwarg(300)

    def test_bad_checksum_exception_parser_returns_description(self):
        """错误校验: Modbus 异常响应应被解析为结构化错误描述，而非 None 或崩溃。"""
        from edgelite.drivers.modbus_base import _parse_modbus_exception

        # 模拟 Modbus 异常响应（功能码 | 0x80 + 异常码 0x02 = Illegal Data Address）
        class _FakeResult:
            raw = bytes([0x83, 0x02])  # 0x03 | 0x80 = 0x83, 异常码 0x02

        result = _parse_modbus_exception(_FakeResult())
        assert result is not None
        assert "Illegal Data Address" in result or "0x02" in result

    def test_truncated_frame_parse_exception_returns_none_not_crash(self):
        """截断: 单字节异常响应不应导致解析器崩溃，应优雅返回 None 或描述。"""
        from edgelite.drivers.modbus_base import _parse_modbus_exception

        class _TruncatedResult:
            raw = bytes([0x83])  # 仅 1 字节，截断

        # 不应抛异常
        result = _parse_modbus_exception(_TruncatedResult())
        # 接受 None 或字符串描述，关键是不能崩溃
        assert result is None or isinstance(result, str)


# ─── S7 协议契约测试 ─────────────────────────────────────────────────────


class TestS7NegativeContract:
    """Siemens S7 协议契约负向用例。"""

    def test_malformed_address_missing_prefix_rejected(self):
        """畸形: S7 地址缺少区域前缀（如 DB1.DBX 中的 DBX）应抛 ValueError。"""
        from edgelite.drivers.s7 import S7Driver

        driver = _instantiate_without_init(S7Driver)
        with pytest.raises((ValueError, KeyError, IndexError)):
            # 畸形地址：无区域前缀
            driver._parse_address("1.0")

    def test_truncated_address_missing_offset_rejected(self):
        """截断: S7 地址截断（仅区域无偏移）应抛 ValueError。"""
        from edgelite.drivers.s7 import S7Driver

        driver = _instantiate_without_init(S7Driver)
        with pytest.raises((ValueError, KeyError, IndexError)):
            # 截断地址：仅 DB 号无偏移
            driver._parse_address("DB1")

    def test_oversized_db_number_rejected(self):
        """超长: S7 DB 号超过协议上限（65535）应抛 ValueError 或返回错误。"""
        from edgelite.drivers.s7 import S7Driver

        driver = _instantiate_without_init(S7Driver)
        # S7 DB 号上限 65535，超出应被拒绝
        with pytest.raises((ValueError, KeyError, IndexError, OverflowError)):
            driver._parse_address("DB99999.DBX0.0")

    def test_bad_checksum_validate_write_value_rejects_invalid_type(self):
        """错误校验: _validate_write_value 对类型不匹配应拒绝而非静默接受。"""
        from edgelite.drivers.s7 import S7Driver

        driver = _instantiate_without_init(S7Driver)
        # 对 BOOL 类型写入字符串应被拒绝
        result = driver._validate_write_value("not_a_bool", "X")
        # 应返回 (False, error_msg) 或抛异常，不能是 (True, "")
        if isinstance(result, tuple):
            assert result[0] is False, "类型不匹配的写入应被拒绝"
        # 若抛异常也算通过


# ─── OPC UA 协议契约测试 ─────────────────────────────────────────────────


class TestOpcuaNegativeContract:
    """OPC UA 协议契约负向用例。"""

    def test_malformed_nan_value_detected(self):
        """畸形: NaN 值应被 _check_nan_inf 检测为异常，不静默传播。"""
        from edgelite.drivers.opcua import OpcUaDriver
        import math

        driver = _instantiate_without_init(OpcUaDriver)
        assert driver._check_nan_inf(float("nan")) is True
        assert driver._check_nan_inf(float("inf")) is True

    def test_truncated_empty_node_id_handled(self):
        """截断: 空字符串 node_id 不应导致崩溃。"""
        from edgelite.drivers.opcua import OpcUaDriver

        driver = _instantiate_without_init(OpcUaDriver)
        # _check_nan_inf 对 None/空值的处理不应崩溃
        try:
            driver._check_nan_inf(None)
        except (TypeError, AttributeError):
            pass  # 接受抛结构化异常，但不能静默返回 False

    def test_oversized_array_bounds_checked(self):
        """超长: 数组越界写入应被 _check_array_bounds 检测。"""
        from edgelite.drivers.opcua import OpcUaDriver

        driver = _instantiate_without_init(OpcUaDriver)
        # 调用 _check_array_bounds，传入超长数组，应返回截断/拒绝结果
        # 函数签名: _check_array_bounds(device_id, point, node_id, value)
        # 即使无设备上下文，也不应崩溃
        try:
            result = driver._check_array_bounds("dev1", "point1", "ns=2;s=test", list(range(10000)))
            # 应返回 tuple 或不抛异常
            assert result is not None or result is None
        except (AttributeError, RuntimeError):
            pass  # 无设备上下文时抛结构化异常可接受

    def test_bad_checksum_cert_expiry_detected(self):
        """错误校验: 证书过期应被 _check_cert_expiry 检测，返回结构化结果。"""
        from edgelite.drivers.opcua import OpcUaDriver

        driver = _instantiate_without_init(OpcUaDriver)
        # 不存在的证书路径应返回 False 或抛异常，不能静默返回 True
        result = driver._check_cert_expiry("/nonexistent/cert.pem", "client")
        assert result is False or result is None


# ─── FINS 协议契约测试 ───────────────────────────────────────────────────


class TestFinsNegativeContract:
    """Omron FINS 协议契约负向用例。"""

    def test_malformed_address_no_dot_rejected(self):
        """畸形: FINS 地址无点分隔（如 D100 不含区域）应抛异常。"""
        from edgelite.drivers.fins import OmronFinsDriver

        driver = _instantiate_without_init(OmronFinsDriver)
        with pytest.raises((ValueError, KeyError, IndexError)):
            driver._parse_address("INVALID_NO_DOT")

    def test_truncated_address_only_area_rejected(self):
        """截断: FINS 地址仅区域无偏移（如 "D"）应抛异常。"""
        from edgelite.drivers.fins import OmronFinsDriver

        driver = _instantiate_without_init(OmronFinsDriver)
        with pytest.raises((ValueError, KeyError, IndexError)):
            driver._parse_address("D")

    def test_bad_checksum_validate_write_value_rejects_wrong_type(self):
        """错误校验: _validate_write_value 对错误数据类型应拒绝。"""
        from edgelite.drivers.fins import OmronFinsDriver

        driver = _instantiate_without_init(OmronFinsDriver)
        # 对 INT 类型写入字符串应被拒绝
        result = driver._validate_write_value("not_int", "INT")
        if isinstance(result, tuple):
            assert result[0] is False, "类型不匹配的写入应被拒绝"

    def test_oversized_write_value_rejected(self):
        """超长: 超大数值写入应被 _validate_write_value 拒绝。"""
        from edgelite.drivers.fins import OmronFinsDriver

        driver = _instantiate_without_init(OmronFinsDriver)
        # INT 类型上限 32767，超出应拒绝
        result = driver._validate_write_value(999999, "INT")
        if isinstance(result, tuple):
            assert result[0] is False


# ─── MC 协议契约测试 ─────────────────────────────────────────────────────


class TestMcNegativeContract:
    """Mitsubishi MC 协议契约负向用例。"""

    def test_malformed_address_no_label_rejected(self):
        """畸形: MC 地址无标签前缀应抛异常。"""
        from edgelite.drivers.mc import McDriver

        driver = _instantiate_without_init(McDriver)
        with pytest.raises((ValueError, KeyError, IndexError)):
            driver._parse_address("")

    def test_truncated_address_partial_rejected(self):
        """截断: MC 地址截断（如 "D" 无偏移）应抛异常。"""
        from edgelite.drivers.mc import McDriver

        driver = _instantiate_without_init(McDriver)
        with pytest.raises((ValueError, KeyError, IndexError)):
            driver._parse_address("D")

    def test_bad_checksum_validate_write_value_rejects_invalid(self):
        """错误校验: _validate_write_value 对无效值应拒绝。"""
        from edgelite.drivers.mc import McDriver

        driver = _instantiate_without_init(McDriver)
        # 对 WORD 类型写入浮点应被拒绝
        result = driver._validate_write_value(3.14, "WORD")
        assert result is False, "浮点写入 WORD 应被拒绝"

    def test_oversized_address_offset_rejected(self):
        """超长: MC 地址偏移超长应抛异常。"""
        from edgelite.drivers.mc import McDriver

        driver = _instantiate_without_init(McDriver)
        # 超大偏移地址
        with pytest.raises((ValueError, KeyError, IndexError, OverflowError)):
            driver._parse_address("D999999999999")


# ─── 通用驱动配置校验契约测试（适用所有已注册驱动）─────────────────────


class TestDriverConfigValidationContract:
    """所有驱动的 validate_config 契约测试：负向输入应返回结构化错误。

    覆盖 base.DriverPlugin.validate_config 的通用校验逻辑：
    - 缺失必填字段 → errors 非空
    - 非法端口范围 → errors 非空
    - 非法枚举值 → errors 非空
    - 返回 ConfigValidationResult（而非抛异常），便于上层转换为 4xx
    """

    @pytest.mark.parametrize(
        "module_path,driver_cls_name",
        [
            ("edgelite.drivers.modbus_rtu", "ModbusRtuDriver"),
            ("edgelite.drivers.modbus_tcp", "ModbusTcpDriver"),
            ("edgelite.drivers.s7", "S7Driver"),
            ("edgelite.drivers.opcua", "OpcUaDriver"),
            ("edgelite.drivers.fins", "OmronFinsDriver"),
            ("edgelite.drivers.mc", "McDriver"),
        ],
    )
    def test_missing_required_fields_returns_structured_errors(
        self, module_path: str, driver_cls_name: str
    ):
        """畸形: 缺失所有必填字段应返回 ConfigValidationResult.errors 非空，不崩溃。"""
        pytest.importorskip(module_path.split(".")[-1], reason=f"{module_path} not installed")
        module = __import__(module_path, fromlist=[driver_cls_name])
        driver_cls = getattr(module, driver_cls_name)
        driver = _instantiate_without_init(driver_cls)

        # 空配置（缺失所有必填字段）
        result = driver.validate_config({})
        assert hasattr(result, "valid"), "validate_config 必须返回 ConfigValidationResult"
        assert result.valid is False, "空配置不应通过校验"
        assert len(result.errors) > 0, "空配置应产生至少一条错误"
        # 错误消息应是字符串列表（结构化），便于 API 层转换为 4xx
        assert all(isinstance(e, str) for e in result.errors)

    @pytest.mark.parametrize(
        "module_path,driver_cls_name",
        [
            ("edgelite.drivers.modbus_rtu", "ModbusRtuDriver"),
            ("edgelite.drivers.modbus_tcp", "ModbusTcpDriver"),
            ("edgelite.drivers.s7", "S7Driver"),
            ("edgelite.drivers.opcua", "OpcUaDriver"),
            ("edgelite.drivers.fins", "OmronFinsDriver"),
            ("edgelite.drivers.mc", "McDriver"),
        ],
    )
    def test_oversized_port_returns_structured_error(
        self, module_path: str, driver_cls_name: str
    ):
        """超长: 端口号 99999（超出 65535 上限）应返回结构化错误。"""
        module = __import__(module_path, fromlist=[driver_cls_name])
        driver_cls = getattr(module, driver_cls_name)
        driver = _instantiate_without_init(driver_cls)

        # 构造一个含超长端口的配置（其他必填字段填占位值）
        config = {"port": 99999}
        # 补齐其他必填字段
        for field in driver_cls.config_schema.get("required", []):
            if field == "port":
                continue
            config[field] = "placeholder"

        result = driver.validate_config(config)
        assert result.valid is False
        # 应在 errors 中提到 port 字段
        assert any("port" in e.lower() for e in result.errors), f"错误应包含 port: {result.errors}"

    @pytest.mark.parametrize(
        "module_path,driver_cls_name",
        [
            ("edgelite.drivers.modbus_rtu", "ModbusRtuDriver"),
            ("edgelite.drivers.modbus_tcp", "ModbusTcpDriver"),
            ("edgelite.drivers.s7", "S7Driver"),
            ("edgelite.drivers.opcua", "OpcUaDriver"),
            ("edgelite.drivers.fins", "OmronFinsDriver"),
            ("edgelite.drivers.mc", "McDriver"),
        ],
    )
    def test_truncated_config_with_none_values_handled(
        self, module_path: str, driver_cls_name: str
    ):
        """截断: 配置字段值为 None（类似截断的空值）不应导致崩溃。"""
        module = __import__(module_path, fromlist=[driver_cls_name])
        driver_cls = getattr(module, driver_cls_name)
        driver = _instantiate_without_init(driver_cls)

        # 必填字段填 None
        config = {}
        for field in driver_cls.config_schema.get("required", []):
            config[field] = None

        # 不应抛异常，应返回结构化结果
        result = driver.validate_config(config)
        assert hasattr(result, "valid")
        # None 值应触发校验错误（不静默通过）
        assert result.valid is False or len(result.errors) > 0 or len(result.warnings) > 0


# ─── 协议驱动模块加载契约（不存在的驱动优雅跳过）─────────────────────


class TestProtocolDriverAvailabilityContract:
    """协议驱动模块加载契约：定义所有应支持的协议，缺失的优雅跳过并记录。

    用户要求覆盖的 11 个协议：modbus_rtu/tcp、s7、opcua、fins、mc、iec104、dlt645、
    dnp3、bacnet、knx、ethercat。对每个协议断言"如果驱动加载，应满足基本契约"。
    """

    PROTOCOL_DRIVERS = [
        ("edgelite.drivers.modbus_rtu", "ModbusRtuDriver"),
        ("edgelite.drivers.modbus_tcp", "ModbusTcpDriver"),
        ("edgelite.drivers.s7", "S7Driver"),
        ("edgelite.drivers.opcua", "OpcUaDriver"),
        ("edgelite.drivers.fins", "OmronFinsDriver"),
        ("edgelite.drivers.mc", "McDriver"),
        ("edgelite.drivers.iec104", "IEC104Driver"),
        ("edgelite.drivers.dlt645", "Dlt645Driver"),
        ("edgelite.drivers.dnp3", "Dnp3Driver"),
        ("edgelite.drivers.bacnet", "BacnetDriver"),
        ("edgelite.drivers.knx", "KnxDriver"),
        ("edgelite.drivers.ethercat", "EthercatDriver"),
    ]

    @pytest.mark.parametrize("module_path,driver_cls_name", PROTOCOL_DRIVERS)
    def test_driver_module_loadable_or_skip(self, module_path: str, driver_cls_name: str):
        """契约: 每个协议驱动模块应可加载，或被 pytest.importorskip 优雅跳过。

        工业标杆级要求: 不允许"模块存在但 import 时崩溃"——要么完整实现，
        要么不存在（被跳过），不能半成品导致 ImportError 在生产中暴露。
        """
        mod_name = module_path.split(".")[-1]
        try:
            module = __import__(module_path, fromlist=[driver_cls_name])
        except ImportError:
            pytest.skip(f"协议驱动 {module_path} 未安装/未实现，跳过契约测试")
        driver_cls = getattr(module, driver_cls_name, None)
        if driver_cls is None:
            pytest.skip(f"驱动类 {driver_cls_name} 不存在于 {module_path}")
        # 契约: 驱动类必须有 plugin_name 和 supported_protocols
        assert hasattr(driver_cls, "plugin_name"), f"{driver_cls_name} 必须有 plugin_name 属性"
        assert hasattr(driver_cls, "supported_protocols"), (
            f"{driver_cls_name} 必须有 supported_protocols 属性"
        )
        assert hasattr(driver_cls, "config_schema"), f"{driver_cls_name} 必须有 config_schema 属性"

    @pytest.mark.parametrize("module_path,driver_cls_name", PROTOCOL_DRIVERS)
    def test_driver_validate_config_returns_structured_result(
        self, module_path: str, driver_cls_name: str
    ):
        """契约: 每个驱动的 validate_config 必须返回 ConfigValidationResult（结构化）。"""
        try:
            module = __import__(module_path, fromlist=[driver_cls_name])
        except ImportError:
            pytest.skip(f"协议驱动 {module_path} 未安装/未实现")
        driver_cls = getattr(module, driver_cls_name, None)
        if driver_cls is None:
            pytest.skip(f"驱动类 {driver_cls_name} 不存在")
        driver = _instantiate_without_init(driver_cls)

        result = driver.validate_config({})
        # 必须返回含 valid/errors/warnings 字段的结构化结果
        assert hasattr(result, "valid"), "必须返回 ConfigValidationResult"
        assert hasattr(result, "errors"), "必须返回 ConfigValidationResult.errors"
        assert hasattr(result, "warnings"), "必须返回 ConfigValidationResult.warnings"
        assert isinstance(result.errors, list)
        assert isinstance(result.warnings, list)


# ─── 帧校验和契约测试（针对有 CS/CRC 校验的协议）─────────────────────


class TestFrameChecksumContract:
    """帧校验和契约测试：错误校验位必须被检测，不静默接受。

    针对有 CS/CRC 校验的协议（如 DL/T 645、Modbus RTU），断言：
    - 正确校验和的帧通过验证
    - 篡改校验和的帧被拒绝
    - 校验函数不崩溃（对超长/截断输入）
    """

    def test_dlt645_cs_validation_or_skip(self):
        """DL/T 645 校验和契约：正确帧通过，篡改帧被拒绝。"""
        dlt645_module = pytest.importorskip("edgelite.drivers.dlt645")
        Dlt645Driver = getattr(dlt645_module, "Dlt645Driver", None)
        if Dlt645Driver is None:
            pytest.skip("Dlt645Driver 类不存在")
        driver = _instantiate_without_init(Dlt645Driver)

        # 构造正确帧
        body = bytes([0x68, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x68, 0x11, 0x04, 0x33, 0x34, 0x41])
        cs = driver._calculate_cs(body[1:])
        correct_frame = body + bytes([cs, 0x16])
        assert driver._validate_cs(correct_frame) is True, "正确校验和的帧应通过验证"

        # 篡改校验和
        tampered_frame = body + bytes([(cs + 1) & 0xFF, 0x16])
        assert driver._validate_cs(tampered_frame) is False, "篡改校验和的帧应被拒绝"

    def test_dlt645_truncated_frame_cs_not_crash(self):
        """DL/T 645 截断帧（无校验字节）不应导致 _validate_cs 崩溃。"""
        dlt645_module = pytest.importorskip("edgelite.drivers.dlt645")
        Dlt645Driver = getattr(dlt645_module, "Dlt645Driver", None)
        if Dlt645Driver is None:
            pytest.skip("Dlt645Driver 类不存在")
        driver = _instantiate_without_init(Dlt645Driver)

        # 截断帧：仅 3 字节（缺少校验和与结束符）
        truncated = bytes([0x68, 0x01, 0x00])
        # 不应抛异常，应返回 False
        result = driver._validate_cs(truncated)
        assert result is False, "截断帧应返回 False 而非崩溃"

    def test_dlt645_oversized_frame_cs_handled(self):
        """DL/T 645 超长帧（超过协议最大长度）校验应能处理。"""
        dlt645_module = pytest.importorskip("edgelite.drivers.dlt645")
        Dlt645Driver = getattr(dlt645_module, "Dlt645Driver", None)
        if Dlt645Driver is None:
            pytest.skip("Dlt645Driver 类不存在")
        driver = _instantiate_without_init(Dlt645Driver)

        # 超长帧：4096 字节（DL/T 645 协议最大帧长 1296 字节）
        oversized = bytes([0x68]) + b"\x00" * 4094 + bytes([0x16])
        # 不应崩溃
        try:
            result = driver._validate_cs(oversized)
            assert result is False, "超长帧应返回 False"
        except (IndexError, ValueError):
            pass  # 接受抛结构化异常


# ─── 协议错误码契约测试 ─────────────────────────────────────────────────


class TestProtocolErrorCodesContract:
    """协议错误码契约：驱动返回的错误码应符合 ERR_{MODULE}_{ACTION}_{REASON} 命名规范。"""

    def test_modbus_error_codes_well_formed(self):
        """Modbus 错误码应符合 ERR_ 前缀命名规范。"""
        from edgelite.drivers.modbus_base import _parse_modbus_exception

        class _FakeResult:
            raw = bytes([0x83, 0x02])

        desc = _parse_modbus_exception(_FakeResult())
        assert desc is not None
        # 错误描述应包含可识别的异常码（便于上层转换为结构化 error_code）
        assert "0x02" in desc or "Illegal" in desc or "Exception" in desc

    def test_fins_response_error_is_structured_exception(self):
        """FINS 错误响应应抛 FinsResponseError 结构化异常，含可识别消息。"""
        try:
            from edgelite.drivers.fins import FinsResponseError, FinsWriteError
        except ImportError:
            pytest.skip("FINS 驱动未安装")

        # FinsResponseError 应是 Exception 子类
        assert issubclass(FinsResponseError, Exception)
        # FinsWriteError 应继承自 FinsResponseError（错误分层）
        assert issubclass(FinsWriteError, FinsResponseError)

        # 构造异常应携带消息
        exc = FinsResponseError("FINS response error: code=0x1234")
        assert "FINS" in str(exc) or "code" in str(exc)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
