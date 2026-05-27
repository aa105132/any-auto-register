"""Komilion 平台插件。"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, LinkSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register


@register
class KomilionPlatform(BasePlatform):
    name = "komilion"
    display_name = "Komilion"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google", "microsoft", "outlook"]
    default_mail_provider = "outlook_token"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _resolve_oauth_password(self, ctx) -> str:
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        credentials = provider_account.get("credentials") if isinstance(provider_account, dict) else None
        if isinstance(credentials, dict) and credentials.get("password"):
            return str(credentials.get("password") or "").strip()
        return str(
            ctx.password
            or ctx.extra.get("oauth_password")
            or ctx.extra.get("google_password")
            or ctx.extra.get("outlook_password")
            or ctx.extra.get("microsoft_password")
            or ""
        ).strip()

    def _run_oauth(self, ctx) -> dict:
        from platforms.komilion.browser_oauth import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google") or "google",
            email_hint=getattr(ctx.identity, "email", "") or ctx.extra.get("oauth_email_hint", ""),
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", ""),
            oauth_password=self._resolve_oauth_password(ctx),
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
                "api_key_info": dict(result.get("api_key_info") or {}),
                "api_verification": dict(result.get("api_verification") or {}),
                "key_create_result": dict(result.get("key_create_result") or {}),
                "session": dict(result.get("session") or {}),
                "cookies": dict(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "providers": dict(result.get("providers") or {}),
                "oauth_provider": str(result.get("oauth_provider") or ""),
                "site_url": "https://www.komilion.com/",
                "dashboard_url": "https://www.komilion.com/dashboard/api-keys",
                "api_base": "https://www.komilion.com/api/v1",
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
            },
        )

    def _run_mailbox(self, ctx, artifacts) -> dict:
        from platforms.komilion.browser_oauth import register_with_email_verification

        if artifacts.verification_link_callback is None:
            raise RuntimeError("Komilion 邮箱注册缺少验证链接回调，请配置 Outlook/邮箱来源")
        return register_with_email_verification(
            email=ctx.identity.email or "",
            password=ctx.password or "",
            verification_link_callback=artifacts.verification_link_callback,
            proxy=ctx.proxy,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout", "registration.timeout"), 300),
            log_fn=ctx.log,
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: object(),
            register_runner=lambda _worker, ctx, artifacts: self._run_mailbox(ctx, artifacts),
            link_spec=LinkSpec(
                keyword="Komilion",
                timeout=180,
                wait_message="等待 Komilion 验证链接邮件...",
                success_label="Komilion 验证链接",
                preview_chars=100,
            ),
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
        return bool(account.token or (account.extra or {}).get("api_key"))
