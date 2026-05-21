"""多网关级联管理器 - 基于mDNS(zeroconf)实现邻居发现与拓扑构建"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

_MDNS_SERVICE_TYPE = "_edgelite._tcp.local."


class TopologyStatus(StrEnum):
    """拓扑节点角色"""
    STANDALONE = "standalone"
    PARENT = "parent"
    CHILD = "child"
    PEER = "peer"


@dataclass
class NeighborInfo:
    """邻居网关信息"""
    neighbor_id: str
    host: str
    port: int
    role: str = "peer"
    properties: dict[str, str] = field(default_factory=dict)
    last_seen: float = field(default_factory=time.time)


@dataclass
class CascadeTopology:
    """级联拓扑数据"""
    local_id: str
    status: TopologyStatus = TopologyStatus.STANDALONE
    parent_id: str | None = None
    children: list[str] = field(default_factory=list)
    peers: list[NeighborInfo] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)


class CascadeManager:
    """多网关级联管理器，使用zeroconf进行mDNS服务发现"""

    def __init__(
        self,
        local_id: str | None = None,
        service_name: str = "EdgeLite",
        parent_host: str | None = None,
        parent_port: int | None = None,
    ):
        self._local_id = local_id or socket.gethostname()
        self._service_name = service_name
        self._parent_host = parent_host
        self._parent_port = parent_port
        self._running = False
        self._zeroconf: Any = None
        self._registration: Any = None
        self._browser: Any = None
        self._neighbors: dict[str, NeighborInfo] = {}
        self._topology = CascadeTopology(local_id=self._local_id)
        self._discover_task: asyncio.Task | None = None

    @property
    def topology(self) -> CascadeTopology:
        """获取当前拓扑数据"""
        return self._topology

    @property
    def neighbors(self) -> list[NeighborInfo]:
        """获取邻居列表"""
        return list(self._neighbors.values())

    async def start(self) -> None:
        """启动级联管理器，注册mDNS服务并开始邻居发现。"""
        if self._running:
            return

        self._running = True

        try:
            from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser

            self._zeroconf = Zeroconf()

            info = ServiceInfo(
                _MDNS_SERVICE_TYPE,
                f"{self._service_name}.{_MDNS_SERVICE_TYPE}",
                addresses=[socket.inet_aton(self._get_local_ip())],
                port=8080,
                properties={
                    "local_id": self._local_id,
                    "version": "1.0",
                },
            )
            await self._zeroconf.async_register_service(info)
            self._registration = info

            self._browser = ServiceBrowser(self._zeroconf, _MDNS_SERVICE_TYPE, self._on_service_state_change)

            if self._parent_host:
                self._topology.status = TopologyStatus.CHILD
                self._topology.parent_id = f"{self._parent_host}:{self._parent_port or 8080}"
                self._discover_task = asyncio.create_task(self._maintain_parent_connection(), name="cascade-parent-maintain")

            logger.info("CascadeManager started: local_id=%s role=%s", self._local_id, self._topology.status)

        except ImportError:
            logger.warning("zeroconf库未安装，级联发现功能不可用")
            self._running = True
        except Exception as e:
            logger.error("CascadeManager启动失败: %s", e)

    async def stop(self) -> None:
        """停止级联管理器，注销mDNS服务。"""
        self._running = False

        if self._discover_task and not self._discover_task.done():
            self._discover_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._discover_task

        if self._zeroconf:
            try:
                if self._registration:
                    await self._zeroconf.async_unregister_service(self._registration)
                await self._zeroconf.async_close()
            except Exception as e:
                logger.debug("Zeroconf关闭异常: %s", e)

        self._zeroconf = None
        self._registration = None
        self._browser = None
        logger.info("CascadeManager stopped")

    def _on_service_state_change(
        self, zeroconf: Any, service_type: str, name: str, state_change: Any
    ) -> None:
        """mDNS服务状态变更回调。"""
        try:
            from zeroconf import ServiceStateChange

            if state_change == ServiceStateChange.Added:
                info = zeroconf.get_service_info(service_type, name)
                if info:
                    neighbor_id = info.properties.get(b"local_id", b"").decode() or name
                    host = socket.inet_ntoa(info.addresses[0]) if info.addresses else ""
                    port = info.port
                    props = {k.decode(): v.decode() for k, v in info.properties.items()}

                    self._neighbors[neighbor_id] = NeighborInfo(
                        neighbor_id=neighbor_id,
                        host=host,
                        port=port,
                        role="peer",
                        properties=props,
                    )
                    self._rebuild_topology()
                    logger.info("发现邻居: id=%s host=%s:%d", neighbor_id, host, port)

            elif state_change == ServiceStateChange.Removed:
                for nid in list(self._neighbors.keys()):
                    if nid in name:
                        del self._neighbors[nid]
                        self._rebuild_topology()
                        logger.info("邻居离线: id=%s", nid)
                        break
        except Exception as e:
            logger.error("mDNS服务状态回调异常: %s", e)

    def _rebuild_topology(self) -> None:
        """根据邻居信息重建拓扑。"""
        self._topology.peers = list(self._neighbors.values())
        if self._topology.parent_id:
            parent_ids = [n for n in self._neighbors if n == self._topology.parent_id]
            self._topology.children = [
                n.neighbor_id for n in self._neighbors.values()
                if n.role == "child"
            ]
        self._topology.updated_at = time.time()

    async def discover_neighbors(self, timeout: float = 5.0) -> list[NeighborInfo]:
        """主动发现邻居网关。

        Args:
            timeout: 发现等待超时(秒)。

        Returns:
            当前已发现的邻居列表。
        """
        if not self._zeroconf:
            return list(self._neighbors.values())

        await asyncio.sleep(min(timeout, 5.0))
        return list(self._neighbors.values())

    def build_topology(self) -> CascadeTopology:
        """构建并返回当前级联拓扑。

        Returns:
            CascadeTopology: 包含本节点角色、父/子/邻居信息的拓扑数据。
        """
        self._rebuild_topology()
        return self._topology

    async def forward_to_parent(self, data: dict[str, Any]) -> bool:
        """将数据转发至父节点网关。

        Args:
            data: 待转发的数据字典。

        Returns:
            转发是否成功。
        """
        if not self._parent_host or not self._parent_port:
            logger.warning("未配置父节点，无法转发数据")
            return False

        try:
            import aiohttp

            url = f"http://{self._parent_host}:{self._parent_port}/api/v1/integration/cascade/forward"
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    return resp.status == 200
        except ImportError:
            logger.warning("aiohttp库未安装，数据转发不可用")
            return False
        except Exception as e:
            logger.error("数据转发至父节点失败: %s", e)
            return False

    async def update_config(self, config: dict[str, Any]) -> None:
        """更新级联配置。

        Args:
            config: 包含parent_host/parent_port/role等字段的配置字典。
        """
        if "parent_host" in config:
            self._parent_host = config["parent_host"]
        if "parent_port" in config:
            self._parent_port = config["parent_port"]
        if "role" in config:
            role = config["role"]
            if role in (TopologyStatus.PARENT, TopologyStatus.CHILD, TopologyStatus.PEER, TopologyStatus.STANDALONE):
                self._topology.status = TopologyStatus(role)
        if self._parent_host:
            self._topology.parent_id = f"{self._parent_host}:{self._parent_port or 8080}"
            self._topology.status = TopologyStatus.CHILD
        self._rebuild_topology()
        logger.info("级联配置已更新: parent=%s:%s role=%s", self._parent_host, self._parent_port, self._topology.status)

    async def remove_neighbor(self, neighbor_id: str) -> bool:
        """移除指定邻居。

        Args:
            neighbor_id: 邻居网关ID。

        Returns:
            是否成功移除。
        """
        if neighbor_id in self._neighbors:
            del self._neighbors[neighbor_id]
            self._rebuild_topology()
            logger.info("邻居已移除: id=%s", neighbor_id)
            return True
        return False

    async def _maintain_parent_connection(self) -> None:
        """维持与父节点的连接（心跳检测）。"""
        while self._running:
            try:
                await asyncio.sleep(30)
                if self._parent_host:
                    success = await self.forward_to_parent({"type": "heartbeat", "local_id": self._local_id})
                    if not success:
                        logger.debug("父节点心跳失败: %s:%s", self._parent_host, self._parent_port)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("父节点维持异常: %s", e)

    @staticmethod
    def _get_local_ip() -> str:
        """获取本机局域网IP地址。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
