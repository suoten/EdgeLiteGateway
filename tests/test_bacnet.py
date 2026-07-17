"""BACnet 驱动单元测试

覆盖协议正确性修复：
- WriteProperty 服务码 = 15 (ASHRAE 135)，原社区版误用 14 (与 ReadPropertyMultiple 冲突)
"""

import asyncio
import sys
from unittest.mock import MagicMock

sys.path.insert(0, "src")

from edgelite.drivers.bacnet import (
    SERVICE_CONFIRMED_READ_PROPERTY,
    SERVICE_CONFIRMED_READ_PROPERTY_MULTIPLE,
    SERVICE_CONFIRMED_WRITE_PROPERTY,
    BACnetClient,
)


class TestBacnetServiceCodes:
    """服务码常量正确性 (ASHRAE 135 Confirmed Service Choice)"""

    def test_read_property_service_code(self):
        """readProperty == 12"""
        assert SERVICE_CONFIRMED_READ_PROPERTY == 12

    def test_write_property_service_code_is_15(self):
        """writeProperty == 15 (社区版原为 14，与 readPropertyMultiple 冲突)"""
        assert SERVICE_CONFIRMED_WRITE_PROPERTY == 15

    def test_read_property_multiple_service_code(self):
        """readPropertyMultiple == 14"""
        assert SERVICE_CONFIRMED_READ_PROPERTY_MULTIPLE == 14

    def test_no_collision_between_write_and_read_multiple(self):
        """writeProperty 与 readPropertyMultiple 不可共用同一服务码"""
        assert SERVICE_CONFIRMED_WRITE_PROPERTY != SERVICE_CONFIRMED_READ_PROPERTY_MULTIPLE


class TestBacnetWritePropertyApdu:
    """write_property 实际构造的 APDU 服务码字节验证"""

    async def test_write_property_apdu_service_choice_byte(self):
        """write_property 发出的 APDU 第 3 字节(服务码)必须为 15"""
        client = BACnetClient("127.0.0.1", timeout=0.5)
        sent: list[bytes] = []
        transport = MagicMock()
        transport.sendto = lambda data, addr: sent.append(bytes(data))
        client._transport = transport

        # 以 task 形式调度，确保协程真正运行
        task = asyncio.ensure_future(
            client.write_property(
                device_instance=100,
                object_type=2,  # Analog Value
                object_instance=1,
                property_id=85,  # presentValue
                value=42.0,
                priority=8,
            )
        )
        # 轮询等待 pending future(invoke_id=1) 注册后再回送 SimpleAck
        for _ in range(200):
            if 1 in client._pending:
                break
            await asyncio.sleep(0.001)
        client.handle_response(1, True)
        ok = await task
        assert ok is True
        assert len(sent) == 1

        bvll = sent[0]
        # BVLL: [0x81, 0x04, len_hi, len_lo] | NPDU: [0x01] | APDU: [0x00, invoke_id, service_choice, ...]
        assert bvll[0] == 0x81  # BACnet/IP version
        assert bvll[4] == 0x01  # NPDU control
        assert bvll[5] == 0x00  # APDU flags (confirmed, no seg)
        assert bvll[6] == 1  # invoke_id
        assert bvll[7] == 15  # service choice = writeProperty

    async def test_read_property_apdu_service_choice_byte(self):
        """对比: read_property 发出的服务码应为 12"""
        client = BACnetClient("127.0.0.1", timeout=0.5)
        sent: list[bytes] = []
        transport = MagicMock()
        transport.sendto = lambda data, addr: sent.append(bytes(data))
        client._transport = transport

        task = asyncio.ensure_future(client.read_property(100, 2, 1, 85))
        for _ in range(200):
            if 1 in client._pending:
                break
            await asyncio.sleep(0.001)
        client.handle_response(1, b"\x2e\x44\x42\x28\x00\x00")  # ComplexAck 数据
        await task
        assert len(sent) == 1
        bvll = sent[0]
        assert bvll[7] == 12  # service choice = readProperty


class TestBacnetApduParseWriteAck:
    """SimpleAck 解析: writeProperty 的 SimpleAck 服务码同样为 15"""

    def test_parse_simple_ack_service_15(self):
        """SimpleAck PDU 中 service_ack 字节 = 15 (writeProperty)"""
        # PDU_TYPE_SIMPLE_ACK=2 -> 0x20; invoke_id=5; service_ack=15
        apdu = bytes([0x20, 0x05, 0x0F])
        from edgelite.drivers.bacnet import _parse_apdu_response

        res = _parse_apdu_response(apdu)
        assert res["type"] == "simple_ack"
        assert res["invoke_id"] == 5
        assert res["service"] == 15
        assert res["data"] is True


# ==================== Task #11: P1 修复测试 ====================


