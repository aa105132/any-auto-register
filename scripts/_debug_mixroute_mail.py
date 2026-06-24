import sys
from pathlib import Path
ROOT = Path('.').resolve()
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
seed = build_mailbox_inventory_seed('outlook_token', item)
extra = dict(seed.extra or {})
extra.update({'identity_provider': 'mailbox', 'mail_provider': 'outlook_token', 'platform': 'debug', '_inventory': {'id': 7189, 'provider_key': 'outlook_token'}})
mailbox = create_mailbox('outlook_token', extra=extra, proxy=None)
mail_acct = mailbox.get_email()
access_token = mailbox._refresh_access_token(mail_acct)
msgs = list(mailbox._fetch_recent_messages(access_token))
import re
for m in msgs:
    subj = str(m.get('subject') or '')
    body_text = str(m.get('body_text') or '')
    body_html = str(m.get('body_html') or '')
    combined = subj + '\n' + body_text + '\n' + body_html
    if 'mixroute' in combined.lower() or 'mix route' in combined.lower():
        print('UID:', m.get('uid'))
        print('SUBJ:', subj)
        print('BODY_TEXT:', repr(body_text[:400]))
        print('BODY_HTML:', repr(body_html[:800]))
        codes = re.findall(r'(?<!\d)(\d{6})(?!\d)', combined)
        print('CODES:', codes)
