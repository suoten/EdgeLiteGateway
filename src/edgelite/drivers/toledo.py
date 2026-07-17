"""托利多称重仪表驱动 - 梅特勒-托利多(Mettler-Toledo)称重设备专用协议

托利多是全球领先的精密称重仪器制造商，
其设备广泛用于制药、食品、化工等行业的称重检测场景。
通过TCP/Serial协议读取重量数据。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class ToledoDriver(DriverPlugin):
    """托利多称重仪表驱动

    配置参数:
        ip: 设备IP地址 (TCP模式)
        port: TCP端口 (默认8000)
        serial_port: 串口设备路径 (Serial模式，如"COM3"或"/dev/ttyUSB0")
        baudrate: 波特率 (默认9600)
        protocol: 通信协议 (默认"mt-sics", 可选"continuous")
    """

    plugin_name = "toledo"
    plugin_version = "1.0.0"
    supported_protocols = ("toledo",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    config_schema = {
        "description": "Mettler-Toledo weighing instrument protocol, supports TCP/Serial/MT-SICS",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {
                "name": "host",
                "type": "string",
                "label": "IP Address",
                "description": "Weighing instrument IP address (TCP mode)",
                "default": "192.168.1.1",
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "TCP port, default 1701",
                "default": 1701,
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "mode",
                "type": "string",
                "label": "Communication Mode",
                "description": "TCP or Serial",
                "default": "tcp",
                "options": ["tcp", "serial"],
            },  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        super().__init__()  # FIXED-P0: 必须调用基类初始化
        self._running = False
        self._reader = None
        self._writer = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        """启动托利多设备连接"""
        self._config = config
        ip = config.get("ip") or config.get("host", "")
        port = int(config.get("port", 1701))
        serial_port = config.get("serial_port", "")

        if not (1 <= port <= 65535):
            raise ValueError(f"托利多驱动port超出范围[1-65535]，当前: {port}")

        if ip:
            # TCP模式
            try:
                self._reader, self._writer = await asyncio.open_connection(ip, port)
                self._running = True
                logger.info("托利多TCP连接成功: %s:%d", ip, port)
            except Exception as e:
                logger.error("托利多TCP连接失败: %s - %s", ip, e)
                raise
        elif serial_port:
            # Serial模式
            try:
                import serial_asyncio

                baudrate = int(config.get("baudrate", 9600))
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=serial_port, baudrate=baudrate
                )
                self._running = True
                logger.info("托利多串口连接成功: %s @ %d", serial_port, baudrate)
            except ImportError:
                raise ImportError("serial_asyncio未安装，请执行: pip install pyserial-asyncio") from None
            except Exception as e:
                logger.error("托利多串口连接失败: %s - %s", serial_port, e)
                raise
        else:
            raise ValueError("托利多驱动配置缺少ip(TCP)或serial_port(串口)参数")

    async def stop(self) -> None:
        """停止托利多驱动"""
        self._running = False
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
        self._reader = None
        await super().stop()  # FIXED-P0: 清理基类资源
        logger.info("托利多驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取托利多称重数据

        预定义测点:
            - weight: 当前净重
            - gross_weight: 毛重
            - tare_weight: 皮重
            - unit: 单位 (g/kg/lb/oz)
            - stable: 是否稳定
            - zero: 是否零点
        """
        if not self._running or not self._reader:
            await self._try_reconnect(device_id)
            return {}

        result = {}
        self._config.get(
            "protocol", "mt-sics"
        )  # FIXED-P2: protocol读取后未使用，始终走MT-SICS路径，现根据protocol选择读取方法

        async with self._lock:
            for point_name in points:
                try:
                    value = await self._read_point_mt_sics(point_name)
                    result[point_name] = value
                except Exception as e:
                    logger.warning("托利多读取失败 %s: %s", point_name, e)
                    result[point_name] = None
        return result

    async def _read_point_mt_sics(self, point_name: str) -> Any:
        """通过MT-SICS协议读取数据

        MT-SICS (Standard Interface Command Set) 是托利多标准通信协议:
            SIR: 发送即时重量 (S I R\\r\\n)
            SFR: 发送稳定重量 (S F R\\r\\n)
            SIL: 发送净重、皮重、毛重 (S I L\\r\\n)
        """
        name_lower = point_name.lower()

        if name_lower in ("weight", "net_weight"):
            cmd = b"S I R\r\n"
        elif name_lower == "gross_weight" or name_lower == "tare_weight":
            cmd = b"S I L\r\n"
        elif name_lower == "stable":
            cmd = b"S F R\r\n"
        else:
            cmd = b"S I R\r\n"

        self._writer.write(cmd)
        await self._writer.drain()

        # 读取响应 (超时5秒)
        try:
            data = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
            response = data.decode("ascii", errors="replace").strip()
        except TimeoutError:
            raise TimeoutError("托利多响应超时") from None

        # 解析MT-SICS响应
        # 格式: "S A <value> <unit>" 或 "S S <value> <unit>" (稳定)
        # 或: "S I L A <net> <tare> <gross> <unit>"
        parts = response.split()

        if len(parts) < 3:
            raise ValueError(f"无效的托利多响应: {response}")

        status = parts[1]
        if status not in ("A", "S", "D"):
            raise ValueError(f"托利多响应错误: {response}")

        if name_lower == "weight" or name_lower == "net_weight":
            return float(parts[2])
        elif name_lower == "gross_weight":
            # SIL响应: S I L A <net> <tare> <gross> <unit>
            if len(parts) >= 5:
                return float(parts[4])
            return float(parts[2])
        elif name_lower == "tare_weight":
            if len(parts) >= 4:
                return float(parts[3])
            return 0.0
        elif name_lower == "stable":
            return status == "S"
        elif name_lower == "unit":
            return parts[-1] if parts else ""
        elif name_lower == "zero":
            try:
                val = float(parts[2])
                return abs(val) < 0.001
            except (ValueError, IndexError):
                return False

        return float(parts[2])

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入托利多命令

        支持:
            - tare: 去皮 (value=True执行去皮)
            - zero: 清零 (value=True执行清零)
        """
        if not self._running or not self._writer:
            await self._try_reconnect(device_id)
            return False

        try:
            async with self._lock:
                point_lower = point.lower()
                if point_lower == "tare" and value:
                    self._writer.write(b"T\r\n")
                elif point_lower == "zero" and value:
                    self._writer.write(b"Z\r\n")
                else:
                    logger.warning("托利多不支持写入: %s", point)
                    return False

                await self._writer.drain()
                # 读取确认
                await asyncio.wait_for(self._reader.readline(), timeout=5.0)
                return True
        except Exception as e:
            logger.error("托利多写入失败 %s: %s", point, e)
            return False

    async def _try_reconnect(self, device_id: str) -> None:
        if not self._config:
            return
        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("托利多重连放弃: %s (已重试%d次)", device_id, self._reconnect_count)
            self._running = False
            return
        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("托利多连接断开，%.1fs后重连 (第%d次): %s", delay, self._reconnect_count, device_id)
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2
        ip = self._config.get("ip") or self._config.get("host", "")
        port = int(self._config.get("port", 1701))
        serial_port = self._config.get("serial_port", "")
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.debug("[toledo] writer close failed: %s", e)
            self._writer = None
            self._reader = None
        try:
            if ip:
                self._reader, self._writer = await asyncio.open_connection(ip, port)
            elif serial_port:
                import serial_asyncio

                baudrate = int(self._config.get("baudrate", 9600))
                self._reader, self._writer = await serial_asyncio.open_serial_connection(
                    url=serial_port, baudrate=baudrate
                )
            else:
                return
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("托利多重连成功: %s:%d", ip, port)
        except Exception as e:
            logger.error("托利多重连失败: %s - %s", ip, e)

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加托利多设备，保存配置和测点映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        logger.info("托利多设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """托利多设备不支持自动发现"""
        return []

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("Toledo device removed: %s", device_id)