class TestBacnetErrorPduParsing:
    """Error PDU 解析安全加固测试

    FIXED-P1: 原代码把 context tag 字节误当作值，完全错误
    - data[3] 是 error_class 的 context tag (0x81)，被误当作 class 值
    - data[4] 是 error_class 的实际值，被误当作 code 值
    - 完全忽略了 error_code 的 context tag 1 (0x89)
    """

    def test_error_pdu_parses_class_and_code(self):
        """正确解析 error_class (ctx tag 0) 和 error_code (ctx tag 1)"""
        # Error PDU: type=5, invoke_id=5, service=12(readProperty),
        # error_class=0(device) ctx-tag0=0x81, value=0x00
        # error_code=31(unknown-property) ctx-tag1=0x89, value=0x1F
        apdu = bytes([0x50, 0x05, 0x0C, 0x81, 0x00, 0x89, 0x1F])
        from edgelite.drivers.bacnet import _parse_apdu_response

        res = _parse_apdu_response(apdu)
        assert res["type"] == "error"
        assert res["invoke_id"] == 5
        assert res["service"] == 12
        assert res["error"]["class"] == 0  # device
        assert res["error"]["code"] == 31  # unknown-property

    def test_error_pdu_access_denied(self):
        """解析 access-denied 错误 (class=1 object, code=5)"""
        # error_class=1(object) ctx-tag0=0x81, value=0x01
        # error_code=5(access-denied) ctx-tag1=0x89, value=0x05
        apdu = bytes([0x50, 0x03, 0x0F, 0x81, 0x01, 0x89, 0x05])
        from edgelite.drivers.bacnet import _parse_apdu_response

        res = _parse_apdu_response(apdu)
        assert res["error"]["class"] == 1
        assert res["error"]["code"] == 5

    def test_error_pdu_short_data_returns_minus_one(self):
        """数据不足时 error_class/code 默认 -1 (不崩溃)"""
        apdu = bytes([0x50, 0x01, 0x0C])  # 只有 type+invoke_id+service，无 error 字段
        from edgelite.drivers.bacnet import _parse_apdu_response

        res = _parse_apdu_response(apdu)
        assert res["type"] == "error"
        assert res["error"]["class"] == -1
        assert res["error"]["code"] == -1

    def test_error_pdu_truncated_tag_does_not_crash(self):
        """截断的 context tag 不导致崩溃"""
        # tag 0x81 声明 length=1 但没有 value 字节
        apdu = bytes([0x50, 0x01, 0x0C, 0x81])
        from edgelite.drivers.bacnet import _parse_apdu_response

        res = _parse_apdu_response(apdu)
        assert res["type"] == "error"
        # 数据不足，class/code 保持 -1
        assert res["error"]["class"] == -1

    def test_error_pdu_multibyte_class_value(self):
        """多字节 error_class 值 (扩展长度)"""
        # error_class=300: ctx-tag0 with length=2
        # context tag 0, length 2: bit7=1,bit6=0,bits5-3=000,bits2-0=010 = 0x82
        # 300 = 0x012C
        # error_code=42: ctx-tag1, length 1 = 0x89
        apdu = bytes([0x50, 0x01, 0x0C, 0x82, 0x01, 0x2C, 0x89, 0x2A])
        from edgelite.drivers.bacnet import _parse_apdu_response

        res = _parse_apdu_response(apdu)
        assert res["error"]["class"] == 300
        assert res["error"]["code"] == 42


