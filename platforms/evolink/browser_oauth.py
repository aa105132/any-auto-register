"""Evolink Google OAuth 自动化。"""
from __future__ import annotations

import json
import time

import requests
from typing import Any

from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page
from core.google_oauth import drive_google_oauth

SITE_URL = "https://evolink.ai/"
SIGNUP_URL = "https://evolink.ai/signup"
KEYS_URL = "https://evolink.ai/dashboard/keys"
WEB_API_BASE = "https://api.evolink.ai/web/api"
LLM_API_BASE = "https://api.evolink.ai/v1"


def _click_by_text(page, words: list[str], *, exclude: list[str] | None = None) -> str:
    try:
        return str(page.evaluate(
            """
            ({words, exclude}) => {
              const blocked = (exclude || []).map(x => String(x).toLowerCase());
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const textOf = (n) => ((n.innerText||n.textContent||n.value||n.getAttribute('aria-label')||'').trim());
              const nodes = [...document.querySelectorAll('button,a,[role=button],input[type=submit],label')].filter(visible);
              const node = nodes.find(n => {
                const text = textOf(n);
                const lower = text.toLowerCase();
                if (blocked.some(x => lower.includes(x))) return false;
                return words.some(w => text === w || text.includes(w));
              });
              if (!node) return '';
              const label = textOf(node) || 'clicked';
              node.click();
              return label;
            }
            """,
            {"words": words, "exclude": exclude or []},
        ) or "").strip()
    except Exception:
        return ""


def _local_storage(page) -> dict[str, Any]:
    try:
        return dict(page.evaluate("""() => Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)]))""") or {})
    except Exception:
        return {}


def _google_prompts(browser: OAuthBrowser, *, timeout: int = 120, log_fn=print) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _evolink_oauth_done(browser):
            return
        for page in browser.pages():
            if page.is_closed() or "accounts.google.com" not in (page.url or ""):
                continue
            clicked = _click_by_text(page, ["Continue", "继续", "Allow", "允许", "Next", "下一步", "I understand", "我了解"])
            if clicked:
                log_fn(f"[Evolink] 点击 Google 提示: {clicked}")
                time.sleep(3)
        time.sleep(1)


def _extract_email_from_firebase(local: dict[str, Any]) -> str:
    for key, value in local.items():
        if key.startswith("firebase:authUser") and isinstance(value, str):
            try:
                data = json.loads(value)
                email = str(data.get("email") or "").strip()
                if email:
                    return email
            except Exception:
                pass
    return ""


