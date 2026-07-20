"""北向MQTT数据转发 - 将采集数据/告警事件转发到MQTT Broker

集成 RingBuffer 实现增量同步与断点续传:
- 消息同时写入 SQLite offline_queue 和 RingBuffer
- 重传优先从 RingBuffer 获取 pending 记录
- 告警消息设置 priority="alarm" 优先重传
- 支持 TLS 配置
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import sqlite3
import ssl
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

from edgelite.config import get_config
from edgelite.constants import (
    _EVENT_BUS_MAX_QUEUE,
    _MQTT_FORWARDER_RECONNECT,
    _MQTT_KEEPALIVE,
    _MQTT_RECONNECT_DELAY,
    _QUEUE_POLL_TIMEOUT,
)

# FIXED(安全) R5-S-06: 复用 drivers/mqtt_client.py 的 SSRF 校验函数，避免重复实现
from edgelite.drivers.mqtt_client import _is_broker_host_safe
from edgelite.storage.offline_queue import OfflineQueue
from edgelite.storage.ring_buffer import RingBuffer

logger = logging.getLogger(__name__)


def _sanitize_topic_segment(s: str) -> str:
    """R5-S-07: 脱敏 MQTT topic 段，替换 MQTT 特殊字符防止 topic 注入。

    将 `/`、`+`、`#`、`\\0` 替换为 `_`，避免 device_id 等用户可控字段
    拼接 topic 时越权访问其他层级或通配符主题。
    """
    if not s:
        return ""
    # 替换 MQTT 特殊字符：/ 层级分隔符、+ 单层通配符、# 多层通配符、\0 空字节
    for ch in ("/", "+", "#", "\0"):
        s = s.replace(ch, "_")
    return s


class MqttForwarder:
    """北向MQTT数据转发器，订阅EventBus事件并转发到MQTT"""

    _OFFLINE_DB_TABLE = """CREATE TABLE IF NOT EXISTS offline_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        payload TEXT NOT NULL,
        qos INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        retry_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        priority TEXT NOT NULL DEFAULT ''
    )"""

    def __init__(self):
        self._running = False
        self._connected = False
        self._connect_task: asyncio.Task | None = None
        self._pub_queue: asyncio.Queue | None = None
        self._event_bus: Any = None
        self._handlers_registered = False
        self._offline_db: sqlite3.Connection | None = None
        self._offline_cache_enabled = True
        self._offline_db_path = "data/mqtt_offline_queue.db"
        self._max_queue_size = 10000
        self._max_retries = 100
        self._retry_interval = 5.0
        self._replay_task: asyncio.Task | None = None
        self._sent_count = 0
        # FIXED: 原问题-连接失败时每5秒WARNING日志刷屏，增加指数退避和日志限流
        self._consecutive_failures = 0
        self._last_connect_fail_log_time = 0.0
        # RingBuffer 增量同步
        self._ring_buffer: RingBuffer | None = None
        self._offline_queue: OfflineQueue | None = None
        self._offline_db_lock = asyncio.Lock()  # FIXED-P0: SQLite离线队列添加并发保护
        self._offline_db_sync_lock = threading.Lock()  # FIXED-P2: W11 同步方法DB操作锁

    async def start(self, event_bus: Any = None) -> None:
        """启动 MQTT 转发器：连接 broker、注册事件处理器、启动后台任务。"""
        config = get_config()
        if not config.mqtt.broker:
            logger.info("MQTT Broker未配置，北向转发不启动")
            return

        if self._running:
            await self.stop()

        self._running = True
        self._pub_queue = asyncio.Queue(maxsize=_EVENT_BUS_MAX_QUEUE)

        offline_cfg = getattr(config, "mqtt", None)
        if offline_cfg:
            self._offline_cache_enabled = getattr(offline_cfg, "offline_cache_enabled", True)
            self._offline_db_path = getattr(offline_cfg, "offline_db_path", "data/mqtt_offline_queue.db")
            self._max_queue_size = getattr(offline_cfg, "max_queue_size", 10000)
            self._max_retries = getattr(offline_cfg, "max_retries", 100)
            self._retry_interval = getattr(offline_cfg, "retry_interval", 5.0)

        if self._offline_cache_enabled:
            self._init_offline_db()

        # 初始化 RingBuffer
        self._init_ring_buffer(config)

        # 初始化 OfflineQueue
        self._offline_queue = OfflineQueue(db_path="data/offline_queue.db")

        if event_bus:
            self._event_bus = event_bus
            event_bus.register_handler("PointUpdateEvent", self._on_point_update)
            event_bus.register_handler("AlarmEvent", self._on_alarm_event)
            event_bus.register_handler("DeviceStatusEvent", self._on_device_status)
            self._handlers_registered = True
            logger.info("MQTT转发器已订阅EventBus事件")

        self._connect_task = asyncio.create_task(self._connect_loop(), name="mqtt-forward-connect")
        logger.info("MQTT北向转发器启动")

    def _init_ring_buffer(self, config: Any) -> None:
        """初始化 RingBuffer"""
        try:
            mqtt_cfg = getattr(config, "mqtt", None)
            capacity = getattr(mqtt_cfg, "ring_buffer_capacity", 50000) if mqtt_cfg else 50000
            compress = getattr(mqtt_cfg, "ring_buffer_compress", True) if mqtt_cfg else True
            self._ring_buffer = RingBuffer(capacity=capacity, compress=compress)
            logger.info("MQTT RingBuffer已初始化: capacity=%d, compress=%s", capacity, compress)

            # 从 SQLite 恢复未同步记录到 RingBuffer
            self._restore_ring_buffer_from_sqlite()
        except Exception as e:
            logger.warning("MQTT RingBuffer初始化失败，仅使用SQLite: %s", e)
            self._ring_buffer = None

    def _restore_ring_buffer_from_sqlite(self) -> None:
        """从 SQLite offline_queue 恢复 pending 记录到 RingBuffer"""
        if not self._ring_buffer or not self._offline_db:
            return
        # FIXED-P2: 使用同步锁保护SQLite并发访问（asyncio.Lock不能在同步方法中使用）
        _held_sync_lock = False
        if hasattr(self, "_offline_db_sync_lock") and self._offline_db_sync_lock:
            self._offline_db_sync_lock.acquire()
            _held_sync_lock = True
        try:
            rows = self._offline_db.execute(
                "SELECT id, topic, payload, qos, priority FROM offline_queue WHERE status='pending' LIMIT 50000"
            ).fetchall()
            if not rows:
                return
            skipped = 0
            for row_id, topic, payload, qos, priority in rows:
                ok = self._ring_buffer.put_sync(
                    {  # FIXED-P2: 检查返回值，满时跳过而非静默丢弃
                        "topic": topic,
                        "payload": payload,
                        "qos": qos,
                        "priority": priority or "",
                        "sqlite_id": row_id,
                    }
                )
                if not ok:
                    skipped += 1
            if skipped:
                logger.warning("RingBuffer full, skipped %d/%d messages from SQLite", skipped, len(rows))
            logger.info("从SQLite恢复%d条离线消息到RingBuffer", len(rows))
        except Exception as e:
            logger.error("从SQLite恢复到RingBuffer失败: %s", e)
        finally:
            if _held_sync_lock:
                try:
                    self._offline_db_sync_lock.release()
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("释放离线数据库同步锁失败: %s", e)

    async def stop(self) -> None:
        """停止 MQTT 转发器：断开连接、注销处理器、清理后台任务。"""
        self._running = False

        if self._handlers_registered and self._event_bus:
            self._event_bus.unregister_handler("PointUpdateEvent", self._on_point_update)
            self._event_bus.unregister_handler("AlarmEvent", self._on_alarm_event)
            self._event_bus.unregister_handler("DeviceStatusEvent", self._on_device_status)
            self._handlers_registered = False

        if self._replay_task and not self._replay_task.done():
            self._replay_task.cancel()
        if self._replay_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._replay_task

        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._connect_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._connect_task

        if self._offline_db:
            self._offline_db.close()
            self._offline_db = None

        self._connected = False
        logger.info("MQTT北向转发器停止")

    async def _on_point_update(self, event: Any) -> None:
        if not self._pub_queue:
            return
        try:
            data = {
                "type": "point_update",
                "device_id": getattr(event, "device_id", ""),
                "point_name": getattr(event, "point_name", ""),
                "value": getattr(event, "value", 0),
                "quality": getattr(event, "quality", "good"),
                "timestamp": time.time(),
                "msg_id": str(uuid.uuid4()),  # FIXED-P2: 重连恢复后消息可能被重复发送，Consumer按msg_id去重
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
            # FIXED-P2: 队列满时尝试写入离线队列，避免消息永久丢失
            if self._offline_cache_enabled:
                try:
                    await self._persist_message_from_data(data)
                except Exception as e:
                    logger.warning("MQTT转发队列满且离线持久化失败: %s", e)
            else:
                logger.warning("MQTT转发队列已满，丢弃消息")
        # FIXED-P0: 删除错误构建的alarm消息——_on_point_update只处理point_update，alarm由_on_alarm_event处理

    async def _on_alarm_event(self, event: Any) -> None:
        # FIXED-P0: 恢复上一轮误删的_on_alarm_event方法
        if not self._pub_queue:
            return
        try:
            data = {
                "type": "alarm",
                "alarm_id": getattr(event, "alarm_id", ""),
                "device_id": getattr(event, "device_id", ""),
                "severity": getattr(event, "severity", ""),
                "action": getattr(event, "action", "firing"),
                "timestamp": time.time(),
                "msg_id": str(uuid.uuid4()),
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
            if self._offline_cache_enabled:
                try:
                    await self._persist_message_from_data(data)
                except Exception as e:
                    logger.warning("MQTT转发队列满且离线持久化失败: %s", e)
            else:
                logger.warning("MQTT转发队列已满，丢弃消息")

    async def _on_device_status(self, event: Any) -> None:
        if not self._pub_queue:
            return
        try:
            data = {
                "type": "device_status",
                "device_id": getattr(event, "device_id", ""),
                "old_status": getattr(event, "old_status", ""),
                "new_status": getattr(event, "new_status", ""),
                "timestamp": time.time(),
                "msg_id": str(uuid.uuid4()),  # FIXED-P2: 重连恢复后设备状态消息可能被重复发送，Consumer按msg_id去重
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
            if self._offline_cache_enabled:
                try:
                    await self._persist_message_from_data(data)
                except Exception as e:
                    logger.warning("MQTT转发队列满且离线持久化失败: %s", e)
            else:
                logger.warning("MQTT转发队列已满，丢弃消息")

    def _build_ssl_context(self, config: Any) -> ssl.SSLContext | None:
        """根据配置构建 SSL/TLS 上下文"""
        mqtt_tls = getattr(config, "mqtt_tls", None)
        if not mqtt_tls or not getattr(mqtt_tls, "enabled", False):
            return None

        cert_reqs_map = {
            "none": ssl.CERT_NONE,
            "optional": ssl.CERT_OPTIONAL,
            "required": ssl.CERT_REQUIRED,
        }
        cert_reqs_value = getattr(mqtt_tls, "cert_reqs", "required")
        # FIXED(安全) R5-S-09: cert_reqs=none 禁用证书验证存在中间人攻击风险，
        # 必须显式设置 EDGELITE_ALLOW_INSECURE_TLS=1 才允许，防止生产环境误配置
        if cert_reqs_value == "none" and os.getenv("EDGELITE_ALLOW_INSECURE_TLS") != "1":
            raise ValueError(
                "cert_reqs=none prohibited, set EDGELITE_ALLOW_INSECURE_TLS=1 to override "
                "(NOT recommended for production)"
            )
        cert_reqs = cert_reqs_map.get(cert_reqs_value, ssl.CERT_REQUIRED)

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = cert_reqs == ssl.CERT_REQUIRED
        ssl_ctx.verify_mode = cert_reqs

        ca_cert = getattr(mqtt_tls, "ca_cert", "")
        if ca_cert and Path(ca_cert).exists():
            ssl_ctx.load_verify_locations(ca_cert)

        client_cert = getattr(mqtt_tls, "client_cert", "")
        client_key = getattr(mqtt_tls, "client_key", "")
        if client_cert and client_key and Path(client_cert).exists() and Path(client_key).exists():
            ssl_ctx.load_cert_chain(client_cert, client_key)

        return ssl_ctx

    async def _connect_loop(self) -> None:
        config = get_config()
        broker_host = config.mqtt.broker

        # FIXED(安全) R5-S-06: SSRF 防护 - 校验 broker 地址，拦截 loopback/link_local 等危险地址
        # 与 drivers/mqtt_client.py 的 _connect_loop 保持一致的安全策略
        if not _is_broker_host_safe(broker_host):
            logger.error(
                "MQTT转发器 code=SSRF_BLOCKED broker address blocked by SSRF protection: %s",
                broker_host,
            )
            self._running = False
            return

        self._host = broker_host
        self._port = config.mqtt.port

        while self._running:
            # FIXED: 每次重连时重新检测内置MQTT Server状态
            # 原问题：只在启动时检测一次，如果内置Server后来才启用，Forwarder不会自动切换端口
            broker_port = config.mqtt.port
            mqtt_server_cfg = getattr(config, "mqtt_server", None)
            if mqtt_server_cfg and getattr(mqtt_server_cfg, "enabled", False):
                server_host = getattr(mqtt_server_cfg, "host", "127.0.0.1")
                server_port = getattr(mqtt_server_cfg, "port", 1888)
                # 如果broker指向localhost且端口是默认1883，自动切换到内置服务器端口
                if broker_host in ("localhost", "127.0.0.1") and broker_port == 1883:
                    if self._port != server_port:
                        logger.info(
                            "MQTT转发器检测到内置MQTT Server，自动切换 %s:%d -> %s:%d",
                            broker_host,
                            broker_port,
                            server_host,
                            server_port,
                        )
                    broker_host = server_host
                    broker_port = server_port
            self._host = broker_host
            self._port = broker_port

            try:
                import aiomqtt

                ssl_ctx = self._build_ssl_context(config)

                connect_kwargs: dict[str, Any] = {
                    "hostname": broker_host,
                    "port": broker_port,
                    "username": config.mqtt.username or None,
                    "password": config.mqtt.password or None,
                    "keepalive": _MQTT_KEEPALIVE,
                }
                if ssl_ctx:
                    connect_kwargs["tls_params"] = aiomqtt.TLSParameters(ssl_context=ssl_ctx)  # type: ignore[call-arg]

                async with aiomqtt.Client(**connect_kwargs) as client:
                    self._connected = True
                    self._consecutive_failures = 0
                    logger.info("MQTT转发器连接成功: %s:%d", broker_host, broker_port)

                    if self._offline_queue:
                        try:

                            async def _offline_send(topic: str, payload: Any) -> bool:
                                if not self._connected:
                                    return False
                                await client.publish(
                                    topic, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), qos=1
                                )
                                return True

                            flushed = await self._offline_queue.flush(send_callback=_offline_send)  # type: ignore[arg-type]
                            if flushed > 0:
                                logger.info("OfflineQueue重传完成: %d条", flushed)
                        except Exception as e:
                            logger.warning("OfflineQueue flush失败: %s", e)

                    if self._offline_cache_enabled and not self._replay_task:
                        pending = self._get_pending_count()
                        ring_pending = self._ring_buffer.get_stats()["pending"] if self._ring_buffer else 0
                        total_pending = pending + ring_pending
                        if total_pending > 0:
                            logger.info(
                                "离线队列有%d条待重传数据(RingBuffer=%d, SQLite=%d)，启动重传",
                                total_pending,
                                ring_pending,
                                pending,
                            )
                        self._replay_task = asyncio.create_task(
                            self._replay_offline_queue(client), name="mqtt-replay-offline"
                        )

                    pub_task = asyncio.create_task(self._publish_loop(client), name="mqtt-forward-publish")

                    try:
                        while self._running:
                            await asyncio.sleep(1)
                    finally:
                        if not pub_task.done():
                            pub_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pub_task
                        if self._replay_task and not self._replay_task.done():
                            self._replay_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await self._replay_task
                        self._replay_task = None
                        self._connected = False

            except asyncio.CancelledError:
                raise
            except ImportError:
                logger.error("aiomqtt未安装，MQTT转发不可用")
                await asyncio.sleep(30)
            except Exception as e:
                err_str = str(e)
                self._consecutive_failures += 1
                # FIXED: 连续失败过多时延长重试间隔，避免日志刷屏和资源浪费
                delay: float
                if self._consecutive_failures > 100:
                    delay = 300  # 超过100次失败，每5分钟重试一次
                else:
                    delay = min(5 * (2 ** (self._consecutive_failures - 1)), 60)
                delay *= 0.5 + random.random() * 0.5  # FIXED-P4: 原问题-退避无抖动，多实例同时重连惊群效应
                now = time.time()
                # FIXED: 日志限流优化 - 前3次每次记录，之后每5分钟记录一次
                should_log = self._consecutive_failures <= 3 or now - self._last_connect_fail_log_time >= 300
                if should_log:
                    self._last_connect_fail_log_time = now
                    if "Connection refused" in err_str or "Errno 111" in err_str:
                        logger.warning(
                            "MQTT Broker未可达(%s:%d)，%d秒后重试(连续失败%d次)",
                            broker_host,
                            broker_port,
                            delay,
                            self._consecutive_failures,
                        )
                    else:
                        logger.error(
                            "MQTT转发器连接异常: %s，%d秒后重试(连续失败%d次)",
                            e,
                            delay,
                            self._consecutive_failures,
                        )
                self._connected = False
                await asyncio.sleep(delay)

    async def _publish_loop(self, client: Any) -> None:
        try:
            while self._running:
                if self._pub_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    data = await asyncio.wait_for(self._pub_queue.get(), timeout=_QUEUE_POLL_TIMEOUT)
                except TimeoutError:
                    continue

                msg_persisted = False  # FIXED-P2: 断连期间防止同一消息被重复持久化

                try:
                    if not self._connected:
                        if self._offline_cache_enabled and not msg_persisted:
                            await self._persist_message_from_data(data)
                            msg_persisted = True
                        if self._offline_queue and not msg_persisted:
                            config = get_config()
                            topic_prefix = config.mqtt.topic_prefix
                            msg_type = data.get("type", "unknown")
                            device_id = data.get("device_id", "")
                            # FIXED(安全) R5-S-07: 脱敏 device_id 防止 topic 注入
                            safe_device_id = _sanitize_topic_segment(device_id)
                            if msg_type == "point_update":
                                topic = f"{topic_prefix}/data/{safe_device_id}"
                            elif msg_type == "alarm":
                                topic = f"{topic_prefix}/alarm/{safe_device_id}"
                            elif msg_type == "device_status":
                                topic = f"{topic_prefix}/status/{safe_device_id}"
                            else:
                                topic = f"{topic_prefix}/misc"
                            await self._offline_queue.enqueue(topic, data)
                            msg_persisted = True
                        # FIXED-P1#19: 原 else 绑定到内层 if，导致持久化成功后仍 sleep(0.5) 浪费性能
                        # 改为仅在未持久化时 sleep
                        if not msg_persisted:
                            await asyncio.sleep(0.5)
                        continue

                    config = get_config()
                    topic_prefix = config.mqtt.topic_prefix

                    msg_type = data.get("type", "unknown")
                    device_id = data.get("device_id", "")

                    # FIXED(安全) R5-S-07: 脱敏 device_id 防止 topic 注入
                    safe_device_id = _sanitize_topic_segment(device_id)
                    if msg_type == "point_update":
                        topic = f"{topic_prefix}/data/{safe_device_id}"
                    elif msg_type == "alarm":
                        topic = f"{topic_prefix}/alarm/{safe_device_id}"
                    elif msg_type == "device_status":
                        topic = f"{topic_prefix}/status/{safe_device_id}"
                    else:
                        topic = f"{topic_prefix}/misc"

                    payload = json.dumps(data, ensure_ascii=False, default=str)
                    await client.publish(topic, payload.encode("utf-8"), qos=1)
                    self._sent_count += 1

                except Exception as e:
                    err_str = str(e)
                    if "not currently connected" in err_str:
                        self._connected = False
                        if self._offline_cache_enabled and not msg_persisted:
                            await self._persist_message_from_data(data)
                            msg_persisted = True
                        if self._offline_queue and not msg_persisted:
                            config = get_config()
                            topic_prefix = config.mqtt.topic_prefix
                            msg_type = data.get("type", "unknown")
                            device_id = data.get("device_id", "")
                            # FIXED(安全) R5-S-07: 脱敏 device_id 防止 topic 注入
                            safe_device_id = _sanitize_topic_segment(device_id)
                            if msg_type == "point_update":
                                topic = f"{topic_prefix}/data/{safe_device_id}"
                            elif msg_type == "alarm":
                                topic = f"{topic_prefix}/alarm/{safe_device_id}"
                            elif msg_type == "device_status":
                                topic = f"{topic_prefix}/status/{safe_device_id}"
                            else:
                                topic = f"{topic_prefix}/misc"
                            await self._offline_queue.enqueue(topic, data)
                            msg_persisted = True
                        logger.warning("MQTT连接已断开，等待重连...")
                    else:
                        logger.error("MQTT发布失败: %s", e)
        except asyncio.CancelledError:
            pass

    def _init_offline_db(self) -> None:
        db_path = Path(self._offline_db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._offline_db = sqlite3.connect(str(db_path), check_same_thread=False)
            try:
                self._offline_db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                logger.warning("WAL模式不可用，使用默认日志模式")
            self._offline_db.execute(self._OFFLINE_DB_TABLE)
            # 为旧表添加 priority 列（如果不存在）
            try:
                self._offline_db.execute("ALTER TABLE offline_queue ADD COLUMN priority TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
            self._offline_db.commit()
            logger.info("离线缓存数据库已初始化: %s", db_path)
        except sqlite3.OperationalError as e:
            logger.warning("离线缓存数据库初始化失败(%s)，离线缓存功能已禁用", e)
            self._offline_cache_enabled = False
            if self._offline_db:
                self._offline_db.close()
            self._offline_db = None

    async def _persist_message_from_data(self, data: dict) -> None:  # FIXED-P0: SQLite离线队列添加并发保护
        if not self._offline_db:
            return
        async with self._offline_db_lock:
            config = get_config()
            topic_prefix = config.mqtt.topic_prefix
            msg_type = data.get("type", "unknown")
            device_id = data.get("device_id", "")
            # FIXED(安全) R5-S-07: 脱敏 device_id 防止 topic 注入（/、+、# 等 MQTT 特殊字符）
            safe_device_id = _sanitize_topic_segment(device_id)
            if msg_type == "point_update":
                topic = f"{topic_prefix}/data/{safe_device_id}"
            elif msg_type == "alarm":
                topic = f"{topic_prefix}/alarm/{safe_device_id}"
            elif msg_type == "device_status":
                topic = f"{topic_prefix}/status/{safe_device_id}"
            else:
                topic = f"{topic_prefix}/misc"
            payload = json.dumps(data, ensure_ascii=False, default=str)
            priority = "alarm" if msg_type == "alarm" else ""
            await self._persist_message(topic, payload, qos=1, priority=priority, data=data)

    async def _persist_message(
        self, topic: str, payload: str, qos: int = 1, priority: str = "", data: dict | None = None
    ) -> None:  # FIXED-P2: 改为async以支持await _check_queue_capacity
        """持久化消息到 SQLite 和 RingBuffer"""
        # 写入 SQLite
        if self._offline_db:
            try:
                await self._check_queue_capacity()
                # FIXED(严重): 原问题-同步SQLite操作(execute/commit)阻塞事件循环;
                # 修复-用asyncio.to_thread包装同步DB操作
                sqlite_id = await asyncio.to_thread(self._persist_message_sync, topic, payload, qos, priority)
            except Exception as e:
                logger.error(
                    "离线消息持久化失败(磁盘满/权限/锁): %s", e
                )  # FIXED-P2: 磁盘满/权限错误静默吞没，导致断网期间数据永久丢失
                sqlite_id = None
        else:
            sqlite_id = None

        # 同时写入 RingBuffer
        if self._ring_buffer:
            try:
                record = {
                    "topic": topic,
                    "payload": payload,
                    "qos": qos,
                    "priority": priority,
                }
                if sqlite_id is not None:
                    record["sqlite_id"] = sqlite_id
                if data:
                    record["data"] = data
                ok = self._ring_buffer.put_sync(record)
                if not ok:  # FIXED-P2: RingBuffer满时记录告警，而非静默丢弃
                    logger.warning("RingBuffer full, message dropped: topic=%s", topic)
            except Exception as e:
                logger.error("RingBuffer写入失败: %s", e)  # FIXED-P2: RingBuffer写入异常静默吞没，消息可能在内存中丢失

    def _persist_message_sync(self, topic: str, payload: str, qos: int, priority: str) -> int | None:
        """同步执行SQLite INSERT，供asyncio.to_thread调用"""
        if not self._offline_db:
            return None
        with self._offline_db_sync_lock:
            cursor = self._offline_db.execute(
                "INSERT INTO offline_queue (topic, payload, qos, created_at, priority) VALUES (?, ?, ?, ?, ?)",
                (topic, payload, qos, time.time(), priority),
            )
            self._offline_db.commit()
            return cursor.lastrowid

    async def _check_queue_capacity(
        self,
    ) -> None:  # FIXED-P2: 改为async，统一到asyncio.Lock+asyncio.to_thread模式，消除混合锁类型
        if not self._offline_db:
            return
        await asyncio.to_thread(self._check_queue_capacity_sync)

    def _check_queue_capacity_sync(self) -> None:
        if not self._offline_db:
            return
        with self._offline_db_sync_lock:
            count = self._get_pending_count()
            if count >= self._max_queue_size:
                self._evict_oldest()

    def _evict_oldest(self) -> None:
        if not self._offline_db:
            return
        try:
            # FIXED: SQLite 不支持 DELETE ... ORDER BY ... LIMIT 语法，使用子查询删除最旧记录
            cursor = self._offline_db.execute(
                "DELETE FROM offline_queue WHERE id IN "
                "(SELECT id FROM offline_queue WHERE status='pending' ORDER BY created_at ASC LIMIT 1)"
            )
            self._offline_db.commit()
            if cursor.rowcount > 0:
                logger.warning("离线队列已满(max=%d)，丢弃最早数据", self._max_queue_size)
        except Exception as e:
            logger.error("离线队列淘汰失败: %s", e)  # FIXED-P2: W11 _evict_oldest由_check_queue_capacity在锁内调用

    def _get_pending_count(self) -> int:
        if not self._offline_db:
            return 0
        try:
            cursor = self._offline_db.execute("SELECT COUNT(*) FROM offline_queue WHERE status='pending'")
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0  # FIXED-P2: W11 _get_pending_count由_check_queue_capacity在锁内调用

    async def _replay_offline_queue(self, client: Any) -> None:
        """重传离线队列消息

        优先从 RingBuffer 获取 pending 记录（增量同步），
        告警消息优先重传。若 RingBuffer 不可用则回退到 SQLite 查询。
        """
        try:
            while self._running and self._connected:
                if self._ring_buffer:
                    replayed = await self._replay_from_ring_buffer(client)
                    if not replayed:
                        # RingBuffer 无数据，回退到 SQLite
                        await self._replay_from_sqlite(client)
                else:
                    await self._replay_from_sqlite(client)

                await asyncio.sleep(self._retry_interval)
        except asyncio.CancelledError:
            pass

    async def _replay_from_ring_buffer(self, client: Any) -> bool:
        """从 RingBuffer 增量同步重传消息"""
        if not self._ring_buffer:
            return False

        # 优先重传告警消息
        alarm_records = await self._ring_buffer.get_pending(limit=50, priority="alarm")
        normal_records = await self._ring_buffer.get_pending(limit=50)
        records = alarm_records + normal_records

        if not records:
            return False

        synced_ring_ids: list[int] = []
        synced_sqlite_ids: list[int] = []
        failed_ring_ids: list[int] = []

        for rec in records:
            if not self._connected or not self._running:
                # 中断时将已取出的 syncing 记录回退为 pending（断点续传）
                failed_ring_ids.extend(
                    cast(int, r["_id"])
                    for r in records
                    if r.get("_id") is not None and r.get("_id") not in synced_ring_ids
                )
                break
            try:
                topic = rec.get("topic", "")
                payload = rec.get("payload", "")
                qos = rec.get("qos", 1)
                await client.publish(topic, payload.encode("utf-8"), qos=qos)
                ring_id = rec.get("_id")
                if ring_id is not None:
                    synced_ring_ids.append(ring_id)
                sqlite_id = rec.get("sqlite_id")
                if sqlite_id is not None:
                    synced_sqlite_ids.append(sqlite_id)
                self._sent_count += 1
            except Exception as e:
                ring_id = rec.get("_id")
                if ring_id is not None:
                    failed_ring_ids.append(ring_id)
                logger.debug("离线消息重传失败(ring_id=%s): %s", ring_id, e)
                break

        # 标记已同步
        if synced_ring_ids:
            await self._ring_buffer.mark_synced(synced_ring_ids)
            # 同步删除 SQLite 中对应记录
            if synced_sqlite_ids and self._offline_db:
                async with self._offline_db_lock:  # FIXED-P1: SQLite删除操作加锁，与_persist_message_from_data互斥
                    # R11-ENG-03: SQLite execute/commit 是同步操作，用 to_thread 包装避免阻塞事件循环
                    def _delete_synced_records() -> None:
                        if not self._offline_db:
                            return
                        try:
                            self._offline_db.execute(
                                "DELETE FROM offline_queue WHERE id IN ({})".format(
                                    ",".join("?" * len(synced_sqlite_ids))
                                ),
                                synced_sqlite_ids,
                            )
                            self._offline_db.commit()
                        except Exception as e:
                            logger.error("删除已同步SQLite记录失败: %s", e)

                    await asyncio.to_thread(_delete_synced_records)

        # 标记失败回退为 pending（断点续传）
        if failed_ring_ids:
            await self._ring_buffer.mark_failed(failed_ring_ids)

        return len(records) > 0

    async def _replay_from_sqlite(self, client: Any) -> None:
        """从 SQLite 重传离线消息（回退路径）"""
        if not self._offline_db:
            await asyncio.sleep(self._retry_interval)
            return
        # FIXED-P1#18: 仅在读取和更新SQLite时持锁，publish网络IO期间不持锁，避免阻塞其他SQLite操作
        async with self._offline_db_lock:
            try:
                rows = self._offline_db.execute(
                    "SELECT id, topic, payload, qos, retry_count, priority FROM offline_queue "
                    "WHERE status='pending' AND retry_count<? ORDER BY priority='alarm' DESC, created_at ASC LIMIT 50",
                    (self._max_retries,),
                ).fetchall()
            except Exception as e:
                logger.error("查询离线队列失败: %s", e)
                return

        if not rows:
            return

        sent_ids: list[int] = []
        failed_ids: list[int] = []
        for row_id, topic, payload, qos, _retry_count, _priority in rows:
            if not self._connected or not self._running:
                break
            try:
                await client.publish(topic, payload.encode("utf-8"), qos=qos)
                sent_ids.append(row_id)
                self._sent_count += 1
            except Exception as e:
                failed_ids.append(row_id)
                logger.debug("离线消息重传失败(id=%d): %s", row_id, e)
                break

        # 更新SQLite状态（持锁）
        async with self._offline_db_lock:
            try:
                if sent_ids:
                    placeholders = ",".join("?" * len(sent_ids))
                    self._offline_db.execute(
                        f"UPDATE offline_queue SET status='sent' WHERE id IN ({placeholders})",
                        sent_ids,
                    )
                if failed_ids:
                    placeholders = ",".join("?" * len(failed_ids))
                    self._offline_db.execute(
                        f"UPDATE offline_queue SET retry_count=retry_count+1 WHERE id IN ({placeholders})",
                        failed_ids,
                    )
                self._offline_db.execute("DELETE FROM offline_queue WHERE status='sent'")
                self._offline_db.commit()
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("删除已发送SQLite离线消息失败: %s", e)

    def get_offline_queue_status(self) -> dict:
        """返回离线缓存队列的状态信息（启用状态、积压数量、环形缓冲统计）。"""
        ring_stats = self._ring_buffer.get_stats() if self._ring_buffer else None
        if not self._offline_db:
            return {
                "enabled": self._offline_cache_enabled,
                "pending_count": ring_stats["pending"] if ring_stats else 0,
                "sent_count": self._sent_count,
                "oldest_timestamp": None,
                "db_size_bytes": 0,
                "ring_buffer": ring_stats,
            }
        try:
            pending = self._get_pending_count()
            cursor = self._offline_db.execute("SELECT MIN(created_at) FROM offline_queue WHERE status='pending'")
            row = cursor.fetchone()
            oldest = row[0] if row and row[0] else None
            db_size = Path(self._offline_db_path).stat().st_size if Path(self._offline_db_path).exists() else 0
            return {
                "enabled": self._offline_cache_enabled,
                "pending_count": pending,
                "sent_count": self._sent_count,
                "oldest_timestamp": oldest,
                "db_size_bytes": db_size,
                "ring_buffer": ring_stats,
            }
        except Exception as e:
            logger.error("获取离线队列状态失败: %s", e)
            return {
                "enabled": self._offline_cache_enabled,
                "pending_count": 0,
                "sent_count": self._sent_count,
                "oldest_timestamp": None,
                "db_size_bytes": 0,
                "ring_buffer": ring_stats,
            }

    @property
    def is_connected(self) -> bool:
        """返回 MQTT broker 连接状态。"""
        return self._connected
