"""KNXnet/IP cEMI 帧构造与解析单元测试

重点验证:
  1. cEMI L_Data.req 帧结构 (MsgCode+AddInfoLen+Ctrl1+Ctrl2+SrcAddr+DstAddr+PayloadLen+TPDU)
  2. 目的地址(DstAddr)正确放置于组地址位置
  3. Ctrl2=0x60 组地址寻址 (原 0xE0 为个体地址, 错误)
  4. TPDU 构造规范: GroupValue_Read(0x00 0x00), GroupValue_Write(0x80|data)
  5. Tunnel Indication 解析: 正确提取 DstAddr 和 TPDU 数据
"""

import asyncio
import struct
import sys

sys.path.insert(0, "src")

from edgelite.drivers.knx import (
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_HEARTBEAT_MAX_FAILURES,
    DATA_TYPE_PERCENT,
    DATA_TYPE_SWITCH,
    DATA_TYPE_U16,
    KNXClient,
    KNX_CEMI_L_BUS_INDICATION,
    SERVICE_TYPE_CONNECTIONSTATE_REQUEST,
    SERVICE_TYPE_CONNECTIONSTATE_RESPONSE,
    SERVICE_TYPE_TUNNEL_REQUEST,
    _bytes_to_knx_address,
    _knx_address_to_bytes,
)


class TestKnxAddressConversion:
    """KNX 地址编解码"""

    def test_address_to_bytes(self):
        """"1/2/3" → [0x0A, 0x03] (area=1<<3|line=2=0x0A, member=3)"""
        assert _knx_address_to_bytes("1/2/3") == bytes([0x0A, 0x03])

    def test_address_roundtrip(self):
        """地址编解码往返"""
        for addr in ("1/2/3", "5/7/100", "15/7/255"):
            encoded = _knx_address_to_bytes(addr)
            decoded = _bytes_to_knx_address(encoded[0], encoded[1])
            assert decoded == addr

    def test_dot_separator(self):
        """点分分隔符支持"""
        assert _knx_address_to_bytes("1.2.3") == _knx_address_to_bytes("1/2/3")


class TestKnxCemiFrameStructure:
    """cEMI L_Data.req 帧结构正确性"""

    def test_frame_length(self):
        """帧长度 = 4(头) + 2(源) + 2(目的) + 1(负载长) + len(TPDU)"""
        tpdu = bytes([0x00, 0x00])  # GroupValue_Read
        cemi = KNXClient._build_cemi_l_data_req(bytes([0x0A, 0x03]), tpdu)
        assert len(cemi) == 4 + 2 + 2 + 1 + 2  # = 11

    def test_message_code(self):
        """MsgCode = 0x11 (L_Data.req)"""
        cemi = KNXClient._build_cemi_l_data_req(b"\x00\x00", b"\x00\x00")
        assert cemi[0] == 0x11

    def test_add_info_len_zero(self):
        """AddInfoLen = 0 (无附加信息)"""
        cemi = KNXClient._build_cemi_l_data_req(b"\x00\x00", b"\x00\x00")
        assert cemi[1] == 0x00

    def test_ctrl1_standard_low_priority(self):
        """Ctrl1 = 0xBC (标准帧, 低优先级, 不重复, 无确认)"""
        cemi = KNXClient._build_cemi_l_data_req(b"\x00\x00", b"\x00\x00")
        assert cemi[2] == 0xBC

    def test_ctrl2_group_addressing(self):
        """Ctrl2 = 0x60 (组地址, hop=6); 原 0xE0 为个体地址, 错误"""
        cemi = KNXClient._build_cemi_l_data_req(b"\x00\x00", b"\x00\x00")
        assert cemi[3] == 0x60
        # bit7=0 表示组地址
        assert (cemi[3] & 0x80) == 0

    def test_source_address_zero(self):
        """源地址 = 0x0000 (由网关填充)"""
        cemi = KNXClient._build_cemi_l_data_req(b"\x00\x00", b"\x00\x00")
        assert cemi[4] == 0x00
        assert cemi[5] == 0x00

    def test_destination_address_correct_position(self):
        """目的地址位于 offset 6-7, 为实际组地址"""
        dest = _knx_address_to_bytes("1/2/3")
        cemi = KNXClient._build_cemi_l_data_req(dest, b"\x00\x00")
        assert cemi[6] == dest[0]
        assert cemi[7] == dest[1]

    def test_payload_len(self):
        """PayloadLen = len(TPDU) - 1"""
        # 2 字节 TPDU → PayloadLen = 1
        cemi = KNXClient._build_cemi_l_data_req(b"\x00\x00", bytes([0x00, 0x00]))
        assert cemi[8] == 1
        # 3 字节 TPDU → PayloadLen = 2
        cemi = KNXClient._build_cemi_l_data_req(b"\x00\x00", bytes([0x80, 0x01, 0x02]))
        assert cemi[8] == 2

    def test_tpdu_appended_after_payload_len(self):
        """TPDU 紧跟 PayloadLen 之后"""
        tpdu = bytes([0x80, 0x42])
        cemi = KNXClient._build_cemi_l_data_req(b"\x00\x00", tpdu)
        assert cemi[9:9 + len(tpdu)] == tpdu


