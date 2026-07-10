"""Alembic environment configuration.

Supports SQLite, MySQL, PostgreSQL, and MSSQL backends.
The database URL is loaded from the application config.
"""

from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Engine

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import target metadata from the ORM models
from edgelite.models.db import Base

target_metadata = Base.metadata


def get_database_url() -> str:
    """Build database URL from application config based on backend type."""
    # Check environment variable first (set by database.py during migration)
    import os as _os
    env_url = _os.environ.get("ALEMBIC_DATABASE_URL")
    if env_url:
        return env_url

    try:
        from urllib.parse import quote_plus

        from edgelite.config import get_config

        app_config = get_config()
        backend = app_config.database.backend.lower()

        if backend == "sqlite":
            db_path = Path(app_config.database.sqlite_path).resolve()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            return f"sqlite+aiosqlite:///{db_path}"

        elif backend in ("mysql", "mariadb"):
            host = app_config.database.host
            port = app_config.database.port or 3306
            user = quote_plus(app_config.database.username)
            pwd = quote_plus(app_config.database.password)
            db = app_config.database.database
            return f"mysql+aiomysql://{user}:{pwd}@{host}:{port}/{db}?charset=utf8mb4"

        elif backend in ("postgresql", "postgres"):
            host = app_config.database.host
            port = app_config.database.port or 5432
            user = quote_plus(app_config.database.username)
            pwd = quote_plus(app_config.database.password)
            db = app_config.database.database
            return f"postgresql+asyncpg://{user}:{pwd}@{host}:{port}/{db}"

        elif backend == "mssql":
            host = app_config.database.host
            port = app_config.database.port or 1433
            user = quote_plus(app_config.database.username)
            pwd = quote_plus(app_config.database.password)
            db = app_config.database.database
            trust_cert = "yes" if getattr(app_config.database, "trust_server_certificate", False) else "no"
            odbc_connect = (
                f"DRIVER={{ODBC Driver 18 for SQL Server}};"
                f"SERVER={host},{port};DATABASE={db};"
                f"UID={user};PWD={pwd};TrustServerCertificate={trust_cert}"  # FIXED-P2: 从配置读取，与database.py一致
            )
            return f"mssql+aioodbc:///?odbc_connect={quote_plus(odbc_connect)}"

        else:
            raise ValueError(f"Unsupported backend: {backend}")

    except Exception as e:
        # R7-S-13: 原代码静默回退到 SQLite，可能导致迁移写入错误的数据库。
        # 配置加载失败时应直接终止迁移，避免在非预期数据库上执行迁移。
        raise RuntimeError(
            f"Failed to load database configuration for Alembic migration: {e}. "
            f"Aborting to prevent migration against an unintended database. "
            f"Please verify EDGELITE_DATABASE__BACKEND and related settings."
        ) from e


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL and not an Engine,
    though an Engine is acceptable here as well. By skipping the Engine
    creation we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url") or get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine and associate a
    connection with the context.

    Uses synchronous engine for migration compatibility.
    """
    url = config.get_main_option("sqlalchemy.url") or get_database_url()

    # Convert async driver URLs to sync driver URLs for Alembic
    sync_url = url
    if "sqlite+aiosqlite:///" in sync_url:
        sync_url = sync_url.replace("sqlite+aiosqlite:///", "sqlite:///")
    elif "mysql+aiomysql://" in sync_url:
        sync_url = sync_url.replace("mysql+aiomysql://", "mysql+pymysql://")
    elif "postgresql+asyncpg://" in sync_url:
        sync_url = sync_url.replace("postgresql+asyncpg://", "postgresql://")
    elif "mssql+aioodbc://" in sync_url:
        # For MSSQL, we still need aioodbc but we can use the sync wrapper
        sync_url = sync_url.replace("mssql+aioodbc://", "mssql://")

    connectable: Engine = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=sync_url,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
