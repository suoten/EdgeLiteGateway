import sqlite3
conn = sqlite3.connect('data/edgelite.db')
c = conn.cursor()
c.execute("SELECT username, must_change_password FROM users WHERE username='admin'")
row = c.fetchone()
print(f"Admin user: {row}")
conn.close()
