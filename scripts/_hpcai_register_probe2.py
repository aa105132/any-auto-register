"""HPC-AI 全流程 + 奖励/鉴权探测（v2，注册中途落盘 token）。

与 v1 区别：复用 protocol_mailbox 的底层 HTTP helper 自己编排流程，在拿到 token 后
立即落盘，避免 credit 校验失败导致 token 丢失；然后用 token 系统性探测奖励/鉴权。
"""
from __future__ import annotations

import json
import random
import string
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.base_captcha import YesCaptcha
from core.base_mailbox import create_mailbox
from platforms.hpcai.protocol_mailbox import (
    API_KEY_CREATE_PATH,
    API_KEY_LIST_PATH,
    BALANCE_PATH,
    CHAT_COMPLETIONS_PATH,
    CREDIT_LIST_PATH,
    MODELS_PATH,
    SIGNUP_URL,
    TURNSTILE_SITE_KEY,
    USER_INFO_PATH,
    VOUCHER_LIST_PATH,
    WELCOME_VOUCHER_CHECK_PATH,
    WELCOME_VOUCHER_CLAIM_PATH,
    _auth_value,
    _build_session,
    _claim_welcome_voucher_http,
    _create_api_key_http,
    _extract_token,
    _find_api_key,
    _get_user_info_http,
    _json_or_text,
    _llm_api_url,
    _login_email_http,
    _register_email_http,
    _request_json,
    _response_ok,
    _send_register_otp_http,
    _verify_api_key_http,
)

OUT = ROOT / "scripts" / "_hpcai_register_result.json"
INVITATION_CODE = "invite_PTTb2sYFpM68a4H2UAdJic"
CONSOLE = "https://www.hpc-ai.com/models-console/models"
KEY_PAGE = "https://www.hpc-ai.com/models-console/api-key"


def log(msg: str) -> None:
    print(f"[probe] {msg}", flush=True)


def strong_password() -> str:
    required = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(string.digits),
        random.choice('!"#$%&()*+,-./:;<=>?@[\\]^_`{|}~'),
    ]
    pool = string.ascii_letters + string.digits + '!"#$%&()*+,-./:;<=>?@[\\]^_`{|}~'
    chars = required + [random.choice(pool) for _ in range(12)]
    random.shuffle(chars)
    return "".join(chars)


def build_captcha() -> YesCaptcha:
    import sqlite3
    con = sqlite3.connect(str(ROOT / "account_manager.db"))
    cur = con.cursor()
    cur.execute("SELECT key, value FROM configs WHERE key IN ('yescaptcha_key','yescaptcha_api_url')")
    cfg = dict(cur.fetchall())
    con.close()
    return YesCaptcha(cfg["yescaptcha_key"], cfg.get("yescaptcha_api_url") or "https://api.yescaptcha.com")


def do_register(provider: str) -> dict:
    log(f"=== 注册阶段：{provider} ===")
    mailbox = create_mailbox(provider, extra={"platform": "hpcai"}, proxy=None)
    account = mailbox.get_email()
    email = account.email
    log(f"邮箱: {email}")
    try:
        before_ids = mailbox.get_current_ids(account)
    except Exception:
        before_ids = set()

    session = _build_session(None)
    otp_send = _send_register_otp_http(session, email)
    log(f"otp_send ok={otp_send.get('ok')} status={otp_send.get('status')} msg={_msg(otp_send)}")
    if not otp_send.get("ok"):
        raise RuntimeError(f"发送验证码失败: {_msg(otp_send)}")

    log("等待 OTP 邮件...")
    otp = mailbox.wait_for_code(account, keyword="", timeout=240, before_ids=before_ids, code_pattern=r"\b\d{6}\b")
    log(f"OTP: {otp}")

    captcha = build_captcha()
    log("解 Turnstile...")
    turnstile = captcha.solve_turnstile(SIGNUP_URL, TURNSTILE_SITE_KEY)
    log(f"Turnstile token 长度={len(turnstile)}")

    password = strong_password()
    reg = _register_email_http(session, email=email, password=password, otp=otp, turnstile=turnstile, invitation_code=INVITATION_CODE)
    log(f"register ok={reg.get('ok')} status={reg.get('status')} msg={_msg(reg)}")
    token = str(reg.get("token") or _extract_token(reg.get("data")) or "").strip()
    if not token:
        log("register 未直接返回 token，尝试 login...")
        login = _login_email_http(session, email, password)
        log(f"login ok={login.get('ok')} status={login.get('status')} msg={_msg(login)}")
        token = str(login.get("token") or _extract_token(login.get("data")) or "").strip()
    if not token:
        raise RuntimeError("未能拿到 accessToken")
    log(f"token 长度={len(token)}")

    # 立即落盘基础信息
    base = {
        "ok": True,
        "provider": provider,
        "email": email,
        "password": password,
        "token": token,
        "register_result": reg,
    }
    OUT.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已落盘基础结果（含 token）: {OUT}")

    return {"session": session, "token": token, "email": email, "password": password, "register_result": reg}


