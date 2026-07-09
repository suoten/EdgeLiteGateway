"""DL/T 645-2007 多帧拼接修复单元测试

覆盖修复项:
- _get_more_flag 控制字节偏移修正 (frame[9] → frame[8])
- _parse_response 数据长度/起始偏移修正 (frame[10]→frame[9], data_start 11→10)
- 多帧拼接防无限循环:
  - 检查最新帧的 more_flag (而非初始响应)
  - MAX_CONTINUATION_FRAMES 安全上限
  - 达到上限时记录告警而非无限循环
"""

from __future__ import annotations

import asyncio
import sys
import struct

sys.path.insert(0, "src")

from edgelite.drivers.dlt645 import (
    CTRL_READ_DATA,
    CTRL_READ_NEXT,
    DLT645_DI_MAP,
    FRAME_HEAD,
    FRAME_TAIL,
    MAX_CONTINUATION_FRAMES,
    Dlt645Driver,
)


# ──────────────────────────────────────────────────────────────────
# 辅助: 构造 DL/T 645-2007 响应帧
# ──────────────────────────────────────────────────────────────────


def _build_response_frame(
    address: str,
    di: str,
    value_bytes: bytes,
    more_flag: bool = False,
    ctrl: int = CTRL_READ_DATA,
) -> bytes:
    """构造 DL/T 645-2007 响应帧

    帧结构: 68H | A0~A5 | 68H | C | L | DATA | CS | 16H
    DATA = DI(4, 反转) + value_bytes, 全部 +33H 加密
    C = 控制字节: bit7=1(从站响应) | bit5=more_flag | bit4-0=功能码
    """
    addr_bytes = Dlt645Driver._encode_address(address)
    di_bytes = bytes.fromhex(di)[::-1]
    data_domain = di_bytes + value_bytes
    data_with_33h = Dlt645Driver._add_33h(data_domain)
    length = len(data_with_33h)

    # 控制字节: bit7=1(响应方向), bit5=more_flag, 低5位=功能码
    control = 0x80 | (ctrl & 0x1F)
    if more_flag:
        control |= 0x20

    frame_body = (
        bytes([FRAME_HEAD])
        + addr_bytes
        + bytes([FRAME_HEAD])
        + bytes([control])
        + bytes([length])
        + data_with_33h
    )
    cs = Dlt645Driver._calculate_cs(frame_body[1:])
    return frame_body + bytes([cs, FRAME_TAIL])


def _make_driver(
    responses: list[bytes],
    address: str = "000000000001",
) -> Dlt645Driver:
    """构造 Dlt645Driver 并注入伪串口，按顺序返回预设响应帧"""
    driver = Dlt645Driver.__new__(Dlt645Driver)
    driver._running = True
    driver._lock = asyncio.Lock()
    driver._config = {}

    response_queue = list(responses)
    write_log: list[bytes] = []

    class _FakeSerial:
        def __init__(self):
            self.is_open = True
            self.timeout = 1.0
            self.in_waiting = 0

        def write(self, data: bytes) -> int:
            write_log.append(bytes(data))
            return len(data)

        def read(self, n: int) -> bytes:
            if not response_queue:
                return b""
            return response_queue.pop(0)

        def close(self):
            self.is_open = False

    driver._serial = _FakeSerial()
    driver._write_log = write_log  # 暴露给测试用于断言
    driver._devices = {
        "dev1": {
            "address": address,
            "di_map": dict(DLT645_DI_MAP),
            "points": [],
        }
    }
    return driver


def _patch_read_response(driver: Dlt645Driver, responses: list[bytes]) -> None:
    """替换 _read_response_async 为同步返回预设帧的协程"""
    queue = list(responses)

    async def _fake_read() -> bytes:
        if not queue:
            return b""
        return queue.pop(0)

    driver._read_response_async = _fake_read  # type: ignore[method-assign]


# ──────────────────────────────────────────────────────────────────
# 1. _get_more_flag 偏移修正
# ──────────────────────────────────────────────────────────────────


