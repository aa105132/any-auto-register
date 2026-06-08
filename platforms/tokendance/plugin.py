"""Tokendance 平台插件。"""
from __future__ import annotations

from pathlib import Path

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import ProtocolOAuthAdapter, RegistrationCapability, RegistrationContext, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register

SITE_URL = "https://tokendance.space"
API_BASE = "https://tokendance.space/api/v1"


@register
class TokendancePlatform(BasePlatform):
    name = "tokendance"
    display_name = "TokenDance"
    version = "1.0.0"
    supported_executors = ["protocol", "cdp_protocol"]
    supported_identity_modes = ["phone"]
    supported_oauth_providers: list[str] = []
    default_phone_provider = "haozhu"
    default_phone_project_id = "108963"

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _should_require_identity_email(self) -> bool:
        return False

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _ensure_phone_provider_defaults(self) -> None:
        phone_provider = getattr(self, "phone_provider", None)
        if phone_provider is None:
            return
        current_project_id = str(getattr(phone_provider, "project_id", "") or "").strip()
        if not current_project_id:
            try:
                setattr(phone_provider, "project_id", self.default_phone_project_id)
            except Exception:
                pass
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        extra.setdefault("phone_provider", self.default_phone_provider)
        extra.setdefault("haozhu_project_id", self.default_phone_project_id)
        extra.setdefault("phone_project_id", self.default_phone_project_id)
        self.config.extra = extra

    def register(self, email: str = None, password: str = None) -> Account:
        if self._get_identity_provider_name() != "phone":
            return super().register(email=email, password=password)
        self._ensure_phone_provider_defaults()
        resolved_password = self._prepare_registration_password(password)
        identity = self._resolve_identity(email, require_email=False)
        ctx = RegistrationContext(
            platform_name=self.name,
            platform_display_name=self.display_name,
            platform=self,
            identity=identity,
            config=self.config,
            email=email,
            password=resolved_password,
            log_fn=self.log,
        )
        from core.registration import ProtocolOAuthFlow

        result = ProtocolOAuthFlow(self.build_protocol_oauth_adapter()).run(ctx)
        return self._attach_identity_metadata(self._account_from_registration_result(result), identity)

    def _run_protocol_phone(self, ctx) -> dict:
        phone_provider = getattr(self, "phone_provider", None)
        if phone_provider is None:
            raise RuntimeError("Tokendance 手机号注册需要启用并配置 phone_provider")
        self._ensure_phone_provider_defaults()
        from platforms.tokendance.protocol_phone import TokendanceAliyunCaptchaSolver, TokendanceProtocolPhoneWorker

        captcha_solver = None
        if ctx.executor_type == "cdp_protocol" or ctx.extra.get("tokendance_use_cdp_captcha") or ctx.extra.get("use_cdp_captcha"):
            captcha_solver = TokendanceAliyunCaptchaSolver(
                proxy=ctx.proxy,
                log_fn=ctx.log,
                chrome_user_data_dir=str(ctx.extra.get("chrome_user_data_dir") or getattr(ctx.identity, "chrome_user_data_dir", "") or ""),
                chrome_cdp_url=str(ctx.extra.get("chrome_cdp_url") or getattr(ctx.identity, "chrome_cdp_url", "") or ""),
                reuse_existing_cdp=bool(ctx.extra.get("reuse_existing_cdp") or ctx.extra.get("oauth_reuse_existing_cdp")),
                timeout=resolve_timeout(ctx.extra, ("tokendance_captcha_timeout", "captcha_timeout"), 120),
            )

        return TokendanceProtocolPhoneWorker(proxy=ctx.proxy, log_fn=ctx.log).run(
            phone_provider=phone_provider,
            otp_timeout=resolve_timeout(ctx.extra, ("phone_otp_timeout", "haozhu_phone_timeout"), 180),
            poll_interval=resolve_timeout(ctx.extra, ("phone_poll_interval", "haozhu_poll_interval"), 15),
            code_pattern=str(ctx.extra.get("phone_code_pattern") or "").strip() or None,
            key_name=str(ctx.extra.get("tokendance_key_name") or ctx.extra.get("api_key_name") or "auto-register").strip() or "auto-register",
            watcha_invitation_code=str(ctx.extra.get("watcha_invitation_code") or ctx.extra.get("invite_code") or "").strip(),
            watcha_password=str(ctx.extra.get("watcha_password") or ctx.password or "").strip(),
            captcha_solver=captcha_solver,
        )

    def _map_result(self, result: dict) -> RegistrationResult:
        api_key = str(result.get("api_key") or result.get("key") or result.get("token") or "").strip()
        user = dict(result.get("account_info") or result.get("user") or {})
        phone = dict(result.get("phone") or {})
        return RegistrationResult(
            email=str(result.get("email") or user.get("email") or user.get("phone") or phone.get("phone") or "").strip(),
            password="",
            user_id=str(result.get("user_id") or user.get("id") or "").strip(),
            token=api_key,
            status=AccountStatus.REGISTERED if api_key else AccountStatus.PENDING,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_info": dict(result.get("api_key_info") or {}),
                "account_info": user,
                "phone": phone,
                "watcha": dict(result.get("watcha") or {}),
                "cookies": dict(result.get("cookies") or {}),
                "cookie_header": str(result.get("session_cookie") or result.get("cookie_header") or ""),
                "request_trace": list(result.get("request_trace") or []),
                "site_url": SITE_URL,
                "api_base": API_BASE,
            },
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_protocol_phone,
            result_mapper=lambda ctx, result: self._map_result(result),
            capability=RegistrationCapability(oauth_allowed_executor_types=("protocol", "cdp_protocol")),
        )

    def check_valid(self, account: Account) -> bool:
        api_key = str(account.token or dict(account.extra or {}).get("api_key") or "").strip()
        if not api_key:
            return False
        try:
            import requests

            response = requests.get(
                f"{API_BASE}/user/balance",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            return response.status_code == 200
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        return [
            {"id": "export_api_key", "label": "导出 API Key", "params": []},
        ]

    @staticmethod
    def _write_key(api_key: str) -> Path:
        output_dir = Path(__file__).resolve().parents[2] / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        for target in (output_dir / "tokendance_keys.txt", output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with target.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        return output_dir / "tokendance_keys.txt"

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "export_api_key":
            raise NotImplementedError(f"未知操作: {action_id}")
        api_key = str(account.token or dict(account.extra or {}).get("api_key") or "").strip()
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key"}
        path = self._write_key(api_key)
        return {"ok": True, "data": {"path": str(path), "email": account.email}}