class TestKnxTpduReadRequest:
    """GroupValue_Read TPDU 构造"""

    def test_read_tpdu_is_group_value_read(self):
        """GroupValue_Read TPDU = [0x00, 0x00]"""
        tpdu = bytes([0x00, 0x00])
        cemi = KNXClient._build_cemi_l_data_req(b"\x0A\x03", tpdu)
        # TPDU 在 cEMI[9:]
        assert cemi[9:] == bytes([0x00, 0x00])
        # APCI 高 2 位 = 00 → GroupValue_Read
        assert (cemi[9] & 0xC0) == 0x00


class TestKnxTpduWriteConstruction:
    """GroupValue_Write TPDU 构造 (通过 write_group_value 捕获)"""

    @staticmethod
    def _capture_write_frame(group_address: str, value, data_type: str) -> bytes:
        """捕获 write_group_value 发送的完整帧"""
        client = KNXClient("127.0.0.1")
        client._connected = True
        client._channel_id = 1
        captured: list[bytes] = []

        class FakeTransport:
            def sendto(self, data, addr):
                captured.append(bytes(data))

        client._transport = FakeTransport()  # type: ignore

        import asyncio

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                client.write_group_value(group_address, value, data_type)
            )
        finally:
            loop.close()
        return captured[0]

    @staticmethod
    def _extract_cemi(frame: bytes) -> bytes:
        """从完整帧中提取 cEMI: header(6) + tunnel_body(3) + cEMI"""
        return frame[9:]

    @staticmethod
    def _extract_tpdu(cemi: bytes) -> bytes:
        """从 cEMI 中提取 TPDU (offset 9+)"""
        return cemi[9:]

    def test_switch_on_tpdu(self):
        """开关 ON: TPDU = [0x81] (GroupValue_Write + data=1)"""
        frame = self._capture_write_frame("1/2/3", True, DATA_TYPE_SWITCH)
        cemi = self._extract_cemi(frame)
        tpdu = self._extract_tpdu(cemi)
        assert tpdu == bytes([0x81])

    def test_switch_off_tpdu(self):
        """开关 OFF: TPDU = [0x80] (GroupValue_Write + data=0)"""
        frame = self._capture_write_frame("1/2/3", False, DATA_TYPE_SWITCH)
        cemi = self._extract_cemi(frame)
        tpdu = self._extract_tpdu(cemi)
        assert tpdu == bytes([0x80])

    def test_percent_tpdu(self):
        """百分比 50: TPDU = [0x80, 50]"""
        frame = self._capture_write_frame("1/2/3", 50, DATA_TYPE_PERCENT)
        cemi = self._extract_cemi(frame)
        tpdu = self._extract_tpdu(cemi)
        assert tpdu == bytes([0x80, 50])

    def test_u16_tpdu(self):
        """U16 值 1000: TPDU = [0x80, 0x03, 0xE8]"""
        frame = self._capture_write_frame("1/2/3", 1000, DATA_TYPE_U16)
        cemi = self._extract_cemi(frame)
        tpdu = self._extract_tpdu(cemi)
        assert tpdu == bytes([0x80, 0x03, 0xE8])

    def test_write_dest_addr_in_cemi(self):
        """写入帧中 cEMI 的 DstAddr 为实际组地址"""
        frame = self._capture_write_frame("1/2/3", True, DATA_TYPE_SWITCH)
        cemi = self._extract_cemi(frame)
        dest = _knx_address_to_bytes("1/2/3")
        assert cemi[6] == dest[0]
        assert cemi[7] == dest[1]

    def test_write_ctrl2_group_addressing(self):
        """写入帧 Ctrl2 = 0x60 (组地址)"""
        frame = self._capture_write_frame("1/2/3", True, DATA_TYPE_SWITCH)
        cemi = self._extract_cemi(frame)
        assert cemi[3] == 0x60

    def test_tunnel_request_body_format(self):
        """Tunnel Request body = ChannelID(1) + SeqCounter(1) + Status(1) + cEMI"""
        frame = self._capture_write_frame("1/2/3", True, DATA_TYPE_SWITCH)
        # KNXnetIP header (6 bytes)
        assert frame[0:2] == bytes([0x10, 0x06])  # version=0x10, hdrlen=0x06
        assert struct.unpack(">H", frame[2:4])[0] == SERVICE_TYPE_TUNNEL_REQUEST
        # Tunnel body at offset 6: ChannelID, SeqCounter, Status
        assert frame[6] == 1  # channel_id
        assert frame[7] == 0  # first sequence counter
        assert frame[8] == 0  # status


