"""Probe DB schema and inventory."""
import json
import sqlite3
from pathlib import Path

db = Path('account_manager.db')
con = sqlite3.connect(db)
print("=== provider_settings columns ===")
print([d[1] for d in con.execute("PRAGMA table_info(provider_settings)").fetchall()])
print("=== mailbox_inventory columns ===")
print([d[1] for d in con.execute("PRAGMA table_info(mailbox_inventory)").fetchall()])

print("\n=== provider_settings rows ===")
for row in con.execute("SELECT * FROM provider_settings LIMIT 30").fetchall():
    print(row)

print("\n=== mailbox_inventory count by provider/status ===")
for row in con.execute("SELECT provider_key, status, COUNT(*) FROM mailbox_inventory GROUP BY provider_key, status").fetchall():
    print(row)

print("\n=== mailbox_inventory sample (outlook_token, unused) ===")
for row in con.execute("SELECT id, provider_key, email, status FROM mailbox_inventory WHERE provider_key='outlook_token' AND status='unused' LIMIT 5").fetchall():
    print(row)
con.close()
