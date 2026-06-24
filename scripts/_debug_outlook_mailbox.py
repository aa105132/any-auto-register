"""Debug: inspect Outlook mailbox current IDs and recent messages for 7189."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.db import init_db
from core.registry import load_all
from core.base_mailbox import create_mailbox
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
    'platform': 'mixroute',
    'platform_name': 'mixroute',
    '_inventory': {'id': 7189, 'provider_key': 'outlook_token'},
})
mailbox = create_mailbox('outlook_token', extra=extra, proxy=None)
mail_acct = seed.mailbox_account
print(f"mail_acct email={mail_acct.email}", flush=True)

# Get current IDs (before any send)
current_ids = mailbox.get_current_ids(mail_acct)
print(f"current_ids ({len(current_ids)}): {list(current_ids)[:10]}", flush=True)

# Fetch recent messages to see what's in the inbox
print("\n=== Recent messages ===", flush=True)
try:
    access_token = mailbox._refresh_access_token(mail_acct)
    msgs = list(mailbox._fetch_recent_messages(access_token))
    print(f"total recent messages: {len(msgs)}", flush=True)
    for m in msgs[:8]:
        uid = m.get('uid', '')
        subj = str(m.get('subject') or m.get('Subject') or '')[:60]
        frm = str(m.get('from') or m.get('From') or '')[:40]
        date = str(m.get('date') or m.get('Date') or m.get('internal_date') or '')[:30]
        body_preview = str(m.get('body') or m.get('text') or '')[:80]
        print(f"  uid={uid} from={frm} date={date} subj={subj}", flush=True)
        print(f"    body={body_preview}", flush=True)
except Exception as e:
    import traceback
    print(f"ERROR: {type(e).__name__}: {e}", flush=True)
    print(traceback.format_exc(), flush=True)
