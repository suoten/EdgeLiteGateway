# 数据库管理模块测试 - 覆盖 storage/database.py
from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sa_inspect

from edgelite.config import DatabaseConfig
from edgelite.models.db import Base, UserORM
from edgelite.storage.database import (
    Database,
    _build_database_url,
    _check_driver,
    _get_sidecar_db_path,
    _needs_rule_type_fix,
    _normalize_rule_type,
    _recreate_indexes_from_list,
)


def _make_config(tmp_path, backend="sqlite", **kwargs):
    """创建测试用数据库配置对象"""
    db_path = str(tmp_path / "test.db")
    defaults = dict(backend=backend, sqlite_path=db_path, backup_dir=str(tmp_path / "backups"))
    defaults.update(kwargs)
    return SimpleNamespace(database=DatabaseConfig(**defaults))


@pytest.fixture(autouse=True)
def _reset_db_singleton():
    """每个测试前后保存和恢复 Database 单例，防止跨测试污染"""
    saved = Database._instance
    Database._instance = None
    yield
    Database._instance = saved


@pytest.fixture
def db_config(tmp_path):
    """返回测试用数据库配置"""
    return _make_config(tmp_path)


@pytest.fixture
async def connected_db(tmp_path):
    """已连接的数据库实例（跳过 sidecar 完整性检查以避免全局配置依赖）"""
    config = _make_config(tmp_path)
    db = Database(config)
    with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
        await db.connect()
    yield db
    await db.close()


@pytest.fixture
async def initialized_db(tmp_path, monkeypatch):
    """已初始化表的数据库（跳过 alembic 迁移以加速）"""
    monkeypatch.setenv("EDGELITE_ADMIN_PASSWORD", "TestPass123!")
    config = _make_config(tmp_path)
    db = Database(config)
    with (
        patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
        patch.object(Database, "_migrate", new_callable=AsyncMock),
    ):
        await db.connect()
        await db.init_tables()
    yield db
    await db.close()


class TestBuildDatabaseUrl:
    """构建数据库 URL 的各后端测试"""

    def test_sqlite_backend(self, tmp_path):
        """SQLite 后端应生成 aiosqlite 驱动的 URL"""
        cfg = _make_config(tmp_path)
        url = _build_database_url(cfg)
        assert "sqlite+aiosqlite" in url
        assert "test.db" in url

    def test_mysql_backend(self, tmp_path):
        """MySQL 后端应生成 aiomysql 驱动的 URL"""
        cfg = _make_config(
            tmp_path, backend="mysql", host="dbhost", port=3307, username="user1", password="p@ss", database="mydb"
        )
        url = _build_database_url(cfg)
        assert url.startswith("mysql+aiomysql://")
        assert "user1" in url
        assert "p%40ss" in url
        assert "dbhost:3307" in url
        assert "mydb" in url

    def test_postgresql_backend(self, tmp_path):
        """PostgreSQL 后端应生成 asyncpg 驱动的 URL"""
        cfg = _make_config(
            tmp_path,
            backend="postgresql",
            host="pghost",
            port=5433,
            username="pguser",
            password="pgpw",
            database="pgdb",
        )
        url = _build_database_url(cfg)
        assert url.startswith("postgresql+asyncpg://")
        assert "pghost:5433" in url
        assert "pgdb" in url

    def test_mssql_backend(self, tmp_path):
        """MSSQL 后端应生成 aioodbc 驱动的 URL"""
        cfg = _make_config(
            tmp_path, backend="mssql", host="mshost", port=1433, username="msuser", password="mspw", database="msdb"
        )
        url = _build_database_url(cfg)
        assert "mssql+aioodbc" in url
        assert "ODBC Driver 18 for SQL Server" in url
        assert "mshost,1433" in url

    def test_mssql_trust_server_certificate(self, tmp_path):
        """MSSQL trust_server_certificate=True 时 URL 应包含 TrustServerCertificate=yes"""
        cfg = _make_config(
            tmp_path,
            backend="mssql",
            host="mshost",
            port=1433,
            username="msuser",
            password="mspw",
            database="msdb",
            trust_server_certificate=True,
        )
        url = _build_database_url(cfg)
        assert "TrustServerCertificate=yes" in url

    def test_unsupported_backend(self, tmp_path):
        """不支持的后端应抛出 ValueError"""
        cfg = _make_config(tmp_path, backend="oracle")
        with pytest.raises(ValueError, match="UNSUPPORTED_BACKEND"):
            _build_database_url(cfg)

    def test_sqlite_creates_parent_dir(self, tmp_path):
        """SQLite 后端应自动创建父目录"""
        nested = tmp_path / "nested" / "deep" / "path"
        cfg = SimpleNamespace(
            database=DatabaseConfig(
                backend="sqlite",
                sqlite_path=str(nested / "test.db"),
                backup_dir=str(tmp_path / "backups"),
            )
        )
        url = _build_database_url(cfg)
        assert nested.exists()


class TestCheckDriver:
    """数据库驱动检查测试"""

    def test_sqlite_driver_available(self):
        """SQLite 驱动应可用，不抛异常"""
        _check_driver("sqlite")

    def test_unknown_backend_no_check(self):
        """未知后端不需要驱动检查，应正常返回"""
        _check_driver("unknown_db")

    def test_mysql_driver_check_raises(self):
        """MySQL 驱动未安装时应抛出 ImportError"""
        with patch("builtins.__import__", side_effect=ImportError("no module")):
            with pytest.raises(ImportError, match="DRIVER_REQUIRED"):
                _check_driver("mysql")


class TestGetSidecarDbPath:
    """sidecar 数据库路径解析测试"""

    def test_resolves_relative_path(self, tmp_path, monkeypatch):
        """应将相对路径解析为绝对路径"""
        cfg = SimpleNamespace(
            database=DatabaseConfig(
                backend="sqlite",
                sqlite_path=str(tmp_path / "main.db"),
                backup_dir=str(tmp_path / "backups"),
            )
        )
        monkeypatch.setattr("edgelite.storage.database.get_config", lambda: cfg)
        result = _get_sidecar_db_path("data/audit.db")
        assert result.endswith("audit.db")
        assert os.path.isabs(result)

    def test_falls_back_when_config_fails(self, monkeypatch):
        """配置获取失败时应回退到 data 目录"""

        def _raise():
            raise Exception("no config")

        monkeypatch.setattr("edgelite.storage.database.get_config", _raise)
        result = _get_sidecar_db_path("data/audit.db")
        assert result.endswith("audit.db")


