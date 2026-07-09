"""测试配置和fixture"""

import asyncio
import os
from typing import Any

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI

from edgelite.api.deps import get_current_user


@pytest.fixture(scope="session", autouse=True)
def _ota_simulation_env():
    """启用 OTA 模拟模式，让单元测试走模拟路径而非抛 NotImplementedError。

    R5-F-07 修复在 OTA _apply()/_rollback() 中添加了 EDGELITE_OTA_ALLOW_SIMULATION 守卫，
    防止生产环境静默虚假成功。测试环境需要显式启用模拟模式。
    """
    os.environ["EDGELITE_OTA_ALLOW_SIMULATION"] = "1"
    yield
    os.environ.pop("EDGELITE_OTA_ALLOW_SIMULATION", None)


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session(tmp_path):
    """测试用SQLAlchemy异步会话"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from edgelite.models.db import Base

    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    session = session_factory()

    yield session

    await session.close()
    await engine.dispose()


# ── Shared API test fixtures (P1-1) ──


def make_user(role: str = "admin", user_id: str = "test-user-id") -> dict[str, str]:
    """Build a mock authenticated user dict returned by get_current_user override."""
    return {
        "user_id": user_id,
        "username": "testadmin" if role == "admin" else f"test{role}",
        "role": role,
    }


def make_app(
    router: Any,
    role: str = "admin",
    services: dict[str, Any] | None = None,
    override_auth: bool = True,
) -> FastAPI:
    """Create a minimal FastAPI test app with one router and mocked app.state services.

    Args:
        router: APIRouter instance to include.
        role: role assigned to the overridden current user (admin/operator/viewer).
        services: mapping of app.state attribute name -> mock value (e.g. AsyncMock).
        override_auth: when True, get_current_user is overridden to return a fixed
            user dict. Set False to test 401/403 on endpoints that lack auth.
    """
    app = FastAPI()
    app.include_router(router)
    if override_auth:
        app.dependency_overrides[get_current_user] = lambda: make_user(role)
    if services:
        for key, val in services.items():
            setattr(app.state, key, val)
    return app


def make_mock_audit_service() -> AsyncMock:
    """Build an AsyncMock audit service with an async log() coroutine."""
    svc = AsyncMock()
    svc.log = AsyncMock(return_value=None)
    return svc


def make_mock_database() -> MagicMock:
    """Build a mock database object with get_session() async context manager + write_lock."""
    db = MagicMock()
    db.write_lock = MagicMock()

    class _SessionCM:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    db.get_session = MagicMock(return_value=_SessionCM())
    return db
