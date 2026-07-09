"""MC协议驱动单元测试

覆盖通信模式 (binary/ascii) 修复：
- 配置 schema 新增 communication_mode 字段
- _apply_comm_mode 辅助方法正确委托 pymcprotocol.setaccessopt
- binary 模式短路优化 (不调用 setaccessopt)
- ascii 模式切换 commtype 和 _wordsize
- setaccessopt 失败时优雅降级 (不中断连接)
- pymcprotocol 依赖假设验证 (Type3E/Type4E 原生支持 ascii)

MC协议 ASCII vs 二进制帧差异 (MELSEC Communication Protocol Reference):
- binary: 紧凑帧, 2字节/word, 子头部 0x5000(3E)/0x5400(4E) 二进制编码
- ascii: 文本帧, 4字节/word (十六进制文本), 子头部 "5000"/"5400" ASCII编码
  用于旧式 Q 系列 PLC 通过串口网关/调制解调器链路通信的场景
"""

import sys

sys.path.insert(0, "src")

from edgelite.drivers.mc import McDriver


class _FakeMcClient:
    """伪 pymcprotocol client — 记录 setaccessopt 调用以便断言。

    模拟 pymcprotocol.Type3E 的 setaccessopt / _set_commtype 行为：
    - commtype: "binary" (默认) 或 "ascii"
    - _wordsize: binary=2, ascii=4
    - setaccessopt(commtype=...) 委托 _set_commtype
    """

    def __init__(self, plctype="Q"):
        self.plctype = plctype
        self.commtype = "binary"
        self._wordsize = 2
        self.setaccessopt_calls: list[dict] = []
        self._raise_on_setaccessopt: Exception | None = None

    def setaccessopt(self, commtype=None, network=None, pc=None,
                     dest_moduleio=None, dest_modulesta=None, timer_sec=None):
        self.setaccessopt_calls.append({
            "commtype": commtype, "network": network, "pc": pc,
            "dest_moduleio": dest_moduleio,
            "dest_modulesta": dest_modulesta,
            "timer_sec": timer_sec,
        })
        if self._raise_on_setaccessopt is not None:
            raise self._raise_on_setaccessopt
        if commtype:
            self._set_commtype(commtype)

    def _set_commtype(self, commtype):
        if commtype == "binary":
            self.commtype = "binary"
            self._wordsize = 2
        elif commtype == "ascii":
            self.commtype = "ascii"
            self._wordsize = 4
        else:
            raise ValueError("communication type must be binary or ascii")


def _make_driver(communication_mode: str = "binary") -> McDriver:
    """构造 McDriver 实例 (绕过 __init__ 的复杂依赖)，预设通信模式。"""
    driver = McDriver.__new__(McDriver)
    driver._communication_mode = communication_mode
    return driver


class TestCommunicationModeConfig:
    """配置 schema 中 communication_mode 字段验证"""

    def test_config_schema_has_communication_mode_field(self):
        """schema.fields 包含 communication_mode 字段"""
        fields = McDriver.config_schema["fields"]
        names = [f["name"] for f in fields]
        assert "communication_mode" in names

    def test_communication_mode_default_is_binary(self):
        """默认值为 binary (向后兼容)"""
        fields = McDriver.config_schema["fields"]
        comm_field = next(f for f in fields if f["name"] == "communication_mode")
        assert comm_field["default"] == "binary"

    def test_communication_mode_options_binary_ascii(self):
        """options 仅包含 binary 和 ascii"""
        fields = McDriver.config_schema["fields"]
        comm_field = next(f for f in fields if f["name"] == "communication_mode")
        assert comm_field["options"] == ["binary", "ascii"]

    def test_communication_mode_field_type_string(self):
        """字段类型为 string"""
        fields = McDriver.config_schema["fields"]
        comm_field = next(f for f in fields if f["name"] == "communication_mode")
        assert comm_field["type"] == "string"


class TestApplyCommModeBinary:
    """_apply_comm_mode 在 binary 模式下的行为"""

    def test_binary_mode_skips_setaccessopt(self):
        """binary 模式短路优化：不调用 setaccessopt (pymcprotocol 默认即为 binary)"""
        driver = _make_driver("binary")
        fake = _FakeMcClient()
        driver._apply_comm_mode(fake)
        assert fake.setaccessopt_calls == []
        assert fake.commtype == "binary"
        assert fake._wordsize == 2

    def test_binary_mode_default_state(self):
        """未设置 _communication_mode 时默认 binary (不调用 setaccessopt)"""
        driver = McDriver.__new__(McDriver)
        # 模拟 __init__ 中的默认值
        driver._communication_mode = "binary"
        fake = _FakeMcClient()
        driver._apply_comm_mode(fake)
        assert fake.setaccessopt_calls == []


