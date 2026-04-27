"""数据库连接管理"""

from __future__ import annotations

import asyncio
import aiosqlite
from pathlib import Path

from edgelite.config import get_config

# SQLite DDL
_TABLES = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    protocol    TEXT NOT NULL CHECK(protocol IN ('modbus_tcp','modbus_rtu','opcua','opc_da','mqtt','mqtt_client','http','http_webhook','simulator','video','s7','mc','fins','allen_bradley','fanuc','mtconnect','toledo','bacnet','serial_port','database_source','barcode_scanner')),
    status      TEXT NOT NULL DEFAULT 'offline' CHECK(status IN ('online','offline','unknown')),
    config      TEXT NOT NULL,
    points      TEXT NOT NULL,
    collect_interval INTEGER NOT NULL DEFAULT 5,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rules (
    rule_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    device_id       TEXT NOT NULL REFERENCES devices(device_id) ON DELETE SET NULL,
    conditions      TEXT NOT NULL,
    logic           TEXT NOT NULL DEFAULT 'AND' CHECK(logic IN ('AND','OR')),
    duration        INTEGER NOT NULL DEFAULT 0,
    severity        TEXT NOT NULL CHECK(severity IN ('critical','warning','info')),
    enabled         INTEGER NOT NULL DEFAULT 1,
    notify_channels TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alarms (
    alarm_id        TEXT PRIMARY KEY,
    rule_id         TEXT NOT NULL,
    device_id       TEXT NOT NULL,
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'firing' CHECK(status IN ('firing','acknowledged','recovered')),
    trigger_value   TEXT NOT NULL,
    trigger_count   INTEGER NOT NULL DEFAULT 1,
    fired_at        TEXT NOT NULL DEFAULT (datetime('now')),
    acknowledged_at TEXT,
    acknowledged_by TEXT,
    recovered_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_alarms_status ON alarms(status);
CREATE INDEX IF NOT EXISTS idx_alarms_device ON alarms(device_id);

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    username   TEXT NOT NULL UNIQUE,
    password   TEXT NOT NULL,
    role       TEXT NOT NULL CHECK(role IN ('admin','operator','viewer')),
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL DEFAULT (datetime('now')),
    user_id       TEXT NOT NULL,
    username      TEXT,
    action        TEXT NOT NULL,
    resource_type TEXT,
    resource_id   TEXT,
    ip_address    TEXT,
    user_agent    TEXT,
    details       TEXT,
    status        TEXT NOT NULL DEFAULT 'success',
    error_message TEXT,
    prev_hash     TEXT,
    record_hash   TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_logs(timestamp);

CREATE TABLE IF NOT EXISTS cache_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    measurement TEXT NOT NULL,
    tags        TEXT NOT NULL,
    fields      TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    """SQLite数据库管理"""

    def __init__(self, db_path: str | None = None):
        config = get_config()
        self.db_path = db_path or config.database.sqlite_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> aiosqlite.Connection:
        """建立数据库连接"""
        # 确保数据目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # 启用WAL模式
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

        return self._conn

    async def init_tables(self) -> None:
        """初始化所有表"""
        assert self._conn is not None
        await self._conn.executescript(_TABLES)
        # 创建默认admin用户（密码: admin123）
        # 注意：实际部署时应强制修改默认密码
        try:
            from edgelite.security.password import hash_password

            hashed = hash_password("admin123")
            await self._conn.execute(
                "INSERT OR IGNORE INTO users (user_id, username, password, role, enabled) "
                "VALUES (?, ?, ?, ?, ?)",
                ("admin", "admin", hashed, "admin", 1),
            )
        except ImportError:
            # 安全模块未就绪时跳过默认用户创建
            pass
        await self._conn.commit()

    async def get_connection(self) -> aiosqlite.Connection:
        """获取数据库连接"""
        if self._conn is None:
            await self.connect()
        assert self._conn is not None
        return self._conn

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def backup(self, backup_path: str) -> None:
        """备份数据库文件"""
        import shutil

        Path(backup_path).parent.mkdir(parents=True, exist_ok=True)
        if self._conn:
            await self._conn.execute("PRAGMA wal_checkpoint=TRUNCATE")
        shutil.copy2(self.db_path, backup_path)
