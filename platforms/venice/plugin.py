"""Venice platform plugin."""

from __future__ import annotations

import random
import string
from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    OtpSpec,
    ProtocolMailboxAdapter,
    RegistrationResult,
)
from core.registry import register
from platforms.venice.core import VeniceClient


@register
class VenicePlatform(BasePlatform):
    name = "venice"
    display_name = "Venice"
    version = "1.0.0"
    supported_executors = ["headless", "headed", "protocol", "cdp_protocol"]
    supported_identity_modes = ["mailbox"]
    protocol_captcha_order = ("yescaptcha", "2captcha", "patchright_harvester")

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return "".join(random.choices(string.ascii_letters + string.digits + "!@#$", k=18))

    @staticmethod
    def _normalize_plan_state(user_type: str) -> str:
        raw = str(user_type or "").strip().upper()
        if not raw or raw == "FREE":
            return "free"
        if raw in {"PLUS", "PRO", "TEAM", "BUSINESS", "ENTERPRISE"}:
            return "subscribed"
        return raw.lower()

    def _build_account_overview(self, result: dict[str, Any]) -> dict[str, Any]:
        profile = dict(result.get("profile") or {})
        api_keys = list(result.get("api_keys") or [])
        credits = int(result.get("credits") or profile.get("venice_credits") or 0)
        user_type = str(profile.get("user_type") or "FREE")
        chips = [
            f"{credits} Credits" if credits else "",
            f"API Keys {len(api_keys)}" if api_keys else "",
            "Seedance 500 Credits" if result.get("seedance_bonus_verified") else "",
        ]
        return {
            "remote_email": profile.get("email") or result.get("email", ""),
            "plan_name": user_type,
            "plan_state": self._normalize_plan_state(user_type),
            "credits": credits,
            "promo_source": "seedance",
            "seedance_source_verified": True,
            "seedance_bonus_verified": bool(result.get("seedance_bonus_verified")),
            "api_key_count": len(api_keys),
            "api_key_last6": str(api_keys[0].get("last6Chars") or "") if api_keys else "",
            "checked_at": result.get("checked_at", ""),
            "chips": [chip for chip in chips if chip],
        }

    def _map_venice_result(self, result: dict[str, Any], *, password: str = "") -> RegistrationResult:
        profile = dict(result.get("profile") or {})
        overview = self._build_account_overview(result)
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=result.get("user_id") or profile.get("user_id") or "",
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "access_token": result.get("access_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "session_token": result.get("session_token", ""),
                "client_id": result.get("client_id", ""),
                "api_key": result.get("api_key", ""),
                "api_key_description": result.get("api_key_description", ""),
                "refresh_token_source": result.get("refresh_token_source", ""),
                "venice_token": result.get("venice_token", ""),
                "profile": profile,
                "api_keys": list(result.get("api_keys") or []),
                "api_usage": dict(result.get("api_usage") or {}),
                "account_overview": overview,
                "promo_source": "seedance",
                "seedance_landing_url": result.get("seedance_landing_url", ""),
                "seedance_generate_url": result.get("seedance_generate_url", ""),
                "clerk_client_cookie": result.get("client_cookie", ""),
                "clerk_session_cookie": result.get("session_cookie", ""),
                "session_id": result.get("session_id", ""),
            },
        )

    def _captcha_preflight(self, ctx) -> None:
        if ctx.identity.identity_provider == "mailbox" and ctx.platform._resolve_captcha_solver() == "local_solver":
            from services.solver_manager import start

            start()

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_venice_result(result, password=ctx.password or ""),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.venice.browser_register",
                fromlist=["VeniceBrowserRegister"],
            ).VeniceBrowserRegister(
                captcha=artifacts.captcha_solver,
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                api_key_description=str(ctx.extra.get("venice_api_key_description", "seedance-auto") or "seedance-auto"),
                expected_credits=int(ctx.extra.get("venice_expected_credits", 500) or 500),
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                keyword="Venice",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for Venice OTP...",
                success_label="Venice OTP",
            ),
            use_captcha_for_mailbox=True,
            preflight=self._captcha_preflight,
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_venice_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.venice.protocol_mailbox",
                fromlist=["VeniceProtocolMailboxWorker"],
            ).VeniceProtocolMailboxWorker(
                proxy=ctx.proxy,
                api_key_description=str(ctx.extra.get("venice_api_key_description", "seedance-auto") or "seedance-auto"),
                expected_credits=int(ctx.extra.get("venice_expected_credits", 500) or 500),
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
                captcha_solver=artifacts.captcha_solver,
            ),
            otp_spec=OtpSpec(
                keyword="Venice",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for Venice OTP...",
                success_label="Venice OTP",
            ),
            use_captcha=True,
            preflight=self._captcha_preflight,
        )

    def get_platform_actions(self) -> list:
        return [
            {
                "id": "get_account_state",
                "label": "Query Venice account state",
                "params": [],
            }
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "get_account_state":
            raise NotImplementedError(f"Venice action not supported: {action_id}")

        access_token = str((account.extra or {}).get("access_token") or account.token or "")
        if not access_token:
            return {"ok": False, "error": "Account missing access_token; cannot query Venice state"}

        try:
            client = VeniceClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
            session_payload = client.get_user_session(access_token)
            usage_payload = client.get_api_usage(access_token)
            api_keys_payload = client.list_api_keys(access_token)
            api_keys = list(api_keys_payload.get("data") or []) if isinstance(api_keys_payload, dict) else []

            credits = int(session_payload.get("veniceCredits") or 0)
            user_type = str(session_payload.get("userType") or "FREE")
            overview = {
                "remote_email": session_payload.get("email") or account.email,
                "plan_name": user_type,
                "plan_state": self._normalize_plan_state(user_type),
                "credits": credits,
                "promo_source": "seedance",
                "api_key_count": len(api_keys),
                "api_key_last6": str(api_keys[0].get("last6Chars") or "") if api_keys else "",
                "chips": [chip for chip in [f"{credits} Credits", f"API Keys {len(api_keys)}"] if chip],
            }
            return {
                "ok": True,
                "data": {
                    "valid": True,
                    "remote_user": {
                        "email": session_payload.get("email"),
                        "user_id": session_payload.get("userId"),
                        "username": session_payload.get("userName"),
                        "user_type": user_type,
                        "user_country": session_payload.get("userCountry"),
                        "credits": credits,
                    },
                    "usage_summary": {
                        "lookback": usage_payload.get("lookback"),
                        "by_key": usage_payload.get("byKey") or [],
                        "top_key_names": usage_payload.get("topKeyNames") or [],
                    },
                    "api_keys": api_keys,
                    "account_overview": overview,
                },
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def check_valid(self, account: Account) -> bool:
        client = VeniceClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        api_key = str((account.extra or {}).get("api_key") or "")
        if api_key:
            try:
                return client.verify_api_key(api_key)
            except Exception:
                pass

        access_token = str((account.extra or {}).get("access_token") or account.token or "")
        if not access_token:
            return False
        try:
            payload = client.get_user_session(access_token)
        except Exception:
            return False
        return bool(payload.get("email"))