class TestGetMoreFlagOffset:
    """验证 _get_more_flag 读取 frame[8] (控制字节) 而非 frame[9] (长度)"""

    def test_more_flag_set(self):
        """控制字节 bit5=1 → 返回 1"""
        # 构造帧: 控制字节 = 0x80 | 0x20 | 0x11 = 0xB1 (响应 + more + 读数据)
        frame = _build_response_frame(
            "000000000001", "02010100", b"\x12\x34", more_flag=True
        )
        assert Dlt645Driver._get_more_flag(frame) == 1

    def test_more_flag_clear(self):
        """控制字节 bit5=0 → 返回 0"""
        frame = _build_response_frame(
            "000000000001", "02010100", b"\x12\x34", more_flag=False
        )
        assert Dlt645Driver._get_more_flag(frame) == 0

    def test_more_flag_not_affected_by_length_byte(self):
        """长度字节 (frame[9]) 的 bit5 不应影响 more_flag 判断

        修复前: _get_more_flag 读 frame[9]，若长度恰好含 bit5 (如 0x24=36)
        会误判为有后续数据。修复后只读 frame[8]。
        """
        # 构造一个长度恰好为 0x24 (bit5=1) 的帧，但控制字节 more=0
        addr_bytes = Dlt645Driver._encode_address("000000000001")
        di_bytes = bytes.fromhex("02010100")[::-1]
        # value 长度使总 DATA 长度 = 0x24 (36 字节): DI(4) + value(32)
        value = bytes([0x01] * 32)
        data_domain = di_bytes + value
        data_with_33h = Dlt645Driver._add_33h(data_domain)
        length = len(data_with_33h)  # = 36 = 0x24
        assert length == 0x24, f"测试前提: 长度应为 0x24, 实际 0x{length:02X}"

        # 控制字节: 0x80 (响应, more=0, 功能码=0)
        control = 0x80
        frame_body = (
            bytes([FRAME_HEAD]) + addr_bytes + bytes([FRAME_HEAD])
            + bytes([control]) + bytes([length]) + data_with_33h
        )
        cs = Dlt645Driver._calculate_cs(frame_body[1:])
        frame = frame_body + bytes([cs, FRAME_TAIL])

        # 修复前会读 frame[9]=0x24, bit5=1 → 误返回 1
        # 修复后读 frame[8]=0x80, bit5=0 → 正确返回 0
        assert Dlt645Driver._get_more_flag(frame) == 0

    def test_more_flag_short_frame_returns_zero(self):
        """帧长度不足时返回 0 (不抛异常)"""
        assert Dlt645Driver._get_more_flag(b"\x68\x00") == 0
        assert Dlt645Driver._get_more_flag(b"") == 0


# ──────────────────────────────────────────────────────────────────
# 2. _parse_response 偏移修正
# ──────────────────────────────────────────────────────────────────


class TestParseResponseOffset:
    """验证 _parse_response 使用正确的帧偏移解析数据"""

    def test_parse_bcd_value(self):
        """解析 BCD 编码的电压值"""
        # DI=02010100, value=0x12 0x34 → BCD 反转后 "3412" → 34.12V (decimal=1? 不, decimal=1 → 341.2)
        # 实际: _decode_bcd 反转 bytes([0x12, 0x34]) → "3412" → int=3412, decimal=1 → 341.2
        # 为简化, 用 decimal=2 测试: 3412/100 = 34.12
        frame = _build_response_frame(
            "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=False
        )
        point_info = {"type": "bcd", "decimal": 2, "unit": "V"}
        result = Dlt645Driver._parse_response(frame, "voltage_a", point_info)
        assert result is not None
        assert abs(result - 34.12) < 0.01

    def test_parse_returns_none_for_short_frame(self):
        """帧长度 < 12 → None"""
        point_info = {"type": "bcd", "decimal": 0}
        assert Dlt645Driver._parse_response(b"\x68" * 10, "x", point_info) is None

    def test_parse_returns_none_for_empty_value(self):
        """DATA 仅含 DI (4字节) 无值数据 → None"""
        frame = _build_response_frame(
            "000000000001", "02010100", b"", more_flag=False
        )
        point_info = {"type": "bcd", "decimal": 0}
        assert Dlt645Driver._parse_response(frame, "x", point_info) is None

    def test_parse_float32_value(self):
        """解析 IEEE 754 浮点"""
        expected = 220.5
        value_bytes = struct.pack("<f", expected)
        frame = _build_response_frame(
            "000000000001", "02010100", value_bytes, more_flag=False
        )
        point_info = {"type": "float32", "decimal": 0}
        result = Dlt645Driver._parse_response(frame, "x", point_info)
        assert result is not None
        assert abs(result - expected) < 1e-6

    def test_parse_hex_value(self):
        """解析 hex (小端整数)"""
        value_bytes = bytes([0x34, 0x12])  # 小端 → 0x1234 = 4660
        frame = _build_response_frame(
            "000000000001", "02010100", value_bytes, more_flag=False
        )
        point_info = {"type": "hex", "decimal": 0}
        result = Dlt645Driver._parse_response(frame, "x", point_info)
        assert result == 0x1234

    def test_parse_uses_correct_length_byte(self):
        """验证 length 取自 frame[9] 而非 frame[10]

        构造一个 frame[10] (数据首字节) 与 frame[9] (真实长度) 不同的帧,
        确保解析使用 frame[9]。
        """
        # DI=02010100, value=0x56 0x78 (2 字节)
        # frame[9] = 6 (DI 4 + value 2), frame[10] = 第一个 +33H 后的数据字节
        frame = _build_response_frame(
            "000000000001", "02010100", bytes([0x56, 0x78]), more_flag=False
        )
        assert frame[9] == 6  # 4 (DI) + 2 (value)
        # frame[10] 是 +33H 后的 DI 首字节, 不等于 6
        assert frame[10] != 6
        point_info = {"type": "bcd", "decimal": 0}
        result = Dlt645Driver._parse_response(frame, "x", point_info)
        assert result is not None
        # _decode_bcd(bytes([0x56, 0x78])) → 反转 "7856" → 7856
        assert result == 7856.0


