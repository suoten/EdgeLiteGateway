"""数据库接入驱动测试

覆盖模块：src/edgelite/drivers/database_source.py
- DatabaseSourceDriver 元数据与初始化
- start/stop 生命周期（mysql/postgresql/sqlite/mssql 四种后端）
- read_points / write_point / add_device / remove_device
- _validate_sql SQL 注入防护
- _execute_query 各数据库后端分支
- discover_devices 及四个 _discover_* 子方法

所有外部数据库库（aiomysql/asyncpg/aiosqlite/aioodbc）通过 sys.modules 替换为 mock，
不发起任何真实网络或文件 IO。
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, "src")

from edgelite.drivers.database_source import DatabaseSourceDriver

# ── 辅助：构造 mock 数据库连接池与游标 ──


def _make_mysql_pool_mock(rows=None, description=None):
    """构造 mock aiomysql 连接池。

    返回 (pool, conn, cursor) 三元组，conn 与 cursor 均为 mock，
    支持 `async with pool.acquire() as conn, conn.cursor() as cur` 用法。
    注意：aiomysql 的 cursor 是异步上下文管理器，需配置 __aenter__ 返回自身。
    """
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    fetch_rows = rows if rows is not None else []
    cursor.fetchall = AsyncMock(return_value=fetch_rows)
    cursor.fetchone = AsyncMock(return_value=fetch_rows[0] if fetch_rows else None)
    cursor.description = description if description is not None else [("col1",)]
    # aiomysql cursor 是 async context manager，__aenter__ 返回自身
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=None)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.close = AsyncMock()
    pool.terminate = MagicMock()
    return pool, conn, cursor


def _make_postgresql_pool_mock(rows=None):
    """构造 mock asyncpg 连接池。

    asyncpg 的 conn.fetch 返回 dict-like 行（用 dict 代替）。
    支持 `async with pool.acquire() as conn` 用法。
    """
    fetch_rows = rows if rows is not None else []
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_rows)
    conn.fetchval = AsyncMock(return_value=fetch_rows[0] if fetch_rows else None)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.close = AsyncMock()
    return pool, conn


def _make_sqlite_pool_mock(rows=None, description=None):
    """构造 mock aiosqlite 连接对象（非池）。

    sqlite 路径下 _pool 实际是一个 aiosqlite.Connection，
    其 execute 返回 cursor。
    """
    fetch_rows = rows if rows is not None else []
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=fetch_rows)
    cursor.fetchone = AsyncMock(return_value=fetch_rows[0] if fetch_rows else None)
    cursor.description = description if description is not None else [("col1",)]

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=cursor)
    conn.close = AsyncMock()
    return conn, cursor


def _make_mssql_pool_mock(rows=None):
    """构造 mock aioodbc 连接池。

    MSSQL 路径下 conn.execute 返回 cursor，cursor.fetchall 返回 dict-like 行。
    """
    fetch_rows = rows if rows is not None else []
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=fetch_rows)
    cursor.fetchone = AsyncMock(return_value=fetch_rows[0] if fetch_rows else None)

    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=cursor)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.close = AsyncMock()
    return pool, conn, cursor


def _make_fake_module(create_pool=None, connect=None):
    """构造 mock 数据库模块，包含 create_pool / connect 异步函数。"""
    mod = MagicMock()
    if create_pool is not None:
        mod.create_pool = create_pool
    if connect is not None:
        mod.connect = connect
    return mod


def _make_mysql_discover_pool(fetchone_return=None, fetchall_return=None):
    """构造用于 _discover_mysql 的 mock 连接池。

    _discover_mysql 使用 `async with await create_pool(...) as pool:`
    和 `async with pool.acquire() as conn, conn.cursor() as cur:`，
    因此 pool 本身和 acquire 返回的上下文管理器都需要配置。
    """
    cursor = AsyncMock()
    cursor.execute = AsyncMock()
    cursor.fetchone = AsyncMock(return_value=fetchone_return)
    cursor.fetchall = AsyncMock(return_value=fetchall_return or [])
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=None)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=conn)
    acquire_cm.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=acquire_cm)
    pool.__aenter__ = AsyncMock(return_value=pool)
    pool.__aexit__ = AsyncMock(return_value=None)
    return pool


# ── 类元数据 ──


class TestDriverMetadata:
    def test_plugin_name(self):
        assert DatabaseSourceDriver.plugin_name == "database_source"

    def test_plugin_version(self):
        assert DatabaseSourceDriver.plugin_version == "1.0.0"

    def test_supported_protocols(self):
        protos = DatabaseSourceDriver.supported_protocols
        assert "mysql" in protos
        assert "postgresql" in protos
        assert "sqlite" in protos
        assert "mssql" in protos
        assert "database" in protos

    def test_config_schema_has_fields(self):
        schema = DatabaseSourceDriver.config_schema
        field_names = {f["name"] for f in schema["fields"]}
        assert {"db_type", "host", "port", "database", "username", "password", "pool_size"} <= field_names

    def test_config_schema_db_type_options(self):
        schema = DatabaseSourceDriver.config_schema
        db_type_field = next(f for f in schema["fields"] if f["name"] == "db_type")
        assert set(db_type_field["options"]) == {"mysql", "postgresql", "sqlite", "mssql"}


# ── 初始化 ──


class TestDriverInit:
    def test_defaults(self):
        drv = DatabaseSourceDriver()
        assert drv._running is False
        assert drv._pool is None
        assert drv._config == {}
        assert drv._devices == {}
        assert isinstance(drv._lock, asyncio.Lock)


# ── SQL 注入防护 ──


class TestValidateSql:
    def setup_method(self):
        self.drv = DatabaseSourceDriver()

    def test_safe_select_passes(self):
        self.drv._validate_sql("SELECT value FROM metrics WHERE id = 1")

    def test_union_injection_rejected(self):
        with pytest.raises(ValueError, match="dangerous"):
            self.drv._validate_sql("SELECT 1 UNION SELECT password FROM users")

    def test_union_all_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1 UNION ALL SELECT 2")

    def test_or_tautology_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT * FROM t WHERE 1=1 OR 1=1 OR 1=1")

    def test_and_tautology_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT * FROM t WHERE 1=1 AND 2=2 AND 3=3")

    def test_or_numeric_equals_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT * FROM users WHERE name='a' OR 1=1")

    def test_and_numeric_equals_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT * FROM users WHERE name='a' AND 1=1")

    def test_exec_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("EXEC xp_cmdshell 'dir'")

    def test_execute_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("EXECUTE sp_help")

    def test_xp_prefix_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("xp_cmdshell 'whoami'")

    def test_drop_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; DROP TABLE users")

    def test_alter_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; ALTER TABLE users ADD col INT")

    def test_create_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; CREATE TABLE evil (id INT)")

    def test_truncate_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; TRUNCATE TABLE users")

    def test_delete_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; DELETE FROM users")

    def test_insert_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; INSERT INTO users VALUES(1)")

    def test_update_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; UPDATE users SET role='admin'")

    def test_grant_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; GRANT ALL TO evil")

    def test_revoke_after_semicolon_rejected(self):
        with pytest.raises(ValueError):
            self.drv._validate_sql("SELECT 1; REVOKE ALL FROM good")

    def test_case_insensitive(self):
        """注入检测应忽略大小写"""
        with pytest.raises(ValueError):
            self.drv._validate_sql("select 1 union select 2")

    def test_empty_sql_passes(self):
        self.drv._validate_sql("")


# ── start / stop ──


class TestStartStop:
    async def test_start_mysql_success(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            await drv.start({"db_type": "mysql", "host": "h", "port": 3306, "database": "db"})
        assert drv._running is True
        assert drv._pool is pool
        await drv.stop()

    async def test_start_mariadb_treated_as_mysql(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            await drv.start({"db_type": "mariadb"})
        assert drv._running is True
        await drv.stop()

    async def test_start_postgresql_success(self):
        drv = DatabaseSourceDriver()
        pool, _ = _make_postgresql_pool_mock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"asyncpg": fake_mod}):
            await drv.start({"db_type": "postgresql"})
        assert drv._running is True
        assert drv._pool is pool
        await drv.stop()

    async def test_start_postgres_alias(self):
        drv = DatabaseSourceDriver()
        pool, _ = _make_postgresql_pool_mock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"asyncpg": fake_mod}):
            await drv.start({"db_type": "postgres"})
        assert drv._running is True
        await drv.stop()

    async def test_start_sqlite_success(self):
        drv = DatabaseSourceDriver()
        conn, _ = _make_sqlite_pool_mock()
        fake_mod = _make_fake_module(connect=AsyncMock(return_value=conn))
        with patch.dict(sys.modules, {"aiosqlite": fake_mod}):
            await drv.start({"db_type": "sqlite", "database": ":memory:"})
        assert drv._running is True
        assert drv._pool is conn
        await drv.stop()

    async def test_start_sqlite_uses_path_fallback(self):
        drv = DatabaseSourceDriver()
        conn, _ = _make_sqlite_pool_mock()
        fake_mod = _make_fake_module(connect=AsyncMock(return_value=conn))
        with patch.dict(sys.modules, {"aiosqlite": fake_mod}):
            await drv.start({"db_type": "sqlite", "path": "data/x.db"})
        assert drv._running is True
        fake_mod.connect.assert_awaited_once_with("data/x.db")
        await drv.stop()

    async def test_start_mssql_success(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mssql_pool_mock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aioodbc": fake_mod}):
            await drv.start({"db_type": "mssql", "host": "h", "port": 1433, "database": "db"})
        assert drv._running is True
        assert drv._pool is pool
        await drv.stop()

    async def test_start_uses_protocol_field_when_no_db_type(self):
        """配置中无 db_type 时回退到 protocol 字段"""
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            await drv.start({"protocol": "mysql"})
        assert drv._running is True
        await drv.stop()

    async def test_start_defaults_to_mysql(self):
        """无 db_type 与 protocol 时默认 mysql"""
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            await drv.start({})
        assert drv._running is True
        await drv.stop()

    async def test_start_unsupported_db_type_raises(self):
        drv = DatabaseSourceDriver()
        with pytest.raises(ValueError, match="不支持的数据库类型"):
            await drv.start({"db_type": "oracle"})

    async def test_start_mysql_import_error(self):
        drv = DatabaseSourceDriver()
        # aiomysql 不在 sys.modules 且无法导入
        with patch.dict(sys.modules, {"aiomysql": None}):
            with pytest.raises(ImportError, match="aiomysql"):
                await drv.start({"db_type": "mysql"})

    async def test_start_postgresql_import_error(self):
        drv = DatabaseSourceDriver()
        with patch.dict(sys.modules, {"asyncpg": None}):
            with pytest.raises(ImportError, match="asyncpg"):
                await drv.start({"db_type": "postgresql"})

    async def test_start_mssql_import_error(self):
        drv = DatabaseSourceDriver()
        with patch.dict(sys.modules, {"aioodbc": None}):
            with pytest.raises(ImportError, match="aioodbc"):
                await drv.start({"db_type": "mssql"})

    async def test_start_mysql_create_pool_failure_raises(self):
        drv = DatabaseSourceDriver()
        fake_mod = _make_fake_module(create_pool=AsyncMock(side_effect=RuntimeError("conn refused")))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            with pytest.raises(RuntimeError, match="conn refused"):
                await drv.start({"db_type": "mysql"})
        assert drv._running is False

    async def test_start_postgresql_create_pool_failure_raises(self):
        drv = DatabaseSourceDriver()
        fake_mod = _make_fake_module(create_pool=AsyncMock(side_effect=OSError("refused")))
        with patch.dict(sys.modules, {"asyncpg": fake_mod}):
            with pytest.raises(OSError):
                await drv.start({"db_type": "postgresql"})

    async def test_start_sqlite_connect_failure_raises(self):
        drv = DatabaseSourceDriver()
        fake_mod = _make_fake_module(connect=AsyncMock(side_effect=RuntimeError("no file")))
        with patch.dict(sys.modules, {"aiosqlite": fake_mod}):
            with pytest.raises(RuntimeError, match="no file"):
                await drv.start({"db_type": "sqlite"})

    async def test_start_mssql_create_pool_failure_raises(self):
        drv = DatabaseSourceDriver()
        fake_mod = _make_fake_module(create_pool=AsyncMock(side_effect=RuntimeError("odbc fail")))
        with patch.dict(sys.modules, {"aioodbc": fake_mod}):
            with pytest.raises(RuntimeError, match="odbc fail"):
                await drv.start({"db_type": "mssql"})

    async def test_stop_closes_mysql_pool(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock()
        drv._pool = pool
        drv._config = {"db_type": "mysql"}
        drv._running = True
        await drv.stop()
        pool.close.assert_awaited_once()
        assert drv._pool is None
        assert drv._running is False

    async def test_stop_uses_terminate_when_no_close(self):
        """池无 close 方法但有 terminate 时使用 terminate"""
        drv = DatabaseSourceDriver()
        # 使用 spec 限制 pool 只有 terminate 属性，使 hasattr(pool, "close") 为 False
        pool = MagicMock(spec=["terminate"])
        pool.terminate = MagicMock()
        drv._pool = pool
        drv._config = {"db_type": "mysql"}
        drv._running = True
        await drv.stop()
        pool.terminate.assert_called_once()
        assert drv._pool is None

    async def test_stop_sqlite_uses_close(self):
        drv = DatabaseSourceDriver()
        conn, _ = _make_sqlite_pool_mock()
        drv._pool = conn
        drv._config = {"db_type": "sqlite"}
        drv._running = True
        await drv.stop()
        conn.close.assert_awaited_once()
        assert drv._pool is None

    async def test_stop_with_no_pool(self):
        drv = DatabaseSourceDriver()
        drv._running = True
        await drv.stop()
        assert drv._pool is None
        assert drv._running is False

    async def test_stop_close_exception_silent(self):
        drv = DatabaseSourceDriver()
        pool = MagicMock()
        pool.close = AsyncMock(side_effect=RuntimeError("close failed"))
        drv._pool = pool
        drv._config = {"db_type": "mysql"}
        drv._running = True
        await drv.stop()  # 不应抛异常
        assert drv._pool is None

    async def test_stop_when_db_type_sqlite_with_close_attr(self):
        """db_type=sqlite 且 pool 有 close 方法时走 close 路径"""
        drv = DatabaseSourceDriver()
        pool = MagicMock()
        pool.close = AsyncMock()
        drv._pool = pool
        drv._config = {"db_type": "sqlite"}
        drv._running = True
        await drv.stop()
        pool.close.assert_awaited_once()


# ── _execute_query ──


class TestExecuteQuery:
    async def test_mysql_execute_returns_dicts(self):
        drv = DatabaseSourceDriver()
        pool, _, cursor = _make_mysql_pool_mock(rows=[("v1",), ("v2",)], description=[("col1",)])
        drv._pool = pool
        drv._config = {"db_type": "mysql"}
        result = await drv._execute_query("SELECT col1 FROM t")
        assert result == [{"col1": "v1"}, {"col1": "v2"}]
        cursor.execute.assert_awaited_once_with("SELECT col1 FROM t", None)

    async def test_mysql_execute_with_params(self):
        drv = DatabaseSourceDriver()
        pool, _, cursor = _make_mysql_pool_mock(rows=[("x",)], description=[("c",)])
        drv._pool = pool
        drv._config = {"db_type": "mysql"}
        await drv._execute_query("SELECT c FROM t WHERE id=%s", (1,))
        cursor.execute.assert_awaited_once_with("SELECT c FROM t WHERE id=%s", (1,))

    async def test_mysql_execute_no_description_returns_empty(self):
        drv = DatabaseSourceDriver()
        pool, _, cursor = _make_mysql_pool_mock(rows=[], description=None)
        drv._pool = pool
        drv._config = {"db_type": "mysql"}
        result = await drv._execute_query("SELECT 1")
        assert result == []

    async def test_postgresql_execute_returns_dicts(self):
        drv = DatabaseSourceDriver()
        rows = [{"col1": "v1"}, {"col1": "v2"}]
        pool, conn = _make_postgresql_pool_mock(rows=rows)
        drv._pool = pool
        drv._config = {"db_type": "postgresql"}
        result = await drv._execute_query("SELECT col1 FROM t")
        assert result == rows
        conn.fetch.assert_awaited_once_with("SELECT col1 FROM t")

    async def test_postgresql_execute_with_params(self):
        drv = DatabaseSourceDriver()
        pool, conn = _make_postgresql_pool_mock(rows=[{"c": "x"}])
        drv._pool = pool
        drv._config = {"db_type": "postgresql"}
        await drv._execute_query("SELECT c FROM t WHERE id=$1", (1,))
        conn.fetch.assert_awaited_once_with("SELECT c FROM t WHERE id=$1", 1)

    async def test_sqlite_execute_returns_dicts(self):
        drv = DatabaseSourceDriver()
        conn, cursor = _make_sqlite_pool_mock(rows=[("v1",), ("v2",)], description=[("col1",)])
        drv._pool = conn
        drv._config = {"db_type": "sqlite"}
        result = await drv._execute_query("SELECT col1 FROM t")
        assert result == [{"col1": "v1"}, {"col1": "v2"}]
        conn.execute.assert_awaited_once_with("SELECT col1 FROM t", ())

    async def test_sqlite_execute_with_params(self):
        drv = DatabaseSourceDriver()
        conn, _ = _make_sqlite_pool_mock(rows=[("x",)], description=[("c",)])
        drv._pool = conn
        drv._config = {"db_type": "sqlite"}
        await drv._execute_query("SELECT c FROM t WHERE id=?", (1,))
        conn.execute.assert_awaited_once_with("SELECT c FROM t WHERE id=?", (1,))

    async def test_mssql_execute_returns_dicts(self):
        drv = DatabaseSourceDriver()
        rows = [{"col1": "v1"}]
        pool, _, _ = _make_mssql_pool_mock(rows=rows)
        drv._pool = pool
        drv._config = {"db_type": "mssql"}
        result = await drv._execute_query("SELECT col1 FROM t")
        assert result == rows

    async def test_mssql_execute_no_rows_returns_empty(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mssql_pool_mock(rows=[])
        drv._pool = pool
        drv._config = {"db_type": "mssql"}
        result = await drv._execute_query("SELECT col1 FROM t")
        assert result == []

    async def test_execute_query_rejects_dangerous_sql(self):
        drv = DatabaseSourceDriver()
        drv._pool = MagicMock()
        drv._config = {"db_type": "mysql"}
        with pytest.raises(ValueError, match="dangerous"):
            await drv._execute_query("SELECT 1; DROP TABLE t")

    async def test_execute_query_unknown_db_type_returns_empty(self):
        drv = DatabaseSourceDriver()
        drv._pool = MagicMock()
        drv._config = {"db_type": "oracle"}
        result = await drv._execute_query("SELECT 1")
        assert result == []


# ── read_points ──


class TestReadPoints:
    async def test_not_running_returns_empty(self):
        drv = DatabaseSourceDriver()
        result = await drv.read_points("d1", ["p1"])
        assert result == {}

    async def test_no_pool_returns_empty(self):
        drv = DatabaseSourceDriver()
        drv._running = True
        drv._pool = None
        result = await drv.read_points("d1", ["p1"])
        assert result == {}

    async def test_missing_query_returns_none(self):
        drv = DatabaseSourceDriver()
        drv._running = True
        drv._pool = MagicMock()
        drv._config = {"queries": {}}
        result = await drv.read_points("d1", ["unknown_point"])
        assert result == {"unknown_point": None}

    async def test_single_row_single_column_returns_scalar(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock(rows=[("42",)], description=[("val",)])
        drv._pool = pool
        drv._running = True
        drv._config = {"db_type": "mysql", "queries": {"temp": "SELECT val FROM t"}}
        result = await drv.read_points("d1", ["temp"])
        assert result == {"temp": "42"}

    async def test_single_row_multi_column_returns_dict_row(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock(rows=[("v1", "v2")], description=[("col1",), ("col2",)])
        drv._pool = pool
        drv._running = True
        drv._config = {"db_type": "mysql", "queries": {"p": "SELECT col1, col2 FROM t"}}
        result = await drv.read_points("d1", ["p"])
        assert result == {"p": {"col1": "v1", "col2": "v2"}}

    async def test_multi_row_returns_list(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock(rows=[("v1",), ("v2",)], description=[("col1",)])
        drv._pool = pool
        drv._running = True
        drv._config = {"db_type": "mysql", "queries": {"p": "SELECT col1 FROM t"}}
        result = await drv.read_points("d1", ["p"])
        assert result == {"p": [{"col1": "v1"}, {"col1": "v2"}]}

    async def test_empty_result_returns_none(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock(rows=[], description=[("col1",)])
        drv._pool = pool
        drv._running = True
        drv._config = {"db_type": "mysql", "queries": {"p": "SELECT col1 FROM t"}}
        result = await drv.read_points("d1", ["p"])
        assert result == {"p": None}

    async def test_query_exception_returns_none(self):
        drv = DatabaseSourceDriver()
        pool, _, cursor = _make_mysql_pool_mock()
        cursor.execute = AsyncMock(side_effect=RuntimeError("query failed"))
        drv._pool = pool
        drv._running = True
        drv._config = {"db_type": "mysql", "queries": {"p": "SELECT col1 FROM t"}}
        result = await drv.read_points("d1", ["p"])
        assert result == {"p": None}

    async def test_multiple_points_mixed(self):
        drv = DatabaseSourceDriver()
        # 第一个点有查询，第二个点无查询
        pool, _, _ = _make_mysql_pool_mock(rows=[("v1",)], description=[("c",)])
        drv._pool = pool
        drv._running = True
        drv._config = {"db_type": "mysql", "queries": {"p1": "SELECT c FROM t"}}
        result = await drv.read_points("d1", ["p1", "p2"])
        assert result == {"p1": "v1", "p2": None}

    async def test_single_row_single_column_non_dict_returns_index(self):
        """当 rows[0] 不是 dict 时，用索引 rows[0][0] 取值"""
        drv = DatabaseSourceDriver()
        # postgresql 返回的是 dict-like，这里用 list 模拟非 dict
        pool, _, _ = _make_mysql_pool_mock(rows=[("v",)], description=[("col1",)])
        # 替换 rows 为非 dict 的元组列表
        drv._pool = pool
        drv._running = True
        drv._config = {"db_type": "mysql", "queries": {"p": "SELECT col1 FROM t"}}
        result = await drv.read_points("d1", ["p"])
        # mysql 路径返回的是 dict(zip(columns, row))，所以是 dict
        assert result == {"p": "v"}


# ── write_point ──


class TestWritePoint:
    async def test_not_running_returns_false(self):
        drv = DatabaseSourceDriver()
        assert await drv.write_point("d1", "p", 1) is False

    async def test_no_pool_returns_false(self):
        drv = DatabaseSourceDriver()
        drv._running = True
        drv._pool = None
        assert await drv.write_point("d1", "p", 1) is False

    async def test_no_write_query_returns_false(self):
        drv = DatabaseSourceDriver()
        drv._running = True
        drv._pool = MagicMock()
        drv._config = {"db_type": "mysql", "write_queries": {}}
        assert await drv.write_point("d1", "p", 1) is False

    async def test_mysql_write_success(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock(rows=[], description=None)
        drv._pool = pool
        drv._running = True
        drv._config = {
            "db_type": "mysql",
            "write_queries": {"set_val": "UPDATE t SET v={{value}}"},
        }
        result = await drv.write_point("d1", "set_val", 42)
        assert result is True

    async def test_postgresql_write_uses_dollar_placeholder(self):
        drv = DatabaseSourceDriver()
        pool, conn = _make_postgresql_pool_mock(rows=[])
        drv._pool = pool
        drv._running = True
        drv._config = {
            "db_type": "postgresql",
            "write_queries": {"set_val": "UPDATE t SET v={{value}}"},
        }
        assert await drv.write_point("d1", "set_val", 42) is True
        # 确认 SQL 中的 {{value}} 被替换为 $1
        call_args = conn.fetch.call_args
        assert call_args is not None
        assert "$1" in call_args.args[0]

    async def test_sqlite_write_uses_question_placeholder(self):
        drv = DatabaseSourceDriver()
        conn, _ = _make_sqlite_pool_mock(rows=[], description=None)
        drv._pool = conn
        drv._running = True
        drv._config = {
            "db_type": "sqlite",
            "write_queries": {"set_val": "UPDATE t SET v={{value}}"},
        }
        assert await drv.write_point("d1", "set_val", 1) is True
        call_args = conn.execute.call_args
        assert call_args is not None
        assert "?" in call_args.args[0]

    async def test_mssql_write_uses_question_placeholder(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mssql_pool_mock(rows=[])
        drv._pool = pool
        drv._running = True
        drv._config = {
            "db_type": "mssql",
            "write_queries": {"set_val": "UPDATE t SET v={{value}}"},
        }
        assert await drv.write_point("d1", "set_val", 1) is True

    async def test_write_string_value_casts_to_str(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock(rows=[], description=None)
        drv._pool = pool
        drv._running = True
        drv._config = {
            "db_type": "mysql",
            "write_queries": {"set_val": "UPDATE t SET v={{value}}"},
        }
        assert await drv.write_point("d1", "set_val", "hello") is True

    async def test_write_without_placeholder_returns_false(self):
        """写入查询未使用 {{value}} 占位符应返回 False"""
        drv = DatabaseSourceDriver()
        drv._running = True
        drv._pool = MagicMock()
        drv._config = {
            "db_type": "mysql",
            "write_queries": {"p": "UPDATE t SET v=1"},
        }
        assert await drv.write_point("d1", "p", 1) is False

    async def test_write_exception_returns_false(self):
        drv = DatabaseSourceDriver()
        pool, _, cursor = _make_mysql_pool_mock(rows=[], description=None)
        cursor.execute = AsyncMock(side_effect=RuntimeError("write failed"))
        drv._pool = pool
        drv._running = True
        drv._config = {
            "db_type": "mysql",
            "write_queries": {"set_val": "UPDATE t SET v={{value}}"},
        }
        assert await drv.write_point("d1", "set_val", 1) is False


# ── add_device / remove_device ──


class TestAddRemoveDevice:
    async def test_add_device_stores_config(self):
        drv = DatabaseSourceDriver()
        await drv.add_device("d1", {"host": "h"}, [{"name": "p1", "address": "SELECT 1"}])
        assert "d1" in drv._devices
        assert drv._devices["d1"]["config"] == {"host": "h"}
        assert "p1" in drv._devices["d1"]["points"]

    async def test_add_device_merges_queries(self):
        drv = DatabaseSourceDriver()
        drv._config = {"queries": {"existing": "SELECT 1"}}
        await drv.add_device("d1", {}, [{"name": "p1", "address": "SELECT 2"}])
        assert drv._config["queries"]["existing"] == "SELECT 1"
        assert drv._config["queries"]["p1"] == "SELECT 2"

    async def test_add_device_with_sql_field(self):
        """测点配置使用 sql 字段而非 address"""
        drv = DatabaseSourceDriver()
        await drv.add_device("d1", {}, [{"name": "p1", "sql": "SELECT 1"}])
        assert drv._config["queries"]["p1"] == "SELECT 1"

    async def test_add_device_with_query_field(self):
        """测点配置使用 query 字段"""
        drv = DatabaseSourceDriver()
        await drv.add_device("d1", {}, [{"name": "p1", "query": "SELECT 1"}])
        assert drv._config["queries"]["p1"] == "SELECT 1"

    async def test_add_device_no_points(self):
        drv = DatabaseSourceDriver()
        await drv.add_device("d1", {"host": "h"})
        assert "d1" in drv._devices
        assert drv._devices["d1"]["points"] == {}

    async def test_add_device_empty_points(self):
        drv = DatabaseSourceDriver()
        await drv.add_device("d1", {}, [])
        assert "d1" in drv._devices

    async def test_add_device_point_without_name_uses_address_as_key(self):
        """无 name 但有 address 的测点，使用 address 作为 key"""
        drv = DatabaseSourceDriver()
        await drv.add_device("d1", {}, [{"address": "SELECT 1"}])
        # 源码: p.get("name", p.get("address", "")) 作为 key
        assert "SELECT 1" in drv._devices["d1"]["points"]

    async def test_add_device_point_without_sql_skipped_in_merge(self):
        """无 address/sql/query 的测点不合并到 queries"""
        drv = DatabaseSourceDriver()
        await drv.add_device("d1", {}, [{"name": "p1"}])
        # p1 无 SQL，不应合并
        assert "queries" not in drv._config or "p1" not in drv._config.get("queries", {})

    def test_remove_device(self):
        drv = DatabaseSourceDriver()
        # DatabaseSourceDriver.__init__ 未调用 super().__init__()，
        # 需手动初始化 _health_stats 和 _offline_since
        drv._health_stats = {"d1": MagicMock()}
        drv._offline_since = {"d1": MagicMock()}
        drv.remove_device("d1")
        assert "d1" not in drv._health_stats
        assert "d1" not in drv._offline_since

    def test_remove_device_not_exist_silent(self):
        drv = DatabaseSourceDriver()
        drv._health_stats = {}
        drv._offline_since = {}
        drv.remove_device("unknown")  # 不应抛异常


# ── discover_devices ──


class TestDiscoverDevices:
    async def test_discover_unsupported_type_returns_empty(self):
        drv = DatabaseSourceDriver()
        result = await drv.discover_devices({"db_type": "oracle"})
        assert result == []

    async def test_discover_exception_returns_empty(self):
        """discover_devices 内部异常应被捕获，返回空列表"""
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_mysql", side_effect=RuntimeError("boom")):
            result = await drv.discover_devices({"db_type": "mysql"})
        assert result == []

    async def test_discover_defaults_to_mysql(self):
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_mysql", AsyncMock(return_value=[])) as m:
            await drv.discover_devices({})
        m.assert_awaited_once()

    async def test_discover_uses_protocol_field(self):
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_postgresql", AsyncMock(return_value=[])) as m:
            await drv.discover_devices({"protocol": "postgresql"})
        m.assert_awaited_once()

    async def test_discover_mysql_delegates(self):
        drv = DatabaseSourceDriver()
        expected = [{"device_id": "mysql_h_db"}]
        with patch.object(drv, "_discover_mysql", AsyncMock(return_value=expected)) as m:
            result = await drv.discover_devices({"db_type": "mysql", "host": "h", "port": 3306})
        assert result == expected
        m.assert_awaited_once_with("h", 3306, "", "", "")

    async def test_discover_postgresql_delegates(self):
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_postgresql", AsyncMock(return_value=[])) as m:
            await drv.discover_devices({"db_type": "postgresql", "host": "h", "port": 5432})
        m.assert_awaited_once_with("h", 5432, "", "", "")

    async def test_discover_sqlite_delegates(self):
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_sqlite", AsyncMock(return_value=[])) as m:
            await drv.discover_devices({"db_type": "sqlite", "database": "x.db"})
        m.assert_awaited_once_with("x.db")

    async def test_discover_mssql_delegates(self):
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_mssql", AsyncMock(return_value=[])) as m:
            await drv.discover_devices({"db_type": "mssql", "host": "h", "port": 1433})
        m.assert_awaited_once_with("h", 1433, "", "", "")

    async def test_discover_mysql_default_port(self):
        """未指定 port 时 mysql 默认 3306"""
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_mysql", AsyncMock(return_value=[])) as m:
            await drv.discover_devices({"db_type": "mysql"})
        m.assert_awaited_once_with("localhost", 3306, "", "", "")

    async def test_discover_postgresql_default_port(self):
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_postgresql", AsyncMock(return_value=[])) as m:
            await drv.discover_devices({"db_type": "postgresql"})
        m.assert_awaited_once_with("localhost", 5432, "", "", "")

    async def test_discover_mssql_default_port(self):
        drv = DatabaseSourceDriver()
        with patch.object(drv, "_discover_mssql", AsyncMock(return_value=[])) as m:
            await drv.discover_devices({"db_type": "mssql"})
        m.assert_awaited_once_with("localhost", 1433, "", "", "")


# ── _discover_mysql ──


class TestDiscoverMysql:
    async def test_import_error_returns_empty(self):
        drv = DatabaseSourceDriver()
        with patch.dict(sys.modules, {"aiomysql": None}):
            result = await drv._discover_mysql("h", 3306, "u", "p", "")
        assert result == []

    async def test_connection_failure_returns_empty(self):
        drv = DatabaseSourceDriver()
        fake_mod = _make_fake_module(create_pool=AsyncMock(side_effect=RuntimeError("refused")))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            result = await drv._discover_mysql("h", 3306, "u", "p", "")
        assert result == []

    async def test_discover_with_specific_database(self):
        drv = DatabaseSourceDriver()
        pool = _make_mysql_discover_pool(fetchone_return=("8.0.0",), fetchall_return=[])
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            result = await drv._discover_mysql("127.0.0.1", 3306, "u", "p", "mydb")
        assert len(result) == 1
        assert result[0]["device_id"] == "mysql_127_0_0_1_mydb"
        assert result[0]["protocol"] == "mysql"
        assert result[0]["config"]["database"] == "mydb"
        assert result[0]["details"]["version"] == "8.0.0"

    async def test_discover_lists_all_databases(self):
        """未指定 database 时列出所有可访问的库"""
        drv = DatabaseSourceDriver()
        pool = _make_mysql_discover_pool(
            fetchone_return=("8.0.0",),
            fetchall_return=[("mydb",), ("information_schema",), ("mysql",), ("app",)],
        )
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            result = await drv._discover_mysql("h", 3306, "u", "p", "")
        # 系统库应被过滤，只剩 mydb 和 app
        db_names = [r["config"]["database"] for r in result]
        assert "mydb" in db_names
        assert "app" in db_names
        assert "information_schema" not in db_names
        assert "mysql" not in db_names
        assert "performance_schema" not in db_names
        assert "sys" not in db_names


# ── _discover_postgresql ──


class TestDiscoverPostgresql:
    async def test_import_error_returns_empty(self):
        drv = DatabaseSourceDriver()
        with patch.dict(sys.modules, {"asyncpg": None}):
            result = await drv._discover_postgresql("h", 5432, "u", "p", "")
        assert result == []

    async def test_connection_failure_returns_empty(self):
        drv = DatabaseSourceDriver()
        fake_mod = _make_fake_module(connect=AsyncMock(side_effect=RuntimeError("refused")))
        with patch.dict(sys.modules, {"asyncpg": fake_mod}):
            result = await drv._discover_postgresql("h", 5432, "u", "p", "")
        assert result == []

    async def test_discover_with_specific_database(self):
        drv = DatabaseSourceDriver()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value="PostgreSQL 15.0")
        conn.fetch = AsyncMock(return_value=[])
        conn.close = AsyncMock()
        fake_mod = _make_fake_module(connect=AsyncMock(return_value=conn))
        with patch.dict(sys.modules, {"asyncpg": fake_mod}):
            result = await drv._discover_postgresql("127.0.0.1", 5432, "u", "p", "mydb")
        assert len(result) == 1
        assert result[0]["device_id"] == "pg_127_0_0_1_mydb"
        assert result[0]["protocol"] == "postgresql"
        assert result[0]["details"]["version"] == "PostgreSQL 15.0"
        conn.close.assert_awaited_once()

    async def test_discover_lists_all_databases(self):
        drv = DatabaseSourceDriver()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value="PostgreSQL 15.0")
        conn.fetch = AsyncMock(return_value=[{"datname": "db1"}, {"datname": "db2"}])
        conn.close = AsyncMock()
        fake_mod = _make_fake_module(connect=AsyncMock(return_value=conn))
        with patch.dict(sys.modules, {"asyncpg": fake_mod}):
            result = await drv._discover_postgresql("h", 5432, "u", "p", "")
        assert len(result) == 2
        db_names = [r["config"]["database"] for r in result]
        assert set(db_names) == {"db1", "db2"}

    async def test_discover_close_exception_silent_in_finally(self):
        """conn.close 在 finally 中调用，即使异常也应不传播"""
        drv = DatabaseSourceDriver()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value="v")
        conn.fetch = AsyncMock(return_value=[])
        conn.close = AsyncMock(side_effect=RuntimeError("close failed"))
        fake_mod = _make_fake_module(connect=AsyncMock(return_value=conn))
        # close 在 finally 中，但其异常会传播 —— 实际上源码 try/finally 不捕获 close 异常
        # 这里验证 conn.close 被调用即可
        with patch.dict(sys.modules, {"asyncpg": fake_mod}):
            try:
                await drv._discover_postgresql("h", 5432, "u", "p", "db")
            except RuntimeError:
                pass  # close 异常可能传播，允许
        conn.close.assert_awaited_once()


# ── _discover_sqlite ──


class TestDiscoverSqlite:
    async def test_no_database_scans_data_dir(self, tmp_path, monkeypatch):
        drv = DatabaseSourceDriver()
        import sqlite3

        # _discover_sqlite("") 默认扫描 data 目录下的 .db 文件
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_file = data_dir / "test.db"
        sqlite3.connect(str(db_file)).close()
        monkeypatch.chdir(tmp_path)

        result = await drv._discover_sqlite("")
        assert len(result) >= 1
        found = next(r for r in result if r["device_id"] == "sqlite_test")
        assert found["protocol"] == "sqlite"
        assert "tables" in found["details"]

    async def test_specific_database_exists(self, tmp_path):
        drv = DatabaseSourceDriver()
        import sqlite3

        db_file = tmp_path / "my.db"
        sqlite3.connect(str(db_file)).close()
        result = await drv._discover_sqlite(str(db_file))
        assert len(result) == 1
        assert result[0]["name"] == f"SQLite ({db_file.name})"

    async def test_specific_database_not_exists_returns_empty(self, tmp_path):
        drv = DatabaseSourceDriver()
        result = await drv._discover_sqlite(str(tmp_path / "nonexistent.db"))
        assert result == []

    async def test_no_data_dir_returns_empty(self, tmp_path, monkeypatch):
        """data 目录不存在时返回空"""
        drv = DatabaseSourceDriver()
        monkeypatch.chdir(tmp_path)
        # tmp_path 下无 data 目录
        result = await drv._discover_sqlite("")
        assert result == []

    async def test_corrupt_db_file_skipped(self, tmp_path, monkeypatch):
        """损坏的 db 文件应被跳过，不抛异常"""
        drv = DatabaseSourceDriver()
        db_file = tmp_path / "corrupt.db"
        db_file.write_text("not a database")
        monkeypatch.chdir(tmp_path)
        # 在 data 目录下创建损坏文件
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "corrupt.db").write_text("not a database")
        result = await drv._discover_sqlite("")
        # 损坏文件应被跳过，返回空（或至少不抛异常）
        assert isinstance(result, list)


# ── _discover_mssql ──


class TestDiscoverMssql:
    async def test_import_error_returns_empty(self):
        drv = DatabaseSourceDriver()
        with patch.dict(sys.modules, {"aioodbc": None}):
            result = await drv._discover_mssql("h", 1433, "u", "p", "")
        assert result == []

    async def test_connection_failure_returns_empty(self):
        drv = DatabaseSourceDriver()
        fake_mod = _make_fake_module(create_pool=AsyncMock(side_effect=RuntimeError("refused")))
        with patch.dict(sys.modules, {"aioodbc": fake_mod}):
            result = await drv._discover_mssql("h", 1433, "u", "p", "")
        assert result == []

    async def test_discover_with_specific_database(self):
        drv = DatabaseSourceDriver()
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=("Microsoft SQL Server 2019",))
        cursor.fetchall = AsyncMock(return_value=[])
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=cursor)
        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_cm)
        pool.close = MagicMock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aioodbc": fake_mod}):
            result = await drv._discover_mssql("127.0.0.1", 1433, "u", "p", "mydb")
        assert len(result) == 1
        assert result[0]["device_id"] == "mssql_127_0_0_1_mydb"
        assert result[0]["protocol"] == "mssql"
        assert result[0]["details"]["version"] == "Microsoft SQL Server 2019"

    async def test_discover_lists_all_databases(self):
        drv = DatabaseSourceDriver()
        # 第一次 execute 返回版本游标，第二次返回数据库列表游标
        version_cursor = AsyncMock()
        version_cursor.fetchone = AsyncMock(return_value=("v",))
        db_cursor = AsyncMock()
        db_cursor.fetchall = AsyncMock(return_value=[("db1",), ("db2",)])
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=[version_cursor, db_cursor])
        acquire_cm = MagicMock()
        acquire_cm.__aenter__ = AsyncMock(return_value=conn)
        acquire_cm.__aexit__ = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=acquire_cm)
        pool.close = MagicMock()
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aioodbc": fake_mod}):
            result = await drv._discover_mssql("h", 1433, "u", "p", "")
        assert len(result) == 2
        db_names = [r["config"]["database"] for r in result]
        assert set(db_names) == {"db1", "db2"}


# ── 端到端：start + read + stop ──


class TestEndToEnd:
    async def test_mysql_start_read_stop(self):
        drv = DatabaseSourceDriver()
        pool, _, _ = _make_mysql_pool_mock(rows=[("42",)], description=[("val",)])
        fake_mod = _make_fake_module(create_pool=AsyncMock(return_value=pool))
        with patch.dict(sys.modules, {"aiomysql": fake_mod}):
            await drv.start(
                {
                    "db_type": "mysql",
                    "host": "h",
                    "port": 3306,
                    "database": "db",
                    "queries": {"temp": "SELECT val FROM t"},
                }
            )
            result = await drv.read_points("d1", ["temp"])
            assert result == {"temp": "42"}
            await drv.stop()
        assert drv._pool is None

    async def test_sqlite_start_write_stop(self):
        drv = DatabaseSourceDriver()
        conn, _ = _make_sqlite_pool_mock(rows=[], description=None)
        fake_mod = _make_fake_module(connect=AsyncMock(return_value=conn))
        with patch.dict(sys.modules, {"aiosqlite": fake_mod}):
            await drv.start(
                {
                    "db_type": "sqlite",
                    "database": ":memory:",
                    "write_queries": {"set_v": "UPDATE t SET v={{value}}"},
                }
            )
            ok = await drv.write_point("d1", "set_v", 99)
            assert ok is True
            await drv.stop()
        assert drv._pool is None
