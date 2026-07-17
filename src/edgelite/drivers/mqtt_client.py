"""MQTT Client驱动 - 基于aiomqtt实现"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import ipaddress
import json
import logging
import os
import random
import socket
import sqlite3  # FIXED-P1: 原问题-模块级注解"sqlite3.Connection"引用sqlite3但仅在方法内局部导入，ruff F821未定义名称; 修复-提升为模块级导入
import time as _time
from collections import deque
from collections.abc import Callable
from typing import Any

from edgelite.config import get_config
from edgelite.constants import (
    _MQTT_DRIVER_RECONNECT,
    _MQTT_KEEPALIVE,
    _MQTT_QUEUE_MAXSIZE,
    _MQTT_RECONNECT_DELAY,
    _QUEUE_POLL_TIMEOUT,
)
from edgelite.drivers.base import (  # #[AUDIT-FIX] 补充 DriverCapabilities 导入
    DriverCapabilities,
    DriverPlugin,
)

logger = logging.getLogger(__name__)

_MAX_PAYLOAD_SIZE = 1048576
_DEFAULT_MAX_PAYLOAD_SIZE = 262144
_LOCAL_PUB_TTL = 300.0  # FIXED-P2: 缓冲消息TTL（5分钟），超时消息在drain时丢弃
_MQTT_PERSIST_DB_NAME = "mqtt_pub_queue.db"


def _is_broker_host_safe(host: str) -> bool:
    """SSRF 校验：拦截 link_local/未指定/组播/保留地址。

    IoT 网关场景下，连接本地 MQTT broker (localhost/127.0.0.1/::1) 是合理且常见的部署方式，
    因此允许 loopback 地址。仅拦截 is_link_local（云元数据 169.254.x.x）、
    is_unspecified、is_multicast、is_reserved 等危险地址。
    注意：IPv6 ::1 的 is_reserved 也为 True，因此先检查 is_loopback 并直接放行。
    域名先通过 socket.getaddrinfo 解析为 IP，再校验每个解析结果。
    """
    if not host:
        return False

    def _check_ip(ip: ipaddress._BaseAddress) -> bool:
        """单个 IP 地址安全校验：loopback 放行，其余危险地址拦截。"""
        if ip.is_loopback:
            return True
        return not (ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved)

    try:
        ip = ipaddress.ip_address(host)
        return _check_ip(ip)
    except ValueError:
        pass
    try:
        addrs = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return False
    if not addrs:
        return False
    for _family, _stype, _proto, _canon, sockaddr in addrs:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if not _check_ip(ip):
            return False
    return True


def _sanitize_topic_segment(s: str) -> str:
    """R5-S-07: 脱敏 MQTT topic 段，替换 MQTT 特殊字符防止 topic 注入。

    将 `/`、`+`、`#`、`\0` 替换为 `_`，避免 device_id 等用户可控字段
    拼接 topic 时越权访问其他层级或通配符主题。
    """
    if not s:
        return ""
    # 替换 MQTT 特殊字符：/ 层级分隔符、+ 单层通配符、# 多层通配符、\0 空字节
    for ch in ("/", "+", "#", "\0"):
        s = s.replace(ch, "_")
    return s


class PersistentPubQueue:
    """FIXED-P2: 离线缓冲持久化到SQLite，防止进程重启数据丢失
    之前：_local_pub_queue使用内存deque，进程重启后缓冲数据全部丢失
    之后：使用SQLite持久化，提供与deque一致的接口（append/popleft/appendleft/clear/__len__/maxlen）
    """

    def __init__(self, maxlen: int = 1000, db_path: str | None = None):
        import os
        import sqlite3
        import threading

        self._maxlen = maxlen
        if db_path is None:
            data_dir = os.environ.get("EDGELITE_DATA_DIR", "data")
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, _MQTT_PERSIST_DB_NAME)
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        import sqlite3

        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        return self._conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mqtt_pub_queue ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "seq REAL NOT NULL, "
                "topic TEXT NOT NULL, "
                "payload TEXT NOT NULL, "
                "ts REAL NOT NULL)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mqtt_pub_seq ON mqtt_pub_queue(seq)")
            conn.commit()

    @property
    def maxlen(self) -> int:
        return self._maxlen

    def append(self, item: tuple) -> None:
        topic, payload, ts = item[0], item[1], item[2] if len(item) >= 3 else _time.time()
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute("SELECT MAX(seq) FROM mqtt_pub_queue").fetchone()
                next_seq = (row[0] or 0.0) + 1.0
                # 容量达上限时淘汰最旧条目
                count = conn.execute("SELECT COUNT(*) FROM mqtt_pub_queue").fetchone()[0]
                if count >= self._maxlen:
                    conn.execute(
                        "DELETE FROM mqtt_pub_queue WHERE id = (SELECT id FROM mqtt_pub_queue ORDER BY seq ASC LIMIT 1)"
                    )
                conn.execute(
                    "INSERT INTO mqtt_pub_queue (seq, topic, payload, ts) VALUES (?, ?, ?, ?)",
                    (next_seq, topic, payload, ts),
                )
                conn.commit()
            except Exception:
                # FIX-EL-R2-GENERAL: 任何SQL失败必须 rollback 释放事务，否则后续
                # append/popleft 全部以 "database is locked" 失败，缓冲数据永久无法落盘。
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise

    def appendleft(self, item: tuple) -> None:
        topic, payload, ts = item[0], item[1], item[2] if len(item) >= 3 else _time.time()
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute("SELECT MIN(seq) FROM mqtt_pub_queue").fetchone()
                prev_seq = (row[0] or 0.0) - 1.0
                # 容量达上限时淘汰最旧条目（与 append 一致的容量检查逻辑）
                count = conn.execute("SELECT COUNT(*) FROM mqtt_pub_queue").fetchone()[0]
                if count >= self._maxlen:
                    conn.execute(
                        "DELETE FROM mqtt_pub_queue WHERE id = (SELECT id FROM mqtt_pub_queue ORDER BY seq ASC LIMIT 1)"
                    )
                conn.execute(
                    "INSERT INTO mqtt_pub_queue (seq, topic, payload, ts) VALUES (?, ?, ?, ?)",
                    (prev_seq, topic, payload, ts),
                )
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise

    def popleft(self) -> tuple:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute("SELECT topic, payload, ts FROM mqtt_pub_queue ORDER BY seq ASC LIMIT 1").fetchone()
                if row is None:
                    raise IndexError("popleft from an empty queue")
                conn.execute(
                    "DELETE FROM mqtt_pub_queue WHERE id = (SELECT id FROM mqtt_pub_queue ORDER BY seq ASC LIMIT 1)"
                )
                conn.commit()
                return (row[0], row[1], row[2])
            except IndexError:
                raise
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise

    def clear(self) -> None:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute("DELETE FROM mqtt_pub_queue")
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                raise

    def __len__(self) -> int:
        with self._lock:
            conn = self._get_conn()
            return conn.execute("SELECT COUNT(*) FROM mqtt_pub_queue").fetchone()[0]

    def __bool__(self) -> bool:
        return self.__len__() > 0

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


class MqttClientDriver(DriverPlugin):
    """MQTT Client协议驱动，订阅设备数据主题"""

    plugin_name = "mqtt_client"
    plugin_version = "0.1.0"
    supported_protocols = ("mqtt",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    # #[AUDIT-FIX] WARNING: 缺失 _required_dependencies 声明，registry 无法预检 aiomqtt 依赖
    _required_dependencies: tuple[str, ...] = ("aiomqtt",)  # FIXED(P2): 原问题-可变默认值list; 修复-改为tuple
    # #[AUDIT-FIX] WARNING: 缺失 capabilities 声明，基类默认 write=False/subscribe=False 与实际不符
    # 实际支持 write（write_point 发布消息）和 subscribe（订阅设备主题）
    capabilities = DriverCapabilities(
        discover=False, read=True, write=True, subscribe=True, batch_read=False, batch_write=False
    )
    config_schema = {
        "description": "MQTT client, subscribes to device data topics, supports JSON parsing",
        "fields": [
            {
                "name": "broker",
                "type": "string",
                "label": "Broker Address",
                "description": "MQTT server address, e.g. localhost or broker.emqx.io",
                "default": "localhost",
                "required": True,
            },
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "MQTT port, default 1883 (plain) or 8883 (TLS)",
                "default": 1883,
            },
            {
                "name": "username",
                "type": "string",
                "label": "Username",
                "description": "MQTT auth username, leave empty if no auth",
            },
            {
                "name": "password",
                "type": "string",
                "label": "Password",
                "description": "MQTT auth password",
                "secret": True,
            },
            {
                "name": "topic",
                "type": "string",
                "label": "Subscribe Topic",
                "description": "MQTT topic to subscribe, supports wildcards like device/+/data",
                "required": True,
            },
            # ── Last Will ──
            {
                "name": "will_topic",
                "type": "string",
                "label": "Will Topic",
                "description": "Last Will topic, leave empty to disable",
                "default": "",
            },
            {
                "name": "will_message",
                "type": "string",
                "label": "Will Message",
                "description": "Last Will payload, sent when client disconnects unexpectedly",
                "default": '{"status":"offline"}',
            },
            {
                "name": "will_qos",
                "type": "integer",
                "label": "Will QoS",
                "description": "Last Will QoS level (0/1/2)",
                "default": 1,
            },
            {
                "name": "will_retain",
                "type": "boolean",
                "label": "Will Retain",
                "description": "Whether to retain the Last Will message",
                "default": True,
            },
            # ── Session Persistence ──
            {
                "name": "clean_session",
                "type": "boolean",
                "label": "Clean Session",
                "description": "If False, Broker restores subscriptions and undelivered messages on reconnect; requires client_id",
                "default": True,
            },
            {
                "name": "client_id",
                "type": "string",
                "label": "Client ID",
                "description": "MQTT client ID, required when clean_session=False; leave empty for auto-generated",
                "default": "",
            },
            # ── TLS Mutual Auth ──
            {
                "name": "tls_enabled",
                "type": "boolean",
                "label": "Enable TLS",
                "description": "Enable TLS/SSL connection",
                "default": False,
            },
            {
                "name": "ca_cert",
                "type": "string",
                "label": "CA Certificate",
                "description": "Path to CA certificate file for server verification",
                "default": "",
            },
            {
                "name": "client_cert",
                "type": "string",
                "label": "Client Certificate",
                "description": "Path to client certificate file for mutual TLS",
                "default": "",
            },
            {
                "name": "client_key",
                "type": "string",
                "label": "Client Key",
                "description": "Path to client private key file for mutual TLS",
                "default": "",
            },
            {
                "name": "cert_reqs",
                "type": "string",
                "label": "Cert Verify Mode",
                "description": "Certificate verification mode: required / optional / none",
                "default": "required",
            },
            # ── Topic Routing ──
            {
                "name": "topic_routes",
                "type": "array",
                "label": "Topic Routes",
                "description": 'Topic-to-point mapping rules, e.g. [{"topic":"device/+/temperature","point":"temperature"}]',
                "default": [],
            },
            # ── Payload Limits ──
            {
                "name": "max_payload_size",
                "type": "integer",
                "label": "Max Payload Size (bytes)",
                "description": "Maximum payload size in bytes to prevent memory exhaustion (default 256KB)",
                "default": 262144,
                "min": 1024,
                "max": 10485760,
            },
        ],
    }

    def __init__(self):
        super().__init__()
        self._device_points: dict[str, list[dict]] = {}
        # CROSS-003: _latest_values 使用两层结构，设备级用普通 dict，测点级限制容量
        self._latest_values: dict[str, dict[str, Any]] = {}
        self._MAX_POINTS_PER_DEVICE = 1000  # CROSS-003: 每个设备最大测点数
        self._connect_task: asyncio.Task | None = None
        self._pub_queue: asyncio.Queue | None = None
        # MQTT-MED-001: 消息异步处理队列
        self._msg_queue: asyncio.Queue | None = None
        self._msg_consumer_task: asyncio.Task | None = None
        self._values_lock = asyncio.Lock()
        self._reconnect_count: int = 0
        self._reconnect_base: float = 1.0
        self._reconnect_max: float = 60.0
        self._reconnect_max_attempts: int = 50
        self._reconnect_exhausted: bool = False
        # MQTT-001: 长重试模式配置 - "interval"=1小时间隔重试, "stop"=完全停止等待手动恢复
        self._long_retry_mode: str = "interval"
        # FIXED-P1: MQTT-01 长重试最大持续时间（默认24小时），超时后停止自动重连
        self._long_retry_max_duration: float = 86400.0
        self._long_retry_start: float = 0.0
        self._subscribed_topics: set[str] = set()
        self._local_pub_queue: PersistentPubQueue | None = None
        self._local_pub_queue_max: int = 1000
        self._will_topic: str = ""
        self._will_message: str = '{"status":"offline"}'
        self._subscriptions_restored: bool = False
        self._pub_queue_drops: int = 0
        self._max_payload_size: int = _DEFAULT_MAX_PAYLOAD_SIZE
        # R5-G-05: 发布 QoS 默认 1（至少一次），避免 QoS 0 网络丢包时命令直接丢失
        self._publish_qos: int = 1

    async def start(self, config: dict) -> None:
        self._running = True
        self._driver_config = config  # 保存设备级配置，优先于全局配置
        self._pub_queue = asyncio.Queue(maxsize=_MQTT_QUEUE_MAXSIZE)
        # FIXED-P2: 离线缓冲持久化到SQLite，进程重启后可恢复未发送的消息
        self._local_pub_queue = PersistentPubQueue(maxlen=self._local_pub_queue_max)
        # MQTT-MED-001: 初始化消息处理队列（容量1000，超出时丢弃旧消息）
        self._msg_queue = asyncio.Queue(maxsize=1000)
        self._will_topic = str(config.get("will_topic", ""))
        self._will_message = str(config.get("will_message", '{"status":"offline"}'))
        self._reconnect_base = float(config.get("reconnect_base", 1.0))
        self._reconnect_max = float(config.get("reconnect_max", 60.0))
        self._reconnect_max_attempts = int(config.get("reconnect_max_attempts", 50))
        self._max_payload_size = int(config.get("max_payload_size", _DEFAULT_MAX_PAYLOAD_SIZE))
        # R5-G-05: 从驱动配置读取发布 QoS，默认 1（至少一次），防止网络丢包时命令丢失
        self._publish_qos = int(config.get("qos", 1))
        # FIXED-P2: QoS 合法性校验，防止非法值导致 aiomqtt 库内部异常
        if self._publish_qos not in (0, 1, 2):
            logger.warning("[mqtt] invalid qos=%d, falling back to 1", self._publish_qos)
            self._publish_qos = 1
        self._connect_task = asyncio.create_task(self._connect_loop(), name="mqtt-client-connect")
        logger.info("MQTT Client驱动启动")

    async def stop(self) -> None:
        self._running = False
        # MQTT-MED-001: 取消消息消费者任务
        if self._msg_consumer_task and not self._msg_consumer_task.done():
            self._msg_consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._msg_consumer_task
        self._msg_consumer_task = None
        # CROSS-004: 取消所有后台任务
        await self._cancel_background_tasks()
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task
        self._device_configs.clear()
        self._device_points.clear()
        self._latest_values.clear()
        self._subscribed_topics.clear()
        if self._local_pub_queue is not None:
            # FIXED-P2: 不清空队列，仅关闭 SQLite 连接，保留未发送消息以符合持久化设计目标
            self._local_pub_queue.close()
            self._local_pub_queue = None
        self._reconnect_count = 0
        self._reconnect_exhausted = False
        # FIXED-P1: 清理pub_queue残留消息，添加None检查防止start()未调用时AttributeError
        if self._pub_queue is not None:
            while not self._pub_queue.empty():
                try:
                    self._pub_queue.get_nowait()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    break
        logger.info("MQTT Client驱动停止")

    async def reset_reconnect_state(self, device_id: str) -> None:
        """FIX-EL-R2-SEVERE: 手动重置重连状态，允许在重连耗尽后恢复重连。
        原实现为同步方法且缺少 device_id 参数，违反基类 DriverPlugin.reset_reconnect_state
        的 async 签名契约，通过基类多态调用时抛 TypeError。
        """
        await super().reset_reconnect_state(device_id)
        if self._reconnect_exhausted:
            self._reconnect_count = 0
            self._reconnect_exhausted = False
            logger.info(
                "[mqtt] code=RECONNECT_RESET msg=Reconnect state reset for device=%s, will retry immediately", device_id
            )
        else:
            logger.info("[mqtt] code=RECONNECT_RESET msg=Reconnect state already active for device=%s", device_id)

    async def add_device(self, device_id: str, config: dict, points: list[dict]) -> None:
        self._device_configs[device_id] = config
        self._device_points[device_id] = points
        self._latest_values[device_id] = {}

    async def remove_device(self, device_id: str) -> None:
        self._device_configs.pop(device_id, None)
        self._device_points.pop(device_id, None)
        self._latest_values.pop(device_id, None)

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        if self._reconnect_exhausted:  # FIXED-P2: 断连时返回空dict，避免返回过期数据
            return {}
        async with self._values_lock:
            # FIXED-P1: 先浅拷贝释放锁，再对可变嵌套值深拷贝，兼顾性能与安全
            raw_values = dict(self._latest_values.get(device_id, {}))
        values = {k: copy.deepcopy(v) if isinstance(v, (dict, list)) else v for k, v in raw_values.items()}
        return {p: values.get(p) for p in points if p in values}

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._pub_queue:
            return False
        config = self._device_configs.get(device_id, {})
        # FIXED(安全) R5-S-07: 脱敏 device_id 防止 topic 注入（/、+、# 等 MQTT 特殊字符）
        _safe_dev_id = _sanitize_topic_segment(device_id)
        publish_topic = config.get("publish_topic", f"edgelite/{_safe_dev_id}/command")
        message = json.dumps({"point": point, "value": value}, ensure_ascii=False)
        # FIXED-P2: 发布前校验载荷大小，防止超限消息导致Broker断连死循环
        encoded = message.encode("utf-8")
        if len(encoded) > self._max_payload_size:
            logger.warning(
                "[mqtt] publish payload too large: %d bytes (max %d), dropping", len(encoded), self._max_payload_size
            )
            return False
        try:
            self._pub_queue.put_nowait((publish_topic, encoded))
            return True
        except asyncio.QueueFull:
            logger.warning("MQTT发布队列已满，丢弃消息")
            return False

    def on_data(self, callback: Callable) -> None:
        self._data_callback = callback

    def _log_error(self, device_id: str, error_code: str, message: str) -> None:
        """统一日志四元组: [mqtt] device={device_id} code={error_code} {message}"""
        logger.error("[mqtt] device=%s code=%s %s", device_id, error_code, message)

    def _compute_backoff_delay(self) -> float | None:
        """计算重连退避延迟。

        Returns:
            float: 延迟秒数（None 表示停止重连，进入手动恢复模式）

        长重试模式行为（MQTT-001）：
            - "interval": 每次重连间隔 1 小时（self._reconnect_max * 60 秒）
            - "stop": 返回 None，表示完全停止自动重连
        """
        if self._reconnect_exhausted:
            if self._long_retry_mode == "stop":
                return None
            return self._reconnect_max * 60
        delay = min(self._reconnect_base * (2**self._reconnect_count), self._reconnect_max)
        jitter = random.uniform(0, 1.0)
        return delay + jitter

    def _on_disconnect(self, reason: str = "") -> None:
        self._subscribed_topics.clear()
        # FIXED: 断线时清理 _msg_queue 中残留消息，防止重连后新消费者处理旧会话消息（Layer 3 - 运行时健壮性）
        # 原问题：_msg_consumer_task 被取消后，_msg_queue 中可能残留未处理消息，
        #         重连后新 _message_consumer 会消费这些旧消息，导致处理过期数据
        if self._msg_queue is not None:
            drained = 0
            while not self._msg_queue.empty():
                try:
                    self._msg_queue.get_nowait()
                    drained += 1
                except asyncio.QueueEmpty:
                    break
            if drained > 0:
                logger.info(
                    "[mqtt] code=ON_DISCONNECT msg=subscription cleared, drained %d stale messages from msg_queue, reason=%s",
                    drained,
                    reason,
                )
            else:
                logger.info("[mqtt] code=ON_DISCONNECT msg=subscription records cleared reason=%s", reason)
        else:
            logger.info("[mqtt] code=ON_DISCONNECT msg=subscription records cleared reason=%s", reason)

    # ── Topic 匹配与变量提取 ──

    @staticmethod
    def _topic_matches(pattern: str, topic: str) -> bool:
        """Check if topic matches MQTT pattern with wildcards (+/#)"""
        pattern_parts = pattern.split("/")
        topic_parts = topic.split("/")
        for i, p in enumerate(pattern_parts):
            if p == "#":
                # FIXED-P2: _topic_matches对#通配符处理不符合MQTT规范，#必须为最后一个层级
                return i == len(pattern_parts) - 1
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

        # FIXED(安全): SSRF 防护 - 校验 broker 地址，拦截 loopback/link_local（云元数据）
        # 允许 is_private（内网 MQTT broker 是合理场景）
        if not _is_broker_host_safe(broker):
            logger.error("[mqtt] broker address blocked by SSRF protection: %s", broker)
            self._running = False
            return

        while self._running:
            try:
                import aiomqtt

                # ── 会话持久化 ──
                clean_session = bool(self._driver_config.get("clean_session", True))
                client_id = self._driver_config.get("client_id", "")
                if not clean_session and not client_id:
                    logger.warning(
                        "[mqtt] device= code=AUTH_FAILED clean_session=False requires a client_id, falling back to clean_session=True"
                    )
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
                # FIXED: 添加 Will 消息长度限制和 QoS 校验（Layer 2 - MQTT 协议规范）
                # MQTT 规范: topic 最长 65535 字节, payload 最长 65535 字节
                # 实际限制: topic ≤ 256 字节, payload ≤ 65536 字节(64KB) 防止内存占用过大
                _MQTT_WILL_MAX_TOPIC_LEN = 256
                _MQTT_WILL_MAX_PAYLOAD_LEN = 65536
                will_topic = self._will_topic
                will_message = self._will_message
                will_qos = int(self._driver_config.get("will_qos", 1))
                will_retain = bool(self._driver_config.get("will_retain", True))

                # FIXED: Will QoS 合法性校验，非法值回退到 QoS 1
                if will_qos not in (0, 1, 2):
                    logger.warning("[mqtt] invalid will_qos=%d, falling back to 1", will_qos)
                    will_qos = 1

                if will_topic:
                    # FIXED: Will topic 长度校验
                    if len(will_topic.encode("utf-8")) > _MQTT_WILL_MAX_TOPIC_LEN:
                        logger.warning(
                            "[mqtt] will_topic too long (%d bytes, max %d), truncating will message",
                            len(will_topic.encode("utf-8")),
                            _MQTT_WILL_MAX_TOPIC_LEN,
                        )
                        will_topic = will_topic[:_MQTT_WILL_MAX_TOPIC_LEN]
                    # FIXED: Will payload 长度校验
                    will_payload_bytes = will_message.encode("utf-8")
                    if len(will_payload_bytes) > _MQTT_WILL_MAX_PAYLOAD_LEN:
                        logger.warning(
                            "[mqtt] will_message too large (%d bytes, max %d), truncating",
                            len(will_payload_bytes),
                            _MQTT_WILL_MAX_PAYLOAD_LEN,
                        )
                        will_message = will_message[:_MQTT_WILL_MAX_PAYLOAD_LEN]
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
                        verify = self._driver_config.get("verify", True)
                        if verify:
                            cert_reqs_str = self._driver_config.get("cert_reqs", "required")
                        else:
                            cert_reqs_str = "none"

                        # FIXED(安全) R5-S-08: cert_reqs=none 禁用证书验证存在中间人攻击风险，
                        # 必须显式设置 EDGELITE_ALLOW_INSECURE_TLS=1 才允许，防止生产环境误配置
                        if cert_reqs_str == "none" and os.getenv("EDGELITE_ALLOW_INSECURE_TLS") != "1":
                            raise ValueError(
                                "cert_reqs=none prohibited, set EDGELITE_ALLOW_INSECURE_TLS=1 to override "
                                "(NOT recommended for production)"
                            )

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
                        logger.info(
                            "[mqtt] device= code=TLS_OK TLS enabled, cert_reqs=%s", cert_reqs_str
                        )  # FIXED-P2: TLS成功时code应为TLS_OK
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        self._log_error("", "TLS_ERROR", f"TLS config failed: {e}")
                        # FIXED-P0: TLS配置失败时使用指数退避，与连接失败退避一致
                        self._reconnect_count += 1
                        # FIXED-P1: TLS失败也检查最大重试次数，防止永久错误时无限重试
                        if self._reconnect_count > self._reconnect_max_attempts:
                            if not self._reconnect_exhausted:
                                self._long_retry_start = _time.time()
                            self._reconnect_exhausted = True
                            if (
                                self._long_retry_start > 0
                                and (_time.time() - self._long_retry_start) > self._long_retry_max_duration
                            ):
                                self._log_error(
                                    "",
                                    "RECONNECT_DURATION_EXCEEDED",
                                    f"TLS config failed, long retry exceeded max duration ({self._long_retry_max_duration:.0f}s)",
                                )
                                return
                        backoff = self._compute_backoff_delay()
                        if backoff is None:
                            return
                        await asyncio.sleep(backoff)
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
                                logger.info(
                                    "[mqtt] device= code=TLS_OK TLS enabled (global config)"
                                )  # FIXED-P2: TLS成功时code应为TLS_OK
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            self._log_error("", "TLS_ERROR", f"Global TLS config failed: {e}")

                client = aiomqtt.Client(**client_kwargs)
                try:
                    await client.__aenter__()
                except Exception as e:
                    # FIXED-P0: __aenter__失败时确保client被清理，防止连接泄漏
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(client.__aexit__(type(e), e, e.__traceback__), timeout=5.0)
                    raise
                try:
                    logger.info("[mqtt] device= code=RECONNECT_OK Connected to %s:%d", broker, port)
                    self._subscriptions_restored = False

                    await self._drain_local_pub_queue(client)

                    # 订阅设备主题
                    _subscribe_success_count = 0  # FIXED-P2: 订阅失败后不触发重连，追踪订阅成功数
                    # FIXED-P1: 遍历前快照，原问题：遍历_device_configs期间add_device/remove_device可能并发修改字典导致RuntimeError；
                    # 修复：使用list()创建快照，避免迭代过程中字典大小改变
                    for device_id, dev_config in list(self._device_configs.items()):
                        topic = dev_config.get("topic") or dev_config.get(
                            "subscribe_topic", f"edgelite/{device_id}/data"
                        )
                        # FIXED-P1: TopicFilter 格式校验（MQTT 协议规范 §3.8）
                        # 原问题：直接订阅未校验 topic，空字符串或含非法字符的 topic 可能导致协议错误
                        if not topic or not isinstance(topic, str):
                            self._log_error(device_id, "SUBSCRIBE_FAILED", "topic is empty or not a string")
                            continue
                        if "\0" in topic:
                            self._log_error(device_id, "SUBSCRIBE_FAILED", f"topic contains null byte: {topic[:64]}")
                            continue
                        # FIXED-P1: topic 长度校验（MQTT 规范 topic 最长 65535 字节）
                        if len(topic.encode("utf-8")) > 65535:
                            self._log_error(
                                device_id,
                                "SUBSCRIBE_FAILED",
                                f"topic too long ({len(topic.encode('utf-8'))} bytes, max 65535)",
                            )
                            continue
                        try:
                            await client.subscribe(topic)
                            self._subscribed_topics.add(topic)
                            _subscribe_success_count += 1
                        except asyncio.CancelledError:
                            raise
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
                                self._subscribed_topics.add(route_topic)
                                _subscribe_success_count += 1
                            except asyncio.CancelledError:
                                raise
                            except Exception as e:
                                self._log_error("", "SUBSCRIBE_FAILED", f"Subscribe route {route_topic} failed: {e}")

                    # FIXED-P2: 订阅失败后不触发重连，所有主题均订阅失败时触发重连
                    _total_subscribe_targets = len(self._device_configs) + len(topic_routes)
                    if _total_subscribe_targets > 0 and _subscribe_success_count == 0:
                        raise ConnectionError("All subscriptions failed")

                    self._subscriptions_restored = True
                    self._reconnect_count = 0
                    self._reconnect_exhausted = False

                    # MQTT-MED-001: 启动消息消费者协程
                    if self._msg_consumer_task is None or self._msg_consumer_task.done():
                        self._msg_consumer_task = asyncio.create_task(
                            self._message_consumer(), name="mqtt-client-consumer"
                        )

                    msg_task = asyncio.create_task(
                        self._message_loop(client),
                        name="mqtt-client-msg",  # #[AUDIT-FIX] _message_loop requires client arg, was missing causing TypeError
                    )
                    pub_task = asyncio.create_task(self._publish_loop(client), name="mqtt-client-publish")

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        # FIXED-P2: _message_consumer任务在连接断开时未取消
                        if self._msg_consumer_task and not self._msg_consumer_task.done():
                            self._msg_consumer_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await self._msg_consumer_task
                        for t in [msg_task, pub_task]:
                            if not t.done():
                                t.cancel()
                        for t in [msg_task, pub_task]:
                            with contextlib.suppress(asyncio.CancelledError):
                                await t
                finally:
                    # FIXED-P0: __aexit__加超时保护，防止Broker无响应时stop()阻塞keepalive周期(默认60秒)
                    try:
                        await asyncio.wait_for(client.__aexit__(None, None, None), timeout=5.0)
                    except TimeoutError:
                        logger.warning(
                            "[mqtt] code=DISCONNECT_TIMEOUT msg=Client __aexit__ timed out after 5s, forcing close"
                        )
                    # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
                    except Exception as e:
                        logger.debug("[mqtt] client.__aexit__ failed: %s", e)

            except asyncio.CancelledError:
                raise
            except ImportError:
                self._log_error("", "CONN_FAILED", "aiomqtt not installed, MQTT driver unavailable")
                self._on_disconnect(reason="import error")
                self._reconnect_count += 1  # FIXED-P0: ImportError分支无退避机制，使用指数退避
                backoff = self._compute_backoff_delay()
                if backoff is None:
                    return
                await asyncio.sleep(backoff)
            except (
                Exception
            ) as e:  # FIXED-P2: 用通用Exception替代aiomqtt.exceptions.MqttError，防止aiomqtt版本不兼容时NameError
                if hasattr(e, "__module__") and "aiomqtt" in str(getattr(e, "__module__", "")):
                    self._on_disconnect(reason="mqtt error")
                else:
                    self._on_disconnect(reason="connection error")
                error_str = str(e).lower()
                if (
                    "auth" in error_str
                    or "credential" in error_str
                    or "unauthorized" in error_str
                    or "reject" in error_str
                ):
                    self._log_error("", "AUTH_FAILED", f"Auth failed: {e}")
                else:
                    self._log_error("", "CONN_LOST", f"Connection lost: {e}")
                self._reconnect_count += 1  # FIXED-P0: _reconnect_count递增时机不一致，统一为先递增再计算退避
                if self._reconnect_count > self._reconnect_max_attempts:
                    if not self._reconnect_exhausted:
                        self._long_retry_start = _time.time()
                    self._reconnect_exhausted = True
                    # FIXED-P1: MQTT-01 长重试超时检查，超过最大持续时间后停止自动重连
                    if (
                        self._long_retry_start > 0
                        and (_time.time() - self._long_retry_start) > self._long_retry_max_duration
                    ):
                        self._log_error(
                            "",
                            "RECONNECT_DURATION_EXCEEDED",
                            f"Long retry exceeded max duration ({self._long_retry_max_duration:.0f}s), stopping auto-reconnect",
                        )
                        if self._data_callback:
                            try:
                                self._data_callback(
                                    device_id="",
                                    data={
                                        "event": "reconnect_stopped",
                                        "reason": "duration_exceeded",
                                        "max_duration": self._long_retry_max_duration,
                                    },
                                )
                            except Exception as exc:
                                logger.warning(
                                    "[mqtt] code=CALLBACK_ERROR msg=reconnect_stopped callback failed: %s", exc
                                )
                        return
                    logger.warning(
                        "[mqtt] code=RECONNECT_EXHAUSTED msg=Max reconnect attempts (%d) reached, long_retry_mode=%s",
                        self._reconnect_max_attempts,
                        self._long_retry_mode,
                    )
                    if self._long_retry_mode == "interval":
                        self._log_error(
                            "", "RECONNECT_EXHAUSTED", "Max reconnect attempts reached, will retry in 1-hour intervals"
                        )
                    else:
                        self._log_error(
                            "",
                            "RECONNECT_STOPPED",
                            "Max reconnect attempts reached, stopping auto-reconnect (await manual recovery)",
                        )
                # FIXED: 先设置 _reconnect_exhausted 标志再计算 backoff，确保首次超限时 _compute_backoff_delay 能检测到标志使用长重试间隔
                backoff = self._compute_backoff_delay()
                # MQTT-001: backoff 为 None 表示停止重连（stop 模式）
                if backoff is None:
                    self._log_error("", "RECONNECT_WAITING_MANUAL", "Waiting for manual recovery or restart")
                    if self._data_callback:
                        try:
                            self._data_callback(
                                device_id="", data={"event": "reconnect_stopped", "reason": "manual_recovery_required"}
                            )
                        except Exception as exc:
                            logger.warning("[mqtt] code=CALLBACK_ERROR msg=reconnect_stopped callback failed: %s", exc)
                    return
                logger.info("[mqtt] reconnecting in %.1fs (attempt %d)", backoff, self._reconnect_count)
                await asyncio.sleep(backoff)

    async def _message_loop(self, client: Any) -> None:
        """MQTT-MED-001: 消息入队协程（快速返回，不阻塞网络循环）

        回调仅做入队，由独立的 _message_consumer 协程处理。
        """
        try:
            async for message in client.messages:
                if not self._running:
                    break
                # MQTT-MED-001: 快速入队，立即返回，不阻塞
                try:
                    self._msg_queue.put_nowait(message)
                except asyncio.QueueFull:
                    # FIXED-P1: 队列满时先get腾出空间再put，保证新消息入队
                    # 原问题：get_nowait可能抛QueueEmpty被泛Exception捕获，后续put_nowait可能不执行
                    # 修复：get_nowait用contextlib.suppress(QueueEmpty)包裹，确保put执行
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._msg_queue.get_nowait()
                    try:
                        self._msg_queue.put_nowait(message)
                    except asyncio.QueueFull:
                        logger.warning("[mqtt] message_loop: queue still full after get, dropping message")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # FIXED-P0: 原问题-仅捕获CancelledError，client断开时MqttError/ConnectionError未被捕获，
            # 任务静默退出，消息处理中断且_connect_loop不感知。添加Exception捕获并记录日志。
            logger.error("[mqtt] code=MESSAGE_LOOP_EXIT msg=_message_loop exited unexpectedly: %s", e)

    async def _message_consumer(self) -> None:
        """MQTT-MED-001: 消息消费者协程（从队列取出消息并处理）"""
        # FIXED-P2: _message_consumer异常处理层次混乱，简化为两层try/except
        while self._running:
            try:
                try:
                    message = await asyncio.wait_for(self._msg_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue

                try:
                    await self._handle_message(message)
                except Exception as e:
                    logger.warning("[mqtt] message consumer error: %s", e)
                    continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[mqtt] message consumer fatal error: %s", e)
                await asyncio.sleep(1.0)  # FIXED-P0: 原代码break导致消费者永久退出无恢复，改为continue+退避等待

    def _persist_overflow_message(self, topic: str, payload: Any) -> None:
        """FIXED-P1: 队列满时将消息持久化到磁盘overflow文件，而非丢弃

        原问题：_publish_loop失败入_local_pub_queue满时仅计数丢弃，QoS>=1的消息可能永久丢失
        修复：将溢出消息以JSON Lines格式追加写入磁盘overflow文件，确保不丢失
        """
        try:
            data_dir = os.environ.get("EDGELITE_DATA_DIR", "data")
            os.makedirs(data_dir, exist_ok=True)
            overflow_path = os.path.join(data_dir, "mqtt_pub_overflow.log")
            # R5-G-07: 写入前检查文件大小，超过 100MB 时轮转，防止溢出文件无限增长占满磁盘
            try:
                if os.path.exists(overflow_path) and os.path.getsize(overflow_path) > 100 * 1024 * 1024:
                    os.replace(overflow_path, overflow_path + ".1")
                    logger.warning("[mqtt] overflow file rotated: %s -> %s", overflow_path, overflow_path + ".1")
            except OSError as rot_err:
                logger.warning("[mqtt] overflow file rotation check failed: %s", rot_err)
            payload_str = (
                payload.decode("utf-8", errors="replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
            )
            with open(overflow_path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps({"topic": topic, "payload": payload_str, "ts": _time.time()}, ensure_ascii=False) + "\n"
                )
            self._pub_queue_drops += 1
            if self._pub_queue_drops % 100 == 1:
                logger.warning(
                    "[mqtt] local_pub_queue full, %d messages persisted to overflow file", self._pub_queue_drops
                )
        except Exception as e:
            logger.error("[mqtt] failed to persist overflow message: %s", e)
            self._pub_queue_drops += 1

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    topic, payload = await asyncio.wait_for(self._pub_queue.get(), timeout=_QUEUE_POLL_TIMEOUT)
                except TimeoutError:  # FIXED-P1: 兼容Python<3.11
                    continue
                try:
                    # R5-G-05: 传入配置的 QoS（默认 1），避免 QoS 0 网络丢包时命令直接丢失
                    await client.publish(topic, payload, qos=self._publish_qos)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log_error("", "PUBLISH_FAILED", f"Publish to {topic} failed: {e}")
                    if self._local_pub_queue is not None:
                        if len(self._local_pub_queue) >= self._local_pub_queue.maxlen:
                            # FIXED-P1: QoS>=1的消息满时持久化到磁盘而非丢弃
                            # 原问题：队列满时仅计数丢弃，QoS>=1消息可能丢失
                            # 修复：将溢出消息写入磁盘overflow文件，确保不丢失
                            await asyncio.to_thread(self._persist_overflow_message, topic, payload)
                        else:
                            self._local_pub_queue.append(
                                (topic, payload, _time.time())
                            )  # FIXED-P2: 缓冲消息增加时间戳，用于TTL过滤
                            logger.debug(
                                "[mqtt] local_pub_queue buffered: topic=%s queue_size=%d",
                                topic,
                                len(self._local_pub_queue),
                            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("[mqtt] code=PUBLISH_LOOP_EXIT msg=_publish_loop exited: %s", e)

    async def _handle_message(self, message: Any) -> None:
        try:
            if len(message.payload) > self._max_payload_size:
                logger.warning(
                    "[mqtt] payload too large: %d bytes (max %d) from topic %s, dropping",
                    len(message.payload),
                    self._max_payload_size,
                    message.topic,
                )
                return
            topic = str(message.topic)

            # MQTT-003: 安全解码 UTF-8，无效 payload 不抛异常
            try:
                payload = message.payload.decode("utf-8")
                # UTF-8 解码成功，尝试 JSON 解析
                try:
                    data = json.loads(payload)  # FIXED-P1: Python默认递归限制约1000层已足够防护深嵌套JSON
                except json.JSONDecodeError as json_err:
                    logger.warning(
                        "[mqtt] code=MESSAGE_PARSE_ERROR msg=JSON parse failed for topic=%s: %s", topic, json_err
                    )
                    return
            except UnicodeDecodeError as decode_err:
                logger.warning(
                    "[mqtt] code=PAYLOAD_DECODE_ERROR msg=UTF-8 decode failed for topic=%s, using base64",
                    topic,
                    decode_err,
                )
                # MQTT-003: UTF-8 解码失败，使用 base64 编码作为值
                payload = base64.b64encode(message.payload).decode("ascii")
                data = payload

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
                        for did, dev_config in list(self._device_configs.items()):  # FIXED-P1: 快照遍历防止竞态
                            sub_topic = dev_config.get("topic") or dev_config.get(
                                "subscribe_topic", f"edgelite/{did}/data"
                            )
                            if topic == sub_topic or topic.endswith(sub_topic):
                                device_id = did
                                break

                    if not device_id:
                        continue

                    # FIXED-P2: _latest_values外层字典无设备数量限制，仅处理已注册设备
                    if device_id not in self._device_configs:
                        logger.debug(
                            "[mqtt] Ignoring message for unregistered device=%s from topic=%s", device_id, topic
                        )
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
                    conflicts = set(extracted.keys()) & set(point_data.keys())
                    if conflicts:
                        logger.debug("[mqtt] topic vars overriding data fields: %s", conflicts)
                    point_data.update(extracted)

                    async with self._values_lock:
                        point_values = self._latest_values.setdefault(device_id, {})
                        # FIXED-P1: LRU淘汰策略，先删除已存在key再插入，使更新后的key排在dict末尾
                        for k, v in point_data.items():
                            point_values.pop(k, None)
                            point_values[k] = v
                        # CROSS-003: 限制每个设备的测点数量
                        while len(point_values) > self._MAX_POINTS_PER_DEVICE:
                            point_values.pop(next(iter(point_values)))

                    if self._data_callback:
                        # FIXED-P1: _data_callback异常被静默吞没，包装为安全回调添加日志记录
                        # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用默认参数绑定当前 device_id 和 point_data 的值
                        async def _safe_callback(device_id=device_id, point_data=point_data):
                            try:
                                await self._data_callback(
                                    device_id=device_id, data=point_data
                                )  # FIXED-P1: 统一回调签名为关键字参数
                            except Exception as e:
                                logger.error("[mqtt_client] Data callback error for device=%s: %s", device_id, e)

                        self._register_task(_safe_callback())
                    return  # 路由匹配成功，结束处理

            # ── 回退到原有设备主题匹配逻辑 ──
            for device_id, dev_config in list(self._device_configs.items()):  # FIXED-P1: 快照遍历防止竞态
                subscribe_topic = dev_config.get("topic") or dev_config.get(
                    "subscribe_topic", f"edgelite/{device_id}/data"
                )
                if topic == subscribe_topic or topic.endswith(subscribe_topic):
                    async with self._values_lock:
                        point_values = self._latest_values.setdefault(device_id, {})
                        # FIXED-P1: LRU淘汰策略，先删除已存在key再插入，使更新后的key排在dict末尾
                        if isinstance(data, dict):
                            for k, v in data.items():
                                point_values.pop(k, None)
                                point_values[k] = v
                        else:
                            point_values.pop("value", None)
                            point_values["value"] = data
                        # CROSS-003: 限制每个设备的测点数量
                        while len(point_values) > self._MAX_POINTS_PER_DEVICE:
                            point_values.pop(next(iter(point_values)))

                    if self._data_callback:
                        # FIXED-P1: _data_callback异常被静默吞没，包装为安全回调添加日志记录
                        # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用默认参数绑定当前 device_id 和 data 的值
                        async def _safe_callback(device_id=device_id, data=data):
                            try:
                                await self._data_callback(
                                    device_id=device_id, data=data
                                )  # FIXED-P1: 统一回调签名为关键字参数
                            except Exception as e:
                                logger.error("[mqtt_client] Data callback error for device=%s: %s", device_id, e)

                        self._register_task(_safe_callback())
                    break

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log_error("", "MESSAGE_PARSE_ERROR", f"Message handling failed: {e}")

    async def _drain_local_pub_queue(self, client: Any) -> int:
        if not self._local_pub_queue:
            return 0
        drained = 0
        expired = 0
        now = _time.time()
        while self._local_pub_queue:
            entry = self._local_pub_queue.popleft()
            # FIXED-P2: 缓冲消息增加TTL过滤，防止发送过时工业控制命令
            if len(entry) >= 3:
                topic, payload, ts = entry[0], entry[1], entry[2]
                if now - ts > _LOCAL_PUB_TTL:
                    expired += 1
                    continue
            else:
                topic, payload = entry[0], entry[1]
            try:
                # R5-G-05: 重连后回灌缓冲消息也使用配置的 QoS，保持发布语义一致
                await client.publish(topic, payload, qos=self._publish_qos)
                drained += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._log_error("", "DRAIN_PUBLISH_FAILED", f"Drain publish to {topic} failed: {e}")
                # FIXED-P1: 发布失败时保留原始时间戳而非重置TTL，防止过期控制命令被延迟发送
                self._local_pub_queue.appendleft((topic, payload, ts if len(entry) >= 3 else _time.time()))
                break
        if expired > 0:
            logger.warning("[mqtt] local_pub_queue expired: %d messages discarded (TTL=%.0fs)", expired, _LOCAL_PUB_TTL)
        if drained > 0:
            logger.info("[mqtt] local_pub_queue drained: %d messages", drained)
        return drained

    def update_will(self, topic: str, payload: str) -> None:
        # FIXED: update_will 也进行长度校验，与 _connect_loop 中一致（Layer 2 - MQTT 协议规范）
        _MQTT_WILL_MAX_TOPIC_LEN = 256
        _MQTT_WILL_MAX_PAYLOAD_LEN = 65536
        if topic and len(topic.encode("utf-8")) > _MQTT_WILL_MAX_TOPIC_LEN:
            logger.warning(
                "[mqtt] update_will: topic too long (%d bytes, max %d), truncating",
                len(topic.encode("utf-8")),
                _MQTT_WILL_MAX_TOPIC_LEN,
            )
            topic = topic[:_MQTT_WILL_MAX_TOPIC_LEN]
        if payload and len(payload.encode("utf-8")) > _MQTT_WILL_MAX_PAYLOAD_LEN:
            logger.warning(
                "[mqtt] update_will: payload too large (%d bytes, max %d), truncating",
                len(payload.encode("utf-8")),
                _MQTT_WILL_MAX_PAYLOAD_LEN,
            )
            payload = payload[:_MQTT_WILL_MAX_PAYLOAD_LEN]
        self._will_topic = topic
        self._will_message = payload
        logger.info("[mqtt] code=WILL_UPDATED topic=%s", topic)
