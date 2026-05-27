"""AnyAPI Google OAuth 自动化。"""
from __future__ import annotations

import time
from typing import Any

import requests

from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page
from core.google_oauth import drive_google_oauth

SITE_URL = "https://anyapi.ai/"
DASHBOARD_URL = "https://dash.anyapi.ai/"
SIGN_IN_URL = "https://dash.anyapi.ai/sign-in"
KEYS_URL = "https://dash.anyapi.ai/?page=api-keys"
API_BASE = "https://api.anyapi.ai/v1"
KEY_GENERATE_URL = "https://dash.anyapi.ai/api/key/generate"


def _click_google_prompts(browser: OAuthBrowser, *, timeout: int = 90, log_fn=print) -> bool:
    deadline = time.time() + timeout
    clicked_any = False
    while time.time() < deadline:
        for page in browser.pages():
            if page.is_closed():
                continue
            url = page.url or ""
            try:
                if "accounts.google.com" in url:
                    clicked = page.evaluate(
                        """
                        () => {
                          const words = ['Continue','继续','Allow','允许','Next','下一步','I understand','我了解'];
                          const nodes = [...document.querySelectorAll('button,input[type=submit],div[role=button]')];
                          const node = nodes.find(n => words.some(w =>
                            ((n.innerText||n.textContent||n.value||n.getAttribute('aria-label')||'').includes(w))
                          ));
                          if (node) { node.click(); return (node.innerText||node.textContent||node.value||node.getAttribute('aria-label')||'clicked'); }
                          return '';
                        }
                        """
                    )
                    if clicked:
                        clicked_any = True
                        log_fn(f"[AnyAPI] 点击 Google 授权提示: {clicked}")
                        time.sleep(3)
                        continue
                if "dash.anyapi.ai" in url and "sign-in" in url:
                    if try_click_provider_on_page(page, "google"):
                        clicked_any = True
                        log_fn("[AnyAPI] 点击 Continue with Google")
                        time.sleep(4)
            except Exception:
                pass
        if any("dash.anyapi.ai" in (p.url or "") and "sign-in" not in (p.url or "") for p in browser.pages() if not p.is_closed()):
            return True
        time.sleep(0.8)
    return clicked_any


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    return browser.cookie_dict(domain_substrings=("anyapi.ai", "dash.anyapi.ai"))


def _create_api_key_http(cookies: dict[str, str], *, alias: str, proxy: str | None = None) -> dict[str, Any]:
    """协议创建 AnyAPI key；OAuth 之后不再依赖页面 fetch。"""
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="dash.anyapi.ai")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    payload = {
        "models": ["basic"],
        "key_alias": alias,
        "duration": None,
        "key_type": "default",
    }
    try:
        response = session.post(
            KEY_GENERATE_URL,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://dash.anyapi.ai",
                "Referer": KEYS_URL,
            },
            proxies=proxies,
            timeout=30,
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:2000]}
        return {"ok": response.ok, "status": response.status_code, "data": data}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _verify_api_key_http(api_key: str, *, proxy: str | None = None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "reason": "missing_api_key"}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(
            f"{API_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            proxies=proxies,
            timeout=30,
        )
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:2000]}
        return {"ok": response.ok, "status": response.status_code, "body": body}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def _extract_logged_email(page) -> str:
    try:
        return str(page.evaluate("() => document.body.innerText.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i)?.[0] || ''") or "").strip()
    except Exception:
        return ""


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
        raise RuntimeError("AnyAPI 当前只支持 Google OAuth 自动化")

    with OAuthBrowser(proxy=proxy, headless=headless, chrome_user_data_dir=chrome_user_data_dir, chrome_cdp_url=chrome_cdp_url, log_fn=log_fn) as browser:
        page = browser.new_page()
        page.goto(SIGN_IN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        try_click_provider_on_page(page, "google")
        drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=lambda b: bool(_cookie_map(b).get("token")),
        )
        if chrome_cdp_url or chrome_user_data_dir:
            browser.auto_select_google_account(timeout=8)
        _click_google_prompts(browser, timeout=min(timeout, 60), log_fn=log_fn)

        deadline = time.time() + timeout
        dashboard_page = page
        while time.time() < deadline:
            for p in browser.pages():
                if p.is_closed():
                    continue
                if "dash.anyapi.ai" in (p.url or "") and "sign-in" not in (p.url or ""):
                    dashboard_page = p
                    break
            cookies = _cookie_map(browser)
            if cookies.get("token"):
                break
            time.sleep(1)
        else:
            raise RuntimeError("AnyAPI OAuth 登录超时，未拿到 dash.anyapi.ai token cookie")

        cookies = _cookie_map(browser)
        alias = f"auto-register-{int(time.time())}"
        result = _create_api_key_http(cookies, alias=alias, proxy=proxy)
        if not result.get("ok"):
            raise RuntimeError(f"AnyAPI 协议创建 API Key 失败: {result}")
        data = result.get("data") or {}
        api_key = str(data.get("key") or data.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError(f"AnyAPI 协议创建 API Key 后未返回 key: {data}")
        api_verification = _verify_api_key_http(api_key, proxy=proxy)
        actual_email = _extract_logged_email(dashboard_page)

    return {
        "email": finalize_oauth_email(actual_email, email_hint, "AnyAPI"),
        "api_key": api_key,
        "api_key_info": data,
        "api_verification": api_verification,
        "key_create_result": result,
        "dashboard_token": cookies.get("token", ""),
        "refresh_token": cookies.get("refresh_token", ""),
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "api_base": API_BASE,
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
    }
