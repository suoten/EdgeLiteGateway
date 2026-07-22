"""Reset EdgeLite admin password directly in SQLite DB."""
import sqlite3
import bcrypt

# New password to set
NEW_PASSWORD = "EdgeLite@2026"

# Hash the password using bcrypt (EdgeLite uses bcrypt with rounds=12)
hashed = bcrypt.hashpw(NEW_PASSWORD.encode('utf-8'), bcrypt.gensalt(rounds=12)).decode('utf-8')

conn = sqlite3.connect('data/edgelite.db')
c = conn.cursor()

# Update admin password
c.execute("UPDATE users SET password = ?, must_change_password = 0 WHERE username = 'admin'", (hashed,))
conn.commit()

# Verify
c.execute("SELECT username, must_change_password FROM users WHERE username = 'admin'")
row = c.fetchone()
print(f"Admin user updated: {row}")
print(f"New password set to: {NEW_PASSWORD}")
conn.close()
