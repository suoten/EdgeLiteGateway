"""OPC-UA基础接入驱动 - 基于opcua-asyncio"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
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
            {"name": "security_mode", "type": "string", "label": "Security Mode", "description": "Encryption mode, None=plaintext, SignAndEncrypt=highest security", "default": "None", "options": ["None", "Sign", "SignAndEncrypt"]},
            {"name": "client_cert_path", "type": "string", "label": "Client Cert Path", "description": "Path to client certificate file (PEM/DER)", "default": ""},
            {"name": "client_key_path", "type": "string", "label": "Client Key Path", "description": "Path to client private key file (PEM/DER)", "default": ""},
            {"name": "ca_cert_path", "type": "string", "label": "CA Cert Path", "description": "Path to CA certificate file (PEM/DER)", "default": ""},
            {"name": "session_timeout", "type": "integer", "label": "Session Timeout (ms)", "description": "OPC UA session timeout in milliseconds", "default": 60000},
            {"name": "subscription_interval", "type": "integer", "label": "Subscription Interval (ms)", "description": "Subscription publishing interval in milliseconds", "default": 500},
            {"name": "deadband_type", "type": "string", "label": "Deadband Type", "description": "Deadband filter type: None=no filter, Absolute=absolute change, Percent=percent of range", "default": "None", "options": ["None", "Absolute", "Percent"]},
            {"name": "deadband_value", "type": "number", "label": "Deadband Value", "description": "Deadband threshold value (0 to disable)", "default": 0},
            {"name": "use_subscription", "type": "boolean", "label": "Use Subscription", "description": "Enable subscription mode for data change notifications", "default": True},
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
        self._connect_fail_count: dict[str, int] = {}
        self._last_connect_log: dict[str, float] = {}
        self._values_lock = threading.Lock()

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
                logger.debug("[opcua] device=%s code=DISCONNECT_FAILED msg=%s", device_id, e)
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
                logger.debug("[opcua] device=%s code=DISCONNECT_FAILED msg=%s", device_id, e)
        self._subscriptions.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        """读取测点值"""
        client = self._clients.get(device_id)
        if not client:
            with self._values_lock:
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
                with self._values_lock:
                    self._latest_values.setdefault(device_id, {})[point_name] = value  # FIXED: 原问题-嵌套硬访问device_id键可能不存在

        except Exception as e:
            self._log_error(device_id, "READ_ERROR", f"msg={e}")

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
            self._log_error(device_id, "WRITE_ERROR", f"msg={e}")
            return False

    def on_data(self, callback: Callable) -> None:
        """注册数据回调"""
        self._data_callback = callback

    def _log_error(self, device_id: str, error_code: str, message: str, level: int = logging.ERROR) -> None:
        """统一日志四元组格式: [opcua] device={device_id} code={error_code} {message}"""
        logger.log(level, "[opcua] device=%s code=%s %s", device_id, error_code, message)

    def _check_cert_expiry(self, cert_path: str, cert_type: str) -> bool:
        """检查证书过期时间，返回True表示证书有效（或无法检查）"""
        if not cert_path:
            return True
        try:
            from cryptography import x509
            from cryptography.hazmat.backends import default_backend
            import datetime

            with open(cert_path, "rb") as f:
                cert = x509.load_pem_x509_certificate(f.read(), default_backend())

            now = datetime.datetime.utcnow()
            if hasattr(cert, "not_valid_after_utc"):
                expires = cert.not_valid_after_utc.replace(tzinfo=None)
            else:
                expires = cert.not_valid_after

            if now > expires:
                logger.error("[opcua] code=CERT_EXPIRED msg=%s certificate expired on %s", cert_type, expires)
                return False

            days_left = (expires - now).days
            if days_left <= 30:
                logger.warning("[opcua] code=CERT_EXPIRING msg=%s certificate expires in %d days (%s)", cert_type, days_left, expires)

            return True
        except ImportError:
            logger.debug("[opcua] code=CERT_CHECK_FAILED msg=cryptography library not installed, skipping cert expiry check")
            return True
        except Exception as e:
            logger.warning("[opcua] code=CERT_CHECK_FAILED msg=Failed to check %s certificate: %s", cert_type, e)
            return True

    async def _connect_device(self, device_id: str) -> None:
        """连接OPC-UA服务器"""
        config = self._device_configs.get(device_id, {})
        server_url = config.get("endpoint") or config.get("server_url", "opc.tcp://localhost:4840")
        username = config.get("username")
        password = config.get("password")
        security_mode_str = config.get("security_mode", "None")
        client_cert_path = config.get("client_cert_path", "")
        client_key_path = config.get("client_key_path", "")
        ca_cert_path = config.get("ca_cert_path", "")
        use_subscription = config.get("use_subscription", True)
        session_timeout = int(config.get("session_timeout", 60000))

        _SECURITY_MODE_MAP = {
            "None": 1,
            "Sign": 2,
            "SignAndEncrypt": 3,
        }

        while self._running:
            try:
                from asyncua import Client

                # 证书过期检查
                if not self._check_cert_expiry(client_cert_path, "Client"):
                    self._log_error(device_id, "CERT_EXPIRED", "msg=Client certificate expired, refusing connection")
                    await asyncio.sleep(60)
                    continue
                if not self._check_cert_expiry(ca_cert_path, "CA"):
                    self._log_error(device_id, "CERT_EXPIRED", "msg=CA certificate expired, refusing connection")
                    await asyncio.sleep(60)
                    continue

                client = Client(server_url)
                client.session_timeout = session_timeout

                if username and password:
                    client.set_user(username)
                    client.set_password(password)

                security_mode_val = _SECURITY_MODE_MAP.get(security_mode_str, 1)
                client.security_mode = security_mode_val

                if client_cert_path and client_key_path:
                    client.certificate = client_cert_path
                    client.private_key = client_key_path
                if ca_cert_path:
                    client.server_certificate = ca_cert_path

                self._log_error(
                    device_id, "CONN_OK",
                    f"msg=Connecting endpoint={server_url} security={security_mode_str} session_timeout={session_timeout}",
                    level=logging.INFO,
                )

                await client.connect()
                self._clients[device_id] = client
                self._connect_fail_count.pop(device_id, None)
                self._log_error(
                    device_id, "CONN_OK",
                    f"msg=Connected successfully endpoint={server_url} security={security_mode_str}",
                    level=logging.INFO,
                )

                # 创建订阅
                if use_subscription:
                    await self._create_subscription(device_id, client, config)

                # 保持连接 + 会话保活
                while self._running:
                    await asyncio.sleep(5)
                    try:
                        # 检查会话状态
                        state = client.session_state
                        if state != 1:  # 1 = Connected
                            self._log_error(device_id, "SESSION_EXPIRED", f"msg=Session state={state}, reconnecting", level=logging.WARNING)
                            break
                        # 保活：读取服务器状态
                        await client.get_objects_node()
                    except Exception:
                        self._log_error(device_id, "SESSION_EXPIRED", "msg=Keep-alive failed, reconnecting", level=logging.WARNING)
                        break

            except asyncio.CancelledError:
                raise
            except ImportError:
                self._log_error(device_id, "CONN_FAILED", "msg=asyncua library not installed")
                await asyncio.sleep(30)
            except Exception as e:
                self._connect_fail_count[device_id] = self._connect_fail_count.get(device_id, 0) + 1
                fails = self._connect_fail_count[device_id]
                delay = min(5 * (2 ** min(fails - 1, 5)), 300)
                now = time.monotonic()
                last_log = self._last_connect_log.get(device_id, 0.0)
                if fails <= 3 or now - last_log >= 60:
                    self._log_error(
                        device_id, "CONN_FAILED",
                        f"msg={e}, retrying in {delay}s (attempt #{fails})",
                        level=logging.WARNING if fails > 3 else logging.ERROR,
                    )
                    self._last_connect_log[device_id] = now
                await asyncio.sleep(delay)
            finally:
                client = self._clients.pop(device_id, None)
                if client:
                    try:
                        await client.disconnect()
                    except Exception as e:
                        logger.debug("[opcua] device=%s code=DISCONNECT_FAILED msg=%s", device_id, e)
                self._subscriptions.pop(device_id, None)

    async def _create_subscription(self, device_id: str, client: Any, config: dict) -> None:
        """创建OPC-UA订阅"""
        try:
            points = self._device_points.get(device_id, [])
            if not points:
                return

            interval = int(config.get("subscription_interval", 500))
            deadband_type_str = config.get("deadband_type", "None")
            deadband_value = float(config.get("deadband_value", 0))

            handler = _SubHandler(device_id, self._latest_values, self._data_callback, self._values_lock)

            subscription = await client.create_subscription(interval, handler)
            self._subscriptions[device_id] = subscription

            use_deadband = deadband_type_str != "None" and deadband_value > 0

            success_count = 0
            for point_def in points:
                node_id = point_def.get("address", "")
                point_name = point_def.get("name", node_id)
                try:
                    node = client.get_node(node_id)
                    if use_deadband:
                        from asyncua.common.subscription import DeadbandType
                        db_type = DeadbandType.Absolute if deadband_type_str == "Absolute" else DeadbandType.Percent
                        await subscription.subscribe_data_change(
                            node,
                            deadband_type=db_type,
                            deadband_value=deadband_value,
                        )
                    else:
                        await subscription.subscribe_data_change(node)
                    success_count += 1
                except Exception as e:
                    self._log_error(
                        device_id, "SUBSCRIPTION_FAILED",
                        f"msg=Node subscribe skipped point={point_name} node={node_id} err={e}",
                        level=logging.WARNING,
                    )

            if success_count > 0:
                self._log_error(
                    device_id, "SUBSCRIPTION_OK",
                    f"msg=Subscription created interval={interval}ms deadband={deadband_type_str}/{deadband_value} nodes={success_count}/{len(points)}",
                    level=logging.INFO,
                )
            else:
                self._log_error(
                    device_id, "SUBSCRIPTION_FAILED",
                    f"msg=All node subscriptions failed nodes=0/{len(points)}",
                )

        except Exception as e:
            self._log_error(device_id, "SUBSCRIPTION_FAILED", f"msg={e}")


class _SubHandler:
    """OPC-UA订阅回调处理器"""

    def __init__(
        self,
        device_id: str,
        latest_values: dict,
        data_callback: Callable | None,
        values_lock: threading.Lock,
    ):
        self.device_id = device_id
        self._latest_values = latest_values
        self._data_callback = data_callback
        self._values_lock = values_lock

    def datachange_notification(self, node: Any, val: Any, data: Any):
        """节点值变化通知（在OPC-UA库线程中被调用，非asyncio上下文）

        FIXED: P1-3 原datachange_notification在OPC-UA库线程中执行，
        asyncio.get_running_loop()抛出RuntimeError导致回调被静默丢弃。
        修复：通过call_soon_threadsafe安全调度到asyncio事件循环。
        """
        node_id = node.nodeid.to_string()
        with self._values_lock:
            self._latest_values.setdefault(self.device_id, {})[node_id] = val

        try:
            loop = asyncio.get_event_loop()
        except Exception:
            return

        def _do_callback():
            if self._data_callback:
                asyncio.create_task(
                    self._data_callback(self.device_id, {node_id: val})
                )

        def _do_publish():
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
                    asyncio.create_task(_app_state.event_bus.publish(event))
            except Exception:
                pass

        try:
            loop.call_soon_threadsafe(_do_callback)
            loop.call_soon_threadsafe(_do_publish)
        except Exception as e:
            logger.warning("[opcua] device=%s code=CALLBACK_FAILED msg=dispatch failed: %s", self.device_id, e)
