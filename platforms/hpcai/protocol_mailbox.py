"""HPC-AI 纯协议邮箱注册、赠金确认与 MaaS API Key 提取。"""
from __future__ import annotations

import re
import time
from typing import Any, Callable
from urllib.parse import urlencode, urljoin

import requests

SITE_URL = "https://www.hpc-ai.com"
SIGNUP_URL = f"{SITE_URL}/account/signup"
SIGNIN_URL = f"{SITE_URL}/account/signin"
MODEL_CONSOLE_URL = f"{SITE_URL}/models-console/models"
API_KEY_PAGE_URL = f"{SITE_URL}/models-console/api-key"
OPENAI_COMPAT_API_BASE = "https://api.hpc-ai.com/inference/v1"
TURNSTILE_SITE_KEY = "0x4AAAAAAC_4lIrK2LRHBJfe"

OTP_PATH = "/api/user/otp"
REGISTER_PATH = "/api/user/register"
LOGIN_PATH = "/api/user/login"
USER_INFO_PATH = "/api/user/info"
BALANCE_PATH = "/api/balance"
CREDIT_LIST_PATH = "/api/credit/list"
VOUCHER_LIST_PATH = "/api/voucher/list"
WELCOME_VOUCHER_CHECK_PATH = "/api/voucher/maas/welcome/check"
WELCOME_VOUCHER_CLAIM_PATH = "/api/voucher/maas/welcome/claim"
API_KEY_CREATE_PATH = "/api/user/maas/key/create"
API_KEY_LIST_PATH = "/api/user/maas/key/list"
MODELS_PATH = "/models"
CHAT_COMPLETIONS_PATH = "/chat/completions"
API_VERIFICATION_MODEL = "deepseek-ai/DeepSeek-V3-0324"

_KEY_RE = re.compile(r"(?:sk|hpc|hpck|ak)-?[A-Za-z0-9_\-]{8,}|[A-Za-z0-9_\-]{32,}")
_TOKEN_KEYS = (
    "accessToken",
    "access_token",
    "token",
    "jwt",
    "idToken",
    "id_token",
    "authToken",
    "authorization",
)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _api_url(path: str, *, query: dict[str, Any] | None = None) -> str:
    url = urljoin(SITE_URL.rstrip("/") + "/", str(path or "").lstrip("/"))
    if query:
        clean = {key: value for key, value in query.items() if value not in (None, "")}
        if clean:
            url = f"{url}?{urlencode(clean, doseq=True)}"
    return url


def _llm_api_url(path: str = MODELS_PATH) -> str:
    normalized = str(path or "").strip() or MODELS_PATH
    return urljoin(OPENAI_COMPAT_API_BASE.rstrip("/") + "/", normalized.lstrip("/"))


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


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
            "Referer": SIGNUP_URL,
            "Sec-Ch-Ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
    )
    return session


def _session_cookie_map(session: requests.Session) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "")
        if not domain or "hpc-ai.com" in domain:
            cookies[str(cookie.name)] = str(cookie.value)
    return cookies


def _import_cookies(session: requests.Session, cookies: dict[str, str]) -> None:
    for name, value in (cookies or {}).items():
        if not name or value in (None, ""):
            continue
        session.cookies.set(str(name), str(value), domain="www.hpc-ai.com")
        session.cookies.set(str(name), str(value), domain=".hpc-ai.com")
        session.cookies.set(str(name), str(value), domain="hpc-ai.com")


def _auth_value(token: str) -> str:
    raw = str(token or "").strip()
    if not raw:
        return ""
    return raw if raw.lower().startswith("bearer ") else f"Bearer {raw}"


