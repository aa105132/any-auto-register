"""Novita 注册与 API Key 提取。"""
from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import requests

from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page

SITE_URL = "https://novita.ai/"
LOGIN_URL = "https://novita.ai/user/login"
DASHBOARD_URL = "https://novita.ai/models-console"
KEYS_URL = "https://novita.ai/settings/key-management"
API_ORIGIN = "https://api-server.novita.ai"
API_BASE = "https://api.novita.ai"
GOOGLE_OAUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_CLIENT_ID = "1009719330755-jorps53l35j75md6f9cqpilicv16kq5l.apps.googleusercontent.com"
GOOGLE_REDIRECT_PATH = "/api/auth"
GOOGLE_SCOPE = "email profile openid"
NOVITA_AUTH_CALLBACK_COOKIE = "auth_callback_url"
NOVITA_AUTH_TYPE_COOKIE = "auth_type"
LOGIN_PATH = "/v1/user/login"
REGISTER_PATH = "/v1/user/register"
USER_INFO_PATH = "/v1/user/info"
EMAIL_VERIFY_PATH = "/v1/user/email/verify"
KEYS_PATH = "/v2/user/key"
QUESTIONNAIRE_PATH = "/v1/user/questionnaire"
BALANCE_PATH = "/v1/billing/balance/detail"
VOUCHER_PATH = "/v1/billing/voucher/list"
VERIFY_PATH = "/v3/model"
SESSION_COOKIE_HINTS = ("token", "novita")


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


def _response_message(data: Any) -> str:
    if isinstance(data, dict):
        for key in ("message", "msg", "error", "detail", "description", "reason"):
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


def _cookie_session(cookies: dict[str, str] | None = None, *, token: str = "", proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    for name, value in (cookies or {}).items():
        session.cookies.set(name, value, domain="novita.ai")
        session.cookies.set(name, value, domain=".novita.ai")
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": SITE_URL.rstrip("/"),
        "Referer": DASHBOARD_URL,
    })
    if token:
        auth_value = token if str(token).lower().startswith("bearer ") else f"Bearer {token}"
        session.headers.update({"Authorization": auth_value})
    return session


def _session_cookie_map(session: requests.Session) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "")
        if not domain or "novita.ai" in domain:
            cookies[str(cookie.name)] = str(cookie.value)
    return cookies


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    try:
        return browser.cookie_dict(domain_substrings=("novita.ai",))
    except Exception:
        return {}


