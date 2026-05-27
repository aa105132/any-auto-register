"""Featherless 纯协议邮箱注册与 API Key 提取。"""
from __future__ import annotations

import re
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse

import requests

SITE_URL = "https://featherless.ai"
REGISTER_URL = f"{SITE_URL}/register"
LOGIN_URL = f"{SITE_URL}/login"
DASHBOARD_URL = f"{SITE_URL}/account/api-keys"
API_ORIGIN = "https://api.featherless.ai"
LLM_API_BASE = f"{API_ORIGIN}/v1"
REGISTER_PATH = "/auth/register"
LOGIN_PATH = "/auth/login"
ME_PATH = "/auth/me"
EMAIL_VERIFY_PATH = "/auth/email-verification"
API_KEYS_PATH = "/api-keys"
MODELS_PATH = "/models"
CHAT_COMPLETIONS_PATH = "/chat/completions"


def _llm_api_url(path: str = "") -> str:
    normalized = str(path or "").strip()
    if not normalized:
        return LLM_API_BASE.rstrip("/")
    return f"{LLM_API_BASE.rstrip('/')}/{normalized.lstrip('/')}"

_KEY_RE = re.compile(r"(?:fls|featherless|sk)[_\-][A-Za-z0-9_\-]{12,}|[A-Za-z0-9_\-]{32,}")


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


def _make_auth_client_id() -> str:
    return str(uuid.uuid4())


def _cookie_session(
    cookies: dict[str, str] | None = None,
    *,
    proxy: str | None = None,
    auth_client_id: str = "",
) -> requests.Session:
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": SITE_URL,
        "Referer": REGISTER_URL,
    })
    client_id = str(auth_client_id or _make_auth_client_id()).strip()
    if client_id:
        session.headers.update({"x-feather-auth-client-id": client_id})
    for name, value in (cookies or {}).items():
        if not value:
            continue
        session.cookies.set(str(name), str(value), domain="api.featherless.ai")
        session.cookies.set(str(name), str(value), domain=".featherless.ai")
        session.cookies.set(str(name), str(value), domain="featherless.ai")
    return session


def _session_cookie_map(session: requests.Session) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "")
        if not domain or "featherless.ai" in domain:
            cookies[str(cookie.name)] = str(cookie.value)
    return cookies


def _find_api_key(data: Any) -> str:
    if isinstance(data, str):
        value = data.strip()
        if not value or value.lower() in {"masked", "****", "null", "none"}:
            return ""
        match = _KEY_RE.search(value)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("key", "api_key", "apiKey", "secret", "token", "value"):
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