def _request_json(
    session: requests.Session,
    path: str,
    *,
    method: str = "POST",
    body: dict[str, Any] | None = None,
    token: str | None = None,
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
        _api_url(path, query=query),
        json=body if method.upper() not in {"GET"} else None,
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
        if data.get("success") is True or data.get("ok") is True or data.get("sent") is True:
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
    if raw.lower().startswith("bearer "):
        return _looks_like_auth_token(raw.split(" ", 1)[1])
    if raw.count(".") == 2 and len(raw) >= 24:
        return True
    if re.fullmatch(r"[A-Za-z0-9_\-]{24,}", raw):
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
            token = _extract_token(data.get(key))
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
            if isinstance(value, dict) and (value.get("email") or value.get("userId") or value.get("id")):
                return dict(value)
        payload = _payload_data(data)
        if payload is not data:
            user = _extract_user(payload)
            if user:
                return user
        if data.get("email") or data.get("userId") or data.get("id"):
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
        # HPC-AI 的创建接口返回 {"key": {"id": "uuid", "fullKey": "sk-..."}}。
        # 必须优先取 fullKey/apiKey 等真正密钥字段，避免把 key.id(UUID) 误当 API Key。
        for key in ("fullKey", "full_key", "apiKey", "api_key", "secret", "value", "token"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                found = _find_api_key(value)
                if found:
                    return found
        for key in ("key", "data", "payload", "result", "item"):
            value = data.get(key)
            if isinstance(value, (dict, list)):
                found = _find_api_key(value)
                if found:
                    return found
            elif isinstance(value, str) and value.strip() and key != "key":
                found = _find_api_key(value)
                if found:
                    return found
        for key, value in data.items():
            if key in {"id", "uuid", "userId", "createdAt", "lastFour", "name"}:
                continue
            found = _find_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_api_key(item)
            if found:
                return found
    return ""


def _send_register_otp_http(session: requests.Session, email: str) -> dict[str, Any]:
    payload = {"email": email, "purpose": "register"}
    result = _request_json(session, OTP_PATH, method="POST", body=payload, referer=SIGNUP_URL)
    data = result.get("data")
    sent = bool(isinstance(data, dict) and data.get("sent")) or _response_ok(result)
    result.update({"ok": sent, "payload": payload})
    return result


def _register_email_http(
    session: requests.Session,
    *,
    email: str,
    password: str,
    otp: str,
    turnstile: str,
    username: str = "",
    invitation_code: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "username": username or email.split("@", 1)[0],
        "email": email,
        "password": password,
        "otp": str(otp or "").strip(),
        "turnstileToken": turnstile,
    }
    if invitation_code:
        payload["invitationCode"] = invitation_code
        payload["invitation_code"] = invitation_code
    result = _request_json(session, REGISTER_PATH, method="POST", body=payload, referer=SIGNUP_URL)
    token = _extract_token(result.get("data"))
    if token:
        session.headers.update({"Authorization": _auth_value(token)})
        session.cookies.set("accessToken", token, domain="www.hpc-ai.com")
        session.cookies.set("accessToken", token, domain=".hpc-ai.com")
    user = _extract_user(result.get("data"))
    message = _response_message(result.get("data")).lower()
    result.update(
        {
            "ok": bool(_response_ok(result) and (token or user or result.get("data"))),
            "token": token,
            "user": user,
            "already_exists": result.get("status") in {400, 409, 422} and any(marker in message for marker in ("exist", "already", "registered", "已存在")),
            "payload_keys": sorted(payload.keys()),
        }
    )
    return result


def _login_email_http(session: requests.Session, email: str, password: str) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    payloads = [
        {"email": email, "password": password, "rememberMe": True},
        {"username": email, "password": password, "rememberMe": True},
    ]
    for payload in payloads:
        result = _request_json(session, LOGIN_PATH, method="POST", body=payload, referer=SIGNIN_URL)
        token = _extract_token(result.get("data"))
        user = _extract_user(result.get("data"))
        item = {**result, "payload_keys": sorted(payload.keys()), "token": token, "user": user}
        attempts.append(item)
        if _response_ok(result) and (token or user or result.get("data")):
            if token:
                session.headers.update({"Authorization": _auth_value(token)})
                session.cookies.set("accessToken", token, domain="www.hpc-ai.com")
                session.cookies.set("accessToken", token, domain=".hpc-ai.com")
            item["attempts"] = attempts
            item["ok"] = True
            return item
    return {"ok": False, "attempts": attempts}


def _get_user_info_http(session: requests.Session, token: str) -> dict[str, Any]:
    result = _request_json(session, USER_INFO_PATH, token=token, referer=MODEL_CONSOLE_URL)
    result["user"] = _extract_user(result.get("data"))
    result["ok"] = bool(_response_ok(result) and (result.get("user") or result.get("data")))
    return result


def _claim_welcome_voucher_http(session: requests.Session, token: str) -> dict[str, Any]:
    check = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=MODEL_CONSOLE_URL)
    claim = _request_json(session, WELCOME_VOUCHER_CLAIM_PATH, token=token, referer=MODEL_CONSOLE_URL)
    return {"ok": bool(_response_ok(check) or _response_ok(claim)), "check": check, "claim": claim}


def _numeric_amount(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        number = float(value)
    except Exception:
        return 0.0
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
            if any(marker in normalized for marker in ("availablebalance", "availablevoucher", "availablecredit", "voucheramount", "creditamount", "balance", "credit", "voucher", "amount")):
                amounts.append(_numeric_amount(value))
            amounts.extend(_walk_amounts(value, _seen=_seen))
    elif isinstance(data, list):
        for item in data:
            amounts.extend(_walk_amounts(item, _seen=_seen))
    return amounts


def _has_minimum_credit(data: Any, minimum: float = 2.0) -> bool:
    return any(amount >= float(minimum or 0.0) for amount in _walk_amounts(data))


def _verify_credit_reward(session: requests.Session, token: str, log_fn: Callable[[str], None] = print, minimum: float = 2.0) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    checks["balance"] = _request_json(session, BALANCE_PATH, method="GET", token=token, referer=MODEL_CONSOLE_URL)
    checks["credit_list"] = _request_json(session, CREDIT_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, token=token, referer=MODEL_CONSOLE_URL)
    checks["voucher_list"] = _request_json(session, VOUCHER_LIST_PATH, method="POST", body={"page": 1, "pageSize": 20}, token=token, referer=MODEL_CONSOLE_URL)
    checks["welcome_check"] = _request_json(session, WELCOME_VOUCHER_CHECK_PATH, token=token, referer=MODEL_CONSOLE_URL)

    amounts: list[float] = []
    for result in checks.values():
        if isinstance(result, dict):
            amounts.extend(_walk_amounts(result.get("data")))
    amount = max(amounts or [0.0])
    ok = amount >= float(minimum or 2.0)
    if ok:
        log_fn(f"[HPC-AI] 已确认赠送额度: ${amount:g}")
    else:
        log_fn(f"[HPC-AI] 未确认 ${minimum:g} 赠送额度")
    return {"ok": bool(ok), "amount": amount, **checks}


def _create_api_key_http(session: requests.Session, token: str, key_name: str = "auto-register") -> dict[str, Any]:
    name = str(key_name or "auto-register").strip() or "auto-register"
    attempts: list[dict[str, Any]] = []
    payloads = [
        {"name": name},
        {"keyName": name},
        {"apiKeyName": name},
    ]
    for payload in payloads:
        try:
            result = _request_json(session, API_KEY_CREATE_PATH, method="POST", body=payload, token=token, referer=API_KEY_PAGE_URL)
            api_key = _find_api_key(result.get("data"))
            item = {**result, "payload": payload, "api_key": api_key}
            attempts.append(item)
            if _response_ok(result) and api_key:
                return {"ok": True, "api_key": api_key, "api_key_info": result.get("data") or {}, "result": item, "attempts": attempts}
        except Exception as exc:
            attempts.append({"ok": False, "error": repr(exc), "payload": payload})
    list_result = _request_json(session, API_KEY_LIST_PATH, token=token, referer=API_KEY_PAGE_URL)
    attempts.append({"action": "list", **list_result})
    api_key = _find_api_key(list_result.get("data"))
    if _response_ok(list_result) and api_key:
        return {"ok": True, "api_key": api_key, "api_key_info": list_result.get("data") or {}, "result": list_result, "attempts": attempts}
    return {"ok": False, "api_key": "", "attempts": attempts}


def _verify_api_key_http(api_key: str, proxy: str | None = None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "reason": "missing_api_key", "url": _llm_api_url(MODELS_PATH)}
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    try:
        response = session.get(
            _llm_api_url(MODELS_PATH),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=45,
        )
        body = _json_or_text(response, limit=1600)
        ok = bool(response.ok and body)
        if ok:
            return {"ok": True, "status": response.status_code, "url": response.url, "body": body}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "url": _llm_api_url(MODELS_PATH)}

    payload = {
        "model": API_VERIFICATION_MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        response = session.post(
            _llm_api_url(CHAT_COMPLETIONS_PATH),
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=45,
        )
        body = _json_or_text(response, limit=1600)
        content = ""
        usage: Any = None
        if isinstance(body, dict):
            usage = body.get("usage")
            choices = body.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
                if isinstance(message, dict):
                    content = str(message.get("content") or "")
        ok = bool(response.ok and (content.strip() or usage or body))
        return {"ok": ok, "status": response.status_code, "url": response.url, "model": API_VERIFICATION_MODEL, "body": body, "content": content, "usage": usage}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "url": _llm_api_url(CHAT_COMPLETIONS_PATH), "model": API_VERIFICATION_MODEL}


class HpcAiProtocolMailboxWorker:
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
        """CDP 混合链路：真实 Chrome 过 Turnstile，同步 token/Cookie/UA 到协议 Session。"""
        if captcha_solver is None:
            raise RuntimeError("HPC-AI cdp_protocol 需要 cdp_turnstile solver，但未配置 captcha_solver")
        self.log(f"[HPC-AI] CDP Turnstile bootstrap: {page_url}")
        if hasattr(captcha_solver, "solve_turnstile_with_session"):
            solved = captcha_solver.solve_turnstile_with_session(page_url, TURNSTILE_SITE_KEY)
        elif hasattr(captcha_solver, "solve_turnstile"):
            solved = captcha_solver.solve_turnstile(page_url, TURNSTILE_SITE_KEY)
        else:
            raise RuntimeError("HPC-AI cdp_protocol 需要支持 solve_turnstile 的 captcha_solver")

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
            raise RuntimeError("HPC-AI CDP Turnstile token 为空")
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
            raise RuntimeError("HPC-AI 协议注册需要 Turnstile token，但未配置 captcha_solver")
        token = str(captcha_solver.solve_turnstile(page_url, TURNSTILE_SITE_KEY) or "").strip()
        if not token or token == "CAPTCHA_FAIL":
            raise RuntimeError("HPC-AI Turnstile 解决失败")
        return token, {"ok": True, "turnstile_token": token, "sitekey": TURNSTILE_SITE_KEY, "page_url": page_url, "mode": "remote"}

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Callable[[], str] | None = None,
        captcha_solver: Any = None,
        key_name: str = "auto-register",
        invitation_code: str = "",
        minimum_credit: float = 2.0,
    ) -> dict:
        cdp_bootstrap: dict[str, Any] = {"register": {}}
        self.log("[HPC-AI] 请求注册邮箱验证码")
        otp_send_result = _send_register_otp_http(self.session, email)
        if not otp_send_result.get("ok"):
            raise RuntimeError(f"HPC-AI 发送注册验证码失败: {otp_send_result}")
        if not otp_callback:
            raise RuntimeError("HPC-AI 注册需要邮箱 OTP 回调，请配置可收信的邮箱来源")
        self.log("[HPC-AI] 等待邮箱 OTP 验证码...")
        otp = str(otp_callback() or "").strip()
        if not otp:
            raise RuntimeError("HPC-AI: 未获取到邮箱 OTP")

        register_turnstile, register_bootstrap = self._solve_turnstile(captcha_solver, SIGNUP_URL)
        cdp_bootstrap["register"] = register_bootstrap
        register_result = _register_email_http(
            self.session,
            email=email,
            password=password,
            otp=otp,
            turnstile=register_turnstile,
            invitation_code=invitation_code,
        )
        if not register_result.get("ok"):
            raise RuntimeError(f"HPC-AI 邮箱注册失败: {register_result}")

        token = str(register_result.get("token") or _extract_token(register_result.get("data")) or "").strip()
        login_result: dict[str, Any] = {}
        if not token:
            login_result = _login_email_http(self.session, email, password)
            if not login_result.get("ok"):
                raise RuntimeError(f"HPC-AI 登录失败: {login_result}")
            token = str(login_result.get("token") or _extract_token(login_result.get("data")) or "").strip()
        if not token:
            raise RuntimeError(f"HPC-AI 注册/登录成功但未返回 accessToken: register={register_result} login={login_result}")

        user_info = _get_user_info_http(self.session, token)
        user = dict(user_info.get("user") or register_result.get("user") or login_result.get("user") or {})

        claim_result = _claim_welcome_voucher_http(self.session, token)
        time.sleep(1)
        credit_result = _verify_credit_reward(self.session, token, log_fn=self.log, minimum=minimum_credit)
        if not credit_result.get("ok"):
            raise RuntimeError(f"HPC-AI 未确认 ${minimum_credit:g} 赠送额度到账: {credit_result}")

        create_result = _create_api_key_http(self.session, token, key_name=key_name)
        if not create_result.get("ok"):
            raise RuntimeError(f"HPC-AI 创建 API Key 失败: {create_result}")
        api_key = str(create_result.get("api_key") or "").strip()
        api_verification = _verify_api_key_http(api_key, proxy=self.proxy)
        cookies = _session_cookie_map(self.session)
        return {
            "email": email,
            "password": password,
            "user": user,
            "user_info": user_info,
            "api_key": api_key,
            "api_key_info": dict(create_result.get("api_key_info") or {}),
            "api_verification": api_verification,
            "key_create_result": create_result.get("result") or create_result,
            "otp_send_result": otp_send_result,
            "register_result": register_result,
            "login_result": login_result,
            "claim_result": claim_result,
            "credit_result": credit_result,
            "balance": credit_result.get("balance") or {},
            "credit_list": credit_result.get("credit_list") or {},
            "voucher_list": credit_result.get("voucher_list") or {},
            "cdp_bootstrap": cdp_bootstrap,
            "session": {"accessToken": token, "user": user},
            "cookies": cookies,
            "cookie_header": "; ".join(f"{name}={value}" for name, value in cookies.items() if value),
            "auth_method": "email",
            "site_url": SITE_URL,
            "dashboard_url": MODEL_CONSOLE_URL,
            "api_base": OPENAI_COMPAT_API_BASE,
            "openai_compatible_api_base": OPENAI_COMPAT_API_BASE,
        }