class TestRuleTypeHelpers:
    """rule_type CHECK 约束辅助函数测试"""

    def test_needs_fix_with_trend(self, tmp_path):
        """包含 trend 值的表需要修复"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE alarms (
            rule_type VARCHAR(32) CHECK(rule_type IN ('threshold','trend'))
        )""")
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as sync_conn:
            assert _needs_rule_type_fix(sync_conn, "alarms") is True
        engine.dispose()
        conn.close()

    def test_needs_fix_with_expression(self, tmp_path):
        """包含 expression 值的表需要修复"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE rules (
            rule_type VARCHAR(32) CHECK(rule_type IN ('threshold','expression'))
        )""")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as sync_conn:
            assert _needs_rule_type_fix(sync_conn, "rules") is True
        engine.dispose()
        conn.close()

    def test_needs_fix_with_ai_only(self, tmp_path):
        """包含独立 ai 值（非 ai_inference）的表需要修复"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE rules (
            rule_type VARCHAR(32) CHECK(rule_type IN ('threshold','ai'))
        )""")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as sync_conn:
            assert _needs_rule_type_fix(sync_conn, "rules") is True
        engine.dispose()
        conn.close()

    def test_no_fix_needed_for_valid(self, tmp_path):
        """已包含 ai_inference 的表不需要修复"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE rules (
            rule_type VARCHAR(32) CHECK(rule_type IN ('threshold','ai_inference','script'))
        )""")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as sync_conn:
            assert _needs_rule_type_fix(sync_conn, "rules") is False
        engine.dispose()
        conn.close()

    def test_no_fix_for_missing_table(self, tmp_path):
        """不存在的表不需要修复"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as sync_conn:
            assert _needs_rule_type_fix(sync_conn, "nonexistent") is False
        engine.dispose()
        conn.close()

    def test_normalize_rule_type_rules(self, tmp_path):
        """rules 表 expression 应转为 script，ai 应转为 ai_inference"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE rules (rule_type VARCHAR(32))")
        conn.execute("INSERT INTO rules VALUES ('expression')")
        conn.execute("INSERT INTO rules VALUES ('ai')")
        conn.commit()
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as sync_conn:
            _normalize_rule_type(sync_conn, "rules")
        with engine.connect() as sync_conn:
            rows = sync_conn.execute(text("SELECT rule_type FROM rules ORDER BY rule_type")).fetchall()
        engine.dispose()
        types = [r[0] for r in rows]
        assert "script" in types
        assert "ai_inference" in types
        conn.close()

    def test_normalize_rule_type_alarms(self, tmp_path):
        """alarms 表 trend 应转为 threshold"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE alarms (rule_type VARCHAR(32))")
        conn.execute("INSERT INTO alarms VALUES ('trend')")
        conn.commit()
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as sync_conn:
            _normalize_rule_type(sync_conn, "alarms")
        with engine.connect() as sync_conn:
            row = sync_conn.execute(text("SELECT rule_type FROM alarms")).fetchone()
        engine.dispose()
        assert row[0] == "threshold"
        conn.close()

    def test_normalize_rule_type_fallback(self, tmp_path):
        """非标准值应回退为 threshold"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE rules (rule_type VARCHAR(32))")
        conn.execute("INSERT INTO rules VALUES ('weird_type')")
        conn.commit()
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as sync_conn:
            _normalize_rule_type(sync_conn, "rules")
        with engine.connect() as sync_conn:
            row = sync_conn.execute(text("SELECT rule_type FROM rules")).fetchone()
        engine.dispose()
        assert row[0] == "threshold"
        conn.close()


class TestEnsureIndexes:
    """索引补建函数测试"""

    def test_recreate_indexes(self, tmp_path):
        """应根据索引定义列表重建索引"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE rules (rule_id TEXT, device_id TEXT, created_at TEXT)")
        conn.commit()
        engine = create_engine(f"sqlite:///{db_path}")
        indexes = [
            {"name": "idx_rules_device", "column_names": ["device_id"], "unique": False},
            {"name": "idx_rules_created", "column_names": ["created_at"], "unique": False},
        ]
        with engine.begin() as sync_conn:
            _recreate_indexes_from_list(sync_conn, "rules", indexes)
        with engine.connect() as sync_conn:
            idxs = sa_inspect(sync_conn).get_indexes("rules")
        engine.dispose()
        idx_names = [i["name"] for i in idxs]
        assert "idx_rules_device" in idx_names
        assert "idx_rules_created" in idx_names
        conn.close()

    def test_recreate_indexes_skips_empty(self, tmp_path):
        """空名称或无列的索引应被跳过"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE rules (rule_id TEXT)")
        conn.commit()
        engine = create_engine(f"sqlite:///{db_path}")
        indexes = [
            {"name": "", "column_names": ["rule_id"], "unique": False},
            {"name": "idx_ok", "column_names": [], "unique": False},
        ]
        with engine.begin() as sync_conn:
            _recreate_indexes_from_list(sync_conn, "rules", indexes)
        engine.dispose()
        conn.close()

    def test_ensure_indexes_creates_missing(self, tmp_path):
        """_ensure_indexes 应补建缺失索引"""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (created_at TEXT)"))
            conn.execute(text("CREATE TABLE devices (created_at TEXT, protocol TEXT)"))
        with engine.begin() as conn:
            Database._ensure_indexes(conn, ["users", "devices"])
        with engine.connect() as conn:
            idxs_users = sa_inspect(conn).get_indexes("users")
            idxs_devices = sa_inspect(conn).get_indexes("devices")
        engine.dispose()
        assert any(i["name"] == "idx_users_created_at" for i in idxs_users)
        assert any(i["name"] == "idx_devices_created_at" for i in idxs_devices)
        assert any(i["name"] == "idx_devices_protocol" for i in idxs_devices)

    def test_ensure_indexes_skips_nonexistent_table(self, tmp_path):
        """_ensure_indexes 应跳过不存在的表"""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            Database._ensure_indexes(conn, ["nonexistent_table"])
        engine.dispose()

    def test_ensure_indexes_idempotent(self, tmp_path):
        """_ensure_indexes 应幂等"""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (created_at TEXT)"))
        with engine.begin() as conn:
            Database._ensure_indexes(conn, ["users"])
        with engine.begin() as conn:
            Database._ensure_indexes(conn, ["users"])
        engine.dispose()


