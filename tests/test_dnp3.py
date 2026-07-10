"""DNP3 驱动单元测试

覆盖协议正确性修复：
- CRC-16 多项式修正: Modbus 0xA001 → DNP3 0xA6BC (反射 0x3D65)
- 替换 0x00 占位符为真实 16 字节数据块 CRC
- 链路层帧结构修正: LEN 单字节 + CTRL 字节 + HeaderCRC + BlockCRC
- Task #13: 质量标志多状态组合 + 传输层分段重组 + SBO 完整流程
"""

import asyncio
import struct
import sys
from typing import Any

sys.path.insert(0, "src")

from edgelite.drivers.dnp3 import (
    APPCONTROL_FIN,
    APPCONTROL_FIR,
    DNP3_DATA_BLOCK_MAX,
    DNP3_HEADER_SIZE,
    DNP3_LINK_CTRL_USER_DATA,
    DNP3_START_BYTES,
    FC_RESPONSE,
    QUALITY_CHATTER_FILTER,
    QUALITY_COMM_LOST,
    QUALITY_LOCAL_FORCED,
    QUALITY_ONLINE,
    QUALITY_REMOTE_FORCED,
    QUALITY_RESTART,
    DNP3Client,
)


class TestDnp3Crc16:
    """CRC-16/DNP 算法正确性"""

    def test_crc_check_value(self):
        """标准校验值: CRC-16/DNP over '123456789' == 0xEA82"""
        assert DNP3Client._calculate_crc16(b"123456789") == 0xEA82

    def test_crc_not_modbus_poly(self):
        """确保不再使用 Modbus 多项式 0xA001 (反射 0x8005)"""
        # Modbus CRC over '123456789' == 0x4B37，DNP3 必须不同
        assert DNP3Client._calculate_crc16(b"123456789") != 0x4B37

    def test_crc_empty(self):
        """空输入 CRC == init ^ xorout == 0xFFFF"""
        assert DNP3Client._calculate_crc16(b"") == 0xFFFF

    def test_crc_returns_int(self):
        """CRC 返回 int (便于块 CRC 拼装)"""
        assert isinstance(DNP3Client._calculate_crc16(b"\x01\x02"), int)


class TestDnp3FrameBuild:
    """_build_transport_frame 链路层帧结构正确性"""

    def _make_client(self, device_address: int = 1) -> DNP3Client:
        c = DNP3Client("127.0.0.1")
        c._device_address = device_address
        return c

    def test_header_structure(self):
        """START / LEN(1B) / CTRL / DST / SRC / HeaderCRC 布局正确"""
        client = self._make_client(device_address=0x0102)
        apdu = bytes([0x80, 0x01, 0x01, 0x00])  # 任意 APDU
        frame = client._build_transport_frame(apdu)

        assert frame[0:2] == b"\x05\x64"                       # START
        # LEN = 5 + len(user_data), user_data = 1(transport) + 4(apdu) = 5
        assert frame[2] == 5 + 5                                # LEN 单字节 = 10
        assert frame[3] == DNP3_LINK_CTRL_USER_DATA            # CTRL = 0x44
        assert struct.unpack_from("<H", frame, 4)[0] == 0x0102  # DST (LE)
        assert struct.unpack_from("<H", frame, 6)[0] == 0x0000  # SRC

    def test_header_crc_valid(self):
        """HeaderCRC 覆盖 LEN+CTRL+DST+SRC (frame[2:8])"""
        client = self._make_client()
        frame = client._build_transport_frame(bytes([0xC0, 0x01, 0x01, 0x02]))
        expected = DNP3Client._calculate_crc16(frame[2:8])
        assert struct.unpack_from("<H", frame, 8)[0] == expected

    def test_no_zero_placeholder_in_body(self):
        """body 中不再出现 0x00 占位 CRC：每个 16 字节块后是真实 2 字节 CRC"""
        client = self._make_client()
        # 20 字节 APDU → user_data 21 字节 → 2 个块 (16 + 5)，各带 CRC
        apdu = bytes(range(20))
        frame = client._build_transport_frame(apdu)

        body = frame[DNP3_HEADER_SIZE:]
        # 块1: body[0:16] + CRC[16:18]; 块2: body[18:23] + CRC[23:25]
        block1, crc1 = body[0:16], struct.unpack_from("<H", body, 16)[0]
        block2, crc2 = body[18:23], struct.unpack_from("<H", body, 23)[0]
        assert crc1 == DNP3Client._calculate_crc16(block1)
        assert crc2 == DNP3Client._calculate_crc16(block2)
        # 占位符 0x0000 在随机数据下几乎不可能同时命中两块 CRC
        assert not (crc1 == 0 and crc2 == 0)

    def test_single_byte_length_field(self):
        """LEN 必须是单字节 (社区版原为 2 字节 struct.pack('<H'))"""
        client = self._make_client()
        frame = client._build_transport_frame(bytes([0x01, 0x02, 0x03]))
        # frame[2] 是 LEN，frame[3] 必须是 CTRL(0x44) 而非 LEN 高字节 0x00
        assert frame[3] == DNP3_LINK_CTRL_USER_DATA

    def test_oversized_apdu_raises(self):
        """user_data 超过 250 字节 (LEN>255) 应抛出 ValueError"""
        client = self._make_client()
        with __import__("pytest").raises(ValueError):
            client._build_transport_frame(b"\x00" * 300)


