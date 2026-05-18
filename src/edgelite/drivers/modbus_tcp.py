"""Modbus TCP驱动 - 基于pymodbus实现"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

import pymodbus
from pymodbus.client import AsyncModbusTcpClient

try:
    from pymodbus.exceptions import ModbusException
except ImportError:
    ModbusException = Exception

from edgelite.constants import _DEVICE_CONNECT_TIMEOUT
from edgelite.drivers.base import DriverPlugin

_PYMODBUS_MAJOR = int(getattr(pymodbus, "__version__", "2.0.0").split(".")[0])

logger = logging.getLogger(__name__)


def _slave_kwarg(slave_id: int) -> dict:
    """返回正确的 Modbus 设备 ID 参数"""
    if _PYMODBUS_MAJOR < 3:
        return {"unit": slave_id}  # pymodbus 2.x
    return {"device_id": slave_id}  # pymodbus 3.x


# 寄存器类型映射
REGISTER_TYPES = {
    "coil": (0, 1),  # 0x区, 读: read_coils, 写: write_coil
    "discrete": (1, 1),  # 1x区, 读: read_discrete_inputs
    "holding": (3, 2),  # 3x区, 读: read_holding_registers, 写: write_register
    "input": (4, 2),  # 4x区, 读: read_input_registers
}

# 数据类型→寄存器数量映射
DATA_TYPE_REGS = {
    "bool": 1,
    "int16": 1,
    "uint16": 1,
    "float32": 2,
    "string": 1,  # 每个寄存器2字节
}


class ModbusTcpDriver(DriverPlugin):
    """Modbus TCP协议驱动"""

    plugin_name = "modbus_tcp"
    plugin_version = "0.1.0"
    supported_protocols = ["modbus_tcp"]
    config_schema = {
        "description": "Modbus TCP industrial standard protocol for reading/writing PLC/instrument coils and registers",
        "fields": [
            {"name": "host", "type": "string", "label": "IP Address", "description": "PLC or gateway IP address, e.g. 192.168.1.100", "default": "192.168.1.100", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "port", "type": "integer", "label": "Port", "description": "Modbus TCP port, default 502", "default": 502, "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "slave_id", "type": "integer", "label": "Slave ID", "description": "Device slave address (Unit ID), usually 1", "default": 1, "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "timeout", "type": "number", "label": "Timeout (s)", "description": "Connection and read timeout", "default": 3.0},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        self._running = False
        self._clients: dict[str, AsyncModbusTcpClient] = {}
        # device_id -> config
        self._device_configs: dict[str, dict] = {}
        # device_id -> points定义
        self._device_points: dict[str, list[dict]] = {}
        # 重试状态
        self._retry_count: dict[str, int] = {}
        self._retry_lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        """启动驱动（config为全局配置，实际连接在add_device时建立）"""
        self._running = True
        logger.info("Modbus TCP驱动启动")

    async def stop(self) -> None:
        """停止驱动，关闭所有连接"""
        self._running = False
        for device_id, client in self._clients.items():
            if client.connected:
                client.close()
                logger.info("Modbus连接关闭: %s", device_id)
        self._clients.clear()
        logger.info("Modbus TCP驱动停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加设备并建立连接"""
        self._device_configs[device_id] = config
        self._device_points[device_id] = points

        host = config.get("host", "127.0.0.1")
        port = config.get("port", 502)
        timeout = config.get("timeout", 5.0)

        client = AsyncModbusTcpClient(
            host=host,
            port=port,
            timeout=timeout,
        )
        self._clients[device_id] = client

        # 尝试连接
        try:
            connected = await client.connect()
            if connected:
                logger.info("Modbus TCP连接成功: %s (%s:%d)", device_id, host, port)
                self._retry_count[device_id] = 0
            else:
                logger.warning("Modbus TCP连接失败: %s (%s:%d)", device_id, host, port)
        except Exception as e:
            logger.warning("Modbus TCP连接异常: %s - %s", device_id, e)

    async def remove_device(self, device_id: str) -> None:
        """移除设备"""
        client = self._clients.pop(device_id, None)
        if client and client.connected:
            client.close()
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._retry_count.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值"""
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            # 尝试重连
            await self._try_reconnect(device_id)
            return {}

        config = self._device_configs.get(device_id, {})
        slave_id = config.get("slave_id", 1)
        device_points = self._device_points.get(device_id, [])

        result = {}
        for point_name in points:
            # 查找测点定义
            pt_def = next((p for p in device_points if p.get("name") == point_name), None)  # FIXED: 原问题-p["name"]硬访问
            if pt_def is None:
                continue

            try:
                value = await self._read_single_point(client, slave_id, pt_def)
                result[point_name] = value
            except ModbusException as e:
                logger.error("Modbus读取失败: %s.%s - %s", device_id, point_name, e)
            except Exception as e:
                logger.error("Modbus读取异常: %s.%s - %s", device_id, point_name, e)

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""
        client = self._clients.get(device_id)
        if client is None or not client.connected:
            return False

        config = self._device_configs.get(device_id, {})
        slave_id = config.get("slave_id", 1)
        device_points = self._device_points.get(device_id, [])

        pt_def = next((p for p in device_points if p.get("name") == point), None)  # FIXED: 原问题-p["name"]硬访问
        if pt_def is None:
            return False

        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")

        try:
            if data_type == "bool":
                await client.write_coil(address, bool(value), **_slave_kwarg(slave_id))
            else:
                # 转换为寄存器值
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
            logger.error("Modbus写入失败: %s.%s - %s", device_id, point, e)
            return False

    def is_device_connected(self, device_id: str) -> bool:
        """检查设备是否已连接"""
        client = self._clients.get(device_id)
        return client is not None and client.connected

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描指定IP或IP段内的Modbus设备"""
        host = config.get("host", "127.0.0.1")
        port = config.get("port", 502)
        slave_ids = config.get("slave_ids", list(range(1, 248)))

        # FIXED: 支持网段扫描，如192.168.1.0/24或192.168.1.*
        hosts = self._expand_hosts(host)

        discovered = []
        for h in hosts:
            for slave_id in slave_ids:
                client = AsyncModbusTcpClient(host=h, port=port, timeout=_DEVICE_CONNECT_TIMEOUT)  # FIXED: 原问题-timeout=2.0魔法数字
                try:
                    connected = await client.connect()
                    if connected:
                        result = await client.read_holding_registers(0, 1, **_slave_kwarg(slave_id))
                        if not result.isError():
                            discovered.append(
                                {
                                    "host": h,
                                    "port": port,
                                    "slave_id": slave_id,
                                    "protocol": "modbus_tcp",
                                    "name": f"modbus-{h.split('.')[-1]}-{slave_id}",
                                }
                            )
                except Exception as e:
                    logger.debug("Modbus TCP发现设备异常[%s:%s]: %s", h, port, e)
                finally:
                    client.close()

        return discovered

    @staticmethod
    def _expand_hosts(host: str) -> list[str]:
        """将IP或网段展开为IP列表，支持x.x.x.x/x和x.x.x.*格式"""
        if "/" in host:
            try:
                import ipaddress

                network = ipaddress.ip_network(host, strict=False)
                return [str(ip) for ip in network.hosts()]
            except ValueError:
                return [host.split("/")[0]]
        if "*" in host:
            prefix = host.rsplit(".", 1)[0]
            return [f"{prefix}.{i}" for i in range(1, 255)]
        return [host]

    async def _read_single_point(
        self, client: AsyncModbusTcpClient, slave_id: int, pt_def: dict
    ) -> Any:
        """读取单个测点"""
        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")
        reg_type = pt_def.get("register_type", "holding")
        reg_count = DATA_TYPE_REGS.get(data_type, 1)

        if reg_type == "coil":
            result = await client.read_coils(address, reg_count, **_slave_kwarg(slave_id))
            if result.isError():
                raise ModbusException(f"读取错误: {result}")
            return bool(result.bits[0])
        elif reg_type == "discrete":
            result = await client.read_discrete_inputs(address, reg_count, **_slave_kwarg(slave_id))
            if result.isError():
                raise ModbusException(f"读取错误: {result}")
            return bool(result.bits[0])
        elif reg_type == "input":
            result = await client.read_input_registers(address, reg_count, **_slave_kwarg(slave_id))
        else:
            result = await client.read_holding_registers(
                address, reg_count, **_slave_kwarg(slave_id)
            )

        if result.isError():
            raise ModbusException(f"读取错误: {result}")

        registers = result.registers

        # 数据类型转换
        if data_type == "bool":
            if len(registers) < 1:  # FIXED: 原问题-registers[0]直接索引无长度检查
                raise ModbusException("Insufficient registers for bool")
            return bool(registers[0])
        elif data_type == "int16":
            if len(registers) < 1:  # FIXED: 原问题-registers[0]直接索引无长度检查
                raise ModbusException("Insufficient registers for int16")
            val = registers[0]
            return val if val < 32768 else val - 65536
        elif data_type == "uint16":
            if len(registers) < 1:  # FIXED: 原问题-registers[0]直接索引无长度检查
                raise ModbusException("Insufficient registers for uint16")
            return registers[0]
        elif data_type == "float32":
            # 两个寄存器组合为IEEE 754浮点
            if len(registers) < 2:  # FIXED: 原问题-registers[0],registers[1]直接索引无长度检查
                raise ModbusException("Insufficient registers for float32")
            raw = struct.pack(">HH", registers[0], registers[1])
            return struct.unpack(">f", raw)[0]
        else:
            if len(registers) < 1:  # FIXED: 原问题-registers[0]直接索引无长度检查
                raise ModbusException("Insufficient registers")
            return registers[0]

    async def _try_reconnect(self, device_id: str) -> None:
        """尝试重连（指数退避）"""
        async with self._retry_lock:
            count = self._retry_count.get(device_id, 0)
            self._retry_count[device_id] = count + 1

        if count > 0 and count % 5 != 0:
            return

        config = self._device_configs.get(device_id)
        if not config:
            return

        client = self._clients.get(device_id)
        if client:
            try:
                client.close()
            except Exception as e:
                logger.debug("Modbus TCP客户端关闭失败[%s]: %s", device_id, e)

        host = config.get("host", "127.0.0.1")
        port = config.get("port", 502)
        timeout = config.get("timeout", 5.0)

        new_client = AsyncModbusTcpClient(host=host, port=port, timeout=timeout)
        try:
            connected = await new_client.connect()
            if connected:
                self._clients[device_id] = new_client
                self._retry_count[device_id] = 0
                logger.info("Modbus TCP重连成功: %s", device_id)
        except Exception as e:
            logger.debug("Modbus TCP重连失败: %s - %s", device_id, e)
