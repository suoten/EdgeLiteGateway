"""OPC DA Gateway - 跨平台OPC DA访问代理

OPC DA (Data Access) 是Windows平台的传统工业数据访问标准。
本模块提供HTTP网关代理，允许Linux/Mac等非Windows平台通过
WebSocket/HTTP连接到远程OPC DA服务器。

架构:
    [Linux网关] <--HTTP/WS--> [Windows OPC代理服务] <--DCOM--> [OPC DA Server]

使用方式:
    1. 在Windows机器上运行OPC代理服务 (opc_da_proxy.exe)
    2. 配置本网关连接到代理服务
    3. 即可在Linux上访问OPC DA数据
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


@dataclass
class OpcDaSubscription:
    """OPC DA订阅"""
    group_name: str
    items: list[str]
    callback: Any = None
    update_rate: int = 1000


class OpcDaGatewayClient:
    """OPC DA网关客户端 - 连接到远程OPC DA代理服务"""

    def __init__(
        self,
        proxy_url: str = "http://localhost:8081",
        timeout: float = 10.0,
    ):
        self._proxy_url = proxy_url.rstrip("/")
        self._timeout = timeout
        self._client = None
        self._connected = False
        self._subscriptions: dict[str, OpcDaSubscription] = {}
        self._latest_values: dict[str, Any] = {}

    async def connect(self) -> bool:
        """连接到OPC DA代理服务"""
        try:
            import httpx
        except ImportError:
            raise ImportError("OPC DA网关需要httpx库: pip install httpx") from None

        self._client = httpx.AsyncClient(
            base_url=self._proxy_url,
            timeout=self._timeout,
        )

        try:
            resp = await self._client.get("/api/v1/health")
            if resp.status_code == 200:
                self._connected = True
                logger.info("OPC DA Gateway connected: %s", self._proxy_url)
                return True
            else:
                logger.error("OPC DA Gateway health check failed: %d", resp.status_code)
                return False
        except Exception as e:
            logger.error("OPC DA Gateway connection failed: %s", e)
            return False

    async def disconnect(self) -> None:
        """断开连接"""
        self._connected = False
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("OPC DA Gateway disconnected")

    async def list_servers(self, host: str = "localhost") -> list[str]:
        """列出可用的OPC服务器"""
        if not self._client or not self._connected:
            return []

        try:
            resp = await self._client.get(f"/api/v1/servers?host={host}")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("servers", [])
            return []
        except Exception as e:
            logger.error("OPC DA list servers failed: %s", e)
            return []

    async def connect_server(self, server_id: str) -> bool:
        """连接到指定的OPC服务器"""
        if not self._client or not self._connected:
            return False

        try:
            resp = await self._client.post(
                "/api/v1/connect",
                json={"server": server_id},
            )
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.error("OPC DA connect server failed: %s", e)
            return False

    async def browse(self, item_path: str = "") -> list[dict]:
        """浏览OPC服务器项树"""
        if not self._client or not self._connected:
            return []

        try:
            resp = await self._client.get(
                "/api/v1/browse",
                params={"path": item_path},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("items", [])
            return []
        except Exception as e:
            logger.error("OPC DA browse failed: %s", e)
            return []

    async def read(self, items: list[str]) -> dict[str, Any]:
        """读取多个OPC项的值

        Args:
            items: OPC项ID列表，如 ["Channel1.Device1.Tag1", "Simulation.Items.Random"]

        Returns:
            {item_id: value} 字典
        """
        if not self._client or not self._connected:
            return {item: None for item in items}

        try:
            resp = await self._client.post(
                "/api/v1/read",
                json={"items": items},
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])

                result = {}
                for item_result in results:
                    item_id = item_result.get("id", "")
                    value = item_result.get("value")
                    quality = item_result.get("quality", "")

                    if quality.lower() == "good":
                        result[item_id] = value
                    else:
                        result[item_id] = None
                        logger.warning("OPC DA quality exception %s: %s", item_id, quality)

                # 缓存最新值
                for item_id, value in result.items():
                    if value is not None:
                        self._latest_values[item_id] = value

                return result
            else:
                logger.warning("OPC DA read failed: %d", resp.status_code)
                return {item: self._latest_values.get(item) for item in items}

        except Exception as e:
            logger.error("OPC DA read error: %s", e)
            return {item: self._latest_values.get(item) for item in items}

    async def write(self, item: str, value: Any) -> bool:
        """写入OPC项的值"""
        if not self._client or not self._connected:
            return False

        try:
            resp = await self._client.post(
                "/api/v1/write",
                json={"item": item, "value": value},
            )
            if resp.status_code == 200:
                data = resp.json()
                return data.get("success", False)
            return False
        except Exception as e:
            logger.error("OPC DA write error: %s", e)
            return False

    async def create_subscription(
        self,
        group_name: str,
        items: list[str],
        update_rate: int = 1000,
    ) -> str | None:
        """创建订阅组"""
        if not self._client or not self._connected:
            return None

        try:
            resp = await self._client.post(
                "/api/v1/subscribe",
                json={
                    "group": group_name,
                    "items": items,
                    "update_rate": update_rate,
                },
            )
            if resp.status_code in (200, 201):
                data = resp.json()
                sub_id = data.get("subscription_id", "")

                self._subscriptions[sub_id] = OpcDaSubscription(
                    group_name=group_name,
                    items=items,
                    update_rate=update_rate,
                )

                logger.info("OPC DA subscription created: %s (%d items)", sub_id, len(items))
                return sub_id
            return None

        except Exception as e:
            logger.error("OPC DA create subscription failed: %s", e)
            return None

    async def remove_subscription(self, subscription_id: str) -> bool:
        """移除订阅组"""
        if not self._client or not self._connected:
            return False

        try:
            resp = await self._client.delete(f"/api/v1/subscribe/{subscription_id}")
            if resp.status_code == 200:
                if subscription_id in self._subscriptions:
                    del self._subscriptions[subscription_id]
                return True
            return False
        except Exception as e:
            logger.error("OPC DA remove subscription failed: %s", e)
            return False

    def on_data(self, subscription_id: str, callback) -> None:
        """注册数据回调"""
        if subscription_id in self._subscriptions:
            self._subscriptions[subscription_id].callback = callback

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected and self._client is not None


class OpcDaGatewayDriver(DriverPlugin):
    """OPC DA Gateway 驱动 - 跨平台OPC DA访问

    通过HTTP/WebSocket连接到远程OPC DA代理服务，
    实现非Windows平台对OPC DA服务器的访问。

    配置参数:
        proxy_url: OPC DA代理服务地址
        timeout: 请求超时(秒)
        default_server: 默认连接的服务器ProgID
    """

    plugin_name = "opc_da_gateway"
    plugin_version = "1.0.0"
    supported_protocols = ["opc_da_gateway", "opc_da_proxy"]
    config_schema = {
        "description": "OPC DA Gateway driver - cross-platform OPC DA access via HTTP proxy",
        "fields": [
            {"name": "proxy_url", "type": "string", "label": "Proxy URL",
             "description": "OPC DA proxy service URL (e.g. http://192.168.1.100:8081)", "default": "http://localhost:8081"},
            {"name": "timeout", "type": "integer", "label": "Timeout (s)",
             "description": "Request timeout in seconds", "default": 10},
            {"name": "default_server", "type": "string", "label": "Default Server",
             "description": "Default OPC DA server ProgID to connect", "default": ""},
            {"name": "default_host", "type": "string", "label": "Default Host",
             "description": "Default OPC server host (for server discovery)", "default": "localhost"},
        ],
    }

    _MAX_RECONNECT_ATTEMPTS = 100
    _RECONNECT_BASE_DELAY = 1.0
    _RECONNECT_MAX_DELAY = 60.0

    def __init__(self):
        self._running = False
        self._client: OpcDaGatewayClient | None = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_delay: float = self._RECONNECT_BASE_DELAY
        self._connected: bool = False
        self._subscriptions: dict[str, str] = {}  # device_id -> subscription_id
        self._data_callback: Any = None

    async def start(self, config: dict) -> None:
        """启动OPC DA网关驱动"""
        self._config = config
        proxy_url = config.get("proxy_url", "http://localhost:8081")
        timeout = float(config.get("timeout", 10.0))

        self._client = OpcDaGatewayClient(proxy_url, timeout)

        try:
            if await self._client.connect():
                self._running = True
                self._connected = True
                self._reconnect_count = 0
                logger.info("OPC DA Gateway driver started: %s", proxy_url)

                # 连接默认服务器
                default_server = config.get("default_server", "")
                if default_server:
                    await self._client.connect_server(default_server)
            else:
                logger.warning("OPC DA Gateway connection failed, will retry in background")
        except Exception as e:
            logger.error("OPC DA Gateway driver start failed: %s", e)
            raise

    async def stop(self) -> None:
        """停止OPC DA网关驱动"""
        self._running = False
        self._connected = False

        # 移除所有订阅
        for sub_id in list(self._subscriptions.values()):
            if self._client:
                await self._client.remove_subscription(sub_id)

        if self._client:
            await self._client.disconnect()
            self._client = None

        logger.info("OPC DA Gateway driver stopped")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加OPC DA设备 - 创建订阅组"""
        if not self._client or not self._connected:
            return

        item_ids = [pt.get("address", pt.get("name", "")) for pt in points]
        item_ids = [i for i in item_ids if i]

        if not item_ids:
            return

        update_rate = config.get("update_rate", 1000)
        sub_id = await self._client.create_subscription(
            group_name=f"edgelite_{device_id}",
            items=item_ids,
            update_rate=update_rate,
        )

        if sub_id:
            self._subscriptions[device_id] = sub_id
            logger.info("OPC DA subscription created for device %s: %s", device_id, sub_id)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取OPC DA项值

        测点地址格式为OPC Item ID:
            如 "Simulation.Items.Random", "Channel1.Device1.Tag1"
        """
        if not self._running or not self._client or not self._connected:
            await self._ensure_connection(device_id)
            return {}

        result = {}
        async with self._lock:
            try:
                result = await self._client.read(points)
            except Exception as e:
                logger.warning("OPC DA Gateway read failed: %s", e)
                result = {p: None for p in points}

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入OPC DA项值"""
        if not self._running or not self._client or not self._connected:
            await self._ensure_connection(device_id)
            return False

        try:
            return await self._client.write(point, value)
        except Exception as e:
            logger.error("OPC DA Gateway write failed: %s", e)
            return False

    async def discover_devices(self, config: dict) -> list[dict]:
        """发现OPC DA服务器"""
        if not self._client or not self._connected:
            return []

        try:
            host = config.get("host", self._config.get("default_host", "localhost"))
            servers = await self._client.list_servers(host)

            return [
                {
                    "name": server,
                    "server_id": server,
                    "protocol": "opc_da_gateway",
                }
                for server in servers
            ]
        except Exception as e:
            logger.error("OPC DA Gateway server discovery failed: %s", e)
            return []

    async def browse_items(self, item_path: str = "") -> list[dict]:
        """浏览OPC服务器项树"""
        if not self._client or not self._connected:
            return []

        try:
            return await self._client.browse(item_path)
        except Exception as e:
            logger.error("OPC DA Gateway browse failed: %s", e)
            return []

    async def _ensure_connection(self, device_id: str) -> None:
        """确保连接有效，必要时重连"""
        if not self._config:
            return

        self._reconnect_count += 1
        if self._reconnect_count > self._MAX_RECONNECT_ATTEMPTS:
            logger.error("OPC DA Gateway reconnect abandoned")
            return

        delay = min(self._reconnect_delay, self._RECONNECT_MAX_DELAY)
        logger.warning("OPC DA Gateway connection lost, retrying in %.1fs (attempt %d)",
                      delay, self._reconnect_count)
        await asyncio.sleep(delay)
        self._reconnect_delay *= 2

        try:
            if self._client:
                if await self._client.connect():
                    self._connected = True
                    self._reconnect_count = 0
                    self._reconnect_delay = self._RECONNECT_BASE_DELAY
                    logger.info("OPC DA Gateway reconnected")

                    # 重新连接服务器
                    default_server = self._config.get("default_server", "")
                    if default_server:
                        await self._client.connect_server(default_server)
        except Exception as e:
            logger.error("OPC DA Gateway reconnect failed: %s", e)

    def on_data(self, callback) -> None:
        """注册数据回调"""
        self._data_callback = callback

    def is_device_connected(self, device_id: str) -> bool:
        """检查连接状态"""
        return self._connected and self._client is not None
