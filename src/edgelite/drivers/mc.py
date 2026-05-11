"""三菱MC协议驱动 - 基于pymcprotocol库，支持iQ-R/iQ-Q系列PLC"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class McDriver(DriverPlugin):
    """三菱MC协议驱动

    配置参数:
        ip: PLC IP地址
        port: 端口号 (默认5007 for iQ-R, 5002 for Q series)
        plc_type: PLC型号 (默认"iQ-R")
    """

    plugin_name = "mitsubishi_mc"
    plugin_version = "1.0.0"
    supported_protocols = ["mc"]
    config_schema = {
        "description": "三菱MC协议（MELSEC Communication），支持Q/L/FX系列PLC",
        "fields": [
            {"name": "host", "type": "string", "label": "IP地址", "description": "PLC的IP地址", "default": "192.168.1.1", "required": True},
            {"name": "port", "type": "integer", "label": "端口", "description": "MC协议端口，默认5007", "default": 5007},
            {"name": "plc_type", "type": "string", "label": "PLC型号", "description": "Q系列=Q，L系列=L，FX系列=iQ-R", "default": "Q", "options": ["Q", "L", "iQ-R"]},
        ],
    }

    def __init__(self):
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        """启动MC驱动连接"""
        try:
            from pymcprotocol import Type3E
        except ImportError:
            raise ImportError("pymcprotocol未安装，请执行: pip install pymcprotocol") from None

        self._config = config
        ip = config.get("ip", "")
        port = int(config.get("port", 5007))
        plc_type = config.get("plc_type", "iQ-R")

        if not ip:
            raise ValueError("MC驱动配置缺少ip参数")

        try:
            self._client = Type3E(ip=ip, port=port, plc_type=plc_type)
            await asyncio.to_thread(self._client.connect)
            self._running = True
            logger.info("MC驱动连接成功: %s:%d (%s)", ip, port, plc_type)
        except Exception as e:
            logger.error("MC驱动连接失败: %s - %s", ip, e)
            raise

    async def stop(self) -> None:
        """停止MC驱动"""
        self._running = False
        if self._client:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception as e:
                logger.warning("MC驱动断开异常: %s", e)
            self._client = None
        logger.info("MC驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取三菱PLC测点值

        测点地址格式: "D100" (数据寄存器), "M0" (内部继电器),
                      "X0" (输入), "Y0" (输出), "W0" (链接寄存器)
        字/位操作通过地址后缀区分:
            "D100" - 读取16位字
            "D100.0" - 读取位
            "D100.U" - 读取无符号16位
            "D100.L" - 读取32位长字
            "D100.F" - 读取浮点数
        """
        if not self._running or not self._client:
            return {}

        result = {}
        async with self._lock:
            for point_addr in points:
                try:
                    value = await asyncio.to_thread(self._read_point, point_addr)
                    result[point_addr] = value
                except Exception as e:
                    logger.warning("MC读取失败 %s: %s", point_addr, e)
                    result[point_addr] = None

        return result

    def _read_point(self, address: str) -> Any:
        """同步读取单个测点"""
        # 解析地址
        addr, suffix = self._parse_address(address)

        if suffix == "bit":
            # 位读取
            return self._client.read_bit_device(addr, 1)[0]
        elif suffix == "word":
            # 字读取(16位有符号)
            values = self._client.read_device(addr, 1)
            return values[0]
        elif suffix == "uword":
            # 无符号字读取
            values = self._client.read_device(addr, 1)
            return values[0] & 0xFFFF
        elif suffix == "long":
            # 双字读取(32位)
            values = self._client.read_device(addr, 2)
            return (values[0] << 16) | (values[1] & 0xFFFF)
        elif suffix == "float":
            # 浮点数读取(32位)
            import struct

            values = self._client.read_device(addr, 2)
            raw = struct.pack(">HH", values[0] & 0xFFFF, values[1] & 0xFFFF)
            return struct.unpack(">f", raw)[0]
        else:
            values = self._client.read_device(addr, 1)
            return values[0]

    def _parse_address(self, address: str) -> tuple[str, str]:
        """解析MC地址，返回(设备地址, 类型后缀)"""
        parts = address.split(".")
        addr = parts[0]

        if len(parts) > 1:
            bit_suffix = parts[1]
            if bit_suffix.isdigit():
                # 位偏移，如 D100.0
                return f"{addr}.{bit_suffix}", "bit"
            suffix_map = {
                "U": "uword",
                "L": "long",
                "F": "float",
            }
            return addr, suffix_map.get(bit_suffix.upper(), "word")

        # 根据设备类型判断默认读取方式
        device_type = addr[0].upper() if addr else ""
        if device_type in ("M", "X", "Y", "B", "F"):
            return addr, "bit"
        return addr, "word"

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入三菱PLC测点值"""
        if not self._running or not self._client:
            return False

        try:
            async with self._lock:
                await asyncio.to_thread(self._write_point, point, value)
            return True
        except Exception as e:
            logger.error("MC写入失败 %s: %s", point, e)
            return False

    def _write_point(self, address: str, value: Any) -> None:
        """同步写入单个测点"""
        addr, suffix = self._parse_address(address)

        if suffix == "bit":
            self._client.write_bit_device(addr, [int(bool(value))])
        else:
            self._client.write_device(addr, [int(value)])

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
        """MC协议不支持自动发现，返回空列表"""
        return []
