"""Kombai 平台插件。

注册链路（浏览器驱动，patchright + 住宅代理）：
- 生成 OAuth code（base64 随机16字符，与 VS Code 扩展 ko() 一致）。
- 打开 agent.kombai.com/vscode-connect?type=new&code={code} → 跳 PropelAuth 注册页。
- 填 email+password 提交 → POST auth.kombai.com/api/fe/v2/signup → ConfirmEmailRequired。
- mailbox.wait_for_link(keyword="Kombai") 收确认邮件链接 → 浏览器导航链接确认邮箱。
- 确认后 GET api.assistant.app.kombai.com/auth/api-key?code={code}&appMode=Assistant
  （带 x-client-context footprint + x-type:agent + x-extension-version 等 IDE headers）
  → {token, referralCode}，token 即 x-api-key 用于 2api WebSocket 聊天。

收信：默认 cfworker/moemail（支持 wait_for_link 提取确认链接，非 OTP 码）。
footprint：仅 IDE 扩展调推理 API 时双重发送（x-client-context header + clientContext body），
web 注册请求不需要 footprint（已 Playwright 抓包确认）。
"""
from __future__ import annotations

from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.kombai.core import (
    API_BASE,
    DEFAULT_MODEL,
    FREE_MODELS,
    KOMBAI_MODELS,
    KOMBAI_ROUTERS,
    SITE_URL,
    WS_BASE,
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
class KombaiPlatform(BasePlatform):
    name = "kombai"
    display_name = "Kombai"
    version = "1.0.0"
    # 浏览器驱动（patchright 过 Cloudflare WAF）+ 协议路径（OAuth code 换 token 纯协议）。
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers: list[str] = []
    # kombai 邮件确认链接（非 OTP），需 wait_for_link 支持的 provider。
    default_mail_provider = "cfworker"
    # kombai 注册无 captcha widget（PropelAuth，已抓包确认），Cloudflare WAF 在服务端。
    protocol_captcha_order = ()

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # kombai 注册 email+password，密码需 PropelAuth 强度（默认 12+ 混合）。
        return password or self._make_random_password(length=16, charset="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$")

    def _map_result(self, result: dict[str, Any]) -> RegistrationResult:
        token = str(result.get("token") or result.get("api_key") or "").strip()
        email = str(result.get("email") or "").strip()
        subscription = dict(result.get("subscription") or {}) if isinstance(result.get("subscription"), dict) else {}
        credit_info = dict(result.get("credit_info") or {}) if isinstance(result.get("credit_info"), dict) else {}
        confirmed = bool(result.get("email_confirmed"))
        # 状态：拿到 token + 邮箱确认 → REGISTERED；邮箱未确认 → PENDING；无 token → INVALID
        if token and confirmed:
            status = AccountStatus.REGISTERED
        elif token:
            status = AccountStatus.REGISTERED  # token 拿到即算注册成功（邮箱确认是 token 前置）
        elif confirmed:
            status = AccountStatus.PENDING
        else:
            status = AccountStatus.INVALID
        account_overview = {
            "remote_email": email,
            "token_created": bool(token),
            "token_preview": account_preview(token),
            "email_confirmed": confirmed,
            "credits": credit_info.get("credits") or subscription.get("credits"),
            "plan": subscription.get("plan") or credit_info.get("plan"),
            "referral_code": str(result.get("referral_code") or ""),
            "chips": [
                item for item in (
                    "邮箱确认" if confirmed else "邮箱未确认",
                    "x-api-key token" if token else "未取到 token",
                    f"credits={credit_info.get('credits') or '?'}" if credit_info else "",
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
                "x_api_key": token,
                "api_base": API_BASE,
                "ws_base": WS_BASE,
                "openai_compatible_api_base": "",  # kombai 无原生 OpenAI 兼容，2api 由 services/twoapi/plugins/kombai.py WS 包装
                "default_free_model": DEFAULT_MODEL,
                "free_models": list(FREE_MODELS),
                "kombai_routers": list(KOMBAI_ROUTERS),
                "kombai_models": list(KOMBAI_MODELS),
                "auth_header": "x-api-key",
                "auth_scheme": "raw-token",
                "site_url": SITE_URL,
                "auth_code": str(result.get("auth_code") or ""),
                "referral_code": str(result.get("referral_code") or ""),
                "email_confirmed": confirmed,
                "subscription": _json_safe(subscription),
                "credit_info": _json_safe(credit_info),
                "auth_result": _json_safe(result.get("auth_result") or {}),
                "signup_result": _json_safe(result.get("signup_result") or {}),
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.kombai.protocol_mailbox",
                fromlist=["KombaiProtocolMailboxWorker"],
            ).KombaiProtocolMailboxWorker(
                proxy=ctx.proxy,
                timeout=resolve_timeout(ctx.extra or {}, ("kombai_timeout", "mail_otp_timeout", "browser_oauth_timeout"), 300),
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                link_callback=artifacts.otp_callback,  # 复用 otp_callback 槽传 link 提取器（见 worker）
                mailbox=getattr(ctx.platform, "mailbox", None),
                mailbox_account=getattr(ctx.identity, "mailbox_account", None),
            ),
            otp_spec=OtpSpec(
                keyword=str(extra.get("kombai_link_keyword") or "Kombai"),
                timeout=resolve_timeout(extra, ("kombai_link_timeout", "mail_otp_timeout"), 240),
                code_pattern=r".",  # kombai 是确认链接非 OTP，用 link_callback 提取链接
                wait_message="等待 Kombai 邮箱确认链接 (keyword='Kombai')...",
                success_label="Kombai 确认链接",
            ),
            use_captcha=False,
            preflight=None,
        )

    def check_valid(self, account: Account) -> bool:
        """校验账号：有 x-api-key token 则 GET /auth/api-key?apiKey={token} 验证。"""
        from platforms.kombai.core import KombaiClient

        client = KombaiClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        token = str((account.extra or {}).get("api_key") or (account.extra or {}).get("x_api_key") or account.token or "")
        if not token:
            return False
        try:
            return client.verify_token(token)
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        return [{"id": "export_api_key", "label": "导出 API Key (x-api-key)", "params": []}]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "export_api_key":
            raise NotImplementedError(f"Kombai 不支持操作: {action_id}")
        token = str(account.token or dict(account.extra or {}).get("api_key") or "").strip()
        if not token:
            return {"ok": False, "error": "该账号没有 x-api-key token"}
        from pathlib import Path
        output_dir = Path(__file__).resolve().parents[2] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "kombai_keys.txt"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{account.email}|{token}\n")
        return {"ok": True, "data": {"path": str(path), "email": account.email, "key_preview": account_preview(token)}}