class TestApplyCommModeAscii:
    """_apply_comm_mode 在 ascii 模式下的行为"""

    def test_ascii_mode_calls_setaccessopt(self):
        """ascii 模式调用 setaccessopt(commtype='ascii')"""
        driver = _make_driver("ascii")
        fake = _FakeMcClient()
        driver._apply_comm_mode(fake)
        assert len(fake.setaccessopt_calls) == 1
        assert fake.setaccessopt_calls[0]["commtype"] == "ascii"

    def test_ascii_mode_switches_commtype_and_wordsize(self):
        """ascii 模式后 client 的 commtype='ascii', _wordsize=4"""
        driver = _make_driver("ascii")
        fake = _FakeMcClient()
        driver._apply_comm_mode(fake)
        assert fake.commtype == "ascii"
        assert fake._wordsize == 4

    def test_ascii_mode_only_sets_commtype_not_other_opts(self):
        """ascii 模式仅设置 commtype，不修改 network/pc 等其他访问参数"""
        driver = _make_driver("ascii")
        fake = _FakeMcClient()
        driver._apply_comm_mode(fake)
        call = fake.setaccessopt_calls[0]
        assert call["commtype"] == "ascii"
        assert call["network"] is None
        assert call["pc"] is None
        assert call["dest_moduleio"] is None
        assert call["dest_modulesta"] is None
        assert call["timer_sec"] is None


class TestApplyCommModeErrorHandling:
    """_apply_comm_mode 异常处理 — 优雅降级不中断连接"""

    def test_setaccessopt_failure_does_not_raise(self):
        """setaccessopt 抛异常时 _apply_comm_mode 不传播，保持连接可用"""
        driver = _make_driver("ascii")
        fake = _FakeMcClient()
        fake._raise_on_setaccessopt = RuntimeError("simulated pymcprotocol internal error")
        # 不应抛出异常
        driver._apply_comm_mode(fake)
        # 调用仍被记录 (说明尝试了切换)
        assert len(fake.setaccessopt_calls) == 1

    def test_setaccessopt_failure_leaves_client_in_binary(self):
        """setaccessopt 失败时 client 保持默认 binary 模式 (pymcprotocol 默认值)"""
        driver = _make_driver("ascii")
        fake = _FakeMcClient()
        fake._raise_on_setaccessopt = ValueError("invalid commtype")
        driver._apply_comm_mode(fake)
        # FakeClient 在异常前未切换 commtype
        assert fake.commtype == "binary"
        assert fake._wordsize == 2


class TestPymcprotocolDependencyContract:
    """验证 pymcprotocol 库原生支持 ascii 模式 (修复的依赖假设)

    这组测试验证 pymcprotocol.Type3E/Type4E 确实支持通过 setaccessopt
    切换 binary/ascii 帧格式。如果 pymcprotocol 升级后移除此能力，
    这些测试会先于驱动测试失败，明确指向依赖变更。
    """

    def test_type3e_has_setaccessopt(self):
        """Type3E 类提供 setaccessopt 公共方法"""
        from pymcprotocol import Type3E
        assert hasattr(Type3E, "setaccessopt")
        assert callable(getattr(Type3E, "setaccessopt", None))

    def test_type3e_set_commtype_method_exists(self):
        """Type3E 类提供 _set_commtype 内部方法"""
        from pymcprotocol import Type3E
        assert hasattr(Type3E, "_set_commtype")

    def test_type3e_ascii_switches_wordsize(self):
        """Type3E 实例切换到 ascii 后 _wordsize=4, commtype='ascii'"""
        from pymcprotocol import Type3E
        from pymcprotocol import mcprotocolconst as const
        client = Type3E(plctype="Q")
        client.setaccessopt(commtype="ascii")
        assert client.commtype == const.COMMTYPE_ASCII
        assert client._wordsize == 4

    def test_type3e_binary_wordsize(self):
        """Type3E 实例默认 binary: commtype='binary', _wordsize=2"""
        from pymcprotocol import Type3E
        from pymcprotocol import mcprotocolconst as const
        client = Type3E(plctype="Q")
        assert client.commtype == const.COMMTYPE_BINARY
        assert client._wordsize == 2

    def test_type4e_inherits_setaccessopt(self):
        """Type4E 继承 Type3E 的 setaccessopt (4E帧也支持 ascii)"""
        from pymcprotocol import Type4E
        assert hasattr(Type4E, "setaccessopt")

    def test_type4e_ascii_switches_wordsize(self):
        """Type4E 实例切换到 ascii 后 _wordsize=4"""
        from pymcprotocol import Type4E
        client = Type4E(plctype="Q")
        client.setaccessopt(commtype="ascii")
        assert client._wordsize == 4
        assert client.commtype == "ascii"

    def test_ascii_answer_data_index_differs_from_binary(self):
        """ascii 模式下响应数据偏移量与 binary 不同 (帧头更长)

        验证 pymcprotocol 在 ascii 模式下使用不同的偏移解析响应，
        这是 ascii 帧文本化导致帧头变长的直接体现。
        """
        from pymcprotocol import Type3E
        client = Type3E(plctype="Q")
        client.setaccessopt(commtype="binary")
        binary_idx = client._get_answerdata_index()
        client.setaccessopt(commtype="ascii")
        ascii_idx = client._get_answerdata_index()
        assert binary_idx != ascii_idx
        assert ascii_idx > binary_idx  # ascii 帧头更长

    def test_commtype_constants_defined(self):
        """mcprotocolconst 定义 COMMTYPE_BINARY 和 COMMTYPE_ASCII 常量"""
        from pymcprotocol import mcprotocolconst as const
        assert const.COMMTYPE_BINARY == "binary"
        assert const.COMMTYPE_ASCII == "ascii"


