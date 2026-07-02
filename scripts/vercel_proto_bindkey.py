"""Vercel 纯协议建key+送额度脚本（给审核通过的号批量跑）。

流程：patchright 本地7897登录 vercel.com/login（邮箱OTP keyword=Vercel）→ 拿 vcp_ token + team_id
→ 纯协议 POST /api/api-keys 建 vck_ key → 调 ai-gateway chat 送 $5 额度 → 查余额验证。
绑卡状态：先建key试，失败(402/403)说明没绑卡，需先浏览器绑卡。
"""
from __future__ import annotations

import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platforms.vercel.protocol_mailbox import _build_outlook_mailbox, _wait_email_otp, _click_continue_with_email_entry
from platforms.vercel.core import page_state, fill_email, enter_otp, LOGIN_URL, VercelClient
from core.base_mailbox import MailboxAccount

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "MillwoodAndrepont446@outlook.com"
PROXY_LOCAL = "http://127.0.0.1:7897"
OUT = ROOT / "scripts" / "_vercel_proto_bindkey_result.json"


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [proto] {msg}", flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] [proto] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


def main() -> int:
    log(f"账号: {EMAIL}")
    mailbox, em = _build_outlook_mailbox(proxy=PROXY_LOCAL, preferred_email=EMAIL, log_fn=log)
    acc = MailboxAccount(email=em, account_id=em)
    before_ids = mailbox.get_current_ids(acc)
    log(f"登录前基线: {len(before_ids)} 封")

    from playwright.sync_api import sync_playwright
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        pw = sync_playwright().start()
        p = urlparse(PROXY_LOCAL)
        br = pw.chromium.launch(headless=False, proxy={"server": f"{p.scheme}://{p.hostname}:{p.port}"},
            args=["--disable-dev-shm-usage", "--disable-default-apps", "--disable-extensions",
                  "--no-first-run", "--no-default-browser-check", "--no-sandbox", "--mute-audio"])
        try: yield br, pw
        finally:
            try: br.close()
            except Exception: pass
            try: pw.stop()
            except Exception: pass

    result = {"email": em, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with _ctx() as (br, pw):
        ctx = br.new_context(viewport={"width": 1366, "height": 800}, locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
        page = ctx.new_page()
        # 登录
        for _g in range(4):
            try:
                page.goto(LOGIN_URL, wait_until="commit", timeout=60000); break
            except Exception as exc:
                log(f"goto 重试 {_g+1}: {str(exc)[:80]}"); page.wait_for_timeout(2000)
        page.wait_for_timeout(3500)
        for _e in range(12):
            if _click_continue_with_email_entry(page, log_fn=log):
                page.wait_for_timeout(2000)
                if page_state(page)["has_email_input"]: break
            page.wait_for_timeout(3000)
        fill_email(page, em, log_fn=log)
        page.wait_for_timeout(5000)
        for _ in range(8):
            st = page_state(page)
            if st["has_otp_input"] or st["otp_sent"]: break
            page.wait_for_timeout(2500)
        otp = _wait_email_otp(mailbox, acc, timeout=180, before_ids=before_ids, log_fn=log)
        if not otp:
            result["error"] = "未收到登录 OTP"; OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); return 1
        log(f"收到 OTP: {otp}")
        enter_otp(page, otp)
        page.wait_for_timeout(8000)
        log(f"登录后 url={page.url[:80]}")

        # 拿 vcp_ token（cookie authorization）
        cookies = {c["name"]: c["value"] for c in ctx.cookies() if "vercel.com" in (c.get("domain") or "")}
        auth = cookies.get("authorization", "")
        vcp_token = unquote(auth).replace("Bearer ", "") if auth else ""
        result["vcp_token"] = vcp_token[:20] + "..." if vcp_token else ""
        log(f"vcp_ token: {result['vcp_token']}")
        if not vcp_token:
            result["error"] = "未拿到 vcp_ token"; OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); return 1

    # 纯协议：建 key + 送额度
    client = VercelClient(proxy=PROXY_LOCAL, log_fn=log)
    team_id = client.get_team_id(vcp_token)
    result["team_id"] = team_id
    log(f"team_id: {team_id}")
    if not team_id:
        result["error"] = "未拿到 team_id"; OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); return 1

    # 先查余额看绑卡没
    bal_before = client.get_credits_balance(vcp_token, team_id)
    result["balance_before"] = bal_before
    log(f"建key前余额: {bal_before}")

    key = client.create_ai_gateway_key(vcp_token, team_id, name="auto-register")
    result["api_key"] = key[:20] + "..." if key else ""
    if not key:
        result["error"] = "建 key 失败（可能未绑卡，需先浏览器绑卡）"
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    # 调一次送额度
    trig = client.trigger_free_credit(key)
    result["trigger"] = {"ok": trig.get("ok"), "response": trig.get("response", "")[:60]}

    bal_after = client.get_credits_balance(vcp_token, team_id)
    result["balance_after"] = bal_after
    result["api_key_full"] = key
    log(f"建key后余额: {bal_after}")
    log(f"完整 key: {key}")

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结果已写入 {OUT}")
    return 0 if trig.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
