"""Zo Computer 协议客户端。"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlparse

import requests

SITE_URL = "https://www.zo.computer"
API_BASE = "https://api.zo.computer"
AUTH_BASE = "https://auth.zo.computer"
CLIENT_ID = "on-substrate"
DEFAULT_COUPON_CODE = "SHEK100"
STRIPE_PUBLISHABLE_KEY_ENV = "ZO_STRIPE_PUBLISHABLE_KEY"

ZO_CARD_ENV_MAP = {
    "number": "ZO_CARD_NUMBER",
    "exp_month": "ZO_CARD_EXP_MONTH",
    "exp_year": "ZO_CARD_EXP_YEAR",
    "cvv": "ZO_CARD_CVV",
    "country": "ZO_CARD_COUNTRY",
    "address": "ZO_CARD_ADDRESS",
    "city": "ZO_CARD_CITY",
    "postal_code": "ZO_CARD_POSTAL_CODE",
    "state": "ZO_CARD_STATE",
    "name": "ZO_CARD_NAME",
}

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


def _api_url(path: str) -> str:
    return urljoin(f"{API_BASE}/", str(path or "").lstrip("/"))


def _auth_url(path: str) -> str:
    return urljoin(f"{AUTH_BASE}/", str(path or "").lstrip("/"))


def _site_url(path: str) -> str:
    return urljoin(f"{SITE_URL}/", str(path or "").lstrip("/"))


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": str(response.text or "")[:2000]}


def _clip(value: Any, limit: int = 1200) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


def _clean_digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


RESERVED_WORKSPACE_HANDLES = {
    "admin", "api", "app", "assets", "auth", "cdn", "cname", "data", "fallback",
    "files", "help", "images", "mail", "media", "proxy", "signup", "static",
    "status", "support", "uploads", "www", "yourhandle",
}


def normalize_workspace_handle(value: Any) -> str:
    """按 Zo 前端规则规整 workspace handle：仅小写字母数字，最长 30。"""
    handle = re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())[:30]
    if handle and handle[0].isdigit():
        handle = f"zo{handle}"[:30]
    if handle in RESERVED_WORKSPACE_HANDLES:
        handle = f"zo{handle}"[:30]
    return handle


def generate_workspace_handle(seed: Any = "") -> str:
    """生成可提交给 /signup 的随机 handle，避免并发注册互撞。"""
    raw = str(seed or "").split("@", 1)[0]
    base = normalize_workspace_handle(raw)
    if len(base) < 4 or base in RESERVED_WORKSPACE_HANDLES:
        base = "zo"
    suffix = secrets.token_hex(4)
    keep = max(2, 30 - len(suffix))
    handle = normalize_workspace_handle(f"{base[:keep]}{suffix}")
    if len(handle) < 4 or handle in RESERVED_WORKSPACE_HANDLES:
        handle = normalize_workspace_handle(f"zo{secrets.token_hex(8)}")
    return handle


def parse_sse_events(response: requests.Response) -> list[dict[str, Any]]:
    """解析 Zo /signup 的 text/event-stream 响应。"""
    events: list[dict[str, Any]] = []
    event_name = ""
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if not event_name and not data_lines:
            return
        raw_data = "\n".join(data_lines).strip()
        data: Any = raw_data
        if raw_data:
            try:
                data = json.loads(raw_data)
            except Exception:
                data = raw_data
        events.append({"event": event_name or "message", "data": data})
        event_name = ""
        data_lines = []

    try:
        iterator = response.iter_lines(decode_unicode=True)
    except Exception:
        iterator = str(response.text or "").splitlines()
    for raw_line in iterator:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line or "")
        if not line:
            flush()
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    flush()
    return events


def extract_workspace_info(data: Any) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    workspaces = data.get("workspaces")
    first: dict[str, Any] = {}
    if isinstance(workspaces, list) and workspaces:
        item = workspaces[0]
        first = item if isinstance(item, dict) else {}

    handle = normalize_workspace_handle(first.get("handle") or "")
    url = str(first.get("url") or "").strip()
    if not handle:
        claims = data.get("claims") if isinstance(data.get("claims"), dict) else {}
        props = claims.get("properties") if isinstance(claims.get("properties"), dict) else {}
        domains = props.get("domains") if isinstance(props.get("domains"), list) else []
        for domain in domains:
            candidate = normalize_workspace_handle(domain)
            if candidate:
                handle = candidate
                break
    if not url and handle:
        url = f"https://{handle}.zo.computer"
    if not handle and url:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if host.endswith(".zo.computer"):
            handle = normalize_workspace_handle(host[: -len(".zo.computer")])
    origin = url.rstrip("/") if url else (f"https://{handle}.zo.computer" if handle else "")
    return {"handle": handle, "url": url, "origin": origin} if handle or origin else {}


def normalize_billing_country(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower()
    if lowered in {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}:
        return "US"
    return raw


def normalize_card_info(extra: dict[str, Any] | None) -> dict[str, str]:
    """从运行时 extra 读取 Zo 绑卡信息，不在源码里固化任何卡号。"""
    data = dict(extra or {})
    nested = data.get("zo_card")
    if isinstance(nested, dict):
        data = {**data, **nested}
    number = _clean_digits(data.get("number") or data.get("card_number") or data.get("cardNo"))
    exp_month = _clean_digits(data.get("exp_month") or data.get("month") or data.get("expiry_month"))
    exp_year = _clean_digits(data.get("exp_year") or data.get("year") or data.get("expiry_year"))
    expiry = str(data.get("expiry") or data.get("exp") or "").strip()
    if expiry and (not exp_month or not exp_year):
        parts = _clean_digits(expiry)
        if len(parts) >= 4:
            exp_month = exp_month or parts[:2]
            exp_year = exp_year or parts[2:]
    if len(exp_year) == 2:
        exp_year = f"20{exp_year}"
    return {
        "number": number,
        "exp_month": exp_month,
        "exp_year": exp_year,
        "cvv": _clean_digits(data.get("cvv") or data.get("cvc") or data.get("security_code")),
        "country": normalize_billing_country(data.get("country") or data.get("billing_country") or ""),
        "address": str(data.get("address") or data.get("billing_address") or data.get("line1") or "").strip(),
        "city": str(data.get("city") or data.get("billing_city") or "").strip(),
        "postal_code": str(data.get("postal_code") or data.get("zip") or data.get("billing_zip") or "").strip(),
        "state": str(data.get("state") or data.get("province") or data.get("billing_state") or "").strip(),
        "name": str(data.get("name") or data.get("cardholder") or data.get("cardholder_name") or "").strip(),
    }



def load_card_info_from_env() -> dict[str, str]:
    return {key: os.environ.get(env_name, "").strip() for key, env_name in ZO_CARD_ENV_MAP.items()}


def load_card_info_from_pool(extra: dict[str, Any] | None = None) -> dict[str, str]:
    """从本地信用卡池取默认有效卡，作为 Zo 绑卡兜底。"""
    data = dict(extra or {})
    pool_path = str(data.get("credit_card_pool_path") or data.get("card_pool_path") or "").strip()
    try:
        from core.credit_card_pool import CreditCardPool

        card = CreditCardPool(pool_path).get_default()
    except Exception:
        return {}
    if not card:
        return {}
    normalized = normalize_card_info({"zo_card": card})
    normalized["_pool_id"] = str(card.get("_pool_id") or card.get("id") or "")
    normalized["_pool_path"] = str(card.get("_pool_path") or pool_path or "")
    return normalized


def resolve_card_info(extra: dict[str, Any] | None) -> dict[str, str]:
    """解析 Zo 绑卡信息；优先 extra.zo_card，其次环境变量，最后使用信用卡池。"""
    data = dict(extra or {})
    has_explicit = isinstance(data.get("zo_card"), dict) or any(
        key in data for key in (
            "number", "card_number", "cardNo", "exp_month", "expiry", "cvv", "cvc",
            "billing_address", "address",
        )
    )
    if has_explicit:
        return normalize_card_info(data)
    env_card = load_card_info_from_env()
    if any(env_card.values()):
        return normalize_card_info({"zo_card": env_card})
    pool_card = load_card_info_from_pool(data)
    if any(pool_card.get(key) for key in ("number", "cvv", "address")):
        return pool_card
    raise RuntimeError("Zo 绑卡缺少字段；请通过 extra.zo_card、ZO_CARD_* 环境变量或 Web 信用卡池传入靶场测试卡")


def mask_card_info(card: dict[str, Any] | None) -> dict[str, str]:
    card = dict(card or {})
    number = _clean_digits(card.get("number"))
    exp_month = _clean_digits(card.get("exp_month"))
    exp_year = _clean_digits(card.get("exp_year"))
    return {
        "brand_hint": "mastercard" if number.startswith(("51", "52", "53", "54", "55")) else "unknown",
        "last4": number[-4:] if len(number) >= 4 else "",
        "exp_month": exp_month,
        "exp_year": exp_year,
        "country": str(card.get("country") or ""),
        "state": str(card.get("state") or ""),
        "postal_code": str(card.get("postal_code") or ""),
    }


def sanitize_sensitive(value: Any) -> Any:
    """递归脱敏支付字段，避免完整卡号/CVV 落入日志或账号 extra。"""
    env_card = load_card_info_from_env()
    card_number = _clean_digits(env_card.get("number"))
    card_cvv = _clean_digits(env_card.get("cvv"))
    sensitive_keys = {"number", "card_number", "cardno", "cvv", "cvc", "security_code", "client_secret"}

    def mask_text(text: str) -> str:
        masked = str(text or "")
        if card_number:
            masked = masked.replace(card_number, f"****{card_number[-4:]}")
        if card_cvv and masked == card_cvv:
            return "***"
        masked = re.sub(r"\b(\d{13,19})\b", lambda m: f"****{m.group(1)[-4:]}", masked)
        masked = re.sub(r"\b(seti_[A-Za-z0-9_]+?_secret_)[A-Za-z0-9_-]+", r"\1****", masked)
        return masked

    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = str(key).lower().replace("-", "_")
            compact_key = normalized_key.replace("_", "")
            if normalized_key in sensitive_keys or compact_key in sensitive_keys:
                if normalized_key == "client_secret" or compact_key == "clientsecret":
                    output[key] = mask_text(str(child or ""))
                    continue
                digits = _clean_digits(child)
                output[key] = f"****{digits[-4:]}" if digits and normalized_key not in {"cvv", "cvc", "security_code"} else "***"
            else:
                output[key] = sanitize_sensitive(child)
        return output
    if isinstance(value, list):
        return [sanitize_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_sensitive(item) for item in value)
    if isinstance(value, str):
        return mask_text(value)
    return value


def validate_card_info(card: dict[str, Any] | None) -> None:
    normalized = normalize_card_info(card or {})
    missing = [key for key in ("number", "exp_month", "exp_year", "cvv", "country", "address", "city", "postal_code", "state") if not normalized.get(key)]
    if missing:
        raise RuntimeError(f"Zo 绑卡缺少字段: {', '.join(missing)}；请通过 extra.zo_card 传入")


def _looks_like_api_key(value: str, *, field_name: str = "") -> bool:
    candidate = str(value or "").strip()
    if not candidate or "*" in candidate or "…" in candidate or "..." in candidate:
        return False
    lowered_name = str(field_name or "").lower()
    if candidate.startswith(("zo_", "zo-", "sk-")):
        return len(candidate) >= 12
    if any(word in lowered_name for word in ("token", "key", "secret")) and len(candidate) >= 16:
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-.]{24,}", candidate))


def find_api_key(data: Any) -> str:
    exact_fields = (
        "api_key", "apiKey", "apikey", "access_token", "accessToken", "token",
        "key", "secret", "plainTextKey", "plaintextKey", "value",
    )
    if isinstance(data, dict):
        for field in exact_fields:
            value = data.get(field)
            if isinstance(value, str) and _looks_like_api_key(value, field_name=field):
                return value.strip()
        for field, value in data.items():
            if isinstance(value, str) and _looks_like_api_key(value, field_name=str(field)):
                return value.strip()
        for value in data.values():
            found = find_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = find_api_key(item)
            if found:
                return found
    if isinstance(data, str):
        for pattern in (r"zo[_-][A-Za-z0-9_\-.]{12,}", r"sk-[A-Za-z0-9_\-.]{12,}"):
            match = re.search(pattern, data)
            if match:
                return match.group(0)
    return ""


_CREDIT_STRONG_HINTS = ("credit", "credits", "balance", "coupon", "discount", "wallet", "remaining", "available", "free")
_CREDIT_WEAK_KEYS = {"amount", "total", "value", "usd"}
_CREDIT_IGNORE_KEYS = {"id", "user_id", "timestamp", "created_at", "updated_at", "exp", "expires_in"}


def _parse_amount_text(value: str) -> list[float]:
    amounts: list[float] = []
    for match in re.finditer(r"-?\d+(?:\.\d+)?", str(value or "").replace(",", "")):
        try:
            amounts.append(float(match.group(0)))
        except ValueError:
            continue
    return amounts


def _credit_path_has_evidence(path: tuple[str, ...]) -> bool:
    lowered = [part.lower() for part in path]
    joined = ".".join(lowered)
    if any(part in _CREDIT_IGNORE_KEYS for part in lowered):
        return False
    if any(hint in joined for hint in _CREDIT_STRONG_HINTS):
        return True
    return any(part in _CREDIT_WEAK_KEYS for part in lowered) and any(part in joined for part in ("billing", "payment", "usage"))


def extract_credit_amount(data: Any, *, with_evidence: bool = False) -> Any:
    candidates: list[tuple[float, str]] = []

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, (*path, str(key)))
            return
        if isinstance(value, list):
            for child in value:
                walk(child, path)
            return
        if not _credit_path_has_evidence(path):
            return
        label = ".".join(path)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            candidates.append((float(value), label))
        elif isinstance(value, str):
            for amount in _parse_amount_text(value):
                candidates.append((amount, label))

    walk(data, ())
    if not candidates:
        return (0.0, []) if with_evidence else 0.0
    amount = max(amount for amount, _label in candidates)
    if with_evidence:
        return amount, [{"amount": amount, "path": label} for amount, label in candidates]
    return amount


class ZoClient:
    """Zo Computer 协议客户端；登录后优先 HTTP，绑卡/未知端点保留浏览器兜底。"""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.timeout = timeout
        self.log_fn = log_fn or (lambda message: None)
        self.session = session or requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({
            "User-Agent": CHROME_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        self.workspace_handle = ""
        self.workspace_origin = ""
        self.host_key = ""

    def _log(self, message: str) -> None:
        try:
            self.log_fn(message)
        except Exception:
            pass

    def _request(self, method: str, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> requests.Response:
        merged = dict(self.session.headers)
        if headers:
            merged.update(headers)
        kwargs.setdefault("timeout", self.timeout)
        for attempt in range(3):
            try:
                return self.session.request(method, url, headers=merged, **kwargs)
            except requests.RequestException:
                if attempt >= 2:
                    raise
                time.sleep(0.8 + attempt)
        raise RuntimeError("unreachable")

    @property
    def cookies(self) -> dict[str, str]:
        return requests.utils.dict_from_cookiejar(self.session.cookies)

    @property
    def cookie_header(self) -> str:
        return "; ".join(f"{name}={value}" for name, value in self.cookies.items() if value)

    def import_cookies(self, cookies: dict[str, str]) -> None:
        for name, value in dict(cookies or {}).items():
            if not name or value is None:
                continue
            for domain in ("www.zo.computer", ".zo.computer", "zo.computer", "api.zo.computer", "auth.zo.computer"):
                self.session.cookies.set(str(name), str(value), domain=domain)

    def import_tokens(self, *, access_token: str = "", refresh_token: str = "") -> None:
        if access_token:
            self.session.cookies.set("access_token", access_token, domain=".zo.computer")
        if refresh_token:
            self.session.cookies.set("refresh_token", refresh_token, domain=".zo.computer")

    def set_workspace(self, *, handle: str = "", url: str = "", host_key: str = "") -> dict[str, str]:
        handle = normalize_workspace_handle(handle)
        origin = str(url or "").strip().rstrip("/")
        if not origin and handle:
            origin = f"https://{handle}.zo.computer"
        if not handle and origin:
            parsed = urlparse(origin)
            host = parsed.hostname or ""
            if host.endswith(".zo.computer"):
                handle = normalize_workspace_handle(host[: -len(".zo.computer")])
        self.workspace_handle = handle
        self.workspace_origin = origin
        if host_key:
            self.host_key = str(host_key).strip()
        return {"handle": self.workspace_handle, "origin": self.workspace_origin, "host_key": self.host_key}

    def auth_headers(self) -> dict[str, str]:
        token = str(self.cookies.get("access_token") or "").strip()
        origin = self.workspace_origin or SITE_URL
        headers = {"Origin": origin, "Referer": f"{origin}/"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self.workspace_origin:
            headers["X-Zo-Workspace-Origin"] = self.workspace_origin
        if self.host_key:
            headers["x-zo-host-key"] = self.host_key
        return headers

    def start_email_authorize(self, *, email: str, redirect_uri: str = SITE_URL) -> dict[str, Any]:
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": f"auto-{int(time.time())}",
            "provider": "email",
        }
        response = self._request("GET", _auth_url("authorize"), params=params, allow_redirects=False, headers={"Accept": "text/html,application/json,*/*"})
        location = response.headers.get("Location") or response.headers.get("location") or ""
        if response.status_code in {301, 302, 303, 307, 308} and location:
            authorize_url = urljoin(AUTH_BASE, location)
        else:
            authorize_url = response.url
        send_result = self.send_email_registration(email=email, authorize_url=authorize_url)
        return {"ok": True, "status": response.status_code, "authorize_url": authorize_url, "send_result": send_result}

    def send_email_registration(self, *, email: str, authorize_url: str | None = None) -> dict[str, Any]:
        if not email:
            raise RuntimeError("Zo 系统邮箱注册需要 email")
        url = authorize_url or _auth_url("email/authorize")
        attempts: list[dict[str, Any]] = []
        payloads = [
            {"email": email},
            {"email": email, "redirect_uri": SITE_URL, "client_id": CLIENT_ID},
        ]
        for method in ("POST", "GET"):
            for payload in payloads:
                try:
                    kwargs: dict[str, Any] = {"allow_redirects": False, "headers": {"Origin": AUTH_BASE, "Referer": url}}
                    if method == "POST":
                        kwargs["json"] = payload
                    else:
                        kwargs["params"] = payload
                    response = self._request(method, url, **kwargs)
                    data = _safe_json(response)
                    attempts.append({"ok": response.ok, "status": response.status_code, "method": method, "url": response.url, "data": data})
                    if response.ok or response.status_code in {202, 204, 302, 303}:
                        return {"ok": True, "status": response.status_code, "method": method, "data": data, "attempts": attempts}
                except Exception as exc:
                    attempts.append({"ok": False, "method": method, "url": url, "error": repr(exc)})
        raise RuntimeError(f"Zo 邮箱登录链接发送失败: {_clip(attempts)}")

    def exchange_code(self, *, code: str, redirect_uri: str = SITE_URL, verifier: str = "") -> dict[str, Any]:
        if not code:
            raise RuntimeError("Zo OAuth code 为空")
        payload = {
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        }
        response = self._request("POST", _auth_url("token"), data=payload, headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": SITE_URL})
        data = _safe_json(response)
        if not response.ok:
            raise RuntimeError(f"Zo token exchange 失败: status={response.status_code} body={_clip(data)}")
        access = str(data.get("access_token") or data.get("access") or "").strip() if isinstance(data, dict) else ""
        refresh = str(data.get("refresh_token") or data.get("refresh") or "").strip() if isinstance(data, dict) else ""
        self.import_tokens(access_token=access, refresh_token=refresh)
        return {"ok": True, "status": response.status_code, "data": data, "access_token": access, "refresh_token": refresh}

    def visit_verification_link(self, verification_link: str) -> dict[str, Any]:
        if not verification_link:
            raise RuntimeError("Zo 验证链接为空")
        response = self._request("GET", verification_link, allow_redirects=True, headers={"Accept": "text/html,application/json,*/*", "Referer": AUTH_BASE})
        parsed = urlparse(str(response.url or ""))
        query = parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        exchange: dict[str, Any] = {}
        if code:
            exchange = self.exchange_code(code=code, redirect_uri=f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else SITE_URL)
        return {"ok": response.ok, "status": response.status_code, "final_url": response.url, "exchange": exchange, "cookies": self.cookies}

    def get_login_state(self) -> dict[str, Any]:
        token = str(self.cookies.get("access_token") or "").strip()
        headers = {"Accept": "application/json", "Origin": SITE_URL, "Referer": f"{SITE_URL}/"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self._request("GET", _site_url("api/login-state"), headers=headers)
        return {"ok": response.ok, "status": response.status_code, "data": _safe_json(response), "path": "/api/login-state"}

    def check_handle_available(self, handle: str) -> dict[str, Any]:
        normalized = normalize_workspace_handle(handle)
        if not normalized:
            return {"ok": False, "available": False, "reason": "empty_handle"}
        response = self._request("GET", _api_url(f"/signup/{normalized}/available"), headers=self.auth_headers())
        data = _safe_json(response)
        available = bool(data.get("available")) if isinstance(data, dict) and "available" in data else None
        return {"ok": response.ok, "status": response.status_code, "handle": normalized, "available": available, "data": data}

    def create_workspace(self, *, handle: str = "", promo_code: str = DEFAULT_COUPON_CODE, signup_code: str = "") -> dict[str, Any]:
        if not handle:
            login_state = self.get_login_state()
            claims = login_state.get("data", {}).get("claims", {}) if isinstance(login_state.get("data"), dict) else {}
            props = claims.get("properties", {}) if isinstance(claims, dict) else {}
            handle = generate_workspace_handle(props.get("email") or props.get("full_name") or "")
        handle = normalize_workspace_handle(handle) or generate_workspace_handle()
        attempts: list[dict[str, Any]] = []
        last_error = ""
        for index in range(5):
            candidate = handle if index == 0 else generate_workspace_handle(handle)
            available_result: dict[str, Any] = {}
            try:
                available_result = self.check_handle_available(candidate)
                attempts.append({"step": "available", **available_result})
                if available_result.get("available") is False:
                    continue
            except Exception as exc:
                attempts.append({"step": "available", "handle": candidate, "ok": False, "error": repr(exc)})
            payload: dict[str, Any] = {"handle": candidate, "dev": False}
            if promo_code:
                payload["promo_code"] = promo_code
            if signup_code:
                payload["code"] = signup_code
            try:
                response = self._request(
                    "POST",
                    _api_url("/signup"),
                    json=payload,
                    headers={**self.auth_headers(), "Content-Type": "application/json", "Accept": "text/event-stream, application/json, */*"},
                    stream=True,
                    timeout=max(self.timeout, 180.0),
                )
                content_type = str(response.headers.get("Content-Type") or response.headers.get("content-type") or "")
                events = parse_sse_events(response) if "event-stream" in content_type or response.ok else []
                data = _safe_json(response) if not events else {}
                error_event = next((event for event in events if event.get("event") == "SignupErrorEvent"), None)
                complete_event = next((event for event in events if event.get("event") == "SignupCompleteEvent"), None)
                step_events = [event for event in events if event.get("event") == "SignupStepEvent"]
                item = {
                    "step": "signup",
                    "ok": response.ok and not error_event,
                    "status": response.status_code,
                    "handle": candidate,
                    "events": events,
                    "data": data,
                }
                attempts.append(item)
                if error_event:
                    last_error = _clip(error_event.get("data"))
                    continue
                if response.ok and (complete_event or step_events or not events):
                    self.set_workspace(handle=candidate)
                    return {
                        "ok": True,
                        "handle": candidate,
                        "workspace_origin": self.workspace_origin,
                        "workspace_url": self.workspace_origin,
                        "events": events,
                        "complete_event": complete_event,
                        "attempts": attempts,
                    }
                last_error = _clip(data)
            except Exception as exc:
                last_error = repr(exc)
                attempts.append({"step": "signup", "ok": False, "handle": candidate, "error": last_error})
        raise RuntimeError(f"Zo 创建 workspace 失败: {last_error or _clip(attempts)}")

    def ensure_workspace(self, *, handle: str = "", promo_code: str = DEFAULT_COUPON_CODE, signup_code: str = "") -> dict[str, Any]:
        login_state = self.get_login_state()
        workspace = extract_workspace_info(login_state.get("data"))
        if workspace.get("origin"):
            self.set_workspace(handle=workspace.get("handle", ""), url=workspace.get("origin", ""))
            return {"ok": True, "source": "login-state", "workspace": workspace, "login_state": login_state}
        try:
            created = self.create_workspace(handle=handle, promo_code=promo_code, signup_code=signup_code)
        except RuntimeError as exc:
            message = str(exc)
            if "Account already has a workspace" not in message:
                raise
            settings = self.get_settings()
            workspace = extract_workspace_info(settings.get("data"))
            if not workspace.get("origin"):
                refreshed_login_state = self.get_login_state()
                workspace = extract_workspace_info(refreshed_login_state.get("data"))
                if workspace.get("origin"):
                    login_state = refreshed_login_state
            if workspace.get("origin"):
                self.set_workspace(handle=workspace.get("handle", ""), url=workspace.get("origin", ""))
                return {
                    "ok": True,
                    "source": "existing-after-conflict",
                    "workspace": workspace,
                    "login_state": login_state,
                    "settings": settings,
                    "conflict_error": message,
                }
            raise
        return {"ok": True, "source": "created", "workspace": {"handle": created.get("handle"), "origin": created.get("workspace_origin")}, "create_result": created, "login_state": login_state}

    def get_settings(self) -> dict[str, Any]:
        response = self._request("GET", _api_url("settings/"), headers=self.auth_headers())
        return {"ok": response.ok, "status": response.status_code, "data": _safe_json(response), "path": "/settings/"}

    def skip_onboarding(self) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        for payload in (
            {"onboarding_completed": True, "onboardingCompleted": True},
            {"signup_choices_skipped": True, "setup_complete": True},
        ):
            try:
                response = self._request("POST", _api_url("settings/"), json=payload, headers={**self.auth_headers(), "Content-Type": "application/json"})
                attempts.append({"ok": response.ok, "status": response.status_code, "path": "/settings/", "payload": payload, "data": _safe_json(response)})
                if response.ok:
                    return {"ok": True, "attempts": attempts}
            except Exception as exc:
                attempts.append({"ok": False, "path": "/settings/", "payload": payload, "error": repr(exc)})
        return {"ok": False, "best_effort": True, "attempts": attempts}

    def skip_phone(self) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        for payload in (
            {"phone_skipped": True, "phoneSkipped": True},
            {"phone_number": None, "skip_phone": True},
        ):
            try:
                response = self._request("POST", _api_url("settings/"), json=payload, headers={**self.auth_headers(), "Content-Type": "application/json"})
                attempts.append({"ok": response.ok, "status": response.status_code, "path": "/settings/", "payload": payload, "data": _safe_json(response)})
                if response.ok:
                    return {"ok": True, "attempts": attempts}
            except Exception as exc:
                attempts.append({"ok": False, "path": "/settings/", "payload": payload, "error": repr(exc)})
        return {"ok": False, "best_effort": True, "attempts": attempts}

    def redeem_coupon(self, *, code: str = DEFAULT_COUPON_CODE) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        candidates = [
            ("GET", "/billing/discount-code", {"code": code}),
            ("GET", "/signup/promo-code", {"code": code}),
            ("POST", "/billing/discount-code", {"code": code}),
            ("POST", "/signup/promo-code", {"code": code}),
        ]
        for method, path, payload in candidates:
            try:
                kwargs: dict[str, Any] = {"headers": self.auth_headers()}
                if method == "GET":
                    kwargs["params"] = payload
                else:
                    kwargs["json"] = payload
                    kwargs["headers"] = {**kwargs["headers"], "Content-Type": "application/json"}
                response = self._request(method, _api_url(path), **kwargs)
                data = _safe_json(response)
                amount, evidence = extract_credit_amount(data, with_evidence=True)
                item = {"ok": response.ok, "status": response.status_code, "method": method, "path": path, "data": data, "amount": amount, "credit_evidence": evidence}
                attempts.append(item)
                if response.ok:
                    return {"ok": True, "status": response.status_code, "method": method, "path": path, "code": code, "amount": amount, "currency": "USD", "data": data, "attempts": attempts, "credit_evidence": evidence}
            except Exception as exc:
                attempts.append({"ok": False, "method": method, "path": path, "error": repr(exc)})
        raise RuntimeError(f"Zo 兑换优惠券失败: {_clip(attempts)}")

    def check_credits(self, *, min_amount: float = 100.0) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        best_amount = 0.0
        best_data: Any = {}
        best_evidence: list[dict[str, Any]] = []
        for path in (
            "/billing/credit-balance?testmode=false",
            "/billing/current-plan?testmode=false",
            "/billing/plan-info?testmode=false&discount_code=SHEK100",
            "/billing/usage-alert?testmode=false&trigger=mount",
            "/models/free-usage",
            "/billing/discount-code",
            "/signup/promo-code",
            "/settings/",
        ):
            try:
                response = self._request("GET", _api_url(path), headers=self.auth_headers())
                data = _safe_json(response)
                amount, evidence = extract_credit_amount(data, with_evidence=True)
                item = {"ok": response.ok, "status": response.status_code, "path": path, "data": data, "amount": amount, "credit_evidence": evidence}
                attempts.append(item)
                if evidence and amount > best_amount:
                    best_amount = amount
                    best_data = data
                    best_evidence = evidence
                if evidence and amount >= min_amount:
                    return {"ok": True, "amount": amount, "currency": "USD", "source": path, "data": data, "credit_evidence": evidence, "attempts": attempts}
            except Exception as exc:
                attempts.append({"ok": False, "path": path, "error": repr(exc), "amount": 0.0, "credit_evidence": []})
        return {"ok": bool(best_evidence) and best_amount >= min_amount, "amount": best_amount, "currency": "USD", "source": "best_effort", "data": best_data, "credit_evidence": best_evidence, "attempts": attempts}

    def create_setup_intent(self) -> dict[str, Any]:
        """创建 Stripe SetupIntent；返回值内部含 client_secret，日志侧只记录脱敏 data。"""
        response = self._request(
            "POST",
            _api_url("/billing/setup-intent"),
            json={},
            headers={**self.auth_headers(), "Content-Type": "application/json"},
        )
        data = _safe_json(response)
        client_secret = str(data.get("client_secret") or "").strip() if isinstance(data, dict) else ""
        if not response.ok or not client_secret:
            raise RuntimeError(f"Zo 创建 Stripe SetupIntent 失败: status={response.status_code} body={_clip(sanitize_sensitive(data))}")
        return {
            "ok": True,
            "status": response.status_code,
            "path": "/billing/setup-intent",
            "client_secret": client_secret,
            "data": sanitize_sensitive(data),
        }

    def confirm_stripe_setup_intent(self, *, client_secret: str, card: dict[str, Any]) -> dict[str, Any]:
        normalized = normalize_card_info(card)
        validate_card_info(normalized)
        publishable_key = os.environ.get(STRIPE_PUBLISHABLE_KEY_ENV, "").strip()
        if not publishable_key:
            raise RuntimeError(f"缺少 {STRIPE_PUBLISHABLE_KEY_ENV}，不能确认 Stripe SetupIntent")
        if "_secret_" not in str(client_secret or ""):
            raise RuntimeError("Zo Stripe SetupIntent client_secret 格式异常")
        intent_id = str(client_secret).split("_secret_", 1)[0]
        origin = self.workspace_origin or SITE_URL
        form = {
            "client_secret": client_secret,
            "payment_method_data[type]": "card",
            "payment_method_data[card][number]": normalized["number"],
            "payment_method_data[card][cvc]": normalized["cvv"],
            "payment_method_data[card][exp_month]": normalized["exp_month"],
            "payment_method_data[card][exp_year]": normalized["exp_year"],
            "payment_method_data[billing_details][name]": normalized.get("name") or "Zo User",
            "payment_method_data[billing_details][address][country]": normalized["country"],
            "payment_method_data[billing_details][address][line1]": normalized["address"],
            "payment_method_data[billing_details][address][city]": normalized["city"],
            "payment_method_data[billing_details][address][postal_code]": normalized["postal_code"],
            "payment_method_data[billing_details][address][state]": normalized["state"],
            "payment_method_data[guid]": str(uuid.uuid4()),
            "payment_method_data[muid]": str(uuid.uuid4()),
            "payment_method_data[sid]": str(uuid.uuid4()),
            "payment_method_data[pasted_fields]": "number",
            "payment_method_data[payment_user_agent]": "stripe.js/58d9408f11; stripe-js-v3/58d9408f11",
            "payment_method_data[referrer]": origin,
            "expected_payment_method_type": "card",
            "use_stripe_sdk": "true",
            "key": publishable_key,
        }
        response = self._request(
            "POST",
            f"https://api.stripe.com/v1/setup_intents/{intent_id}/confirm",
            data=form,
            headers={
                "Authorization": f"Bearer {publishable_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": origin,
                "Referer": f"{origin}/",
                "User-Agent": CHROME_UA,
                "Accept": "application/json",
            },
        )
        data = _safe_json(response)
        status = str(data.get("status") or "").strip() if isinstance(data, dict) else ""
        payment_method = str(data.get("payment_method") or "").strip() if isinstance(data, dict) else ""
        setup_intent = str(data.get("id") or intent_id).strip() if isinstance(data, dict) else intent_id
        ok = bool(response.ok and status == "succeeded" and payment_method)
        return {
            "ok": ok,
            "status": response.status_code,
            "stripe_status": status,
            "setup_intent": setup_intent,
            "payment_method": payment_method,
            "data": sanitize_sensitive(data),
        }

    def bind_card(self, *, card: dict[str, Any], require_confirmed: bool = True) -> dict[str, Any]:
        normalized = normalize_card_info(card)
        validate_card_info(normalized)
        masked = mask_card_info(normalized)
        attempts: list[dict[str, Any]] = []
        try:
            setup = self.create_setup_intent()
            attempts.append({
                "ok": True,
                "status": setup.get("status"),
                "path": setup.get("path"),
                "data": setup.get("data"),
                "card": masked,
            })
            confirmed = self.confirm_stripe_setup_intent(client_secret=str(setup.get("client_secret") or ""), card=normalized)
            attempts.append({"step": "stripe_confirm", **confirmed, "card": masked})
            if confirmed.get("ok"):
                return {
                    "ok": True,
                    "status": confirmed.get("status"),
                    "stripe_status": confirmed.get("stripe_status"),
                    "setup_intent": confirmed.get("setup_intent"),
                    "payment_method": confirmed.get("payment_method"),
                    "card": masked,
                    "attempts": attempts,
                }
        except Exception as exc:
            attempts.append({"ok": False, "step": "setup_intent_or_stripe_confirm", "error": repr(exc), "card": masked})

        if require_confirmed:
            raise RuntimeError(f"Zo HTTP 绑卡未确认成功，需要浏览器/CDP 兜底: {_clip(sanitize_sensitive(attempts))}")
        return {"ok": False, "card": masked, "attempts": sanitize_sensitive(attempts)}

    def create_access_token(self, *, name: str = "auto-register") -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        payloads = [{"name": name}, {"label": name}, {"description": name}, {}]
        for path in ("/api-keys/", "/access-tokens", "/tokens", "/settings/access-tokens", "/user-services/"):
            try:
                listed = self._request("GET", _api_url(path), headers=self.auth_headers())
                listed_data = _safe_json(listed)
                attempts.append({"ok": listed.ok, "status": listed.status_code, "method": "GET", "path": path, "data": listed_data})
                existing = find_api_key(listed_data)
                if existing:
                    return {"ok": True, "api_key": existing, "api_key_info": listed_data, "source": "list", "attempts": attempts}
            except Exception as exc:
                attempts.append({"ok": False, "method": "GET", "path": path, "error": repr(exc)})
            for payload in payloads:
                try:
                    created = self._request("POST", _api_url(path), json=payload, headers={**self.auth_headers(), "Content-Type": "application/json"})
                    data = _safe_json(created)
                    attempts.append({"ok": created.ok, "status": created.status_code, "method": "POST", "path": path, "payload": payload, "data": data})
                    api_key = find_api_key(data)
                    if api_key:
                        return {"ok": True, "api_key": api_key, "api_key_info": data, "source": "create", "attempts": attempts}
                    if created.status_code in {401, 403, 404, 405}:
                        break
                except Exception as exc:
                    attempts.append({"ok": False, "method": "POST", "path": path, "payload": payload, "error": repr(exc)})
        raise RuntimeError(f"Zo 创建 Access Token 失败，需要浏览器/CDP 兜底: {_clip(attempts)}")

    def verify_api_key(self, api_key: str) -> dict[str, Any]:
        if not api_key:
            return {"ok": False, "reason": "missing_api_key"}
        try:
            response = self._request("GET", _api_url("models/available"), headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
            data = _safe_json(response)
            return {"ok": response.status_code < 500, "status": response.status_code, "body": data}
        except Exception as exc:
            return {"ok": False, "error": repr(exc)}
