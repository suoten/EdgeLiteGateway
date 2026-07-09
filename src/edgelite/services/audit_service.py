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
import threading
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

# R6-S-04修复: 导入CSV单元格净化函数，防止公式注入
from edgelite.services.data_import_export import _sanitize_csv_cell

logger = logging.getLogger(__name__)

# S-10: 提取 PRAGMA 配置为模块级常量，确保所有连接创建/重建路径都调用
# WAL 模式提升并发写入性能；busy_timeout 避免高并发下 "database is locked"；
# synchronous=NORMAL 在 WAL 模式下兼顾安全性与性能
_DB_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",  # 启用外键约束，连接级设置
)


class AuditAction(StrEnum):
    LOGIN = "login"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    TOKEN_REFRESH = "token_refresh"
    DEVICE_CREATE = "device_create"
    DEVICE_UPDATE = "device_update"
    DEVICE_DELETE = "device_delete"
    DEVICE_START = "device_start"
    DEVICE_STOP = "device_stop"
    DEVICE_WRITE_POINT = "device_write_point"  # SEC-FIX-V05: 写入审计持久化
    RULE_CREATE = "rule_create"
    RULE_UPDATE = "rule_update"
    RULE_DELETE = "rule_delete"
    RULE_ENABLE = "rule_enable"
    RULE_DISABLE = "rule_disable"
    # SEC-FIX-V07: 脚本引擎审计动作
    SCRIPT_CREATE = "script_create"
    SCRIPT_UPDATE = "script_update"
    SCRIPT_DELETE = "script_delete"
    SCRIPT_ENABLE = "script_enable"
    SCRIPT_DISABLE = "script_disable"
    ALARM_ACK = "alarm_ack"
    ALARM_DELETE = "alarm_delete"  # FIXED(严重): 告警物理删除审计动作
    USER_CREATE = "user_create"
    USER_UPDATE = "user_update"
    USER_DELETE = "user_delete"
    PASSWORD_CHANGE = "password_change"
    BACKUP_CREATE = "backup_create"
    BACKUP_RESTORE = "backup_restore"
    CONFIG_UPDATE = "config_update"
    PLUGIN_LOAD = "plugin_load"
    PLUGIN_UNLOAD = "plugin_unload"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_UNLOCKED = "account_unlocked"
    DRIVER_CONFIG_UPDATE = "driver_config_update"
    SHADOW_UPDATE = "shadow_update"
    COMMAND_APPROVE = "command_approve"
    COMMAND_REJECT = "command_reject"
    CONFIG_VERSION_SAVE = "config_version_save"
    CONFIG_VERSION_ROLLBACK = "config_version_rollback"
    OTA_START = "ota_start"
    OTA_COMPLETED = "ota_completed"
    OTA_FAILED = "ota_failed"
    OTA_ROLLBACK = "ota_rollback"
    # FIXED: Password reset audit actions
    FORGOT_PASSWORD_REQUEST = "forgot_password_request"
    FORGOT_PASSWORD_USER_NOT_FOUND = "forgot_password_user_not_found"
    FORGOT_PASSWORD_EMAIL_ERROR = "forgot_password_email_error"
    FORGOT_PASSWORD_DB_ERROR = "forgot_password_db_error"
    # FIXED-H01: Rate limiting audit action
    FORGOT_PASSWORD_RATE_LIMITED = "forgot_password_rate_limited"
    # FIXED-H02: API Key usage audit actions
    API_KEY_USED = "api_key_used"
    API_KEY_FAILED = "api_key_failed"
    # FIXED(严重): Password reset usage audit actions
    PASSWORD_RESET_USED = "password_reset_used"
    PASSWORD_RESET_REUSED = "password_reset_reused"
    PASSWORD_RESET_RATELIMITED = "password_reset_ratelimited"
    # 第三轮审计修复: 高风险操作审计动作补充
    SCRIPT_TEST = "script_test"
    CACHE_CLEAR = "cache_clear"
    LOG_CLEAR = "log_clear"
    DRIVER_RELOAD = "driver_reload"
    SERVICE_START = "service_start"
    SERVICE_STOP = "service_stop"
    PLATFORM_CONNECT = "platform_connect"
    PLATFORM_DISCONNECT = "platform_disconnect"
    BACKUP_DELETE = "backup_delete"
    DOWNSAMPLE_TRIGGER = "downsample_trigger"
    CIRCUIT_BREAKER_RESET = "circuit_breaker_reset"
    CONFIG_RELOAD = "config_reload"
    # CONFIG_UPDATE 已存在，无需重复定义
    RPC_EXECUTE = "rpc_execute"
    MODEL_HOT_SWAP = "model_hot_swap"
    AB_TEST_PROMOTE = "ab_test_promote"
    AB_TEST_ROLLBACK = "ab_test_rollback"
    AB_TEST_DELETE = "ab_test_delete"
    # 第四轮修复: 补充审计动作
    NOTIFY_CONFIG_UPDATE = "notify_config_update"  # 通知渠道配置更新
    NOTIFY_CHANNEL_TEST = "notify_channel_test"  # 通知渠道测试
    NOTIFY_CHANNEL_TOGGLE = "notify_channel_toggle"  # 通知渠道启用/禁用
    NOTIFY_CHANNEL_DELETE = "notify_channel_delete"  # 通知渠道删除
    DATA_EXPORT = "data_export"  # 数据导出
    DATA_IMPORT = "data_import"  # 数据导入
    LINKAGE_RULE_CREATE = "linkage_rule_create"  # 联动规则创建
    LINKAGE_RULE_UPDATE = "linkage_rule_update"  # 联动规则更新
    LINKAGE_RULE_DELETE = "linkage_rule_delete"  # 联动规则删除
    LINKAGE_RULE_ENABLE = "linkage_rule_enable"  # 联动规则启用
    LINKAGE_RULE_DISABLE = "linkage_rule_disable"  # 联动规则禁用
    ALARM_SUPPRESS = "alarm_suppress"  # 告警抑制
    ALARM_SILENCE_CREATE = "alarm_silence_create"  # 告警静默创建
    ALARM_SILENCE_DELETE = "alarm_silence_delete"  # 告警静默删除
    RESOURCE_TRANSFER = "resource_transfer"  # 资源所有权转移