def _extract_token(data: Any, cookies: dict[str, str] | None = None) -> str:
    if isinstance(data, dict):
        for key in ("token", "accessToken", "access_token", "jwt", "idToken"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("payload", "data", "result", "user"):
            token = _extract_token(data.get(key), cookies=None)
            if token:
                return token
    if cookies:
        for name, value in cookies.items():
            low = name.lower()
            if any(hint in low for hint in SESSION_COOKIE_HINTS) and value:
                return str(value).strip()
    return ""


def _extract_email(user_result: dict[str, Any], fallback: str = "") -> str:
    data = user_result.get("data") if isinstance(user_result, dict) else {}
    if isinstance(data, dict):
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
        for key in ("email", "email_cn", "username", "mobilePhone"):
            value = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(value, str) and "@" in value:
                return value.strip()
    return str(fallback or "").strip()


def _find_api_key(data: Any) -> str:
    if isinstance(data, str):
        value = data.strip()
        if not value or value.lower() in {"masked", "****", "null", "none"}:
            return ""
        match = re.search(r"novita[_\-][A-Za-z0-9_\-]{8,}|sk-[A-Za-z0-9_\-]{12,}|[A-Za-z0-9_\-]{32,}", value)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("apiKey", "api_key", "secret", "token", "value", "key"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                found = _find_api_key(value)
                if found:
                    return found
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


def _register_email_http(session: requests.Session, email: str, password: str, *, name: str = "") -> dict[str, Any]:
    local_part = (email.split("@", 1)[0] if "@" in email else email).strip()
    payloads = [
        {"email": email, "password": password, "username": name or local_part, "fromInviteCode": ""},
        {"email": email, "password": password, "firstName": name or local_part, "lastName": "", "fromInviteCode": ""},
        {"email": email, "password": password},
    ]
    attempts: list[dict[str, Any]] = []
    for payload in payloads:
        try:
            response = session.post(urljoin(API_ORIGIN, REGISTER_PATH), json=payload, timeout=45)
            data = _json_or_text(response)
            message = _response_message(data).lower()
            already_exists = response.status_code in {400, 409} and any(token in message for token in ("exist", "already", "registered"))
            needs_verify = any(token in message for token in ("verify", "verification", "active", "email"))
            item = {"ok": bool(response.ok or already_exists or needs_verify), "status": response.status_code, "data": data, "payload_keys": list(payload.keys()), "already_exists": already_exists, "needs_verify": needs_verify}
            attempts.append(item)
            if item["ok"]:
                item["attempts"] = attempts
                return item
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload_keys": list(payload.keys())})
    return {"ok": False, "attempts": attempts}


def _verify_email_http(session: requests.Session, verification_link: str) -> dict[str, Any]:
    if not verification_link:
        return {"ok": False, "reason": "missing_verification_link"}
    parsed = urlparse(verification_link)
    query = parse_qs(parsed.query or "")
    token = ""
    for key in ("token", "code", "verifyCode", "verificationCode", "activeCode"):
        values = query.get(key) or []
        if values:
            token = str(values[0] or "").strip()
            break
    attempts: list[dict[str, Any]] = []
    if token:
        for payload in ({"token": token}, {"code": token}, {"verifyCode": token}, {"activeCode": token}):
            try:
                response = session.post(urljoin(API_ORIGIN, EMAIL_VERIFY_PATH), json=payload, timeout=45)
                data = _json_or_text(response)
                item = {"ok": response.ok, "status": response.status_code, "data": data, "payload": payload}
                attempts.append(item)
                if response.ok:
                    item["attempts"] = attempts
                    return item
            except Exception as exc:
                attempts.append({"ok": False, "error": repr(exc), "payload": payload})
    try:
        response = session.get(verification_link, allow_redirects=True, timeout=60)
        data = _json_or_text(response, limit=1000)
        ok = bool(response.ok or "success" in str(response.url).lower() or "models-console" in str(response.url).lower())
        return {"ok": ok, "status": response.status_code, "url": response.url, "data": data, "attempts": attempts}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "attempts": attempts, "url": verification_link}


def _login_email_http(session: requests.Session, email: str, password: str) -> dict[str, Any]:
    payloads = [
        {"email": email, "password": password},
        {"account": email, "password": password},
        {"username": email, "password": password},
    ]
    attempts: list[dict[str, Any]] = []
    for payload in payloads:
        try:
            response = session.post(urljoin(API_ORIGIN, LOGIN_PATH), json=payload, timeout=45)
            data = _json_or_text(response)
            cookies = _session_cookie_map(session)
            token = _extract_token(data, cookies)
            item = {"ok": bool(response.ok and (token or data)), "status": response.status_code, "data": data, "cookies": cookies, "session_token": token, "payload_keys": list(payload.keys())}
            attempts.append(item)
            if item["ok"]:
                item["attempts"] = attempts
                if token:
                    session.headers.update({"Authorization": token})
                return item
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload_keys": list(payload.keys())})
    return {"ok": False, "attempts": attempts, "cookies": _session_cookie_map(session), "session_token": ""}


def _get_user_info_http(cookies: dict[str, str] | None = None, *, token: str = "", proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies or {}, token=token, proxy=proxy)
    try:
        response = session.get(urljoin(API_ORIGIN, USER_INFO_PATH), timeout=30)
        data = _json_or_text(response)
        ok = response.ok and isinstance(data, dict) and bool(data.get("payload") or data.get("data") or data.get("uuid") or data.get("email"))
        return {"ok": ok, "status": response.status_code, "data": data, "cookies": _session_cookie_map(session)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _default_questionnaire_payload(email: str = "") -> dict[str, Any]:
    local = (email.split("@", 1)[0] if "@" in email else "auto").strip() or "auto"
    return {
        "firstName": local[:32],
        "lastName": "Register",
        "companyName": "Individual",
        "country": "US",
        "role": "Developer",
        "useCase": "LLM API",
        "currentMonthlySpendOnAiModels": "$0-$100",
        "teamSize": "1",
        "receiveMarketingEmail": False,
    }


def _submit_questionnaire_http(cookies: dict[str, str] | None = None, *, token: str = "", email: str = "", proxy: str | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies or {}, token=token, proxy=proxy)
    body = dict(_default_questionnaire_payload(email))
    if payload:
        body.update(payload)
    attempts: list[dict[str, Any]] = []
    variants = [body]
    variants.append({
        "role": body["role"],
        "companyName": body["companyName"],
        "country": body["country"],
        "currentMonthlySpendOnAiModels": body["currentMonthlySpendOnAiModels"],
        "useCase": body["useCase"],
    })
    for variant in variants:
        try:
            response = session.post(urljoin(API_ORIGIN, QUESTIONNAIRE_PATH), json=variant, timeout=45)
            data = _json_or_text(response)
            item = {"ok": response.ok, "status": response.status_code, "data": data, "payload": variant}
            attempts.append(item)
            if response.ok:
                item["attempts"] = attempts
                return item
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload": variant})
    return {"ok": False, "attempts": attempts}


def _create_api_key_http(cookies: dict[str, str] | None = None, *, token: str = "", name: str = "auto-register", proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies or {}, token=token, proxy=proxy)
    attempts: list[dict[str, Any]] = []
    payloads = [
        {"name": name},
        {"name": name, "expireTime": ""},
        {"name": name, "budget_type": "Unlimited", "budget": 0},
    ]
    for payload in payloads:
        try:
            response = session.post(urljoin(API_ORIGIN, KEYS_PATH), json=payload, timeout=45)
            data = _json_or_text(response)
            api_key = _find_api_key(data)
            item = {"ok": response.ok, "status": response.status_code, "data": data, "api_key": api_key, "payload": payload}
            attempts.append(item)
            if response.ok and api_key:
                return {"ok": True, "api_key": api_key, "result": item, "attempts": attempts}
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload": payload})
    list_result = _list_api_keys_http(cookies, token=token, proxy=proxy)
    attempts.append({"action": "list", **list_result})
    api_key = _find_api_key(list_result.get("data"))
    if list_result.get("ok") and api_key:
        return {"ok": True, "api_key": api_key, "result": list_result, "attempts": attempts}
    return {"ok": False, "api_key": "", "attempts": attempts}


def _list_api_keys_http(cookies: dict[str, str] | None = None, *, token: str = "", proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies or {}, token=token, proxy=proxy)
    try:
        response = session.get(urljoin(API_ORIGIN, KEYS_PATH), timeout=30)
        data = _json_or_text(response)
        return {"ok": response.ok, "status": response.status_code, "data": data, "api_key": _find_api_key(data)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _get_balance_http(cookies: dict[str, str] | None = None, *, token: str = "", proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies or {}, token=token, proxy=proxy)
    try:
        response = session.get(urljoin(API_ORIGIN, BALANCE_PATH), timeout=30)
        return {"ok": response.ok, "status": response.status_code, "data": _json_or_text(response)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _get_voucher_http(cookies: dict[str, str] | None = None, *, token: str = "", proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies or {}, token=token, proxy=proxy)
    try:
        response = session.get(urljoin(API_ORIGIN, VOUCHER_PATH), timeout=30)
        return {"ok": response.ok, "status": response.status_code, "data": _json_or_text(response)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _google_redirect_uri() -> str:
    return urljoin(SITE_URL, GOOGLE_REDIRECT_PATH.lstrip("/"))


def _build_google_oauth_url() -> str:
    """构造 Novita 前端 bundle 里的 Google OAuth URL。"""
    query = urlencode(
        {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": _google_redirect_uri(),
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
        },
        quote_via=quote,
    )
    return f"{GOOGLE_OAUTH_URL}?{query}"


def _encoded_oauth_callback_cookie_value() -> str:
    """返回原生按钮经 js-cookie 写出的 auth_callback_url cookie 值。"""
    return quote(quote(_google_redirect_uri(), safe=""), safe="")


def _set_novita_oauth_cookies(page) -> None:
    """同步 Novita 前端发起 OAuth 前写入的 cookie 状态。"""
    redirect_uri = _google_redirect_uri()
    cookies = [
        {
            "name": NOVITA_AUTH_CALLBACK_COOKIE,
            "value": _encoded_oauth_callback_cookie_value(),
            "domain": "novita.ai",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
        {
            "name": NOVITA_AUTH_TYPE_COOKIE,
            "value": "google",
            "domain": "novita.ai",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        },
    ]
    try:
        page.context.add_cookies(cookies)
    except Exception:
        pass
    try:
        page.evaluate(
            """
            ({callbackName, callbackValue, typeName}) => {
              document.cookie = `${callbackName}=${callbackValue}; path=/; SameSite=Lax; Secure`;
              document.cookie = `${typeName}=google; path=/; SameSite=Lax; Secure`;
            }
            """,
            {
                "callbackName": NOVITA_AUTH_CALLBACK_COOKIE,
                "callbackValue": _encoded_oauth_callback_cookie_value(),
                "typeName": NOVITA_AUTH_TYPE_COOKIE,
            },
        )
    except Exception:
        pass


def _open_oauth_url_nonblocking(page, url: str, *, log_fn=print) -> bool:
    """用新标签非阻塞打开 Google OAuth URL，避免跨站 goto 等 load 超时。"""
    if not url:
        return False
    attempts: list[str] = []
    try:
        new_page = page.context.new_page()
        new_page.set_default_navigation_timeout(15000)
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
        if "accounts.google.com" not in str(new_page.url or ""):
            try:
                new_page.goto(url, wait_until="commit", timeout=12000)
            except Exception as exc:
                if "Timeout" not in str(exc) and "Navigation" not in str(exc):
                    raise
        log_fn(f"[Novita] 已用协议 URL 发起 Google OAuth: {new_page.url}")
        return True
    except Exception as exc:
        attempts.append(f"new_page={exc!r}")
    try:
        page.evaluate("u => { window.open(u, '_blank'); }", url)
        log_fn("[Novita] 已用 window.open 发起 Google OAuth")
        return True
    except Exception as exc:
        attempts.append(f"window_open={exc!r}")
    try:
        page.evaluate("u => { window.location.assign(u); }", url)
        log_fn("[Novita] 已用当前标签发起 Google OAuth")
        return True
    except Exception as exc:
        attempts.append(f"same_page={exc!r}")
    log_fn(f"[Novita] Google OAuth URL 打开失败: {'; '.join(attempts)}")
    return False


def _open_google_oauth_protocol(page, *, log_fn=print) -> bool:
    """协议化启动 Novita Google OAuth，按钮点击仅作为兜底。"""
    try:
        if "novita.ai" not in str(page.url or ""):
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        log_fn(f"[Novita] 打开登录页失败，继续协议发起 OAuth: {exc!r}")
    _set_novita_oauth_cookies(page)
    return _open_oauth_url_nonblocking(page, _build_google_oauth_url(), log_fn=log_fn)


def _verify_api_key_http(api_key: str, *, proxy: str | None = None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "reason": "missing_api_key", "path": VERIFY_PATH}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(
            f"{API_BASE}{VERIFY_PATH}",
            headers={"Authorization": api_key, "X-Novita-Source": "any-auto-register", "X-Api-Source": "any-auto-register"},
            proxies=proxies,
            timeout=30,
        )
        return {"ok": response.ok, "status": response.status_code, "path": VERIFY_PATH, "url": f"{API_BASE}{VERIFY_PATH}", "body": _json_or_text(response)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "path": VERIFY_PATH}


def _click_novita_google_button(page, *, log_fn=print) -> bool:
    """点击 Novita 登录页的 Google 按钮，兼容中文 UI 和图标包裹层。"""
    try:
        target = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
              };
              const candidates = [...document.querySelectorAll('button, a, [role="button"], div, span')]
                .filter(el => visible(el));
              let best = null;
              for (const node of candidates) {
                const text = [
                  node.innerText || '',
                  node.textContent || '',
                  node.getAttribute('aria-label') || '',
                  node.getAttribute('title') || '',
                  node.getAttribute('href') || '',
                ].join(' ').trim();
                if (!text) continue;
                const lowered = text.toLowerCase();
                const isGoogle = lowered.includes('google') || text.includes('使用 Google 登录') || text.includes('使用Google登录');
                if (!isGoogle) continue;
                const clickable = node.closest('button, a, [role="button"]') || node;
                if (!visible(clickable) || clickable.disabled) continue;
                const r = clickable.getBoundingClientRect();
                const exactText = text === '使用 Google 登录' || text === '使用Google登录';
                const isNativeClickable = ['BUTTON', 'A'].includes(clickable.tagName) || clickable.getAttribute('role') === 'button';
                const googleId = (clickable.id || '').toLowerCase().includes('google');
                const area = r.width * r.height;
                // Novita 页面的大容器也包含 Google 文本，必须偏向最小真实按钮。
                if (!googleId && !exactText && area > 90000) continue;
                const score = (googleId ? 20 : 0)
                  + (exactText ? 12 : 0)
                  + (isNativeClickable ? 8 : 0)
                  + (lowered.includes('google') ? 4 : 0)
                  - Math.min(10, area / 50000);
                if (!best || score > best.score) {
                  best = {
                    node: clickable,
                    score,
                    boundingBox: {x: r.x, y: r.y, width: r.width, height: r.height},
                    text: text.slice(0, 120),
                    id: clickable.id || ''
                  };
                }
              }
              if (!best) return null;
              best.node.scrollIntoView({block: 'center', inline: 'center'});
              const r = best.node.getBoundingClientRect();
              best.boundingBox = {x: r.x, y: r.y, width: r.width, height: r.height};
              return {boundingBox: best.boundingBox, text: best.text, id: best.id};
            }
            """
        )
        if not target or not target.get("boundingBox"):
            return False
        box = target["boundingBox"]
        x = float(box["x"]) + float(box["width"]) / 2
        y = float(box["y"]) + float(box["height"]) / 2
        page.mouse.click(x, y)
        log_fn(f"[Novita] 已用真实鼠标点击 Google 登录按钮: id={target.get('id', '')} text={target.get('text', '')}")
        return True
    except Exception as exc:
        log_fn(f"[Novita] 点击 Google 登录按钮失败: {exc!r}")
        return False


def _start_google_oauth(browser: OAuthBrowser, page, *, log_fn=print) -> bool:
    # Novita 前端 bundle 的 Google 按钮实际只是设置 auth cookie 后跳到
    # accounts.google.com/o/oauth2/v2/auth。实测中文登录页按钮能定位但点击后
    # URL 可能不变，所以优先按协议直接构造 OAuth URL；按钮点击只做兜底。
    if _open_google_oauth_protocol(page, log_fn=log_fn):
        return True
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(1)
    except Exception as exc:
        log_fn(f"[Novita] 打开登录页失败，继续尝试 OAuth: {exc!r}")
    return bool(
        _click_novita_google_button(page, log_fn=log_fn)
        or try_click_provider_on_page(page, "google")
        or browser.try_click_provider("google")
    )


def _oauth_done(browser: OAuthBrowser) -> bool:
    cookies = _cookie_map(browser)
    return bool(_extract_token({}, cookies))


def _oauth_failed_reason(browser: OAuthBrowser) -> str:
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        if "auth_res=failed" in url:
            return url
        if "novita.ai" not in url:
            continue
        try:
            body = str(page.locator("body").inner_text(timeout=800) or "")
        except Exception:
            body = ""
        if "auth_res=failed" in body:
            return "auth_res=failed"
    return ""


def _oauth_done_or_failed(browser: OAuthBrowser) -> bool:
    return _oauth_done(browser) or bool(_oauth_failed_reason(browser))


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
) -> dict[str, Any]:
    if (oauth_provider or "google").strip().lower() != "google":
        raise RuntimeError(f"Novita 当前只支持 Google OAuth: {oauth_provider}")

    with OAuthBrowser(proxy=proxy, headless=headless, chrome_user_data_dir=chrome_user_data_dir, chrome_cdp_url=chrome_cdp_url, log_fn=log_fn) as browser:
        page = browser.new_page()
        if not _start_google_oauth(browser, page, log_fn=log_fn):
            raise RuntimeError("Novita 未找到 Google OAuth 入口")
        google_result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 220),
            log_fn=log_fn,
            stop_when=_oauth_done_or_failed,
        )
        log_fn(f"[Novita] Google OAuth driver 返回: url={getattr(google_result, 'last_url', '')}")
        deadline = time.time() + max(30, timeout)
        cookies = _cookie_map(browser)
        session_token = _extract_token({}, cookies)
        user_info = _get_user_info_http(cookies, token=session_token, proxy=proxy) if (cookies or session_token) else {"ok": False}
        while time.time() < deadline and not user_info.get("ok"):
            failed_reason = _oauth_failed_reason(browser)
            if failed_reason:
                raise RuntimeError(f"Novita OAuth 回调失败: {failed_reason}")
            cookies = _cookie_map(browser)
            session_token = session_token or _extract_token({}, cookies)
            if cookies or session_token:
                user_info = _get_user_info_http(cookies, token=session_token, proxy=proxy)
                if user_info.get("ok"):
                    break
            time.sleep(1)
        if not user_info.get("ok"):
            snapshot = google_oauth_snapshot(browser)
            raise RuntimeError(f"Novita OAuth 后未拿到用户信息: {user_info}; snapshot={snapshot[:2]}")

        actual_email = finalize_oauth_email(_extract_email(user_info, email_hint), email_hint, "Novita")
        questionnaire_result = _submit_questionnaire_http(cookies, token=session_token, email=actual_email, proxy=proxy)
        key_result = _create_api_key_http(cookies, token=session_token, name=f"auto-register-{int(time.time())}", proxy=proxy)
        api_key = str(key_result.get("api_key") or "").strip()
        if not key_result.get("ok") or not api_key:
            raise RuntimeError(f"Novita 已登录但协议创建/获取 API Key 失败: {key_result}")
        api_verification = _verify_api_key_http(api_key, proxy=proxy)
        balance = _get_balance_http(cookies, token=session_token, proxy=proxy)
        voucher = _get_voucher_http(cookies, token=session_token, proxy=proxy)

    return {
        "email": actual_email,
        "password": "",
        "api_key": api_key,
        "api_key_info": key_result.get("result") or {},
        "key_create_result": key_result,
        "api_verification": api_verification,
        "questionnaire_result": questionnaire_result,
        "balance": balance,
        "voucher": voucher,
        "session": user_info.get("data") or {},
        "session_token": session_token,
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "api_base": API_BASE,
        "auth_header": "Authorization",
    }


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
        raise RuntimeError("Novita 邮箱注册缺少 email")
    if not password:
        raise RuntimeError("Novita 邮箱注册缺少 password")
    if verification_link_callback is None:
        raise RuntimeError("Novita 邮箱注册缺少验证链接回调")

    session = _cookie_session({}, proxy=proxy)
    register_result = _register_email_http(session, email, password)
    if not register_result.get("ok"):
        raise RuntimeError(f"Novita 邮箱注册请求失败: {register_result}")

    log_fn("[Novita] 邮箱注册请求已提交，等待验证链接...")
    verification_link = verification_link_callback()
    if not verification_link:
        raise RuntimeError("Novita 未收到邮箱验证链接")

    verify_result = _verify_email_http(session, verification_link)
    if not verify_result.get("ok"):
        raise RuntimeError(f"Novita 邮箱验证失败: {verify_result}")

    login_result = _login_email_http(session, email, password)
    if not login_result.get("ok"):
        raise RuntimeError(f"Novita 邮箱登录失败: {login_result}")

    cookies = _session_cookie_map(session)
    session_token = str(login_result.get("session_token") or _extract_token(login_result.get("data"), cookies) or "").strip()
    user_info = _get_user_info_http(cookies, token=session_token, proxy=proxy)
    deadline = time.time() + max(15, min(timeout, 120))
    while time.time() < deadline and not user_info.get("ok"):
        time.sleep(1)
        cookies = _session_cookie_map(session)
        user_info = _get_user_info_http(cookies, token=session_token, proxy=proxy)
    if not user_info.get("ok"):
        raise RuntimeError(f"Novita 邮箱验证/登录后未拿到用户信息: {user_info}; cookies={list(cookies)}")

    actual_email = _extract_email(user_info, email)
    questionnaire_result = _submit_questionnaire_http(cookies, token=session_token, email=actual_email, proxy=proxy)
    key_result = _create_api_key_http(cookies, token=session_token, name=f"auto-register-{int(time.time())}", proxy=proxy)
    api_key = str(key_result.get("api_key") or "").strip()
    if not key_result.get("ok") or not api_key:
        raise RuntimeError(f"Novita 邮箱流已登录但创建/获取 API Key 失败: {key_result}")
    api_verification = _verify_api_key_http(api_key, proxy=proxy)
    balance = _get_balance_http(cookies, token=session_token, proxy=proxy)
    voucher = _get_voucher_http(cookies, token=session_token, proxy=proxy)

    return {
        "email": actual_email,
        "password": password,
        "api_key": api_key,
        "api_key_info": key_result.get("result") or {},
        "key_create_result": key_result,
        "api_verification": api_verification,
        "questionnaire_result": questionnaire_result,
        "balance": balance,
        "voucher": voucher,
        "register_result": register_result,
        "verification_link": verification_link,
        "verify_result": verify_result,
        "login_result": login_result,
        "session": user_info.get("data") or {},
        "session_token": session_token,
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "api_base": API_BASE,
        "auth_header": "Authorization",
    }
