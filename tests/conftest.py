"""测试配置和fixture"""

import asyncio
import pytest
import pytest_asyncio
from pathlib import Path


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_connection(tmp_path):
    """测试用SQLite连接"""
    import aiosqlite
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    await conn.close()
