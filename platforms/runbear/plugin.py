"""Runbear 平台插件。

注册链路（浏览器驱动，patchright + 住宅代理 + Turnstile + 确认邮件链接）：
- 打开 https://auth.runbear.io/en/signup?rt={base64(app.runbear.io/overview)}&signedUp=true
- 填 First name/Last name/Email/Password/How did you hear(combobox 选 "Search engine")/勾 ToS
- Cloudflare Turnstile widget 自动解（sitekey 0x4AAAAAADrn0IM-tpRSsa_-，浏览器内渲染）
- click "Sign up with email" → POST auth.runbear.io/api/fe/v2/signup{email,pwd,turnstile_token,
  first_name,last_name,properties{referral_source,tos}} → 200 → /en/login/confirm_email
- mailbox.wait_for_link(keyword="Runbear") 收确认邮件链接 → 浏览器导航链接确认邮箱
- 确认后拿 PropelAuth access_token（cookie 或 /api/fe/v2/login_state）→ app.runbear.io

收信：默认 cfworker/moemail（支持 wait_for_link 提取确认链接，非 OTP 码）。
2api：登录后创建 agent 关联模型 → assistant UUID → agent chat LLM（端点待抓，见 core.py TODO）。
"""
from __future__ import annotations

from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.runbear.core import (
    APP_URL,
    AUTH_BASE,
    DEFAULT_MODEL,
    FREE_MODELS,
    REFERRAL_SOURCES,
    RUNBEAR_MODELS,
    SITE_URL,
    TURNSTILE_SITEKEY,
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
class RunbearPlatform(BasePlatform):
    name = "runbear"
    display_name = "Runbear"
    version = "1.0.0"
    # 浏览器驱动（patchright 过 Turnstile）+ 协议路径（signup POST 纯协议但 Turnstile 需浏览器解）。
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers: list[str] = []
    # runbear 邮件确认链接（非 OTP），需 wait_for_link 支持的 provider。
    default_mail_provider = "cfworker"
    # runbear 注册有 Cloudflare Turnstile widget（浏览器内渲染解，纯协议需 solver 传 token）。
    protocol_captcha_order = ("turnstile",)

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # runbear 密码 min_length=8，无大小写/数字/特殊要求，no_common_passwords。
        return password or self._make_random_password(length=14, charset="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")

    def _map_result(self, result: dict[str, Any]) -> RegistrationResult:
        token = str(result.get("access_token") or result.get("token") or result.get("api_key") or "").strip()
        email = str(result.get("email") or "").strip()
        confirmed = bool(result.get("email_confirmed"))
        assistant_uuid = str(result.get("assistant_uuid") or "")
        # 状态：邮箱确认 + 拿到 access_token → REGISTERED；邮箱未确认 → PENDING；无 token → INVALID
        if token and confirmed:
            status = AccountStatus.REGISTERED
        elif confirmed:
            status = AccountStatus.PENDING
        elif token:
            status = AccountStatus.REGISTERED
        else:
            status = AccountStatus.INVALID
        account_overview = {
            "remote_email": email,
            "token_created": bool(token),
            "token_preview": account_preview(token),
            "email_confirmed": confirmed,
            "assistant_uuid": assistant_uuid,
            "chips": [
                item for item in (
                    "邮箱确认" if confirmed else "邮箱未确认",
                    "access_token" if token else "未取到 token",
                    f"assistant={assistant_uuid[:8]}" if assistant_uuid else "",
                ) if item
            ],
        }
        return RegistrationResult(
            email=email,
            password=str(result.get("password") or ""),
            user_id=str(result.get("user_id") or ""),
            token=token,
            status=status,
            extra={
                "api_key": token,
                "ai_api_token": token,
                "access_token": token,
                "assistant_uuid": assistant_uuid,
                "api_base": str(result.get("api_base") or APP_URL),
                "openai_compatible_api_base": "",  # runbear 无原生 OpenAI 兼容，2api 由 services/twoapi/plugins/runbear.py 包装
                "default_free_model": DEFAULT_MODEL,
                "free_models": list(FREE_MODELS),
                "runbear_models": list(RUNBEAR_MODELS),
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "site_url": SITE_URL,
                "auth_base": AUTH_BASE,
                "turnstile_sitekey": TURNSTILE_SITEKEY,
                "referral_source": str(result.get("referral_source") or ""),
                "email_confirmed": confirmed,
                "signup_result": _json_safe(result.get("signup_result") or {}),
                "confirm_result": _json_safe(result.get("confirm_result") or {}),
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.runbear.protocol_mailbox",
                fromlist=["RunbearProtocolMailboxWorker"],
            ).RunbearProtocolMailboxWorker(
                proxy=ctx.proxy,
                timeout=resolve_timeout(ctx.extra or {}, ("runbear_timeout", "mail_otp_timeout", "browser_oauth_timeout"), 300),
                log_fn=ctx.log,
                captcha_solver=artifacts.captcha_solver,
                referral_source=str((ctx.extra or {}).get("runbear_referral_source") or "Search engine"),
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                first_name=str((ctx.extra or {}).get("runbear_first_name") or "Auto"),
                last_name=str((ctx.extra or {}).get("runbear_last_name") or "Register"),
                link_callback=artifacts.otp_callback,
                mailbox=getattr(ctx.platform, "mailbox", None),
                mailbox_account=getattr(ctx.identity, "mailbox_account", None),
            ),
            otp_spec=OtpSpec(
                keyword=str(extra.get("runbear_link_keyword") or "Runbear"),
                timeout=resolve_timeout(extra, ("runbear_link_timeout", "mail_otp_timeout"), 240),
                code_pattern=r".",  # runbear 是确认链接非 OTP，用 link_callback 提取链接
                wait_message="等待 Runbear 邮箱确认链接 (keyword='Runbear')...",
                success_label="Runbear 确认链接",
            ),
            use_captcha=True,  # Turnstile widget
            preflight=None,
        )

    def check_valid(self, account: Account) -> bool:
        """校验账号：有 access_token 则查 login_state 验证。"""
        from platforms.runbear.core import RunbearClient

        client = RunbearClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        token = str((account.extra or {}).get("access_token") or (account.extra or {}).get("api_key") or account.token or "")
        if not token:
            return False
        try:
            state = client.get_login_state()
            return bool(state.get("is_authenticated"))
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        return [{"id": "export_access_token", "label": "导出 Access Token", "params": []}]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "export_access_token":
            raise NotImplementedError(f"Runbear 不支持操作: {action_id}")
        token = str(account.token or dict(account.extra or {}).get("access_token") or "").strip()
        if not token:
            return {"ok": False, "error": "该账号没有 access_token"}
        from pathlib import Path
        output_dir = Path(__file__).resolve().parents[2] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "runbear_keys.txt"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{account.email}|{token}\n")
        return {"ok": True, "data": {"path": str(path), "email": account.email, "key_preview": account_preview(token)}}