class TestDatabaseInit:
    """Database 初始化和属性测试"""

    def test_init_defaults(self, tmp_path):
        """默认初始化应设置后端、引擎为 None"""
        config = _make_config(tmp_path)
        db = Database(config)
        assert db.backend == "sqlite"
        assert db._engine is None
        assert db._session_factory is None
        assert db.use_fine_grained_locks is True

    def test_init_without_fine_grained_locks(self, tmp_path):
        """禁用细粒度锁时不应创建表锁"""
        config = _make_config(tmp_path)
        db = Database(config, use_fine_grained_locks=False)
        assert db.use_fine_grained_locks is False
        assert len(db._table_locks) == 0

    def test_init_registers_singleton(self, tmp_path):
        """初始化应注册为单例"""
        config = _make_config(tmp_path)
        db = Database(config)
        assert Database.get_instance() is db

    def test_get_instance_none_when_not_init(self):
        """未初始化时 get_instance 应返回 None"""
        assert Database.get_instance() is None

    def test_db_path_sqlite(self, tmp_path):
        """SQLite 后端应返回数据库文件路径"""
        config = _make_config(tmp_path)
        db = Database(config)
        assert db.db_path.endswith("test.db")

    def test_db_path_non_sqlite(self, tmp_path):
        """非 SQLite 后端应返回空字符串"""
        config = _make_config(tmp_path, backend="mysql", host="h", username="u", password="p", database="d")
        db = Database(config)
        assert db.db_path == ""

    def test_audit_db_path_sqlite(self, tmp_path):
        """SQLite 后端应返回 audit.db 路径"""
        config = _make_config(tmp_path)
        db = Database(config)
        assert db.audit_db_path.endswith("audit.db")

    def test_audit_db_path_non_sqlite(self, tmp_path):
        """非 SQLite 后端应返回空字符串"""
        config = _make_config(tmp_path, backend="mysql", host="h", username="u", password="p", database="d")
        db = Database(config)
        assert db.audit_db_path == ""

    def test_engine_raises_when_not_connected(self, tmp_path):
        """未连接时访问 engine 应抛出 RuntimeError"""
        config = _make_config(tmp_path)
        db = Database(config)
        with pytest.raises(RuntimeError, match="NOT_CONNECTED"):
            _ = db.engine

    def test_write_lock_property(self, tmp_path):
        """write_lock 属性应返回 asyncio.Lock"""
        config = _make_config(tmp_path)
        db = Database(config)
        assert isinstance(db.write_lock, asyncio.Lock)


class TestLockManagement:
    """锁管理测试"""

    def test_get_table_lock_known(self, tmp_path):
        """已知表名应返回锁对象"""
        config = _make_config(tmp_path)
        db = Database(config)
        lock = db.get_table_lock("devices")
        assert lock is not None
        assert isinstance(lock, asyncio.Lock)

    def test_get_table_lock_unknown(self, tmp_path):
        """未知表名应返回 None"""
        config = _make_config(tmp_path)
        db = Database(config)
        assert db.get_table_lock("unknown_table") is None

    def test_get_table_lock_disabled(self, tmp_path):
        """禁用细粒度锁时应返回 None"""
        config = _make_config(tmp_path)
        db = Database(config, use_fine_grained_locks=False)
        assert db.get_table_lock("devices") is None

    def test_get_table_lock_name_known(self, tmp_path):
        """已知表名应返回锁名称"""
        config = _make_config(tmp_path)
        db = Database(config)
        assert db.get_table_lock_name("devices") == "_table_lock_devices"

    def test_get_table_lock_name_unknown(self, tmp_path):
        """未知表名应返回 None"""
        config = _make_config(tmp_path)
        db = Database(config)
        assert db.get_table_lock_name("unknown_table") is None

    def test_get_lock_status_enabled(self, tmp_path):
        """启用细粒度锁时状态应包含所有表锁"""
        config = _make_config(tmp_path)
        db = Database(config)
        status = db.get_lock_status()
        assert status["use_fine_grained_locks"] is True
        assert "global_lock" in status
        assert "table_locks" in status
        assert len(status["table_locks"]) == len(Database._TABLE_LOCK_NAMES)
        for name in Database._TABLE_LOCK_NAMES:
            assert name in status["table_locks"]

    def test_get_lock_status_disabled(self, tmp_path):
        """禁用细粒度锁时 table_locks 应为空"""
        config = _make_config(tmp_path)
        db = Database(config, use_fine_grained_locks=False)
        status = db.get_lock_status()
        assert status["use_fine_grained_locks"] is False
        assert status["table_locks"] == {}

    async def test_lock_status_shows_locked(self, tmp_path):
        """获取锁后状态应显示为已锁定"""
        config = _make_config(tmp_path)
        db = Database(config)
        lock = db.get_table_lock("devices")
        async with lock:
            status = db.get_lock_status()
            assert status["table_locks"]["devices"]["locked"] is True


