"""Komilion OAuth 自动化。"""
from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import urljoin

import requests

from core.base_identity import normalize_oauth_provider
from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page

SITE_URL = "https://www.komilion.com/"
SIGNUP_URL = "https://www.komilion.com/auth/signup"
LOGIN_URL = "https://www.komilion.com/auth/login"
DASHBOARD_URL = "https://www.komilion.com/dashboard/api-keys"
AUTH_BASE = "https://www.komilion.com/api/auth"
CSRF_PATH = "/api/auth/csrf"
SIGNIN_PATH_PREFIX = "/api/auth/signin/"
KEYS_PATH = "/api/user/api-keys"
SIGNUP_PATH = "/api/signup"
VERIFY_EMAIL_PATH = "/api/auth/verify-email"
CREDENTIALS_CALLBACK_PATH = "/api/auth/callback/credentials"
API_BASE = "https://www.komilion.com/api/v1"
VERIFY_PATH = "/models"
SESSION_COOKIE_NAMES = (
    "__Secure-next-auth.session-token",
    "next-auth.session-token",
)


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    try:
        return browser.cookie_dict(domain_substrings=("komilion.com", "www.komilion.com"))
    except Exception:
        return {}


def _cookie_session(cookies: dict[str, str], *, proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="www.komilion.com")
        session.cookies.set(name, value, domain=".komilion.com")
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": SITE_URL.rstrip("/"),
        "Referer": DASHBOARD_URL,
    })
    return session


def _session_cookie_map(session: requests.Session) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "")
        if not domain or "komilion.com" in domain:
            cookies[str(cookie.name)] = str(cookie.value)
    return cookies


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


