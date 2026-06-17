"""GetToken 平台插件。

已侦察到的注册链路（2026-05-18）：
1. 前端通过 Portal Login SDK 打开 https://pay.imgto.link/{locale}/auth/connect/...
2. Portal 成功后向 gettoken.dev 回传 loginToken。
3. POST /api/auth/portal-login {loginToken, referralCode?, referralHost, referralSlug?}
4. GET /api/user/me 验证 session。
5. 登录后进入 /console/api-keys，前端再加载 API Key 列表/创建入口。

协议优先策略：如果调用方已经提供 gettoken_portal_login_token，则直接走 3-5；
否则协议层明确要求降级到 CDP/真实浏览器获取 loginToken。
"""
from __future__ import annotations

from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registry import register


@register
class GetTokenPlatform(BasePlatform):
    name = "gettoken"
    display_name = "GetToken"
    version = "1.0.0"
    supported_executors = ["protocol", "headed", "headless"]
    supported_identity_modes = ["oauth_browser", "phone"]
    supported_oauth_providers = ["google"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox


    def register(self, email: str = None, password: str = None) -> Account:
        if self._get_identity_provider_name() != "phone":
            return super().register(email=email, password=password)
        resolved_password = self._prepare_registration_password(password)
        identity = self._resolve_identity(email, require_email=False)
        from core.registration import ProtocolOAuthFlow, RegistrationContext
        ctx = RegistrationContext(
            platform_name=self.name,
            platform_display_name=self.display_name,
            platform=self,
            identity=identity,
            config=self.config,
            email=email,
            password=resolved_password,
            log_fn=self.log,
        )
        adapter = self.build_protocol_oauth_adapter()
        result = ProtocolOAuthFlow(adapter).run(ctx)
        return self._attach_identity_metadata(self._account_from_registration_result(result), identity)

    def _should_require_identity_email(self) -> bool:
        return False

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _map_result(self, result: dict) -> RegistrationResult:
        api_key = str(result.get("api_key") or "").strip()
        email = str(result.get("email") or "").strip()
        return RegistrationResult(
            email=email,
            password="",
            user_id=str(result.get("user_id") or ""),
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.PENDING,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_info": dict(result.get("api_key_info") or {}),
                "account_info": dict(result.get("account_info") or {}),
                "cookies": str(result.get("session_cookie") or ""),
                "cookie_map": dict(result.get("cookies") or {}),
                "request_trace": list(result.get("request_trace") or []),
                "registration_note": str(result.get("registration_note") or ""),
                "phone": dict(result.get("phone") or {}),
                "portal_result": dict(result.get("portal_result") or {}),
                "api_base": "https://gettoken.dev",
            },
        )

    def _run_protocol_phone(self, ctx) -> dict:
        phone_provider = getattr(self, "phone_provider", None)
        if phone_provider is None:
            raise RuntimeError("GetToken 手机号注册需要启用并配置 phone_provider")
        captcha_solver = self._make_gettoken_phone_captcha_solver(ctx)
        return __import__(
            "platforms.gettoken.protocol_oauth",
            fromlist=["GetTokenProtocolPhoneWorker"],
        ).GetTokenProtocolPhoneWorker(proxy=ctx.proxy, log_fn=ctx.log).run(
            phone_provider=phone_provider,
            referral_code=str(ctx.extra.get("gettoken_referral_code") or ""),
            referral_slug=str(ctx.extra.get("gettoken_referral_slug") or ""),
            create_api_key=ctx.extra.get("gettoken_create_api_key", True),
            otp_timeout=int(ctx.extra.get("phone_otp_timeout", ctx.extra.get("haozhu_phone_timeout", 180)) or 180),
            poll_interval=int(ctx.extra.get("phone_poll_interval", ctx.extra.get("haozhu_poll_interval", 15)) or 15),
            code_pattern=str(ctx.extra.get("phone_code_pattern") or "").strip() or None,
            captcha_solver=captcha_solver,
        )

    def _make_gettoken_phone_captcha_solver(self, ctx):
        """GetToken 手机号链路目前要求腾讯滑块，优先使用本地 CDP solver。"""
        from core.base_captcha import create_captcha_solver

        extra = dict(ctx.extra or {})
        requested = str(extra.get("gettoken_phone_captcha_solver") or self.config.captcha_solver or "").strip().lower()
        # 图灵云本身只负责识别滑块距离，实际仍需本地 Chrome 执行腾讯回调。
        # 因此用户把 GetToken 手机号 solver 指到 tulingcloud 时，自动包进 cdp_turnstile。
        solver_name = "cdp_turnstile" if requested in {"", "auto", "tulingcloud", "tulingcloud_api"} else requested
        solver = create_captcha_solver(solver_name, self.config.extra)
        if not hasattr(solver, "solve_tencent_captcha"):
            raise RuntimeError(f"GetToken 手机号注册需要支持腾讯滑块的验证码 solver，当前 {solver_name} 不支持")
        return solver

    def _run_protocol_or_browser_oauth(self, ctx) -> dict:
        portal_login_token = str(ctx.extra.get("gettoken_portal_login_token") or "").strip()
        if portal_login_token:
            return __import__(
                "platforms.gettoken.protocol_oauth",
                fromlist=["GetTokenProtocolOAuthWorker"],
            ).GetTokenProtocolOAuthWorker(proxy=ctx.proxy, log_fn=ctx.log).run(
                email_hint=getattr(ctx.identity, "email", "") or ctx.extra.get("oauth_email_hint", ""),
                portal_login_token=portal_login_token,
                referral_code=str(ctx.extra.get("gettoken_referral_code") or ""),
                referral_slug=str(ctx.extra.get("gettoken_referral_slug") or ""),
                create_api_key=ctx.extra.get("gettoken_create_api_key", True),
            )

        # ? portal_login_token ????????? Google OAuth??????????? OAuth?
        return __import__(
            "platforms.gettoken.browser_oauth",
            fromlist=["register_with_browser_oauth"],
        ).register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google"),
            email_hint=getattr(ctx.identity, "email", "") or ctx.extra.get("oauth_email_hint", ""),
            google_password=self._resolve_oauth_password(ctx),
            timeout=int(ctx.extra.get("browser_oauth_timeout", ctx.extra.get("manual_oauth_timeout", 300)) or 300),
            log_fn=ctx.log,
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", ""),
            create_api_key=ctx.extra.get("gettoken_create_api_key", True),
        )

    def build_protocol_oauth_adapter(self):
        oauth_runner = self._run_protocol_phone if self._get_identity_provider_name() == "phone" else self._run_protocol_or_browser_oauth
        return ProtocolOAuthAdapter(
            oauth_runner=oauth_runner,
            result_mapper=lambda ctx, result: self._map_result(result),
            capability=RegistrationCapability(oauth_allowed_executor_types=("protocol",)),
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            oauth_runner=lambda ctx: __import__(
                "platforms.gettoken.browser_oauth",
                fromlist=["register_with_browser_oauth"],
            ).register_with_browser_oauth(
                proxy=ctx.proxy,
                oauth_provider=getattr(ctx.identity, "oauth_provider", "google"),
                email_hint=getattr(ctx.identity, "email", "") or ctx.extra.get("oauth_email_hint", ""),
                google_password=self._resolve_oauth_password(ctx),
                timeout=int(ctx.extra.get("browser_oauth_timeout", ctx.extra.get("manual_oauth_timeout", 300)) or 300),
                log_fn=ctx.log,
                chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", ""),
                chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", ""),
                create_api_key=ctx.extra.get("gettoken_create_api_key", True),
            ),
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed", "headless")),
        )

    @staticmethod
    def _resolve_oauth_password(ctx) -> str:
        for key in ("oauth_password", "google_password", "hstockplus_google_password"):
            value = str(ctx.extra.get(key) or "").strip()
            if value:
                return value
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = dict(extra.get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        return str(credentials.get("password") or "").strip()

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        api_key = account.token or extra.get("api_key") or ""
        if not api_key:
            return False
        try:
            import requests
            resp = requests.get(
                "https://api.gettoken.dev/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        return [
            {"id": "get_user", "label": "查询用户信息", "params": []},
            {"id": "export_api_key", "label": "导出 API Key", "params": []},
            {"id": "export_all_keys", "label": "导出全部 GetToken API Key", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "get_user":
            from platforms.gettoken.actions import get_user
            api_key = account.token or dict(account.extra or {}).get("api_key", "")
            cookies = dict(account.extra or {}).get("cookies", "")
            return get_user(api_key=api_key, cookies=cookies)
        if action_id == "export_api_key":
            return self._export_one(account)
        if action_id == "export_all_keys":
            return self._export_all()
        raise NotImplementedError(f"未知操作: {action_id}")

    @staticmethod
    def _write_key(api_key: str) -> str:
        import os
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "gettoken_keys.txt")
        with open(path, "a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        with open(os.path.join(output_dir, "keys.txt"), "a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        with open(os.path.join(output_dir, "ai_api_tokens.txt"), "a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        return path

    @classmethod
    def _export_one(cls, account: Account) -> dict:
        extra = dict(account.extra or {})
        api_key = str(extra.get("api_key") or account.token or "").strip()
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key"}
        path = cls._write_key(api_key)
        return {"ok": True, "data": {"message": f"API Key 已导出到 {path}", "email": account.email}}

    @classmethod
    def _export_all(cls) -> dict:
        from sqlmodel import Session, select
        from core.db import AccountModel, engine
        count = 0
        with Session(engine) as session:
            for row in session.exec(select(AccountModel).where(AccountModel.platform == "gettoken")).all():
                extra = dict(row.extra or {})
                key = str(extra.get("api_key") or row.token or "").strip()
                if key:
                    cls._write_key(key)
                    count += 1
        return {"ok": True, "data": {"message": f"已导出 {count} 个 API Key", "count": count}}
