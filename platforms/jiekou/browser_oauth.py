"""Jiekou AI Google OAuth 注册与 API Key 创建。"""
from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse

from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page
from platforms.jiekou.protocol_mailbox import (
    DASHBOARD_URL,
    LOGIN_URL,
    HIGHWAY_API_BASE,
    OPENAI_API_BASE,
    OPENAI_COMPAT_API_BASE,
    OPENAI_COMPAT_V1_API_BASE,
    SITE_URL,
    _build_session,
    _create_api_key_http,
    _get_user_info_http,
    _import_cookies,
    _submit_questionnaire_http,
    _verify_api_key_http,
    _verify_voucher_reward,
)

GOOGLE_CLIENT_ID = "674520143921-el37tpuei0pidio6bsligf746cdflort.apps.googleusercontent.com"
GOOGLE_OAUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH_CALLBACK_PATH = "/api/auth"
GITHUB_BIND_CALLBACK_PATH = "/api/auth/bind-github"
GITHUB_CLIENT_ID = "Ov23liVJjffZ0aP2BmRU"


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    try:
        return browser.cookie_dict(domain_substrings=("jiekou.ai",))
    except Exception:
        return {}


def _wait_for_browser_token(browser: OAuthBrowser, *, timeout: int = 180) -> dict[str, Any]:
    deadline = time.time() + max(10, timeout)
    last_cookies: dict[str, str] = {}
    while time.time() < deadline:
        cookies = _cookie_map(browser)
        last_cookies = cookies
        token = str(cookies.get("token") or "").strip()
        if token:
            return {"ok": True, "token": token, "cookies": cookies}
        time.sleep(1)
    return {"ok": False, "token": "", "cookies": last_cookies}


def _build_google_oauth_url(*, state: str = "") -> str:
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": f"{SITE_URL}{OAUTH_CALLBACK_PATH}",
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "online",
        "prompt": "select_account",
        "state": state or f"auto-{secrets.token_hex(8)}",
    }
    return f"{GOOGLE_OAUTH_URL}?{urlencode(params)}"


def build_github_bind_url(*, state: str = "", redirect_uri: str | None = None) -> str:
    """构造 GitHub 绑定 URL，仅用于注册后人工/后续可逆探测。"""
    params = {
        "scope": "user:email",
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": redirect_uri or f"{SITE_URL}{GITHUB_BIND_CALLBACK_PATH}",
    }
    if state:
        params["state"] = state
    return f"https://github.com/login/oauth/authorize?{urlencode(params)}"


def _is_authenticated(browser: OAuthBrowser) -> bool:
    token_result = _wait_for_browser_token(browser, timeout=1)
    if token_result.get("token"):
        return True
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        if "jiekou.ai" in url and ("auth_res=success" in url or "/settings" in url or "/console" in url):
            return True
    return False


def _extract_email_from_page(browser: OAuthBrowser) -> str:
    for page in browser.pages():
        if page.is_closed() or "jiekou.ai" not in str(page.url or ""):
            continue
        try:
            value = str(page.evaluate("() => document.body.innerText.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i)?.[0] || ''") or "").strip()
            if value:
                return value
        except Exception:
            continue
    return ""


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
    questionnaire_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("Jiekou 当前只支持 Google OAuth 自动化")

    oauth_url = _build_google_oauth_url()
    with OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        reuse_existing_cdp=reuse_existing_cdp,
        log_fn=log_fn,
    ) as browser:
        page = browser.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        clicked = try_click_provider_on_page(page, "google")
        if not clicked:
            page.goto(oauth_url, wait_until="commit", timeout=60000)
        google_result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=lambda b: _is_authenticated(b),
        )
        if getattr(google_result, "blocked_on_password", False):
            raise RuntimeError(f"Jiekou Google OAuth 未完成: {google_result.last_url} :: {google_result.last_body[:300]}")
        if chrome_cdp_url or chrome_user_data_dir or reuse_existing_cdp:
            browser.auto_select_google_account(timeout=8)
        token_result = _wait_for_browser_token(browser, timeout=min(timeout, 180))
        if not token_result.get("token"):
            snapshot = google_oauth_snapshot(browser)
            raise RuntimeError(f"Jiekou OAuth 登录后未拿到 token cookie: cookies={sorted((token_result.get('cookies') or {}).keys())}, pages={snapshot}")
        token = str(token_result.get("token") or "").strip()
        cookies = dict(token_result.get("cookies") or {})
        browser_email = _extract_email_from_page(browser)

    session = _build_session(proxy)
    _import_cookies(session, cookies)
    user_info = _get_user_info_http(session, token)
    user = dict(user_info.get("user") or {})
    actual_email = finalize_oauth_email(str(user.get("email") or browser_email or ""), email_hint, "Jiekou")

    questionnaire_result = _submit_questionnaire_http(session, token, payload=questionnaire_payload)
    if not questionnaire_result.get("ok"):
        raise RuntimeError(f"Jiekou OAuth 问卷提交失败: {questionnaire_result}")
    voucher_result = _verify_voucher_reward(session, token, log_fn=log_fn)
    if not voucher_result.get("ok"):
        raise RuntimeError(f"Jiekou OAuth 未确认  体验券到账: {voucher_result}")
    create_result = _create_api_key_http(session, token, key_name=key_name)
    if not create_result.get("ok"):
        raise RuntimeError(f"Jiekou OAuth 创建 API Key 失败: {create_result}")
    api_key = str(create_result.get("api_key") or "").strip()
    api_verification = _verify_api_key_http(api_key, proxy=proxy)

    return {
        "email": actual_email,
        "password": "",
        "auth_method": "google_oauth",
        "oauth_provider": "google",
        "user": user,
        "user_info": user_info,
        "api_key": api_key,
        "api_key_info": dict(create_result.get("api_key_info") or {}),
        "api_verification": api_verification,
        "key_create_result": create_result.get("result") or create_result,
        "questionnaire_result": questionnaire_result,
        "voucher_result": voucher_result,
        "point_info": voucher_result.get("point_info") or {},
        "balance_total": voucher_result.get("balance_total") or {},
        "voucher_num": voucher_result.get("voucher_num") or {},
        "voucher_list": voucher_result.get("voucher_list") or {},
        "session": {"token": token, "user": user},
        "cookies": cookies,
        "cookie_header": "; ".join(f"{name}={value}" for name, value in cookies.items() if value),
        "github_bind_supported": True,
        "github_bind_url": build_github_bind_url(),
        "github_bind_callback_path": GITHUB_BIND_CALLBACK_PATH,
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "api_base": OPENAI_COMPAT_V1_API_BASE,
        "legacy_api_base": OPENAI_API_BASE,
        "openai_compatible_api_base": OPENAI_COMPAT_API_BASE,
        "openai_compatible_v1_api_base": OPENAI_COMPAT_V1_API_BASE,
        "direct_api_base": HIGHWAY_API_BASE,
    }
