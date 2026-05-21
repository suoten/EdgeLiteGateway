"""北向MQTT数据转发 - 将采集数据/告警事件转发到MQTT Broker"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from edgelite.config import get_config
from edgelite.constants import _EVENT_BUS_MAX_QUEUE, _MQTT_KEEPALIVE, _MQTT_RECONNECT_DELAY, _MQTT_FORWARDER_RECONNECT, _QUEUE_POLL_TIMEOUT

logger = logging.getLogger(__name__)


class MqttForwarder:
    """北向MQTT数据转发器，订阅EventBus事件并转发到MQTT"""

    _OFFLINE_DB_TABLE = """CREATE TABLE IF NOT EXISTS offline_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        payload TEXT NOT NULL,
        qos INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL,
        retry_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending'
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

    async def start(self, event_bus: Any = None) -> None:
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

        if event_bus:
            self._event_bus = event_bus
            event_bus.register_handler("PointUpdateEvent", self._on_point_update)
            event_bus.register_handler("AlarmEvent", self._on_alarm_event)
            event_bus.register_handler("DeviceStatusEvent", self._on_device_status)
            self._handlers_registered = True
            logger.info("MQTT转发器已订阅EventBus事件")

        self._connect_task = asyncio.create_task(self._connect_loop(), name="mqtt-forward-connect")
        logger.info("MQTT北向转发器启动")

    async def stop(self) -> None:
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
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("MQTT转发队列已满，丢弃消息")

    async def _on_alarm_event(self, event: Any) -> None:
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
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
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
            }
            self._pub_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("MQTT转发队列已满，丢弃消息")

    async def _connect_loop(self) -> None:
        config = get_config()

        while self._running:
            try:
                import aiomqtt

                async with aiomqtt.Client(
                    hostname=config.mqtt.broker,
                    port=config.mqtt.port,
                    username=config.mqtt.username or None,
                    password=config.mqtt.password or None,
                    keepalive=_MQTT_KEEPALIVE,
                ) as client:
                    self._connected = True
                    logger.info("MQTT转发器连接成功: %s:%d", config.mqtt.broker, config.mqtt.port)

                    if self._offline_cache_enabled and not self._replay_task:
                        pending = self._get_pending_count()
                        if pending > 0:
                            logger.info("离线队列有%d条待重传数据，启动重传", pending)
                        self._replay_task = asyncio.create_task(
                            self._replay_offline_queue(client), name="mqtt-replay-offline"
                        )

                    pub_task = asyncio.create_task(
                        self._publish_loop(client), name="mqtt-forward-publish"
                    )

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
                if "Connection refused" in err_str or "Errno 111" in err_str:
                    logger.warning(
                        "MQTT Broker未可达(%s:%d)，5秒后重试", config.mqtt.broker, config.mqtt.port
                    )
                else:
                    logger.error("MQTT转发器连接异常: %s，5秒后重试", e)
                self._connected = False
                await asyncio.sleep(5)

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

                try:
                    if not self._connected:
                        if self._offline_cache_enabled:
                            self._persist_message_from_data(data)
                        else:
                            await asyncio.sleep(0.5)
                        continue

                    config = get_config()
                    topic_prefix = config.mqtt.topic_prefix

                    msg_type = data.get("type", "unknown")
                    device_id = data.get("device_id", "")

                    if msg_type == "point_update":
                        topic = f"{topic_prefix}/data/{device_id}"
                    elif msg_type == "alarm":
                        topic = f"{topic_prefix}/alarm/{device_id}"
                    elif msg_type == "device_status":
                        topic = f"{topic_prefix}/status/{device_id}"
                    else:
                        topic = f"{topic_prefix}/misc"

                    payload = json.dumps(data, ensure_ascii=False, default=str)
                    await client.publish(topic, payload.encode("utf-8"), qos=1)
                    self._sent_count += 1

                except Exception as e:
                    err_str = str(e)
                    if "not currently connected" in err_str:
                        self._connected = False
                        if self._offline_cache_enabled:
                            self._persist_message_from_data(data)
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
            self._offline_db.commit()
            logger.info("离线缓存数据库已初始化: %s", db_path)
        except sqlite3.OperationalError as e:
            logger.warning("离线缓存数据库初始化失败(%s)，离线缓存功能已禁用", e)
            self._offline_cache_enabled = False
            if self._offline_db:
                self._offline_db.close()
            self._offline_db = None

    def _persist_message_from_data(self, data: dict) -> None:
        if not self._offline_db:
            return
        config = get_config()
        topic_prefix = config.mqtt.topic_prefix
        msg_type = data.get("type", "unknown")
        device_id = data.get("device_id", "")
        if msg_type == "point_update":
            topic = f"{topic_prefix}/data/{device_id}"
        elif msg_type == "alarm":
            topic = f"{topic_prefix}/alarm/{device_id}"
        elif msg_type == "device_status":
            topic = f"{topic_prefix}/status/{device_id}"
        else:
            topic = f"{topic_prefix}/misc"
        payload = json.dumps(data, ensure_ascii=False, default=str)
        self._persist_message(topic, payload, qos=1)

    def _persist_message(self, topic: str, payload: str, qos: int = 1) -> None:
        if not self._offline_db:
            return
        try:
            self._check_queue_capacity()
            self._offline_db.execute(
                "INSERT INTO offline_queue (topic, payload, qos, created_at) VALUES (?, ?, ?, ?)",
                (topic, payload, qos, time.time()),
            )
            self._offline_db.commit()
        except Exception as e:
            logger.error("离线消息持久化失败: %s", e)

    def _check_queue_capacity(self) -> None:
        if not self._offline_db:
            return
        count = self._get_pending_count()
        if count >= self._max_queue_size:
            self._evict_oldest()

    def _evict_oldest(self) -> None:
        if not self._offline_db:
            return
        try:
            cursor = self._offline_db.execute(
                "DELETE FROM offline_queue WHERE status='pending' ORDER BY created_at ASC LIMIT 1"
            )
            self._offline_db.commit()
            if cursor.rowcount > 0:
                logger.warning("离线队列已满(max=%d)，丢弃最早数据", self._max_queue_size)
        except Exception as e:
            logger.error("离线队列淘汰失败: %s", e)

    def _get_pending_count(self) -> int:
        if not self._offline_db:
            return 0
        try:
            cursor = self._offline_db.execute(
                "SELECT COUNT(*) FROM offline_queue WHERE status='pending'"
            )
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0

    async def _replay_offline_queue(self, client: Any) -> None:
        try:
            while self._running and self._connected:
                if not self._offline_db:
                    await asyncio.sleep(self._retry_interval)
                    continue
                try:
                    rows = self._offline_db.execute(
                        "SELECT id, topic, payload, qos, retry_count FROM offline_queue "
                        "WHERE status='pending' AND retry_count<? ORDER BY created_at ASC LIMIT 50",
                        (self._max_retries,),
                    ).fetchall()
                except Exception as e:
                    logger.error("查询离线队列失败: %s", e)
                    await asyncio.sleep(self._retry_interval)
                    continue

                if not rows:
                    await asyncio.sleep(self._retry_interval)
                    continue

                for row_id, topic, payload, qos, retry_count in rows:
                    if not self._connected or not self._running:
                        break
                    try:
                        await client.publish(topic, payload.encode("utf-8"), qos=qos)
                        self._offline_db.execute(
                            "UPDATE offline_queue SET status='sent' WHERE id=?", (row_id,)
                        )
                        self._offline_db.commit()
                        self._sent_count += 1
                    except Exception as e:
                        self._offline_db.execute(
                            "UPDATE offline_queue SET retry_count=retry_count+1 WHERE id=?",
                            (row_id,),
                        )
                        self._offline_db.commit()
                        logger.debug("离线消息重传失败(id=%d): %s", row_id, e)
                        break

                try:
                    self._offline_db.execute(
                        "DELETE FROM offline_queue WHERE status='sent'"
                    )
                    self._offline_db.commit()
                except Exception:
                    pass

                await asyncio.sleep(self._retry_interval)
        except asyncio.CancelledError:
            pass

    def get_offline_queue_status(self) -> dict:
        if not self._offline_db:
            return {
                "enabled": self._offline_cache_enabled,
                "pending_count": 0,
                "sent_count": self._sent_count,
                "oldest_timestamp": None,
                "db_size_bytes": 0,
            }
        try:
            pending = self._get_pending_count()
            cursor = self._offline_db.execute(
                "SELECT MIN(created_at) FROM offline_queue WHERE status='pending'"
            )
            row = cursor.fetchone()
            oldest = row[0] if row and row[0] else None
            db_size = Path(self._offline_db_path).stat().st_size if Path(self._offline_db_path).exists() else 0
            return {
                "enabled": self._offline_cache_enabled,
                "pending_count": pending,
                "sent_count": self._sent_count,
                "oldest_timestamp": oldest,
                "db_size_bytes": db_size,
            }
        except Exception as e:
            logger.error("获取离线队列状态失败: %s", e)
            return {
                "enabled": self._offline_cache_enabled,
                "pending_count": 0,
                "sent_count": self._sent_count,
                "oldest_timestamp": None,
                "db_size_bytes": 0,
            }

    @property
    def is_connected(self) -> bool:
        return self._connected
