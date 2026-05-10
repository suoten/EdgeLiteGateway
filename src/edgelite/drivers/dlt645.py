"""DL/T 645-2007 多功能电能表通信协议驱动

支持：
- DL/T 645-2007 标准帧格式
- 读数据（功能码0x11）和读后续数据（功能码0x14）
- BCD编码/解码，IEEE 754浮点解析
- RS485半双工串口通信，asyncio.Lock互斥
- 多帧数据自动拼接
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

FRAME_HEAD = 0x68
FRAME_TAIL = 0x16
CTRL_READ_DATA = 0x11
CTRL_READ_NEXT = 0x14
MAX_RETRIES = 3

DLT645_DI_MAP: dict[str, dict[str, Any]] = {
    "voltage_a": {"di": "02010100", "unit": "V", "decimal": 1, "type": "bcd"},
    "voltage_b": {"di": "02010200", "unit": "V", "decimal": 1, "type": "bcd"},
    "voltage_c": {"di": "02010300", "unit": "V", "decimal": 1, "type": "bcd"},
    "current_a": {"di": "02020100", "unit": "A", "decimal": 3, "type": "bcd"},
    "current_b": {"di": "02020200", "unit": "A", "decimal": 3, "type": "bcd"},
    "current_c": {"di": "02020300", "unit": "A", "decimal": 3, "type": "bcd"},
    "active_power": {"di": "02030000", "unit": "W", "decimal": 4, "type": "bcd"},
    "reactive_power": {"di": "02040000", "unit": "var", "decimal": 4, "type": "bcd"},
    "power_factor": {"di": "02060000", "unit": "", "decimal": 4, "type": "bcd"},
    "energy_pos_active": {"di": "00010000", "unit": "kWh", "decimal": 2, "type": "bcd"},
    "energy_neg_active": {"di": "00020000", "unit": "kWh", "decimal": 2, "type": "bcd"},
    "frequency": {"di": "02050000", "unit": "Hz", "decimal": 3, "type": "bcd"},
    "phase_angle": {"di": "02070000", "unit": "\u00b0", "decimal": 1, "type": "bcd"},
}


class Dlt645Driver(DriverPlugin):
    """DL/T 645-2007 电能表通信协议驱动"""

    plugin_name = "dlt645"
    plugin_version = "1.0.0"
    supported_protocols = ["dlt645", "dlt645_2007"]

    def __init__(self):
        self._running = False
        self._serial = None
        self._lock = asyncio.Lock()
        self._config: dict = {}
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        try:
            import serial
        except ImportError:
            raise ImportError("pyserial未安装，请执行: pip install pyserial") from None

        self._config = config
        port = config.get("port", "COM1")
        baud_rate = int(config.get("baud_rate", 2400))
        data_bits = int(config.get("data_bits", 8))
        parity = config.get("parity", "E")
        stop_bits = float(config.get("stop_bits", 1))
        timeout = float(config.get("timeout", 5.0))

        parity_map = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
            "M": serial.PARITY_MARK,
            "S": serial.PARITY_SPACE,
        }
        bytesize_map = {
            5: serial.FIVEBITS,
            6: serial.SIXBITS,
            7: serial.SEVENBITS,
            8: serial.EIGHTBITS,
        }
        stopbits_map = {
            1: serial.STOPBITS_ONE,
            1.5: serial.STOPBITS_ONE_POINT_FIVE,
            2: serial.STOPBITS_TWO,
        }

        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baud_rate,
                bytesize=bytesize_map.get(data_bits, serial.EIGHTBITS),
                parity=parity_map.get(parity, serial.PARITY_EVEN),
                stopbits=stopbits_map.get(stop_bits, serial.STOPBITS_ONE),
                timeout=timeout,
            )
            self._running = True
            logger.info(
                "DL/T 645驱动启动 (port=%s, baud=%d, parity=%s)",
                port,
                baud_rate,
                parity,
            )
        except Exception as e:
            logger.error("DL/T 645驱动启动失败: %s", e)
            raise

    async def stop(self) -> None:
        self._running = False
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception as e:
                logger.warning("串口关闭异常: %s", e)
        self._serial = None
        self._devices.clear()
        logger.info("DL/T 645驱动已停止")

    async def add_device(
        self, device_id: str, config: dict, points: list[dict] | None = None
    ) -> None:
        address = config.get("address", "")
        if not address:
            raise ValueError(f"设备 {device_id} 缺少电表地址(address)")

        di_map = config.get("di_map")
        if di_map is None:
            di_map = dict(DLT645_DI_MAP)

        self._devices[device_id] = {
            "address": address,
            "di_map": di_map,
            "points": points or [],
        }
        logger.info("DL/T 645添加设备: %s (地址=%s)", device_id, address)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        if not self._running or not self._serial:
            return {p: None for p in points}

        device = self._devices.get(device_id)
        if device is None:
            logger.warning("设备未注册: %s", device_id)
            return {p: None for p in points}

        address = device["address"]
        di_map = device["di_map"]

        result: dict[str, Any] = {}
        for point_name in points:
            point_info = di_map.get(point_name)
            if point_info is None:
                logger.warning("未知的测点: %s", point_name)
                result[point_name] = None
                continue

            di = point_info["di"]
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    async with self._lock:
                        frame = self._build_read_frame(address, di)
                        await asyncio.to_thread(self._serial.write, frame)
                        await asyncio.sleep(0.05)

                        response = await asyncio.to_thread(self._read_response)

                    if not response:
                        logger.debug(
                            "测点 %s 第%d次读取无响应",
                            point_name,
                            attempt,
                        )
                        continue

                    if not self._validate_cs(response):
                        logger.debug(
                            "测点 %s 第%d次读取CS校验失败",
                            point_name,
                            attempt,
                        )
                        continue

                    parsed = self._parse_response(response, point_name, point_info)
                    all_data = parsed if parsed is not None else None

                    if all_data is not None:
                        seq = 1
                        while True:
                            more_flag = self._get_more_flag(response)
                            if more_flag != 1:
                                break
                            try:
                                async with self._lock:
                                    next_frame = self._build_read_next_frame(
                                        address,
                                        di,
                                        seq,
                                    )
                                    await asyncio.to_thread(
                                        self._serial.write,
                                        next_frame,
                                    )
                                    await asyncio.sleep(0.05)
                                    next_resp = await asyncio.to_thread(
                                        self._read_response,
                                    )
                                if not next_resp or not self._validate_cs(next_resp):
                                    break
                                next_data = self._parse_response(
                                    next_resp,
                                    point_name,
                                    point_info,
                                )
                                if next_data is None:
                                    break
                                all_data = all_data + next_data
                                seq += 1
                            except Exception:
                                break

                        result[point_name] = all_data
                        break

                except Exception as e:
                    logger.debug(
                        "测点 %s 第%d次读取异常: %s",
                        point_name,
                        attempt,
                        e,
                    )

            if point_name not in result:
                result[point_name] = None
                logger.warning("测点 %s 读取失败(重试%d次)", point_name, MAX_RETRIES)

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """DL/T 645-2007 协议主要用于电能表数据抄读，写操作受限。

        说明：
        - DL/T 645-2007 标准主要用于读取电能数据，写操作支持有限
        - 部分电表支持远程拉合闸、费率设置等写操作，但需要厂商支持
        - 如需写操作，请确认电表是否支持相关功能码

        返回 False 表示写操作不支持。如需此功能，请联系电表厂商获取协议扩展文档。
        """
        return False

    @staticmethod
    def _encode_address(addr_str: str) -> bytes:
        addr_str = addr_str.replace(" ", "")
        if len(addr_str) != 12:
            raise ValueError(f"电表地址长度必须为12位BCD，实际: {len(addr_str)}")

        bcd_bytes = bytes(int(addr_str[i : i + 2], 16) for i in range(0, 12, 2))
        return bcd_bytes[::-1]

    @staticmethod
    def _decode_address(addr_bytes: bytes) -> str:
        if len(addr_bytes) != 6:
            raise ValueError(f"地址字节长度必须为6，实际: {len(addr_bytes)}")
        reversed_bytes = addr_bytes[::-1]
        return "".join(f"{b:02X}" for b in reversed_bytes)

    @staticmethod
    def _add_33h(data: bytes) -> bytes:
        return bytes((b + 0x33) & 0xFF for b in data)

    @staticmethod
    def _sub_33h(data: bytes) -> bytes:
        return bytes((b - 0x33) & 0xFF for b in data)

    @staticmethod
    def _calculate_cs(frame_data: bytes) -> int:
        return sum(frame_data) & 0xFF

    @classmethod
    def _validate_cs(cls, frame: bytes) -> bool:
        if len(frame) < 4:
            return False
        try:
            tail_pos = len(frame) - 1
            if frame[tail_pos] != FRAME_TAIL:
                return False
            cs_pos = tail_pos - 1
            cs = frame[cs_pos]
            data_for_cs = frame[1:cs_pos]
            return cls._calculate_cs(data_for_cs) == cs
        except Exception:
            return False

    @classmethod
    def _build_read_frame(cls, address: str, di: str) -> bytes:
        addr_bytes = cls._encode_address(address)
        di_bytes = bytes.fromhex(di)[::-1]
        data_domain = di_bytes
        data_with_33h = cls._add_33h(data_domain)
        length = len(data_with_33h)

        frame_body = (
            bytes([FRAME_HEAD])
            + addr_bytes
            + bytes([FRAME_HEAD])
            + bytes([CTRL_READ_DATA])
            + bytes([length])
            + data_with_33h
        )
        cs = cls._calculate_cs(frame_body[1:])
        return frame_body + bytes([cs, FRAME_TAIL])

    @classmethod
    def _build_read_next_frame(cls, address: str, di: str, seq: int) -> bytes:
        addr_bytes = cls._encode_address(address)
        di_bytes = bytes.fromhex(di)[::-1]
        seq_byte = (seq & 0xFF).to_bytes(1, "little")
        data_domain = di_bytes + seq_byte
        data_with_33h = cls._add_33h(data_domain)
        length = len(data_with_33h)

        frame_body = (
            bytes([FRAME_HEAD])
            + addr_bytes
            + bytes([FRAME_HEAD])
            + bytes([CTRL_READ_NEXT])
            + bytes([length])
            + data_with_33h
        )
        cs = cls._calculate_cs(frame_body[1:])
        return frame_body + bytes([cs, FRAME_TAIL])

    @staticmethod
    def _decode_bcd(data_bytes: bytes, decimal_places: int = 0) -> float:
        for byte in data_bytes:
            high = (byte >> 4) & 0x0F
            low = byte & 0x0F
            if high > 9 or low > 9:
                logger.warning("BCD非法半字节: 0x%02X", byte)
                return 0.0

        bcd_str = ""
        for b in reversed(data_bytes):
            high = (b >> 4) & 0x0F
            low = b & 0x0F
            bcd_str += f"{high}{low}"

        value = int(bcd_str)
        if decimal_places > 0:
            value = value / (10**decimal_places)
        return float(value)

    @staticmethod
    def _decode_float32(data_bytes: bytes) -> float:
        if len(data_bytes) != 4:
            raise ValueError(f"IEEE 754浮点需要4字节，实际: {len(data_bytes)}")
        return struct.unpack("<f", data_bytes)[0]

    @classmethod
    def _parse_response(
        cls,
        frame: bytes,
        point_name: str,
        point_info: dict,
    ) -> float | None:
        try:
            if len(frame) < 12:
                return None

            length = frame[10]
            data_start = 11
            data_end = data_start + length

            if data_end > len(frame) - 2:
                return None

            encrypted_data = frame[data_start:data_end]
            raw_data = cls._sub_33h(encrypted_data)

            di_len = 4
            di = raw_data[:di_len]
            value_data = raw_data[di_len:]

            data_type = point_info.get("type", "bcd")
            decimal = point_info.get("decimal", 0)

            if data_type == "bcd":
                return cls._decode_bcd(value_data, decimal)
            elif data_type == "float32":
                return cls._decode_float32(value_data[:4])
            elif data_type == "hex":
                return int.from_bytes(value_data, byteorder="little")
            else:
                return cls._decode_bcd(value_data, decimal)
        except Exception as e:
            logger.warning("解析响应帧失败 %s: %s", point_name, e)
            return None

    @staticmethod
    def _get_more_flag(frame: bytes) -> int:
        try:
            ctrl = frame[9]
            if ctrl & 0x20:
                return 1
            return 0
        except Exception:
            return 0

    def _read_response(self) -> bytes:
        if not self._serial or not self._serial.is_open:
            return b""

        buf = bytearray()
        start_found = False
        timeout = self._serial.timeout if self._serial.timeout else 5.0
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            b = self._serial.read(1)
            if not b:
                break

            byte = b[0]
            buf.append(byte)

            if not start_found:
                if byte == FRAME_HEAD and len(buf) == 1:
                    continue
                if byte == FRAME_HEAD and len(buf) > 1 and buf[-2] == FRAME_HEAD:
                    start_found = True
                    head_pos = len(buf) - 2
                    if head_pos > 0:
                        buf = buf[head_pos:]
                    continue
                if len(buf) > 8 and buf[0] == FRAME_HEAD:
                    start_found = True
                continue

            if byte == FRAME_TAIL and len(buf) >= 12:
                break

        return bytes(buf)

    async def discover_devices(self, config: dict) -> list[dict]:
        try:
            import serial.tools.list_ports
        except ImportError:
            return []

        ports = serial.tools.list_ports.comports()
        return [
            {
                "device_id": p.device,
                "name": p.description,
                "protocol": "dlt645",
                "details": {
                    "hwid": p.hwid,
                    "manufacturer": p.manufacturer,
                },
            }
            for p in ports
        ]
