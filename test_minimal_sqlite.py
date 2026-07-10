"""Minimal SQLite test to isolate 'database is locked' issue."""
import asyncio
import os

import aiosqlite


async def test_minimal():
    db_path = 'data/test_minimal.db'
    # Clean start
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    print(f'1. Connecting to {db_path}...')
    db = await aiosqlite.connect(db_path)
    print('2. Connected.')

    print('3. Setting PRAGMAs...')
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA busy_timeout=5000")
    print('4. PRAGMAs set.')

    print('5. Creating table...')
    await db.execute("CREATE TABLE IF NOT EXISTS test (id INTEGER PRIMARY KEY, value REAL)")
    await db.commit()
    print('6. Table created.')

    print('7. Inserting data...')
    await db.execute("INSERT INTO test (value) VALUES (1.0)")
    await db.commit()
    print('8. Data inserted.')

    print('9. Querying data...')
    cursor = await db.execute("SELECT * FROM test")
    rows = await cursor.fetchall()
    print(f'10. Query result: {rows}')

    await db.close()
    print('11. Connection closed.')

    # Cleanup
    for suffix in ['', '-wal', '-shm']:
        try:
            os.unlink(f'{db_path}{suffix}')
        except OSError:
            pass

    print('=== Minimal SQLite test PASSED ===')

if __name__ == '__main__':
    asyncio.run(test_minimal())