class TestDnp3RoundTrip:
    """build → extract 往返一致性 + CRC 校验"""

    def _make_client(self) -> DNP3Client:
        c = DNP3Client("127.0.0.1")
        return c

    def test_extract_recovers_user_data(self):
        """_extract_user_data 还原 transport+APDU，校验全部通过"""
        client = self._make_client()
        apdu = bytes([0x80, 0x01, 0x01, 0x00, 0x01, 0x01, 0x06, 0x00, 0x00, 0x00, 0x00, 0x0F])
        frame = client._build_transport_frame(apdu)
        user_data = client._extract_user_data(frame)
        assert user_data is not None
        assert user_data[0] == 0xC0            # transport header
        assert user_data[1:] == apdu           # APDU 原样还原

    def test_extract_multi_block(self):
        """超过 16 字节的 user_data 跨块仍能正确剥离 CRC 还原"""
        client = self._make_client()
        apdu = bytes(range(40))                # 40 字节 → user_data 41 字节 → 3 块
        frame = client._build_transport_frame(apdu)
        user_data = client._extract_user_data(frame)
        assert user_data is not None
        assert len(user_data) == 41
        assert user_data[1:] == apdu

    def test_header_crc_tamper_rejected(self):
        """篡改 header 字节 → HeaderCRC 校验失败 → 返回 None"""
        client = self._make_client()
        frame = bytearray(client._build_transport_frame(bytes([0x01, 0x02, 0x03])))
        frame[4] ^= 0xFF                       # 篡改 destination
        assert client._extract_user_data(bytes(frame)) is None

    def test_block_crc_tamper_rejected(self):
        """篡改 body 字节 → BlockCRC 校验失败 → 返回 None"""
        client = self._make_client()
        frame = bytearray(client._build_transport_frame(bytes(range(20))))
        frame[DNP3_HEADER_SIZE + 5] ^= 0xFF    # 篡改第一块内某字节
        assert client._extract_user_data(bytes(frame)) is None

    def test_bad_start_rejected(self):
        """非 0x0564 起始字节 → 拒绝"""
        client = self._make_client()
        frame = bytearray(client._build_transport_frame(bytes([0x01])))
        frame[0] = 0x00
        assert client._extract_user_data(bytes(frame)) is None


# ==================== Task #13: 质量标志 / 分段重组 / SBO 流程 ====================


class _FakeReader:
    """模拟 asyncio.StreamReader，从预设缓冲区按序返回字节。

    readexactly(n) 返回前 n 字节并从缓冲区移除；不足时抛 IncompleteReadError。
    """

    def __init__(self, buffer: bytes):
        self._buffer = bytearray(buffer)

    async def readexactly(self, n: int) -> bytes:
        if len(self._buffer) < n:
            raise asyncio.IncompleteReadError(
                partial=bytes(self._buffer), expected=n
            )
        chunk = bytes(self._buffer[:n])
        del self._buffer[:n]
        return chunk


class _FakeWriter:
    """模拟 asyncio.StreamWriter，捕获写入帧供断言。"""

    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(bytes(data))

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return default