class AuditService:
    """审计日志服务"""

    def __init__(self, db_path: str = "data/edgelite.db", tamper_proof: bool = True):
        self._db_path = db_path
        self._tamper_proof = tamper_proof
        self._initialized = False
        self._last_hash = ""
        self._login_fail_counts: dict[str, int] = {}
        self._login_fail_threshold = 5
        self._login_fail_max_keys = 10000
        self._on_audit_alert: Any = None
        self._conn: Any = None  # FIXED-P2: 持久化数据库连接，避免每次操作新建连接
        self._db_lock = threading.Lock()  # FIXED-P1: 原问题-AuditService无并发锁保护，多线程同时写sqlite3.Connection时"database is locked"
        # S-09: 保护 _check_login_anomaly 中 _login_fail_counts 的读-改-写临界区，
        # 避免并发协程导致计数器丢失更新和重复告警
        self._login_anomaly_lock = asyncio.Lock()

    def set_alert_callback(self, callback: Any) -> None:
        self._on_audit_alert = callback

    async def close(self) -> None:
        if self._conn is not None:  # FIXED-P2: 关闭持久连接
            try:
                self._conn.close()
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("关闭审计数据库连接失败: %s", e)
            self._conn = None
        self._initialized = False

    async def initialize(self) -> None:
        await asyncio.to_thread(self._sync_initialize)

    def _apply_db_pragmas(self, conn) -> None:
        """S-10: 对连接应用 PRAGMA 配置（WAL/busy_timeout/synchronous）

        确保所有连接创建与重建路径都调用此方法，避免默认 rollback journal
        模式导致的性能下降和高并发下 "database is locked" 错误。
        """
        for pragma in _DB_PRAGMAS:
            conn.execute(pragma)

    def _sync_initialize(self) -> None:
        import sqlite3
        from pathlib import Path

        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
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
        if "timestamp" in columns and "created_at" not in columns:
            cursor.execute("ALTER TABLE audit_logs RENAME COLUMN timestamp TO created_at")
            columns = [c if c != "timestamp" else "created_at" for c in columns]

        expected_columns = {
            "user_agent": "TEXT",
            "details": "TEXT",
            "status": "TEXT DEFAULT 'success'",
            "error_message": "TEXT",
            "prev_hash": "TEXT",
            "record_hash": "TEXT",
            "before_value": "TEXT",
            "after_value": "TEXT",
        }
        for col_name, col_def in expected_columns.items():
            if col_name not in columns:
                cursor.execute(f"ALTER TABLE audit_logs ADD COLUMN {col_name} {col_def}")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_logs(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action)")

        # FIXED: 添加 append-only 防篡改触发器，阻止 UPDATE/DELETE 操作 [2026-06-29]
        # 原问题-审计日志仅靠哈希链事后检测篡改，不能阻止有 DB 写权限的攻击者直接修改/删除记录
        # 触发器在 SQL 层拦截 UPDATE/DELETE，抛出异常阻止操作执行
        # 例外：_sync_cleanup 的物理删除需要通过临时禁用触发器实现（ATTACH + DETACH 模式）
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS audit_no_update
            BEFORE UPDATE ON audit_logs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs is append-only: UPDATE not allowed');
            END
        """)
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS audit_no_delete
            BEFORE DELETE ON audit_logs
            FOR EACH ROW
            BEGIN
                SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE not allowed');
            END
        """)

        # FIXED: P2-4 登录失败计数仅存内存，服务重启后清零导致暴力破解防护失效
        # 创建独立的登录失败计数表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS login_fail_counts (
                key TEXT PRIMARY KEY,
                fail_count INTEGER NOT NULL DEFAULT 0,
                last_attempt REAL NOT NULL
            )
        """)
        # 从数据库加载已有计数
        cursor.execute("SELECT key, fail_count FROM login_fail_counts")
        for row in cursor.fetchall():
            self._login_fail_counts[row[0]] = row[1]

        conn.commit()

        if self._tamper_proof:
            cursor.execute("SELECT record_hash FROM audit_logs ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            self._last_hash = row[0] if row else ""

        # S-10: 复用 PRAGMA 辅助方法，确保初始连接也配置 WAL/busy_timeout/synchronous
        self._apply_db_pragmas(conn)
        self._conn = conn  # FIXED-P2: 保持持久连接，避免每次_sync_log新建连接
        self._initialized = True

    def _compute_record_hash(self, record: dict, prev_hash: str) -> str:
        # R6-S-10: 将 details_json/user_agent/error_message/before_value_json/after_value_json 纳入哈希计算
        # 原实现仅覆盖部分字段，攻击者可篡改 details/before_value/after_value 等字段而不破坏哈希链
        content = (
            f"{record['created_at']}|"
            f"{record.get('user_id', '')}|"
            f"{record.get('username', '')}|"
            f"{record['action']}|"
            f"{record.get('resource_type', '')}|"
            f"{record.get('resource_id', '')}|"
            f"{record.get('ip_address', '')}|"
            f"{record.get('status', '')}|"
            f"{record.get('user_agent', '')}|"
            f"{record.get('details_json', '')}|"
            f"{record.get('error_message', '')}|"
            f"{record.get('before_value_json', '')}|"
            f"{record.get('after_value_json', '')}|"
            f"{prev_hash}"
        )
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # R5-S-27 修复(严重): 审计日志 before_value/after_value 中的敏感字段未脱敏，
    # 密码/token/secret 等明文写入审计文件造成二次泄露。
    _SENSITIVE_FIELD_PATTERNS = frozenset({
        "password", "passwd", "pwd", "secret", "token", "access_token",
        "refresh_token", "api_key", "apikey", "private_key", "client_secret",
        "authorization", "credential", "credentials", "master_key", "kdf_salt",
    })

    @classmethod
    def _mask_sensitive(cls, value: Any) -> Any:
        """递归脱敏字典/列表中的敏感字段值"""
        if isinstance(value, dict):
            masked = {}
            for k, v in value.items():
                key_lower = str(k).lower()
                if key_lower in cls._SENSITIVE_FIELD_PATTERNS or any(p in key_lower for p in cls._SENSITIVE_FIELD_PATTERNS):
                    masked[k] = "***REDACTED***" if v else v
                else:
                    masked[k] = cls._mask_sensitive(v)
            return masked
        if isinstance(value, list):
            return [cls._mask_sensitive(item) for item in value]
        return value

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
        before_value: Any | None = None,
        after_value: Any | None = None,
    ) -> None:
        if not self._initialized:
            await self.initialize()

        timestamp = datetime.now(UTC).isoformat()
        # R5-S-27: 脱敏后再序列化，确保敏感字段不写入审计日志
        details = self._mask_sensitive(details) if details else details
        before_value = self._mask_sensitive(before_value) if before_value is not None else before_value
        after_value = self._mask_sensitive(after_value) if after_value is not None else after_value
        details_json = json.dumps(details, ensure_ascii=False) if details else None
        before_value_json = json.dumps(before_value, ensure_ascii=False, default=str) if before_value is not None else None
        after_value_json = json.dumps(after_value, ensure_ascii=False, default=str) if after_value is not None else None

        # FIX-P0: 原代码在 _db_lock 外读取 prev_hash(=self._last_hash) 并在写后更新
        # _last_hash，并发 log() 调用会使用相同 prev_hash 插入记录，破坏审计哈希链。
        # 现将 record 字典传入 _sync_log，由其在 _db_lock 临界区内完成
        # prev_hash 读取、record_hash 计算、DB 写入与 _last_hash 更新，保证原子性。
        record = None
        if self._tamper_proof:
            record = {
                "created_at": timestamp,
                "user_id": user_id,
                "username": username,
                "action": action.value,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "ip_address": ip_address,
                "status": status,
                # R6-S-10: 将以下字段纳入哈希计算，防止篡改
                "user_agent": user_agent,
                "details_json": details_json,
                "error_message": error_message,
                "before_value_json": before_value_json,
                "after_value_json": after_value_json,
            }

        await asyncio.to_thread(
            self._sync_log,
            timestamp,
            user_id,
            username,
            action.value,
            resource_type,
            resource_id,
            ip_address,
            user_agent,
            details_json,
            status,
            error_message,
            record,
            before_value_json,
            after_value_json,
        )

        if action == AuditAction.LOGIN_FAILED and ip_address:
            await self._check_login_anomaly(ip_address, username)

    def _sync_log(
        self,
        timestamp,
        user_id,
        username,
        action,
        resource_type,
        resource_id,
        ip_address,
        user_agent,
        details_json,
        status,
        error_message,
        record,
        before_value_json,
        after_value_json,
    ) -> bool:
        """FIXED-P0: 返回写入是否成功，供 log 方法决定是否更新 _last_hash"""
        # FIXED-P1: 原问题-多线程并发写持久sqlite3.Connection时"database is locked"
        # 改为使用_db_lock保护所有DB写入操作
        with self._db_lock:
            # FIX-P0: 将 prev_hash 读取、record_hash 计算与 _last_hash 更新
            # 全部纳入 _db_lock 临界区，避免并发 log() 调用读取到相同的
            # prev_hash 并写入记录，从而破坏审计哈希链的完整性
            prev_hash = ""
            record_hash = ""
            if record is not None:
                prev_hash = self._last_hash
                record_hash = self._compute_record_hash(record, prev_hash)
            conn = self._conn
            if conn is None:
                # S-10: 写入失败后重建连接路径，必须配置 PRAGMA，否则默认 rollback
                # journal 模式会导致性能下降和高并发下 "database is locked"
                import sqlite3
                logger.warning("审计数据库连接为空，重建连接: %s", self._db_path)
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                # S-10: 重建后立即执行 PRAGMA 配置，复用辅助方法确保一致性
                self._apply_db_pragmas(conn)
                self._conn = conn
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO audit_logs (
                        created_at, user_id, username, action, resource_type, resource_id,
                        ip_address, user_agent, details, status, error_message,
                        prev_hash, record_hash, before_value, after_value
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        timestamp,
                        user_id,
                        username,
                        action,
                        resource_type,
                        resource_id,
                        ip_address,
                        user_agent,
                        details_json,
                        status,
                        error_message,
                        prev_hash,
                        record_hash,
                        before_value_json,
                        after_value_json,
                    ),
                )
                conn.commit()
                # FIX-P0: 仅在写入成功后才在锁内更新 _last_hash，
                # 确保哈希链与已持久化的记录严格一致
                if record is not None and record_hash:
                    self._last_hash = record_hash
                return True  # FIXED-P0: 返回成功标志
            except Exception as e:
                # FIXED-P0: 记录写入失败的错误日志，原代码完全静默
                logger.error("Audit log write failed: %s", e)
                try:
                    conn.close()
                except Exception as e:
                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                    logger.warning("关闭异常连接失败: %s", e)
                # S-10: 记录连接重建事件，便于排查审计日志持续丢失问题
                logger.warning("审计数据库连接因写入异常已关闭，下次写入将触发重建")
                self._conn = None
                return False  # FIXED-P0: 返回失败标志

    async def _check_login_anomaly(self, ip_address: str, username: str | None) -> None:
        key = f"{ip_address}:{username or 'unknown'}"
        # S-09: 使用 asyncio.Lock 保护 _login_fail_counts 的读-改-写临界区，
        # 锁粒度仅覆盖计数器更新、LRU 清理、阈值判断与重置；
        # 告警触发（含 await 网络 IO）在锁外执行，避免持锁等待
        alert_info = None
        count_to_persist = 0
        async with self._login_anomaly_lock:
            self._login_fail_counts[key] = self._login_fail_counts.get(key, 0) + 1
            count_to_persist = self._login_fail_counts[key]
            if len(self._login_fail_counts) > self._login_fail_max_keys:
                sorted_keys = sorted(
                    self._login_fail_counts.keys(),
                    key=lambda k: self._login_fail_counts[k],
                )
                for k in sorted_keys[: len(self._login_fail_counts) - self._login_fail_max_keys // 2]:
                    del self._login_fail_counts[k]
            if self._login_fail_counts[key] >= self._login_fail_threshold:
                # S-09: 在锁内准备告警数据并重置计数器，
                # 确保只有一个协程能触发告警，避免重复告警
                alert_info = {
                    "type": "login_anomaly",
                    "ip_address": ip_address,
                    "username": username,
                    "fail_count": self._login_fail_counts[key],
                }
                self._login_fail_counts[key] = 0
                count_to_persist = 0
        # S-09: 告警触发在锁外执行，避免持锁 await 网络 IO 导致阻塞其他协程
        if alert_info and self._on_audit_alert:
            try:
                await self._on_audit_alert(alert_info)
            except Exception as e:
                logger.warning("Audit alert callback failed: %s", e)
        # FIXED: P2-4 持久化登录失败计数到数据库，防止服务重启后计数清零
        # R8-C-01 修复(致命): 原代码在 async 函数中直接使用 threading.Lock(_db_lock)，
        # 阻塞整个事件循环。改为通过 asyncio.to_thread 在工作线程中执行同步 DB 写入，
        # threading.Lock 仅在工作线程中获取，不阻塞事件循环。
        try:
            await asyncio.to_thread(self._persist_login_fail_count, key, count_to_persist)
        except Exception as e:
            logger.warning("持久化登录失败计数失败: %s", e)

    def _persist_login_fail_count(self, key: str, count: int) -> None:
        """R8-C-01: 同步持久化登录失败计数（通过 asyncio.to_thread 调用，避免阻塞事件循环）"""
        import sqlite3
        import time as _time

        # _db_lock 是 threading.Lock，在工作线程中获取不阻塞事件循环
        with self._db_lock:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            try:
                cursor = conn.cursor()
                # S-10: 短期连接也需配置 PRAGMA，避免与持久连接并发时锁等待超时
                self._apply_db_pragmas(conn)
                # FIXED-P1: 原代码 last_attempt 值错误地使用 fail_count 而非时间戳
                cursor.execute(
                    "INSERT OR REPLACE INTO login_fail_counts (key, fail_count, last_attempt) VALUES (?, ?, ?)",
                    (key, count, _time.time()),
                )
                conn.commit()
            finally:
                conn.close()

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
            self._sync_query,
            user_id,
            action,
            resource_type,
            start_time,
            end_time,
            page,
            size,
        )

    def _sync_query(self, user_id, action, resource_type, start_time, end_time, page, size):
        import sqlite3

        # FIXED-P0: 原代码新建连接且无锁保护，与 _sync_log 并发时可能 "database is locked"
        # 改为使用 _db_lock 保护，复用持久连接
        with self._db_lock:
            conn = self._conn
            if conn is None:
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                should_close = True
            else:
                should_close = False
            try:
                self._apply_db_pragmas(conn)  # FIXED: fallback 路径也需应用 PRAGMA 优化 (S-10 回归)
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

                # 安全说明：conditions 中的字符串必须为硬编码字面量，禁止拼接用户输入。
                # 当前所有 conditions 字符串（"user_id = ?", "action = ?" 等）均为硬编码字面量，
                # 用户输入通过 params 参数化传递，不存在 SQL 注入风险。
                # 维护者注意：如需新增过滤条件，必须使用 "column = ?" 形式并将值加入 params，
                # 严禁使用 f-string 或字符串拼接将用户输入直接嵌入 WHERE 子句。
                where = " AND ".join(conditions) if conditions else "1=1"

                cursor.execute(f"SELECT COUNT(*) FROM audit_logs WHERE {where}", params)
                total = cursor.fetchone()[0]

                offset = (page - 1) * size
                # FIXED: 原问题-SELECT * 会返回所有列（含 prev_hash/record_hash 等内部字段），
                # 且无法利用覆盖索引；改为显式列名，减少 IO 并避免泄露内部字段
                cursor.execute(
                    f"SELECT id, action, user_id, resource_type, resource_id, "
                    f"ip_address, created_at, status, details, before_value, after_value "
                    f"FROM audit_logs WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                    params + [size, offset],
                )
                columns = [desc[0] for desc in cursor.description]
                rows = [dict(zip(columns, row, strict=False)) for row in cursor.fetchall()]

                return rows, total
            finally:
                if should_close:
                    conn.close()

    async def verify_integrity(self) -> dict:
        return await asyncio.to_thread(self._sync_verify_integrity)

    def _sync_verify_integrity(self) -> dict:
        import sqlite3

        # FIXED-P0: 原代码新建连接且无锁保护，改为使用 _db_lock 保护
        with self._db_lock:
            conn = self._conn
            if conn is None:
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                should_close = True
            else:
                should_close = False
            try:
                cursor = conn.cursor()
                # R6-S-10: SELECT 增加 user_agent/details/error_message/before_value/after_value 字段
                cursor.execute(
                    "SELECT id, created_at, user_id, username, action, "
                    "resource_type, resource_id, ip_address, status, "
                    "user_agent, details, error_message, before_value, after_value, "
                    "prev_hash, record_hash FROM audit_logs ORDER BY id ASC"
                )
                rows = cursor.fetchall()
            finally:
                if should_close:
                    conn.close()

        total = len(rows)
        broken_at = []
        prev_hash = ""
        for row in rows:
            # R6-S-10: record dict 与 log 方法保持一致，纳入新增哈希字段
            record = {
                "created_at": row[1],
                "user_id": row[2],
                "username": row[3],
                "action": row[4],
                "resource_type": row[5],
                "resource_id": row[6],
                "ip_address": row[7],
                "status": row[8],
                "user_agent": row[9],
                "details_json": row[10],
                "error_message": row[11],
                "before_value_json": row[12],
                "after_value_json": row[13],
            }
            expected_hash = self._compute_record_hash(record, prev_hash)
            # R6-S-10: record_hash 列索引从 row[10] 变为 row[15]（新增5列后移）
            if row[15] != expected_hash:
                broken_at.append(row[0])
            prev_hash = row[15]

        return {"valid": len(broken_at) == 0, "total": total, "broken_at": broken_at}

    async def export_csv(
        self, start_time: datetime | None = None, end_time: datetime | None = None
    ) -> str:
        return await asyncio.to_thread(self._sync_export_csv, start_time, end_time)

    def _sync_export_csv(self, start_time, end_time) -> str:
        import sqlite3

        # FIXED-P0: 原代码新建连接且无锁保护，改为使用 _db_lock 保护
        with self._db_lock:
            conn = self._conn
            if conn is None:
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                should_close = True
            else:
                should_close = False
            try:
                self._apply_db_pragmas(conn)  # FIXED: fallback 路径也需应用 PRAGMA 优化 (S-10 回归)
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
                # FIXED(一般): 原问题-SELECT * 无 LIMIT，audit_logs 表长期运行后可能达百万级，
                # 全量加载到内存再写 CSV 会 OOM
                # 修复：添加最大行数上限保护，并使用 fetchmany 流式读取
                # FIXED: SELECT * 改为显式列名，减少 IO 并避免泄露 prev_hash/record_hash 内部字段
                _MAX_EXPORT_ROWS = 1_000_000
                cursor.execute(
                    f"SELECT id, action, user_id, resource_type, resource_id, "
                    f"ip_address, created_at, status, details, before_value, after_value "
                    f"FROM audit_logs WHERE {where} ORDER BY id ASC LIMIT ?",
                    params + [_MAX_EXPORT_ROWS],
                )
                columns = [desc[0] for desc in cursor.description]

                output = io.StringIO()
                writer = csv.writer(output)
                writer.writerow(columns)
                # FIXED(一般): 使用 fetchmany 流式读取，避免一次性加载全部数据到内存
                while True:
                    batch = cursor.fetchmany(1000)
                    if not batch:
                        break
                    for row in batch:
                        # R6-S-04修复: 对每个单元格净化后再写入，防止CSV公式注入
                        writer.writerow([_sanitize_csv_cell(cell) for cell in row])
                return output.getvalue()
            finally:
                if should_close:
                    conn.close()

    async def cleanup(self, retention_days: int = 90) -> int:
        return await asyncio.to_thread(self._sync_cleanup, retention_days)

    def _sync_cleanup(self, retention_days: int) -> int:
        import sqlite3

        # FIXED(严重): 原问题-使用 naive local time，与主库 ORM 的 _utcnow() (UTC) 不一致，
        # 跨时区部署或服务器时区变更时，保留期清理会误删或漏删。
        # 修复：统一使用 UTC。
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        # FIXED-P0: 原代码新建连接且无锁保护，改为使用 _db_lock 保护
        with self._db_lock:
            conn = self._conn
            if conn is None:
                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                should_close = True
            else:
                should_close = False
            try:
                self._apply_db_pragmas(conn)  # FIXED: fallback 路径也需应用 PRAGMA 优化 (S-10 回归)
                cursor = conn.cursor()
                # FIXED: 临时禁用 append-only 触发器以允许合规保留期清理 [2026-06-29]
                # 清理操作本身会记录在 audit_logs 中（_sync_log），保证可追溯
                cursor.execute("PRAGMA foreign_keys=OFF")  # 防止级联约束干扰
                try:
                    cursor.execute("DROP TRIGGER IF EXISTS audit_no_delete")
                    cursor.execute("DELETE FROM audit_logs WHERE created_at < ?", (cutoff,))
                    deleted = cursor.rowcount
                    conn.commit()
                finally:
                    # 重新创建触发器恢复 append-only 保护
                    cursor.execute("""
                        CREATE TRIGGER IF NOT EXISTS audit_no_delete
                        BEFORE DELETE ON audit_logs
                        FOR EACH ROW
                        BEGIN
                            SELECT RAISE(ABORT, 'audit_logs is append-only: DELETE not allowed');
                        END
                    """)
                    cursor.execute("PRAGMA foreign_keys=ON")
                    conn.commit()
                # FIXED-P2: 同时清理过期的登录失败计数（超过 retention_days 未活动的记录）
                # FIXED(严重): cutoff_ts 必须使用 UTC epoch，与 time.time() 一致
                cutoff_ts = (datetime.now(UTC) - timedelta(days=retention_days)).timestamp()
                cursor.execute("DELETE FROM login_fail_counts WHERE last_attempt < ?", (cutoff_ts,))
                conn.commit()
                return deleted
            finally:
                if should_close:
                    conn.close()
