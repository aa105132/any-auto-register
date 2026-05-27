"""CometAPI 平台插件。"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register

SITE_URL = "https://www.cometapi.com"
CONSOLE_URL = "https://www.cometapi.com/console"
API_BASE = "https://api.cometapi.com/v1"


class _OtpCapture:
    """记录邮箱 OTP 回调返回值，便于协议适配层复用/测试。"""

    def __init__(self):
        self.value = ""

    def set(self, value) -> None:
        self.value = str(value or "").strip()

    def __bool__(self) -> bool:
        return bool(self.value)

    def __str__(self) -> str:
        return self.value

    def __eq__(self, other) -> bool:
        return self.value == str(other or "").strip()


@register
class CometAPIPlatform(BasePlatform):
    name = "cometapi"
    display_name = "CometAPI"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google"]
    default_mail_provider = "outlook_token"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _resolve_google_password(self, ctx) -> str:
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        credentials = provider_account.get("credentials") if isinstance(provider_account, dict) else None
        if isinstance(credentials, dict) and credentials.get("password"):
            return str(credentials.get("password") or "").strip()
        return str(
            getattr(ctx, "password", "")
            or ctx.extra.get("google_password")
            or ctx.extra.get("oauth_password")
            or ""
        ).strip()

    def _key_name(self, ctx) -> str:
        return str(
            ctx.extra.get("cometapi_key_name")
            or ctx.extra.get("api_key_name")
            or "default"
        ).strip() or "default"

    def _run_oauth(self, ctx) -> dict:
        from platforms.cometapi.browser_oauth import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google") or "google",
            email_hint=getattr(ctx.identity, "email", "") or ctx.extra.get("oauth_email_hint", ""),
            google_password=self._resolve_google_password(ctx),
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", ""),
            reuse_existing_cdp=bool(ctx.extra.get("reuse_existing_cdp") or ctx.extra.get("oauth_reuse_existing_cdp")),
            key_name=self._key_name(ctx),
            claim_rewards=bool(ctx.extra.get("cometapi_claim_rewards", True)),
        )

    def _run_mailbox(self, ctx, artifacts) -> dict:
        from platforms.cometapi.browser_oauth import register_with_email_otp

        if artifacts.otp_callback is None:
            raise RuntimeError("CometAPI 邮箱注册缺少 OTP 回调，请配置可收信的邮箱来源")
        otp_capture = _OtpCapture()

        def otp_callback():
            code = artifacts.otp_callback()
            otp_capture.set(code)
            return code

        return register_with_email_otp(
            email=ctx.identity.email or "",
            otp_callback=otp_callback,
            email_otp=otp_capture,
            proxy=ctx.proxy,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout", "registration.timeout"), 300),
            log_fn=ctx.log,
            key_name=self._key_name(ctx),
            invite_code=str(ctx.extra.get("cometapi_invite_code") or ctx.extra.get("invite_code") or "").strip(),
            claim_rewards=bool(ctx.extra.get("cometapi_claim_rewards", True)),
        )

    def _map_result(self, result: dict) -> RegistrationResult:
        user = dict(result.get("user") or {}) if isinstance(result.get("user"), dict) else {}
        api_key = str(result.get("api_key") or result.get("key") or result.get("token") or "").strip()
        user_id = str(result.get("user_id") or user.get("id") or "").strip()
        quota = user.get("quota")
        used_quota = user.get("used_quota")
        newbie_rewards = dict(result.get("newbie_rewards") or {}) if isinstance(result.get("newbie_rewards"), dict) else {}
        email = str(result.get("email") or user.get("email") or "").strip()
        account_overview = {
            "remote_email": email,
            "api_key_created": bool(api_key),
            "balance_quota": quota,
            "used_quota": used_quota,
            "newbie_rewards": newbie_rewards,
            "chips": [item for item in ("邮箱 OTP" if result.get("auth_method") == "email_otp" else str(result.get("auth_method") or "Google OAuth"), "API Key" if api_key else "", "奖励任务") if item],
        }
        return RegistrationResult(
            email=email,
            password=str(result.get("password") or ""),
            user_id=user_id,
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.INVALID,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_info": dict(result.get("api_key_info") or {}),
                "api_verification": dict(result.get("api_verification") or {}),
                "key_create_result": dict(result.get("key_create_result") or {}),
                "newbie_rewards": newbie_rewards,
                "user": user,
                "session": dict(result.get("session") or {}),
                "cookies": dict(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "auth_method": str(result.get("auth_method") or ""),
                "site_url": SITE_URL + "/",
                "dashboard_url": CONSOLE_URL + "/token",
                "api_base": API_BASE,
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            worker_builder=lambda ctx, artifacts: object(),
            register_runner=lambda _worker, ctx, artifacts: self._run_mailbox(ctx, artifacts),
            otp_spec=OtpSpec(
                keyword="CometAPI",
                timeout=180,
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="等待 CometAPI 邮箱验证码...",
                success_label="CometAPI 验证码",
            ),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(oauth_runner=self._run_oauth, result_mapper=lambda ctx, result: self._map_result(result))

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            oauth_runner=self._run_oauth,
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed", "headless")),
        )

    def check_valid(self, account: Account) -> bool:
        return bool(account.token or (account.extra or {}).get("api_key") or (account.extra or {}).get("ai_api_token"))
