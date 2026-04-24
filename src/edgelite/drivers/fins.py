"""欧姆龙FINS协议驱动 - 基于pylogix/pyfins库，支持CJ/CP/NJ系列PLC"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class OmronFinsDriver(DriverPlugin):
    """欧姆龙FINS协议驱动

    配置参数:
        ip: PLC IP地址
        port: FINS UDP/TCP端口 (默认9600)
        source_node: 源节点号 (默认0)
        dest_node: 目标节点号 (自动获取)
    """

    plugin_name = "omron_fins"
    plugin_version = "1.0.0"
    supported_protocols = ["fins"]

    def __init__(self):
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        """启动FINS驱动连接"""
        try:
            from pyfins import FinsClient
        except ImportError:
            raise ImportError(
                "pyfins未安装，请执行: pip install pyfins"
            )

        self._config = config
        ip = config.get("ip", "")
        port = int(config.get("port", 9600))

        if not ip:
            raise ValueError("FINS驱动配置缺少ip参数")

        try:
            self._client = FinsClient(ip, port=port)
            await asyncio.to_thread(self._client.connect)
            self._running = True
            logger.info("FINS驱动连接成功: %s:%d", ip, port)
        except Exception as e:
            logger.error("FINS驱动连接失败: %s - %s", ip, e)
            raise

    async def stop(self) -> None:
        """停止FINS驱动"""
        self._running = False
        if self._client:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception as e:
                logger.warning("FINS驱动断开异常: %s", e)
            self._client = None
        logger.info("FINS驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取欧姆龙PLC测点值

        测点地址格式: "D100" (DM区), "CIO100" (CIO区), "W100" (工作区)
        完整格式: "area,offset,bits" 如 "DM,100,16" (DM区偏移100，16位)
        简写格式: "D100" 自动解析为DM区
        """
        if not self._running or not self._client:
            return {}

        result = {}
        async with self._lock:
            for point_addr in points:
                try:
                    value = await asyncio.to_thread(
                        self._read_point, point_addr
                    )
                    result[point_addr] = value
                except Exception as e:
                    logger.warning("FINS读取失败 %s: %s", point_addr, e)
                    result[point_addr] = None
        return result

    def _parse_address(self, address: str) -> tuple[int, int, int]:
        """解析FINS地址为(area_code, offset, bit_count)

        欧姆龙内存区域:
            CIO: area=0xB0 (PLC), 偏移0-6143
            WR:  area=0xB1, 偏移0-511
            HR:  area=0xB2, 偏移0-511
            AR:  area=0xB3, 偏移0-255
            DM:  area=0x82, 偏移0-32767
            EM:  area=0x90+, 偏移0-32767
        """
        # 简写格式解析
        addr_upper = address.upper()

        if addr_upper.startswith("D"):
            # DM区: D100 -> (0x82, 100, 16)
            offset = int(addr_upper[1:])
            return (0x82, offset, 16)
        elif addr_upper.startswith("CIO") or addr_upper.startswith("C"):
            # CIO区
            prefix = "CIO" if addr_upper.startswith("CIO") else "C"
            offset = int(addr_upper[len(prefix):])
            return (0xB0, offset, 16)
        elif addr_upper.startswith("W"):
            # 工作区
            offset = int(addr_upper[1:])
            return (0xB1, offset, 16)
        elif addr_upper.startswith("H"):
            # 保持寄存器区
            offset = int(addr_upper[1:])
            return (0xB2, offset, 16)
        elif addr_upper.startswith("A"):
            # 辅助区
            offset = int(addr_upper[1:])
            return (0xB3, offset, 16)
        else:
            # 逗号分隔格式: "area,offset,bits"
            parts = address.split(",")
            if len(parts) >= 3:
                return (int(parts[0], 0), int(parts[1]), int(parts[2]))
            elif len(parts) == 2:
                return (int(parts[0], 0), int(parts[1]), 16)
            raise ValueError(f"无效的FINS地址: {address}")

    def _read_point(self, address: str) -> Any:
        """同步读取单个测点"""
        area, offset, bit_count = self._parse_address(address)

        if bit_count == 1:
            return self._client.read_bit(area, offset, 0)
        elif bit_count == 16:
            return self._client.read_word(area, offset)
        elif bit_count == 32:
            high = self._client.read_word(area, offset)
            low = self._client.read_word(area, offset + 1)
            return (high << 16) | (low & 0xFFFF)
        else:
            return self._client.read_word(area, offset)

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入欧姆龙PLC测点值"""
        if not self._running or not self._client:
            return False

        try:
            async with self._lock:
                area, offset, bit_count = self._parse_address(point)
                if bit_count == 1:
                    await asyncio.to_thread(
                        self._client.write_bit, area, offset, 0, int(bool(value))
                    )
                else:
                    await asyncio.to_thread(
                        self._client.write_word, area, offset, int(value)
                    )
            return True
        except Exception as e:
            logger.error("FINS写入失败 %s: %s", point, e)
            return False


    def _read_points_batch(self, points: list[str]) -> dict[str, Any]:
        """同步批量读取（单次to_thread调用，减少线程切换开销）"""
        result = {}
        for p in points:
            try:
                result[p] = self._read_point(p)
            except Exception:
                result[p] = None
        return result

    async def discover_devices(self, config: dict) -> list[dict]:
        """FINS协议不支持自动发现"""
        return []
