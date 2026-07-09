"""Smoke test for SQLite storage layer read/write operations."""
import sys
import asyncio
import os
import time

sys.path.insert(0, 'src')
os.environ.setdefault('EDGELITE_CONFIG_PATH', 'configs/config.example.yaml')


async def test_sqlite_ts_only():
    """Test SQLite time series storage independently."""
    from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

    db_path = 'data/test_ts_smoke.db'
    for suffix in ['', '-wal', '-shm']:
        f = f'{db_path}{suffix}'
        try:
            os.unlink(f)
        except OSError:
            pass

    ts = SqliteTimeSeriesStorage(db_path)
    await ts.start()

    ts1 = time.time_ns()
    ts2 = ts1 + 1_000_000_000
    ts3 = ts2 + 1_000_000_000
    await ts.write_point('test', 'dev1', 'temp', 25.5, timestamp_ns=ts1)
    await ts.write_point('test', 'dev1', 'temp', 26.0, timestamp_ns=ts2)
    await ts.write_point('test', 'dev1', 'temp', 27.0, timestamp_ns=ts3)

    latest = await ts.query_latest('dev1', ['temp'])
    assert 'temp' in latest, 'Should have latest temp'
    assert latest['temp']['value'] == 27.0, f'Latest should be 27.0, got {latest["temp"]["value"]}'
    print(f'TS latest query: OK (value={latest["temp"]["value"]})')

    stats = await ts.get_stats()
    print(f'TS stats: records={stats["total_records"]}')
    assert stats['total_records'] == 3, f'Should have 3 records, got {stats["total_records"]}'

    await ts.stop()

    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    print('=== SQLite TS smoke test PASSED ===')


async def test_main_db_only():
    """Test main database independently."""
    from edgelite.config import get_config
    from edgelite.storage.database import Database
    from edgelite.models.db import UserORM
    from sqlalchemy import select, text

    db_path = 'data/test_smoke.db'
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    config = get_config()
    config.database.sqlite_path = db_path
    config.database.backend = 'sqlite'

    db = Database(config)
    await db.connect()
    await db.init_tables()

    async with db.session() as session:
        result = await session.execute(select(UserORM).where(UserORM.username == 'admin'))
        admin = result.scalar_one_or_none()
        assert admin is not None, 'Admin user should exist'
        print(f'Admin user check: OK (role={admin.role})')

        result = await session.execute(text('SELECT count(*) FROM users'))
        print(f'User count: {result.scalar()}')

        result = await session.execute(text('SELECT count(*) FROM rules'))
        print(f'Rules count: {result.scalar()}')

        result = await session.execute(text('SELECT count(*) FROM alarms'))
        print(f'Alarms count: {result.scalar()}')

    await db.close()

    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    print('=== Main DB smoke test PASSED ===')


async def main():
    print('--- Test 1: SQLite TS only ---')
    await test_sqlite_ts_only()

    print('\n--- Test 2: Main DB only ---')
    await test_main_db_only()

    print('\n=== All storage smoke tests PASSED ===')


if __name__ == '__main__':
    asyncio.run(main())
