"""AIHubMix 平台插件。

注册链路：
- 邮箱 + 密码走 Clerk Frontend API（clerk.aihubmix.com/v1）协议注册，邮箱 OTP 校验，
  Turnstile captcha 重试。
- Google OAuth 走 Clerk-mediated oauth_google strategy：浏览器点 Continue with Google
  → Google 登录（drive_google_oauth）→ Clerk 回调落地 console.aihubmix.com。
- 拿 key：协议尝试 console.aihubmix.com 的 Next.js Server Action（动态提取
  createApiKey action ID），未命中则浏览器 DOM 兜底点 Create Key 读 sk-。
- IMAP 收码必须扫 Junk（Clerk 邮件可能落 Junk），outlook_token provider 默认即扫
  INBOX/Junk/Junk Email，故 default_mail_provider=outlook_token。

支持三种执行器：
- protocol：纯 HTTP 协议（Clerk API + Server Action 拿 key）
- headless/headed/cdp_protocol：浏览器驱动（Clerk 表单 / Google OAuth）+ 协议拿 key
"""

from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    OtpSpec,
    ProtocolMailboxAdapter,
    ProtocolOAuthAdapter,
    RegistrationCapability,
    RegistrationResult,
)
from core.registration.helpers import resolve_timeout
from core.registry import register

SITE_URL = "https://aihubmix.com/"
CONSOLE_URL = "https://console.aihubmix.com"


