from __future__ import annotations

import datetime as dt
import json
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests

from core.privy_throttle import acquire_send_slot, execute_with_429_retry


def _utcnow_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


class AtxpClient:
    """ATXP 协议最小客户端。"""

    CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )
    RAW_CONNECTION_TOKEN_RE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z0-9]{16,64}$")
    CONNECTION_TOKEN_FIELDS = ("connectionToken", "connection_token")
    GATEWAY_RETRY_STATUS_CODES = {402, 503}
    GATEWAY_RETRY_MAX_ATTEMPTS = 3
    GATEWAY_RETRY_DELAY_SECONDS = 3.0
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
        # 全局节流 + 429 退避，避免 ATXP/Anuma 并发同时打 auth.privy.io 撞限流
        slept = acquire_send_slot()
        if slept > 0:
            self._log(f"send_privy_code: throttle wait {slept:.2f}s")
        response = execute_with_429_retry(
            lambda: self.session.post(
                "https://auth.privy.io/api/v1/passwordless/init",
                headers=self._privy_headers(ca_id),
                json={"email": email},
                timeout=self.timeout,
            ),
            log_fn=self._log,
            label="atxp send_privy_code",
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

    def refresh_privy_token(self, refresh_token: str) -> dict[str, Any]:
        """用 refresh_token 刷新 privy access token。"""
        self._log("refresh_privy_token")
        headers = {
            **self._origin_headers(),
            **self.PRIVY_HEADERS_BASE,
        }
        response = self.session.post(
            "https://auth.privy.io/api/v1/sessions",
            headers=headers,
            json={"refresh_token": refresh_token},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = self._json_object(response, "Privy /sessions (refresh)")
        new_token = payload.get("token") or payload.get("access_token") or ""
        new_refresh = (
            payload.get("refresh_token")
            or self._cookie_get(response, "privy-refresh-token")
            or self._cookie_get(self.session, "privy-refresh-token")
            or refresh_token
        )
        payload["token"] = new_token
        payload["refresh_token"] = new_refresh
        return payload

    def check_balance(self, token: str, *, refresh_token: str = "") -> dict[str, Any]:
        """检查账户余额和限制状态。

        即使服务端返回 HTTP 402 (fraud_blocked) 也会尝试解析 JSON，
        把 restriction 信息返回给调用方判断，而不是直接抛 HTTPError。
        如果 token 过期 (401)，会尝试用 refresh_token 刷新后重试。
        """
        self._log("check_balance")
        response = self.session.get(
            self._build_url(self.base_url, "/balance"),
            headers=self._bearer_headers(token),
            timeout=self.timeout,
        )
        status_code = int(getattr(response, "status_code", 200) or 200)

        # Token 过期，尝试刷新
        if status_code == 401 and refresh_token:
            self._log("check_balance: token expired, refreshing...")
            try:
                refreshed = self.refresh_privy_token(refresh_token)
                new_token = refreshed.get("token", "")
                if new_token:
                    self._log("check_balance: token refreshed, retrying")
                    response = self.session.get(
                        self._build_url(self.base_url, "/balance"),
                        headers=self._bearer_headers(new_token),
                        timeout=self.timeout,
                    )
                    status_code = int(getattr(response, "status_code", 200) or 200)
                    # 把刷新后的 token 信息附加到结果中
                    result = self._parse_balance_response(response, status_code)
                    result["_refreshed_token"] = new_token
                    result["_refreshed_refresh_token"] = refreshed.get("refresh_token", "")
                    return result
            except Exception as exc:
                self._log(f"check_balance: token refresh failed: {exc}")

        return self._parse_balance_response(response, status_code)

    def check_balance_via_connection(self, connection_token: str) -> dict[str, Any]:
        """用 connection_token 查询 chat.atxp.ai 余额，返回 {balance: number}。"""
        self._log("check_balance_via_connection")
        response = self.session.get(
            "https://chat.atxp.ai/api/balance",
            headers=self._bearer_headers(connection_token),
            timeout=self.timeout,
        )
        response.raise_for_status()
        return self._json_object(response, "ATXP chat /api/balance")

    def _parse_balance_response(self, response: Any, status_code: int) -> dict[str, Any]:
        if status_code == 402:
            try:
                payload = self._json_object(response, "ATXP /balance (402)")
            except (ValueError, TypeError):
                payload = {}
            restriction = payload.get("restriction") or {}
            if not restriction:
                restriction = {"error": "fraud_blocked", "http_status": 402}
            payload.setdefault("restriction", restriction)
            return payload
        if status_code == 404:
            # 部分代理 / 全新账号在 /balance 上会回 404，视为"信息不可用"，与旧版 non-fatal 行为对齐
            self._log("check_balance: 404 (balance unavailable, treated as non-fatal)")
            return {"http_status": 404, "balance_unavailable": True}
        response.raise_for_status()
        return self._json_object(response, "ATXP /balance")

    def probe_gateway_connection(self, connection_string: str) -> dict[str, Any]:
        self._log("probe_gateway_connection")
        for attempt in range(self.GATEWAY_RETRY_MAX_ATTEMPTS):
            response = self.session.get(
                self._build_url(self.gateway_url, "/v1/models"),
                headers=self._gateway_headers(connection_string),
                timeout=self.timeout,
            )
            status_code = int(getattr(response, "status_code", 200) or 200)

            should_retry = (
                status_code in self.GATEWAY_RETRY_STATUS_CODES
                and attempt < self.GATEWAY_RETRY_MAX_ATTEMPTS - 1
            )
            if should_retry:
                self._log(
                    "probe_gateway_connection retry"
                    f" status={status_code}"
                    f" attempt={attempt + 1}/{self.GATEWAY_RETRY_MAX_ATTEMPTS}"
                    f" sleep={self.GATEWAY_RETRY_DELAY_SECONDS}s"
                )
                time.sleep(self.GATEWAY_RETRY_DELAY_SECONDS)
                continue

            if status_code == 402:
                try:
                    payload = self._json_object(response, "ATXP Gateway /v1/models (402)")
                except (ValueError, TypeError):
                    payload = {}
                restriction = payload.get("restriction") if isinstance(payload, dict) else None
                if not isinstance(restriction, dict):
                    restriction = {"error": "payment_required", "http_status": 402}
                error_code = str(restriction.get("error") or "payment_required")
                message = str(restriction.get("message") or response.text[:200] or "Payment Required")
                raise RuntimeError(f"ATXP gateway_402: {error_code}; {message}")

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

        raise RuntimeError("ATXP Gateway probe reached unexpected retry termination")

    CLOWDBOT_URL = "https://clowdbot.atxp.ai"
    AUTH_ATXP_URL = "https://auth.atxp.ai"
    CLOWDBOT_OIDC_LOGIN = "https://clowdbot.atxp.ai/api/v2/auth/login"
    CLOWDBOT_API_V2 = "https://clowdbot.atxp.ai/api/v2"

    def complete_clowdbot_tasks(self, privy_token: str, account_id: str, email: str) -> dict[str, Any]:
        self._log(
            "complete_clowdbot_tasks:"
            f" privy_token={'yes' if privy_token else 'no'}"
            f", account_id={account_id or '-'}"
            f", email={email or '-'}"
        )

        # -- Phase 1: OIDC login to get clowdbot session cookie --
        cb_session = self._clowdbot_oidc_login(privy_token)
        self._log("clowdbot OIDC login completed")

        # Verify authenticated
        check = self._clowdbot_api(cb_session, "/auth/check")
        if not check.get("authenticated"):
            raise RuntimeError("Clowdbot OIDC login failed: not authenticated")

        # -- Phase 2: Read current onboarding state --
        user_info = self._clowdbot_api(cb_session, "/user")
        steps_data = self._clowdbot_api(cb_session, "/onboarding/steps")
        steps = steps_data.get("steps") or []
        self._log(f"onboarding steps: {steps_data.get('completedCount', 0)}/{steps_data.get('totalSteps', 0)}")

        completed_slugs: set[str] = set()
        for step in steps:
            if step.get("completed"):
                slug = step.get("slug") or ""
                if slug:
                    completed_slugs.add(slug)

        result: dict[str, Any] = {
            "instance_id": "",
            "claimed_agent_email": "",
            "create_clowdbot_completed": "create_clowdbot" in completed_slugs,
            "claim_email_completed": "claim_email" in completed_slugs,
            "reward_progress": {
                "completed": steps_data.get("completedCount", 0),
                "total": steps_data.get("totalSteps", 0),
            },
        }

        # -- Phase 3: Complete create_clowdbot --
        if "create_clowdbot" not in completed_slugs:
            try:
                reward = self._clowdbot_api(
                    cb_session, "/onboarding/complete/create_clowdbot", method="POST",
                )
                self._log(f"create_clowdbot: {reward.get('status', '?')} {reward.get('creditDisplay', '')}")
                result["create_clowdbot_completed"] = True
            except Exception as exc:
                self._log(f"create_clowdbot failed: {exc}")
        else:
            self._log("create_clowdbot: already completed")

        # -- Phase 4: Complete claim_email --
        if "claim_email" not in completed_slugs:
            # Derive a username from the email local part
            username = email.split("@")[0] if "@" in email else email
            # Validate username availability
            try:
                email_check = self._clowdbot_api(
                    cb_session, f"/onboarding/check-email/{username}",
                )
                available = email_check.get("available", False)
                agent_email = email_check.get("email", "")
                self._log(f"check-email {username}: available={available} email={agent_email}")
            except Exception as exc:
                self._log(f"check-email failed: {exc}")
                available = False
                agent_email = ""

            try:
                reward = self._clowdbot_api(
                    cb_session, "/onboarding/complete/claim_email", method="POST",
                )
                self._log(f"claim_email: {reward.get('status', '?')} {reward.get('creditDisplay', '')}")
                result["claim_email_completed"] = True
                result["claimed_agent_email"] = agent_email or f"{username}@atxp.email"
            except Exception as exc:
                self._log(f"claim_email failed: {exc}")
        else:
            self._log("claim_email: already completed")

        # -- Phase 5: Re-read final state --
        final_steps = self._clowdbot_api(cb_session, "/onboarding/steps")
        final_instances = self._clowdbot_api(cb_session, "/instance")
        instances = final_instances.get("instances") or []
        if instances:
            first = instances[0] if isinstance(instances[0], dict) else {}
            result["instance_id"] = str(first.get("id") or first.get("instanceId") or "")

        result["reward_progress"] = {
            "completed": final_steps.get("completedCount", 0),
            "total": final_steps.get("totalSteps", 0),
            "earned": final_steps.get("totalEarnedDisplay", ""),
        }

        return result

    # ---- Clowdbot OIDC helpers ----

    def _clowdbot_oidc_login(self, privy_token: str) -> requests.Session:
        """Execute the 3-domain OIDC redirect chain to obtain clowdbot session cookies.

        Flow: clowdbot /auth/login → auth.atxp.ai/authorize → accounts.atxp.ai/authorize
              → auth.atxp.ai/authorize → clowdbot /auth/callback (sets session cookie)
        """
        cb_session = requests.Session()
        if self.proxy:
            cb_session.proxies.update({"http": self.proxy, "https": self.proxy})
        cb_session.headers.update({
            "user-agent": self.CHROME_UA,
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

        # Step 1: clowdbot /auth/login → 302 to auth.atxp.ai (sets oidc_verifier cookie)
        r1 = cb_session.get(self.CLOWDBOT_OIDC_LOGIN, allow_redirects=False, timeout=self.timeout)
        if r1.status_code not in (301, 302, 303, 307):
            raise RuntimeError(f"Clowdbot OIDC step 1 expected redirect, got {r1.status_code}")
        auth_url = r1.headers.get("Location", "")

        # Step 2: auth.atxp.ai/authorize → 302 to accounts.atxp.ai/authorize
        r2 = cb_session.get(auth_url, allow_redirects=False, timeout=self.timeout)
        if r2.status_code not in (301, 302, 303, 307):
            raise RuntimeError(f"Clowdbot OIDC step 2 expected redirect, got {r2.status_code}")
        accounts_url = r2.headers.get("Location", "")

        # Step 3: accounts.atxp.ai/authorize with Privy Bearer → 302 back to auth.atxp.ai
        r3 = cb_session.get(
            accounts_url,
            allow_redirects=False,
            timeout=self.timeout,
            headers={"authorization": f"Bearer {privy_token}"},
        )
        if r3.status_code not in (301, 302, 303, 307):
            raise RuntimeError(f"Clowdbot OIDC step 3 expected redirect, got {r3.status_code}")

        # Steps 4+: Follow remaining redirects (auth.atxp.ai → clowdbot callback → clowdbot /)
        url = r3.headers.get("Location", "")
        for _ in range(6):
            if not url:
                break
            r = cb_session.get(url, allow_redirects=False, timeout=self.timeout)
            if r.status_code not in (301, 302, 303, 307):
                break
            next_url = r.headers.get("Location", "")
            if next_url.startswith("/"):
                parsed_current = urlparse(url)
                next_url = f"{parsed_current.scheme}://{parsed_current.netloc}{next_url}"
            url = next_url

        return cb_session

    def _clowdbot_api(
        self,
        cb_session: requests.Session,
        path: str,
        *,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call a clowdbot API endpoint using cookie-based auth."""
        url = f"{self.CLOWDBOT_API_V2}{path}"
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": self.CLOWDBOT_URL,
            "referer": f"{self.CLOWDBOT_URL}/",
        }
        if method.upper() == "POST":
            response = cb_session.post(url, headers=headers, json=json_body or {}, timeout=self.timeout)
        else:
            response = cb_session.get(url, headers=headers, timeout=self.timeout)
        response.raise_for_status()
        return self._json_object(response, f"Clowdbot {path}")

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