class TestBacnetBitStringDecoding:
    """Bit String 解码安全加固测试

    FIXED-P1: 添加 unused_bits 范围验证 (0-7) 和数据长度检查
    """

    def test_empty_bit_string(self):
        """空 bit string (length=0) 返回 0"""
        from edgelite.drivers.bacnet import _decode_application_data

        # 代码库 tag 编码: bits7-4=tag_number, bit3=class(0=app), bits2-0=length
        # application tag 8 (BIT_STRING), length=0 → 0x80
        result = _decode_application_data(0x80, b"")
        assert result == 0

    def test_single_byte_bit_string(self):
        """1 字节 bit string (8 bits, unused=0)"""
        from edgelite.drivers.bacnet import _decode_application_data

        # application tag 8, length=2 (1 byte unused_bits + 1 byte data) → 0x82
        # unused_bits=0, data=0xFF → val=0xFF
        result = _decode_application_data(0x82, b"\x00\xff")
        assert result == 0xFF

    def test_bit_string_with_unused_bits(self):
        """带 unused_bits 的 bit string (高位有效，低位丢弃)"""
        from edgelite.drivers.bacnet import _decode_application_data

        # application tag 8, length=2 → 0x82
        # unused_bits=3, data=0xFF → 0xFF >> 3 = 0x1F (5 bits)
        result = _decode_application_data(0x82, b"\x03\xff")
        assert result == 0x1F

    def test_bit_string_invalid_unused_bits_masked(self):
        """unused_bits > 7 时被 mask 到 0-7 范围 (不崩溃)"""
        from edgelite.drivers.bacnet import _decode_application_data

        # unused_bits=9 (invalid), data=0xFF
        # 9 & 0x07 = 1 → 0xFF >> 1 = 0x7F
        result = _decode_application_data(0x82, b"\x09\xff")
        assert result == 0x7F

    def test_bit_string_truncated_data(self):
        """数据截断时不崩溃 (length 声明 2 但只有 1 字节)"""
        from edgelite.drivers.bacnet import _decode_application_data

        # application tag 8, length=2 → 0x82, 但 data 只有 1 字节
        result = _decode_application_data(0x82, b"\x00")
        # unused_bits=0, bit_bytes=b"" (data[1:2] 在 1 字节输入时为空)
        assert result == 0

    def test_bit_string_only_unused_bits_byte(self):
        """只有 unused_bits 字节无数据 (length=1)"""
        from edgelite.drivers.bacnet import _decode_application_data

        # application tag 8, length=1 → 0x81 (只有 unused_bits)
        result = _decode_application_data(0x81, b"\x05")
        # bit_bytes = data[1:1] = b"" → 返回 0
        assert result == 0


class TestBacnetComplexAckInvokeId:
    """ComplexAck invoke_id 解析修正测试

    FIXED-P1: 原代码 invoke_id 取 data[2] (service_ack_choice)，应为 data[1]
    """

    def test_non_segmented_complex_ack_invoke_id(self):
        """非分段 ComplexAck 的 invoke_id 在 data[1]"""
        from edgelite.drivers.bacnet import _parse_apdu_response

        # 0x30: PDU type 3 (ComplexAck), no segmentation
        # data[1]=7 (invoke_id), data[2]=12 (service_ack=readProperty)
        apdu = bytes([0x30, 0x07, 0x0C, 0x2E, 0x44, 0x42])
        res = _parse_apdu_response(apdu)
        assert res["type"] == "complex_ack"
        assert res["invoke_id"] == 7  # 原代码会返回 12 (service_ack)
        assert res["service"] == 12
        assert res["segmented"] is False

    def test_non_segmented_complex_ack_data_offset(self):
        """非分段 ComplexAck 的 data 从 data[3] 开始 (原代码从 data[4] 开始)"""
        from edgelite.drivers.bacnet import _parse_apdu_response

        apdu = bytes([0x30, 0x07, 0x0C, 0xAA, 0xBB])
        res = _parse_apdu_response(apdu)
        assert res["data"] == b"\xaa\xbb"  # 原代码会返回 b"\xBB" (少 1 字节)


class TestBacnetSegmentationFlags:
    """分段标志位解析测试

    FIXED-P1: 原代码完全忽略 segmented/more_follows 标志位
    """

    def test_non_segmented_flags_false(self):
        """非分段 PDU 的 segmented/more_follows 为 False"""
        from edgelite.drivers.bacnet import _parse_apdu_response

        apdu = bytes([0x30, 0x07, 0x0C])
        res = _parse_apdu_response(apdu)
        assert res["segmented"] is False
        assert res["more_follows"] is False

    def test_segmented_first_segment_flags(self):
        """分段首段: segmented=True, more_follows=True"""
        from edgelite.drivers.bacnet import _parse_apdu_response

        # 0x3C = 0x30 | 0x08(segmented) | 0x04(more_follows)
        # seq=0, window=5, invoke_id=7, service=12
        apdu = bytes([0x3C, 0x00, 0x05, 0x07, 0x0C, 0xAA])
        res = _parse_apdu_response(apdu)
        assert res["segmented"] is True
        assert res["more_follows"] is True
        assert res["sequence_number"] == 0
        assert res["window_size"] == 5
        assert res["invoke_id"] == 7
        assert res["service"] == 12

    def test_segmented_last_segment_flags(self):
        """分段末段: segmented=True, more_follows=False"""
        from edgelite.drivers.bacnet import _parse_apdu_response

        # 0x38 = 0x30 | 0x08(segmented), no more_follows
        # seq=1, window=5, invoke_id=7 (后续段无 service choice)
        apdu = bytes([0x38, 0x01, 0x05, 0x07, 0xBB])
        res = _parse_apdu_response(apdu)
        assert res["segmented"] is True
        assert res["more_follows"] is False
        assert res["sequence_number"] == 1
        assert res["invoke_id"] == 7
        # 后续段 (seq>0) 无 service choice
        assert res["service"] is None

    def test_segmented_first_segment_data_offset(self):
        """分段首段 data 从 data[5] 开始 (跳过 flags/seq/window/invoke_id/service)"""
        from edgelite.drivers.bacnet import _parse_apdu_response

        apdu = bytes([0x3C, 0x00, 0x05, 0x07, 0x0C, 0xAA, 0xBB])
        res = _parse_apdu_response(apdu)
        assert res["data"] == b"\xaa\xbb"

    def test_segmented_subsequent_segment_data_offset(self):
        """分段后续段 data 从 data[4] 开始 (无 service choice)"""
        from edgelite.drivers.bacnet import _parse_apdu_response

        apdu = bytes([0x38, 0x01, 0x05, 0x07, 0xCC, 0xDD])
        res = _parse_apdu_response(apdu)
        assert res["data"] == b"\xcc\xdd"


