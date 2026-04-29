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

    def __init__(self):
        self._running = False
        self._pool = None
        self._config: dict = {}
        self._lock = asyncio.Lock()

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
            raise ImportError("aiomysql未安装，请执行: pip install aiomysql")

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

    async def _init_postgresql(self, config: dict) -> None:
        try:
            import asyncpg
        except ImportError:
            raise ImportError("asyncpg未安装，请执行: pip install asyncpg")

        from urllib.parse import quote_plus
        dsn = f"postgresql://{quote_plus(config.get('username', 'postgres'))}:{quote_plus(config.get('password', ''))}@{config.get('host', 'localhost')}:{config.get('port', 5432)}/{config.get('database', 'postgres')}"
        self._pool = await asyncpg.create_pool(dsn, min_size=1, max_size=int(config.get("pool_size", 5)))

    async def _init_sqlite(self, config: dict) -> None:
        import aiosqlite

        db_path = config.get("database", config.get("path", "data/source.db"))
        self._pool = await aiosqlite.connect(db_path)

    async def _init_mssql(self, config: dict) -> None:
        try:
            import aioodbc
        except ImportError:
            raise ImportError("aioodbc未安装，请执行: pip install aioodbc")

        conn_str = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={config.get('host', 'localhost')},{config.get('port', 1433)};"
            f"DATABASE={config.get('database', '')};"
            f"UID={config.get('username', 'sa')};"
            f"PWD={config.get('password', '')};"
            f"TrustServerCertificate=yes"
        )
        self._pool = await aioodbc.create_pool(dsn=conn_str, minsize=1, maxsize=int(config.get("pool_size", 5)))

    async def stop(self) -> None:
        self._running = False
        if self._pool:
            try:
                if hasattr(self._pool, "close"):
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

    async def _execute_query(self, sql: str, params: tuple | None = None) -> list[dict]:
        db_type = self._config.get("db_type", "mysql")

        if db_type in ("mysql", "mariadb"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(sql, params)
                    rows = await cur.fetchall()
                    columns = [desc[0] for desc in cur.description] if cur.description else []
                    return [dict(zip(columns, row)) for row in rows]

        elif db_type in ("postgresql", "postgres"):
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *(params or []))
                return [dict(row) for row in rows]

        elif db_type == "sqlite":
            cursor = await self._pool.execute(sql, params or ())
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return [dict(zip(columns, row)) for row in rows]

        elif db_type == "mssql":
            async with self._pool.acquire() as conn:
                cursor = await conn.execute(sql, params or ())
                rows = await cursor.fetchall()
                return [dict(row) for row in rows] if rows else []

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
                else:
                    sql = sql_template.replace("{{value}}", "?")
                await self._execute_query(sql, (value,))
            else:
                await self._execute_query(sql_template)
            return True
        except Exception as e:
            logger.error("数据库写入失败 %s: %s", point, e)
            return False
