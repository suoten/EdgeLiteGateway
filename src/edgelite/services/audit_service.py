"""审计日志服务

支持：
- 完整操作记录
- 防篡改签名（哈希链）
- 合规审计报告导出（CSV）
- 日志保留策略
- 异常登录检测
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class AuditAction(str, Enum):
    LOGIN = "login"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    TOKEN_REFRESH = "token_refresh"
    DEVICE_CREATE = "device_create"
    DEVICE_UPDATE = "device_update"
    DEVICE_DELETE = "device_delete"
    DEVICE_START = "device_start"
    DEVICE_STOP = "device_stop"
    RULE_CREATE = "rule_create"
    RULE_UPDATE = "rule_update"
    RULE_DELETE = "rule_delete"
    RULE_ENABLE = "rule_enable"
    RULE_DISABLE = "rule_disable"
    ALARM_ACK = "alarm_ack"
    USER_CREATE = "user_create"
    USER_UPDATE = "user_update"
    USER_DELETE = "user_delete"
    PASSWORD_CHANGE = "password_change"
    BACKUP_CREATE = "backup_create"
    BACKUP_RESTORE = "backup_restore"
    CONFIG_UPDATE = "config_update"
    PLUGIN_LOAD = "plugin_load"
    PLUGIN_UNLOAD = "plugin_unload"


class AuditService:
    """审计日志服务"""

    def __init__(self, db_path: str = "data/edgelite.db", tamper_proof: bool = True):
        self._db_path = db_path
        self._tamper_proof = tamper_proof
        self._initialized = False
        self._last_hash = ""
        self._login_fail_counts: dict[str, int] = {}
        self._login_fail_threshold = 5
        self._on_audit_alert: Any = None

    def set_alert_callback(self, callback: Any) -> None:
        self._on_audit_alert = callback

    async def close(self) -> None:
        self._initialized = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._sync_initialize)

    def _sync_initialize(self) -> None:
        import sqlite3
        from pathlib import Path
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id TEXT,
                username TEXT,
                action TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                ip_address TEXT,
                user_agent TEXT,
                details TEXT,
                status TEXT DEFAULT 'success',
                error_message TEXT,
                prev_hash TEXT,
                record_hash TEXT
            )
        """)

        cursor.execute("PRAGMA table_info(audit_logs)")
        columns = [col[1] for col in cursor.fetchall()]
        if 'timestamp' in columns and 'created_at' not in columns:
            cursor.execute("ALTER TABLE audit_logs RENAME COLUMN timestamp TO created_at")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_logs(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action)")

        conn.commit()

        if self._tamper_proof:
            cursor.execute("SELECT record_hash FROM audit_logs ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            self._last_hash = row[0] if row else ""

        conn.close()
        self._initialized = True

    def _compute_record_hash(self, record: dict, prev_hash: str) -> str:
        content = f"{record['created_at']}|{record.get('user_id', '')}|{record.get('username', '')}|{record['action']}|{record.get('resource_type', '')}|{record.get('resource_id', '')}|{record.get('ip_address', '')}|{record.get('status', '')}|{prev_hash}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    async def log(
        self,
        action: AuditAction,
        user_id: str | None = None,
        username: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        details: dict | None = None,
        status: str = "success",
        error_message: str | None = None,
    ) -> None:
        if not self._initialized:
            await self.initialize()

        timestamp = datetime.now().isoformat()
        details_json = json.dumps(details, ensure_ascii=False) if details else None

        prev_hash = ""
        record_hash = ""
        if self._tamper_proof:
            record = {
                "created_at": timestamp, "user_id": user_id, "username": username,
                "action": action.value, "resource_type": resource_type,
                "resource_id": resource_id, "ip_address": ip_address, "status": status,
            }
            prev_hash = self._last_hash
            record_hash = self._compute_record_hash(record, prev_hash)
            self._last_hash = record_hash

        await asyncio.to_thread(
            self._sync_log, timestamp, user_id, username, action.value,
            resource_type, resource_id, ip_address, user_agent,
            details_json, status, error_message, prev_hash, record_hash,
        )

        if action == AuditAction.LOGIN_FAILED and ip_address:
            await self._check_login_anomaly(ip_address, username)

    def _sync_log(self, timestamp, user_id, username, action, resource_type,
                  resource_id, ip_address, user_agent, details_json, status,
                  error_message, prev_hash, record_hash) -> None:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO audit_logs (
                created_at, user_id, username, action, resource_type, resource_id,
                ip_address, user_agent, details, status, error_message,
                prev_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            timestamp, user_id, username, action, resource_type, resource_id,
            ip_address, user_agent, details_json, status, error_message,
            prev_hash, record_hash,
        ))

        conn.commit()
        conn.close()

    async def _check_login_anomaly(self, ip_address: str, username: str | None) -> None:
        key = f"{ip_address}:{username or 'unknown'}"
        self._login_fail_counts[key] = self._login_fail_counts.get(key, 0) + 1
        if self._login_fail_counts[key] >= self._login_fail_threshold:
            if self._on_audit_alert:
                try:
                    await self._on_audit_alert({
                        "type": "login_anomaly",
                        "ip_address": ip_address,
                        "username": username,
                        "fail_count": self._login_fail_counts[key],
                    })
                except Exception:
                    pass
            self._login_fail_counts[key] = 0

    async def query(
        self,
        user_id: str | None = None,
        action: AuditAction | None = None,
        resource_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict], int]:
        return await asyncio.to_thread(
            self._sync_query, user_id, action, resource_type,
            start_time, end_time, page, size,
        )

    def _sync_query(self, user_id, action, resource_type, start_time, end_time, page, size):
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()

        conditions = []
        params = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if action:
            conditions.append("action = ?")
            params.append(action.value)
        if resource_type:
            conditions.append("resource_type = ?")
            params.append(resource_type)
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time.isoformat())

        where = " AND ".join(conditions) if conditions else "1=1"

        cursor.execute(f"SELECT COUNT(*) FROM audit_logs WHERE {where}", params)
        total = cursor.fetchone()[0]

        offset = (page - 1) * size
        cursor.execute(
            f"SELECT * FROM audit_logs WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [size, offset],
        )
        columns = [desc[0] for desc in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

        conn.close()
        return rows, total

    async def verify_integrity(self) -> dict:
        return await asyncio.to_thread(self._sync_verify_integrity)

    def _sync_verify_integrity(self) -> dict:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id, created_at, user_id, username, action, resource_type, resource_id, ip_address, status, prev_hash, record_hash FROM audit_logs ORDER BY id ASC")
        rows = cursor.fetchall()
        conn.close()

        total = len(rows)
        broken_at = []
        prev_hash = ""
        for row in rows:
            record = {
                "created_at": row[1], "user_id": row[2], "username": row[3],
                "action": row[4], "resource_type": row[5], "resource_id": row[6],
                "ip_address": row[7], "status": row[8],
            }
            expected_hash = self._compute_record_hash(record, prev_hash)
            if row[10] != expected_hash:
                broken_at.append(row[0])
            prev_hash = row[10]

        return {"valid": len(broken_at) == 0, "total": total, "broken_at": broken_at}

    async def export_csv(self, start_time: datetime | None = None, end_time: datetime | None = None) -> str:
        return await asyncio.to_thread(self._sync_export_csv, start_time, end_time)

    def _sync_export_csv(self, start_time, end_time) -> str:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()

        conditions = []
        params = []
        if start_time:
            conditions.append("created_at >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("created_at <= ?")
            params.append(end_time.isoformat())

        where = " AND ".join(conditions) if conditions else "1=1"
        cursor.execute(f"SELECT * FROM audit_logs WHERE {where} ORDER BY id ASC", params)
        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        conn.close()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in rows:
            writer.writerow(row)

        return output.getvalue()

    async def cleanup(self, retention_days: int = 90) -> int:
        return await asyncio.to_thread(self._sync_cleanup, retention_days)

    def _sync_cleanup(self, retention_days: int) -> int:
        import sqlite3
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM audit_logs WHERE created_at < ?", (cutoff,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted
