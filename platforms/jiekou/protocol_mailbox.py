"""Jiekou AI 纯协议邮箱注册、问卷奖励确认与 API Key 提取。"""
from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests

SITE_URL = "https://jiekou.ai"
REGISTER_URL = f"{SITE_URL}/user/register"
LOGIN_URL = f"{SITE_URL}/user/login"
DASHBOARD_URL = f"{SITE_URL}/settings/key-management"
CONTROL_API_BASE = "https://api-server.jiekou.ai"
LLM_API_BASE = "https://api.jiekou.ai"
OPENAI_API_BASE = "https://api.jiekou.ai/v1"
# 前端 Base URL 弹窗展示的实际兼容入口；保留 OPENAI_API_BASE 兼容现有测试/旧调用侧。
OPENAI_COMPAT_API_BASE = "https://api.jiekou.ai/openai"
OPENAI_COMPAT_V1_API_BASE = "https://api.jiekou.ai/openai/v1"
HIGHWAY_API_BASE = "https://api.highwayapi.ai/openai"
TURNSTILE_SITE_KEY = "0x4AAAAAAB1sNhmgzD9Pm-oE"

REGISTER_PATH = "/v1/user/register"
LOGIN_PATH = "/v1/user/login"
USER_INFO_PATH = "/v1/user/info"
EMAIL_VERIFY_PATH = "/v1/user/email/verify"
QUESTIONNAIRE_PATH = "/v1/user/questionnaire"
API_KEYS_PATH = "/v2/user/key"
POINT_INFO_PATH = "/v1/user/pointInfo"
VOUCHER_NUM_PATH = "/v1/billing/voucher/num"
VOUCHER_LIST_PATH = "/v1/billing/voucher/list"
BALANCE_TOTAL_PATH = "/v1/billing/balance/total"
MODELS_PATH = "/v1/models"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
API_VERIFICATION_MODEL = "gpt-4o-mini"

_KEY_RE = re.compile(r"(?:sk|jk|jiekou)[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-]{32,}")
_TOKEN_KEYS = (
    "token",
    "accessToken",
    "access_token",
    "jwt",
    "idToken",
    "id_token",
    "authToken",
    "authorization",
)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_STATIC_LINK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".map", ".woff", ".woff2")
_STATIC_LINK_PATH_HINTS = ("/logo/", "/logos/", "/asset/", "/assets/", "/_next/", "/static/", "/images/", "/img/")


def _looks_like_static_asset_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    path = str(parsed.path or "").lower()
    if any(path.endswith(suffix) for suffix in _STATIC_LINK_SUFFIXES):
        return True
    return any(hint in path for hint in _STATIC_LINK_PATH_HINTS)


def _api_url(path: str, *, base_url: str = CONTROL_API_BASE, query: dict[str, Any] | None = None) -> str:
    url = urljoin(base_url.rstrip("/") + "/", str(path or "").lstrip("/"))
    if query:
        clean = {key: value for key, value in query.items() if value not in (None, "")}
        if clean:
            url = f"{url}?{urlencode(clean, doseq=True)}"
    return url


def _llm_api_url(path: str = MODELS_PATH, *, base_url: str = OPENAI_COMPAT_V1_API_BASE) -> str:
    normalized = str(path or "").strip() or MODELS_PATH
    if base_url.rstrip("/").endswith("/v1") and normalized.startswith("/v1/"):
        normalized = normalized[3:]
    return urljoin(base_url.rstrip("/") + "/", normalized.lstrip("/"))


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


def _extract_jiekou_verification_link(text: str) -> str:
    combined = str(text or "")
    urls = [
        raw.strip().rstrip(").,;'")
        for raw in re.findall(r"https?://[^\s<>\"']+", combined, re.IGNORECASE)
    ]
    if not urls:
        return ""
    blocked_hosts = ("w3.org", "www.w3.org")
    candidates = []
    for url in urls:
        lower = url.lower()
        try:
            host = str(urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if host in blocked_hosts or _looks_like_static_asset_url(url):
            continue
        score = 0
        if "jiekou.ai" in host:
            score += 100
        if "email/verify" in lower or "verify" in lower or "verification" in lower:
            score += 50
        if "token=" in lower or "code=" in lower:
            score += 30
        if "api-server.jiekou.ai" in host:
            score += 10
        candidates.append((score, url))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates[0][0] > 0 else ""


def _build_session(proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/json",
            "Origin": SITE_URL,
            "Referer": REGISTER_URL,
        }
    )
    return session


