"""HPC-AI 平台插件。"""
from __future__ import annotations

import random
import string
from pathlib import Path
from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.hpcai.protocol_mailbox import MODEL_CONSOLE_URL, OPENAI_COMPAT_API_BASE, SITE_URL


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


def _credit_amount(credit_result: dict[str, Any]) -> float:
    try:
        return float(credit_result.get("amount") or 0.0)
    except Exception:
        return 0.0


@register
class HpcAiPlatform(BasePlatform):
    name = "hpcai"
    display_name = "HPC-AI"
    version = "1.0.0"
    supported_executors = ["protocol", "cdp_protocol"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers: list[str] = []
    default_mail_provider = "outlook_token"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _strong_password(self) -> str:
        required = [
            random.choice(string.ascii_lowercase),
            random.choice(string.ascii_uppercase),
            random.choice(string.digits),
            random.choice('!"#$%&()*+,-./:;<=>?@[\\]^_`{|}~'),
        ]
        pool = string.ascii_letters + string.digits + '!"#$%&()*+,-./:;<=>?@[\\]^_`{|}~'
        chars = required + [random.choice(pool) for _ in range(12)]
        random.shuffle(chars)
        return "".join(chars)

    def _prepare_registration_password(self, password: str | None) -> str | None:
        raw = str(password or "").strip()
        if not raw or len(raw) < 8:
            return self._strong_password()
        has_lower = any(ch.islower() for ch in raw)
        has_upper = any(ch.isupper() for ch in raw)
        has_digit = any(ch.isdigit() for ch in raw)
        has_symbol = any(not ch.isalnum() for ch in raw)
        return raw if all((has_lower, has_upper, has_digit, has_symbol)) else self._strong_password()

    def _resolve_captcha_solver(self) -> str:
        requested = str(self.config.captcha_solver or "").strip().lower()
        if self.config.executor_type == "cdp_protocol" and (not requested or requested == "auto"):
            return "cdp_turnstile"
        return super()._resolve_captcha_solver()

    def _key_name(self, ctx) -> str:
        return str(ctx.extra.get("hpcai_key_name") or ctx.extra.get("api_key_name") or "auto-register").strip() or "auto-register"

    def _invitation_code(self, ctx) -> str:
        return str(ctx.extra.get("hpcai_invitation_code") or ctx.extra.get("invitation_code") or ctx.extra.get("invite_code") or "ban429-mapi").strip()

    def _minimum_credit(self, ctx) -> float:
        value = ctx.extra.get("hpcai_minimum_credit") or ctx.extra.get("minimum_credit") or 2.0
        try:
            return float(value)
        except Exception:
            return 2.0

    def _run_mailbox(self, ctx, artifacts) -> dict:
        worker = __import__(
            "platforms.hpcai.protocol_mailbox",
            fromlist=["HpcAiProtocolMailboxWorker"],
        ).HpcAiProtocolMailboxWorker(
            proxy=ctx.proxy,
            log_fn=ctx.log,
            use_cdp_bridge=(ctx.executor_type == "cdp_protocol"),
        )
        if artifacts.otp_callback is None:
            raise RuntimeError("HPC-AI 邮箱注册缺少 OTP 回调，请配置可收信的邮箱来源")
        return worker.run(
            email=ctx.identity.email or "",
            password=ctx.password or "",
            otp_callback=artifacts.otp_callback,
            captcha_solver=artifacts.captcha_solver,
            key_name=self._key_name(ctx),
            invitation_code=self._invitation_code(ctx),
            minimum_credit=self._minimum_credit(ctx),
        )

    def _map_result(self, result: dict) -> RegistrationResult:
        user = dict(result.get("user") or result.get("session") or {}) if isinstance(result.get("user") or result.get("session"), dict) else {}
        api_key = str(result.get("api_key") or result.get("key") or result.get("token") or "").strip()
        credit_result = dict(result.get("credit_result") or {}) if isinstance(result.get("credit_result"), dict) else {}
        api_verification = dict(result.get("api_verification") or {}) if isinstance(result.get("api_verification"), dict) else {}
        credit_ok = bool(credit_result.get("ok")) and _credit_amount(credit_result) >= 2.0
        api_call_ok = bool(api_verification.get("ok"))
        email = str(result.get("email") or user.get("email") or "").strip()
        account_overview = {
            "remote_email": email,
            "user_id": str(result.get("user_id") or user.get("userId") or user.get("uuid") or user.get("uid") or user.get("id") or ""),
            "api_key_created": bool(api_key),
            "api_call_verified": api_call_ok,
            "credit_verified": credit_ok,
            "credit_amount": _credit_amount(credit_result),
            "chips": [item for item in ("系统邮箱", "赠金" if credit_ok else "赠金未确认", "真实调用" if api_call_ok else "调用失败", "API Key" if api_key else "") if item],
        }
        return RegistrationResult(
            email=email,
            password=str(result.get("password") or ""),
            user_id=str(result.get("user_id") or user.get("userId") or user.get("uuid") or user.get("uid") or user.get("id") or ""),
            token=api_key,
            status=AccountStatus.REGISTERED if api_key and credit_ok and api_call_ok else AccountStatus.INVALID,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_base": OPENAI_COMPAT_API_BASE,
                "openai_compatible_api_base": OPENAI_COMPAT_API_BASE,
                "openai_compatible_v1_api_base": OPENAI_COMPAT_API_BASE,
                "llm_api_base": OPENAI_COMPAT_API_BASE,
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "credit_result": _json_safe(credit_result),
                "api_key_info": _json_safe(result.get("api_key_info") or {}),
                "api_verification": _json_safe(api_verification),
                "key_create_result": _json_safe(result.get("key_create_result") or {}),
                "otp_send_result": _json_safe(result.get("otp_send_result") or {}),
                "register_result": _json_safe(result.get("register_result") or {}),
                "login_result": _json_safe(result.get("login_result") or {}),
                "claim_result": _json_safe(result.get("claim_result") or {}),
                "balance": _json_safe(result.get("balance") or {}),
                "credit_list": _json_safe(result.get("credit_list") or {}),
                "voucher_list": _json_safe(result.get("voucher_list") or {}),
                "cdp_bootstrap": _json_safe(result.get("cdp_bootstrap") or {}),
                "user": _json_safe(user),
                "session": _json_safe(result.get("session") or {}),
                "cookies": _json_safe(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "auth_method": str(result.get("auth_method") or ""),
                "site_url": SITE_URL,
                "dashboard_url": MODEL_CONSOLE_URL,
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: object(),
            register_runner=lambda _worker, ctx, artifacts: self._run_mailbox(ctx, artifacts),
            otp_spec=OtpSpec(
                keyword="",
                timeout=resolve_timeout(self.config.extra if isinstance(self.config.extra, dict) else {}, ("hpcai_otp_timeout", "mail_otp_timeout"), 180),
                code_pattern=r"\b\d{6}\b",
                wait_message="等待 HPC-AI 邮箱验证码...",
                success_label="HPC-AI 邮箱验证码",
            ),
            use_captcha=True,
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        api_key = str(account.token or extra.get("api_key") or extra.get("ai_api_token") or "").strip()
        verification = extra.get("api_verification") if isinstance(extra.get("api_verification"), dict) else {}
        return bool(api_key and verification.get("ok"))

    def get_platform_actions(self) -> list:
        return [{"id": "export_api_key", "label": "导出 API Key", "params": []}]

    @staticmethod
    def _write_key(api_key: str) -> Path:
        output_dir = Path(__file__).resolve().parents[2] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        for target in (output_dir / "hpcai_keys.txt", output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with target.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        return output_dir / "hpcai_keys.txt"

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "export_api_key":
            raise NotImplementedError(f"未知操作: {action_id}")
        api_key = str(account.token or dict(account.extra or {}).get("api_key") or "").strip()
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key"}
        path = self._write_key(api_key)
        return {"ok": True, "data": {"path": str(path), "email": account.email}}
