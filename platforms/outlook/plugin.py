"""Outlook/Hotmail 邮箱自动注册平台插件。

平台自身产出 Outlook/Hotmail 邮箱（不消费外部 mailbox provider），注册成功后：
  - 用内置公开 client_id + PKCE 走 Microsoft OAuth2 拿 refresh_token（scope 含
    IMAP.AccessAsUser.All + offline_access + Graph Mail.ReadWrite/Send/User.Read），
    refresh_token 可长期刷新 access_token 走 IMAP XOAUTH2 收件（复用 OutlookTokenMailbox）。
  - 双写：mailbox_inventory(outlook_token) 供其它平台领用复用 + outlook_accounts_pool.json
    供离线导出。

身份模式：outlook_self（新）。区别于 mailbox（消费外部邮箱）和 oauth_browser（消费外部
OAuth 账号）——这里 email 由 worker 在注册过程中随机生成，BasePlatform 不要求外部传 email。

执行器：headless / headed / cdp_protocol 均走浏览器自动化（patchright/camoufox）。
"""
from __future__ import annotations

from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    RegistrationCapability,
    RegistrationResult,
)
from core.registry import register
from platforms.outlook.constants import (
    DEFAULT_BOT_PROTECTION_WAIT_SECONDS,
    DEFAULT_EMAIL_SUFFIX,
    DEFAULT_MAX_CAPTCHA_RETRIES,
    DEFAULT_OAUTH_TIMEOUT,
    DEFAULT_REGISTER_TIMEOUT,
    EXTRA_BOT_PROTECTION_WAIT,
    EXTRA_EMAIL_SUFFIX,
    EXTRA_MAX_CAPTCHA_RETRIES,
    EXTRA_OAUTH_TIMEOUT,
    EXTRA_REGISTER_TIMEOUT,
    EXTRA_USE_CAMOUFOX,
    EXTRA_USE_PROTOCOL_PROOF,
    OUTLOOK_IMAP_PORT,
    OUTLOOK_IMAP_SERVER,
)


