"""BACnet协议驱动 - 基于BAC0库，支持楼宇自控设备接入

BACnet (Building Automation and Control Networks) 是楼宇自动化领域的标准通信协议，
广泛用于HVAC(暖通空调)、照明、安防等楼宇自控系统。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class BACnetDriver(DriverPlugin):
    """BACnet协议驱动

    配置参数:
        ip: BACnet设备IP地址
        port: BACnet/IP UDP端口 (默认47808)
        device_id: BACnet设备ID
        subnet: 子网掩码 (可选，用于Who-Is广播)
    """

    plugin_name = "bacnet"
    plugin_version = "1.0.0"
    supported_protocols = ["bacnet"]

    def __init__(self):
        self._running = False
        self._bacnet = None
        self._config: dict = {}
        self._lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        try:
            import BAC0
        except ImportError:
            raise ImportError("BAC0未安装，请执行: pip install BAC0")

        self._config = config
        ip = config.get("ip", "")
        port = int(config.get("port", 47808))
        subnet = config.get("subnet", "")

        try:
            if ip and subnet:
                self._bacnet = BAC0.connect(ip=ip, subnet=subnet)
            else:
                self._bacnet = BAC0.lite()

            self._running = True
            logger.info("BACnet驱动启动成功 (ip=%s, port=%d)", ip or "auto", port)
        except Exception as e:
            logger.error("BACnet驱动启动失败: %s", e)
            raise

    async def stop(self) -> None:
        self._running = False
        if self._bacnet:
            try:
                await asyncio.to_thread(self._bacnet.disconnect)
            except Exception as e:
                logger.warning("BACnet驱动断开异常: %s", e)
            self._bacnet = None
        logger.info("BACnet驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        if not self._running or not self._bacnet:
            return {}

        result = {}
        device_ip = self._config.get("ip", "")
        device_id_bacnet = self._config.get("device_id", "")

        async with self._lock:
            for point_addr in points:
                try:
                    value = await asyncio.to_thread(
                        self._read_point, device_ip, device_id_bacnet, point_addr
                    )
                    result[point_addr] = value
                except Exception as e:
                    logger.warning("BACnet读取失败 %s: %s", point_addr, e)
                    result[point_addr] = None

        return result

    def _read_point(self, device_ip: str, device_id: str, address: str) -> Any:
        parts = address.split(",")
        if len(parts) >= 3:
            obj_type = parts[0]
            instance = parts[1]
            prop = parts[2]
            read_addr = f"{device_ip} {obj_type} {instance} {prop}"
        elif len(parts) == 2:
            obj_type = parts[0]
            instance = parts[1]
            read_addr = f"{device_ip} {obj_type} {instance} presentValue"
        else:
            read_addr = f"{device_ip} {address}"

        return self._bacnet.read(read_addr)

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._running or not self._bacnet:
            return False

        device_ip = self._config.get("ip", "")

        try:
            async with self._lock:
                parts = point.split(",")
                if len(parts) >= 2:
                    obj_type = parts[0]
                    instance = parts[1]
                    prop = parts[2] if len(parts) >= 3 else "presentValue"
                    write_addr = f"{device_ip} {obj_type} {instance} {prop}"
                else:
                    write_addr = f"{device_ip} {point}"

                await asyncio.to_thread(self._bacnet.write, write_addr, value)
            return True
        except Exception as e:
            logger.error("BACnet写入失败 %s: %s", point, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        if not self._bacnet:
            return []

        try:
            devices = await asyncio.to_thread(self._bacnet.whois)
            result = []
            for dev in devices or []:
                result.append({
                    "device_id": str(dev.get("device_id", "")),
                    "name": dev.get("name", f"bacnet-device-{dev.get('device_id', '')}"),
                    "ip": dev.get("address", ""),
                    "protocol": "bacnet",
                })
            return result
        except Exception as e:
            logger.error("BACnet设备发现失败: %s", e)
            return []
