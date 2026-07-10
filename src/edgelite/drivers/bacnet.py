"""BACnet/IP 驱动 - 基于BACpypes库实现楼宇自控协议

BACnet是楼宇自动化和控制领域的标准协议（ASHRAE 135）。
支持：
- BACnet/IP over UDP (默认端口47808)
- 设备发现 (Who-Is/I-Am)
- 读写模拟值 (Analog Value)、数字值 (Binary Value)、多状态值
- 属性读写 (ReadProperty/WriteProperty)
- COV订阅 (Change of Value) - 支持实时变化通知
- 批量读取优化 - ReadPropertyMultiple减少通信开销
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

# BACnet/IP 默认配置
DEFAULT_BACNET_PORT = 47808
BROADCAST_ADDRESS = "255.255.255.255"

# BACnet 功能码
PRIORITY_ARRAY_DEFAULT = 0
SERVICE_CONFIRMED_READ_PROPERTY = 12
SERVICE_CONFIRMED_WRITE_PROPERTY = 15  # WriteProperty (ASHRAE 135)
SERVICE_CONFIRMED_READ_PROPERTY_MULTIPLE = 14  # ReadPropertyMultiple
SERVICE_UNCONFIRMED_WHO_IS = 8
SERVICE_UNCONFIRMED_I_AM = 0
SERVICE_CONFIRMED_SUBSCRIBE_COV = 5
SERVICE_UNCONFIRMED_COV_NOTIFICATION = 2

# BACnet APDU 类型
PDU_TYPE_CONFIRMED_REQUEST = 0
PDU_TYPE_UNCONFIRMED_REQUEST = 1
PDU_TYPE_SIMPLE_ACK = 2
PDU_TYPE_COMPLEX_ACK = 3
PDU_TYPE_ERROR = 5
PDU_TYPE_REJECT = 4

# BACnet 对象类型
OBJECT_TYPE_ANALOG_INPUT = 0
OBJECT_TYPE_ANALOG_OUTPUT = 1
OBJECT_TYPE_ANALOG_VALUE = 2
OBJECT_TYPE_BINARY_INPUT = 3
OBJECT_TYPE_BINARY_OUTPUT = 4
OBJECT_TYPE_BINARY_VALUE = 5
OBJECT_TYPE_MULTI_STATE_INPUT = 13
OBJECT_TYPE_MULTI_STATE_OUTPUT = 14
OBJECT_TYPE_MULTI_STATE_VALUE = 19
OBJECT_TYPE_DEVICE = 8

# BACnet 应用层数据类型标签
APPLICATION_TAG_NULL = 0
APPLICATION_TAG_BOOLEAN = 1
APPLICATION_TAG_UNSIGNED = 2
APPLICATION_TAG_INTEGER = 3
APPLICATION_TAG_REAL = 4
APPLICATION_TAG_DOUBLE = 5
APPLICATION_TAG_OCTET_STRING = 6
APPLICATION_TAG_CHARACTER_STRING = 7
APPLICATION_TAG_BIT_STRING = 8
APPLICATION_TAG_ENUMERATED = 9
APPLICATION_TAG_DATE = 10
APPLICATION_TAG_TIME = 11
APPLICATION_TAG_OBJECT_IDENTIFIER = 12

# BACnet BVLC 功能码
BVLC_ORIGINAL_UNICAST_NPDU = 0x04
BVLC_ORIGINAL_BROADCAST_NPDU = 0x0B

# BACnet 常用属性ID
PROP_OBJECT_IDENTIFIER = 75
PROP_OBJECT_NAME = 77
PROP_OBJECT_TYPE = 79
PROP_PRESENT_VALUE = 85
PROP_DESCRIPTION = 28
PROP_STATUS_FLAGS = 111
PROP_UNITS = 117
PROP_DEVICE_TYPE = 103
PROP_VENDOR_NAME = 121
PROP_VENDOR_IDENTIFIER = 120
PROP_MODEL_NAME = 70
PROP_FIRMWARE_REVISION = 44
PROP_APPLICATION_SOFTWARE_VERSION = 12
PROP_PROTOCOL_VERSION = 98
PROP_PROTOCOL_REVISION = 139
PROP_MAX_APDU_LENGTH_ACCEPTED = 62
PROP_SEGMENTATION_SUPPORTED = 107
PROP_OBJECT_LIST = 76
PROP_RELIABILITY = 103
PROP_OUT_OF_SERVICE = 81
PROP_PRIORITY_ARRAY = 87
PROP_RELINQUISH_DEFAULT = 104
PROP_COV_INCREMENT = 22
PROP_MINIMUM_VALUE = 69
PROP_MAXIMUM_VALUE = 65
PROP_RESOLUTION = 96
PROP_STATE_TEXT = 110

# BACnet 错误码
ERROR_CLASS_DEVICE = 0
ERROR_CLASS_OBJECT = 1
ERROR_CLASS_PROPERTY = 2
ERROR_CLASS_RESOURCES = 3
ERROR_CLASS_SECURITY = 4
ERROR_CLASS_SERVICES = 5
ERROR_CLASS_VT = 6
ERROR_CLASS_COMMUNICATION = 7

ERROR_CODE_UNKNOWN_OBJECT = 31
ERROR_CODE_UNKNOWN_PROPERTY = 32
ERROR_CODE_READ_ACCESS_DENIED = 37
ERROR_CODE_WRITE_ACCESS_DENIED = 39
ERROR_CODE_VALUE_OUT_OF_RANGE = 42
ERROR_CODE_NOT_COV_SUBSCRIBABLE = 45


def _decode_application_data(tag_byte: int, data: bytes) -> Any:
    """解码BACnet应用层数据

    Args:
        tag_byte: TLV标签字节 (高4位=tag number, 低4位=tag class+length)
        data: 标签后的数据字节

    Returns:
        解码后的Python值
    """
    tag_number = (tag_byte >> 4) & 0x0F
    tag_class = (tag_byte >> 3) & 0x01
    length = tag_byte & 0x07

    # 上下文标签(1)不在此处解码，返回原始数据
    if tag_class == 1:
        return data

    if tag_number == APPLICATION_TAG_NULL:
        return None
    elif tag_number == APPLICATION_TAG_BOOLEAN:
        # Boolean特殊: length字段即为值(0或1)
        return length != 0
    elif tag_number == APPLICATION_TAG_UNSIGNED:
        if length == 0:
            return 0
        elif length == 1:
            return data[0]
        elif length == 2:
            return struct.unpack(">H", data[:2])[0]
        elif length == 3:
            return int.from_bytes(data[:3], "big", signed=False)
        elif length == 4:
            return struct.unpack(">I", data[:4])[0]
        else:
            return int.from_bytes(data[:length], "big", signed=False)
    elif tag_number == APPLICATION_TAG_INTEGER:
        if length == 0:
            return 0
        elif length == 1:
            return struct.unpack(">b", data[:1])[0]
        elif length == 2:
            return struct.unpack(">h", data[:2])[0]
        elif length == 3:
            return int.from_bytes(data[:3], "big", signed=True)
        elif length == 4:
            return struct.unpack(">i", data[:4])[0]
        else:
            return int.from_bytes(data[:length], "big", signed=True)
    elif tag_number == APPLICATION_TAG_REAL:
        if len(data) >= 4:
            return struct.unpack(">f", data[:4])[0]
    elif tag_number == APPLICATION_TAG_DOUBLE:
        if len(data) >= 8:
            return struct.unpack(">d", data[:8])[0]
    elif tag_number == APPLICATION_TAG_OCTET_STRING:
        return data[:length]
    elif tag_number == APPLICATION_TAG_CHARACTER_STRING:
        if length > 0 and len(data) > 0:
            encoding = data[0]
            if encoding == 0:  # ANSI X3.4
                return data[1:length].decode("ascii", errors="replace")
            elif encoding == 1:  # UCS-2
                return data[1:length].decode("utf-16-be", errors="replace")
            elif encoding == 2:  # UCS-4
                return data[1:length].decode("utf-32-be", errors="replace")
            else:
                return data[1:length].decode("utf-8", errors="replace")
        return ""
    elif tag_number == APPLICATION_TAG_BIT_STRING:
        if length > 0 and len(data) > 0:
            # unused_bits 必须在 0-7 范围内，无效值用掩码截断到合法范围
            unused_bits = data[0] & 0x07
            bit_bytes = data[1:length]
            val = int.from_bytes(bit_bytes, "big", signed=False)
            if unused_bits > 0:
                val >>= unused_bits
            return val
        return 0
    elif tag_number == APPLICATION_TAG_ENUMERATED:
        if length == 0:
            return 0
        elif length == 1:
            return data[0]
        elif length == 2:
            return struct.unpack(">H", data[:2])[0]
        elif length == 4:
            return struct.unpack(">I", data[:4])[0]
        else:
            return int.from_bytes(data[:length], "big", signed=False)
    elif tag_number == APPLICATION_TAG_DATE:
        if length >= 4:
            year = data[0]
            month = data[1]
            day = data[2]
            weekday = data[3]
            # year=255表示未指定, month=255表示未指定
            year_val = year + 1900 if year < 255 else None
            return {
                "year": year_val,
                "month": month if month < 255 else None,
                "day": day if day < 255 else None,
                "weekday": weekday if weekday < 255 else None,
            }
    elif tag_number == APPLICATION_TAG_TIME:
        if length >= 4:
            hour = data[0]
            minute = data[1]
            second = data[2]
            hundredths = data[3]
            return {
                "hour": hour if hour < 255 else None,
                "minute": minute if minute < 255 else None,
                "second": second if second < 255 else None,
                "hundredths": hundredths if hundredths < 255 else None,
            }
    elif tag_number == APPLICATION_TAG_OBJECT_IDENTIFIER:
        if length == 4 and len(data) >= 4:
            obj_id = struct.unpack(">I", data[:4])[0]
            obj_type = (obj_id >> 22) & 0x3FF
            obj_inst = obj_id & 0x3FFFFF
            return {"type": obj_type, "instance": obj_inst}

    return data[:length] if length > 0 else data


def _parse_tagged_unsigned(data: bytes, offset: int) -> tuple[int | None, int]:
    """解析标签化的无符号整数值

    跳过标签字节，根据长度字段提取值。
    用于 Error PDU 中 error_class 和 error_code 的顺序解析。

    标签字节格式 (BACnet TLV):
    - bits7-4: 标签号
    - bit3: 标签类别 (0=应用, 1=上下文)
    - bits2-0: 长度 (0-4=直接长度, 5=1字节扩展, 6/7=扩展或开/闭标签)

    Args:
        data: 数据字节
        offset: 起始偏移

    Returns:
        (值, 下一个偏移) - 值为 None 表示解析失败 (数据不足或非值标签)
    """
    if offset >= len(data):
        return None, offset

    tag_byte = data[offset]
    length = tag_byte & 0x07
    tag_class = (tag_byte >> 3) & 0x01

    # 上下文标签的 length=6/7 是 opening/closing tag，不是值标签
    if tag_class == 1 and length in (6, 7):
        return None, offset

    offset += 1

    # 扩展长度编码 (5=1字节, 6=2字节[仅应用标签], 7=4字节[仅应用标签])
    if length == 5:
        if offset >= len(data):
            return None, offset
        length = data[offset]
        offset += 1
    elif length == 6:
        if offset + 1 >= len(data):
            return None, offset
        length = struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2
    elif length == 7:
        if offset + 3 >= len(data):
            return None, offset
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4

    # length=0 表示值为 0
    if length == 0:
        return 0, offset

    # 检查值数据是否足够
    if offset + length > len(data):
        return None, offset

    value = int.from_bytes(data[offset : offset + length], "big", signed=False)
    return value, offset + length


def _parse_apdu_response(data: bytes) -> dict[str, Any]:
    """解析BACnet APDU响应

    Returns:
        解析结果字典，包含 type/invoke_id/service/data 等字段
        complex_ack 类型还包含 segmented/more_follows/sequence_number/window_size
    """
    result: dict[str, Any] = {
        "type": None,
        "invoke_id": None,
        "data": None,
        "error": None,
        "segmented": False,
        "more_follows": False,
    }

    if len(data) < 2:
        return result

    pdu_type = (data[0] >> 4) & 0x0F

    if pdu_type == PDU_TYPE_CONFIRMED_REQUEST:
        # Confirmed Request:
        # 非分段: [flags, invoke_id, service_choice, service_request...]
        # 分段:   [flags, seq, window, invoke_id, service_choice, service_request...]
        result["type"] = "confirmed_request"
        segmented = bool(data[0] & 0x08)
        more_follows = bool(data[0] & 0x04)
        result["segmented"] = segmented
        result["more_follows"] = more_follows
        if len(data) >= 2:
            result["invoke_id"] = data[1]
            if segmented:
                if len(data) >= 4:
                    result["sequence_number"] = data[2]
                    result["window_size"] = data[3]
                    if len(data) >= 5:
                        result["service"] = data[4]
                        result["data"] = data[5:]
            else:
                if len(data) >= 3:
                    result["service"] = data[2]
                    result["data"] = data[3:]
    elif pdu_type == PDU_TYPE_UNCONFIRMED_REQUEST:
        # Unconfirmed Request: service_choice
        result["type"] = "unconfirmed_request"
        if len(data) >= 2:
            result["service"] = data[1]
            result["data"] = data[2:]
    elif pdu_type == PDU_TYPE_SIMPLE_ACK:
        # SimpleAck: invoke_id | service_ack
        result["type"] = "simple_ack"
        if len(data) >= 3:
            result["invoke_id"] = data[1]
            result["service"] = data[2]
            result["data"] = True
    elif pdu_type == PDU_TYPE_COMPLEX_ACK:
        # ComplexAck:
        # 非分段: [flags, invoke_id, service_ack, data...]
        # 分段首段: [flags, seq, window, invoke_id, service_ack, data...]
        # 分段后续: [flags, seq, window, invoke_id, data...] (无 service_ack)
        result["type"] = "complex_ack"
        segmented = bool(data[0] & 0x08)
        more_follows = bool(data[0] & 0x04)
        result["segmented"] = segmented
        result["more_follows"] = more_follows
        if not segmented:
            # 非分段: invoke_id 在 data[1], service 在 data[2], data 从 data[3] 开始
            if len(data) >= 2:
                result["invoke_id"] = data[1]
            if len(data) >= 3:
                result["service"] = data[2]
                result["data"] = data[3:]
        else:
            # 分段: seq 在 data[1], window 在 data[2], invoke_id 在 data[3]
            if len(data) >= 4:
                result["sequence_number"] = data[1]
                result["window_size"] = data[2]
                result["invoke_id"] = data[3]
                if data[1] == 0:
                    # 首段: service 在 data[4], data 从 data[5] 开始
                    if len(data) >= 5:
                        result["service"] = data[4]
                        result["data"] = data[5:]
                else:
                    # 后续段: 无 service, data 从 data[4] 开始
                    result["service"] = None
                    result["data"] = data[4:]
    elif pdu_type == PDU_TYPE_ERROR:
        # Error: [type, invoke_id, service, error_class(ctx tag 0), error_code(ctx tag 1)]
        result["type"] = "error"
        if len(data) >= 3:
            result["invoke_id"] = data[1]
            result["service"] = data[2]
            # 顺序解析 error_class 和 error_code (各自带标签字节)
            error_class, next_off = _parse_tagged_unsigned(data, 3)
            if error_class is None:
                result["error"] = {"class": -1, "code": -1}
            else:
                error_code, _ = _parse_tagged_unsigned(data, next_off)
                result["error"] = {
                    "class": error_class,
                    "code": error_code if error_code is not None else -1,
                }
        else:
            result["error"] = {"class": -1, "code": -1}
    elif pdu_type == PDU_TYPE_REJECT:
        result["type"] = "reject"
        if len(data) >= 3:
            result["invoke_id"] = data[1]
            result["error"] = {"reason": data[2]}

    return result


def _parse_i_am(data: bytes, source_addr: tuple) -> dict | None:
    """解析I-Am响应数据

    Args:
        data: I-Am APDU数据(不含BVLL/NPDU头)
        source_addr: 源地址 (ip, port)

    Returns:
        设备信息字典或None
    """
    try:
        # I-Am格式: service(0) | ObjectIdentifier | MaxAPDULength | Segmentation | VendorID
        offset = 0
        if len(data) < 1 or data[0] != SERVICE_UNCONFIRMED_I_AM:
            return None
        offset = 1

        # Object Identifier (context tag 0, 4 bytes)
        if offset + 5 > len(data):
            return None
        if data[offset] != 0x0C:  # context tag 0, length 4
            return None
        offset += 1
        obj_id = struct.unpack(">I", data[offset : offset + 4])[0]
        device_instance = obj_id & 0x3FFFFF
        offset += 4

        # Max APDU Length (context tag 1)
        if offset >= len(data):
            return None
        max_apdu_tag = data[offset]
        max_apdu_len = max_apdu_tag & 0x07
        offset += 1
        if offset + max_apdu_len > len(data):
            return None
        offset += max_apdu_len

        # Segmentation Supported (context tag 2, enum)
        if offset >= len(data):
            return None
        seg_tag = data[offset]
        seg_len = seg_tag & 0x07
        offset += 1
        if offset + seg_len > len(data):
            return None
        offset += seg_len

        # Vendor ID (context tag 3, unsigned)
        vendor_id = 0
        if offset < len(data):
            vendor_tag = data[offset]
            vendor_len = vendor_tag & 0x07
            offset += 1
            if offset + vendor_len <= len(data):
                if vendor_len == 1:
                    vendor_id = data[offset]
                elif vendor_len == 2:
                    vendor_id = struct.unpack(">H", data[offset : offset + 2])[0]

        return {
            "device_id": device_instance,
            "instance": device_instance,
            "address": source_addr[0],
            "port": source_addr[1],
            "vendor_id": vendor_id,
        }
    except Exception as e:
        logger.debug("BACnet I-Am解析异常: %s", e)
        return None


def _parse_read_property_response(data: bytes) -> Any:
    """解析ReadProperty的ComplexAck响应，提取属性值"""
    try:
        offset = 0
        # 跳过opening tag for Object Identifier (context tag 0)
        if offset < len(data) and data[offset] == 0x0C:
            offset += 5  # tag + 4 bytes object id

        # 跳过opening tag for Property Identifier (context tag 1)
        if offset < len(data) and data[offset] == 0x19:
            offset += 2  # tag + property id

        # 查找Property Value opening tag (context tag 2)
        while offset < len(data):
            if data[offset] == 0x2E:  # opening tag 2
                offset += 1
                break
            offset += 1

        if offset >= len(data):
            return data

        # 解析应用层数据标签
        tag_byte = data[offset]
        (tag_byte >> 4) & 0x0F
        tag_class = (tag_byte >> 3) & 0x01
        length = tag_byte & 0x07

        if tag_class == 0:  # Application tag
            offset += 1
            value_data = data[offset : offset + length]
            return _decode_application_data(tag_byte, value_data)
        else:
            # Context-tagged data - 尝试提取原始数据
            offset += 1
            return data[offset : offset + length] if length > 0 else data[offset:]

    except Exception as e:
        logger.debug("BACnet ReadProperty响应解析异常: %s", e)
        return data


def _parse_cov_notification(data: bytes) -> dict | None:
    """解析COV通知

    Returns:
        {"process_id": int, "object": {...}, "values": {prop_id: value}}
    """
    try:
        offset = 0
        result: dict[str, Any] = {"process_id": 0, "object": None, "values": {}}

        # Subscriber Process Identifier (context tag 0)
        if offset < len(data) and data[offset] == 0x09:
            offset += 1
            result["process_id"] = data[offset]
            offset += 1

        # Initiating Device Identifier (context tag 1)
        if offset < len(data) and data[offset] == 0x19:
            offset += 2  # skip tag + length byte
        elif offset < len(data) and data[offset] == 0x1C:
            offset += 5  # tag + 4 bytes

        # Monitored Object Identifier (context tag 2)
        if offset < len(data) and data[offset] == 0x2C:
            offset += 1
            obj_id = struct.unpack(">I", data[offset : offset + 4])[0]
            obj_type = (obj_id >> 22) & 0x3FF
            obj_inst = obj_id & 0x3FFFFF
            result["object"] = {"type": obj_type, "instance": obj_inst}
            offset += 4

        # Time of notification (context tag 3) - optional, skip
        if offset < len(data) and data[offset] == 0x39:
            offset += 1
            tag_len = data[offset] & 0x07 if offset < len(data) else 0
            offset += 1 + tag_len

        # List of Values (context tag 4 opening)
        if offset < len(data) and data[offset] == 0x4E:
            offset += 1
            # 解析属性值列表
            while offset < len(data):
                if data[offset] == 0x4F:  # closing tag 4
                    break
                # Property Identifier (context tag 0)
                if data[offset] == 0x09:
                    prop_id = data[offset + 1]
                    offset += 2
                    # Property Value opening tag (context tag 1)
                    if offset < len(data) and data[offset] == 0x1E:
                        offset += 1
                        # 应用数据标签
                        if offset < len(data):
                            tag_byte = data[offset]
                            tag_len = tag_byte & 0x07
                            tag_class = (tag_byte >> 3) & 0x01
                            if tag_class == 0:
                                offset += 1
                                value_data = data[offset : offset + tag_len]
                                result["values"][prop_id] = _decode_application_data(tag_byte, value_data)
                                offset += tag_len
                            else:
                                offset += 1 + tag_len
                        # closing tag 1
                        if offset < len(data) and data[offset] == 0x1F:
                            offset += 1
                    continue
                offset += 1

        return result
    except Exception as e:
        logger.debug("BACnet COV通知解析异常: %s", e)
        return None


class _SegmentReassembler:
    """BACnet 分段响应重组器

    负责将多个分段的 ComplexAck 响应重新组装成完整数据。
    每个分段通过 invoke_id 标识所属请求，sequence_number 标识分段顺序。
    首段 (seq=0) 携带 service choice，后续段不携带。

    线程安全性: 本类非线程安全，需在单线程事件循环中使用。
    """

    def __init__(self, max_segments: int = 64):
        """初始化重组器

        Args:
            max_segments: 单个 invoke_id 允许的最大分段数，超过则丢弃缓冲区
        """
        self._max_segments = max_segments
        # invoke_id -> {"service": int|None, "segments": {seq: bytes}}
        self._buffers: dict[int, dict[str, Any]] = {}

    def add_segment(
        self,
        invoke_id: int,
        sequence_number: int,
        more_follows: bool,
        service: int | None,
        data: bytes,
    ) -> dict | None:
        """添加一个分段到重组缓冲区

        Args:
            invoke_id: BACnet invoke ID，标识所属请求
            sequence_number: 分段序号 (0=首段)
            more_follows: 是否还有后续分段
            service: 服务码 (仅首段有，后续段为 None)
            data: 本分段的数据载荷

        Returns:
            重组完成时返回 {"data": bytes, "service": int|None}，
            未完成或缓冲区被丢弃时返回 None
        """
        # 获取或创建缓冲区
        if invoke_id not in self._buffers:
            self._buffers[invoke_id] = {
                "service": None,
                "segments": {},
            }

        buf = self._buffers[invoke_id]

        # 首段 (seq=0) 设置 service
        if sequence_number == 0 and service is not None:
            buf["service"] = service

        # 重复分段检查: 已存在的序号不重复添加
        if sequence_number not in buf["segments"]:
            buf["segments"][sequence_number] = data

        # 检查是否超过最大分段数
        if len(buf["segments"]) > self._max_segments:
            # 超过上限，丢弃缓冲区防止内存泄漏
            del self._buffers[invoke_id]
            return None

        # 如果没有后续分段，完成重组
        if not more_follows:
            # 按序号排序拼接所有分段数据
            ordered_data = b"".join(buf["segments"][seq] for seq in sorted(buf["segments"].keys()))
            result = {
                "data": ordered_data,
                "service": buf["service"],
            }
            # 清理已完成的缓冲区
            del self._buffers[invoke_id]
            return result

        return None

    def cancel(self, invoke_id: int) -> None:
        """取消指定 invoke_id 的重组缓冲区

        用于响应超时或放弃等待时清理不完整的分段缓冲区。

        Args:
            invoke_id: 要取消的 BACnet invoke ID
        """
        self._buffers.pop(invoke_id, None)

    def get_pending_count(self) -> int:
        """返回当前待重组的 invoke_id 数量

        Returns:
            正在等待更多分段的 invoke_id 数量
        """
        return len(self._buffers)


class BACnetClient:
    """BACnet/IP 客户端封装"""

    def __init__(self, host: str, port: int = DEFAULT_BACNET_PORT, timeout: float = 5.0):
        self._host = host
        self._port = port
        self._timeout = timeout
        self._reader: asyncio.DatagramProtocol | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_invoke_id = 1
        self._lock = asyncio.Lock()
        self._bvlc_port = port
        # Who-Is设备发现收集器
        self._discovered_devices: list[dict] = []
        self._discovery_event: asyncio.Event | None = None
        # COV通知回调
        self._cov_callback: Callable | None = None

    async def connect(self) -> None:
        """建立UDP连接"""
        loop = asyncio.get_running_loop()
        self._reader, self._transport = await loop.create_datagram_endpoint(
            lambda: _BACnetProtocol(self),
            remote_addr=(self._host, self._bvlc_port),
        )

    def close(self) -> None:
        """关闭连接"""
        if self._transport:
            self._transport.close()
            self._transport = None
        # 清理所有pending future
        for _invoke_id, future in self._pending.items():
            if not future.done():
                future.cancel()
        self._pending.clear()

    def set_cov_callback(self, callback: Callable) -> None:
        """设置COV通知回调函数

        Args:
            callback: 回调函数，签名为 callback(process_id, object_info, values)
        """
        self._cov_callback = callback

    async def read_property(
        self,
        device_instance: int,
        object_type: int,
        object_instance: int,
        property_id: int,
    ) -> Any | None:
        """读取BACnet属性

        Args:
            device_instance: 设备实例号
            object_type: 对象类型 (如 OBJECT_TYPE_ANALOG_VALUE)
            object_instance: 对象实例号
            property_id: 属性ID (85=presentValue, 28=description)

        Returns:
            读取的属性值，失败返回None
        """
        invoke_id = await self._get_invoke_id()

        # 构造NPDU
        npdu = bytes([0x01])  # NPDU control: data expecting reply

        # 构造APDU - Confirmed Service Request
        # PDU type=0 (confirmed), SEG=0, MOR=0, SA=0
        apdu = bytes([0x00])  # APDU flags
        apdu += bytes([invoke_id])  # invoke ID
        apdu += bytes([SERVICE_CONFIRMED_READ_PROPERTY])  # service choice

        # ReadProperty 服务参数
        # Object Identifier (context tag 0, length 4)
        apdu += bytes([0x0C])  # context tag 0, length 4
        apdu += struct.pack(">I", (object_type << 22) | object_instance)

        # Property Identifier (context tag 1, length 1)
        apdu += bytes([0x19])  # context tag 1, length 1
        apdu += bytes([property_id])

        # 封装BVLL
        bvll = self._build_bvll(BVLC_ORIGINAL_UNICAST_NPDU, npdu + apdu)

        # 发送并等待响应
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[invoke_id] = future

        try:
            if self._transport:
                self._transport.sendto(bvll, (self._host, self._bvlc_port))
            else:
                return None

            result = await asyncio.wait_for(future, timeout=self._timeout)
            return result
        except TimeoutError:
            logger.warning(
                "BACnet读取超时: device=%d, object=%d/%d, property=%d",
                device_instance,
                object_type,
                object_instance,
                property_id,
            )
            return None
        except Exception as e:
            logger.error("BACnet读取失败: %s", e)
            return None
        finally:
            self._pending.pop(invoke_id, None)

    async def write_property(
        self,
        device_instance: int,
        object_type: int,
        object_instance: int,
        property_id: int,
        value: Any,
        priority: int = 8,
    ) -> bool:
        """写入BACnet属性

        Returns:
            写入是否成功（等待设备确认）
        """
        invoke_id = await self._get_invoke_id()

        # 构造NPDU
        npdu = bytes([0x01])  # NPDU control: data expecting reply

        # 构造APDU - Confirmed Service Request
        apdu = bytes([0x00])  # APDU flags
        apdu += bytes([invoke_id])
        apdu += bytes([SERVICE_CONFIRMED_WRITE_PROPERTY])

        # Object Identifier (context tag 0, length 4)
        apdu += bytes([0x0C])
        apdu += struct.pack(">I", (object_type << 22) | object_instance)

        # Property Identifier (context tag 1, length 1)
        apdu += bytes([0x19])
        apdu += bytes([property_id])

        # Property Value (context tag 2 opening)
        apdu += bytes([0x3E])  # opening tag 2

        # 编码值 - 根据类型使用正确的应用标签
        if isinstance(value, bool):
            # Boolean: tag_number=1, length字段=值(0或1)
            apdu += bytes([0x11 if value else 0x10])
        elif isinstance(value, float):
            apdu += bytes([0x44])  # application tag real, length=4
            apdu += struct.pack(">f", value)
        elif isinstance(value, int):
            if value >= 0:
                # Unsigned Integer
                if value < 0x100:
                    apdu += bytes([0x21, value])  # tag=2, len=1
                elif value < 0x10000:
                    apdu += bytes([0x22])  # tag=2, len=2
                    apdu += struct.pack(">H", value)
                elif value < 0x100000000:
                    apdu += bytes([0x24])  # tag=2, len=4
                    apdu += struct.pack(">I", value)
                else:
                    # 超大值用8字节
                    apdu += bytes([0x28])
                    apdu += struct.pack(">Q", value)
            else:
                # Signed Integer
                if -0x80 <= value < 0x80:
                    apdu += bytes([0x31, value & 0xFF])  # tag=3, len=1
                elif -0x8000 <= value < 0x8000:
                    apdu += bytes([0x32])
                    apdu += struct.pack(">h", value)
                elif -0x80000000 <= value < 0x80000000:
                    apdu += bytes([0x34])
                    apdu += struct.pack(">i", value)
                else:
                    apdu += bytes([0x38])
                    apdu += struct.pack(">q", value)
        elif isinstance(value, str):
            # Character String
            encoded = value.encode("utf-8")
            header_len = 1  # encoding byte
            total_len = header_len + len(encoded)
            if total_len < 0x05:
                apdu += bytes([0x70 + total_len])  # tag=7, len=total_len
            elif total_len < 0x100:
                apdu += bytes([0x75, total_len])  # tag=7, extended len
            else:
                apdu += bytes([0x75, 0xFE])  # tag=7, 2-byte extended len
                apdu += struct.pack(">H", total_len)
            apdu += bytes([0x00])  # ANSI X3.4 encoding
            apdu += encoded
        elif isinstance(value, bytes):
            # Octet String
            if len(value) < 5:
                apdu += bytes([0x60 + len(value)])  # tag=6, len
            elif len(value) < 0x100:
                apdu += bytes([0x65, len(value)])
            else:
                apdu += bytes([0x65, 0xFE])
                apdu += struct.pack(">H", len(value))
            apdu += value
        elif isinstance(value, dict) and "type" in value and "instance" in value:
            # Object Identifier
            obj_id = (value["type"] << 22) | (value["instance"] & 0x3FFFFF)
            apdu += bytes([0xC4])  # tag=12, len=4
            apdu += struct.pack(">I", obj_id)

        # Property Value closing tag
        apdu += bytes([0x3F])  # closing tag 2

        # Priority (context tag 3, length 1)
        apdu += bytes([0x31, priority])

        bvll = self._build_bvll(BVLC_ORIGINAL_UNICAST_NPDU, npdu + apdu)

        # 注册pending future等待SimpleAck确认
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[invoke_id] = future

        try:
            if self._transport:
                self._transport.sendto(bvll, (self._host, self._bvlc_port))
            else:
                return False

            result = await asyncio.wait_for(future, timeout=self._timeout)
            # SimpleAck返回True，Error返回False
            return result is True
        except TimeoutError:
            logger.warning(
                "BACnet写入超时: device=%d, object=%d/%d, property=%d",
                device_instance,
                object_type,
                object_instance,
                property_id,
            )
            return False
        except Exception as e:
            logger.error("BACnet写入失败: %s", e)
            return False
        finally:
            self._pending.pop(invoke_id, None)

    async def who_is(self, device_instance_low: int = 0, device_instance_high: int = 4194303) -> list[dict]:
        """发送Who-Is请求发现设备

        Args:
            device_instance_low: 设备实例号下限 (0=不限制)
            device_instance_high: 设备实例号上限 (4194303=不限制)

        Returns:
            发现的设备列表 [{"device_id": ..., "address": ..., "instance": ..., "vendor_id": ...}]
        """
        # 清空之前的发现结果
        self._discovered_devices = []
        self._discovery_event = asyncio.Event()

        # 构造NPDU - 广播
        npdu = bytes([0x01])  # NPDU control: data, no reply expected

        # 构造APDU - Unconfirmed Service Request
        apdu = bytes([0x10])  # PDU type=1(unconfirmed), reserved
        apdu += bytes([SERVICE_UNCONFIRMED_WHO_IS])  # service choice = Who-Is

        # Device instance range (optional)
        if device_instance_low > 0 or device_instance_high < 4194303:
            apdu += bytes([0x09])  # context tag 0, length 1 for low
            apdu += bytes([device_instance_low & 0xFF])
            apdu += bytes([0x19])  # context tag 1, length 1 for high
            apdu += bytes([device_instance_high & 0xFF])

        bvll = self._build_bvll(BVLC_ORIGINAL_BROADCAST_NPDU, npdu + apdu)

        if self._transport:
            self._transport.sendto(bvll, (BROADCAST_ADDRESS, self._bvlc_port))

        # 等待I-Am响应收集
        try:
            await asyncio.wait_for(self._discovery_event.wait(), timeout=self._timeout)
        except TimeoutError:
            pass  # 超时后返回已收集的设备

        return self._discovered_devices

    def handle_i_am(self, device_info: dict) -> None:
        """处理收到的I-Am响应，添加到发现列表"""
        # 去重：相同device_id不重复添加
        device_id = device_info.get("device_id")
        if not any(d.get("device_id") == device_id for d in self._discovered_devices):
            self._discovered_devices.append(device_info)
            logger.debug("BACnet发现设备: id=%d, address=%s", device_id, device_info.get("address"))

    async def subscribe_cov(
        self,
        device_instance: int,
        object_type: int,
        object_instance: int,
        lifetime: int = 0,
    ) -> bool:
        """订阅COV变化通知

        Args:
            device_instance: 设备实例号
            object_type: 对象类型
            object_instance: 对象实例号
            lifetime: 订阅生命周期(秒)，0表示永久

        Returns:
            订阅是否成功（等待设备确认）
        """
        invoke_id = await self._get_invoke_id()

        # 构造NPDU
        npdu = bytes([0x01])

        # 构造APDU - Confirmed Service Request
        apdu = bytes([0x00])
        apdu += bytes([invoke_id])
        apdu += bytes([SERVICE_CONFIRMED_SUBSCRIBE_COV])

        # Subscriber Process Identifier (context tag 0, unsigned)
        apdu += bytes([0x09, 0x01])  # context tag 0, length 1, process id = 1

        # Monitored Object Identifier (context tag 1, length 4)
        apdu += bytes([0x1C])  # context tag 1, length 4
        apdu += struct.pack(">I", (object_type << 22) | object_instance)

        # Issue Confirmed Notifications (context tag 2, boolean true)
        apdu += bytes([0x21])  # context tag 2, boolean true

        # Lifetime (context tag 3, unsigned) - 仅当lifetime > 0时包含
        if lifetime > 0:
            if lifetime < 0x100:
                apdu += bytes([0x39, lifetime])  # context tag 3, length 1
            elif lifetime < 0x10000:
                apdu += bytes([0x3A])  # context tag 3, length 2
                apdu += struct.pack(">H", lifetime)

        bvll = self._build_bvll(BVLC_ORIGINAL_UNICAST_NPDU, npdu + apdu)

        # 注册pending future等待确认
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[invoke_id] = future

        try:
            if self._transport:
                self._transport.sendto(bvll, (self._host, self._bvlc_port))
            else:
                return False

            result = await asyncio.wait_for(future, timeout=self._timeout)
            return result is True
        except TimeoutError:
            logger.warning("BACnet COV订阅超时: device=%d, object=%d/%d", device_instance, object_type, object_instance)
            return False
        except Exception as e:
            logger.error("BACnet COV订阅失败: %s", e)
            return False
        finally:
            self._pending.pop(invoke_id, None)

    async def read_property_multiple(
        self,
        device_instance: int,
        requests: list[tuple[int, int, list[int]]],
    ) -> dict[int, dict[int, Any]]:
        """批量读取多个对象的多个属性（真正的ReadPropertyMultiple服务）

        Args:
            device_instance: 设备实例号
            requests: [(object_type, object_instance, [property_id, ...]), ...]

        Returns:
            {object_instance: {property_id: value}}
        """
        invoke_id = await self._get_invoke_id()

        # 构造NPDU
        npdu = bytes([0x01])

        # 构造APDU - Confirmed Service Request (ReadPropertyMultiple = 14)
        apdu = bytes([0x00])
        apdu += bytes([invoke_id])
        apdu += bytes([SERVICE_CONFIRMED_READ_PROPERTY_MULTIPLE])

        # 构造读取访问规范列表
        for obj_type, obj_inst, prop_ids in requests:
            # Object Identifier (context tag 0, length 4)
            apdu += bytes([0x0C])
            apdu += struct.pack(">I", (obj_type << 22) | obj_inst)

            # List of Property References (opening tag 1)
            apdu += bytes([0x1E])  # opening tag 1

            for prop_id in prop_ids:
                # Property Identifier (context tag 0, length 1)
                apdu += bytes([0x09, prop_id])

            # List of Property References (closing tag 1)
            apdu += bytes([0x1F])  # closing tag 1

        bvll = self._build_bvll(BVLC_ORIGINAL_UNICAST_NPDU, npdu + apdu)

        # 注册pending future
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[invoke_id] = future

        try:
            if self._transport:
                self._transport.sendto(bvll, (self._host, self._bvlc_port))
            else:
                return {}

            raw_result = await asyncio.wait_for(future, timeout=self._timeout)
            if raw_result is None:
                return {}
            # 解析ReadPropertyMultiple响应
            if isinstance(raw_result, bytes):
                return self._parse_rpm_response(raw_result)
            return {}
        except TimeoutError:
            logger.warning("BACnet批量读取超时: device=%d", device_instance)
            return {}
        except Exception as e:
            logger.error("BACnet批量读取失败: %s", e)
            return {}
        finally:
            self._pending.pop(invoke_id, None)

    def _parse_rpm_response(self, data: bytes) -> dict[int, dict[int, Any]]:
        """解析ReadPropertyMultiple的ComplexAck响应"""
        results: dict[int, dict[int, Any]] = {}
        try:
            offset = 0
            while offset < len(data):
                # Object Identifier (context tag 0, length 4)
                if offset >= len(data) or data[offset] != 0x0C:
                    break
                offset += 1
                if offset + 4 > len(data):
                    break
                obj_id = struct.unpack(">I", data[offset : offset + 4])[0]
                obj_inst = obj_id & 0x3FFFFF
                offset += 4
                results[obj_inst] = {}

                # List of Read Access Results (opening tag 1)
                if offset >= len(data) or data[offset] != 0x1E:
                    break
                offset += 1

                # 读取属性值列表
                while offset < len(data):
                    if data[offset] == 0x1F:  # closing tag 1
                        offset += 1
                        break

                    # Property Identifier (context tag 0)
                    if data[offset] == 0x09:
                        prop_id = data[offset + 1]
                        offset += 2

                        # Read Result (opening tag 1)
                        if offset < len(data) and data[offset] == 0x1E:
                            offset += 1
                            # 检查是否有错误
                            if offset < len(data) and data[offset] == 0x09:
                                # Error: error_class, error_code
                                offset += 1  # skip tag
                                results[obj_inst][prop_id] = None
                                # 跳到closing tag
                                while offset < len(data) and data[offset] != 0x1F:
                                    offset += 1
                                if offset < len(data):
                                    offset += 1  # skip closing tag
                            else:
                                # Property Value
                                if offset < len(data):
                                    tag_byte = data[offset]
                                    tag_len = tag_byte & 0x07
                                    tag_class = (tag_byte >> 3) & 0x01
                                    if tag_class == 0:  # application tag
                                        offset += 1
                                        value_data = data[offset : offset + tag_len]
                                        results[obj_inst][prop_id] = _decode_application_data(tag_byte, value_data)
                                        offset += tag_len
                                    else:
                                        offset += 1 + tag_len
                                        results[obj_inst][prop_id] = None
                                # closing tag 1
                                if offset < len(data) and data[offset] == 0x1F:
                                    offset += 1
                        continue
                    offset += 1

        except Exception as e:
            logger.debug("BACnet RPM响应解析异常: %s", e)

        return results

    def _build_bvll(self, bvll_function: int, npdu: bytes) -> bytes:
        """构建BVLL头"""
        bvll = bytes([0x81])  # BACnet/IP version
        bvll += bytes([bvll_function])  # BVLL function
        bvll += struct.pack(">H", len(npdu) + 4)  # BVLL length (header + NPDU)
        return bvll + npdu

    async def _get_invoke_id(self) -> int:
        async with self._lock:
            invoke_id = self._next_invoke_id
            self._next_invoke_id = (self._next_invoke_id + 1) & 0xFF
            if self._next_invoke_id == 0:
                self._next_invoke_id = 1
            return invoke_id

    def handle_response(self, invoke_id: int, data: Any) -> None:
        """处理收到的确认响应"""
        future = self._pending.get(invoke_id)
        if future and not future.done():
            future.set_result(data)

    def handle_cov_notification(self, cov_data: dict) -> None:
        """处理收到的COV通知"""
        if self._cov_callback:
            try:
                self._cov_callback(cov_data)
            except Exception as e:
                logger.error("BACnet COV回调异常: %s", e)


class _BACnetProtocol(asyncio.DatagramProtocol):
    """BACnet/IP UDP协议处理器"""

    def __init__(self, client: BACnetClient):
        self._client = client
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport
        logger.debug("BACnet UDP连接已建立")

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        """处理接收到的数据报"""
        try:
            if len(data) < 4:
                return

            # 解析BVLL
            version = data[0]
            if version != 0x81:
                return
            bvll_function = data[1]
            struct.unpack(">H", data[2:4])[0]

            # BVLL Result / ACK 等控制消息
            if bvll_function in (0x00, 0x01, 0x02, 0x03):
                return

            npdu = data[4:]

            if len(npdu) < 2:
                return

            # 解析NPDU
            npdu_control = npdu[0]
            offset = 1

            # 检查是否有目标地址
            if npdu_control & 0x20:  # DNET present
                if offset + 2 > len(npdu):
                    return
                struct.unpack(">H", npdu[offset : offset + 2])[0]
                offset += 2
                if offset < len(npdu):
                    dlen = npdu[offset]
                    offset += 1 + dlen
                if npdu_control & 0x08:  # SNET present
                    if offset + 2 > len(npdu):
                        return
                    offset += 2
                    if offset < len(npdu):
                        slen = npdu[offset]
                        offset += 1 + slen

            # 跳过hop count
            if npdu_control & 0x20:
                offset += 1

            if offset >= len(npdu):
                return

            # 解析APDU
            apdu = npdu[offset:]
            if len(apdu) < 1:
                return

            parsed = _parse_apdu_response(apdu)

            if parsed["type"] == "unconfirmed_request" and parsed.get("service") == SERVICE_UNCONFIRMED_I_AM:
                # I-Am响应 - 设备发现
                i_am_data = parsed.get("data", b"")
                if i_am_data:
                    device_info = _parse_i_am(bytes([SERVICE_UNCONFIRMED_I_AM]) + i_am_data, addr)
                    if device_info:
                        self._client.handle_i_am(device_info)
                return

            if (
                parsed["type"] == "unconfirmed_request"
                and parsed.get("service") == SERVICE_UNCONFIRMED_COV_NOTIFICATION
            ):
                # COV通知
                cov_data = parsed.get("data", b"")
                if cov_data:
                    cov_result = _parse_cov_notification(cov_data)
                    if cov_result:
                        self._client.handle_cov_notification(cov_result)
                return

            if parsed["type"] == "simple_ack":
                invoke_id = parsed.get("invoke_id")
                if invoke_id is not None:
                    self._client.handle_response(invoke_id, True)
                return

            if parsed["type"] == "complex_ack":
                invoke_id = parsed.get("invoke_id")
                if invoke_id is not None:
                    raw_data = parsed.get("data")
                    if raw_data is not None:
                        # 解析ReadProperty响应
                        value = _parse_read_property_response(raw_data)
                        self._client.handle_response(invoke_id, value)
                    else:
                        self._client.handle_response(invoke_id, True)
                return

            if parsed["type"] == "error":
                invoke_id = parsed.get("invoke_id")
                if invoke_id is not None:
                    error = parsed.get("error", {})
                    logger.warning(
                        "BACnet错误响应: invoke_id=%d, class=%s, code=%s",
                        invoke_id,
                        error.get("class"),
                        error.get("code"),
                    )
                    self._client.handle_response(invoke_id, None)
                return

            if parsed["type"] == "reject":
                invoke_id = parsed.get("invoke_id")
                if invoke_id is not None:
                    logger.warning(
                        "BACnet拒绝响应: invoke_id=%d, reason=%s", invoke_id, parsed.get("error", {}).get("reason")
                    )
                    self._client.handle_response(invoke_id, None)
                return

        except Exception as e:
            logger.debug("BACnet数据解析异常: %s", e)

    def error_received(self, exc: Exception) -> None:
        logger.warning("BACnet UDP错误: %s", exc)


class BACnetDriver(DriverPlugin):
    """BACnet/IP 协议驱动

    配置参数:
        host: BACnet设备IP地址 (默认广播地址)
        port: BACnet/IP端口 (默认47808)
        device_instance: 本设备实例号 (用于标识网关自身)
        timeout: 通信超时秒 (默认5)
        enable_cov: 启用COV订阅模式 (默认True)
    """

    plugin_name = "bacnet_ip"
    plugin_version = "1.2.0"
    supported_protocols = ["bacnet", "bacnet_ip", "bacnetip"]
    config_schema = {
        "description": "BACnet/IP building automation protocol, supports HVAC/Lighting/Access control",
        "fields": [
            {
                "name": "host",
                "type": "string",
                "label": "BACnet Device IP",
                "description": "BACnet device or broadcast address",
                "default": "192.168.1.255",
            },
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "BACnet/IP port (default 47808)",
                "default": 47808,
            },
            {
                "name": "device_instance",
                "type": "integer",
                "label": "Device Instance",
                "description": "BACnet device instance number",
                "default": 100,
            },
            {
                "name": "timeout",
                "type": "number",
                "label": "Timeout (s)",
                "description": "Communication timeout in seconds",
                "default": 5.0,
            },
            {
                "name": "enable_cov",
                "type": "boolean",
                "label": "Enable COV Subscription",
                "description": "Enable Change-of-Value subscription for real-time updates",
                "default": True,
            },
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    # BACnet 预定义对象属性ID
    PROP_PRESENT_VALUE = 85
    PROP_STATUS_FLAGS = 111
    PROP_DESCRIPTION = 28
    PROP_UNITS = 117
    PROP_DEVICE_TYPE = 103

    def __init__(self):
        self._running = False
        self._client: BACnetClient | None = None
        self._config: dict = {}
        self._device_points: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        # COV订阅相关
        self._cov_enabled: bool = True
        self._cov_subscriptions: dict[str, dict] = {}  # point_addr -> subscription info
        self._data_callback: Callable | None = None
        self._latest_values: dict[str, dict[str, Any]] = {}
        self._values_lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        """启动BACnet驱动"""
        self._config = config
        host = config.get("host", "192.168.1.255")
        port = int(config.get("port", DEFAULT_BACNET_PORT))
        timeout = float(config.get("timeout", 5.0))
        self._cov_enabled = config.get("enable_cov", True)

        self._client = BACnetClient(host, port, timeout)
        # 注册COV通知回调
        self._client.set_cov_callback(self._on_cov_notification)
        try:
            await self._client.connect()
            self._running = True
            self._reconnect_count = 0
            logger.info("BACnet驱动启动成功: %s:%d, COV=%s", host, port, "enabled" if self._cov_enabled else "disabled")
        except Exception as e:
            logger.error("BACnet驱动启动失败: %s", e)
            raise

    def on_data(self, callback: Callable) -> None:
        """注册数据回调，用于COV变化通知"""
        self._data_callback = callback

    def is_device_connected(self, device_id: str) -> bool:
        """检查BACnet连接状态"""
        return self._running and self._client is not None

    def get_cov_status(self) -> dict:
        """获取COV订阅状态"""
        return {
            "enabled": self._cov_enabled,
            "subscriptions": len(self._cov_subscriptions),
        }

    async def stop(self) -> None:
        """停止BACnet驱动"""
        self._running = False
        # 取消所有COV订阅任务
        self._cov_subscriptions.clear()
        if self._client:
            self._client.close()
            self._client = None
        logger.info("BACnet驱动已停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加BACnet设备"""
        self._device_points[device_id] = {
            "config": config,
            "points": {p.get("name", ""): p for p in points if p.get("name")},
        }

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._device_points.pop(device_id, None)
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("BACnet device removed: %s", device_id)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取BACnet测点值

        测点地址格式: "type:instance.property" 如 "AV:1.presentValue"
        支持的对象类型前缀:
            AI - Analog Input
            AO - Analog Output
            AV - Analog Value
            BI - Binary Input
            BO - Binary Output
            BV - Binary Value
            MSI - Multi-State Input
            MSO - Multi-State Output
            MSV - Multi-State Value

        支持批量读取，自动优化通信
        """
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        device_info = self._device_points.get(device_id, {})
        device_config = device_info.get("config", {})

        result = {}
        # 尝试批量读取优化
        if len(points) > 1:
            try:
                batch_result = await self._read_points_batch(device_config, points)
                result.update(batch_result)
                return result
            except Exception as e:
                logger.debug("BACnet批量读取失败，回退逐点读取: %s", e)

        for point_addr in points:
            try:
                value = await self._read_point(device_config, point_addr)
                result[point_addr] = value
            except Exception as e:
                logger.warning("BACnet读取失败 %s.%s: %s", device_id, point_addr, e)
                result[point_addr] = None

        return result

    async def _read_points_batch(self, device_config: dict, points: list[str]) -> dict[str, Any]:
        """批量读取多个测点 - 使用ReadPropertyMultiple服务"""
        result = {}
        device_instance = device_config.get("device_instance", 100)

        # 按对象分组，同一对象的多个属性可以合并到一次RPM请求
        obj_requests: dict[tuple[int, int], list[int]] = {}
        point_to_obj: dict[str, tuple[int, int, int]] = {}  # point_addr -> (obj_type, obj_inst, prop_id)

        for point_addr in points:
            parsed = self._parse_point_addr(point_addr)
            if parsed is None:
                result[point_addr] = None
                continue
            obj_type, obj_inst, prop_id = parsed
            key = (obj_type, obj_inst)
            if key not in obj_requests:
                obj_requests[key] = []
            if prop_id not in obj_requests[key]:
                obj_requests[key].append(prop_id)
            point_to_obj[point_addr] = (obj_type, obj_inst, prop_id)

        if not obj_requests:
            return result

        # 构建RPM请求列表
        rpm_requests = [(obj_type, obj_inst, prop_ids) for (obj_type, obj_inst), prop_ids in obj_requests.items()]

        try:
            rpm_results = await self._client.read_property_multiple(device_instance, rpm_requests)

            # 将RPM结果映射回point_addr
            for point_addr, (_obj_type, obj_inst, prop_id) in point_to_obj.items():
                obj_result = rpm_results.get(obj_inst, {})
                result[point_addr] = obj_result.get(prop_id)
        except Exception as e:
            # RPM失败，回退并发逐点读取
            logger.debug("BACnet ReadPropertyMultiple失败，回退并发读取: %s", e)
            batch_size = 5
            for i in range(0, len(points), batch_size):
                batch = points[i : i + batch_size]
                tasks = [self._read_point(device_config, p) for p in batch]
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                for j, res in enumerate(batch_results):
                    result[batch[j]] = None if isinstance(res, Exception) else res

        return result

    def _parse_point_addr(self, point_addr: str) -> tuple[int, int, int] | None:
        """解析测点地址

        Returns:
            (object_type, object_instance, property_id) 或 None
        """
        parts = point_addr.split(".")
        if not parts:
            return None

        obj_part = parts[0]
        obj_info = obj_part.split(":")
        if len(obj_info) != 2:
            return None

        obj_type_str = obj_info[0].upper()
        try:
            instance = int(obj_info[1])
        except ValueError:
            return None

        property_id = self.PROP_PRESENT_VALUE
        if len(parts) > 1:
            prop_str = parts[1].lower()
            property_id = self._get_property_id(prop_str)

        object_type = self._get_object_type(obj_type_str)
        return (object_type, instance, property_id)

    async def _read_point(self, device_config: dict, point_addr: str) -> Any | None:
        """解析地址并读取单个测点"""
        parsed = self._parse_point_addr(point_addr)
        if parsed is None:
            return None

        object_type, instance, property_id = parsed
        device_instance = device_config.get("device_instance", 100)

        return await self._client.read_property(device_instance, object_type, instance, property_id)

    @staticmethod
    def _get_object_type(type_str: str) -> int:
        """获取对象类型码"""
        type_map = {
            "AI": OBJECT_TYPE_ANALOG_INPUT,
            "AO": OBJECT_TYPE_ANALOG_OUTPUT,
            "AV": OBJECT_TYPE_ANALOG_VALUE,
            "BI": OBJECT_TYPE_BINARY_INPUT,
            "BO": OBJECT_TYPE_BINARY_OUTPUT,
            "BV": OBJECT_TYPE_BINARY_VALUE,
            "MSI": OBJECT_TYPE_MULTI_STATE_INPUT,
            "MSO": OBJECT_TYPE_MULTI_STATE_OUTPUT,
            "MSV": OBJECT_TYPE_MULTI_STATE_VALUE,
            "DEV": OBJECT_TYPE_DEVICE,
        }
        return type_map.get(type_str.upper(), OBJECT_TYPE_ANALOG_VALUE)

    @staticmethod
    def _get_property_id(prop_str: str) -> int:
        """获取属性ID - 完整的BACnet属性映射"""
        prop_map = {
            # 基本属性
            "presentvalue": 85,
            "statusflags": 111,
            "description": 28,
            "units": 117,
            "devicetype": 103,
            "objectname": 77,
            "objecttype": 79,
            "objectidentifier": 75,
            # 设备对象属性
            "vendorname": 121,
            "vendoridentifier": 120,
            "vendorid": 120,
            "modelname": 70,
            "firmwarerevision": 44,
            "firmware": 44,
            "applicationsoftwareversion": 12,
            "protocolversion": 98,
            "protocolrevision": 139,
            "maxapdulengthaccepted": 62,
            "maxapdulength": 62,
            "segmentationsupported": 107,
            "segmentation": 107,
            "objectlist": 76,
            # 值属性
            "reliability": 103,
            "outofservice": 81,
            "priorityarray": 87,
            "relinquishdefault": 104,
            "covincrement": 22,
            "minimumvalue": 69,
            "maximumvalue": 65,
            "resolution": 96,
            "statetext": 110,
            "eventstate": 36,
            "notificationclass": 17,
            "highlimit": 45,
            "lowlimit": 59,
            "deadband": 25,
            "limit": 59,
            "feedbackvalue": 40,
        }
        return prop_map.get(prop_str.lower(), 85)

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入BACnet测点值"""
        if not self._running or not self._client:
            return False

        device_info = self._device_points.get(device_id, {})
        device_config = device_info.get("config", {})

        parsed = self._parse_point_addr(point)
        if parsed is None:
            return False

        object_type, instance, _ = parsed
        device_instance = device_config.get("device_instance", 100)

        try:
            success = await self._client.write_property(
                device_instance, object_type, instance, self.PROP_PRESENT_VALUE, value
            )
            if success:
                self._record_write_success(device_id)
            else:
                self._record_write_failure(device_id)
            return success
        except Exception as e:
            logger.error("BACnet写入失败 %s.%s: %s", device_id, point, e)
            self._record_write_failure(device_id)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """BACnet设备发现 - 发送Who-Is获取在线设备"""
        if not self._client:
            return []

        try:
            devices = await self._client.who_is()
            return devices
        except Exception as e:
            logger.error("BACnet设备发现失败: %s", e)
            return []

    async def read_device_info(self, device_id: str) -> dict[str, Any]:
        """读取BACnet设备对象的详细信息（厂商、型号等）

        Args:
            device_id: 设备ID

        Returns:
            设备信息字典
        """
        if not self._client or not self._running:
            return {}

        device_info = self._device_points.get(device_id, {})
        device_config = device_info.get("config", {})
        device_instance = device_config.get("device_instance", 100)

        info: dict[str, Any] = {"device_instance": device_instance}

        # 使用ReadPropertyMultiple批量读取设备属性
        try:
            rpm_result = await self._client.read_property_multiple(
                device_instance,
                [
                    (
                        OBJECT_TYPE_DEVICE,
                        device_instance,
                        [
                            PROP_OBJECT_NAME,
                            PROP_VENDOR_NAME,
                            PROP_VENDOR_IDENTIFIER,
                            PROP_MODEL_NAME,
                            PROP_FIRMWARE_REVISION,
                            PROP_DESCRIPTION,
                            PROP_PROTOCOL_VERSION,
                            PROP_PROTOCOL_REVISION,
                        ],
                    )
                ],
            )
            props = rpm_result.get(device_instance, {})
            info["object_name"] = props.get(PROP_OBJECT_NAME)
            info["vendor_name"] = props.get(PROP_VENDOR_NAME)
            info["vendor_id"] = props.get(PROP_VENDOR_IDENTIFIER)
            info["model_name"] = props.get(PROP_MODEL_NAME)
            info["firmware_revision"] = props.get(PROP_FIRMWARE_REVISION)
            info["description"] = props.get(PROP_DESCRIPTION)
            info["protocol_version"] = props.get(PROP_PROTOCOL_VERSION)
            info["protocol_revision"] = props.get(PROP_PROTOCOL_REVISION)
        except Exception as e:
            logger.warning("BACnet设备信息批量读取失败，回退逐个读取: %s", e)
            # 回退逐个读取
            prop_list = [
                ("object_name", PROP_OBJECT_NAME),
                ("vendor_name", PROP_VENDOR_NAME),
                ("vendor_id", PROP_VENDOR_IDENTIFIER),
                ("model_name", PROP_MODEL_NAME),
                ("firmware_revision", PROP_FIRMWARE_REVISION),
                ("description", PROP_DESCRIPTION),
            ]
            for name, prop_id in prop_list:
                try:
                    value = await self._client.read_property(
                        device_instance, OBJECT_TYPE_DEVICE, device_instance, prop_id
                    )
                    info[name] = value
                except Exception:
                    info[name] = None

        return info

    async def subscribe_cov_point(self, device_id: str, point_addr: str, lifetime: int = 300) -> bool:
        """订阅单个测点的COV变化通知

        Args:
            device_id: 设备ID
            point_addr: 测点地址，如 "AV:1.presentValue"
            lifetime: 订阅生命周期(秒)，默认300秒

        Returns:
            订阅是否成功
        """
        if not self._client or not self._running:
            return False

        device_info = self._device_points.get(device_id, {})
        device_config = device_info.get("config", {})
        device_instance = device_config.get("device_instance", 100)

        parsed = self._parse_point_addr(point_addr)
        if parsed is None:
            return False

        obj_type, obj_inst, _ = parsed
        success = await self._client.subscribe_cov(device_instance, obj_type, obj_inst, lifetime)

        if success:
            self._cov_subscriptions[point_addr] = {
                "device_id": device_id,
                "object_type": obj_type,
                "object_instance": obj_inst,
                "lifetime": lifetime,
                "subscribed_at": asyncio.get_event_loop().time(),
            }

        return success

    async def subscribe_all_cov(self, device_id: str, lifetime: int = 300) -> int:
        """订阅设备所有测点的COV变化

        Args:
            device_id: 设备ID
            lifetime: 订阅生命周期(秒)

        Returns:
            成功订阅的测点数量
        """
        device_info = self._device_points.get(device_id, {})
        points = device_info.get("points", {})
        success_count = 0

        for point_name in points:
            if await self.subscribe_cov_point(device_id, point_name, lifetime):
                success_count += 1

        logger.info("BACnet COV订阅完成: %s, 成功%d个", device_id, success_count)
        return success_count

    def _on_cov_notification(self, cov_data: dict) -> None:
        """处理COV通知回调

        Args:
            cov_data: {"process_id": int, "object": {type, instance}, "values": {prop_id: value}}
        """
        try:
            obj_info = cov_data.get("object")
            if not obj_info:
                return

            obj_type = obj_info.get("type")
            obj_inst = obj_info.get("instance")
            values = cov_data.get("values", {})

            # 查找对应的device_id和point_addr
            for point_addr, sub_info in self._cov_subscriptions.items():
                if sub_info.get("object_type") == obj_type and sub_info.get("object_instance") == obj_inst:
                    device_id = sub_info.get("device_id", "")
                    # 提取presentValue
                    present_value = values.get(PROP_PRESENT_VALUE)
                    if present_value is not None and self._data_callback:
                        self._data_callback(device_id, point_addr, present_value)

            # 更新latest_values缓存
            async def _update_cache():
                async with self._values_lock:
                    for point_addr, sub_info in self._cov_subscriptions.items():
                        if sub_info.get("object_type") == obj_type and sub_info.get("object_instance") == obj_inst:
                            device_id = sub_info.get("device_id", "")
                            if device_id not in self._latest_values:
                                self._latest_values[device_id] = {}
                            present_value = values.get(PROP_PRESENT_VALUE)
                            if present_value is not None:
                                self._latest_values[device_id][point_addr] = present_value

            # 安全地调度异步更新
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_update_cache())
            except RuntimeError:
                pass

        except Exception as e:
            logger.error("BACnet COV通知处理异常: %s", e)

    async def _try_reconnect(self, device_id: str) -> None:
        """重连机制"""
        if not self._config:
            return

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("BACnet重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("BACnet连接断开，%.1fs后重连 (第%d次)", delay, self._reconnect_count)
        await asyncio.sleep(delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY)

        try:
            host = self._config.get("host", "192.168.1.255")
            port = int(self._config.get("port", DEFAULT_BACNET_PORT))
            timeout = float(self._config.get("timeout", 5.0))

            if self._client:
                self._client.close()

            self._client = BACnetClient(host, port, timeout)
            self._client.set_cov_callback(self._on_cov_notification)
            await self._client.connect()
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("BACnet重连成功: %s:%d", host, port)

            # 重连后恢复COV订阅
            if self._cov_enabled and self._cov_subscriptions:
                await self._resubscribe_cov()

        except Exception as e:
            logger.error("BACnet重连失败: %s", e)

    async def _resubscribe_cov(self) -> None:
        """重连后恢复COV订阅"""
        resubscribe_list = list(self._cov_subscriptions.items())
        success = 0
        for point_addr, sub_info in resubscribe_list:
            device_id = sub_info.get("device_id", "")
            try:
                result = await self.subscribe_cov_point(device_id, point_addr, sub_info.get("lifetime", 300))
                if result:
                    success += 1
            except Exception as e:
                logger.warning("BACnet COV重新订阅失败 %s: %s", point_addr, e)

        if resubscribe_list:
            logger.info("BACnet COV重新订阅: 成功%d/%d", success, len(resubscribe_list))