@register
class OutlookRegisterPlatform(BasePlatform):
    name = "outlook"
    display_name = "Outlook 邮箱注册"
    version = "1.0.0"
    supported_executors = ["headless", "headed", "cdp_protocol"]
    supported_identity_modes = ["outlook_self"]
    supported_oauth_providers: list[str] = []
    default_mail_provider = ""

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox  # 不使用，outlook_self 自生成邮箱

    def _should_require_identity_email(self) -> bool:
        # outlook_self 模式：email 由 worker 注册过程中生成，不要求外部传 email
        return False

    def _should_use_browser_registration_flow(self, identity) -> bool:
        # 所有执行器都走浏览器自动化（Outlook 注册必须开浏览器过 Arkose）
        return True

    def _map_result(self, result: dict) -> RegistrationResult:
        ok = bool(result.get("ok"))
        email = str(result.get("email") or "").strip()
        password = str(result.get("password") or "").strip()
        refresh_token = str(result.get("refresh_token") or "").strip()
        client_id = str(result.get("client_id") or "").strip()
        access_token = str(result.get("access_token") or "").strip()
        expires_at = str(result.get("expires_at") or "").strip()
        scope = str(result.get("scope") or "").strip()
        return RegistrationResult(
            email=email,
            password=password,
            user_id="",
            token=refresh_token,  # 主令牌放 refresh_token（长期有效）
            status=AccountStatus.REGISTERED if (ok and refresh_token) else AccountStatus.INVALID,
            extra={
                "refresh_token": refresh_token,
                "client_id": client_id,
                "access_token": access_token,
                "expires_at": expires_at,
                "scope": scope,
                "imap_server": OUTLOOK_IMAP_SERVER,
                "imap_port": OUTLOOK_IMAP_PORT,
                "first_name": str(result.get("first_name") or ""),
                "last_name": str(result.get("last_name") or ""),
                "birthdate": str(result.get("birthdate") or ""),
                "source": "outlook_self_register",
                "register_error": str(result.get("error") or result.get("oauth_error") or ""),
                "account_overview": {
                    "email": email,
                    "client_id": client_id,
                    "has_refresh_token": bool(refresh_token),
                    "has_access_token": bool(access_token),
                    "imap_server": OUTLOOK_IMAP_SERVER,
                    "imap_port": OUTLOOK_IMAP_PORT,
                    "chips": [item for item in ("系统邮箱", "IMAP 令牌" if refresh_token else "令牌缺失", "OAuth2 PKCE") if item],
                },
            },
        )

    def _resolve_extra_int(self, ctx, key: str, default: int) -> int:
        try:
            return int(ctx.extra.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def _build_worker_kwargs(self, ctx) -> dict[str, Any]:
        extra = ctx.extra or {}
        return {
            "headless": (ctx.executor_type == "headless"),
            "proxy": ctx.proxy,
            "email_suffix": str(extra.get(EXTRA_EMAIL_SUFFIX, DEFAULT_EMAIL_SUFFIX) or DEFAULT_EMAIL_SUFFIX).strip().lower(),
            "bot_protection_wait": self._resolve_extra_int(ctx, EXTRA_BOT_PROTECTION_WAIT, DEFAULT_BOT_PROTECTION_WAIT_SECONDS),
            "max_captcha_retries": self._resolve_extra_int(ctx, EXTRA_MAX_CAPTCHA_RETRIES, DEFAULT_MAX_CAPTCHA_RETRIES),
            "use_camoufox": str(extra.get(EXTRA_USE_CAMOUFOX, "")).strip().lower() in {"1", "true", "yes", "on"},
            "use_protocol_proof": str(extra.get(EXTRA_USE_PROTOCOL_PROOF, "true")).strip().lower() in {"1", "true", "yes", "on"},
            "register_timeout": self._resolve_extra_int(ctx, EXTRA_REGISTER_TIMEOUT, DEFAULT_REGISTER_TIMEOUT),
            "oauth_timeout": self._resolve_extra_int(ctx, EXTRA_OAUTH_TIMEOUT, DEFAULT_OAUTH_TIMEOUT),
            "extra": dict(extra),
            "log_fn": ctx.log,
        }

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.outlook.browser_register",
                fromlist=["OutlookBrowserRegister"],
            ).OutlookBrowserRegister(**self._build_worker_kwargs(ctx)),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            capability=RegistrationCapability(
                browser_mailbox_requires_email=False,
                browser_mailbox_requires_mailbox=False,
            ),
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        refresh_token = str(account.token or extra.get("refresh_token") or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        return bool(refresh_token and client_id)

    def get_platform_actions(self) -> list:
        return [
            {"id": "export_refresh_token", "label": "导出 refresh_token 行", "params": []},
            {"id": "verify_imap_login", "label": "验证 IMAP 登录", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "export_refresh_token":
            extra = dict(account.extra or {})
            email = str(account.email or "").strip()
            password = str(account.password or extra.get("password") or "").strip()
            client_id = str(extra.get("client_id") or "").strip()
            refresh_token = str(account.token or extra.get("refresh_token") or "").strip()
            if not email or not client_id or not refresh_token:
                return {"ok": False, "error": "该账号缺少 email/client_id/refresh_token"}
            line = f"{email}----{password}----{client_id}----{refresh_token}"
            return {"ok": True, "data": {"line": line, "email": email}}
        if action_id == "verify_imap_login":
            return self._verify_imap_login(account)
        raise NotImplementedError(f"Outlook 不支持操作: {action_id}")

    def _verify_imap_login(self, account: Account) -> dict:
        """用 refresh_token 刷新 access_token 后尝试 IMAP XOAUTH2 登录，验证令牌可用性。"""
        extra = dict(account.extra or {})
        email = str(account.email or "").strip()
        client_id = str(extra.get("client_id") or "").strip()
        refresh_token = str(account.token or extra.get("refresh_token") or "").strip()
        if not email or not client_id or not refresh_token:
            return {"ok": False, "error": "缺少 email/client_id/refresh_token"}
        try:
            import requests
            resp = requests.post(
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return {"ok": False, "error": f"刷新 access_token 失败: HTTP {resp.status_code} {resp.text[:200]}"}
            access_token = str(resp.json().get("access_token") or "").strip()
            if not access_token:
                return {"ok": False, "error": "刷新未返回 access_token"}
            # IMAP XOAUTH2 登录尝试
            import imaplib
            import base64
            auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
            conn = imaplib.IMAP4_SSL(OUTLOOK_IMAP_SERVER, OUTLOOK_IMAP_PORT)
            try:
                conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
                typ, _ = conn.select("INBOX", readonly=True)
                ok = typ == "OK"
                return {"ok": ok, "data": {"imap_login": ok, "email": email}}
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as exc:
            return {"ok": False, "error": f"IMAP 验证异常: {type(exc).__name__}: {str(exc)[:200]}"}