class TestConnect:
    """数据库连接测试"""

    async def test_connect_sqlite_success(self, tmp_path):
        """SQLite 连接应成功并创建引擎"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            engine = await db.connect()
        assert engine is not None
        assert db._engine is not None
        assert db._session_factory is not None
        await db.close()

    async def test_connect_sets_wal_mode(self, tmp_path):
        """连接后应设置 WAL 模式"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        async with db._engine.connect() as conn:
            result = await conn.execute(text("PRAGMA journal_mode"))
            mode = result.fetchone()[0]
        await db.close()
        assert mode.lower() == "wal"

    async def test_connect_sets_busy_timeout(self, tmp_path):
        """连接后应设置 busy_timeout=5000"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        async with db._engine.connect() as conn:
            result = await conn.execute(text("PRAGMA busy_timeout"))
            timeout = result.fetchone()[0]
        await db.close()
        assert timeout == 5000

    async def test_connect_sets_synchronous_normal(self, tmp_path):
        """连接后应设置 synchronous=NORMAL"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        async with db._engine.connect() as conn:
            result = await conn.execute(text("PRAGMA synchronous"))
            sync = result.fetchone()[0]
        await db.close()
        assert sync == 1

    async def test_connect_sets_foreign_keys_on(self, tmp_path):
        """连接后应启用 foreign_keys"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        async with db._engine.connect() as conn:
            result = await conn.execute(text("PRAGMA foreign_keys"))
            fk = result.fetchone()[0]
        await db.close()
        assert fk == 1

    async def test_connect_failure_raises_runtime_error(self, tmp_path):
        """连接失败应抛出 RuntimeError"""
        config = _make_config(tmp_path)
        db = Database(config)
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("connection refused")
        mock_engine.dispose = AsyncMock()
        with (
            patch("edgelite.storage.database.create_async_engine", return_value=mock_engine),
            patch.object(Database, "_register_sqlite_pragmas"),
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
        ):
            with pytest.raises((RuntimeError, AttributeError)):
                await db.connect()

    async def test_connect_returns_engine(self, tmp_path):
        """connect 应返回 AsyncEngine"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            engine = await db.connect()
        assert engine is db._engine
        await db.close()


class TestSessionManagement:
    """会话管理测试"""

    async def test_get_session_returns_session(self, tmp_path):
        """get_session 应返回 AsyncSession"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        session = db.get_session()
        assert session is not None
        await session.close()
        await db.close()

    def test_get_session_raises_when_not_connected(self, tmp_path):
        """未连接时 get_session 应抛出 RuntimeError"""
        config = _make_config(tmp_path)
        db = Database(config)
        with pytest.raises(RuntimeError, match="NOT_CONNECTED"):
            db.get_session()

    async def test_session_context_manager(self, tmp_path):
        """session() 上下文管理器应自动关闭会话"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        async with db.session() as session:
            result = await session.execute(text("SELECT 1"))
            assert result.fetchone()[0] == 1
        await db.close()

    async def test_session_raises_when_not_connected(self, tmp_path):
        """未连接时 session() 应抛出 RuntimeError"""
        config = _make_config(tmp_path)
        db = Database(config)
        with pytest.raises(RuntimeError, match="NOT_CONNECTED"):
            async with db.session() as session:
                pass

    async def test_session_rollback_on_error(self, tmp_path):
        """session() 出错时应正常关闭（finally 块）"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        with pytest.raises(ValueError):
            async with db.session() as session:
                raise ValueError("test error")
        await db.close()


class TestClose:
    """关闭数据库测试"""

    async def test_close_disposes_engine(self, tmp_path):
        """close 应释放引擎并置为 None"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        assert db._engine is not None
        await db.close()
        assert db._engine is None
        assert db._session_factory is None

    async def test_close_when_not_connected(self, tmp_path):
        """未连接时 close 应安全返回"""
        config = _make_config(tmp_path)
        db = Database(config)
        await db.close()

    async def test_engine_raises_after_close(self, tmp_path):
        """关闭后访问 engine 应抛出 RuntimeError"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        await db.close()
        with pytest.raises(RuntimeError, match="NOT_CONNECTED"):
            _ = db.engine


class TestInitTables:
    """表初始化测试"""

    async def test_init_tables_creates_tables(self, tmp_path):
        """init_tables 应创建所有 ORM 表"""
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
            await db.init_tables()
        async with db._engine.begin() as conn:
            tables = await conn.run_sync(lambda sync_conn: sa_inspect(sync_conn).get_table_names())
        await db.close()
        assert "devices" in tables
        assert "rules" in tables
        assert "alarms" in tables
        assert "users" in tables

    async def test_init_tables_creates_admin_user(self, tmp_path, monkeypatch):
        """init_tables 应创建初始 admin 用户"""
        monkeypatch.setenv("EDGELITE_ADMIN_PASSWORD", "TestPass123!")
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
            await db.init_tables()
        from sqlalchemy import select

        async with db._session_factory() as session:
            result = await session.execute(select(UserORM).where(UserORM.username == "admin"))
            admin = result.scalar_one_or_none()
        await db.close()
        assert admin is not None
        assert admin.role == "admin"
        assert admin.must_change_password is True

    async def test_init_tables_idempotent_admin(self, tmp_path, monkeypatch):
        """admin 已存在时不应重复创建"""
        monkeypatch.setenv("EDGELITE_ADMIN_PASSWORD", "TestPass123!")
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
            await db.init_tables()
            await db.init_tables()
        from sqlalchemy import func, select

        async with db._session_factory() as session:
            result = await session.execute(select(func.count()).select_from(UserORM).where(UserORM.username == "admin"))
            count = result.scalar()
        await db.close()
        assert count == 1

    async def test_init_tables_raises_when_not_connected(self, tmp_path):
        """未连接时 init_tables 应抛出 RuntimeError"""
        config = _make_config(tmp_path)
        db = Database(config)
        with pytest.raises(RuntimeError, match="NOT_CONNECTED"):
            await db.init_tables()

    async def test_init_tables_raises_when_session_not_init(self, tmp_path):
        """session_factory 为 None 时应抛出 RuntimeError"""
        config = _make_config(tmp_path)
        db = Database(config)
        db._engine = MagicMock()
        db._session_factory = None
        with pytest.raises(RuntimeError, match="SESSION_NOT_INIT"):
            await db.init_tables()

    async def test_init_tables_password_reset(self, tmp_path, monkeypatch):
        """admin 已存在且设置 RESET 标志时应重置密码"""
        monkeypatch.setenv("EDGELITE_ADMIN_PASSWORD", "FirstPass123!")
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
            await db.init_tables()
        await db.close()
        monkeypatch.setenv("EDGELITE_RESET_ADMIN_PASSWORD", "true")
        monkeypatch.setenv("EDGELITE_ADMIN_PASSWORD", "NewPass456!")
        db2 = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db2.connect()
            await db2.init_tables()
        from sqlalchemy import select

        from edgelite.security.password import verify_password

        async with db2._session_factory() as session:
            result = await session.execute(select(UserORM).where(UserORM.username == "admin"))
            admin = result.scalar_one_or_none()
        await db2.close()
        assert admin is not None
        assert verify_password("NewPass456!", admin.password)


class TestPasswordResetHelpers:
    """密码重置辅助方法测试"""

    async def test_check_password_already_reset_match(self, tmp_path, monkeypatch):
        """密码匹配时应返回 True"""

        monkeypatch.setenv("EDGELITE_ADMIN_PASSWORD", "TestPass123!")
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
            await db.init_tables()
        from sqlalchemy import select

        async with db._session_factory() as session:
            result = await session.execute(select(UserORM).where(UserORM.username == "admin"))
            admin = result.scalar_one_or_none()
        await db.close()
        assert admin is not None
        result = await db._check_password_already_reset(admin, "TestPass123!")
        assert result is True

    async def test_check_password_already_reset_no_match(self, tmp_path, monkeypatch):
        """密码不匹配时应返回 False"""
        monkeypatch.setenv("EDGELITE_ADMIN_PASSWORD", "TestPass123!")
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
            await db.init_tables()
        from sqlalchemy import select

        async with db._session_factory() as session:
            result = await session.execute(select(UserORM).where(UserORM.username == "admin"))
            admin = result.scalar_one_or_none()
        await db.close()
        result = await db._check_password_already_reset(admin, "WrongPass!")
        assert result is False

    async def test_check_password_already_reset_exception(self, tmp_path):
        """异常时应返回 False"""
        config = _make_config(tmp_path)
        db = Database(config)
        fake_user = MagicMock()
        fake_user.password = "invalid_hash"
        result = await db._check_password_already_reset(fake_user, "somepass")
        assert result is False

    def test_clear_password_reset_flag_clears_env(self, tmp_path, monkeypatch):
        """应从环境变量中清除重置标志"""
        monkeypatch.setenv("EDGELITE_RESET_ADMIN_PASSWORD", "true")
        monkeypatch.setenv("EDGELITE_ADMIN_PASSWORD", "somepass")
        config = _make_config(tmp_path)
        db = Database(config)
        db._clear_password_reset_flag()
        assert "EDGELITE_RESET_ADMIN_PASSWORD" not in os.environ
        assert "EDGELITE_ADMIN_PASSWORD" not in os.environ

    def test_clear_password_reset_flag_no_env_file(self, tmp_path, monkeypatch):
        """没有 .env 文件时应安全处理"""
        monkeypatch.setenv("EDGELITE_RESET_ADMIN_PASSWORD", "true")
        monkeypatch.chdir(tmp_path)
        config = _make_config(tmp_path)
        db = Database(config)
        db._clear_password_reset_flag()
        assert "EDGELITE_RESET_ADMIN_PASSWORD" not in os.environ


class TestSqliteIntegrity:
    """SQLite 完整性检查测试"""

    async def test_integrity_check_healthy_db(self, tmp_path):
        """健康数据库的完整性检查应通过"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        await db._check_sqlite_integrity()
        await db.close()

    async def test_integrity_check_no_engine(self, tmp_path):
        """引擎为 None 时应安全返回"""
        config = _make_config(tmp_path)
        db = Database(config)
        await db._check_sqlite_integrity()

    async def test_integrity_check_corrupt_triggers_rebuild(self, tmp_path):
        """完整性检查失败时应尝试重建"""
        config = _make_config(tmp_path)
        db = Database(config)
        mock_engine = MagicMock()
        mock_conn = AsyncMock()
        mock_result = MagicMock()
        mock_result.fetchone.return_value = ["corrupt"]
        mock_conn.execute = AsyncMock(return_value=mock_result)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_engine.begin.return_value = mock_ctx
        db._engine = mock_engine
        with (
            patch.object(Database, "_rebuild_sqlite_database", new_callable=AsyncMock) as mock_rebuild,
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
        ):
            await db._check_sqlite_integrity()
            mock_rebuild.assert_called()


