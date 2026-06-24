"""AnyCap 平台插件。"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register


@register
class AnyCapPlatform(BasePlatform):
    name = "anycap"
    display_name = "AnyCap"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed", "cdp_protocol"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _should_use_browser_registration_flow(self, identity) -> bool:
        return getattr(identity, "identity_provider", "") == "oauth_browser" and (self.config.executor_type or "") in ("headless", "headed", "cdp_protocol")

    def _resolve_google_password(self, ctx) -> str:
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        credentials = provider_account.get("credentials") if isinstance(provider_account, dict) else None
        if isinstance(credentials, dict) and credentials.get("password"):
            return str(credentials.get("password") or "").strip()
        return str(ctx.password or ctx.extra.get("google_password") or "").strip()

    def _run_oauth(self, ctx) -> dict:
        from platforms.anycap.browser_oauth import register_with_browser_oauth

        extra = dict(ctx.extra or {})
        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google") or "google",
            email_hint=getattr(ctx.identity, "email", "") or extra.get("oauth_email_hint", ""),
            google_password=self._resolve_google_password(ctx),
            timeout=resolve_timeout(extra, ("anycap_oauth_timeout", "browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", "") or str(extra.get("anycap_chrome_user_data_dir") or ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", "") or str(extra.get("anycap_cdp_url") or ""),
            api_key_name=str(extra.get("anycap_api_key_name") or extra.get("api_key_name") or "auto-register"),
            # AnyCap Auth0 Google OAuth client 对 Playwright Chromium 触发 signin/rejected，
            # 默认走 Camoufox（反检测 Firefox）；可经 extra 显式关闭。
            use_camoufox=str(extra.get("anycap_oauth_use_camoufox", "true")).strip().lower() in {"1", "true", "yes", "on"},
            cancel_token=getattr(ctx, "cancel_token", None),
        )

    def _map_result(self, result: dict) -> RegistrationResult:
        api_key = str(result.get("api_key") or "").strip()
        return RegistrationResult(
            email=str(result.get("email") or "").strip(),
            password="",
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.PENDING,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "access_token": str(result.get("access_token") or ""),
                "api_key_info": dict(result.get("api_key_info") or {}),
                "api_verification": dict(result.get("api_verification") or {}),
                "key_create_result": dict(result.get("key_create_result") or {}),
                "profile": dict(result.get("profile") or {}),
                "cookies": dict(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "site_url": "https://anycap.ai/",
                "dashboard_url": "https://anycap.ai/dashboard",
                "api_base": "https://api.anycap.ai",
                "native_api_base": "https://api.anycap.ai",
                "credit_amount": 100.0,
            },
        )


    def build_protocol_mailbox_adapter(self):
        extra = (self.config.extra if self.config else {}) or {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.anycap.browser_oauth",
                fromlist=["AnyCapMailboxRegistrar"],
            ).AnyCapMailboxRegistrar(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                timeout=resolve_timeout(ctx.extra, ("anycap_oauth_timeout", "browser_oauth_timeout", "mail_otp_timeout"), 240),
                chrome_path=str(extra.get("anycap_chrome_path", "") or ""),
                cdp_url=str(extra.get("anycap_cdp_url", "") or ""),
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or ctx.platform._make_random_password(),
            ),
            otp_spec=OtpSpec(
                keyword="Auth0",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for AnyCap/Auth0 email verification code...",
                success_label="AnyCap Auth0 OTP",
            ),
            use_captcha=False,
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(oauth_runner=self._run_oauth, result_mapper=lambda ctx, result: self._map_result(result))

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            oauth_runner=self._run_oauth,
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed", "headless", "cdp_protocol")),
        )

    def check_valid(self, account: Account) -> bool:
        return bool(account.token or (account.extra or {}).get("api_key"))
