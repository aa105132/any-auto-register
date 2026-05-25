from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.base_platform import Account, AccountStatus
from core.db import save_account
from core.google_account_pool import GoogleAccountPool
from platforms.zo.browser_oauth import register_with_browser_oauth
from platforms.zo.core import API_BASE, mask_card_info, resolve_card_info, sanitize_sensitive

OUT_DIR = ROOT / "output"
OUT_DIR.mkdir(exist_ok=True)
RESULTS_PATH = OUT_DIR / "zo_e2e_result.json"
ERRORS_PATH = OUT_DIR / "zo_e2e_errors.jsonl"
PROXY_URLS_PATH = OUT_DIR / "zo_proxy_urls.txt"
OPENAI_PROXY_URLS_PATH = OUT_DIR / "openai_proxy_urls.txt"



def _mask_email(email: str) -> str:
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    return f"{local[:2]}***@{domain}"


def _preview_key(key: str) -> str:
    if len(key) <= 12:
        return "***" if key else ""
    return f"{key[:8]}...{key[-4:]}"


def _write_key_files(email: str, api_key: str) -> None:
    if not api_key:
        return
    for name in ("zo_keys.txt", "keys.txt", "ai_api_tokens.txt"):
        path = OUT_DIR / name
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines = [line for line in lines if not (line.startswith("zo|") and f"|{email}|" in line)]
        lines.append(f"zo|{email}|{api_key}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")