class TestSidecarIntegrity:
    """Sidecar 数据库完整性检查测试"""

    async def test_sidecar_check_skips_nonexistent(self, tmp_path):
        """不存在的 sidecar 文件应跳过"""
        config = _make_config(tmp_path)
        db = Database(config)
        await db._check_sidecar_integrity()

    async def test_sidecar_check_healthy(self, tmp_path, monkeypatch):
        """健康的 sidecar 应通过检查"""
        sidecar_path = str(tmp_path / "audit.db")
        conn = sqlite3.connect(sidecar_path)
        conn.execute("CREATE TABLE test (id INTEGER)")
        conn.commit()
        conn.close()
        monkeypatch.setattr("edgelite.storage.database._get_sidecar_db_path", lambda p: sidecar_path)
        config = _make_config(tmp_path)
        db = Database(config)
        await db._check_sidecar_integrity()

    async def test_sidecar_check_corrupt_triggers_recovery(self, tmp_path, monkeypatch):
        """损坏的 sidecar 应触发恢复"""
        sidecar_path = str(tmp_path / "audit.db")
        with open(sidecar_path, "wb") as f:
            f.write(b"not a database")
        monkeypatch.setattr("edgelite.storage.database._get_sidecar_db_path", lambda p: sidecar_path)
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(db, "_recover_sidecar_db", new_callable=AsyncMock) as mock_recover:
            await db._check_sidecar_integrity()
            mock_recover.assert_called()


