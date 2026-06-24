"""MixRoute (mixroute.ai) 平台插件。

注册链路（new-api 架构，OpenAI 兼容推理网关）：
- 邮箱注册走 new-api JSON 协议：GET /api/verification 发验证码 → POST
  /api/user/register（username+password+email+verification_code+turnstile），
  Turnstile 由 /api/status.turnstile_site_key 下发。
- Google 登录走 new-api 的 OIDC provider：/api/oauth/state →
  accounts.google.com/o/oauth2/v2/auth → 回调 /oauth/oidc → 落地 /dashboard。
- 拿 key：POST /api/token/（new-api 标准 payload），响应 data.key 为明文 key。

支持四种执行器：
- protocol：纯 HTTP 协议（remote 打码解 Turnstile + /api 协议注册 + 拿 key）。
- cdp_protocol：CDP 混合（真实 Chrome 过 Turnstile，同步 cookie/UA 后走 /api 协议）。
- headless/headed：浏览器填表注册（真实 Chrome 填表 + 协议拿 key）。
- oauth_browser：Google OAuth 登录（浏览器 + drive_google_oauth + 协议拿 key）。
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

SITE_URL = "https://mixroute.ai/"
CONSOLE_URL = "https://console.mixroute.ai"
API_BASE = "https://api.mixroute.ai/v1"


@register
class MixRoutePlatform(BasePlatform):
    name = "mixroute"
    display_name = "MixRoute"
    version = "1.0.0"
    supported_executors = ["protocol", "cdp_protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google"]
    # new-api 邮件验证码可能落 Junk，outlook_token 扫 INBOX/Junk/Junk Email。
    default_mail_provider = "outlook_token"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # MixRoute 要求密码至少 8 位；用强密码避免被复杂度策略拒绝。
        return password or self._make_random_password(length=14)

    def _resolve_captcha_solver(self) -> str:
        requested = str(self.config.captcha_solver or "").strip().lower()
        if self.config.executor_type == "cdp_protocol" and (not requested or requested == "auto"):
            return "cdp_turnstile"
        return super()._resolve_captcha_solver()

    def _key_name(self, ctx) -> str:
        return str(
            ctx.extra.get("mixroute_key_name")
            or ctx.extra.get("api_key_name")
            or "auto-register"
        ).strip() or "auto-register"

    def _aff_code(self, ctx) -> str:
        return str(
            ctx.extra.get("mixroute_aff_code")
            or ctx.extra.get("aff_code")
            or ctx.extra.get("aff")
            or ""
        ).strip()

    def _username(self, ctx) -> str:
        return str(ctx.extra.get("mixroute_username") or "").strip()

    def _resolve_google_password(self, ctx) -> str:
        """从 Google 账号池复用账号取密码，回退到任务显式配置。"""
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

    def _resolve_totp_secret(self, ctx) -> str:
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        pool_totp = mailbox_extra.get("google_pool_totp_secret") or mailbox_extra.get("totp_secret")
        if pool_totp:
            return str(pool_totp).strip()
        return str(ctx.extra.get("totp_secret") or ctx.extra.get("google_totp_secret") or "").strip()

    def _map_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        user = dict(result.get("user") or {}) if isinstance(result.get("user"), dict) else {}
        api_key = str(result.get("api_key") or "").strip()
        user_id = str(result.get("user_id") or user.get("id") or "").strip()
        email = str(result.get("email") or user.get("email") or "").strip()
        account_overview = {
            "remote_email": email,
            "username": str(result.get("username") or user.get("username") or ""),
            "api_key_created": bool(api_key),
            "auth_method": str(result.get("auth_method") or ""),
            "chips": [item for item in (
                "邮箱" if result.get("auth_method") == "email" else "Google",
                "API Key" if api_key else "",
            ) if item],
        }
        return RegistrationResult(
            email=email,
            password=password or str(result.get("password") or ""),
            user_id=user_id,
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.INVALID,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_info": dict(result.get("api_key_info") or {}),
                "api_verification": dict(result.get("api_verification") or {}),
                "key_create_result": dict(result.get("key_create_result") or {}),
                "username": str(result.get("username") or user.get("username") or ""),
                "user": user,
                "session_token": str(result.get("session_token") or result.get("access_token") or ""),
                "access_token": str(result.get("access_token") or ""),
                "cookies": dict(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "auth_method": str(result.get("auth_method") or ""),
                "api_key_source": str(result.get("api_key_source") or ""),
                "site_url": str(result.get("site_url") or SITE_URL),
                "dashboard_url": str(result.get("dashboard_url") or CONSOLE_URL + "/dashboard"),
                "api_base": str(result.get("api_base") or API_BASE),
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "account_overview": account_overview,
            },
        )

    def _resolve_proxy(self, ctx) -> str | None:
        """协议注册需走代理绕过 new-api 对 /api/user/register 的 per-IP 限流。

        优先用任务显式 proxy；未设时若 resin 已启用则自动轮换一个出口 IP。
        mixroute_proxy / mixroute_resin_enabled 可显式关闭此行为。
        """
        proxy = str(ctx.proxy or "").strip() or None
        if proxy:
            return proxy
        extra = dict(ctx.extra or {})
        if str(extra.get("mixroute_resin_enabled", "true")).strip().lower() not in {"1", "true", "yes", "on"}:
            return None
        try:
            from core.config_store import config_store
            from core.resin_proxy import resolve_resin_proxy_config
            import time as _time
            rp = resolve_resin_proxy_config({
                "resin_enabled": "true",
                "resin_scheme": config_store.get("resin_scheme", ""),
                "resin_host": config_store.get("resin_host", ""),
                "resin_port": config_store.get("resin_port", ""),
                "resin_token": config_store.get("resin_token", ""),
                "resin_default_platform": config_store.get("resin_default_platform", "Default"),
                "resin_platform_map": config_store.get("resin_platform_map", ""),
            }, task_platform="mixroute", account=f"mr{int(_time.time())%100000}", require_enabled=True)
            return str(rp.get("proxy_url") or "").strip() or None
        except Exception:
            return None

    def _run_mailbox_protocol(self, ctx, artifacts) -> dict:
        """protocol / cdp_protocol 执行器的邮箱注册。"""
        worker = __import__(
            "platforms.mixroute.protocol_register",
            fromlist=["MixRouteProtocolRegister"],
        ).MixRouteProtocolRegister(
            proxy=self._resolve_proxy(ctx),
            log_fn=ctx.log,
            use_cdp_bridge=(ctx.executor_type == "cdp_protocol"),
        )
        if artifacts.otp_callback is None:
            raise RuntimeError("MixRoute 邮箱注册缺少 OTP 回调，请配置可收信的邮箱来源")
        return worker.run(
            email=ctx.identity.email or "",
            password=ctx.password or "",
            username=self._username(ctx),
            otp_callback=artifacts.otp_callback,
            captcha_solver=artifacts.captcha_solver,
            key_name=self._key_name(ctx),
            aff_code=self._aff_code(ctx),
        )

    def _run_oauth(self, ctx) -> dict:
        from platforms.mixroute.browser_oauth import register_with_browser_oauth

        extra = dict(ctx.extra or {})
        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google") or "google",
            email_hint=getattr(ctx.identity, "email", "") or extra.get("oauth_email_hint", ""),
            google_password=self._resolve_google_password(ctx),
            totp_secret=self._resolve_totp_secret(ctx),
            key_name=self._key_name(ctx),
            timeout=resolve_timeout(extra, ("mixroute_oauth_timeout", "browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", "") or str(extra.get("mixroute_chrome_user_data_dir") or ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", "") or str(extra.get("mixroute_cdp_url") or ""),
            use_camoufox=str(extra.get("mixroute_oauth_use_camoufox", "true")).strip().lower() in {"1", "true", "yes", "on"},
            cancel_token=getattr(ctx, "cancel_token", None),
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: object(),
            register_runner=lambda _worker, ctx, artifacts: self._run_mailbox_protocol(ctx, artifacts),
            otp_spec=OtpSpec(
                keyword="MixRoute",
                # MixRoute 邮件是 HTML-only，正文含 emoji 实体（&#128737; 飞碟、&#128274; 锁），
                # 其 codepoint 恰好是 6 位数字，会被通用 \d{6} 误匹配。
                # 用负向断言排除 &#...; HTML 实体，只取真正的验证码。
                code_pattern=r"(?<!&#)(?<!\d)(\d{6})(?!\d)(?!;)",
                wait_message="等待 MixRoute 邮箱验证码...",
                success_label="MixRoute 验证码",
            ),
            # Turnstile 在发送验证码与注册端点都要，需打码。
            use_captcha=True,
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
                "platforms.mixroute.browser_register",
                fromlist=["MixRouteBrowserRegistrar"],
            ).MixRouteBrowserRegistrar(
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                key_name=self._key_name(ctx),
                timeout=resolve_timeout(
                    ctx.extra,
                    ("mixroute_browser_timeout", "browser_oauth_timeout", "manual_oauth_timeout"),
                    240,
                ),
                chrome_path=str(extra.get("mixroute_chrome_path", "") or ""),
                cdp_url=str(extra.get("mixroute_cdp_url", "") or ""),
                headless=str(extra.get("mixroute_headless", "false") or "false").strip().lower() in {"1", "true", "yes"},
                log_fn=ctx.log,
                cancel_token=getattr(ctx, "cancel_token", None),
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
                username=self._username(ctx),
            ),
            otp_spec=OtpSpec(
                keyword="MixRoute",
                # 同 protocol：排除 HTML emoji 实体（&#128737; 等）的 6 位 codepoint。
                code_pattern=r"(?<!&#)(?<!\d)(\d{6})(?!\d)(?!;)",
                wait_message="等待 MixRoute 邮箱验证码...",
                success_label="MixRoute 验证码",
            ),
            # 浏览器里 Turnstile widget 由真实 Chrome 自然通过，不需要 captcha_solver。
            use_captcha_for_mailbox=False,
        )

    def _should_use_browser_registration_flow(self, identity) -> bool:
        # oauth_browser（Google OAuth 登录）在 headless/headed/cdp_protocol 下走浏览器 OAuth adapter；
        # 邮箱注册：protocol/cdp_protocol 走 ProtocolMailboxFlow（new-api 协议），headless/headed 走浏览器填表。
        if getattr(identity, "identity_provider", "") == "oauth_browser":
            return (self.config.executor_type or "") in ("headless", "headed", "cdp_protocol")
        return (self.config.executor_type or "") in ("headless", "headed")

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        api_key = str(account.token or extra.get("api_key") or extra.get("ai_api_token") or "").strip()
        if not api_key:
            return False
        # 有 api_verification 记录时直接用，否则走协议验证。
        verification = extra.get("api_verification") if isinstance(extra.get("api_verification"), dict) else {}
        if verification:
            return bool(verification.get("ok"))
        try:
            from platforms.mixroute.core import verify_api_key_http
            proxy = (self.config.proxy if self.config else None)
            return bool(verify_api_key_http(api_key, proxy=proxy).get("ok"))
        except Exception:
            return False

    def get_quota(self, account: Account) -> dict:
        # MixRoute 是充值型网关，额度查询需 /api/user/self 会话，协议层暂不暴露。
        return {}
