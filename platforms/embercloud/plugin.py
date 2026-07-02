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
    # Turnstile 解码顺序：本地 solver 服务（camoufox 模式，实测能本地解 EmberCloud
    # Clerk managed Turnstile 拿 token）优先，yescaptcha 远程兜底，2captcha 最后。
    # 实测：playwright/patchright/camoufox launch 的浏览器 Cloudflare 直接不渲染 widget，
    # 自动化点击也过不了；唯有本地 solver 服务（完整资源加载 + 多策略点击 + widget 注入）
    # 或远程打码能拿到 token。local_solver 无需外部 API key，优先用。
    protocol_captcha_order = ("local_solver", "yescaptcha", "2captcha")

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password(length=18)

    def _captcha_preflight(self, ctx) -> None:
        """local_solver 模式需要本地 Turnstile solver 服务在跑，注册前自动拉起。

        solver 服务（services/turnstile_solver，camoufox 模式）实测能本地解 EmberCloud
        Clerk managed Turnstile 拿到 700+ 字符 token 并被 Clerk 接受。
        """
        if ctx.platform._resolve_captcha_solver() == "local_solver":
            from services.solver_manager import start

            start()

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
        # 纯协议注册：Clerk Frontend API sign_up + 本地 Turnstile solver（camoufox 模式）
        # 拿 token 注入 sign_up + 邮箱 OTP + 协议拿 key（dashboard credit 预热 + Server
        # Action 创建 ek_live_）。
        # 实测：浏览器内 Clerk managed Turnstile 在自动化 Chrome/Firefox 下 Cloudflare 直接
        # 不渲染 widget（playwright/patchright/camoufox launch 均被识破），点 checkbox 也
        # 过不了；唯有本地 solver 服务（完整资源加载 + 多策略点击 + widget 注入）或远程
        # 打码能拿到 token。local_solver 优先（无需外部 API key），preflight 自动拉起服务。
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.embercloud.protocol_mailbox",
                fromlist=["EmberCloudProtocolMailboxWorker"],
            ).EmberCloudProtocolMailboxWorker(
                proxy=ctx.proxy,
                api_key_name=str(
                    ctx.extra.get("embercloud_api_key_name")
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
                keyword="Ember",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for EmberCloud email verification code (scans INBOX + Junk)...",
                success_label="EmberCloud OTP",
            ),
            # Turnstile 由本地 solver 服务（camoufox）拿 token 注入 sign_up，必须启用 captcha_solver。
            use_captcha=True,
            preflight=self._captcha_preflight,
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