class TestKnxTunnelIndicationParse:
    """Tunnel Indication cEMI 解析 (目的地地址 + TPDU 数据提取)"""

    @staticmethod
    def _build_tunnel_indication(
        group_addr: str, apci_byte: int, data_bytes: bytes = b""
    ) -> bytes:
        """构建一个标准的 Tunnel Indication 帧"""
        # cEMI L_Data.ind
        dest = _knx_address_to_bytes(group_addr)
        tpdu = bytes([apci_byte]) + data_bytes
        payload_len = max(0, len(tpdu) - 1)
        cemi = bytes([
            KNX_CEMI_L_BUS_INDICATION,  # 0x2B
            0x00,  # AddInfoLen
            0xBC,  # Ctrl1
            0x60,  # Ctrl2 (group)
        ]) + b"\x00\x00" + dest + bytes([payload_len]) + tpdu

        # KNXnetIP header (6) + tunnel body (3) + cEMI
        total_len = 6 + 3 + len(cemi)
        header = struct.pack(">BBHH", 0x10, 0x06, 0x0420, total_len)
        tunnel_body = bytes([0x01, 0x00, 0x00])  # ChannelID, SeqCounter, Status
        return header + tunnel_body + cemi

    def test_parse_switch_write_on(self):
        """解析 GroupValue_Write ON → group_addr + value=1"""
        client = KNXClient("127.0.0.1")
        frame = self._build_tunnel_indication("1/2/3", 0x81)  # Write ON
        client._handle_tunnel_indication(frame)
        assert client._latest_values.get("1/2/3") == 1

    def test_parse_switch_write_off(self):
        """解析 GroupValue_Write OFF → value=0"""
        client = KNXClient("127.0.0.1")
        frame = self._build_tunnel_indication("1/2/3", 0x80)  # Write OFF
        client._handle_tunnel_indication(frame)
        assert client._latest_values.get("1/2/3") == 0

    def test_parse_percent_write(self):
        """解析 GroupValue_Write 百分比 75 → value=75"""
        client = KNXClient("127.0.0.1")
        frame = self._build_tunnel_indication("5/1/10", 0x80, bytes([75]))
        client._handle_tunnel_indication(frame)
        assert client._latest_values.get("5/1/10") == 75

    def test_parse_u16_write(self):
        """解析 GroupValue_Write U16 值 4660 → value=4660"""
        client = KNXClient("127.0.0.1")
        frame = self._build_tunnel_indication("2/3/4", 0x80, bytes([0x12, 0x34]))
        client._handle_tunnel_indication(frame)
        assert client._latest_values.get("2/3/4") == 0x1234

    def test_parse_group_value_read_no_data(self):
        """解析 GroupValue_Read → 不更新缓存 (无数据)"""
        client = KNXClient("127.0.0.1")
        frame = self._build_tunnel_indication("1/2/3", 0x00, bytes([0x00]))
        client._handle_tunnel_indication(frame)
        assert "1/2/3" not in client._latest_values

    def test_parse_callback_triggered(self):
        """解析后触发回调"""
        client = KNXClient("127.0.0.1")
        received: list[tuple] = []
        client.set_group_value_callback(lambda addr, val: received.append((addr, val)))
        frame = self._build_tunnel_indication("1/2/3", 0x81)  # Write ON
        client._handle_tunnel_indication(frame)
        assert len(received) == 1
        assert received[0] == ("1/2/3", 1)


