"""IEC 60870-5-104 电力远动规约驱动"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import struct
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from edgelite.drivers.base import DriverPlugin
from edgelite.engine.event_bus import PointUpdateEvent

logger = logging.getLogger(__name__)

START_BYTE = 0x68

U_FRAME_STARTDT_ACT = 0x07
U_FRAME_STARTDT_CON = 0x0B
U_FRAME_STOPDT_ACT = 0x13
U_FRAME_STOPDT_CON = 0x17
U_FRAME_TESTFR_ACT = 0x2B
U_FRAME_TESTFR_CON = 0x2F

TI_M_SP_NA = 1
TI_M_SP_TB = 30
TI_M_DP_NA = 3
TI_M_DP_TB = 31
TI_M_ME_NA = 9
TI_M_ME_NB = 11
TI_M_ME_NC = 13
TI_M_ME_TD = 34
TI_C_SC_NA = 45
TI_C_DC_NA = 46
TI_C_IC_NA_1 = 100
TI_C_CS_NA_1 = 103

COT_PERIODIC = 1
COT_BACKGROUND = 2
COT_SPONTANEOUS = 3
COT_INITIALIZED = 4
COT_REQUEST = 5
COT_ACTIVATION = 6
COT_ACTIVATION_CON = 7
COT_DEACTIVATION = 8
COT_DEACTIVATION_CON = 9
COT_INTERROGATED_BY_STATION = 20

SBO_SELECT = 0x01
SBO_EXECUTE = 0x02

QUALITY_IV = 0x20
QUALITY_NT = 0x10
QUALITY_SB = 0x08
QUALITY_BL = 0x04
QUALITY_OV = 0x02


def _cp56time2a_to_datetime(data: bytes, offset: int) -> datetime:
    ms = struct.unpack_from("<H", data, offset)[0]
    minute = data[offset + 2] & 0x3F
    (data[offset + 2] >> 7) & 0x01
    hour = data[offset + 3] & 0x1F
    (data[offset + 3] >> 5) & 0x07
    day = data[offset + 4] & 0x1F
    (data[offset + 4] >> 5) & 0x07
    month = data[offset + 5] & 0x0F
    year = data[offset + 6] & 0x7F
    year += 2000 if year < 70 else 1900
    try:
        dt = datetime(year, month, day, hour, minute, ms // 1000, (ms % 1000) * 1000, tzinfo=UTC)
    except ValueError:
        dt = datetime.now(UTC)
    return dt


class Iec104Driver(DriverPlugin):
    """IEC 60870-5-104 电力远动规约驱动"""

    plugin_name = "iec104"
    plugin_version = "1.0.0"
    supported_protocols = ["iec104"]
    config_schema = {
        "description": "IEC 60870-5-104 电力远动规约，用于与电力SCADA系统通信",
        "fields": [
            {"name": "host", "type": "string", "label": "IP地址", "description": "SCADA或保护装置IP地址", "default": "127.0.0.1", "required": True},
            {"name": "port", "type": "integer", "label": "端口", "description": "IEC 104默认端口2404", "default": 2404},
            {"name": "asdu_addr", "type": "integer", "label": "ASDU地址", "description": "ASDU公共地址", "default": 1},
            {"name": "heartbeat_interval", "type": "number", "label": "心跳间隔(秒)", "description": "T3超时时间，心跳发送间隔", "default": 30.0},
        ],
    }

    def __init__(self):
        self._running = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ssn: int = 0
        self._rsn: int = 0
        self._connect_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None
        self._ioa_map: dict[int, str] = {}
        self._data_callback: Callable | None = None
        self._event_bus: Any = None
        self._device_id: str = ""
        self._host: str = "127.0.0.1"
        self._port: int = 2404
        self._asdu_addr: int = 1
        self._asdu_addr_length: int = 2
        self._cause_of_tx_length: int = 2
        self._heartbeat_interval: float = 30.0
        self._t1_timeout: float = 15.0
        self._t2_timeout: float = 10.0
        self._t3_timeout: float = 20.0
        self._clock_sync: bool = True
        self._reconnect_delay: float = 1.0
        self._max_reconnect_delay: float = 60.0
        self._testfr_sent: bool = False
        self._last_received_ssn: int = 0
        self._s_frame_needed: bool = False
        self._connected: bool = False
        self._startdt_confirmed: bool = False
        self._sbo_select_timeout: float = 10.0
        self._sbo_selected_ioa: int | None = None
        self._sbo_select_event: asyncio.Event = asyncio.Event()
        self._sbo_execute_event: asyncio.Event = asyncio.Event()
        self._lock: asyncio.Lock = asyncio.Lock()

    def on_data(self, callback: Callable) -> None:
        self._data_callback = callback

    async def start(self, config: dict) -> None:
        self._running = True
        iec_cfg = config.get("iec104", config)
        self._host = config.get("host", self._host)
        self._port = config.get("port", iec_cfg.get("default_port", 2404))
        self._asdu_addr = config.get("asdu_addr", 1)
        self._asdu_addr_length = iec_cfg.get("asdu_addr_length", 2)
        self._cause_of_tx_length = iec_cfg.get("cause_of_tx_length", 2)
        self._heartbeat_interval = iec_cfg.get("heartbeat_interval", 30.0)
        self._t1_timeout = iec_cfg.get("t1_timeout", 15.0)
        self._t2_timeout = iec_cfg.get("t2_timeout", 10.0)
        self._t3_timeout = iec_cfg.get("t3_timeout", 20.0)
        self._clock_sync = iec_cfg.get("clock_sync", True)
        self._device_id = config.get("device_id", "")
        self._event_bus = config.get("event_bus")
        self._ioa_map = config.get("ioa_map", {})
        self._connect_task = asyncio.create_task(self._connect_loop())
        logger.info("IEC 104驱动启动, 目标: %s:%d", self._host, self._port)

    async def stop(self) -> None:
        self._running = False
        for task in (self._connect_task, self._heartbeat_task, self._receive_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._connect_task = None
        self._heartbeat_task = None
        self._receive_task = None
        await self._close_connection()
        logger.info("IEC 104驱动停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """IEC 104 协议使用事件驱动模式，数据通过 TCP 连接被动接收，不支持主动轮询读取。

        说明：
        - IEC 104 是事件驱动的协议，主站发起总召唤后，从站周期性上报数据
        - 本驱动通过订阅模式接收数据，数据通过 _publish_point 发布到事件总线
        - 如需获取最新数据，请使用事件订阅或配置更短的上报周期

        返回空字典是协议设计特性，不是错误。
        如需读取数据，请通过 WebSocket 订阅 /ws/v1/realtime 实时获取数据更新。
        """
        return {}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        ioa = None
        for ioa_key, name in self._ioa_map.items():
            if name == point:
                ioa = ioa_key
                break
        if ioa is None:
            for ioa_key, _name in self._ioa_map.items():
                if str(ioa_key) == point:
                    ioa = ioa_key
                    break
        if ioa is None:
            logger.warning("未找到测点IOA映射: %s", point)
            return False
        if not self._connected or not self._startdt_confirmed:
            logger.warning("IEC 104未连接，无法遥控: %s", point)
            return False
        try:
            select_ok = await self._send_sbo_select(ioa, value)
            if not select_ok:
                logger.warning("SBO选择失败: IOA=%d", ioa)
                return False
            exec_ok = await self._send_sbo_execute(ioa, value)
            if not exec_ok:
                logger.warning("SBO执行失败: IOA=%d", ioa)
                return False
            return True
        except Exception as e:
            logger.error("SBO遥控异常: IOA=%d - %s", ioa, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """IEC 104 协议不支持自动设备发现。

        说明：
        - IEC 104 是主从模式协议，需要预先配置从站 IP:Port 和 ASDU 地址
        - 设备发现需要在子站侧手动配置或使用网络扫描工具
        - 本方法返回空列表是协议限制，不是错误

        如需添加设备，请在 EdgeLite 管理界面手动输入设备地址信息。
        """
        return []

    def _next_ssn(self) -> int:
        ssn = self._ssn
        self._ssn = (self._ssn + 1) % 32768
        return ssn

    def _next_rsn(self) -> int:
        rsn = self._rsn
        self._rsn = (self._rsn + 1) % 32768
        return rsn

    def _build_u_frame(self, u_type: int) -> bytes:
        return struct.pack("BBBBB", START_BYTE, 4, u_type, 0x00, 0x00)

    def _build_s_frame(self) -> bytes:
        rsn = self._rsn
        return struct.pack("=BBHH", START_BYTE, 4, 0x0001, rsn << 1)

    def _build_i_frame(self, asdu: bytes) -> bytes:
        ssn = self._next_ssn()
        rsn = self._rsn
        control = struct.pack("<HH", ssn << 1, rsn << 1)
        payload = control + asdu
        length = len(payload)
        return struct.pack("=BB", START_BYTE, length) + payload

    def _build_asdu_header(
        self, ti: int, sq: int, num_obj: int, cot: int, oa: int, asdu_addr: int
    ) -> bytes:
        header = struct.pack("BBB", ti, (sq << 7) | (num_obj & 0x7F), cot & 0x3F)
        if self._cause_of_tx_length == 2:
            header += struct.pack("B", (cot >> 8) & 0x03)
        header += struct.pack("B", oa)
        if self._asdu_addr_length == 2:
            header += struct.pack("<H", asdu_addr)
        else:
            header += struct.pack("B", asdu_addr & 0xFF)
        return header

    async def _send_frame(self, frame: bytes) -> None:
        if self._writer is None:
            return
        try:
            self._writer.write(frame)
            await self._writer.drain()
        except Exception as e:
            logger.error("发送帧失败: %s", e)
            await self._close_connection()

    async def _send_startdt_act(self) -> None:
        frame = self._build_u_frame(U_FRAME_STARTDT_ACT)
        await self._send_frame(frame)
        logger.debug("发送STARTDT ACT")

    async def _send_stopdt_act(self) -> None:
        frame = self._build_u_frame(U_FRAME_STOPDT_ACT)
        await self._send_frame(frame)
        logger.debug("发送STOPDT ACT")

    async def _send_testfr(self) -> None:
        frame = self._build_u_frame(U_FRAME_TESTFR_ACT)
        self._testfr_sent = True
        await self._send_frame(frame)
        logger.debug("发送TESTFR ACT")

    async def _send_s_frame(self) -> None:
        frame = self._build_s_frame()
        await self._send_frame(frame)
        logger.debug("发送S帧, RSN=%d", self._rsn)

    async def _send_general_interrogation(self) -> None:
        asdu_header = self._build_asdu_header(
            ti=TI_C_IC_NA_1,
            sq=0,
            num_obj=1,
            cot=COT_ACTIVATION,
            oa=0,
            asdu_addr=self._asdu_addr,
        )
        ioa_bytes = struct.pack("<H", 0)
        ioc = struct.pack("B", SBO_SELECT)
        asdu = asdu_header + ioa_bytes + ioc
        frame = self._build_i_frame(asdu)
        await self._send_frame(frame)
        logger.info("发送总召唤命令, ASDU_ADDR=%d", self._asdu_addr)

    async def _send_clock_sync(self) -> None:
        now = datetime.now(UTC)
        ms = now.second * 1000 + now.microsecond // 1000
        minute = now.minute
        hour = now.hour
        day = now.day
        month = now.month
        year = now.year - 2000 if now.year >= 2000 else now.year - 1900
        dow = now.isoweekday()
        day_with_dow = (dow << 5) | day
        time_bytes = struct.pack(
            "<HBBBBBB",
            ms,
            minute | 0x00,
            hour | 0x00,
            day_with_dow,
            month,
            year,
        )
        asdu_header = self._build_asdu_header(
            ti=TI_C_CS_NA_1,
            sq=0,
            num_obj=1,
            cot=COT_ACTIVATION,
            oa=0,
            asdu_addr=self._asdu_addr,
        )
        ioa_bytes = struct.pack("<H", 0)
        asdu = asdu_header + ioa_bytes + time_bytes
        frame = self._build_i_frame(asdu)
        await self._send_frame(frame)
        logger.info("发送时钟同步命令")

    async def _send_sbo_select(self, ioa: int, value: Any) -> bool:
        is_double = isinstance(value, (int, float)) and abs(value) > 1
        ti = TI_C_DC_NA if is_double else TI_C_SC_NA
        asdu_header = self._build_asdu_header(
            ti=ti,
            sq=0,
            num_obj=1,
            cot=COT_ACTIVATION,
            oa=0,
            asdu_addr=self._asdu_addr,
        )
        ioa_bytes = struct.pack("<H", ioa & 0xFFFF)
        if is_double:
            dco = 0x01 if value >= 1 else 0x02
            ioc = struct.pack("BB", dco, SBO_SELECT)
        else:
            sco = 0x01 if bool(value) else 0x00
            ioc = struct.pack("BB", sco, SBO_SELECT)
        asdu = asdu_header + ioa_bytes + ioc
        frame = self._build_i_frame(asdu)
        self._sbo_select_event.clear()
        self._sbo_selected_ioa = ioa
        await self._send_frame(frame)
        try:
            await asyncio.wait_for(self._sbo_select_event.wait(), timeout=self._sbo_select_timeout)
            return True
        except TimeoutError:
            logger.warning("SBO选择超时: IOA=%d", ioa)
            self._sbo_selected_ioa = None
            return False

    async def _send_sbo_execute(self, ioa: int, value: Any) -> bool:
        is_double = isinstance(value, (int, float)) and abs(value) > 1
        ti = TI_C_DC_NA if is_double else TI_C_SC_NA
        asdu_header = self._build_asdu_header(
            ti=ti,
            sq=0,
            num_obj=1,
            cot=COT_ACTIVATION,
            oa=0,
            asdu_addr=self._asdu_addr,
        )
        ioa_bytes = struct.pack("<H", ioa & 0xFFFF)
        if is_double:
            dco = 0x01 if value >= 1 else 0x02
            ioc = struct.pack("BB", dco, SBO_EXECUTE)
        else:
            sco = 0x01 if bool(value) else 0x00
            ioc = struct.pack("BB", sco, SBO_EXECUTE)
        asdu = asdu_header + ioa_bytes + ioc
        frame = self._build_i_frame(asdu)
        self._sbo_execute_event.clear()
        await self._send_frame(frame)
        try:
            await asyncio.wait_for(self._sbo_execute_event.wait(), timeout=self._sbo_select_timeout)
            return True
        except TimeoutError:
            logger.warning("SBO执行超时: IOA=%d", ioa)
            return False

    def _parse_asdu(self, data: bytes) -> list[dict]:
        results = []
        if len(data) < 6:
            return results
        offset = 0
        ti = data[offset]
        offset += 1
        sq_num = data[offset]
        offset += 1
        sq = (sq_num >> 7) & 0x01
        num_obj = sq_num & 0x7F
        if num_obj == 0:
            return results
        cot_low = data[offset]
        offset += 1
        if self._cause_of_tx_length == 2:
            cot_high = data[offset] & 0x03
            offset += 1
            cot = cot_low | (cot_high << 8)
        else:
            cot = cot_low
            cot_high = 0
        data[offset]
        offset += 1
        if self._asdu_addr_length == 2:
            asdu_addr = struct.unpack_from("<H", data, offset)[0]
            offset += 2
        else:
            asdu_addr = data[offset]
            offset += 1

        for i in range(num_obj):
            try:
                ioa_low = data[offset]
                ioa_mid = data[offset + 1]
                offset += 2
                if self._asdu_addr_length == 2:
                    ioa_high = data[offset]
                    offset += 1
                    ioa = ioa_low | (ioa_mid << 8) | (ioa_high << 16)
                else:
                    ioa = ioa_low | (ioa_mid << 8)
                base_ioa = ioa

                if sq == 1 and i > 0:
                    ioa = base_ioa + i

                point = self._parse_information_object(ti, data, offset, ioa, cot, asdu_addr)
                if point is not None:
                    results.append(point)
                    offset = point.get("_next_offset", offset)
                    if "_next_offset" in point:
                        del point["_next_offset"]
                else:
                    break
            except (IndexError, struct.error) as e:
                logger.warning("ASDU解析越界: TI=%d, obj=%d - %s", ti, i, e)
                break
        return results

    def _parse_information_object(
        self, ti: int, data: bytes, offset: int, ioa: int, cot: int, asdu_addr: int
    ) -> dict | None:
        result: dict[str, Any] = {
            "ioa": ioa,
            "ti": ti,
            "cot": cot,
            "asdu_addr": asdu_addr,
        }

        if ti == TI_M_SP_NA:
            siq = data[offset]
            value = siq & 0x01
            quality = self._decode_quality(siq >> 1)
            result.update(value=value, quality=quality, data_type="bool")
            result["_next_offset"] = offset + 1

        elif ti == TI_M_SP_TB:
            siq = data[offset]
            value = siq & 0x01
            quality = self._decode_quality(siq >> 1)
            ts = _cp56time2a_to_datetime(data, offset + 1)
            result.update(value=value, quality=quality, timestamp=ts.isoformat(), data_type="bool")
            result["_next_offset"] = offset + 8

        elif ti == TI_M_DP_NA:
            diq = data[offset]
            value = diq & 0x03
            quality = self._decode_quality(diq >> 2)
            result.update(value=value, quality=quality, data_type="int")
            result["_next_offset"] = offset + 1

        elif ti == TI_M_DP_TB:
            diq = data[offset]
            value = diq & 0x03
            quality = self._decode_quality(diq >> 2)
            ts = _cp56time2a_to_datetime(data, offset + 1)
            result.update(value=value, quality=quality, timestamp=ts.isoformat(), data_type="int")
            result["_next_offset"] = offset + 8

        elif ti == TI_M_ME_NA:
            nva = struct.unpack_from("<h", data, offset)[0]
            value = nva / 32768.0
            qds = data[offset + 2]
            quality = self._decode_quality(qds >> 1)
            result.update(value=value, quality=quality, data_type="float")
            result["_next_offset"] = offset + 3

        elif ti == TI_M_ME_NB:
            sva = struct.unpack_from("<i", data, offset)[0]
            qds = data[offset + 3]
            quality = self._decode_quality(qds >> 1)
            result.update(value=float(sva), quality=quality, data_type="float")
            result["_next_offset"] = offset + 4

        elif ti == TI_M_ME_NC:
            fval = struct.unpack_from("<f", data, offset)[0]
            qds = data[offset + 4]
            quality = self._decode_quality(qds >> 1)
            result.update(value=fval, quality=quality, data_type="float")
            result["_next_offset"] = offset + 5

        elif ti == TI_M_ME_TD:
            fval = struct.unpack_from("<f", data, offset)[0]
            qds = data[offset + 4]
            quality = self._decode_quality(qds >> 1)
            ts = _cp56time2a_to_datetime(data, offset + 5)
            result.update(value=fval, quality=quality, timestamp=ts.isoformat(), data_type="float")
            result["_next_offset"] = offset + 12

        elif ti == TI_C_SC_NA:
            sco = data[offset]
            value = sco & 0x01
            qu = (sco >> 2) & 0x1F
            sbo = data[offset + 1]
            result.update(value=value, sbo_qualifier=qu, sbo_command=sbo, data_type="bool")
            result["_next_offset"] = offset + 2

        elif ti == TI_C_DC_NA:
            dco = data[offset]
            value = dco & 0x03
            qu = (dco >> 2) & 0x1F
            sbo = data[offset + 1]
            result.update(value=value, sbo_qualifier=qu, sbo_command=sbo, data_type="int")
            result["_next_offset"] = offset + 2

        elif ti == TI_C_IC_NA_1:
            ioc = data[offset]
            result.update(value=ioc, data_type="int")
            result["_next_offset"] = offset + 1

        elif ti == TI_C_CS_NA_1:
            result.update(value="clock_sync", data_type="str")
            result["_next_offset"] = offset + 7

        else:
            logger.warning("未支持的类型标识: TI=%d", ti)
            return None

        return result

    @staticmethod
    def _decode_quality(qds: int) -> str:
        parts = []
        if qds & (QUALITY_OV >> 1):
            parts.append("overflow")
        if qds & (QUALITY_BL >> 1):
            parts.append("blocked")
        if qds & (QUALITY_SB >> 1):
            parts.append("substituted")
        if qds & (QUALITY_NT >> 1):
            parts.append("not_topical")
        if qds & (QUALITY_IV >> 1):
            parts.append("invalid")
        return ",".join(parts) if parts else "good"

    async def _connect_loop(self) -> None:
        delay = self._reconnect_delay
        while self._running:
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self._host, self._port),
                    timeout=self._t1_timeout,
                )
                self._connected = True
                self._startdt_confirmed = False
                self._ssn = 0
                self._rsn = 0
                self._last_received_ssn = 0
                self._s_frame_needed = False
                delay = self._reconnect_delay
                logger.info("IEC 104 TCP连接成功: %s:%d", self._host, self._port)

                await self._send_startdt_act()

                self._receive_task = asyncio.create_task(self._receive_loop())
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                if self._receive_task:
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._receive_task

                if self._heartbeat_task and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._heartbeat_task
                self._heartbeat_task = None

            except TimeoutError:
                logger.warning("IEC 104连接超时: %s:%d", self._host, self._port)
            except ConnectionRefusedError:
                logger.warning("IEC 104连接被拒绝: %s:%d", self._host, self._port)
            except OSError as e:
                logger.warning("IEC 104连接错误: %s:%d - %s", self._host, self._port, e)
            except Exception as e:
                logger.error("IEC 104连接异常: %s", e)

            await self._close_connection()
            if not self._running:
                break
            logger.info("IEC 104 %d秒后重连...", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._max_reconnect_delay)

    async def _heartbeat_loop(self) -> None:
        last_send_time = asyncio.get_running_loop().time()
        while self._running and self._connected:
            try:
                await asyncio.sleep(min(self._t3_timeout, self._heartbeat_interval) / 2)
                if not self._connected:
                    break
                now = asyncio.get_running_loop().time()
                if self._s_frame_needed:
                    await self._send_s_frame()
                    self._s_frame_needed = False
                    last_send_time = now
                    continue
                elapsed = now - last_send_time
                if elapsed >= self._t3_timeout:
                    if self._testfr_sent:
                        logger.warning("TESTFR超时未收到确认，触发重连")
                        await self._close_connection()
                        return
                    await self._send_testfr()
                    last_send_time = now
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("心跳循环异常: %s", e)
                break

    async def _receive_loop(self) -> None:
        while self._running and self._connected and self._reader is not None:
            try:
                start_byte = await self._reader.readexactly(1)
                if start_byte[0] != START_BYTE:
                    continue
                length_byte = await self._reader.readexactly(1)
                apdu_length = length_byte[0]
                if apdu_length < 4 or apdu_length > 253:
                    logger.warning("无效APDU长度: %d", apdu_length)
                    continue
                rest = await self._reader.readexactly(apdu_length)
                frame = start_byte + length_byte + rest
                await self._handle_frame(frame)
            except asyncio.IncompleteReadError:
                logger.warning("IEC 104连接断开（读取不完整）")
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("接收循环异常: %s", e)
                break
        await self._close_connection()

    async def _handle_frame(self, frame: bytes) -> None:
        if len(frame) < 6:
            return
        frame[1]
        ctrl_byte1 = frame[2]
        frame_type = ctrl_byte1 & 0x03

        if frame_type == 0x01:
            ssn_raw = struct.unpack_from("<H", frame, 2)[0]
            rsn_raw = struct.unpack_from("<H", frame, 4)[0]
            received_ssn = ssn_raw >> 1
            expected_ssn = self._rsn % 32768
            if received_ssn != expected_ssn:
                logger.warning("SSN不连续: 期望%d 收到%d (可能丢帧)", expected_ssn, received_ssn)
            self._rsn = (received_ssn + 1) % 32768
            self._s_frame_needed = True
            asdu_data = frame[6:]
            points = self._parse_asdu(asdu_data)
            for pt in points:
                await self._publish_point(pt)
        elif frame_type == 0x03:
            u_type = ctrl_byte1
            if u_type == U_FRAME_TESTFR_CON:
                self._testfr_sent = False
                logger.debug("收到TESTFR确认")
            elif u_type == U_FRAME_STARTDT_CON:
                self._startdt_confirmed = True
                logger.info("收到STARTDT确认，连接就绪")
                await self._send_general_interrogation()
                if self._clock_sync:
                    await self._send_clock_sync()
            elif u_type == U_FRAME_STOPDT_CON:
                logger.info("收到STOPDT确认")
            elif u_type == U_FRAME_TESTFR_ACT:
                con_frame = self._build_u_frame(U_FRAME_TESTFR_CON)
                await self._send_frame(con_frame)
                logger.debug("收到TESTFR ACT，回复确认")
        elif frame_type == 0x02:
            rsn_raw = struct.unpack_from("<H", frame, 4)[0]
            acked_ssn = rsn_raw >> 1
            logger.debug("收到S帧, 确认SSN=%d", acked_ssn)

    async def _publish_point(self, point: dict) -> None:
        ioa = point.get("ioa", 0)
        value = point.get("value", 0.0)
        quality = point.get("quality", "good")
        ti = point.get("ti", 0)
        cot = point.get("cot", 0)

        point_name = self._ioa_map.get(ioa, str(ioa))

        if self._data_callback is not None:
            try:
                if asyncio.iscoroutinefunction(self._data_callback):
                    await self._data_callback(self._device_id, point_name, value, quality)
                else:
                    self._data_callback(self._device_id, point_name, value, quality)
            except Exception as e:
                logger.error("数据回调异常: %s", e)

        if self._event_bus is not None:
            try:
                event = PointUpdateEvent(
                    device_id=self._device_id,
                    point_name=point_name,
                    value=float(value) if isinstance(value, (int, float)) else 0.0,
                    quality=quality,
                )
                await self._event_bus.publish(event)
            except Exception as e:
                logger.error("EventBus发布异常: %s", e)

        logger.debug(
            "测点更新: IOA=%d, TI=%d, COT=%d, value=%s, quality=%s",
            ioa,
            ti,
            cot,
            value,
            quality,
        )

    async def _close_connection(self) -> None:
        self._connected = False
        self._startdt_confirmed = False
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.debug("IEC104写入端关闭失败: %s", e)
            self._writer = None
        self._reader = None
