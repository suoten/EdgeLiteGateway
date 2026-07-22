"""Clear EdgeLite rate limiter state."""
import sqlite3
from pathlib import Path

db_path = Path("data/edgelite.db")
if not db_path.exists():
    print("Database not found")
    exit(1)

conn = sqlite3.connect(str(db_path))
c = conn.cursor()

# List all tables
c.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in c.fetchall()]
print(f"Tables: {tables}")

# Find and clear rate limit tables
for table in tables:
    if "rate" in table.lower() or "limit" in table.lower() or "attempt" in table.lower() or "lock" in table.lower():
        print(f"  Clearing table: {table}")
        c.execute(f"DELETE FROM {table}")

# Also check for login_attempts or similar
for table in tables:
    try:
        c.execute(f"SELECT COUNT(*) FROM {table}")
        count = c.fetchone()[0]
        if count > 0 and ("rate" in table.lower() or "attempt" in table.lower() or "lock" in table.lower()):
            print(f"  Table {table} has {count} rows, clearing...")
            c.execute(f"DELETE FROM {table}")
    except Exception:
        pass

conn.commit()
conn.close()
print("Done clearing rate limiter state")
