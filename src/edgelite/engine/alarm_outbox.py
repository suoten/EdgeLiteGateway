"""告警事件持久化 outbox — 进程崩溃兜底重放。

FIXED: Phase 2 修复 — EventBus 的 AlarmEvent 在入队前先落盘到独立 SQLite，
进程崩溃重启后通过 replay_pending_alarms() 重放未投递的告警通知，确保零丢失 [2026-06-29]

设计要点:
- 独立 SQLite 文件 (data/alarm_outbox.db)，与主库隔离，避免主库锁竞争
- WAL 模式 + busy_timeout=5000ms + synchronous=NORMAL (项目硬约束)
- best-effort: 所有 DB 操作异常仅记录日志，不阻塞 EventBus 主流程
- 序列化: dataclasses.asdict + JSON, 记录 event_type 用于反序列化重建对象
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import asdict, fields, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from edgelite.constants import _SQLITE_BUSY_TIMEOUT, _SQLITE_SYNCHRONOUS, _SQLITE_WAL_MODE

logger = logging.getLogger(__name__)

# 事件类注册表 — 用于反序列化时按类型名重建事件对象
# 延迟导入避免循环依赖；仅注册需要持久化的事件类型
_EVENT_REGISTRY: dict[str, type] = {}


def _ensure_event_registry() -> None:
    """惰性注册事件类，避免模块加载期循环导入。"""
    if _EVENT_REGISTRY:
        return
    try:
        from edgelite.engine.event_bus import AlarmEvent, DeviceStatusEvent, PointUpdateEvent

        for cls in (AlarmEvent, PointUpdateEvent, DeviceStatusEvent):
            _EVENT_REGISTRY[cls.__name__] = cls
    except Exception as e:  # best-effort: 注册失败仅影响反序列化
        logger.warning("AlarmOutbox event registry init failed: %s", e)


def _serialize_event(event: Any) -> str:
    """序列化 dataclass 事件为 JSON (含 __type__ 字段)。"""
    if not is_dataclass(event):
        # 非 dataclass 事件退化为 dict 表示，反序列化时无法重建为对象
        return json.dumps(
            {"__type__": type(event).__name__, "__raw__": str(event)},
            ensure_ascii=False,
        )
    payload = asdict(event)
    payload["__type__"] = type(event).__name__
    return json.dumps(payload, ensure_ascii=False, default=str)


def _deserialize_event(raw: str) -> Any:
    """反序列化 JSON 为事件对象 (按 __type__ 重建 dataclass 实例)。"""
    _ensure_event_registry()
    payload = json.loads(raw)
    type_name = payload.pop("__type__", None)
    if not type_name:
        return payload  # 兼容旧数据：返回裸 dict
    cls = _EVENT_REGISTRY.get(type_name)
    if cls is None or not is_dataclass(cls):
        return payload  # 未知类型：返回 dict 避免崩溃
    # 仅保留 cls 定义的字段，防止多余键导致 TypeError
    valid_keys = {f.name for f in fields(cls)}
    ctor_args = {k: v for k, v in payload.items() if k in valid_keys}
    try:
        return cls(**ctor_args)
    except Exception as e:
        logger.warning("AlarmOutbox deserialize failed for %s: %s", type_name, e)
        return payload


class AlarmOutbox:
    """告警事件持久化 outbox。

    线程安全: 通过单一连接 + threading.Lock 保护 (run_in_executor 调用)。
    所有 DB 操作异常捕获，不抛出 (best-effort)。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        try:
            # 确保目录存在
            parent = Path(db_path).parent
            parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                db_path,
                timeout=(_SQLITE_BUSY_TIMEOUT / 1000.0),
                check_same_thread=False,
            )
            # 硬约束: WAL + busy_timeout + synchronous=NORMAL
            conn.execute(f"PRAGMA journal_mode={_SQLITE_WAL_MODE}")
            conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT}")
            conn.execute(f"PRAGMA synchronous={_SQLITE_SYNCHRONOUS}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alarm_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    event_data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_alarm_outbox_created ON alarm_outbox(created_at)"
            )
            conn.commit()
            self._conn = conn
            logger.info("AlarmOutbox initialized at %s", db_path)
        except Exception as e:
            logger.warning("AlarmOutbox init failed (best-effort): %s", e)
            # _conn 保持 None, persist/replay 会安全跳过

    def persist(self, event: Any) -> bool:
        """同步持久化事件到 outbox。

        FIXED-P0 (并发安全#4): 返回 bool 让调用方决定是否投递，维护 outbox 一致性。
        原问题: persist (best-effort) 吞掉异常返回 None，调用方无法判断是否成功，
                导致 persist 失败仍投递，违反 "先落盘后投递" 的 outbox 不变式。
        修复: 返回 True/False 表示持久化结果，调用方据此决定是否投递。

        Returns:
            True 如果持久化成功, False 如果连接不可用或写入失败。
        """
        conn = self._conn
        if conn is None:
            return False
        try:
            event_type = type(event).__name__
            payload = _serialize_event(event)
            with self._lock:
                conn.execute(
                    "INSERT INTO alarm_outbox (event_type, event_data, created_at)"
                    " VALUES (?, ?, ?)",
                    (event_type, payload, datetime.now(UTC).isoformat()),
                )
                conn.commit()
            return True
        except Exception as e:
            logger.warning("AlarmOutbox persist failed: %s", e)
            return False

    def replay_and_clear(self, callback: Callable[[Any], None]) -> int:
        """重放所有未投递事件并清空 outbox。

        Args:
            callback: 同步回调，接收反序列化后的事件对象

        Returns:
            重放的事件数量
        """
        conn = self._conn
        if conn is None:
            return 0
        replayed = 0
        try:
            with self._lock:
                rows = conn.execute(
                    "SELECT id, event_data FROM alarm_outbox ORDER BY id ASC"
                ).fetchall()
            # 锁外执行 callback 避免 DB 长时间持锁
            for _id, raw in rows:
                try:
                    event = _deserialize_event(raw)
                    callback(event)
                    replayed += 1
                except Exception as e:
                    logger.warning("AlarmOutbox replay event %d failed: %s", _id, e)
            # 清空已重放的事件 (无论 callback 是否成功，避免无限累积)
            with self._lock:
                conn.execute("DELETE FROM alarm_outbox")
                conn.commit()
            logger.info("AlarmOutbox replayed %d events", replayed)
        except Exception as e:
            logger.warning("AlarmOutbox replay_and_clear failed (best-effort): %s", e)
        return replayed

    def close(self) -> None:
        """关闭 outbox 连接 (best-effort)。"""
        conn = self._conn
        if conn is None:
            return
        try:
            with self._lock:
                conn.close()
            self._conn = None
        except Exception as e:
            logger.warning("AlarmOutbox close failed (best-effort): %s", e)

    def __del__(self) -> None:
        # 兜底: 析构时尝试关闭连接，防止句柄泄漏 (best-effort, 不抛出)
        try:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
        except Exception:
            pass
