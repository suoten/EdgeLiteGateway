from __future__ import annotations

import struct
import sys

sys.path.insert(0, "src")

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from edgelite.drivers import profinet
from edgelite.drivers.profinet import (
    DCP_GET_SET,
    DCP_HELLO,
    DCP_IDENTIFY,
    DCP_IP,
    DCP_LIGHT,
    DCP_NAME,
    DCP_OPT_DEVICE_PROPERTIES,
    DCP_OPT_DHCP,
    DCP_OPT_IP,
    DCP_RESET,
    DCP_RESPONSE,
    DCP_SUB_DEVICE_NAME,
    DCP_SUB_IP_PARAMS,
    DCP_SUBDEVICE_ID,
    DCP_SUBOEM,
    DCP_SV_BEGIN,
    DCP_SV_END,
    DCP_SV_GET,
    DCP_SV_IDENTIFY,
    DCP_SV_SET,
    DEFAULT_PNET_PORT,
    ETH_HEADER_SIZE,
    PROFINET_ETHERTYPE,
    VLAN_TAG_SIZE,
    DCP_SUBmanufacturer,
    ProfinetClient,
    ProfinetDevice,
    ProfinetDriver,
    _build_dcp_header,
    _build_dcp_option,
    _build_ethernet_header,
    _pack_mac_address,
    _parse_dcp_response,
    _parse_mac_address,
    _ProfinetProtocol,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_device(**kwargs) -> ProfinetDevice:
    """Create a ProfinetDevice with sensible defaults."""
    defaults = dict(
        device_name="test-device",
        ip_address="192.168.1.10",
        subnet_mask="255.255.255.0",
        gateway="192.168.1.1",
        mac_address="00:11:22:33:44:55",
        vendor_id=42,
        device_id=100,
        device_role="IO Device",
        station_name="test-device",
        manufacturer="TestVendor",
    )
    defaults.update(kwargs)
    return ProfinetDevice(**defaults)


def _make_driver(running: bool = False, snap7_enabled: bool = False) -> ProfinetDriver:
    """Create a ProfinetDriver with base-class attributes manually set.

    ProfinetDriver.__init__ does NOT call super().__init__(), so _health_stats
    and _offline_since are missing. We set them here for testability.
    """
    d = ProfinetDriver()
    # Mimic DriverPlugin.__init__ attributes needed by remove_device etc.
    d._health_stats = {}
    d._offline_since = {}
    d._running = running
    d._snap7_enabled = snap7_enabled
    return d


def _build_dcp_response_bytes(
    mac: bytes = b"\x00\x11\x22\x33\x44\x55",
    ip: str = "192.168.1.10",
    mask: str = "255.255.255.0",
    gateway: str = "192.168.1.1",
    device_name: str = "test-device",
    manufacturer: str = "TestVendor",
    vendor_id: int = 42,
    device_id: int = 100,
    include_vlan: bool = False,
) -> bytes:
    """Build a synthetic DCP response byte stream for testing _parse_dcp_response.

    Note: _parse_dcp_response reads the MAC from data[0:6] (destination MAC position),
    so we place the device MAC there. The VLAN tag detection checks data[14:16] for
    the TPID (0x8100), which means the frame structure expected by the parser is:
    dst(6) + src(6) + ethertype(2) + [TPID(2) + TCI(2)] + DCP header + options
    """
    # Parser reads MAC from data[0:6], so put the device MAC in the first 6 bytes
    dst_mac = mac
    src_mac = b"\x00" * 6
    header = dst_mac + src_mac + struct.pack(">H", PROFINET_ETHERTYPE)
    if include_vlan:
        # Parser checks data[14:16] for TPID after the 14-byte ethernet header
        header += b"\x81\x00" + struct.pack(">H", 0x2001)

    # DCP header: frame_id(2) + service_id(1) + service_type(1) + xid(4) + reserved(2) = 10 bytes
    dcp_header = struct.pack(">H", DCP_IDENTIFY)
    dcp_header += bytes([DCP_SV_IDENTIFY, 0x01])
    dcp_header += struct.pack(">I", 1)
    dcp_header += struct.pack(">H", 0)

    # DCP options
    options = b""

    # IP params
    ip_bytes = bytes(int(x) for x in ip.split("."))
    mask_bytes = bytes(int(x) for x in mask.split("."))
    gw_bytes = bytes(int(x) for x in gateway.split("."))
    ip_block = ip_bytes + mask_bytes + gw_bytes
    options += struct.pack(">HH", DCP_OPT_IP, DCP_SUB_IP_PARAMS)
    options += struct.pack(">H", len(ip_block))
    options += ip_block

    # Device name
    name_bytes = device_name.encode("utf-8") + b"\x00"
    options += struct.pack(">HH", DCP_OPT_DEVICE_PROPERTIES, DCP_SUB_DEVICE_NAME)
    options += struct.pack(">H", len(name_bytes))
    options += name_bytes

    # Manufacturer
    mfr_bytes = manufacturer.encode("utf-8") + b"\x00"
    options += struct.pack(">HH", DCP_OPT_DEVICE_PROPERTIES, DCP_SUBmanufacturer)
    options += struct.pack(">H", len(mfr_bytes))
    options += mfr_bytes

    # Device ID
    id_block = struct.pack(">HH", vendor_id, device_id)
    options += struct.pack(">HH", DCP_OPT_DEVICE_PROPERTIES, DCP_SUBDEVICE_ID)
    options += struct.pack(">H", len(id_block))
    options += id_block

    return header + dcp_header + options


# ── Test Constants ────────────────────────────────────────────────────────────


class TestConstants:
    """Test Profinet protocol constants are defined with correct values."""

    def test_default_port(self):
        assert DEFAULT_PNET_PORT == 34964

    def test_ethertype(self):
        assert PROFINET_ETHERTYPE == 0x8892

    def test_dcp_frame_types(self):
        assert DCP_HELLO == 0x0001
        assert DCP_GET_SET == 0x0002
        assert DCP_IDENTIFY == 0x0003
        assert DCP_IP == 0x0004
        assert DCP_NAME == 0x0005
        assert DCP_LIGHT == 0x0008
        assert DCP_RESET == 0x0009
        assert DCP_RESPONSE == 0x000A

    def test_dcp_options(self):
        assert DCP_OPT_IP == 0x0002
        assert DCP_OPT_DEVICE_PROPERTIES == 0x0003
        assert DCP_OPT_DHCP == 0x0005

    def test_dcp_suboptions(self):
        assert DCP_SUB_IP_PARAMS == 0x0001
        assert DCP_SUB_DEVICE_NAME == 0x0002
        assert DCP_SUBmanufacturer == 0x0003
        assert DCP_SUBDEVICE_ID == 0x0004
        assert DCP_SUBOEM == 0x0005

    def test_dcp_service_ids(self):
        assert DCP_SV_GET == 0x03
        assert DCP_SV_SET == 0x04
        assert DCP_SV_IDENTIFY == 0x05
        assert DCP_SV_BEGIN == 0x06
        assert DCP_SV_END == 0x07

    def test_ethernet_sizes(self):
        assert ETH_HEADER_SIZE == 14
        assert VLAN_TAG_SIZE == 4


# ── Test ProfinetDevice ───────────────────────────────────────────────────────


class TestProfinetDevice:
    """Test ProfinetDevice dataclass."""

    def test_default_construction(self):
        d = ProfinetDevice(
            device_name="dev1",
            ip_address="10.0.0.1",
            subnet_mask="255.0.0.0",
            gateway="10.0.0.254",
            mac_address="AA:BB:CC:DD:EE:FF",
            vendor_id=1,
            device_id=2,
            device_role="IO Controller",
            station_name="station1",
            manufacturer="Siemens",
        )
        assert d.device_name == "dev1"
        assert d.ip_address == "10.0.0.1"
        assert d.subnet_mask == "255.0.0.0"
        assert d.gateway == "10.0.0.254"
        assert d.mac_address == "AA:BB:CC:DD:EE:FF"
        assert d.vendor_id == 1
        assert d.device_id == 2
        assert d.device_role == "IO Controller"
        assert d.station_name == "station1"
        assert d.manufacturer == "Siemens"

    def test_field_assignment(self):
        d = _make_device()
        d.device_name = "changed"
        assert d.device_name == "changed"

    def test_equality(self):
        d1 = _make_device()
        d2 = _make_device()
        assert d1 == d2


# ── Test _parse_mac_address ───────────────────────────────────────────────────


class TestParseMacAddress:
    """Test _parse_mac_address helper."""

    def test_standard_mac(self):
        assert _parse_mac_address(b"\x00\x11\x22\x33\x44\x55") == "00:11:22:33:44:55"

    def test_all_zeros(self):
        assert _parse_mac_address(b"\x00\x00\x00\x00\x00\x00") == "00:00:00:00:00:00"

    def test_all_ff(self):
        assert _parse_mac_address(b"\xff\xff\xff\xff\xff\xff") == "FF:FF:FF:FF:FF:FF"

    def test_uppercase_hex(self):
        result = _parse_mac_address(b"\xab\xcd\xef\x01\x23\x45")
        assert result == "AB:CD:EF:01:23:45"

    def test_single_byte(self):
        assert _parse_mac_address(b"\x01") == "01"

    def test_empty_bytes(self):
        assert _parse_mac_address(b"") == ""


# ── Test _pack_mac_address ────────────────────────────────────────────────────


class TestPackMacAddress:
    """Test _pack_mac_address helper."""

    def test_standard_mac(self):
        assert _pack_mac_address("00:11:22:33:44:55") == b"\x00\x11\x22\x33\x44\x55"

    def test_all_zeros(self):
        assert _pack_mac_address("00:00:00:00:00:00") == b"\x00\x00\x00\x00\x00\x00"

    def test_all_ff(self):
        assert _pack_mac_address("FF:FF:FF:FF:FF:FF") == b"\xff\xff\xff\xff\xff\xff"

    def test_lowercase_hex(self):
        assert _pack_mac_address("ab:cd:ef:01:23:45") == b"\xab\xcd\xef\x01\x23\x45"

    def test_roundtrip(self):
        original = "AA:BB:CC:DD:EE:FF"
        packed = _pack_mac_address(original)
        assert _parse_mac_address(packed) == original

    def test_single_byte_mac(self):
        assert _pack_mac_address("01") == b"\x01"


# ── Test _build_ethernet_header ───────────────────────────────────────────────


class TestBuildEthernetHeader:
    """Test _build_ethernet_header helper."""

    def test_without_vlan(self):
        dst = b"\xff" * 6
        header = _build_ethernet_header(dst, PROFINET_ETHERTYPE)
        # dst(6) + src(6) + ethertype(2) = 14
        assert len(header) == 14
        assert header[:6] == dst
        # src_mac is all zeros
        assert header[6:12] == b"\x00" * 6
        assert struct.unpack(">H", header[12:14])[0] == PROFINET_ETHERTYPE

    def test_with_vlan(self):
        dst = b"\xff" * 6
        header = _build_ethernet_header(dst, PROFINET_ETHERTYPE, vlan_id=1)
        # dst(6) + src(6) + TPID(2) + VLAN(2) = 16
        assert len(header) == 16
        assert header[:6] == dst
        assert header[6:12] == b"\x00" * 6
        # TPID
        assert header[12:14] == b"\x81\x00"
        # VLAN info: vlan_id=1 with PCP=3, DEI=0 => 0x2001
        assert struct.unpack(">H", header[14:16])[0] == 0x2001

    def test_vlan_id_masking(self):
        """vlan_id is masked with 0x0FFF."""
        header = _build_ethernet_header(b"\xff" * 6, PROFINET_ETHERTYPE, vlan_id=0xFFF)
        vlan_info = struct.unpack(">H", header[14:16])[0]
        # 0xFFF & 0x0FFF = 0xFFF, | 0x2000 = 0x2FFF
        assert vlan_info == 0x2FFF

    def test_vlan_id_zero(self):
        header = _build_ethernet_header(b"\xff" * 6, PROFINET_ETHERTYPE, vlan_id=0)
        vlan_info = struct.unpack(">H", header[14:16])[0]
        assert vlan_info == 0x2000  # PCP=3

    def test_src_mac_is_zeros(self):
        header = _build_ethernet_header(b"\x01\x02\x03\x04\x05\x06", 0x1234)
        assert header[6:12] == b"\x00\x00\x00\x00\x00\x00"

    def test_custom_ethertype(self):
        header = _build_ethernet_header(b"\xff" * 6, 0x0800)
        assert struct.unpack(">H", header[12:14])[0] == 0x0800


# ── Test _build_dcp_header ────────────────────────────────────────────────────


class TestBuildDcpHeader:
    """Test _build_dcp_header helper."""

    def test_basic_header(self):
        header = _build_dcp_header(
            frame_id=DCP_IDENTIFY,
            service_id=DCP_SV_IDENTIFY,
            service_type=0x03,
            xid=1,
        )
        # frame_id(2) + service_id(1) + service_type(1) + xid(4) + reserved(2) = 10
        assert len(header) == 10
        assert struct.unpack(">H", header[0:2])[0] == DCP_IDENTIFY
        assert header[2] == DCP_SV_IDENTIFY
        assert header[3] == 0x03
        assert struct.unpack(">I", header[4:8])[0] == 1
        assert struct.unpack(">H", header[8:10])[0] == 0

    def test_with_reserved(self):
        header = _build_dcp_header(
            frame_id=DCP_GET_SET,
            service_id=DCP_SV_SET,
            service_type=0x00,
            xid=100,
            reserved=0xFFFF,
        )
        assert struct.unpack(">H", header[0:2])[0] == DCP_GET_SET
        assert header[2] == DCP_SV_SET
        assert struct.unpack(">I", header[4:8])[0] == 100
        assert struct.unpack(">H", header[8:10])[0] == 0xFFFF

    def test_xid_zero(self):
        header = _build_dcp_header(DCP_HELLO, DCP_SV_BEGIN, 0x01, 0)
        assert struct.unpack(">I", header[4:8])[0] == 0

    def test_xid_max(self):
        header = _build_dcp_header(DCP_HELLO, DCP_SV_BEGIN, 0x01, 0xFFFFFFFF)
        assert struct.unpack(">I", header[4:8])[0] == 0xFFFFFFFF


# ── Test _build_dcp_option ────────────────────────────────────────────────────


class TestBuildDcpOption:
    """Test _build_dcp_option helper."""

    def test_basic_option(self):
        result = _build_dcp_option(DCP_OPT_DEVICE_PROPERTIES, DCP_SUB_DEVICE_NAME)
        assert len(result) == 4
        assert struct.unpack(">H", result[0:2])[0] == DCP_OPT_DEVICE_PROPERTIES
        assert struct.unpack(">H", result[2:4])[0] == DCP_SUB_DEVICE_NAME

    def test_ip_option(self):
        result = _build_dcp_option(DCP_OPT_IP, DCP_SUB_IP_PARAMS)
        assert struct.unpack(">H", result[0:2])[0] == DCP_OPT_IP
        assert struct.unpack(">H", result[2:4])[0] == DCP_SUB_IP_PARAMS

    def test_with_block_info(self):
        """block_info parameter is accepted but not used in output."""
        result = _build_dcp_option(DCP_OPT_IP, DCP_SUB_IP_PARAMS, block_info=0x01)
        assert len(result) == 4  # block_info not packed

    def test_all_suboptions(self):
        for sub in [DCP_SUB_DEVICE_NAME, DCP_SUBmanufacturer, DCP_SUBDEVICE_ID, DCP_SUBOEM]:
            result = _build_dcp_option(DCP_OPT_DEVICE_PROPERTIES, sub)
            assert struct.unpack(">H", result[2:4])[0] == sub


# ── Test _parse_dcp_response ──────────────────────────────────────────────────


class TestParseDcpResponse:
    """Test _parse_dcp_response with various inputs."""

    def test_short_data_returns_none(self):
        assert _parse_dcp_response(b"\x00" * 10) is None

    def test_empty_data_returns_none(self):
        assert _parse_dcp_response(b"") is None

    def test_minimal_valid_data(self):
        """Data with exactly 24 bytes but no options."""
        data = b"\x00" * 24
        result = _parse_dcp_response(data)
        # With 24 bytes: offset starts at 14 (ETH_HEADER_SIZE)
        # Then reads 2+1+1+4+2 = 10 bytes for DCP header => offset = 24
        # While loop: offset(24) < len(data)-4(20) => False, no options parsed
        # MAC: data[0:6] => "00:00:00:00:00:00"
        assert result is not None
        assert result.mac_address == "00:00:00:00:00:00"
        assert result.ip_address == "0.0.0.0"
        assert result.subnet_mask == "255.255.255.0"
        assert result.gateway == "0.0.0.0"
        assert result.device_name == ""
        assert result.manufacturer == ""
        assert result.vendor_id == 0
        assert result.device_id == 0
        assert result.device_role == "IO Device"
        assert result.station_name == ""

    def test_full_response(self):
        data = _build_dcp_response_bytes()
        result = _parse_dcp_response(data)
        assert result is not None
        assert result.device_name == "test-device"
        assert result.ip_address == "192.168.1.10"
        assert result.subnet_mask == "255.255.255.0"
        assert result.gateway == "192.168.1.1"
        assert result.mac_address == "00:11:22:33:44:55"
        assert result.vendor_id == 42
        assert result.device_id == 100
        assert result.manufacturer == "TestVendor"
        assert result.device_role == "IO Device"
        assert result.station_name == "test-device"

    def test_response_with_vlan(self):
        data = _build_dcp_response_bytes(include_vlan=True)
        result = _parse_dcp_response(data)
        assert result is not None
        assert result.device_name == "test-device"
        assert result.ip_address == "192.168.1.10"
        assert result.mac_address == "00:11:22:33:44:55"

    def test_mac_from_first_six_bytes(self):
        data = _build_dcp_response_bytes(mac=b"\xaa\xbb\xcc\xdd\xee\xff")
        result = _parse_dcp_response(data)
        assert result is not None
        assert result.mac_address == "AA:BB:CC:DD:EE:FF"

    def test_ip_option_parsing(self):
        data = _build_dcp_response_bytes(ip="10.20.30.40", mask="255.255.0.0", gateway="10.20.0.1")
        result = _parse_dcp_response(data)
        assert result.ip_address == "10.20.30.40"
        assert result.subnet_mask == "255.255.0.0"
        assert result.gateway == "10.20.0.1"

    def test_device_name_parsing(self):
        data = _build_dcp_response_bytes(device_name="my-plc")
        result = _parse_dcp_response(data)
        assert result.device_name == "my-plc"
        assert result.station_name == "my-plc"

    def test_manufacturer_parsing(self):
        data = _build_dcp_response_bytes(manufacturer="Siemens AG")
        result = _parse_dcp_response(data)
        assert result.manufacturer == "Siemens AG"

    def test_device_id_parsing(self):
        data = _build_dcp_response_bytes(vendor_id=0x1234, device_id=0x5678)
        result = _parse_dcp_response(data)
        assert result.vendor_id == 0x1234
        assert result.device_id == 0x5678

    def test_device_id_short_block(self):
        """Device ID block with < 4 bytes should not crash."""
        # Build custom data with short device ID block
        # Parser reads MAC from data[0:6], so put device MAC in dst position
        dst_mac = b"\x00\x11\x22\x33\x44\x55"
        src_mac = b"\xff" * 6
        header = dst_mac + src_mac + struct.pack(">H", PROFINET_ETHERTYPE)
        dcp_header = struct.pack(">H", DCP_IDENTIFY)
        dcp_header += bytes([DCP_SV_IDENTIFY, 0x01])
        dcp_header += struct.pack(">I", 1)
        dcp_header += struct.pack(">H", 0)
        # Short device ID block (only 2 bytes)
        options = struct.pack(">HH", DCP_OPT_DEVICE_PROPERTIES, DCP_SUBDEVICE_ID)
        options += struct.pack(">H", 2)
        options += struct.pack(">H", 0x1234)
        data = header + dcp_header + options
        result = _parse_dcp_response(data)
        assert result is not None
        # vendor_id stays 0 because block_data < 4
        assert result.vendor_id == 0
        assert result.device_id == 0

    def test_unknown_option_skipped(self):
        """Unknown options should be skipped without error."""
        # Parser reads MAC from data[0:6], so put device MAC in dst position
        dst_mac = b"\x00\x11\x22\x33\x44\x55"
        src_mac = b"\xff" * 6
        header = dst_mac + src_mac + struct.pack(">H", PROFINET_ETHERTYPE)
        dcp_header = struct.pack(">H", DCP_IDENTIFY)
        dcp_header += bytes([DCP_SV_IDENTIFY, 0x01])
        dcp_header += struct.pack(">I", 1)
        dcp_header += struct.pack(">H", 0)
        # Unknown option 0x0099
        options = struct.pack(">HH", 0x0099, 0x0001)
        options += struct.pack(">H", 4)
        options += b"\xde\xad\xbe\xef"
        data = header + dcp_header + options
        result = _parse_dcp_response(data)
        assert result is not None
        assert result.mac_address == "00:11:22:33:44:55"

    def test_block_len_exceeds_data(self):
        """Block length exceeding data should break the loop."""
        # Parser reads MAC from data[0:6], so put device MAC in dst position
        dst_mac = b"\x00\x11\x22\x33\x44\x55"
        src_mac = b"\xff" * 6
        header = dst_mac + src_mac + struct.pack(">H", PROFINET_ETHERTYPE)
        dcp_header = struct.pack(">H", DCP_IDENTIFY)
        dcp_header += bytes([DCP_SV_IDENTIFY, 0x01])
        dcp_header += struct.pack(">I", 1)
        dcp_header += struct.pack(">H", 0)
        # Option with block_len larger than remaining data
        options = struct.pack(">HH", DCP_OPT_IP, DCP_SUB_IP_PARAMS)
        options += struct.pack(">H", 999)  # huge block_len
        options += b"\x00" * 4  # only 4 bytes available
        data = header + dcp_header + options
        result = _parse_dcp_response(data)
        # Should not crash; IP not parsed because block exceeds data
        assert result is not None
        assert result.ip_address == "0.0.0.0"

    def test_exception_returns_none(self):
        """Any exception in parsing returns None."""
        # Pass a non-bytes object to trigger exception
        result = _parse_dcp_response(None)  # type: ignore
        assert result is None

    def test_vlan_tag_detection(self):
        """Verify VLAN tag at offset 14 is detected and skipped."""
        data = _build_dcp_response_bytes(include_vlan=True, device_name="vlan-device")
        result = _parse_dcp_response(data)
        assert result is not None
        assert result.device_name == "vlan-device"

    def test_empty_device_name(self):
        data = _build_dcp_response_bytes(device_name="")
        result = _parse_dcp_response(data)
        assert result is not None
        assert result.device_name == ""

    def test_station_name_equals_device_name(self):
        data = _build_dcp_response_bytes(device_name="station-test")
        result = _parse_dcp_response(data)
        assert result.station_name == result.device_name


# ── Test ProfinetClient.__init__ ──────────────────────────────────────────────


class TestProfinetClientInit:
    """Test ProfinetClient initialization."""

    def test_default_init(self):
        c = ProfinetClient()
        assert c._interface_ip == "0.0.0.0"
        assert c._port == DEFAULT_PNET_PORT
        assert c._transport is None
        assert c._protocol is None
        assert c._xid == 0
        assert c._discovered_devices == {}

    def test_custom_init(self):
        c = ProfinetClient(interface_ip="192.168.1.100", port=50000)
        assert c._interface_ip == "192.168.1.100"
        assert c._port == 50000


# ── Test ProfinetClient.connect ───────────────────────────────────────────────


class TestProfinetClientConnect:
    """Test ProfinetClient.connect."""

    async def test_connect_success(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        mock_protocol = MagicMock()

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.create_datagram_endpoint = AsyncMock(return_value=(mock_transport, mock_protocol))
            result = await c.connect()

        assert result is True
        assert c._transport is mock_transport
        assert c._protocol is not None

    async def test_connect_failure(self):
        c = ProfinetClient()

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.create_datagram_endpoint = AsyncMock(side_effect=OSError("bind failed"))
            result = await c.connect()

        assert result is False
        assert c._transport is None


# ── Test ProfinetClient.close ─────────────────────────────────────────────────


class TestProfinetClientClose:
    """Test ProfinetClient.close."""

    def test_close_with_transport(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        c._transport = mock_transport
        c.close()
        mock_transport.close.assert_called_once()
        assert c._transport is None

    def test_close_without_transport(self):
        c = ProfinetClient()
        c.close()
        assert c._transport is None

    def test_close_idempotent(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        c._transport = mock_transport
        c.close()
        c.close()  # second close should not raise
        assert c._transport is None


# ── Test ProfinetClient._get_next_xid ─────────────────────────────────────────


class TestProfinetClientXid:
    """Test ProfinetClient._get_next_xid."""

    def test_first_xid(self):
        c = ProfinetClient()
        assert c._get_next_xid() == 1

    def test_incrementing_xid(self):
        c = ProfinetClient()
        assert c._get_next_xid() == 1
        assert c._get_next_xid() == 2
        assert c._get_next_xid() == 3

    def test_wraparound(self):
        c = ProfinetClient()
        c._xid = 0xFFFFFFFF
        result = c._get_next_xid()
        assert result == 0  # wraps to 0

    def test_xid_stays_in_uint32(self):
        c = ProfinetClient()
        c._xid = 0xFFFFFFFE
        assert c._get_next_xid() == 0xFFFFFFFF
        assert c._get_next_xid() == 0


# ── Test ProfinetClient.discover_devices ──────────────────────────────────────


class TestProfinetClientDiscover:
    """Test ProfinetClient.discover_devices."""

    async def test_discover_no_transport(self):
        c = ProfinetClient()
        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()):
            result = await c.discover_devices(timeout=0.01)
        assert result == []

    async def test_discover_with_transport(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        c._transport = mock_transport

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()):
            result = await c.discover_devices(timeout=0.01)

        assert result == []
        mock_transport.sendto.assert_called_once()
        # Verify broadcast destination
        call_args = mock_transport.sendto.call_args
        assert call_args[0][1] == ("<broadcast>", c._port)

    async def test_discover_clears_previous(self):
        c = ProfinetClient()
        c._discovered_devices["old"] = _make_device()
        mock_transport = MagicMock()
        c._transport = mock_transport

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()):
            await c.discover_devices(timeout=0.01)

        assert "old" not in c._discovered_devices

    async def test_discover_returns_found_devices(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        c._transport = mock_transport

        # Simulate packet reception during sleep (discover clears devices first)
        async def _simulate_packet(*args, **kwargs):
            c.handle_packet(_build_dcp_response_bytes(mac=b"\x00\x11\x22\x33\x44\x55"))

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock(side_effect=_simulate_packet)):
            result = await c.discover_devices(timeout=0.01)

        assert len(result) == 1
        assert result[0].device_name == "test-device"

    async def test_discover_sends_broadcast_frame(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        c._transport = mock_transport

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()):
            await c.discover_devices(timeout=0.01)

        sent_frame = mock_transport.sendto.call_args[0][0]
        # Frame should start with broadcast MAC
        assert sent_frame[:6] == b"\xff" * 6
        # Should contain ethertype
        assert struct.unpack(">H", sent_frame[12:14])[0] == PROFINET_ETHERTYPE


# ── Test ProfinetClient.get_device_by_name ────────────────────────────────────


class TestProfinetClientGetByName:
    """Test ProfinetClient.get_device_by_name."""

    async def test_device_found(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        c._transport = mock_transport

        # Simulate packet reception during sleep (get_device_by_name clears devices first)
        async def _simulate_packet(*args, **kwargs):
            c.handle_packet(
                _build_dcp_response_bytes(
                    mac=b"\xaa\xbb\xcc\xdd\xee\xff",
                    device_name="target-device",
                )
            )

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock(side_effect=_simulate_packet)):
            result = await c.get_device_by_name("target-device", timeout=0.01)

        assert result is not None
        assert result.device_name == "target-device"
        assert result.mac_address == "AA:BB:CC:DD:EE:FF"

    async def test_device_not_found(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        c._transport = mock_transport

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()):
            result = await c.get_device_by_name("nonexistent", timeout=0.01)

        assert result is None

    async def test_clears_previous_devices(self):
        c = ProfinetClient()
        c._discovered_devices["old"] = _make_device()
        mock_transport = MagicMock()
        c._transport = mock_transport

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()):
            await c.get_device_by_name("any", timeout=0.01)

        assert "old" not in c._discovered_devices

    async def test_no_transport(self):
        c = ProfinetClient()

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()):
            result = await c.get_device_by_name("any", timeout=0.01)

        assert result is None

    async def test_sends_frame_with_device_name(self):
        c = ProfinetClient()
        mock_transport = MagicMock()
        c._transport = mock_transport

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()):
            await c.get_device_by_name("my-plc", timeout=0.01)

        sent_frame = mock_transport.sendto.call_args[0][0]
        # The device name should be embedded in the frame
        assert b"my-plc" in sent_frame


