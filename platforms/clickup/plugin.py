"""ClickUp 平台插件。

注册链路（浏览器驱动，patchright + 住宅代理 + reCAPTCHA v3）：
- 打开 https://app.clickup.com/signup
- 填 username/email/password → reCAPTCHA v3 浏览器内自动解
  （sitekey 6Lf6D0YoAAAAAEgVBxwLwC_gxFaDBPyYZX19ocU1，action=signup，
  页面 JS 在 submit 时调 grecaptcha.execute 自动出 token，无需外部 solver）
- click "Sign up" → POST /user/v1/user{username,email,password,recaptchaV3,...}
  → 自动 login → session cookie → 跳 dashboard
- 无邮件确认（直接登录），不需要 wait_for_link/wait_for_code
- 拿 session cookie + workspace JWT（cu_jwt）作 token

收信：默认 cfworker（仍需邮箱填表，但不收 OTP/确认链接）。
2api：ClickUp Brain SSE（端点待 Playwright 抓动态 URL，见 core.py ClickUpClient.chat TODO）。
"""
from __future__ import annotations

from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.clickup.core import (
    APP_URL,
    CLICKUP_MODELS,
    DEFAULT_MODEL,
    FREE_MODELS,
    RECAPTCHA_V3_SITEKEY,
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
class ClickUpPlatform(BasePlatform):
    name = "clickup"
    display_name = "ClickUp"
    version = "1.0.0"
    # 浏览器驱动（patchright 过 reCAPTCHA v3）+ 协议路径（需浏览器解 recaptcha v3 token）。
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers: list[str] = []
    # clickup 仍需邮箱填注册表单，但无邮件确认（直接登录），不收 OTP/确认链接。
    default_mail_provider = "cfworker"
    # reCAPTCHA v3 浏览器内自动解（grecaptcha.execute action=signup），纯协议需 solver 传 token。
    protocol_captcha_order = ("recaptcha_v3",)

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # clickup 密码要求 8+，建议 14 混合（含大小写+数字+符号）。
        return password or self._make_random_password(
            length=14,
            charset="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$",
        )

    def _map_result(self, result: dict[str, Any]) -> RegistrationResult:
        # token 优先 cu_jwt（workspace JWT），其次 session cookie 值。
        cu_jwt = str(result.get("cu_jwt") or "").strip()
        session_cookie = str(result.get("session_cookie") or "").strip()
        token = cu_jwt or session_cookie
        email = str(result.get("email") or "").strip()
        workspace_id = str(result.get("workspace_id") or "").strip()
        cookies = dict(result.get("cookies") or {})
        # 状态：拿到 session/cu_jwt → REGISTERED（clickup 无邮件确认，直接登录即成功）。
        if token:
            status = AccountStatus.REGISTERED
        else:
            status = AccountStatus.INVALID
        account_overview = {
            "remote_email": email,
            "token_created": bool(token),
            "token_preview": account_preview(token),
            "cu_jwt": bool(cu_jwt),
            "session_cookie": bool(session_cookie),
            "workspace_id": workspace_id,
            "chips": [
                item for item in (
                    "邮箱注册" if token else "未取到 token",
                    "cu_jwt" if cu_jwt else ("session" if session_cookie else ""),
                    f"ws={workspace_id[:8]}" if workspace_id else "",
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
                "cu_jwt": cu_jwt,
                "session_cookie": session_cookie,
                "workspace_id": workspace_id,
                "cookies": _json_safe(cookies),
                "api_base": str(result.get("api_base") or APP_URL),
                "openai_compatible_api_base": "",  # clickup 无原生 OpenAI 兼容，2api 由 services/twoapi/plugins/clickup.py 包装
                "default_free_model": DEFAULT_MODEL,
                "free_models": list(FREE_MODELS),
                "clickup_models": list(CLICKUP_MODELS),
                "auth_header": "Authorization",
                "auth_scheme": "Bearer",
                "site_url": SITE_URL,
                "recaptcha_v3_sitekey": RECAPTCHA_V3_SITEKEY,
                "signup_result": _json_safe(result.get("signup_result") or {}),
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.clickup.protocol_mailbox",
                fromlist=["ClickUpProtocolMailboxWorker"],
            ).ClickUpProtocolMailboxWorker(
                proxy=ctx.proxy,
                timeout=resolve_timeout(ctx.extra or {}, ("clickup_timeout", "mail_otp_timeout", "browser_oauth_timeout"), 300),
                log_fn=ctx.log,
                captcha_solver=artifacts.captcha_solver,
                username=str((ctx.extra or {}).get("clickup_username") or ""),
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                username=str((ctx.extra or {}).get("clickup_username") or ""),
                mailbox=getattr(ctx.platform, "mailbox", None),
                mailbox_account=getattr(ctx.identity, "mailbox_account", None),
            ),
            otp_spec=OtpSpec(
                keyword=str(extra.get("clickup_link_keyword") or "ClickUp"),
                timeout=resolve_timeout(extra, ("clickup_otp_timeout", "mail_otp_timeout"), 60),
                code_pattern=r".",  # clickup 无邮件确认，OTP 兜底（实际不收 OTP）
                wait_message="ClickUp 无邮件确认，跳过 OTP 等待...",
                success_label="ClickUp 直接登录",
            ),
            use_captcha=True,  # reCAPTCHA v3（浏览器内自动解）
            preflight=None,
        )

    def check_valid(self, account: Account) -> bool:
        """校验账号：有 session cookie/cu_jwt 则尝试登录态验证。"""
        from platforms.clickup.core import ClickUpClient

        client = ClickUpClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        token = str((account.extra or {}).get("cu_jwt") or (account.extra or {}).get("api_key") or account.token or "")
        if not token:
            return False
        # clickup 无轻量 token 校验端点，暂以有 token 视为有效（TODO 抓 user/me 验证）。
        try:
            return bool(token)
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        return [{"id": "export_cu_jwt", "label": "导出 cu_jwt / Session", "params": []}]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "export_cu_jwt":
            raise NotImplementedError(f"ClickUp 不支持操作: {action_id}")
        token = str(account.token or dict(account.extra or {}).get("cu_jwt") or "").strip()
        if not token:
            return {"ok": False, "error": "该账号没有 cu_jwt/session token"}
        from pathlib import Path
        output_dir = Path(__file__).resolve().parents[2] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "clickup_keys.txt"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{account.email}|{token}\n")
        return {"ok": True, "data": {"path": str(path), "email": account.email, "key_preview": account_preview(token)}}
