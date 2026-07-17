"""Profinet Driver - DCP Protocol + Snap7 Integration

WARNING: This driver is a pure-Python reference implementation and has NOT been
validated against real hardware. Production use is NOT recommended without
thorough testing. This driver ONLY implements DCP (Discovery and Configuration
Protocol) device discovery natively; it does NOT implement Profinet RT/IRT
real-time data exchange. IO data read/write is bridged via the Snap7 library
(S7 communication), which is NOT native Profinet RT/IRT and cannot achieve
sub-millisecond determinism. For production Profinet RT/IRT, use a certified
Profinet controller (e.g. CodeSYS, SoftPLC with Profinet stack, or dedicated
CP cards from Siemens).

Supported Features:
- Profinet DCP (Discovery and Configuration Protocol) device discovery
- Device name configuration via DCP Set
- Snap7 bridge for S7/Profinet hybrid networks (NOT native RT/IRT)
- Basic read/write operations via Snap7 library (S7 protocol, not Profinet IO)

Known Limitations (STUB):
- read_io_data / write_io_data 仅记录 warning 并返回 None，需通过 Snap7
  bridge 间接读写，未实现 GSDML 解析与 Profinet IO Controller 栈
- 不支持 IRT (Isochronous Real-Time) 硬实时等时同步

Dependencies:
    pip install python-snap7
    Linux: apt install snap7
    Windows: Included with python-snap7
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from dataclasses import dataclass, field
from typing import Any

from edgelite.drivers.base import DriverPlugin
from edgelite.drivers.snap7_integration import (
    SNAP7_AVAILABLE,
    ProfinetSnap7Bridge,
    S7Area,
    Snap7Client,
    Snap7ConnectionInfo,
)

logger = logging.getLogger(__name__)

# Profinet 默认配置
DEFAULT_PNET_PORT = 34964
PROFINET_ETHERTYPE = 0x8892

# DCP 帧类型
DCP_HELLO = 0x0001
DCP_GET_SET = 0x0002
DCP_IDENTIFY = 0x0003
DCP_IP = 0x0004
DCP_NAME = 0x0005
DCP_LIGHT = 0x0008
DCP_RESET = 0x0009
DCP_RESPONSE = 0x000A

# DCP 选项
DCP_OPT_IP = 0x0002
DCP_OPT_DEVICE_PROPERTIES = 0x0003
DCP_OPT_DHCP = 0x0005

# DCP SubOption
DCP_SUB_IP_PARAMS = 0x0001
DCP_SUB_DEVICE_NAME = 0x0002
DCP_SUBmanufacturer = 0x0003
DCP_SUBDEVICE_ID = 0x0004
DCP_SUBOEM = 0x0005

# DCP 服务ID
DCP_SV_GET = 0x03
DCP_SV_SET = 0x04
DCP_SV_IDENTIFY = 0x05
DCP_SV_BEGIN = 0x06
DCP_SV_END = 0x07

# 以太网帧头
ETH_HEADER_SIZE = 14
VLAN_TAG_SIZE = 4


@dataclass
class ProfinetDevice:
    """Profinet设备信息"""

    device_name: str
    ip_address: str
    subnet_mask: str
    gateway: str
    mac_address: str
    vendor_id: int
    device_id: int
    device_role: str  # IO Device / IO Controller / IO Supervisor
    station_name: str
    manufacturer: str


def _parse_mac_address(mac_bytes: bytes) -> str:
    """解析MAC地址"""
    return ":".join(f"{b:02X}" for b in mac_bytes)


def _pack_mac_address(mac_str: str) -> bytes:
    """打包MAC地址"""
    return bytes(int(x, 16) for x in mac_str.split(":"))


def _build_ethernet_header(dst_mac: bytes, ethertype: int, vlan_id: int | None = None) -> bytes:
    """构建以太网帧头"""
    src_mac = bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00])  # 网关MAC占位

    if vlan_id is not None:
        # VLAN tagged frame
        header = dst_mac + src_mac + bytes([0x81, 0x00])  # TPID
        vlan_info = (vlan_id & 0x0FFF) | 0x2000  # PCP=3, DEI=0
        header += struct.pack(">H", vlan_info)
        return header

    return dst_mac + src_mac + struct.pack(">H", ethertype)


def _build_dcp_header(
    frame_id: int,
    service_id: int,
    service_type: int,
    xid: int,
    reserved: int = 0,
) -> bytes:
    """构建DCP帧头"""
    header = struct.pack(">H", frame_id)  # FrameID
    header += bytes([service_id, service_type])  # ServiceID, ServiceType
    header += struct.pack(">I", xid)  # Xid (Transaction ID)
    header += struct.pack(">H", reserved)  # Reserved
    return header


def _build_dcp_option(option: int, suboption: int, block_info: int = 0) -> bytes:
    """构建DCP选项"""
    option_block = struct.pack(">HH", option, suboption)  # Option, SubOption
    return option_block


def _parse_dcp_response(data: bytes) -> ProfinetDevice | None:
    """解析DCP响应"""
    try:
        if len(data) < 24:
            return None

        # 跳过以太网头
        offset = ETH_HEADER_SIZE

        # 检查VLAN标签
        if len(data) > offset and data[offset : offset + 2] == b"\x81\x00":
            offset += VLAN_TAG_SIZE

        # 解析DCP头
        struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2

        data[offset]
        offset += 1
        data[offset]
        offset += 1

        struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4

        struct.unpack(">H", data[offset : offset + 2])[0]
        offset += 2

        # 解析DCP选项
        device_name = ""
        ip_address = "0.0.0.0"
        subnet_mask = "255.255.255.0"
        gateway = "0.0.0.0"
        mac_address = ""
        vendor_id = 0
        device_id = 0
        device_role = "IO Device"
        manufacturer = ""

        while offset < len(data) - 4:
            option = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2
            suboption = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2
            block_len = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2

            if offset + block_len > len(data):
                break

            block_data = data[offset : offset + block_len]
            offset += block_len

            if option == DCP_OPT_IP and suboption == DCP_SUB_IP_PARAMS:
                # IP参数
                ip_bytes = block_data[0:4]
                ip_address = ".".join(str(b) for b in ip_bytes)
                mask_bytes = block_data[4:8]
                subnet_mask = ".".join(str(b) for b in mask_bytes)
                gw_bytes = block_data[8:12]
                gateway = ".".join(str(b) for b in gw_bytes)

            elif option == DCP_OPT_DEVICE_PROPERTIES:
                if suboption == DCP_SUB_DEVICE_NAME:
                    # 设备名称 (null-terminated string)
                    device_name = block_data.rstrip(b"\x00").decode("utf-8", errors="ignore")
                elif suboption == DCP_SUBmanufacturer:
                    # 制造商信息
                    manufacturer = block_data.rstrip(b"\x00").decode("utf-8", errors="ignore")
                elif suboption == DCP_SUBDEVICE_ID:
                    # 设备ID
                    if len(block_data) >= 4:
                        vendor_id = struct.unpack(">H", block_data[0:2])[0]
                        device_id = struct.unpack(">H", block_data[2:4])[0]

        # 获取MAC地址
        if len(data) >= 6:
            mac_address = _parse_mac_address(data[0:6])

        return ProfinetDevice(
            device_name=device_name,
            ip_address=ip_address,
            subnet_mask=subnet_mask,
            gateway=gateway,
            mac_address=mac_address,
            vendor_id=vendor_id,
            device_id=device_id,
            device_role=device_role,
            station_name=device_name,
            manufacturer=manufacturer,
        )

    except Exception as e:
        logger.debug("DCP响应解析失败: %s", e)
        return None


class ProfinetClient:
    """Profinet DCP 客户端封装"""

    def __init__(self, interface_ip: str = "0.0.0.0", port: int = DEFAULT_PNET_PORT):
        self._interface_ip = interface_ip
        self._port = port
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _ProfinetProtocol | None = None
        self._xid = 0
        self._discovered_devices: dict[str, ProfinetDevice] = {}

    async def connect(self) -> bool:
        """建立Profinet连接"""
        try:
            loop = asyncio.get_running_loop()
            self._protocol = _ProfinetProtocol(self)

            self._transport, _ = await loop.create_datagram_endpoint(
                lambda: self._protocol,
                local_addr=(self._interface_ip, self._port),
                allow_broadcast=True,
            )

            logger.info("Profinet DCP connection established: %s:%d", self._interface_ip, self._port)
            return True

        except Exception as e:
            logger.error("Profinet connection failed: %s", e)
            return False

    def close(self) -> None:
        """关闭连接"""
        if self._transport:
            self._transport.close()
            self._transport = None

    async def discover_devices(self, timeout: float = 5.0) -> list[ProfinetDevice]:
        """发现Profinet设备

        发送广播Identify请求，收集所有响应设备。
        """
        self._discovered_devices.clear()

        # 构造DCP Identify All请求
        dst_mac = bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])

        # DCP Header
        dcp_header = _build_dcp_header(
            frame_id=DCP_IDENTIFY,
            service_id=DCP_SV_IDENTIFY,
            service_type=0x03,  # Request without response
            xid=self._get_next_xid(),
        )

        # DCP Option: Device Properties (all suboptions)
        dcp_payload = struct.pack(">HH", DCP_OPT_DEVICE_PROPERTIES, 0xFF)  # All suboptions

        # 完整帧
        frame = _build_ethernet_header(dst_mac, PROFINET_ETHERTYPE) + dcp_header + dcp_payload

        # 发送广播
        if self._transport:
            self._transport.sendto(frame, ("<broadcast>", self._port))

        # 等待响应
        await asyncio.sleep(timeout)

        return list(self._discovered_devices.values())

    async def get_device_by_name(self, device_name: str, timeout: float = 3.0) -> ProfinetDevice | None:
        """通过设备名称获取设备信息"""
        self._discovered_devices.clear()

        # 构造DCP Identify请求
        dst_mac = bytes([0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])

        dcp_header = _build_dcp_header(
            frame_id=DCP_IDENTIFY,
            service_id=DCP_SV_IDENTIFY,
            service_type=0x03,
            xid=self._get_next_xid(),
        )

        # Option: Device Name
        name_bytes = device_name.encode("utf-8") + b"\x00"
        dcp_payload = struct.pack(">HH", DCP_OPT_DEVICE_PROPERTIES, DCP_SUB_DEVICE_NAME)
        dcp_payload += struct.pack(">H", len(name_bytes)) + name_bytes

        frame = _build_ethernet_header(dst_mac, PROFINET_ETHERTYPE) + dcp_header + dcp_payload

        if self._transport:
            self._transport.sendto(frame, ("<broadcast>", self._port))

        await asyncio.sleep(timeout)

        for device in self._discovered_devices.values():
            if device.device_name == device_name:
                return device

        return None

    async def read_io_data(
        self,
        device: ProfinetDevice,
        slot: int,
        subslot: int,
        data_size: int,
    ) -> bytes | None:
        """Read IO data (simplified implementation, actual requires RPC)

        Note: Full Profinet RT/IRT IO data exchange requires:
        - Specialized network card (e.g., Intel i210, i225)
        - Profinet stack library (e.g., p-net, snap7)
        - Or dedicated Profinet hardware

        For full IO functionality, consider using the S7 driver with snap7,
        which provides similar capabilities for Siemens devices.
        """
        logger.warning("Profinet IO read requires snap7 library or specialized hardware support")
        # TODO(协议驱动-Profinet, 负责人: @iot-driver-team, 计划版本: v2.0.0):
        #   当前为 STUB 实现，未实现原生 Profinet RT/IRT IO 数据交换。
        #   原生 RT/IRT 需要：专用网卡(Intel i210/i225)、Profinet 栈(p-net/cap)、
        #   GSDML 设备描述文件解析、IO Controller 状态机(OP->SAFE_OP->OP)、
        #   周期性 PDO 交换与 ACW Watchdog。
        #   临时方案：通过 Snap7 bridge (snap7_integration.py) 间接读写 S7 数据区，
        #   但这不是原生 Profinet IO，无法满足 RT/IRT 实时性。
        return None

    async def write_io_data(
        self,
        device: ProfinetDevice,
        slot: int,
        subslot: int,
        data: bytes,
    ) -> bool:
        """Write IO data

        Note: Full Profinet RT/IRT IO data exchange requires:
        - Specialized network card (e.g., Intel i210, i225)
        - Profinet stack library (e.g., p-net, snap7)
        - Or dedicated Profinet hardware

        For full IO functionality, consider using the S7 driver with snap7.
        """
        logger.warning("Profinet IO write requires snap7 library or specialized hardware support")
        # TODO(协议驱动-Profinet, 负责人: @iot-driver-team, 计划版本: v2.0.0):
        #   当前为 STUB 实现，未实现原生 Profinet RT/IRT IO 写入。参见 read_io_data 的 TODO。
        return False

    def handle_packet(self, data: bytes) -> None:
        """处理接收到的数据包"""
        device = _parse_dcp_response(data)
        if device:
            key = device.mac_address
            self._discovered_devices[key] = device
            logger.info("Discovered Profinet device: %s @ %s", device.device_name, device.ip_address)

    def _get_next_xid(self) -> int:
        """获取下一个事务ID"""
        self._xid = (self._xid + 1) & 0xFFFFFFFF
        return self._xid


class _ProfinetProtocol(asyncio.DatagramProtocol):
    """Profinet UDP协议处理器"""

    def __init__(self, client: ProfinetClient):
        self._client = client

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        logger.debug("Profinet UDP连接已建立")

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._client.handle_packet(data)

    def error_received(self, exc: Exception) -> None:
        logger.warning("Profinet UDP error: %s", exc)


class ProfinetDriver(DriverPlugin):
    """Profinet 协议驱动 - DCP + Snap7集成

    配置参数:
        interface_ip: 本机网卡IP (用于发送广播)
        port: Profinet端口 (默认34964)
        enable_snap7: 启用Snap7进行IO数据交换 (默认False)
        snap7_plc_ip: Snap7连接的PLC IP (当enable_snap7=True时)
        snap7_rack: Snap7连接机架号 (默认0)
        snap7_slot: Snap7连接槽号 (默认1)
    """

    plugin_name = "profinet"
    plugin_version = "1.1.0"
    supported_protocols = ("profinet", "profinet_dcp", "pn")  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "Profinet industrial Ethernet protocol with Snap7 integration for IO data exchange",
        "fields": [
            {
                "name": "interface_ip",
                "type": "string",
                "label": "Interface IP",
                "description": "Local network interface IP for sending broadcasts",
                "default": "0.0.0.0",
            },
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "Profinet DCP port (default 34964)",
                "default": 34964,
            },
            {
                "name": "enable_snap7",
                "type": "boolean",
                "label": "Enable Snap7 IO",
                "description": "Enable Snap7 for IO data exchange (requires python-snap7)",
                "default": False,
            },
            {
                "name": "snap7_plc_ip",
                "type": "string",
                "label": "Snap7 PLC IP",
                "description": "PLC IP for Snap7 connection (when Snap7 enabled)",
                "default": "",
            },
            {
                "name": "snap7_rack",
                "type": "integer",
                "label": "Snap7 Rack",
                "description": "PLC rack number for Snap7",
                "default": 0,
            },
            {
                "name": "snap7_slot",
                "type": "integer",
                "label": "Snap7 Slot",
                "description": "PLC slot number for Snap7",
                "default": 1,
            },
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        super().__init__()  # FIXED-P0: 必须调用基类初始化
        self._running = False
        self._client: ProfinetClient | None = None
        self._snap7_client: Snap7Client | None = None
        self._snap7_bridge: ProfinetSnap7Bridge | None = None
        self._config: dict = {}
        self._device_points: dict[str, dict] = {}
        self._devices: dict[str, ProfinetDevice] = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._snap7_enabled: bool = False

    async def start(self, config: dict) -> None:
        """启动Profinet驱动"""
        self._config = config
        interface_ip = config.get("interface_ip", "0.0.0.0")
        port = int(config.get("port", DEFAULT_PNET_PORT))
        self._snap7_enabled = config.get("enable_snap7", False)

        self._client = ProfinetClient(interface_ip, port)

        # 初始化Snap7客户端
        if self._snap7_enabled and SNAP7_AVAILABLE:
            try:
                self._snap7_client = Snap7Client()
                plcip = config.get("snap7_plc_ip", "")
                rack = int(config.get("snap7_rack", 0))
                slot = int(config.get("snap7_slot", 1))

                if plcip:
                    info = Snap7ConnectionInfo(plcip, rack, slot)
                    if self._snap7_client.connect(info):
                        self._snap7_bridge = ProfinetSnap7Bridge()
                        self._snap7_bridge._s7_client = self._snap7_client
                        logger.info("Profinet Snap7 bridge connected: %s", plcip)
                    else:
                        logger.warning("Profinet Snap7 bridge connection failed")
                        self._snap7_client = None
                else:
                    logger.warning("Profinet Snap7 enabled but no PLC IP configured")
            except Exception as e:
                logger.warning("Profinet Snap7 initialization failed: %s", e)
                self._snap7_client = None

        try:
            connected = await self._client.connect()
            if connected:
                self._running = True
                self._reconnect_count = 0

                mode = "Snap7 IO" if self._snap7_enabled else "DCP Discovery"
                logger.info("Profinet driver started: %s:%d (%s)", interface_ip, port, mode)
            else:
                logger.error("Profinet driver connection failed")
        except Exception as e:
            logger.error("Profinet driver start failed: %s", e)
            raise

    async def stop(self) -> None:
        """停止Profinet驱动"""
        self._running = False

        if self._snap7_bridge:
            self._snap7_bridge.disconnect()
            self._snap7_bridge = None

        if self._snap7_client:
            self._snap7_client.destroy()
            self._snap7_client = None

        if self._client:
            self._client.close()
            self._client = None
        await super().stop()
        logger.info("Profinet driver stopped")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取Profinet测点值

        测点地址格式:
            "slot:subslot.index" 如 "1:1.0" (slot 1, subslot 1, index 0)
            "db.db_number.start" 如 "db.1.0" (S7 DB访问)

        当启用Snap7时，优先使用Snap7进行IO数据交换。
        """
        if not self._running or not self._client:
            return {}

        device_info = self._device_points.get(device_id, {})
        device_info.get("config", {})

        result = {}
        for point_addr in points:
            try:
                # 优先使用Snap7读取
                if self._snap7_bridge and self._snap7_bridge.is_connected:
                    value = await self._read_via_snap7(point_addr)
                    result[point_addr] = value
                else:
                    # 使用标准方式
                    device_name = device_info.get("device_name", device_id)
                    device = self._devices.get(device_name)

                    if device:
                        if "status" in point_addr.lower():
                            result[point_addr] = 1  # Online
                        elif "ip" in point_addr.lower():
                            result[point_addr] = device.ip_address
                        else:
                            result[point_addr] = 0
                    else:
                        result[point_addr] = None

            except Exception as e:
                logger.warning("Profinet read failed %s.%s: %s", device_id, point_addr, e)
                result[point_addr] = None

        return result

    async def _read_via_snap7(self, address: str) -> Any:
        """通过Snap7读取数据

        地址格式:
            "io:slot:subslot:index:size" - Profinet IO数据
            "db:dbnum:start:size" - S7 DB数据
            "pa:start:size" - Process Outputs
            "pe:start:size" - Process Inputs
        """
        if not self._snap7_bridge:
            return None

        parts = address.lower().split(":")

        try:
            if parts[0] == "io" and len(parts) >= 5:
                # Profinet IO
                slot = int(parts[1])
                subslot = int(parts[2])
                index = int(parts[3])
                size = int(parts[4])
                data = self._snap7_bridge.read_io_data(slot, subslot, index, size)
                return data.hex() if data else None

            elif parts[0] == "db" and len(parts) >= 3:
                # S7 DB
                dbnum = int(parts[1])
                start = int(parts[2])
                size = int(parts[3]) if len(parts) > 3 else 2

                if size == 2:
                    value = self._snap7_client.read_db_int16(dbnum, start)
                else:
                    data = self._snap7_client.read_area(S7Area.DB, dbnum, start, size)
                    value = data.hex() if data else None
                return value

            elif parts[0] == "pa":
                # Process Outputs
                start = int(parts[1])
                size = int(parts[2]) if len(parts) > 2 else 2
                data = self._snap7_client.read_area(S7Area.PA, 0, start, size)
                return data.hex() if data else None

            elif parts[0] == "pe":
                # Process Inputs
                start = int(parts[1])
                size = int(parts[2]) if len(parts) > 2 else 2
                data = self._snap7_client.read_area(S7Area.PE, 0, start, size)
                return data.hex() if data else None

            else:
                logger.warning("Unknown Snap7 address format: %s", address)
                return None

        except (ValueError, IndexError) as e:
            logger.warning("Snap7 address parse error: %s - %s", address, e)
            return None

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入Profinet测点值

        当启用Snap7时，支持写入IO数据。
        """
        if not self._running:
            return False

        # Snap7写入
        if self._snap7_bridge and self._snap7_bridge.is_connected:
            try:
                return await self._write_via_snap7(point, value)
            except Exception as e:
                logger.error("Profinet write failed %s.%s: %s", device_id, point, e)
                return False

        logger.warning("Profinet write requires Snap7 enabled")
        return False

    async def _write_via_snap7(self, address: str, value: Any) -> bool:
        """通过Snap7写入数据"""
        if not self._snap7_bridge:
            return False

        parts = address.lower().split(":")

        try:
            if parts[0] == "db" and len(parts) >= 3:
                dbnum = int(parts[1])
                start = int(parts[2])
                data = struct.pack(">h", int(value))
                return self._snap7_client.write_area(S7Area.DB, dbnum, start, data)

            return False

        except (ValueError, struct.error) as e:
            logger.warning("Snap7 write error: %s - %s", address, e)
            return False

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加Profinet设备"""
        device_name = config.get("device_name", device_id)
        pn_slot = config.get("pn_slot", 1)
        pn_subslot = config.get("pn_subslot", 1)
        db_number = config.get("db_number", 1)
        db_start = config.get("db_start", 0)

        self._device_points[device_id] = {
            "config": config,
            "points": {p.get("name", ""): p for p in points if p.get("name")},
            "device_name": device_name,
        }

        # 建立Profinet到S7 DB的映射
        if self._snap7_bridge and config.get("map_to_db", False):
            for i, _pt in enumerate(points):
                self._snap7_bridge.map_pn_to_db(pn_slot, pn_subslot, i, db_number, db_start + i * 2, 2)
                logger.debug("Mapped device %s point %d to DB%d.%d", device_id, i, db_number, db_start + i * 2)

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        device_info = self._device_points.pop(device_id, None)
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        if device_info:
            device_name = device_info.get("device_name", "")
            self._devices.pop(device_name, None)
        logger.info("Profinet device removed: %s", device_id)

    async def discover_devices(self, config: dict) -> list[dict]:
        """Profinet设备发现 - 发送DCP Identify广播"""
        if not self._client:
            return []

        try:
            devices = await self._client.discover_devices(timeout=5.0)
            results = []

            for device in devices:
                self._devices[device.device_name] = device
                results.append(
                    {
                        "device_id": device.device_id,
                        "device_name": device.device_name,
                        "ip_address": device.ip_address,
                        "mac_address": device.mac_address,
                        "vendor_id": device.vendor_id,
                        "manufacturer": device.manufacturer,
                        "type": "profinet",
                    }
                )

            return results

        except Exception as e:
            logger.error("Profinet device discovery failed: %s", e)
            return []

    def is_device_connected(self, device_id: str) -> bool:
        """检查Profinet设备连接状态"""
        if not self._running:
            return False

        device_info = self._device_points.get(device_id, {})
        device_name = device_info.get("device_name", device_id)

        return device_name in self._devices

    async def _try_reconnect(self, device_id: str) -> None:
        """重连机制"""
        if not self._config:
            return

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("Profinet reconnect abandoned: %s", device_id)
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("Profinet connection lost, reconnecting in %.1fs (attempt %d)", delay, self._reconnect_count)
        await asyncio.sleep(delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._RECONNECT_MAX_DELAY)

        try:
            interface_ip = self._config.get("interface_ip", "0.0.0.0")
            port = int(self._config.get("port", DEFAULT_PNET_PORT))

            if self._client:
                self._client.close()

            self._client = ProfinetClient(interface_ip, port)
            connected = await self._client.connect()

            if connected:
                self._running = True
                self._reconnect_count = 0
                self._reconnect_delay = self._RECONNECT_BASE_DELAY
                logger.info("Profinet reconnected successfully")

        except Exception as e:
            logger.error("Profinet reconnect failed: %s", e)
