"""LemonData Google OAuth + HTTP API key 创建链路。"""
from __future__ import annotations

import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from core.google_oauth import drive_google_oauth
from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page
from platforms.lemondata.core import DASHBOARD_URL, LLM_API_BASE, SIGNIN_URL, SITE_URL, LemonDataClient, find_api_key


@contextmanager
def isolated_oauth_browser_options(
    *,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    allow_shared_cdp: bool = False,
):
    """为 LemonData 并发 OAuth 分配隔离浏览器上下文。"""
    if chrome_user_data_dir:
        yield {"chrome_user_data_dir": chrome_user_data_dir, "chrome_cdp_url": chrome_cdp_url if allow_shared_cdp else ""}
        return
    if chrome_cdp_url and allow_shared_cdp:
        yield {"chrome_user_data_dir": "", "chrome_cdp_url": chrome_cdp_url}
        return
    profile_root = Path(tempfile.mkdtemp(prefix="lemondata_oauth_"))
    try:
        yield {"chrome_user_data_dir": str(profile_root), "chrome_cdp_url": ""}
    finally:
        # OAuthBrowser 只会关闭 context；这里清理本平台自动创建的隔离 profile。
        try:
            import shutil

            shutil.rmtree(profile_root, ignore_errors=True)
        except Exception:
            pass


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    try:
        return browser.cookie_dict(domain_substrings=("tokenlab.sh", "lemondata.cc"))
    except Exception:
        return {}


def _has_auth_cookie(cookies: dict[str, str]) -> bool:
    return any("session-token" in str(name).lower() for name in cookies.keys())


def _create_api_key_http(cookies: dict[str, str], *, name: str, proxy: str | None = None, user_agent: str = "", log_fn=print) -> dict[str, Any]:
    client = LemonDataClient(proxy=proxy, user_agent=user_agent, log_fn=log_fn)
    client.import_cookies(cookies)
    result = client.create_or_find_api_key(name=name)
    api_key = str(result.get("api_key") or find_api_key(result) or "").strip()
    return {"ok": bool(api_key), "api_key": api_key, "data": result.get("api_key_info") or result, "raw": result}


def _verify_api_key_http(api_key: str, *, proxy: str | None = None, user_agent: str = "", log_fn=print) -> dict[str, Any]:
    return LemonDataClient(proxy=proxy, user_agent=user_agent, log_fn=log_fn).verify_api_key(api_key)


def _create_api_key_in_browser(page, *, name: str, log_fn=print) -> dict[str, Any]:
    """在已登录 LemonData 页面内用同源 fetch 创建 API Key。

    LemonData dashboard 使用 /api/csrf + csrf_token cookie + x-csrf-token。
    HTTP Session 重放在部分场景会被判 CSRF 失败；浏览器上下文能保持
    Next/Auth.js 的同源 cookie 行为，因此作为 OAuth 成功后的 fallback。
    """
    try:
        result = page.evaluate(
            r"""
            async ({name}) => {
              const out = {ok: false, attempts: []};
              const csrfResponse = await fetch('/api/csrf', {method: 'GET', credentials: 'include'});
              let csrfJson = {};
              try { csrfJson = await csrfResponse.json(); } catch (e) { csrfJson = {raw: await csrfResponse.text()}; }
              const csrfToken = csrfJson.token || csrfJson.csrfToken || '';
              out.csrf = {ok: csrfResponse.ok, status: csrfResponse.status, hasToken: !!csrfToken};
              const headers = {'Content-Type': 'application/json'};
              if (csrfToken) headers['x-csrf-token'] = csrfToken;
              const readJson = async (response) => {
                const text = await response.text();
                try { return JSON.parse(text); } catch (e) { return {raw: text.slice(0, 1200)}; }
              };
              const orgResponse = await fetch('/api/dashboard/organizations', {credentials: 'include'});
              const orgData = await readJson(orgResponse);
              out.organizations = {ok: orgResponse.ok, status: orgResponse.status, data: orgData};
              const ids = [];
              const walk = (value) => {
                if (Array.isArray(value)) return value.forEach(walk);
                if (!value || typeof value !== 'object') return;
                const id = value.id || value.orgId || value.organizationId || value.slug;
                if (typeof id === 'string' && id && !ids.includes(id)) ids.push(id);
                Object.values(value).forEach(walk);
              };
              walk(orgData);
              if (!ids.includes('default')) ids.push('default');
              for (const orgId of ids) {
                const endpoint = `/api/dashboard/organizations/${encodeURIComponent(orgId)}/api-keys`;
                for (const payload of [{name}, {label: name}, {keyName: name}, {description: name}, {}]) {
                  const response = await fetch(endpoint, {
                    method: 'POST',
                    credentials: 'include',
                    headers,
                    body: JSON.stringify(payload),
                  });
                  const data = await readJson(response);
                  out.attempts.push({endpoint, status: response.status, ok: response.ok, payload, data});
                  if (response.ok) return {ok: true, data, attempts: out.attempts, organizations: out.organizations, csrf: out.csrf};
                  if ([401, 403, 404, 405].includes(response.status)) break;
                }
              }
              return out;
            }
            """,
            {"name": name},
        ) or {}
        api_key = str(find_api_key(result) or "").strip()
        return {"ok": bool(api_key), "api_key": api_key, "data": result.get("data") or result, "raw": result}
    except Exception as exc:
        log_fn(f"[LemonData] browser create API key failed: {exc!r}")
        return {"ok": False, "error": repr(exc)}


