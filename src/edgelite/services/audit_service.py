"""审计日志服务

支持：
- 完整操作记录（5大类20+操作类型）
- 防篡改签名（哈希链）
- 合规审计报告导出（CSV/JSON）
- 日志保留策略
- 异常登录检测
"""

from __future__ import annotations

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

    async def initialize(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
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

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_logs(timestamp)")
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
        content = f"{record['timestamp']}|{record.get('user_id', '')}|{record.get('username', '')}|{record['action']}|{record.get('resource_type', '')}|{record.get('resource_id', '')}|{record.get('ip_address', '')}|{record.get('status', '')}|{prev_hash}"
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
                "timestamp": timestamp, "user_id": user_id, "username": username,
                "action": action.value, "resource_type": resource_type,
                "resource_id": resource_id, "ip_address": ip_address, "status": status,
            }
            prev_hash = self._last_hash
            record_hash = self._compute_record_hash(record, prev_hash)
            self._last_hash = record_hash

        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO audit_logs (
                timestamp, user_id, username, action, resource_type, resource_id,
                ip_address, user_agent, details, status, error_message,
                prev_hash, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            timestamp, user_id, username, action.value, resource_type, resource_id,
            ip_address, user_agent, details_json, status, error_message,
            prev_hash, record_hash,
        ))

        conn.commit()
        conn.close()

        if action == AuditAction.LOGIN_FAILED and ip_address:
            await self._check_login_anomaly(ip_address, username)

    async def _check_login_anomaly(self, ip_address: str, username: str | None) -> None:
        self._login_fail_counts[ip_address] = self._login_fail_counts.get(ip_address, 0) + 1
        count = self._login_fail_counts[ip_address]

        if count >= self._login_fail_threshold:
            logger.warning("异常登录检测: IP=%s 连续失败%d次", ip_address, count)
            if self._on_audit_alert:
                try:
                    await self._on_audit_alert({
                        "type": "login_anomaly",
                        "ip_address": ip_address,
                        "fail_count": count,
                        "username": username,
                        "timestamp": datetime.now().isoformat(),
                    })
                except Exception as e:
                    logger.error("审计告警回调失败: %s", e)

    def reset_login_fail_count(self, ip_address: str) -> None:
        self._login_fail_counts.pop(ip_address, None)

    async def verify_integrity(self) -> dict:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT id, timestamp, user_id, username, action, resource_type, "
            "resource_id, ip_address, status, prev_hash, record_hash "
            "FROM audit_logs ORDER BY id ASC"
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return {"valid": True, "total": 0, "broken_at": []}

        broken_at = []
        expected_prev_hash = ""

        for row in rows:
            (log_id, timestamp, user_id, username, action, resource_type,
             resource_id, ip_address, status, prev_hash, record_hash) = row

            if prev_hash != expected_prev_hash:
                broken_at.append(log_id)

            record = {
                "timestamp": timestamp, "user_id": user_id, "username": username,
                "action": action, "resource_type": resource_type,
                "resource_id": resource_id, "ip_address": ip_address, "status": status,
            }
            computed_hash = self._compute_record_hash(record, prev_hash)
            if computed_hash != record_hash:
                broken_at.append(log_id)

            expected_prev_hash = record_hash

        return {"valid": len(broken_at) == 0, "total": len(rows), "broken_at": broken_at}

    async def query(
        self,
        user_id: str | None = None,
        action: AuditAction | None = None,
        resource_type: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict], int]:
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
            conditions.append("timestamp >= ?")
            params.append(start_time.isoformat())
        if end_time:
            conditions.append("timestamp <= ?")
            params.append(end_time.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        cursor.execute(f"SELECT COUNT(*) FROM audit_logs WHERE {where_clause}", params)
        total = cursor.fetchone()[0]

        offset = (page - 1) * size
        cursor.execute(
            f"SELECT id, timestamp, user_id, username, action, resource_type, resource_id, "
            f"ip_address, user_agent, details, status, error_message "
            f"FROM audit_logs WHERE {where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [size, offset],
        )

        rows = cursor.fetchall()
        conn.close()

        logs = [
            {
                "id": row[0], "timestamp": row[1], "user_id": row[2], "username": row[3],
                "action": row[4], "resource_type": row[5], "resource_id": row[6],
                "ip_address": row[7], "user_agent": row[8],
                "details": json.loads(row[9]) if row[9] else None,
                "status": row[10], "error_message": row[11],
            }
            for row in rows
        ]

        return logs, total

    async def export_csv(self, start_time: datetime | None = None, end_time: datetime | None = None) -> str:
        logs, _ = await self.query(start_time=start_time, end_time=end_time, size=10000)

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "时间", "用户ID", "用户名", "操作", "资源类型", "资源ID", "IP地址", "状态", "详情"])

        for log in logs:
            writer.writerow([
                log["id"], log["timestamp"], log["user_id"], log["username"],
                log["action"], log["resource_type"], log["resource_id"],
                log["ip_address"], log["status"],
                json.dumps(log["details"], ensure_ascii=False) if log.get("details") else "",
            ])

        return output.getvalue()

    async def cleanup(self, retention_days: int = 90) -> int:
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM audit_logs WHERE timestamp < datetime('now', ?)", (f"-{retention_days} days",))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        logger.info("清理过期审计日志: %d 条", deleted)
        return deleted
