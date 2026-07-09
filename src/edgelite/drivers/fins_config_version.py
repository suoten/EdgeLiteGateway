"""FINS 驱动配置版本管理 - 基于 aiosqlite 的异步版本控制

为欧姆龙 FINS 协议驱动提供配置版本管理能力：
- 保存配置快照（每次 save_version 自增版本号）
- 查询历史版本列表与指定版本配置
- 回滚到目标版本
- 配置变更审计轨迹
- 两版本差异对比

关键差异（相对 OPC UA/S7）：
- get_versions 接收 2 个参数（device_id, limit），而非 1 个
- diff_versions 为 async 方法（OPC UA/S7 为 sync）
- stop() 为 async 方法（fins.py:1136 需要 await）

实现：aiosqlite 两张表，惰性初始化连接。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = "data/fins_config_versions.db"

_CREATE_VERSIONS_TABLE = """
CREATE TABLE IF NOT EXISTS config_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    config_json TEXT NOT NULL,
    change_summary TEXT NOT NULL DEFAULT '',
    operator TEXT NOT NULL DEFAULT 'system',
    created_at REAL NOT NULL,
    UNIQUE(device_id, version)
)
"""

_CREATE_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS config_audit_trail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    version INTEGER,
    action TEXT NOT NULL,
    operator TEXT NOT NULL DEFAULT 'system',
    details TEXT,
    created_at REAL NOT NULL
)
"""

_CREATE_INDEX_DEVICE = """
CREATE INDEX IF NOT EXISTS idx_versions_device
ON config_versions (device_id, version DESC)
"""

_CREATE_INDEX_AUDIT_DEVICE = """
CREATE INDEX IF NOT EXISTS idx_audit_device
ON config_audit_trail (device_id, created_at DESC)
"""


class FinsConfigVersionManager:
    """FINS 配置版本管理器（全异步实现）。

    使用 aiosqlite 持久化配置快照，连接惰性初始化（首次操作时建立）。
    每台设备维护独立的版本号序列（从 1 开始自增）。
    """

    def __init__(self) -> None:
        self._db_path = _DB_PATH
        self._db: aiosqlite.Connection | None = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        """惰性初始化数据库连接与表结构。"""
        if self._db is not None:
            return self._db
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute(_CREATE_VERSIONS_TABLE)
        await self._db.execute(_CREATE_AUDIT_TABLE)
        await self._db.execute(_CREATE_INDEX_DEVICE)
        await self._db.execute(_CREATE_INDEX_AUDIT_DEVICE)
        await self._db.commit()
        logger.info("[fins-cv] 配置版本数据库已初始化: %s", self._db_path)
        return self._db

    async def save_version(
        self,
        device_id: str,
        config: dict[str, Any],
        change_summary: str,
        operator: str,
    ) -> int:
        """保存配置快照，返回新版本号（从 1 开始自增）。"""
        db = await self._ensure_db()
        cursor = await db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM config_versions WHERE device_id = ?",
            (device_id,),
        )
        row = await cursor.fetchone()
        current_max = row[0] if row else 0
        new_version = current_max + 1
        config_json = json.dumps(config, ensure_ascii=False, default=str)
        now = time.time()
        await db.execute(
            """INSERT INTO config_versions
               (device_id, version, config_json, change_summary, operator, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (device_id, new_version, config_json, change_summary, operator, now),
        )
        await db.execute(
            """INSERT INTO config_audit_trail
               (device_id, version, action, operator, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (device_id, new_version, "save", operator, change_summary, now),
        )
        await db.commit()
        logger.info("[fins-cv] 保存配置版本: device=%s version=%d operator=%s", device_id, new_version, operator)
        return new_version

    async def get_current(self, device_id: str) -> dict[str, Any] | None:
        """获取设备当前（最新）版本信息，无记录返回 None。"""
        db = await self._ensure_db()
        cursor = await db.execute(
            """SELECT version, config_json, change_summary, operator, created_at
               FROM config_versions
               WHERE device_id = ?
               ORDER BY version DESC
               LIMIT 1""",
            (device_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "device_id": device_id,
            "version": row["version"],
            "config": json.loads(row["config_json"]),
            "change_summary": row["change_summary"],
            "operator": row["operator"],
            "created_at": datetime.fromtimestamp(row["created_at"], tz=UTC).isoformat(),
        }

    async def get_versions(self, device_id: str, limit: int) -> list[dict[str, Any]]:
        """获取设备历史版本列表（倒序，最多 limit 条）。

        注意：接收 2 个参数（device_id, limit），与 OPC UA/S7 的 1 参数签名不同。
        """
        db = await self._ensure_db()
        cursor = await db.execute(
            """SELECT version, change_summary, operator, created_at
               FROM config_versions
               WHERE device_id = ?
               ORDER BY version DESC
               LIMIT ?""",
            (device_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "device_id": device_id,
                "version": row["version"],
                "change_summary": row["change_summary"],
                "operator": row["operator"],
                "created_at": datetime.fromtimestamp(row["created_at"], tz=UTC).isoformat(),
            }
            for row in rows
        ]

    async def get_version_config(
        self, device_id: str, version: int
    ) -> dict[str, Any] | None:
        """获取指定版本的完整配置，不存在返回 None。"""
        db = await self._ensure_db()
        cursor = await db.execute(
            """SELECT version, config_json, change_summary, operator, created_at
               FROM config_versions
               WHERE device_id = ? AND version = ?""",
            (device_id, version),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "device_id": device_id,
            "version": row["version"],
            "config": json.loads(row["config_json"]),
            "change_summary": row["change_summary"],
            "operator": row["operator"],
            "created_at": datetime.fromtimestamp(row["created_at"], tz=UTC).isoformat(),
        }

    async def rollback(
        self, device_id: str, target_version: int, operator: str
    ) -> dict[str, Any] | None:
        """回滚到目标版本，返回该版本配置；目标版本不存在返回 None。

        回滚操作在审计轨迹中记录一条 rollback 记录，但不删除后续版本
        （保留完整历史，新保存的版本号继续自增）。
        """
        config_data = await self.get_version_config(device_id, target_version)
        if config_data is None:
            logger.warning("[fins-cv] 回滚失败-目标版本不存在: device=%s version=%d", device_id, target_version)
            return None
        db = await self._ensure_db()
        now = time.time()
        await db.execute(
            """INSERT INTO config_audit_trail
               (device_id, version, action, operator, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (device_id, target_version, "rollback", operator, f"rollback to v{target_version}", now),
        )
        await db.commit()
        logger.info("[fins-cv] 回滚配置: device=%s to_version=%d operator=%s", device_id, target_version, operator)
        return config_data

    async def get_audit_trail(self, device_id: str, limit: int) -> list[dict[str, Any]]:
        """获取设备配置变更审计轨迹（倒序，最多 limit 条）。"""
        db = await self._ensure_db()
        cursor = await db.execute(
            """SELECT version, action, operator, details, created_at
               FROM config_audit_trail
               WHERE device_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (device_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "device_id": device_id,
                "version": row["version"],
                "action": row["action"],
                "operator": row["operator"],
                "details": row["details"],
                "created_at": datetime.fromtimestamp(row["created_at"], tz=UTC).isoformat(),
            }
            for row in rows
        ]

    async def diff_versions(
        self, device_id: str, version_a: int, version_b: int
    ) -> dict[str, Any]:
        """对比两个版本的配置差异（async 方法）。

        Returns:
            含 changes 列表的字典，每个变更项含 key/old_value/new_value。
            若任一版本不存在，changes 为空列表并在 missing 中标注。
        """
        config_a = await self.get_version_config(device_id, version_a)
        config_b = await self.get_version_config(device_id, version_b)
        result: dict[str, Any] = {
            "device_id": device_id,
            "version_a": version_a,
            "version_b": version_b,
            "changes": [],
        }
        if config_a is None:
            result["missing"] = f"version {version_a} not found"
            return result
        if config_b is None:
            result["missing"] = f"version {version_b} not found"
            return result
        cfg_a = config_a["config"] if isinstance(config_a["config"], dict) else {}
        cfg_b = config_b["config"] if isinstance(config_b["config"], dict) else {}
        all_keys = set(cfg_a.keys()) | set(cfg_b.keys())
        changes: list[dict[str, Any]] = []
        for key in sorted(all_keys):
            val_a = cfg_a.get(key)
            val_b = cfg_b.get(key)
            if val_a != val_b:
                changes.append({"key": key, "old_value": val_a, "new_value": val_b})
        result["changes"] = changes
        return result

    async def stop(self) -> None:
        """关闭数据库连接（必须 async，fins.py 中 await 调用）。"""
        if self._db is not None:
            try:
                await self._db.close()
            except Exception as e:  # noqa: BLE001
                logger.debug("[fins-cv] 关闭数据库异常: %s", e)
            self._db = None
            logger.info("[fins-cv] 配置版本数据库已关闭")
