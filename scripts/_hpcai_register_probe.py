"""HPC-AI 注册 + 奖励/鉴权探测脚本（独立运行，不复用任务调度）。

阶段：
  A. 用项目配置的系统邮箱（cfworker）生成邮箱并走 HpcAiProtocolMailboxWorker 完成注册，
     捕获 register / login / welcome claim / balance / credit / voucher / api_key / models / chat 全量响应。
  B. 用拿到的 accessToken 做奖励重放与鉴权探测：
     - welcome/check & welcome/claim 重复调用（重放）
     - balance / credit/list / voucher/list 复查
     - 不带 token 调用受保护接口（鉴权缺口）
     - 再次 create api key（多 key）
     - /models 与 /chat/completions 真实调用
  C. 落盘 scripts/_hpcai_register_result.json 供分析。
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
    OPENAI_COMPAT_API_BASE,
    SITE_URL,
    SIGNUP_URL,
    TURNSTILE_SITE_KEY,
    VOUCHER_LIST_PATH,
    WELCOME_VOUCHER_CHECK_PATH,
    WELCOME_VOUCHER_CLAIM_PATH,
    HpcAiProtocolMailboxWorker,
    _api_url,
    _auth_value,
    _build_session,
    _json_or_text,
    _llm_api_url,
    _request_json,
)

OUT = ROOT / "scripts" / "_hpcai_register_result.json"

INVITATION_CODE = "invite_PTTb2sYFpM68a4H2UAdJic"


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


def build_mailbox(provider: str):
    extra = {"platform": "hpcai"}
    mailbox = create_mailbox(provider, extra=extra, proxy=None)
    return mailbox


def build_captcha() -> YesCaptcha:
    # 直接读 configs 表里的 yescaptcha 配置
    import sqlite3
    con = sqlite3.connect(str(ROOT / "account_manager.db"))
    cur = con.cursor()
    cur.execute("SELECT value FROM configs WHERE key IN ('yescaptcha_key','yescaptcha_api_url')")
    cfg = {k: v for k, v in cur.fetchall()} if False else {}
    cur.execute("SELECT key, value FROM configs WHERE key IN ('yescaptcha_key','yescaptcha_api_url')")
    for k, v in cur.fetchall():
        cfg[k] = v
    con.close()
    return YesCaptcha(cfg["yescaptcha_key"], cfg.get("yescaptcha_api_url") or "https://api.yescaptcha.com")


def register_with_mailbox(provider: str) -> dict:
    log(f"=== 阶段 A：使用 {provider} 注册 HPC-AI ===")
    mailbox = build_mailbox(provider)
    log("生成邮箱中...")
    account = mailbox.get_email()
    email = account.email
    log(f"邮箱: {email}")
    before_ids = set()
    try:
        before_ids = mailbox.get_current_ids(account)
        log(f"已有邮件 ID 数: {len(before_ids)}")
    except Exception as exc:
        log(f"get_current_ids 失败（忽略）: {exc}")

    password = strong_password()
    captcha = build_captcha()

    def otp_callback() -> str:
        log("等待 HPC-AI OTP 验证码邮件...")
        code = mailbox.wait_for_code(
            account,
            keyword="",
            timeout=240,
            before_ids=before_ids,
            code_pattern=r"\b\d{6}\b",
        )
        log(f"OTP: {code}")
        return code

    worker = HpcAiProtocolMailboxWorker(proxy=None, log_fn=log, use_cdp_bridge=False)
    result = worker.run(
        email=email,
        password=password,
        otp_callback=otp_callback,
        captcha_solver=captcha,
        key_name="probe-auto",
        invitation_code=INVITATION_CODE,
        minimum_credit=2.0,
    )
    result["_mailbox_provider"] = provider
    return result


def probe_rewards_and_auth(token: str, api_key: str) -> dict:
    log("=== 阶段 B：奖励重放与鉴权探测 ===")
    session = _build_session(None)
    session.headers.update({"Authorization": _auth_value(token)})
    findings: dict = {"replay": {}, "auth_gap": {}, "multi_key": {}, "api_call": {}}

    # B1. 重放 welcome/check 与 welcome/claim（带 token）
    log("B1 重放 welcome/check + claim（带 token）")
    findings["replay"]["check_1"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=SITE_URL + "/models-console/models")
    findings["replay"]["claim_1"] = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=SITE_URL + "/models-console/models")
    time.sleep(1)
    findings["replay"]["check_2"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=SITE_URL + "/models-console/models")
    findings["replay"]["claim_2"] = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=SITE_URL + "/models-console/models")
    findings["replay"]["balance"] = _request_json(session, BALANCE_PATH, method="GET", token=token, referer=SITE_URL + "/models-console/models")
    findings["replay"]["credit_list"] = _request_json(session, CREDIT_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, token=token, referer=SITE_URL + "/models-console/models")
    findings["replay"]["voucher_list"] = _request_json(session, VOUCHER_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, token=token, referer=SITE_URL + "/models-console/models")

    # B2. 鉴权缺口：不带 token 调用受保护接口
    log("B2 鉴权缺口：无 token 调用受保护接口")
    naked = _build_session(None)
    findings["auth_gap"]["balance_no_token"] = _request_json(naked, BALANCE_PATH, method="GET", referer=SITE_URL + "/models-console/models")
    findings["auth_gap"]["credit_list_no_token"] = _request_json(naked, CREDIT_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, referer=SITE_URL + "/models-console/models")
    findings["auth_gap"]["voucher_list_no_token"] = _request_json(naked, VOUCHER_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, referer=SITE_URL + "/models-console/models")
    findings["auth_gap"]["welcome_claim_no_token"] = _request_json(naked, WELCOME_VOUCHER_CLAIM_PATH, referer=SITE_URL + "/models-console/models")
    findings["auth_gap"]["key_create_no_token"] = _request_json(naked, API_KEY_CREATE_PATH, method="POST", body={"name": "naked"}, referer=SITE_URL + "/models-console/api-key")
    findings["auth_gap"]["key_list_no_token"] = _request_json(naked, API_KEY_LIST_PATH, referer=SITE_URL + "/models-console/api-key")
    # 错误 token
    bad = _build_session(None)
    findings["auth_gap"]["balance_bad_token"] = _request_json(bad, BALANCE_PATH, method="GET", token="Bearer fake.token.value", referer=SITE_URL + "/models-console/models")

    # B3. 多 key：再次 create
    log("B3 多 key：再次 create api key")
    findings["multi_key"]["create_2"] = _request_json(session, API_KEY_CREATE_PATH, method="POST", body={"name": "probe-auto-2"}, token=token, referer=SITE_URL + "/models-console/api-key")
    findings["multi_key"]["create_3"] = _request_json(session, API_KEY_CREATE_PATH, method="POST", body={"name": "probe-auto-3"}, token=token, referer=SITE_URL + "/models-console/api-key")
    findings["multi_key"]["key_list"] = _request_json(session, API_KEY_LIST_PATH, token=token, referer=SITE_URL + "/models-console/api-key")

    # B4. 真实调用 /models 与 /chat/completions
    log("B4 /models 与 /chat/completions 真实调用")
    import requests as _r
    llm = _r.Session()
    if api_key:
        resp = llm.get(_llm_api_url(MODELS_PATH), headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}, timeout=45)
        findings["api_call"]["models"] = {"ok": resp.ok, "status": resp.status_code, "body": _json_or_text(resp, limit=2000)}
        chat = llm.post(
            _llm_api_url(CHAT_COMPLETIONS_PATH),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"},
            json={"model": "deepseek-ai/DeepSeek-V3-0324", "messages": [{"role": "user", "content": "Reply with exactly: pong"}], "max_tokens": 16, "temperature": 0},
            timeout=45,
        )
        findings["api_call"]["chat_completions"] = {"ok": chat.ok, "status": chat.status_code, "body": _json_or_text(chat, limit=2000)}
    # 无 key 调 chat（鉴权）
    chat_nok = llm.post(
        _llm_api_url(CHAT_COMPLETIONS_PATH),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={"model": "deepseek-ai/DeepSeek-V3-0324", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
        timeout=30,
    )
    findings["api_call"]["chat_no_key"] = {"ok": chat_nok.ok, "status": chat_nok.status_code, "body": _json_or_text(chat_nok, limit=1000)}
    return findings


def main() -> None:
    provider = sys.argv[1] if len(sys.argv) > 1 else "cfworker"
    try:
        result = register_with_mailbox(provider)
    except Exception as exc:
        log(f"注册失败: {exc!r}")
        OUT.write_text(json.dumps({"ok": False, "error": repr(exc), "provider": provider}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise

    token = str(result.get("session", {}).get("accessToken") or "")
    api_key = str(result.get("api_key") or "")
    log(f"注册成功 email={result.get('email')} token={'有' if token else '无'} api_key={'有' if api_key else '无'}")

    findings = {}
    if token:
        try:
            findings = probe_rewards_and_auth(token, api_key)
        except Exception as exc:
            log(f"探测异常: {exc!r}")
            findings = {"error": repr(exc)}

    payload = {
        "ok": True,
        "provider": provider,
        "email": result.get("email"),
        "password": result.get("password"),
        "user_id": result.get("user_id"),
        "token": token,
        "api_key": api_key,
        "register_result": result.get("register_result"),
        "login_result": result.get("login_result"),
        "claim_result": result.get("claim_result"),
        "credit_result": result.get("credit_result"),
        "balance": result.get("balance"),
        "credit_list": result.get("credit_list"),
        "voucher_list": result.get("voucher_list"),
        "api_key_info": result.get("api_key_info"),
        "api_verification": result.get("api_verification"),
        "findings": findings,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结果已写入: {OUT}")


if __name__ == "__main__":
    main()
