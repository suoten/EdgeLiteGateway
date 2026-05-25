"""MQTT Client驱动 - 基于aiomqtt实现"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

from edgelite.config import get_config
from edgelite.constants import _MQTT_DRIVER_RECONNECT, _MQTT_KEEPALIVE, _MQTT_QUEUE_MAXSIZE, _MQTT_RECONNECT_DELAY, _QUEUE_POLL_TIMEOUT
from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class MqttClientDriver(DriverPlugin):
    """MQTT Client协议驱动，订阅设备数据主题"""

    plugin_name = "mqtt_client"
    plugin_version = "0.1.0"
    supported_protocols = ["mqtt"]
    config_schema = {
        "description": "MQTT client, subscribes to device data topics, supports JSON parsing",
        "fields": [
            {"name": "broker", "type": "string", "label": "Broker Address", "description": "MQTT server address, e.g. localhost or broker.emqx.io", "default": "localhost", "required": True},
            {"name": "port", "type": "integer", "label": "Port", "description": "MQTT port, default 1883 (plain) or 8883 (TLS)", "default": 1883},
            {"name": "username", "type": "string", "label": "Username", "description": "MQTT auth username, leave empty if no auth"},
            {"name": "password", "type": "string", "label": "Password", "description": "MQTT auth password", "secret": True},
            {"name": "topic", "type": "string", "label": "Subscribe Topic", "description": "MQTT topic to subscribe, supports wildcards like device/+/data", "required": True},
            # ── Last Will ──
            {"name": "will_topic", "type": "string", "label": "Will Topic", "description": "Last Will topic, leave empty to disable", "default": ""},
            {"name": "will_message", "type": "string", "label": "Will Message", "description": "Last Will payload, sent when client disconnects unexpectedly", "default": '{"status":"offline"}'},
            {"name": "will_qos", "type": "integer", "label": "Will QoS", "description": "Last Will QoS level (0/1/2)", "default": 1},
            {"name": "will_retain", "type": "boolean", "label": "Will Retain", "description": "Whether to retain the Last Will message", "default": True},
            # ── Session Persistence ──
            {"name": "clean_session", "type": "boolean", "label": "Clean Session", "description": "If False, Broker restores subscriptions and undelivered messages on reconnect; requires client_id", "default": True},
            {"name": "client_id", "type": "string", "label": "Client ID", "description": "MQTT client ID, required when clean_session=False; leave empty for auto-generated", "default": ""},
            # ── TLS Mutual Auth ──
            {"name": "tls_enabled", "type": "boolean", "label": "Enable TLS", "description": "Enable TLS/SSL connection", "default": False},
            {"name": "ca_cert", "type": "string", "label": "CA Certificate", "description": "Path to CA certificate file for server verification", "default": ""},
            {"name": "client_cert", "type": "string", "label": "Client Certificate", "description": "Path to client certificate file for mutual TLS", "default": ""},
            {"name": "client_key", "type": "string", "label": "Client Key", "description": "Path to client private key file for mutual TLS", "default": ""},
            {"name": "cert_reqs", "type": "string", "label": "Cert Verify Mode", "description": "Certificate verification mode: required / optional / none", "default": "required"},
            # ── Topic Routing ──
            {"name": "topic_routes", "type": "array", "label": "Topic Routes", "description": "Topic-to-point mapping rules, e.g. [{\"topic\":\"device/+/temperature\",\"point\":\"temperature\"}]", "default": []},
        ],
    }

    def __init__(self):
        self._running = False
        self._device_configs: dict[str, dict] = {}
        self._device_points: dict[str, list[dict]] = {}
        self._latest_values: dict[str, dict[str, Any]] = {}
        self._connect_task: asyncio.Task | None = None
        self._data_callback: Callable | None = None
        self._pub_queue: asyncio.Queue | None = None
        self._values_lock = asyncio.Lock()

    async def start(self, config: dict) -> None:
        self._running = True
        self._driver_config = config  # 保存设备级配置，优先于全局配置
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)
        self._connect_task = asyncio.create_task(self._connect_loop(), name="mqtt-client-connect")
        logger.info("MQTT Client驱动启动")

    async def stop(self) -> None:
        self._running = False
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        logger.info("MQTT Client驱动停止")

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}

    async def remove_device(self, device_id: str) -> None:
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        async with self._values_lock:
            values = self._latest_values.get(device_id, {}).copy()
        return {p: values.get(p) for p in points if p in values}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._pub_queue:
            return False
        config = self._device_configs.get(device_id, {})
        publish_topic = config.get("publish_topic", f"edgelite/{device_id}/command")
        message = json.dumps({"point": point, "value": value}, ensure_ascii=False)
        try:
            self._pub_queue.put_nowait((publish_topic, message.encode("utf-8")))
            return True
        except asyncio.QueueFull:
            logger.warning("MQTT发布队列已满，丢弃消息")
            return False

    def on_data(self, callback: Callable) -> None:
        self._data_callback = callback

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        """统一日志四元组: [mqtt] device={device_id} code={error_code} {message}"""
        logger.error("[mqtt] device=%s code=%s %s", device_id, error_code, message)

    # ── Topic 匹配与变量提取 ──

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """Check if topic matches MQTT pattern with wildcards (+/#)"""
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")
        for i, p in enumerate(pattern_parts):
            if p == "#":
                return True
            if i >= len(topic_parts):
                return False
            if p != "+" and p != topic_parts[i]:
                return False
        return len(pattern_parts) == len(topic_parts)

    @staticmethod
    def _extract_from_topic(pattern: str, topic: str) -> dict:
        """Extract variables from topic based on pattern, e.g. {device_id}"""
        result: dict[str, str] = {}
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")
        for i, p in enumerate(pattern_parts):
            if i < len(topic_parts) and p.startswith("{") and p.endswith("}"):
                key = p[1:-1]
                result[key] = topic_parts[i]
        return result

    async def _connect_loop(self) -> None:
        config = get_config()
        # 优先使用设备级配置，回退到全局配置
        broker = self._driver_config.get("broker") or config.mqtt.broker
        port = self._driver_config.get("port") or config.mqtt.port
        username = self._driver_config.get("username") or config.mqtt.username or None
        password = self._driver_config.get("password") or config.mqtt.password or None

        while self._running:
            try:
                import aiomqtt

                # ── 会话持久化 ──
                clean_session = bool(self._driver_config.get("clean_session", True))
                client_id = self._driver_config.get("client_id", "")
                if not clean_session and not client_id:
                    logger.warning("[mqtt] device= code=AUTH_FAILED clean_session=False requires a client_id, falling back to clean_session=True")
                    clean_session = True

                client_kwargs: dict[str, Any] = {
                    "hostname": broker,
                    "port": port,
                    "username": username,
                    "password": password,
                    "keepalive": _MQTT_KEEPALIVE,
                    "clean_session": clean_session,
                }
                if client_id:
                    client_kwargs["client_id"] = client_id

                # ── 遗嘱消息 ──
                will_topic = self._driver_config.get("will_topic", "")
                will_message = self._driver_config.get("will_message", '{"status":"offline"}')
                will_qos = int(self._driver_config.get("will_qos", 1))
                will_retain = bool(self._driver_config.get("will_retain", True))

                if will_topic:
                    client_kwargs["will"] = aiomqtt.Will(
                        topic=will_topic,
                        payload=will_message.encode("utf-8"),
                        qos=will_qos,
                        retain=will_retain,
                    )

                # ── TLS 双向认证 ──
                tls_enabled = bool(self._driver_config.get("tls_enabled", False))
                ssl_context = None
                if tls_enabled:
                    try:
                        import ssl as _ssl

                        ca_cert = self._driver_config.get("ca_cert", "")
                        client_cert = self._driver_config.get("client_cert", "")
                        client_key = self._driver_config.get("client_key", "")
                        cert_reqs_str = self._driver_config.get("cert_reqs", "required")

                        ssl_context = _ssl.create_default_context()
                        if ca_cert:
                            ssl_context.load_verify_locations(ca_cert)
                        if client_cert and client_key:
                            ssl_context.load_cert_chain(client_cert, client_key)

                        cert_reqs_map = {
                            "required": _ssl.CERT_REQUIRED,
                            "optional": _ssl.CERT_OPTIONAL,
                            "none": _ssl.CERT_NONE,
                        }
                        ssl_context.verify_mode = cert_reqs_map.get(cert_reqs_str, _ssl.CERT_REQUIRED)
                        if cert_reqs_str in ("optional", "none"):
                            ssl_context.check_hostname = False

                        client_kwargs["tls_params"] = aiomqtt.TLSParameters(ssl_context=ssl_context)
                        logger.info("[mqtt] device= code=TLS_ERROR TLS enabled, cert_reqs=%s", cert_reqs_str)
                    except Exception as e:
                        self._log_error("", "TLS_ERROR", f"TLS config failed: {e}")
                        # TLS 配置失败不继续连接，等待重试
                        await asyncio.sleep(_MQTT_RECONNECT_DELAY)
                        continue
                else:
                    # 回退到全局 TLS 配置
                    tls_config = getattr(config, "mqtt_tls", None)
                    if tls_config:
                        try:
                            from edgelite.engine.mqtt_tls import MqttTlsHelper

                            ssl_context = MqttTlsHelper.create_ssl_context(
                                ca_cert=getattr(tls_config, "ca_cert", ""),
                                client_cert=getattr(tls_config, "client_cert", ""),
                                client_key=getattr(tls_config, "client_key", ""),
                                cert_reqs=getattr(tls_config, "cert_reqs", "required"),
                            )
                            if ssl_context:
                                client_kwargs["tls_params"] = aiomqtt.TLSParameters(ssl_context=ssl_context)
                                logger.info("[mqtt] device= code=TLS_ERROR TLS enabled (global config)")
                        except Exception as e:
                            self._log_error("", "TLS_ERROR", f"Global TLS config failed: {e}")

                async with aiomqtt.Client(**client_kwargs) as client:
                    logger.info("[mqtt] device= code=RECONNECT_OK Connected to %s:%d", broker, port)

                    # 订阅设备主题
                    for device_id, dev_config in self._device_configs.items():
                        topic = dev_config.get("topic") or dev_config.get("subscribe_topic", f"edgelite/{device_id}/data")
                        try:
                            await client.subscribe(topic)
                        except Exception as e:
                            self._log_error(device_id, "SUBSCRIBE_FAILED", f"Subscribe {topic} failed: {e}")

                    # 订阅路由映射中的主题
                    topic_routes = self._driver_config.get("topic_routes", [])
                    subscribed_route_topics: set[str] = set()
                    for route in topic_routes:
                        route_topic = route.get("topic", "")
                        if route_topic and route_topic not in subscribed_route_topics:
                            try:
                                await client.subscribe(route_topic)
                                subscribed_route_topics.add(route_topic)
                            except Exception as e:
                                self._log_error("", "SUBSCRIBE_FAILED", f"Subscribe route {route_topic} failed: {e}")

                    msg_task = asyncio.create_task(
                        self._message_loop(client), name="mqtt-client-msg"
                    )
                    pub_task = asyncio.create_task(
                        self._publish_loop(client), name="mqtt-client-publish"
                    )

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        for t in [msg_task, pub_task]:
                            if not t.done():
                                t.cancel()
                        for t in [msg_task, pub_task]:
                            with contextlib.suppress(asyncio.CancelledError):
                                await t

            except asyncio.CancelledError:
                raise
            except ImportError:
                self._log_error("", "CONN_FAILED", "aiomqtt not installed, MQTT driver unavailable")
                await asyncio.sleep(_MQTT_DRIVER_RECONNECT)
            except aiomqtt.exceptions.MqttError as e:
                error_str = str(e).lower()
                if "auth" in error_str or "credential" in error_str or "unauthorized" in error_str or "reject" in error_str:
                    self._log_error("", "AUTH_FAILED", f"Auth failed: {e}")
                else:
                    self._log_error("", "CONN_LOST", f"Connection lost: {e}")
                await asyncio.sleep(_MQTT_RECONNECT_DELAY)
            except Exception as e:
                self._log_error("", "CONN_FAILED", f"Connection error: {e}, retrying in {_MQTT_RECONNECT_DELAY}s")
                await asyncio.sleep(_MQTT_RECONNECT_DELAY)

    async def _message_loop(self, client: Any) -> None:
        try:
            async for message in client.messages:
                if not self._running:
                    break
                await self._handle_message(message)
        except asyncio.CancelledError:
            pass

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    topic, payload = await asyncio.wait_for(self._pub_queue.get(), timeout=_QUEUE_POLL_TIMEOUT)
                except TimeoutError:
                    continue
                try:
                    await client.publish(topic, payload)
                except Exception as e:
                    self._log_error("", "PUBLISH_FAILED", f"Publish to {topic} failed: {e}")
        except asyncio.CancelledError:
            pass

    async def _handle_message(self, message: Any) -> None:
        try:
            topic = str(message.topic)
            payload = message.payload.decode("utf-8")

            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"value": payload}

            # ── 优先使用 topic_routes 路由映射 ──
            topic_routes = self._driver_config.get("topic_routes", [])
            if topic_routes:
                for route in topic_routes:
                    route_topic = route.get("topic", "")
                    point_name = route.get("point", "")
                    if not route_topic or not self._topic_matches(route_topic, topic):
                        continue

                    # 从 topic 提取变量
                    extracted = self._extract_from_topic(route_topic, topic)

                    # 确定目标 device_id
                    device_id = extracted.get("device_id", "")
                    if not device_id:
                        # 回退：遍历设备配置匹配
                        for did, dev_config in self._device_configs.items():
                            sub_topic = dev_config.get("topic") or dev_config.get("subscribe_topic", f"edgelite/{did}/data")
                            if topic == sub_topic or topic.endswith(sub_topic):
                                device_id = did
                                break

                    if not device_id:
                        continue

                    # 构建测点数据
                    point_data: dict[str, Any]
                    if point_name:
                        point_data = {point_name: data if not isinstance(data, dict) else data.get(point_name, data)}
                    elif isinstance(data, dict):
                        point_data = data
                    else:
                        point_data = {"value": data}

                    # 合并提取的变量作为测点
                    point_data.update(extracted)

                    async with self._values_lock:
                        self._latest_values.setdefault(device_id, {}).update(point_data)

                    if self._data_callback:
                        await self._data_callback(device_id, point_data)
                    return  # 路由匹配成功，结束处理

            # ── 回退到原有设备主题匹配逻辑 ──
            for device_id, dev_config in self._device_configs.items():
                subscribe_topic = dev_config.get("topic") or dev_config.get("subscribe_topic", f"edgelite/{device_id}/data")
                if topic == subscribe_topic or topic.endswith(subscribe_topic):
                    async with self._values_lock:
                        if isinstance(data, dict):
                            self._latest_values.setdefault(device_id, {}).update(data)
                        else:
                            self._latest_values.setdefault(device_id, {})["value"] = data

                    if self._data_callback:
                        await self._data_callback(device_id, data)
                    break

        except Exception as e:
            self._log_error("", "MESSAGE_PARSE_ERROR", f"Message handling failed: {e}")
