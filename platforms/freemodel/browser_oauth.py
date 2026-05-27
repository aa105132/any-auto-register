"""FreeModel Google OAuth + 手机号验证 + API Key 创建流程。"""
from __future__ import annotations

import re
import time
from typing import Any

import requests

from core.google_oauth import drive_google_oauth
from core.oauth_browser import OAuthBrowser, finalize_oauth_email

SITE_URL = "https://freemodel.dev"
SITE_HOME_URL = f"{SITE_URL}/"
GOOGLE_OAUTH_URL = f"{SITE_URL}/api/auth/google/redirect"
API_BASE = "https://api.freemodel.dev"


def _redact_debug_text(value: str, *, limit: int = 360) -> str:
    text = str(value or "")
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
    text = re.sub(r"([?&](?:code|state)=)[^&\s]+", r"\1<redacted>", text)
    return text[:limit]


def _browser_debug_snapshot(browser: OAuthBrowser) -> list[dict[str, str]]:
    pages: list[dict[str, str]] = []
    for page in browser.pages():
        try:
            if page.is_closed():
                continue
            body = ""
            try:
                body = str(page.locator("body").inner_text(timeout=1200) or "")
            except Exception:
                body = ""
            pages.append({
                "url": _redact_debug_text(str(page.url or ""), limit=520),
                "body": _redact_debug_text(body, limit=520),
            })
        except Exception:
            continue
    return pages


def _site_pages(browser: OAuthBrowser) -> list:
    pages = []
    for page in browser.pages():
        try:
            if page.is_closed():
                continue
            if "freemodel.dev" in str(page.url or ""):
                pages.append(page)
        except Exception:
            continue
    return pages


def _ensure_site_page(browser: OAuthBrowser):
    pages = _site_pages(browser)
    if pages:
        return pages[-1]
    page = browser.active_page() or browser.new_page()
    page.goto(SITE_HOME_URL, wait_until="domcontentloaded", timeout=60000)
    return page


