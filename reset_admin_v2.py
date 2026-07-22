"""Reset EdgeLite admin password with fresh bcrypt hash."""
import sqlite3
from pathlib import Path
import bcrypt

db_path = Path("data/edgelite.db")
if not db_path.exists():
    print("Database not found")
    exit(1)

# Generate fresh bcrypt hash for EdgeLite@2026
password = "EdgeLite@2026"
hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
print(f"Generated hash: {hashed}")

conn = sqlite3.connect(str(db_path))
c = conn.cursor()

# Check current admin record
c.execute("SELECT user_id, username, password, must_change_password FROM users WHERE username = 'admin'")
row = c.fetchone()
print(f"Current admin: {row}")

# Update password
c.execute(
    "UPDATE users SET password = ?, must_change_password = 0 WHERE username = 'admin'",
    (hashed,),
)
conn.commit()

# Verify
c.execute("SELECT password, must_change_password FROM users WHERE username = 'admin'")
updated = c.fetchone()
print(f"Updated: must_change_password={updated[1]}")

# Verify hash matches
if bcrypt.checkpw(password.encode(), updated[0].encode()):
    print(f"Password verification: OK (matches '{password}')")
else:
    print("Password verification: FAILED")

conn.close()
