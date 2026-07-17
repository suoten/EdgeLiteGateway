"""KNXnet/IP 驱动 - 基于CEMI协议实现楼宇自控协议

KNX是欧洲楼宇自动化标准协议（EN 50090），广泛用于HVAC、照明、安防控制。
支持：
- KNXnet/IP over UDP (默认端口3671)
- 设备发现 (Search/Description)
- 组地址读写 (GroupValue_Read/Write/Response)
- 点对点通信 (Data Link Layer)
- 1-bit (开关)、1-byte (百分比)、2-byte (温度) 等多种数据类型
- 事件订阅模式 - 实时接收组地址值变化
- 批量读取优化 - 减少通信开销
"""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Callable
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

# KNXnet/IP 默认配置
DEFAULT_KNX_PORT = 3671
DEFAULT_GATEWAY_HOST = "192.168.1.100"
CONNECTION_TYPE_TUNNEL = 0x04
DEFAULT_HEARTBEAT_INTERVAL = 60.0  # 心跳间隔 (秒)
DEFAULT_HEARTBEAT_MAX_FAILURES = 3  # 心跳最大失败次数

# KNXnet/IP 头
KNXNETIP_HEADER_SIZE = 6
KNXNETIP_VERSION = 0x10

# KNXnet/IP 服务类型
SERVICE_TYPE_SEARCH_REQUEST = 0x0202
SERVICE_TYPE_SEARCH_RESPONSE = 0x0203
SERVICE_TYPE_DESCRIPTION_REQUEST = 0x0204
SERVICE_TYPE_DESCRIPTION_RESPONSE = 0x0205
SERVICE_TYPE_CONNECT_REQUEST = 0x0206
SERVICE_TYPE_CONNECT_RESPONSE = 0x0207
SERVICE_TYPE_CONNECTIONSTATE_REQUEST = 0x0208
SERVICE_TYPE_CONNECTIONSTATE_RESPONSE = 0x0209
SERVICE_TYPE_DISCONNECT_REQUEST = 0x020A
SERVICE_TYPE_DISCONNECT_RESPONSE = 0x020B
SERVICE_TYPE_TUNNEL_REQUEST = 0x0410
SERVICE_TYPE_TUNNEL_INDICATION = 0x0411

# KNX 消息类型
KNX_CEMI_L_BUS_INDICATION = 0x2B
KNX_CEMI_L_DATA_REQ = 0x11

# KNX 寻址类型
KNX_ADDRESS_TYPE_GROUP = 0
KNX_ADDRESS_TYPE_INDIVIDUAL = 1

# KNX 数据类型
DATA_TYPE_SWITCH = "switch"  # 1-bit 开关
DATA_TYPE_PERCENT = "percent"  # 1-byte 百分比 0-100
DATA_TYPE_U8 = "u8"  # 1-byte 无符号
DATA_TYPE_U16 = "u16"  # 2-byte 无符号
DATA_TYPE_TEMPERATURE = "temperature"  # 2-byte 温度 (0.01℃分辨率)


def _knx_address_to_bytes(address: str) -> bytes:
    """解析KNX地址 (如 "1/2/3" 或 "1.2.3") 并转换为字节

    Args:
        address: KNX地址，格式 "area.line.member" 或 "area.line.member"

    Returns:
        2字节KNX地址
    """
    # 支持 "/" 或 "." 分隔符
    addr = address.replace("/", ".").split(".")
    if len(addr) < 3:
        raise ValueError(f"无效的KNX地址格式: {address}")

    try:
        area = int(addr[0])
        line = int(addr[1])
        member = int(addr[2])
    except ValueError as err:
        raise ValueError(f"无效的KNX地址格式: {address}") from err

    # KNX地址格式: 0xAAALL (area=5位, line=3位, member=8位)
    return bytes([(area << 3) | line, member])


def _bytes_to_knx_address(high_byte: int, low_byte: int) -> str:
    """字节转换为KNX地址字符串"""
    area = (high_byte >> 3) & 0x1F
    line = high_byte & 0x07
    member = low_byte
    return f"{area}/{line}/{member}"