def _build_frame_with_transport(client: DNP3Client, transport: int, apdu: bytes) -> bytes:
    """构建指定传输头的链路层帧 (用于多分段测试)。

    _build_transport_frame 固定使用 0xC0 (FIR+FIN)，本助手允许自定义传输头，
    以模拟 FIR-only / FIN-only / 中间段 等分段场景。
    """
    user_data = bytes([transport]) + apdu
    length = 5 + len(user_data)
    header_body = bytes([length & 0xFF, DNP3_LINK_CTRL_USER_DATA])
    header_body += struct.pack("<H", client._device_address)
    header_body += struct.pack("<H", 0x0000)
    frame = DNP3_START_BYTES + header_body
    frame += struct.pack("<H", DNP3Client._calculate_crc16(header_body))
    for i in range(0, len(user_data), DNP3_DATA_BLOCK_MAX):
        block = user_data[i:i + DNP3_DATA_BLOCK_MAX]
        frame += block + struct.pack("<H", DNP3Client._calculate_crc16(block))
    return bytes(frame)


def _build_response_frame(
    client: DNP3Client, seq: int, func_code: int = FC_RESPONSE
) -> bytes:
    """构建单帧响应 (FIR+FIN)，匹配给定序列号与功能码。

    user_data 布局: transport(0xC0) + app_control(0xC0) + seq + func_code + data_len(0)
    _send_command_and_wait 校验 user_data[2]==seq, user_data[3]==func_code。
    """
    apdu = bytes([0xC0, seq, func_code, 0x00])
    return _build_frame_with_transport(client, 0xC0, apdu)


class TestDnp3QualityMultiState:
    """质量标志多状态组合解码 (Task #13)

    FIXED-P1: 原代码用 elif 只返回单个状态，DNP3 质量位是可组合位掩码。
    """

    def test_online_only_returns_good(self):
        """仅 ONLINE 位 → good"""
        assert DNP3Client._decode_quality(QUALITY_ONLINE) == "good"

    def test_offline_when_online_bit_clear(self):
        """ONLINE=0 → offline (无论其他标志是否设置)"""
        assert DNP3Client._decode_quality(0x00) == "offline"
        assert DNP3Client._decode_quality(QUALITY_RESTART) == "offline"
        assert DNP3Client._decode_quality(QUALITY_COMM_LOST | QUALITY_RESTART) == "offline"

    def test_restart_only(self):
        """ONLINE + RESTART → restart"""
        assert DNP3Client._decode_quality(QUALITY_ONLINE | QUALITY_RESTART) == "restart"

    def test_comm_lost_only(self):
        """ONLINE + COMM_LOST → comm_lost"""
        assert DNP3Client._decode_quality(QUALITY_ONLINE | QUALITY_COMM_LOST) == "comm_lost"

    def test_multi_state_combination(self):
        """多标志组合: ONLINE + RESTART + COMM_LOST → restart,comm_lost"""
        flags = QUALITY_ONLINE | QUALITY_RESTART | QUALITY_COMM_LOST
        result = DNP3Client._decode_quality(flags)
        parts = set(result.split(","))
        assert parts == {"restart", "comm_lost"}

    def test_all_quality_flags_combined(self):
        """全部质量标志组合 (除 RESERVED/DT) → 5 个状态"""
        flags = (
            QUALITY_ONLINE | QUALITY_RESTART | QUALITY_COMM_LOST
            | QUALITY_REMOTE_FORCED | QUALITY_LOCAL_FORCED | QUALITY_CHATTER_FILTER
        )
        result = DNP3Client._decode_quality(flags)
        parts = set(result.split(","))
        assert parts == {
            "restart", "comm_lost", "remote_forced", "local_forced", "chatter_filter"
        }

    def test_forced_flags_combined(self):
        """ONLINE + REMOTE_FORCED + LOCAL_FORCED → remote_forced,local_forced"""
        flags = QUALITY_ONLINE | QUALITY_REMOTE_FORCED | QUALITY_LOCAL_FORCED
        result = DNP3Client._decode_quality(flags)
        parts = set(result.split(","))
        assert parts == {"remote_forced", "local_forced"}

    def test_chatter_filter(self):
        """ONLINE + CHATTER_FILTER → chatter_filter"""
        assert DNP3Client._decode_quality(QUALITY_ONLINE | QUALITY_CHATTER_FILTER) == "chatter_filter"


