"""Check EdgeLite users table schema."""
import sqlite3

conn = sqlite3.connect('data/edgelite.db')
c = conn.cursor()

# Get table schema
c.execute("PRAGMA table_info(users)")
columns = c.fetchall()
print("Users table columns:")
for col in columns:
    print(f"  {col}")

# Get admin user data
c.execute("SELECT * FROM users WHERE username = 'admin'")
row = c.fetchone()
print(f"\nAdmin user data: {row}")

conn.close()
