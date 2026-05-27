"""FreeModel 平台插件。"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register

SITE_URL = "https://freemodel.dev/"
API_BASE = "https://api.freemodel.dev"


@register
class FreeModelPlatform(BasePlatform):
    name = "freemodel"
    display_name = "FreeModel"
    version = "1.0.0"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers = []
    default_mail_provider = "outlook_token"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _resolve_google_password(self, ctx) -> str:
        """解析账号池复用模式下的 Google 密码。"""
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        credentials = provider_account.get("credentials") if isinstance(provider_account, dict) else None
        if isinstance(credentials, dict) and credentials.get("password"):
            return str(credentials.get("password") or "").strip()
        return str(ctx.password or ctx.extra.get("google_password") or "").strip()

    def _resolve_invite_code(self, ctx) -> str:
        return str(
            ctx.extra.get("freemodel_invite_code")
            or ctx.extra.get("invite_code")
            or ""
        ).strip()

    def _run_oauth(self, ctx) -> dict:
        from platforms.freemodel.browser_oauth import register_with_browser_oauth

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
            invite_code=self._resolve_invite_code(ctx),
            phone_provider=getattr(ctx.platform, "phone_provider", None),
            phone_timeout=resolve_timeout(ctx.extra, ("phone_otp_timeout", "haozhu_phone_timeout", "qianchuan_phone_timeout"), 180),
            phone_poll_interval=resolve_timeout(ctx.extra, ("phone_poll_interval", "haozhu_poll_interval", "qianchuan_poll_interval"), 15),
            phone_code_pattern=str(ctx.extra.get("phone_code_pattern") or "").strip() or None,
            phone_send_attempts=resolve_timeout(ctx.extra, ("freemodel_phone_send_attempts", "phone_send_attempts"), 3),
            key_name=str(ctx.extra.get("freemodel_key_name") or "default").strip() or "default",
        )

    def _map_result(self, result: dict) -> RegistrationResult:
        key_payload = result.get("key_create_result") or result.get("api_key_info") or {}
        referral_payload = dict(result.get("referral") or {})
        phone = str(result.get("phone") or "").strip()
        verified_at = str(result.get("verified_at") or result.get("verifiedAt") or "").strip()
        api_key = str(
            result.get("api_key")
            or result.get("secret")
            or result.get("key_secret")
            or ""
        ).strip()
        api_key_id = str(result.get("api_key_id") or result.get("key_id") or "").strip()
        api_key_name = str(result.get("api_key_name") or result.get("key_name") or "").strip()
        referral_code = str(
            result.get("referral_code")
            or referral_payload.get("code")
            or ""
        ).strip()
        used_invite_code = str(result.get("used_invite_code") or "").strip()
        user = dict(result.get("user") or {})
        user_id = str(result.get("user_id") or user.get("id") or "").strip()
        verification_phone = dict(result.get("verification_phone") or {})
        if phone:
            verification_phone.setdefault("phone", phone)
        if verified_at:
            verification_phone.setdefault("verified_at", verified_at)
        account_overview = {
            "remote_email": str(result.get("email") or user.get("email") or "").strip(),
            "api_key_created": bool(api_key),
            "api_key_id": api_key_id,
            "verified_phone": bool(verified_at or phone),
            "referral_code": referral_code,
            "chips": [item for item in (str(result.get("auth_method") or "邮箱 OTP").strip(), "手机号已验证" if verified_at or phone else "", "API Key") if item],
        }
        return RegistrationResult(
            email=str(result.get("email") or user.get("email") or "").strip(),
            password="",
            user_id=user_id,
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.INVALID,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_id": api_key_id,
                "api_key_name": api_key_name,
                "api_key_info": dict(result.get("api_key_info") or {}) if isinstance(result.get("api_key_info"), dict) else key_payload,
                "key_create_result": key_payload,
                "referral": referral_payload,
                "referral_code": referral_code,
                "invite_code": referral_code,
                "used_invite_code": used_invite_code,
                "verified_at": verified_at,
                "verification_phone": verification_phone,
                "phone_send_result": dict(result.get("phone_send_result") or {}),
                "phone_verify_result": dict(result.get("phone_verify_result") or {}),
                "user": user,
                "cookies": dict(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "site_url": SITE_URL,
                "api_base": API_BASE,
                "account_overview": account_overview,
            },
        )

    def _run_mailbox(self, ctx, artifacts) -> dict:
        from platforms.freemodel.browser_oauth import register_with_email_otp

        if artifacts.otp_callback is None:
            raise RuntimeError("FreeModel 邮箱注册缺少 OTP 回调，请配置可收信的邮箱来源")
        return register_with_email_otp(
            email=ctx.identity.email or "",
            otp_callback=artifacts.otp_callback,
            proxy=ctx.proxy,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout", "registration.timeout"), 300),
            log_fn=ctx.log,
            invite_code=self._resolve_invite_code(ctx),
            phone_provider=getattr(ctx.platform, "phone_provider", None),
            phone_timeout=resolve_timeout(ctx.extra, ("phone_otp_timeout", "haozhu_phone_timeout", "qianchuan_phone_timeout"), 180),
            phone_poll_interval=resolve_timeout(ctx.extra, ("phone_poll_interval", "haozhu_poll_interval", "qianchuan_poll_interval"), 15),
            phone_code_pattern=str(ctx.extra.get("phone_code_pattern") or "").strip() or None,
            phone_send_attempts=resolve_timeout(ctx.extra, ("freemodel_phone_send_attempts", "phone_send_attempts"), 3),
            key_name=str(ctx.extra.get("freemodel_key_name") or "default").strip() or "default",
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            worker_builder=lambda ctx, artifacts: object(),
            register_runner=lambda _worker, ctx, artifacts: self._run_mailbox(ctx, artifacts),
            otp_spec=OtpSpec(
                keyword="",
                timeout=180,
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="等待 FreeModel 邮箱验证码...",
                success_label="FreeModel 验证码",
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
