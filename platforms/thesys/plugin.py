"""Thesys Console 平台插件。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.thesys.core import (
    CHAT_COMPLETIONS_URL,
    CONTROL_API_BASE,
    DEFAULT_FREE_MODEL,
    FREE_MODELS,
    MODELS_URL,
    OPENAI_COMPAT_API_BASE,
    SITE_URL,
    account_preview,
)


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
class ThesysPlatform(BasePlatform):
    name = "thesys"
    display_name = "Thesys"
    version = "1.0.0"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers: list[str] = []
    default_mail_provider = "cfworker"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # Thesys 当前邮箱 OTP 链路无密码；保留 password 仅用于本地账号记录。
        return password or self._make_random_password(length=16, charset="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

    def _key_name(self, ctx) -> str:
        return str(ctx.extra.get("thesys_key_name") or ctx.extra.get("api_key_name") or "auto-register").strip() or "auto-register"

    def _verify_chat(self, ctx) -> bool:
        value = str(ctx.extra.get("thesys_verify_chat") or "true").strip().lower()
        return value not in {"0", "false", "no", "off"}

    def _verify_model(self, ctx) -> str:
        return str(ctx.extra.get("thesys_verify_model") or ctx.extra.get("model") or DEFAULT_FREE_MODEL).strip() or DEFAULT_FREE_MODEL

    def _map_result(self, result: dict[str, Any]) -> RegistrationResult:
        api_key = str(result.get("api_key") or "").strip()
        user = dict(result.get("user") or {}) if isinstance(result.get("user"), dict) else {}
        org = dict(result.get("org") or {}) if isinstance(result.get("org"), dict) else {}
        api_verification = dict(result.get("api_verification") or {}) if isinstance(result.get("api_verification"), dict) else {}
        chat_verification = dict(result.get("chat_verification") or {}) if isinstance(result.get("chat_verification"), dict) else {}
        email = str(result.get("email") or user.get("email") or "").strip()
        account_overview = {
            "remote_email": email,
            "user_id": str(result.get("user_id") or user.get("id") or user.get("userId") or ""),
            "org_id": str(result.get("org_id") or org.get("id") or ""),
            "org_name": str(org.get("name") or ""),
            "api_key_created": bool(api_key),
            "api_key_preview": account_preview(api_key),
            "models_endpoint_ok": bool(api_verification.get("ok")),
            "free_chat_ok": bool(chat_verification.get("ok")) if chat_verification else None,
            "verified_model": str(result.get("verified_model") or DEFAULT_FREE_MODEL),
            "chips": [
                item
                for item in (
                    "系统邮箱",
                    "API Key" if api_key else "未取到 Key",
                    "OpenAI 兼容" if api_verification.get("ok") else "OpenAI 兼容未验证",
                    "Free 模型可调" if chat_verification.get("ok") else "Free 模型未验证",
                )
                if item
            ],
        }
        return RegistrationResult(
            email=email,
            password=str(result.get("password") or ""),
            user_id=account_overview["user_id"],
            token=api_key,
            status=AccountStatus.REGISTERED if api_key and api_verification.get("ok") else AccountStatus.INVALID,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_base": OPENAI_COMPAT_API_BASE,
                "llm_api_base": OPENAI_COMPAT_API_BASE,
                "openai_compatible_api_base": OPENAI_COMPAT_API_BASE,
                "openai_compatible_v1_api_base": OPENAI_COMPAT_API_BASE,
                "chat_completions_url": CHAT_COMPLETIONS_URL,
                "models_url": MODELS_URL,
                "default_free_model": DEFAULT_FREE_MODEL,
                "free_models": list(FREE_MODELS),
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "control_api_base": CONTROL_API_BASE,
                "site_url": SITE_URL,
                "api_key_name": str(result.get("api_key_name") or ""),
                "api_key_info": _json_safe(result.get("api_key_info") or {}),
                "api_key_list": _json_safe(result.get("api_key_list") or {}),
                "api_verification": _json_safe(api_verification),
                "chat_verification": _json_safe(chat_verification),
                "billing": _json_safe(result.get("billing") or {}),
                "user": _json_safe(user),
                "org": _json_safe(org),
                "orgs": _json_safe(result.get("orgs") or []),
                "auth_cookies": _json_safe(result.get("auth_cookies") or {}),
                "auth_result": _json_safe(result.get("auth_result") or {}),
                "otp_send_result": _json_safe(result.get("otp_send_result") or {}),
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.thesys.protocol_mailbox",
                fromlist=["ThesysProtocolMailboxWorker"],
            ).ThesysProtocolMailboxWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
                key_name=self._key_name(ctx),
                verify_chat=self._verify_chat(ctx),
                verify_model=self._verify_model(ctx),
            ),
            otp_spec=OtpSpec(
                keyword=str(extra.get("thesys_otp_keyword") or "Thesys"),
                timeout=resolve_timeout(extra, ("thesys_otp_timeout", "mail_otp_timeout"), 180),
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="等待 Thesys 邮箱验证码...",
                success_label="Thesys 邮箱验证码",
            ),
            use_captcha=False,
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        return bool(account.token or extra.get("api_key") or extra.get("ai_api_token"))

    def get_platform_actions(self) -> list:
        return [{"id": "export_api_key", "label": "导出 API Key", "params": []}]

    @staticmethod
    def _write_key(api_key: str) -> Path:
        output_dir = Path(__file__).resolve().parents[2] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        for target in (output_dir / "thesys_keys.txt", output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with target.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        return output_dir / "thesys_keys.txt"

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "export_api_key":
            raise NotImplementedError(f"未知操作: {action_id}")
        api_key = str(account.token or dict(account.extra or {}).get("api_key") or "").strip()
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key"}
        path = self._write_key(api_key)
        return {"ok": True, "data": {"path": str(path), "email": account.email, "key_preview": account_preview(api_key)}}
