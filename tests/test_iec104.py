"""IEC 60870-5-104 驱动单元测试

重点验证质量描述符(QDS)位布局 (IEC 60870-5-4):
  bit0=保留, bit1=OV, bit2=BL, bit3=SB, bit4=NT, bit5=IV, bits6-7=保留
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import sys

sys.path.insert(0, "src")

from edgelite.drivers.iec104 import (
    QUALITY_BL,
    QUALITY_IV,
    QUALITY_NT,
    QUALITY_OV,
    QUALITY_SB,
    SBO_EXECUTE,
    SBO_SELECT,
    TI_C_SC_NA,
    TI_M_DP_NA,
    TI_M_ME_NA,
    TI_M_ME_NC,
    TI_M_SP_NA,
    U_FRAME_TESTFR_ACT,
    U_FRAME_TESTFR_CON,
    Iec104Driver,
)


class TestIec104QualityConstants:
    """QDS 常量位布局正确性 (IEC 60870-5-4)"""

    def test_ov_bit1(self):
        """OV(溢出) = bit1 = 0x02"""
        assert QUALITY_OV == 0x02

    def test_bl_bit2(self):
        """BL(封锁) = bit2 = 0x04"""
        assert QUALITY_BL == 0x04

    def test_sb_bit3(self):
        """SB(被替代) = bit3 = 0x08"""
        assert QUALITY_SB == 0x08

    def test_nt_bit4(self):
        """NT(非当前) = bit4 = 0x10"""
        assert QUALITY_NT == 0x10

    def test_iv_bit5(self):
        """IV(无效) = bit5 = 0x20"""
        assert QUALITY_IV == 0x20

    def test_no_overlap(self):
        """各质量位互不重叠"""
        masks = [QUALITY_OV, QUALITY_BL, QUALITY_SB, QUALITY_NT, QUALITY_IV]
        for i, a in enumerate(masks):
            for b in masks[i + 1 :]:
                assert a & b == 0, f"掩码重叠: {a:#x} & {b:#x}"


class TestIec104DecodeQuality:
    """_decode_quality 直接解码 QDS 字节"""

    def test_good(self):
        """QDS=0 → good"""
        assert Iec104Driver._decode_quality(0x00) == "good"

    def test_overflow_only(self):
        """QDS=0x02 → overflow"""
        assert Iec104Driver._decode_quality(0x02) == "overflow"

    def test_blocked_bit2(self):
        """QDS=0x04 → blocked (BL bit2)"""
        assert Iec104Driver._decode_quality(0x04) == "blocked"

    def test_substituted_bit3(self):
        """QDS=0x08 → substituted (SB bit3)"""
        assert Iec104Driver._decode_quality(0x08) == "substituted"

    def test_blocked_and_substituted(self):
        """QDS=0x0C → blocked,substituted (BL bit2 + SB bit3)"""
        result = Iec104Driver._decode_quality(0x0C)
        parts = set(result.split(","))
        assert parts == {"blocked", "substituted"}

    def test_substituted(self):
        """QDS=0x08 → substituted"""
        assert Iec104Driver._decode_quality(0x08) == "substituted"

    def test_not_topical(self):
        """QDS=0x10 → not_topical"""
        assert Iec104Driver._decode_quality(0x10) == "not_topical"

    def test_invalid(self):
        """QDS=0x20 → invalid"""
        assert Iec104Driver._decode_quality(0x20) == "invalid"

    def test_all_flags(self):
        """QDS=0x7E → 全部标志 (OV+BL+SB+NT+IV)"""
        # 0x7E = 0111 1110 = OV(0x02)+BL(0x04)+SB(0x08)+NT(0x10)+IV(0x20)
        result = Iec104Driver._decode_quality(0x7E)
        parts = set(result.split(","))
        assert parts == {"overflow", "blocked", "substituted", "not_topical", "invalid"}

    def test_reserved_bit7_ignored(self):
        """bit7 保留位不影响解码"""
        assert Iec104Driver._decode_quality(0x80) == "good"
        assert Iec104Driver._decode_quality(0x82) == "overflow"


class TestIec104SiqParse:
    """单点信息(SIQ)解析: value=bit0, QDS=bits1-7

    关键回归点: value 位(bit0)不得误触发 OV(bit1=0x02)
    """

    def _parse(self, siq_byte: int) -> dict:
        driver = Iec104Driver.__new__(Iec104Driver)
        data = bytes([siq_byte])
        return driver._parse_information_object(ti=TI_M_SP_NA, data=data, offset=0, ioa=1, cot=3, asdu_addr=1)

    def test_value_on_good_quality(self):
        """SIQ=0x01: value=1, 无质量标志 → good"""
        r = self._parse(0x01)
        assert r["value"] == 1
        assert r["quality"] == "good"

    def test_value_on_overflow(self):
        """SIQ=0x03: value=1, OV=1 → overflow (bit0 不干扰 bit1)"""
        r = self._parse(0x03)
        assert r["value"] == 1
        assert r["quality"] == "overflow"

    def test_value_off_invalid(self):
        """SIQ=0x20: value=0, IV=1 → invalid"""
        r = self._parse(0x20)
        assert r["value"] == 0
        assert r["quality"] == "invalid"

    def test_value_off_good(self):
        """SIQ=0x00: value=0, 无标志 → good"""
        r = self._parse(0x00)
        assert r["value"] == 0
        assert r["quality"] == "good"


class TestIec104DiqParse:
    """双点信息(DIQ)解析: value=bits0-1, QDS=bits2-7

    关键回归点: value 位(bits0-1)不得误触发质量标志
    实现使用 (diq & 0xFC) >> 1 将 DIQ 质量位归一化到 QDS 位置后解码。
    DIQ 质量位布局: bit2=reserved, bit3=BL, bit4=SB, bit5=NT, bit6=IV
    """

    def _parse(self, diq_byte: int) -> dict:
        driver = Iec104Driver.__new__(Iec104Driver)
        data = bytes([diq_byte])
        return driver._parse_information_object(ti=TI_M_DP_NA, data=data, offset=0, ioa=1, cot=3, asdu_addr=1)

    def test_value_intermediate_good(self):
        """DIQ=0x02: value=2(中间态), 无标志 → good

        0x02 bits0-1=value=2, 无质量位 → good
        """
        r = self._parse(0x02)
        assert r["value"] == 2
        assert r["quality"] == "good"

    def test_value_off_good(self):
        """DIQ=0x01: value=1(分), 无标志 → good"""
        r = self._parse(0x01)
        assert r["value"] == 1
        assert r["quality"] == "good"

    def test_value_on_good(self):
        """DIQ=0x03: value=3(合), 无标志 → good"""
        r = self._parse(0x03)
        assert r["value"] == 3
        assert r["quality"] == "good"

    def test_value_intermediate_with_invalid(self):
        """DIQ=0x42: value=2, IV=1 → invalid

        0x42 = bits0-1(value=2) + bit6(IV in DIQ); (0x42 & 0xFC)>>1 = 0x20 → invalid
        """
        r = self._parse(0x42)
        assert r["value"] == 2
        assert r["quality"] == "invalid"

    def test_value_on_with_substituted(self):
        """DIQ=0x13: value=3, SB=1 → substituted

        0x13 = bits0-1(value=3) + bit4(SB in DIQ); (0x13 & 0xFC)>>1 = 0x08 → substituted
        """
        r = self._parse(0x13)
        assert r["value"] == 3
        assert r["quality"] == "substituted"


class TestIec104AnalogQdsParse:
    """模拟量 QDS 解析: QDS 字节不含 value 位, 直接解码"""

    def _parse_me_na(self, nva: int, qds: int) -> dict:
        driver = Iec104Driver.__new__(Iec104Driver)
        data = struct.pack("<hB", nva, qds)
        return driver._parse_information_object(ti=TI_M_ME_NA, data=data, offset=0, ioa=1, cot=3, asdu_addr=1)

    def test_good(self):
        """QDS=0x00 → good"""
        r = self._parse_me_na(0, 0x00)
        assert r["quality"] == "good"

    def test_overflow(self):
        """QDS=0x02 → overflow"""
        r = self._parse_me_na(100, 0x02)
        assert r["quality"] == "overflow"

    def test_not_topical(self):
        """QDS=0x10 → not_topical (NT=bit4=0x10)"""
        r = self._parse_me_na(-1000, 0x10)
        assert r["quality"] == "not_topical"

    def test_invalid_overflow(self):
        """QDS=0x22 → invalid,overflow (IV=0x20 + OV=0x02)"""
        r = self._parse_me_na(0, 0x22)
        parts = set(r["quality"].split(","))
        assert parts == {"invalid", "overflow"}

    def test_me_nc_float_with_qds(self):
        """TI_M_ME_NC (浮点) QDS 解码"""
        driver = Iec104Driver.__new__(Iec104Driver)
        data = struct.pack("<fB", 3.14, 0x20)  # value + IV(0x20)
        r = driver._parse_information_object(ti=TI_M_ME_NC, data=data, offset=0, ioa=1, cot=3, asdu_addr=1)
        assert abs(r["value"] - 3.14) < 1e-6
        assert r["quality"] == "invalid"


class TestIec104SboConfirmation:
    """SBO (Select Before Operate) 确认响应处理

    关键回归点: 原 _sbo_select_event / _sbo_execute_event 从未被 set(),
    导致 SBO 遥控必定超时失败。修复后在 _handle_frame 收到命令确认时唤醒等待方。
    """

    @staticmethod
    def _make_driver(ioa: int = 123) -> Iec104Driver:
        """构造一个绕过 __init__ 的驱动实例, 仅填充 SBO 所需属性"""
        d = Iec104Driver.__new__(Iec104Driver)
        d._sbo_selected_ioa = ioa
        d._sbo_select_event = asyncio.Event()
        d._sbo_execute_event = asyncio.Event()
        d._sbo_select_result = None
        d._sbo_execute_result = None
        d._sbo_select_timeout = 0.5
        d._sbo_execute_timeout = 0.5
        d._asdu_addr = 1
        d._asdu_addr_length = 2
        d._cause_of_tx_length = 2
        d._ssn = 0
        d._rsn = 0
        d._connected = True
        d._startdt_confirmed = True
        d._ioa_map = {ioa: f"point_{ioa}"}
        return d

    @staticmethod
    def _cmd_point(ioa: int, ti: int, cot: int, sbo_cmd: int) -> dict:
        return {
            "ioa": ioa,
            "ti": ti,
            "cot": cot,
            "asdu_addr": 1,
            "value": 1,
            "sbo_qualifier": 0,
            "sbo_command": sbo_cmd,
            "data_type": "bool",
        }

    def test_select_confirm_sets_event(self):
        """Select-Confirm (COT=7) 唤醒 select 等待并标记成功"""
        d = self._make_driver(ioa=123)
        d._handle_sbo_confirmation([self._cmd_point(123, TI_C_SC_NA, 7, SBO_SELECT)])
        assert d._sbo_select_result is True
        assert d._sbo_select_event.is_set()

    def test_execute_confirm_sets_event(self):
        """Execute-Confirm (COT=7) 唤醒 execute 等待并标记成功"""
        d = self._make_driver(ioa=123)
        d._handle_sbo_confirmation([self._cmd_point(123, TI_C_SC_NA, 7, SBO_EXECUTE)])
        assert d._sbo_execute_result is True
        assert d._sbo_execute_event.is_set()

    def test_select_rejected_marks_failure(self):
        """Select 被拒 (COT!=7) 标记失败但仍唤醒等待方"""
        d = self._make_driver(ioa=123)
        d._handle_sbo_confirmation(
            [
                self._cmd_point(123, TI_C_SC_NA, 47, SBO_SELECT)  # COT=47=未知类型
            ]
        )
        assert d._sbo_select_result is False
        assert d._sbo_select_event.is_set()

    def test_execute_rejected_marks_failure(self):
        """Execute 被拒 (COT!=7) 标记失败但仍唤醒等待方"""
        d = self._make_driver(ioa=123)
        d._handle_sbo_confirmation(
            [
                self._cmd_point(123, TI_C_SC_NA, 46, SBO_EXECUTE)  # COT=46=未知原因
            ]
        )
        assert d._sbo_execute_result is False
        assert d._sbo_execute_event.is_set()

    def test_mismatched_ioa_ignored(self):
        """IOA 不匹配时不触发确认"""
        d = self._make_driver(ioa=123)
        d._handle_sbo_confirmation([self._cmd_point(999, TI_C_SC_NA, 7, SBO_SELECT)])
        assert d._sbo_select_result is None
        assert not d._sbo_select_event.is_set()

    def test_no_selected_ioa_ignored(self):
        """无选中 IOA 时忽略所有命令确认"""
        d = self._make_driver(ioa=123)
        d._sbo_selected_ioa = None
        d._handle_sbo_confirmation([self._cmd_point(123, TI_C_SC_NA, 7, SBO_SELECT)])
        assert d._sbo_select_result is None
        assert not d._sbo_select_event.is_set()

    async def test_full_sbo_flow_success(self):
        """完整 SBO 流程: Select→确认→Execute→确认 → 返回 True"""
        d = self._make_driver(ioa=123)
        # 模拟 _send_frame 为空操作
        d._send_frame = lambda frame: asyncio.sleep(0)  # type: ignore

        async def simulate_select_confirm():
            # 等 select 命令发出后回复确认
            await asyncio.sleep(0.02)
            d._handle_sbo_confirmation([self._cmd_point(123, TI_C_SC_NA, 7, SBO_SELECT)])

        async def simulate_execute_confirm():
            # FIXED: 先等待 select 确认完成，再延迟触发 execute 确认
            # 原问题: 两个模拟同时启动，execute 确认可能在 SBO 流程进入 execute 阶段前到达
            # 导致确认被忽略，execute 超时失败（偶发竞态）
            await d._sbo_select_event.wait()
            await asyncio.sleep(0.02)
            d._handle_sbo_confirmation([self._cmd_point(123, TI_C_SC_NA, 7, SBO_EXECUTE)])

        sim_select = asyncio.ensure_future(simulate_select_confirm())
        sim_execute = asyncio.ensure_future(simulate_execute_confirm())
        ok = await d.write_point("dev1", "point_123", True)
        await asyncio.gather(sim_select, sim_execute)
        assert ok is True

    async def test_sbo_select_timeout_returns_false(self):
        """Select 超时 → write_point 返回 False"""
        d = self._make_driver(ioa=123)
        d._send_frame = lambda frame: asyncio.sleep(0)  # type: ignore
        ok = await d.write_point("dev1", "point_123", True)
        assert ok is False


# ==================== Task #12: P1 TESTFR 重试机制 ====================


async def _async_noop(*args, **kwargs):
    """异步空操作，用于测试中 mock 异步方法（避免未等待协程警告）"""
    pass


class TestIec104TestfrRetry:
    """TESTFR 重试机制测试

    FIXED-P1: 原代码仅 1 次 TESTFR 尝试即断连，无重试机制
    - 新增 _testfr_retry_count / _testfr_max_retries / _testfr_sent_time
    - T3 空闲超时 → 首次发送 TESTFR_ACT (retry_count=0)
    - T1 确认超时 → retry_count++ 并重发，达到上限才断连
    - TESTFR_CON 收到 → retry_count 清零
    """

    @staticmethod
    def _make_driver(t3=0.06, t1=0.06, max_retries=3) -> Iec104Driver:
        """构造绕过 __init__ 的驱动实例，仅填充心跳循环所需属性"""
        d = Iec104Driver.__new__(Iec104Driver)
        d._running = True
        d._connected = True
        d._testfr_sent = False
        d._testfr_retry_count = 0
        d._testfr_max_retries = max_retries
        d._testfr_sent_time = 0.0
        d._t3_timeout = t3
        d._t1_timeout = t1
        d._heartbeat_interval = t3
        d._s_frame_needed = False
        d._startdt_confirmed = True
        d._startdt_event = asyncio.Event()
        return d

    @staticmethod
    def _build_testfr_con_frame() -> bytes:
        """构造 6 字节 TESTFR_CON U-帧"""
        return bytes([0x68, 0x04, U_FRAME_TESTFR_CON, 0x00, 0x00, 0x00])

    def test_config_schema_includes_testfr_max_retries(self):
        """config_schema 包含 testfr_max_retries 字段"""
        fields = Iec104Driver.config_schema["fields"]
        names = [f["name"] for f in fields]
        assert "testfr_max_retries" in names
        # 默认值为 3
        testfr_field = next(f for f in fields if f["name"] == "testfr_max_retries")
        assert testfr_field["default"] == 3

    def test_testfr_con_resets_retry_counter_and_flag(self):
        """TESTFR_CON 收到后 _testfr_sent=False 且 retry_count=0 (常量验证)"""
        # 验证 U_FRAME_TESTFR_CON 常量正确 (0x2F)
        assert U_FRAME_TESTFR_CON == 0x2F
        assert U_FRAME_TESTFR_ACT == 0x2B

    async def test_testfr_con_resets_after_retry(self):
        """重试过程中收到 TESTFR_CON 重置状态"""
        d = self._make_driver()
        d._testfr_sent = True
        d._testfr_retry_count = 1
        d._send_frame = _async_noop
        d._send_general_interrogation = _async_noop
        d._send_clock_sync = _async_noop

        await d._handle_frame(self._build_testfr_con_frame())
        assert d._testfr_sent is False
        assert d._testfr_retry_count == 0

    async def test_first_testfr_sent_on_t3_timeout(self):
        """T3 空闲超时后首次发送 TESTFR_ACT，retry_count=0"""
        d = self._make_driver(t3=0.04, t1=0.04)
        sent_frames: list[bytes] = []

        async def mock_send(frame: bytes):
            sent_frames.append(frame)

        d._send_frame = mock_send
        closed = False

        async def mock_close():
            nonlocal closed
            closed = True

        d._close_connection = mock_close

        task = asyncio.ensure_future(d._heartbeat_loop())
        await asyncio.sleep(0.08)  # 等 T3 超时
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert d._testfr_sent is True
        assert d._testfr_retry_count == 0
        assert len(sent_frames) >= 1
        # 验证是 TESTFR_ACT 帧 (ctrl_byte1 = frame[2])
        assert sent_frames[0][2] == U_FRAME_TESTFR_ACT
        assert closed is False

    async def test_testfr_retries_on_t1_timeout(self):
        """T1 超时后重试 TESTFR_ACT，retry_count 递增"""
        d = self._make_driver(t3=0.04, t1=0.04, max_retries=5)
        send_count = 0

        async def mock_send(frame: bytes):
            nonlocal send_count
            send_count += 1

        d._send_frame = mock_send
        closed = False

        async def mock_close():
            nonlocal closed
            closed = True

        d._close_connection = mock_close

        task = asyncio.ensure_future(d._heartbeat_loop())
        # T3(0.04) + 2*T1(0.04*2=0.08) ≈ 0.12s，等待 0.18s 确保两次重试
        await asyncio.sleep(0.18)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # 应发送: 1 初始 + 至少 2 次重试 = 3+
        assert send_count >= 3
        assert d._testfr_retry_count >= 2
        assert closed is False  # max_retries=5，尚未达到

    async def test_testfr_disconnects_after_max_retries(self):
        """达到 max_retries 后触发断连"""
        d = self._make_driver(t3=0.04, t1=0.04, max_retries=2)
        send_count = 0

        async def mock_send(frame: bytes):
            nonlocal send_count
            send_count += 1

        d._send_frame = mock_send
        closed = False

        async def mock_close():
            nonlocal closed
            closed = True

        d._close_connection = mock_close

        task = asyncio.ensure_future(d._heartbeat_loop())
        # T3(0.04) + 3*T1(0.12) ≈ 0.16s，等待 0.3s 确保断连
        await asyncio.sleep(0.3)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # max_retries=2 → 1 初始 + 2 重试 = 3 次发送后断连
        assert send_count == 3
        assert closed is True

    async def test_testfr_con_during_retry_cycle_resets(self):
        """重试周期中收到 TESTFR_CON → 重置计数器"""
        # 使用大 T3/T1 避免心跳循环在测试期间触发额外 TESTFR
        d = self._make_driver(t3=10.0, t1=10.0, max_retries=5)
        d._send_frame = _async_noop
        d._close_connection = _async_noop
        d._send_general_interrogation = _async_noop
        d._send_clock_sync = _async_noop

        # 启动心跳循环 (T3=10s，测试期间不会自然触发 TESTFR)
        task = asyncio.ensure_future(d._heartbeat_loop())

        # 手动模拟"正在等待 TESTFR_CON"的中间状态
        d._testfr_sent = True
        d._testfr_retry_count = 2

        # 处理 TESTFR_CON 帧
        await d._handle_frame(self._build_testfr_con_frame())
        assert d._testfr_sent is False
        assert d._testfr_retry_count == 0

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def test_close_connection_resets_testfr_state(self):
        """_close_connection 重置 TESTFR 状态"""
        d = self._make_driver()
        d._testfr_sent = True
        d._testfr_retry_count = 3
        d._writer = None  # 避免 writer.close() 调用

        await d._close_connection()
        assert d._testfr_sent is False
        assert d._testfr_retry_count == 0
        assert d._connected is False
