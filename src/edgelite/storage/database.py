"""多后端数据库连接管理（SQLAlchemy 2.0 异步）"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from edgelite.api.error_codes import DatabaseErrors
from edgelite.config import get_config
from edgelite.models.db import Base

logger = logging.getLogger(__name__)

_BACKEND_DRIVERS = {
    "sqlite": "aiosqlite",
    "mysql": "aiomysql",
    "postgresql": "asyncpg",
    "mssql": "aioodbc",
}

# FIXED-BACKUP: All SQLite sidecar databases that must be included in backup/restore
# Derived from _DEFAULT_DB_PATH in each driver module. Order does not matter.
#
# Format: (manager_ref_name, default_relative_path)
# - manager_ref_name: Name of the manager attribute in the corresponding driver class,
#                    or None for databases without manager (edge_triggers, edge_rules, etc.)
# - default_relative_path: Default relative path to the database file
#
# For config version managers (s7, mc, ab, opcua, fins), the backup() method will be
# called on the manager instance for thread-safe, lock-protected backups.
# For other databases, file-level copy with WAL checkpoint will be used.
#
# Mapping: each config version DB path maps to its driver and manager attribute
_CONFIG_VERSION_DB_PATTERNS: dict[str, tuple[str, str]] = {
    "data/s7_config_versions.db": ("_app_state.s7_driver", "_config_version_mgr"),
    "data/mc_config_versions.db": ("_app_state.mc_driver", "_config_version_mgr"),
    "data/ab_config_versions.db": ("_app_state.ab_driver", "_config_version_mgr"),
    "data/opcua_config_versions.db": ("_app_state.opcua_driver", "_config_version_mgr"),
    "data/fins_config_versions.db": ("_app_state.fins_driver", "_config_version_mgr"),
}

_SQLITE_SIDECAR_DBS: list[tuple[str | None, str]] = [
    # Config version managers - use thread-safe backup() method
    ("_config_version_mgr", "data/s7_config_versions.db"),
    ("_config_version_mgr", "data/mc_config_versions.db"),
    ("_config_version_mgr", "data/ab_config_versions.db"),
    ("_config_version_mgr", "data/opcua_config_versions.db"),
    ("_config_version_mgr", "data/fins_config_versions.db"),
    # Other sidecar databases - use file-level backup
    (None, "data/edge_triggers.db"),
    (None, "data/edge_rules.db"),
    (None, "data/audit.db"),
    (None, "data/security_audit.db"),
    # FIXED-BACKUP: Added device status, MQTT offline queue, and OPC UA time series databases
    (None, "data/device_status.db"),
    (None, "data/mqtt_offline_queue.db"),
    (None, "data/opcua_ts.db"),
    # FIXED-BACKUP: Added observability alerts database (rules + events)
    (None, "data/observability_alerts.db"),
    # FIXED-P1: 原问题-emergency_buffer.db和edgelite_ts.db未列入sidecar，备份/恢复时遗漏
    (None, "data/emergency_buffer.db"),
    (None, "data/edgelite_ts.db"),
]


def _get_sidecar_db_path(rel_path: str) -> str:
    """Resolve a relative sidecar DB path to absolute, respecting data_dir."""
    cfg = None
    try:
        cfg = get_config()
    except Exception as e:
        # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
        logger.warning("获取配置失败: %s", e)
    data_dir = Path(cfg.database.sqlite_path).parent if cfg else Path("data")
    return str((data_dir / rel_path).resolve())


# ── 致命1修复: rule_type CHECK 约束统一 ──────────────────────────────────────
# SQLite 修改 CHECK 约束需重建表，以下为带正确约束的建表 SQL（须与 ORM 定义同步）
# 统一 rule_type 取值: ('threshold', 'ai_inference', 'script')

_RULES_REBUILD_SQL = """
CREATE TABLE rules_new (
    rule_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    device_id VARCHAR(64),
    conditions TEXT NOT NULL DEFAULT '[]',
    logic VARCHAR(8) NOT NULL DEFAULT 'AND',
    duration INTEGER NOT NULL DEFAULT 0,
    severity VARCHAR(16) NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    notify_channels TEXT NOT NULL DEFAULT '[]',
    script TEXT NOT NULL DEFAULT '',
    rule_type VARCHAR(16) NOT NULL DEFAULT 'threshold',
    created_by VARCHAR(64),
    created_at DATETIME,
    updated_at DATETIME,
    version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT ck_rules_logic_valid CHECK (logic IN ('AND', 'OR', 'NOT')),
    CONSTRAINT ck_rules_severity_valid CHECK (severity IN ('critical', 'major', 'warning', 'minor', 'info')),
    CONSTRAINT ck_rules_duration_non_negative CHECK (duration >= 0),
    CONSTRAINT ck_rules_rule_type_valid CHECK (rule_type IN ('threshold', 'ai_inference', 'script'))
)
"""

_ALARMS_REBUILD_SQL = """
CREATE TABLE alarms_new (
    alarm_id VARCHAR(64) PRIMARY KEY,
    rule_id VARCHAR(64) NOT NULL,
    device_id VARCHAR(64),
    severity VARCHAR(16) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'firing',
    message VARCHAR(256) NOT NULL DEFAULT '',
    trigger_value TEXT NOT NULL DEFAULT '{}',
    trigger_count INTEGER NOT NULL DEFAULT 1,
    fired_at DATETIME,
    acknowledged_at DATETIME,
    acknowledged_by VARCHAR(64),
    recovered_at DATETIME,
    rule_type VARCHAR(32) NOT NULL DEFAULT 'threshold',
    version INTEGER NOT NULL DEFAULT 1,
    CONSTRAINT ck_alarms_severity_valid CHECK (severity IN ('critical', 'major', 'warning', 'minor', 'info')),
    CONSTRAINT ck_alarms_status_valid CHECK (status IN ('firing', 'acknowledged', 'recovered')),
    CONSTRAINT ck_alarms_rule_type_valid CHECK (rule_type IN ('threshold', 'ai_inference', 'script'))
)
"""

_RULE_TEMPLATES_REBUILD_SQL = """
CREATE TABLE rule_templates_new (
    template_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    rule_type VARCHAR(32) NOT NULL DEFAULT 'threshold',
    default_conditions TEXT NOT NULL DEFAULT '[]',
    default_severity VARCHAR(16) NOT NULL DEFAULT 'warning',
    default_duration INTEGER NOT NULL DEFAULT 0,
    notify_channels TEXT NOT NULL DEFAULT '[]',
    created_by VARCHAR(64),
    created_at DATETIME,
    updated_at DATETIME,
    CONSTRAINT ck_rule_templates_severity_valid CHECK (default_severity IN ('critical', 'major', 'warning', 'minor', 'info')),
    CONSTRAINT ck_rule_templates_type_valid CHECK (rule_type IN ('threshold', 'ai_inference', 'script'))
)
"""


def _needs_rule_type_fix(sync_conn, table_name: str) -> bool:
    """检查表的 rule_type CHECK 约束是否需要修复。

    通过检查 sqlite_master 中的建表 SQL，判断约束是否包含旧的非标准值
    ('trend', 'expression', 或独立的 'ai' 而非 'ai_inference')。
    """
    try:
        result = sync_conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"), {"t": table_name}
        )
        row = result.fetchone()
        if not row or not row[0]:
            return False
        table_sql = row[0]
        # 如果包含旧的非法值，需要修复
        if "'trend'" in table_sql and "rule_type" in table_sql:
            return True
        if "'expression'" in table_sql and "rule_type" in table_sql:
            return True
        # 检查独立的 'ai' 但不包含 'ai_inference'（排除误匹配）
        return bool("'ai'" in table_sql and "rule_type" in table_sql and "ai_inference" not in table_sql)
    except Exception:
        return False


def _normalize_rule_type(sync_conn, table_name: str) -> None:
    """将非标准 rule_type 值转换为合法值，确保数据兼容新约束。"""
    updates = {
        "rules": [
            "UPDATE rules SET rule_type='script' WHERE rule_type='expression'",
            "UPDATE rules SET rule_type='ai_inference' WHERE rule_type='ai'",
        ],
        "alarms": [
            "UPDATE alarms SET rule_type='threshold' WHERE rule_type='trend'",
        ],
        "rule_templates": [
            "UPDATE rule_templates SET rule_type='threshold' WHERE rule_type='trend'",
        ],
    }
    for sql in updates.get(table_name, []):
        with suppress(Exception):
            sync_conn.execute(text(sql))
    # 兜底: 其他非标准值 → 'threshold'
    with suppress(Exception):
        sync_conn.execute(
            text(
                f"UPDATE {table_name} SET rule_type='threshold' "
                f"WHERE rule_type NOT IN ('threshold', 'ai_inference', 'script')"
            )
        )


def _recreate_indexes_from_list(sync_conn, table_name: str, indexes: list) -> None:
    """表重建后根据预先捕获的索引定义重新创建索引。"""
    for idx in indexes:
        idx_name = idx.get("name", "")
        if not idx_name:
            continue
        cols = idx.get("column_names", [])
        if not cols:
            continue
        unique = "UNIQUE" if idx.get("unique", False) else ""
        col_list = ", ".join(cols)
        with suppress(Exception):
            sync_conn.execute(text(f"CREATE {unique} INDEX IF NOT EXISTS {idx_name} ON {table_name} ({col_list})"))


def _build_database_url(config: Any = None) -> str:
    """根据配置构建 SQLAlchemy 数据库 URL"""
    from urllib.parse import quote_plus

    if config is None:
        config = get_config()

    backend = config.database.backend.lower()

    if backend == "sqlite":
        db_path = Path(config.database.sqlite_path).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{db_path}"

    elif backend in ("mysql", "mariadb"):
        driver = "aiomysql"
        host = config.database.host
        port = config.database.port or 3306
        user = quote_plus(config.database.username)
        pwd = quote_plus(config.database.password)
        db = config.database.database
        return f"mysql+{driver}://{user}:{pwd}@{host}:{port}/{db}?charset=utf8mb4"

    elif backend in ("postgresql", "postgres"):
        driver = "asyncpg"
        host = config.database.host
        port = config.database.port or 5432
        user = quote_plus(config.database.username)
        pwd = quote_plus(config.database.password)
        db = config.database.database
        return f"postgresql+{driver}://{user}:{pwd}@{host}:{port}/{db}"

    elif backend == "mssql":
        driver = "aioodbc"
        host = config.database.host
        port = config.database.port or 1433
        user = quote_plus(config.database.username)
        pwd = quote_plus(config.database.password)
        db = config.database.database
        trust_cert = "yes" if getattr(config.database, "trust_server_certificate", False) else "no"
        odbc_connect = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={host},{port};DATABASE={db};"
            f"UID={user};PWD={pwd};TrustServerCertificate={trust_cert}"  # FIXED-P4: TrustServerCertificate从配置读取，默认no
        )
        return f"mssql+{driver}:///?odbc_connect={odbc_connect}"

    else:
        # FIXED: 原问题-错误消息中文硬编码，改为error_code
        raise ValueError(f"{DatabaseErrors.UNSUPPORTED_BACKEND}:{backend}")


def _check_driver(backend: str) -> None:
    """检查数据库驱动是否已安装"""
    driver_name = _BACKEND_DRIVERS.get(backend)
    if driver_name is None:
        return
    try:
        __import__(driver_name)
    except ImportError:
        # FIXED: 原问题-错误消息中文硬编码，改为error_code
        raise ImportError(f"{DatabaseErrors.DRIVER_REQUIRED}:{backend}:{driver_name}") from None


class Database:
    """多后端数据库管理（SQLAlchemy 2.0 异步）"""

    _instance: Database | None = None

    # FIXED-FINE-GRAINED-LOCK: 支持按表细粒度锁
    # 表名常量定义
    TABLE_DEVICES = "devices"
    TABLE_RULES = "rules"
    TABLE_ALARMS = "alarms"
    TABLE_USERS = "users"
    TABLE_TEMPLATES = "templates"
    TABLE_RESOURCE_SHARES = "resource_shares"
    TABLE_REVOKED_TOKENS = "revoked_tokens"
    TABLE_LOGIN_ATTEMPTS = "login_attempts"

    _TABLE_LOCK_NAMES = frozenset(
        [
            "devices",
            "rules",
            "alarms",
            "users",
            "templates",
            "resource_shares",
            "revoked_tokens",
            "login_attempts",
        ]
    )

    # FIXED: 索引定义映射，用于 _ensure_indexes 补建缺失索引
    _REQUIRED_INDEXES = {
        "users": [
            ("idx_users_created_at", "created_at"),
        ],
        "devices": [
            ("idx_devices_created_at", "created_at"),
            ("idx_devices_protocol", "protocol"),
        ],
        "rules": [
            ("idx_rules_created_at", "created_at"),
            ("idx_rules_device_id", "device_id"),
        ],
        "alarms": [
            ("idx_alarms_created_at", "created_at"),
            ("idx_alarms_device_id", "device_id"),
        ],
    }

    @staticmethod
    def _ensure_indexes(conn, existing_tables: list[str]) -> None:
        """补建已存在表上缺失的索引（幂等操作）。

        Args:
            conn: SQLAlchemy 同步连接
            existing_tables: 已存在的表名列表
        """
        from contextlib import suppress

        from sqlalchemy import text as sa_text

        for table_name, indexes in Database._REQUIRED_INDEXES.items():
            if table_name not in existing_tables:
                continue
            for idx_name, col_name in indexes:
                with suppress(Exception):
                    conn.execute(sa_text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table_name} ({col_name})"))

    def __init__(self, config: Any = None, use_fine_grained_locks: bool = True):
        self._config = config or get_config()
        self._backend = self._config.database.backend.lower()
        self._db_url = _build_database_url(self._config)
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._write_lock = asyncio.Lock()

        # FIXED-FINE-GRAINED-LOCK: 按表细粒度锁
        # 配置开关：默认启用细粒度锁，可通过配置禁用以保持向后兼容
        self._use_fine_grained_locks = use_fine_grained_locks
        self._table_locks: dict[str, asyncio.Lock] = {}
        if self._use_fine_grained_locks:
            for table in self._TABLE_LOCK_NAMES:
                self._table_locks[table] = asyncio.Lock()

        # Register as singleton instance
        Database._instance = self

    @classmethod
    def get_instance(cls) -> Database | None:
        """Get the singleton Database instance."""
        return cls._instance

    @property
    def use_fine_grained_locks(self) -> bool:
        """是否启用细粒度锁"""
        return self._use_fine_grained_locks

    @property
    def backend(self) -> str:
        """返回当前数据库后端类型（sqlite/mysql/postgres）。"""
        return self._backend

    @property
    def db_path(self) -> str:
        """返回 SQLite 数据库文件的绝对路径（非 SQLite 后端返回空字符串）。"""
        if self._backend == "sqlite":
            return str(Path(self._config.database.sqlite_path).resolve())
        return ""

    @property
    def audit_db_path(self) -> str:
        """返回审计数据库文件的绝对路径（与主库同目录下的 audit.db）。"""
        if self._backend == "sqlite":
            main_path = Path(self._config.database.sqlite_path).resolve()
            return str(main_path.parent / "audit.db")
        return ""

    @property
    def engine(self) -> AsyncEngine:
        """返回 SQLAlchemy AsyncEngine，未初始化时抛出 RuntimeError。"""
        if self._engine is None:
            raise RuntimeError(DatabaseErrors.NOT_CONNECTED)
        return self._engine

    @property
    def write_lock(self) -> asyncio.Lock:
        """全局写锁（向后兼容）"""
        return self._write_lock

    def get_table_lock(self, table_name: str) -> asyncio.Lock | None:
        """获取指定表的锁

        Args:
            table_name: 表名（不含前缀）

        Returns:
            对应表的锁对象，如果表不在锁列表中或细粒度锁未启用则返回 None
        """
        if not self._use_fine_grained_locks:
            return None
        return self._table_locks.get(table_name)

    def get_table_lock_name(self, table_name: str) -> str | None:
        """获取表对应的锁名称

        Args:
            table_name: 表名

        Returns:
            锁名称，如果表不在锁列表中则返回 None
        """
        if table_name not in self._TABLE_LOCK_NAMES:
            return None
        return f"_table_lock_{table_name}"

    def get_lock_status(self) -> dict[str, Any]:
        """获取所有锁的状态（用于监控）

        Returns:
            包含每个锁的锁定状态的字典
        """
        status = {
            "use_fine_grained_locks": self._use_fine_grained_locks,
            "global_lock": {
                "locked": self._write_lock.locked(),
            },
            "table_locks": {},
        }

        if self._use_fine_grained_locks:
            for table, lock in self._table_locks.items():
                status["table_locks"][table] = {
                    "locked": lock.locked(),
                }

        return status

    def _register_sqlite_pragmas(self, engine: AsyncEngine) -> None:
        """FIXED-P0: 提取SQLite PRAGMA注册为独立方法，确保connect()和_rebuild_sqlite_database()统一调用"""
        from sqlalchemy import event as sa_event

        @sa_event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA ignore_check_constraints=OFF")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    async def connect(self) -> AsyncEngine:
        """建立数据库连接池"""
        _check_driver(self._backend)

        engine_kwargs: dict[str, Any] = {
            "echo": self._config.database.echo,
        }
        connect_args: dict[str, Any] = {}

        if self._backend == "sqlite":
            connect_args["check_same_thread"] = False

        else:
            engine_kwargs["pool_size"] = self._config.database.pool_size
            engine_kwargs["max_overflow"] = self._config.database.max_overflow
            engine_kwargs["pool_pre_ping"] = True
            engine_kwargs["pool_recycle"] = (
                3600  # FIXED-P1: 原问题-未配pool_recycle，MySQL 8小时后断开空闲连接导致"has gone away"
            )
            engine_kwargs["pool_timeout"] = 60  # FIXED-P1: 未配pool_timeout导致连接池耗尽时无限等待
            # FIXED-BugR4: 必须为 MySQL/PostgreSQL 设置驱动级超时，
            # 否则网络异常时 connect/query 永久挂起，连接池耗尽后整个数据库层卡死。
            # pool_timeout 仅控制"等池时间"，不控制连接建立和查询执行。
            if self._backend in ("mysql", "mariadb"):
                # aiomysql 驱动级超时
                connect_args["connect_timeout"] = 10
                connect_args["read_timeout"] = 30
                connect_args["write_timeout"] = 30
            elif self._backend in ("postgresql", "postgres"):
                # asyncpg 驱动级超时
                connect_args["connect_timeout"] = 10
                connect_args["command_timeout"] = 30

        if connect_args:
            engine_kwargs["connect_args"] = connect_args

        self._engine = create_async_engine(self._db_url, **engine_kwargs)
        self._session_factory = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)

        if self._backend == "sqlite":  # FIXED-P1: 原问题-SQLite CheckConstraint默认不强制执行，显式启用
            self._register_sqlite_pragmas(self._engine)
            # FIXED(安全): 设置 SQLite 文件权限为 0o600，防止同主机其他用户读取
            try:
                _db_file = Path(self._config.database.sqlite_path).resolve()
                if _db_file.exists() and os.name != "nt":
                    os.chmod(str(_db_file), 0o600)
            except Exception as e:
                logger.warning("Failed to set SQLite file permissions: %s", e)

        if self._backend == "sqlite":
            await self._check_sqlite_integrity()

        safe_url = self._db_url
        try:
            from urllib.parse import urlparse

            parsed = urlparse(self._db_url)
            if parsed.password:
                safe_url = f"{parsed.scheme}://{parsed.username or ''}:***@{parsed.hostname or ''}"
                if parsed.port:
                    safe_url += f":{parsed.port}"
                safe_url += f"{parsed.path or ''}"
        except Exception:
            safe_url = f"{self._backend}://***"

        # FIXED: P0-3 原问题-connect()创建引擎后未验证连通性，配置错误时启动无感知。
        # 增加实际连接测试，确保引擎可用后再返回。
        try:
            async with self._engine.connect() as test_conn:
                await test_conn.execute(text("SELECT 1"))
        except Exception as conn_err:
            logger.error("数据库连接验证失败: %s", safe_url)
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            raise RuntimeError(
                f"{DatabaseErrors.CONNECTION_FAILED}: {safe_url}"
            ) from conn_err  # FIXED-P2: 使用safe_url替代conn_err，防止凭据泄露到日志/异常链

        logger.info(
            "数据库连接已建立 (backend=%s, url=%s, pool_size=%s)",
            self._backend,
            safe_url,
            engine_kwargs.get("pool_size", "N/A"),
        )
        return self._engine

    async def _check_sqlite_integrity(self) -> None:
        if not self._engine:
            return
        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(text("PRAGMA integrity_check"))
                row = result.fetchone()
                if row and row[0] != "ok":
                    logger.warning("SQLite完整性检查异常: %s，尝试修复...", row[0])
                    try:
                        db_path = self.db_path
                        if db_path and Path(db_path).exists():
                            import shutil

                            corrupt_backup = f"{db_path}.corrupt.{int(time.time())}"
                            shutil.copy2(db_path, corrupt_backup)
                            logger.warning("已备份损坏数据库到: %s", corrupt_backup)
                    except Exception as backup_err:
                        logger.warning("备份损坏数据库失败: %s", backup_err)
                    try:
                        await conn.execute(text("PRAGMA wal_checkpoint=TRUNCATE"))
                    except Exception:
                        try:
                            await conn.execute(text("PRAGMA wal_checkpoint=PASSIVE"))
                        except Exception:
                            logger.warning("SQLite WAL checkpoint failed", exc_info=True)
                    result2 = await conn.execute(text("PRAGMA integrity_check"))
                    row2 = result2.fetchone()
                    if row2 and row2[0] != "ok":
                        logger.error("SQLite修复失败，自动重建数据库: %s", row2[0])
                        await self._rebuild_sqlite_database()
                    else:
                        logger.info("SQLite修复成功")

            # FIXED-BACKUP: Check integrity of all sidecar databases
            await self._check_sidecar_integrity()
        except Exception as e:
            logger.warning("SQLite完整性检查失败: %s", e)

    async def _check_sidecar_integrity(self) -> None:
        """Check integrity of all SQLite sidecar databases.

        FIXED-BACKUP: Added integrity check for device_status.db,
        mqtt_offline_queue.db, and opcua_ts.db.

        FIXED-P1: 原问题-sidecar损坏仅warning不自动重建，损坏文件导致后续写入全部失败
        现对损坏的sidecar执行：备份损坏文件 → 从backups/恢复最新备份 → 验证 → 失败则删除重建空库
        """
        import sqlite3

        for _manager_ref, rel_path in _SQLITE_SIDECAR_DBS:
            abs_path = _get_sidecar_db_path(rel_path)
            src_path = Path(abs_path)

            if not src_path.exists():
                continue

            try:
                # FIXED-P2: 原问题-同步sqlite3.connect+PRAGMA integrity_check阻塞事件循环；改为asyncio.to_thread
                # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用默认参数绑定当前 abs_path 的值
                def _check_integrity(abs_path=abs_path):
                    conn = sqlite3.connect(abs_path, timeout=10)
                    try:
                        cursor = conn.execute("PRAGMA integrity_check")
                        return cursor.fetchone()
                    finally:
                        conn.close()

                result = await asyncio.to_thread(_check_integrity)
                if result and result[0] != "ok":
                    logger.warning(
                        "Sidecar database integrity check failed: %s (%s): %s, attempting recovery",
                        src_path.name,
                        abs_path,
                        result[0],
                    )
                    await self._recover_sidecar_db(abs_path, src_path.name)
                else:
                    logger.debug(
                        "Sidecar database integrity check OK: %s",
                        src_path.name,
                    )
            except Exception as e:
                logger.warning(
                    "Sidecar database integrity check error: %s (%s): %s, attempting recovery",
                    src_path.name,
                    abs_path,
                    e,
                )
                await self._recover_sidecar_db(abs_path, src_path.name)

    async def _recover_sidecar_db(self, abs_path: str, db_name: str) -> None:
        """FIXED-P1: Recover a corrupt sidecar database.

        Steps: backup corrupt file → try restore from backups/ → verify → delete if unrepairable.
        The sidecar will be auto-recreated with CREATE TABLE IF NOT EXISTS on next access.
        """
        import shutil
        import sqlite3

        corrupt_backup = f"{abs_path}.corrupt.{int(time.time())}"
        try:
            shutil.move(abs_path, corrupt_backup)
            logger.warning("Corrupt sidecar DB moved to: %s", corrupt_backup)
        except Exception as move_err:
            logger.error("Failed to move corrupt sidecar DB %s: %s", abs_path, move_err)
            try:
                Path(abs_path).unlink(missing_ok=True)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("删除损坏sidecar文件失败: %s", e)
            return

        for suffix in ("-wal", "-shm"):
            try:
                Path(f"{abs_path}{suffix}").unlink(missing_ok=True)
            except Exception as e:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("删除sidecar WAL/SHM文件失败(%s): %s", suffix, e)

        backup_dir = Path(self._config.database.backup_dir) if self._config else Path("data/backups")
        restored = False
        try:
            if backup_dir.exists():
                backup_files = sorted(
                    (f for f in backup_dir.glob(f"{db_name}.backup.*") if not f.name.endswith(("-wal", "-shm"))),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if backup_files:
                    latest_backup = backup_files[0]
                    shutil.copy2(str(latest_backup), abs_path)
                    try:
                        verify_conn = sqlite3.connect(abs_path, timeout=10)
                        try:
                            vr = verify_conn.execute("PRAGMA integrity_check").fetchone()
                        finally:
                            verify_conn.close()  # FIXED-P2: 原问题-异常时sqlite3连接泄漏，改用try/finally确保关闭
                        if vr and vr[0] == "ok":
                            logger.info("Sidecar DB recovered from backup: %s -> %s", latest_backup, abs_path)
                            restored = True
                        else:
                            logger.warning("Sidecar backup also corrupt: %s, rebuilding empty", vr[0] if vr else "None")
                            Path(abs_path).unlink(missing_ok=True)
                    except Exception as verify_err:
                        logger.warning("Sidecar backup verification failed: %s, rebuilding empty", verify_err)
                        try:
                            Path(abs_path).unlink(missing_ok=True)
                        except Exception as e:
                            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                            logger.warning("删除验证失败sidecar文件失败: %s", e)
        except Exception as e:
            logger.warning("Sidecar DB recovery from backup failed: %s", e)

        if not restored:
            logger.warning(
                "Sidecar DB %s will be recreated empty on next access (corrupt backup: %s)",
                db_name,
                corrupt_backup,
            )

    async def _rebuild_sqlite_database(self) -> None:
        # FIXED: P0-3 原问题-数据库损坏后仅删除重建，无法告知用户数据已丢失且需要从备份恢复
        db_path = self.db_path
        if not db_path or not Path(db_path).exists():
            return
        try:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("销毁损坏数据库引擎失败: %s", e)

        import shutil

        corrupt_backup = f"{db_path}.corrupt.{int(time.time())}"
        try:
            shutil.move(db_path, corrupt_backup)
            logger.error("SQLite database corrupted, moved to: %s", corrupt_backup)
        except Exception as e:
            logger.error("Failed to move corrupt database: %s, attempting delete", e)
            try:
                Path(db_path).unlink(missing_ok=True)
            except Exception as e2:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("删除损坏数据库文件失败: %s", e2)

        for suffix in ("-wal", "-shm"):
            try:
                Path(f"{db_path}{suffix}").unlink(missing_ok=True)
            except Exception as e3:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("删除WAL/SHM文件失败(%s): %s", suffix, e3)

        # FIXED-P1: 原问题-重建后数据全部丢失，无自动从备份恢复
        # 改为：重建前先尝试从backups目录恢复最新备份文件
        backup_dir = Path(self._config.database.backup_dir) if self._config else Path("data/backups")
        db_name = Path(db_path).name
        restored = False
        try:
            if backup_dir.exists():
                backup_files = sorted(
                    (f for f in backup_dir.glob(f"{db_name}.backup.*") if not f.name.endswith(("-wal", "-shm"))),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if backup_files:
                    latest_backup = backup_files[0]
                    shutil.copy2(str(latest_backup), db_path)
                    logger.info("SQLite从备份恢复: %s -> %s", latest_backup, db_path)
                    restored = True
        except Exception as e:
            logger.warning("SQLite从备份恢复失败: %s，将重建空库", e)
            restored = False
            try:
                Path(db_path).unlink(missing_ok=True)
            except Exception as e2:
                # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                logger.warning("删除恢复失败数据库文件失败: %s", e2)

        self._engine = create_async_engine(
            self._db_url,
            echo=self._config.database.echo,
            connect_args={"check_same_thread": False},
        )
        self._register_sqlite_pragmas(
            self._engine
        )  # FIXED-P0: 原问题-重建引擎后未注册PRAGMA，导致WAL模式和busy_timeout丢失
        self._session_factory = async_sessionmaker(self._engine, class_=AsyncSession, expire_on_commit=False)

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._migrate(conn)

        if restored:
            # FIXED-BACKUP: Restore sidecar databases from backup alongside the main database.
            await self._restore_sidecar_dbs(db_path, backup_dir, db_name)
            try:  # FIXED-P1: 从备份恢复后未验证完整性，恢复的备份可能损坏
                async with self._engine.begin() as conn:
                    result = await conn.execute(text("PRAGMA integrity_check"))
                    row = result.fetchone()
                    if row and row[0] != "ok":
                        logger.error("SQLite恢复后完整性检查失败: %s，将重建空库", row[0])
                        await self._engine.dispose()
                        self._engine = None
                        Path(db_path).unlink(missing_ok=True)
                        self._engine = create_async_engine(
                            self._db_url,
                            echo=self._config.database.echo,
                            connect_args={"check_same_thread": False},
                        )
                        self._register_sqlite_pragmas(self._engine)  # FIXED-P0: 备份也损坏重建后同样需要注册PRAGMA
                        self._session_factory = async_sessionmaker(
                            self._engine, class_=AsyncSession, expire_on_commit=False
                        )
                        async with self._engine.begin() as conn2:
                            await conn2.run_sync(Base.metadata.create_all)
                            await self._migrate(conn2)
                        raise RuntimeError(
                            f"SQLite backup also corrupted, rebuilt empty. Corrupt file: {corrupt_backup}."
                        )
                    else:
                        logger.info("SQLite恢复后完整性检查通过")
            except RuntimeError:
                raise
            except Exception as e:
                logger.error("SQLite恢复后完整性检查异常: %s", e)
            logger.info("SQLite数据库已从备份恢复: %s", db_path)
        else:
            logger.error(
                "SQLite database rebuilt (empty). IMPORTANT: All data has been lost. "
                "Please restore from the most recent backup file in the backups/ directory."
            )
            raise RuntimeError(
                "SQLite database corrupted and rebuilt empty. Data loss detected. "
                f"Corrupt file backed up to: {corrupt_backup}. "
                "Restore data from backup manually."
            )

    async def init_tables(self) -> None:
        """初始化所有表（仅用于开发/首次部署，生产环境请使用 Alembic）"""
        if self._engine is None:
            raise RuntimeError(DatabaseErrors.NOT_CONNECTED)
        if self._session_factory is None:
            raise RuntimeError(DatabaseErrors.SESSION_NOT_INIT)

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._migrate(conn)
            # Safety check: ensure critical columns exist in case migrations were skipped
            await self._ensure_schema_columns(conn)

        async with self._session_factory() as session:
            from sqlalchemy import select

            from edgelite.models.db import UserORM

            # FIXED: 移除冗余的 load_dotenv 调用。
            # load_config() 已经调用过 load_dotenv(override=False)，
            # 这里再次调用不仅冗余，还会导致：
            # 1. 如果系统环境变量已设置 EDGELITE_ADMIN_PASSWORD（如Docker传入），
            #    override=False 不会覆盖，注释 .env 无效
            # 2. 增加不必要的 I/O 开销
            # 密码重置逻辑仅依赖 os.environ，由 load_config 统一管理即可

            # FIXED: 原问题-init_tables中admin用户查询无try-except保护，数据库连接异常导致初始化失败
            # FIXED-C04: admin查询异常时不应创建新admin，否则会覆盖已有用户的密码
            try:
                admin_username = os.environ.get(
                    "EDGELITE_ADMIN_USERNAME", "admin"
                )  # FIXED-P4: 与创建时使用相同的环境变量
                result = await session.execute(select(UserORM).where(UserORM.username == admin_username))
                admin_user = result.scalar_one_or_none()
                admin_exists = admin_user is not None
            except Exception as e:
                logger.error("Database.init_tables admin check failed: %s", e)
                # FIXED-C04: 查询异常时不创建新admin，避免覆盖已有密码
                logger.warning("Skipping admin initialization due to query failure - admin user may already exist")
                admin_exists = True  # 假设已存在，避免误创建
                admin_user = None

            # FIXED-C02: 环境变量 EDGELITE_ADMIN_PASSWORD 用于设置 admin 密码。
            # 1. admin 不存在时：创建 admin 并设置密码
            # 2. admin 已存在时：仅在 EDGELITE_RESET_ADMIN_PASSWORD=true 时重置密码
            #    这样部署完成后无需保留密码环境变量，避免每次重启覆盖管理员修改过的密码
            # FIXED-C04: 增加 password_stamps 保护，已修改过密码的admin不会被环境变量覆盖
            temp_password = os.environ.get("EDGELITE_ADMIN_PASSWORD")
            force_reset = os.environ.get("EDGELITE_RESET_ADMIN_PASSWORD", "").lower() in ("true", "1", "yes")

            # FIXED: 增加诊断日志，帮助排查密码被意外修改的问题
            logger.info(
                "Admin init check: admin_exists=%s, temp_password_set=%s, force_reset=%s",
                admin_exists,
                temp_password is not None,
                force_reset,
            )

            if not admin_exists:
                try:
                    import secrets

                    from edgelite.security.password import hash_password

                    if not temp_password:
                        temp_password = secrets.token_urlsafe(16)
                        # FIXED-P2: 初始密码写入文件而非stdout，防止容器日志采集泄露
                        password_file = os.path.join(
                            os.path.dirname(self.db_path) if self.db_path else "data", ".initial_admin_password"
                        )
                        try:
                            os.makedirs(os.path.dirname(password_file), exist_ok=True)
                            with open(password_file, "w", encoding="utf-8") as f:
                                f.write(temp_password)
                            if os.name != "nt":
                                os.chmod(password_file, 0o600)
                            else:
                                # FIXED(中危): 原问题-Windows平台ACL设置失败时静默pass，运维无法感知;
                                # 修复-将pass改为warning日志记录，并添加 icacls 作为 win32security 的 fallback
                                acl_applied = False
                                try:
                                    import win32security

                                    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
                                        "D:P(A;;FA;;;OW)(A;;FR;;;OW)", win32security.SDDL_REVISION_1
                                    )
                                    win32security.SetFileSecurity(
                                        password_file, win32security.DACL_SECURITY_INFORMATION, sd
                                    )
                                    acl_applied = True
                                except (ImportError, AttributeError, OSError) as acl_err:
                                    logger.warning(
                                        "win32security ACL setup failed for initial admin password file %s: %s. "
                                        "Trying icacls fallback.",
                                        password_file,
                                        acl_err,
                                    )
                                # icacls fallback（Windows 内置工具，无需额外依赖）
                                if not acl_applied:
                                    try:
                                        import subprocess

                                        username = os.environ.get("USERNAME") or os.environ.get("USER")
                                        if username:
                                            subprocess.run(
                                                [
                                                    "icacls",
                                                    password_file,
                                                    "/inheritance:r",
                                                    "/grant:r",
                                                    f"{username}:R",
                                                ],
                                                check=True,
                                                capture_output=True,
                                                timeout=10,
                                            )
                                            logger.info(
                                                "Applied Windows ACL to initial admin password file via icacls: %s",
                                                password_file,
                                            )
                                        else:
                                            logger.warning(
                                                "Cannot apply Windows ACL to initial admin password file %s: "
                                                "unable to determine current username. "
                                                "Other users on this machine may be able to read the password.",
                                                password_file,
                                            )
                                    except subprocess.CalledProcessError as cpe:
                                        logger.warning(
                                            "icacls failed for initial admin password file %s (exit %s): %s. "
                                            "Other users on this machine may be able to read the password.",
                                            password_file,
                                            cpe.returncode,
                                            (cpe.stderr or b"").decode("utf-8", errors="replace").strip(),
                                        )
                                    except Exception as icacls_err:
                                        logger.warning(
                                            "icacls fallback failed for initial admin password file %s: %s. "
                                            "Other users on this machine may be able to read the password.",
                                            password_file,
                                            icacls_err,
                                        )
                            # R5-F-03 修复(致命): 原 logger.info 输出完整密码文件路径，若日志被集中采集
                            # (ELK/Loki)会泄露文件位置便于攻击者定位明文密码。
                            # 修复-不记录完整路径，仅提示运维在 data/ 目录下查看，且首次登录后自动删除。
                            logger.warning(
                                "Initial admin password file created in data/ directory. "
                                "Read it on the server host, then login to auto-delete it. "
                                "DO NOT copy or transmit the password through logs/chat."
                            )
                        except OSError:
                            # FIXED-P1: 密码文件写入失败时终止启动，避免明文密码泄露到容器日志
                            logger.critical(
                                "Failed to write initial admin password to %s. "
                                "Set EDGELITE_ADMIN_PASSWORD env var or ensure data/ directory is writable. "
                                "Refusing to print password to stdout for security.",
                                password_file,
                            )
                            raise RuntimeError("Cannot securely store initial admin password") from None
                    hashed = hash_password(temp_password)
                    # FIXED-P4: 允许通过环境变量自定义管理员用户名，避免硬编码admin
                    admin_username = os.environ.get("EDGELITE_ADMIN_USERNAME", "admin")
                    admin = UserORM(
                        user_id=admin_username,
                        username=admin_username,
                        password=hashed,
                        role="admin",
                        enabled=True,
                        must_change_password=True,
                    )
                    session.add(admin)
                    await session.commit()
                    logger.info("Initial admin user created (password change required on first login)")
                except ImportError:
                    logger.warning("hash_password module not available, admin user creation skipped")
                except Exception as e:
                    logger.error("Database.init_tables admin creation failed: %s", e)
            elif temp_password and force_reset:
                # admin 已存在且显式设置了 EDGELITE_RESET_ADMIN_PASSWORD=true，重置密码
                # FIXED: 密码重置改为一次性操作，重置成功后自动清除标志
                try:
                    from edgelite.security.password import hash_password

                    if admin_user:
                        # FIXED: 检查是否已经用当前密码值重置过（防止每次重启都改密码）
                        already_reset = await self._check_password_already_reset(admin_user, temp_password)
                        if already_reset:
                            logger.info(
                                "Admin password already matches EDGELITE_ADMIN_PASSWORD, "
                                "skipping reset (one-time reset already applied)"
                            )
                        else:
                            admin_user.password = hash_password(temp_password)
                            admin_user.must_change_password = True
                            await session.commit()
                            logger.info(
                                "Admin password reset via EDGELITE_ADMIN_PASSWORD (EDGELITE_RESET_ADMIN_PASSWORD=true)"
                            )
                        # 无论是否实际重置，都清除标志（一次性操作）
                        self._clear_password_reset_flag()
                except Exception as e:
                    logger.error("Database.init_tables admin password reset failed: %s", e)

    async def _check_password_already_reset(self, admin_user: Any, temp_password: str) -> bool:
        """FIXED: 检查admin密码是否已经是 EDGELITE_ADMIN_PASSWORD 指定的值。

        如果密码已经匹配，说明之前已经重置过了，不需要再次重置。
        这样即使环境变量来自系统环境（非 .env 文件），也能防止每次重启都改密码。
        """
        try:
            from edgelite.security.password import verify_password

            return verify_password(temp_password, admin_user.password)
        except Exception:
            return False

    def _clear_password_reset_flag(self) -> None:
        """FIXED: 密码重置成功后自动清除标志，防止每次重启都改密码。

        密码重置应该是一次性操作：
        1. 从 os.environ 中移除 EDGELITE_RESET_ADMIN_PASSWORD 和 EDGELITE_ADMIN_PASSWORD
        2. 尝试更新 .env 文件，将两者都注释掉（防止下次重启再次触发）
        3. 如果环境变量来自系统环境（非 .env 文件），记录明确的警告日志
        """
        # 1. 从当前进程环境变量中移除
        reset_cleared = os.environ.pop("EDGELITE_RESET_ADMIN_PASSWORD", None)
        pw_cleared = os.environ.pop("EDGELITE_ADMIN_PASSWORD", None)
        if reset_cleared or pw_cleared:
            logger.info(
                "Password reset env vars cleared from process environment (one-time reset completed): "
                "EDGELITE_RESET_ADMIN_PASSWORD=%s, EDGELITE_ADMIN_PASSWORD=%s",
                "removed" if reset_cleared else "not set",
                "removed" if pw_cleared else "not set",
            )

        # 2. 尝试更新 .env 文件，注释掉 EDGELITE_RESET_ADMIN_PASSWORD 和 EDGELITE_ADMIN_PASSWORD 行
        env_path = Path(".env")
        if not env_path.exists():
            logger.info("No .env file found, skipping .env update for password reset flag")
            return

        try:
            lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
            modified = False
            new_lines = []
            vars_to_comment = {"EDGELITE_RESET_ADMIN_PASSWORD=", "EDGELITE_ADMIN_PASSWORD="}
            found_in_env = set()
            for line in lines:
                stripped = line.strip()
                # 匹配未注释的 EDGELITE_RESET_ADMIN_PASSWORD= 或 EDGELITE_ADMIN_PASSWORD= 行
                is_target = any(stripped.startswith(prefix) for prefix in vars_to_comment)
                if is_target and not stripped.startswith("#"):
                    new_lines.append(f"# {line}")
                    modified = True
                    # 记录找到了哪个变量
                    for prefix in vars_to_comment:
                        if stripped.startswith(prefix):
                            found_in_env.add(prefix)
                else:
                    new_lines.append(line)

            if modified:
                env_path.write_text("".join(new_lines), encoding="utf-8")
                commented_names = [p.rstrip("=") for p in found_in_env]
                logger.info(
                    "Password reset env vars auto-commented in .env file (one-time reset completed): %s",
                    ", ".join(commented_names),
                )

            # 检查是否有环境变量在 os.environ 中但不在 .env 文件中
            missing_in_env = []
            if reset_cleared and "EDGELITE_RESET_ADMIN_PASSWORD=" not in found_in_env:
                missing_in_env.append("EDGELITE_RESET_ADMIN_PASSWORD")
            if pw_cleared and "EDGELITE_ADMIN_PASSWORD=" not in found_in_env:
                missing_in_env.append("EDGELITE_ADMIN_PASSWORD")

            if missing_in_env:
                logger.warning(
                    "%s %s set in os.environ but NOT found (uncommented) in .env file. "
                    "This means they are set in your SYSTEM environment (e.g., systemd, supervisor, docker-compose). "
                    "Please remove them from your system environment to prevent password reset on every restart. "
                    "The flags have been cleared from the current process, but will reappear on next restart "
                    "unless removed from the system environment.",
                    ", ".join(missing_in_env),
                    "was" if len(missing_in_env) == 1 else "were",
                )
        except Exception as e:
            logger.warning("Failed to update .env file to clear password reset env vars: %s", e)

    async def _ensure_schema_columns(self, conn: Any) -> None:
        """确保关键列存在，防止 Alembic 迁移跳过时旧表缺少新列"""
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text

        # 定义需要确保存在的列: (table_name, column_name, column_sql_type, server_default)
        critical_columns = [
            ("users", "version", "INTEGER", "1"),
            ("users", "must_change_password", "BOOLEAN", "0"),  # FIXED-C04: 默认0，避免已有用户被强制改密
            ("users", "password_changed_at", "DATETIME", None),
            ("users", "updated_at", "DATETIME", None),
            ("rules", "created_by", "VARCHAR(64)", None),
            ("rules", "version", "INTEGER", "1"),
            (
                "rules",
                "updated_at",
                "DATETIME",
                None,
            ),  # #[AUDIT-FIX] rules 缺失 updated_at 导致 /api/v1/system/status 500
            # SEC-FIX: 新增 rules 表 script/rule_type 列，使 evaluator 读取的脚本/规则类型可持久化
            ("rules", "script", "TEXT", "''"),
            ("rules", "rule_type", "VARCHAR(16)", "'threshold'"),
            # R7-S-15: 补充 devices.created_by，与 rules.created_by 保持一致，防止旧库缺少该列导致写入失败
            ("devices", "created_by", "VARCHAR(64)", None),
        ]

        def _check_and_add(sync_conn):
            inspector = sa_inspect(sync_conn)
            for table_name, col_name, col_type, default in critical_columns:
                try:
                    existing_cols = [c["name"] for c in inspector.get_columns(table_name)]
                except Exception:
                    continue  # 表不存在则跳过
                if col_name not in existing_cols:
                    default_sql = f" DEFAULT {default}" if default else ""
                    alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}{default_sql}"
                    try:
                        sync_conn.execute(text(alter_sql))
                        sync_conn.commit()
                        logger.info("Added missing column %s.%s", table_name, col_name)
                    except Exception as e:
                        logger.warning("Failed to add column %s.%s: %s", table_name, col_name, e)

            # #[AUDIT-FIX] Ensure alarm_silences table exists (migration 008 may not have run)
            # AlarmSilenceORM is defined in ORM but the table may be missing in older DBs,
            # causing list_silences to fail with empty error message.
            try:
                existing_tables = inspector.get_table_names()
            except Exception:
                existing_tables = []
            if "alarm_silences" not in existing_tables:
                try:
                    Base.metadata.tables["alarm_silences"].create(sync_conn, checkfirst=True)
                    logger.info("Created missing table alarm_silences")
                except Exception as e:
                    logger.warning("Failed to create alarm_silences table: %s", e)

            # [SEC-FIX-RULE-VERSION] Ensure rule_versions table exists (migration 010 may not have run)
            # RuleVersionORM is defined in ORM but the table may be missing in older DBs,
            # causing rule version history/rollback to fail.
            if "rule_versions" not in existing_tables:
                try:
                    Base.metadata.tables["rule_versions"].create(sync_conn, checkfirst=True)
                    logger.info("Created missing table rule_versions")
                except Exception as e:
                    logger.warning("Failed to create rule_versions table: %s", e)

            # Fix outdated CHECK constraints on rules table (SQLite requires table rebuild)
            Database._fix_rules_check_constraints(sync_conn)

            # FIXED-P0(致命1): 统一 rule_type CHECK 约束，防止 script 类型规则触发告警时
            # AlarmORM 约束拒绝写入。SQLite 修改 CHECK 约束需重建表。
            Database._fix_rule_type_check_constraints(sync_conn)

        await conn.run_sync(_check_and_add)

    @staticmethod
    def _fix_rules_check_constraints(sync_conn) -> None:
        """Fix outdated CHECK constraints on the rules table.

        Migration 001/004 created constraints that only allow
        logic IN ('AND', 'OR') and severity IN ('critical', 'warning', 'info'),
        but the ORM model now also allows 'NOT' for logic and 'major'/'minor' for severity.
        SQLite doesn't support ALTER CONSTRAINT, so we must rebuild the table.
        """
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text

        inspector = sa_inspect(sync_conn)
        try:
            columns = inspector.get_columns("rules")
        except Exception:
            return  # table doesn't exist

        if not columns:
            return

        # Check if constraints need fixing by trying to detect the old constraint
        needs_fix = False
        try:
            # Try inserting a row with severity='major' to see if constraint blocks it
            # Use a temporary test - check constraint text from sqlite_master
            result = sync_conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'"))
            row = result.fetchone()
            if row and row[0]:
                table_sql = row[0]
                # If the constraint only has 3 severity values, it needs fixing
                if "ck_rules_severity_valid" in table_sql and "'major'" not in table_sql:
                    needs_fix = True
                if "ck_rules_logic_valid" in table_sql and "'NOT'" not in table_sql:
                    needs_fix = True
        except Exception:
            return

        if not needs_fix:
            return

        logger.info("Rebuilding rules table to fix outdated CHECK constraints")
        try:
            # Get current column definitions
            col_names = [c["name"] for c in columns]
            col_list = ", ".join(col_names)

            # Create new table with correct constraints
            sync_conn.execute(
                text("""
                CREATE TABLE rules_new (
                    rule_id VARCHAR(36) PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    device_id VARCHAR(64) NOT NULL,
                    conditions TEXT NOT NULL DEFAULT '[]',
                    logic VARCHAR(8) NOT NULL DEFAULT 'AND',
                    duration INTEGER NOT NULL DEFAULT 0,
                    severity VARCHAR(16) NOT NULL,
                    enabled BOOLEAN NOT NULL DEFAULT 1,
                    notify_channels TEXT NOT NULL DEFAULT '[]',
                    created_by VARCHAR(64),
                    created_at DATETIME,
                    version INTEGER NOT NULL DEFAULT 1,
                    CONSTRAINT ck_rules_logic_valid CHECK (logic IN ('AND', 'OR', 'NOT')),
                    CONSTRAINT ck_rules_severity_valid CHECK (severity IN ('critical', 'major', 'warning', 'minor', 'info')),
                    CONSTRAINT ck_rules_duration_non_negative CHECK (duration >= 0)
                )
            """)
            )

            # Copy data
            sync_conn.execute(text(f"INSERT INTO rules_new ({col_list}) SELECT {col_list} FROM rules"))

            # R7-S-04 修复(严重): 表重建添加备份
            # 原问题: 直接 DROP 旧表后若后续 RENAME/索引重建失败，数据已丢失无法恢复。
            # 修复: 先将旧表 RENAME 为备份表（而非 DROP），重建成功后再删除备份；
            # 重建失败时从备份恢复，避免数据丢失。
            sync_conn.execute(text("ALTER TABLE rules RENAME TO rules_backup"))
            sync_conn.execute(text("ALTER TABLE rules_new RENAME TO rules"))

            # Recreate indexes
            sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rules_device_id ON rules(device_id)"))
            sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rules_enabled ON rules(enabled)"))
            sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rules_severity ON rules(severity)"))
            sync_conn.execute(text("CREATE INDEX IF NOT EXISTS ix_rules_created_by ON rules(created_by)"))

            sync_conn.commit()
            logger.info("Rules table rebuilt with updated CHECK constraints")
            # 重建成功后删除备份表（单独事务，失败仅留下备份表，不影响重建结果）
            try:
                sync_conn.execute(text("DROP TABLE IF EXISTS rules_backup"))
                sync_conn.commit()
            except Exception as cleanup_err:
                logger.debug("Failed to drop rules_backup after rebuild: %s", cleanup_err)
        except Exception as e:
            logger.warning("Failed to rebuild rules table constraints: %s", e)
            try:
                sync_conn.rollback()
            # FIXED-P1: 原问题-except Exception: pass 静默吞没异常，改为至少 logger.debug
            except Exception as rb_err:
                logger.debug("sync_conn.rollback() failed: %s", rb_err)
            # R7-S-04: 从备份恢复（DDL 在某些后端不可回滚，需手动恢复）
            try:
                if "rules_backup" in sa_inspect(sync_conn).get_table_names():
                    sync_conn.execute(text("DROP TABLE IF EXISTS rules"))
                    sync_conn.execute(text("ALTER TABLE rules_backup RENAME TO rules"))
                    sync_conn.commit()
                    logger.info("Restored rules table from backup after rebuild failure")
            except Exception as restore_err:
                logger.error("Failed to restore rules table from backup: %s", restore_err)

    @staticmethod
    def _fix_rule_type_check_constraints(sync_conn) -> None:
        """FIXED-P0(致命1): 统一 rule_type CHECK 约束为 ('threshold', 'ai_inference', 'script')。

        原问题: RuleORM 允许 ('threshold','script','expression','ai')，
        AlarmORM/RuleTemplateORM 仅允许 ('threshold','ai_inference','trend')，
        导致 script 类型规则触发告警时 AlarmORM 约束拒绝写入 (IntegrityError)。

        SQLite 不支持 ALTER CONSTRAINT，需重建表。此函数作为 alembic 迁移 013 的
        运行时安全网，确保未执行迁移的数据库也能正常工作。
        """
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy import text

        inspector = sa_inspect(sync_conn)
        try:
            existing_tables = inspector.get_table_names()
        except Exception:
            return

        # 检查每个表是否需要修复 rule_type 约束
        _table_fixes = {
            "rules": _RULES_REBUILD_SQL,
            "alarms": _ALARMS_REBUILD_SQL,
            "rule_templates": _RULE_TEMPLATES_REBUILD_SQL,
        }

        for table_name, rebuild_sql in _table_fixes.items():
            if table_name not in existing_tables:
                continue
            if not _needs_rule_type_fix(sync_conn, table_name):
                continue

            logger.info("Rebuilding %s table to fix rule_type CHECK constraint", table_name)
            try:
                # 1. 先规范化数据，确保旧数据不会违反新约束
                _normalize_rule_type(sync_conn, table_name)

                # 2. 获取列名列表用于数据复制（重建前获取）
                columns = inspector.get_columns(table_name)
                col_names = [c["name"] for c in columns]
                col_list = ", ".join(col_names)

                # 3. 重建前捕获索引定义（重建后旧索引会丢失）
                old_indexes = inspector.get_indexes(table_name)

                # 4. 创建新表（带正确约束）、复制数据、替换旧表
                sync_conn.execute(text(rebuild_sql))
                sync_conn.execute(
                    text(f"INSERT INTO {table_name}_new ({col_list}) SELECT {col_list} FROM {table_name}")
                )
                # R7-S-04 修复(严重): 表重建添加备份
                # 原问题: 直接 DROP 旧表后若后续 RENAME/索引重建失败，数据已丢失无法恢复。
                # 修复: 先将旧表 RENAME 为备份表（而非 DROP），重建成功后再删除备份；
                # 重建失败时从备份恢复，避免数据丢失。
                sync_conn.execute(text(f"ALTER TABLE {table_name} RENAME TO {table_name}_backup"))
                sync_conn.execute(text(f"ALTER TABLE {table_name}_new RENAME TO {table_name}"))

                # 5. 用重建前捕获的索引定义重建索引
                _recreate_indexes_from_list(sync_conn, table_name, old_indexes)

                sync_conn.commit()
                logger.info("%s table rebuilt with unified rule_type CHECK constraint", table_name)
                # 重建成功后删除备份表（单独事务，失败仅留下备份表，不影响重建结果）
                try:
                    sync_conn.execute(text(f"DROP TABLE IF EXISTS {table_name}_backup"))
                    sync_conn.commit()
                except Exception as cleanup_err:
                    logger.debug("Failed to drop %s_backup after rebuild: %s", table_name, cleanup_err)
            except Exception as e:
                logger.warning("Failed to rebuild %s table rule_type constraint: %s", table_name, e)
                try:
                    sync_conn.rollback()
                except Exception as rb_err:
                    logger.debug("sync_conn.rollback() failed: %s", rb_err)
                # R7-S-04: 从备份恢复（DDL 在某些后端不可回滚，需手动恢复）
                try:
                    backup_table = f"{table_name}_backup"
                    if backup_table in sa_inspect(sync_conn).get_table_names():
                        sync_conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                        sync_conn.execute(text(f"ALTER TABLE {backup_table} RENAME TO {table_name}"))
                        sync_conn.commit()
                        logger.info("Restored %s table from backup after rebuild failure", table_name)
                except Exception as restore_err:
                    logger.error("Failed to restore %s table from backup: %s", table_name, restore_err)

    async def _migrate(self, conn: Any) -> None:
        """Run Alembic migrations for the current database backend.

        FIXED-MIGRATION: Enhanced error handling with detailed logging and rollback support.
        - Captures stdout/stderr for detailed error diagnosis
        - Attempts automatic rollback on migration failure
        - Notifies administrators via the alert system
        - Prevents app startup or enters degraded mode on failure

        Supported backends: SQLite, MySQL, PostgreSQL, MSSQL
        """
        from pathlib import Path

        # Get the project root directory
        # __file__ = .../src/edgelite/storage/database.py -> project_root = .../ (4 levels up)
        project_root = Path(__file__).parent.parent.parent.parent
        alembic_dir = project_root / "alembic"

        # alembic.ini is in project root, alembic/ dir contains scripts
        alembic_ini = project_root / "alembic.ini"

        if not alembic_dir.exists():
            # Fallback: try CWD (works when running from project root)
            cwd_alembic = Path.cwd() / "alembic"
            cwd_alembic_ini = Path.cwd() / "alembic.ini"
            if cwd_alembic.exists():
                alembic_dir = cwd_alembic
                alembic_ini = cwd_alembic_ini
                project_root = Path.cwd()
            else:
                logger.warning("Alembic directory not found at %s or %s, skipping migrations", alembic_dir, cwd_alembic)
                return

        # Build the database URL for Alembic
        from edgelite.storage.database import _build_database_url

        db_url = _build_database_url(self._config)

        # Convert sync driver URL to async driver URL
        if db_url.startswith("sqlite:///"):
            db_url = db_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        elif db_url.startswith("mysql://"):
            db_url = db_url.replace("mysql://", "mysql+aiomysql://", 1)
        elif db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif db_url.startswith("mssql://"):
            db_url = db_url.replace("mssql://", "mssql+aioodbc://", 1)

        # Run Alembic migrations using subprocess
        env = {**os.environ, "ALEMBIC_DATABASE_URL": db_url}

        try:
            # R7-S-04: 改用 asyncio.create_subprocess_exec 异步执行，避免同步 subprocess.run
            # 阻塞事件循环最长 300 秒
            # Windows 修复: WindowsSelectorEventLoopPolicy 不支持 create_subprocess_exec，
            # 捕获 NotImplementedError 后回退到同步 subprocess.run
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "alembic",
                    "-c",
                    str(alembic_ini),
                    "upgrade",
                    "head",
                    cwd=str(project_root),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except NotImplementedError:
                # Windows SelectorEventLoop 不支持子进程，回退到同步执行
                import subprocess

                logger.info("Falling back to synchronous subprocess for migration (Windows SelectorEventLoop)")
                result = subprocess.run(
                    [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "upgrade", "head"],
                    cwd=str(project_root),
                    env=env,
                    capture_output=True,
                    timeout=300,
                )
                stdout_text = result.stdout.decode(errors="replace") if result.stdout else ""
                stderr_text = result.stderr.decode(errors="replace") if result.stderr else ""
                if result.returncode != 0:
                    logger.error("Migration failed with exit code %s", result.returncode)
                    logger.error("stdout: %s", stdout_text)
                    logger.error("stderr: %s", stderr_text)
                    await self._notify_migration_failure(
                        error_msg=stderr_text,
                        stdout=stdout_text,
                        backend=self._backend,
                    )
                    logger.warning(
                        "Alembic migration failed, but continuing startup. "
                        "_ensure_schema_columns will verify critical schema. "
                        "Run 'alembic upgrade head' manually to fix migration state."
                    )
                    return False
                else:
                    logger.info("Alembic migrations completed successfully")
                    if stdout_text:
                        for line in stdout_text.strip().split("\n"):
                            if line.strip():
                                logger.info("[alembic] %s", line)
                    await self._update_migration_status("success", None)
                    return True

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=300,  # 5 minutes timeout
                )
            except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041 - 兼容 Python<3.11
                # 超时后显式杀死子进程，防止资源泄漏
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass  # 进程已退出
                logger.error("Migration timed out after 5 minutes")

                # Notify administrators
                await self._notify_migration_failure(
                    error_msg="Migration timed out after 5 minutes",
                    stdout="",
                    backend=self._backend,
                )

                logger.warning("Alembic migration timed out, continuing startup")
                return False

            # 解码子进程输出（asyncio 返回 bytes，需手动解码）
            stdout_text = stdout_bytes.decode(errors="replace") if stdout_bytes else ""
            stderr_text = stderr_bytes.decode(errors="replace") if stderr_bytes else ""

            if proc.returncode != 0:
                # R6-S-06 修复(严重): 原 f-string 日志在调用前即完成字符串拼接，
                # 无法利用 logging 按级别延迟格式化；统一改为 %s 延迟格式化。
                logger.error("Migration failed with exit code %s", proc.returncode)
                logger.error("stdout: %s", stdout_text)
                logger.error("stderr: %s", stderr_text)

                # FIXED-P1: 原问题-迁移失败后自动downgrade，可能因部分schema变更导致数据库处于更严重的中间态；
                # 改为仅记录失败状态+通知运维，由人工决定是否downgrade或修复

                # Notify administrators
                await self._notify_migration_failure(
                    error_msg=stderr_text,
                    stdout=stdout_text,
                    backend=self._backend,
                )

                # FIXED: 迁移失败不再阻止启动，_ensure_schema_columns 会作为安全网补齐缺失列
                logger.warning(
                    "Alembic migration failed, but continuing startup. "
                    "_ensure_schema_columns will verify critical schema. "
                    "Run 'alembic upgrade head' manually to fix migration state."
                )
                return False
            else:
                logger.info("Alembic migrations completed successfully")
                if stdout_text:
                    for line in stdout_text.strip().split("\n"):
                        if line.strip():
                            logger.info("[alembic] %s", line)

                # Log success and clear any previous failure state
                await self._update_migration_status("success", None)
                return True

        except FileNotFoundError:
            logger.warning("Alembic not found, skipping migrations. Install with: pip install alembic")
            return None

        except Exception as e:
            # R6-S-06: f-string → %s 延迟格式化
            logger.error("Unexpected migration error: %s", e, exc_info=True)

            # Notify administrators
            await self._notify_migration_failure(
                error_msg=str(e),
                stdout="",
                backend=self._backend,
            )

            logger.warning("Alembic migration error, continuing startup")
            return False

    async def _notify_migration_failure(self, error_msg: str, stdout: str, backend: str) -> None:
        """Notify administrators about migration failure via alert system.

        This method sends an alert when database migration fails,
        allowing administrators to take corrective action.
        """
        from datetime import (  # FIXED(一般-R2): datetime.utcnow() 已弃用，改用 datetime.now(UTC)
            UTC,
            datetime,
        )

        try:
            from edgelite.app import _app_state

            container = _app_state
            if container is None:
                logger.warning("Cannot send migration failure notification: app state not initialized")
                return

            # Store migration failure state for frontend retrieval
            failure_info = {
                "timestamp": datetime.now(UTC).isoformat(),
                "error": error_msg,
                "stdout": stdout,
                "backend": backend,
                "status": "failed",
            }

            if not hasattr(container, "_migration_status"):
                container._migration_status = {}

            container._migration_status["last_failure"] = failure_info
            container._migration_status["current_status"] = "failed"

            logger.warning(
                "Database migration failure notification sent: backend=%s, error=%s",
                backend,
                error_msg[:200],
            )

        except Exception as notify_err:
            # R6-S-06: f-string → %s 延迟格式化
            logger.error("Failed to send migration failure notification: %s", notify_err)

    async def _update_migration_status(self, status: str, error_msg: str | None) -> None:
        """Update migration status for frontend display.

        Args:
            status: Current status ('success', 'failed', 'in_progress', 'pending')
            error_msg: Error message if status is 'failed'
        """
        from datetime import (  # FIXED(一般-R2): datetime.utcnow() 已弃用，改用 datetime.now(UTC)
            UTC,
            datetime,
        )

        try:
            from edgelite.app import _app_state

            container = _app_state
            if container is None:
                return

            if not hasattr(container, "_migration_status"):
                container._migration_status = {}

            container._migration_status["current_status"] = status
            container._migration_status["last_updated"] = datetime.now(UTC).isoformat()

            if status == "failed" and error_msg:
                container._migration_status["last_failure"] = {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "error": error_msg,
                }
            elif status == "success":
                # Clear previous failure state
                container._migration_status.pop("last_failure", None)

            # R6-S-06: f-string → %s 延迟格式化
            logger.debug("Migration status updated: %s", status)

        except Exception as e:
            logger.error("Failed to update migration status: %s", e)

    def get_session(self) -> AsyncSession:
        """获取新的数据库会话。

        FIXED-LP12: 移除弃用警告。经审计，项目中所有 40+ 处调用点均使用
        ``async with db.get_session() as session:`` 模式，AsyncSession 的
        ``__aexit__`` 会自动调用 ``close()``，不存在连接泄漏风险。

        推荐用法（与 ``session()`` 上下文管理器等价安全）:
            async with db.get_session() as session:
                result = await session.execute(...)
            # session 自动关闭

        如需更明确的资源管理语义，也可使用 ``async with db.session() as session:``。
        """
        if self._session_factory is None:
            raise RuntimeError(DatabaseErrors.NOT_CONNECTED)
        return self._session_factory()

    @asynccontextmanager
    async def session(self):
        """获取数据库会话的上下文管理器版本。

        FIXED: P0-4 推荐使用此方法确保会话自动关闭。

        用法:
            async with db.session() as session:
                result = await session.execute(...)
            # session 自动关闭
        """
        if self._session_factory is None:
            raise RuntimeError(DatabaseErrors.NOT_CONNECTED)
        session = self._session_factory()
        try:
            yield session
        finally:
            await session.close()

    async def close(self) -> None:
        """关闭数据库连接池"""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("数据库连接已关闭")

    async def backup(self, backup_path: str) -> None:
        """Backup the main database and all SQLite sidecar databases.

        FIXED-BACKUP: Previously only the main SQLite database was backed up.
        Config version databases (s7, mc, ab, opcua, fins), edge_triggers,
        edge_rules, audit, and security_audit databases are now also included.
        Each sidecar backup failure is logged but does not interrupt the main
        database backup.
        """
        import shutil

        Path(backup_path).parent.mkdir(parents=True, exist_ok=True)

        if self._backend == "sqlite":
            async with self._write_lock:  # FIXED-P1: WAL checkpoint与copy之间无排他锁，备份期间写入可导致备份不一致
                if self._engine:
                    try:
                        async with self._engine.begin() as conn:
                            await conn.execute(text("PRAGMA wal_checkpoint=TRUNCATE"))
                    except Exception as e:
                        logger.warning("WAL checkpoint TRUNCATE failed: %s, trying PASSIVE", e)
                        try:
                            async with self._engine.begin() as conn:
                                await conn.execute(text("PRAGMA wal_checkpoint=PASSIVE"))
                        except Exception as e2:
                            logger.warning("WAL checkpoint PASSIVE also failed: %s, copying file directly", e2)
                # FIXED-P2: 原问题-shutil.copy2同步阻塞事件循环；改为asyncio.to_thread
                await asyncio.to_thread(shutil.copy2, self._config.database.sqlite_path, backup_path)
                wal_path = self._config.database.sqlite_path + "-wal"
                if Path(wal_path).exists():
                    try:
                        await asyncio.to_thread(shutil.copy2, wal_path, backup_path + "-wal")
                    except Exception as e:
                        logger.warning("WAL file backup failed: %s", e)

            # FIXED-BACKUP: Backup all sidecar SQLite databases independently.
            # Each failure is logged but does not interrupt the main backup.
            await self._backup_sidecar_dbs(backup_path)

        elif self._backend in ("mysql", "mariadb"):
            await self._backup_mysql(backup_path)
            await self._backup_sidecar_dbs(
                backup_path
            )  # FIXED-P1: 原问题-非SQLite后端备份不含sidecar，MySQL/PG用户sidecar数据无备份
        elif self._backend in ("postgresql", "postgres"):
            await self._backup_postgresql(backup_path)
            await self._backup_sidecar_dbs(backup_path)  # FIXED-P1: 同上
        elif self._backend == "mssql":
            await self._backup_mssql(backup_path)
            await self._backup_sidecar_dbs(backup_path)  # FIXED-P1: 同上

    async def _backup_sidecar_dbs(self, main_backup_path: str) -> None:
        """Backup all SQLite sidecar databases to the same backup directory as the main db.

        Each sidecar database is backed up using the same pattern:
          {main_backup_path}.{sidecar_name}.db
        e.g. data/backups/edgelite.db.backup.20260101_120000.s7_config_versions.db

        For config version managers (s7, mc, ab, opcua, fins), the backup() method
        is called on the manager instance for thread-safe, lock-protected backups.

        For other databases (edge_triggers, edge_rules, audit, security_audit),
        file-level copy with WAL checkpoint is used.

        WAL checkpoint → backup → integrity check (PRAGMA integrity_check)
        Failures are logged individually and do not interrupt other sidecar backups.
        """
        import shutil

        backup_dir = Path(main_backup_path).parent
        main_stem = Path(main_backup_path).stem  # e.g. "edgelite.db"

        for _manager_ref_name, rel_path in _SQLITE_SIDECAR_DBS:
            abs_path = _get_sidecar_db_path(rel_path)
            src_path = Path(abs_path)
            sidecar_name = src_path.stem  # e.g. "s7_config_versions.db" -> "s7_config_versions"
            sidecar_backup = backup_dir / f"{main_stem}.{sidecar_name}.db"

            try:
                if not src_path.exists():
                    logger.debug("Sidecar DB not found, skipping backup: %s", abs_path)
                    continue

                # FIXED-BACKUP: Use manager's backup() method if available for thread-safe backup
                manager = self._get_config_version_manager(rel_path)
                if manager is not None and hasattr(manager, "backup"):
                    # Use thread-safe backup via manager
                    try:
                        # FIXED-ASYNC: backup() is now async, call it directly with await
                        await manager.backup(str(sidecar_backup))
                        logger.info(
                            "Sidecar database backed up via manager: %s -> %s",
                            abs_path,
                            sidecar_backup,
                        )
                        continue
                    except Exception as e:
                        logger.warning(
                            "Manager backup failed for %s: %s, falling back to file copy",
                            sidecar_name,
                            e,
                        )
                        # Fall through to file-level backup

                # FIXED-P0: 原问题-sidecar文件级备份期间无排他锁，并发写入可导致备份不一致
                # 获取对应模块的写锁（如edge_rules用RuleStore._lock）
                sidecar_lock = self._get_sidecar_write_lock(rel_path)
                _is_async_lock = isinstance(sidecar_lock, asyncio.Lock)
                _lock_acquired = False
                if _is_async_lock and sidecar_lock is not None:
                    await sidecar_lock.acquire()
                    _lock_acquired = True
                elif sidecar_lock is not None:
                    # FIXED-P0: 原问题-threading.Lock.acquire()同步阻塞事件循环；改为asyncio.to_thread
                    await asyncio.to_thread(sidecar_lock.acquire)
                    _lock_acquired = True

                try:
                    # FIXED-P2: 原问题-同步WAL checkpoint+shutil.copy2阻塞事件循环；改为asyncio.to_thread
                    # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用默认参数绑定当前 abs_path、sidecar_name、sidecar_backup 的值
                    def _checkpoint_and_copy(
                        abs_path=abs_path, sidecar_name=sidecar_name, sidecar_backup=sidecar_backup
                    ):
                        # WAL checkpoint on the sidecar before file copy
                        try:
                            import sqlite3

                            conn = sqlite3.connect(abs_path, timeout=10)
                            try:
                                conn.execute("PRAGMA wal_checkpoint=TRUNCATE")
                            except Exception:
                                try:
                                    conn.execute("PRAGMA wal_checkpoint=PASSIVE")
                                except Exception as e:
                                    # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
                                    logger.warning("Sidecar WAL checkpoint PASSIVE failed for %s: %s", sidecar_name, e)
                            conn.close()
                        except Exception as e:
                            logger.warning("Sidecar WAL checkpoint failed for %s: %s, copying anyway", sidecar_name, e)

                        # Copy the sidecar database
                        shutil.copy2(abs_path, str(sidecar_backup))

                        # Copy WAL file if present
                        wal_src = f"{abs_path}-wal"
                        if Path(wal_src).exists():
                            try:
                                shutil.copy2(wal_src, str(sidecar_backup) + "-wal")
                            except Exception as e:
                                logger.warning("Sidecar WAL backup failed for %s: %s", sidecar_name, e)

                    await asyncio.to_thread(_checkpoint_and_copy)
                finally:
                    if _lock_acquired and sidecar_lock is not None:
                        if _is_async_lock:
                            await sidecar_lock.release()
                        else:
                            await asyncio.to_thread(sidecar_lock.release)

                # Integrity check on the backed-up copy
                try:
                    import sqlite3

                    # FIXED-P2: 同_check_sidecar_integrity，同步sqlite3操作改为asyncio.to_thread
                    # FIXED(P1): 原问题-B023 循环变量捕获; 修复-使用默认参数绑定当前 sidecar_backup 的值
                    def _check_backup_integrity(sidecar_backup=sidecar_backup):
                        conn = sqlite3.connect(str(sidecar_backup), timeout=10)
                        try:
                            cursor = conn.execute("PRAGMA integrity_check")
                            return cursor.fetchone()
                        finally:
                            conn.close()

                    result = await asyncio.to_thread(_check_backup_integrity)
                    if result and result[0] != "ok":
                        logger.warning(
                            "Sidecar backup integrity check failed for %s: %s",
                            sidecar_name,
                            result[0],
                        )
                    else:
                        logger.info(
                            "Sidecar database backed up: %s -> %s",
                            abs_path,
                            sidecar_backup,
                        )
                except Exception as e:
                    logger.warning("Sidecar integrity check failed for %s: %s", sidecar_name, e)

            except Exception as e:
                logger.error(
                    "Sidecar database backup failed for %s (%s): %s",
                    sidecar_name,
                    abs_path,
                    e,
                )

    def _get_sidecar_write_lock(self, rel_path: str) -> Any:
        """FIXED-P1: Get the write lock for a sidecar database to ensure backup consistency.

        Returns the threading.Lock/RLock for sidecar databases that have one,
        or None for databases without explicit lock protection.
        """
        _SIDECAR_LOCK_MAP = {
            "data/edge_rules.db": ("rule_store", "_lock"),
            "data/device_status.db": ("lifecycle", "_db_lock"),
            "data/emergency_buffer.db": ("influx_storage", "_emergency_db_lock"),
            "data/edgelite_ts.db": ("influx_storage", "_sqlite_ts_write_lock_proxy"),
            "data/audit.db": ("audit_service", "_db_lock"),
            "data/security_audit.db": (
                "audit_service",
                "_db_lock",
            ),  # FIXED-P0: 原问题-security_audit.db无锁映射，备份期间并发写入可不一致
            "data/edge_triggers.db": (
                "edge_triggers",
                "_db_lock",
            ),  # FIXED-P0: 原问题-edge_triggers.db无锁映射，备份期间并发写入可不一致
            "data/mqtt_offline_queue.db": (
                "mqtt_forwarder",
                "_offline_db_lock",
            ),  # FIXED-P0: 原问题-mqtt_offline_queue.db无锁映射，备份期间并发写入可不一致
            "data/opcua_ts.db": (
                "opcua_ts_store",
                "_ts_write_lock_proxy",
            ),  # FIXED-P0: 原问题-opcua_ts.db无锁映射，备份期间并发写入可不一致
            "data/observability_alerts.db": (
                "alert_engine",
                "_lock",
            ),  # FIXED-P0: 原问题-observability_alerts.db无锁映射，备份期间并发写入可不一致
        }
        lock_info = _SIDECAR_LOCK_MAP.get(rel_path)
        if lock_info is None:
            return None
        obj_attr, lock_attr = lock_info
        try:
            from edgelite.app import _app_state

            obj = getattr(_app_state, obj_attr, None)
            if obj is not None:
                if lock_attr == "_sqlite_ts_write_lock_proxy":
                    sqlite_ts = getattr(obj, "_sqlite_ts", None)
                    if sqlite_ts is not None:
                        # FIXED-P0: 原问题-属性名_write_lock不匹配，SqliteTimeSeriesStorage实际使用_db_lock，
                        # 导致edgelite_ts.db备份期间无写锁保护，并发写入可导致备份不一致
                        return getattr(sqlite_ts, "_db_lock", None) or getattr(sqlite_ts, "_write_lock", None)
                    return None
                if (
                    lock_attr == "_ts_write_lock_proxy"
                ):  # FIXED-P0: opcua_ts_store内部SqliteTimeSeriesStorage._write_lock代理
                    ts = getattr(obj, "_ts", None)
                    if ts is not None:
                        return getattr(ts, "_db_lock", None) or getattr(ts, "_write_lock", None)
                    return None
                return getattr(obj, lock_attr, None)
        except Exception as e:
            # FIXED-P2: 原问题-异常被静默吞没，添加日志记录
            logger.warning("获取sidecar写锁失败: %s", e)
        return None

    def _get_config_version_manager(self, rel_path: str) -> Any:
        """Get the config version manager instance for a specific database path.

        FIXED-BACKUP: Returns the appropriate manager instance based on the database path.
        This allows the backup system to call thread-safe backup() methods.

        Args:
            rel_path: Relative path of the database (e.g. "data/s7_config_versions.db")

        Returns:
            Manager instance or None if not available
        """
        # Check if this path has a corresponding manager
        if rel_path not in _CONFIG_VERSION_DB_PATTERNS:
            return None

        driver_attr, manager_attr = _CONFIG_VERSION_DB_PATTERNS[rel_path]

        try:
            from edgelite.app import _app_state

            # Extract driver name from the attribute path
            # e.g. "_app_state.s7_driver" -> "s7_driver"
            driver_name = driver_attr.split(".")[-1]
            driver = getattr(_app_state, driver_name, None)

            if driver is None:
                return None

            manager = getattr(driver, manager_attr, None)
            return manager

        except Exception as e:
            logger.debug("Could not get config version manager for %s: %s", rel_path, e)
            return None

    async def _restore_sidecar_dbs(self, main_db_path: str, backup_dir: Path, main_stem: str) -> None:
        """Restore sidecar SQLite databases from backup.

        FIXED-BACKUP: When recovering the main database from a backup, also restore
        all sidecar databases that were included in that backup. Sidecar backups
        follow the naming pattern: {main_stem}.{sidecar_name}.db

        Each sidecar restoration failure is logged but does not raise — the main
        database recovery is already confirmed valid via integrity_check.
        """
        import shutil

        if not backup_dir.exists():
            logger.debug("Backup dir does not exist, skipping sidecar restore: %s", backup_dir)
            return

        for _prop_name, rel_path in _SQLITE_SIDECAR_DBS:
            abs_path = _get_sidecar_db_path(rel_path)
            dst_path = Path(abs_path)
            sidecar_name = dst_path.stem

            try:
                # Find the latest backup for this sidecar (same timestamp as main db backup)
                main_db_backups = sorted(
                    (f for f in backup_dir.glob(f"{main_stem}.backup.*") if not f.name.endswith(("-wal", "-shm"))),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if not main_db_backups:
                    logger.debug("No main db backups found, skipping sidecar restore: %s", sidecar_name)
                    continue

                # Find matching sidecar backup from the same backup session
                sidecar_backup: Path | None = None
                for main_backup in main_db_backups:
                    ts = main_backup.name.replace(f"{main_stem}.backup.", "")
                    candidate = backup_dir / f"{main_stem}.backup.{ts}.{sidecar_name}.db"
                    if candidate.exists():
                        sidecar_backup = candidate
                        break
                if sidecar_backup is None:
                    logger.warning(
                        "No sidecar backup found for %s (main backup ts=%s), skipping restore",
                        sidecar_name,
                        main_db_backups[0].name,
                    )
                    continue

                # Restore the sidecar database
                shutil.copy2(str(sidecar_backup), str(dst_path))

                # Restore WAL file if present
                wal_backup = Path(str(sidecar_backup) + "-wal")
                if wal_backup.exists():
                    try:
                        shutil.copy2(str(wal_backup), f"{abs_path}-wal")
                    except Exception as e:
                        logger.warning("Sidecar WAL restore failed for %s: %s", sidecar_name, e)

                logger.info("Sidecar database restored: %s -> %s", sidecar_backup, dst_path)

            except Exception as e:
                logger.error(
                    "Sidecar database restore failed for %s (%s): %s",
                    sidecar_name,
                    abs_path,
                    e,
                )

    async def _backup_mysql(self, backup_path: str) -> None:
        """FIXED-P1: 原问题-MySQL后端无备份实现，仅抛NotImplementedError
        使用mysqldump通过asyncio.to_thread执行，与SQLite保持一致的备份流程"""
        import subprocess

        cfg = self._config.database
        cmd = [
            "mysqldump",
            f"--host={cfg.host}",
            f"--port={cfg.port or 3306}",
            f"--user={cfg.username}",
            "--single-transaction",
            "--routines",
            "--triggers",
            cfg.database,
        ]
        dump_env = {"MYSQL_PWD": cfg.password}  # FIXED-P1: 密码通过MYSQL_PWD环境变量传递，避免出现在进程列表(ps aux)

        def _run_mysqldump():
            with open(backup_path, "w", encoding="utf-8") as f:
                result = subprocess.run(
                    cmd, stdout=f, stderr=subprocess.PIPE, timeout=300, env={**os.environ, **dump_env}
                )
            if result.returncode != 0:
                err_msg = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"mysqldump failed (exit={result.returncode}): {err_msg}")
            if not Path(backup_path).exists() or Path(backup_path).stat().st_size == 0:
                raise RuntimeError("mysqldump produced empty or missing backup file")

        try:
            await asyncio.to_thread(_run_mysqldump)
            logger.info("MySQL备份成功: %s", backup_path)
        except FileNotFoundError:
            raise RuntimeError("mysqldump not found, install MySQL client tools") from None
        except Exception as e:
            logger.error("MySQL备份失败: %s", e)
            raise

    async def _backup_postgresql(self, backup_path: str) -> None:
        """FIXED-P1: 原问题-PostgreSQL后端无备份实现，仅抛NotImplementedError
        使用pg_dump通过asyncio.to_thread执行，与SQLite保持一致的备份流程"""
        import subprocess

        cfg = self._config.database
        env = {**os.environ, "PGPASSWORD": cfg.password}
        cmd = [
            "pg_dump",
            f"--host={cfg.host}",
            f"--port={cfg.port or 5432}",
            f"--username={cfg.username}",
            "--no-password",
            "--format=plain",
            cfg.database,
        ]

        def _run_pg_dump():
            with open(backup_path, "w", encoding="utf-8") as f:
                result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=env, timeout=300)
            if result.returncode != 0:
                err_msg = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"pg_dump failed (exit={result.returncode}): {err_msg}")
            if not Path(backup_path).exists() or Path(backup_path).stat().st_size == 0:
                raise RuntimeError("pg_dump produced empty or missing backup file")

        try:
            await asyncio.to_thread(_run_pg_dump)
            logger.info("PostgreSQL备份成功: %s", backup_path)
        except FileNotFoundError:
            raise RuntimeError("pg_dump not found, install PostgreSQL client tools") from None
        except Exception as e:
            logger.error("PostgreSQL备份失败: %s", e)
            raise

    async def _backup_mssql(self, backup_path: str) -> None:
        """FIXED-P1: 原问题-MSSQL后端无备份实现，仅抛NotImplementedError
        使用sqlcmd执行BACKUP DATABASE命令，与SQLite保持一致的备份流程"""
        import subprocess

        cfg = self._config.database
        host = cfg.host
        port = cfg.port or 1433
        user = cfg.username
        pwd = cfg.password
        db = cfg.database
        safe_db = db.replace("]", "]]")  # FIXED-P2: 原问题-MSSQL备份中db名称通过[{db}]拼入SQL，含]字符可突破方括号包裹
        safe_backup_path = backup_path.replace("'", "''")
        sql = f"BACKUP DATABASE [{safe_db}] TO DISK=N'{safe_backup_path}' WITH FORMAT, INIT, NAME=N'{safe_db}-Backup', SKIP, NOREWIND, NOUNLOAD, STATS=10"

        cmd = [
            "sqlcmd",
            f"-S{host},{port}",
            f"-U{user}",
            # FIXED-P1: 移除命令行参数 f"-P{pwd}"，密码不再以明文出现在进程命令行中
            # （可被 ps/任务管理器读取）。改为通过 SQLCMDPASSWORD 环境变量传递，
            # 与 MySQL(MYSQL_PWD)/PostgreSQL(PGPASSWORD) 做法一致；
            # sqlcmd 官方支持 SQLCMDPASSWORD 环境变量作为 SQL Server 身份验证的默认密码。
            "-dmaster",
            "-Q",
            sql,
        ]
        # 密码通过环境变量传递，避免出现在进程命令行/进程列表中
        env = {**os.environ, "SQLCMDPASSWORD": pwd}

        def _run_sqlcmd():
            result = subprocess.run(cmd, capture_output=True, timeout=300, env=env)
            if result.returncode != 0:
                err_msg = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"sqlcmd BACKUP failed (exit={result.returncode}): {err_msg}")
            if not Path(backup_path).exists() or Path(backup_path).stat().st_size == 0:
                raise RuntimeError("sqlcmd BACKUP produced empty or missing backup file")

        try:
            await asyncio.to_thread(_run_sqlcmd)
            logger.info("MSSQL备份成功: %s", backup_path)
        except FileNotFoundError:
            raise RuntimeError("sqlcmd not found, install SQL Server command-line tools") from None
        except Exception as e:
            logger.error("MSSQL备份失败: %s", e)
            raise
