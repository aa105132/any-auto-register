"""CometAPI 协议优先注册：邮箱 OTP / Google OAuth + API Key + 新手奖励状态。"""
from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import requests

from core.google_oauth import drive_google_oauth
from core.oauth_browser import OAuthBrowser, finalize_oauth_email

SITE_URL = "https://www.cometapi.com"
CONSOLE_URL = "https://www.cometapi.com/console"
LOGIN_URL = f"{CONSOLE_URL}/login"
TOKEN_URL = f"{CONSOLE_URL}/token"
API_BASE = "https://api.cometapi.com/v1"
TOKEN_PATH = "/api/token/"
VERIFY_PATH = "/models"
GOOGLE_OAUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def _build_session(proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": SITE_URL,
        "Referer": LOGIN_URL,
        "Cache-Control": "no-store",
    })
    return session


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


def _request_json(
    session: requests.Session,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    referer: str | None = None,
) -> dict[str, Any]:
    url = f"{SITE_URL}{path}" if path.startswith("/") else path
    headers = {}
    if referer:
        headers["Referer"] = referer
    response = session.request(method.upper(), url, json=body, headers=headers, timeout=45)
    data = _json_or_text(response)
    return {"ok": response.ok, "status": response.status_code, "data": data, "text": response.text[:2000], "url": response.url}


def _response_success(result: dict[str, Any]) -> bool:
    data = result.get("data")
    if isinstance(data, dict) and "success" in data:
        return bool(data.get("success"))
    return bool(result.get("ok"))


def _response_data(result: dict[str, Any]) -> Any:
    data = result.get("data")
    if isinstance(data, dict) and "data" in data:
        return data.get("data")
    return data


def _response_message(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("message", "error", "detail", "reason"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
    return str(result.get("text") or "")


def _require_success(label: str, result: dict[str, Any]) -> Any:
    if not _response_success(result):
        raise RuntimeError(f"CometAPI {label} 失败: HTTP {result.get('status')} {_response_message(result)}")
    return _response_data(result)


def _session_cookie_dict(session: requests.Session) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "")
        if not domain or "cometapi.com" in domain:
            cookies[str(cookie.name)] = str(cookie.value)
    return cookies


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k)