# ── Test ProfinetClient.read_io_data / write_io_data ─────────────────────────


class TestProfinetClientIO:
    """Test ProfinetClient.read_io_data and write_io_data (simplified stubs)."""

    async def test_read_io_data_returns_none(self):
        c = ProfinetClient()
        device = _make_device()
        result = await c.read_io_data(device, slot=1, subslot=1, data_size=4)
        assert result is None

    async def test_write_io_data_returns_false(self):
        c = ProfinetClient()
        device = _make_device()
        result = await c.write_io_data(device, slot=1, subslot=1, data=b"\x00\x01")
        assert result is False


# ── Test ProfinetClient.handle_packet ─────────────────────────────────────────


class TestProfinetClientHandlePacket:
    """Test ProfinetClient.handle_packet."""

    def test_valid_packet_adds_device(self):
        c = ProfinetClient()
        data = _build_dcp_response_bytes()
        c.handle_packet(data)
        assert len(c._discovered_devices) == 1
        device = list(c._discovered_devices.values())[0]
        assert device.device_name == "test-device"

    def test_invalid_packet_ignored(self):
        c = ProfinetClient()
        c.handle_packet(b"\x00" * 10)  # too short
        assert len(c._discovered_devices) == 0

    def test_packet_keyed_by_mac(self):
        c = ProfinetClient()
        data = _build_dcp_response_bytes(mac=b"\xaa\xbb\xcc\xdd\xee\xff")
        c.handle_packet(data)
        assert "AA:BB:CC:DD:EE:FF" in c._discovered_devices

    def test_multiple_packets_different_macs(self):
        c = ProfinetClient()
        data1 = _build_dcp_response_bytes(mac=b"\xaa\xbb\xcc\xdd\xee\xff", device_name="dev1")
        data2 = _build_dcp_response_bytes(mac=b"\x11\x22\x33\x44\x55\x66", device_name="dev2")
        c.handle_packet(data1)
        c.handle_packet(data2)
        assert len(c._discovered_devices) == 2

    def test_same_mac_overwrites(self):
        c = ProfinetClient()
        data1 = _build_dcp_response_bytes(mac=b"\xaa\xbb\xcc\xdd\xee\xff", device_name="dev1")
        data2 = _build_dcp_response_bytes(mac=b"\xaa\xbb\xcc\xdd\xee\xff", device_name="dev2")
        c.handle_packet(data1)
        c.handle_packet(data2)
        assert len(c._discovered_devices) == 1
        device = list(c._discovered_devices.values())[0]
        assert device.device_name == "dev2"


