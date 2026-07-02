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

    def _make_captcha(self, **kwargs):
        # 按 executor_type 选打码 solver（与 build_protocol_mailbox_adapter 路径选择一致）：
        # - 纯协议（protocol）：强制远程纯 HTTP 打码（YesCaptcha 带代理），零浏览器。打码平台
        #   用同任务代理解题，token 与注册请求同出口 IP（参照 airouter._harvest_turnstile），
        #   避免本机 IP 暴露 + IP 风控。
        # - 浏览器（cdp_protocol/headless/headed）：用 _resolve_captcha_solver（默认 cdp_turnstile）
        #   + anycap 专属 chrome/cdp 配置透传，solver 与注册浏览器用同一套 Chrome。
        # extra[anycap_captcha_solver] 可显式指定（yescaptcha/2captcha/cdp_turnstile），覆盖默认。
        extra = dict(self.config.extra or {})
        requested = str(extra.get("anycap_captcha_solver") or "").strip().lower()
        executor_type = str(self.config.executor_type or "").strip().lower()
        force_protocol = str(extra.get("anycap_use_protocol", "") or "").strip().lower() in {"1", "true", "yes", "on"}
        is_protocol_path = executor_type == "protocol" or force_protocol

        # 显式指定优先
        if requested in {"cdp", "cdp_turnstile", "cdp_protocol", "local_solver", "browser"}:
            if not str(extra.get("chrome_path") or "").strip() and str(extra.get("anycap_chrome_path") or "").strip():
                extra["chrome_path"] = extra["anycap_chrome_path"]
            if not str(extra.get("chrome_cdp_url") or "").strip() and str(extra.get("anycap_cdp_url") or "").strip():
                extra["chrome_cdp_url"] = extra["anycap_cdp_url"]
            from core.base_captcha import create_captcha_solver
            return create_captcha_solver("cdp_turnstile", extra)
        if requested in {"yescaptcha", "yescaptcha_api", "2captcha", "twocaptcha", "twocaptcha_api"}:
            from core.base_captcha import create_captcha_solver
            return create_captcha_solver(requested, extra)

        if is_protocol_path:
            # 纯协议路径默认 yescaptcha（纯 HTTP 带代理），2captcha 兜底
            from core.base_captcha import create_captcha_solver
            return create_captcha_solver("yescaptcha", extra)
        # 浏览器路径：用 _resolve_captcha_solver（cdp_protocol 默认 cdp_turnstile）+ chrome 配置透传
        if not str(extra.get("chrome_path") or "").strip() and str(extra.get("anycap_chrome_path") or "").strip():
            extra["chrome_path"] = extra["anycap_chrome_path"]
        if not str(extra.get("chrome_cdp_url") or "").strip() and str(extra.get("anycap_cdp_url") or "").strip():
            extra["chrome_cdp_url"] = extra["anycap_cdp_url"]
        from core.base_captcha import create_captcha_solver
        return create_captcha_solver(self._resolve_captcha_solver(), extra)

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
        # 按 executor_type 选路径（用户可选协议或浏览器，参照 airouter）：
        # - protocol → 纯协议（AnyCapProtocolMailboxWorker，零浏览器，ProtocolExecutor=curl_cffi
        #   + YesCaptcha 带代理解 Turnstile，全程树脂代理，打码 token 与注册同出口 IP）
        # - cdp_protocol/headless/headed → 浏览器路径（AnyCapMailboxRegistrar，真实 Chrome CDP
        #   驱动 Auth0 UI，打码 solver 注入 token，已修复 sync 隔离/already registered 早失败）
        # extra[anycap_use_browser]=true 强制浏览器，extra[anycap_use_protocol]=true 强制纯协议。
        executor_type = str(self.config.executor_type or "").strip().lower()
        force_browser = str(extra.get("anycap_use_browser", "") or "").strip().lower() in {"1", "true", "yes", "on"}
        force_protocol = str(extra.get("anycap_use_protocol", "") or "").strip().lower() in {"1", "true", "yes", "on"}
        use_browser = force_browser or (executor_type in {"cdp_protocol", "headless", "headed"} and not force_protocol)

        if use_browser:
            # 浏览器路径（AnyCapMailboxRegistrar）
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
                    inventory_id=int((ctx.extra or {}).get("_inventory", {}).get("id", 0) or 0),
                    captcha_solver=artifacts.captcha_solver,
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
                    timeout=resolve_timeout(extra, ("anycap_otp_timeout", "anycap_oauth_timeout", "mail_otp_timeout"), 180),
                ),
                use_captcha=True,
            )
        # 纯协议路径（零浏览器）：protocol executor → ProtocolExecutor + AnyCapProtocolRegister
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.anycap.protocol_register",
                fromlist=["AnyCapProtocolMailboxWorker"],
            ).AnyCapProtocolMailboxWorker(
                executor=artifacts.executor,
                captcha=artifacts.captcha_solver,
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or ctx.platform._make_random_password(),
                otp_callback=artifacts.otp_callback,
            ),
            otp_spec=OtpSpec(
                keyword="Auth0",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for AnyCap/Auth0 email verification code...",
                success_label="AnyCap Auth0 OTP",
                timeout=resolve_timeout(extra, ("anycap_otp_timeout", "anycap_oauth_timeout", "mail_otp_timeout"), 180),
            ),
            use_captcha=True,
            use_executor=True,
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
