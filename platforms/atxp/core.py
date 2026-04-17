from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

import requests


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


class AtxpClient:
    """ATXP 协议最小客户端。"""

    PRIVY_HEADERS_BASE = {
        "privy-client": "react-auth:3.10.2",
        "privy-app-id": "cma1jnfkk01mml20n6fyvsmll",
        "privy-client-id": "client-WY6L6ApVtkaEUHas1qqZ4fFKtQuUF67ghGYyd82oa5PTw",
        "privy-ui": "t",
        "origin": "https://accounts.atxp.ai",
        "referer": "https://accounts.atxp.ai/",
        "content-type": "application/json",
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
        return response.json()

    def authenticate_privy(self, email: str, code: str, ca_id: str) -> dict[str, Any]:
        self._log("authenticate_privy")
        response = self.session.post(
            "https://auth.privy.io/api/v1/passwordless/authenticate",
            headers=self._privy_headers(ca_id),
            json={"email": email, "code": code, "mode": "login-or-sign-up"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
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
            "https://accounts.atxp.ai/me",
            headers=headers,
            timeout=self.timeout,
        )
        me_response.raise_for_status()
        me_payload = me_response.json()

        ensure_response = self.session.post(
            "https://accounts.atxp.ai/wallets/ensure",
            headers=headers,
            json={},
            timeout=self.timeout,
        )
        ensure_response.raise_for_status()
        ensure_payload = ensure_response.json()

        connection_response = self.session.get(
            "https://accounts.atxp.ai/connection-token",
            headers=headers,
            timeout=self.timeout,
        )
        connection_response.raise_for_status()
        connection_text = getattr(connection_response, "text", "") or ""
        try:
            connection_payload = connection_response.json()
        except ValueError:
            connection_payload = json.loads(connection_text) if connection_text else {}
        if not connection_text:
            connection_text = json.dumps(connection_payload)

        embedded_wallets = me_payload.get("embeddedWallets") or []
        embedded_wallet = embedded_wallets[0] if embedded_wallets else {}
        wallet_address = (
            ((ensure_payload.get("wallet") or {}).get("address"))
            or embedded_wallet.get("address")
            or ""
        )

        return {
            "me": me_payload,
            "wallet_info": ensure_payload,
            "account_id": me_payload.get("accountId", ""),
            "wallet_address": wallet_address,
            "connection_token": connection_payload.get("connectionToken", ""),
            "connection_text": connection_text,
        }

    def probe_gateway_connection(self, connection_string: str) -> dict[str, Any]:
        self._log("probe_gateway_connection")
        response = self.session.get(
            "https://llm.atxp.ai/v1/models",
            headers={"authorization": f"Bearer {connection_string}"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        models = payload.get("data") or []
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

    def _privy_headers(self, ca_id: str) -> dict[str, str]:
        return {**self.PRIVY_HEADERS_BASE, "privy-ca-id": ca_id}

    @staticmethod
    def _bearer_headers(token: str) -> dict[str, str]:
        return {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "origin": "https://accounts.atxp.ai",
            "referer": "https://accounts.atxp.ai/",
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
