"""多后端数据库连接管理（SQLAlchemy 2.0 异步）"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text

from edgelite.config import get_config
from edgelite.models.db import Base
from edgelite.api.error_codes import DatabaseErrors

logger = logging.getLogger(__name__)

_BACKEND_DRIVERS = {
    "sqlite": "aiosqlite",
    "mysql": "aiomysql",
    "postgresql": "asyncpg",
    "mssql": "aioodbc",
}


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
        odbc_connect = (
            f"DRIVER={{ODBC Driver 18 for SQL Server}};"
            f"SERVER={host},{port};DATABASE={db};"
            f"UID={user};PWD={pwd};TrustServerCertificate=yes"
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

    def __init__(self, config: Any = None):
        self._config = config or get_config()
        self._backend = self._config.database.backend.lower()
        self._db_url = _build_database_url(self._config)
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._write_lock = asyncio.Lock()

    @property
    def backend(self) -> str:
        return self._backend

    @property
    def db_path(self) -> str:
        if self._backend == "sqlite":
            return str(Path(self._config.database.sqlite_path).resolve())
        return ""

    @property
    def audit_db_path(self) -> str:
        if self._backend == "sqlite":
            main_path = Path(self._config.database.sqlite_path).resolve()
            return str(main_path.parent / "audit.db")
        return ""

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError(DatabaseErrors.NOT_CONNECTED)
        return self._engine

    @property
    def write_lock(self) -> asyncio.Lock:
        return self._write_lock

    async def connect(self) -> AsyncEngine:
        """建立数据库连接池"""
        _check_driver(self._backend)

        engine_kwargs: dict[str, Any] = {
            "echo": self._config.database.echo,
        }

        if self._backend == "sqlite":
            engine_kwargs["connect_args"] = {"check_same_thread": False}
        else:
            engine_kwargs["pool_size"] = self._config.database.pool_size
            engine_kwargs["max_overflow"] = self._config.database.max_overflow
            engine_kwargs["pool_pre_ping"] = True

        self._engine = create_async_engine(self._db_url, **engine_kwargs)
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

        if self._backend == "sqlite":
            await self._check_sqlite_integrity()

        logger.info("数据库连接已建立 (backend=%s)", self._backend)
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
        except Exception as e:
            logger.warning("SQLite完整性检查失败: %s", e)

    async def _rebuild_sqlite_database(self) -> None:  # FIXED: 原问题-数据库损坏后仅日志告警不重建，系统带损坏数据库继续运行导致全量数据丢失
        db_path = self.db_path
        if not db_path or not Path(db_path).exists():
            return
        try:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
        except Exception:
            pass

        import shutil
        corrupt_backup = f"{db_path}.corrupt.{int(time.time())}"
        try:
            shutil.move(db_path, corrupt_backup)
            logger.warning("损坏数据库已移至: %s", corrupt_backup)
        except Exception as e:
            logger.error("移动损坏数据库失败: %s，尝试删除重建", e)
            try:
                Path(db_path).unlink(missing_ok=True)
            except Exception:
                pass

        for suffix in ("-wal", "-shm"):
            try:
                Path(f"{db_path}{suffix}").unlink(missing_ok=True)
            except Exception:
                pass

        self._engine = create_async_engine(
            self._db_url,
            echo=self._config.database.echo,
            connect_args={"check_same_thread": False},
        )
        self._session_factory = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._migrate_sqlite(conn)

        logger.info("SQLite数据库已自动重建")

    async def init_tables(self) -> None:
        """初始化所有表（仅用于开发/首次部署，生产环境请使用 Alembic）"""
        if self._engine is None:
            raise RuntimeError(DatabaseErrors.NOT_CONNECTED)
        if self._session_factory is None:
            raise RuntimeError(DatabaseErrors.SESSION_NOT_INIT)

        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if self._backend == "sqlite":
                await self._migrate_sqlite(conn)

        async with self._session_factory() as session:
            from sqlalchemy import select

            from edgelite.models.db import UserORM

            # FIXED: 原问题-init_tables中admin用户查询无try-except保护，数据库连接异常导致初始化失败
            try:
                result = await session.execute(select(UserORM).where(UserORM.username == "admin"))
                admin_exists = result.scalar_one_or_none() is not None
            except Exception as e:
                logger.error("Database.init_tables admin check failed: %s", e)
                admin_exists = False

            if not admin_exists:
                try:
                    from edgelite.security.password import hash_password

                    # FIXED: 原问题-默认密码admin123硬编码在代码中，安全风险极高
                    temp_password = os.environ.get("EDGELITE_ADMIN_PASSWORD")
                    if not temp_password:
                        logger.error(
                            "EDGELITE_ADMIN_PASSWORD environment variable not set, "
                            "admin user creation skipped"
                        )
                        return
                    hashed = hash_password(temp_password)
                    admin = UserORM(
                        user_id="admin",
                        username="admin",
                        password=hashed,
                        role="admin",
                        enabled=True,
                        must_change_password=True,
                    )
                    session.add(admin)
                    await session.commit()
                    logger.warning(
                        "已创建默认管理员用户 (admin)，请立即登录修改密码！",
                    )
                except ImportError:
                    logger.warning("hash_password module not available, admin user creation skipped")
                except Exception as e:
                    logger.error("Database.init_tables admin creation failed: %s", e)

    async def _migrate_sqlite(self, conn: Any) -> None:
        """SQLite 自动迁移：为已有表添加缺失的列"""
        from sqlalchemy import text

        migrations = [
            ("users", "must_change_password", "BOOLEAN NOT NULL DEFAULT 0"),
            ("users", "updated_at", "DATETIME DEFAULT NULL"),
            ("alarms", "message", "VARCHAR(256) NOT NULL DEFAULT ''"),
        ]
        for table, column, definition in migrations:
            try:
                result = await conn.execute(text(f"PRAGMA table_info({table})"))
                columns = {row[1] for row in result.fetchall()}
                if column not in columns:
                    await conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                    )
                    logger.info("数据库迁移: %s.%s 列已添加", table, column)
            except Exception as e:
                logger.warning("数据库迁移 %s.%s 跳过: %s", table, column, e)

    def get_session(self) -> AsyncSession:
        """获取新的数据库会话"""
        if self._session_factory is None:
            raise RuntimeError(DatabaseErrors.NOT_CONNECTED)
        return self._session_factory()

    async def close(self) -> None:
        """关闭数据库连接池"""
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("数据库连接已关闭")

    async def backup(self, backup_path: str) -> None:
        """备份数据库"""
        import shutil

        Path(backup_path).parent.mkdir(parents=True, exist_ok=True)

        if self._backend == "sqlite":
            if self._engine:
                try:
                    async with self._engine.begin() as conn:
                        await conn.execute(text("PRAGMA wal_checkpoint=TRUNCATE"))
                except Exception as e:
                    logger.warning("WAL checkpoint TRUNCATE失败: %s，尝试PASSIVE模式", e)
                    try:
                        async with self._engine.begin() as conn:
                            await conn.execute(text("PRAGMA wal_checkpoint=PASSIVE"))
                    except Exception as e2:
                        logger.warning("WAL checkpoint PASSIVE也失败: %s，直接复制文件", e2)
            shutil.copy2(self._config.database.sqlite_path, backup_path)
            wal_path = self._config.database.sqlite_path + "-wal"
            if Path(wal_path).exists():
                try:
                    shutil.copy2(wal_path, backup_path + "-wal")
                except Exception as e:
                    logger.warning("WAL文件备份失败: %s", e)
        else:
            logger.warning(
                "备份功能暂不支持 %s 后端，请使用数据库自带的备份工具（如 pg_dump/mysqldump）",
                self._backend,
            )