def _find_api_key(data: Any) -> str:
    if isinstance(data, str):
        match = re.search(r"sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{32,}", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("key", "token", "api_key", "apiKey", "value", "secret"):
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


def _extract_user(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if isinstance(data.get("user"), dict):
            return dict(data.get("user") or {})
        if any(k in data for k in ("id", "email", "username", "quota")):
            return dict(data)
    return {}


def _check_user_http(session: requests.Session, email: str) -> dict[str, Any]:
    return _request_json(session, f"/api/user/check?key={quote(email)}", referer=LOGIN_URL)


def _send_verification_http(session: requests.Session, email: str, *, turnstile: str = "") -> dict[str, Any]:
    return _request_json(
        session,
        f"/api/verification?email={quote(email)}&turnstile={quote(turnstile or '')}",
        referer=LOGIN_URL,
    )


def _login_email_http(
    session: requests.Session,
    email: str,
    code: str,
    *,
    turnstile: str = "",
    invite_code: str = "",
    castle_request_token: str = "",
) -> dict[str, Any]:
    body = {
        "email": email,
        "verification_code": code,
        "aff_code": invite_code,
        "castle_request_token": castle_request_token,
    }
    return _request_json(session, f"/api/user/login?turnstile={quote(turnstile or '')}", method="POST", body=body, referer=LOGIN_URL)


def _get_user_self_http(session: requests.Session) -> dict[str, Any]:
    return _request_json(session, "/api/user/self", referer=TOKEN_URL)


def _get_status_http(session: requests.Session) -> dict[str, Any]:
    # 控制台从该接口读取 GoogleOAuthEnabled / newbie_tasks / turnstile 配置。
    for path in ("/api/status", "/api/option/"):
        result = _request_json(session, path, referer=LOGIN_URL)
        if _response_success(result):
            return result
    return result


def _create_api_key_http(
    session: requests.Session,
    *,
    key_name: str = "default",
    log_fn=print,
) -> dict[str, Any]:
    name = str(key_name or "default").strip() or "default"
    payload = {
        "name": name,
        "credit_limit": 1,
        "expired_time": -1,
        "unlimited_quota": True,
        "model_limits_enabled": False,
        "model_limits": [],
        "allow_ips": "",
        "group": "",
        "status": 1,
    }
    create_result = _request_json(session, "/api/token/", method="post", body=payload, referer=TOKEN_URL)
    data = _require_success("创建 API Key", create_result)
    api_key = _find_api_key(data)
    if not api_key:
        list_result = _request_json(session, "/api/token", referer=TOKEN_URL)
        list_data = _require_success("读取 API Key", list_result)
        api_key = _find_api_key(list_data)
        data = {"create": data, "list": list_data}
    if api_key and not api_key.startswith("sk-"):
        api_key = f"sk-{api_key}"
    if not api_key:
        raise RuntimeError(f"CometAPI 创建 API Key 成功但未返回 key: {data}")
    log_fn(f"[CometAPI] API Key 已创建: {name}")
    return {"ok": True, "api_key": api_key, "api_key_info": data, "result": create_result}


def _verify_api_key_http(api_key: str, *, proxy: str | None = None) -> dict[str, Any]:
    verify_url = "https://api.cometapi.com/v1/models"
    if not api_key:
        return {"ok": False, "reason": "missing_api_key", "url": verify_url}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(
            verify_url,
            headers={"Authorization": f"Bearer {api_key}"},
            proxies=proxies,
            timeout=30,
        )
        body = _json_or_text(response)
        return {"ok": response.ok, "status": response.status_code, "url": verify_url, "body": body}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "url": verify_url}


def _claim_newbie_rewards_http(session: requests.Session, *, log_fn=print) -> dict[str, Any]:
    user_result = _get_user_self_http(session)
    user = _extract_user(_response_data(user_result)) if _response_success(user_result) else {}
    status_result = _get_status_http(session)
    status = _response_data(status_result) if _response_success(status_result) else {}
    tasks_config = []
    if isinstance(status, dict):
        tasks_config = status.get("newbie_tasks") or []
    tasks_state = {}
    for task in ("first_create_token", "first_api_call", "first_charge"):
        try:
            tasks_state[task] = bool(user.get(task)) if isinstance(user, dict) else False
        except Exception:
            tasks_state[task] = False
    claimed_bonus = 0
    if isinstance(tasks_config, list):
        for item in tasks_config:
            if isinstance(item, dict) and item.get("task") == "first_create_token" and item.get("enabled", True):
                try:
                    claimed_bonus = int(item.get("bonus") or 0)
                except Exception:
                    claimed_bonus = 0
    log_fn("[CometAPI] 已读取新手奖励/额度状态")
    return {
        "ok": True,
        "user": user,
        "status": status if isinstance(status, dict) else {},
        "tasks": tasks_state,
        "claimed_bonus": claimed_bonus,
        "user_result": user_result,
        "status_result": status_result,
    }


def register_with_email_otp(
    *,
    email: str,
    otp_callback,
    proxy: str | None = None,
    timeout: int = 300,
    log_fn=print,
    key_name: str = "default",
    invite_code: str = "",
    claim_rewards: bool = True,
    email_otp: Any = None,
) -> dict[str, Any]:
    resolved_email = str(email or "").strip()
    if not resolved_email:
        raise RuntimeError("CometAPI 邮箱 OTP 注册缺少邮箱地址")
    if otp_callback is None:
        raise RuntimeError("CometAPI 邮箱 OTP 注册缺少验证码回调")

    session = _build_session(proxy)
    session.get(LOGIN_URL, timeout=45)

    # 协议顺序: /api/user/check?key= -> /api/verification?email= -> /api/user/login?turnstile=
    # CometAPI 前端先检查邮箱是否存在，再发送验证码。
    check_result = _check_user_http(session, resolved_email)
    _require_success("检查用户", check_result)

    send_result = _send_verification_http(session, resolved_email)
    _require_success("发送邮箱验证码", send_result)
    log_fn("[CometAPI] 邮箱验证码已发送，等待收件...")

    code = str(email_otp or otp_callback() or "").strip()
    if not code:
        raise RuntimeError("CometAPI 邮箱来源未返回验证码")

    login_result = _login_email_http(session, resolved_email, code, invite_code=invite_code)
    login_data = _require_success("邮箱验证码登录", login_result)
    user = _extract_user(login_data)
    actual_email = str(user.get("email") or resolved_email).strip()

    key_result = _create_api_key_http(session, key_name=key_name, log_fn=log_fn)
    api_key = str(key_result.get("api_key") or "").strip()
    api_verification = _verify_api_key_http(api_key, proxy=proxy)
    rewards = _claim_newbie_rewards_http(session, log_fn=log_fn) if claim_rewards else {}
    if rewards.get("user"):
        user = dict(rewards.get("user") or user)

    cookies = _session_cookie_dict(session)
    return {
        "email": actual_email,
        "auth_method": "email_otp",
        "user": user,
        "user_id": str(user.get("id") or ""),
        "api_key": api_key,
        "api_key_info": dict(key_result.get("api_key_info") or {}),
        "key_create_result": key_result,
        "api_verification": api_verification,
        "newbie_rewards": rewards,
        "email_check_result": check_result,
        "email_otp_send_result": send_result,
        "email_otp_login_result": login_result,
        "cookies": cookies,
        "cookie_header": _cookie_header(cookies),
        "site_url": SITE_URL + "/",
        "dashboard_url": TOKEN_URL,
        "api_base": API_BASE,
    }


def _state_http(session: requests.Session) -> str:
    result = _request_json(session, "/api/oauth/state", referer=LOGIN_URL)
    data = _require_success("获取 OAuth state", result)
    return str(data or "").strip()


def _precheck_http(session: requests.Session, castle_request_token: str = "") -> dict[str, Any]:
    if not castle_request_token:
        return {"ok": True, "skipped": True}
    result = _request_json(session, f"/api/oauth/pre-check?castle_request_token={quote(castle_request_token)}", referer=LOGIN_URL)
    data = _require_success("OAuth pre-check", result)
    if isinstance(data, dict) and data.get("allowed") is False:
        raise RuntimeError(f"CometAPI OAuth pre-check 拒绝注册: {data}")
    return {"ok": True, "data": data}


def _status_google_config(session: requests.Session) -> dict[str, Any]:
    result = _get_status_http(session)
    data = _response_data(result) if _response_success(result) else {}
    return dict(data or {}) if isinstance(data, dict) else {}


def _build_google_oauth_url(status: dict[str, Any], state: str) -> str:
    client_id = str(status.get("google_client_id") or status.get("GoogleClientId") or "").strip()
    redirect_uri = str(status.get("oidc_redirect_uri") or f"{SITE_URL}/console/oauth/google").strip()
    if not client_id:
        raise RuntimeError(f"CometAPI 状态接口未返回 google_client_id: {status}")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": "email profile",
        "response_type": "code",
    }
    return f"{GOOGLE_OAUTH_URL}?{urlencode(params)}"


