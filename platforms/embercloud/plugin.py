"""EmberCloud 平台插件。

注册链路（协议优先）：
- 邮箱 + 密码走 Clerk Frontend API（clerk.embercloud.ai/v1）协议注册，邮箱 OTP 校验，
  Turnstile captcha 重试。
- 拿 key：协议尝试 dashboard 后端候选路由，未命中则无头浏览器复用 Clerk 会话在
  /dashboard/keys 点 Create Key 抓取 ek_live_ 明文。
- IMAP 收码必须扫 Junk（验证码落在 Junk 文件夹），outlook_token provider 默认即扫
  INBOX/Junk/Junk Email，故 default_mail_provider=outlook_token。
"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register


@register
class EmberCloudPlatform(BasePlatform):
    name = "embercloud"
    display_name = "EmberCloud"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed", "cdp_protocol"]
    supported_identity_modes = ["mailbox"]
    # Clerk 注册带 Turnstile；临时邮箱域不封但 YYDS Mail 收不到 EmberCloud 邮件，
    # 默认复用 Outlook Token IMAP（已扫 INBOX/Junk/Junk Email）。
    default_mail_provider = "outlook_token"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password(length=18)

    def _map_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        api_key = str(result.get("api_key") or "").strip()
        return RegistrationResult(
            email=str(result.get("email") or "").strip(),
            password=password or str(result.get("password") or ""),
            user_id=str(result.get("user_id") or ""),
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.PENDING,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_name": str(result.get("api_key_name") or ""),
                "api_key_source": str(result.get("api_key_source") or ""),
                "key_create_result": dict(result.get("key_create_result") or {}),
                "api_verification": dict(result.get("api_verification") or {}),
                "models": dict(result.get("models") or {}),
                "access_token": str(result.get("access_token") or ""),
                "refresh_token": str(result.get("refresh_token") or ""),
                "refresh_token_source": str(result.get("refresh_token_source") or ""),
                "session_token": str(result.get("session_token") or ""),
                "client_id": str(result.get("client_id") or ""),
                "client_cookie": str(result.get("client_cookie") or ""),
                "session_cookie": str(result.get("session_cookie") or ""),
                "session_id": str(result.get("session_id") or ""),
                "site_url": str(result.get("site_url") or "https://www.embercloud.ai/"),
                "dashboard_url": str(result.get("dashboard_url") or "https://www.embercloud.ai/dashboard"),
                "api_base": str(result.get("api_base") or "https://api.embercloud.ai"),
                "native_api_base": str(result.get("native_api_base") or "https://api.embercloud.ai"),
                "auth_header": "Authorization",
                "auth_scheme": "Bearer ek_live_...",
                "checked_at": str(result.get("checked_at") or ""),
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = (self.config.extra if self.config else {}) or {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.embercloud.browser_register",
                fromlist=["EmberCloudBrowserRegistrar"],
            ).EmberCloudBrowserRegistrar(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                api_key_name=str(
                    ctx.extra.get("embercloud_api_key_name")
                    or ctx.extra.get("api_key_name")
                    or "auto-register"
                ),
                timeout=resolve_timeout(
                    ctx.extra,
                    ("embercloud_browser_timeout", "browser_oauth_timeout", "manual_oauth_timeout"),
                    240,
                ),
                chrome_path=str(extra.get("embercloud_chrome_path", "") or ""),
                cdp_url=str(extra.get("embercloud_cdp_url", "") or ""),
                headless=str(extra.get("embercloud_headless", "false") or "false").strip().lower() in {"1", "true", "yes"},
                log_fn=ctx.log,
                cancel_token=getattr(ctx, "cancel_token", None),
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                keyword="Ember",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for EmberCloud email verification code (scans INBOX + Junk)...",
                success_label="EmberCloud OTP",
            ),
            # Turnstile 在浏览器里由 Clerk 组件自动处理，不需要 captcha_solver。
            use_captcha=False,
        )

    def check_valid(self, account: Account) -> bool:
        from platforms.embercloud.core import EmberCloudClient

        client = EmberCloudClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        api_key = str((account.extra or {}).get("api_key") or account.token or "")
        if not api_key:
            return False
        try:
            return client.verify_api_key(api_key)
        except Exception:
            return False

    def get_quota(self, account: Account) -> dict:
        # EmberCloud 控制台显示 $1.00 免费额度；额度查询需 dashboard 会话，协议无对应端点。
        return {}
