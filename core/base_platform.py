"""平台插件基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
from enum import Enum
import random
import string
import time

from core.registration import BrowserRegistrationFlow, ProtocolMailboxFlow, ProtocolOAuthFlow, RegistrationContext, RegistrationResult


class AccountStatus(str, Enum):
    REGISTERED   = "registered"
    TRIAL        = "trial"
    SUBSCRIBED   = "subscribed"
    EXPIRED      = "expired"
    INVALID      = "invalid"


@dataclass
class Account:
    platform: str
    email: str
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: AccountStatus = AccountStatus.REGISTERED
    trial_end_time: int = 0       # unix timestamp
    extra: dict = field(default_factory=dict)  # 平台自定义字段
    created_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class RegisterConfig:
    """注册任务配置"""
    executor_type: str = "protocol"   # protocol | headless | headed | cdp_protocol
    captcha_solver: str = "auto"  # auto | yescaptcha | 2captcha | local_solver | manual
    proxy: Optional[str] = None
    extra: dict = field(default_factory=dict)


def _json_safe(value: Any, *, _seen: set[int] | None = None) -> Any:
    """把平台结果/身份元数据清洗成可 JSON 序列化结构，避免循环引用阻断保存。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
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


class BasePlatform(ABC):
    # 子类必须定义
    name: str = ""
    display_name: str = ""
    version: str = "1.0.0"
    # 子类声明支持的执行器类型，未列出的自动降级到 protocol
    supported_executors: list = ["protocol", "headless", "headed"]
    supported_identity_modes: list = ["mailbox"]
    supported_oauth_providers: list = []
    protocol_captcha_order: tuple[str, ...] = ("yescaptcha", "2captcha")

    def __init__(self, config: RegisterConfig = None):
        self.config = config or RegisterConfig()
        self._log_fn = print
        if self.config.executor_type not in self.supported_executors:
            raise NotImplementedError(
                f"{self.display_name} 暂不支持 '{self.config.executor_type}' 执行器，"
                f"当前支持: {self.supported_executors}"
            )

    def set_logger(self, logger):
        self._log_fn = logger or print

    def log(self, message: str):
        try:
            self._log_fn(message)
        except UnicodeEncodeError:
            import sys as _sys
            try:
                _sys.stdout.buffer.write(message.encode("utf-8", errors="replace") + b"\n")
                _sys.stdout.buffer.flush()
            except Exception:
                pass

    def _make_random_password(self, length: int = 16, charset: Optional[str] = None) -> str:
        chars = charset or (string.ascii_letters + string.digits + "!@#$")
        return "".join(random.choices(chars, k=length))

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password()

    def _should_require_identity_email(self) -> bool:
        return self._get_identity_provider_name() != "oauth_browser"

    def _browser_registration_label(self, identity) -> str:
        return getattr(identity, "email", "") or "(oauth)"

    def build_browser_registration_adapter(self):
        return None

    def build_protocol_mailbox_adapter(self):
        return None

    def build_protocol_oauth_adapter(self):
        return None

    def _should_use_browser_registration_flow(self, identity) -> bool:
        return (self.config.executor_type or "") in ("headless", "headed")

    def _account_from_registration_result(self, result: RegistrationResult) -> Account:
        status = result.status or AccountStatus.REGISTERED
        if isinstance(status, str):
            try:
                status = AccountStatus(status)
            except Exception:
                status = AccountStatus.REGISTERED
        return Account(
            platform=self.name,
            email=result.email,
            password=result.password,
            user_id=result.user_id,
            region=result.region,
            token=result.token,
            status=status,
            trial_end_time=result.trial_end_time,
            extra=dict(result.extra or {}),
        )

    def register(self, email: str = None, password: str = None) -> Account:
        resolved_password = self._prepare_registration_password(password)
        identity = self._resolve_identity(email, require_email=self._should_require_identity_email())
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

        if self._should_use_browser_registration_flow(identity):
            self.log(f"使用浏览器模式注册: {self._browser_registration_label(identity)}")
            adapter = self.build_browser_registration_adapter()
            if adapter is None:
                raise NotImplementedError(f"{self.display_name} 未实现浏览器注册适配器")
            result = BrowserRegistrationFlow(adapter).run(ctx)
            return self._attach_identity_metadata(self._account_from_registration_result(result), identity)

        if getattr(identity, "identity_provider", "") == "oauth_browser":
            adapter = self.build_protocol_oauth_adapter()
            if adapter is None:
                raise RuntimeError(
                    f"{self.display_name} 当前仅浏览器模式支持 oauth_browser，请使用受支持的浏览器执行器"
                )
            result = ProtocolOAuthFlow(adapter).run(ctx)
            return self._attach_identity_metadata(self._account_from_registration_result(result), identity)

        self.log(f"邮箱: {identity.email}")
        adapter = self.build_protocol_mailbox_adapter()
        if adapter is None:
            raise NotImplementedError(f"{self.display_name} 未实现协议邮箱注册适配器")
        result = ProtocolMailboxFlow(adapter).run(ctx)
        return self._attach_identity_metadata(self._account_from_registration_result(result), identity)

    @abstractmethod
    def check_valid(self, account: Account) -> bool:
        """检测账号是否有效"""
        ...

    def get_trial_url(self, account: Account) -> Optional[str]:
        """生成试用激活链接（可选实现）"""
        return None

    def get_platform_actions(self) -> list:
        """
        返回平台支持的额外操作列表，每项格式:
        {"id": str, "label": str, "params": [{"key": str, "label": str, "type": str}]}
        """
        return []

    def get_desktop_state(self) -> dict:
        return {
            "available": False,
            "message": f"{self.display_name or self.name} 暂未提供桌面应用状态探测",
        }

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        """
        执行平台特定操作，返回 {"ok": bool, "data": any, "error": str}
        """
        raise NotImplementedError(f"平台 {self.name} 不支持操作: {action_id}")

    def get_quota(self, account: Account) -> dict:
        """查询账号配额（可选实现）"""
        return {}

    def _make_executor(self):
        """根据 config 创建执行器"""
        from .executors.protocol import ProtocolExecutor
        t = self.config.executor_type
        if t == "protocol":
            return ProtocolExecutor(proxy=self.config.proxy)
        elif t == "headless":
            from .executors.playwright import PlaywrightExecutor
            return PlaywrightExecutor(proxy=self.config.proxy, headless=True)
        elif t == "headed":
            from .executors.playwright import PlaywrightExecutor
            return PlaywrightExecutor(proxy=self.config.proxy, headless=False)
        raise ValueError(f"未知执行器类型: {t}")

    def _make_captcha(self, **kwargs):
        """根据 config 创建验证码解决器"""
        from .base_captcha import create_captcha_solver

        return create_captcha_solver(self._resolve_captcha_solver(), self.config.extra)

    def _has_configured_captcha(self, solver_name: str) -> bool:
        from .base_captcha import has_captcha_configured

        return has_captcha_configured(solver_name, self.config.extra)

    def _resolve_captcha_solver(self) -> str:
        requested = str(self.config.captcha_solver or "").strip().lower()
        if requested and requested not in {"", "auto"}:
            if not self._has_configured_captcha(requested):
                raise RuntimeError(f"{requested} 未配置，无法创建验证码解决器")
            return requested

        if self.config.executor_type in {"headless", "headed"}:
            return "local_solver"

        if self.config.executor_type == "cdp_protocol":
            return "cdp_turnstile"

        protocol_order = list(self.protocol_captcha_order)
        try:
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            protocol_order = ProviderSettingsRepository().get_enabled_captcha_order(protocol_order)
        except Exception:
            protocol_order = list(self.protocol_captcha_order)

        for solver_name in protocol_order:
            if self._has_configured_captcha(solver_name):
                return solver_name

        raise RuntimeError("协议模式未配置可用的远程验证码服务，请先启用并配置至少一个验证码 provider")

    def _get_identity_provider_name(self) -> str:
        from .base_identity import normalize_identity_provider
        return normalize_identity_provider(self.config.extra.get("identity_provider", "mailbox"))

    def _get_identity_provider(self):
        from .base_identity import create_identity_provider

        mode = self._get_identity_provider_name()
        if mode not in self.supported_identity_modes:
            raise NotImplementedError(
                f"{self.display_name} 暂不支持 identity_provider='{mode}'，"
                f"当前支持: {self.supported_identity_modes}"
            )
        return create_identity_provider(
            mode,
            mailbox=getattr(self, "mailbox", None),
            extra=self.config.extra,
        )

    def _resolve_identity(self, email: str = None, *, require_email: bool = True):
        identity = self._get_identity_provider().resolve(email)
        self._last_identity = identity
        if require_email and not identity.email:
            raise ValueError(
                f"{self.display_name} 注册流程未获取到可用邮箱，"
                f"请提供 email 或配置支持的 identity_provider"
            )
        return identity

    def _build_identity_snapshot(self, identity) -> dict:
        snapshot = {
            "identity_provider": getattr(identity, "identity_provider", "") or "",
            "resolved_email": getattr(identity, "email", "") or "",
            "oauth_provider": getattr(identity, "oauth_provider", "") or "",
            "chrome_user_data_dir": getattr(identity, "chrome_user_data_dir", "") or "",
            "chrome_cdp_url": getattr(identity, "chrome_cdp_url", "") or "",
            "metadata": dict(getattr(identity, "metadata", {}) or {}),
        }
        mailbox_account = getattr(identity, "mailbox_account", None)
        if mailbox_account:
            mailbox_extra = dict(getattr(mailbox_account, "extra", {}) or {})
            mailbox_snapshot = {
                "provider": (self.config.extra or {}).get("mail_provider", ""),
                "email": getattr(mailbox_account, "email", "") or "",
                "account_id": str(getattr(mailbox_account, "account_id", "") or ""),
            }
            provider_account = mailbox_extra.get("provider_account")
            if isinstance(provider_account, dict):
                snapshot["provider_account"] = _json_safe(provider_account)
                credentials = provider_account.get("credentials")
                if isinstance(credentials, dict):
                    for field in ("mailbox_jwt", "address_password"):
                        value = credentials.get(field)
                        if value not in (None, ""):
                            mailbox_snapshot[field] = value
            provider_resource = mailbox_extra.get("provider_resource")
            if isinstance(provider_resource, dict):
                snapshot["provider_resource"] = _json_safe(provider_resource)
                metadata = provider_resource.get("metadata")
                if isinstance(metadata, dict):
                    for field in ("address_id", "api_url", "auth_mode"):
                        value = metadata.get(field)
                        if value not in (None, ""):
                            mailbox_snapshot[field] = value
            snapshot["mailbox"] = mailbox_snapshot
        return snapshot

    def _attach_identity_metadata(self, account: Account, identity=None) -> Account:
        actual_identity = identity or getattr(self, "_last_identity", None)
        if not actual_identity:
            return account
        extra = dict(account.extra or {})
        identity_snapshot = self._build_identity_snapshot(actual_identity)
        extra["identity"] = _json_safe(identity_snapshot)
        if identity_snapshot.get("mailbox"):
            mailbox = dict(extra.get("verification_mailbox", {}) or {})
            for field, value in dict(identity_snapshot["mailbox"] or {}).items():
                if field in {"provider", "email", "account_id"} or field not in mailbox:
                    mailbox[field] = value
            extra["verification_mailbox"] = mailbox
        provider_accounts = list(extra.get("provider_accounts", []) or [])
        if isinstance(identity_snapshot.get("provider_account"), dict):
            provider_accounts.append(identity_snapshot["provider_account"])
        if provider_accounts:
            extra["provider_accounts"] = provider_accounts
        provider_resources = list(extra.get("provider_resources", []) or [])
        if isinstance(identity_snapshot.get("provider_resource"), dict):
            provider_resources.append(identity_snapshot["provider_resource"])
        if provider_resources:
            extra["provider_resources"] = provider_resources
        phone_provider = getattr(self, "phone_provider", None)
        phone_account = getattr(phone_provider, "_last_account", None) if phone_provider else None
        if phone_account:
            phone_extra = dict(getattr(phone_account, "extra", {}) or {})
            phone_snapshot = {
                "provider": (self.config.extra or {}).get("phone_provider", ""),
                "phone": getattr(phone_account, "phone", "") or "",
                "project_id": getattr(phone_account, "project_id", "") or "",
            }
            extra["verification_phone"] = {**dict(extra.get("verification_phone", {}) or {}), **phone_snapshot}
            phone_provider_account = phone_extra.get("provider_account")
            if isinstance(phone_provider_account, dict):
                provider_accounts.append(_json_safe(phone_provider_account))
                extra["provider_accounts"] = provider_accounts
            phone_provider_resource = phone_extra.get("provider_resource")
            if isinstance(phone_provider_resource, dict):
                provider_resources.append(_json_safe(phone_provider_resource))
                extra["provider_resources"] = provider_resources
        account.extra = _json_safe(extra)
        return account