def _collect_dashboard_api_requests(page) -> list[dict[str, Any]]:
    """监听 dashboard API 请求，便于后续确认真实 key endpoint。"""
    captured: list[dict[str, Any]] = []

    def on_response(response) -> None:
        try:
            url = str(response.url or "")
            if "/api/dashboard/" not in url:
                return
            captured.append({"url": url, "status": int(response.status), "method": response.request.method})
        except Exception:
            return

    try:
        page.on("response", on_response)
    except Exception:
        pass
    return captured


def _start_google_oauth_protocol(page, *, callback_url: str = DASHBOARD_URL, log_fn=print) -> bool:
    """用 Auth.js 协议发起 Google OAuth，失败再由页面点击兜底。"""
    try:
        result = page.evaluate(
            """
            async ({callbackUrl}) => {
              const csrfResponse = await fetch('/api/auth/csrf', {credentials: 'include'});
              const csrfJson = await csrfResponse.json();
              const csrfToken = csrfJson.csrfToken || csrfJson.token || '';
              const response = await fetch('/api/auth/signin/google', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Auth-Return-Redirect': '1'},
                credentials: 'include',
                body: new URLSearchParams({csrfToken, callbackUrl})
              });
              const text = await response.text();
              let data = {};
              try { data = JSON.parse(text); } catch (e) { data = {raw: text}; }
              return {ok: response.ok, status: response.status, data};
            }
            """,
            {"callbackUrl": callback_url},
        ) or {}
        data = result.get("data") if isinstance(result, dict) else {}
        url = str(data.get("url") or data.get("redirect") or "") if isinstance(data, dict) else ""
        if url:
            log_fn(f"[LemonData] Auth.js Google OAuth URL: {url[:160]}")
            page.goto(url, wait_until="commit", timeout=90000)
            return True
        log_fn(f"[LemonData] Auth.js Google OAuth 未返回 URL: {result}")
    except Exception as exc:
        log_fn(f"[LemonData] Auth.js Google OAuth 发起异常: {exc!r}")
    return False