def _session_cookie_map(session: requests.Session) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "")
        if not domain or "jiekou.ai" in domain:
            cookies[str(cookie.name)] = str(cookie.value)
    return cookies


def _import_cookies(session: requests.Session, cookies: dict[str, str]) -> None:
    for name, value in (cookies or {}).items():
        if not name or value in (None, ""):
            continue
        session.cookies.set(str(name), str(value), domain="jiekou.ai")
        session.cookies.set(str(name), str(value), domain=".jiekou.ai")
        session.cookies.set(str(name), str(value), domain="api-server.jiekou.ai")


def _auth_value(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    return raw if raw.lower().startswith("bearer ") else f"Bearer {raw}"


def _request_json(
    session: requests.Session,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str | None = None,
    base_url: str = CONTROL_API_BASE,
    query: dict[str, Any] | None = None,
    referer: str | None = None,
    timeout: int = 45,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    auth = _auth_value(token or "")
    if auth:
        headers["Authorization"] = auth
    response = session.request(
        method.upper(),
        _api_url(path, base_url=base_url, query=query),
        json=body,
        headers=headers,
        timeout=timeout,
    )
    data = _json_or_text(response)
    return {
        "ok": response.ok,
        "status": response.status_code,
        "data": data,
        "text": response.text[:2000],
        "url": response.url,
        "cookies": _session_cookie_map(session),
    }


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


def _response_ok(result: dict[str, Any]) -> bool:
    data = result.get("data")
    if isinstance(data, dict):
        if data.get("code") in (0, "0", 200, "200"):
            return True
        if data.get("success") is True or data.get("ok") is True:
            return True
        if data.get("code") in (401, "401"):
            return False
    return bool(result.get("ok"))


def _payload_data(data: Any) -> Any:
    if isinstance(data, dict):
        for key in ("data", "payload", "result"):
            value = data.get(key)
            if value not in (None, ""):
                return value
    return data


def _looks_like_auth_token(value: str) -> bool:
    raw = str(value or "").strip()
    if not raw or _EMAIL_RE.match(raw):
        return False
    lowered = raw.lower()
    if lowered.startswith("bearer "):
        return _looks_like_auth_token(raw.split(" ", 1)[1])
    if raw.count(".") == 2 and len(raw) >= 24:
        return True
    if re.fullmatch(r"[A-Za-z0-9_\-]{32,}", raw):
        return True
    if len(raw) >= 32 and re.fullmatch(r"[A-Za-z0-9_+\-/]+={0,2}", raw):
        return True
    return False


def _extract_token(data: Any, *, _trusted_key: bool = False) -> str:
    if isinstance(data, str):
        value = data.strip()
        if value.lower().startswith("bearer "):
            value = value.split(" ", 1)[1].strip()
        if _trusted_key or _looks_like_auth_token(value):
            return value if _looks_like_auth_token(value) else ""
        return ""
    if isinstance(data, dict):
        for key in _TOKEN_KEYS:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                token = _extract_token(value, _trusted_key=True)
                if token:
                    return token
        for key in ("data", "payload", "result", "user", "session"):
            value = data.get(key)
            token = _extract_token(value)
            if token:
                return token
    if isinstance(data, list):
        for item in data:
            token = _extract_token(item)
            if token:
                return token
    return ""


def _extract_user(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        for key in ("user", "account", "profile"):
            value = data.get(key)
            if isinstance(value, dict) and (value.get("email") or value.get("uuid") or value.get("uid") or value.get("id")):
                return dict(value)
        payload = _payload_data(data)
        if payload is not data:
            user = _extract_user(payload)
            if user:
                return user
        if data.get("email") or data.get("uuid") or data.get("uid") or data.get("id"):
            return dict(data)
    return {}


def _find_api_key(data: Any) -> str:
    if isinstance(data, str):
        value = data.strip()
        if not value or "****" in value or value.lower() in {"masked", "null", "none"}:
            return ""
        match = _KEY_RE.search(value)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("apiKey", "api_key", "key", "token", "secret", "value"):
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


def _verification_query(verification_link: str) -> dict[str, str]:
    if not verification_link:
        return {}
    parsed = urlparse(verification_link)
    out: dict[str, str] = {}
    for query_text in (parsed.query, parsed.fragment):
        query = parse_qs(query_text or "")
        for key, values in query.items():
            if values and str(values[0] or "").strip():
                out[str(key)] = str(values[0] or "").strip()
    return out


def _extract_verification_token(verification_link: str) -> str:
    query = _verification_query(verification_link)
    for key in ("token", "code", "verifyCode", "verification_token", "emailToken"):
        if query.get(key):
            return query[key]
    if not verification_link:
        return ""
    parsed = urlparse(verification_link)
    path_tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if len(path_tail) >= 12 and re.fullmatch(r"[A-Za-z0-9._\-]+", path_tail):
        return path_tail
    return ""


def _register_email_http(
    session: requests.Session,
    email: str,
    password: str,
    turnstile: str,
    invite_code: str = "",
) -> dict[str, Any]:
    payload = {
        "email": email,
        "password": password,
        "confirmPassword": password,
        "redirectUrl": "/user/login",
        "cloudflareToken": turnstile,
        "allowNotification": True,
    }
    if invite_code:
        payload["fromInviteCode"] = invite_code
    result = _request_json(session, REGISTER_PATH, method="POST", body=payload, referer=REGISTER_URL)
    data = result.get("data")
    message = _response_message(data).lower()
    already_exists = result.get("status") in {400, 409, 422} and any(marker in message for marker in ("exist", "already", "registered", "已存在"))
    needs_verify = any(marker in message for marker in ("active", "verify", "验证", "激活"))
    result.update({"ok": bool(_response_ok(result) or already_exists or needs_verify), "already_exists": already_exists, "needs_verify": needs_verify, "payload_keys": list(payload.keys())})
    return result


def _verify_email_http(session: requests.Session, verification_link: str, email: str = "") -> dict[str, Any]:
    if not verification_link:
        return {"ok": False, "reason": "missing_verification_link"}
    token = _extract_verification_token(verification_link)
    query = _verification_query(verification_link)
    link_email = str(query.get("email") or query.get("mail") or email or "").strip()
    attempts: list[dict[str, Any]] = []
    if token:
        payloads = [
            {"token": token},
            {"code": token},
            {"verifyCode": token},
            {"emailToken": token},
            {"token": token, "email": link_email} if link_email else {"token": token},
            {"code": token, "email": link_email} if link_email else {"code": token},
        ]
        # 如果邮件链接里还有其它参数，原样补一次，兼容前端直接透传 query 的实现。
        if query:
            payloads.append(dict(query))
        for payload in payloads:
            try:
                result = _request_json(session, EMAIL_VERIFY_PATH, method="POST", body=payload, referer=verification_link)
                item = {**result, "payload": payload}
                attempts.append(item)
                if _response_ok(result):
                    item["attempts"] = attempts
                    return item
            except Exception as exc:
                attempts.append({"ok": False, "error": repr(exc), "payload": payload})
    try:
        response = session.get(verification_link, allow_redirects=True, timeout=60)
        data = _json_or_text(response, limit=1200)
        ok = bool(response.ok or "success" in str(response.url).lower() or "login" in str(response.url).lower())
        return {"ok": ok, "status": response.status_code, "url": response.url, "data": data, "attempts": attempts, "cookies": _session_cookie_map(session)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "url": verification_link, "attempts": attempts, "cookies": _session_cookie_map(session)}


def _login_email_http(
    session: requests.Session,
    email: str,
    password: str,
    turnstile: str,
    invite_code: str = "",
) -> dict[str, Any]:
    payload = {
        "email": email,
        "password": password,
        "redirectUrl": "/settings/key-management",
        "cloudflareToken": turnstile,
    }
    if invite_code:
        payload["fromInviteCode"] = invite_code
    result = _request_json(session, LOGIN_PATH, method="POST", body=payload, referer=LOGIN_URL)
    token = _extract_token(result.get("data"))
    if token:
        session.cookies.set("token", token, domain="jiekou.ai")
        session.cookies.set("token", token, domain=".jiekou.ai")
    user = _extract_user(result.get("data"))
    result.update({"ok": bool(_response_ok(result) and (token or user or result.get("data"))), "token": token, "user": user})
    return result


def _get_user_info_http(session: requests.Session, token: str) -> dict[str, Any]:
    result = _request_json(session, USER_INFO_PATH, token=token, referer=DASHBOARD_URL)
    result["user"] = _extract_user(result.get("data"))
    result["ok"] = bool(_response_ok(result) and (result.get("user") or result.get("data")))
    return result


def _questionnaire_payloads(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    # 运行时服务端 validator 显示最小必填字段为 UserQuestionnaireRequest.Name。
    defaults = [
        {"name": "Auto Register"},
        {
            "name": "Auto Register",
            "companyName": "个人开发者",
            "country": "CN",
            "role": "developer",
            "usage": "LLM API integration",
            "source": "auto-register",
            "companySize": "1-10",
        },
        {
            "companyName": "Personal Developer",
            "country": "CN",
            "job": "developer",
            "useCase": "OpenAI compatible API testing",
            "source": "search",
        },
        {
            "name": "auto-register",
            "company": "个人开发者",
            "question": "LLM API integration",
            "answer": "OpenAI compatible API testing",
        },
        {},
    ]
    if payload:
        return [dict(payload), *defaults]
    return defaults


def _submit_questionnaire_http(
    session: requests.Session,
    token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for item_payload in _questionnaire_payloads(payload):
        try:
            result = _request_json(session, QUESTIONNAIRE_PATH, method="POST", body=item_payload, token=token, referer=SITE_URL)
            result["payload"] = item_payload
            attempts.append(result)
            message = _response_message(result.get("data")).lower()
            ok = _response_ok(result) or (result.get("ok") and ("success" in message or "成功" in message))
            if ok:
                return {"ok": True, "result": result, "attempts": attempts, "payload": item_payload}
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload": item_payload})
    return {"ok": False, "attempts": attempts}


def _get_point_info_http(session: requests.Session, token: str) -> dict[str, Any]:
    return _request_json(session, POINT_INFO_PATH, token=token, referer=SITE_URL)


def _get_voucher_list_http(session: requests.Session, token: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    query_variants = [None, {"templateIds": []}, {"templateIds": ["questionnaire"]}, {"templateIds": ["new_user", "questionnaire"]}]
    for query in query_variants:
        try:
            result = _request_json(session, VOUCHER_LIST_PATH, token=token, query=query, referer=SITE_URL)
            result["query"] = query
            attempts.append(result)
            if _response_ok(result) or _has_usd_one_voucher(result.get("data")):
                result["attempts"] = attempts
                return result
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "query": query})
    return {"ok": False, "attempts": attempts}


def _get_voucher_num_http(session: requests.Session, token: str, business_type: str = "") -> dict[str, Any]:
    return _request_json(session, VOUCHER_NUM_PATH, token=token, query={"businessType": business_type} if business_type else None, referer=SITE_URL)


def _get_balance_total_http(session: requests.Session, token: str) -> dict[str, Any]:
    return _request_json(session, BALANCE_TOTAL_PATH, token=token, referer=SITE_URL)


def _numeric_amount(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except Exception:
        return 0.0
    # 前端把 voucherBalance 这类 raw 值除以 1e4；amountOff 同样兼容 raw/decimal 两种形态。
    if abs(number) >= 1000:
        return number / 10000.0
    return number


def _walk_amounts(data: Any, *, _seen: set[int] | None = None) -> list[float]:
    amounts: list[float] = []
    if _seen is None:
        _seen = set()
    if isinstance(data, (dict, list)):
        obj_id = id(data)
        if obj_id in _seen:
            return amounts
        _seen.add(obj_id)
    if isinstance(data, dict):
        for key, value in data.items():
            normalized = str(key or "").lower()
            if any(marker in normalized for marker in ("voucherbalance", "amountoff", "voucheramount", "rewardamount", "balance", "credit")):
                amounts.append(_numeric_amount(value))
            amounts.extend(_walk_amounts(value, _seen=_seen))
    elif isinstance(data, list):
        for item in data:
            amounts.extend(_walk_amounts(item, _seen=_seen))
    return amounts


def _has_usd_one_voucher(data: Any) -> bool:
    return any(amount >= 1.0 for amount in _walk_amounts(data))


def _verify_voucher_reward(session: requests.Session, token: str, log_fn: Callable[[str], None] = print) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    point_info = _get_point_info_http(session, token)
    checks["point_info"] = point_info
    balance_total = _get_balance_total_http(session, token)
    checks["balance_total"] = balance_total
    voucher_num = _get_voucher_num_http(session, token)
    checks["voucher_num"] = voucher_num
    voucher_list = _get_voucher_list_http(session, token)
    checks["voucher_list"] = voucher_list

    amounts: list[float] = []
    for result in checks.values():
        amounts.extend(_walk_amounts(result.get("data") if isinstance(result, dict) else result))
        if isinstance(result, dict):
            amounts.extend(_walk_amounts(result.get("attempts")))
    amount = max(amounts or [0.0])
    ok = amount >= 1.0 or any(_has_usd_one_voucher(result.get("data")) for result in checks.values() if isinstance(result, dict))
    if ok:
        log_fn(f"[Jiekou] 已确认问卷/体验券额度: {amount:g}")
    else:
        log_fn("[Jiekou] 未确认  体验券到账")
    return {"ok": bool(ok), "amount": amount, **checks}


def _create_api_key_http(session: requests.Session, token: str, key_name: str = "auto-register") -> dict[str, Any]:
    name = str(key_name or "auto-register").strip() or "auto-register"
    attempts: list[dict[str, Any]] = []
    for payload in ({"name": name, "expireTime": ""}, {"name": name, "expireTime": None}, {"name": name}):
        try:
            result = _request_json(session, API_KEYS_PATH, method="POST", body=payload, token=token, referer=DASHBOARD_URL)
            data = result.get("data")
            api_key = _find_api_key(data)
            item = {**result, "payload": payload, "api_key": api_key}
            attempts.append(item)
            if _response_ok(result) and api_key:
                return {"ok": True, "api_key": api_key, "api_key_info": data if isinstance(data, dict) else {"raw": data}, "result": item, "attempts": attempts}
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload": payload})
    list_result = _request_json(session, API_KEYS_PATH, token=token, referer=DASHBOARD_URL)
    attempts.append({"action": "list", **list_result})
    api_key = _find_api_key(list_result.get("data"))
    if _response_ok(list_result) and api_key:
        return {"ok": True, "api_key": api_key, "api_key_info": list_result.get("data") or {}, "result": list_result, "attempts": attempts}
    return {"ok": False, "api_key": "", "attempts": attempts}


def _verify_api_key_http(api_key: str, proxy: str | None = None) -> dict[str, Any]:
    url = _llm_api_url(CHAT_COMPLETIONS_PATH)
    if not api_key:
        return {"ok": False, "reason": "missing_api_key", "url": url}
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    payload = {
        "model": API_VERIFICATION_MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        response = session.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=45,
        )
        body = _json_or_text(response, limit=1600)
        reason = ""
        message = ""
        content = ""
        usage: Any = None
        if isinstance(body, dict):
            reason = str(body.get("reason") or "")
            message = str(body.get("message") or "")
            usage = body.get("usage")
            choices = body.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message_obj = choices[0].get("message")
                if isinstance(message_obj, dict):
                    content = str(message_obj.get("content") or "")
        ok = bool(response.ok and (content.strip() or usage))
        result = {"ok": ok, "status": response.status_code, "url": url, "model": API_VERIFICATION_MODEL, "body": body, "content": content, "usage": usage}
        if reason:
            result["reason"] = reason
        if message:
            result["message"] = message
        if not ok and response.status_code == 403 and not reason:
            result["reason"] = "FORBIDDEN"
        return result
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "url": url, "model": API_VERIFICATION_MODEL}


class JiekouProtocolMailboxWorker:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        use_cdp_bridge: bool = False,
    ) -> None:
        self.session = _build_session(proxy)
        self.proxy = proxy
        self.log = log_fn
        self.use_cdp_bridge = bool(use_cdp_bridge)

    def _solve_turnstile_cdp(self, captcha_solver: Any, page_url: str) -> dict[str, Any]:
        """CDP 混合链路：用真实浏览器过 Cloudflare/Turnstile，再回到 HTTP 协议。

        solver 若支持 solve_turnstile_with_session，会同时返回 token、同域 Cookie
        与浏览器 UA；这里同步到 requests.Session，后续注册/登录仍走协议请求。
        """
        if captcha_solver is None:
            raise RuntimeError("Jiekou cdp_protocol 需要 cdp_turnstile solver，但未配置 captcha_solver")
        self.log(f"[Jiekou] CDP Turnstile bootstrap: {page_url}")
        if hasattr(captcha_solver, "solve_turnstile_with_session"):
            solved = captcha_solver.solve_turnstile_with_session(page_url, TURNSTILE_SITE_KEY)
        elif hasattr(captcha_solver, "solve_turnstile"):
            solved = captcha_solver.solve_turnstile(page_url, TURNSTILE_SITE_KEY)
        else:
            raise RuntimeError("Jiekou cdp_protocol 需要支持 solve_turnstile 的 captcha_solver")

        cookies: dict[str, str] = {}
        user_agent = ""
        mode = "cdp_protocol"
        if isinstance(solved, dict):
            solution = solved.get("solution") if isinstance(solved.get("solution"), dict) else {}
            token = str(
                solved.get("turnstile_token")
                or solved.get("token")
                or solved.get("value")
                or solution.get("token")
                or solution.get("value")
                or ""
            ).strip()
            raw_cookies = solved.get("cookies") or {}
            if isinstance(raw_cookies, dict):
                cookies = {str(name): str(value) for name, value in raw_cookies.items() if name and value is not None}
            user_agent = str(solved.get("user_agent") or solved.get("userAgent") or "").strip()
            mode = str(solved.get("mode") or mode).strip() or mode
        else:
            token = str(solved or "").strip()

        if not token or token == "CAPTCHA_FAIL":
            raise RuntimeError("Jiekou CDP Turnstile token 为空")
        if cookies:
            _import_cookies(self.session, cookies)
        if user_agent:
            self.session.headers.update({"User-Agent": user_agent})
        return {
            "ok": True,
            "turnstile_token": token,
            "sitekey": TURNSTILE_SITE_KEY,
            "page_url": page_url,
            "mode": mode,
            "cookie_names": sorted(cookies.keys()),
            "user_agent_synced": bool(user_agent),
        }

    def _solve_turnstile(self, captcha_solver: Any, page_url: str) -> tuple[str, dict[str, Any]]:
        if self.use_cdp_bridge:
            cdp_bootstrap = self._solve_turnstile_cdp(captcha_solver, page_url)
            return str(cdp_bootstrap.get("turnstile_token") or "").strip(), cdp_bootstrap
        if captcha_solver is None or not hasattr(captcha_solver, "solve_turnstile"):
            raise RuntimeError("Jiekou 协议注册需要 Turnstile token，但未配置 captcha_solver")
        token = str(captcha_solver.solve_turnstile(page_url, TURNSTILE_SITE_KEY) or "").strip()
        if not token or token == "CAPTCHA_FAIL":
            raise RuntimeError("Jiekou Turnstile 解决失败")
        return token, {"ok": True, "turnstile_token": token, "sitekey": TURNSTILE_SITE_KEY, "page_url": page_url, "mode": "remote"}

    def run(
        self,
        *,
        email: str,
        password: str,
        verification_link_callback: Callable[[], str] | None = None,
        captcha_solver: Any = None,
        key_name: str = "auto-register",
        invite_code: str = "",
        questionnaire_payload: dict[str, Any] | None = None,
    ) -> dict:
        cdp_bootstrap: dict[str, Any] = {"register": {}, "login": {}}
        register_turnstile, register_bootstrap = self._solve_turnstile(captcha_solver, REGISTER_URL)
        cdp_bootstrap["register"] = register_bootstrap
        register_result = _register_email_http(self.session, email, password, register_turnstile, invite_code=invite_code)
        if not register_result.get("ok"):
            raise RuntimeError(f"Jiekou 邮箱注册失败: {register_result}")

        verify_result: dict[str, Any] = {}
        if not register_result.get("already_exists"):
            if not verification_link_callback:
                raise RuntimeError("Jiekou 注册需要验证链接回调，请配置可收信的邮箱来源")
            self.log("Jiekou 激活邮件已发送，等待验证链接...")
            verification_link = verification_link_callback()
            if not verification_link:
                raise RuntimeError("Jiekou: 未获取到验证链接")
            verify_result = _verify_email_http(self.session, verification_link, email=email)
            if not verify_result.get("ok"):
                raise RuntimeError(f"Jiekou 邮箱验证失败: {verify_result}")

        login_turnstile, login_bootstrap = self._solve_turnstile(captcha_solver, LOGIN_URL)
        cdp_bootstrap["login"] = login_bootstrap
        login_result = _login_email_http(self.session, email, password, login_turnstile, invite_code=invite_code)
        if not login_result.get("ok"):
            raise RuntimeError(f"Jiekou 登录失败: {login_result}")
        token = str(login_result.get("token") or _extract_token(login_result.get("data")) or self.session.cookies.get("token") or "").strip()
        if not token:
            raise RuntimeError(f"Jiekou 登录成功但未返回 token: {login_result}")

        user_info = _get_user_info_http(self.session, token)
        user = dict(user_info.get("user") or login_result.get("user") or {})
        questionnaire_result = _submit_questionnaire_http(self.session, token, payload=questionnaire_payload)
        if not questionnaire_result.get("ok"):
            raise RuntimeError(f"Jiekou 问卷提交失败: {questionnaire_result}")
        post_questionnaire_user_info = _get_user_info_http(self.session, token)
        if post_questionnaire_user_info.get("user"):
            user = dict(post_questionnaire_user_info.get("user") or user)

        # 领取 1 美元券必须以控制面查询确认；否则不创建/不保存为成功账号。
        time.sleep(1)
        voucher_result = _verify_voucher_reward(self.session, token, log_fn=self.log)
        if not voucher_result.get("ok"):
            raise RuntimeError(f"Jiekou 未确认  体验券到账: {voucher_result}")

        create_result = _create_api_key_http(self.session, token, key_name=key_name)
        if not create_result.get("ok"):
            raise RuntimeError(f"Jiekou 创建 API Key 失败: {create_result}")
        api_key = str(create_result.get("api_key") or "").strip()
        api_verification = _verify_api_key_http(api_key, proxy=self.proxy)
        cookies = _session_cookie_map(self.session)
        return {
            "email": email,
            "password": password,
            "user": user,
            "user_info": user_info,
            "post_questionnaire_user_info": post_questionnaire_user_info,
            "api_key": api_key,
            "api_key_info": dict(create_result.get("api_key_info") or {}),
            "api_verification": api_verification,
            "key_create_result": create_result.get("result") or create_result,
            "register_result": register_result,
            "verify_result": verify_result,
            "login_result": login_result,
            "questionnaire_result": questionnaire_result,
            "voucher_result": voucher_result,
            "cdp_bootstrap": cdp_bootstrap,
            "point_info": voucher_result.get("point_info") or {},
            "balance_total": voucher_result.get("balance_total") or {},
            "voucher_num": voucher_result.get("voucher_num") or {},
            "voucher_list": voucher_result.get("voucher_list") or {},
            "session": {"token": token, "user": user},
            "cookies": cookies,
            "cookie_header": "; ".join(f"{name}={value}" for name, value in cookies.items() if value),
            "auth_method": "email",
            "site_url": SITE_URL,
            "dashboard_url": DASHBOARD_URL,
            "api_base": OPENAI_COMPAT_V1_API_BASE,
            "legacy_api_base": OPENAI_API_BASE,
            "openai_compatible_api_base": OPENAI_COMPAT_API_BASE,
            "openai_compatible_v1_api_base": OPENAI_COMPAT_V1_API_BASE,
            "direct_api_base": HIGHWAY_API_BASE,
            "control_api_base": CONTROL_API_BASE,
        }