def _write_deduped_line(path: Path, email: str, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [item for item in lines if not (item.startswith("zo|") and f"|{email}|" in item)]
    lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

def _write_proxy_files(email: str, api_key: str, proxy_result: dict[str, Any]) -> None:
    """保存 Zo Space OpenAI 兼容代理地址；URL 内保留完整 Zo key 供客户端直接使用。"""
    if not email or not api_key or not proxy_result.get("ok"):
        return
    base_url = str(proxy_result.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return
    _write_deduped_line(
        PROXY_URLS_PATH,
        email,
        f"zo|{email}|{base_url}|zo_api_key={api_key}|api_key=dummy",
    )
    _write_deduped_line(
        OPENAI_PROXY_URLS_PATH,
        email,
        f"zo|{email}|{base_url}|api_key=dummy",
    )


def _extract_workspace_from_record(record: dict[str, Any]) -> tuple[str, str]:
    workspace_result = dict(record.get("workspace_result") or {})
    workspace = dict(workspace_result.get("workspace") or {})
    handle = str(workspace.get("handle") or workspace_result.get("handle") or "").strip()
    origin = str(
        workspace.get("origin")
        or workspace.get("url")
        or workspace_result.get("workspace_origin")
        or workspace_result.get("workspace_url")
        or ""
    ).strip().rstrip("/")
    if not origin and handle:
        origin = f"https://{handle}.zo.computer"
    return handle, origin


def _deploy_proxy_for_record(
    record: dict[str, Any],
    *,
    timeout: int = 180,
    verify_chat: bool = True,
    log_fn=print,
) -> dict[str, Any]:
    from scripts.deploy_zo_openai_proxy import DEFAULT_SOURCE_DIR, ZoProxyDeployContext, deploy_proxy

    email = str(record.get("email") or "").strip()
    api_key = str(record.get("api_key") or "").strip()
    cookies = dict(record.get("cookies") or {})
    handle, origin = _extract_workspace_from_record(record)
    ctx = ZoProxyDeployContext(
        result_path=RESULTS_PATH,
        handle=handle,
        workspace_origin=origin,
        access_token=str(cookies.get("access_token") or ""),
        refresh_token=str(cookies.get("refresh_token") or ""),
        api_key=api_key,
        cookies=cookies,
    )
    log_fn("[ZoProxy] 开始快速部署 OpenAI 兼容代理到 zo.space")
    proxy_result = deploy_proxy(
        ctx,
        DEFAULT_SOURCE_DIR,
        verify=True,
        verify_chat=verify_chat,
        timeout=float(timeout),
    )
    _write_proxy_files(email, api_key, proxy_result)
    log_fn(f"[ZoProxy] 部署完成: ok={bool(proxy_result.get('ok'))} base={proxy_result.get('base_url_preview') or ''}")
    return proxy_result


def _save_record(record: dict[str, Any]) -> None:
    safe = sanitize_sensitive(record)
    RESULTS_PATH.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    email = str(record.get("email") or "")
    api_key = str(record.get("api_key") or "")
    if api_key:
        _write_key_files(email, api_key)
        save_account(Account(platform="zo", email=email, password="", token=api_key, status=AccountStatus.REGISTERED, extra=safe))


def run_one(*, timeout: int, headless: bool, email: str = "", password: str = "", deploy_proxy_after: bool = True, require_proxy_deploy: bool = False, proxy_timeout: int = 180, proxy_verify_chat: bool = True) -> dict[str, Any]:
    pool = GoogleAccountPool()
    reserved = None
    if email:
        reserved = pool.get_by_email(email, exclude_platforms=["zo"])
        if reserved is None:
            raise RuntimeError(f"指定 Google 账号不可用或已被 Zo 占用: {_mask_email(email)}")
    else:
        reserved = pool.acquire(exclude_platforms=["zo"])
    if reserved is None:
        raise RuntimeError("Google 账号池没有可用于 Zo 的账号")
    email = reserved.email
    password = password or reserved.password
    profile = OUT_DIR / f"chrome_zo_e2e_{email.replace('@', '_at_').replace('.', '_')}"
    logs: list[str] = []

    def log(message: Any) -> None:
        text = sanitize_sensitive(str(message))
        logs.append(str(text))
        print(text, flush=True)

    try:
        result = register_with_browser_oauth(
            email_hint=email,
            google_password=password,
            oauth_provider="google",
            timeout=timeout,
            log_fn=log,
            headless=headless,
            chrome_user_data_dir=str(profile),
            extra={"zo_min_credit": 100.0},
        )
        api_key = str(result.get("api_key") or "").strip()
        card = mask_card_info(resolve_card_info({}))
        record = {
            "ok": bool(api_key and (result.get("credit_result") or {}).get("ok") and (result.get("card_binding_result") or {}).get("ok")),
            "saved_at": int(time.time()),
            "platform": "zo",
            "email": str(result.get("email") or email),
            "api_key": api_key,
            "api_key_preview": _preview_key(api_key),
            "api_base": API_BASE,
            "api_key_info": result.get("api_key_info") or {},
            "api_verification": result.get("api_verification") or {},
            "key_create_result": result.get("key_create_result") or {},
            "coupon_result": result.get("coupon_result") or {},
            "credit_result": result.get("credit_result") or {},
            "card_binding_result": result.get("card_binding_result") or {},
            "workspace_result": result.get("workspace_result") or {},
            "card": card,
            "cookies": result.get("cookies") or {},
            "logs_tail": logs[-80:],
        }
        if record["ok"] and deploy_proxy_after:
            try:
                proxy_result = _deploy_proxy_for_record(
                    record,
                    timeout=proxy_timeout,
                    verify_chat=proxy_verify_chat,
                    log_fn=log,
                )
                record["proxy_deploy_result"] = proxy_result
                record["openai_proxy_base_url"] = str(proxy_result.get("base_url") or "")
                record["openai_proxy_api_key"] = "dummy"
                record["openai_proxy_models_url"] = str(proxy_result.get("models_url") or "")
                record["openai_proxy_chat_url"] = str(proxy_result.get("chat_url") or "")
                record["proxy_deployed"] = bool(proxy_result.get("ok"))
                if require_proxy_deploy and not proxy_result.get("ok"):
                    record["ok"] = False
                    raise RuntimeError(f"Zo proxy 快速部署校验失败: {proxy_result}")
            except Exception as deploy_exc:
                record["proxy_deploy_result"] = {"ok": False, "error": repr(deploy_exc)}
                record["proxy_deployed"] = False
                log(f"[ZoProxy] 快速部署失败: {deploy_exc!r}")
                if require_proxy_deploy:
                    record["ok"] = False
                    _save_record(record)
                    raise
        _save_record(record)
        if record["ok"]:
            pool.mark_registered(email, "zo")
        else:
            pool.release(email, "zo")
        return record
    except Exception as exc:
        pool.release(email, "zo")
        error = {"ok": False, "platform": "zo", "email": email, "error": repr(sanitize_sensitive(str(exc))), "logs_tail": logs[-80:]}
        with ERRORS_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(error, ensure_ascii=False) + "\n")
        raise


def _print_summary(record: dict[str, Any]) -> None:
    print(json.dumps({
        "ok": record.get("ok"),
        "email": _mask_email(str(record.get("email") or "")),
        "api_key_preview": record.get("api_key_preview"),
        "credit_ok": (record.get("credit_result") or {}).get("ok"),
        "credit_amount": (record.get("credit_result") or {}).get("amount"),
        "card_ok": (record.get("card_binding_result") or {}).get("ok"),
        "proxy_deployed": record.get("proxy_deployed"),
        "proxy_base_url_preview": (record.get("proxy_deploy_result") or {}).get("base_url_preview"),
        "proxy_urls_path": str(PROXY_URLS_PATH),
        "card": record.get("card"),
        "result_path": str(RESULTS_PATH),
    }, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--skip-proxy-deploy", action="store_true", help="只注册和取 Zo API key，不自动部署 zo.space OpenAI 代理")
    parser.add_argument("--require-proxy-deploy", action="store_true", help="代理部署或校验失败时，本次注册视为失败")
    parser.add_argument("--proxy-timeout", type=int, default=180)
    parser.add_argument("--skip-proxy-chat-verify", action="store_true", help="只校验 /models，不实际请求 /chat/completions")
    parser.add_argument("--attempts", type=int, default=1, help="未指定邮箱时，Google 账号失效可自动换号重试次数")
    args = parser.parse_args()
    attempts = 1 if args.email else max(1, int(args.attempts or 1))
    last_error: Exception | None = None
    for index in range(attempts):
        try:
            if attempts > 1:
                print(f"[ZoE2E] attempt {index + 1}/{attempts}", flush=True)
            record = run_one(
                timeout=args.timeout,
                headless=args.headless,
                email=args.email,
                password=args.password,
                deploy_proxy_after=not args.skip_proxy_deploy,
                require_proxy_deploy=args.require_proxy_deploy,
                proxy_timeout=args.proxy_timeout,
                proxy_verify_chat=not args.skip_proxy_chat_verify,
            )
            _print_summary(record)
            return
        except Exception as exc:
            last_error = exc
            message = str(exc)
            retryable = (not args.email) and (
                "Google OAuth 账号已被删除" in message
                or "google_account_deleted" in message
                or "contact your domain administrator" in message.lower()
            )
            if not retryable or index + 1 >= attempts:
                raise
            print(f"[ZoE2E] 当前 Google 账号不可用，换下一个: {sanitize_sensitive(message)[:300]}", flush=True)
    if last_error:
        raise last_error


if __name__ == "__main__":
    main()