# ──────────────────────────────────────────────────────────────────
# 3. 多帧拼接 — 防无限循环
# ──────────────────────────────────────────────────────────────────


class TestMultiFrameConcatenation:
    """验证多帧拼接正确终止，不会无限循环"""

    async def test_single_frame_no_continuation(self):
        """单帧响应 (more=0) 不触发续读"""
        # 构造单帧: more=0
        resp = _build_response_frame(
            "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=False
        )
        driver = _make_driver([resp])
        _patch_read_response(driver, [resp])

        result = await driver.read_points("dev1", ["voltage_a"])
        assert "voltage_a" in result
        assert result["voltage_a"] is not None
        # 只发了一帧 (初始读), 无续读请求
        assert len(driver._write_log) == 1

    async def test_multi_frame_normal_termination(self):
        """多帧响应: 第一帧 more=1, 第二帧 more=0 → 正常终止"""
        resp1 = _build_response_frame(
            "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=True
        )
        resp2 = _build_response_frame(
            "000000000001", "02010100", bytes([0x56, 0x78]), more_flag=False
        )
        driver = _make_driver([resp1, resp2])
        _patch_read_response(driver, [resp1, resp2])

        result = await driver.read_points("dev1", ["voltage_a"])
        assert result["voltage_a"] is not None
        # 发了两帧: 初始读 + 1 次续读
        assert len(driver._write_log) == 2
        # 第二帧是 CTRL_READ_NEXT (0x14)
        assert driver._write_log[1][8] == CTRL_READ_NEXT

    async def test_multi_frame_checks_latest_response_more_flag(self):
        """验证循环检查最新帧的 more_flag, 而非初始响应

        场景: 初始响应 more=1, 第二帧 more=0 → 应在第二帧后终止。
        修复前: 始终检查初始响应 (more=1), 会继续请求第三帧。
        """
        resp1 = _build_response_frame(
            "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=True
        )
        resp2 = _build_response_frame(
            "000000000001", "02010100", bytes([0x56, 0x78]), more_flag=False
        )
        driver = _make_driver([resp1, resp2, b""])
        _patch_read_response(driver, [resp1, resp2, b""])

        await driver.read_points("dev1", ["voltage_a"])
        # 修复后: resp2 more=0 → 终止, 只发 2 帧
        # 修复前: 检查 resp1 more=1 → 请求第 3 帧 (但返回空 → break)
        assert len(driver._write_log) == 2, (
            f"应在第二帧 more=0 后终止, 但发了 {len(driver._write_log)} 帧"
        )

    async def test_max_continuation_frames_limit(self):
        """恶意电表持续返回 more=1 → 达到 MAX_CONTINUATION_FRAMES 上限后终止

        修复前: 无上限, 无限循环。
        """
        # 构造 MAX_CONTINUATION_FRAMES + 1 个 more=1 的帧
        responses = [
            _build_response_frame(
                "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=True
            )
            for _ in range(MAX_CONTINUATION_FRAMES + 5)
        ]
        driver = _make_driver(responses)
        _patch_read_response(driver, responses)

        # 必须在合理时间内完成 (不无限循环)
        result = await asyncio.wait_for(
            driver.read_points("dev1", ["voltage_a"]),
            timeout=5.0,
        )
        assert result["voltage_a"] is not None
        # 初始读 + MAX_CONTINUATION_FRAMES 次续读
        expected_writes = 1 + MAX_CONTINUATION_FRAMES
        assert len(driver._write_log) == expected_writes, (
            f"应发送 {expected_writes} 帧 (1 初始 + {MAX_CONTINUATION_FRAMES} 续读), "
            f"实际 {len(driver._write_log)}"
        )

    async def test_continuation_stops_on_empty_response(self):
        """续读返回空响应 → 立即终止"""
        resp1 = _build_response_frame(
            "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=True
        )
        driver = _make_driver([resp1, b""])
        _patch_read_response(driver, [resp1, b""])

        result = await driver.read_points("dev1", ["voltage_a"])
        # 仍然返回已收集的数据
        assert result["voltage_a"] is not None
        assert len(driver._write_log) == 2  # 初始 + 1 次续读 (返回空 → break)

    async def test_continuation_stops_on_cs_failure(self):
        """续读返回 CS 校验失败的帧 → 立即终止"""
        resp1 = _build_response_frame(
            "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=True
        )
        # 篡改 CS 字节 (倒数第二字节)
        bad_resp = bytearray(
            _build_response_frame(
                "000000000001", "02010100", bytes([0x56, 0x78]), more_flag=False
            )
        )
        bad_resp[-2] = (bad_resp[-2] + 1) & 0xFF
        driver = _make_driver([resp1, bytes(bad_resp)])
        _patch_read_response(driver, [resp1, bytes(bad_resp)])

        result = await driver.read_points("dev1", ["voltage_a"])
        assert result["voltage_a"] is not None
        assert len(driver._write_log) == 2

    async def test_continuation_stops_on_parse_failure(self):
        """续读返回的帧无法解析 (值数据为空) → 立即终止"""
        resp1 = _build_response_frame(
            "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=True
        )
        # 第二帧只有 DI 无 value → _parse_response 返回 None
        resp2 = _build_response_frame(
            "000000000001", "02010100", b"", more_flag=False
        )
        driver = _make_driver([resp1, resp2])
        _patch_read_response(driver, [resp1, resp2])

        result = await driver.read_points("dev1", ["voltage_a"])
        # resp1 数据仍然返回
        assert result["voltage_a"] is not None
        assert len(driver._write_log) == 2

    async def test_seq_increments_in_continuation_frames(self):
        """续读帧的 seq 字段递增 (1, 2, 3...)"""
        resp1 = _build_response_frame(
            "000000000001", "02010100", bytes([0x12, 0x34]), more_flag=True
        )
        resp2 = _build_response_frame(
            "000000000001", "02010100", bytes([0x56, 0x78]), more_flag=True
        )
        resp3 = _build_response_frame(
            "000000000001", "02010100", bytes([0x9A, 0xBC]), more_flag=False
        )
        driver = _make_driver([resp1, resp2, resp3])
        _patch_read_response(driver, [resp1, resp2, resp3])

        await driver.read_points("dev1", ["voltage_a"])
        assert len(driver._write_log) == 3
        # 续读帧的 seq 在 data 域中: DI(4) + seq(1), seq 加了 33H
        # _build_read_next_frame: data_domain = di_bytes + seq_byte, 然后 +33H
        # seq=1 → 0x01 + 0x33 = 0x34, seq=2 → 0x02 + 0x33 = 0x35
        frame1 = driver._write_log[1]  # 第一次续读, seq=1
        frame2 = driver._write_log[2]  # 第二次续读, seq=2
        # data 域起始: frame[10], 长度 frame[9]
        # DI(4字节) + seq(1字节) = 5 字节, 第5字节 (offset 10+4=14) 是 seq+33H
        assert frame1[14] == (1 + 0x33) & 0xFF  # seq=1 → 0x34
        assert frame2[14] == (2 + 0x33) & 0xFF  # seq=2 → 0x35