# ── Test _ProfinetProtocol ────────────────────────────────────────────────────


class TestProfinetProtocol:
    """Test _ProfinetProtocol class."""

    def test_init(self):
        client = ProfinetClient()
        proto = _ProfinetProtocol(client)
        assert proto._client is client

    def test_connection_made(self):
        client = ProfinetClient()
        proto = _ProfinetProtocol(client)
        transport = MagicMock()
        # Should not raise
        proto.connection_made(transport)

    def test_datagram_received_calls_handle_packet(self):
        client = ProfinetClient()
        proto = _ProfinetProtocol(client)
        data = _build_dcp_response_bytes()
        proto.datagram_received(data, ("192.168.1.1", 34964))
        assert len(client._discovered_devices) == 1

    def test_datagram_received_invalid_data(self):
        client = ProfinetClient()
        proto = _ProfinetProtocol(client)
        proto.datagram_received(b"\x00" * 5, ("192.168.1.1", 34964))
        assert len(client._discovered_devices) == 0

    def test_error_received(self):
        client = ProfinetClient()
        proto = _ProfinetProtocol(client)
        # Should not raise
        proto.error_received(OSError("test error"))


# ── Test ProfinetDriver.__init__ ──────────────────────────────────────────────


class TestProfinetDriverInit:
    """Test ProfinetDriver initialization."""

    def test_default_state(self):
        d = ProfinetDriver()
        assert d._running is False
        assert d._client is None
        assert d._snap7_client is None
        assert d._snap7_bridge is None
        assert d._config == {}
        assert d._device_points == {}
        assert d._devices == {}
        assert d._reconnect_count == 0
        assert d._reconnect_delay == ProfinetDriver._RECONNECT_BASE_DELAY
        assert d._snap7_enabled is False

    def test_plugin_metadata(self):
        assert ProfinetDriver.plugin_name == "profinet"
        assert ProfinetDriver.plugin_version == "1.1.0"
        assert "profinet" in ProfinetDriver.supported_protocols
        assert "profinet_dcp" in ProfinetDriver.supported_protocols
        assert "pn" in ProfinetDriver.supported_protocols

    def test_config_schema_fields(self):
        fields = ProfinetDriver.config_schema["fields"]
        field_names = [f["name"] for f in fields]
        assert "interface_ip" in field_names
        assert "port" in field_names
        assert "enable_snap7" in field_names
        assert "snap7_plc_ip" in field_names
        assert "snap7_rack" in field_names
        assert "snap7_slot" in field_names

    def test_reconnect_constants(self):
        assert ProfinetDriver._MAX_RECONNECT_ATTEMPTS == 100
        assert ProfinetDriver._RECONNECT_BASE_DELAY == 1.0
        assert ProfinetDriver._RECONNECT_MAX_DELAY == 60.0

    def test_no_super_init(self):
        """ProfinetDriver.__init__ does not call super().__init__(),
        so base-class attributes like _health_stats are missing.
        """
        d = ProfinetDriver()
        assert not hasattr(d, "_health_stats")
        assert not hasattr(d, "_offline_since")


