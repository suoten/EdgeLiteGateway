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
        rack: 机架号 (默认0)
        slot: 插槽号 (默认1，S7-1200/1500默认1，S7-300默认2)
        db_number: 数据块编号

    常见PLC型号rack/slot配置参考:
        S7-200:   rack=0, slot=1  (通过CP243扩展)
        S7-300:   rack=0, slot=2  (CPU在slot 2)
        S7-400:   rack=0, slot=2  (CPU在slot 2)
        S7-1200:  rack=0, slot=1  (CPU在slot 1)
        S7-1500:  rack=0, slot=1  (CPU在slot 1)
    """

    plugin_name = "siemens_s7"
    plugin_version = "1.0.0"
    supported_protocols = ["s7"]
    config_schema = {
        "description": "Siemens S7 PLC protocol (S7-200/300/400/1200/1500)",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {"name": "host", "type": "string", "label": "IP Address", "description": "PLC IP address", "default": "192.168.1.1", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "rack", "type": "integer", "label": "Rack", "description": "Hardware rack number (0-7), usually 0", "default": 0},  # FIXED: 原问题-中文硬编码label/description
            {"name": "slot", "type": "integer", "label": "Slot", "description": "CPU slot number (0-31), S7-300 usually 2, S7-1200/1500 usually 0 or 1", "default": 1},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY

    async def start(self, config: dict) -> None:
        """启动S7驱动连接"""
        try:
            import snap7
        except ImportError:
            raise ImportError(
                "snap7未安装，请执行: pip install python-snap7。"
                "同时需要下载snap7动态库: https://snap7.sourceforge.net/"
            ) from None

        self._config = config
        ip = config.get("ip", "")
        try:
            rack = int(config.get("rack", 0))
            slot = int(config.get("slot", 1))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid S7 rack/slot config value: {e}") from e

        if not ip:
            raise ValueError("S7 driver config missing 'ip' parameter")

        if not (0 <= rack <= 7):
            raise ValueError(
                f"S7 rack out of range [0-7], got {rack}. "
                f"Common: S7-300/400 rack=0, S7-1200/1500 rack=0"
            )
        if not (0 <= slot <= 31):
            raise ValueError(
                f"S7 slot out of range [0-31], got {slot}. "
                f"Common: S7-300 slot=2, S7-1200/1500 slot=1"
            )

        try:
            self._client = snap7.client.Client()
            await asyncio.to_thread(self._client.connect, ip, rack, slot)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("S7驱动连接成功: %s (rack=%d, slot=%d)", ip, rack, slot)
        except Exception as e:
            logger.error(
                "S7驱动连接失败: %s (rack=%d, slot=%d) - %s。请检查IP地址及rack/slot配置",
                ip, rack, slot, e,
            )
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
            await self._try_reconnect(device_id)
            return {}

        result = {}
        async with self._lock:
            try:
                values = await asyncio.to_thread(self._read_points_batch, points)
                result = values
            except Exception as e:
                logger.warning("S7批量读取失败，退回逐点读取: %s", e)
                if not self._is_connected():
                    await self._try_reconnect(device_id)
                    return {}
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
        if len(parts) < 2 or not parts[0].startswith("DB"):
            raise ValueError(f"Invalid S7 address format: {address}, expected DBN.TB")

        try:  # FIXED: 原问题-parts[0][2:]/parts[1]硬索引，格式错误时IndexError/ValueError
            db_number = int(parts[0][2:])
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid S7 DB number in address: {address}") from e
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid S7 type/offset in address: {address}")
        type_char = parts[1][0].upper()
        try:
            byte_offset = int(parts[1][1:])
        except ValueError as e:
            raise ValueError(f"Invalid S7 byte offset in address: {address}") from e
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
            await self._try_reconnect(device_id)
            return False

        try:
            async with self._lock:
                await asyncio.to_thread(self._write_point, point, value)
            return True
        except Exception as e:
            logger.error("S7写入失败 %s: %s", point, e)
            if not self._is_connected():
                await self._try_reconnect(device_id)
            return False

    def _write_point(self, address: str, value: Any) -> None:
        """同步写入单个测点"""
        parts = address.split(".")
        try:  # FIXED: 原问题-_write_point同样存在硬索引问题
            db_number = int(parts[0][2:])
        except (ValueError, IndexError) as e:
            raise ValueError(f"Invalid S7 DB number in address: {address}") from e
        if len(parts) < 2 or not parts[1]:
            raise ValueError(f"Invalid S7 type/offset in address: {address}")
        type_char = parts[1][0].upper()
        try:
            byte_offset = int(parts[1][1:])
        except ValueError as e:
            raise ValueError(f"Invalid S7 byte offset in address: {address}") from e
        bit_offset = int(parts[2]) if len(parts) > 2 else 0

        if type_char == "X":
            data = self._client.db_read(db_number, byte_offset, 1)
            if value:
                data[0] |= 1 << bit_offset
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

    def _is_connected(self) -> bool:
        """检查S7客户端连接状态"""
        if not self._client:
            return False
        try:
            return self._client.get_connected()
        except Exception:
            return False

    async def _try_reconnect(self, device_id: str) -> None:
        """指数退避重连：初始1秒，最大60秒，每次翻倍，最多100次"""
        if not self._config:
            return

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error(
                "S7重连放弃: %s (已重试%d次)，设备标记offline",
                device_id, self._reconnect_count,
            )
            self._running = False
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning(
            "S7连接断开，%0.1fs后重连 (第%d次): %s",
            delay, self._reconnect_count, device_id,
        )
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2

        ip = self._config.get("ip", "")
        rack = int(self._config.get("rack", 0))
        slot = int(self._config.get("slot", 1))

        try:
            import snap7
        except ImportError:
            return

        if self._client:
            try:
                await asyncio.to_thread(self._client.disconnect)
            except Exception:
                pass

        try:
            self._client = snap7.client.Client()
            await asyncio.to_thread(self._client.connect, ip, rack, slot)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info(
                "S7重连成功: %s (rack=%d, slot=%d)", ip, rack, slot,
            )
        except Exception as e:
            logger.error(
                "S7重连失败: %s (rack=%d, slot=%d) - %s",
                ip, rack, slot, e,
            )