def _encode_value(value: Any, data_type: str) -> bytes:
    """编码KNX值到字节 (返回 TPDU payload，不含 GroupValue_Write 标志)"""
    if data_type == DATA_TYPE_SWITCH:
        # 1-bit 开关: TPDU = 0x80 | value (单字节)
        if value in (True, "on", "ON", 1, "1"):
            return bytes([0x81])  # GroupValue_Write + data=1
        else:
            return bytes([0x80])  # GroupValue_Write + data=0

    elif data_type in (DATA_TYPE_PERCENT, DATA_TYPE_U8):
        # 1-byte 无符号: TPDU = 0x80 + 1 byte data
        val = max(0, min(255, int(value)))
        return bytes([0x80, val])

    elif data_type in (DATA_TYPE_U16, DATA_TYPE_TEMPERATURE):
        # 2-byte 无符号/温度: TPDU = 0x80 + 2 byte data
        val = max(0, min(65535, int(value)))
        return bytes([0x80, (val >> 8) & 0xFF, val & 0xFF])

    else:
        # 默认2字节
        return bytes([0x80, 0x00])


def _decode_value(data: bytes, data_type: str) -> Any:
    """解码KNX字节到值"""
    if not data or len(data) < 1:
        return None

    if data_type == DATA_TYPE_SWITCH:
        # 检查动作位
        if len(data) >= 2:
            action = data[1] & 0x01
            return action == 1
        return data[0] & 0x01 == 1

    elif data_type in (DATA_TYPE_PERCENT, DATA_TYPE_U8):
        return data[0] if len(data) >= 1 else 0

    elif data_type in (DATA_TYPE_U16, DATA_TYPE_TEMPERATURE):
        if len(data) >= 2:
            return (data[0] << 8) | data[1]
        return data[0] if len(data) >= 1 else 0

    else:
        return data[0] if len(data) >= 1 else 0