def probe(session, token: str) -> dict:
    log("=== 探测阶段 ===")
    findings: dict = {}
    session.headers.update({"Authorization": _auth_value(token)})

    log("user/info")
    findings["user_info"] = _get_user_info_http(session, token)

    log("welcome check x3 / claim x3")
    findings["welcome_check_1"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=CONSOLE)
    findings["welcome_claim_1"] = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=CONSOLE)
    claim1 = _claim_welcome_voucher_http(session, token)
    findings["welcome_claim_combined"] = claim1
    time.sleep(1)
    findings["welcome_check_2"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=CONSOLE)
    findings["welcome_claim_2"] = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=CONSOLE)
    time.sleep(1)
    findings["welcome_check_3"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=CONSOLE)
    findings["welcome_claim_3"] = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=CONSOLE)

    log("balance")
    findings["balance"] = _request_json(session, BALANCE_PATH, method="GET", token=token, referer=CONSOLE)

    log("credit/voucher list（原 body + 修正 body）")
    findings["credit_list_orig"] = _request_json(session, CREDIT_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, token=token, referer=CONSOLE)
    findings["voucher_list_orig"] = _request_json(session, VOUCHER_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, token=token, referer=CONSOLE)
    now_ms = int(time.time() * 1000)
    fixed = {"page": 1, "pageSize": 20, "expireTimeAfter": now_ms - 30 * 86400 * 1000, "sortBy": "createTime", "sortOrder": "desc"}
    findings["credit_list_fixed"] = _request_json(session, CREDIT_LIST_PATH, method="POST", body=fixed, token=token, referer=CONSOLE)
    findings["voucher_list_fixed"] = _request_json(session, VOUCHER_LIST_PATH, method="POST", body=fixed, token=token, referer=CONSOLE)
    findings["credit_list_zero"] = _request_json(session, CREDIT_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20, "expireTimeAfter": 0}, token=token, referer=CONSOLE)
    findings["voucher_list_zero"] = _request_json(session, VOUCHER_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20, "expireTimeAfter": 0}, token=token, referer=CONSOLE)

    log("鉴权缺口：无 token / 错误 token")
    naked = _build_session(None)
    findings["auth_gap"] = {
        "balance_no_token": _request_json(naked, BALANCE_PATH, method="GET", referer=CONSOLE),
        "credit_list_no_token": _request_json(naked, CREDIT_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20, "expireTimeAfter": 0}, referer=CONSOLE),
        "voucher_list_no_token": _request_json(naked, VOUCHER_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20, "expireTimeAfter": 0}, referer=CONSOLE),
        "welcome_claim_no_token": _request_json(naked, WELCOME_VOUCHER_CLAIM_PATH, referer=CONSOLE),
        "key_create_no_token": _request_json(naked, API_KEY_CREATE_PATH, method="POST", body={"name": "naked"}, referer=KEY_PAGE),
        "key_list_no_token": _request_json(naked, API_KEY_LIST_PATH, referer=KEY_PAGE),
        "user_info_no_token": _request_json(naked, USER_INFO_PATH, referer=CONSOLE),
        "balance_bad_token": _request_json(_build_session(None), BALANCE_PATH, method="GET", token="Bearer fake.token.value", referer=CONSOLE),
    }

    log("多 key：list + 多次 create")
    findings["key_list_1"] = _request_json(session, API_KEY_LIST_PATH, token=token, referer=KEY_PAGE)
    findings["key_create_1"] = _request_json(session, API_KEY_CREATE_PATH, method="POST", body={"name": "probe-a"}, token=token, referer=KEY_PAGE)
    findings["key_create_2"] = _request_json(session, API_KEY_CREATE_PATH, method="POST", body={"name": "probe-b"}, token=token, referer=KEY_PAGE)
    findings["key_create_3"] = _request_json(session, API_KEY_CREATE_PATH, method="POST", body={"name": "probe-c"}, token=token, referer=KEY_PAGE)
    findings["key_list_2"] = _request_json(session, API_KEY_LIST_PATH, token=token, referer=KEY_PAGE)

    log("api key 提取 + 真实调用")
    api_key = ""
    for kc in ("key_create_1", "key_create_2", "key_create_3"):
        r = findings.get(kc, {})
        api_key = _find_api_key(r.get("data")) or api_key
    if not api_key:
        api_key = _find_api_key(findings.get("key_list_1", {}).get("data")) or ""
    findings["_resolved_api_key"] = api_key
    log(f"resolved api_key={'有' if api_key else '无'}")

    import requests as _r
    llm = _r.Session()
    if api_key:
        try:
            resp = llm.get(_llm_api_url(MODELS_PATH), headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}, timeout=45)
            findings["api_call_models"] = {"ok": resp.ok, "status": resp.status_code, "body": _json_or_text(resp, limit=4000)}
        except Exception as exc:
            findings["api_call_models"] = {"error": repr(exc)}
        try:
            chat = llm.post(_llm_api_url(CHAT_COMPLETIONS_PATH),
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"},
                json={"model": "deepseek-ai/DeepSeek-V3-0324", "messages": [{"role": "user", "content": "Reply with exactly: pong"}], "max_tokens": 16, "temperature": 0},
                timeout=60)
            findings["api_call_chat"] = {"ok": chat.ok, "status": chat.status_code, "body": _json_or_text(chat, limit=4000)}
        except Exception as exc:
            findings["api_call_chat"] = {"error": repr(exc)}
    try:
        chat_nok = llm.post(_llm_api_url(CHAT_COMPLETIONS_PATH),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"model": "deepseek-ai/DeepSeek-V3-0324", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8}, timeout=30)
        findings["api_call_chat_no_key"] = {"ok": chat_nok.ok, "status": chat_nok.status_code, "body": _json_or_text(chat_nok, limit=1500)}
    except Exception as exc:
        findings["api_call_chat_no_key"] = {"error": repr(exc)}

    return findings