# ──────────────────────────────────────────────────────────────────
# 4. 常量
# ──────────────────────────────────────────────────────────────────


class TestConstants:
    """模块常量"""

    def test_max_continuation_frames_is_reasonable(self):
        """MAX_CONTINUATION_FRAMES 应为合理上限 (10-100)"""
        assert 10 <= MAX_CONTINUATION_FRAMES <= 100
        assert MAX_CONTINUATION_FRAMES == 32

    def test_ctrl_constants(self):
        assert CTRL_READ_DATA == 0x11
        assert CTRL_READ_NEXT == 0x14


# ──────────────────────────────────────────────────────────────────
# 5. 帧构建 — 验证 _build_read_next_frame
# ──────────────────────────────────────────────────────────────────


class TestBuildReadNextFrame:
    """续读帧构建正确性"""

    def test_read_next_frame_structure(self):
        frame = Dlt645Driver._build_read_next_frame(
            "000000000001", "02010100", seq=1
        )
        assert frame[0] == FRAME_HEAD
        assert frame[7] == FRAME_HEAD
        assert frame[8] == CTRL_READ_NEXT  # 0x14
        assert frame[-1] == FRAME_TAIL
        # 长度: DI(4) + seq(1) = 5
        assert frame[9] == 5

    def test_read_next_frame_seq_byte(self):
        """seq 字节在 data 域第 5 字节 (DI 之后), 加 33H"""
        for seq in (1, 5, 10, 255):
            frame = Dlt645Driver._build_read_next_frame(
                "000000000001", "02010100", seq=seq
            )
            # data 域: frame[10..14], seq 在 frame[14] (第5字节, 加33H)
            assert frame[14] == (seq + 0x33) & 0xFF

    def test_read_next_frame_cs_valid(self):
        """续读帧 CS 校验通过"""
        frame = Dlt645Driver._build_read_next_frame(
            "000000000001", "02010100", seq=3
        )
        assert Dlt645Driver._validate_cs(frame) is True
