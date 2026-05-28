
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import string
import sys
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.base_mailbox import create_mailbox
from core.db import save_account
from platforms.swarms.plugin import SwarmsPlatform
from platforms.swarms.protocol_mailbox import SwarmsProtocolMailboxWorker


def mask_email(email: str) -> str:
    local, _, domain = str(email or '').partition('@')
    if not domain:
        return '<unknown>'
    masked = (local[:2] + '***' + local[-2:]) if len(local) > 4 else local[:1] + '***'
    return masked + '@' + domain


def make_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return 'Sw@' + ''.join(secrets.choice(alphabet) for _ in range(18)) + '9!'


def credit_amount(payload: Any) -> float:
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, dict):
        for key in ('data', 'credit', 'credits', 'balance', 'amount'):
            if key in payload:
                try:
                    return credit_amount(payload.get(key))
                except Exception:
                    pass
        for value in payload.values():
            found = credit_amount(value)
            if found:
                return found
    try:
        return float(str(payload).strip())
    except Exception:
        return 0.0


def safe_log(message: str) -> None:
    text = str(message)
    if 'sk-' in text or 'access_token' in text or 'refresh_token' in text or 'sb-db-auth-token' in text:
        text = '[redacted-sensitive-line]'
    print('[log] ' + text.encode('ascii', 'backslashreplace').decode('ascii'), flush=True)


def verify_call(api_key: str) -> tuple[int, str]:
    if not api_key:
        return 0, 'no-key'
    response = requests.post(
        'https://api.swarms.world/v1/chat/completions',
        json={
            'model': 'claude-opus-4-6',
            'messages': [{'role': 'user', 'content': 'reply only: ok'}],
            'max_tokens': 8,
            'stream': False,
        },
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        timeout=45,
    )
    if response.status_code >= 400:
        return response.status_code, response.text[:240].replace(api_key, '<redacted>')
    try:
        data = response.json()
        content = (((data.get('choices') or [{}])[0].get('message') or {}).get('content') or '')[:80]
        return response.status_code, content
    except Exception:
        return response.status_code, response.text[:160]


def run_once(*, proxy: str | None, mail_provider: str, mail_extra: dict[str, Any]) -> dict[str, Any]:
    mailbox = create_mailbox(mail_provider, mail_extra)
    mailbox_account = mailbox.get_email()
    before_ids = mailbox.get_current_ids(mailbox_account)
    email = mailbox_account.email
    password = make_password()
    print(f'[start] email={mask_email(email)}', flush=True)

    def wait_link() -> str:
        print('[mail] waiting Swarms verification link', flush=True)
        link = mailbox.wait_for_link(mailbox_account, keyword='swarms', timeout=240, before_ids=before_ids)
        print('[mail] link_received=yes', flush=True)
        return link

    worker = SwarmsProtocolMailboxWorker(proxy=proxy, log_fn=safe_log)
    result = worker.run(email=email, password=password, verification_link_callback=wait_link)
    api_key = str(result.get('api_key') or '')
    credit = credit_amount(result.get('credit_info') if isinstance(result.get('credit_info'), dict) else {})
    status, content = verify_call(api_key)
    print('[result] email=' + mask_email(email), flush=True)
    print('[result] user_id_set=' + str(bool(result.get('user_id'))), flush=True)
    print('[result] credit=' + str(credit), flush=True)
    print('[result] api_key=' + ('yes' if api_key else 'no'), flush=True)
    print('[call] status=' + str(status), flush=True)
    print('[call] content=' + str(content).encode('ascii', 'backslashreplace').decode('ascii'), flush=True)
    if not api_key or status != 200:
        raise RuntimeError(f'no usable api key: credit={credit} call={status}/{content}')

    platform = SwarmsPlatform()
    registration_result = platform._map_swarms_result(result, password=password)
    account_obj = platform._account_from_registration_result(registration_result)
    saved = save_account(account_obj)
    print('[saved] account_id=' + str(saved.id) + ' email=' + mask_email(email), flush=True)
    return {'id': int(saved.id), 'email': mask_email(email), 'credit': credit, 'call_status': status}


def main() -> None:
    parser = argparse.ArgumentParser(description='Register Swarms accounts and immediately save usable API-key accounts to DB.')
    parser.add_argument('--count', type=int, default=1)
    parser.add_argument('--max-attempts', type=int, default=0)
    parser.add_argument('--proxy-env', default='SWARMS_REGISTER_PROXY', help='Read proxy URL from env var; proxy is never printed.')
    parser.add_argument('--mail-provider', default='yyds_mail')
    args = parser.parse_args()

    target = max(1, args.count)
    max_attempts = args.max_attempts if args.max_attempts > 0 else target
    proxy = os.environ.get(args.proxy_env) or None
    successes: list[dict[str, Any]] = []
    failures: list[str] = []
    for attempt in range(1, max_attempts + 1):
        if len(successes) >= target:
            break
        print(f'[attempt] {attempt}/{max_attempts}', flush=True)
        try:
            successes.append(run_once(proxy=proxy, mail_provider=args.mail_provider, mail_extra={}))
        except Exception as exc:
            message = str(exc).encode('ascii', 'backslashreplace').decode('ascii')[:500]
            failures.append(message)
            print('[fail] ' + message, flush=True)
    print('[summary] ' + json.dumps({'target': target, 'attempts': min(max_attempts, len(successes) + len(failures)), 'saved': len(successes), 'successes': successes, 'failures': failures}, ensure_ascii=False), flush=True)
    if len(successes) < target:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
