"""数据库接入驱动 - 支持将关系型数据库作为数据源

支持：
- MySQL / MariaDB
- PostgreSQL
- SQLite
- SQL Server (通过odbc)
- 自定义SQL查询作为测点
- 定时轮询查询
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from edgelite.drivers.base import DriverPlugin

logger = logging.getLogger(__name__)


class DatabaseSourceDriver(DriverPlugin):
    """数据库接入驱动

    配置参数:
        db_type: 数据库类型 mysql/postgresql/sqlite/mssql
        host: 数据库主机 (默认localhost)
        port: 数据库端口 (默认按类型)
        database: 数据库名
        username: 用户名
        password: 密码
        queries: 测点SQL映射 {point_name: sql_statement}
        pool_size: 连接池大小 (默认5)
    """

    plugin_name = "database_source"
    plugin_version = "1.0.0"
    supported_protocols = ["database", "mysql", "postgresql", "sqlite", "mssql"]
    config_schema = {
        "description": "Database integration, maps database table fields to data points via SQL queries",  # FIXED: 原问题-中文硬编码description
        "fields": [
            {
                "name": "db_type",
                "type": "string",
                "label": "Database Type",
                "description": "Target database type",
                "default": "mysql",
                "required": True,
                "options": ["mysql", "postgresql", "sqlite", "mssql"],
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "host",
                "type": "string",
                "label": "Host",
                "description": "Database server IP or hostname",
                "default": "localhost",
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "port",
                "type": "integer",
                "label": "Port",
                "description": "Database port, MySQL default 3306, PostgreSQL default 5432",
                "default": 3306,
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "database",
                "type": "string",
                "label": "Database",
                "description": "Database name to connect to",
                "required": True,
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "username",
                "type": "string",
                "label": "Username",
                "description": "Database login username",
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "password",
                "type": "string",
                "label": "Password",
                "description": "Database login password",
                "secret": True,
            },  # FIXED: 原问题-中文硬编码label/description
            {
                "name": "pool_size",
                "type": "integer",
                "label": "Pool Size",
                "description": "Maximum connection pool size",
                "default": 5,
            },  # FIXED: 原问题-中文硬编码label/description
        ],
    }

    def __init__(self):
        self._running = False
        self._pool = None
        self._config: dict = {}
        self._lock = asyncio.Lock()
        self._devices: dict[str, dict] = {}

    async def start(self, config: dict) -> None:
        self._config = config
        db_type = config.get("db_type", config.get("protocol", "mysql"))

        try:
            if db_type in ("mysql", "mariadb"):
                await self._init_mysql(config)
            elif db_type in ("postgresql", "postgres"):
                await self._init_postgresql(config)
            elif db_type == "sqlite":
                await self._init_sqlite(config)
            elif db_type == "mssql":
                await self._init_mssql(config)
            else:
                raise ValueError(f"不支持的数据库类型: {db_type}")

            self._running = True
            logger.info("数据库接入驱动启动成功 (type=%s)", db_type)
        except Exception as e:
            logger.error("数据库接入驱动启动失败: %s", e)
            raise

    async def _init_mysql(self, config: dict) -> None:
        try:
            import aiomysql
        except ImportError:
            raise ImportError("aiomysql未安装，请执行: pip install aiomysql") from None

        # FIXED: 原问题-_init_mysql连接池创建无try-except保护
        try:
            self._pool = await aiomysql.create_pool(
                host=config.get("host", "localhost"),
                port=int(config.get("port", 3306)),
                db=config.get("database", ""),
                user=config.get("username", "root"),
                password=config.get("password", ""),
                minsize=1,
                maxsize=int(config.get("pool_size", 5)),
                autocommit=True,
            )
        except Exception as e:
            logger.error("DatabaseSource._init_mysql connection failed: %s", e)
            raise

    async def _init_postgresql(self, config: dict) -> None:
        try:
            import asyncpg
        except ImportError:
            raise ImportError("asyncpg未安装，请执行: pip install asyncpg") from None

        # FIXED-P0: 不再将密码嵌入DSN URL，改为通过password参数单独传递，避免凭据泄露到日志
        try:
            self._pool = await asyncpg.create_pool(
                host=config.get("host", "localhost"),
                port=int(config.get("port", 5432)),
                database=config.get("database", "postgres"),
                user=config.get("username", "postgres"),
                password=config.get("password", ""),
                min_size=1,
                max_size=int(config.get("pool_size", 5)),
            )
        except Exception as e:
            logger.error("DatabaseSource._init_postgresql connection failed: %s", e)
            raise

    async def _init_sqlite(self, config: dict) -> None:
        import aiosqlite

        db_path = config.get("database", config.get("path", "data/source.db"))
        # FIXED: 原问题-_init_sqlite连接创建无try-except保护
        try:
            self._pool = await aiosqlite.connect(db_path)
        except Exception as e:
            logger.error("DatabaseSource._init_sqlite connection failed: %s", e)
            raise

    async def _init_mssql(self, config: dict) -> None:
        try:
            import aioodbc
        except ImportError:
            raise ImportError("aioodbc未安装，请执行: pip install aioodbc") from None

        # FIXED-P0: 连接字符串中的密码通过参数化方式构建，避免凭据硬编码
        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={config.get('host', 'localhost')},{config.get('port', 1433)};"
            f"DATABASE={config.get('database', '')};"
            f"UID={config.get('username', 'sa')};"
            f"PWD={config.get('password', '')};"
            f"TrustServerCertificate=yes"
        )
        # 注意：连接字符串包含凭据，切勿记录到日志中
        # FIXED: 原问题-_init_mssql连接池创建无try-except保护
        try:
            self._pool = await aioodbc.create_pool(dsn=conn_str, minsize=1, maxsize=int(config.get("pool_size", 5)))
        except Exception as e:
            logger.error("DatabaseSource._init_mssql connection failed: %s", e)
            raise

    async def stop(self) -> None:
        self._running = False
        if self._pool:
            try:
                db_type = self._config.get("db_type", "mysql")
                if db_type == "sqlite" or hasattr(self._pool, "close"):
                    await self._pool.close()
                elif hasattr(self._pool, "terminate"):
                    self._pool.terminate()
            except Exception as e:
                logger.warning("数据库连接池关闭异常: %s", e)
            self._pool = None
        logger.info("数据库接入驱动已停止")

    async def read_points(self, device_id: str, points: list[str]) -> dict[str, Any]:
        if not self._running or not self._pool:
            return {}

        queries = self._config.get("queries", {})
        result = {}

        for point in points:
            sql = queries.get(point, "")
            if not sql:
                result[point] = None
                continue

            try:
                rows = await self._execute_query(sql)
                if rows:
                    if len(rows) == 1 and len(rows[0]) == 1:
                        result[point] = list(rows[0].values())[0] if isinstance(rows[0], dict) else rows[0][0]
                    elif len(rows) == 1:
                        result[point] = rows[0]
                    else:
                        result[point] = rows
                else:
                    result[point] = None
            except Exception as e:
                logger.warning("数据库查询失败 %s: %s", point, e)
                result[point] = None

        return result

    # FIXED: P0-10 原问题-正则仅检测分号开头的危险语句，可被SELECT OR/AND/UNION注入绕过。
    # 扩展检测模式以覆盖常见SQL注入向量。
    _SQL_DANGEROUS_PATTERNS = re.compile(
        r"(\bOR\b.*\bOR\b|\bAND\b.*\bAND\b|\bOR\b\s*\d+\s*=\s*\d+|\bAND\b\s*\d+\s*=\s*\d+"
        r"|\bUNION\b.*\bSELECT\b|\bUNION\b\s+ALL|\bEXEC\b|\bEXECUTE\b|\bxp_)"
        r"|;\s*(DROP|ALTER|CREATE|TRUNCATE|DELETE|INSERT|UPDATE|GRANT|REVOKE)\b",
        re.IGNORECASE,
    )

    def _validate_sql(self, sql: str) -> None:
        if self._SQL_DANGEROUS_PATTERNS.search(sql):
            raise ValueError("SQL contains potentially dangerous statement")

    async def _execute_query(self, sql: str, params: tuple | None = None) -> list[dict]:
        # FIXED: 原问题-SQL查询直接执行无注入防护，现执行前先验证
        self._validate_sql(sql)
        db_type = self._config.get("db_type", "mysql")

        if db_type in ("mysql", "mariadb"):
            async with self._pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
                columns = [desc[0] for desc in cur.description] if cur.description else []
                return [dict(zip(columns, row, strict=False)) for row in rows]

        elif db_type in ("postgresql", "postgres"):
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *(params or []))
                return [dict(row) for row in rows]

        elif db_type == "sqlite":
            cursor = await self._pool.execute(sql, params or ())
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row, strict=False)) for row in rows]

        elif db_type == "mssql":
            async with self._pool.acquire() as conn:
                cursor = await conn.execute(sql, params or ())
                rows = await cursor.fetchall()
                return [dict(row) for row in rows] if rows else []

        return []

    async def add_device(self, device_id: str, config: dict, points: list[dict] | None = None) -> None:
        """添加数据库设备，保存配置和查询映射"""
        if points is None:
            points = []
        self._devices[device_id] = {
            "config": config,
            "points": {p.get("name", p.get("address", "")): p for p in points if p.get("name") or p.get("address")},
        }
        # 合并新设备的SQL查询到驱动配置中
        queries = dict(self._config.get("queries", {}))
        for pt in points:
            name = pt.get("name", "")
            sql = pt.get("address", pt.get("sql", pt.get("query", "")))
            if name and sql:
                queries[name] = sql
        if queries:
            self._config["queries"] = queries
        logger.info("数据库设备已添加: %s (%d测点)", device_id, len(points))

    async def discover_devices(self, config: dict) -> list[dict]:
        """通过连接测试发现可用的数据库

        config参数:
            db_type: 数据库类型 mysql/postgresql/sqlite/mssql
            host: 数据库主机 (默认localhost)
            port: 数据库端口 (默认按类型)
            database: 数据库名 (可选，不指定则列出所有可访问的库)
            username: 用户名
            password: 密码
        对于SQLite: database参数为文件路径，测试文件是否存在及可读
        对于MySQL/PostgreSQL/MSSQL: 测试连接是否成功，成功则返回该数据库信息
        """
        db_type = config.get("db_type", config.get("protocol", "mysql"))
        host = config.get("host", "localhost")
        port = int(config.get("port", 0))
        database = config.get("database", "")
        username = config.get("username", "")
        password = config.get("password", "")

        discovered = []

        try:
            if db_type in ("mysql", "mariadb"):
                discovered = await self._discover_mysql(host, port or 3306, username, password, database)
            elif db_type in ("postgresql", "postgres"):
                discovered = await self._discover_postgresql(host, port or 5432, username, password, database)
            elif db_type == "sqlite":
                discovered = await self._discover_sqlite(database)
            elif db_type == "mssql":
                discovered = await self._discover_mssql(host, port or 1433, username, password, database)
            else:
                logger.warning("数据库发现: 不支持的类型 %s", db_type)
        except Exception as e:
            logger.error("数据库发现失败 (%s): %s", db_type, e)

        return discovered

    async def _discover_mysql(self, host: str, port: int, username: str, password: str, database: str) -> list[dict]:
        """发现MySQL/MariaDB数据库"""
        try:
            import aiomysql
        except ImportError:
            logger.warning("aiomysql未安装，无法发现MySQL数据库")
            return []

        try:
            async with await aiomysql.create_pool(
                host=host,
                port=port,
                user=username,
                password=password,
                db=database or "information_schema",
                minsize=1,
                maxsize=1,
                autocommit=True,
            ) as pool:
                async with pool.acquire() as conn, conn.cursor() as cur:
                    # 获取服务器版本
                    await cur.execute("SELECT VERSION()")
                    row = await cur.fetchone()
                    version = row[0] if row else ""

                    # 获取可访问的数据库列表
                    if not database:
                        await cur.execute("SHOW DATABASES")
                        databases = [
                            r[0]
                            for r in await cur.fetchall()
                            if r[0] not in ("information_schema", "mysql", "performance_schema", "sys")
                        ]
                    else:
                        databases = [database]

            return [
                {
                    "device_id": f"mysql_{host.replace('.', '_')}_{db}",
                    "name": f"MySQL ({host}:{port}/{db})" + (f" - {version}" if version else ""),
                    "protocol": "mysql",
                    "config": {
                        "db_type": "mysql",
                        "host": host,
                        "port": port,
                        "database": db,
                    },
                    "points": [],
                    "details": {
                        "version": version,
                    },
                }
                for db in databases
            ]
        except Exception as e:
            logger.debug("MySQL发现: 连接 %s:%d 失败 - %s", host, port, e)
            return []

    async def _discover_postgresql(
        self, host: str, port: int, username: str, password: str, database: str
    ) -> list[dict]:
        """发现PostgreSQL数据库"""
        try:
            import asyncpg
        except ImportError:
            logger.warning("asyncpg未安装，无法发现PostgreSQL数据库")
            return []

        # FIXED-P0: 不再将密码嵌入DSN URL，改为通过password参数单独传递
        try:
            conn = await asyncpg.connect(
                host=host,
                port=port,
                database=database or "postgres",
                user=username,
                password=password,
            )
            try:
                version = await conn.fetchval("SELECT version()")

                if not database:
                    rows = await conn.fetch("SELECT datname FROM pg_database WHERE datistemplate = false")
                    databases = [r["datname"] for r in rows]
                else:
                    databases = [database]
            finally:
                await conn.close()

            return [
                {
                    "device_id": f"pg_{host.replace('.', '_')}_{db}",
                    "name": f"PostgreSQL ({host}:{port}/{db})",
                    "protocol": "postgresql",
                    "config": {
                        "db_type": "postgresql",
                        "host": host,
                        "port": port,
                        "database": db,
                    },
                    "points": [],
                    "details": {
                        "version": version or "",
                    },
                }
                for db in databases
            ]
        except Exception as e:
            logger.debug("PostgreSQL发现: 连接 %s:%d 失败 - %s", host, port, e)
            return []

    async def _discover_sqlite(self, database: str) -> list[dict]:
        """发现SQLite数据库文件"""
        from pathlib import Path

        if not database:
            # 默认扫描data目录下的.db文件
            data_dir = Path("data")
            if not data_dir.exists():
                return []
            db_files = list(data_dir.glob("*.db"))
        else:
            p = Path(database)
            if p.exists():
                db_files = [p]
            else:
                return []

        discovered = []
        for db_file in db_files:
            try:
                import aiosqlite

                async with aiosqlite.connect(str(db_file)) as conn:
                    cursor = await conn.execute("SELECT sqlite_version()")
                    row = await cursor.fetchone()
                    version = row[0] if row else ""
                    tables = []
                    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = [r[0] for r in await cursor.fetchall()]

                discovered.append(
                    {
                        "device_id": f"sqlite_{db_file.stem}",
                        "name": f"SQLite ({db_file.name})",
                        "protocol": "sqlite",
                        "config": {
                            "db_type": "sqlite",
                            "database": str(db_file),
                        },
                        "points": [],
                        "details": {
                            "version": version,
                            "tables": tables,
                        },
                    }
                )
            except Exception as e:
                logger.debug("SQLite发现: %s 失败 - %s", db_file, e)

        return discovered

    async def _discover_mssql(self, host: str, port: int, username: str, password: str, database: str) -> list[dict]:
        """发现SQL Server数据库"""
        try:
            import aioodbc
        except ImportError:
            logger.warning("aioodbc未安装，无法发现MSSQL数据库")
            return []

        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={host},{port};"
            f"DATABASE={database or 'master'};"
            f"UID={username};"
            f"PWD={password};"
            f"TrustServerCertificate=yes"
        )

        try:
            pool = await aioodbc.create_pool(dsn=conn_str, minsize=1, maxsize=1)
            try:
                async with pool.acquire() as conn:
                    cursor = await conn.execute("SELECT @@VERSION")
                    row = await cursor.fetchone()
                    version = row[0] if row else ""

                    if not database:
                        cursor = await conn.execute("SELECT name FROM sys.databases WHERE database_id > 4")
                        databases = [r[0] for r in await cursor.fetchall()]
                    else:
                        databases = [database]
            finally:
                pool.close()

            return [
                {
                    "device_id": f"mssql_{host.replace('.', '_')}_{db}",
                    "name": f"SQL Server ({host}:{port}/{db})",
                    "protocol": "mssql",
                    "config": {
                        "db_type": "mssql",
                        "host": host,
                        "port": port,
                        "database": db,
                    },
                    "points": [],
                    "details": {
                        "version": version or "",
                    },
                }
                for db in databases
            ]
        except Exception as e:
            logger.debug("MSSQL发现: 连接 %s:%d 失败 - %s", host, port, e)
            return []

    async def write_point(self, device_id: str, point: str, value: Any) -> bool:
        if not self._running or not self._pool:
            return False

        queries = self._config.get("write_queries", {})
        sql_template = queries.get(point, "")

        if not sql_template:
            return False

        try:
            if "{{value}}" in sql_template:
                db_type = self._config.get("db_type", "mysql")
                if db_type == "postgresql":
                    sql = sql_template.replace("{{value}}", "$1")
                elif db_type in ("mysql", "mariadb"):
                    sql = sql_template.replace("{{value}}", "%s")
                else:
                    sql = sql_template.replace("{{value}}", "?")
                if not isinstance(value, (int, float, bool)):
                    value = str(value)
                await self._execute_query(sql, (value,))
            else:
                logger.warning("写入查询未使用参数化占位符: %s", point)
                return False
            return True
        except Exception as e:
            logger.error("数据库写入失败 %s: %s", point, e)
            return False

    def remove_device(self, device_id: str) -> None:
        """Remove a device at runtime"""
        self._health_stats.pop(device_id, None)
        self._offline_since.pop(device_id, None)
        logger.info("Database source device removed: %s", device_id)