def _extract_user(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        for key in ("user", "account", "profile"):
            value = data.get(key)
            if isinstance(value, dict) and (value.get("email") or value.get("id")):
                return dict(value)
        if data.get("email") or data.get("id"):
            return dict(data)
        for key in ("data", "result", "payload"):
            value = data.get(key)
            user = _extract_user(value)
            if user:
                return user
    return {}


def _register_email_http(session: requests.Session, email: str, password: str) -> dict[str, Any]:
    state_variants = [
        {"email_verification_return_to": "/account"},
        {"returnTo": "/account"},
        {},
    ]
    attempts: list[dict[str, Any]] = []
    for state in state_variants:
        payload = {"email": email, "password": password}
        if state:
            payload["state"] = state
        try:
            response = session.post(urljoin(API_ORIGIN, REGISTER_PATH), json=payload, timeout=45)
            data = _json_or_text(response)
            message = _response_message(data).lower()
            user = _extract_user(data)
            already_exists = response.status_code in {400, 409, 422} and any(token in message for token in ("exist", "already", "registered"))
            needs_verify = any(token in message for token in ("verify", "verification"))
            ok = bool(response.ok or already_exists or needs_verify)
            item = {
                "ok": ok,
                "status": response.status_code,
                "data": data,
                "user": user,
                "payload_keys": list(payload.keys()),
                "already_exists": already_exists,
                "needs_verify": needs_verify,
                "cookies": _session_cookie_map(session),
            }
            attempts.append(item)
            if ok:
                item["attempts"] = attempts
                return item
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload_keys": list(payload.keys())})
    return {"ok": False, "attempts": attempts, "cookies": _session_cookie_map(session)}


def _extract_verification_token(verification_link: str) -> str:
    if not verification_link:
        return ""
    parsed = urlparse(verification_link)
    query = parse_qs(parsed.query or "")
    for key in ("token", "code", "verification_token", "email_verification_token"):
        values = query.get(key) or []
        if values and str(values[0] or "").strip():
            return str(values[0] or "").strip()
    fragment = parse_qs(parsed.fragment or "")
    for key in ("token", "code"):
        values = fragment.get(key) or []
        if values and str(values[0] or "").strip():
            return str(values[0] or "").strip()
    path_tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if len(path_tail) >= 16 and re.fullmatch(r"[A-Za-z0-9._\-]+", path_tail):
        return path_tail
    return ""


def _verify_email_http(session: requests.Session, verification_link: str) -> dict[str, Any]:
    if not verification_link:
        return {"ok": False, "reason": "missing_verification_link"}
    token = _extract_verification_token(verification_link)
    attempts: list[dict[str, Any]] = []
    if token:
        for payload in ({"token": token}, {"code": token}):
            try:
                response = session.post(urljoin(API_ORIGIN, EMAIL_VERIFY_PATH), json=payload, timeout=45)
                data = _json_or_text(response)
                item = {"ok": response.ok, "status": response.status_code, "data": data, "payload": payload, "cookies": _session_cookie_map(session)}
                attempts.append(item)
                if response.ok:
                    item["attempts"] = attempts
                    return item
            except Exception as exc:
                attempts.append({"ok": False, "error": repr(exc), "payload": payload})
    try:
        response = session.get(verification_link, allow_redirects=True, timeout=60)
        data = _json_or_text(response, limit=1000)
        ok = bool(response.ok or "verify" in str(response.url).lower() or "account" in str(response.url).lower())
        return {"ok": ok, "status": response.status_code, "url": response.url, "data": data, "attempts": attempts, "cookies": _session_cookie_map(session)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "attempts": attempts, "url": verification_link, "cookies": _session_cookie_map(session)}


def _login_email_http(session: requests.Session, email: str, password: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for payload in ({"email": email, "password": password}, {"username": email, "password": password}):
        try:
            response = session.post(urljoin(API_ORIGIN, LOGIN_PATH), json=payload, timeout=45)
            data = _json_or_text(response)
            user = _extract_user(data)
            item = {
                "ok": bool(response.ok and (user or data)),
                "status": response.status_code,
                "data": data,
                "user": user,
                "cookies": _session_cookie_map(session),
                "payload_keys": list(payload.keys()),
            }
            attempts.append(item)
            if item["ok"]:
                item["attempts"] = attempts
                return item
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload_keys": list(payload.keys())})
    return {"ok": False, "attempts": attempts, "cookies": _session_cookie_map(session)}


def _get_me_http(session: requests.Session) -> dict[str, Any]:
    try:
        response = session.get(urljoin(API_ORIGIN, ME_PATH), timeout=30)
        data = _json_or_text(response)
        user = _extract_user(data)
        return {"ok": bool(response.ok and user), "status": response.status_code, "data": data, "user": user, "cookies": _session_cookie_map(session)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}, "cookies": _session_cookie_map(session)}


def _list_api_keys_http(session: requests.Session) -> dict[str, Any]:
    try:
        response = session.get(urljoin(API_ORIGIN, API_KEYS_PATH), timeout=30)
        data = _json_or_text(response)
        return {"ok": response.ok, "status": response.status_code, "data": data, "api_key": _find_api_key(data)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _create_api_key_http(session: requests.Session, *, name: str = "auto-register") -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    payloads = [{"name": name}, {"name": name[:50]}, {}]
    for payload in payloads:
        try:
            response = session.post(urljoin(API_ORIGIN, API_KEYS_PATH), json=payload, timeout=45)
            data = _json_or_text(response)
            api_key = _find_api_key(data)
            item = {"ok": response.ok, "status": response.status_code, "data": data, "api_key": api_key, "payload": payload}
            attempts.append(item)
            if response.ok and api_key:
                return {"ok": True, "api_key": api_key, "api_key_info": data if isinstance(data, dict) else {"raw": data}, "result": item, "attempts": attempts}
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload": payload})
    list_result = _list_api_keys_http(session)
    attempts.append({"action": "list", **list_result})
    api_key = _find_api_key(list_result.get("data"))
    if list_result.get("ok") and api_key:
        return {"ok": True, "api_key": api_key, "api_key_info": list_result.get("data") or {}, "result": list_result, "attempts": attempts}
    return {"ok": False, "api_key": "", "attempts": attempts}


def _verify_api_key_http(api_key: str, *, proxy: str | None = None, deep: bool = False) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "reason": "missing_api_key", "path": MODELS_PATH}
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    try:
        models_url = _llm_api_url(MODELS_PATH)
        response = session.get(models_url, timeout=30)
        data = _json_or_text(response, limit=1000)
        result: dict[str, Any] = {
            "ok": response.ok,
            "status": response.status_code,
            "method": "GET",
            "url": models_url,
            "body": data,
            "note": "models endpoint may be public; status only proves key format/request path, not billable inference access",
        }
        if deep:
            payload = {"model": "meta-llama/Meta-Llama-3.1-8B-Instruct", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
            chat = session.post(_llm_api_url(CHAT_COMPLETIONS_PATH), json=payload, timeout=45)
            result["chat_completions"] = {"ok": chat.ok, "status": chat.status_code, "body": _json_or_text(chat, limit=1200)}
            result["ok"] = bool(chat.ok)
        return result
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "path": MODELS_PATH}


class FeatherlessProtocolMailboxWorker:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.session = _cookie_session(proxy=proxy)
        self.proxy = proxy
        self.log = log_fn

    def run(
        self,
        *,
        email: str,
        password: str,
        verification_link_callback: Callable[[], str] | None = None,
        key_name: str = "auto-register",
        verify_deep: bool = False,
    ) -> dict:
        register_result = _register_email_http(self.session, email, password)
        if not register_result.get("ok"):
            raise RuntimeError(f"Featherless 邮箱注册失败: {register_result}")

        verify_result: dict[str, Any] = {}
        login_result: dict[str, Any] = {}
        post_login_me: dict[str, Any] = {}
        pre_verify_me = _get_me_http(self.session)
        register_user = dict(register_result.get("user") or {})
        pre_verify_user = dict(pre_verify_me.get("user") or {})
        email_verified = register_user.get("email_verified") is True or pre_verify_user.get("email_verified") is True

        if register_result.get("already_exists") and not email_verified:
            login_result = _login_email_http(self.session, email, password)
            if not login_result.get("ok"):
                raise RuntimeError(f"Featherless 账号已存在但无法登录，请更换邮箱或使用原密码: {login_result}")
            post_login_me = _get_me_http(self.session)
            login_user = dict(login_result.get("user") or {})
            post_login_user = dict(post_login_me.get("user") or {})
            email_verified = (
                login_user.get("email_verified") is True
                or post_login_user.get("email_verified") is True
            )
            if email_verified:
                self.log("Featherless 账号已存在且邮箱已验证，跳过等待验证邮件")

        needs_email_verification = bool(register_result.get("needs_verify") or not email_verified)
        if needs_email_verification:
            if not verification_link_callback:
                raise RuntimeError("Featherless 注册需要验证链接回调，请配置可收信的邮箱来源")
            self.log("Featherless 邮箱未验证，等待验证链接以激活 API access/free trial...")
            verification_link = verification_link_callback()
            if not verification_link:
                raise RuntimeError("Featherless: 未获取到验证链接")
            verify_result = _verify_email_http(self.session, verification_link)
            if not verify_result.get("ok"):
                raise RuntimeError(f"Featherless 邮箱验证失败: {verify_result}")

        if not login_result:
            login_result = _login_email_http(self.session, email, password)
        if not login_result.get("ok"):
            raise RuntimeError(f"Featherless 登录失败: {login_result}")

        me_result = post_login_me if post_login_me.get("ok") else _get_me_http(self.session)
        user = dict(me_result.get("user") or login_result.get("user") or {})
        create_result = _create_api_key_http(self.session, name=key_name)
        if not create_result.get("ok"):
            raise RuntimeError(f"Featherless 创建 API Key 失败: {create_result}")
        api_key = str(create_result.get("api_key") or "").strip()
        api_verification = _verify_api_key_http(api_key, proxy=self.proxy, deep=verify_deep)
        cookies = _session_cookie_map(self.session)
        return {
            "email": email,
            "password": password,
            "user": user,
            "api_key": api_key,
            "api_key_info": dict(create_result.get("api_key_info") or {}),
            "api_verification": api_verification,
            "key_create_result": create_result.get("result") or create_result,
            "register_result": register_result,
            "verify_result": verify_result,
            "pre_verify_me": pre_verify_me,
            "login_result": login_result,
            "me": me_result,
            "session": user,
            "cookies": cookies,
            "cookie_header": "; ".join(f"{name}={value}" for name, value in cookies.items() if value),
            "auth_method": "email",
            "api_base": LLM_API_BASE,
            "site_url": SITE_URL,
            "dashboard_url": DASHBOARD_URL,
        }
