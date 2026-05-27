"""BlendSpace 平台插件。"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


@register
class BlendSpacePlatform(BasePlatform):
    name = "blendspace"
    display_name = "BlendSpace"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["oauth_browser"]
    supported_oauth_providers = ["google"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _map_blendspace_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        session_id = str(result.get("session_id", "") or "").strip()
        return RegistrationResult(
            email=result["email"],
            password=password or result.get("password", ""),
            token=session_id,
            status=AccountStatus.REGISTERED,
            extra={
                "session_id": session_id,
                "sessionId": session_id,
                "wasp_session_id": session_id,
                "final_url": result.get("final_url", ""),
            },
        )

    def _resolve_google_password(self, ctx) -> str:
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        credentials = provider_account.get("credentials") if isinstance(provider_account, dict) else None
        if isinstance(credentials, dict):
            password = str(credentials.get("password") or "").strip()
            if password:
                return password
        return str(ctx.password or ctx.extra.get("google_password") or "").strip()

    def _run_protocol_oauth(self, ctx) -> dict:
        from platforms.blendspace.browser_oauth import register_with_browser_oauth

        provider = ctx.identity.oauth_provider or "google"
        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=provider,
            email_hint=ctx.identity.email,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
            google_password=self._resolve_google_password(ctx),
            reuse_existing_cdp=_truthy(ctx.extra.get("oauth_reuse_existing_cdp") or ctx.extra.get("reuse_existing_cdp")),
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_blendspace_result(result),
            browser_worker_builder=lambda ctx, artifacts: None,
            browser_register_runner=lambda worker, ctx, artifacts: {},
            oauth_runner=self._run_protocol_oauth,
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed",)),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_protocol_oauth,
            result_mapper=lambda ctx, result: self._map_blendspace_result(result),
        )

    def check_valid(self, account: Account) -> bool:
        extra = account.extra or {}
        return bool(extra.get("session_id") or extra.get("sessionId") or account.token)
