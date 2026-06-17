"""Thesys 纯协议注册与 OpenAI 兼容 API Key 探测。"""
from __future__ import annotations

import re
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import requests

SITE_URL = "https://console.thesys.dev/"
CONTROL_API_BASE = "https://api.app.thesys.dev"
OPENAI_API_ORIGIN = "https://api.thesys.dev"
OPENAI_COMPAT_API_BASE = f"{OPENAI_API_ORIGIN}/v1/embed"
CHAT_COMPLETIONS_URL = f"{OPENAI_COMPAT_API_BASE}/chat/completions"
MODELS_URL = f"{OPENAI_COMPAT_API_BASE}/models"

DEFAULT_FREE_MODEL = "c1/google/gemini-3.1-pro-free/v-20260331"
FREE_MODELS = [
    "c1/google/gemini-3.5-flash-free/v-20260331",
    "c1/google/gemini-3.1-pro-free/v-20260331",
    "c1/google/gemini-3.1-flash-lite-free/v-20260331",
]

_API_KEY_RE = re.compile(r"(?:c1|thesys|sk|tk|key)[A-Za-z0-9_\-]{16,}|[A-Za-z0-9_\-]{80,}")


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


def _api_url(path: str) -> str:
    return urljoin(CONTROL_API_BASE.rstrip("/") + "/", str(path or "").lstrip("/"))


def _cookie_map(session: requests.Session) -> dict[str, str]:
    result: dict[str, str] = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "")
        if not domain or "thesys.dev" in domain:
            result[str(cookie.name)] = str(cookie.value)
    return result


def _safe_response(response: requests.Response) -> dict[str, Any]:
    return {
        "ok": bool(response.ok),
        "status": int(response.status_code),
        "content_type": response.headers.get("content-type", ""),
        "data": _json_or_text(response),
        "text": response.text[:2000],
    }


def _raise_for_api(response: requests.Response, label: str) -> Any:
    data = _json_or_text(response)
    if response.ok:
        return data
    raise RuntimeError(f"Thesys {label}失败: status={response.status_code} body={data}")


def _find_first(data: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return value
        for value in data.values():
            found = _find_first(value, keys)
            if found not in (None, ""):
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_first(item, keys)
            if found not in (None, ""):
                return found
    return None


def _extract_list(data: Any, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        for value in data.values():
            found = _extract_list(value, keys)
            if found:
                return found
    return []


def extract_api_key(data: Any) -> str:
    """从创建 API Key 响应中递归提取只出现一次的明文 key。"""
    if isinstance(data, str):
        text = data.strip()
        if len(text) >= 32 and _API_KEY_RE.fullmatch(text):
            return text
        return ""
    if isinstance(data, dict):
        for key in ("apiKey", "api_key", "key", "secret", "token", "value", "plainKey", "rawKey"):
            found = extract_api_key(data.get(key))
            if found:
                return found
        for value in data.values():
            found = extract_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = extract_api_key(item)
            if found:
                return found
    return ""


class ThesysClient:
    def __init__(self, *, proxy: str | None = None, log_fn=print) -> None:
        self.proxy = proxy
        self.log = log_fn or (lambda _msg: None)
        self.session = requests.Session()
        # 注册请求只走任务代理，避免继承系统代理造成同一批任务出口不一致。
        self.session.trust_env = False
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://console.thesys.dev",
                "Referer": "https://console.thesys.dev/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
        )

    def _post(self, path: str, body: dict[str, Any] | None = None, *, timeout: int = 45) -> Any:
        response = self.session.post(_api_url(path), json=body or {}, timeout=timeout)
        return _raise_for_api(response, path)

    def generate_email_otp(self, email: str, *, app: str = "chat") -> dict[str, Any]:
        data = self._post("/auth/otp.generate.email", {"email": email, "app": app})
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def extract_pre_auth_session_id(data: Any) -> str:
        value = _find_first(data, ("preAuthSessionId", "pre_auth_session_id", "preAuthSessionID", "id"))
        return str(value or "").strip()

    def verify_otp(
        self,
        *,
        pre_auth_session_id: str,
        code: str,
        device_id: str = "",
        app: str = "console",
    ) -> dict[str, Any]:
        body = {
            "preAuthSessionId": pre_auth_session_id,
            "deviceId": device_id or f"codex-{uuid.uuid4().hex}",
            "userInputCode": str(code or "").strip(),
            "app": app,
        }
        data = self._post("/auth/otp.verify", body)
        result = data if isinstance(data, dict) else {"data": data}
        result["cookies"] = _cookie_map(self.session)
        return result

    def user_me(self) -> dict[str, Any]:
        data = self._post("/users.me", {})
        return data if isinstance(data, dict) else {"data": data}

    def list_orgs(self) -> list[dict[str, Any]]:
        data = self._post("/orgs.list.mine", {})
        items = _extract_list(data, ("orgs", "organizations", "items", "data"))
        return [item for item in items if isinstance(item, dict)]

    def create_api_key(self, *, org_id: str, name: str = "auto-register", usage_type: str = "C1") -> dict[str, Any]:
        body = {"orgId": org_id, "name": name or "auto-register", "usageType": usage_type or "C1"}
        data = self._post("/application/application.createApiKey", body)
        return data if isinstance(data, dict) else {"data": data}

    def list_api_keys(self, *, org_id: str, usage_type: str = "C1") -> dict[str, Any]:
        data = self._post("/application/application.listApiKeys", {"orgId": org_id, "usageType": usage_type or "C1"})
        return data if isinstance(data, dict) else {"data": data}

    def get_billing(self, *, org_id: str) -> dict[str, Any]:
        data = self._post("/billing.get", {"orgId": org_id})
        return data if isinstance(data, dict) else {"data": data}

    def verify_models(self, api_key: str, *, timeout: int = 45) -> dict[str, Any]:
        response = self.session.get(
            MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=timeout,
        )
        payload = _safe_response(response)
        payload["ok"] = bool(response.ok)
        return payload

    def probe_chat_completion(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_FREE_MODEL,
        timeout: int = 90,
    ) -> dict[str, Any]:
        body = {
            "model": model or DEFAULT_FREE_MODEL,
            "reasoning_effort": "minimal",
            "messages": [{"role": "user", "content": "只输出：OK"}],
            "max_tokens": 512,
            "stream": False,
        }
        response = self.session.post(
            CHAT_COMPLETIONS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=timeout,
        )
        payload = _safe_response(response)
        content = ""
        data = payload.get("data")
        if isinstance(data, dict):
            try:
                content = str(data.get("choices", [{}])[0].get("message", {}).get("content") or "")
            except Exception:
                content = ""
        payload.update({"ok": bool(response.ok and content), "model": model, "content_preview": content[:200]})
        return payload


def account_preview(api_key: str) -> str:
    raw = str(api_key or "")
    if len(raw) <= 16:
        return raw
    return f"{raw[:8]}...{raw[-6:]}"
