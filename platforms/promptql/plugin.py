"""PromptQL 平台插件。

注册链路（浏览器驱动，patchright + 住宅代理 + Turnstile widget + OTP 邮件码）：
- 打开 https://prompt.ql.app/login
- 填 email → Cloudflare Turnstile widget 自动解（sitekey 0x4AAAAAADsy_TOiX96NjTFT，浏览器内渲染）
- click "Continue with email"/"Send code" → SPA POST auth.pro.ql.app/otp/send{email,captcha_token} → {nonce}
- mailbox.wait_for_code(keyword="PromptQL") 收 6 位 OTP（任意邮箱都发，6 位数字）
- 填 OTP 提交 → SPA POST auth.pro.ql.app/otp/verify{email,otp,nonce} → 200 set session cookie
- 读浏览器 session cookie 作 access_token（Hasura OIDC session）

收信：默认 cfworker/moemail（支持 wait_for_code 收 OTP 码，非确认链接）。
2api：登录后 Playground V2 GraphQL thread LLM（CreateEmptyThread→SendThreadMessage→
getThreadEventsStream WS），端点待登录抓包（见 platforms/promptql/core.py PromptQLClient.chat TODO）。
"""
from __future__ import annotations

from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register
from platforms.promptql.core import (
    APP_URL,
    AUTH_BASE,
    DEFAULT_MODEL,
    FREE_MODELS,
    PROMPTQL_MODELS,
    SITE_URL,
    TURNSTILE_SITEKEY,
    account_preview,
)


def _json_safe(value: Any, *, _seen: set[int] | None = None) -> Any:
    """把平台结果清洗成可 JSON 序列化结构，避免循环引用阻断保存。"""
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
class PromptQLPlatform(BasePlatform):
    name = "promptql"
    display_name = "PromptQL"
    version = "1.0.0"
    # 浏览器驱动（patchright 过 Cloudflare WAF + Turnstile widget）+ 协议路径（OTP 纯协议但 Turnstile 需浏览器解）。
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox"]
    supported_oauth_providers: list[str] = []
    # promptql 是 OTP 邮件码（非确认链接），需 wait_for_code 支持的 provider。
    default_mail_provider = "cfworker"
    # promptql 注册有 Cloudflare Turnstile widget（浏览器内渲染解，纯协议需 solver 传 token）。
    protocol_captcha_order = ("turnstile",)

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # promptql 是 OTP 登录无密码字段；返回 None，worker 不使用 password。
        return None

    def _map_result(self, result: dict[str, Any]) -> RegistrationResult:
        token = str(
            result.get("access_token")
            or result.get("session_cookie")
            or result.get("token")
            or ""
        ).strip()
        email = str(result.get("email") or "").strip()
        confirmed = bool(result.get("email_confirmed"))
        project_id = str(result.get("project_id") or "")
        build_fqdn = str(result.get("build_fqdn") or "")
        # 状态：拿到 session cookie + OTP 已验证 → REGISTERED；仅 OTP 验证 → PENDING；无 token → INVALID
        if token and confirmed:
            status = AccountStatus.REGISTERED
        elif token:
            status = AccountStatus.REGISTERED
        elif confirmed:
            status = AccountStatus.PENDING
        else:
            status = AccountStatus.INVALID
        account_overview = {
            "remote_email": email,
            "token_created": bool(token),
            "token_preview": account_preview(token),
            "email_confirmed": confirmed,
            "project_id": project_id,
            "build_fqdn": build_fqdn,
            "chips": [
                item for item in (
                    "OTP 已验证" if confirmed else "OTP 未验证",
                    "session cookie" if token else "未取到 token",
                    f"project={project_id[:8]}" if project_id else "",
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
                "session_cookie": token,
                "api_base": str(result.get("api_base") or APP_URL),
                "openai_compatible_api_base": "",  # promptql 无原生 OpenAI 兼容，2api 由 services/twoapi/plugins/promptql.py 包装
                "default_free_model": DEFAULT_MODEL,
                "free_models": list(FREE_MODELS),
                "promptql_models": list(PROMPTQL_MODELS),
                "auth_header": "Cookie",
                "auth_scheme": "session",
                "site_url": SITE_URL,
                "auth_base": AUTH_BASE,
                "turnstile_sitekey": TURNSTILE_SITEKEY,
                "email_confirmed": confirmed,
                "project_id": project_id,
                "build_fqdn": build_fqdn,
                "signup_result": _json_safe(result.get("signup_result") or {}),
                "verify_result": _json_safe(result.get("verify_result") or {}),
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result({**dict(result or {}), "password": ctx.password or ""}),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.promptql.protocol_mailbox",
                fromlist=["PromptQLProtocolMailboxWorker"],
            ).PromptQLProtocolMailboxWorker(
                proxy=ctx.proxy,
                timeout=resolve_timeout(ctx.extra or {}, ("promptql_timeout", "mail_otp_timeout", "browser_oauth_timeout"), 300),
                log_fn=ctx.log,
                captcha_solver=artifacts.captcha_solver,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
                mailbox=getattr(ctx.platform, "mailbox", None),
                mailbox_account=getattr(ctx.identity, "mailbox_account", None),
            ),
            otp_spec=OtpSpec(
                keyword=str(extra.get("promptql_otp_keyword") or "PromptQL"),
                timeout=resolve_timeout(extra, ("promptql_otp_timeout", "mail_otp_timeout"), 240),
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",  # 6 位数字 OTP（非确认链接）
                wait_message="等待 PromptQL 邮箱 OTP 验证码 (keyword='PromptQL')...",
                success_label="PromptQL OTP",
            ),
            use_captcha=True,  # Turnstile widget
            preflight=None,
        )

    def check_valid(self, account: Account) -> bool:
        """校验账号：promptql 暂无独立 token 校验端点（session cookie 校验需登录态 GraphQL），
        基本检查 token 存在即视为有效；TODO 登录抓包后补 /me 或 GraphQL 校验。
        """
        token = str(
            (account.extra or {}).get("access_token")
            or (account.extra or {}).get("session_cookie")
            or account.token
            or ""
        )
        return bool(token)

    def get_platform_actions(self) -> list:
        return [{"id": "export_access_token", "label": "导出 Access Token (session cookie)", "params": []}]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "export_access_token":
            raise NotImplementedError(f"PromptQL 不支持操作: {action_id}")
        token = str(account.token or dict(account.extra or {}).get("access_token") or "").strip()
        if not token:
            return {"ok": False, "error": "该账号没有 session cookie / access_token"}
        from pathlib import Path
        output_dir = Path(__file__).resolve().parents[2] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "promptql_keys.txt"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{account.email}|{token}\n")
        return {"ok": True, "data": {"path": str(path), "email": account.email, "key_preview": account_preview(token)}}
