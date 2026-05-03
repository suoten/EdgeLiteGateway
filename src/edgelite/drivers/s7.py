"""西门子S7协议驱动 - 基于snap7库，支持S7-200/300/400/1200/1500"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class S7Driver(DriverPlugin):
    """西门子S7协议驱动

    配置参数:
        ip: PLC IP地址
        port: TCP端口 (默认102，S7标准端口)
        rack: 机架号 (默认0)
        slot: 插槽号 (默认1，S7-1200/1500默认1，S7-300默认2)
        db_number: 数据块编号
    """

    plugin_name = "siemens_s7"
    plugin_version = "1.1.0"
    supported_protocols = ["s7"]

    def __init__(self):
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        """启动S7驱动连接"""
        try:
            import snap7
        except ImportError:
            raise ImportError(
                "snap7未安装，请执行: pip install python-snap7。"
                "同时需要下载snap7动态库: https://snap7.sourceforge.net/"
            )

        self._config = config
        ip = config.get("ip", "")
        port = int(config.get("port", 102))
        rack = int(config.get("rack", 0))
        slot = int(config.get("slot", 1))

        if not ip:
            raise ValueError("S7驱动配置缺少ip参数")

        try:
            self._client = snap7.client.Client()
            await asyncio.to_thread(self._client.connect, ip, rack, slot, port)
            self._running = True
            logger.info("S7驱动连接成功: %s:%d (rack=%d, slot=%d)", ip, port, rack, slot)
        except Exception as e:
            logger.error("S7驱动连接失败: %s:%d - %s", ip, port, e)
            raise

    async def stop(self) -> None:
        """停止S7驱动"""
        self._running = False
        if self._client:
            try:
                await asyncio.to_thread(self._client.disconnect)
            except Exception as e:
                logger.warning("S7驱动断开异常: %s", e)
            self._client = None
        logger.info("S7驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取S7 PLC测点值

        测点地址格式: "DB1.X0.0" (数据块.类型偏移.位偏移)
        支持的类型前缀:
            X - 位(BOOL)
            B - 字节(INT8)
            W - 字(INT16)
            D - 双字(INT32/FLOAT)
            R - 实数(FLOAT32)
        """
        if not self._running or not self._client:
            return {}

        result = {}
        async with self._lock:
            # 批量读取：一次to_thread调用读取所有点，避免N次线程切换
            try:
                values = await asyncio.to_thread(self._read_points_batch, points)
                result = values
            except Exception as e:
                logger.warning("S7批量读取失败，退回逐点读取: %s", e)
                for point_addr in points:
                    try:
                        value = await asyncio.to_thread(self._read_point, point_addr)
                        result[point_addr] = value
                    except Exception as e2:
                        logger.warning("S7读取失败 %s: %s", point_addr, e2)
                        result[point_addr] = None

        return result

    def _read_points_batch(self, addresses: list[str]) -> dict[str, Any]:
        """同步批量读取多个测点（单次to_thread调用，减少线程切换开销）"""
        result = {}
        for addr in addresses:
            try:
                result[addr] = self._read_point(addr)
            except Exception as e:
                logger.warning("S7读取失败 %s: %s", addr, e)
                result[addr] = None
        return result

    def _read_point(self, address: str) -> Any:
        """同步读取单个测点（在线程池中执行）"""
        parts = address.split(".")
        if len(parts) < 3 or not parts[0].startswith("DB"):
            raise ValueError(f"无效的S7地址格式: {address}，应为DBN.TB")

        db_number = int(parts[0][2:])
        type_char = parts[1][0].upper()
        byte_offset = int(parts[1][1:])
        bit_offset = int(parts[2]) if len(parts) > 2 else 0

        if type_char == "X":
            # 读取位(BOOL)
            data = self._client.db_read(db_number, byte_offset, 1)
            return bool(data[0] & (1 << bit_offset))
        elif type_char == "B":
            # 读取字节
            data = self._client.db_read(db_number, byte_offset, 1)
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "W":
            # 读取字(INT16)
            data = self._client.db_read(db_number, byte_offset, 2)
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "D":
            # 读取双字(INT32)
            data = self._client.db_read(db_number, byte_offset, 4)
            return int.from_bytes(data, byteorder="big", signed=True)
        elif type_char == "R":
            # 读取实数(FLOAT32)
            data = self._client.db_read(db_number, byte_offset, 4)
            import struct
            return struct.unpack(">f", data)[0]
        else:
            raise ValueError(f"不支持的S7数据类型: {type_char}")

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入S7 PLC测点值"""
        if not self._running or not self._client:
            return False

        try:
            async with self._lock:
                await asyncio.to_thread(self._write_point, point, value)
            return True
        except Exception as e:
            logger.error("S7写入失败 %s: %s", point, e)
            return False

    def _write_point(self, address: str, value: Any) -> None:
        """同步写入单个测点"""
        parts = address.split(".")
        db_number = int(parts[0][2:])
        type_char = parts[1][0].upper()
        byte_offset = int(parts[1][1:])
        bit_offset = int(parts[2]) if len(parts) > 2 else 0

        if type_char == "X":
            data = self._client.db_read(db_number, byte_offset, 1)
            if value:
                data[0] |= (1 << bit_offset)
            else:
                data[0] &= ~(1 << bit_offset)
            self._client.db_write(db_number, byte_offset, data)
        elif type_char == "W":
            data = value.to_bytes(2, byteorder="big", signed=True)
            self._client.db_write(db_number, byte_offset, data)
        elif type_char == "D":
            data = value.to_bytes(4, byteorder="big", signed=True)
            self._client.db_write(db_number, byte_offset, data)
        elif type_char == "R":
            import struct
            data = struct.pack(">f", float(value))
            self._client.db_write(db_number, byte_offset, data)

    async def discover_devices(self, config: dict) -> list[dict]:
        """S7协议不支持自动发现，返回空列表"""
        return []
