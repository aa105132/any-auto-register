"""CodeWords 平台插件 (codewords.agemo.ai)。

支持两种注册路径:
  1. Google OAuth — 主要推荐 (邮箱魔法链接服务端可能未配置)
  2. Email Magic Link — 需 Turnstile 验证码 + 邮箱验证链接
"""

from __future__ import annotations

from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    LinkSpec,
    OtpSpec,
    ProtocolMailboxAdapter,
    ProtocolOAuthAdapter,
    RegistrationCapability,
    RegistrationResult,
)
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.codewords.core import CodewordsClient


@register
class CodewordsPlatform(BasePlatform):
    name = "codewords"
    display_name = "CodeWords"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google"]

    def __init__(self, config: RegisterConfig = None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    # ------------------------------------------------------------------
    # 无密码 — 邮箱路径无需密码, OAuth 路径也同样
    # ------------------------------------------------------------------
    def _prepare_registration_password(self, password: str | None) -> str | None:
        return ""

    # ------------------------------------------------------------------
    # 结果映射
    # ------------------------------------------------------------------
    def _map_result(self, raw: dict) -> RegistrationResult:
        session = raw.get("session") or {}
        user = session.get("user") or {}
        cookies = raw.get("cookies") or {}
        return RegistrationResult(
            email=raw.get("email", ""),
            password="",
            user_id=str(user.get("email") or user.get("id") or raw.get("email", "")),
            token=raw.get("token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "session": session,
                "cookies": cookies,
                "session_token": raw.get("token", ""),
            },
        )

    def _map_oauth_result(self, raw: dict) -> RegistrationResult:
        session = raw.get("session") or {}
        user = session.get("user") or {}
        cookies = raw.get("cookies") or {}
        token = raw.get("token", "")
        email = raw.get("email", "") or str(user.get("email", ""))
        return RegistrationResult(
            email=email,
            password="",
            user_id=str(user.get("email") or user.get("id") or email),
            token=token,
            status=AccountStatus.REGISTERED,
            extra={
                "session": session,
                "cookies": cookies,
                "session_token": token,
                "user_info": user,
            },
        )

    # ------------------------------------------------------------------
    # Google OAuth (浏览器)
    # ------------------------------------------------------------------
    def _run_browser_oauth(self, ctx) -> dict:
        from platforms.codewords.browser_register import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=ctx.identity.oauth_provider,
            email_hint=ctx.identity.email,
            timeout=resolve_timeout(
                ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300
            ),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
            google_password=str(ctx.password or ctx.extra.get("google_password") or ""),
        )

    # ------------------------------------------------------------------
    # 浏览器注册适配器
    # ------------------------------------------------------------------
    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_oauth_result(result)
            if ctx.identity.identity_provider == "oauth_browser"
            else self._map_result(result),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.codewords.browser_register", fromlist=["CodewordsBrowserRegister"]
            ).CodewordsBrowserRegister(
                captcha=artifacts.captcha_solver,
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                verification_link_callback=artifacts.verification_link_callback,
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
            ),
            oauth_runner=self._run_browser_oauth,
            capability=RegistrationCapability(
                oauth_headless_requires_browser_reuse=True,
            ),
            link_spec=LinkSpec(
                keyword="codewords",
                timeout=120,
                wait_message="等待 CodeWords 验证链接...",
                success_label="CodeWords 验证链接",
                preview_chars=100,
            ),
            use_captcha_for_mailbox=True,
        )

    # ------------------------------------------------------------------
    # 协议邮箱注册适配器 (Magic Link)
    # ------------------------------------------------------------------
    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, raw: self._map_result(raw),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.codewords.protocol_mailbox",
                fromlist=["CodewordsProtocolMailboxWorker"],
            ).CodewordsProtocolMailboxWorker(
                proxy=ctx.proxy,
                captcha=artifacts.captcha_solver,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                verification_link_callback=artifacts.verification_link_callback,
            ),
            link_spec=LinkSpec(
                keyword="codewords",
                timeout=120,
                wait_message="等待 CodeWords 验证链接...",
                success_label="CodeWords 验证链接",
                preview_chars=100,
            ),
            use_captcha=True,
        )

    # ------------------------------------------------------------------
    # 协议 OAuth 适配器 (Google)
    # ------------------------------------------------------------------
    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_browser_oauth,
            result_mapper=lambda ctx, result: self._map_oauth_result(result),
        )

    # ------------------------------------------------------------------
    # 账号有效性检测 — 用存储的 cookies 访问 /api/auth/session
    # ------------------------------------------------------------------
    def check_valid(self, account: Account) -> bool:
        cookies = (account.extra or {}).get("cookies") or {}
        if not cookies:
            return bool(account.token)

        import requests as _requests
        try:
            s = _requests.Session()
            s.headers.update({"User-Agent": CodewordsClient.UA})
            for key, value in cookies.items():
                s.cookies.set(key, str(value))
            r = s.get(
                f"{CodewordsClient.BASE_URL}/api/auth/session",
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            user = data.get("user") or {}
            return bool(user.get("email"))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 平台操作
    # ------------------------------------------------------------------
    def get_platform_actions(self) -> list:
        return [
            {"id": "check_session", "label": "检查会话状态", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "check_session":
            return {
                "ok": self.check_valid(account),
                "data": {
                    "valid": self.check_valid(account),
                    "email": account.email,
                },
            }
        raise NotImplementedError(f"未知操作: {action_id}")