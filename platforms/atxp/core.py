from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import requests


class AtxpClient:
    """ATXP 协议最小客户端。"""

    def __init__(
        self,
        timeout: float = 30.0,
        proxy: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        session: requests.Session | None = None,
        base_url: str = "https://api.atxp.ai",
        gateway_url: str = "https://gateway.atxp.ai",
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.log_fn = log_fn
        self.base_url = base_url.rstrip("/")
        self.gateway_url = gateway_url.rstrip("/")
        self.session = session or requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def send_privy_code(self, email: str) -> dict[str, Any]:
        self._log("send_privy_code")
        response = self.session.post(
            f"{self.base_url}/privy/send-code",
            json={"email": email},
            timeout=self.timeout,
        )
        return response.json()

    def authenticate_privy(self, code: str, email: str | None = None) -> dict[str, Any]:
        self._log("authenticate_privy")
        payload: dict[str, Any] = {"code": code}
        if email:
            payload["email"] = email
        response = self.session.post(
            f"{self.base_url}/privy/authenticate",
            json=payload,
            timeout=self.timeout,
        )
        data = response.json()
        token = self._pick(data, ["token", "access_token"])
        refresh_token = self._pick(data, ["refresh_token"])
        if not refresh_token:
            refresh_token = self._cookie_get(response, "refresh_token")
        if not refresh_token:
            refresh_token = self._cookie_get(self.session, "refresh_token")
        return {
            "token": token,
            "refresh_token": refresh_token,
            "raw": data,
        }

    def fetch_atxp_bundle(self, token: str) -> dict[str, Any]:
        self._log("fetch_atxp_bundle")
        response = self.session.get(
            f"{self.base_url}/bundle",
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        data = response.json()
        root = data.get("data", data)
        me = root.get("me", {})
        wallet_info = root.get("wallet_info", {})
        account_id = wallet_info.get("account_id") or me.get("account_id")
        wallet_address = wallet_info.get("wallet_address") or me.get("wallet_address")
        connection_token = wallet_info.get("connection_token") or root.get("connection_token")
        connection_text = wallet_info.get("connection_text") or root.get("connection_text")
        return {
            "me": me,
            "wallet_info": wallet_info,
            "account_id": account_id,
            "wallet_address": wallet_address,
            "connection_token": connection_token,
            "connection_text": connection_text,
            "raw": data,
        }

    def probe_gateway_connection(self, connection_token: str) -> dict[str, Any]:
        self._log("probe_gateway_connection")
        response = self.session.get(
            f"{self.gateway_url}/models",
            headers={"Authorization": f"Bearer {connection_token}"},
            timeout=self.timeout,
        )
        data = response.json()
        root = data.get("data", data)
        models = root.get("models") or []
        first_model = models[0] if models else None
        if isinstance(first_model, dict):
            model_id = first_model.get("id") or first_model.get("model")
        else:
            model_id = first_model
        return {
            "success": bool(data.get("success", model_id is not None)),
            "checked_at": dt.datetime.now(dt.UTC).isoformat(),
            "model": model_id,
            "model_count": len(models),
            "raw": data,
        }

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    @staticmethod
    def _pick(data: dict[str, Any], keys: list[str]) -> Any:
        for key in keys:
            if key in data and data[key] is not None:
                return data[key]
        nested = data.get("data")
        if isinstance(nested, dict):
            for key in keys:
                if key in nested and nested[key] is not None:
                    return nested[key]
        return None

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
