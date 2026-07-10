"""OPC UA Server 驱动 - 作为 OPC UA 服务器向下游系统提供数据"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


@dataclass
class OpcUaNode:
    """OPC UA 节点定义"""

    node_id: str
    display_name: str
    data_type: str = "Float"
    value: Any = None
    quality: str = "good"
    timestamp: datetime | None = None
    writable: bool = False
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "display_name": self.display_name,
            "data_type": self.data_type,
            "value": self.value,
            "quality": self.quality,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "writable": self.writable,
            "description": self.description,
        }


@dataclass
class OpcUaSubscription:
    """OPC UA 订阅定义"""

    subscription_id: str
    node_ids: list[str]
    callback: Callable | None = None
    sampling_interval: float = 500.0  # ms
    publishing_interval: float = 1000.0  # ms
    max_notifications_per_publish: int = 1000


class OpcUaServerDriver(DriverPlugin):
    """OPC UA Server 协议驱动

    将 EdgeLite 网关作为 OPC UA 服务器，向下游系统（如 SCADA、MES）提供数据。

    支持功能：
    - 启动内置 OPC UA 服务器
    - 动态注册/注销节点
    - 数据变化订阅（Subscription）
    - TLS 安全传输
    - 节点浏览（Address Space）
    """

    plugin_name = "opcua_server"
    plugin_version = "1.0.0"
    supported_protocols = ["opcua_server"]

    config_schema = {
        "description": "OPC UA Server driver - acts as OPC UA server to provide data to downstream systems",
        "fields": [
            {
                "name": "host",
                "type": "string",
                "label": "Bind Address",
                "description": "IP address to bind the OPC UA server",
                "default": "0.0.0.0",
            },
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "OPC UA server port, default 4840",
                "default": 4840,
            },
            {
                "name": "server_name",
                "type": "string",
                "label": "Server Name",
                "description": "OPC UA server display name",
                "default": "EdgeLite Gateway",
            },
            {
                "name": "namespace",
                "type": "string",
                "label": "Namespace",
                "description": "Custom namespace URI for nodes",
                "default": "http://edgelite.io/nodes",
            },
            {
                "name": "enable_tls",
                "type": "boolean",
                "label": "Enable TLS",
                "description": "Enable TLS encryption",
                "default": False,
            },
            {
                "name": "cert_path",
                "type": "string",
                "label": "Server Certificate Path",
                "description": "Path to server certificate file (PEM)",
                "default": "",
            },
            {
                "name": "key_path",
                "type": "string",
                "label": "Private Key Path",
                "description": "Path to server private key file (PEM)",
                "default": "",
            },
            {
                "name": "allow_anonymous",
                "type": "boolean",
                "label": "Allow Anonymous",
                "description": "Allow anonymous access",
                "default": True,
            },
            {
                "name": "username",
                "type": "string",
                "label": "Username",
                "description": "Username for authentication",
                "default": "",
            },
            {
                "name": "password",
                "type": "string",
                "label": "Password",
                "description": "Password for authentication",
                "secret": True,
                "default": "",
            },
        ],
    }

    def __init__(self):
        super().__init__()
        self._server = None
        self._nodes: dict[str, OpcUaNode] = {}
        self._subscriptions: dict[str, OpcUaSubscription] = {}
        self._values_lock = threading.Lock()
        self._data_callback: Callable | None = None
        self._server_task: asyncio.Task | None = None
        self._running = False
        self._config: dict = {}
        self._namespace_idx: int = 2  # 0=OPC, 1=Local, 2+=Custom
        self._sub_counter: int = 0
        self._node_counter: int = 0

    async def start(self, config: dict) -> None:
        """启动 OPC UA 服务器"""
        self._config = config
        self._running = True

        try:
            from asyncua import Server, ua
            from asyncua.server.users import User, UserRole
        except ImportError:
            raise ImportError("asyncua未安装，请执行: pip install asyncua>=1.1.0") from None

        try:
            self._server = Server()

            # 服务器基本配置
            server_name = config.get("server_name", "EdgeLite Gateway")
            await self._server.init()
            self._server.set_server_name(server_name)

            # 注册自定义命名空间
            namespace = config.get("namespace", "http://edgelite.io/nodes")
            self._namespace_idx = await self._server.register_namespace(namespace)

            # TLS 配置
            if config.get("enable_tls", False):
                cert_path = config.get("cert_path", "")
                key_path = config.get("key_path", "")
                if cert_path and key_path:
                    # FIXED-P1: load_certificate/load_private_key 是协程, 必须 await,
                    # 否则 TLS 证书/私钥未真正加载, 加密形同虚设
                    await self._server.load_certificate(cert_path)
                    await self._server.load_private_key(key_path)
                    logger.info("OPC UA Server TLS enabled")

            # 用户认证配置
            if not config.get("allow_anonymous", True):
                username = config.get("username", "")
                password = config.get("password", "")
                if username and password:

                    class CustomUserManager:
                        def get_user(self, iserver, username: str, password: str) -> User:
                            if username == self.username and password == self.password:
                                return User(role=UserRole.Admin)
                            # FIXED-P1: 认证失败必须返回 None 拒绝连接,
                            # 原实现降级为匿名用户会导致认证绕过
                            return None

                    user_manager = CustomUserManager()
                    user_manager.username = username
                    user_manager.password = password
                    self._server.user_manager = user_manager
                    logger.info("OPC UA Server authentication enabled")

            # 节点访问回调
            self._server.set_attribute_value_callback(self._node_value_callback)

            # 启动服务器
            host = config.get("host", "0.0.0.0")
            port = int(config.get("port", 4840))
            # FIXED-P1: set_endpoint 必须在 start 之前调用,
            # 否则服务器使用默认端点, 自定义 host/port 配置失效
            endpoint_url = f"opc.tcp://{host}:{port}"
            self._server.set_endpoint(endpoint_url)
            await self._server.start()
            logger.info(
                "OPC UA Server started: opc.tcp://%s:%d",
                host if host != "0.0.0.0" else "localhost",
                port,
            )

        except Exception as e:
            logger.error("OPC UA Server start failed: %s", e)
            self._running = False
            raise

    async def stop(self) -> None:
        """停止 OPC UA 服务器"""
        self._running = False

        if self._server:
            try:
                await self._server.stop()
                logger.info("OPC UA Server stopped")
            except Exception as e:
                logger.warning("OPC UA Server stop error: %s", e)
            self._server = None

        self._nodes.clear()
        self._subscriptions.clear()

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加设备并注册其所有测点为 OPC UA 节点"""
        if not self._server:
            raise RuntimeError("OPC UA Server not started")

        points = points or []
        added_count = 0

        for point in points:
            node_id = point.get("name", "")
            if not node_id:
                continue

            display_name = point.get("display_name") or point.get("label") or node_id
            data_type = point.get("data_type", "Float")
            writable = point.get("writable", False)
            description = point.get("description", f"Device {device_id} - {node_id}")

            # 转换为 OPC UA 数据类型
            opcua_dtype = self._to_opcua_type(data_type)

            try:
                await self._register_node(
                    node_id=node_id,
                    display_name=display_name,
                    data_type=opcua_dtype,
                    writable=writable,
                    description=description,
                    initial_value=point.get("value"),
                )
                added_count += 1
            except Exception as e:
                logger.warning(
                    "Failed to register node %s for device %s: %s",
                    node_id,
                    device_id,
                    e,
                )

        logger.info(
            "OPC UA Server: registered %d nodes for device %s",
            added_count,
            device_id,
        )

    async def _register_node(
        self,
        node_id: str,
        display_name: str,
        data_type: str = "Float",
        writable: bool = False,
        description: str = "",
        initial_value: Any = None,
    ) -> str:
        """注册单个 OPC UA 节点"""
        if not self._server:
            raise RuntimeError("OPC UA Server not started")

        from asyncua import ua

        # 构建节点 ID
        full_node_id = f"{node_id}"
        node_idx = self._namespace_idx

        try:
            # 创建对象节点
            self._server.get_namespace_array()[node_idx - 1] if node_idx > 0 else ""
            idx = node_idx

            # 获取 Objects 节点

            # 创建可变变量
            var = await self._server.nodes.objects.add_variable(
                nodeid=f"ns={idx};s={full_node_id}",
                bname=display_name,
                val=initial_value if initial_value is not None else 0.0,
                datatype=getattr(ua.ObjectIds, data_type, ua.ObjectIds.Float),
            )

            # 设置可写属性
            await var.set_writable(writable)

            # 保存节点信息
            with self._values_lock:
                self._nodes[node_id] = OpcUaNode(
                    node_id=node_id,
                    display_name=display_name,
                    data_type=data_type,
                    value=initial_value if initial_value is not None else 0.0,
                    timestamp=datetime.now(UTC),
                    writable=writable,
                    description=description,
                )

            logger.debug("Registered OPC UA node: %s", node_id)
            return node_id

        except Exception as e:
            logger.error("Failed to register node %s: %s", node_id, e)
            raise

    async def _node_value_callback(self, node, attr, value):
        """节点值变更回调"""
        pass

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值"""
        result = {}
        with self._values_lock:
            for point_name in points:
                node = self._nodes.get(point_name)
                if node:
                    result[point_name] = node.value
                else:
                    result[point_name] = None
        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""
        if not self._server:
            return False

        with self._values_lock:
            node = self._nodes.get(point)
            if not node:
                logger.warning("OPC UA node not found: %s", point)
                return False

            if not node.writable:
                logger.warning("OPC UA node not writable: %s", point)
                return False

            # 尝试转换类型
            try:
                if node.data_type == "Float":
                    value = float(value)
                elif node.data_type == "Int32":
                    value = int(value)
                elif node.data_type == "Boolean":
                    value = bool(value)
            except (ValueError, TypeError) as e:
                logger.error("Type conversion failed for %s: %s", point, e)
                return False

            # 更新值
            node.value = value
            node.timestamp = datetime.now(UTC)
            node.quality = "good"

            # 更新 OPC UA 服务器中的变量
            try:
                from asyncua import ua

                self._server.get_namespace_array()[self._namespace_idx - 1] if self._namespace_idx > 0 else ""
                var = self._server.get_node(f"ns={self._namespace_idx};s={point}")
                dv = ua.DataValue(ua.Variant(value, getattr(ua.VariantType, node.data_type, ua.VariantType.Float)))
                dv.ServerTimestamp = datetime.now(UTC)
                dv.SourceTimestamp = datetime.now(UTC)
                await var.set_value(dv)
                return True
            except Exception as e:
                logger.error("OPC UA write failed for %s: %s", point, e)
                return False

    async def write_points_batch(self, device_id: str, points: dict[str, Any]) -> dict[str, bool]:
        """批量写入多个测点"""
        results = {}
        for point_name, value in points.items():
            results[point_name] = await self.write_point(device_id, point_name, value)
        return results

    async def update_point_value(self, point: str, value: Any, quality: str = "good") -> bool:
        """更新测点值（从内部数据源同步）

        这是 OPC UA Server 的核心方法，用于从南向驱动接收数据并更新到 OPC UA 节点。

        Args:
            point: 测点名（节点 ID）
            value: 新的值
            quality: 数据质量 (good/suspect/invalid)

        Returns:
            True 表示更新成功
        """
        if not self._server:
            return False

        with self._values_lock:
            node = self._nodes.get(point)
            if not node:
                logger.debug("OPC UA node not registered, auto-creating: %s", point)
                try:
                    await self._register_node(node_id=point, display_name=point)
                    node = self._nodes.get(point)
                except Exception:
                    return False

            if node is None:
                return False

            # 转换类型
            try:
                if node.data_type == "Float":
                    value = float(value)
                elif node.data_type == "Int32":
                    value = int(value)
                elif node.data_type == "Boolean":
                    value = bool(value)
            except (ValueError, TypeError):
                return False

            node.value = value
            node.quality = quality
            node.timestamp = datetime.now(UTC)

        # 更新 OPC UA 服务器中的变量
        try:
            from asyncua import ua

            var = self._server.get_node(f"ns={self._namespace_idx};s={point}")
            dtype = getattr(ua.VariantType, node.data_type, ua.VariantType.Float)
            dv = ua.DataValue(ua.Variant(value, dtype))
            dv.ServerTimestamp = datetime.now(UTC)
            dv.SourceTimestamp = datetime.now(UTC)
            dv.Quality = getattr(ua.StatusCode, quality.title(), ua.StatusCode.Good)
            await var.set_value(dv)

            # 触发订阅回调
            self._notify_subscriptions(point, value, quality)

            logger.debug(
                "OPC UA node updated: %s = %s (quality=%s)",
                point,
                value,
                quality,
            )
            return True
        except Exception as e:
            logger.error("OPC UA update failed for %s: %s", point, e)
            return False

    def _notify_subscriptions(self, node_id: str, value: Any, quality: str) -> None:
        """通知订阅者数据变化"""
        for sub in self._subscriptions.values():
            if node_id in sub.node_ids and sub.callback:
                try:
                    if asyncio.iscoroutinefunction(sub.callback):
                        asyncio.create_task(sub.callback(node_id, value, quality))
                    else:
                        sub.callback(node_id, value, quality)
                except Exception as e:
                    logger.warning("Subscription callback error: %s", e)

    def on_data(self, callback: Callable) -> None:
        """注册数据回调"""
        self._data_callback = callback

    async def subscribe(
        self,
        node_ids: list[str],
        callback: Callable,
        sampling_interval: float = 500.0,
    ) -> str:
        """创建数据订阅

        Args:
            node_ids: 要订阅的节点 ID 列表
            callback: 数据变化回调函数 (node_id, value, quality)
            sampling_interval: 采样间隔 (毫秒)

        Returns:
            subscription_id: 订阅 ID
        """
        self._sub_counter += 1
        sub_id = f"sub_{self._sub_counter}"

        subscription = OpcUaSubscription(
            subscription_id=sub_id,
            node_ids=node_ids,
            callback=callback,
            sampling_interval=sampling_interval,
        )
        self._subscriptions[sub_id] = subscription

        logger.info(
            "OPC UA subscription created: %s for %d nodes",
            sub_id,
            len(node_ids),
        )
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        """取消订阅"""
        if subscription_id in self._subscriptions:
            del self._subscriptions[subscription_id]
            logger.info("OPC UA subscription removed: %s", subscription_id)
            return True
        return False

    def get_nodes(self) -> list[dict]:
        """获取所有注册的节点"""
        with self._values_lock:
            return [node.to_dict() for node in self._nodes.values()]

    def get_subscriptions(self) -> list[dict]:
        """获取所有订阅"""
        return [
            {
                "subscription_id": sub.subscription_id,
                "node_ids": sub.node_ids,
                "sampling_interval": sub.sampling_interval,
            }
            for sub in self._subscriptions.values()
        ]

    def is_device_connected(self, device_id: str) -> bool:
        """检查服务器是否运行"""
        return self._running and self._server is not None

    @staticmethod
    def _to_opcua_type(data_type: str) -> str:
        """将通用数据类型转换为 OPC UA 数据类型"""
        mapping = {
            "float32": "Float",
            "float64": "Double",
            "int16": "Int16",
            "int32": "Int32",
            "int64": "Int64",
            "uint16": "UInt16",
            "uint32": "UInt32",
            "uint64": "UInt64",
            "bool": "Boolean",
            "string": "String",
            "datetime": "DateTime",
            "byte": "Byte",
        }
        return mapping.get(data_type.lower(), "Float")

    async def discover_devices(self, config: dict) -> list[dict]:
        """OPC UA Server 不支持设备发现"""
        return []

    async def remove_device(self, device_id: str) -> None:
        """移除设备（注销其所有节点）"""
        # 查找属于该设备的所有节点并移除
        nodes_to_remove = [node_id for node_id, node in list(self._nodes.items()) if device_id in node.description]
        for node_id in nodes_to_remove:
            try:
                await self._unregister_node(node_id)
            except Exception as e:
                logger.warning("Failed to unregister node %s: %s", node_id, e)

    async def _unregister_node(self, node_id: str) -> None:
        """注销 OPC UA 节点"""
        if not self._server:
            return

        try:
            node = self._server.get_node(f"ns={self._namespace_idx};s={node_id}")
            await node.delete()
            with self._values_lock:
                self._nodes.pop(node_id, None)
            logger.debug("Unregistered OPC UA node: %s", node_id)
        except Exception as e:
            logger.warning("Failed to unregister node %s: %s", node_id, e)
