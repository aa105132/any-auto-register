"""Zo Computer 平台插件。"""
from __future__ import annotations

from pathlib import Path

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, LinkSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.zo.core import API_BASE, AUTH_BASE, SITE_URL


@register
class ZoPlatform(BasePlatform):
    name = "zo"
    display_name = "Zo Computer"
    version = "1.0.0"
    supported_executors = ["protocol", "cdp_protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google"]

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
        for key in ("google_password", "oauth_password"):
            value = str(ctx.extra.get(key) or "").strip()
            if value:
                return value
        return str(ctx.password or "").strip()

    def _run_oauth(self, ctx) -> dict:
        from platforms.zo.browser_oauth import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google") or "google",
            email_hint=getattr(ctx.identity, "email", "") or ctx.extra.get("oauth_email_hint", ""),
            google_password=self._resolve_google_password(ctx),
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", "") or str(ctx.extra.get("chrome_cdp_url") or ""),
            allow_shared_cdp=bool(ctx.extra.get("zo_allow_shared_cdp", False)),
            extra=ctx.extra,
        )

    def _map_result(self, result: dict) -> RegistrationResult:
        api_key = str(result.get("api_key") or result.get("access_token") or "").strip()
        credit_result = dict(result.get("credit_result") or result.get("balance_result") or {})
        card_binding_result = dict(result.get("card_binding_result") or result.get("card_result") or {})
        credit_amount = float(credit_result.get("amount") or 0.0)
        credit_ok = bool(credit_result.get("ok")) and credit_amount >= 100.0
        card_ok = bool(card_binding_result.get("ok"))
        account_info = dict(result.get("account_info") or result.get("settings") or {})
        user = account_info.get("user") if isinstance(account_info.get("user"), dict) else {}
        email = str(result.get("email") or user.get("email") or account_info.get("email") or "").strip()
        return RegistrationResult(
            email=email,
            password=str(result.get("password") or ""),
            user_id=str(result.get("user_id") or user.get("id") or account_info.get("id") or ""),
            token=api_key,
            status=AccountStatus.REGISTERED if api_key and credit_ok and card_ok else AccountStatus.INVALID,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_info": dict(result.get("api_key_info") or {}),
                "api_verification": dict(result.get("api_verification") or {}),
                "key_create_result": dict(result.get("key_create_result") or {}),
                "coupon_result": dict(result.get("coupon_result") or {}),
                "credit_result": credit_result,
                "card_binding_result": card_binding_result,
                "onboarding_result": dict(result.get("onboarding_result") or {}),
                "phone_result": dict(result.get("phone_result") or {}),
                "account_info": account_info,
                "settings": dict(result.get("settings") or {}),
                "cookies": dict(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "site_url": SITE_URL,
                "auth_base": AUTH_BASE,
                "api_base": API_BASE,
                "auth_header": "Authorization: Bearer",
            },
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.zo.protocol_mailbox",
                fromlist=["ZoProtocolMailboxWorker"],
            ).ZoProtocolMailboxWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
                extra=ctx.extra,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                verification_link_callback=artifacts.verification_link_callback,
            ),
            link_spec=LinkSpec(
                keyword="Zo",
                timeout=180,
                wait_message="等待 Zo 验证链接邮件...",
                success_label="Zo 验证链接",
                preview_chars=100,
            ),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_oauth,
            result_mapper=lambda ctx, result: self._map_result(result),
            capability=RegistrationCapability(oauth_allowed_executor_types=("protocol", "cdp_protocol")),
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_result(result),
            oauth_runner=self._run_oauth,
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed", "headless")),
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        return bool(account.token or extra.get("api_key") or extra.get("ai_api_token"))

    def get_platform_actions(self) -> list:
        return [
            {"id": "export_api_key", "label": "导出 API Key", "params": []},
            {"id": "export_all_keys", "label": "导出全部 Zo API Key", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "export_api_key":
            return self._export_one(account)
        if action_id == "export_all_keys":
            return self._export_all()
        raise NotImplementedError(f"未知操作: {action_id}")

    @staticmethod
    def _write_key(api_key: str) -> Path:
        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        zo_path = output_dir / "zo_keys.txt"
        for path in (zo_path, output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        return zo_path

    @classmethod
    def _export_one(cls, account: Account) -> dict:
        extra = dict(account.extra or {})
        api_key = str(extra.get("api_key") or extra.get("ai_api_token") or account.token or "").strip()
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
            for row in session.exec(select(AccountModel).where(AccountModel.platform == "zo")).all():
                extra = dict(row.extra or {})
                key = str(extra.get("api_key") or extra.get("ai_api_token") or row.token or "").strip()
                if key:
                    cls._write_key(key)
                    count += 1
        return {"ok": True, "data": {"message": f"已导出 {count} 个 API Key", "count": count}}
