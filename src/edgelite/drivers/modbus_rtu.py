"""Modbus RTU驱动 - 基于pymodbus串口实现"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any

try:
    import pymodbus
    from pymodbus.client import AsyncModbusSerialClient

    _PYMODBUS_AVAILABLE = True
except ImportError:
    pymodbus = None
    AsyncModbusSerialClient = None
    _PYMODBUS_AVAILABLE = False

try:
    from pymodbus.exceptions import ModbusException
except ImportError:
    ModbusException = Exception

from edgelite.constants import _DEVICE_CONNECT_TIMEOUT
from edgelite.drivers.base import DriverPlugin

if _PYMODBUS_AVAILABLE:
    _PYMODBUS_MAJOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[0])
    _PYMODBUS_MINOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[1]) if _PYMODBUS_MAJOR >= 3 else 0
    _PYMODBUS_37_PLUS = _PYMODBUS_MAJOR > 3 or (_PYMODBUS_MAJOR == 3 and _PYMODBUS_MINOR >= 7)
else:
    _PYMODBUS_MAJOR = 3
    _PYMODBUS_MINOR = 0
    _PYMODBUS_37_PLUS = False

logger = logging.getLogger(__name__)

# FIXED: pymodbus 3.7+ 不再接受 slave/unit 作为关键字参数，需使用 client.slave_id
_SLAVE_KWARG_NAME: str | None = None


def _detect_slave_kwarg_name() -> str | None:
    """根据pymodbus版本号检测正确的slave参数名"""
    if _PYMODBUS_MAJOR < 3:
        return "slave"
    if _PYMODBUS_MAJOR == 3 and _PYMODBUS_MINOR < 7:
        return "unit"
    return None  # pymodbus 3.7+, 使用 client.slave_id


def _slave_kwarg(slave_id: int) -> dict:
    global _SLAVE_KWARG_NAME
    if _SLAVE_KWARG_NAME is None:
        _SLAVE_KWARG_NAME = _detect_slave_kwarg_name()
    if _SLAVE_KWARG_NAME is None:
        return {}
    return {_SLAVE_KWARG_NAME: slave_id}


def _set_client_slave_id(client: Any, slave_id: int) -> None:
    """为 pymodbus 3.7+ 设置 client.slave_id"""
    if _SLAVE_KWARG_NAME is None and hasattr(client, "slave_id"):
        client.slave_id = slave_id


def _read_kwargs(count: int, slave_id: int) -> dict:
    kwargs = _slave_kwarg(slave_id)
    kwargs["count"] = count  # FIXED: 始终传count
    return kwargs


REGISTER_TYPES = {
    "coil": (0, 1),
    "discrete": (1, 1),
    "holding": (3, 2),
    "input": (4, 2),
}

DATA_TYPE_REGS = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "float64": 4,
    "string": 1,
}

# 字节序→(寄存器打包格式, 浮点/整数解包格式)
_BYTE_ORDER_FMT = {
    "ABCD": (">", ">"),   # Big-Endian (默认)
    "BADC": ("<", ">"),   # Big-Endian Byte Swap
    "CDAB": (">", "<"),   # Little-Endian Word Swap
    "DCBA": ("<", "<"),   # Little-Endian (完全反转)
}


class ModbusRtuDriver(DriverPlugin):

    plugin_name = "modbus_rtu"
    plugin_version = "1.0.0"
    supported_protocols = ["modbus-rtu"]
    config_schema = {
        "description": "Modbus RTU serial protocol for RS485/RS232 bus devices",
        "fields": [
            {"name": "port", "type": "string", "label": "Serial Port", "description": "Serial device path, e.g. COM3 or /dev/ttyUSB0", "default": "/dev/ttyUSB0", "required": True},
            {"name": "baudrate", "type": "integer", "label": "Baud Rate", "description": "Communication baud rate", "default": 9600, "required": True},
            {"name": "parity", "type": "string", "label": "Parity", "description": "Parity check: N/E/O", "default": "N"},
            {"name": "stopbits", "type": "integer", "label": "Stop Bits", "description": "Stop bits: 1 or 2", "default": 1},
            {"name": "bytesize", "type": "integer", "label": "Data Bits", "description": "Data bits: 7 or 8", "default": 8},
            {"name": "unit_id", "type": "integer", "label": "Slave ID", "description": "Device slave address (Unit ID)", "default": 1, "required": True},
            {"name": "timeout", "type": "number", "label": "Timeout (s)", "description": "Connection and read timeout", "default": 3.0},
            {"name": "byte_order", "type": "string", "label": "Byte Order", "description": "Multi-register byte order: ABCD(Big-Endian), BADC, CDAB, DCBA(Little-Endian)", "default": "ABCD", "options": ["ABCD", "BADC", "CDAB", "DCBA"]},
            {"name": "reconnect_interval", "type": "number", "label": "Reconnect Interval (s)", "description": "Seconds between reconnection attempts", "default": 10.0},
            {"name": "max_reconnect_attempts", "type": "integer", "label": "Max Reconnect Attempts", "description": "Maximum consecutive reconnection attempts before giving up (0=unlimited)", "default": 0},
        ],
    }

    def __init__(self):
        self._running = False
        self._clients: dict[str, Any] = {}
        self._device_configs: dict[str, dict] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._retry_count: dict[str, int] = {}
        self._retry_lock = asyncio.Lock()
        self._read_fail_tracker: dict[tuple[str, str], tuple[float, float]] = {}

    _LOG_INTERVAL = 60.0

    async def start(self, config: dict) -> None:
        if not _PYMODBUS_AVAILABLE:
            logger.warning("pymodbus未安装，Modbus RTU驱动无法正常工作")
        self._running = True
        logger.info("Modbus RTU驱动启动")

    async def stop(self) -> None:
        self._running = False
        for device_id, client in self._clients.items():
            if client.connected:
                client.close()
                logger.info("Modbus RTU连接关闭: %s", device_id)
        self._clients.clear()
        logger.info("Modbus RTU驱动停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        self._device_configs[device_id] = config
        self._device_points[device_id] = points

        if not _PYMODBUS_AVAILABLE:
            self._log_error(device_id, "CONN_FAILED", "pymodbus not installed, cannot create serial connection")
            return

        port = config.get("port", "/dev/ttyUSB0")
        baudrate = config.get("baudrate", 9600)
        parity = config.get("parity", "N")
        stopbits = config.get("stopbits", 1)
        bytesize = config.get("bytesize", 8)
        timeout = config.get("timeout", 3.0)

        client = AsyncModbusSerialClient(
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
            timeout=timeout,
        )
        self._clients[device_id] = client

        try:
            connected = await client.connect()
            if connected:
                logger.info("Modbus RTU连接成功: %s (%s@%d)", device_id, port, baudrate)
                self._retry_count[device_id] = 0
            else:
                self._log_error(device_id, "CONN_FAILED", f"connect failed to {port}@{baudrate}")
        except Exception as e:
            self._log_error(device_id, "CONN_FAILED", str(e))

    async def remove_device(self, device_id: str) -> None:
        client = self._clients.pop(device_id, None)
        if client and client.connected:
            client.close()
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._retry_count.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值（支持批量合并读取和自动分包）"""
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            await self._try_reconnect(device_id)
            return {}

        config = self._device_configs.get(device_id, {})
        slave_id = config.get("unit_id", 1)
        byte_order = config.get("byte_order", "ABCD")
        device_points = self._device_points.get(device_id, [])

        # 构建测点定义映射
        pt_map: dict[str, dict] = {}
        for point_name in points:
            pt_def = next((p for p in device_points if p.get("name") == point_name), None)
            if pt_def is not None:
                pt_map[point_name] = pt_def

        if not pt_map:
            return {}

        # 分离 coil/discrete（位操作）和 holding/input（寄存器操作）
        bit_points: dict[str, dict] = {}
        reg_points: dict[str, dict] = {}
        for name, pt_def in pt_map.items():
            reg_type = pt_def.get("register_type", "holding")
            if reg_type in ("coil", "discrete"):
                bit_points[name] = pt_def
            else:
                reg_points[name] = pt_def

        result: dict[str, Any] = {}

        # 位类型测点逐个读取
        for point_name, pt_def in bit_points.items():
            try:
                value = await self._read_single_point(client, slave_id, pt_def, byte_order, device_id)
                result[point_name] = value
                self._read_fail_tracker.pop((device_id, point_name), None)
            except ModbusException as e:
                self._log_throttled(device_id, point_name, e)
            except Exception as e:
                self._log_throttled(device_id, point_name, e)

        # 寄存器类型测点批量合并读取
        if reg_points:
            batch_result = await self._batch_read_points(client, slave_id, reg_points, byte_order, device_id)
            for point_name, value in batch_result.items():
                result[point_name] = value
                self._read_fail_tracker.pop((device_id, point_name), None)

        return result

    async def _batch_read_points(
        self, client: Any, slave_id: int,
        point_defs: dict[str, dict], byte_order: str, device_id: str,
    ) -> dict[str, Any]:
        """批量合并读取寄存器测点，自动分包（>125寄存器自动拆分）。

        将连续/相邻地址的测点合并为一次读取请求，每段不超过125个寄存器，
        超过则拆分为多个子段并发执行。
        """
        # 按地址排序
        sorted_points = sorted(point_defs.items(), key=lambda x: int(x[1].get("address", 0)))

        # 合并连续/相邻地址为读取段
        MAX_REGS = 125
        segments: list[tuple[int, int, list[tuple[str, dict]]]] = []
        seg_start: int | None = None
        seg_end: int = 0
        seg_items: list[tuple[str, dict]] = []

        for name, pt_def in sorted_points:
            addr = int(pt_def.get("address", 0))
            data_type = pt_def.get("data_type", "float32")
            n_regs = DATA_TYPE_REGS.get(data_type, 1)

            if seg_start is None:
                seg_start = addr
                seg_end = addr + n_regs
                seg_items = [(name, pt_def)]
            elif addr <= seg_end and (addr + n_regs - seg_start) <= MAX_REGS:
                seg_end = max(seg_end, addr + n_regs)
                seg_items.append((name, pt_def))
            else:
                segments.append((seg_start, seg_end - seg_start, seg_items))
                seg_start = addr
                seg_end = addr + n_regs
                seg_items = [(name, pt_def)]

        if seg_start is not None:
            segments.append((seg_start, seg_end - seg_start, seg_items))

        # 对超过125寄存器的段进行拆分
        sub_segments: list[tuple[int, int, list[tuple[str, dict]]]] = []
        for start, count, items in segments:
            if count <= MAX_REGS:
                sub_segments.append((start, count, items))
            else:
                sub_start = None
                sub_end = 0
                sub_items: list[tuple[str, dict]] = []
                for name, pt_def in items:
                    addr = int(pt_def.get("address", 0))
                    data_type = pt_def.get("data_type", "float32")
                    n_regs = DATA_TYPE_REGS.get(data_type, 1)
                    if sub_start is None:
                        sub_start = addr
                        sub_end = addr + n_regs
                        sub_items = [(name, pt_def)]
                    elif (addr + n_regs - sub_start) <= MAX_REGS:
                        sub_end = max(sub_end, addr + n_regs)
                        sub_items.append((name, pt_def))
                    else:
                        sub_segments.append((sub_start, sub_end - sub_start, sub_items))
                        sub_start = addr
                        sub_end = addr + n_regs
                        sub_items = [(name, pt_def)]
                if sub_start is not None:
                    sub_segments.append((sub_start, sub_end - sub_start, sub_items))

        # 并发执行所有子段读取
        result: dict[str, Any] = {}
        failed_points: dict[str, Exception] = {}
        crc_error_detected = False

        async def _read_segment(
            start_addr: int, count: int, items: list[tuple[str, dict]],
        ) -> None:
            nonlocal crc_error_detected
            _set_client_slave_id(client, slave_id)
            try:
                read_result = await client.read_holding_registers(
                    start_addr, **_read_kwargs(count, slave_id)
                )
                if read_result.isError():
                    err_str = str(read_result)
                    # 检测CRC错误
                    if "crc" in err_str.lower() or "checksum" in err_str.lower():
                        crc_error_detected = True
                    for name, _ in items:
                        failed_points[name] = ModbusException(f"批量读取错误: {read_result}")
                    return
                registers = read_result.registers
                for name, pt_def in items:
                    addr = int(pt_def.get("address", 0))
                    data_type = pt_def.get("data_type", "float32")
                    n_regs = DATA_TYPE_REGS.get(data_type, 1)
                    offset = addr - start_addr
                    if offset + n_regs > len(registers):
                        failed_points[name] = ModbusException("Insufficient registers in batch")
                        continue
                    pt_regs = registers[offset:offset + n_regs]
                    try:
                        value = self._decode_point_value(pt_regs, data_type, byte_order)
                        result[name] = value
                    except Exception as e:
                        failed_points[name] = e
            except Exception as e:
                err_str = str(e)
                if "crc" in err_str.lower() or "checksum" in err_str.lower():
                    crc_error_detected = True
                for name, _ in items:
                    failed_points[name] = e

        # 判断每个测点的 register_type，input 类型需要单独读取
        holding_segments: list[tuple[int, int, list[tuple[str, dict]]]] = []
        input_items: list[tuple[str, dict]] = []

        for start, count, items in sub_segments:
            all_input = all(pt_def.get("register_type", "holding") == "input" for _, pt_def in items)
            if all_input:
                input_items.extend(items)
            else:
                holding_segments.append((start, count, items))

        # 并发读取 holding 段
        if holding_segments:
            tasks = [_read_segment(s, c, i) for s, c, i in holding_segments]
            await asyncio.gather(*tasks, return_exceptions=True)

        # 逐个读取 input 类型测点
        for name, pt_def in input_items:
            try:
                value = await self._read_single_point(client, slave_id, pt_def, byte_order, device_id)
                result[name] = value
            except Exception as e:
                failed_points[name] = e

        # CRC错误触发重连
        if crc_error_detected:
            self._log_error(device_id, "CRC_ERROR", "CRC check failure detected, triggering reconnect")
            await self._try_reconnect(device_id)

        # 记录失败的测点
        for name, err in failed_points.items():
            self._log_throttled(device_id, name, err)

        return result

    def _decode_point_value(self, registers: list[int], data_type: str, byte_order: str) -> Any:
        """从寄存器列表解码单个测点值"""
        if data_type == "bool":
            return bool(registers[0]) if registers else False
        elif data_type == "int16":
            val = registers[0] if registers else 0
            return val if val < 32768 else val - 65536
        elif data_type == "uint16":
            return registers[0] if registers else 0
        elif data_type == "int32":
            return self._decode_registers(registers, byte_order, "i", 2)
        elif data_type == "uint32":
            return self._decode_registers(registers, byte_order, "I", 2)
        elif data_type == "float32":
            return self._decode_registers(registers, byte_order, "f", 2)
        elif data_type == "float64":
            return self._decode_registers(registers, byte_order, "d", 4)
        else:
            return registers[0] if registers else 0

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return False

        config = self._device_configs.get(device_id, {})
        slave_id = config.get("unit_id", 1)
        byte_order = config.get("byte_order", "ABCD")
        device_points = self._device_points.get(device_id, [])

        pt_def = next((p for p in device_points if p.get("name") == point), None)
        if pt_def is None:
            return False

        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")

        _set_client_slave_id(client, slave_id)

        try:
            if data_type == "bool":
                await client.write_coil(address, bool(value), **_slave_kwarg(slave_id))
            elif data_type == "int16":
                await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(slave_id))
            elif data_type == "uint16":
                await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(slave_id))
            elif data_type == "int32":
                regs = self._encode_value(int(value), byte_order, "i", 2)
                await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            elif data_type == "uint32":
                regs = self._encode_value(int(value), byte_order, "I", 2)
                await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            elif data_type == "float32":
                regs = self._encode_value(float(value), byte_order, "f", 2)
                await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            elif data_type == "float64":
                regs = self._encode_value(float(value), byte_order, "d", 4)
                await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            else:
                await client.write_register(address, int(value) & 0xFFFF, **_slave_kwarg(slave_id))
            return True
        except Exception as e:
            self._log_error(device_id, "WRITE_ERROR", f"{point}: {e}")
            return False

    def is_device_connected(self, device_id: str) -> bool:
        client = self._clients.get(device_id)
        return client is not None and client.connected

    async def _read_single_point(
        self, client: Any, slave_id: int, pt_def: dict,
        byte_order: str = "ABCD", device_id: str = "",
    ) -> Any:
        """读取单个测点"""
        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")
        reg_type = pt_def.get("register_type", "holding")
        reg_count = DATA_TYPE_REGS.get(data_type, 1)

        _set_client_slave_id(client, slave_id)

        if reg_type == "coil":
            result = await client.read_coils(address, **_read_kwargs(reg_count, slave_id))
            if result.isError():
                raise ModbusException(f"读取错误: {result}")
            return bool(result.bits[0])
        elif reg_type == "discrete":
            result = await client.read_discrete_inputs(address, **_read_kwargs(reg_count, slave_id))
            if result.isError():
                raise ModbusException(f"读取错误: {result}")
            return bool(result.bits[0])
        elif reg_type == "input":
            result = await client.read_input_registers(address, **_read_kwargs(reg_count, slave_id))
        else:
            result = await client.read_holding_registers(
                address, **_read_kwargs(reg_count, slave_id)
            )

        if result.isError():
            err_str = str(result)
            # 检测CRC错误，触发重连
            if device_id and ("crc" in err_str.lower() or "checksum" in err_str.lower()):
                self._log_error(device_id, "CRC_ERROR", f"CRC check failure: {err_str}")
                await self._try_reconnect(device_id)
            raise ModbusException(f"读取错误: {result}")

        registers = result.registers

        if data_type == "bool":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for bool")
            return bool(registers[0])
        elif data_type == "int16":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for int16")
            val = registers[0]
            return val if val < 32768 else val - 65536
        elif data_type == "uint16":
            if len(registers) < 1:
                raise ModbusException("Insufficient registers for uint16")
            return registers[0]
        elif data_type == "int32":
            return self._decode_registers(registers, byte_order, "i", 2)
        elif data_type == "uint32":
            return self._decode_registers(registers, byte_order, "I", 2)
        elif data_type == "float32":
            return self._decode_registers(registers, byte_order, "f", 2)
        elif data_type == "float64":
            return self._decode_registers(registers, byte_order, "d", 4)
        else:
            if len(registers) < 1:
                raise ModbusException("Insufficient registers")
            return registers[0]

    @staticmethod
    def _decode_registers(registers: list[int], byte_order: str, fmt_char: str, n_regs: int) -> Any:
        """根据字节序将寄存器列表解码为指定类型"""
        if len(registers) < n_regs:
            raise ModbusException(f"Insufficient registers: need {n_regs}, got {len(registers)}")
        reg_pack, val_unpack = _BYTE_ORDER_FMT.get(byte_order, (">", ">"))
        raw = struct.pack(f"{reg_pack}{'H' * n_regs}", *registers[:n_regs])
        return struct.unpack(f"{val_unpack}{fmt_char}", raw)[0]

    @staticmethod
    def _encode_value(value: Any, byte_order: str, fmt_char: str, n_regs: int) -> list[int]:
        """根据字节序将值编码为寄存器列表"""
        reg_pack, val_unpack = _BYTE_ORDER_FMT.get(byte_order, (">", ">"))
        raw = struct.pack(f"{val_unpack}{fmt_char}", value)
        return list(struct.unpack(f"{reg_pack}{'H' * n_regs}", raw))

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        """统一错误日志，包含 protocol_name/device_id/error_code/timestamp 四元组"""
        logger.error(
            "[%s] device=%s code=%s msg=%s",
            self.plugin_name, device_id, error_code, message,
        )

    def _log_throttled(self, device_id: str, point_name: str, error: Exception) -> None:
        key = (device_id, point_name)
        now = time.monotonic()
        first_time, last_log = self._read_fail_tracker.get(key, (now, 0.0))
        level = logging.WARNING if now - first_time < 5.0 else logging.DEBUG
        if now - last_log >= self._LOG_INTERVAL:
            logger.log(level, "Modbus RTU读取失败: %s.%s - %s", device_id, point_name, error)
            self._read_fail_tracker[key] = (first_time, now)
        else:
            self._read_fail_tracker[key] = (first_time, last_log)

    async def _try_reconnect(self, device_id: str) -> None:
        """尝试重连（指数退避）"""
        config = self._device_configs.get(device_id, {})
        max_attempts = config.get("max_reconnect_attempts", 0)

        async with self._retry_lock:
            count = self._retry_count.get(device_id, 0)
            # 检查是否超过最大重连次数（0=不限制）
            if max_attempts > 0 and count >= max_attempts:
                self._log_error(device_id, "CONN_FAILED", f"exceeded max reconnect attempts ({max_attempts})")
                return
            self._retry_count[device_id] = count + 1

        # >=5次失败后每5次尝试一次重连，实现限流重连机制
        if count < 5 or count % 5 != 0:
            return

        # 重置计数器，已进入重连流程
        async with self._retry_lock:
            self._retry_count[device_id] = 0

        if not config:
            return

        client = self._clients.get(device_id)
        if client:
            try:
                client.close()
            except Exception as e:
                logger.debug("Modbus RTU客户端关闭失败[%s]: %s", device_id, e)

        if not _PYMODBUS_AVAILABLE:
            return

        port = config.get("port", "/dev/ttyUSB0")
        baudrate = config.get("baudrate", 9600)
        parity = config.get("parity", "N")
        stopbits = config.get("stopbits", 1)
        bytesize = config.get("bytesize", 8)
        timeout = config.get("timeout", 3.0)

        new_client = AsyncModbusSerialClient(
            port=port,
            baudrate=baudrate,
            parity=parity,
            stopbits=stopbits,
            bytesize=bytesize,
            timeout=timeout,
        )
        try:
            connected = await new_client.connect()
            if connected:
                self._clients[device_id] = new_client
                self._retry_count[device_id] = 0
                self._log_error(device_id, "RECONNECT_OK", f"reconnected to {port}@{baudrate}")
        except Exception as e:
            self._log_error(device_id, "CONN_FAILED", str(e))
