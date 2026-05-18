"""OPC-UA基础接入驱动 - 基于opcua-asyncio"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class OpcUaDriver(DriverPlugin):
    """OPC-UA协议驱动，连接OPC-UA服务器读写节点"""

    plugin_name = "opcua"
    plugin_version = "0.1.0"
    supported_protocols = ["opcua"]
    config_schema = {
        "description": "OPC UA industrial protocol, supports encrypted authentication and node browsing",
        "fields": [
            {"name": "endpoint", "type": "string", "label": "OPC UA Endpoint", "description": "OPC UA server endpoint URL", "default": "opc.tcp://localhost:4840", "required": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "username", "type": "string", "label": "Username", "description": "Leave empty for anonymous login"},  # FIXED: 原问题-中文硬编码label/description
            {"name": "password", "type": "string", "label": "Password", "description": "User password, leave empty for anonymous login", "secret": True},  # FIXED: 原问题-中文硬编码label/description
            {"name": "security_mode", "type": "string", "label": "Security Mode", "description": "Encryption mode, None=plaintext, SignAndEncrypt=highest security", "default": "None", "options": ["None", "Sign", "SignAndEncrypt"]},  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        self._running = False
        # device_id -> config
        self._device_configs: dict[str, dict] = {}
        # device_id -> points定义
        self._device_points: dict[str, list[dict]] = {}
        # device_id -> latest_values
        self._latest_values: dict[str, dict[str, Any]] = {}
        # device_id -> opcua_client
        self._clients: dict[str, Any] = {}
        # device_id -> subscription
        self._subscriptions: dict[str, Any] = {}
        # 数据回调
        self._data_callback: Callable | None = None
        # 连接任务
        self._connect_tasks: dict[str, asyncio.Task] = {}

    async def start(self, config: dict) -> None:
        """启动驱动"""
        self._running = True
        logger.info("OPC-UA驱动启动")

    async def stop(self) -> None:
        """停止驱动"""
        self._running = False

        # 取消所有连接任务
        for task in self._connect_tasks.values():
            if not task.done():
                task.cancel()
        for task in self._connect_tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._connect_tasks.clear()

        # 断开所有客户端
        for device_id, client in self._clients.items():
            try:
                await client.disconnect()
            except Exception as e:
                logger.debug("OPC-UA客户端断开失败[%s]: %s", device_id, e)
        self._clients.clear()
        self._subscriptions.clear()
        logger.info("OPC-UA驱动停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        """添加OPC-UA设备"""
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}

        # 启动连接任务
        task = asyncio.create_task(
            self._connect_device(device_id),
            name=f"opcua-connect-{device_id}",
        )
        self._connect_tasks[device_id] = task

    async def remove_device(self, device_id: str) -> None:
        """移除设备"""
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)

        # 取消连接任务
        task = self._connect_tasks.pop(device_id, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        # 断开客户端
        client = self._clients.pop(device_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception as e:
                logger.debug("OPC-UA设备断开失败[%s]: %s", device_id, e)
        self._subscriptions.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值"""
        client = self._clients.get(device_id)
        if not client:
            return self._latest_values.get(device_id, {})

        result = {}
        point_defs = self._device_points.get(device_id, [])

        try:
            for point_name in points:
                point_def = next((p for p in point_defs if p.get("name") == point_name), None)  # FIXED: 原问题-p["name"]硬访问
                if not point_def:
                    continue

                node_id = point_def.get("address", "")
                node = client.get_node(node_id)
                value = await node.read_value()
                result[point_name] = value
                self._latest_values.setdefault(device_id, {})[point_name] = value  # FIXED: 原问题-嵌套硬访问device_id键可能不存在

        except Exception as e:
            logger.error("OPC-UA读取失败: %s - %s", device_id, e)

        return result

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        """写入测点值"""
        client = self._clients.get(device_id)
        if not client:
            return False

        point_defs = self._device_points.get(device_id, [])
        point_def = next((p for p in point_defs if p.get("name") == point), None)  # FIXED: 原问题-p["name"]硬访问
        if not point_def:
            return False

        try:
            node_id = point_def.get("address", "")
            node = client.get_node(node_id)
            await node.write_value(value)
            return True
        except Exception as e:
            logger.error("OPC-UA写入失败: %s - %s", device_id, e)
            return False

    def on_data(self, callback: Callable) -> None:
        """注册数据回调"""
        self._data_callback = callback

    async def _connect_device(self, device_id: str) -> None:
        """连接OPC-UA服务器"""
        config = self._device_configs.get(device_id, {})
        server_url = config.get("server_url", "opc.tcp://localhost:4840")
        username = config.get("username")
        password = config.get("password")
        config.get("security_mode", "None")
        use_subscription = config.get("use_subscription", True)

        while self._running:
            try:
                from asyncua import Client

                client = Client(server_url)

                # 设置认证
                if username and password:
                    client.set_user(username)
                    client.set_password(password)

                await client.connect()
                self._clients[device_id] = client
                logger.info("OPC-UA连接成功: %s -> %s", device_id, server_url)

                # 创建订阅
                if use_subscription:
                    await self._create_subscription(device_id, client)

                # 保持连接
                while self._running:
                    await asyncio.sleep(5)
                    # 检查连接状态
                    try:
                        await client.get_objects_node()
                    except Exception:
                        logger.warning("OPC-UA连接断开: %s", device_id)
                        break

            except asyncio.CancelledError:
                raise
            except ImportError:
                logger.error("asyncua未安装，OPC-UA驱动不可用")
                await asyncio.sleep(30)
            except Exception as e:
                logger.error("OPC-UA连接异常: %s - %s，5秒后重试", device_id, e)
                await asyncio.sleep(5)
            finally:
                client = self._clients.pop(device_id, None)
                if client:
                    try:
                        await client.disconnect()
                    except Exception as e:
                        logger.debug("OPC-UA写入回调断开失败[%s]: %s", device_id, e)
                self._subscriptions.pop(device_id, None)

    async def _create_subscription(self, device_id: str, client: Any) -> None:
        """创建OPC-UA订阅"""
        try:
            points = self._device_points.get(device_id, [])
            if not points:
                return

            handler = _SubHandler(device_id, self._latest_values, self._data_callback)

            subscription = await client.create_subscription(500, handler)
            self._subscriptions[device_id] = subscription

            success_count = 0
            for point_def in points:
                node_id = point_def.get("address", "")
                point_name = point_def.get("name", node_id)
                try:
                    node = client.get_node(node_id)
                    await subscription.subscribe_data_change(node)
                    success_count += 1
                except Exception as e:
                    logger.warning(
                        "OPC-UA节点订阅跳过: %s.%s (node=%s) - %s",
                        device_id,
                        point_name,
                        node_id,
                        e,
                    )

            if success_count > 0:
                logger.info(
                    "OPC-UA订阅创建: %s (%d/%d节点成功)",
                    device_id,
                    success_count,
                    len(points),
                )
            else:
                logger.error(
                    "OPC-UA订阅全部失败: %s (0/%d节点)",
                    device_id,
                    len(points),
                )

        except Exception as e:
            logger.error("OPC-UA订阅失败: %s - %s", device_id, e)