class TestRecoverSidecar:
    """Sidecar 数据库恢复测试"""

    async def test_recover_from_backup(self, tmp_path):
        """有备份时应从备份恢复"""
        sidecar_path = str(tmp_path / "audit.db")
        with open(sidecar_path, "wb") as f:
            f.write(b"corrupt data")
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        backup_file = backup_dir / "audit.db.backup.20260101_120000"
        conn = sqlite3.connect(str(backup_file))
        conn.execute("CREATE TABLE test (id INTEGER)")
        conn.commit()
        conn.close()
        config = _make_config(tmp_path)
        db = Database(config)
        await db._recover_sidecar_db(sidecar_path, "audit.db")
        assert Path(sidecar_path).exists()
        verify_conn = sqlite3.connect(sidecar_path)
        result = verify_conn.execute("PRAGMA integrity_check").fetchone()
        verify_conn.close()
        assert result[0] == "ok"

    async def test_recover_no_backup_rebuilds_empty(self, tmp_path):
        """无备份时应删除损坏文件（空库将在下次访问时重建）"""
        sidecar_path = str(tmp_path / "audit.db")
        with open(sidecar_path, "wb") as f:
            f.write(b"corrupt data")
        config = _make_config(tmp_path)
        db = Database(config)
        await db._recover_sidecar_db(sidecar_path, "audit.db")
        assert not Path(sidecar_path).exists()

    async def test_recover_corrupt_backup_deleted(self, tmp_path):
        """备份也损坏时应删除文件"""
        sidecar_path = str(tmp_path / "audit.db")
        with open(sidecar_path, "wb") as f:
            f.write(b"corrupt data")
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        backup_file = backup_dir / "audit.db.backup.20260101_120000"
        with open(backup_file, "wb") as f:
            f.write(b"also corrupt")
        config = _make_config(tmp_path)
        db = Database(config)
        await db._recover_sidecar_db(sidecar_path, "audit.db")
        assert not Path(sidecar_path).exists()


