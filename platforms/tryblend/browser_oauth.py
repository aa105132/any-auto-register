"""TryBlend Google OAuth 自动化。"""
from __future__ import annotations

import base64
import json
import time
from typing import Any

from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page
from core.google_oauth import drive_google_oauth

SITE_URL = "https://www.tryblend.ai/"
SIGN_UP_URL = "https://www.tryblend.ai/auth/sign-up"
SETTINGS_URL = "https://www.tryblend.ai/settings"


def _b64decode_supabase_cookie(value: str) -> dict[str, Any]:
    raw = (value or "").strip()
    if raw.startswith("base64-"):
        raw = raw[len("base64-"):]
    try:
        padding = "=" * (-len(raw) % 4)
        text = base64.urlsafe_b64decode((raw + padding).encode()).decode("utf-8", errors="replace")
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _supabase_session_from_cookies(cookies: dict[str, str]) -> dict[str, Any]:
    parts = []
    for name in sorted(cookies):
        if name.startswith("sb-") and "auth-token" in name:
            parts.append(cookies[name])
    joined = "".join(parts)
    return _b64decode_supabase_cookie(joined) if joined else {}


def _wait_for_google_page(browser: OAuthBrowser, *, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any((not p.is_closed()) and "accounts.google.com" in (p.url or "") for p in browser.pages()):
            return True
        time.sleep(0.5)
    return False


def _start_google_oauth(browser: OAuthBrowser, page, *, log_fn=print) -> None:
    # Prefer Playwright's trusted click on the real React button. DOM node.click()
    # can return true while React does not open the OAuth flow.
    for label in ("Sign up with Google", "Continue with Google", "Google"):
        try:
            button = page.get_by_role("button", name=label).first
            if button.count() > 0:
                button.click(timeout=8000)
                if _wait_for_google_page(browser, timeout=20):
                    log_fn(f"[TryBlend] started Google OAuth via button: {label}")
                    return
        except Exception:
            pass
    if try_click_provider_on_page(page, "google") and _wait_for_google_page(browser, timeout=20):
        log_fn("[TryBlend] started Google OAuth via provider detector")
        return
    raise RuntimeError("TryBlend 点击 Google 注册按钮后未打开 Google OAuth 页面")


def _click_google_prompts(browser: OAuthBrowser, *, timeout: int = 120, log_fn=print) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any("tryblend.ai" in (p.url or "") and "accounts.google.com" not in (p.url or "") and ("auth=success" in (p.url or "") or "settings" in (p.url or "")) for p in browser.pages() if not p.is_closed()):
            return
        for page in browser.pages():
            if page.is_closed() or "accounts.google.com" not in (page.url or ""):
                continue
            try:
                clicked = page.evaluate(
                    """
                    () => {
                      const words = ['Continue','继续','Allow','允许','Next','下一步','I understand','我了解'];
                      const nodes = [...document.querySelectorAll('button,input[type=submit],div[role=button]')];
                      const node = nodes.find(n => words.some(w => ((n.innerText||n.textContent||n.value||n.getAttribute('aria-label')||'').includes(w))));
                      if (!node) return '';
                      const label = node.innerText || node.textContent || node.value || node.getAttribute('aria-label') || 'clicked';
                      node.click();
                      return label;
                    }
                    """
                )
                if clicked:
                    log_fn(f"[TryBlend] 点击 Google 提示: {clicked}")
                    time.sleep(3)
            except Exception:
                pass
        time.sleep(1)


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
        raise RuntimeError("TryBlend 当前只支持 Google OAuth 自动化")

    with OAuthBrowser(proxy=proxy, headless=headless, chrome_user_data_dir=chrome_user_data_dir, chrome_cdp_url=chrome_cdp_url, log_fn=log_fn) as browser:
        page = browser.new_page()
        page.goto(SIGN_UP_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        _start_google_oauth(browser, page, log_fn=log_fn)
        drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=lambda b: any(
                "tryblend.ai" in (p.url or "") and "accounts.google.com" not in (p.url or "")
                and ("auth=success" in (p.url or "") or "settings" in (p.url or ""))
                for p in b.pages() if not p.is_closed()
            ),
        )
        if chrome_cdp_url or chrome_user_data_dir:
            browser.auto_select_google_account(timeout=8)
        _click_google_prompts(browser, timeout=min(timeout, 60), log_fn=log_fn)
        try:
            page.goto(SETTINGS_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
        except Exception:
            pass
        cookies = browser.cookie_dict(domain_substrings=("tryblend.ai",))
        session = _supabase_session_from_cookies(cookies)
        access_token = str(session.get("access_token") or "").strip()
        refresh_token = str(session.get("refresh_token") or "").strip()
        expires_at = session.get("expires_at")
        expires_in = session.get("expires_in")
        token_type = str(session.get("token_type") or "bearer").strip()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        actual_email = str(user.get("email") or "").strip()
        if not access_token:
            raise RuntimeError("TryBlend OAuth 未拿到 Supabase access_token")

    return {
        "email": finalize_oauth_email(actual_email, email_hint, "TryBlend"),
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "expires_in": expires_in,
        "token_type": token_type,
        "supabase_session": session,
        "cookies": cookies,
        "api_base": SITE_URL.rstrip('/'),
        "site_url": SITE_URL,
    }