def _response_message(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "error", "detail", "description", "reason"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in data.values():
            nested = _response_message(value)
            if nested:
                return nested
    if isinstance(data, list):
        for item in data:
            nested = _response_message(item)
            if nested:
                return nested
    if isinstance(data, str):
        return data.strip()
    return ""


def _signup_email_http(
    session: requests.Session,
    email: str,
    password: str,
    *,
    proxy: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    local_part = (email.split("@", 1)[0] if "@" in email else email).strip()
    payload = {
        "name": (name or local_part or "Auto Register").strip(),
        "email": email,
        "password": password,
        "acceptTerms": True,
    }
    try:
        response = session.post(
            urljoin(SITE_URL, SIGNUP_PATH),
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": SITE_URL.rstrip("/"),
                "Referer": SIGNUP_URL,
            },
            timeout=45,
        )
        data = _json_or_text(response)
        message = _response_message(data).lower()
        already_exists = response.status_code in {400, 409} and any(
            token in message for token in ("exist", "already", "registered", "taken")
        )
        needs_verify = any(token in message for token in ("verify", "verification", "confirm", "email"))
        ok = bool(response.ok or already_exists or needs_verify)
        return {
            "ok": ok,
            "status": response.status_code,
            "data": data,
            "text": response.text[:1000],
            "already_exists": already_exists,
            "needs_verify": needs_verify,
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _visit_verification_link_http(session: requests.Session, verification_link: str) -> dict[str, Any]:
    if not verification_link:
        return {"ok": False, "reason": "missing_verification_link"}
    try:
        response = session.get(verification_link, allow_redirects=True, timeout=60)
        data = _json_or_text(response, limit=1000)
        return {
            "ok": response.ok or "status=success" in str(response.url).lower() or "dashboard" in str(response.url).lower(),
            "status": response.status_code,
            "url": response.url,
            "data": data,
            "text": response.text[:1000],
            "cookies": _session_cookie_map(session),
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "url": verification_link}


def _credentials_login_http(
    session: requests.Session,
    email: str,
    password: str,
    *,
    proxy: str | None = None,
    callback_url: str = DASHBOARD_URL,
) -> dict[str, Any]:
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    try:
        csrf_response = session.get(urljoin(SITE_URL, CSRF_PATH), timeout=30)
        csrf_data = _json_or_text(csrf_response)
        csrf_token = str(csrf_data.get("csrfToken") or "") if isinstance(csrf_data, dict) else ""
        if not csrf_token:
            return {
                "ok": False,
                "stage": "csrf",
                "status": csrf_response.status_code,
                "data": csrf_data,
                "cookies": _session_cookie_map(session),
            }

        response = session.post(
            urljoin(SITE_URL, CREDENTIALS_CALLBACK_PATH),
            data={
                "csrfToken": csrf_token,
                "email": email,
                "password": password,
                "redirect": "false",
                "json": "true",
                "callbackUrl": callback_url,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json, text/plain, */*",
                "Origin": SITE_URL.rstrip("/"),
                "Referer": LOGIN_URL,
            },
            allow_redirects=True,
            timeout=45,
        )
        data = _json_or_text(response)
        cookies = _session_cookie_map(session)
        session_result = _get_session_http(cookies, proxy=proxy) if cookies else {"ok": False}
        has_session_cookie = any(cookies.get(name) for name in SESSION_COOKIE_NAMES)
        ok = bool(session_result.get("ok") or has_session_cookie or (response.ok and "error" not in str(data).lower()))
        return {
            "ok": ok,
            "status": response.status_code,
            "data": data,
            "text": response.text[:1000],
            "cookies": cookies,
            "session": session_result,
        }
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "cookies": _session_cookie_map(session)}


def register_with_email_verification(
    *,
    email: str,
    password: str,
    verification_link_callback: Callable[[], str] | None,
    proxy: str | None = None,
    timeout: int = 300,
    log_fn=print,
) -> dict[str, Any]:
    if not email:
        raise RuntimeError("Komilion 邮箱注册缺少 email")
    if not password:
        raise RuntimeError("Komilion 邮箱注册缺少 password")
    if verification_link_callback is None:
        raise RuntimeError("Komilion 邮箱注册缺少验证链接回调")

    session = _cookie_session({}, proxy=proxy)
    signup_result = _signup_email_http(session, email, password, proxy=proxy)
    if not signup_result.get("ok"):
        raise RuntimeError(f"Komilion 邮箱注册请求失败: {signup_result}")

    log_fn("[Komilion] 邮箱注册请求已提交，等待验证链接...")
    verification_link = verification_link_callback()
    if not verification_link:
        raise RuntimeError("Komilion 未收到邮箱验证链接")

    visit_result = _visit_verification_link_http(session, verification_link)
    if not visit_result.get("ok"):
        raise RuntimeError(f"Komilion 邮箱验证链接访问失败: {visit_result}")

    credentials_login_result = _credentials_login_http(session, email, password, proxy=proxy)
    if not credentials_login_result.get("ok"):
        raise RuntimeError(f"Komilion credentials 登录失败: {credentials_login_result}")

    cookies = _session_cookie_map(session)
    session_result = _get_session_http(cookies, proxy=proxy) if cookies else {"ok": False}
    deadline = time.time() + max(15, min(timeout, 120))
    while time.time() < deadline and not session_result.get("ok"):
        time.sleep(1)
        cookies = _session_cookie_map(session)
        session_result = _get_session_http(cookies, proxy=proxy) if cookies else {"ok": False}
    if not session_result.get("ok"):
        raise RuntimeError(f"Komilion 邮箱验证/登录后未拿到 session: {session_result}; cookies={list(cookies)}")

    actual_email = _extract_email(session_result, email)
    key_result = _create_api_key_http(cookies, proxy=proxy)
    api_key = str(key_result.get("api_key") or "").strip()
    if not key_result.get("ok") or not api_key:
        raise RuntimeError(f"Komilion 邮箱流已登录但创建/获取 API Key 失败: {key_result}")
    api_verification = _verify_api_key_http(api_key, proxy=proxy)
    if not api_verification.get("ok"):
        raise RuntimeError(f"Komilion 邮箱流已拿到 API Key 但验证失败: verify={api_verification}; key_result={key_result}")

    return {
        "email": actual_email,
        "password": password,
        "api_key": api_key,
        "api_key_info": key_result.get("result") or {},
        "key_create_result": key_result,
        "api_verification": api_verification,
        "signup_result": signup_result,
        "verification_link": verification_link,
        "visit_result": visit_result,
        "credentials_login_result": credentials_login_result,
        "session": session_result.get("data") or {},
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "api_base": API_BASE,
        "auth_header": "Authorization",
    }


def _providers_http(*, proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session({}, proxy=proxy)
    try:
        response = session.get(f"{AUTH_BASE}/providers", timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:2000]}
        return {"ok": response.ok, "status": response.status_code, "data": data}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _get_session_http(cookies: dict[str, str], *, proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies, proxy=proxy)
    try:
        response = session.get(f"{AUTH_BASE}/session", timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:2000]}
        ok = response.ok and isinstance(data, dict) and bool(data.get("user"))
        return {"ok": ok, "status": response.status_code, "data": data}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _find_api_key(data: Any) -> str:
    if isinstance(data, str):
        match = re.search(r"ck_[A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{12,}|[A-Za-z0-9_\-]{24,}", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("apiKey", "api_key", "key", "token", "secret", "value"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                found = _find_api_key(value)
                return found or value.strip()
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


def _extract_email(session_result: dict[str, Any], fallback: str) -> str:
    data = session_result.get("data") if isinstance(session_result, dict) else {}
    if isinstance(data, dict):
        user = data.get("user") if isinstance(data.get("user"), dict) else data
        if isinstance(user, dict):
            for key in ("email", "user_email"):
                if user.get(key):
                    return str(user.get(key)).strip()
    return fallback


def _create_api_key_http(cookies: dict[str, str], *, proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies, proxy=proxy)
    attempts: list[dict[str, Any]] = []
    payloads = ({"action": "generate"}, {"action": "regenerate"})
    for payload in payloads:
        action = str(payload.get("action") or "")
        try:
            response = session.post(
                urljoin(SITE_URL, KEYS_PATH),
                json=payload,
                headers={"Content-Type": "application/json", "Origin": SITE_URL.rstrip("/"), "Referer": DASHBOARD_URL},
                timeout=30,
            )
            try:
                data = response.json()
            except Exception:
                data = {"raw": response.text[:2000]}
            api_key = _find_api_key(data)
            item = {"ok": response.ok, "status": response.status_code, "action": action, "data": data, "api_key": api_key}
            attempts.append(item)
            if response.ok and api_key:
                return {"ok": True, "api_key": api_key, "result": item, "attempts": attempts}
        except Exception as exc:
            attempts.append({"ok": False, "action": action, "error": repr(exc)})

    try:
        response = session.get(urljoin(SITE_URL, f"{KEYS_PATH}?reveal=true"), timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:2000]}
        api_key = _find_api_key(data)
        item = {"ok": response.ok, "status": response.status_code, "action": "reveal", "data": data, "api_key": api_key}
        attempts.append(item)
        if response.ok and api_key:
            return {"ok": True, "api_key": api_key, "result": item, "attempts": attempts}
    except Exception as exc:
        attempts.append({"ok": False, "action": "reveal", "error": repr(exc)})
    return {"ok": False, "api_key": "", "attempts": attempts}


def _verify_api_key_http(api_key: str, *, proxy: str | None = None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "reason": "missing_api_key", "path": VERIFY_PATH}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        verify_url = "https://www.komilion.com/api/v1/models"
        response = requests.get(
            verify_url,
            headers={"Authorization": f"Bearer {api_key}"},
            proxies=proxies,
            timeout=30,
        )
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:2000]}
        return {"ok": response.ok, "status": response.status_code, "path": VERIFY_PATH, "url": verify_url, "body": body}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "path": VERIFY_PATH}


def _open_oauth_url_nonblocking(page, url: str, *, log_fn=print) -> bool:
    if not url:
        return False
    try:
        new_page = page.context.new_page()
        try:
            new_page.goto("about:blank", wait_until="commit", timeout=5000)
        except Exception:
            pass
        try:
            new_page.evaluate("u => { window.location.assign(u); }", url)
        except Exception as exc:
            if "Execution context was destroyed" not in str(exc) and "Navigation" not in str(exc):
                raise
        time.sleep(1)
        if "accounts.google.com" not in str(new_page.url or "") and "login.microsoft" not in str(new_page.url or ""):
            try:
                new_page.goto(url, wait_until="commit", timeout=12000)
            except Exception as exc:
                if "Timeout" not in str(exc) and "Navigation" not in str(exc):
                    raise
        log_fn(f"[Komilion] 已用新标签发起 OAuth: {new_page.url}")
        return True
    except Exception as exc:
        log_fn(f"[Komilion] 新标签打开 OAuth URL 失败: {exc!r}")
    try:
        page.evaluate("u => { window.location.assign(u); }", url)
        log_fn("[Komilion] 已用当前标签发起 OAuth")
        return True
    except Exception as exc:
        log_fn(f"[Komilion] OAuth URL 打开失败: {exc!r}")
        return False


def _sync_requests_cookies_to_browser(page, session: requests.Session) -> None:
    cookies = []
    for cookie in session.cookies:
        if "komilion.com" not in str(cookie.domain or ""):
            continue
        cookies.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain or ".komilion.com",
            "path": cookie.path or "/",
            "httpOnly": bool(cookie.has_nonstandard_attr("HttpOnly") or cookie.name.startswith("__")),
            "secure": bool(cookie.secure or cookie.name.startswith("__Secure") or cookie.name.startswith("__Host")),
            "sameSite": "Lax",
        })
    if cookies:
        try:
            page.context.add_cookies(cookies)
        except Exception:
            pass


def _start_nextauth_oauth_protocol(page, provider: str, *, callback_url: str = DASHBOARD_URL, proxy: str | None = None, log_fn=print) -> bool:
    normalized = normalize_oauth_provider(provider or "google")
    if normalized == "outlook":
        normalized = "microsoft"
    session = _cookie_session({}, proxy=proxy)
    try:
        csrf_url = "https://www.komilion.com/api/auth/csrf"
        csrf_response = session.get(csrf_url, timeout=30)
        csrf_data = csrf_response.json()
        csrf_token = str(csrf_data.get("csrfToken") or "")
        if not csrf_token:
            log_fn(f"[Komilion] NextAuth csrf 未返回 token: status={csrf_response.status_code} body={csrf_response.text[:300]}")
            return False
        response = session.post(
            f"https://www.komilion.com/api/auth/signin/{normalized}",
            data={"csrfToken": csrf_token, "callbackUrl": callback_url, "json": "true"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Origin": SITE_URL.rstrip("/"),
                "Referer": SIGNUP_URL,
            },
            allow_redirects=False,
            timeout=30,
        )
        _sync_requests_cookies_to_browser(page, session)
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:1000]}
        url = ""
        if isinstance(data, dict):
            url = str(data.get("url") or data.get("redirect") or data.get("location") or "")
        if not url:
            url = str(response.headers.get("location") or response.headers.get("Location") or "")
        if not url:
            log_fn(f"[Komilion] NextAuth signin/{normalized} 未返回 redirect: status={response.status_code} data={data}")
            return False
        log_fn(f"[Komilion] NextAuth {normalized} OAuth URL: {url[:160]}")
        return _open_oauth_url_nonblocking(page, url, log_fn=log_fn)
    except Exception as exc:
        log_fn(f"[Komilion] NextAuth 协议发起 OAuth 异常: {exc!r}")
        return False


def _wait_provider_page(browser: OAuthBrowser, provider: str, *, timeout: int = 30, log_fn=print) -> bool:
    normalized = normalize_oauth_provider(provider or "google")
    deadline = time.time() + max(1, timeout)
    last_urls = ""
    provider_hosts = ("accounts.google.com",) if normalized == "google" else (
        "login.microsoftonline.com",
        "login.live.com",
        "login.microsoft.com",
    )
    while time.time() < deadline:
        urls: list[str] = []
        for page in browser.pages():
            if page.is_closed():
                continue
            current = str(page.url or "")
            urls.append(current)
            if any(host in current for host in provider_hosts):
                log_fn(f"[Komilion] 已进入 OAuth 授权页: {current}")
                return True
        joined = " | ".join(urls)[:600]
        if joined and joined != last_urls:
            last_urls = joined
            log_fn(f"[Komilion] 等待 OAuth 授权页，当前标签: {joined}")
        time.sleep(0.5)
    log_fn(f"[Komilion] 等待 OAuth 授权页超时，最后标签: {last_urls}")
    return False


def _komilion_oauth_done(browser: OAuthBrowser) -> bool:
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        if "komilion.com" in url and "/auth/" not in url and "accounts.google.com" not in url:
            return True
    cookies = _cookie_map(browser)
    return any(cookies.get(name) for name in SESSION_COOKIE_NAMES)


def _click_provider_fallback(page, provider: str, *, log_fn=print) -> bool:
    if try_click_provider_on_page(page, provider):
        log_fn(f"[Komilion] 页面点击 OAuth provider: {provider}")
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
    oauth_password: str = "",
) -> dict:
    normalized_provider = normalize_oauth_provider(oauth_provider or "google")
    if normalized_provider == "outlook":
        normalized_provider = "microsoft"
    providers_result = _providers_http(proxy=proxy)
    providers = providers_result.get("data") if isinstance(providers_result, dict) else {}
    if normalized_provider in {"microsoft", "outlook"} and not (isinstance(providers, dict) and normalized_provider in providers):
        raise RuntimeError(f"Komilion 当前 NextAuth providers 未开放 Microsoft/Outlook OAuth: {providers_result}")
    if not (isinstance(providers, dict) and normalized_provider in providers):
        raise RuntimeError(f"Komilion 未开放 OAuth provider={normalized_provider}: {providers_result}")

    with OAuthBrowser(proxy=proxy, headless=headless, chrome_user_data_dir=chrome_user_data_dir, chrome_cdp_url=chrome_cdp_url, log_fn=log_fn) as browser:
        page = browser.new_page()
        events: list[dict[str, Any]] = []

        def on_request(req):
            url = str(req.url or "")
            if "komilion.com" in url and ("/api/" in url or "/auth/" in url):
                events.append({"type": "request", "method": req.method, "url": url, "post": (req.post_data or "")[:1000]})

        def on_response(resp):
            try:
                url = str(resp.url or "")
                if "komilion.com" not in url or ("/api/" not in url and "/auth/" not in url):
                    return
                headers = dict(resp.headers or {})
                events.append({
                    "type": "response",
                    "url": url,
                    "status": resp.status,
                    "location": headers.get("location") or headers.get("Location") or "",
                    "content_type": headers.get("content-type") or "",
                })
            except BaseException:
                return

        try:
            page.on("request", on_request)
            page.on("response", on_response)
            browser.context.on("request", on_request)
            browser.context.on("response", on_response)
        except Exception:
            pass

        try:
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=90000)
            time.sleep(1)
        except Exception as exc:
            log_fn(f"[Komilion] 打开 signup 页失败，继续协议 OAuth: {exc!r}")

        opened = _start_nextauth_oauth_protocol(page, normalized_provider, proxy=proxy, log_fn=log_fn)
        if not opened:
            opened = _click_provider_fallback(page, normalized_provider, log_fn=log_fn)
        if not opened or not _wait_provider_page(browser, normalized_provider, timeout=35, log_fn=log_fn):
            raise RuntimeError(f"Komilion 已尝试发起 OAuth，但未进入 provider={normalized_provider} 授权页; captured={events[-20:]}")

        if normalized_provider == "google":
            log_fn("[Komilion] 开始驱动 Google OAuth 表单/consent")
            drive_google_oauth(
                browser,
                email=email_hint,
                password=oauth_password,
                timeout=min(timeout, 220),
                log_fn=log_fn,
                stop_when=_komilion_oauth_done,
            )
        else:
            raise RuntimeError("Komilion Microsoft/Outlook OAuth 入口已探测，但项目尚无 Microsoft 表单 driver；需要接入 Outlook/Microsoft OAuth driver 后再跑")

        deadline = time.time() + max(30, timeout)
        cookies = _cookie_map(browser)
        session_result = _get_session_http(cookies, proxy=proxy) if cookies else {"ok": False}
        while time.time() < deadline and not session_result.get("ok"):
            cookies = _cookie_map(browser)
            if cookies:
                session_result = _get_session_http(cookies, proxy=proxy)
                if session_result.get("ok"):
                    break
            time.sleep(1)
        if not session_result.get("ok"):
            snapshot = google_oauth_snapshot(browser) if normalized_provider == "google" else []
            raise RuntimeError(f"Komilion OAuth 后未拿到 NextAuth session: session={session_result}; snapshot={snapshot[:2]}; captured={events[-30:]}")

        actual_email = finalize_oauth_email(_extract_email(session_result, email_hint), email_hint, "Komilion")
        key_result = _create_api_key_http(cookies, proxy=proxy)
        api_key = str(key_result.get("api_key") or "").strip()
        if not key_result.get("ok") or not api_key:
            raise RuntimeError(f"Komilion 已登录但协议创建/获取 API Key 失败: {key_result}; captured={events[-30:]}")
        api_verification = _verify_api_key_http(api_key, proxy=proxy)
        if not api_verification.get("ok"):
            raise RuntimeError(f"Komilion 已拿到 API Key 但验证失败: verify={api_verification}; key_result={key_result}")

    return {
        "email": actual_email,
        "api_key": api_key,
        "api_key_info": key_result.get("result") or {},
        "key_create_result": key_result,
        "api_verification": api_verification,
        "session": session_result.get("data") or {},
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "providers": providers if isinstance(providers, dict) else {},
        "oauth_provider": normalized_provider,
        "captured_requests": events[-50:],
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "api_base": API_BASE,
        "auth_header": "Authorization",
    }