def _msg(result: dict) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        for k in ("message", "msg", "error", "detail"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:200]
    return str(result.get("text") or "")[:200]


def main() -> None:
    provider = sys.argv[1] if len(sys.argv) > 1 else "cfworker"
    try:
        reg = do_register(provider)
    except Exception as exc:
        log(f"注册失败: {exc!r}")
        OUT.write_text(json.dumps({"ok": False, "error": repr(exc), "provider": provider}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    try:
        findings = probe(reg["session"], reg["token"])
    except Exception as exc:
        log(f"探测异常: {exc!r}")
        findings = {"error": repr(exc)}
    payload = {
        **json.loads(OUT.read_text(encoding="utf-8")),
        "findings": findings,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"完整结果已写入: {OUT}")

    log("===== 摘要 =====")
    ui = findings.get("user_info", {}).get("data")
    log(f"user_info: {json.dumps(ui, ensure_ascii=False)[:300]}")
    for i in (1, 2, 3):
        wc = findings.get(f"welcome_check_{i}", {}).get("data")
        wl = findings.get(f"welcome_claim_{i}", {})
        log(f"welcome_check_{i}={json.dumps(wc, ensure_ascii=False)[:160]} | claim_{i} status={wl.get('status')} data={json.dumps(wl.get('data'), ensure_ascii=False)[:160]}")
    log(f"balance={json.dumps(findings.get('balance', {}).get('data'), ensure_ascii=False)[:200]}")
    log(f"credit_list_fixed status={findings.get('credit_list_fixed', {}).get('status')} data={json.dumps(findings.get('credit_list_fixed', {}).get('data'), ensure_ascii=False)[:400]}")
    log(f"voucher_list_fixed status={findings.get('voucher_list_fixed', {}).get('status')} data={json.dumps(findings.get('voucher_list_fixed', {}).get('data'), ensure_ascii=False)[:400]}")
    ag = findings.get("auth_gap", {})
    log(f"auth_gap balance_no_token status={ag.get('balance_no_token', {}).get('status')} data={json.dumps(ag.get('balance_no_token', {}).get('data'), ensure_ascii=False)[:200]}")
    log(f"auth_gap user_info_no_token status={ag.get('user_info_no_token', {}).get('status')}")
    log(f"key_list_1 status={findings.get('key_list_1', {}).get('status')} data={json.dumps(findings.get('key_list_1', {}).get('data'), ensure_ascii=False)[:400]}")
    log(f"key_create_1 status={findings.get('key_create_1', {}).get('status')} data={json.dumps(findings.get('key_create_1', {}).get('data'), ensure_ascii=False)[:300]}")
    log(f"api_call_models={json.dumps(findings.get('api_call_models', {}), ensure_ascii=False)[:300]}")
    log(f"api_call_chat={json.dumps(findings.get('api_call_chat', {}), ensure_ascii=False)[:400]}")


if __name__ == "__main__":
    main()