# ==================== Task #14: ConnectionStateRequest 心跳保活 ====================


class _FakeKnxTransport:
    """模拟 DatagramTransport，捕获 sendto 发送的帧"""

    def __init__(self):
        self.sent: list[bytes] = []
        self._closed = False

    def sendto(self, data, addr):
        self.sent.append(bytes(data))

    def close(self):
        self._closed = True


class TestKnxConnectionStateRequest:
    """ConnectionStateRequest 帧构造正确性 (Task #14)"""

    def _make_client(self, channel_id: int = 5) -> KNXClient:
        c = KNXClient("127.0.0.1")
        c._channel_id = channel_id
        return c

    def test_frame_length(self):
        """帧总长 = 6(头) + 8(HPAI) + 1(ChannelID) + 1(Reserved) = 16 字节"""
        frame = self._make_client()._build_connectionstate_request()
        assert len(frame) == 16

    def test_service_type(self):
        """Service Type = 0x0208 (ConnectionStateRequest)"""
        frame = self._make_client()._build_connectionstate_request()
        assert struct.unpack(">H", frame[2:4])[0] == SERVICE_TYPE_CONNECTIONSTATE_REQUEST

    def test_total_length_field(self):
        """Total Length 字段 = 16"""
        frame = self._make_client()._build_connectionstate_request()
        assert struct.unpack(">H", frame[4:6])[0] == 16

    def test_channel_id_encoded(self):
        """Channel ID 正确编码在 offset 14"""
        frame = self._make_client(channel_id=42)._build_connectionstate_request()
        assert frame[14] == 42

    def test_hpai_structure_length(self):
        """HPAI structure_length = 0x08"""
        frame = self._make_client()._build_connectionstate_request()
        assert frame[6] == 0x08

    def test_hpai_protocol_udp(self):
        """HPAI host_protocol_code = 0x01 (IPv4 UDP)"""
        frame = self._make_client()._build_connectionstate_request()
        assert frame[7] == 0x01

    def test_reserved_byte_zero(self):
        """Reserved 字节 (offset 15) = 0x00"""
        frame = self._make_client()._build_connectionstate_request()
        assert frame[15] == 0x00

    def test_knxnetip_header_version(self):
        """KNXnet/IP 版本 = 0x10"""
        frame = self._make_client()._build_connectionstate_request()
        assert frame[0] == 0x10
        assert frame[1] == 0x06  # header_size


