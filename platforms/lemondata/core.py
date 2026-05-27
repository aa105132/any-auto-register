"""LemonData HTTP / Auth.js 协议客户端。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable
from urllib.parse import urljoin

import requests

SITE_URL = "https://lemondata.cc"
SIGNIN_URL = "https://lemondata.cc/signin"
DASHBOARD_URL = "https://lemondata.cc/dashboard/api"
API_BASE = "https://api.lemondata.cc"
LLM_API_BASE = "https://api.lemondata.cc/v1"
TURNSTILE_SITEKEY = "0x4AAAAAACgPfXQhg8TKlBOO"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)


def _site_url(path: str) -> str:
    return urljoin(f"{SITE_URL}/", str(path or "").lstrip("/"))


def _api_url(path: str) -> str:
    return urljoin(f"{API_BASE}/", str(path or "").lstrip("/"))


def _safe_json(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": str(response.text or "")[:2000]}


def _clip(value: Any, limit: int = 500) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


def _looks_like_api_key(value: str, *, field_name: str = "") -> bool:
    candidate = str(value or "").strip()
    if not candidate or "*" in candidate or "…" in candidate or "..." in candidate:
        return False
    lowered_name = str(field_name or "").lower()
    lowered = candidate.lower()
    if lowered.startswith(("ld_", "ld-", "sk-", "lemon_", "lmd_")):
        return True
    if "key" in lowered_name and len(candidate) >= 16:
        return True
    if lowered_name in {"token", "secret", "value", "plain", "plaintext", "plain_text"} and len(candidate) >= 24:
        return True
    return False


def find_api_key(data: Any) -> str:
    """从创建/列表响应里递归提取可用 API key。"""
    exact_fields = (
        "api_key", "apiKey", "apikey", "key", "token", "secret",
        "plainTextKey", "plaintextKey", "plain_text_key", "value",
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
        for pattern in (r"ld[_-][A-Za-z0-9_\-]{12,}", r"sk-[A-Za-z0-9_\-]{12,}"):
            match = re.search(pattern, data)
            if match:
                return match.group(0)
    return ""


_BALANCE_HINTS = ("balance", "credit", "credits", "wallet", "remaining", "available", "amount", "usd")
_BALANCE_STRONG_HINTS = ("balance", "credit", "credits", "wallet", "remaining", "available")
_BALANCE_WEAK_KEYS = {"amount", "total", "value", "usd"}
_BALANCE_IGNORE_KEYS = {"id", "orgid", "organizationid", "user_id", "created_at", "updated_at", "timestamp"}


def _parse_amount_text(value: str) -> list[float]:
    text = str(value or "").replace(",", "")
    amounts: list[float] = []
    for match in re.finditer(r"-?\d+(?:\.\d+)?", text):
        try:
            amounts.append(float(match.group(0)))
        except ValueError:
            continue
    return amounts


def _balance_path_has_evidence(path: tuple[str, ...]) -> bool:
    lowered = [part.lower() for part in path]
    joined = ".".join(lowered)
    if any(part in _BALANCE_IGNORE_KEYS for part in lowered):
        return False
    if any(hint in joined for hint in _BALANCE_STRONG_HINTS):
        return True
    return any(part in _BALANCE_WEAK_KEYS for part in lowered) and any(
        hint in joined for hint in ("billing", "payment", "topup", "top_up", "quota")
    )


def extract_balance_amount(data: Any, *, with_evidence: bool = False) -> Any:
    """从明确余额/账单 JSON 里提取账户余额美元金额。"""
    candidates: list[tuple[float, str]] = []

    def add_candidates(value: Any, path: tuple[str, ...]) -> None:
        if not _balance_path_has_evidence(path):
            return
        label = ".".join(path)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            candidates.append((float(value), label))
        elif isinstance(value, str):
            for amount in _parse_amount_text(value):
                candidates.append((amount, label))

    def walk(value: Any, path: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, (*path, str(key)))
            return
        if isinstance(value, list):
            for child in value:
                walk(child, path)
            return
        add_candidates(value, path)

    walk(data, ())
    if not candidates:
        return (0.0, []) if with_evidence else 0.0
    amount = max(amount for amount, _label in candidates)
    if with_evidence:
        evidence = [{"amount": amount, "path": label} for amount, label in candidates]
        return amount, evidence
    return amount

def _extract_org_ids(data: Any) -> list[str]:
    ids: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            keys = {str(k).lower(): k for k in value.keys()}
            for name in ("id", "slug", "orgid", "organizationid"):
                original = keys.get(name)
                if original is not None:
                    raw = value.get(original)
                    if isinstance(raw, str) and raw.strip():
                        ids.append(raw.strip())
                        break
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    deduped: list[str] = []
    for item in ids:
        if item not in deduped:
            deduped.append(item)
    if "default" not in deduped:
        deduped.append("default")
    return deduped


class LemonDataClient:
    """LemonData 协议客户端；优先 HTTP，CDP 仅用于挑战/token bootstrap。"""

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
            self.session.cookies.set(str(name), str(value), domain="lemondata.cc")
            self.session.cookies.set(str(name), str(value), domain=".lemondata.cc")
    def get_captcha_policy(self) -> dict[str, Any]:
        response = self._request("GET", _site_url("api/auth/captcha-policy"), headers={"Referer": SIGNIN_URL})
        return {"ok": response.ok, "status": response.status_code, "data": _safe_json(response)}

    def captcha_required(self) -> bool:
        policy = self.get_captcha_policy()
        data = policy.get("data") if isinstance(policy, dict) else {}
        if isinstance(data, dict):
            nested = data.get("data") if isinstance(data.get("data"), dict) else data
            if nested.get("turnstileRequired") is False:
                return False
        return True

    def get_auth_csrf(self) -> str:
        response = self._request("GET", _site_url("api/auth/csrf"), headers={"Referer": SIGNIN_URL})
        data = _safe_json(response)
        token = str(data.get("csrfToken") or data.get("token") or "").strip() if isinstance(data, dict) else ""
        if not response.ok or not token:
            raise RuntimeError(f"LemonData Auth.js CSRF 获取失败: status={response.status_code} body={_clip(data)}")
        return token

    def get_dashboard_csrf(self) -> str:
        attempts: list[dict[str, Any]] = []
        for path in ("api/csrf", "api/dashboard/csrf", "dashboard/api/csrf"):
            response = self._request("GET", _site_url(path), headers={"Referer": DASHBOARD_URL})
            data = _safe_json(response)
            attempts.append({"path": path, "status": response.status_code, "data": data})
            token = ""
            if isinstance(data, dict):
                nested = data.get("data") if isinstance(data.get("data"), dict) else data
                token = str(
                    nested.get("token")
                    or nested.get("csrfToken")
                    or nested.get("csrf_token")
                    or nested.get("x-csrf-token")
                    or ""
                ).strip()
            if response.ok and token:
                return token
        raise RuntimeError(f"LemonData dashboard CSRF 获取失败: {_clip(attempts, 1200)}")

    def bootstrap_cdp_challenge(self, captcha_solver: Any = None) -> dict[str, Any]:
        """CDP 混合链路：只用 CDP 获取 Turnstile token，其余继续 HTTP。"""
        if not captcha_solver or not hasattr(captcha_solver, "solve_turnstile"):
            return {"ok": False, "reason": "missing_cdp_solver", "turnstile_token": ""}
        self._log("LemonData CDP bootstrap: 获取 Turnstile token")
        token = str(captcha_solver.solve_turnstile(SIGNIN_URL, TURNSTILE_SITEKEY) or "").strip()
        return {"ok": bool(token), "turnstile_token": token, "sitekey": TURNSTILE_SITEKEY, "page_url": SIGNIN_URL}

    def verify_captcha(self, *, email: str, token: str, timezone: str = "Asia/Shanghai", viewport_width: int = 1920) -> dict[str, Any]:
        if not token:
            raise RuntimeError("LemonData Turnstile token 为空")
        payload = {"email": email, "token": token, "timezone": timezone, "viewportWidth": int(viewport_width or 1920)}
        response = self._request(
            "POST",
            _site_url("api/auth/verify-captcha"),
            headers={"Content-Type": "application/json", "Origin": SITE_URL, "Referer": SIGNIN_URL},
            data=json.dumps(payload, separators=(",", ":")),
        )
        data = _safe_json(response)
        if not response.ok:
            code = str(data.get("code") or "") if isinstance(data, dict) else ""
            if response.status_code == 429 and code == "too_many_registrations":
                raise RuntimeError(
                    f"LemonData captcha 校验失败: status={response.status_code} body={_clip(data)}; "
                    "当前网络段被 LemonData 注册策略限流，建议改用 Google OAuth + 账号池路径"
                )
            raise RuntimeError(f"LemonData captcha 校验失败: status={response.status_code} body={_clip(data)}")
        return {"ok": True, "status": response.status_code, "data": data}

    def send_email_signin(self, *, email: str, callback_url: str = DASHBOARD_URL) -> dict[str, Any]:
        csrf = self.get_auth_csrf()
        response = self._request(
            "POST",
            _site_url("api/auth/signin/email"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Auth-Return-Redirect": "1",
                "Origin": SITE_URL,
                "Referer": SIGNIN_URL,
            },
            data={"email": email, "csrfToken": csrf, "callbackUrl": callback_url},
            allow_redirects=False,
        )
        data = _safe_json(response)
        if not response.ok:
            raise RuntimeError(f"LemonData magic link 发送失败: status={response.status_code} body={_clip(data)}")
        return {"ok": True, "status": response.status_code, "data": data}

    def visit_verification_link(self, verification_link: str) -> dict[str, Any]:
        if not verification_link:
            raise RuntimeError("LemonData 验证链接为空")
        response = self._request(
            "GET",
            verification_link,
            headers={"Referer": SIGNIN_URL, "Accept": "text/html,application/xhtml+xml,application/json"},
            allow_redirects=True,
            timeout=max(self.timeout, 60),
        )
        return {"ok": response.ok, "status": response.status_code, "final_url": response.url, "cookies": self.cookies}

    def get_session(self) -> dict[str, Any]:
        response = self._request("GET", _site_url("api/auth/session"), headers={"Referer": DASHBOARD_URL})
        return {"ok": response.ok, "status": response.status_code, "data": _safe_json(response)}

    def get_organizations(self) -> dict[str, Any]:
        response = self._request("GET", _site_url("api/dashboard/organizations"), headers={"Referer": DASHBOARD_URL})
        return {"ok": response.ok, "status": response.status_code, "data": _safe_json(response)}

    def _dashboard_headers(self, csrf_token: str = "") -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": SITE_URL,
            "Referer": DASHBOARD_URL,
        }
        if csrf_token:
            headers["x-csrf-token"] = csrf_token
            headers["X-CSRF-Token"] = csrf_token
            headers["X-XSRF-Token"] = csrf_token
        return headers

    def _try_json_endpoint(self, method: str, path: str, *, csrf_token: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if payload is not None:
            kwargs["data"] = json.dumps(payload, separators=(",", ":"))
        response = self._request(method, _site_url(path), headers=self._dashboard_headers(csrf_token), **kwargs)
        return {"ok": response.ok, "status": response.status_code, "path": path, "method": method, "data": _safe_json(response)}
    def create_or_find_api_key(self, *, name: str = "auto-register") -> dict[str, Any]:
        organizations = self.get_organizations()
        org_ids = _extract_org_ids(organizations.get("data"))
        endpoints: list[str] = []
        for org_id in org_ids:
            endpoints.append(f"api/dashboard/organizations/{org_id}/api-keys")
        endpoints.extend(["api/dashboard/organizations/api-keys", "api/dashboard/api-keys"])

        csrf_token = ""
        try:
            csrf_token = self.get_dashboard_csrf()
        except Exception as exc:
            self._log(f"LemonData dashboard CSRF 获取失败，继续尝试无 CSRF GET: {exc}")

        attempts: list[dict[str, Any]] = []
        payloads = [{"name": name}, {"label": name}, {"keyName": name}, {"description": name}, {}]
        for endpoint in endpoints:
            try:
                listed = self._try_json_endpoint("GET", endpoint, csrf_token=csrf_token)
                attempts.append(listed)
                existing_key = find_api_key(listed.get("data"))
                if existing_key:
                    return {"ok": True, "api_key": existing_key, "api_key_info": listed.get("data") or {}, "source": "list", "attempts": attempts, "organizations": organizations}
            except Exception as exc:
                attempts.append({"ok": False, "method": "GET", "path": endpoint, "error": repr(exc)})
            for payload in payloads:
                try:
                    created = self._try_json_endpoint("POST", endpoint, csrf_token=csrf_token, payload=payload)
                    attempts.append(created)
                    api_key = find_api_key(created.get("data"))
                    if api_key:
                        return {"ok": True, "api_key": api_key, "api_key_info": created.get("data") or {}, "source": "create", "attempts": attempts, "organizations": organizations}
                    if created.get("status") in {401, 403, 404, 405}:
                        break
                except Exception as exc:
                    attempts.append({"ok": False, "method": "POST", "path": endpoint, "payload": payload, "error": repr(exc)})
        raise RuntimeError(f"LemonData 创建/提取 API Key 失败: {_clip(attempts, 1800)}")


    def check_balance(self, *, min_amount: float = 1.0) -> dict[str, Any]:
        """查询 dashboard 余额；无法确认达到门槛时返回 ok=False。"""
        attempts: list[dict[str, Any]] = []
        best_amount = 0.0
        best_data: Any = {}
        best_evidence: list[dict[str, Any]] = []
        organizations: dict[str, Any] = {}
        try:
            organizations = self.get_organizations()
            org_amount, org_evidence = extract_balance_amount(organizations.get("data"), with_evidence=True)
            attempts.append({**organizations, "path": "api/dashboard/organizations", "amount": org_amount, "balance_evidence": org_evidence})
            if org_evidence and org_amount > best_amount:
                best_amount = org_amount
                best_data = organizations.get("data")
                best_evidence = org_evidence
        except Exception as exc:
            attempts.append({"ok": False, "path": "api/dashboard/organizations", "error": repr(exc), "amount": 0.0, "balance_evidence": []})

        org_ids = _extract_org_ids(organizations.get("data") if organizations else {})
        endpoints: list[str] = []
        for org_id in org_ids:
            endpoints.extend([
                f"api/dashboard/organizations/{org_id}/balance",
                f"api/dashboard/organizations/{org_id}/billing",
                f"api/dashboard/organizations/{org_id}/credits",
                f"api/dashboard/organizations/{org_id}/usage",
            ])
        endpoints.extend([
            "api/dashboard/balance",
            "api/dashboard/billing",
            "api/dashboard/billing/summary",
            "api/dashboard/credits",
            "api/dashboard/usage",
        ])

        seen: set[str] = set()
        for endpoint in endpoints:
            if endpoint in seen:
                continue
            seen.add(endpoint)
            try:
                result = self._try_json_endpoint("GET", endpoint)
                amount, evidence = extract_balance_amount(result.get("data"), with_evidence=True)
                result["amount"] = amount
                result["balance_evidence"] = evidence
                attempts.append(result)
                if evidence and amount > best_amount:
                    best_amount = amount
                    best_data = result.get("data")
                    best_evidence = evidence
                if evidence and amount >= min_amount:
                    return {"ok": True, "amount": amount, "currency": "USD", "source": endpoint, "data": result.get("data") or {}, "balance_evidence": evidence, "attempts": attempts}
            except Exception as exc:
                attempts.append({"ok": False, "path": endpoint, "error": repr(exc), "amount": 0.0})

        return {"ok": bool(best_evidence) and best_amount >= min_amount, "amount": best_amount, "currency": "USD", "source": "best_effort", "data": best_data or {}, "balance_evidence": best_evidence, "attempts": attempts}

    def require_min_balance(self, *, min_amount: float = 1.0) -> dict[str, Any]:
        balance = self.check_balance(min_amount=min_amount)
        amount = float(balance.get("amount") or 0.0)
        if not balance.get("ok") or amount < float(min_amount):
            raise RuntimeError(f"LemonData 余额未达标或未找到明确余额证据，注册不计成功: amount=${amount:.4f}, required=${float(min_amount):.2f}, source={balance.get("source", "")}")
        return balance


    def verify_api_key(self, api_key: str) -> dict[str, Any]:
        if not api_key:
            return {"ok": False, "reason": "missing_api_key"}
        try:
            response = self._request("GET", _api_url("v1/models"), headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
            body = _safe_json(response)
            model_count = len(body.get("data", [])) if isinstance(body, dict) else 0
            return {
                "ok": bool(api_key) and response.status_code < 500,
                "status": response.status_code,
                "model_count": model_count,
            }
        except Exception as exc:
            return {"ok": False, "error": repr(exc)}
