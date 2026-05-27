"""Featherless 平台插件。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, LinkSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.featherless.protocol_mailbox import API_ORIGIN, DASHBOARD_URL, LLM_API_BASE, SITE_URL


def _json_safe(value: Any, *, _seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if _seen is None:
        _seen = set()
    obj_id = id(value)
    if obj_id in _seen:
        return "<circular>"
    if isinstance(value, dict):
        _seen.add(obj_id)
        try:
            return {str(key): _json_safe(item, _seen=_seen) for key, item in value.items()}
        finally:
            _seen.discard(obj_id)
    if isinstance(value, (list, tuple, set)):
        _seen.add(obj_id)
        try:
            return [_json_safe(item, _seen=_seen) for item in value]
        finally:
            _seen.discard(obj_id)
    return str(value)


@register
class FeatherlessPlatform(BasePlatform):
    name = "featherless"
    display_name = "Featherless"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google"]
    default_mail_provider = "outlook_token"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password(length=16)

    def _resolve_google_password(self, ctx) -> str:
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        credentials = provider_account.get("credentials") if isinstance(provider_account, dict) else None
        if isinstance(credentials, dict) and credentials.get("password"):
            return str(credentials.get("password") or "").strip()
        return str(ctx.password or ctx.extra.get("google_password") or ctx.extra.get("oauth_password") or "").strip()

    def _key_name(self, ctx) -> str:
        return str(ctx.extra.get("featherless_key_name") or ctx.extra.get("api_key_name") or "auto-register").strip() or "auto-register"

    def _run_mailbox(self, ctx, artifacts) -> dict:
        worker = __import__(
            "platforms.featherless.protocol_mailbox",
            fromlist=["FeatherlessProtocolMailboxWorker"],
        ).FeatherlessProtocolMailboxWorker(
            proxy=ctx.proxy,
            log_fn=ctx.log,
        )
        if artifacts.verification_link_callback is None:
            raise RuntimeError("Featherless 邮箱注册缺少验证链接回调，请配置可收信的邮箱来源")
        return worker.run(
            email=ctx.identity.email or "",
            password=ctx.password or "",
            verification_link_callback=artifacts.verification_link_callback,
            key_name=self._key_name(ctx),
            verify_deep=bool(ctx.extra.get("featherless_deep_verify") or ctx.extra.get("deep_verify_api_key")),
        )

    def _run_oauth(self, ctx) -> dict:
        from platforms.featherless.browser_oauth import register_with_browser_oauth

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
            reuse_existing_cdp=bool(ctx.extra.get("reuse_existing_cdp") or ctx.extra.get("oauth_reuse_existing_cdp")),
            key_name=self._key_name(ctx),
            verify_deep=bool(ctx.extra.get("featherless_deep_verify") or ctx.extra.get("deep_verify_api_key")),
        )

    def _map_result(self, result: dict) -> RegistrationResult:
        user = dict(result.get("user") or result.get("session") or {}) if isinstance(result.get("user") or result.get("session"), dict) else {}
        api_key = str(result.get("api_key") or result.get("key") or result.get("token") or "").strip()
        email = str(result.get("email") or user.get("email") or "").strip()
        account_overview = {
            "remote_email": email,
            "user_id": str(result.get("user_id") or user.get("id") or ""),
            "email_verified": user.get("email_verified"),
            "api_key_created": bool(api_key),
            "auth_method": str(result.get("auth_method") or ""),
            "chips": [item for item in ("Google OAuth" if result.get("auth_method") == "google_oauth" else "系统邮箱", "API Key" if api_key else "") if item],
        }
        return RegistrationResult(
            email=email,
            password=str(result.get("password") or ""),
            user_id=str(result.get("user_id") or user.get("id") or ""),
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.PENDING,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_info": _json_safe(result.get("api_key_info") or {}),
                "api_verification": _json_safe(result.get("api_verification") or {}),
                "key_create_result": _json_safe(result.get("key_create_result") or {}),
                "register_result": _json_safe(result.get("register_result") or {}),
                "verify_result": _json_safe(result.get("verify_result") or {}),
                "login_result": _json_safe(result.get("login_result") or {}),
                "me": _json_safe(result.get("me") or {}),
                "user": user,
                "session": _json_safe(result.get("session") or {}),
                "cookies": _json_safe(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "auth_method": str(result.get("auth_method") or ""),
                "site_url": SITE_URL,
                "dashboard_url": DASHBOARD_URL,
                "api_base": LLM_API_BASE,
                "control_api_base": API_ORIGIN,
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: object(),
            register_runner=lambda _worker, ctx, artifacts: self._run_mailbox(ctx, artifacts),
            link_spec=LinkSpec(
                keyword="Featherless",
                timeout=180,
                wait_message="等待 Featherless 验证链接邮件...",
                success_label="Featherless 验证链接",
                preview_chars=100,
            ),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_oauth,
            result_mapper=lambda ctx, result: self._map_result(result),
            capability=RegistrationCapability(oauth_allowed_executor_types=("protocol", "headless", "headed")),
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
            {"id": "export_all_keys", "label": "导出全部 Featherless API Key", "params": []},
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
        featherless_path = output_dir / "featherless_keys.txt"
        for path in (featherless_path, output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        return featherless_path

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
            for row in session.exec(select(AccountModel).where(AccountModel.platform == "featherless")).all():
                extra = dict(row.extra or {})
                key = str(extra.get("api_key") or extra.get("ai_api_token") or row.token or "").strip()
                if key:
                    cls._write_key(key)
                    count += 1
        return {"ok": True, "data": {"message": f"已导出 {count} 个 API Key", "count": count}}
