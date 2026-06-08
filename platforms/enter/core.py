"""Enter platform - Auth0 protocol client."""

from __future__ import annotations

import re
import time
import urllib.parse
import uuid
from typing import Any

import requests

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

AUTH0_DOMAIN = "auth.converge.ai"
APP_ORIGIN = "https://enter.converge.ai"
API_DOMAIN = "api.enter.pro"
API_AUDIENCE = "https://api.enter.pro"
CLIENT_ID = "anCisSaaIA36fTZ2DUMiTMro3bYuptrf"
# PKCE verifier/challenge 由本地注册链路固定生成；Auth0 只要求二者匹配。
CODE_VERIFIER = "m8Tg8P7x9P4g2QmW0K4bF6vE1LxN3sR5uY7cD9nH2jK6pQ1a"
CODE_CHALLENGE = "wl4xqt5G44TNv8KzmVRFFFXlrz0MfMIA1hVyffSZHuk"
REDIRECT_URI = APP_ORIGIN
SIGNUP_URL = f"https://{AUTH0_DOMAIN}/authorize"
TOKEN_URL = f"https://{AUTH0_DOMAIN}/oauth/token"
API_BASE = f"https://{API_DOMAIN}/code/api/v1"


def is_success_response(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    code = payload.get("code")
    if code is None:
        return True
    return code in (0, 200, "0", "200", "success")


def extract_ai_api_token(payload: Any) -> str:
    """从 Enter API 的新旧返回结构中提取可用 AI/API Key。"""
    token_keys = {
        "aiApiToken",
        "ai_api_token",
        "apiKey",
        "api_key",
        "key",
        "token",
        "secret",
        "secretKey",
        "secret_key",
        "value",
    }
    seen: set[int] = set()

    def walk(value: Any) -> str:
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith(("sk-", "sk_", "sk-nh_", "ent_", "ak_")) and "*" not in raw:
                return raw
            return ""
        if isinstance(value, dict):
            obj_id = id(value)
            if obj_id in seen:
                return ""
            seen.add(obj_id)
            for key in token_keys:
                raw = value.get(key)
                if isinstance(raw, str):
                    raw = raw.strip()
                    if raw and "*" not in raw:
                        return raw
            for key in ("data", "api_key", "apiKey", "key", "token", "secret", "items", "keys", "apiKeys"):
                if key in value:
                    found = walk(value.get(key))
                    if found:
                        return found
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        if isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return ""

    return walk(payload)


def _utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _parse_auth_code_from_url(url: str) -> str | None:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    return code


def _extract_otp_from_text(text: str) -> str | None:
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return m.group(0) if m else None


class EnterClient:
    def __init__(
        self,
        proxy: str | None = None,
        timeout: int = 30,
        session: requests.Session | None = None,
        log_fn: Any = None,
    ):
        self._proxy = proxy
        self._timeout = timeout
        self._session = session or requests.Session()
        self._log = log_fn or (lambda msg: None)

    def _log_msg(self, msg: str) -> None:
        self._log(msg)

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        kwargs.setdefault("allow_redirects", False)
        if self._proxy:
            kwargs["proxies"] = {"http": self._proxy, "https": self._proxy}
        return self._session.request(method, url, **kwargs)

    def _request_json(self, method: str, url: str, **kwargs) -> dict[str, Any] | list[Any]:
        r = self._request(method, url, **kwargs)
        try:
            return r.json()
        except ValueError:
            return {}

    def build_signup_url(self, state: str = "", nonce: str = "") -> str:
        if not state:
            state = f"signup-state-{uuid.uuid4().hex[:12]}"
        if not nonce:
            nonce = f"signup-nonce-{uuid.uuid4().hex[:12]}"
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "response_mode": "query",
            "scope": "openid profile email offline_access",
            "audience": API_AUDIENCE,
            "state": state,
            "nonce": nonce,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "screen_hint": "signup",
        }
        return f"{SIGNUP_URL}?{urllib.parse.urlencode(params)}"

    def build_login_url(self, state: str = "", nonce: str = "") -> str:
        if not state:
            state = f"login-state-{uuid.uuid4().hex[:12]}"
        if not nonce:
            nonce = f"login-nonce-{uuid.uuid4().hex[:12]}"
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "response_mode": "query",
            "scope": "openid profile email offline_access",
            "audience": API_AUDIENCE,
            "state": state,
            "nonce": nonce,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
        }
        return f"{SIGNUP_URL}?{urllib.parse.urlencode(params)}"

    def exchange_code_for_tokens(self, code: str) -> dict[str, Any]:
        payload = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code_verifier": CODE_VERIFIER,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
        headers = {"Content-Type": "application/json"}
        r = self._request("POST", TOKEN_URL, json=payload, headers=headers)
        data = r.json() if r.ok else {}
        return {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", ""),
            "id_token": data.get("id_token", ""),
            "expires_in": data.get("expires_in", 0),
            "token_type": data.get("token_type", ""),
        }

    def refresh_token(self, refresh_token: str) -> dict[str, Any]:
        payload = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }
        headers = {"Content-Type": "application/json"}
        r = self._request("POST", TOKEN_URL, json=payload, headers=headers)
        data = r.json() if r.ok else {}
        return {
            "access_token": data.get("access_token", ""),
            "refresh_token": data.get("refresh_token", data.get("access_token") or ""),
            "id_token": data.get("id_token", ""),
            "expires_in": data.get("expires_in", 0),
            "token_type": data.get("token_type", ""),
        }

    def get_workspaces(self, access_token: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        }
        return self._request_json("GET", f"{API_BASE}/workspaces", headers=headers, timeout=self._timeout)

    def get_user_info(self, access_token: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        }
        return self._request_json("GET", f"{API_BASE}/users/info", headers=headers, timeout=self._timeout)

    def get_or_create_project(self, access_token: str, workspace_id: str, name: str = "sandbox", prompt: str = "Hello") -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }
        body = {"name": name, "prompt": prompt}
        return self._request_json(
            "POST",
            f"{API_BASE}/workspaces/{workspace_id}/projects",
            headers=headers,
            json=body,
            timeout=self._timeout,
        )

    def enable_entercloud(self, access_token: str, project_id: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        }
        return self._request_json(
            "POST",
            f"{API_BASE}/projects/{project_id}/entercloud/enable",
            headers=headers,
            timeout=90,
        )

    def get_entercloud_status(self, access_token: str, project_id: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        }
        return self._request_json(
            "GET",
            f"{API_BASE}/projects/{project_id}/entercloud/status",
            headers=headers,
            timeout=60,
        )

    def connect_ai_capability(self, access_token: str, project_id: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        }
        return self._request_json(
            "POST",
            f"{API_BASE}/projects/{project_id}/ai-capability/connect",
            headers=headers,
            timeout=60,
        )

    def get_ai_capability_stats(self, access_token: str, workspace_id: str, project_id: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        }
        return self._request_json(
            "GET",
            f"{API_BASE}/workspaces/{workspace_id}/projects/{project_id}/ai-capability/stats",
            headers=headers,
            timeout=60,
        )

    def claim_referral(self, access_token: str, code: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        }
        return self._request_json(
            "POST",
            f"{API_BASE}/referral/claim",
            headers=headers,
            params={"code": code},
            timeout=self._timeout,
        )

    def remix_project(self, access_token: str, workspace_id: str, project_id: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
        }
        return self._request_json(
            "POST",
            f"{API_BASE}/projects/{project_id}/remix",
            headers=headers,
            json={"workspace_id": workspace_id},
            timeout=self._timeout,
        )

    def get_classroom_quests(self, access_token: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Origin": APP_ORIGIN,
            "Referer": f"{APP_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
        }
        return self._request_json(
            "GET",
            f"{API_BASE}/classroom/quests",
            headers=headers,
            timeout=self._timeout,
        )
