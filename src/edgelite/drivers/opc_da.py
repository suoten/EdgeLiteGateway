"""OPC DA Client驱动 - 基于OpenOPC/OPCDA库，连接经典OPC DA Server

OPC DA (Data Access) 是Windows平台传统的工业数据访问标准，
大量老旧SCADA/DCS系统仍使用OPC DA。.NET/COM技术栈。
通过OpenOPC-Python3或OPCDA-Client库实现跨平台访问。

也支持通过OPC DA Gateway代理访问非Windows平台。
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
    plugin_version = "1.1.0"
    supported_protocols = ["opc_da"]
    config_schema = {
        "description": "OPC DA classic protocol (Windows COM), reads data from legacy OPC servers",
        "fields": [
            {"name": "prog_id", "type": "string", "label": "ProgID",
             "description": "OPC DA server ProgID, e.g. Matrikon.OPC.Simulation", "required": True},
            {"name": "host", "type": "string", "label": "Host",
             "description": "OPC server host, leave empty for local machine", "default": "localhost"},
            {"name": "gateway", "type": "string", "label": "Gateway",
             "description": "OPC gateway address (optional, for remote access)", "default": ""},
            {"name": "use_groups", "type": "boolean", "label": "Use Groups",
             "description": "Use OPC groups for batch subscription", "default": True},
            {"name": "update_rate", "type": "integer", "label": "Update Rate (ms)",
             "description": "OPC group update rate in milliseconds", "default": 1000},
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
        # 新增: 订阅功能
        self._subscription = None
        self._subscribed_items: set[str] = set()
        self._latest_values: dict[str, Any] = {}
        self._values_lock = asyncio.Lock()
        self._data_callback: Any = None
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        """启动OPC DA连接"""
        import sys
        if sys.platform != "win32":
            logger.warning("OPC DA仅支持Windows平台，当前平台: %s。可通过OPC网关代理访问。", sys.platform)

        try:
            import OpenOPC
        except ImportError:
            raise ImportError(
                "OpenOPC not installed. Run: pip install OpenOPC-Python3. "
                "Note: OPC DA requires Windows platform or OPC gateway proxy"
            ) from None

        self._config = config
        server = config.get("server", config.get("prog_id", ""))
        host = config.get("host", "localhost")
        gateway = config.get("gateway", "")

        if not server:
            raise ValueError("OPC DA driver config missing 'server' parameter (ProgID)")

        try:
            if gateway:
                self._client = OpenOPC.open_gateway(gateway, host=host)
            else:
                self._client = OpenOPC.client(host=host)

            await asyncio.to_thread(self._client.connect, server)
            self._running = True
            self._reconnect_count = 0
            self._reconnect_delay = self._RECONNECT_BASE_DELAY
            logger.info("OPC DA连接成功: %s@%s", server, host)

            # 创建订阅组
            use_groups = config.get("use_groups", True)
            if use_groups:
                await self._create_subscription()

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
                        if not isinstance(item, (list, tuple)) or len(item) < 2:
                            continue
                        tag_name = item[0] if isinstance(item[0], str) else str(item[0])
                        value = item[1]
                        quality = item[2] if len(item) > 2 else "Unknown"
                        if quality == "Good":
                            result[tag_name] = value
                        else:
                            result[tag_name] = None
                            logger.warning("OPC DA quality exception %s: %s", tag_name, quality)
                elif isinstance(tags, (list, tuple)) and len(tags) >= 2:
                    # 单个读取结果
                    quality = tags[2] if len(tags) > 2 else "Unknown"
                    result[points[0]] = tags[1] if quality == "Good" else None
            except Exception as e:
                logger.warning("OPC DA batch read failed: %s", e)
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

    async def _create_subscription(self) -> None:
        """创建OPC订阅组"""
        if not self._client:
            return

        try:
            update_rate = self._config.get("update_rate", 1000)
            self._subscription = self._client.group(
                name="edgelite_group",
                update_rate=update_rate,
            )
            logger.info("OPC DA订阅组创建成功")
        except Exception as e:
            logger.warning("OPC DA订阅组创建失败: %s", e)

    async def add_subscription(self, points: list[str]) -> None:
        """添加订阅项"""
        if not self._subscription:
            return

        try:
            for point in points:
                if point not in self._subscribed_items:
                    self._subscription.add(point)
                    self._subscribed_items.add(point)
            logger.info("OPC DA订阅项已添加: %d个", len(points))
        except Exception as e:
            logger.error("OPC DA订阅项添加失败: %s", e)

    def on_data(self, callback) -> None:
        """注册数据回调"""
        self._data_callback = callback

    def is_device_connected(self, device_id: str) -> bool:
        """检查OPC DA连接状态"""
        return self._running and self._client is not None

    def get_subscription_stats(self) -> dict:
        """获取订阅统计"""
        return {
            "subscription_active": self._subscription is not None,
            "subscribed_items": len(self._subscribed_items),
            "reconnect_count": self._reconnect_count,
        }

    async def list_servers(self, host: str = "localhost") -> list[str]:
        """列出指定主机上的OPC DA Server"""
        try:
            import OpenOPC

            client = OpenOPC.client(host=host)
            return client.servers()
        except Exception as e:
            logger.error("列出OPC服务器失败: %s", e)
            return []

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加OPC DA设备，保存配置并将测点添加到订阅组"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        # 将新设备的测点添加到订阅组
        if self._subscription and points:
            item_ids = [p.get("address", p.get("name", "")) for p in points if p.get("address") or p.get("name")]
            if item_ids:
                await self.add_subscription(item_ids)
        logger.info("OPC DA设备已添加: %s (%d测点)", device_id, len(points))

    async def browse_server_items(self) -> list[dict]:
        """浏览OPC DA Server的可用项"""
        if not self._client:
            return []

        try:
            tree = await asyncio.to_thread(self._client.browse)
            items = []
            for branch in tree:
                items.append({
                    "name": branch,
                    "protocol": "opc_da",
                })
            return items
        except Exception as e:
            logger.error("OPC DA浏览失败: %s", e)
            return []

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        # 清理订阅缓存
        self._subscribed_items.clear()
        self._latest_values.clear()
        logger.info("OPC DA device removed: %s", device_id)
