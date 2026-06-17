"""AI-ROUTER 平台插件。"""
from __future__ import annotations

import random
import string
from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register


@register
class AiRouterPlatform(BasePlatform):
    name = "airouter"
    display_name = "AI-ROUTER"
    version = "1.0.0"
    supported_executors = ["protocol", "cdp_protocol"]
    supported_identity_modes = ["mailbox"]

    def _resolve_captcha_solver(self) -> str:
        requested = str((self.config.captcha_solver if self.config else "") or "").strip().lower()
        if (self.config and self.config.executor_type == "cdp_protocol") and (not requested or requested == "auto"):
            return "cdp_turnstile"
        return super()._resolve_captcha_solver()

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _strong_password(self) -> str:
        required = [
            random.choice(string.ascii_lowercase),
            random.choice(string.ascii_uppercase),
            random.choice(string.digits),
            random.choice("!@#$%^&*_-+=?"),
        ]
        pool = string.ascii_letters + string.digits + "!@#$%^&*_-+=?"
        chars = required + [random.choice(pool) for _ in range(12)]
        random.shuffle(chars)
        return "".join(chars)

    def _prepare_registration_password(self, password: str | None) -> str | None:
        raw = str(password or "")
        if len(raw) >= 6:
            return raw
        return self._strong_password()

    def _map_result(self, result: dict[str, Any]) -> RegistrationResult:
        api_key = str(result.get("api_key") or "").strip()
        return RegistrationResult(
            email=str(result.get("email") or "").strip(),
            password=str(result.get("password") or ""),
            user_id=str((result.get("user") or {}).get("id") or (result.get("user") or {}).get("user_id") or ""),
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.PENDING,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "access_token": str(result.get("access_token") or ""),
                "refresh_token": str(result.get("refresh_token") or ""),
                "expires_in": result.get("expires_in", 0),
                "token_type": str(result.get("token_type") or ""),
                "user": dict(result.get("user") or {}),
                "me": dict(result.get("me") or {}),
                "balance": result.get("balance"),
                "min_success_balance": result.get("min_success_balance"),
                "api_key_info": dict(result.get("api_key_info") or {}),
                "api_verification": dict(result.get("api_verification") or {}),
                "key_create_result": dict(result.get("key_create_result") or {}),
                "affiliate_fingerprint": str(result.get("affiliate_fingerprint") or ""),
                "browser_fingerprint": dict(result.get("browser_fingerprint") or {}),
                "group_id": result.get("group_id"),
                "group_info": dict(result.get("group_info") or {}),
                "site_url": str(result.get("site_url") or "https://ai-router.dev/"),
                "register_url": str(result.get("register_url") or "https://ai-router.dev/register"),
                "dashboard_url": str(result.get("dashboard_url") or "https://ai-router.dev/dashboard"),
                "api_base": str(result.get("api_base") or "https://api.ai-router.dev/api/v1"),
                "native_api_base": str(result.get("native_api_base") or "https://api.ai-router.dev/api/v1"),
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = (self.config.extra if self.config else {}) or {}
        captcha_solver = str(extra.get("airouter_captcha_solver") or "").strip().lower() or self._resolve_captcha_solver()
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.airouter.protocol_mailbox",
                fromlist=["AiRouterMailboxRegistrar"],
            ).AiRouterMailboxRegistrar(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                timeout=resolve_timeout(ctx.extra, ("airouter_timeout", "mail_otp_timeout"), 240),
                chrome_path=str(extra.get("airouter_chrome_path", "") or ""),
                cdp_url=str(extra.get("airouter_cdp_url", "") or ""),
                log_fn=ctx.log,
                promo_code=str(extra.get("airouter_promo_code") or extra.get("promo_code") or ""),
                invitation_code=str(extra.get("airouter_invitation_code") or extra.get("invitation_code") or ""),
                aff_code=str(extra.get("airouter_aff_code") or extra.get("aff_code") or ""),
                api_key_name=str(extra.get("airouter_api_key_name") or extra.get("api_key_name") or "auto-register"),
                group_id=extra.get("airouter_group_id") or extra.get("group_id") or None,
                min_success_balance=float(extra.get("airouter_min_success_balance") or extra.get("min_success_balance") or 20.0),
                webrtc_client_ip=str(extra.get("airouter_webrtc_client_ip") or extra.get("webrtc_client_ip") or ""),
                captcha_solver=captcha_solver,
                yescaptcha_key=str(extra.get("yescaptcha_key") or ""),
                yescaptcha_api_url=str(extra.get("yescaptcha_api_url") or "https://api.yescaptcha.com"),
                allow_external_cdp=str(extra.get("airouter_allow_external_cdp") or "").strip().lower() in {"1", "true", "yes", "on"},
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or ctx.platform._make_random_password(),
            ),
            otp_spec=OtpSpec(
                keyword="AI-ROUTER",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for AI-ROUTER email verification code...",
                success_label="AI-ROUTER OTP",
            ),
            use_captcha=False,
        )

    def check_valid(self, account: Account) -> bool:
        return bool(account.token or (account.extra or {}).get("api_key"))