# ── Test ProfinetDriver.start ─────────────────────────────────────────────────


class TestProfinetDriverStart:
    """Test ProfinetDriver.start."""

    async def test_start_without_snap7(self):
        d = _make_driver()

        with patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            await d.start({"interface_ip": "0.0.0.0", "port": 34964})

        assert d._running is True
        assert d._reconnect_count == 0
        assert d._snap7_enabled is False
        assert d._snap7_client is None

    async def test_start_with_snap7_no_plc_ip(self):
        d = _make_driver()

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.SNAP7_AVAILABLE", True),
            patch("edgelite.drivers.profinet.Snap7Client") as mock_snap7_cls,
        ):
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client
            mock_snap7 = MagicMock()
            mock_snap7_cls.return_value = mock_snap7

            await d.start({"enable_snap7": True, "snap7_plc_ip": ""})

        assert d._running is True
        assert d._snap7_enabled is True
        # No PLC IP => snap7_client should be set but bridge not connected
        assert d._snap7_client is not None
        assert d._snap7_bridge is None

    async def test_start_with_snap7_and_plc_ip_success(self):
        d = _make_driver()

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.SNAP7_AVAILABLE", True),
            patch("edgelite.drivers.profinet.Snap7Client") as mock_snap7_cls,
            patch("edgelite.drivers.profinet.ProfinetSnap7Bridge") as mock_bridge_cls,
        ):
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            mock_snap7 = MagicMock()
            mock_snap7.connect = MagicMock(return_value=True)
            mock_snap7_cls.return_value = mock_snap7

            mock_bridge = MagicMock()
            mock_bridge_cls.return_value = mock_bridge

            await d.start(
                {
                    "enable_snap7": True,
                    "snap7_plc_ip": "192.168.1.1",
                    "snap7_rack": 0,
                    "snap7_slot": 1,
                }
            )

        assert d._running is True
        assert d._snap7_client is mock_snap7
        assert d._snap7_bridge is mock_bridge

    async def test_start_with_snap7_connect_failure(self):
        d = _make_driver()

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.SNAP7_AVAILABLE", True),
            patch("edgelite.drivers.profinet.Snap7Client") as mock_snap7_cls,
        ):
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            mock_snap7 = MagicMock()
            mock_snap7.connect = MagicMock(return_value=False)
            mock_snap7_cls.return_value = mock_snap7

            await d.start({"enable_snap7": True, "snap7_plc_ip": "192.168.1.1"})

        assert d._running is True
        # Snap7 connection failed => snap7_client set to None
        assert d._snap7_client is None
        assert d._snap7_bridge is None

    async def test_start_snap7_init_exception(self):
        d = _make_driver()

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.SNAP7_AVAILABLE", True),
            patch("edgelite.drivers.profinet.Snap7Client") as mock_snap7_cls,
        ):
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client
            mock_snap7_cls.side_effect = RuntimeError("init failed")

            await d.start({"enable_snap7": True, "snap7_plc_ip": "192.168.1.1"})

        assert d._running is True
        assert d._snap7_client is None

    async def test_start_snap7_not_available(self):
        d = _make_driver()

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.SNAP7_AVAILABLE", False),
        ):
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            await d.start({"enable_snap7": True, "snap7_plc_ip": "192.168.1.1"})

        assert d._running is True
        # SNAP7_AVAILABLE is False => no snap7 client created
        assert d._snap7_client is None

    async def test_start_connection_failure_raises(self):
        d = _make_driver()

        with patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(side_effect=OSError("connection failed"))
            mock_client_cls.return_value = mock_client

            with pytest.raises(OSError):
                await d.start({"interface_ip": "0.0.0.0"})

        assert d._running is False

    async def test_start_client_connect_returns_false(self):
        d = _make_driver()

        with patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await d.start({"interface_ip": "0.0.0.0"})

        # connect returned False => _running stays False (not set to True)
        assert d._running is False

    async def test_start_with_custom_port(self):
        d = _make_driver()

        with patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            await d.start({"port": 50000})

        mock_client_cls.assert_called_once_with("0.0.0.0", 50000)


