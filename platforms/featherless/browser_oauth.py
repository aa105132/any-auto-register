"""Featherless Google OAuth 注册与 API Key 创建。"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, finalize_oauth_email
from platforms.featherless.protocol_mailbox import (
    API_ORIGIN,
    DASHBOARD_URL,
    LLM_API_BASE,
    SITE_URL,
    _cookie_session,
    _create_api_key_http,
    _get_me_http,
    _verify_api_key_http,
)

GOOGLE_OAUTH_URL = "https://api.featherless.ai/auth/google/callback"


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    try:
        return browser.cookie_dict(domain_substrings=("featherless.ai",))
    except Exception:
        return {}


def _session_from_browser_cookies(cookies: dict[str, str], *, proxy: str | None = None):
    return _cookie_session(cookies, proxy=proxy)


def _get_me_from_browser(browser: OAuthBrowser, *, proxy: str | None = None) -> dict[str, Any]:
    cookies = _cookie_map(browser)
    if not cookies:
        return {"ok": False, "reason": "missing_cookies", "cookies": {}}
    session = _session_from_browser_cookies(cookies, proxy=proxy)
    result = _get_me_http(session)
    result["cookies"] = cookies
    return result


def _is_featherless_account_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    host = str(parsed.hostname or "").lower()
    if host != "featherless.ai" and not host.endswith(".featherless.ai"):
        return False
    return "/account" in str(parsed.path or "").lower()


def _is_authenticated(browser: OAuthBrowser, *, proxy: str | None = None) -> bool:
    result = _get_me_from_browser(browser, proxy=proxy)
    if result.get("ok"):
        return True
    return any(
        _is_featherless_account_url(str(page.url or ""))
        for page in browser.pages()
        if not page.is_closed()
    )


def _open_google_oauth(page, *, state: str = "", log_fn=print) -> None:
    url = GOOGLE_OAUTH_URL
    if state:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}state={state}"
    log_fn(f"[Featherless] 打开 Google OAuth: {url}")
    page.goto(url, wait_until="commit", timeout=90000)


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    google_password: str = "",
    timeout: int = 300,
    log_fn=print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    reuse_existing_cdp: bool = False,
    key_name: str = "auto-register",
    verify_deep: bool = False,
) -> dict:
    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("Featherless 当前只支持 Google OAuth 自动化")

    with OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        reuse_existing_cdp=reuse_existing_cdp,
        log_fn=log_fn,
    ) as browser:
        page = browser.new_page()
        _open_google_oauth(page, log_fn=log_fn)
        google_result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=lambda b: _is_authenticated(b, proxy=proxy),
        )
        if getattr(google_result, "blocked_on_password", False):
            raise RuntimeError(f"Featherless Google OAuth 未完成: {google_result.last_url} :: {google_result.last_body[:300]}")
        if chrome_cdp_url or chrome_user_data_dir or reuse_existing_cdp:
            browser.auto_select_google_account(timeout=8)

        deadline = time.time() + max(30, min(timeout, 120))
        me_result: dict[str, Any] = {}
        cookies: dict[str, str] = {}
        while time.time() < deadline:
            me_result = _get_me_from_browser(browser, proxy=proxy)
            cookies = dict(me_result.get("cookies") or _cookie_map(browser))
            if me_result.get("ok"):
                break
            time.sleep(1)
        if not me_result.get("ok"):
            snapshot = google_oauth_snapshot(browser)
            raise RuntimeError(f"Featherless OAuth 登录后未拿到 /auth/me: cookies={sorted(cookies.keys())}, pages={snapshot}")

        user = dict(me_result.get("user") or {})
        actual_email = str(user.get("email") or "").strip()
        final_email = finalize_oauth_email(actual_email, email_hint, "Featherless")
        session = _session_from_browser_cookies(cookies, proxy=proxy)
        # 用同一个 cookie session 再打一次 /auth/me，确认后续控制面 API 能复用。
        session_me_result = _get_me_http(session)
        if session_me_result.get("ok"):
            me_result = session_me_result
            user = dict(me_result.get("user") or user)
        create_result = _create_api_key_http(session, name=key_name)
        if not create_result.get("ok"):
            raise RuntimeError(f"Featherless 创建 API Key 失败: {create_result}")
        api_key = str(create_result.get("api_key") or "").strip()
        api_verification = _verify_api_key_http(api_key, proxy=proxy, deep=verify_deep)
        final_cookies = dict(cookies or {})

    return {
        "email": final_email,
        "password": "",
        "user": user,
        "api_key": api_key,
        "api_key_info": dict(create_result.get("api_key_info") or {}),
        "api_verification": api_verification,
        "key_create_result": create_result.get("result") or create_result,
        "me": me_result,
        "session": user,
        "cookies": final_cookies,
        "cookie_header": "; ".join(f"{name}={value}" for name, value in final_cookies.items() if value),
        "auth_method": "google_oauth",
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "api_base": LLM_API_BASE,
        "control_api_base": API_ORIGIN,
    }
