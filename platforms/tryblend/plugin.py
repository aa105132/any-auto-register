"""TryBlend 平台插件。"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register


@register
class TryBlendPlatform(BasePlatform):
    name = "tryblend"
    display_name = "TryBlend"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["oauth_browser"]
    supported_oauth_providers = ["google"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _run_oauth(self, ctx) -> dict:
        from platforms.tryblend.browser_oauth import register_with_browser_oauth
        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google") or "google",
            email_hint=getattr(ctx.identity, "email", "") or ctx.extra.get("oauth_email_hint", ""),
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", ""),
            google_password=str(ctx.password or ctx.extra.get("google_password") or ""),
        )

    def _map_result(self, result: dict) -> RegistrationResult:
        token = str(result.get("access_token") or "").strip()
        return RegistrationResult(
            email=str(result.get("email") or "").strip(),
            password="",
            token=token,
            status=AccountStatus.REGISTERED if token else AccountStatus.PENDING,
            extra={
                "access_token": token,
                "refresh_token": str(result.get("refresh_token") or ""),
                "expires_at": result.get("expires_at"),
                "expires_in": result.get("expires_in"),
                "token_type": str(result.get("token_type") or "bearer"),
                "supabase_session": dict(result.get("supabase_session") or {}),
                "cookies": dict(result.get("cookies") or {}),
                "api_base": "https://www.tryblend.ai",
                "site_url": "https://www.tryblend.ai/",
            },
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(oauth_runner=self._run_oauth, result_mapper=lambda ctx, result: self._map_result(result))

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            oauth_runner=self._run_oauth,
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed",)),
        )

    def check_valid(self, account: Account) -> bool:
        return bool(account.token or (account.extra or {}).get("access_token"))
