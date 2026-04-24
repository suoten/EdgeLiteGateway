"""多网关级联管理器 - v1.1 Pro版特性"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from edgelite.config import get_config

logger = logging.getLogger(__name__)


class GatewayNode:
    """网关节点信息"""

    def __init__(self, node_id: str, endpoint: str, name: str = ""):
        self.node_id = node_id
        self.endpoint = endpoint
        self.name = name or node_id
        self.status = "unknown"
        self.last_heartbeat = 0
        self.devices: list[str] = []
        self.metadata: dict = {}


class ClusterManager:
    """多网关级联管理器，支持网关集群协同"""

    def __init__(self):
        self._nodes: dict[str, GatewayNode] = {}
        self._local_node_id = ""
        self._client = httpx.AsyncClient(timeout=10.0)
        self._heartbeat_task: asyncio.Task | None = None
        self._sync_task: asyncio.Task | None = None
        self._running = False

    async def start(self, local_node_id: str, config: dict | None = None) -> None:
        """启动集群管理器"""
        self._local_node_id = local_node_id
        self._running = True

        # 加载节点配置
        if config and "nodes" in config:
            for node_cfg in config["nodes"]:
                node = GatewayNode(
                    node_id=node_cfg["node_id"],
                    endpoint=node_cfg["endpoint"],
                    name=node_cfg.get("name", ""),
                )
                self._nodes[node.node_id] = node

        # 启动心跳和同步任务
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._sync_task = asyncio.create_task(self._sync_loop())

        logger.info("集群管理器启动，本节点: %s，集群节点: %d", local_node_id, len(self._nodes))

    async def stop(self) -> None:
        """停止集群管理器"""
        self._running = False
        for task in [self._heartbeat_task, self._sync_task]:
            if task and not task.done():
                task.cancel()
        logger.info("集群管理器停止")

    async def _heartbeat_loop(self) -> None:
        """心跳检测循环"""
        while self._running:
            for node_id, node in self._nodes.items():
                if node_id == self._local_node_id:
                    continue
                try:
                    resp = await self._client.get(f"{node.endpoint}/api/v1/system/status")
                    if resp.status_code == 200:
                        node.status = "online"
                        node.last_heartbeat = time.time()
                        data = resp.json().get("data", {})
                        node.metadata = data
                    else:
                        node.status = "error"
                except Exception:
                    node.status = "offline"

            await asyncio.sleep(30)

    async def _sync_loop(self) -> None:
        """数据同步循环 - 同步设备列表、告警状态、配置到集群节点"""
        while self._running:
            try:
                for node_id, node in self._nodes.items():
                    if node_id == self._local_node_id:
                        continue
                    if node.status != "online":
                        continue

                    # 同步设备列表
                    try:
                        resp = await self._client.get(
                            f"{node.endpoint}/api/v1/devices",
                            params={"page": 1, "size": 1000},
                        )
                        if resp.status_code == 200:
                            data = resp.json().get("data", [])
                            node.devices = [d["device_id"] for d in data]
                    except Exception as e:
                        logger.debug("同步设备列表失败: %s -> %s", node_id, e)

                    # 同步告警状态（推送本地firing告警到对端）
                    try:
                        await self._client.post(
                            f"{node.endpoint}/api/v1/cluster/sync/alarms",
                            json={"source_node": self._local_node_id},
                        )
                    except Exception as e:
                        logger.debug("同步告警状态失败: %s -> %s", node_id, e)

                    # 同步配置变更（推送本地配置版本到对端）
                    try:
                        await self._client.post(
                            f"{node.endpoint}/api/v1/cluster/sync/config",
                            json={"source_node": self._local_node_id},
                        )
                    except Exception as e:
                        logger.debug("同步配置失败: %s -> %s", node_id, e)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("集群同步循环异常: %s", e)

            await asyncio.sleep(60)

    async def add_node(self, node_id: str, endpoint: str, name: str = "") -> bool:
        """添加网关节点"""
        if node_id in self._nodes:
            return False

        node = GatewayNode(node_id=node_id, endpoint=endpoint, name=name)
        self._nodes[node_id] = node
        logger.info("添加网关节点: %s (%s)", node_id, endpoint)
        return True

    async def remove_node(self, node_id: str) -> bool:
        """移除网关节点"""
        if node_id not in self._nodes:
            return False

        del self._nodes[node_id]
        logger.info("移除网关节点: %s", node_id)
        return True

    def get_node(self, node_id: str) -> GatewayNode | None:
        """获取节点信息"""
        return self._nodes.get(node_id)

    def list_nodes(self) -> list[dict]:
        """列出所有节点"""
        return [
            {
                "node_id": node.node_id,
                "name": node.name,
                "endpoint": node.endpoint,
                "status": node.status,
                "last_heartbeat": node.last_heartbeat,
                "devices": len(node.devices),
            }
            for node in self._nodes.values()
        ]

    async def proxy_request(
        self, target_node_id: str, method: str, path: str, data: dict | None = None
    ) -> dict | None:
        """代理请求到目标节点"""
        node = self._nodes.get(target_node_id)
        if node is None:
            return None

        try:
            url = f"{node.endpoint}{path}"
            if method == "GET":
                resp = await self._client.get(url)
            elif method == "POST":
                resp = await self._client.post(url, json=data)
            elif method == "PUT":
                resp = await self._client.put(url, json=data)
            elif method == "DELETE":
                resp = await self._client.delete(url)
            else:
                return None

            return resp.json()
        except Exception as e:
            logger.error("代理请求失败: %s -> %s: %s", target_node_id, path, e)
            return None

    async def broadcast_event(self, event_type: str, event_data: dict) -> dict[str, bool]:
        """广播事件到所有节点"""
        results = {}
        for node_id, node in self._nodes.items():
            if node_id == self._local_node_id:
                continue
            try:
                resp = await self._client.post(
                    f"{node.endpoint}/api/v1/cluster/event",
                    json={"type": event_type, "data": event_data},
                )
                results[node_id] = resp.status_code == 200
            except Exception:
                results[node_id] = False
        return results

    async def get_cluster_status(self) -> dict:
        """获取集群状态"""
        online = sum(1 for n in self._nodes.values() if n.status == "online")
        offline = sum(1 for n in self._nodes.values() if n.status == "offline")

        return {
            "local_node_id": self._local_node_id,
            "total_nodes": len(self._nodes),
            "online_nodes": online,
            "offline_nodes": offline,
            "nodes": self.list_nodes(),
        }