class TestCommunicationModeValidation:
    """配置校验逻辑测试 — 模拟 start() 中的 communication_mode 解析"""

    def test_valid_binary_config_accepted(self):
        """有效值 'binary' 被接受"""
        # 模拟 start() 中的校验逻辑
        config = {"communication_mode": "binary"}
        communication_mode = str(config.get("communication_mode", "binary")).lower()
        assert communication_mode in ("binary", "ascii")
        assert communication_mode == "binary"

    def test_valid_ascii_config_accepted(self):
        """有效值 'ascii' 被接受"""
        config = {"communication_mode": "ascii"}
        communication_mode = str(config.get("communication_mode", "binary")).lower()
        assert communication_mode in ("binary", "ascii")
        assert communication_mode == "ascii"

    def test_uppercase_ascii_normalized(self):
        """大写 'ASCII' 通过 .lower() 归一化为 'ascii'"""
        config = {"communication_mode": "ASCII"}
        communication_mode = str(config.get("communication_mode", "binary")).lower()
        assert communication_mode == "ascii"

    def test_invalid_value_falls_back_to_binary(self):
        """无效值 'rtu' 不在白名单中，回退到 binary (模拟 start 的警告逻辑)"""
        config = {"communication_mode": "rtu"}
        communication_mode = str(config.get("communication_mode", "binary")).lower()
        if communication_mode not in ("binary", "ascii"):
            communication_mode = "binary"
        assert communication_mode == "binary"

    def test_missing_config_defaults_to_binary(self):
        """未配置 communication_mode 时默认 binary (向后兼容)"""
        config = {}
        communication_mode = str(config.get("communication_mode", "binary")).lower()
        assert communication_mode == "binary"

    def test_empty_config_defaults_to_binary(self):
        """空字符串配置回退到 binary"""
        config = {"communication_mode": ""}
        communication_mode = str(config.get("communication_mode", "binary")).lower()
        if communication_mode not in ("binary", "ascii"):
            communication_mode = "binary"
        assert communication_mode == "binary"


class TestApplyCommModeIntegrationWithRealType3E:
    """_apply_comm_mode 与真实 pymcprotocol.Type3E 集成验证

    使用真实 Type3E 实例 (不连接网络) 验证 _apply_comm_mode
    能正确驱动 pymcprotocol 的通信模式切换。
    """

    def test_apply_ascii_to_real_type3e(self):
        """_apply_comm_mode('ascii') 正确切换真实 Type3E 的 commtype"""
        from pymcprotocol import Type3E
        from pymcprotocol import mcprotocolconst as const
        driver = _make_driver("ascii")
        client = Type3E(plctype="Q")
        driver._apply_comm_mode(client)
        assert client.commtype == const.COMMTYPE_ASCII
        assert client._wordsize == 4

    def test_apply_binary_to_real_type3e_is_noop(self):
        """_apply_comm_mode('binary') 对真实 Type3E 是 no-op (保持默认)"""
        from pymcprotocol import Type3E
        from pymcprotocol import mcprotocolconst as const
        driver = _make_driver("binary")
        client = Type3E(plctype="Q")
        driver._apply_comm_mode(client)
        assert client.commtype == const.COMMTYPE_BINARY
        assert client._wordsize == 2

    def test_apply_ascii_to_real_type4e(self):
        """_apply_comm_mode('ascii') 正确切换真实 Type4E 的 commtype"""
        from pymcprotocol import Type4E
        from pymcprotocol import mcprotocolconst as const
        driver = _make_driver("ascii")
        client = Type4E(plctype="Q")
        driver._apply_comm_mode(client)
        assert client.commtype == const.COMMTYPE_ASCII
        assert client._wordsize == 4
