"""Run one real mixroute.ai registration end-to-end (cdp_protocol executor).

CDP 拿 Turnstile token + 协议注册（/api/user/register）+ 协议拿 key（/api/token/）。
用 outlook_token 邮箱池收验证码（扫 INBOX/Junk）。

Run: python scripts/run_mixroute_once.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from application.mailbox_inventory_support import build_mailbox_inventory_seed
from core.base_mailbox import create_mailbox
from core.base_platform import RegisterConfig
from core.db import init_db, save_account
from core.registry import get, load_all
from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository


def _mask(value: str) -> str:
    raw = str(value or '')
    if len(raw) <= 10:
        return raw[:2] + '***' if raw else ''
    return raw[:6] + '...' + raw[-4:]


def _lookup_saved_account_id(platform: str, email: str) -> int:
    db_path = ROOT.joinpath('account_manager.db')
    with sqlite3.connect(db_path) as con:
        row = con.execute(
            'SELECT id FROM accounts WHERE platform=? AND email=? ORDER BY id DESC LIMIT 1',
            (platform, email),
        ).fetchone()
    return int(row[0]) if row else 0


def main() -> int:
    init_db()
    load_all()
    repo = MailboxInventoryRepository()
    # 支持命令行指定 inventory id，避免复用含旧 OTP 的邮箱
    target_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if target_id:
        # 直接 claim 指定 id
        all_items = repo.list_by_provider('outlook_token', status='unused')
        item = next((x for x in all_items if int(x.get('id') or 0) == target_id), None)
        if item:
            repo.update_item(target_id, status='running', task_id='manual-mixroute', platform='mixroute')
    else:
        claimed = repo.claim_available('outlook_token', count=1, task_id='manual-mixroute', platform='mixroute', include_outlook_aliases=False)
        item = claimed[0] if claimed else None
    seed = build_mailbox_inventory_seed('outlook_token', item) if item else None
    if seed is None:
        print(json.dumps({'ok': False, 'error': 'no_outlook_inventory'}, ensure_ascii=False), flush=True)
        return 2
    inventory_id = int(item.get('id') or 0)
    extra = dict(seed.extra or {})
    extra.update({
        'identity_provider': 'mailbox',
        'mail_provider': 'outlook_token',
        'platform': 'mixroute',
        'platform_name': 'mixroute',
        'mixroute_otp_timeout': 240,
        'mail_otp_timeout': 240,
        '_inventory': {'id': inventory_id, 'provider_key': 'outlook_token'},
    })
    result_path = ROOT.joinpath('output', f'mixroute_register_once_{int(time.time())}.json')
    result_path.parent.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []

    def log(msg: str) -> None:
        text = str(msg)
        logs.append(text)
        print(text, flush=True)

    try:
        mailbox = create_mailbox('outlook_token', extra=extra, proxy=None)
        platform_cls = get('mixroute')
        platform = platform_cls(
            config=RegisterConfig(executor_type='cdp_protocol', captcha_solver='auto', proxy=None, extra=extra),
            mailbox=mailbox,
        )
        platform.set_logger(log)
        account = platform.register(email=seed.email, password=seed.password or None)
        save_account(account)
        account_id = _lookup_saved_account_id(account.platform, account.email)
        api_key = str(account.token or (account.extra or {}).get('api_key') or '')
        api_verification = (account.extra or {}).get('api_verification') or {}
        payload = {
            'ok': True,
            'account_id': account_id,
            'email': account.email,
            'status': str(account.status),
            'api_key_preview': _mask(api_key),
            'api_base': (account.extra or {}).get('api_base'),
            'api_verification_ok': bool(api_verification.get('ok')),
            'username': (account.extra or {}).get('username'),
            'auth_method': (account.extra or {}).get('auth_method'),
            'inventory_id': inventory_id,
            'logs': logs,
            'account_extra': account.extra,
        }
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print('RESULT_JSON=' + str(result_path), flush=True)
        summary = {k: payload[k] for k in ('ok','account_id','email','status','api_key_preview','api_base','api_verification_ok','username','auth_method','inventory_id')}
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if api_key and api_verification.get('ok'):
            repo.mark_registration_success(inventory_id, registered_email=account.email, task_id='manual-mixroute', platform='mixroute')
        return 0 if (api_key and api_verification.get('ok')) else 1
    except Exception as exc:
        error = str(exc)
        try:
            repo.update_item(inventory_id, status='unused', last_error=error[:500], task_id='manual-mixroute', platform='mixroute')
        except Exception:
            pass
        payload = {
            'ok': False,
            'email': seed.email,
            'inventory_id': inventory_id,
            'error': error,
            'traceback': traceback.format_exc(),
            'logs': logs,
        }
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        print('RESULT_JSON=' + str(result_path), flush=True)
        print('ERROR=' + error, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
