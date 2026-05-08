"""OPC DA Client驱动 - 基于OpenOPC/OPCDA库，连接经典OPC DA Server

OPC DA (Data Access) 是Windows平台传统的工业数据访问标准，
大量老旧SCADA/DCS系统仍使用OPC DA。.NET/COM技术栈。
通过OpenOPC-Python3或OPCDA-Client库实现跨平台访问。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class OpcDaDriver(DriverPlugin):
    """OPC DA Client驱动

    配置参数:
        server: OPC DA Server的ProgID (如"Matrikon.OPC.Simulation")
        host: OPC Server所在主机 (默认localhost)
        gateway: OPC网关地址 (可选，用于远程访问)
    """

    plugin_name = "opc_da"
    plugin_version = "1.0.0"
    supported_protocols = ["opc_da"]

    def __init__(self):
        self._running = False
        self._client = None
        self._config: dict = {}
        self._lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        """启动OPC DA连接"""
        try:
            import OpenOPC
        except ImportError:
            raise ImportError(
                "OpenOPC未安装，请执行: pip install OpenOPC-Python3。"
                "注意：OPC DA需要Windows平台或通过OPC网关代理访问"
            ) from None

        self._config = config
        server = config.get("server", "")
        host = config.get("host", "localhost")
        gateway = config.get("gateway", "")

        if not server:
            raise ValueError("OPC DA驱动配置缺少server参数(ProgID)")

        try:
            if gateway:
                self._client = OpenOPC.open_gateway(gateway, host=host)
            else:
                self._client = OpenOPC.client(host=host)

            self._client.connect(server)
            self._running = True
            logger.info("OPC DA连接成功: %s@%s", server, host)
        except Exception as e:
            logger.error("OPC DA连接失败: %s@%s - %s", server, host, e)
            raise

    async def stop(self) -> None:
        """停止OPC DA驱动"""
        self._running = False
        if self._client:
            try:
                await asyncio.to_thread(self._client.close)
            except Exception as e:
                logger.warning("OPC DA断开异常: %s", e)
            self._client = None
        logger.info("OPC DA驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取OPC DA项值

        测点地址格式为OPC Item ID:
            如 "Simulation.Items.Random", "Channel1.Device1.Tag1"
        """
        if not self._running or not self._client:
            return {}

        result = {}
        async with self._lock:
            try:
                # OpenOPC支持批量读取
                tags = await asyncio.to_thread(self._client.read, points)
                if isinstance(tags, list):
                    for item in tags:
                        # OpenOPC返回 (tag_name, value, quality, timestamp)
                        tag_name = item[0]
                        value = item[1]
                        quality = item[2]
                        if quality == "Good":
                            result[tag_name] = value
                        else:
                            result[tag_name] = None
                            logger.warning("OPC DA质量异常 %s: %s", tag_name, quality)
                else:
                    result[points[0]] = tags[1] if tags[2] == "Good" else None
            except Exception as e:
                logger.warning("OPC DA批量读取失败: %s", e)
                for point in points:
                    result[point] = None
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入OPC DA项值"""
        if not self._running or not self._client:
            return False

        try:
            async with self._lock:
                await asyncio.to_thread(self._client.write, (point, value))
            return True
        except Exception as e:
            logger.error("OPC DA写入失败 %s: %s", point, e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """浏览OPC DA Server的可用项"""
        if not self._client:
            return []

        try:
            # 浏览OPC服务器节点
            tree = await asyncio.to_thread(self._client.browse)
            items = []
            for branch in tree:
                items.append(
                    {
                        "name": branch,
                        "protocol": "opc_da",
                    }
                )
            return items
        except Exception as e:
            logger.error("OPC DA浏览失败: %s", e)
            return []

    async def list_servers(self, host: str = "localhost") -> list[str]:
        """列出指定主机上的OPC DA Server"""
        try:
            import OpenOPC

            client = OpenOPC.client(host=host)
            return client.servers()
        except Exception as e:
            logger.error("列出OPC服务器失败: %s", e)
            return []
