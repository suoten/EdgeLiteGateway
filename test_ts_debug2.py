"""Minimal reproduction of 'database is locked' in SqliteTimeSeriesStorage."""
import asyncio
import os
import sys

sys.path.insert(0, 'src')
os.environ.setdefault('EDGELITE_CONFIG_PATH', 'configs/config.example.yaml')


async def test_no_delay():
    """Test without any delay - should reproduce the error."""
    from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

    db_path = 'data/test_ts_nodebug.db'
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    ts = SqliteTimeSeriesStorage(db_path)
    await ts.start()
    # NO delay - write immediately
    await ts.write_point('test', 'dev1', 'temp', 25.5)
    print('No-delay test: PASSED')
    await ts.stop()
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass


async def test_sleep_zero():
    """Test with asyncio.sleep(0) - just yields to event loop."""
    from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

    db_path = 'data/test_ts_sleep0.db'
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    ts = SqliteTimeSeriesStorage(db_path)
    await ts.start()
    await asyncio.sleep(0)  # Yield to event loop
    await ts.write_point('test', 'dev1', 'temp', 25.5)
    print('Sleep(0) test: PASSED')
    await ts.stop()
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass


async def test_no_flush_task():
    """Test without the _periodic_flush task."""
    from edgelite.storage.sqlite_ts import SqliteTimeSeriesStorage

    db_path = 'data/test_ts_noflush.db'
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    ts = SqliteTimeSeriesStorage(db_path)
    await ts.start()
    # Cancel the flush task immediately
    if ts._flush_task and not ts._flush_task.done():
        ts._flush_task.cancel()
        try:
            await ts._flush_task
        except asyncio.CancelledError:
            pass
        ts._flush_task = None
    # Now try to write
    await ts.write_point('test', 'dev1', 'temp', 25.5)
    print('No-flush-task test: PASSED')
    await ts.stop()
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass


async def main():
    print('--- Test 1: No delay ---')
    try:
        await test_no_delay()
    except Exception as e:
        print(f'No-delay test: FAILED - {e}')

    print('--- Test 2: Sleep(0) ---')
    try:
        await test_sleep_zero()
    except Exception as e:
        print(f'Sleep(0) test: FAILED - {e}')

    print('--- Test 3: No flush task ---')
    try:
        await test_no_flush_task()
    except Exception as e:
        print(f'No-flush-task test: FAILED - {e}')


if __name__ == '__main__':
    asyncio.run(main())
