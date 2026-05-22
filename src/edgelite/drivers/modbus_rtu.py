"""Modbus RTU驱动 - 基于pymodbus串口实现"""

from __future__ import annotations

import asyncio
import logging
import struct
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
    "float32": 2,
    "string": 1,
}


def _slave_kwarg(slave_id: int) -> dict:
    if _PYMODBUS_MAJOR < 3:
        return {"unit": slave_id}
    return {"slave": slave_id}


def _read_kwargs(count: int, slave_id: int) -> dict:
    kwargs = _slave_kwarg(slave_id)
    if _PYMODBUS_37_PLUS:
        kwargs["count"] = count
    return kwargs


class ModbusRtuDriver(DriverPlugin):

    plugin_name = "Modbus RTU"
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
        ],
    }

    def __init__(self):
        self._running = False
        self._clients: dict[str, Any] = {}
        self._device_configs: dict[str, dict] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._retry_count: dict[str, int] = {}
        self._retry_lock = asyncio.Lock()

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
            logger.warning("pymodbus未安装，无法建立串口连接: %s", device_id)
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
                logger.warning("Modbus RTU连接失败: %s (%s@%d)", device_id, port, baudrate)
        except Exception as e:
            logger.warning("Modbus RTU连接异常: %s - %s", device_id, e)

    async def remove_device(self, device_id: str) -> None:
        client = self._clients.pop(device_id, None)
        if client and client.connected:
            client.close()
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._retry_count.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            await self._try_reconnect(device_id)
            return {}

        config = self._device_configs.get(device_id, {})
        slave_id = config.get("unit_id", 1)
        device_points = self._device_points.get(device_id, [])

        result = {}
        for point_name in points:
            pt_def = next((p for p in device_points if p.get("name") == point_name), None)
            if pt_def is None:
                continue

            try:
                value = await self._read_single_point(client, slave_id, pt_def)
                result[point_name] = value
            except ModbusException as e:
                logger.error("Modbus RTU读取失败: %s.%s - %s", device_id, point_name, e)
            except Exception as e:
                logger.error("Modbus RTU读取异常: %s.%s - %s", device_id, point_name, e)

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return False

        config = self._device_configs.get(device_id, {})
        slave_id = config.get("unit_id", 1)
        device_points = self._device_points.get(device_id, [])

        pt_def = next((p for p in device_points if p.get("name") == point), None)
        if pt_def is None:
            return False

        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")

        try:
            if data_type == "bool":
                await client.write_coil(address, bool(value), **_slave_kwarg(slave_id))
            else:
                if data_type == "float32":
                    raw = struct.pack(">f", float(value))
                    regs = [struct.unpack(">H", raw[i : i + 2])[0] for i in range(0, 4, 2)]
                else:
                    regs = [int(value)]
                if len(regs) == 1:
                    await client.write_register(address, regs[0], **_slave_kwarg(slave_id))
                else:
                    await client.write_registers(address, regs, **_slave_kwarg(slave_id))
            return True
        except Exception as e:
            logger.error("Modbus RTU写入失败: %s.%s - %s", device_id, point, e)
            return False

    def is_device_connected(self, device_id: str) -> bool:
        client = self._clients.get(device_id)
        return client is not None and client.connected

    async def _read_single_point(self, client: Any, slave_id: int, pt_def: dict) -> Any:
        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")
        reg_type = pt_def.get("register_type", "holding")
        reg_count = DATA_TYPE_REGS.get(data_type, 1)

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
        elif data_type == "float32":
            if len(registers) < 2:
                raise ModbusException("Insufficient registers for float32")
            raw = struct.pack(">HH", registers[0], registers[1])
            return struct.unpack(">f", raw)[0]
        else:
            if len(registers) < 1:
                raise ModbusException("Insufficient registers")
            return registers[0]

    async def _try_reconnect(self, device_id: str) -> None:
        async with self._retry_lock:
            count = self._retry_count.get(device_id, 0)
            self._retry_count[device_id] = count + 1

        if count < 5 or count % 5 != 0:
            return

        config = self._device_configs.get(device_id)
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
                logger.info("Modbus RTU重连成功: %s", device_id)
        except Exception as e:
            logger.debug("Modbus RTU重连失败: %s - %s", device_id, e)