class TestKnxConnectionStateResponse:
    """ConnectionStateResponse 响应处理 (Task #14)"""

    @staticmethod
    def _build_response(channel_id: int, status: int) -> bytes:
        """构建 ConnectionStateResponse 帧 (8 字节)

        结构: KNXnetIP头(6) + ChannelID(1) + Status(1)
        """
        header = struct.pack(">BBHH", 0x10, 0x06,
                             SERVICE_TYPE_CONNECTIONSTATE_RESPONSE, 8)
        return header + bytes([channel_id, status])

    def _make_client(self, channel_id: int = 5) -> KNXClient:
        c = KNXClient("127.0.0.1")
        c._channel_id = channel_id
        c._transport = _FakeKnxTransport()
        c._heartbeat_timeout = 2.0
        return c

    async def test_status_ok_resolves_true(self):
        """status=0x00 (E_NO_ERROR) → future resolved True"""
        client = self._make_client(channel_id=5)
        task = asyncio.create_task(client._send_connectionstate_request())
        await asyncio.sleep(0.01)  # 等待请求发出

        client.handle_packet(self._build_response(5, 0x00))
        result = await task
        assert result is True

    async def test_status_error_resolves_false(self):
        """status=0x21 (E_CONNECTION_ID) → future resolved False"""
        client = self._make_client(channel_id=5)
        task = asyncio.create_task(client._send_connectionstate_request())
        await asyncio.sleep(0.01)

        client.handle_packet(self._build_response(5, 0x21))
        result = await task
        assert result is False

    async def test_timeout_returns_false(self):
        """无响应 → 超时 → False"""
        client = self._make_client(channel_id=5)
        client._heartbeat_timeout = 0.05
        result = await client._send_connectionstate_request()
        assert result is False

    async def test_no_transport_returns_false(self):
        """无 transport → 直接返回 False"""
        client = KNXClient("127.0.0.1")
        client._transport = None
        result = await client._send_connectionstate_request()
        assert result is False

    def test_response_without_pending_future_ignored(self):
        """无 pending future → 响应被忽略 (不崩溃)"""
        client = self._make_client(channel_id=5)
        # 不设置 _heartbeat_response_future
        client.handle_packet(self._build_response(5, 0x00))
        assert client._heartbeat_response_future is None

    def test_response_clears_future_after_resolve(self):
        """响应处理后 future 被清理"""
        client = self._make_client(channel_id=5)

        async def _do():
            task = asyncio.create_task(client._send_connectionstate_request())
            await asyncio.sleep(0.01)
            client.handle_packet(self._build_response(5, 0x00))
            await task

        asyncio.run(_do())
        assert client._heartbeat_response_future is None

    async def test_request_sends_frame_via_transport(self):
        """_send_connectionstate_request 通过 transport.sendto 发送帧"""
        client = self._make_client(channel_id=7)
        task = asyncio.create_task(client._send_connectionstate_request())
        await asyncio.sleep(0.01)
        client.handle_packet(self._build_response(7, 0x00))
        await task

        assert len(client._transport.sent) == 1  # type: ignore
        sent_frame = client._transport.sent[0]  # type: ignore
        assert struct.unpack(">H", sent_frame[2:4])[0] == SERVICE_TYPE_CONNECTIONSTATE_REQUEST
        assert sent_frame[14] == 7  # channel_id


class TestKnxHeartbeatLoop:
    """心跳循环行为 (Task #14)"""

    def _make_client(self, interval: float = 0.001, max_failures: int = 3) -> KNXClient:
        c = KNXClient("127.0.0.1", heartbeat_interval=interval)
        c._connected = True
        c._heartbeat_timeout = 0.05
        c._heartbeat_max_failures = max_failures
        return c

    async def test_success_resets_failures(self):
        """心跳成功 → failures 重置为 0"""
        client = self._make_client(interval=0.001)
        client._heartbeat_failures = 2  # 预设失败计数

        call_count = 0

        async def mock_send():
            nonlocal call_count
            call_count += 1
            return True

        client._send_connectionstate_request = mock_send  # type: ignore

        task = asyncio.create_task(client._heartbeat_loop())
        await asyncio.sleep(0.02)  # 运行几轮
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert call_count > 0
        assert client._heartbeat_failures == 0

    async def test_failure_increments_counter(self):
        """心跳失败 → failures 递增"""
        client = self._make_client(interval=0.001, max_failures=99)

        async def mock_send():
            return False

        client._send_connectionstate_request = mock_send  # type: ignore

        task = asyncio.create_task(client._heartbeat_loop())
        await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert client._heartbeat_failures > 0

    async def test_max_failures_disconnects(self):
        """连续失败达上限 → _connected = False，循环退出"""
        client = self._make_client(interval=0.001, max_failures=3)

        async def mock_send():
            return False

        client._send_connectionstate_request = mock_send  # type: ignore

        await client._heartbeat_loop()  # 循环应自行退出

        assert client._heartbeat_failures == 3
        assert client._connected is False

    async def test_loop_exits_when_disconnected(self):
        """_connected=False → 循环立即退出"""
        client = self._make_client(interval=0.001)
        client._connected = False

        call_count = 0

        async def mock_send():
            nonlocal call_count
            call_count += 1
            return True

        client._send_connectionstate_request = mock_send  # type: ignore

        await client._heartbeat_loop()
        assert call_count == 0  # 未发送任何心跳

    async def test_alternating_success_failure(self):
        """交替成功/失败 → failures 在成功时重置"""
        client = self._make_client(interval=0.001, max_failures=5)
        results = iter([False, True, False, True, False])

        async def mock_send():
            try:
                return next(results)
            except StopIteration:
                client._connected = False
                return True

        client._send_connectionstate_request = mock_send  # type: ignore

        task = asyncio.create_task(client._heartbeat_loop())
        await asyncio.sleep(0.03)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # 最后一次失败后 failures 应为 1 (上一次成功已重置)
        assert client._heartbeat_failures <= 1