# ── Test ProfinetDriver.stop ──────────────────────────────────────────────────


class TestProfinetDriverStop:
    """Test ProfinetDriver.stop."""

    async def test_stop_full(self):
        d = _make_driver(running=True)
        mock_client = MagicMock()
        mock_snap7_client = MagicMock()
        mock_bridge = MagicMock()
        d._client = mock_client
        d._snap7_client = mock_snap7_client
        d._snap7_bridge = mock_bridge

        await d.stop()

        assert d._running is False
        mock_bridge.disconnect.assert_called_once()
        mock_snap7_client.destroy.assert_called_once()
        mock_client.close.assert_called_once()
        assert d._snap7_bridge is None
        assert d._snap7_client is None
        assert d._client is None

    async def test_stop_no_snap7(self):
        d = _make_driver(running=True)
        mock_client = MagicMock()
        d._client = mock_client

        await d.stop()

        assert d._running is False
        mock_client.close.assert_called_once()
        assert d._client is None

    async def test_stop_no_client(self):
        d = _make_driver(running=True)

        await d.stop()

        assert d._running is False

    async def test_stop_idempotent(self):
        d = _make_driver(running=True)
        mock_client = MagicMock()
        d._client = mock_client

        await d.stop()
        await d.stop()

        assert d._running is False


# ── Test ProfinetDriver.read_points ───────────────────────────────────────────