def _extract_logged_email(page, session_data: dict[str, Any] | None = None) -> str:
    data = dict(session_data or {})
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    email = str(user.get("email") or data.get("email") or "").strip()
    if email:
        return email
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
    allow_shared_cdp: bool = False,
) -> dict:
    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("LemonData 当前只支持 Google OAuth 自动化")

    with isolated_oauth_browser_options(
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        allow_shared_cdp=allow_shared_cdp,
    ) as browser_options, OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=browser_options["chrome_user_data_dir"],
        chrome_cdp_url=browser_options["chrome_cdp_url"],
        log_fn=log_fn,
    ) as browser:
        page = browser.new_page()
        page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=90000)
        try:
            browser_user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip()
        except Exception:
            browser_user_agent = ""
        if browser_user_agent:
            log_fn(f"[LemonData] browser user-agent: {browser_user_agent[:120]}")
        captured_requests = _collect_dashboard_api_requests(page)
        time.sleep(1)
        if not _start_google_oauth_protocol(page, log_fn=log_fn):
            try_click_provider_on_page(page, "google")

        google_result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=lambda b: _has_auth_cookie(_cookie_map(b))
            or any("/dashboard" in (p.url or "") for p in b.pages() if not p.is_closed()),
        )
        if getattr(google_result, "blocked_on_password", False):
            raise RuntimeError(f"LemonData Google OAuth 未完成: {google_result.last_url} :: {google_result.last_body[:300]}")
        if chrome_cdp_url or chrome_user_data_dir:
            browser.auto_select_google_account(timeout=8)

        log_fn(f"[LemonData] Google OAuth driver done: last_url={getattr(google_result, 'last_url', '')}")
        deadline = time.time() + max(30, min(timeout, 120))
        dashboard_page = page
        cookies: dict[str, str] = {}
        last_wait_log = 0.0
        while time.time() < deadline:
            page_urls: list[str] = []
            for item in browser.pages():
                if item.is_closed():
                    continue
                page_urls.append(str(item.url or ""))
                if any(host in (item.url or "") for host in ("tokenlab.sh", "lemondata.cc")):
                    dashboard_page = item
                if "/dashboard" in (item.url or ""):
                    dashboard_page = item
            cookies = _cookie_map(browser)
            if _has_auth_cookie(cookies):
                break
            if time.time() - last_wait_log > 8:
                last_wait_log = time.time()
                log_fn(f"[LemonData] wait auth cookie: cookies={sorted(cookies.keys())} urls={page_urls[-4:]}")
            try:
                if "accounts.google.com" not in (dashboard_page.url or ""):
                    dashboard_page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                if time.time() - last_wait_log > 8:
                    log_fn(f"[LemonData] dashboard poll failed: {exc!r}")
            time.sleep(1)
        if not _has_auth_cookie(cookies):
            page_urls = [str(p.url or "") for p in browser.pages() if not p.is_closed()]
            raise RuntimeError(f"LemonData OAuth 登录超时，未拿到 Auth.js session cookie: cookies={sorted(cookies.keys())}, urls={page_urls}")

        client = LemonDataClient(proxy=proxy, user_agent=browser_user_agent, log_fn=log_fn)
        client.import_cookies(cookies)
        session_result = client.get_session()
        session_data = session_result.get("data") or {}
        key_name = f"auto-register-{int(time.time())}"
        create_result = _create_api_key_http(cookies, name=key_name, proxy=proxy, user_agent=browser_user_agent, log_fn=log_fn)
        if not create_result.get("ok"):
            log_fn(f"[LemonData] HTTP 创建 API Key 失败，改用浏览器同源 fetch: {create_result}")
            create_result = _create_api_key_in_browser(dashboard_page, name=key_name, log_fn=log_fn)
        if not create_result.get("ok"):
            raise RuntimeError(f"LemonData 创建 API Key 失败: {create_result}")
        api_key = str(create_result.get("api_key") or find_api_key(create_result) or "").strip()
        api_verification = _verify_api_key_http(api_key, proxy=proxy, user_agent=browser_user_agent, log_fn=log_fn)
        balance_result = client.require_min_balance(min_amount=1.0)
        actual_email = _extract_logged_email(dashboard_page, session_data)

    return {
        "email": finalize_oauth_email(actual_email, email_hint, "LemonData"),
        "api_key": api_key,
        "api_key_info": create_result.get("data") or {},
        "api_verification": api_verification,
        "balance_result": balance_result,
        "key_create_result": create_result.get("raw") or create_result,
        "session": session_data,
        "cookies": cookies,
        "cookie_header": "; ".join(f"{name}={value}" for name, value in cookies.items() if value),
        "captured_requests": captured_requests,
        "browser_user_agent": browser_user_agent,
        "api_base": LLM_API_BASE,
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
    }
