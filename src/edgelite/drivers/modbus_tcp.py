"""Modbus TCP驱动 - 基于pymodbus实现"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)

# 寄存器类型映射
REGISTER_TYPES = {
    "coil": (0, 1),       # 0x区, 读: read_coils, 写: write_coil
    "discrete": (1, 1),   # 1x区, 读: read_discrete_inputs
    "holding": (3, 2),    # 3x区, 读: read_holding_registers, 写: write_register
    "input": (4, 2),      # 4x区, 读: read_input_registers
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
            pt_def = next((p for p in device_points if p["name"] == point_name), None)
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

        pt_def = next((p for p in device_points if p["name"] == point), None)
        if pt_def is None:
            return False

        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")

        try:
            if data_type == "bool":
                await client.write_coil(address, bool(value), slave=slave_id)
            else:
                # 转换为寄存器值
                if data_type == "float32":
                    regs = list(struct.pack(">f", float(value)))
                else:
                    regs = [int(value)]
                if len(regs) == 1:
                    await client.write_register(address, regs[0], slave=slave_id)
                else:
                    await client.write_registers(address, regs, slave=slave_id)
            return True
        except Exception as e:
            logger.error("Modbus写入失败: %s.%s - %s", device_id, point, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """扫描指定IP段内的Modbus设备"""
        host = config.get("host", "127.0.0.1")
        port = config.get("port", 502)
        slave_ids = config.get("slave_ids", list(range(1, 248)))

        discovered = []
        for slave_id in slave_ids:
            try:
                client = AsyncModbusTcpClient(host=host, port=port, timeout=2.0)
                connected = await client.connect()
                if connected:
                    # 尝试读取1个保持寄存器
                    result = await client.read_holding_registers(0, 1, slave=slave_id)
                    if not result.isError():
                        discovered.append({
                            "host": host,
                            "port": port,
                            "slave_id": slave_id,
                        })
                client.close()
            except Exception:
                pass

        return discovered

    async def _read_single_point(
        self, client: AsyncModbusTcpClient, slave_id: int, pt_def: dict
    ) -> Any:
        """读取单个测点"""
        address = int(pt_def.get("address", 0))
        data_type = pt_def.get("data_type", "float32")
        reg_count = DATA_TYPE_REGS.get(data_type, 1)

        # 读取保持寄存器（默认3x区）
        result = await client.read_holding_registers(address, reg_count, slave=slave_id)

        if result.isError():
            raise ModbusException(f"读取错误: {result}")

        registers = result.registers

        # 数据类型转换
        if data_type == "bool":
            return bool(registers[0])
        elif data_type == "int16":
            val = registers[0]
            return val if val < 32768 else val - 65536
        elif data_type == "uint16":
            return registers[0]
        elif data_type == "float32":
            # 两个寄存器组合为IEEE 754浮点
            raw = struct.pack(">HH", registers[0], registers[1])
            return struct.unpack(">f", raw)[0]
        else:
            return registers[0]

    async def _try_reconnect(self, device_id: str) -> None:
        """尝试重连（指数退避）"""
        async with self._retry_lock:
            count = self._retry_count.get(device_id, 0)
            # 指数退避，最大间隔60秒
            delay = min(2 ** count, 60)
            self._retry_count[device_id] = count + 1

        if count > 0 and count % 5 != 0:
            # 不是每次都重试，按退避间隔
            return

        config = self._device_configs.get(device_id)
        if not config:
            return

        client = self._clients.get(device_id)
        if client:
            try:
                client.close()
            except Exception:
                pass

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
