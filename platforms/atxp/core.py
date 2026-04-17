from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


class AtxpClient:
    """ATXP 协议最小客户端。"""

    CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    RAW_CONNECTION_TOKEN_RE = re.compile(r"^(?=.*(?:conn|token))[A-Za-z0-9_-]{16,}$", re.IGNORECASE)
    CONNECTION_TOKEN_FIELDS = ("connectionToken", "connection_token")
    PRIVY_HEADERS_BASE = {
        "privy-client": "react-auth:3.10.2",
        "privy-app-id": "cma1jnfkk01mml20n6fyvsmll",
        "privy-client-id": "client-WY6L6ApVtkaEUHas1qqZ4fFKtQuUF67ghGYyd82oa5PTw",
        "privy-ui": "t",
    }

    def __init__(
        self,
        timeout: float = 30.0,
        proxy: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        session: requests.Session | None = None,
        base_url: str = "https://accounts.atxp.ai",
        gateway_url: str = "https://llm.atxp.ai",
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.log_fn = log_fn
        self.base_url = base_url.rstrip("/")
        self.gateway_url = gateway_url.rstrip("/")
        self.session = session or requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def send_privy_code(self, email: str, ca_id: str) -> dict[str, Any]:
        self._log("send_privy_code")
        response = self.session.post(
            "https://auth.privy.io/api/v1/passwordless/init",
            headers=self._privy_headers(ca_id),
            json={"email": email},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._json_object(response, "Privy /passwordless/init")

    def authenticate_privy(self, email: str, code: str, ca_id: str) -> dict[str, Any]:
        self._log("authenticate_privy")
        response = self.session.post(
            "https://auth.privy.io/api/v1/passwordless/authenticate",
            headers=self._privy_headers(ca_id),
            json={"email": email, "code": code, "mode": "login-or-sign-up"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = self._json_object(response, "Privy /passwordless/authenticate")
        refresh_token = (
            payload.get("refresh_token")
            or self._cookie_get(response, "privy-refresh-token")
            or self._cookie_get(response, "refresh_token")
            or self._cookie_get(self.session, "privy-refresh-token")
            or self._cookie_get(self.session, "refresh_token")
            or ""
        )
        payload["refresh_token"] = refresh_token
        return payload

    def fetch_atxp_bundle(self, token: str) -> dict[str, Any]:
        self._log("fetch_atxp_bundle")
        headers = self._bearer_headers(token)

        me_response = self.session.get(
            self._build_url(self.base_url, "/me"),
            headers=headers,
            timeout=self.timeout,
        )
        me_response.raise_for_status()
        me_payload = self._json_object(me_response, "ATXP /me")

        ensure_response = self.session.post(
            self._build_url(self.base_url, "/wallets/ensure"),
            headers=headers,
            json={},
            timeout=self.timeout,
        )
        ensure_response.raise_for_status()
        ensure_payload = self._json_object(ensure_response, "ATXP /wallets/ensure")

        connection_response = self.session.get(
            self._build_url(self.base_url, "/connection-token"),
            headers=headers,
            timeout=self.timeout,
        )
        connection_response.raise_for_status()
        connection_text = getattr(connection_response, "text", "") or ""
        connection_payload: dict[str, Any] | None = None
        try:
            connection_payload = self._json_object(connection_response, "ATXP /connection-token")
        except ValueError:
            connection_payload = self._extract_connection_payload_from_text(connection_text)
        if not connection_text:
            connection_text = json.dumps(connection_payload or {}, ensure_ascii=False)

        wallet_address = self._extract_wallet_address(me_payload, ensure_payload)
        connection_token = (
            self._extract_connection_token(connection_payload)
            or self._extract_connection_token_from_text(connection_text, allow_plain_text=True)
        )
        if not connection_token:
            preview_source = connection_text or json.dumps(connection_payload or {}, ensure_ascii=False)
            raise ValueError(
                "ATXP /connection-token 返回格式无法提取 connection token: "
                f"{self._clip(preview_source)}"
            )

        return {
            "me": me_payload,
            "wallet_info": ensure_payload,
            "account_id": me_payload.get("accountId", ""),
            "wallet_address": wallet_address,
            "connection_token": connection_token,
            "connection_text": connection_text,
        }

    def probe_gateway_connection(self, connection_string: str) -> dict[str, Any]:
        self._log("probe_gateway_connection")
        response = self.session.get(
            self._build_url(self.gateway_url, "/v1/models"),
            headers=self._gateway_headers(connection_string),
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = self._json_object(response, "ATXP Gateway /v1/models")
        models = payload.get("data")
        if not isinstance(models, list):
            raise TypeError(
                f"ATXP Gateway /v1/models.data 必须是 list，实际为 {type(models).__name__}"
            )
        first_model = models[0] if models else {}
        model_id = first_model.get("id", "") if isinstance(first_model, dict) else str(first_model)
        return {
            "success": True,
            "checked_at": _utcnow_iso(),
            "model": model_id,
            "model_count": len(models),
        }

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    def _origin_headers(self) -> dict[str, str]:
        return {
            "user-agent": self.CHROME_UA,
            "accept": "application/json",
            "content-type": "application/json",
            "origin": self.base_url,
            "referer": f"{self.base_url}/",
        }

    def _privy_headers(self, ca_id: str) -> dict[str, str]:
        return {
            **self._origin_headers(),
            **self.PRIVY_HEADERS_BASE,
            "privy-ca-id": ca_id,
        }

    def _bearer_headers(self, token: str) -> dict[str, str]:
        return {
            **self._origin_headers(),
            "authorization": f"Bearer {token}",
        }

    @staticmethod
    def _cookie_get(target: Any, key: str) -> Any:
        cookies = getattr(target, "cookies", None)
        if cookies is None:
            return None
        getter = getattr(cookies, "get", None)
        if callable(getter):
            return getter(key)
        if isinstance(cookies, dict):
            return cookies.get(key)
        return None

    @staticmethod
    def _build_url(base_url: str, path: str) -> str:
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _clip(text: str, max_len: int = 240) -> str:
        text = str(text or "")
        return text if len(text) <= max_len else f"{text[:max_len]}..."

    @staticmethod
    def _json_object(response: Any, label: str) -> dict[str, Any]:
        payload = response.json()
        if not isinstance(payload, dict):
            raise TypeError(f"{label} 响应必须是 JSON object，实际为 {type(payload).__name__}")
        return payload

    @classmethod
    def _extract_connection_payload_from_text(cls, text: str) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped:
            return None
        try:
            payload = json.loads(stripped)
        except ValueError:
            return None
        if isinstance(payload, dict):
            return payload
        return None

    @classmethod
    def _extract_wallet_address(cls, me_payload: dict[str, Any], ensure_payload: dict[str, Any]) -> str:
        nested_wallet = ensure_payload.get("wallet")
        if not isinstance(nested_wallet, dict):
            nested_wallet = {}
        embedded_wallets = me_payload.get("embeddedWallets")
        embedded_wallet = embedded_wallets[0] if isinstance(embedded_wallets, list) and embedded_wallets else {}
        if not isinstance(embedded_wallet, dict):
            embedded_wallet = {}
        return (
            str(ensure_payload.get("address") or "").strip()
            or str(nested_wallet.get("address") or "").strip()
            or str(embedded_wallet.get("address") or "").strip()
        )

    @classmethod
    def _extract_connection_token(cls, payload: Any) -> str:
        if isinstance(payload, dict):
            for key in cls.CONNECTION_TOKEN_FIELDS:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for value in payload.values():
                if not isinstance(value, (dict, list, str)):
                    continue
                token = cls._extract_connection_token(value)
                if token:
                    return token
            return ""
        if isinstance(payload, list):
            for item in payload:
                token = cls._extract_connection_token(item)
                if token:
                    return token
            return ""
        if isinstance(payload, str):
            return cls._extract_connection_token_from_text(payload, allow_plain_text=False)
        return ""

    @classmethod
    def _extract_connection_token_from_text(cls, text: str, *, allow_plain_text: bool) -> str:
        stripped = text.strip()
        if not stripped:
            return ""
        parsed_payload = cls._extract_connection_payload_from_text(stripped)
        if parsed_payload:
            token = cls._extract_connection_token(parsed_payload)
            if token:
                return token
        parsed = urlparse(stripped)
        if parsed.scheme and parsed.netloc:
            query = parse_qs(parsed.query)
            for key in cls.CONNECTION_TOKEN_FIELDS:
                values = query.get(key)
                if values and values[0]:
                    return values[0]
        if allow_plain_text and cls.RAW_CONNECTION_TOKEN_RE.fullmatch(stripped):
            return stripped
        return ""

    @classmethod
    def _gateway_headers(cls, connection_string: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {connection_string}",
            "user-agent": cls.CHROME_UA,
            "accept": "application/json",
            "content-type": "application/json",
        }
