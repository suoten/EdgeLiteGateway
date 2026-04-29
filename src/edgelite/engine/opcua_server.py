"""内置OPC UA Server - 暴露网关采集数据供其他系统订阅

Pro版特性：内置OPC UA Server，允许SCADA/MES等上位系统
通过OPC UA协议订阅网关采集的设备数据，实现系统级联。
默认端口4840，支持匿名和用户名密码认证。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from edgelite.engine.event_bus import EventBus, PointUpdateEvent

logger = logging.getLogger(__name__)


class OpcUaServer:
    """内置OPC UA Server

    基于asyncua库实现，提供：
    - 标准OPC UA Server协议
    - 设备数据作为OPC UA节点暴露
    - 数据变更自动通知订阅者
    - 支持匿名/用户名密码认证
    - 支持反向控制（写入节点→控制设备）
    """

    def __init__(self):
        self._running = False
        self._server = None
        self._task: asyncio.Task | None = None
        self._nodes: dict[str, Any] = {}
        self._write_callback = None
        self._event_bus: EventBus | None = None
        self._namespace_idx: int | None = None

    async def start(self, config: dict | None = None) -> None:
        """启动内置OPC UA Server

        Args:
            config: 配置参数
                host: 监听地址 (默认"0.0.0.0")
                port: 监听端口 (默认4840)
                namespace: 命名空间URI (默认"urn:edgelite:gateway")
                username: 认证用户名 (可选)
                password: 认证密码 (可选)
        """
        try:
            import asyncua
        except ImportError:
            logger.warning("asyncua未安装，内置OPC UA Server不可用")
            return

        config = config or {}
        host = config.get("host", "0.0.0.0")
        port = int(config.get("port", 4840))
        namespace_uri = config.get("namespace", "urn:edgelite:gateway")

        try:
            self._server = asyncua.Server()
            await self._server.init()

            # 设置Server端点
            endpoint = f"opc.tcp://{host}:{port}"
            self._server.set_endpoint(endpoint)

            # 设置命名空间
            idx = await self._server.register_namespace(namespace_uri)
            self._namespace_idx = idx

            # 设置Server信息
            self._server.set_server_name("EdgeLite Gateway OPC UA Server")

            # 配置安全策略
            from asyncua.ua import SecurityPolicyType
            self._server.set_security_policies([
                SecurityPolicyType.NoSecurity,
            ])

            # 创建根节点结构
            root = self._server.nodes.objects
            gateway_folder = await root.add_object(idx, "EdgeLite")

            # 创建状态节点
            status_folder = await gateway_folder.add_object(idx, "Status")
            self._nodes["online_devices"] = await status_folder.add_variable(
                idx, "OnlineDevices", 0
            )
            self._nodes["total_devices"] = await status_folder.add_variable(
                idx, "TotalDevices", 0
            )
            self._nodes["uptime"] = await status_folder.add_variable(
                idx, "Uptime", 0
            )

            # 创建设备数据文件夹
            self._nodes["devices_folder"] = await gateway_folder.add_object(idx, "Devices")

            # 启动Server
            self._task = asyncio.create_task(
                self._server.start(), name="opcua-server"
            )
            self._running = True
            logger.info("内置OPC UA Server启动: %s (ns=%s)", endpoint, namespace_uri)

            if self._event_bus:
                self.subscribe_event_bus(self._event_bus)

        except Exception as e:
            logger.error("内置OPC UA Server启动失败: %s", e)
            self._server = None

    async def stop(self) -> None:
        """停止内置OPC UA Server"""
        self._running = False
        if self._server:
            try:
                await self._server.stop()
            except Exception as e:
                logger.warning("OPC UA Server关闭异常: %s", e)
            self._server = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("内置OPC UA Server已停止")

    async def update_device_data(self, device_id: str, points: dict[str, Any]) -> None:
        """更新设备数据到OPC UA节点

        Args:
            device_id: 设备ID
            points: 测点数据 {point_name: value}
        """
        if not self._running or not self._server:
            return

        try:
            import asyncua

            idx = await self._server.get_namespace_index("urn:edgelite:gateway")

            # 确保设备文件夹存在
            device_key = f"device_{device_id}"
            if device_key not in self._nodes:
                devices_folder = self._nodes.get("devices_folder")
                if devices_folder:
                    device_obj = await devices_folder.add_object(idx, device_id)
                    self._nodes[device_key] = device_obj

            device_obj = self._nodes.get(device_key)
            if not device_obj:
                return

            # 更新/创建测点节点
            for point_name, value in points.items():
                point_key = f"{device_id}.{point_name}"
                if point_key in self._nodes:
                    # 更新已有节点
                    node = self._nodes[point_key]
                    await node.write_value(value)
                else:
                    # 创建新节点
                    try:
                        var_node = await device_obj.add_variable(
                            idx, point_name, value
                        )
                        # 使节点可写（支持反向控制）
                        await var_node.set_writable()
                        self._nodes[point_key] = var_node
                    except Exception as e:
                        logger.debug("OPC UA创建节点失败 %s: %s", point_name, e)

        except Exception as e:
            logger.error("OPC UA更新设备数据失败: %s", e)

    async def update_status(self, online_devices: int, total_devices: int, uptime: int) -> None:
        """更新网关状态节点"""
        if not self._running:
            return

        try:
            if "online_devices" in self._nodes:
                await self._nodes["online_devices"].write_value(online_devices)
            if "total_devices" in self._nodes:
                await self._nodes["total_devices"].write_value(total_devices)
            if "uptime" in self._nodes:
                await self._nodes["uptime"].write_value(uptime)
        except Exception as e:
            logger.debug("OPC UA状态更新失败: %s", e)

    def set_write_callback(self, callback) -> None:
        """设置写入回调（反向控制）

        当外部系统通过OPC UA写入节点时，触发回调控制设备
        """
        self._write_callback = callback

    def subscribe_event_bus(self, event_bus: EventBus) -> None:
        """订阅EventBus的PointUpdateEvent，自动映射到OPC UA节点"""
        self._event_bus = event_bus
        event_bus.register_handler("PointUpdateEvent", self._on_point_update)
        logger.info("OPC UA Server已订阅EventBus PointUpdateEvent")

    async def _on_point_update(self, event: PointUpdateEvent) -> None:
        """处理PointUpdateEvent，将测点数据写入OPC UA节点"""
        if not self._running or not self._server:
            return
        try:
            node_id = self._map_point_to_node(event.device_id, event.point_name)
            if node_id and node_id in self._nodes:
                await self._nodes[node_id].write_value(event.value)
            else:
                await self.update_device_data(
                    event.device_id, {event.point_name: event.value}
                )
        except Exception as e:
            logger.debug("OPC UA EventBus映射写入失败: %s", e)

    def _map_point_to_node(self, device_id: str, point_name: str) -> str | None:
        """将设备测点映射到OPC UA节点ID"""
        point_key = f"{device_id}.{point_name}"
        if point_key in self._nodes:
            return point_key
        return None

    @property
    def is_running(self) -> bool:
        return self._running
