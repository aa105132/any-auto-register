"""Debug: send verification + register with verbose response capture."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json
from core.base_captcha import CdpTurnstileSolver
from platforms.mixroute.core import (
    TURNSTILE_SITEKEY, REGISTER_URL, _build_session,
    send_verification_http, register_http, _response_success, _response_message,
    _response_data, get_status_http,
)

session = _build_session()

# Read status
status = get_status_http(session)
print(f"[status] success={_response_success(status)} turnstile_check={(_response_data(status) or {}).get('turnstile_check')}", flush=True)
print(f"[status] email_verification={(_response_data(status) or {}).get('email_verification')}", flush=True)

# Solve turnstile
solver = CdpTurnstileSolver(headless=False)
token = solver.solve_turnstile(REGISTER_URL, TURNSTILE_SITEKEY)
print(f"[turnstile] token len={len(str(token))}", flush=True)

# Use a fresh test email - we need a real receivable one. For debug, just probe the send endpoint.
# Use a throwaway to see the send response structure.
test_email = "test_probe_mixroute@example.com"
print(f"\n[send] email={test_email}", flush=True)
send = send_verification_http(session, test_email, turnstile=str(token))
print(f"[send] success={_response_success(send)} status={send.get('status')}", flush=True)
print(f"[send] data={json.dumps(send.get('data'), ensure_ascii=False)[:500]}", flush=True)
print(f"[send] message={_response_message(send)}", flush=True)