class TestDnp3SegmentReassembly:
    """传输层分段重组 (Task #13)

    FIXED-P1: 原代码只读单个链路帧，不处理 FIR/FIN 分段。
    """

    def _make_client(self) -> DNP3Client:
        c = DNP3Client("127.0.0.1")
        c._connected = True
        c._timeout = 2.0
        return c

    async def test_single_frame_fir_fin(self):
        """单帧 (FIR+FIN) 重组返回完整 user_data"""
        client = self._make_client()
        apdu = bytes([0xC0, 0x01, 0x0F, 0x00, 0x01])
        frame = _build_frame_with_transport(client, 0xC0, apdu)
        client._reader = _FakeReader(frame)

        result = await client._reassemble_response()
        assert result is not None
        assert result[0] == 0xC0           # 传输头保留
        assert result[1:] == apdu          # APDU 原样还原

    async def test_two_segment_reassembly(self):
        """两段重组: FIR(无FIN) + FIN(无FIR)"""
        client = self._make_client()
        apdu1 = bytes([0xC0, 0x01, 0x0F, 0x00])
        apdu2 = bytes([0x01, 0x02, 0x03, 0x04])
        frame1 = _build_frame_with_transport(client, APPCONTROL_FIR, apdu1)        # 0x80 FIR only
        frame2 = _build_frame_with_transport(client, APPCONTROL_FIN, apdu2)        # 0x40 FIN only
        client._reader = _FakeReader(frame1 + frame2)

        result = await client._reassemble_response()
        assert result is not None
        assert result[0] == APPCONTROL_FIR            # 首段传输头保留
        assert result[1:] == apdu1 + apdu2            # 首段 APDU + 后续段 APDU (去传输头)

    async def test_three_segment_reassembly(self):
        """三段重组: FIR + 中间段 + FIN"""
        client = self._make_client()
        apdu1 = bytes([0xC0, 0x01, 0x0F])
        apdu2 = bytes([0xAA, 0xBB])
        apdu3 = bytes([0xCC, 0xDD, 0xEE])
        frame1 = _build_frame_with_transport(client, APPCONTROL_FIR, apdu1)         # 0x80 FIR
        frame2 = _build_frame_with_transport(client, 0x00, apdu2)                   # 中间段
        frame3 = _build_frame_with_transport(client, APPCONTROL_FIN, apdu3)         # 0x40 FIN
        client._reader = _FakeReader(frame1 + frame2 + frame3)

        result = await client._reassemble_response()
        assert result is not None
        assert result[0] == APPCONTROL_FIR
        assert result[1:] == apdu1 + apdu2 + apdu3

    async def test_missing_fin_returns_none_on_eof(self):
        """首段 FIR 但无 FIN，后续无数据 → IncompleteReadError → None"""
        client = self._make_client()
        apdu1 = bytes([0xC0, 0x01, 0x0F])
        frame1 = _build_frame_with_transport(client, APPCONTROL_FIR, apdu1)  # FIR only, 无 FIN
        client._reader = _FakeReader(frame1)  # 无后续数据

        result = await client._reassemble_response()
        assert result is None

    async def test_bad_start_byte_returns_none(self):
        """错误起始字节 → _read_link_frame 返回 None → 重组返回 None"""
        client = self._make_client()
        bad_frame = bytes([0x00, 0x00]) + bytes(8)  # 非 0x0564
        client._reader = _FakeReader(bad_frame)

        result = await client._reassemble_response()
        assert result is None


