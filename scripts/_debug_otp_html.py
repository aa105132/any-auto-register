import sys, re
from pathlib import Path
ROOT = Path('.').resolve()
sys.path.insert(0, str(ROOT))
from core.db import init_db
from core.registry import load_all
from core.base_mailbox import create_mailbox
from application.mailbox_inventory_support import build_mailbox_inventory_seed
from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository
init_db(); load_all()
repo = MailboxInventoryRepository()
items = repo.list_by_provider('outlook_token', status='unused')
item = next((x for x in items if int(x.get('id') or 0) == 7190), None)
seed = build_mailbox_inventory_seed('outlook_token', item)
extra = dict(seed.extra or {})
extra.update({'identity_provider': 'mailbox', 'mail_provider': 'outlook_token', 'platform': 'debug', '_inventory': {'id': 7190, 'provider_key': 'outlook_token'}})
mailbox = create_mailbox('outlook_token', extra=extra, proxy=None)
mail_acct = mailbox.get_email()
access_token = mailbox._refresh_access_token(mail_acct)
msgs = list(mailbox._fetch_recent_messages(access_token))
for m in msgs:
    combined = str(m.get('subject','')) + '\n' + str(m.get('body_text','')) + '\n' + str(m.get('body_html',''))
    if 'mixroute' in combined.lower():
        html = str(m.get('body_html',''))
        # find all 6-digit codes and their surrounding context
        for match in re.finditer(r'(\d{6})', html):
            start = max(0, match.start()-40)
            end = min(len(html), match.end()+40)
            print(f"CODE {match.group(1)} context: ...{html[start:end]}...", flush=True)
        # also look for "code" / "verification" label near a number
        print("\n--- looking for code label ---", flush=True)
        for match in re.finditer(r'(?:code|verification|otp|password)[^<]{0,60}(\d{6})', html, re.I):
            print(f"LABELED CODE: {match.group(1)} -> {match.group(0)[:80]}", flush=True)
        break
