"""Vellum (Vellum Assistant) 平台插件。

注册：WorkOS AuthKit 浏览器流程（headless，走 resin）。
- 邮箱身份用 cfworker / pangxie888.com（过 WorkOS Radar，见记忆 vellum-registration-workos）。
- 手机验证用美国号豪猪项目（+1），WorkOS 不向 +86 发码。
- 邀请码默认 H5QJRV，可用上一个号的 own_invite_code 链式传入 vellum_invite_code。
API Key 提取：账号需进入 Vellum Assistant 容器用 CES/bun 解密（见记忆 vellum-apikey-extraction），
注册落地后此处仅捕获会话；APIKey 提取以平台动作/后续步骤补全。
"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register

SITE_URL = "https://www.vellum.ai"
DEFAULT_INVITE_CODE = "H5QJRV"


def _resolve_phone_country_code(extra: dict) -> str:
    """解析 Vellum 手机表单国家码。

    优先显式 vellum 配置；其次回退到接码 provider 的国家码（api.cc 的 apicc_country_code
    或通用 phone_country_code），避免「provider 设了 +1 但表单仍用默认 +86」的脱节。
    归一化为带 + 前缀（"1" -> "+1"）。
    """
    extra = extra or {}
    for key in ("vellum_phone_country_code", "apicc_country_code", "phone_country_code"):
        value = str(extra.get(key) or "").strip()
        if value:
            return "+" + value.lstrip("+") if not value.startswith("+") else value
    return "+86"



@register
class VellumPlatform(BasePlatform):
    name = "vellum"
    display_name = "Vellum Assistant"
    version = "1.0.0"
    supported_executors = ["headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google"]
    default_mail_provider = "cfworker"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        config = config or RegisterConfig()
        # 运行时列举/执行平台动作时以默认 protocol 实例化本类；本平台仅支持浏览器注册，
        # 故把 protocol 降级为 headless 以通过 BasePlatform 的执行器校验（真实注册仍显式传 headless/headed）。
        if getattr(config, "executor_type", "") == "protocol":
            import dataclasses
            config = dataclasses.replace(config, executor_type="headless")
        super().__init__(config)
        self.mailbox = mailbox

    def _resolve_google_password(self, ctx) -> str:
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        credentials = provider_account.get("credentials") if isinstance(provider_account, dict) else None
        if isinstance(credentials, dict) and credentials.get("password"):
            return str(credentials.get("password") or "").strip()
        return str(ctx.password or ctx.extra.get("google_password") or "").strip()

    def _resolve_totp_secret(self, ctx) -> str:
        pool_totp = ""
        mailbox_account = getattr(ctx.identity, "mailbox_account", None)
        mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {}) if mailbox_account else {}
        provider_account = mailbox_extra.get("provider_account") if isinstance(mailbox_extra, dict) else None
        metadata = provider_account.get("metadata") if isinstance(provider_account, dict) else None
        if isinstance(metadata, dict) and metadata.get("totp_secret"):
            pool_totp = metadata.get("totp_secret")
        if pool_totp:
            return str(pool_totp).strip()
        return str(ctx.extra.get("totp_secret") or ctx.extra.get("google_totp_secret") or "").strip()

    def _run_oauth(self, ctx) -> dict:
        from platforms.vellum.browser_oauth import register_with_browser_oauth

        extra = dict(ctx.extra or {})
        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=getattr(ctx.identity, "oauth_provider", "google") or "google",
            email_hint=getattr(ctx.identity, "email", "") or extra.get("oauth_email_hint", ""),
            google_password=self._resolve_google_password(ctx),
            totp_secret=self._resolve_totp_secret(ctx),
            invite_code=str(extra.get("vellum_invite_code") or DEFAULT_INVITE_CODE).strip() or DEFAULT_INVITE_CODE,
            timeout=resolve_timeout(extra, ("vellum_oauth_timeout", "browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=getattr(ctx.identity, "chrome_user_data_dir", "") or str(extra.get("vellum_chrome_user_data_dir") or ""),
            chrome_cdp_url=getattr(ctx.identity, "chrome_cdp_url", "") or str(extra.get("vellum_cdp_url") or ""),
            use_camoufox=str(extra.get("vellum_oauth_use_camoufox", "true")).strip().lower() in {"1", "true", "yes", "on"},
            cancel_token=getattr(ctx, "cancel_token", None),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(oauth_runner=self._run_oauth, result_mapper=lambda ctx, result: self._map_result(ctx, result))

    def _map_result(self, ctx, result: dict) -> RegistrationResult:
        result = dict(result or {})
        api_key = str(result.get("api_key") or "").strip()
        return RegistrationResult(
            email=str(result.get("email") or ctx.identity.email or "").strip(),
            password=str(result.get("password") or ctx.password or "").strip(),
            user_id=str(result.get("user_id") or "").strip(),
            token=api_key,
            status=AccountStatus.REGISTERED if result.get("phone_verified") else AccountStatus.PENDING,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "assistant_api_key": api_key,
                "platform_assistant_id": str(result.get("platform_assistant_id") or "").strip(),
                "webhook_secret": str(result.get("webhook_secret") or "").strip(),
                "platform_user_id": str(result.get("platform_user_id") or "").strip(),
                "platform_organization_id": str(result.get("platform_organization_id") or "").strip(),
                "local_assistant_id": str(result.get("local_assistant_id") or "").strip(),
                "client_installation_id": str(result.get("client_installation_id") or "").strip(),
                "runtime_assistant_id": str(result.get("runtime_assistant_id") or "").strip(),
                "balance_usd": str(result.get("balance_usd") or "").strip(),
                "own_invite_code": str(result.get("own_invite_code") or "").strip(),
                "invite_code_used": str(ctx.extra.get("vellum_invite_code") or DEFAULT_INVITE_CODE).strip(),
                "landed_url": str(result.get("landed_url") or ""),
                "phone_verified": bool(result.get("phone_verified")),
                "resin_ip": str(result.get("resin_ip") or ""),
                "cookies": dict(result.get("cookies") or {}),
                "site_url": SITE_URL,
            },
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=self._map_result,
            oauth_runner=self._run_oauth,
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed", "headless", "cdp_protocol")),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.vellum.browser_register",
                fromlist=["VellumBrowserRegister"],
                ).VellumBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                # 外层任务已经解析出显式代理时，必须优先使用该代理；
                # 否则 Vellum 内部 resin 轮换会覆盖 Webshare/自定义代理，并卡在 IP 探测阶段。
                resin_rotate=not bool(str(ctx.proxy or "").strip()),
                otp_callback=artifacts.otp_callback,
                phone_callback=artifacts.phone_callback,
                invite_code=str(ctx.extra.get("vellum_invite_code") or DEFAULT_INVITE_CODE).strip() or DEFAULT_INVITE_CODE,
                country_code=_resolve_phone_country_code(ctx.extra),
                nav_attempts=int(ctx.extra.get("vellum_nav_attempts", 6) or 6),
                phone_wait_attempts=int(ctx.extra.get("vellum_phone_wait_attempts", 20) or 20),
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                keyword="",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="等待 Vellum 邮箱验证码...",
                success_label="Vellum 邮箱验证码",
            ),
            use_captcha_for_mailbox=False,
        )

    def check_valid(self, account: Account) -> bool:
        # TODO: 账号有效性以 Vellum API Key / 会话验证；APIKey 提取落地后完善（见 vellum-apikey-extraction）。
        extra = dict(account.extra or {})
        return bool(str(account.token or extra.get("api_key") or "").strip()) or bool(extra.get("phone_verified"))

    def get_platform_actions(self) -> list:
        return [
            {"id": "query_balance", "label": "查询余额", "params": []},
            {"id": "extract_credentials", "label": "提取API Key/凭据", "params": []},
            {"id": "show_own_invite", "label": "查看本号邀请码", "params": []},
        ]

    def _account_login(self, account: Account) -> tuple[str, str]:
        extra = dict(account.extra or {})
        email = str(account.email or extra.get("email") or "").strip()
        password = str(account.password or extra.get("password") or "").strip()
        return email, password

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "query_balance":
            from datetime import datetime
            from platforms.vellum.session_api import run_session
            email, password = self._account_login(account)
            if not email or not password:
                return {"ok": False, "error": "缺少邮箱或密码，无法登录查询余额"}
            res = run_session(email, password, provision_key=False, log=self.log)
            if not res.get("ok"):
                return {"ok": False, "error": res.get("error") or "查询余额失败"}
            bal = dict(res.get("balance") or {})
            return {"ok": True, "data": {
                "account_overview": {
                    "balance_usd": str(res.get("balance_usd") or ""),
                    "balance_settled_usd": str(bal.get("settled_balance_usd") or ""),
                    "balance_pending_usd": str(bal.get("pending_compute_usd") or ""),
                    "balance_checked_at": datetime.now().isoformat(timespec="seconds"),
                },
                "balance_usd": str(res.get("balance_usd") or ""),
            }}
        if action_id == "extract_credentials":
            from platforms.vellum.session_api import run_session
            email, password = self._account_login(account)
            if not email or not password:
                return {"ok": False, "error": "缺少邮箱或密码，无法登录提取凭据"}
            # 已签发过的号(extra 存有 runtime/installation id)走 reprovision 轮换；否则首次 ensure-registration 签发。
            extra = dict(account.extra or {})
            cid = str(extra.get("client_installation_id") or "").strip()
            rid = str(extra.get("runtime_assistant_id") or "").strip()
            reprovision = bool(cid and rid)
            res = run_session(email, password, provision_key=True,
                              client_installation_id=cid, runtime_assistant_id=rid,
                              reprovision=reprovision, log=self.log)
            if not res.get("ok") or not res.get("assistant_api_key"):
                return {"ok": False, "error": res.get("error") or f"签发凭据失败(status={res.get('provision_status')} code={res.get('provision_code')})"}
            return {"ok": True, "data": {
                "credential_updates": {
                    "assistant_api_key": str(res.get("assistant_api_key") or ""),
                    "platform_assistant_id": str(res.get("platform_assistant_id") or ""),
                    "webhook_secret": str(res.get("webhook_secret") or ""),
                    "platform_user_id": str(res.get("platform_user_id") or ""),
                },
                "account_overview": {
                    "balance_usd": str(res.get("balance_usd") or ""),
                    "platform_organization_id": str(res.get("platform_organization_id") or ""),
                    "local_assistant_id": str(res.get("local_assistant_id") or ""),
                    "client_installation_id": str(res.get("client_installation_id") or ""),
                    "runtime_assistant_id": str(res.get("runtime_assistant_id") or ""),
                },
            }}
        if action_id == "show_own_invite":
            code = str(dict(account.extra or {}).get("own_invite_code") or "").strip()
            if not code:
                return {"ok": False, "error": "该账号未捕获到邀请码（需注册落地后从分享页提取）"}
            return {"ok": True, "data": {"invite_code": code, "invite_url": f"{SITE_URL}/r/{code}"}}
        raise NotImplementedError(f"Vellum 不支持操作: {action_id}")
