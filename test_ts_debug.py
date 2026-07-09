"""Debug test for SQLite TS storage - identify 'database is locked' root cause."""
import asyncio
import os
import sys
import time

sys.path.insert(0, 'src')
os.environ.setdefault('EDGELITE_CONFIG_PATH', 'configs/config.example.yaml')


async def test_ts_debug():
    from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

    db_path = 'data/test_ts_debug.db'
    # Clean start
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    ts = SqliteTimeSeriesStorage(db_path)
    print('1. Starting TS storage...')
    await ts.start()
    print(f'2. Started. _db={ts._db}, _pending_writes={ts._pending_writes}, _flush_task={ts._flush_task}')

    # Add small delay to let _periodic_flush settle
    print('3. Sleeping 0.2s...')
    await asyncio.sleep(0.2)

    print('4. Writing point 1...')
    try:
        await ts.write_point('test', 'dev1', 'temp', 25.5)
        print('5. Write 1 OK')
    except Exception as e:
        print(f'5. Write 1 FAILED: {e}')
        print(f'   _db={ts._db}, _pending_writes={ts._pending_writes}')
        # Try without lock
        if ts._db:
            try:
                cursor = await ts._db.execute("SELECT count(*) FROM device_points")
                row = await cursor.fetchone()
                print(f'   Direct query: count={row[0]}')
            except Exception as qe:
                print(f'   Direct query also failed: {qe}')
        await ts.stop()
        return

    print('6. Writing point 2...')
    await ts.write_point('test', 'dev1', 'temp', 26.0)
    print('7. Write 2 OK')

    print('8. Writing point 3...')
    await ts.write_point('test', 'dev1', 'temp', 27.0)
    print('9. Write 3 OK')

    latest = await ts.query_latest('dev1', ['temp'])
    print(f'10. Latest: {latest}')

    await ts.stop()
    print('=== TS debug test PASSED ===')

    # Cleanup
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass


if __name__ == '__main__':
    asyncio.run(test_ts_debug())
