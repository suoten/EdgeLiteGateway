"""Device lifecycle manager with SQLite persistence"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from edgelite.engine.event_bus import DeviceStatusEvent, EventBus
from edgelite.storage.sqlite_pragmas import apply_standard_pragmas, check_and_convert_to_wal

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = "data/device_status.db"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS device_status (
    device_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'offline',
    updated_at REAL NOT NULL
)
"""

_UPSERT_SQL = """
INSERT INTO device_status (device_id, status, updated_at)
VALUES (?, ?, ?)
ON CONFLICT(device_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at
"""

_SELECT_ALL_SQL = "SELECT device_id, status FROM device_status"

_DELETE_SQL = "DELETE FROM device_status WHERE device_id = ?"


class DeviceLifecycleManager:
    """Device lifecycle manager with SQLite persistence"""

    def __init__(self, event_bus: EventBus, db_path: str | None = None):
        """Initialize DeviceLifecycleManager.

        Single lock architecture: _sqlite_lock protects all shared state
        (_status_map, _db_conn) for cross-thread safety.
        """
        self._event_bus = event_bus
        self._status_map: dict[str, str] = {}
        self._db_path = db_path or self._resolve_db_path()
        self._db_conn = None
        # FIXED-P2: 简化单锁设计 - _sqlite_lock 保护所有共享状态
        # 消除 _db_lock + _sqlite_lock 嵌套锁，避免死锁风险
        self._sqlite_lock = threading.RLock()
        self._restore_statuses()

    def _resolve_db_path(self) -> str:
        try:
            from edgelite.config import get_config

            config = get_config()
            sqlite_path = getattr(config.database, "sqlite_path", "")
            if sqlite_path:
                return str(Path(sqlite_path).parent / "device_status.db")
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("解析数据库路径配置失败: %s", e)
        return _DEFAULT_DB_PATH

    def _get_conn(self):
        # FIXED-P0: 原问题-_get_conn无锁保护，多协程并发调用可创建多个连接导致泄漏；加sqlite_lock保护
        with self._sqlite_lock:
            if self._db_conn is not None:
                return self._db_conn
            try:
                import sqlite3

                # FIXED-SQLITE-PRAGMA: Check and convert to WAL mode if needed
                check_and_convert_to_wal(self._db_path)

                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
                self._db_conn = sqlite3.connect(self._db_path, check_same_thread=False)
                # FIXED-SQLITE-PRAGMA: Apply standard pragmas for reliable concurrent access
                apply_standard_pragmas(self._db_conn)
                self._db_conn.execute(_CREATE_TABLE_SQL)
                self._db_conn.commit()
            except Exception as e:
                logger.error("Failed to open device_status database: %s", e)
                self._db_conn = None
            return self._db_conn

    def _restore_statuses(self) -> None:
        conn = self._get_conn()
        if conn is None:
            return
        try:
            cursor = conn.execute(_SELECT_ALL_SQL)
            for row in cursor.fetchall():
                self._status_map[row[0]] = row[1]
            logger.info("Restored %d device statuses from SQLite", len(self._status_map))
        except Exception as e:
            logger.error("Failed to restore device statuses: %s", e)

    async def _persist_status(self, device_id: str, status: str) -> None:
        """Persist device status to SQLite (thread-safe via _sqlite_lock)."""
        import time

        def _do_persist():
            with self._sqlite_lock:
                conn = self._get_conn()
                if conn is None:
                    return
                conn.execute(_UPSERT_SQL, (device_id, status, time.time()))
                conn.commit()

        try:
            await asyncio.to_thread(_do_persist)
        except Exception as e:
            logger.error("Failed to persist device status for %s: %s", device_id, e)
            raise

    async def _delete_status(self, device_id: str) -> None:
        """Delete device status from SQLite (thread-safe via _sqlite_lock)."""

        def _do_delete():
            with self._sqlite_lock:
                conn = self._get_conn()
                if conn is None:
                    return
                conn.execute(_DELETE_SQL, (device_id,))
                conn.commit()

        try:
            await asyncio.to_thread(_do_delete)
        except Exception as e:
            logger.error("Failed to delete device status for %s: %s", device_id, e)
            raise

    async def _transition_status(self, device_id: str, new_status: str) -> None:
        """Common status transition logic (idempotent, thread-safe).

        Publishes event on success, publishes correction event on rollback.
        """

        def _do_transition():
            with self._sqlite_lock:
                old_status = self._status_map.get(device_id, "offline")
                if old_status == new_status:
                    return None, None  # No change, no event
                self._status_map[device_id] = new_status
                event = DeviceStatusEvent(
                    device_id=device_id,
                    old_status=old_status,
                    new_status=new_status,
                )
                return old_status, event

        old_status, event = await asyncio.to_thread(_do_transition)
        if event is None:
            return
        try:
            await self._persist_status(device_id, event.new_status)
        except Exception as e:
            logger.warning("Persist status failed, rolling back: %s", e)
            correction_event = DeviceStatusEvent(
                device_id=device_id,
                old_status=event.new_status,  # Tried status
                new_status=old_status,  # Rolled back status
            )
            await asyncio.to_thread(lambda: self._status_map.__setitem__(device_id, old_status))
            try:
                await self._event_bus.publish(correction_event)
            except Exception as e:
                logger.warning("Failed to publish correction event for device %s: %s", device_id, e)
            raise
        try:
            await self._event_bus.publish(event)
        except Exception as e:
            logger.exception("EventBus publish device status event failed: %s", device_id, exc_info=e)

    async def on_device_online(self, device_id: str) -> None:
        """将设备状态切换为 online 并发布事件。"""
        await self._transition_status(device_id, "online")

    async def on_device_offline(self, device_id: str) -> None:
        """将设备状态切换为 offline 并发布事件。"""
        await self._transition_status(device_id, "offline")

    async def on_device_unknown(self, device_id: str) -> None:
        """将设备状态切换为 unknown 并发布事件。"""
        await self._transition_status(device_id, "unknown")

    async def get_status(self, device_id: str) -> str:
        """获取设备的当前生命周期状态（online/offline/unknown）。"""
        def _do_get():
            with self._sqlite_lock:
                return self._status_map.get(device_id, "offline")

        return await asyncio.to_thread(_do_get)

    async def remove_device(self, device_id: str) -> None:
        """从生命周期管理中移除设备（删除内存和 SQLite 中的状态记录）。"""
        await self._delete_status(device_id)

        def _do_remove():
            with self._sqlite_lock:
                self._status_map.pop(device_id, None)

        await asyncio.to_thread(_do_remove)

    async def close(self) -> None:
        """关闭 SQLite 连接并释放资源。"""
        def _do_close():
            with self._sqlite_lock:
                if self._db_conn:
                    try:
                        self._db_conn.close()
                    except Exception as e:
                        logger.warning("关闭SQLite连接失败: %s", e)
                    self._db_conn = None

        await asyncio.to_thread(_do_close)