def _page_fetch_json_on_page(page, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    return dict(page.evaluate(
        """
        async ({path, method, body}) => {
          const headers = {"Accept": "application/json"};
          const options = {method, credentials: "include", headers};
          if (body !== null && body !== undefined) {
            headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(body);
          }
          const response = await fetch(path, options);
          const text = await response.text();
          let data = null;
          try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
          return {ok: response.ok, status: response.status, data, text};
        }
        """,
        {"path": path, "method": method.upper(), "body": body},
    ) or {})


def _page_fetch_json(browser: OAuthBrowser, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> dict[str, Any]:
    page = _ensure_site_page(browser)
    return _page_fetch_json_on_page(page, path, method=method, body=body)


def _result_detail(result: dict[str, Any]) -> Any:
    return result.get("data") if result.get("data") is not None else result.get("text")


def _result_error_text(result: dict[str, Any]) -> str:
    detail = _result_detail(result)
    if isinstance(detail, dict):
        parts: list[str] = []
        for key in ("error", "message", "msg", "detail", "reason", "code"):
            value = detail.get(key)
            if value not in (None, ""):
                parts.append(str(value))
        return " ".join(parts)
    return str(detail or "")


def _is_phone_send_retryable(result: dict[str, Any]) -> bool:
    if result.get("ok"):
        return False
    status = int(result.get("status") or 0)
    error_text = _result_error_text(result).lower()
    return status == 429 or "daily_limit_exceeded" in error_text


def _verified_at_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = str(
        payload.get("verifiedAt")
        or payload.get("verified_at")
        or payload.get("phoneVerifiedAt")
        or payload.get("phone_verified_at")
        or ""
    ).strip()
    if direct:
        return direct
    user = payload.get("user")
    if isinstance(user, dict):
        return str(
            user.get("verified_at")
            or user.get("verifiedAt")
            or user.get("phoneVerifiedAt")
            or user.get("phone_verified_at")
            or ""
        ).strip()
    return ""


def _already_verified_phone_result(verified_at: str, *, source: str = "site_state") -> dict[str, Any]:
    return {
        "phone": "",
        "verified_at": str(verified_at or "").strip(),
        "phone_send_result": {"already_verified": True, "source": source},
        "phone_verify_result": {},
    }


def _current_phone_verified_at_session(session: requests.Session) -> str:
    auth_me = _session_request_json(session, "/api/auth/me")
    if auth_me.get("ok"):
        verified_at = _verified_at_from_payload(auth_me.get("data"))
        if verified_at:
            return verified_at
    billing = _session_request_json(session, "/api/billing")
    if billing.get("ok"):
        verified_at = _verified_at_from_payload(billing.get("data"))
        if verified_at:
            return verified_at
    return ""


def _current_phone_verified_at_browser(browser: OAuthBrowser) -> str:
    auth_me = _page_fetch_json(browser, "/api/auth/me")
    if auth_me.get("ok"):
        verified_at = _verified_at_from_payload(auth_me.get("data"))
        if verified_at:
            return verified_at
    billing = _page_fetch_json(browser, "/api/billing")
    if billing.get("ok"):
        verified_at = _verified_at_from_payload(billing.get("data"))
        if verified_at:
            return verified_at
    return ""


def _release_phone_safely(phone_provider, phone_account, *, log_fn=print) -> None:
    try:
        released = phone_provider.release_phone(phone_account)
        if released:
            log_fn("[FreeModel] 已释放未使用手机号，准备换号重试")
    except Exception as exc:
        log_fn(f"[FreeModel] 释放未使用手机号失败，继续换号: {exc}")


def _require_ok(label: str, result: dict[str, Any]) -> Any:
    if not result.get("ok"):
        detail = _result_detail(result)
        raise RuntimeError(f"FreeModel {label} 失败: HTTP {result.get('status')} {detail}")
    return result.get("data")


def _try_current_user(browser: OAuthBrowser) -> dict[str, Any]:
    for page in _site_pages(browser):
        try:
            result = _page_fetch_json_on_page(page, "/api/auth/me")
            if result.get("ok") and isinstance(result.get("data"), dict):
                return dict(result.get("data") or {})
        except Exception:
            continue
    return {}


def _wait_for_user(browser: OAuthBrowser, *, timeout: int = 120) -> dict[str, Any]:
    deadline = time.time() + max(5, int(timeout or 120))
    last_payload: dict[str, Any] = {}
    while time.time() < deadline:
        payload = _try_current_user(browser)
        if payload:
            last_payload = payload
        user = payload.get("user") if isinstance(payload, dict) else None
        if isinstance(user, dict) and user:
            return user
        time.sleep(1)
    try:
        payload = _page_fetch_json(browser, "/api/auth/me")
        if payload.get("ok") and isinstance(payload.get("data"), dict):
            user = payload["data"].get("user")
            if isinstance(user, dict) and user:
                return user
            last_payload = dict(payload.get("data") or {})
    except Exception:
        pass
    snapshot = _browser_debug_snapshot(browser)
    raise RuntimeError(f"FreeModel OAuth 登录超时，未获取到 /api/auth/me 用户信息: {last_payload}; pages={snapshot}")


def _set_referral(browser: OAuthBrowser, invite_code: str, *, log_fn=print) -> dict[str, Any]:
    code = str(invite_code or "").strip()
    if not code:
        return {}
    result = _page_fetch_json(browser, "/api/auth/set-referral", method="POST", body={"code": code})
    data = _require_ok("设置邀请码", result)
    log_fn(f"[FreeModel] 已设置邀请码: {code}")
    return dict(data or {}) if isinstance(data, dict) else {"raw": data}


def _verify_phone(
    browser: OAuthBrowser,
    phone_provider,
    *,
    timeout: int = 180,
    poll_interval: int = 15,
    code_pattern: str | None = None,
    send_attempts: int = 3,
    log_fn=print,
) -> dict[str, Any]:
    verified_at = _current_phone_verified_at_browser(browser)
    if verified_at:
        log_fn("[FreeModel] 站点显示手机号已验证，跳过短信发送")
        return _already_verified_phone_result(verified_at, source="site_state")
    attempts = max(int(send_attempts or 1), 1)
    last_send_result: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        log_fn("[FreeModel] 等待手机号来源分配号码...")
        phone_account = phone_provider.get_phone()
        phone = str(getattr(phone_account, "phone", "") or "").strip()
        if not phone:
            raise RuntimeError("FreeModel 手机号来源未返回号码")
        suffix = f" ({attempt}/{attempts})" if attempts > 1 else ""
        log_fn(f"[FreeModel] 手机号来源返回号码{suffix}: {phone[:4]}****")

        send_result = _page_fetch_json(browser, "/api/phone/send-sms", method="POST", body={"phone": phone})
        last_send_result = send_result
        if not send_result.get("ok"):
            detail = _result_detail(send_result)
            if _is_phone_send_retryable(send_result) and attempt < attempts:
                log_fn(f"[FreeModel] 发送短信触发限流，释放当前号码并换号重试: HTTP {send_result.get('status')} {detail}")
                _release_phone_safely(phone_provider, phone_account, log_fn=log_fn)
                continue
            _require_ok("发送短信", send_result)
        send_data = send_result.get("data")
        verified_at = _verified_at_from_payload(send_data)
        if isinstance(send_data, dict) and send_data.get("already_verified") and verified_at:
            log_fn("[FreeModel] 站点返回手机号已验证，跳过短信验证码等待")
            return {
                "phone": phone,
                "verified_at": verified_at,
                "phone_send_result": dict(send_data),
                "phone_verify_result": {},
            }
        log_fn("[FreeModel] 短信已发送，等待验证码...")

        code = phone_provider.wait_for_code(
            phone_account,
            timeout=timeout,
            poll_interval=poll_interval,
            code_pattern=code_pattern,
        )
        if not code:
            raise RuntimeError("FreeModel 手机号来源未返回短信验证码")
        log_fn(f"[FreeModel] 短信验证码: {code}")

        verify_result = _page_fetch_json(browser, "/api/phone/verify", method="POST", body={"phone": phone, "code": code})
        verify_data = _require_ok("验证手机号", verify_result)
        verified_at = _verified_at_from_payload(verify_data)
        if not verified_at:
            refreshed = _page_fetch_json(browser, "/api/auth/me")
            if refreshed.get("ok") and isinstance(refreshed.get("data"), dict):
                user = refreshed["data"].get("user") or {}
                if isinstance(user, dict):
                    verified_at = _verified_at_from_payload(user)
        if not verified_at:
            log_fn("[FreeModel] 手机号验证接口未返回 verifiedAt，继续保存接口返回结果")
        return {
            "phone": phone,
            "verified_at": verified_at,
            "phone_send_result": dict(send_data or {}) if isinstance(send_data, dict) else {"raw": send_data},
            "phone_verify_result": dict(verify_data or {}) if isinstance(verify_data, dict) else {"raw": verify_data},
        }

    _require_ok("发送短信", last_send_result or {"ok": False, "status": 0, "text": "no send attempt"})
    raise RuntimeError("FreeModel 发送短信失败: 未执行有效尝试")


def _extract_api_key(key_data: Any) -> dict[str, str]:
    data = dict(key_data or {}) if isinstance(key_data, dict) else {}
    key_obj = data.get("key")
    key_dict = dict(key_obj or {}) if isinstance(key_obj, dict) else {}
    secret = str(
        data.get("secret")
        or data.get("apiKey")
        or data.get("api_key")
        or data.get("token")
        or key_dict.get("secret")
        or key_dict.get("apiKey")
        or key_dict.get("api_key")
        or (key_obj if isinstance(key_obj, str) else "")
        or ""
    ).strip()
    key_id = str(data.get("id") or data.get("keyId") or data.get("key_id") or key_dict.get("id") or "").strip()
    name = str(data.get("name") or key_dict.get("name") or "").strip()
    return {"api_key": secret, "api_key_id": key_id, "api_key_name": name}


def _create_api_key(browser: OAuthBrowser, *, key_name: str, log_fn=print) -> dict[str, Any]:
    name = str(key_name or "default").strip() or "default"
    result = _page_fetch_json(browser, "/api/keys", method="POST", body={"name": name})
    data = _require_ok("创建 API Key", result)
    extracted = _extract_api_key(data)
    if not extracted["api_key"]:
        raise RuntimeError(f"FreeModel 创建 API Key 成功但未返回 key/secret: {data}")
    log_fn(f"[FreeModel] API Key 已创建: {name}")
    return {
        **extracted,
        "api_key_info": dict(data or {}) if isinstance(data, dict) else {"raw": data},
        "key_create_result": dict(data or {}) if isinstance(data, dict) else {"raw": data},
    }


def _get_referral(browser: OAuthBrowser) -> dict[str, Any]:
    result = _page_fetch_json(browser, "/api/referral")
    data = _require_ok("获取邀请码", result)
    return dict(data or {}) if isinstance(data, dict) else {"raw": data}


def _build_session(proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": SITE_URL,
        "Referer": SITE_HOME_URL,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    })
    return session


def _session_request_json(
    session: requests.Session,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{SITE_URL}{path}" if path.startswith("/") else path
    response = session.request(method.upper(), url, json=body, timeout=45)
    text = response.text
    try:
        data = response.json() if text else None
    except Exception:
        data = None
    return {"ok": response.ok, "status": response.status_code, "data": data, "text": text[:2000]}


def _session_cookie_dict(session: requests.Session) -> dict[str, str]:
    return {cookie.name: cookie.value for cookie in session.cookies}


def _session_cookie_header(session: requests.Session) -> str:
    return "; ".join(f"{name}={value}" for name, value in _session_cookie_dict(session).items() if name)


def _verify_phone_session(
    session: requests.Session,
    phone_provider,
    *,
    timeout: int = 180,
    poll_interval: int = 15,
    code_pattern: str | None = None,
    send_attempts: int = 3,
    log_fn=print,
) -> dict[str, Any]:
    verified_at = _current_phone_verified_at_session(session)
    if verified_at:
        log_fn("[FreeModel] 站点显示手机号已验证，跳过短信发送")
        return _already_verified_phone_result(verified_at, source="site_state")
    attempts = max(int(send_attempts or 1), 1)
    last_send_result: dict[str, Any] | None = None
    for attempt in range(1, attempts + 1):
        log_fn("[FreeModel] 等待手机号来源分配号码...")
        phone_account = phone_provider.get_phone()
        phone = str(getattr(phone_account, "phone", "") or "").strip()
        if not phone:
            raise RuntimeError("FreeModel 手机号来源未返回号码")
        suffix = f" ({attempt}/{attempts})" if attempts > 1 else ""
        log_fn(f"[FreeModel] 手机号来源返回号码{suffix}: {phone[:4]}****")

        send_result = _session_request_json(session, "/api/phone/send-sms", method="POST", body={"phone": phone})
        last_send_result = send_result
        if not send_result.get("ok"):
            detail = _result_detail(send_result)
            if _is_phone_send_retryable(send_result) and attempt < attempts:
                log_fn(f"[FreeModel] 发送短信触发限流，释放当前号码并换号重试: HTTP {send_result.get('status')} {detail}")
                _release_phone_safely(phone_provider, phone_account, log_fn=log_fn)
                continue
            _require_ok("发送短信", send_result)
        send_data = send_result.get("data")
        verified_at = _verified_at_from_payload(send_data)
        if isinstance(send_data, dict) and send_data.get("already_verified") and verified_at:
            log_fn("[FreeModel] 站点返回手机号已验证，跳过短信验证码等待")
            return {
                "phone": phone,
                "verified_at": verified_at,
                "phone_send_result": dict(send_data),
                "phone_verify_result": {},
            }
        log_fn("[FreeModel] 短信已发送，等待验证码...")

        code = phone_provider.wait_for_code(
            phone_account,
            timeout=timeout,
            poll_interval=poll_interval,
            code_pattern=code_pattern,
        )
        if not code:
            raise RuntimeError("FreeModel 手机号来源未返回短信验证码")
        log_fn(f"[FreeModel] 短信验证码: {code}")

        verify_result = _session_request_json(session, "/api/phone/verify", method="POST", body={"phone": phone, "code": code})
        verify_data = _require_ok("验证手机号", verify_result)
        verified_at = _verified_at_from_payload(verify_data)
        if not verified_at:
            refreshed = _session_request_json(session, "/api/auth/me")
            if refreshed.get("ok") and isinstance(refreshed.get("data"), dict):
                user = refreshed["data"].get("user") or {}
                if isinstance(user, dict):
                    verified_at = _verified_at_from_payload(user)
        return {
            "phone": phone,
            "verified_at": verified_at,
            "phone_send_result": dict(send_data or {}) if isinstance(send_data, dict) else {"raw": send_data},
            "phone_verify_result": dict(verify_data or {}) if isinstance(verify_data, dict) else {"raw": verify_data},
        }

    _require_ok("发送短信", last_send_result or {"ok": False, "status": 0, "text": "no send attempt"})
    raise RuntimeError("FreeModel 发送短信失败: 未执行有效尝试")


def _create_api_key_session(session: requests.Session, *, key_name: str, log_fn=print) -> dict[str, Any]:
    name = str(key_name or "default").strip() or "default"
    result = _session_request_json(session, "/api/keys", method="POST", body={"name": name})
    data = _require_ok("创建 API Key", result)
    extracted = _extract_api_key(data)
    if not extracted["api_key"]:
        raise RuntimeError(f"FreeModel 创建 API Key 成功但未返回 key/secret: {data}")
    log_fn(f"[FreeModel] API Key 已创建: {name}")
    return {
        **extracted,
        "api_key_info": dict(data or {}) if isinstance(data, dict) else {"raw": data},
        "key_create_result": dict(data or {}) if isinstance(data, dict) else {"raw": data},
    }


def _get_referral_session(session: requests.Session) -> dict[str, Any]:
    result = _session_request_json(session, "/api/referral")
    data = _require_ok("获取邀请码", result)
    return dict(data or {}) if isinstance(data, dict) else {"raw": data}


def register_with_email_otp(
    *,
    email: str,
    otp_callback,
    proxy: str | None = None,
    timeout: int = 300,
    log_fn=print,
    invite_code: str = "",
    phone_provider=None,
    phone_timeout: int = 180,
    phone_poll_interval: int = 15,
    phone_code_pattern: str | None = None,
    phone_send_attempts: int = 3,
    key_name: str = "default",
) -> dict[str, Any]:
    resolved_email = str(email or "").strip()
    if not resolved_email:
        raise RuntimeError("FreeModel 邮箱 OTP 注册缺少邮箱地址")
    if otp_callback is None:
        raise RuntimeError("FreeModel 邮箱 OTP 注册缺少验证码回调")

    session = _build_session(proxy)
    session.get(SITE_HOME_URL, timeout=45)

    referral_set_result: dict[str, Any] = {}
    if str(invite_code or "").strip():
        referral_result = _session_request_json(
            session,
            "/api/auth/set-referral",
            method="POST",
            body={"code": str(invite_code or "").strip()},
        )
        referral_data = _require_ok("设置邀请码", referral_result)
        referral_set_result = dict(referral_data or {}) if isinstance(referral_data, dict) else {"raw": referral_data}
        log_fn(f"[FreeModel] 已设置邀请码: {str(invite_code or '').strip()}")

    send_result = _session_request_json(session, "/api/auth/send-otp", method="POST", body={"email": resolved_email})
    send_data = _require_ok("发送邮箱验证码", send_result)
    log_fn("[FreeModel] 邮箱验证码已发送，等待收件...")

    code = str(otp_callback() or "").strip()
    if not code:
        raise RuntimeError("FreeModel 邮箱来源未返回验证码")

    verify_result = _session_request_json(
        session,
        "/api/auth/verify-otp",
        method="POST",
        body={"email": resolved_email, "code": code},
    )
    verify_data = _require_ok("验证邮箱验证码", verify_result)
    user = dict((verify_data or {}).get("user") or {}) if isinstance(verify_data, dict) else {}
    if not user:
        raise RuntimeError(f"FreeModel 邮箱验证码验证成功但未返回 user: {verify_data}")
    actual_email = str(user.get("email") or resolved_email).strip()
    if actual_email.lower() != resolved_email.lower():
        raise RuntimeError(f"FreeModel 登录邮箱与预期不一致: 实际 {actual_email}，预期 {resolved_email}")

    phone_result: dict[str, Any] = {}
    if phone_provider is not None:
        phone_result = _verify_phone_session(
            session,
            phone_provider,
            timeout=phone_timeout,
            poll_interval=phone_poll_interval,
            code_pattern=phone_code_pattern,
            send_attempts=phone_send_attempts,
            log_fn=log_fn,
        )

    key_result = _create_api_key_session(session, key_name=key_name, log_fn=log_fn)
    referral = _get_referral_session(session)
    referral_code = str(referral.get("code") or "").strip()

    return {
        "email": actual_email,
        "auth_method": "邮箱 OTP",
        "user": user,
        "user_id": str(user.get("id") or ""),
        "used_invite_code": str(invite_code or "").strip(),
        "referral_set_result": referral_set_result,
        "email_otp_send_result": dict(send_data or {}) if isinstance(send_data, dict) else {"raw": send_data},
        "email_otp_verify_result": dict(verify_data or {}) if isinstance(verify_data, dict) else {"raw": verify_data},
        "referral": referral,
        "referral_code": referral_code,
        **phone_result,
        **key_result,
        "cookies": _session_cookie_dict(session),
        "cookie_header": _session_cookie_header(session),
        "site_url": SITE_HOME_URL,
        "api_base": API_BASE,
    }


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
    invite_code: str = "",
    phone_provider=None,
    phone_timeout: int = 180,
    phone_poll_interval: int = 15,
    phone_code_pattern: str | None = None,
    phone_send_attempts: int = 3,
    key_name: str = "default",
) -> dict:
    if (oauth_provider or "google").strip().lower() != "google":
        raise RuntimeError(f"FreeModel 当前只支持 Google OAuth: {oauth_provider}")

    with OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        reuse_existing_cdp=reuse_existing_cdp,
        log_fn=log_fn,
    ) as browser:
        page = browser.new_page()
        page.goto(SITE_HOME_URL, wait_until="domcontentloaded", timeout=60000)
        referral_set_result = _set_referral(browser, invite_code, log_fn=log_fn) if invite_code else {}

        try:
            page.goto(GOOGLE_OAUTH_URL, wait_until="commit", timeout=90000)
        except Exception as exc:
            current_url = str(getattr(page, "url", "") or "")
            log_fn(f"[FreeModel] OAuth 入口加载未完成，继续检查当前页面: {exc}")
            if "accounts.google.com" not in current_url and "freemodel.dev" not in current_url:
                raise

        drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(max(int(timeout or 300), 30), 240),
            log_fn=log_fn,
            stop_when=lambda b: bool((_try_current_user(b).get("user") or {})),
        )
        if chrome_cdp_url or chrome_user_data_dir:
            browser.auto_select_google_account(timeout=8)

        user = _wait_for_user(browser, timeout=min(max(int(timeout or 300), 30), 180))
        actual_email = finalize_oauth_email(str(user.get("email") or "").strip(), email_hint, "FreeModel")

        phone_result: dict[str, Any] = {}
        if phone_provider is not None:
            phone_result = _verify_phone(
                browser,
                phone_provider,
                timeout=phone_timeout,
                poll_interval=phone_poll_interval,
                code_pattern=phone_code_pattern,
                send_attempts=phone_send_attempts,
                log_fn=log_fn,
            )

        key_result = _create_api_key(browser, key_name=key_name, log_fn=log_fn)
        referral = _get_referral(browser)
        referral_code = str(referral.get("code") or "").strip()
        cookies = browser.cookie_dict(domain_substrings=("freemodel.dev",))

    return {
        "email": actual_email,
        "auth_method": "Google OAuth",
        "user": user,
        "user_id": str(user.get("id") or ""),
        "used_invite_code": str(invite_code or "").strip(),
        "referral_set_result": referral_set_result,
        "referral": referral,
        "referral_code": referral_code,
        **phone_result,
        **key_result,
        "cookies": cookies,
        "cookie_header": "; ".join(f"{name}={value}" for name, value in cookies.items() if name),
        "site_url": SITE_HOME_URL,
        "api_base": API_BASE,
    }