def _browser_cookies_to_session(browser: OAuthBrowser, session: requests.Session) -> dict[str, str]:
    cookies = browser.cookie_dict(domain_substrings=("cometapi.com", "www.cometapi.com"))
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="www.cometapi.com")
        session.cookies.set(name, value, domain=".cometapi.com")
    return cookies


def _wait_for_browser_login(browser: OAuthBrowser, session: requests.Session, *, timeout: int = 180) -> dict[str, Any]:
    deadline = time.time() + max(15, timeout)
    last_result: dict[str, Any] = {}
    while time.time() < deadline:
        _browser_cookies_to_session(browser, session)
        result = _get_user_self_http(session)
        last_result = result
        if _response_success(result):
            user = _extract_user(_response_data(result))
            if user:
                return user
        time.sleep(1)
    raise RuntimeError(f"CometAPI OAuth 登录超时，未获取到 /api/user/self: {last_result}")


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
    key_name: str = "default",
    claim_rewards: bool = True,
) -> dict[str, Any]:
    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("CometAPI 当前只支持 Google OAuth")

    session = _build_session(proxy)
    session.get(LOGIN_URL, timeout=45)
    # 协议优先: /api/oauth/state -> /api/oauth/pre-check -> accounts.google.com/o/oauth2/v2/auth；必要时由 OAuthBrowser/CDP 接管。
    state = _state_http(session)
    status = _status_google_config(session)
    _precheck_http(session, "")
    oauth_url = _build_google_oauth_url(status, state)

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
        try:
            page.evaluate("url => { window.location.assign(url); }", oauth_url)
        except Exception:
            page.goto(oauth_url, wait_until="commit", timeout=30000)
        drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=lambda _b: any("cometapi.com" in (p.url or "") and "/console/oauth/google" in (p.url or "") for p in browser.pages() if not p.is_closed()),
        )
        user = _wait_for_browser_login(browser, session, timeout=min(timeout, 180))
        cookies = _browser_cookies_to_session(browser, session)

    actual_email = finalize_oauth_email(str(user.get("email") or ""), email_hint, "CometAPI")
    key_result = _create_api_key_http(session, key_name=key_name, log_fn=log_fn)
    api_key = str(key_result.get("api_key") or "").strip()
    api_verification = _verify_api_key_http(api_key, proxy=proxy)
    rewards = _claim_newbie_rewards_http(session, log_fn=log_fn) if claim_rewards else {}
    if rewards.get("user"):
        user = dict(rewards.get("user") or user)

    cookies = _session_cookie_dict(session) or cookies
    return {
        "email": actual_email,
        "auth_method": "google_oauth",
        "oauth_provider": "google",
        "user": user,
        "user_id": str(user.get("id") or ""),
        "api_key": api_key,
        "api_key_info": dict(key_result.get("api_key_info") or {}),
        "key_create_result": key_result,
        "api_verification": api_verification,
        "newbie_rewards": rewards,
        "session": {"user": user},
        "cookies": cookies,
        "cookie_header": _cookie_header(cookies),
        "oauth_state": state,
        "oauth_url": oauth_url,
        "site_url": SITE_URL + "/",
        "dashboard_url": TOKEN_URL,
        "api_base": API_BASE,
    }
