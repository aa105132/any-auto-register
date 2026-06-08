"""Venice protocol mailbox registration worker."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from typing import Any, Callable, Optional

from platforms.venice.core import (
    DEFAULT_PROXY_PRECHECK_URLS,
    SEEDANCE_LANDING_URL,
    SEEDANCE_GENERATE_URL,
    SEEDANCE_SIGNUP_URL,
    TURNSTILE_SITEKEY,
    VeniceClient,
    decode_jwt_payload,
)

LANDING_CREDIT_PROMO_VALUE_TO_CREDITS = 100


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _pick_str(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _trim_profile(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": payload.get("email", ""),
        "user_id": payload.get("userId", ""),
        "user_name": payload.get("userName", ""),
        "user_type": payload.get("userType", ""),
        "user_country": payload.get("userCountry", ""),
        "venice_credits": int(payload.get("veniceCredits") or 0),
        "venice_mode": payload.get("veniceMode", ""),
        "referral_code": payload.get("referralCode", ""),
        "rate_limits": payload.get("rateLimits") or {},
    }


def _trim_api_usage(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "lookback": payload.get("lookback", ""),
        "byKey": payload.get("byKey") or [],
        "topKeyNames": payload.get("topKeyNames") or [],
    }


def _mask_sensitive(value: Any, *, depth: int = 0) -> Any:
    if depth >= 3:
        return "..."
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).lower()
            if any(marker in key_lower for marker in ("token", "password", "secret", "cookie", "jwt", "authorization", "api_key")):
                masked[str(key)] = "***"
            else:
                masked[str(key)] = _mask_sensitive(item, depth=depth + 1)
        return masked
    if isinstance(value, list):
        return [_mask_sensitive(item, depth=depth + 1) for item in value[:8]]
    return value


def _extract_error_codes(payload: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    response = payload.get("response") if isinstance(payload.get("response"), dict) else None
    for container in (payload, response):
        if not isinstance(container, dict):
            continue
        for item in list(container.get("errors") or [])[:8]:
            if isinstance(item, dict):
                candidate = item.get("code") or item.get("message") or item.get("long_message")
                if candidate:
                    codes.append(str(candidate))
            elif item:
                codes.append(str(item))
    return codes


def _summarize_sign_up_payload(payload: dict[str, Any]) -> str:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    summary = {
        "top_level_keys": sorted(str(key) for key in payload.keys()),
        "id": payload.get("id"),
        "status": payload.get("status"),
        "response_id": response.get("id"),
        "response_status": response.get("status"),
        "error_codes": _extract_error_codes(payload),
        "payload_preview": _mask_sensitive(payload),
    }
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 1200 else text[:1200] + "...<truncated>"


def _extract_sign_up_id(payload: dict[str, Any]) -> str:
    sign_up_id = _pick_str(payload, "id")
    if sign_up_id:
        return sign_up_id
    response = payload.get("response")
    if isinstance(response, dict):
        sign_up_id = _pick_str(response, "id")
        if sign_up_id:
            return sign_up_id
    client = payload.get("client")
    if isinstance(client, dict):
        sign_up = client.get("sign_up")
        if isinstance(sign_up, dict):
            sign_up_id = _pick_str(sign_up, "id")
            if sign_up_id:
                return sign_up_id
    return ""


def _summarize_verification_payload(payload: dict[str, Any]) -> str:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    summary = {
        "top_level_keys": sorted(str(key) for key in payload.keys()),
        "created_session_id": payload.get("created_session_id"),
        "session_id": payload.get("session_id"),
        "created_user_id": payload.get("created_user_id"),
        "user_id": payload.get("user_id"),
        "response_created_session_id": response.get("created_session_id"),
        "response_session_id": response.get("session_id"),
        "response_created_user_id": response.get("created_user_id"),
        "response_user_id": response.get("user_id"),
        "error_codes": _extract_error_codes(payload),
        "payload_preview": _mask_sensitive(payload),
    }
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 1200 else text[:1200] + "...<truncated>"


def _extract_verification_session_id(payload: dict[str, Any]) -> str:
    session_id = _pick_str(payload, "created_session_id", "session_id")
    if session_id:
        return session_id
    response = payload.get("response")
    if isinstance(response, dict):
        session_id = _pick_str(response, "created_session_id", "session_id")
        if session_id:
            return session_id
    client = payload.get("client")
    if isinstance(client, dict):
        session_id = _pick_str(client, "last_active_session_id", "created_session_id", "session_id")
        if session_id:
            return session_id
        sessions = list(client.get("sessions") or [])
        for item in sessions[:5]:
            if isinstance(item, dict):
                session_id = _pick_str(item, "id", "session_id")
                if session_id:
                    return session_id
        sign_up = client.get("sign_up")
        if isinstance(sign_up, dict):
            session_id = _pick_str(sign_up, "created_session_id", "session_id")
            if session_id:
                return session_id
    return ""


def _extract_verification_user_id(payload: dict[str, Any]) -> str:
    user_id = _pick_str(payload, "created_user_id", "user_id")
    if user_id:
        return user_id
    response = payload.get("response")
    if isinstance(response, dict):
        user_id = _pick_str(response, "created_user_id", "user_id")
        if user_id:
            return user_id
    client = payload.get("client")
    if isinstance(client, dict):
        sign_up = client.get("sign_up")
        if isinstance(sign_up, dict):
            user_id = _pick_str(sign_up, "created_user_id", "user_id")
            if user_id:
                return user_id
    return ""


def _summarize_landing_credit_payload(payload: dict[str, Any]) -> str:
    summary = {
        "top_level_keys": sorted(str(key) for key in payload.keys()),
        "alreadyRedeemed": payload.get("alreadyRedeemed"),
        "creditsAmount": payload.get("creditsAmount"),
        "payload_preview": _mask_sensitive(payload),
    }
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 1200 else text[:1200] + "...<truncated>"


def _summarize_user_session_payload(payload: dict[str, Any]) -> str:
    summary = {
        "top_level_keys": sorted(str(key) for key in payload.keys()),
        "email": payload.get("email"),
        "userId": payload.get("userId"),
        "userName": payload.get("userName"),
        "userType": payload.get("userType"),
        "veniceCredits": payload.get("veniceCredits"),
        "has_token": bool(payload.get("token")),
        "payload_preview": _mask_sensitive(payload),
    }
    text = json.dumps(summary, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 1200 else text[:1200] + "...<truncated>"


class VeniceProtocolMailboxWorker:
    def __init__(
        self,
        *,
        client: VeniceClient | None = None,
        proxy: str | None = None,
        api_key_description: str = "seedance-auto",
        expected_credits: int = 500,
        credits_poll_attempts: int = 1,
        credits_poll_interval_sec: float = 2.0,
        proxy_precheck_enabled: bool = False,
        log_fn: Callable[[str], None] = print,
        **_kwargs,
    ) -> None:
        self.client = client or VeniceClient(proxy=proxy, log_fn=log_fn)
        self.api_key_description = api_key_description
        self.expected_credits = expected_credits
        self.credits_poll_attempts = max(int(credits_poll_attempts or 1), 1)
        self.credits_poll_interval_sec = max(float(credits_poll_interval_sec or 0.0), 0.0)
        self.proxy_precheck_enabled = bool(proxy_precheck_enabled)
        self.log = log_fn

    def _solve_turnstile(self, captcha_solver) -> str:
        if captcha_solver is None:
            raise RuntimeError("Venice 协议注册缺少验证码解决器")
        self.log("Venice Turnstile 打码中…")
        token = str(captcha_solver.solve_turnstile(SEEDANCE_SIGNUP_URL, TURNSTILE_SITEKEY) or "").strip()
        if not token:
            raise RuntimeError("Venice Turnstile 打码返回空 token")
        return token

    @staticmethod
    def _needs_captcha_retry(exc: Exception) -> bool:
        message = str(exc or "").lower()
        return any(
            marker in message
            for marker in (
                "captcha_missing_token",
                "captcha_required",
                "captcha_invalid",
                "captcha token",
                "failed security validations",
                "authentication unsuccessful",
            )
        )

    def _run_proxy_precheck(self) -> None:
        results = self.client.probe_proxy_origins(DEFAULT_PROXY_PRECHECK_URLS)
        summary = json.dumps(results, ensure_ascii=False, sort_keys=True)
        self.log(f"Venice proxy precheck summary: {summary}")
        ips = [
            str(item.get("ip") or "").strip()
            for item in results
            if isinstance(item, dict) and str(item.get("ip") or "").strip()
        ]
        unique_ips = sorted(set(ips))
        if len(unique_ips) > 1:
            raise RuntimeError(f"Venice proxy precheck IP drift: {unique_ips}")

    def _create_sign_up_with_optional_captcha(
        self,
        *,
        email: str,
        password: str,
        captcha_solver,
    ) -> dict[str, Any]:
        try:
            return self.client.create_sign_up(email=email, password=password, captcha_token=None)
        except Exception as exc:
            if not self._needs_captcha_retry(exc):
                raise
            if captcha_solver is None:
                raise
            captcha_token = self._solve_turnstile(captcha_solver)
            return self.client.create_sign_up(
                email=email,
                password=password,
                captcha_token=captcha_token,
            )

    def _collect_auth_state(
        self,
        *,
        access_token: str,
        default_session_id: str = "",
        default_user_id: str = "",
        default_client_id: str = "",
    ) -> dict[str, str]:
        client_cookie = self.client.get_cookie("__client")
        session_cookie = self.client.get_cookie("__session") or access_token
        client_payload = decode_jwt_payload(client_cookie)
        session_payload = decode_jwt_payload(session_cookie or access_token)
        refresh_token = str(client_payload.get("rotating_token") or "")
        return {
            "access_token": str(access_token or session_cookie or ""),
            "session_token": str(session_cookie or access_token or ""),
            "refresh_token": refresh_token,
            "refresh_token_source": "clerk.__client.rotating_token" if refresh_token else "",
            "client_id": _pick_str(client_payload, "id") or default_client_id,
            "client_cookie": client_cookie,
            "session_cookie": session_cookie,
            "session_id": _pick_str(session_payload, "sid") or default_session_id,
            "user_id": _pick_str(session_payload, "sub") or default_user_id,
        }

    def _extract_api_key(self, payload: dict[str, Any]) -> str:
        data = payload.get("data")
        if isinstance(data, dict):
            return _pick_str(data, "apiKey", "key", "token")
        return _pick_str(payload, "apiKey", "key", "token")

    def _log_sign_up_summary(self, payload: dict[str, Any], *, stage: str) -> str:
        summary = _summarize_sign_up_payload(payload)
        self.log(f"Venice sign_up 响应[{stage}]: {summary}")
        return summary

    def _log_verification_summary(self, payload: dict[str, Any]) -> str:
        summary = _summarize_verification_payload(payload)
        self.log(f"Venice 邮箱验证响应: {summary}")
        return summary

    def _log_landing_credit_summary(self, payload: dict[str, Any]) -> str:
        summary = _summarize_landing_credit_payload(payload)
        inferred_credits = self._landing_credit_bonus_credits(payload)
        self.log(
            f"Venice landing-credit: {summary}; "
            f"inferred_credits={inferred_credits}; expected_credits={self.expected_credits}"
        )
        return summary

    def _log_user_session_summary(self, payload: dict[str, Any], *, stage: str) -> str:
        summary = _summarize_user_session_payload(payload)
        self.log(f"Venice 用户会话[{stage}]: {summary}")
        return summary

    def _landing_credit_bonus_credits(self, payload: dict[str, Any]) -> int:
        if not isinstance(payload, dict) or bool(payload.get("alreadyRedeemed")):
            return 0
        promo_value = float(payload.get("creditsAmount") or 0)
        if promo_value <= 0:
            return 0

        direct_credits = int(round(promo_value))
        if direct_credits >= self.expected_credits:
            return direct_credits

        scaled_credits = int(round(promo_value * LANDING_CREDIT_PROMO_VALUE_TO_CREDITS))
        if scaled_credits >= self.expected_credits:
            return scaled_credits
        return 0

    @staticmethod
    def _is_token_only_user_session(payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        keys = {str(key) for key in payload.keys()}
        return bool(payload.get("token")) and keys <= {"token"}

    def _wait_for_expected_credits(self, access_token: str, landing_credit_payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
        last_summary = ""
        last_credits = 0
        last_payload: dict[str, Any] = {}
        inferred_credits = self._landing_credit_bonus_credits(landing_credit_payload)
        for attempt in range(1, self.credits_poll_attempts + 1):
            payload = self.client.get_user_session(access_token)
            last_payload = payload if isinstance(payload, dict) else {}
            last_credits = int(last_payload.get("veniceCredits") or 0)
            last_summary = self._log_user_session_summary(last_payload, stage=f"poll-{attempt}")
            if last_credits >= self.expected_credits:
                return last_payload, last_credits
            if inferred_credits >= self.expected_credits and self._is_token_only_user_session(last_payload):
                inferred_payload = dict(last_payload)
                inferred_payload["veniceCredits"] = inferred_credits
                self.log(f"Venice 落地页积分已确认; 推断积分={inferred_credits}")
                return inferred_payload, inferred_credits
            if attempt < self.credits_poll_attempts:
                self.log(
                    f"Venice 积分轮询 [{attempt}/{self.credits_poll_attempts}] "
                    f"当前={last_credits} 目标>={self.expected_credits}"
                )
                if self.credits_poll_interval_sec > 0:
                    time.sleep(self.credits_poll_interval_sec)
        if inferred_credits >= self.expected_credits and self._is_token_only_user_session(last_payload):
            inferred_payload = dict(last_payload)
            inferred_payload["veniceCredits"] = inferred_credits
            self.log(f"Venice 落地页积分已确认（token-only 会话）; 最终推断积分={inferred_credits}")
            return inferred_payload, inferred_credits
        raise RuntimeError(
            f"Seedance 注册未达到预期 credits={last_credits}; expected_credits>={self.expected_credits}"
        )

    def _bootstrap_seedance_context(self, access_token: str) -> None:
        self.log("Venice Seedance 上下文预热中…")
        self.client.open_seedance_landing()
        self.client.get_encrypted_models(access_token)
        bootstrap_payload = self.client.get_user_session(access_token)
        payload = bootstrap_payload if isinstance(bootstrap_payload, dict) else {}
        self._log_user_session_summary(payload, stage="bootstrap-1")

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Optional[Callable[[], str]] = None,
        captcha_solver=None,
    ) -> dict[str, Any]:
        if self.proxy_precheck_enabled:
            self._run_proxy_precheck()
        clerk_client = self.client.init_clerk_client()
        client_id = _pick_str(dict(clerk_client.get("response") or {}), "id") or _pick_str(clerk_client, "id")
        sign_up = self._create_sign_up_with_optional_captcha(
            email=email,
            password=password,
            captcha_solver=captcha_solver,
        )
        sign_up_summary = self._log_sign_up_summary(sign_up, stage="注册提交")
        sign_up_id = _extract_sign_up_id(sign_up)
        if not sign_up_id:
            raise RuntimeError(f"Venice 注册响应缺少 sign_up_id: {sign_up_summary}")

        self.client.prepare_email_verification(sign_up_id)
        otp = str(otp_callback() if otp_callback else "").strip()
        if not otp:
            raise RuntimeError("未收到 Venice 验证码")

        verification = self.client.attempt_email_verification(sign_up_id, code=otp)
        verification_summary = self._log_verification_summary(verification)
        session_id = _extract_verification_session_id(verification)
        user_id = _extract_verification_user_id(verification)
        if not session_id:
            raise RuntimeError(f"Venice 邮箱验证完成但缺少 session_id")

        session_token_payload = self.client.create_session_token(session_id)
        access_token = _pick_str(session_token_payload, "jwt", "token", "session_token")
        if not access_token:
            raise RuntimeError("Venice 会话令牌接口未返回 jwt")

        auth_state = self._collect_auth_state(
            access_token=access_token,
            default_session_id=session_id,
            default_user_id=user_id,
            default_client_id=client_id,
        )

        self._bootstrap_seedance_context(auth_state["access_token"])
        landing_credit_payload = self.client.claim_landing_credit(auth_state["access_token"])
        self._log_landing_credit_summary(landing_credit_payload)
        user_session, credits = self._wait_for_expected_credits(auth_state["access_token"], landing_credit_payload)

        api_usage = self.client.get_api_usage(auth_state["access_token"])
        api_key_payload = self.client.create_api_key(
            auth_state["access_token"],
            description=self.api_key_description,
        )
        api_key = self._extract_api_key(api_key_payload)
        if not api_key:
            raise RuntimeError("Venice API Key 已创建但响应中缺少 apiKey")

        api_keys_payload = self.client.list_api_keys(auth_state["access_token"])
        api_keys = list(api_keys_payload.get("data") or []) if isinstance(api_keys_payload, dict) else []
        if not api_keys:
            api_keys = [{"description": self.api_key_description, "apiKey": api_key}]

        return {
            "email": email,
            "password": password,
            "user_id": auth_state["user_id"] or str(user_session.get("userId") or ""),
            "session_id": auth_state["session_id"] or session_id,
            "access_token": auth_state["access_token"],
            "refresh_token": auth_state["refresh_token"],
            "refresh_token_source": auth_state["refresh_token_source"],
            "session_token": auth_state["session_token"],
            "client_id": auth_state["client_id"],
            "client_cookie": auth_state["client_cookie"],
            "session_cookie": auth_state["session_cookie"],
            "api_key": api_key,
            "api_key_description": self.api_key_description,
            "venice_token": str(user_session.get("token") or ""),
            "credits": credits,
            "profile": _trim_profile(user_session),
            "api_usage": _trim_api_usage(api_usage),
            "api_keys": api_keys,
            "seedance_bonus_verified": True,
            "seedance_landing_url": SEEDANCE_LANDING_URL,
            "seedance_generate_url": SEEDANCE_GENERATE_URL,
            "checked_at": _utcnow_iso(),
        }