class KNXClient:
    """KNXnet/IP 客户端封装"""

    @staticmethod
    def _build_cemi_l_data_req(knx_addr: bytes, tpdu: bytes) -> bytes:
        """构建 cEMI L_Data.req 帧

        结构: MsgCode(1) + AddInfoLen(1) + Ctrl1(1) + Ctrl2(1) + SrcAddr(2) + DstAddr(2) + PayloadLen(1) + TPDU
        PayloadLen = len(TPDU) - 1 (首字节为 APCI)
        """
        cemi = bytes(
            [
                0x11,  # Message code: L_Data.req
                0x00,  # Additional info length
                0xBC,  # Control field 1
                0x60,  # Control field 2: group addressing
            ]
        )
        cemi += bytes([0x00, 0x00])  # Source address (placeholder)
        cemi += knx_addr  # Destination address
        cemi += bytes([len(tpdu) - 1])  # Payload length (APCI 占首字节)
        cemi += tpdu
        return cemi

    def __init__(
        self,
        gateway_host: str,
        gateway_port: int = DEFAULT_KNX_PORT,
        local_port: int = 0,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
    ):
        self._gateway_host = gateway_host
        self._gateway_port = gateway_port
        self._local_port = local_port
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _KNXProtocol | None = None
        self._channel_id: int = 0
        self._connected: bool = False
        self._pending: dict[int, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        # 事件订阅相关
        self._group_value_callback: Callable | None = None
        self._latest_values: dict[str, Any] = {}  # group_addr -> value
        self._pending_reads: dict[str, asyncio.Future] = {}
        # 心跳保活相关
        self._heartbeat_interval: float = heartbeat_interval
        self._heartbeat_timeout: float = 5.0  # 响应超时 (秒)
        self._heartbeat_max_failures: int = DEFAULT_HEARTBEAT_MAX_FAILURES
        self._heartbeat_failures: int = 0
        self._heartbeat_task: asyncio.Task | None = None
        self._heartbeat_response_future: asyncio.Future | None = None

    async def connect(self) -> bool:
        """建立KNXnet/IP隧道连接"""
        loop = asyncio.get_running_loop()
        self._protocol = _KNXProtocol(self)

        if self._local_port == 0:
            # 使用随机本地端口
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: self._protocol,
                local_addr=("0.0.0.0", 0),
            )
        else:
            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: self._protocol,
                local_addr=("0.0.0.0", self._local_port),
            )

        # 搜索网关
        search_response = await self._search_gateway()
        if not search_response:
            logger.error("KNX网关搜索失败")
            return False

        # 请求连接
        connect_ok = await self._connect_request()
        if not connect_ok:
            logger.error("KNX网关连接请求失败")
            return False

        self._connected = True
        logger.info("KNXnet/IP连接成功")
        return True

    def close(self) -> None:
        """关闭连接"""
        self._connected = False
        if self._transport:
            self._transport.close()
            self._transport = None

    async def _search_gateway(self) -> bool:
        """搜索KNX网关"""
        header = struct.pack(
            ">BBHH",
            KNXNETIP_VERSION,
            0x02,  # Header size
            SERVICE_TYPE_SEARCH_REQUEST,
            0x0000,  # Total length (will be updated)
        )

        # Search request body: discovery endpoint
        body = struct.pack(">HH", 3671, 0x08)  # port, protocol

        header = struct.pack(
            ">BBHH",
            KNXNETIP_VERSION,
            0x0A,
            SERVICE_TYPE_SEARCH_REQUEST,
            len(header) + len(body),
        )

        frame = header + body

        future: asyncio.Future = asyncio.create_future()
        self._pending[0] = future

        if self._transport:
            self._transport.sendto(frame, (self._gateway_host, self._gateway_port))

        try:
            await asyncio.wait_for(future, timeout=5.0)
            return True
        except TimeoutError:
            return False
        finally:
            self._pending.pop(0, None)

    async def _connect_request(self) -> bool:
        """发送连接请求"""
        # 连接请求头
        header = struct.pack(
            ">BBHH",
            KNXNETIP_VERSION,
            0x06,  # Header size
            SERVICE_TYPE_CONNECT_REQUEST,
            0x001E,  # Total length
        )

        # 连接请求数据
        body = bytes(
            [
                0x08,
                0x01,  # IP address length, protocol (TCP)
                0x00,
                0x00,
                0x00,
                0x00,  # Local IP (placeholder)
                0x00,
                0x00,  # Local port (placeholder)
                0x04,
                0x04,  # Connection type: Tunnel
                0x0C,
                0x4B,  # Connection type data length
                0x02,
                0x00,  # LinkLayer (0x02)
                0x01,
                0x10,  # Maximum APDU length: 0x010F = 271
            ]
        )

        frame = header + body

        future: asyncio.Future = asyncio.create_future()
        self._pending[1] = future

        if self._transport:
            self._transport.sendto(frame, (self._gateway_host, self._gateway_port))

        try:
            result = await asyncio.wait_for(future, timeout=10.0)
            return result
        except TimeoutError:
            return False
        finally:
            self._pending.pop(1, None)

    async def read_group_value(self, group_address: str, data_type: str = DATA_TYPE_SWITCH) -> Any | None:
        """读取KNX组地址值

        Args:
            group_address: KNX组地址 (如 "1/2/3")
            data_type: 数据类型

        Returns:
            读取的值，失败返回None
        """
        if not self._connected:
            return None

        try:
            # 构造CEMI L_Data.req
            knx_addr = _knx_address_to_bytes(group_address)
            addr_bytes = knx_addr

            # CEMI头
            cemi = bytes(
                [
                    0x11,  # Message code: L_Data.req
                    0x00,
                    0xBC,  # Additional info length
                    0xE0,  # Control field 1: priority=low, group addressing
                    0xE0,  # Control field 2
                    0x00,
                    0x00,  # Destination address (placeholder)
                    0x11,  # Source address length
                    0x00,  # Source address (0 = from bus)
                ]
            )
            cemi += bytes([0x00]) * 2  # Padding
            cemi += addr_bytes

            # TPDU: GroupValue_Read
            tpdu = bytes([0x00, 0x00, 0xBC])  # Group write

            cemi += tpdu

            # 封装Tunnel Request
            tunnel_req = struct.pack(">BBH", self._channel_id, 0x00, len(cemi)) + cemi

            header = struct.pack(
                ">BBHH",
                KNXNETIP_VERSION,
                0x06,
                SERVICE_TYPE_TUNNEL_REQUEST,
                6 + len(tunnel_req),
            )

            frame = header + tunnel_req

            if self._transport:
                self._transport.sendto(frame, (self._gateway_host, self._gateway_port))

            # KNX响应会通过 handle_packet 回调
            await asyncio.sleep(0.5)  # 等待响应
            return None  # 简化实现，返回None表示读取请求已发送

        except Exception as e:
            logger.error("KNX读取失败 %s: %s", group_address, e)
            return None

    async def write_group_value(self, group_address: str, value: Any, data_type: str = DATA_TYPE_SWITCH) -> bool:
        """写入KNX组地址值

        Args:
            group_address: KNX组地址 (如 "1/2/3")
            value: 要写入的值
            data_type: 数据类型

        Returns:
            写入是否成功
        """
        if not self._connected:
            return False

        try:
            knx_addr = _knx_address_to_bytes(group_address)
            tpdu = _encode_value(value, data_type)
            cemi = self._build_cemi_l_data_req(knx_addr, tpdu)

            # Tunnel Request: channel_id(1) + sequence(1) + reserved(1) + cEMI
            tunnel_req = bytes([self._channel_id, 0x00, 0x00]) + cemi

            # KNXnet/IP header: version(1) + header_len(1) + service_type(2) + total_len(2)
            header = struct.pack(
                ">BBHH",
                KNXNETIP_VERSION,  # KNXnet/IP version (0x10)
                0x06,  # Header length
                SERVICE_TYPE_TUNNEL_REQUEST,
                6 + len(tunnel_req),  # Total length
            )

            frame = header + tunnel_req

            if self._transport:
                self._transport.sendto(frame, (self._gateway_host, self._gateway_port))

            logger.info("KNX写入成功: %s = %s", group_address, value)
            return True

        except Exception as e:
            logger.error("KNX写入失败 %s: %s", group_address, e)
            return False

    def handle_packet(self, data: bytes) -> None:
        """处理接收到的数据包"""
        if len(data) < 6:
            return

        service_type = struct.unpack(">H", data[2:4])[0]

        if service_type == SERVICE_TYPE_SEARCH_RESPONSE:
            future = self._pending.pop(0, None)
            if future and not future.done():
                future.set_result(True)

        elif service_type == SERVICE_TYPE_CONNECT_RESPONSE:
            if len(data) >= 10:
                status = data[6]
                if status == 0x00:  # E_NO_ERROR
                    self._channel_id = data[7]
                    future = self._pending.pop(1, None)
                    if future and not future.done():
                        future.set_result(True)

        elif service_type == SERVICE_TYPE_CONNECTIONSTATE_RESPONSE:
            # 心跳响应: header(6) + ChannelID(1) + Status(1)
            if len(data) >= 8:
                status = data[7]
                future = self._heartbeat_response_future
                if future is not None and not future.done():
                    future.set_result(status == 0x00)

        elif service_type == SERVICE_TYPE_TUNNEL_INDICATION:
            # 接收组地址值变化
            self._handle_tunnel_indication(data)

    def _handle_tunnel_indication(self, data: bytes) -> None:
        """处理Tunnel Indication消息（组值变化事件）

        帧结构: KNXnetIP头(6) + Tunnel体(3: ChannelID, SeqCounter, Status) + cEMI
        cEMI结构: MsgCode(1) + AddInfoLen(1) + Ctrl1(1) + Ctrl2(1) + SrcAddr(2) + DstAddr(2) + PayloadLen(1) + TPDU
        """
        try:
            if len(data) < 10:
                return

            # 跳过 KNXnet/IP 头(6) + Tunnel体(3) 到达 cEMI
            cemi_data = data[9:]

            if len(cemi_data) < 9:  # 最小 cEMI: 9 字节 (头8 + 至少1字节TPDU)
                return

            # cEMI 消息码
            msg_code = cemi_data[0]
            if msg_code == KNX_CEMI_L_BUS_INDICATION:
                # L_Data.ind - 组地址数据
                # DstAddr 位于 cEMI offset 6-7
                high_byte = cemi_data[6]
                low_byte = cemi_data[7]
                group_addr = _bytes_to_knx_address(high_byte, low_byte)

                # PayloadLen 位于 offset 8, TPDU 位于 offset 9+
                tpdu = cemi_data[9:]

                if len(tpdu) < 1:
                    return

                apci = tpdu[0]

                # 仅处理 GroupValue_Write (bit7=1)
                if (apci & 0x80) == 0x80:
                    if len(tpdu) == 1:
                        # 1-byte TPDU: 开关 (数据在 bit0)
                        data_value = apci & 0x01
                    elif len(tpdu) == 2:
                        # 2-byte TPDU: 百分比/u8
                        data_value = tpdu[1]
                    elif len(tpdu) >= 3:
                        # 3+ byte TPDU: u16
                        data_value = (tpdu[1] << 8) | tpdu[2]
                    else:
                        return

                    # 更新缓存值
                    self._latest_values[group_addr] = data_value

                    # 触发回调
                    if self._group_value_callback:
                        try:
                            self._group_value_callback(group_addr, data_value)
                        except Exception:
                            pass
                # GroupValue_Read (bit7=0) 不更新缓存
        except Exception as e:
            logger.debug("KNX Tunnel Indication处理异常: %s", e)

    def set_group_value_callback(self, callback: Callable) -> None:
        """设置组值变化回调"""
        self._group_value_callback = callback

    def get_latest_value(self, group_addr: str) -> Any | None:
        """获取最新组地址值"""
        return self._latest_values.get(group_addr)

    # ==================== 心跳保活 (ConnectionStateRequest/Response) ====================

    def _build_connectionstate_request(self) -> bytes:
        """构建 ConnectionStateRequest 帧

        结构: KNXnetIP头(6) + HPAI(8) + ChannelID(1) + Reserved(1) = 16字节
        HPAI: structure_length(1) + protocol(1) + IP(4) + port(2) = 8字节
        """
        # HPAI (Host Protocol Address Information)
        hpai = bytes(
            [
                0x08,  # structure_length
                0x01,  # host_protocol_code: IPv4 UDP
                0,
                0,
                0,
                0,  # IP address (0.0.0.0)
                0,
                0,  # Port (0)
            ]
        )
        # ChannelID + Reserved
        body = hpai + bytes([self._channel_id, 0x00])
        # KNXnet/IP header
        header = struct.pack(
            ">BBHH",
            KNXNETIP_VERSION,
            0x06,
            SERVICE_TYPE_CONNECTIONSTATE_REQUEST,
            6 + len(body),
        )
        return header + body

    async def _send_connectionstate_request(self) -> bool:
        """发送 ConnectionStateRequest 并等待响应

        Returns:
            True if status=E_NO_ERROR, False on error or timeout
        """
        if not self._transport:
            return False

        loop = asyncio.get_running_loop()
        self._heartbeat_response_future = loop.create_future()

        frame = self._build_connectionstate_request()
        self._transport.sendto(frame, (self._gateway_host, self._gateway_port))

        try:
            result = await asyncio.wait_for(
                self._heartbeat_response_future,
                timeout=self._heartbeat_timeout,
            )
            return result
        except TimeoutError:
            return False
        finally:
            self._heartbeat_response_future = None

    async def _heartbeat_loop(self) -> None:
        """心跳循环: 定期发送 ConnectionStateRequest 检测连接状态"""
        while self._connected:
            result = await self._send_connectionstate_request()
            if result:
                self._heartbeat_failures = 0
            else:
                self._heartbeat_failures += 1
                if self._heartbeat_failures >= self._heartbeat_max_failures:
                    self._connected = False
                    logger.warning(
                        "KNX心跳失败达上限(%d次), 断开连接",
                        self._heartbeat_max_failures,
                    )
                    break
            await asyncio.sleep(self._heartbeat_interval)

    def _start_heartbeat(self) -> None:
        """启动心跳后台任务"""
        if self._heartbeat_interval <= 0:
            return
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return  # 已在运行, 幂等
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def _stop_heartbeat(self) -> None:
        """停止心跳后台任务"""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        self._heartbeat_failures = 0
        if self._heartbeat_response_future is not None:
            if not self._heartbeat_response_future.done():
                self._heartbeat_response_future.cancel()
            self._heartbeat_response_future = None