class TestProfinetDriverReadPoints:
    """Test ProfinetDriver.read_points."""

    async def test_not_running_returns_empty(self):
        d = _make_driver(running=False)
        result = await d.read_points("dev1", ["point1"])
        assert result == {}

    async def test_no_client_returns_empty(self):
        d = _make_driver(running=True)
        d._client = None
        result = await d.read_points("dev1", ["point1"])
        assert result == {}

    async def test_snap7_path(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        mock_bridge = MagicMock()
        mock_bridge.is_connected = True
        d._snap7_bridge = mock_bridge

        with patch.object(d, "_read_via_snap7", new=AsyncMock(return_value=42)):
            result = await d.read_points("dev1", ["io:1:1:0:2"])

        assert result == {"io:1:1:0:2": 42}

    async def test_non_snap7_with_device_status(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        device = _make_device()
        d._devices["dev1"] = device
        d._device_points["dev1"] = {"device_name": "dev1", "config": {}, "points": {}}

        result = await d.read_points("dev1", ["status"])

        assert result == {"status": 1}

    async def test_non_snap7_with_device_ip(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        device = _make_device(ip_address="10.0.0.5")
        d._devices["mydevice"] = device
        d._device_points["dev1"] = {"device_name": "mydevice", "config": {}, "points": {}}

        result = await d.read_points("dev1", ["ip_address"])

        assert result == {"ip_address": "10.0.0.5"}

    async def test_non_snap7_with_device_other(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        device = _make_device()
        d._devices["dev1"] = device
        d._device_points["dev1"] = {"device_name": "dev1", "config": {}, "points": {}}

        result = await d.read_points("dev1", ["temperature"])

        assert result == {"temperature": 0}

    async def test_non_snap7_without_device(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        d._device_points["dev1"] = {"device_name": "nonexistent", "config": {}, "points": {}}

        result = await d.read_points("dev1", ["temperature"])

        assert result == {"temperature": None}

    async def test_non_snap7_no_device_info(self):
        d = _make_driver(running=True)
        d._client = MagicMock()

        result = await d.read_points("unknown_dev", ["point1"])

        assert result == {"point1": None}

    async def test_exception_returns_none(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        mock_bridge = MagicMock()
        mock_bridge.is_connected = True
        d._snap7_bridge = mock_bridge

        with patch.object(d, "_read_via_snap7", new=AsyncMock(side_effect=RuntimeError("err"))):
            result = await d.read_points("dev1", ["point1"])

        assert result == {"point1": None}

    async def test_multiple_points_mixed(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        device = _make_device(ip_address="10.0.0.5")
        d._devices["dev1"] = device
        d._device_points["dev1"] = {"device_name": "dev1", "config": {}, "points": {}}

        result = await d.read_points("dev1", ["status", "ip", "temperature"])

        assert result == {"status": 1, "ip": "10.0.0.5", "temperature": 0}

    async def test_status_case_insensitive(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        device = _make_device()
        d._devices["dev1"] = device
        d._device_points["dev1"] = {"device_name": "dev1", "config": {}, "points": {}}

        result = await d.read_points("dev1", ["DeviceStatus"])

        assert result == {"DeviceStatus": 1}

    async def test_uses_device_name_from_config(self):
        d = _make_driver(running=True)
        d._client = MagicMock()
        device = _make_device(ip_address="1.2.3.4")
        d._devices["configured_name"] = device
        d._device_points["dev1"] = {
            "device_name": "configured_name",
            "config": {},
            "points": {},
        }

        result = await d.read_points("dev1", ["ip"])

        assert result == {"ip": "1.2.3.4"}


# ── Test ProfinetDriver._read_via_snap7 ───────────────────────────────────────


class TestProfinetDriverReadViaSnap7:
    """Test ProfinetDriver._read_via_snap7 address parsing."""

    async def test_no_bridge_returns_none(self):
        d = _make_driver(running=True)
        d._snap7_bridge = None
        result = await d._read_via_snap7("io:1:1:0:2")
        assert result is None

    async def test_io_address(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        mock_bridge.read_io_data = MagicMock(return_value=b"\x01\x02")
        d._snap7_bridge = mock_bridge
        d._snap7_client = MagicMock()

        result = await d._read_via_snap7("io:1:2:3:4")

        mock_bridge.read_io_data.assert_called_once_with(1, 2, 3, 4)
        assert result == "0102"

    async def test_io_address_returns_none(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        mock_bridge.read_io_data = MagicMock(return_value=None)
        d._snap7_bridge = mock_bridge

        result = await d._read_via_snap7("io:1:2:3:4")
        assert result is None

    async def test_db_address_size_2(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.read_db_int16 = MagicMock(return_value=42)
        d._snap7_client = mock_snap7

        result = await d._read_via_snap7("db:1:100:2")

        mock_snap7.read_db_int16.assert_called_once_with(1, 100)
        assert result == 42

    async def test_db_address_default_size(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.read_db_int16 = MagicMock(return_value=10)
        d._snap7_client = mock_snap7

        result = await d._read_via_snap7("db:1:100")

        mock_snap7.read_db_int16.assert_called_once_with(1, 100)
        assert result == 10

    async def test_db_address_other_size(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.read_area = MagicMock(return_value=b"\xde\xad\xbe\xef")
        d._snap7_client = mock_snap7

        result = await d._read_via_snap7("db:1:100:4")

        mock_snap7.read_area.assert_called_once()
        assert result == "deadbeef"

    async def test_db_address_other_size_returns_none(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.read_area = MagicMock(return_value=None)
        d._snap7_client = mock_snap7

        result = await d._read_via_snap7("db:1:100:4")
        assert result is None

    async def test_pa_address(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.read_area = MagicMock(return_value=b"\x01\x02")
        d._snap7_client = mock_snap7

        result = await d._read_via_snap7("pa:10:2")

        mock_snap7.read_area.assert_called_once()
        assert result == "0102"

    async def test_pa_address_default_size(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.read_area = MagicMock(return_value=b"\x01\x02")
        d._snap7_client = mock_snap7

        result = await d._read_via_snap7("pa:10")

        mock_snap7.read_area.assert_called_once()
        assert result == "0102"

    async def test_pe_address(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.read_area = MagicMock(return_value=b"\x03\x04")
        d._snap7_client = mock_snap7

        result = await d._read_via_snap7("pe:20:2")

        mock_snap7.read_area.assert_called_once()
        assert result == "0304"

    async def test_pe_address_default_size(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.read_area = MagicMock(return_value=b"\x03\x04")
        d._snap7_client = mock_snap7

        result = await d._read_via_snap7("pe:20")

        assert result == "0304"

    async def test_unknown_format(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge

        result = await d._read_via_snap7("unknown:1:2:3")
        assert result is None

    async def test_io_address_too_few_parts(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge

        result = await d._read_via_snap7("io:1:2")
        assert result is None

    async def test_value_error_in_parse(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge

        result = await d._read_via_snap7("io:abc:2:3:4")
        assert result is None

    async def test_index_error_in_parse(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge

        result = await d._read_via_snap7("")
        assert result is None

    async def test_db_address_too_few_parts(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        d._snap7_client = MagicMock()

        result = await d._read_via_snap7("db:1")
        assert result is None

    async def test_address_case_insensitive(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        mock_bridge.read_io_data = MagicMock(return_value=b"\x01\x02")
        d._snap7_bridge = mock_bridge

        result = await d._read_via_snap7("IO:1:2:3:4")
        assert result == "0102"


# ── Test ProfinetDriver.write_point ───────────────────────────────────────────


class TestProfinetDriverWritePoint:
    """Test ProfinetDriver.write_point."""

    async def test_not_running_returns_false(self):
        d = _make_driver(running=False)
        result = await d.write_point("dev1", "db:1:0", 42)
        assert result is False

    async def test_snap7_path_success(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        mock_bridge.is_connected = True
        d._snap7_bridge = mock_bridge

        with patch.object(d, "_write_via_snap7", new=AsyncMock(return_value=True)):
            result = await d.write_point("dev1", "db:1:0", 42)

        assert result is True

    async def test_snap7_path_exception(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        mock_bridge.is_connected = True
        d._snap7_bridge = mock_bridge

        with patch.object(d, "_write_via_snap7", new=AsyncMock(side_effect=RuntimeError("err"))):
            result = await d.write_point("dev1", "db:1:0", 42)

        assert result is False

    async def test_no_snap7_returns_false(self):
        d = _make_driver(running=True)
        d._snap7_bridge = None

        result = await d.write_point("dev1", "db:1:0", 42)
        assert result is False

    async def test_snap7_not_connected_returns_false(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        mock_bridge.is_connected = False
        d._snap7_bridge = mock_bridge

        result = await d.write_point("dev1", "db:1:0", 42)
        assert result is False


# ── Test ProfinetDriver._write_via_snap7 ──────────────────────────────────────


class TestProfinetDriverWriteViaSnap7:
    """Test ProfinetDriver._write_via_snap7."""

    async def test_no_bridge_returns_false(self):
        d = _make_driver(running=True)
        d._snap7_bridge = None
        result = await d._write_via_snap7("db:1:0", 42)
        assert result is False

    async def test_db_write_success(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.write_area = MagicMock(return_value=True)
        d._snap7_client = mock_snap7

        result = await d._write_via_snap7("db:1:100", 42)

        assert result is True
        mock_snap7.write_area.assert_called_once()
        # Verify the data packed as int16
        call_args = mock_snap7.write_area.call_args
        packed_data = call_args[0][3] if len(call_args[0]) > 3 else call_args[1].get("data")
        assert packed_data == struct.pack(">h", 42)

    async def test_db_write_failure(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.write_area = MagicMock(return_value=False)
        d._snap7_client = mock_snap7

        result = await d._write_via_snap7("db:1:100", 42)
        assert result is False

    async def test_non_db_format_returns_false(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        d._snap7_client = MagicMock()

        result = await d._write_via_snap7("io:1:2:3:4", 42)
        assert result is False

    async def test_value_error_in_parse(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        d._snap7_client = MagicMock()

        result = await d._write_via_snap7("db:abc:100", 42)
        assert result is False

    async def test_struct_error_in_pack(self):
        """struct.error when value can't be packed as int16."""
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        d._snap7_client = mock_snap7

        # Value too large for int16 triggers struct.error
        result = await d._write_via_snap7("db:1:100", 999999)
        assert result is False

    async def test_db_write_negative_value(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.write_area = MagicMock(return_value=True)
        d._snap7_client = mock_snap7

        result = await d._write_via_snap7("db:1:100", -42)

        assert result is True
        call_args = mock_snap7.write_area.call_args
        packed_data = call_args[0][3] if len(call_args[0]) > 3 else call_args[1].get("data")
        assert packed_data == struct.pack(">h", -42)

    async def test_db_too_few_parts(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        d._snap7_client = MagicMock()

        result = await d._write_via_snap7("db:1", 42)
        assert result is False


# ── Test ProfinetDriver.add_device ────────────────────────────────────────────


class TestProfinetDriverAddDevice:
    """Test ProfinetDriver.add_device."""

    async def test_add_device_basic(self):
        d = _make_driver(running=True)
        points = [{"name": "p1"}, {"name": "p2"}]
        config = {"device_name": "dev1"}

        await d.add_device("dev1", config, points)

        assert "dev1" in d._device_points
        info = d._device_points["dev1"]
        assert info["device_name"] == "dev1"
        assert "p1" in info["points"]
        assert "p2" in info["points"]

    async def test_add_device_default_name(self):
        d = _make_driver(running=True)
        await d.add_device("dev1", {}, [{"name": "p1"}])

        info = d._device_points["dev1"]
        assert info["device_name"] == "dev1"

    async def test_add_device_with_db_mapping(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        points = [{"name": "p1"}, {"name": "p2"}, {"name": "p3"}]
        config = {
            "device_name": "dev1",
            "pn_slot": 1,
            "pn_subslot": 2,
            "db_number": 10,
            "db_start": 100,
            "map_to_db": True,
        }

        await d.add_device("dev1", config, points)

        # Should map 3 points to DB
        assert mock_bridge.map_pn_to_db.call_count == 3
        # First point: slot=1, subslot=2, index=0, db=10, start=100
        mock_bridge.map_pn_to_db.assert_any_call(1, 2, 0, 10, 100, 2)
        # Second point: index=1, start=102
        mock_bridge.map_pn_to_db.assert_any_call(1, 2, 1, 10, 102, 2)
        # Third point: index=2, start=104
        mock_bridge.map_pn_to_db.assert_any_call(1, 2, 2, 10, 104, 2)

    async def test_add_device_no_mapping_without_bridge(self):
        d = _make_driver(running=True)
        d._snap7_bridge = None
        points = [{"name": "p1"}]
        config = {"map_to_db": True}

        # Should not raise
        await d.add_device("dev1", config, points)
        assert "dev1" in d._device_points

    async def test_add_device_no_mapping_flag(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        points = [{"name": "p1"}]
        config = {"map_to_db": False}

        await d.add_device("dev1", config, points)
        mock_bridge.map_pn_to_db.assert_not_called()

    async def test_add_device_points_without_name_skipped(self):
        d = _make_driver(running=True)
        points = [{"name": "p1"}, {"no_name": "x"}, {"name": "p2"}]

        await d.add_device("dev1", {}, points)

        info = d._device_points["dev1"]
        assert "p1" in info["points"]
        assert "p2" in info["points"]
        assert len(info["points"]) == 2

    async def test_add_device_default_slot_subslot(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        config = {"map_to_db": True}
        points = [{"name": "p1"}]

        await d.add_device("dev1", config, points)

        # Default pn_slot=1, pn_subslot=1, db_number=1, db_start=0
        mock_bridge.map_pn_to_db.assert_called_once_with(1, 1, 0, 1, 0, 2)


# ── Test ProfinetDriver.remove_device ─────────────────────────────────────────


class TestProfinetDriverRemoveDevice:
    """Test ProfinetDriver.remove_device."""

    def test_remove_existing_device(self):
        d = _make_driver(running=True)
        d._device_points["dev1"] = {"device_name": "dev1", "config": {}, "points": {}}
        d._devices["dev1"] = _make_device()

        d.remove_device("dev1")

        assert "dev1" not in d._device_points
        assert "dev1" not in d._devices

    def test_remove_nonexistent_device(self):
        d = _make_driver(running=True)

        # Should not raise
        d.remove_device("nonexistent")

    def test_remove_clears_devices_by_device_name(self):
        d = _make_driver(running=True)
        d._device_points["dev1"] = {"device_name": "configured_name", "config": {}, "points": {}}
        d._devices["configured_name"] = _make_device()

        d.remove_device("dev1")

        assert "dev1" not in d._device_points
        assert "configured_name" not in d._devices

    def test_remove_clears_health_stats(self):
        d = _make_driver(running=True)
        d._device_points["dev1"] = {"device_name": "dev1", "config": {}, "points": {}}
        d._health_stats["dev1"] = MagicMock()
        d._offline_since["dev1"] = MagicMock()

        d.remove_device("dev1")

        assert "dev1" not in d._health_stats
        assert "dev1" not in d._offline_since


# ── Test ProfinetDriver.discover_devices ──────────────────────────────────────


class TestProfinetDriverDiscoverDevices:
    """Test ProfinetDriver.discover_devices."""

    async def test_no_client_returns_empty(self):
        d = _make_driver(running=True)
        d._client = None

        result = await d.discover_devices({})

        assert result == []

    async def test_discover_success(self):
        d = _make_driver(running=True)
        mock_client = MagicMock()
        device = _make_device()
        mock_client.discover_devices = AsyncMock(return_value=[device])
        d._client = mock_client

        result = await d.discover_devices({})

        assert len(result) == 1
        entry = result[0]
        assert entry["device_name"] == "test-device"
        assert entry["ip_address"] == "192.168.1.10"
        assert entry["mac_address"] == "00:11:22:33:44:55"
        assert entry["vendor_id"] == 42
        assert entry["manufacturer"] == "TestVendor"
        assert entry["type"] == "profinet"
        assert entry["device_id"] == 100
        # Device should be stored internally
        assert "test-device" in d._devices

    async def test_discover_multiple_devices(self):
        d = _make_driver(running=True)
        mock_client = MagicMock()
        dev1 = _make_device(device_name="dev1", mac_address="AA:BB:CC:DD:EE:01")
        dev2 = _make_device(device_name="dev2", mac_address="AA:BB:CC:DD:EE:02")
        mock_client.discover_devices = AsyncMock(return_value=[dev1, dev2])
        d._client = mock_client

        result = await d.discover_devices({})

        assert len(result) == 2
        assert "dev1" in d._devices
        assert "dev2" in d._devices

    async def test_discover_exception_returns_empty(self):
        d = _make_driver(running=True)
        mock_client = MagicMock()
        mock_client.discover_devices = AsyncMock(side_effect=RuntimeError("err"))
        d._client = mock_client

        result = await d.discover_devices({})

        assert result == []

    async def test_discover_empty_result(self):
        d = _make_driver(running=True)
        mock_client = MagicMock()
        mock_client.discover_devices = AsyncMock(return_value=[])
        d._client = mock_client

        result = await d.discover_devices({})

        assert result == []


# ── Test ProfinetDriver.is_device_connected ───────────────────────────────────


class TestProfinetDriverIsDeviceConnected:
    """Test ProfinetDriver.is_device_connected."""

    def test_not_running_returns_false(self):
        d = _make_driver(running=False)
        d._devices["dev1"] = _make_device()

        assert d.is_device_connected("dev1") is False

    def test_device_connected(self):
        d = _make_driver(running=True)
        d._devices["dev1"] = _make_device()
        d._device_points["dev1"] = {"device_name": "dev1", "config": {}, "points": {}}

        assert d.is_device_connected("dev1") is True

    def test_device_not_connected(self):
        d = _make_driver(running=True)

        assert d.is_device_connected("nonexistent") is False

    def test_uses_device_name_from_config(self):
        d = _make_driver(running=True)
        d._devices["configured_name"] = _make_device()
        d._device_points["dev1"] = {
            "device_name": "configured_name",
            "config": {},
            "points": {},
        }

        assert d.is_device_connected("dev1") is True

    def test_no_device_info_uses_device_id(self):
        d = _make_driver(running=True)
        d._devices["dev1"] = _make_device()

        # No device_info => uses device_id as device_name
        assert d.is_device_connected("dev1") is True


# ── Test ProfinetDriver._try_reconnect ────────────────────────────────────────


class TestProfinetDriverTryReconnect:
    """Test ProfinetDriver._try_reconnect."""

    async def test_no_config_returns_early(self):
        d = _make_driver(running=True)
        d._config = {}

        await d._try_reconnect("dev1")

        assert d._reconnect_count == 0

    async def test_reconnect_success(self):
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        mock_client = MagicMock()
        d._client = mock_client

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()),
        ):
            new_client = MagicMock()
            new_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = new_client

            await d._try_reconnect("dev1")

        assert d._running is True
        # On successful reconnect, _reconnect_count is reset to 0
        assert d._reconnect_count == 0
        assert d._reconnect_delay == ProfinetDriver._RECONNECT_BASE_DELAY
        mock_client.close.assert_called_once()

    async def test_reconnect_failure(self):
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        d._client = MagicMock()

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()),
        ):
            new_client = MagicMock()
            new_client.connect = AsyncMock(side_effect=OSError("failed"))
            mock_client_cls.return_value = new_client

            await d._try_reconnect("dev1")

        # _running stays as it was (True from _make_driver), reconnect_count incremented
        assert d._reconnect_count == 1

    async def test_reconnect_connect_returns_false(self):
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        d._client = MagicMock()

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()),
        ):
            new_client = MagicMock()
            new_client.connect = AsyncMock(return_value=False)
            mock_client_cls.return_value = new_client

            await d._try_reconnect("dev1")

        assert d._reconnect_count == 1

    async def test_reconnect_delay_doubles(self):
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        d._client = MagicMock()
        initial_delay = d._reconnect_delay

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()),
        ):
            new_client = MagicMock()
            new_client.connect = AsyncMock(return_value=False)
            mock_client_cls.return_value = new_client

            await d._try_reconnect("dev1")

        assert d._reconnect_delay == min(initial_delay * 2, ProfinetDriver._RECONNECT_MAX_DELAY)

    async def test_reconnect_delay_capped(self):
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        d._client = MagicMock()
        d._reconnect_delay = ProfinetDriver._RECONNECT_MAX_DELAY

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()),
        ):
            new_client = MagicMock()
            new_client.connect = AsyncMock(return_value=False)
            mock_client_cls.return_value = new_client

            await d._try_reconnect("dev1")

        assert d._reconnect_delay == ProfinetDriver._RECONNECT_MAX_DELAY

    async def test_max_reconnect_attempts(self):
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        d._reconnect_count = ProfinetDriver._MAX_RECONNECT_ATTEMPTS

        with patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await d._try_reconnect("dev1")

        # Should return early without sleeping
        # Count is incremented to MAX+1 before the > MAX check
        mock_sleep.assert_not_called()
        assert d._reconnect_count == ProfinetDriver._MAX_RECONNECT_ATTEMPTS + 1

    async def test_reconnect_no_existing_client(self):
        """Reconnect should work even if _client is None."""
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        d._client = None

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()),
        ):
            new_client = MagicMock()
            new_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = new_client

            await d._try_reconnect("dev1")

        assert d._running is True
        assert d._client is new_client

    async def test_reconnect_increments_count(self):
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        d._client = MagicMock()
        d._reconnect_count = 5

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()),
        ):
            new_client = MagicMock()
            new_client.connect = AsyncMock(return_value=False)
            mock_client_cls.return_value = new_client

            await d._try_reconnect("dev1")

        assert d._reconnect_count == 6

    async def test_reconnect_resets_on_success(self):
        d = _make_driver(running=True)
        d._config = {"interface_ip": "0.0.0.0", "port": 34964}
        d._client = MagicMock()
        d._reconnect_count = 10
        d._reconnect_delay = 30.0

        with (
            patch("edgelite.drivers.profinet.ProfinetClient") as mock_client_cls,
            patch("edgelite.drivers.profinet.asyncio.sleep", new=AsyncMock()),
        ):
            new_client = MagicMock()
            new_client.connect = AsyncMock(return_value=True)
            mock_client_cls.return_value = new_client

            await d._try_reconnect("dev1")

        assert d._reconnect_count == 0
        assert d._reconnect_delay == ProfinetDriver._RECONNECT_BASE_DELAY


# ── Test Integration: Write then Read ─────────────────────────────────────────


class TestWriteThenRead:
    """Integration tests for write-then-read cycles."""

    async def test_write_db_then_read_db(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        # Write returns True, read returns 42
        mock_snap7.write_area = MagicMock(return_value=True)
        mock_snap7.read_db_int16 = MagicMock(return_value=42)
        d._snap7_client = mock_snap7

        write_result = await d._write_via_snap7("db:1:100", 42)
        read_result = await d._read_via_snap7("db:1:100:2")

        assert write_result is True
        assert read_result == 42

    async def test_read_io_then_write_db(self):
        d = _make_driver(running=True)
        mock_bridge = MagicMock()
        mock_bridge.read_io_data = MagicMock(return_value=b"\x01\x02")
        d._snap7_bridge = mock_bridge
        mock_snap7 = MagicMock()
        mock_snap7.write_area = MagicMock(return_value=True)
        d._snap7_client = mock_snap7

        read_result = await d._read_via_snap7("io:1:2:3:4")
        write_result = await d._write_via_snap7("db:1:100", 10)

        assert read_result == "0102"
        assert write_result is True


# ── Test Module-level imports ─────────────────────────────────────────────────


class TestModuleImports:
    """Test that module-level imports work correctly."""

    def test_snap7_available_flag(self):
        # SNAP7_AVAILABLE should be a boolean
        from edgelite.drivers.profinet import SNAP7_AVAILABLE

        assert isinstance(SNAP7_AVAILABLE, bool)

    def test_snap7_classes_importable(self):
        from edgelite.drivers.profinet import ProfinetSnap7Bridge, S7Area, Snap7Client, Snap7ConnectionInfo

        assert S7Area is not None
        assert Snap7Client is not None
        assert Snap7ConnectionInfo is not None
        assert ProfinetSnap7Bridge is not None

    def test_s7area_enum_values(self):
        from edgelite.drivers.snap7_integration import S7Area

        assert S7Area.PE.value == 0x81
        assert S7Area.PA.value == 0x82
        assert S7Area.MK.value == 0x83
        assert S7Area.DB.value == 0x84

    def test_profinet_module_logger(self):
        assert profinet.logger is not None
        assert profinet.logger.name == "edgelite.drivers.profinet"
