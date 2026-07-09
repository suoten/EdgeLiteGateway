"""DNP3 驱动 - 分布式网络协议 (IEC 62351-6)

DNP3是电力/水务SCADA系统中广泛使用的协议(ANSI C37.1, IEEE 1815)，
与IEC 104类似但更注重配电自动化场景。

支持:
- DNP3 over TCP (默认端口20000)
- 16位/32位二进制输入状态
- 模拟输入 (16/32位整数、浮点)
- 计数器输入
- 双点命令、单点命令
- 直接操作/选择-执行操作 (SBO)
- 主动上报 (unsolicited response)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

# DNP3 默认配置
DEFAULT_DNP3_PORT = 20000

# DNP3 功能码
FC_READ = 0x01
FC_WRITE = 0x02
FC_SELECT = 0x03
FC_OPERATE = 0x04
FC_DIRECT_OPERATE = 0x05
FC_DIRECT_OPERATE_NORESP = 0x06
FC_IMMEDIATE_FREEZE = 0x07
FC_IMMEDIATE_FREEZE_NORESP = 0x08
FC_FREEZE_CLEAR = 0x09
FC_FREEZE_CLEAR_NORESP = 0x0A
FC_FREEZE_AT_TIME = 0x0B
FC_FREEZE_AT_TIME_NORESP = 0x0C
FC_PRIVATE = 0x13
FC_RESPONSE = 0x0F
FC_UNSOLICITED_RESPONSE = 0x14

# DNP3 对象组 (部分常用)
GROUP_BINARY_INPUT = 1
GROUP_BINARY_INPUT_EVENT = 2
GROUP_DOUBLE_BINARY_INPUT = 3
GROUP_DOUBLE_BINARY_EVENT = 4
GROUP_BINARY_OUTPUT = 10
GROUP_BINARY_OUTPUT_EVENT = 11
GROUP_DOUBLE_BINARY_OUTPUT = 12
GROUP_DOUBLE_BINARY_OUTPUT_EVENT = 13
GROUP_ANALOG_INPUT = 30
GROUP_ANALOG_INPUT_EVENT = 32
GROUP_ANALOG_OUTPUT = 40
GROUP_ANALOG_INPUT_16BIT = 34
GROUP_ANALOG_INPUT_32BIT = 35
GROUP_ANALOG_INPUT_FLOAT = 36
GROUP_COUNTER = 20
GROUP_COUNTER_EVENT = 22

# DNP3 变体
VAR_BINARY_INPUT = 1
VAR_BINARY_INPUT_WITH_STATUS = 2
VAR_ANALOG_INPUT_16BIT = 1
VAR_ANALOG_INPUT_32BIT = 2
VAR_ANALOG_INPUT_FLOAT = 3
VAR_ANALOG_INPUT_DOUBLE = 4
VAR_ANALOG_INPUT_32BIT_SNS = 5
VAR_ANALOG_INPUT_FP = 6
VAR_COUNTER_16BIT = 1
VAR_COUNTER_32BIT = 2
VAR_COUNTER_16BIT_SNS = 5

# 质量标志
QUALITY_ONLINE = 0x01
QUALITY_RESTART = 0x02
QUALITY_COMM_LOST = 0x04
QUALITY_REMOTE_FORCED = 0x08
QUALITY_LOCAL_FORCED = 0x10
QUALITY_CHATTER_FILTER = 0x20
QUALITY_RESERVED = 0x40
QUALITY_DT = 0x80

# Application Control
APPCONTROL_FIR = 0x80
APPCONTROL_FIN = 0x40
APPCONTROL_CON = 0x20
APPCONTROL_UNS = 0x10

# DNP3 传输头
DNP3_HEADER_SIZE = 10
DNP3_START_BYTES = b"\x05\x64"  # DNP3 起始字节 (同步字)
DNP3_LINK_CTRL_USER_DATA = 0x44  # 链路层控制字节: User Data (PRM=1, FCB=0, FCV=0, Function=4)
DNP3_DATA_BLOCK_MAX = 16  # DNP3 数据块最大字节数 (不含 CRC)


def _decode_uint16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _decode_int16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<h", data, offset)[0]


def _decode_uint32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _decode_int32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<i", data, offset)[0]


def _decode_float(data: bytes, offset: int) -> float:
    return struct.unpack_from("<f", data, offset)[0]


def _decode_double(data: bytes, offset: int) -> float:
    return struct.unpack_from("<d", data, offset)[0]


@dataclass
class DNP3Point:
    """DNP3数据点"""
    index: int
    group: int
    variation: int
    value: Any
    quality: str
    timestamp: datetime | None = None


@dataclass
class DNP3Device:
    """DNP3设备信息"""
    device_address: int
    name: str = ""
    online: bool = False


class DNP3Client:
    """DNP3 TCP客户端封装"""

    def __init__(self, host: str, port: int = DEFAULT_DNP3_PORT, timeout: float = 10.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._device_address: int = 1
        self._sequence: int = 0
        self._unsol_sequence: int = 0
        self._connected: bool = False
        self._data_callback: Any = None

    async def connect(self) -> bool:
        """建立DNP3连接"""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._timeout,
            )
            self._connected = True
            logger.info("DNP3 TCP连接成功: %s:%d", self._host, self._port)
            return True
        except Exception as e:
            logger.error("DNP3连接失败: %s - %s", self._host, e)
            return False

    def close(self) -> None:
        """关闭连接"""
        self._connected = False
        if self._writer:
            self._writer.close()
            self._writer = None
        self._reader = None

    async def read_binary_inputs(self, start: int = 0, count: int = 16) -> list[DNP3Point]:
        """读取二进制输入"""
        return await self._read_points(GROUP_BINARY_INPUT, VAR_BINARY_INPUT_WITH_STATUS, start, count)

    async def read_analog_inputs(
        self, start: int = 0, count: int = 16, variation: int = VAR_ANALOG_INPUT_32BIT
    ) -> list[DNP3Point]:
        """读取模拟输入"""
        return await self._read_points(GROUP_ANALOG_INPUT, variation, start, count)

    async def read_counters(self, start: int = 0, count: int = 16) -> list[DNP3Point]:
        """读取计数器"""
        return await self._read_points(GROUP_COUNTER, VAR_COUNTER_32BIT, start, count)

    async def _read_points(
        self, group: int, variation: int, start: int, count: int
    ) -> list[DNP3Point]:
        """读取DNP3数据点"""
        if not self._connected:
            return []

        seq = self._get_next_sequence()

        # 构建读请求
        apdu = bytes([APPCONTROL_FIR | APPCONTROL_FIN])  # Application control
        apdu += bytes([seq])  # Sequence
        apdu += bytes([FC_READ])  # Function code

        # 对象头: Group, Variation, Qualifier
        # Qualifier = 0x06 (范围: 0x00, 0x00, start, stop)
        apdu += bytes([group])  # Group
        apdu += bytes([variation])  # Variation (0 = use default)
        apdu += bytes([0x06])  # Qualifier code: range
        apdu += bytes([0x00, start & 0xFF, (start >> 8) & 0xFF])  # Start index
        apdu += bytes([0x00, count & 0xFF, (count >> 8) & 0xFF])  # Stop index

        frame = self._build_transport_frame(apdu)

        try:
            if self._writer:
                self._writer.write(frame)
                await self._writer.drain()

            # 读取响应
            response = await asyncio.wait_for(self._reader.readexactly(DNP3_HEADER_SIZE), timeout=self._timeout)
            resp_len = struct.unpack_from("<H", response, 8)[0]

            if resp_len > DNP3_HEADER_SIZE:
                resp_body = await self._reader.readexactly(resp_len - DNP3_HEADER_SIZE)
                response += resp_body

            return self._parse_response(response)

        except asyncio.TimeoutError:
            logger.warning("DNP3读取超时: %s:%d", self._host, self._port)
            return []
        except Exception as e:
            logger.error("DNP3读取失败: %s", e)
            return []

    async def write_binary_output(self, index: int, value: bool, op_type: str = "direct") -> bool:
        """写入二进制输出

        Args:
            index: 输出点索引
            value: True=闭合, False=断开
            op_type: direct(直接) 或 sbo(选择执行)
        """
        if not self._connected:
            return False

        def _build_apdu(seq: int, func_code: int) -> bytes:
            apdu = bytes([APPCONTROL_FIR | APPCONTROL_FIN])
            apdu += bytes([seq])
            apdu += bytes([func_code])
            apdu += bytes([GROUP_BINARY_OUTPUT])
            apdu += bytes([VAR_BINARY_INPUT])
            apdu += bytes([0x07])
            apdu += bytes([0x00, index & 0xFF, (index >> 8) & 0xFF])
            control = 0xFC if not value else 0xF8
            apdu += bytes([control, 0x00, 0x00, 0x00])
            apdu += bytes([0x00])
            return apdu

        try:
            if op_type == "sbo":
                # SBO: SELECT → 响应 → OPERATE → 响应
                select_seq = self._get_next_sequence()
                select_apdu = _build_apdu(select_seq, FC_SELECT)
                select_resp = await self._send_command_and_wait(select_apdu, select_seq, FC_RESPONSE)
                if select_resp is None:
                    return False
                operate_seq = self._get_next_sequence()
                operate_apdu = _build_apdu(operate_seq, FC_OPERATE)
                operate_resp = await self._send_command_and_wait(operate_apdu, operate_seq, FC_RESPONSE)
                return operate_resp is not None
            else:
                # DIRECT_OPERATE: 命令 → 响应
                seq = self._get_next_sequence()
                apdu = _build_apdu(seq, FC_DIRECT_OPERATE)
                resp = await self._send_command_and_wait(apdu, seq, FC_RESPONSE)
                return resp is not None
        except Exception as e:
            logger.error("DNP3写入失败: %s", e)
            return False

    async def write_analog_output(self, index: int, value: float, op_type: str = "direct") -> bool:
        """写入模拟输出"""
        if not self._connected:
            return False

        def _build_apdu(seq: int, func_code: int) -> bytes:
            apdu = bytes([APPCONTROL_FIR | APPCONTROL_FIN])
            apdu += bytes([seq])
            apdu += bytes([func_code])
            apdu += bytes([GROUP_ANALOG_OUTPUT])
            apdu += bytes([VAR_ANALOG_INPUT_DOUBLE])
            apdu += bytes([0x07])
            apdu += bytes([0x00, index & 0xFF, (index >> 8) & 0xFF])
            apdu += struct.pack("<d", value)
            apdu += bytes([0x00])
            return apdu

        try:
            if op_type == "sbo":
                select_seq = self._get_next_sequence()
                select_apdu = _build_apdu(select_seq, FC_SELECT)
                select_resp = await self._send_command_and_wait(select_apdu, select_seq, FC_RESPONSE)
                if select_resp is None:
                    return False
                operate_seq = self._get_next_sequence()
                operate_apdu = _build_apdu(operate_seq, FC_OPERATE)
                operate_resp = await self._send_command_and_wait(operate_apdu, operate_seq, FC_RESPONSE)
                return operate_resp is not None
            else:
                seq = self._get_next_sequence()
                apdu = _build_apdu(seq, FC_DIRECT_OPERATE)
                resp = await self._send_command_and_wait(apdu, seq, FC_RESPONSE)
                return resp is not None
        except Exception as e:
            logger.error("DNP3模拟输出写入失败: %s", e)
            return False

    async def enable_unsolicited(self, device_address: int) -> bool:
        """启用主动上报"""
        if not self._connected:
            return False

        self._device_address = device_address
        seq = self._get_next_sequence()

        apdu = bytes([APPCONTROL_FIR | APPCONTROL_FIN])
        apdu += bytes([seq])
        apdu += bytes([0x14])  # Enable unsolicited
        apdu += bytes([GROUP_BINARY_INPUT])  # Object: binary input
        apdu += bytes([0xFF])  # Variation: all
        apdu += bytes([0x06])  # Qualifier: range
        apdu += bytes([0x00, 0x00, 0x00])  # Start
        apdu += bytes([0x00, 0xFF, 0xFF])  # Stop

        frame = self._build_transport_frame(apdu)

        try:
            if self._writer:
                self._writer.write(frame)
                await self._writer.drain()
            return True
        except Exception as e:
            logger.error("DNP3启用主动上报失败: %s", e)
            return False

    def _build_transport_frame(self, apdu: bytes) -> bytes:
        """构建DNP3传输帧 (正确帧结构: START + LEN + CTRL + DST + SRC + HeaderCRC + DataBlocks)"""
        transport_byte = 0xC0  # FIR + FIN
        user_data = bytes([transport_byte]) + apdu
        length = 5 + len(user_data)  # 5 = CTRL(1) + DST(2) + SRC(2)
        if length > 0xFF:
            raise ValueError(f"APDU 过大: user_data {len(user_data)} 字节, LEN {length} 超过 255 上限")
        # Header body: LEN + CTRL + DST + SRC
        header_body = bytes([length & 0xFF, DNP3_LINK_CTRL_USER_DATA])
        header_body += struct.pack("<H", self._device_address)
        header_body += struct.pack("<H", 0x0000)
        frame = DNP3_START_BYTES + header_body
        # Header CRC
        frame += struct.pack("<H", self._calculate_crc16(header_body))
        # Data blocks (max 16 bytes each + 2 byte CRC)
        for i in range(0, len(user_data), DNP3_DATA_BLOCK_MAX):
            block = user_data[i:i + DNP3_DATA_BLOCK_MAX]
            frame += block + struct.pack("<H", self._calculate_crc16(block))
        return frame

    def _extract_user_data(self, frame: bytes) -> bytes | None:
        """从 DNP3 帧中提取 user_data (transport + APDU)，校验 HeaderCRC 和 BlockCRC。

        返回 user_data 字节串；校验失败返回 None。
        """
        if len(frame) < DNP3_HEADER_SIZE:
            return None
        # 校验起始字节
        if frame[0:2] != DNP3_START_BYTES:
            return None
        # 校验 HeaderCRC (CRC of bytes 2-7: LEN+CTRL+DST+SRC)
        header_body = frame[2:8]
        header_crc = struct.unpack_from("<H", frame, 8)[0]
        if self._calculate_crc16(header_body) != header_crc:
            return None
        # 提取数据块
        user_data = bytearray()
        offset = DNP3_HEADER_SIZE
        while offset < len(frame):
            # 确定块大小 (最大 DNP3_DATA_BLOCK_MAX，最后一块可能更小)
            remaining = len(frame) - offset
            if remaining <= 2:
                # 只剩 CRC 或更少，没有数据
                break
            block_size = min(DNP3_DATA_BLOCK_MAX, remaining - 2)  # 留 2 字节给 CRC
            block = frame[offset:offset + block_size]
            if len(block) == 0:
                break
            # 确保 CRC 字节存在
            if offset + len(block) + 2 > len(frame):
                return None
            block_crc = struct.unpack_from("<H", frame, offset + len(block))[0]
            if self._calculate_crc16(block) != block_crc:
                return None
            user_data.extend(block)
            offset += len(block) + 2  # block data + CRC
        return bytes(user_data)

    @staticmethod
    def _calculate_crc16(data: bytes) -> int:
        """计算CRC-16/DNP (polynomial 0x3D65, reflected 0xA6BC, init=0x0000, xorout=0xFFFF)"""
        crc = 0x0000
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0xA6BC
                else:
                    crc >>= 1
        return crc ^ 0xFFFF

    def _parse_response(self, data: bytes) -> list[DNP3Point]:
        """解析DNP3响应"""
        points = []

        if len(data) < DNP3_HEADER_SIZE:
            return points

        # 跳过数据链路头
        offset = DNP3_HEADER_SIZE

        # 传输层第一个字节
        if offset >= len(data):
            return points
        offset += 1  # Skip transport header

        # 应用层头
        if offset + 4 > len(data):
            return points

        app_control = data[offset]
        offset += 1
        sequence = data[offset]
        offset += 1
        func_code = data[offset]
        offset += 1

        # 长度
        data_len = data[offset]
        offset += 1

        # 解析对象数据
        while offset < len(data) - 1:
            try:
                if offset >= len(data):
                    break

                group = data[offset]
                offset += 1
                variation = data[offset]
                offset += 1
                qualifier = data[offset]
                offset += 1

                # 读取范围
                range_start = struct.unpack_from("<H", data, offset)[0]
                offset += 2
                range_stop = struct.unpack_from("<H", data, offset)[0]
                offset += 2

                count = range_stop - range_start + 1

                for i in range(count):
                    index = range_start + i
                    value, quality = self._decode_object_value(group, variation, data, offset)
                    points.append(DNP3Point(
                        index=index,
                        group=group,
                        variation=variation,
                        value=value,
                        quality=quality,
                    ))
                    offset = self._advance_offset(group, variation, offset)

            except (IndexError, struct.error):
                break

        return points

    def _decode_object_value(self, group: int, variation: int, data: bytes, offset: int) -> tuple[Any, str]:
        """解码对象值"""
        quality = "good"

        if group == GROUP_BINARY_INPUT:
            if variation == VAR_BINARY_INPUT_WITH_STATUS:
                flags = data[offset]
                value = bool(flags & 0x01)
                quality = self._decode_quality(flags)
                offset += 1
            else:
                value = bool(data[offset] & 0x01)
                offset += 1

        elif group == GROUP_ANALOG_INPUT or group == GROUP_ANALOG_INPUT_16BIT:
            if variation == VAR_ANALOG_INPUT_16BIT:
                value = _decode_int16(data, offset)
                offset += 2
            elif variation == VAR_ANALOG_INPUT_32BIT:
                value = _decode_int32(data, offset)
                offset += 4
            elif variation == VAR_ANALOG_INPUT_32BIT_SNS:
                value = _decode_uint32(data, offset)
                offset += 4
            elif variation == VAR_ANALOG_INPUT_FLOAT:
                value = _decode_float(data, offset)
                offset += 4
            elif variation == VAR_ANALOG_INPUT_DOUBLE:
                value = _decode_double(data, offset)
                offset += 8
            else:
                value = _decode_int16(data, offset)
                offset += 2

        elif group == GROUP_COUNTER:
            if variation == VAR_COUNTER_16BIT:
                value = struct.unpack_from("<H", data, offset)[0]
                offset += 2
            elif variation == VAR_COUNTER_32BIT:
                value = _decode_uint32(data, offset)
                offset += 4
            else:
                value = struct.unpack_from("<H", data, offset)[0]
                offset += 2

        else:
            value = 0

        return value, quality

    @staticmethod
    def _decode_quality(flags: int) -> str:
        """解码质量标志 (位掩码组合)"""
        if flags & QUALITY_ONLINE == 0:
            return "offline"
        states = []
        if flags & QUALITY_RESTART:
            states.append("restart")
        if flags & QUALITY_COMM_LOST:
            states.append("comm_lost")
        if flags & QUALITY_REMOTE_FORCED:
            states.append("remote_forced")
        if flags & QUALITY_LOCAL_FORCED:
            states.append("local_forced")
        if flags & QUALITY_CHATTER_FILTER:
            states.append("chatter_filter")
        if not states:
            return "good"
        return ",".join(states)

    async def _read_link_frame(self) -> bytes | None:
        """读取单个链路层帧，返回 user_data (transport + APDU)；失败返回 None"""
        if not self._reader:
            return None
        try:
            # 读取起始字节
            start = await asyncio.wait_for(self._reader.readexactly(2), timeout=self._timeout)
            if start != DNP3_START_BYTES:
                return None
            # 读取长度字节
            length_byte = await asyncio.wait_for(self._reader.readexactly(1), timeout=self._timeout)
            length = length_byte[0]
            # 读取剩余头 (CTRL + DST + SRC + HeaderCRC = 7 bytes)
            remaining_header = await asyncio.wait_for(self._reader.readexactly(7), timeout=self._timeout)
            header = start + length_byte + remaining_header
            # 读取数据块
            user_data_len = length - 5  # 5 = CTRL(1) + DST(2) + SRC(2)
            if user_data_len < 0:
                return None
            data = bytearray()
            remaining = user_data_len
            while remaining > 0:
                block_size = min(DNP3_DATA_BLOCK_MAX, remaining)
                block = await asyncio.wait_for(self._reader.readexactly(block_size), timeout=self._timeout)
                crc_bytes = await asyncio.wait_for(self._reader.readexactly(2), timeout=self._timeout)
                block_crc = struct.unpack("<H", crc_bytes)[0]
                if self._calculate_crc16(block) != block_crc:
                    return None
                data.extend(block)
                remaining -= block_size
            return bytes(data)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, Exception):
            return None

    async def _reassemble_response(self) -> bytes | None:
        """重组传输层分段响应，返回完整 user_data；失败返回 None"""
        reassembled = bytearray()
        first_frame = True
        while True:
            user_data = await self._read_link_frame()
            if user_data is None:
                return None if first_frame else None
            if first_frame:
                if not (user_data[0] & APPCONTROL_FIR):
                    return None  # 首帧必须 FIR
                reassembled.extend(user_data)
                first_frame = False
            else:
                # 后续帧去掉传输头
                reassembled.extend(user_data[1:])
            # 检查 FIN
            if user_data[0] & APPCONTROL_FIN:
                return bytes(reassembled)

    async def _send_command_and_wait(self, apdu: bytes, expected_seq: int, expected_func: int) -> bytes | None:
        """发送命令帧并等待响应，校验序列号和功能码"""
        frame = self._build_transport_frame(apdu)
        if self._writer:
            self._writer.write(frame)
            await self._writer.drain()
        else:
            return None
        response = await self._reassemble_response()
        if response is None or len(response) < 4:
            return None
        # user_data[0] = transport, [1] = app_control, [2] = seq, [3] = func_code
        if response[2] != expected_seq or response[3] != expected_func:
            return None
        return response

    @staticmethod
    def _advance_offset(group: int, variation: int, offset: int) -> int:
        """推进偏移量"""
        if group in (GROUP_BINARY_INPUT, GROUP_BINARY_OUTPUT):
            return offset + 1
        elif group in (GROUP_ANALOG_INPUT, GROUP_ANALOG_INPUT_16BIT):
            if variation in (VAR_ANALOG_INPUT_32BIT, VAR_ANALOG_INPUT_32BIT_SNS, VAR_ANALOG_INPUT_FLOAT):
                return offset + 4
            elif variation == VAR_ANALOG_INPUT_DOUBLE:
                return offset + 8
            else:
                return offset + 2
        elif group == GROUP_COUNTER:
            if variation == VAR_COUNTER_32BIT:
                return offset + 4
            else:
                return offset + 2
        return offset + 1

    def _get_next_sequence(self) -> int:
        """获取下一个序列号"""
        self._sequence = (self._sequence + 1) & 0xFF
        return self._sequence


class DNP3Driver(DriverPlugin):
    """DNP3 协议驱动

    配置参数:
        host: DNP3主站/设备IP地址
        port: DNP3 TCP端口 (默认20000)
        device_address: DNP3设备地址 (默认1)
        timeout: 通信超时秒 (默认10)
    """

    plugin_name = "dnp3"
    plugin_version = "1.0.0"
    supported_protocols = ["dnp3", "dnp3_tcp"]
    config_schema = {
        "description": "DNP3 distributed network protocol for SCADA systems (power/water utility)",
        "fields": [
            {"name": "host", "type": "string", "label": "IP Address",
             "description": "DNP3 master/outstation IP address", "default": "192.168.1.1", "required": True},
            {"name": "port", "type": "integer", "label": "Port",
             "description": "DNP3 TCP port (default 20000)", "default": 20000},
            {"name": "device_address", "type": "integer", "label": "Device Address",
             "description": "DNP3 device address (default 1)", "default": 1},
            {"name": "timeout", "type": "number", "label": "Timeout (s)",
             "description": "Communication timeout in seconds", "default": 10.0},
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        self._running = False
        self._client: DNP3Client | None = None
        self._config: dict = {}
        self._device_points: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY

    async def start(self, config: dict) -> None:
        """启动DNP3驱动"""
        self._config = config
        host = config.get("host", "")
        port = int(config.get("port", DEFAULT_DNP3_PORT))
        timeout = float(config.get("timeout", 10.0))

        if not host:
            raise ValueError("DNP3驱动配置缺少host参数")

        self._client = DNP3Client(host, port, timeout)
        try:
            connected = await self._client.connect()
            if connected:
                self._running = True
                self._reconnect_count = 0
                logger.info("DNP3驱动启动成功: %s:%d", host, port)
            else:
                logger.error("DNP3驱动连接失败")
        except Exception as e:
            logger.error("DNP3驱动启动异常: %s", e)
            raise

    async def stop(self) -> None:
        """停止DNP3驱动"""
        self._running = False
        if self._client:
            self._client.close()
            self._client = None
        logger.info("DNP3驱动已停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加DNP3设备"""
        self._device_points[device_id] = {
            "config": config,
            "points": {p.get("name", ""): p for p in points if p.get("name")},
        }

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._device_points.pop(device_id, None)
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("DNP3 device removed: %s", device_id)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取DNP3测点值

        测点地址格式: "type:index" 如 "binary:0", "analog:10", "counter:0"
        类型前缀:
            binary - 二进制输入
            analog - 模拟输入
            counter - 计数器
        """
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        result = {}
        device_info = self._device_points.get(device_id, {})
        device_pts = device_info.get("points", {})

        # 按类型分组读取
        binary_points = []
        analog_points = []
        counter_points = []

        for point_addr in points:
            pt_def = device_pts.get(point_addr, {})
            point_type = pt_def.get("point_type", "")

            if "binary" in point_type.lower():
                binary_points.append((point_addr, pt_def))
            elif "analog" in point_type.lower():
                analog_points.append((point_addr, pt_def))
            elif "counter" in point_type.lower():
                counter_points.append((point_addr, pt_def))

        # 读取二进制输入
        if binary_points:
            try:
                data = await self._client.read_binary_inputs()
                for point_addr, pt_def in binary_points:
                    index = pt_def.get("index", 0)
                    for d in data:
                        if d.index == index:
                            result[point_addr] = d.value
                            break
            except Exception as e:
                logger.warning("DNP3二进制读取失败: %s", e)

        # 读取模拟输入
        if analog_points:
            try:
                data = await self._client.read_analog_inputs()
                for point_addr, pt_def in analog_points:
                    index = pt_def.get("index", 0)
                    for d in data:
                        if d.index == index:
                            result[point_addr] = d.value
                            break
            except Exception as e:
                logger.warning("DNP3模拟读取失败: %s", e)

        # 读取计数器
        if counter_points:
            try:
                data = await self._client.read_counters()
                for point_addr, pt_def in counter_points:
                    index = pt_def.get("index", 0)
                    for d in data:
                        if d.index == index:
                            result[point_addr] = d.value
                            break
            except Exception as e:
                logger.warning("DNP3计数器读取失败: %s", e)

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入DNP3测点值"""
        if not self._running or not self._client:
            return False

        device_info = self._device_points.get(device_id, {})
        device_pts = device_info.get("points", {})
        pt_def = device_pts.get(point, {})
        point_type = pt_def.get("point_type", "")
        index = pt_def.get("index", 0)

        try:
            if "binary" in point_type.lower():
                return await self._client.write_binary_output(index, bool(value))
            elif "analog" in point_type.lower():
                return await self._client.write_analog_output(index, float(value))
            else:
                return False
        except Exception as e:
            logger.error("DNP3写入失败 %s.%s: %s", device_id, point, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """DNP3设备发现 - 需要预先配置"""
        return []

    def is_device_connected(self, device_id: str) -> bool:
        """检查DNP3连接状态"""
        return self._running and self._client is not None

    async def _try_reconnect(self, device_id: str) -> None:
        """重连机制"""
        if not self._config:
            return

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("DNP3重连放弃: %s", device_id)
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("DNP3连接断开，%.1fs后重连 (第%d次)", delay, self._reconnect_count)
        await asyncio.sleep(delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY)

        try:
            host = self._config.get("host", "")
            port = int(self._config.get("port", DEFAULT_DNP3_PORT))
            timeout = float(self._config.get("timeout", 10.0))

            if self._client:
                self._client.close()

            self._client = DNP3Client(host, port, timeout)
            connected = await self._client.connect()

            if connected:
                self._running = True
                self._reconnect_count = 0
                self._reconnect_delay = self._RECONNECT_BASE_DELAY
                logger.info("DNP3重连成功: %s:%d", host, port)
        except Exception as e:
            logger.error("DNP3重连失败: %s", e)