class TestDnp3SboFlow:
    """SBO (Select Before Operate) 完整流程 (Task #13)

    FIXED-P1: 原代码只发 SELECT 不读响应、不发 OPERATE。
    修复后实现 SELECT → 读响应 → OPERATE → 读响应 的完整 SBO 流程。
    """

    def _make_client(self) -> DNP3Client:
        c = DNP3Client("127.0.0.1")
        c._connected = True
        c._timeout = 2.0
        return c

    async def test_sbo_binary_output_success(self):
        """SBO 流程: SELECT→响应→OPERATE→响应 → True，发送 2 个命令帧"""
        client = self._make_client()
        # _sequence 初始 0: select_seq=1, operate_seq=2
        select_resp = _build_response_frame(client, seq=1)
        operate_resp = _build_response_frame(client, seq=2)
        client._reader = _FakeReader(select_resp + operate_resp)
        client._writer = _FakeWriter()

        result = await client.write_binary_output(index=5, value=True, op_type="sbo")
        assert result is True
        assert len(client._writer.written) == 2   # SELECT + OPERATE

    async def test_sbo_select_failure_returns_false(self):
        """SELECT 响应功能码错误 → False，仅发送 SELECT (不发送 OPERATE)"""
        client = self._make_client()
        # 响应 func_code=FC_READ(0x01) 而非 FC_RESPONSE(0x0F)
        bad_resp = _build_response_frame(client, seq=1, func_code=0x01)
        client._reader = _FakeReader(bad_resp)
        client._writer = _FakeWriter()

        result = await client.write_binary_output(index=5, value=True, op_type="sbo")
        assert result is False
        assert len(client._writer.written) == 1   # 仅 SELECT

    async def test_sbo_operate_failure_returns_false(self):
        """SELECT 成功但 OPERATE 响应 seq 不匹配 → False，发送 2 个命令帧"""
        client = self._make_client()
        select_resp = _build_response_frame(client, seq=1)
        # OPERATE 期望 seq=2，但响应 seq=99
        bad_operate_resp = _build_response_frame(client, seq=99)
        client._reader = _FakeReader(select_resp + bad_operate_resp)
        client._writer = _FakeWriter()

        result = await client.write_binary_output(index=5, value=True, op_type="sbo")
        assert result is False
        assert len(client._writer.written) == 2   # SELECT + OPERATE 均已发送

    async def test_direct_operate_success(self):
        """DIRECT_OPERATE 流程: 命令→响应 → True，仅 1 个命令帧"""
        client = self._make_client()
        resp = _build_response_frame(client, seq=1)
        client._reader = _FakeReader(resp)
        client._writer = _FakeWriter()

        result = await client.write_binary_output(index=5, value=True, op_type="direct")
        assert result is True
        assert len(client._writer.written) == 1

    async def test_direct_operate_seq_mismatch_returns_false(self):
        """DIRECT_OPERATE 响应 seq 不匹配 → False"""
        client = self._make_client()
        resp = _build_response_frame(client, seq=99)  # 期望 seq=1
        client._reader = _FakeReader(resp)
        client._writer = _FakeWriter()

        result = await client.write_binary_output(index=5, value=True, op_type="direct")
        assert result is False

    async def test_sbo_no_response_returns_false(self):
        """SBO 无响应 (空缓冲区) → False"""
        client = self._make_client()
        client._reader = _FakeReader(b"")
        client._writer = _FakeWriter()

        result = await client.write_binary_output(index=5, value=True, op_type="sbo")
        assert result is False

    async def test_sbo_analog_output_success(self):
        """SBO 模拟输出流程: SELECT→响应→OPERATE→响应 → True"""
        client = self._make_client()
        select_resp = _build_response_frame(client, seq=1)
        operate_resp = _build_response_frame(client, seq=2)
        client._reader = _FakeReader(select_resp + operate_resp)
        client._writer = _FakeWriter()

        result = await client.write_analog_output(index=3, value=42.5, op_type="sbo")
        assert result is True
        assert len(client._writer.written) == 2

    async def test_direct_analog_output_success(self):
        """DIRECT_OPERATE 模拟输出 → True"""
        client = self._make_client()
        resp = _build_response_frame(client, seq=1)
        client._reader = _FakeReader(resp)
        client._writer = _FakeWriter()

        result = await client.write_analog_output(index=3, value=42.5, op_type="direct")
        assert result is True
        assert len(client._writer.written) == 1

    async def test_not_connected_returns_false(self):
        """未连接 → 直接返回 False，不发送任何帧"""
        client = self._make_client()
        client._connected = False
        client._writer = _FakeWriter()

        result = await client.write_binary_output(index=5, value=True, op_type="sbo")
        assert result is False
        assert len(client._writer.written) == 0
