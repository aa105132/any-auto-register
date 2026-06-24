import sys, time, re
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
print("polling 7190 inbox for MixRoute OTP...", flush=True)
deadline = time.time() + 90
while time.time() < deadline:
    msgs = list(mailbox._fetch_recent_messages(access_token))
    for m in msgs:
        combined = str(m.get('subject','')) + '\n' + str(m.get('body_text','')) + '\n' + str(m.get('body_html',''))
        if 'mixroute' in combined.lower():
            codes = re.findall(r'(?<!\d)(\d{6})(?!\d)', combined)
            print(f"MIXROUTE MAIL uid={m.get('uid')} subj={m.get('subject')} codes={codes}", flush=True)
            if codes:
                print(f"OTP={codes[0]}", flush=True)
                sys.exit(0)
    time.sleep(3)
print("TIMEOUT no MixRoute mail found", flush=True)