class TestRebuildDatabase:
    """数据库重建测试"""

    async def test_rebuild_no_db_path_returns(self, tmp_path):
        """数据库路径不存在时应直接返回"""
        config = _make_config(tmp_path, backend="mysql", host="h", username="u", password="p", database="d")
        db = Database(config)
        await db._rebuild_sqlite_database()

    async def test_rebuild_no_backup_raises(self, tmp_path):
        """无备份重建应抛出 RuntimeError（数据丢失）"""
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
        await db.close()
        db_path = db.db_path
        assert Path(db_path).exists()
        db2 = Database(config)
        db2._engine = None
        db2._session_factory = None
        with patch.object(Database, "_migrate", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="corrupted|Data loss|rebuilt"):
                await db2._rebuild_sqlite_database()

    async def test_rebuild_with_backup_restores(self, tmp_path):
        """有备份时应从备份恢复"""
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
            async with db.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        await db.close()
        db_path = db.db_path
        db_name = Path(db_path).name
        backup_dir = Path(config.database.backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        import shutil

        backup_file = backup_dir / (db_name + ".backup.20260101_120000")
        shutil.copy2(db_path, backup_file)
        db2 = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db2.connect()
        await db2.close()


class TestEnsureSchemaColumns:
    """schema 列补全测试"""

    async def test_ensure_schema_columns_adds_missing(self, tmp_path):
        """应补齐缺失的关键列"""
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
        async with db._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(lambda sc: sc.execute(text("ALTER TABLE users DROP COLUMN must_change_password")))
        async with db._engine.begin() as conn:
            await db._ensure_schema_columns(conn)
        async with db._engine.connect() as conn:
            cols = await conn.run_sync(lambda sc: [c["name"] for c in sa_inspect(sc).get_columns("users")])
        await db.close()
        assert "must_change_password" in cols

    async def test_ensure_schema_columns_creates_alarm_silences(self, tmp_path):
        """应创建缺失的 alarm_silences 表"""
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
        async with db._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(lambda sc: sc.execute(text("DROP TABLE IF EXISTS alarm_silences")))
        async with db._engine.begin() as conn:
            await db._ensure_schema_columns(conn)
        async with db._engine.connect() as conn:
            tables = await conn.run_sync(lambda sc: sa_inspect(sc).get_table_names())
        await db.close()
        assert "alarm_silences" in tables

    async def test_ensure_schema_columns_creates_rule_versions(self, tmp_path):
        """应创建缺失的 rule_versions 表"""
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
        async with db._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(lambda sc: sc.execute(text("DROP TABLE IF EXISTS rule_versions")))
        async with db._engine.begin() as conn:
            await db._ensure_schema_columns(conn)
        async with db._engine.connect() as conn:
            tables = await conn.run_sync(lambda sc: sa_inspect(sc).get_table_names())
        await db.close()
        assert "rule_versions" in tables


class TestFixConstraints:
    """CHECK 约束修复测试"""

    def test_fix_rules_check_constraints_no_table(self, tmp_path):
        """rules 表不存在时应安全返回"""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            Database._fix_rules_check_constraints(conn)
        engine.dispose()

    def test_fix_rules_check_constraints_already_valid(self, tmp_path):
        """约束已正确时不需要修复"""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(
                text("""CREATE TABLE rules (
                rule_id VARCHAR(64) PRIMARY KEY,
                name VARCHAR(64) NOT NULL,
                device_id VARCHAR(64),
                conditions TEXT DEFAULT '[]',
                logic VARCHAR(8) DEFAULT 'AND',
                duration INTEGER DEFAULT 0,
                severity VARCHAR(16) NOT NULL,
                enabled BOOLEAN DEFAULT 1,
                notify_channels TEXT DEFAULT '[]',
                created_by VARCHAR(64),
                created_at DATETIME,
                version INTEGER DEFAULT 1,
                updated_at DATETIME,
                script TEXT DEFAULT '',
                rule_type VARCHAR(16) DEFAULT 'threshold',
                CONSTRAINT ck_rules_logic_valid CHECK (logic IN ('AND', 'OR', 'NOT')),
                CONSTRAINT ck_rules_severity_valid CHECK (severity IN ('critical', 'major', 'warning', 'minor', 'info')),
                CONSTRAINT ck_rules_duration_non_negative CHECK (duration >= 0),
                CONSTRAINT ck_rules_rule_type_valid CHECK (rule_type IN ('threshold', 'ai_inference', 'script'))
            )""")
            )
        with engine.connect() as conn:
            Database._fix_rules_check_constraints(conn)
        engine.dispose()

    def test_fix_rule_type_check_constraints_no_tables(self, tmp_path):
        """表不存在时应安全返回"""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            Database._fix_rule_type_check_constraints(conn)
        engine.dispose()

    def test_fix_rule_type_check_constraints_already_valid(self, tmp_path):
        """约束已正确时不需要修复"""
        db_path = str(tmp_path / "test.db")
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(
                text("""CREATE TABLE rules (
                rule_id VARCHAR(64) PRIMARY KEY,
                rule_type VARCHAR(16) DEFAULT 'threshold',
                CONSTRAINT ck_rules_rule_type_valid CHECK (rule_type IN ('threshold', 'ai_inference', 'script'))
            )""")
            )
        with engine.connect() as conn:
            Database._fix_rule_type_check_constraints(conn)
        engine.dispose()


class TestMigrate:
    """数据库迁移测试"""

    async def test_migrate_no_alembic_dir(self, tmp_path, monkeypatch):
        """alembic 目录不存在时应跳过迁移"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        monkeypatch.chdir(tmp_path)
        with patch("pathlib.Path.exists", return_value=False):
            result = await db._migrate(MagicMock())
        await db.close()
        assert result is None

    async def test_migrate_success(self, tmp_path, monkeypatch):
        """迁移成功应返回 True"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        monkeypatch.chdir(tmp_path)
        alembic_dir = tmp_path / "alembic"
        alembic_dir.mkdir()
        (tmp_path / "alembic.ini").write_text("[alembic]")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"OK", b""))
        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch.object(db, "_update_migration_status", new_callable=AsyncMock),
        ):
            result = await db._migrate(MagicMock())
        await db.close()
        assert result is True

    async def test_migrate_failure(self, tmp_path, monkeypatch):
        """迁移失败应返回 False"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        monkeypatch.chdir(tmp_path)
        alembic_dir = tmp_path / "alembic"
        alembic_dir.mkdir()
        (tmp_path / "alembic.ini").write_text("[alembic]")
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch.object(db, "_notify_migration_failure", new_callable=AsyncMock),
        ):
            result = await db._migrate(MagicMock())
        await db.close()
        assert result is False

    async def test_migrate_timeout(self, tmp_path, monkeypatch):
        """迁移超时应返回 False"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        monkeypatch.chdir(tmp_path)
        alembic_dir = tmp_path / "alembic"
        alembic_dir.mkdir()
        (tmp_path / "alembic.ini").write_text("[alembic]")
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(side_effect=TimeoutError())
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch("asyncio.wait_for", side_effect=TimeoutError()),
            patch.object(db, "_notify_migration_failure", new_callable=AsyncMock),
        ):
            result = await db._migrate(MagicMock())
        await db.close()
        assert result is False

    async def test_migrate_file_not_found(self, tmp_path, monkeypatch):
        """alembic 命令不存在时应返回 None"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        monkeypatch.chdir(tmp_path)
        alembic_dir = tmp_path / "alembic"
        alembic_dir.mkdir()
        (tmp_path / "alembic.ini").write_text("[alembic]")
        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
            result = await db._migrate(MagicMock())
        await db.close()
        assert result is None


class TestMigrationStatus:
    """迁移状态管理测试"""

    async def test_notify_migration_failure_no_app_state(self, tmp_path):
        """_app_state 为 None 时应安全返回"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch("edgelite.app._app_state", None):
            await db._notify_migration_failure("err", "out", "sqlite")

    async def test_notify_migration_failure_stores_status(self, tmp_path):
        """应将失败状态存储到 _app_state"""
        config = _make_config(tmp_path)
        db = Database(config)
        mock_state = SimpleNamespace()
        with patch("edgelite.app._app_state", mock_state):
            await db._notify_migration_failure("migration error", "stdout", "sqlite")
        assert hasattr(mock_state, "_migration_status")
        assert mock_state._migration_status["current_status"] == "failed"
        assert mock_state._migration_status["last_failure"]["error"] == "migration error"

    async def test_update_migration_status_success(self, tmp_path):
        """成功状态应清除上次失败记录"""
        config = _make_config(tmp_path)
        db = Database(config)
        mock_state = SimpleNamespace()
        mock_state._migration_status = {"last_failure": {"error": "old"}}
        with patch("edgelite.app._app_state", mock_state):
            await db._update_migration_status("success", None)
        assert "last_failure" not in mock_state._migration_status
        assert mock_state._migration_status["current_status"] == "success"

    async def test_update_migration_status_failed(self, tmp_path):
        """失败状态应存储错误信息"""
        config = _make_config(tmp_path)
        db = Database(config)
        mock_state = SimpleNamespace()
        with patch("edgelite.app._app_state", mock_state):
            await db._update_migration_status("failed", "some error")
        assert mock_state._migration_status["current_status"] == "failed"
        assert mock_state._migration_status["last_failure"]["error"] == "some error"

    async def test_update_migration_status_no_app_state(self, tmp_path):
        """_app_state 为 None 时应安全返回"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch("edgelite.app._app_state", None):
            await db._update_migration_status("success", None)


class TestBackup:
    """数据库备份测试"""

    async def test_backup_sqlite(self, tmp_path):
        """SQLite 备份应创建备份文件"""
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
        backup_path = str(tmp_path / "backup.db")
        with patch.object(db, "_backup_sidecar_dbs", new_callable=AsyncMock):
            await db.backup(backup_path)
        await db.close()
        assert Path(backup_path).exists()

    async def test_backup_sqlite_with_wal(self, tmp_path):
        """有 WAL 文件时应一并备份"""
        config = _make_config(tmp_path)
        db = Database(config)
        with (
            patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock),
            patch.object(Database, "_migrate", new_callable=AsyncMock),
        ):
            await db.connect()
        async with db._engine.begin() as conn:
            await conn.execute(text("CREATE TABLE test (id INTEGER)"))
            await conn.execute(text("INSERT INTO test VALUES (1)"))
        backup_path = str(tmp_path / "backup.db")
        with patch.object(db, "_backup_sidecar_dbs", new_callable=AsyncMock):
            await db.backup(backup_path)
        await db.close()
        assert Path(backup_path).exists()
        assert Path(backup_path).stat().st_size > 0


class TestBackupSidecar:
    """Sidecar 数据库备份测试"""

    async def test_backup_sidecar_dbs_skips_nonexistent(self, tmp_path):
        """不存在的 sidecar 应跳过"""
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        main_backup = str(tmp_path / "backup.db")
        await db._backup_sidecar_dbs(main_backup)
        await db.close()

    async def test_backup_sidecar_dbs_copies_existing(self, tmp_path, monkeypatch):
        """存在的 sidecar 应被备份"""
        sidecar_path = str(tmp_path / "audit.db")
        conn = sqlite3.connect(sidecar_path)
        conn.execute("CREATE TABLE test (id INTEGER)")
        conn.commit()
        conn.close()
        monkeypatch.setattr("edgelite.storage.database._get_sidecar_db_path", lambda p: sidecar_path)
        config = _make_config(tmp_path)
        db = Database(config)
        with patch.object(Database, "_check_sidecar_integrity", new_callable=AsyncMock):
            await db.connect()
        main_backup = str(tmp_path / "backups" / "main.db")
        Path(main_backup).parent.mkdir(parents=True, exist_ok=True)
        await db._backup_sidecar_dbs(main_backup)
        await db.close()
        backup_files = list(Path(main_backup).parent.glob("*.db"))
        assert len(backup_files) > 0

    def test_get_sidecar_write_lock_unknown(self, tmp_path):
        """未知路径应返回 None"""
        config = _make_config(tmp_path)
        db = Database(config)
        result = db._get_sidecar_write_lock("data/unknown.db")
        assert result is None

    def test_get_config_version_manager_unknown(self, tmp_path):
        """未知路径应返回 None"""
        config = _make_config(tmp_path)
        db = Database(config)
        result = db._get_config_version_manager("data/unknown.db")
        assert result is None

    def test_get_config_version_manager_no_driver(self, tmp_path):
        """驱动不存在时应返回 None"""
        config = _make_config(tmp_path)
        db = Database(config)
        mock_state = SimpleNamespace()
        with patch("edgelite.app._app_state", mock_state):
            result = db._get_config_version_manager("data/s7_config_versions.db")
        assert result is None


class TestRestoreSidecar:
    """Sidecar 数据库恢复测试"""

    async def test_restore_sidecar_no_backup_dir(self, tmp_path):
        """备份目录不存在时应安全返回"""
        config = _make_config(tmp_path)
        db = Database(config)
        backup_dir = tmp_path / "nonexistent"
        await db._restore_sidecar_dbs("main.db", backup_dir, "main")

    async def test_restore_sidecar_no_backups(self, tmp_path, monkeypatch):
        """无备份文件时应跳过"""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        monkeypatch.setattr("edgelite.storage.database._get_sidecar_db_path", lambda p: str(tmp_path / "audit.db"))
        config = _make_config(tmp_path)
        db = Database(config)
        await db._restore_sidecar_dbs("main.db", backup_dir, "main")


class TestBackupBackends:
    """非 SQLite 后端备份测试"""

    async def test_backup_mysql_not_found(self, tmp_path):
        """mysqldump 不存在时应抛出 RuntimeError"""
        config = _make_config(tmp_path, backend="mysql", host="h", username="u", password="p", database="d")
        db = Database(config)
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(RuntimeError, match="mysqldump not found"):
                await db._backup_mysql(str(tmp_path / "backup.sql"))

    async def test_backup_mysql_failure(self, tmp_path):
        """mysqldump 失败时应抛出 RuntimeError"""
        config = _make_config(tmp_path, backend="mysql", host="h", username="u", password="p", database="d")
        db = Database(config)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"connection refused"
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="mysqldump failed"):
                await db._backup_mysql(str(tmp_path / "backup.sql"))

    async def test_backup_postgresql_not_found(self, tmp_path):
        """pg_dump 不存在时应抛出 RuntimeError"""
        config = _make_config(tmp_path, backend="postgresql", host="h", username="u", password="p", database="d")
        db = Database(config)
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(RuntimeError, match="pg_dump not found"):
                await db._backup_postgresql(str(tmp_path / "backup.sql"))

    async def test_backup_postgresql_failure(self, tmp_path):
        """pg_dump 失败时应抛出 RuntimeError"""
        config = _make_config(tmp_path, backend="postgresql", host="h", username="u", password="p", database="d")
        db = Database(config)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"connection refused"
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="pg_dump failed"):
                await db._backup_postgresql(str(tmp_path / "backup.sql"))

    async def test_backup_mssql_not_found(self, tmp_path):
        """sqlcmd 不存在时应抛出 RuntimeError"""
        config = _make_config(tmp_path, backend="mssql", host="h", username="u", password="p", database="d")
        db = Database(config)
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(RuntimeError, match="sqlcmd not found"):
                await db._backup_mssql(str(tmp_path / "backup.bak"))

    async def test_backup_mssql_failure(self, tmp_path):
        """sqlcmd 失败时应抛出 RuntimeError"""
        config = _make_config(tmp_path, backend="mssql", host="h", username="u", password="p", database="d")
        db = Database(config)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"login failed"
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="sqlcmd BACKUP failed"):
                await db._backup_mssql(str(tmp_path / "backup.bak"))

    async def test_backup_dispatches_to_mysql(self, tmp_path):
        """MySQL 后端应调用 _backup_mysql"""
        config = _make_config(tmp_path, backend="mysql", host="h", username="u", password="p", database="d")
        db = Database(config)
        with (
            patch.object(db, "_backup_mysql", new_callable=AsyncMock) as mock_mysql,
            patch.object(db, "_backup_sidecar_dbs", new_callable=AsyncMock),
        ):
            await db.backup(str(tmp_path / "backup.sql"))
        mock_mysql.assert_called_once()

    async def test_backup_dispatches_to_postgresql(self, tmp_path):
        """PostgreSQL 后端应调用 _backup_postgresql"""
        config = _make_config(tmp_path, backend="postgresql", host="h", username="u", password="p", database="d")
        db = Database(config)
        with (
            patch.object(db, "_backup_postgresql", new_callable=AsyncMock) as mock_pg,
            patch.object(db, "_backup_sidecar_dbs", new_callable=AsyncMock),
        ):
            await db.backup(str(tmp_path / "backup.sql"))
        mock_pg.assert_called_once()

    async def test_backup_dispatches_to_mssql(self, tmp_path):
        """MSSQL 后端应调用 _backup_mssql"""
        config = _make_config(tmp_path, backend="mssql", host="h", username="u", password="p", database="d")
        db = Database(config)
        with (
            patch.object(db, "_backup_mssql", new_callable=AsyncMock) as mock_mssql,
            patch.object(db, "_backup_sidecar_dbs", new_callable=AsyncMock),
        ):
            await db.backup(str(tmp_path / "backup.bak"))
        mock_mssql.assert_called_once()
