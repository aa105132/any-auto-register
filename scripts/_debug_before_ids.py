"""Debug: check before_ids + wait_for_code behavior for outlook mailbox 7189."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import init_db
from core.registry import load_all
from core.base_mailbox import create_mailbox, MailboxAccount
from application.mailbox_inventory_support import build_mailbox_inventory_seed
from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository

init_db()
load_all()
repo = MailboxInventoryRepository()
items = repo.list_by_provider('outlook_token', status='unused')
item = next((x for x in items if int(x.get('id') or 0) == 7189), None)
print(f"item: id={item.get('id')} email={item.get('email')}", flush=True)

seed = build_mailbox_inventory_seed('outlook_token', item)
extra = dict(seed.extra or {})
extra.update({
    'identity_provider': 'mailbox',
    'mail_provider': 'outlook_token',
    'platform': 'debug',
    '_inventory': {'id': 7189, 'provider_key': 'outlook_token'},
})
mailbox = create_mailbox('outlook_token', extra=extra, proxy=None)
mail_acct = mailbox.get_email()
print(f"mail_acct email={mail_acct.email} account_id={mail_acct.account_id}", flush=True)

# Get current IDs
t0 = time.time()
current_ids = mailbox.get_current_ids(mail_acct)
print(f"get_current_ids took {time.time()-t0:.1f}s, count={len(current_ids)}", flush=True)
print(f"sample ids: {list(current_ids)[:5]}", flush=True)

# Fetch recent messages directly
print("\n=== _fetch_recent_messages ===", flush=True)
try:
    access_token = mailbox._refresh_access_token(mail_acct)
    msgs = list(mailbox._fetch_recent_messages(access_token))
    print(f"total messages: {len(msgs)}", flush=True)
    import re
    for m in msgs[:10]:
        uid = m.get('uid', '')
        subj = str(m.get('subject') or '')[:50]
        body = str(m.get('body_text') or '')[:120]
        in_before = uid in current_ids
        # find 6-digit codes
        codes = re.findall(r'(?<!\d)(\d{6})(?!\d)', body)
        print(f"  uid={uid} in_before={in_before} subj={subj}", flush=True)
        if codes:
            print(f"    codes found: {codes}", flush=True)
        if 'mixroute' in body.lower() or 'mix route' in body.lower():
            print(f"    *** MIXROUTE MAIL ***", flush=True)
        print(f"    body={body[:80]}", flush=True)
except Exception as e:
    import traceback
    print(f"ERROR: {type(e).__name__}: {e}", flush=True)
    print(traceback.format_exc(), flush=True)
