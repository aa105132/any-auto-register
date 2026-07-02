"""Hex 平台插件。

注册链路（浏览器驱动，patchright + 住宅代理 + magic link 确认链接，无密码无 captcha）：
- patchright 打开 https://app.hex.tech/signup，填 email + name → click 发 magic link
  → POST /auth/magic/signup{email,name} → {success}（app.hex.tech 自有 magic-link，见 core.py）。
- mailbox.wait_for_link(keyword="Hex") 收 magic link 邮件链接（非 OTP）。
- patchright navigate magic link → /auth/magic/callback → session cookie。
- 拿 session cookie 作 token（无密码无 captcha，email-only magic link）。

收信：默认 cfworker/moemail（支持 wait_for_link 提取 magic link，非 OTP 码）。
2api：GraphQL APQ thread LLM（待登录抓 variables，见 core.py HexClient.chat TODO）。
"""
from __future__ import annotations

from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.hex.core import (
    APP_URL,
    DEFAULT_MODEL,
    FREE_MODELS,
    GRAPHQL_API,
    HEX_MODELS,
    SITE_URL,
    account_preview,
)


def _json_safe(value: Any, *, _seen: set[int] | None = None) -> Any:
    """把平台结果/cookie 清洗成可 JSON 序列化结构，避免循环引用阻断保存。"""
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
class HexPlatform(BasePlatform):
    name = "hex"
    display_name = "Hex"
    version = "1.0.0"
    # 浏览器驱动（patchright 过 Cloudflare WAF）+ 协议路径（magic-link signup 纯协议 POST）。
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers: list[str] = []
    # hex 是 magic link 确认链接（非 OTP），需 wait_for_link 支持的 provider。
    default_mail_provider = "cfworker"
    # hex 注册无 captcha widget（app.hex.tech 自有 magic-link，185 bundle 确认）。
    protocol_captcha_order = ()

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # hex magic-link email-only，无密码。返回空串占位（worker 不消费 password）。
        return ""

    def _map_result(self, result: dict[str, Any]) -> RegistrationResult:
        token = str(result.get("token") or result.get("session_token") or "").strip()
        email = str(result.get("email") or "").strip()
        session_cookies = dict(result.get("session_cookies") or {}) if isinstance(result.get("session_cookies"), dict) else {}
        confirmed = bool(result.get("email_confirmed"))
        # 状态：拿到 session cookie + magic link 确认 → REGISTERED；未确认 → PENDING；无 token → INVALID
        if token and confirmed:
            status = AccountStatus.REGISTERED
        elif token:
            status = AccountStatus.REGISTERED  # session cookie 拿到即算注册成功（magic link 回调即确认）
        elif confirmed:
            status = AccountStatus.PENDING
        else:
            status = AccountStatus.INVALID
        account_overview = {
            "remote_email": email,
            "token_created": bool(token),
            "token_preview": account_preview(token),
            "email_confirmed": confirmed,
            "session_cookie_count": len(session_cookies),
            "chips": [
                item for item in (
                    "邮箱确认" if confirmed else "邮箱未确认",
                    "session cookie" if token else "未取到 session",
                    f"cookies={len(session_cookies)}" if session_cookies else "",
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
                "session_token": token,
                "session_cookies": _json_safe(session_cookies),
                "api_base": APP_URL,
                "graphql_api": GRAPHQL_API,
                # hex 无原生 OpenAI 兼容，2api 由 services/twoapi/plugins/hex.py 包装 GraphQL thread LLM
                "openai_compatible_api_base": "",
                "default_free_model": DEFAULT_MODEL,
                "free_models": list(FREE_MODELS),
                "hex_models": list(HEX_MODELS),
                "auth_header": "Cookie",
                "auth_scheme": "cookie",
                "site_url": SITE_URL,
                "email_confirmed": confirmed,
                "callback_result": _json_safe(result.get("callback_result") or {}),
                "signup_result": _json_safe(result.get("signup_result") or {}),
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.hex.protocol_mailbox",
                fromlist=["HexProtocolMailboxWorker"],
            ).HexProtocolMailboxWorker(
                proxy=ctx.proxy,
                timeout=resolve_timeout(ctx.extra or {}, ("hex_timeout", "mail_otp_timeout", "browser_oauth_timeout"), 300),
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                name=str((ctx.extra or {}).get("hex_name") or "Auto Register"),
                link_callback=artifacts.otp_callback,  # 复用 otp_callback 槽传 link 提取器（见 worker）
                mailbox=getattr(ctx.platform, "mailbox", None),
                mailbox_account=getattr(ctx.identity, "mailbox_account", None),
            ),
            otp_spec=OtpSpec(
                keyword=str(extra.get("hex_link_keyword") or "Hex"),
                timeout=resolve_timeout(extra, ("hex_link_timeout", "mail_otp_timeout"), 240),
                code_pattern=r".",  # hex 是 magic link 非 OTP，用 link_callback 提取链接
                wait_message="等待 Hex magic link 邮件链接 (keyword='Hex')...",
                success_label="Hex magic link",
            ),
            use_captcha=False,
            preflight=None,
        )

    def check_valid(self, account: Account) -> bool:
        """校验账号：有 session cookie token 即视为有效（TODO：真实 GraphQL 验证待 chat 实现）。"""
        token = str((account.extra or {}).get("session_token") or (account.extra or {}).get("api_key") or account.token or "")
        return bool(token)

    def get_platform_actions(self) -> list:
        return [{"id": "export_session", "label": "导出 Session Cookie", "params": []}]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "export_session":
            raise NotImplementedError(f"Hex 不支持操作: {action_id}")
        token = str(account.token or dict(account.extra or {}).get("session_token") or "").strip()
        if not token:
            return {"ok": False, "error": "该账号没有 session cookie"}
        from pathlib import Path
        output_dir = Path(__file__).resolve().parents[2] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "hex_keys.txt"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{account.email}|{token}\n")
        return {"ok": True, "data": {"path": str(path), "email": account.email, "key_preview": account_preview(token)}}
