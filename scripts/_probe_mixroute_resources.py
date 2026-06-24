"""Probe available resources for mixroute registration test."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import init_db
from core.registry import load_all
from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository

init_db()
load_all()

# 1. Check outlook_token mailbox inventory
repo = MailboxInventoryRepository()
for provider in ('outlook_token',):
    try:
        available = repo.list_available(provider_key=provider, limit=5)
        print(f"[mailbox] {provider}: {len(available)} available")
        for item in available[:3]:
            print(f"  id={item.get('id')} email={item.get('email','')[:20]}... status={item.get('status')}")
    except Exception as e:
        print(f"[mailbox] {provider}: error {type(e).__name__}: {e}")

# 2. Check Chrome
from core.base_captcha import CdpTurnstileSolver
solver = CdpTurnstileSolver()
try:
    chrome = solver._resolve_chrome()
    print(f"[chrome] resolved: {chrome}")
except Exception as e:
    print(f"[chrome] NOT FOUND: {e}")

# 3. Check captcha provider settings (yescaptcha / cdp_turnstile)
import sqlite3
db = ROOT / 'account_manager.db'
if db.exists():
    con = sqlite3.connect(db)
    try:
        rows = con.execute("SELECT provider_type, driver_type, enabled, settings FROM provider_settings WHERE provider_type IN ('captcha','mailbox')").fetchall()
        for pt, dt, en, st in rows:
            s = json.loads(st) if st else {}
            # mask sensitive
            masked = {k: (v[:6]+'...' if isinstance(v,str) and len(v)>10 and any(x in k.lower() for x in ('key','token','secret','password','refresh')) else v) for k,v in s.items()}
            print(f"[provider] type={pt} driver={dt} enabled={en} settings={masked}")
    except Exception as e:
        print(f"[provider] error: {e}")
    finally:
        con.close()
else:
    print(f"[db] not found: {db}")

# 4. Check mixroute platform registration
from core.registry import get
cls = get('mixroute')
print(f"[platform] mixroute: executors={cls.supported_executors} mail={cls.default_mail_provider}")
