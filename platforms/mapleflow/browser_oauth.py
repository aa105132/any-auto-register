"""MapleFlow Google OAuth 自动化。"""
from __future__ import annotations

import json
import time
from typing import Any

import requests

from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, finalize_oauth_email

SITE_URL = "https://mapleflow.io/"
DASHBOARD_URL = "https://mapleflow.io/dashboard/"
GOOGLE_OAUTH_URL = "https://api.mapleflow.io/auth/google"
API_BASE = "https://api.mapleflow.io"
LLM_API_BASE = "https://api.mapleflow.io"


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    return browser.cookie_dict(domain_substrings=("mapleflow.io", "api.mapleflow.io"))


def _cookie_session(cookies: dict[str, str], *, proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="api.mapleflow.io")
        session.cookies.set(name, value, domain="mapleflow.io")
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _get_me_http(cookies: dict[str, str], *, proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies, proxy=proxy)
    try:
        response = session.get(f"{API_BASE}/auth/me", timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:2000]}
        return {"ok": response.ok, "status": response.status_code, "data": data}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _create_api_key_http(cookies: dict[str, str], *, name: str, proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies, proxy=proxy)
    try:
        response = session.post(
            f"{API_BASE}/auth/keys/create",
            json={"name": name},
            headers={"Content-Type": "application/json", "Origin": SITE_URL.rstrip('/'), "Referer": DASHBOARD_URL},
            timeout=30,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:2000]}
        return {"ok": response.ok, "status": response.status_code, "data": data}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _find_api_key(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("api_key", "apiKey", "key", "token"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            found = _find_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_api_key(item)
            if found:
                return found
    return ""


def _verify_api_key_http(api_key: str, *, proxy: str | None = None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "reason": "missing_api_key"}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    for path in ("/account", "/ai/models", "/models"):
        try:
            response = requests.get(
                f"{API_BASE}{path}",
                headers={"X-API-Key": api_key},
                proxies=proxies,
                timeout=30,
            )
            try:
                body = response.json()
            except Exception:
                body = {"raw": response.text[:2000]}
            if response.ok:
                return {"ok": True, "status": response.status_code, "path": path, "body": body}
            last = {"ok": False, "status": response.status_code, "path": path, "body": body}
        except Exception as exc:
            last = {"ok": False, "path": path, "error": repr(exc)}
    return last


def _oauth_done(browser: OAuthBrowser) -> bool:
    cookies = _cookie_map(browser)
    if cookies:
        me = _get_me_http(cookies)
        return bool(me.get("ok") and (me.get("data") or {}).get("success", True))
    return any(
        "mapleflow.io" in (page.url or "") and "accounts.google.com" not in (page.url or "") and "auth/google" not in (page.url or "")
        for page in browser.pages() if not page.is_closed()
    )


def _extract_email(me: dict[str, Any], fallback: str) -> str:
    data = me.get("data") if isinstance(me, dict) else {}
    if isinstance(data, dict):
        user = data.get("data") if isinstance(data.get("data"), dict) else data
        for key in ("email", "user_email"):
            if user.get(key):
                return str(user.get(key)).strip()
    return fallback


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    timeout: int = 300,
    log_fn=print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    google_password: str = "",
) -> dict:
    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("MapleFlow 当前只支持 Google OAuth 自动化")

    with OAuthBrowser(proxy=proxy, headless=headless, chrome_user_data_dir=chrome_user_data_dir, chrome_cdp_url=chrome_cdp_url, log_fn=log_fn) as browser:
        page = browser.new_page()
        page.goto(GOOGLE_OAUTH_URL, wait_until="commit", timeout=90000)
        time.sleep(2)
        drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 220),
            log_fn=log_fn,
            stop_when=_oauth_done,
        )
        deadline = time.time() + max(30, timeout)
        cookies = _cookie_map(browser)
        me = _get_me_http(cookies, proxy=proxy) if cookies else {"ok": False}
        while time.time() < deadline and not me.get("ok"):
            cookies = _cookie_map(browser)
            if cookies:
                me = _get_me_http(cookies, proxy=proxy)
                if me.get("ok"):
                    break
            time.sleep(1)
        if not me.get("ok"):
            snapshot = google_oauth_snapshot(browser)
            if any("输入您听到或看到的文字" in item.get("body", "") or "Enter the text" in item.get("body", "") for item in snapshot):
                raise RuntimeError("MapleFlow Google OAuth 遇到验证码；请使用已有 CDP 登录态或换账号/节点后重试")
            raise RuntimeError(f"MapleFlow OAuth 后协议 /auth/me 未通过: {me}")

        create_result = _create_api_key_http(cookies, name=f"auto-register-{int(time.time())}", proxy=proxy)
        if not create_result.get("ok"):
            raise RuntimeError(f"MapleFlow 协议创建 API Key 失败: {create_result}")
        api_key = _find_api_key(create_result.get("data"))
        if not api_key:
            raise RuntimeError(f"MapleFlow 创建 API Key 后未返回 key: {create_result}")
        api_verification = _verify_api_key_http(api_key, proxy=proxy)

    return {
        "email": finalize_oauth_email(_extract_email(me, email_hint), email_hint, "MapleFlow"),
        "api_key": api_key,
        "api_key_info": create_result.get("data") or {},
        "api_verification": api_verification,
        "key_create_result": create_result,
        "me": me.get("data") or {},
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "api_base": LLM_API_BASE,
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "auth_url": GOOGLE_OAUTH_URL,
    }