def _create_key_http(token: str, *, name: str, proxy: str | None = None) -> dict[str, Any]:
    """协议创建 Evolink API key；OAuth 后不依赖页面 fetch。"""
    if not token:
        return {"ok": False, "reason": "missing_routerapi_token", "data": {}}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    payload = {
        "name": name,
        "models": ["evolink/auto"],
        "quota": 0,
        "unlimitedQuota": True,
        "rateLimit": 0,
        "ipWhitelist": [],
        "expiresAt": None,
        "dailyQuota": 0,
        "dailyResetTimezone": "America/Los_Angeles",
    }
    try:
        response = requests.post(
            f"{WEB_API_BASE}/keys",
            json=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
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
            f"{LLM_API_BASE}/models",
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


def _find_key(data: Any) -> str:
    if isinstance(data, dict):
        for k in ("key", "apiKey", "api_key", "token"):
            if data.get(k):
                return str(data[k]).strip()
        for v in data.values():
            found = _find_key(v)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_key(item)
            if found:
                return found
    return ""


def _start_google_oauth(page, *, log_fn=print) -> None:
    """启动 Evolink Google OAuth。

    Evolink 使用 Firebase signInWithPopup；必须显式等待 popup，
    否则主页面停在 Connecting，driver 可能看不到真正的 Google 页。
    """
    for label in ("Sign up with Google", "Continue with Google", "Google"):
        try:
            with page.expect_popup(timeout=15000) as popup_info:
                page.get_by_role("button", name=label).click(timeout=8000)
            popup = popup_info.value
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            log_fn(f"[Evolink] opened Google OAuth popup: {popup.url}")
            return
        except Exception:
            pass
    before = set(page.context.pages)
    if try_click_provider_on_page(page, "google"):
        deadline = time.time() + 15
        while time.time() < deadline:
            new_pages = [p for p in page.context.pages if p not in before and not p.is_closed()]
            google_pages = [p for p in page.context.pages if not p.is_closed() and "accounts.google.com" in str(p.url or "")]
            if google_pages:
                log_fn(f"[Evolink] opened Google OAuth popup: {google_pages[-1].url}")
                return
            if new_pages:
                try:
                    new_pages[-1].wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                log_fn(f"[Evolink] opened OAuth popup: {new_pages[-1].url}")
                return
            time.sleep(0.5)
        log_fn("[Evolink] clicked Google OAuth provider")
        return
    clicked = _click_by_text(page, ["Sign up with Google", "Continue with Google", "Google"], exclude=["Terms", "Privacy"])
    if clicked:
        log_fn(f"[Evolink] clicked Google OAuth button: {clicked}")

def _extract_router_token(local: dict[str, Any]) -> tuple[str, str]:
    direct = str(local.get("routerapi_token") or local.get("routerapiToken") or "").strip()
    refresh = str(local.get("routerapi_token_refresh") or local.get("routerapiRefreshToken") or "").strip()
    if direct:
        return direct, refresh
    for value in local.values():
        if not isinstance(value, str):
            continue
        try:
            data = json.loads(value)
        except Exception:
            continue
        if isinstance(data, dict):
            token = str(data.get("routerapi_token") or data.get("routerapiToken") or data.get("accessToken") or "").strip()
            ref = str(data.get("routerapi_token_refresh") or data.get("refreshToken") or "").strip()
            if token:
                return token, ref
    return "", refresh


def _has_evolink_auth_state(page) -> bool:
    if page.is_closed() or "evolink.ai" not in str(page.url or ""):
        return False
    try:
        local = _local_storage(page)
    except Exception:
        return False
    if _extract_router_token(local)[0]:
        return True
    return any(str(key).startswith("firebase:authUser") for key in local.keys())


def _evolink_oauth_done(browser: OAuthBrowser) -> bool:
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        if "evolink.ai" in url and "dashboard" in url:
            return True
        if _has_evolink_auth_state(page):
            return True
    return False



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
        raise RuntimeError("Evolink 当前只支持 Google OAuth 自动化")

    with OAuthBrowser(proxy=proxy, headless=headless, chrome_user_data_dir=chrome_user_data_dir, chrome_cdp_url=chrome_cdp_url, log_fn=log_fn) as browser:
        page = browser.new_page()
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        _start_google_oauth(page, log_fn=log_fn)
        drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=_evolink_oauth_done,
        )
        if chrome_cdp_url or chrome_user_data_dir:
            browser.auto_select_google_account(timeout=8)
        _google_prompts(browser, timeout=min(timeout, 60), log_fn=log_fn)
        dash_page = next((p for p in browser.pages() if _has_evolink_auth_state(p)), None) or next((p for p in browser.pages() if not p.is_closed() and "evolink.ai" in (p.url or "")), page)
        local = _local_storage(dash_page)
        if not _extract_router_token(local)[0]:
            try:
                dash_page.goto(KEYS_URL, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)
            except Exception:
                pass
            local = _local_storage(dash_page)
        router_token, router_refresh = _extract_router_token(local)
        if not router_token:
            raise RuntimeError("Evolink OAuth 未拿到 routerapi_token")
        result = _create_key_http(router_token, name=f"auto-register-save-{int(time.time())}", proxy=proxy)
        if not result.get("ok"):
            raise RuntimeError(f"Evolink 协议创建 API Key 失败: {result}")
        raw_key = _find_key(result.get("data"))
        if not raw_key:
            raise RuntimeError(f"Evolink 创建 API Key 后未找到 key: {result}")
        api_key = raw_key if raw_key.startswith("sk-") else f"sk-{raw_key}"
        cookies = browser.cookie_dict(domain_substrings=("evolink.ai",))
        actual_email = _extract_email_from_firebase(local)

    return {
        "email": finalize_oauth_email(actual_email, email_hint, "Evolink"),
        "api_key": api_key,
        "raw_key": raw_key,
        "api_key_info": result.get("data") or {},
        "api_verification": _verify_api_key_http(api_key, proxy=proxy),
        "key_create_result": result,
        "routerapi_token": router_token,
        "routerapi_token_refresh": router_refresh,
        "firebase_auth": {k: v for k, v in local.items() if k.startswith("firebase:authUser")},
        "cookies": cookies,
        "api_base": LLM_API_BASE,
        "site_url": SITE_URL,
    }
