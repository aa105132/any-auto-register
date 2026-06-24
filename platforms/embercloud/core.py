"""EmberCloud Clerk / OpenAI-compatible inference protocol client.

EmberCloud (embercloud.ai) is a Clerk-authenticated, OpenAI-compatible inference
gateway. Registration goes through Clerk's Frontend API (clerk.embercloud.ai/v1)
with Cloudflare Turnstile captcha enabled; API keys are `ek_live_` prefixed and
managed from the dashboard. This module wraps the protocol pieces shared by the
mailbox worker: Clerk client/sign_up/email-code verification/session token, plus
helpers for locating the dashboard key-create backend.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from requests import Session
from requests import exceptions as req_exc


SITE_URL = "https://www.embercloud.ai/"
SIGN_IN_URL = "https://www.embercloud.ai/sign-in"
DASHBOARD_URL = "https://www.embercloud.ai/dashboard"
KEYS_DASHBOARD_URL = "https://www.embercloud.ai/dashboard/keys"
API_BASE = "https://api.embercloud.ai"
API_V1_BASE = f"{API_BASE}/v1"
MODELS_URL = f"{API_V1_BASE}/models"

CLERK_FRONTEND_BASE = "https://clerk.embercloud.ai"
CLERK_API_VERSION = "2025-11-10"
CLERK_JS_VERSION = "5.125.10"

# Clerk instance environment (实地 /v1/environment 抓取)：Turnstile，smart widget。
TURNSTILE_SITEKEY = "0x4AAAAAAAWXJGBD7bONzLBd"
TURNSTILE_SITEKEY_INVISIBLE = "0x4AAAAAAAFV93qQdS0ycilX"
CAPTCHA_PROVIDER = "turnstile"
CAPTCHA_WIDGET_TYPE = "smart"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

API_KEY_PATTERN = re.compile(r"ek_live_[A-Za-z0-9_-]{32,}")


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _pick_str(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _extract_api_key(data: Any) -> str:
    """在 key 创建响应里尽力找出 ek_live_ 形式的明文 key。"""
    if isinstance(data, str):
        match = API_KEY_PATTERN.search(data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("api_key", "apiKey", "key", "token", "value", "secret", "raw_key", "plaintext"):
            found = _extract_api_key(data.get(key))
            if found:
                return found
        # 任何字符串值里匹配 ek_live_ 前缀，覆盖 {key: {id, ...}, key_prefix, ...} 等结构。
        for value in data.values():
            found = _extract_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _extract_api_key(item)
            if found:
                return found
    return ""


class EmberCloudClient:
    """EmberCloud Clerk Frontend API / inference client."""

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        timeout: int = 30,
        log_fn: Optional[Callable[[str], None]] = None,
        session: Optional[Session] = None,
        clerk_base: str = CLERK_FRONTEND_BASE,
        clerk_api_version: str = CLERK_API_VERSION,
        clerk_js_version: str = CLERK_JS_VERSION,
    ) -> None:
        self.clerk_base = clerk_base.rstrip("/")
        self.clerk_api_version = clerk_api_version
        self.clerk_js_version = clerk_js_version
        self.timeout = timeout
        self.log_fn = log_fn
        self.session = session if session is not None else requests.Session()
        self.session.trust_env = False
        self._proxy_candidates = self._build_proxy_candidates(proxy)
        self._active_proxy_index = 0
        self._apply_proxy_candidate()
        if self._proxy_candidates:
            schemes = " -> ".join(urlsplit(item).scheme or "unknown" for item in self._proxy_candidates)
            self._log(f"EmberCloud 代理已配置: {schemes}; trust_env={self.session.trust_env}")

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    def _parse_json(self, response: Any, method: str, url: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(f"EmberCloud returned a non-JSON response [{method} {url}]") from exc

    @staticmethod
    def _build_proxy_candidates(proxy: str | None) -> list[str]:
        value = str(proxy or "").strip()
        if not value:
            return []
        candidates = [value]
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"}:
            fallback = urlunsplit(("socks5h", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
            if fallback and fallback not in candidates:
                candidates.append(fallback)
        return candidates

    def _apply_proxy_candidate(self) -> None:
        active_proxy = ""
        if self._proxy_candidates:
            active_proxy = self._proxy_candidates[self._active_proxy_index]
        self.session.proxies = (
            {"http": active_proxy, "https": active_proxy}
            if active_proxy
            else {}
        )

    def _activate_next_proxy_candidate(self, exc: Exception, *, method: str, url: str) -> bool:
        if self._active_proxy_index + 1 >= len(self._proxy_candidates):
            return False
        current_proxy = self._proxy_candidates[self._active_proxy_index]
        self._active_proxy_index += 1
        next_proxy = self._proxy_candidates[self._active_proxy_index]
        self._apply_proxy_candidate()
        self._log(
            "EmberCloud proxy fallback activated "
            f"[{method} {url}] {urlsplit(current_proxy).scheme or 'unknown'} -> "
            f"{urlsplit(next_proxy).scheme or 'unknown'}: {str(exc)[:240]}"
        )
        return True

    def _request(
        self,
        method: str,
        url: str,
        *,
        allow_proxy_fallback: bool = True,
        **kwargs: Any,
    ):
        method_upper = method.upper()
        while True:
            try:
                return self.session.request(method_upper, url, timeout=self.timeout, **kwargs)
            except req_exc.RequestException as exc:
                if allow_proxy_fallback and self._activate_next_proxy_candidate(
                    exc,
                    method=method_upper,
                    url=url,
                ):
                    continue
                raise RuntimeError(f"EmberCloud request failed [{method_upper} {url}]: {exc}") from exc

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        method_upper = method.upper()
        response = self._request(method_upper, url, **kwargs)
        if not 200 <= response.status_code < 300:
            detail = (response.text or "")[:400]
            raise RuntimeError(
                f"EmberCloud HTTP error [{method_upper} {url}] "
                f"status={response.status_code}: {detail}"
            )
        return self._parse_json(response, method_upper, url)

    def get_cookie(self, name: str) -> str:
        return str(self.session.cookies.get(name) or "")

    def _clerk_query_params(self) -> dict[str, str]:
        return {
            "__clerk_api_version": self.clerk_api_version,
            "_clerk_js_version": self.clerk_js_version,
        }

    def _clerk_headers(
        self,
        *,
        referer: str = SIGN_IN_URL,
        content_type: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Origin": "https://www.embercloud.ai",
            "Referer": referer,
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def init_clerk_client(self) -> dict[str, Any]:
        self._log("初始化 EmberCloud Clerk 客户端")
        payload = self._request_json(
            "GET",
            f"{self.clerk_base}/v1/client",
            params=self._clerk_query_params(),
            headers=self._clerk_headers(referer="https://www.embercloud.ai/"),
        )
        if not isinstance(payload, dict):
            raise ValueError("Clerk client initialization response format is invalid")
        return payload

    def get_environment(self) -> dict[str, Any]:
        payload = self._request_json(
            "GET",
            f"{self.clerk_base}/v1/environment",
            params=self._clerk_query_params(),
            headers=self._clerk_headers(referer="https://www.embercloud.ai/"),
        )
        if not isinstance(payload, dict):
            raise ValueError("Clerk environment response format is invalid")
        return payload

    @staticmethod
    def _needs_captcha_retry(payload: Any, exc: Exception | None = None) -> bool:
        text = ""
        if isinstance(payload, dict):
            text = json.dumps(payload, ensure_ascii=False)
        if exc is not None:
            text = f"{text} {exc}"
        text = str(text or "").lower()
        return any(
            marker in text
            for marker in (
                "captcha_missing_token",
                "captcha_required",
                "captcha_invalid",
                "captcha token",
                "failed security validations",
                "authentication unsuccessful",
            )
        )

    def create_sign_up(
        self,
        *,
        email: str,
        password: str,
        captcha_token: str | None = None,
        captcha_widget_type: str = CAPTCHA_WIDGET_TYPE,
        locale: str = "en-US",
    ) -> dict[str, Any]:
        self._log("提交 EmberCloud Clerk 注册")
        form_data = {
            "email_address": email,
            "password": password,
            "captcha_widget_type": captcha_widget_type,
            "locale": locale,
        }
        if captcha_token:
            form_data["captcha_token"] = captcha_token
        payload = self._request_json(
            "POST",
            f"{self.clerk_base}/v1/client/sign_ups",
            params=self._clerk_query_params(),
            data=form_data,
            headers=self._clerk_headers(content_type="application/x-www-form-urlencoded"),
        )
        if not isinstance(payload, dict):
            raise ValueError("Clerk sign_up response format is invalid")
        return payload

    @staticmethod
    def extract_sign_up_id(payload: dict[str, Any]) -> str:
        sign_up_id = _pick_str(payload, "id")
        if sign_up_id:
            return sign_up_id
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        sign_up_id = _pick_str(response, "id")
        if sign_up_id:
            return sign_up_id
        client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
        sign_up = client.get("sign_up") if isinstance(client.get("sign_up"), dict) else {}
        return _pick_str(sign_up, "id")

    def prepare_email_verification(self, sign_up_id: str) -> dict[str, Any]:
        self._log("请求 EmberCloud 邮箱验证码")
        payload = self._request_json(
            "POST",
            f"{self.clerk_base}/v1/client/sign_ups/{sign_up_id}/prepare_verification",
            params=self._clerk_query_params(),
            data={"strategy": "email_code"},
            headers=self._clerk_headers(content_type="application/x-www-form-urlencoded"),
        )
        if not isinstance(payload, dict):
            raise ValueError("Clerk prepare_verification response format is invalid")
        return payload

    def attempt_email_verification(self, sign_up_id: str, *, code: str) -> dict[str, Any]:
        self._log("提交 EmberCloud 邮箱验证码")
        payload = self._request_json(
            "POST",
            f"{self.clerk_base}/v1/client/sign_ups/{sign_up_id}/attempt_verification",
            params=self._clerk_query_params(),
            data={"strategy": "email_code", "code": code},
            headers=self._clerk_headers(content_type="application/x-www-form-urlencoded"),
        )
        if not isinstance(payload, dict):
            raise ValueError("Clerk attempt_verification response format is invalid")
        return payload

    @staticmethod
    def extract_verification_session_id(payload: dict[str, Any]) -> str:
        session_id = _pick_str(payload, "created_session_id", "session_id")
        if session_id:
            return session_id
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        session_id = _pick_str(response, "created_session_id", "session_id")
        if session_id:
            return session_id
        client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
        session_id = _pick_str(client, "last_active_session_id", "created_session_id", "session_id")
        if session_id:
            return session_id
        for item in list(client.get("sessions") or [])[:5]:
            if isinstance(item, dict):
                session_id = _pick_str(item, "id", "session_id")
                if session_id:
                    return session_id
        sign_up = client.get("sign_up") if isinstance(client.get("sign_up"), dict) else {}
        return _pick_str(sign_up, "created_session_id", "session_id")

    @staticmethod
    def extract_verification_user_id(payload: dict[str, Any]) -> str:
        user_id = _pick_str(payload, "created_user_id", "user_id")
        if user_id:
            return user_id
        response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
        user_id = _pick_str(response, "created_user_id", "user_id")
        if user_id:
            return user_id
        client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
        sign_up = client.get("sign_up") if isinstance(client.get("sign_up"), dict) else {}
        return _pick_str(sign_up, "created_user_id", "user_id")

    def create_session_token(self, session_id: str) -> dict[str, Any]:
        self._log("创建 EmberCloud 会话令牌")
        payload = self._request_json(
            "POST",
            f"{self.clerk_base}/v1/client/sessions/{session_id}/tokens",
            params=self._clerk_query_params(),
            headers=self._clerk_headers(content_type="application/x-www-form-urlencoded"),
            data={},
        )
        if not isinstance(payload, dict):
            raise ValueError("Clerk session token response format is invalid")
        return payload

    def collect_auth_state(
        self,
        *,
        access_token: str,
        default_session_id: str = "",
        default_user_id: str = "",
    ) -> dict[str, str]:
        client_cookie = self.get_cookie("__client")
        session_cookie = self.get_cookie("__session") or access_token
        client_payload = decode_jwt_payload(client_cookie)
        session_payload = decode_jwt_payload(session_cookie or access_token)
        refresh_token = str(client_payload.get("rotating_token") or "")
        return {
            "access_token": str(access_token or session_cookie or ""),
            "session_token": str(session_cookie or access_token or ""),
            "refresh_token": refresh_token,
            "refresh_token_source": "clerk.__client.rotating_token" if refresh_token else "",
            "client_id": _pick_str(client_payload, "id"),
            "client_cookie": client_cookie,
            "session_cookie": session_cookie,
            "session_id": _pick_str(session_payload, "sid") or default_session_id,
            "user_id": _pick_str(session_payload, "sub") or default_user_id,
        }

    def verify_api_key(self, api_key: str) -> bool:
        if not api_key:
            return False
        self._log("验证 EmberCloud API Key")
        payload = self._request_json(
            "GET",
            MODELS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        return isinstance(payload, dict) and isinstance(payload.get("data"), list)

    def list_models_raw(self, api_key: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            MODELS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
