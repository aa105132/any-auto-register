"""HPC-AI 奖励/鉴权探测（B 阶段独立版）。

使用一个已注册账号的 accessToken，系统性地探测：
  1. welcome/check 与 welcome/claim 的真实行为 + 重放
  2. /api/balance、/api/credit/list、/api/voucher/list（含修正后的 body：expireTimeAfter）
  3. 鉴权缺口：无 token / 错误 token 调受保护接口
  4. 多 key：重复 create api key
  5. /models 与 /chat/completions 真实调用
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platforms.hpcai.protocol_mailbox import (
    API_KEY_CREATE_PATH,
    API_KEY_LIST_PATH,
    BALANCE_PATH,
    CHAT_COMPLETIONS_PATH,
    CREDIT_LIST_PATH,
    MODELS_PATH,
    USER_INFO_PATH,
    VOUCHER_LIST_PATH,
    WELCOME_VOUCHER_CHECK_PATH,
    WELCOME_VOUCHER_CLAIM_PATH,
    _auth_value,
    _build_session,
    _find_api_key,
    _json_or_text,
    _llm_api_url,
    _request_json,
    _response_ok,
)

OUT = ROOT / "scripts" / "_hpcai_reward_probe_result.json"
CONSOLE = "https://www.hpc-ai.com/models-console/models"
KEY_PAGE = "https://www.hpc-ai.com/models-console/api-key"


def log(msg: str) -> None:
    print(f"[probe] {msg}", flush=True)


def safe_json(d):
    try:
        return json.dumps(d, ensure_ascii=False, indent=2)
    except Exception:
        return repr(d)


def main() -> None:
    token = sys.argv[1] if len(sys.argv) > 1 else ""
    if not token:
        # 从上次注册结果里取
        prev = ROOT / "scripts" / "_hpcai_register_result.json"
        if prev.exists():
            data = json.loads(prev.read_text(encoding="utf-8"))
            token = str(data.get("token") or "")
    if not token:
        log("未提供 token，且无上次注册结果，退出")
        return
    log(f"使用 token 长度={len(token)} 前缀={token[:24]}...")

    session = _build_session(None)
    session.headers.update({"Authorization": _auth_value(token)})
    findings: dict = {}

    # 0. user/info（确认身份）
    log("user/info")
    findings["user_info"] = _request_json(session, USER_INFO_PATH, token=token, referer=CONSOLE)

    # 1. welcome 重放
    log("welcome check x3 / claim x3")
    findings["welcome_check_1"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=CONSOLE)
    findings["welcome_claim_1"] = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=CONSOLE)
    time.sleep(1)
    findings["welcome_check_2"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=CONSOLE)
    findings["welcome_claim_2"] = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=CONSOLE)
    time.sleep(1)
    findings["welcome_check_3"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=CONSOLE)
    findings["welcome_claim_3"] = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=CONSOLE)

    # 2. balance / credit / voucher（原始 + 修正 body）
    log("balance / credit / voucher（原 body）")
    findings["balance"] = _request_json(session, BALANCE_PATH, method="GET", token=token, referer=CONSOLE)
    findings["credit_list_orig"] = _request_json(session, CREDIT_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, token=token, referer=CONSOLE)
    findings["voucher_list_orig"] = _request_json(session, VOUCHER_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, token=token, referer=CONSOLE)

    log("credit / voucher（补 expireTimeAfter/排序字段）")
    now_ms = int(time.time() * 1000)
    far_ms = now_ms - 30 * 86400 * 1000
    fixed_body = {
        "page": 1,
        "pageSize": 20,
        "expireTimeAfter": far_ms,
        "sortBy": "createTime",
        "sortOrder": "desc",
    }
    findings["credit_list_fixed"] = _request_json(session, CREDIT_LIST_PATH, method="POST", body=fixed_body, token=token, referer=CONSOLE)
    findings["voucher_list_fixed"] = _request_json(session, VOUCHER_LIST_PATH, method="POST", body=fixed_body, token=token, referer=CONSOLE)
    # 另一组：只补 expireTimeAfter=0
    findings["credit_list_zero"] = _request_json(session, CREDIT_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20, "expireTimeAfter": 0}, token=token, referer=CONSOLE)
    findings["voucher_list_zero"] = _request_json(session, VOUCHER_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20, "expireTimeAfter": 0}, token=token, referer=CONSOLE)

    # 3. 鉴权缺口
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

    # 4. 多 key
    log("多 key：list + 多次 create")
    findings["key_list_1"] = _request_json(session, API_KEY_LIST_PATH, token=token, referer=KEY_PAGE)
    findings["key_create_1"] = _request_json(session, API_KEY_CREATE_PATH, method="POST", body={"name": "probe-a"}, token=token, referer=KEY_PAGE)
    findings["key_create_2"] = _request_json(session, API_KEY_CREATE_PATH, method="POST", body={"name": "probe-b"}, token=token, referer=KEY_PAGE)
    findings["key_create_3"] = _request_json(session, API_KEY_CREATE_PATH, method="POST", body={"name": "probe-c"}, token=token, referer=KEY_PAGE)
    findings["key_list_2"] = _request_json(session, API_KEY_LIST_PATH, token=token, referer=KEY_PAGE)

    # 5. 真实调用
    log("/models 与 /chat/completions")
    import requests as _r
    llm = _r.Session()
    # 取一个 key
    api_key = ""
    kl = findings.get("key_list_1", {})
    if isinstance(kl.get("data"), (dict, list)):
        api_key = _find_api_key(kl.get("data")) or ""
    if not api_key:
        for kc in ("key_create_1", "key_create_2", "key_create_3"):
            r = findings.get(kc, {})
            if isinstance(r.get("data"), (dict, list)):
                api_key = _find_api_key(r.get("data")) or api_key
    findings["_resolved_api_key"] = api_key
    if api_key:
        try:
            resp = llm.get(_llm_api_url(MODELS_PATH), headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"}, timeout=45)
            findings["api_call_models"] = {"ok": resp.ok, "status": resp.status_code, "body": _json_or_text(resp, limit=4000)}
        except Exception as exc:
            findings["api_call_models"] = {"error": repr(exc)}
        try:
            chat = llm.post(
                _llm_api_url(CHAT_COMPLETIONS_PATH),
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"},
                json={"model": "deepseek-ai/DeepSeek-V3-0324", "messages": [{"role": "user", "content": "Reply with exactly: pong"}], "max_tokens": 16, "temperature": 0},
                timeout=60,
            )
            findings["api_call_chat"] = {"ok": chat.ok, "status": chat.status_code, "body": _json_or_text(chat, limit=4000)}
        except Exception as exc:
            findings["api_call_chat"] = {"error": repr(exc)}
    # 无 key 调 chat
    try:
        chat_nok = llm.post(
            _llm_api_url(CHAT_COMPLETIONS_PATH),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"model": "deepseek-ai/DeepSeek-V3-0324", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 8},
            timeout=30,
        )
        findings["api_call_chat_no_key"] = {"ok": chat_nok.ok, "status": chat_nok.status_code, "body": _json_or_text(chat_nok, limit=1500)}
    except Exception as exc:
        findings["api_call_chat_no_key"] = {"error": repr(exc)}

    OUT.write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结果已写入: {OUT}")

    # 控制台摘要
    log("===== 摘要 =====")
    log(f"user_info.ok={_response_ok(findings.get('user_info', {}))} data={safe_json(findings.get('user_info', {}).get('data'))[:400]}")
    log(f"welcome_check: {safe_json(findings.get('welcome_check_1', {}).get('data'))[:200]}")
    log(f"welcome_claim_1: status={findings.get('welcome_claim_1', {}).get('status')} data={safe_json(findings.get('welcome_claim_1', {}).get('data'))[:200]}")
    log(f"balance: {safe_json(findings.get('balance', {}).get('data'))[:200]}")
    log(f"credit_list_fixed: status={findings.get('credit_list_fixed', {}).get('status')} data={safe_json(findings.get('credit_list_fixed', {}).get('data'))[:400]}")
    log(f"voucher_list_fixed: status={findings.get('voucher_list_fixed', {}).get('status')} data={safe_json(findings.get('voucher_list_fixed', {}).get('data'))[:400]}")
    log(f"key_list_1: status={findings.get('key_list_1', {}).get('status')} data={safe_json(findings.get('key_list_1', {}).get('data'))[:400]}")
    log(f"key_create_1: status={findings.get('key_create_1', {}).get('status')} data={safe_json(findings.get('key_create_1', {}).get('data'))[:300]}")
    log(f"resolved_api_key={'有' if api_key else '无'}")
    log(f"api_call_models: {safe_json(findings.get('api_call_models', {}))[:300]}")
    log(f"api_call_chat: {safe_json(findings.get('api_call_chat', {}))[:400]}")


if __name__ == "__main__":
    main()