class TestKnxHeartbeatLifecycle:
    """心跳任务生命周期管理 (Task #14)"""

    async def test_start_creates_task(self):
        """_start_heartbeat 创建后台任务"""
        client = KNXClient("127.0.0.1", heartbeat_interval=0.1)
        client._connected = True
        client._start_heartbeat()

        assert client._heartbeat_task is not None
        assert not client._heartbeat_task.done()

        client._stop_heartbeat()
        assert client._heartbeat_task is None

    def test_start_disabled_when_interval_zero(self):
        """heartbeat_interval=0 → 不创建心跳任务"""
        client = KNXClient("127.0.0.1", heartbeat_interval=0)
        client._connected = True
        client._start_heartbeat()

        assert client._heartbeat_task is None

    async def test_stop_cancels_running_task(self):
        """_stop_heartbeat 取消正在运行的心跳任务"""
        client = KNXClient("127.0.0.1", heartbeat_interval=0.05)
        client._connected = True
        client._start_heartbeat()

        assert client._heartbeat_task is not None
        client._stop_heartbeat()
        assert client._heartbeat_task is None

    def test_stop_resets_failure_counter(self):
        """_stop_heartbeat 重置失败计数"""
        client = KNXClient("127.0.0.1", heartbeat_interval=0.05)
        client._heartbeat_failures = 5
        client._stop_heartbeat()
        assert client._heartbeat_failures == 0

    async def test_start_idempotent(self):
        """重复 _start_heartbeat 不创建多个任务"""
        client = KNXClient("127.0.0.1", heartbeat_interval=0.1)
        client._connected = True
        client._start_heartbeat()
        first_task = client._heartbeat_task
        client._start_heartbeat()  # 再次调用
        assert client._heartbeat_task is first_task
        client._stop_heartbeat()

    async def test_stop_cancels_pending_response_future(self):
        """_stop_heartbeat 取消未完成的响应 Future"""
        client = KNXClient("127.0.0.1", heartbeat_interval=0.1)
        loop = asyncio.get_running_loop()
        client._heartbeat_response_future = loop.create_future()

        client._stop_heartbeat()
        assert client._heartbeat_response_future is None


class TestKnxHeartbeatConfig:
    """心跳配置 schema 与默认值 (Task #14)"""

    def test_config_schema_includes_heartbeat_interval(self):
        """config_schema 包含 heartbeat_interval 字段"""
        from edgelite.drivers.knx import KNXDriver
        fields = KNXDriver.config_schema["fields"]
        names = [f["name"] for f in fields]
        assert "heartbeat_interval" in names

    def test_default_heartbeat_interval(self):
        """DEFAULT_HEARTBEAT_INTERVAL = 60.0 秒"""
        assert DEFAULT_HEARTBEAT_INTERVAL == 60.0

    def test_default_max_failures(self):
        """DEFAULT_HEARTBEAT_MAX_FAILURES = 3"""
        assert DEFAULT_HEARTBEAT_MAX_FAILURES == 3

    def test_client_default_heartbeat_interval(self):
        """KNXClient 默认 heartbeat_interval = 60.0"""
        client = KNXClient("127.0.0.1")
        assert client._heartbeat_interval == DEFAULT_HEARTBEAT_INTERVAL

    def test_client_custom_heartbeat_interval(self):
        """KNXClient 接受自定义 heartbeat_interval"""
        client = KNXClient("127.0.0.1", heartbeat_interval=30.0)
        assert client._heartbeat_interval == 30.0