@register
class AIHubMixPlatform(BasePlatform):
    name = "aihubmix"
    display_name = "AIHubMix"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed", "cdp_protocol"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google"]
    # Clerk 邮件可能落 Junk，outlook_token 扫 INBOX/Junk/Junk Email（与 embercloud 一致）。
    default_mail_provider = "outlook_token"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # aihubmix Clerk environment 显示密码 max length 0（无限制）、无复杂度要求，
        # 但仍用强密码避免被其它策略拒绝。
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
                "site_url": str(result.get("site_url") or SITE_URL),
                "dashboard_url": str(result.get("dashboard_url") or CONSOLE_URL),
                "api_base": str(result.get("api_base") or "https://aihubmix.com/v1"),
                "native_api_base": str(result.get("api_base") or "https://aihubmix.com/v1"),
                "auth_header": "Authorization",
                "auth_scheme": "Bearer sk-...",
                "checked_at": str(result.get("checked_at") or ""),
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = (self.config.extra if self.config else {}) or {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.aihubmix.protocol_register",
                fromlist=["AIHubMixProtocolRegister"],
            ).AIHubMixProtocolRegister(
                proxy=ctx.proxy,
                api_key_name=str(
                    ctx.extra.get("aihubmix_api_key_name")
                    or ctx.extra.get("api_key_name")
                    or "auto-register"
                ),
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
                captcha_solver=artifacts.captcha_solver,
            ),
            otp_spec=OtpSpec(
                # keyword 留空：Clerk 验证邮件主题/正文可能不含 "AIHubMix" 字样，
                # 用空 keyword 匹配所有邮件，靠 6 位数字 pattern 提取验证码。
                keyword="",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for AIHubMix email verification code (scans INBOX + Junk)...",
                success_label="AIHubMix OTP",
            ),
            # Clerk smart captcha 需打码：use_captcha=True 让 ProtocolMailboxFlow 注入 captcha_solver。
            use_captcha=True,
        )

    def _resolve_google_password(self, ctx) -> str:
        """从 Google 账号池复用账号取密码，回退到任务显式配置。"""
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        credentials = provider_account.get("credentials") if isinstance(provider_account, dict) else None
        if isinstance(credentials, dict) and credentials.get("password"):
            return str(credentials.get("password") or "").strip()
        return str(ctx.password or ctx.extra.get("google_password") or ctx.extra.get("oauth_password") or "").strip()

    def _resolve_totp_secret(self, ctx) -> str:
        """解析 2FA TOTP secret：优先 Google 账号池复用账号的 totp_secret，回退到任务显式配置。"""
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        pool_totp = mailbox_extra.get("google_pool_totp_secret") or mailbox_extra.get("totp_secret")
        if pool_totp:
            return str(pool_totp).strip()
        return str(ctx.extra.get("totp_secret") or ctx.extra.get("google_totp_secret") or "").strip()

    def _run_oauth(self, ctx) -> dict:
        from platforms.aihubmix.browser_oauth import register_with_browser_oauth

        extra = dict(ctx.extra or {})
        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google") or "google",
            email_hint=getattr(ctx.identity, "email", "") or extra.get("oauth_email_hint", ""),
            google_password=self._resolve_google_password(ctx),
            totp_secret=self._resolve_totp_secret(ctx),
            timeout=resolve_timeout(extra, ("aihubmix_oauth_timeout", "browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", "") or str(extra.get("aihubmix_chrome_user_data_dir") or ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", "") or str(extra.get("aihubmix_cdp_url") or ""),
            use_camoufox=str(extra.get("aihubmix_oauth_use_camoufox", "true")).strip().lower() in {"1", "true", "yes", "on"},
            cancel_token=getattr(ctx, "cancel_token", None),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_oauth,
            result_mapper=lambda ctx, result: self._map_result(result, password=ctx.password or ""),
        )

    def build_browser_registration_adapter(self):
        extra = (self.config.extra if self.config else {}) or {}
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_result(result, password=ctx.password or ""),
            oauth_runner=self._run_oauth,
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed", "headless", "cdp_protocol")),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.aihubmix.browser_register",
                fromlist=["AIHubMixBrowserRegistrar"],
            ).AIHubMixBrowserRegistrar(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                api_key_name=str(
                    ctx.extra.get("aihubmix_api_key_name")
                    or ctx.extra.get("api_key_name")
                    or "auto-register"
                ),
                timeout=resolve_timeout(
                    ctx.extra,
                    ("aihubmix_browser_timeout", "browser_oauth_timeout", "manual_oauth_timeout"),
                    240,
                ),
                chrome_path=str(extra.get("aihubmix_chrome_path", "") or ""),
                cdp_url=str(extra.get("aihubmix_cdp_url", "") or ""),
                headless=str(extra.get("aihubmix_headless", "false") or "false").strip().lower() in {"1", "true", "yes"},
                log_fn=ctx.log,
                cancel_token=getattr(ctx, "cancel_token", None),
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                # keyword 留空：Clerk 验证邮件主题/正文可能不含 "AIHubMix" 字样，
                # 用空 keyword 匹配所有邮件，靠 6 位数字 pattern 提取验证码。
                keyword="",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for AIHubMix email verification code (scans INBOX + Junk)...",
                success_label="AIHubMix OTP",
            ),
            # Clerk Turnstile 在浏览器里由 Clerk 组件自动处理，不需要 captcha_solver。
            use_captcha_for_mailbox=False,
        )

    def _should_use_browser_registration_flow(self, identity) -> bool:
        # oauth_browser（Google OAuth 登录）在 headless/headed/cdp_protocol 下走浏览器 OAuth adapter；
        # 邮箱注册：protocol 走 ProtocolMailboxFlow（Clerk API），headless/headed 走浏览器流程。
        if getattr(identity, "identity_provider", "") == "oauth_browser":
            return (self.config.executor_type or "") in ("headless", "headed", "cdp_protocol")
        return (self.config.executor_type or "") in ("headless", "headed")

    def check_valid(self, account: Account) -> bool:
        from platforms.aihubmix.core import AIHubMixClient

        client = AIHubMixClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        api_key = str((account.extra or {}).get("api_key") or account.token or "")
        if not api_key:
            return False
        try:
            return client.verify_api_key(api_key)
        except Exception:
            return False

    def get_quota(self, account: Account) -> dict:
        # AIHubMix 注册不送 credits（充值型），但有 free 旗舰模型可白嫖。
        # 额度查询需 dashboard 会话，协议无对应端点。
        return {}