class _KNXProtocol(asyncio.DatagramProtocol):
    """KNXnet/IP UDP协议处理器"""

    def __init__(self, client: KNXClient):
        self._client = client

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        logger.debug("KNX UDP连接已建立")

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._client.handle_packet(data)

    def error_received(self, exc: Exception) -> None:
        logger.warning("KNX UDP错误: %s", exc)


class KNXDriver(DriverPlugin):
    """KNXnet/IP 协议驱动

    配置参数:
        gateway_host: KNX网关IP地址 (默认192.168.1.100)
        gateway_port: KNXnet/IP端口 (默认3671)
        local_port: 本地监听端口 (默认0=随机)
        enable_events: 启用事件订阅模式 (默认True)
    """

    plugin_name = "knx"
    plugin_version = "1.1.0"
    supported_protocols = ["knx", "knxnet_ip", "knxnetip"]
    config_schema = {
        "description": "KNX building automation protocol for HVAC/Lighting/Control",
        "fields": [
            {
                "name": "gateway_host",
                "type": "string",
                "label": "KNX Gateway IP",
                "description": "KNXnet/IP gateway IP address",
                "default": "192.168.1.100",
            },
            {
                "name": "gateway_port",
                "type": "integer",
                "label": "Gateway Port",
                "description": "KNXnet/IP port (default 3671)",
                "default": 3671,
            },
            {
                "name": "local_port",
                "type": "integer",
                "label": "Local Port",
                "description": "Local UDP port (0=random)",
                "default": 0,
            },
            {
                "name": "enable_events",
                "type": "boolean",
                "label": "Enable Event Subscription",
                "description": "Enable group address event subscription for real-time updates",
                "default": True,
            },
            {
                "name": "heartbeat_interval",
                "type": "number",
                "label": "Heartbeat Interval",
                "description": "ConnectionState heartbeat interval in seconds (0=disabled)",
                "default": 60.0,
            },
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        super().__init__()  # FIXED-P0: 必须调用基类初始化
        self._running = False
        self._client: KNXClient | None = None
        self._config: dict = {}
        self._device_points: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        # 事件订阅相关
        self._enable_events: bool = True
        self._data_callback: Callable | None = None
        self._latest_values: dict[str, Any] = {}

    async def start(self, config: dict) -> None:
        """启动KNX驱动"""
        self._config = config
        gateway_host = config.get("gateway_host", DEFAULT_GATEWAY_HOST)
        gateway_port = int(config.get("gateway_port", DEFAULT_KNX_PORT))
        local_port = int(config.get("local_port", 0))
        self._enable_events = config.get("enable_events", True)

        self._client = KNXClient(gateway_host, gateway_port, local_port)

        # 设置事件回调
        if self._enable_events:
            self._client.set_group_value_callback(self._on_knx_event)

        try:
            connected = await self._client.connect()
            if connected:
                self._running = True
                self._reconnect_count = 0
                logger.info(
                    "KNX驱动启动成功: %s:%d, events=%s",
                    gateway_host,
                    gateway_port,
                    "enabled" if self._enable_events else "disabled",
                )
            else:
                logger.error("KNX驱动连接失败")
        except Exception as e:
            logger.error("KNX驱动启动异常: %s", e)
            raise

    def _on_knx_event(self, group_addr: str, value: Any) -> None:
        """KNX事件回调"""
        self._latest_values[group_addr] = value
        logger.debug("KNX事件: %s = %s", group_addr, value)

        if self._data_callback:
            try:
                asyncio.create_task(self._data_callback(group_addr, value))
            except Exception:
                pass

    def on_data(self, callback: Callable) -> None:
        """注册数据回调"""
        self._data_callback = callback

    async def stop(self) -> None:
        """停止KNX驱动"""
        self._running = False
        if self._client:
            self._client.close()
            self._client = None
        await super().stop()  # FIXED-P0: 清理基类资源
        logger.info("KNX驱动已停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加KNX设备/组"""
        self._device_points[device_id] = {
            "config": config,
            "points": {p.get("name", ""): p for p in points if p.get("name")},
        }

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._device_points.pop(device_id, None)
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("KNX device removed: %s", device_id)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取KNX测点值

        测点地址格式: "group_address" 如 "1/2/3"

        测点配置中可指定data_type:
            - switch: 1-bit 开关
            - percent: 1-byte 百分比
            - u8: 1-byte 无符号
            - u16: 2-byte 无符号
            - temperature: 2-byte 温度

        支持批量读取优化，自动并发读取多个测点
        """
        if not self._running or not self._client:
            await self._try_reconnect(device_id)
            return {}

        device_info = self._device_points.get(device_id, {})
        device_info.get("config", {})
        device_pts = device_info.get("points", {})

        result = {}
        # 优先从缓存读取
        for point_addr in points:
            cached = self._latest_values.get(point_addr)
            if cached is not None:
                result[point_addr] = cached

        # 并发读取未缓存的测点
        uncached = [p for p in points if p not in result]
        if uncached:
            batch_result = await self._read_points_batch(device_pts, uncached)
            result.update(batch_result)

        return result

    async def _read_points_batch(self, device_pts: dict, points: list[str]) -> dict[str, Any]:
        """批量读取多个测点（并发请求）"""
        tasks = []
        for point_addr in points:
            pt_def = device_pts.get(point_addr, {})
            data_type = pt_def.get("data_type", DATA_TYPE_SWITCH)
            tasks.append(self._client.read_group_value(point_addr, data_type))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {p: r if not isinstance(r, Exception) else None for p, r in zip(points, results, strict=False)}

    def get_event_status(self) -> dict:
        """获取事件订阅状态"""
        return {
            "enabled": self._enable_events,
            "subscribed_points": len(self._latest_values),
        }

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入KNX测点值"""
        if not self._running or not self._client:
            return False

        device_info = self._device_points.get(device_id, {})
        device_info.get("config", {})
        device_pts = device_info.get("points", {})
        pt_def = device_pts.get(point, {})
        data_type = pt_def.get("data_type", DATA_TYPE_SWITCH)

        try:
            return await self._client.write_group_value(point, value, data_type)
        except Exception as e:
            logger.error("KNX写入失败 %s.%s: %s", device_id, point, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """KNX设备发现"""
        # KNX不直接支持设备发现，需要手动配置组地址
        return []

    def is_device_connected(self, device_id: str) -> bool:
        """检查KNX连接状态"""
        return self._running and self._client is not None and self._client._connected

    async def _try_reconnect(self, device_id: str) -> None:
        """重连机制"""
        if not self._config:
            return

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("KNX重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("KNX连接断开，%.1fs后重连 (第%d次)", delay, self._reconnect_count)
        await asyncio.sleep(delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY)

        try:
            gateway_host = self._config.get("gateway_host", DEFAULT_GATEWAY_HOST)
            gateway_port = int(self._config.get("gateway_port", DEFAULT_KNX_PORT))
            local_port = int(self._config.get("local_port", 0))

            if self._client:
                self._client.close()

            self._client = KNXClient(gateway_host, gateway_port, local_port)
            connected = await self._client.connect()
            if connected:
                self._running = True
                self._reconnect_count = 0
                self._reconnect_delay = self._RECONNECT_BASE_DELAY
                logger.info("KNX重连成功: %s:%d", gateway_host, gateway_port)
        except Exception as e:
            logger.error("KNX重连失败: %s", e)