class TestSegmentReassembler:
    """分段重组器测试"""

    def test_two_segment_reassembly(self):
        """两段重组: 首段(more=True) + 末段(more=False)"""
        from edgelite.drivers.bacnet import _SegmentReassembler

        r = _SegmentReassembler()
        # 第一段
        result = r.add_segment(invoke_id=1, sequence_number=0, more_follows=True, service=12, data=b"\xaa\xbb")
        assert result is None  # 等待更多分段
        # 最后一段
        result = r.add_segment(invoke_id=1, sequence_number=1, more_follows=False, service=None, data=b"\xcc\xdd")
        assert result is not None
        assert result["data"] == b"\xaa\xbb\xcc\xdd"
        assert result["service"] == 12

    def test_three_segment_reassembly(self):
        """三段重组"""
        from edgelite.drivers.bacnet import _SegmentReassembler

        r = _SegmentReassembler()
        r.add_segment(1, 0, True, 12, b"\x01")
        r.add_segment(1, 1, True, None, b"\x02")
        result = r.add_segment(1, 2, False, None, b"\x03")
        assert result is not None
        assert result["data"] == b"\x01\x02\x03"
        assert result["service"] == 12

    def test_single_segment_reassembly(self):
        """单段重组 (segmented=True 但 more_follows=False)"""
        from edgelite.drivers.bacnet import _SegmentReassembler

        r = _SegmentReassembler()
        result = r.add_segment(1, 0, False, 12, b"\x42")
        assert result is not None
        assert result["data"] == b"\x42"
        assert result["service"] == 12

    def test_duplicate_segment_handled(self):
        """重复分段不导致数据重复"""
        from edgelite.drivers.bacnet import _SegmentReassembler

        r = _SegmentReassembler()
        r.add_segment(1, 0, True, 12, b"\xaa")
        # 重复发送第一段
        r.add_segment(1, 0, True, 12, b"\xaa")
        result = r.add_segment(1, 1, False, None, b"\xbb")
        assert result["data"] == b"\xaa\xbb"  # 不是 \xAA\xAA\xBB

    def test_cancel_clears_buffer(self):
        """cancel 清理指定 invoke_id 的缓冲区"""
        from edgelite.drivers.bacnet import _SegmentReassembler

        r = _SegmentReassembler()
        r.add_segment(1, 0, True, 12, b"\xaa")
        assert r.get_pending_count() == 1
        r.cancel(1)
        assert r.get_pending_count() == 0
        # 取消后收到末段不会返回重组结果
        result = r.add_segment(1, 1, False, None, b"\xbb")
        assert result is not None  # 新缓冲区，只有一段(末段)
        assert result["data"] == b"\xbb"

    def test_max_segments_overflow(self):
        """超过最大分段数时丢弃缓冲区"""
        from edgelite.drivers.bacnet import _SegmentReassembler

        r = _SegmentReassembler(max_segments=3)
        r.add_segment(1, 0, True, 12, b"\x01")
        r.add_segment(1, 1, True, None, b"\x02")
        r.add_segment(1, 2, True, None, b"\x03")
        # 第 4 段超过上限 (max=3)
        result = r.add_segment(1, 3, True, None, b"\x04")
        assert result is None  # 缓冲区被丢弃
        assert r.get_pending_count() == 0

    def test_different_invoke_ids_independent(self):
        """不同 invoke_id 的分段缓冲互不干扰"""
        from edgelite.drivers.bacnet import _SegmentReassembler

        r = _SegmentReassembler()
        r.add_segment(1, 0, True, 12, b"\xaa")
        r.add_segment(2, 0, True, 14, b"\xbb")
        assert r.get_pending_count() == 2
        # 完成 invoke_id=1
        result1 = r.add_segment(1, 1, False, None, b"\xcc")
        assert result1["data"] == b"\xaa\xcc"
        # invoke_id=2 仍在缓冲
        assert r.get_pending_count() == 1
