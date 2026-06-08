"""Venice HTTP / Clerk protocol client."""

from __future__ import annotations

import base64
import ipaddress
import json
import re
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from requests import Session
from requests import exceptions as req_exc


SEEDANCE_LANDING_URL = "https://venice.ai/lp/seedance"
SEEDANCE_SIGNUP_URL = (
    "https://venice.ai/sign-up?redirect_url=%2Flp%2Fseedance%2Fgenerate&source=seedance-landing"
)
SEEDANCE_GENERATE_URL = "https://venice.ai/lp/seedance/generate"
TURNSTILE_SITEKEY = "0x4AAAAAAAWXJGBD7bONzLBd"
TURNSTILE_SITEKEY_INVISIBLE = "0x4AAAAAAAFV93qQdS0ycilX"
DEFAULT_CLERK_API_VERSION = "2025-11-10"
DEFAULT_CLERK_JS_VERSION = "5.125.10"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)
DEFAULT_PROXY_PRECHECK_URLS = (
    "https://api.ipify.org?format=json",
    "https://httpbin.org/ip",
    "https://ifconfig.me/ip",
)
IP_TOKEN_RE = re.compile(r"[A-Fa-f0-9:.]+")


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


class VeniceClient:
    """Venice Outerface / Clerk / OpenAPI client."""

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        timeout: int = 30,
        log_fn: Optional[Callable[[str], None]] = None,
        session: Optional[Session] = None,
        outerface_base: str = "https://outerface.venice.ai",
        api_base: str = "https://api.venice.ai",
        clerk_base: str = "https://clerk.venice.ai",
        clerk_api_version: str = DEFAULT_CLERK_API_VERSION,
        clerk_js_version: str = DEFAULT_CLERK_JS_VERSION,
    ) -> None:
        self.outerface_base = outerface_base.rstrip("/")
        self.api_base = api_base.rstrip("/")
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
            self._log(f"Venice 代理已配置: {schemes}; trust_env={self.session.trust_env}")

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    def _parse_json(self, response: Any, method: str, url: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(f"Venice returned a non-JSON response [{method} {url}]") from exc

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
            "Venice proxy fallback activated "
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
                raise RuntimeError(f"Venice request failed [{method_upper} {url}]: {exc}") from exc

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        method_upper = method.upper()
        response = self._request(method_upper, url, **kwargs)

        if not 200 <= response.status_code < 300:
            detail = (response.text or "")[:300]
            raise RuntimeError(
                f"Venice HTTP error [{method_upper} {url}] "
                f"status={response.status_code}: {detail}"
            )
        return self._parse_json(response, method_upper, url)

    @staticmethod
    def _extract_origin_ip(raw_text: str) -> str:
        text = str(raw_text or "").strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            for key in ("ip", "origin"):
                value = payload.get(key)
                if value in (None, ""):
                    continue
                candidate = VeniceClient._extract_origin_ip(str(value))
                if candidate:
                    return candidate
        for token in IP_TOKEN_RE.findall(text):
            candidate = token.strip(" ,\"'[]{}()")
            if not candidate:
                continue
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            return candidate
        return ""

    def probe_proxy_origins(self, urls: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        targets = [str(url).strip() for url in (urls or DEFAULT_PROXY_PRECHECK_URLS) if str(url).strip()]
        results: list[dict[str, Any]] = []
        headers = {
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        for url in targets:
            try:
                response = self._request(
                    "GET",
                    url,
                    headers=headers,
                    allow_redirects=True,
                    allow_proxy_fallback=False,
                )
                body = str(response.text or "")
                origin_ip = self._extract_origin_ip(body)
                item = {
                    "url": url,
                    "status": response.status_code,
                    "ip": origin_ip,
                }
                if not origin_ip:
                    item["body_preview"] = body[:160]
                results.append(item)
            except Exception as exc:
                results.append({"url": url, "error": str(exc)[:300]})
        return results

    def _outerface_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Origin": "https://venice.ai",
            "Referer": "https://venice.ai/",
            "User-Agent": DEFAULT_USER_AGENT,
        }

    def _clerk_query_params(self) -> dict[str, str]:
        return {
            "__clerk_api_version": self.clerk_api_version,
            "_clerk_js_version": self.clerk_js_version,
        }

    def _clerk_headers(
        self,
        *,
        referer: str = SEEDANCE_SIGNUP_URL,
        content_type: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Origin": "https://venice.ai",
            "Referer": referer,
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def get_cookie(self, name: str) -> str:
        return str(self.session.cookies.get(name) or "")

    def open_seedance_landing(self) -> None:
        self._log("打开 Venice Seedance 落地页")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://venice.ai/",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        response = self._request(
            "GET",
            SEEDANCE_LANDING_URL,
            headers=headers,
            allow_redirects=True,
        )
        if not 200 <= response.status_code < 300:
            detail = (response.text or "")[:300]
            raise RuntimeError(
                f"Venice Seedance landing returned unexpected status [{SEEDANCE_LANDING_URL}] "
                f"status={response.status_code}: {detail}"
            )

    def get_encrypted_models(
        self,
        access_token: str,
        *,
        mature_filter: bool = True,
        only_safe_venice: bool = True,
    ) -> Any:
        self._log("Venice 模型列表预热")
        return self._request_json(
            "GET",
            f"{self.outerface_base}/api/app/encrypted_models",
            params={
                "matureFilter": str(bool(mature_filter)).lower(),
                "onlySafeVenice": str(bool(only_safe_venice)).lower(),
            },
            headers=self._outerface_headers(access_token),
        )

    def init_clerk_client(self) -> dict[str, Any]:
        self._log("初始化 Venice Clerk 客户端")
        payload = self._request_json(
            "GET",
            f"{self.clerk_base}/v1/client",
            params=self._clerk_query_params(),
            headers=self._clerk_headers(),
        )
        if not isinstance(payload, dict):
            raise ValueError("Clerk client initialization response format is invalid")
        return payload

    def create_sign_up(
        self,
        *,
        email: str,
        password: str,
        captcha_token: str | None = None,
        captcha_widget_type: str = "smart",
        locale: str = "en-US",
    ) -> dict[str, Any]:
        self._log("提交 Venice Clerk 注册")
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

    def prepare_email_verification(self, sign_up_id: str) -> dict[str, Any]:
        self._log("请求 Venice 邮箱验证码")
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
        self._log("提交 Venice 邮箱验证码")
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

    def create_session_token(self, session_id: str) -> dict[str, Any]:
        self._log("创建 Venice 会话令牌")
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

    def get_user_session(self, access_token: str) -> dict[str, Any]:
        self._log("查询 Venice 用户会话")
        payload = self._request_json(
            "GET",
            f"{self.outerface_base}/api/user/session",
            headers=self._outerface_headers(access_token),
        )
        if not isinstance(payload, dict):
            raise ValueError("Venice user session response format is invalid")
        return payload

    def get_api_usage(self, access_token: str, *, lookback: str = "7d") -> dict[str, Any]:
        self._log(f"查询 Venice API 用量 lookback={lookback}")
        payload = self._request_json(
            "GET",
            f"{self.outerface_base}/api/app/user/api/usage",
            params={"lookback": lookback},
            headers=self._outerface_headers(access_token),
        )
        if not isinstance(payload, dict):
            raise ValueError("Venice API usage response format is invalid")
        return payload

    def list_api_keys(self, access_token: str) -> dict[str, Any]:
        self._log("查询 Venice API Keys")
        payload = self._request_json(
            "GET",
            f"{self.outerface_base}/api/app/user/api/api_keys",
            headers=self._outerface_headers(access_token),
        )
        if not isinstance(payload, dict):
            raise ValueError("Venice API key list response format is invalid")
        return payload

    def claim_landing_credit(self, access_token: str) -> dict[str, Any]:
        self._log("领取 Venice Seedance 落地页积分")
        payload = self._request_json(
            "POST",
            f"{self.outerface_base}/api/app/user/landing-credit",
            headers=self._outerface_headers(access_token),
        )
        if not isinstance(payload, dict):
            raise ValueError("Venice landing-credit response format is invalid")
        return payload

    def create_api_key(
        self,
        access_token: str,
        *,
        description: str,
        api_key_type: str = "INFERENCE",
    ) -> dict[str, Any]:
        self._log("创建 Venice API Key")
        payload = self._request_json(
            "POST",
            f"{self.outerface_base}/api/app/user/api/api_keys",
            headers=self._outerface_headers(access_token),
            json={
                "description": description,
                "apiKeyType": api_key_type,
                "consumptionLimit": {"diem": None, "usd": None},
            },
        )
        if not isinstance(payload, dict):
            raise ValueError("Venice create_api_key response format is invalid")
        return payload

    def verify_api_key(self, api_key: str) -> bool:
        if not api_key:
            return False
        self._log("验证 Venice API Key")
        payload = self._request_json(
            "GET",
            f"{self.api_base}/api/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )
        return isinstance(payload, dict) and isinstance(payload.get("data"), list)
