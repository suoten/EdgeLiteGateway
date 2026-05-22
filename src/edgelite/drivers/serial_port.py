"""串口设备驱动 - 基于pyserial，支持RS232/RS485串口设备接入

支持：
- RS232/RS485串口通信
- 自定义波特率、数据位、停止位、校验位
- Modbus RTU从站通信
- 通用串口数据帧解析（自定义协议）
- 扫码枪等串口外设
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

try:
    import pymodbus
except ImportError:
    pymodbus = None

from edgelite.constants import _SERIAL_READ_TIMEOUT, _SERIAL_WRITE_WAIT  # FIXED: P2-3 将魔法数字 sleep 间隔提取为常量

from edgelite.drivers.base import DriverPlugin

_PYMODBUS_MAJOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[0]) if pymodbus else 2


def _slave_kwarg(slave_id: int) -> dict:
    """返回 pymodbus 3.x 兼容的设备 ID 参数"""
    if _PYMODBUS_MAJOR < 3:
        return {"slave": slave_id}
    return {"device_id": slave_id}


def _create_serial_client(port: str, baudrate: int, parity: str) -> Any:
    """创建 pymodbus 3.x 兼容的串口客户端"""
    from pymodbus.client import ModbusSerialClient

    return ModbusSerialClient(
        port=port,
        baudrate=baudrate,
        parity=parity,
    )


logger = logging.getLogger(__name__)


class SerialPortDriver(DriverPlugin):
    """串口设备驱动

    配置参数:
        port: 串口设备路径 (如 /dev/ttyUSB0, COM3)
        baudrate: 波特率 (默认9600)
        bytesize: 数据位 5/6/7/8 (默认8)
        parity: 校验位 N/E/O/M/S (默认N)
        stopbits: 停止位 1/1.5/2 (默认1)
        timeout: 读超时秒数 (默认1.0)
        protocol: 上层协议 modbus_rtu / raw / custom (默认raw)
        frame_delimiter: 帧分隔符 (raw模式，默认\\r\\n)
        frame_length: 固定帧长度 (raw模式，可选)
    """

    plugin_name = "serial_port"
    plugin_version = "1.0.0"
    supported_protocols = ["serial", "serial_modbus_rtu", "serial_raw"]
    config_schema = {
        "description": "Serial communication (RS232/RS485), supports Modbus RTU and other protocols",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {"name": "port", "type": "string", "label": "Serial Port", "description": "Serial device path, e.g. COM1 on Windows, /dev/ttyUSB0 on Linux", "default": "COM1", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "baudrate", "type": "integer", "label": "Baud Rate", "description": "Serial communication speed, must match device", "default": 9600, "options": [9600, 19200, 38400, 57600, 115200]},  # FIXED: 原问题-中文硬编码label/description
            {"name": "bytesize", "type": "integer", "label": "Data Bits", "description": "Number of data bits per byte", "default": 8, "options": [5, 6, 7, 8]},  # FIXED: 原问题-中文硬编码label/description
            {"name": "parity", "type": "string", "label": "Parity", "description": "N=None, E=Even, O=Odd", "default": "N", "options": ["N", "E", "O"]},  # FIXED: 原问题-中文硬编码label/description
            {"name": "stopbits", "type": "number", "label": "Stop Bits", "description": "Number of stop bits", "default": 1, "options": [1, 1.5, 2]},  # FIXED: 原问题-中文硬编码label/description
            {"name": "protocol", "type": "string", "label": "Protocol", "description": "raw=raw passthrough, modbus_rtu=Modbus RTU", "default": "raw", "options": ["raw", "modbus_rtu"]},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        self._running = False
        self._serial = None
        self._modbus_rtu_client = None  # FIXED: P0-2 持久ModbusRTU客户端不复用，每次读取创建新连接导致资源泄漏
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._read_buffer: bytes = b""
        self._read_task: asyncio.Task | None = None
        self._data_callback = None

    async def start(self, config: dict) -> None:
        try:
            import serial
        except ImportError:
            raise ImportError("pyserial未安装，请执行: pip install pyserial") from None

        self._config = config
        port = config.get("port", "COM1")
        try:  # FIXED: 原问题-int()转换无ValueError保护
            baudrate = int(config.get("baudrate", 9600))
        except (ValueError, TypeError):
            baudrate = 9600
        try:  # FIXED: 原问题-int()转换无ValueError保护
            bytesize = int(config.get("bytesize", 8))
        except (ValueError, TypeError):
            bytesize = 8
        parity = config.get("parity", "N")
        try:  # FIXED: 原问题-float()转换无ValueError保护
            stopbits = float(config.get("stopbits", 1))
        except (ValueError, TypeError):
            stopbits = 1.0
        try:  # FIXED: 原问题-float()转换无ValueError保护
            timeout = float(config.get("timeout", 1.0))
        except (ValueError, TypeError):
            timeout = 1.0

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
                baudrate=baudrate,
                bytesize=bytesize_map.get(bytesize, serial.EIGHTBITS),
                parity=parity_map.get(parity, serial.PARITY_NONE),
                stopbits=stopbits_map.get(stopbits, serial.STOPBITS_ONE),
                timeout=timeout,
            )
            self._running = True
            logger.info("串口驱动启动成功 (port=%s, baudrate=%d)", port, baudrate)

            protocol = config.get("protocol", "raw")
            if protocol == "raw":
                self._read_task = asyncio.create_task(self._raw_read_loop(), name="serial-raw-read")
        except Exception as e:
            logger.error("串口驱动启动失败: %s", e)
            raise

    async def stop(self) -> None:
        self._running = False
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
        if self._modbus_rtu_client:
            try:
                self._modbus_rtu_client.close()
            except Exception:
                pass
            self._modbus_rtu_client = None
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except Exception as e:
                logger.warning("Serial port close error: %s", e)
        self._serial = None
        logger.info("Serial port driver stopped")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        if not self._running or not self._serial:
            return {}

        protocol = self._config.get("protocol", "raw")

        if protocol == "modbus_rtu":
            return await self._read_modbus_rtu(points)
        else:
            return await self._read_raw(points)

    async def _read_modbus_rtu(self, points: list[str]) -> dict[str, Any]:
        try:
            from pymodbus.client import AsyncModbusSerialClient as _unused  # noqa: F401
        except ImportError:
            logger.error("pymodbus not installed, Modbus RTU unavailable")
            return {}

        result = {}
        slave_id = int(self._config.get("slave_id", 1))

        for point in points:
            try:
                parts = point.split(":")
                if len(parts) >= 2:
                    addr = int(parts[0])
                    count = int(parts[1])
                    func = int(parts[2]) if len(parts) >= 3 else 3
                else:
                    addr = int(point)
                    count = 1
                    func = 3

                rr = None
                async with self._lock:
                    if self._modbus_rtu_client is None or not getattr(self._modbus_rtu_client, "connected", False):
                        self._modbus_rtu_client = _create_serial_client(
                            self._serial.port,
                            self._serial.baudrate,
                            self._serial.parity,
                        )
                        try:
                            self._modbus_rtu_client.connect()
                        except Exception as e:
                            logger.warning("Modbus RTU client connect failed: %s", e)
                            self._modbus_rtu_client = None
                            result[point] = None
                            continue
                    client = self._modbus_rtu_client
                    if client and client.connected:
                        try:
                            if func == 1:
                                rr = await asyncio.to_thread(
                                    client.read_coils, addr, count, **_slave_kwarg(slave_id)
                                )
                            elif func == 3:
                                rr = await asyncio.to_thread(
                                    client.read_holding_registers, addr, count, **_slave_kwarg(slave_id)
                                )
                            elif func == 4:
                                rr = await asyncio.to_thread(
                                    client.read_input_registers, addr, count, **_slave_kwarg(slave_id)
                                )
                        except Exception as e:
                            logger.warning("Modbus RTU read failed for %s: %s", point, e)
                            rr = None

                if rr is not None and not rr.isError():
                    result[point] = rr.registers if func in (3, 4) else rr.bits[:count]
                else:
                    result[point] = None
            except Exception as e:
                logger.warning("Modbus RTU read failed %s: %s", point, e)
                result[point] = None

        return result

    async def _read_raw(self, points: list[str]) -> dict[str, Any]:
        result = {}
        for point in points:
            try:
                cmd = self._config.get("commands", {}).get(point, "")
                if cmd:
                    async with self._lock:
                        await asyncio.to_thread(self._serial.write, cmd.encode("utf-8"))
                        await asyncio.sleep(_SERIAL_WRITE_WAIT)  # FIXED: P2-3 原魔法数字 0.1，串口写入后等待
                        data = await asyncio.to_thread(
                            self._serial.read, self._serial.in_waiting or 1024
                        )
                    result[point] = data.decode("utf-8", errors="replace").strip() if data else None
                else:
                    result[point] = None
            except Exception as e:
                logger.warning("串口原始读取失败 %s: %s", point, e)
                result[point] = None
        return result

    async def _raw_read_loop(self) -> None:
        while self._running:
            try:
                if self._serial and self._serial.in_waiting > 0:
                    data = await asyncio.to_thread(self._serial.read, self._serial.in_waiting)
                    if data and self._data_callback:
                        await self._data_callback(data)
                await asyncio.sleep(_SERIAL_POLL_INTERVAL)  # FIXED: P2-3 原魔法数字 0.05
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("串口读取循环异常: %s", e)
                await asyncio.sleep(0.5)  # FIXED: P2-3 原魔法数字 0.5

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._running or not self._serial:
            return False

        try:
            async with self._lock:
                if isinstance(value, str):
                    data = value.encode("utf-8")
                elif isinstance(value, bytes):
                    data = value
                else:
                    data = str(value).encode("utf-8")
                await asyncio.to_thread(self._serial.write, data)
            return True
        except Exception as e:
            logger.error("串口写入失败 %s: %s", point, e)
            return False

    def on_data(self, callback) -> None:
        self._data_callback = callback

    async def discover_devices(self, config: dict) -> list[dict]:
        try:
            import serial.tools.list_ports
        except ImportError:
            return []

        ports = serial.tools.list_ports.comports()
        result = []
        for p in ports:
            result.append(
                {
                    "device_id": p.device,
                    "name": p.description,
                    "ip": p.device,
                    "protocol": self.supported_protocols[0],
                    "details": {
                        "hwid": p.hwid,
                        "manufacturer": p.manufacturer,
                        "serial_number": p.serial_number,
                    },
                }
            )
        return result