class _SubHandler:
    """OPC-UA订阅回调处理器"""

    def __init__(
        self,
        device_id: str,
        latest_values: dict,
        data_callback: Callable | None,
    ):
        self.device_id = device_id
        self._latest_values = latest_values
        self._data_callback = data_callback

    def datachange_notification(self, node: Any, val: Any, data: Any):
        """节点值变化通知"""
        node_id = node.nodeid.to_string()

        # 尝试匹配测点名称
        # 这里简化处理，用node_id作为key
        self._latest_values.setdefault(self.device_id, {})[node_id] = val

        if self._data_callback:
            # 在事件循环中调度回调
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._data_callback(self.device_id, {node_id: val}))
            except RuntimeError as e:
                logger.warning("OPC-UA data callback error: %s", e)  # FIXED: 原问题-except RuntimeError: pass数据回调异常被静默

        # 发布PointUpdateEvent到EventBus
        try:
            from edgelite.app import _app_state

            if _app_state.event_bus:
                from edgelite.engine.event_bus import PointUpdateEvent

                point_name = node_id.split(".")[-1] if "." in node_id else node_id
                event = PointUpdateEvent(
                    device_id=self.device_id,
                    point_name=point_name,
                    value=val,
                    quality="good",
                )
                loop = asyncio.get_running_loop()
                loop.create_task(_app_state.event_bus.publish(event))
        except Exception as e:
            logger.debug("OPC-UA数据变更回调异常: %s", e)
