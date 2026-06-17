"""注册身份提供者抽象。"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


IDENTITY_PROVIDER_ALIASES = {
    "": "mailbox",
    "email": "mailbox",
    "mail": "mailbox",
    "mailbox": "mailbox",
    "oauth": "oauth_browser",
    "oauth_browser": "oauth_browser",
    "oauth_manual": "oauth_browser",   # backward-compat
    "manual_oauth": "oauth_browser",   # backward-compat
    "phone": "phone",
    "phone_sms": "phone",
    "sms": "phone",
}

OAUTH_PROVIDER_ALIASES = {
    "google": "google",
    "google-oauth2": "google",
    "github": "github",
    "linkedin": "linkedin",
    "linkedin-openid": "linkedin",
    "microsoft": "microsoft",
    "outlook": "microsoft",
    "office365": "microsoft",
    "windowslive": "microsoft",
    "live": "microsoft",
    "apple": "apple",
    "x": "x",
    "twitter": "x",
    "builderid": "builderid",
    "builder-id": "builderid",
    "builder_id": "builderid",
    "builder id": "builderid",
    "awsbuilderid": "builderid",
    "aws builder id": "builderid",
    "sso": "pilipala_sso",
    "pilipala": "pilipala_sso",
    "pilipala-sso": "pilipala_sso",
    "pilipala_sso": "pilipala_sso",
    "edu_pilipala": "pilipala_sso",
    "edu.pilipala.store": "pilipala_sso",
}


def normalize_identity_provider(value: Optional[str]) -> str:
    return IDENTITY_PROVIDER_ALIASES.get((value or "").strip().lower(), (value or "").strip().lower() or "mailbox")


def normalize_oauth_provider(value: Optional[str]) -> str:
    raw = (value or "").strip().lower()
    return OAUTH_PROVIDER_ALIASES.get(raw, raw)


def _normalize_bool_flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on", "enabled"}:
        return True
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _is_mailbox_alias_email(requested_email: str, provider_email: str, mode: str) -> bool:
    requested = (requested_email or "").strip().lower()
    provider = (provider_email or "").strip().lower()
    normalized_mode = (mode or "").strip().lower()
    if not requested or not provider or "@" not in requested or "@" not in provider:
        return False

    requested_local, requested_domain = requested.split("@", 1)
    provider_local, provider_domain = provider.split("@", 1)
    if requested_domain != provider_domain:
        return False

    if normalized_mode == "plus":
        prefix = f"{provider_local}+"
        return requested_local.startswith(prefix) and len(requested_local) > len(prefix)
    if normalized_mode == "dot":
        prefix = f"{provider_local}."
        return requested_local.startswith(prefix) and len(requested_local) > len(prefix)
    return False


def _can_accept_requested_mailbox_email(requested_email: str, provider_email: str, extra: dict | None = None) -> bool:
    requested = (requested_email or "").strip()
    provider = (provider_email or "").strip()
    if not requested or not provider:
        return False
    if requested == provider:
        return True

    options = extra or {}
    if not _normalize_bool_flag(options.get("sub_mail_use_on_first_register"), default=False):
        return False
    mode = str(options.get("sub_mail_mode") or "").strip().lower()
    if mode not in {"plus", "dot"}:
        return False
    return _is_mailbox_alias_email(requested, provider, mode)


@dataclass
class IdentityMaterial:
    identity_provider: str = "mailbox"
    email: str = ""
    mailbox_account: Any = None
    before_ids: set = field(default_factory=set)
    oauth_provider: str = ""
    chrome_user_data_dir: str = ""
    chrome_cdp_url: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def has_mailbox(self) -> bool:
        return self.mailbox_account is not None


class BaseIdentityProvider(ABC):
    identity_provider: str = "mailbox"

    def __init__(self, *, mailbox=None, extra: dict = None):
        self.mailbox = mailbox
        self.extra = extra or {}

    @abstractmethod
    def resolve(self, requested_email: Optional[str] = None) -> IdentityMaterial:
        ...


class MailboxIdentityProvider(BaseIdentityProvider):
    identity_provider = "mailbox"

    def resolve(self, requested_email: Optional[str] = None) -> IdentityMaterial:
        requested_email = (requested_email or "").strip()
        if not self.mailbox:
            return IdentityMaterial(identity_provider=self.identity_provider, email=requested_email)

        mail_acct = self.mailbox.get_email()
        email = getattr(mail_acct, "email", "") or ""
        if not requested_email and not email:
            provider_name = getattr(self.mailbox, "__class__", type(self.mailbox)).__name__
            raise ValueError(f"{provider_name} 未返回可用邮箱，请检查 mailbox provider 配置或服务状态")
        if requested_email and email and not _can_accept_requested_mailbox_email(requested_email, email, self.extra):
            raise ValueError(f"传入邮箱 {requested_email} 与当前邮箱 provider 返回的 {email} 不一致")
        before_ids = self.mailbox.get_current_ids(mail_acct) if mail_acct else set()
        return IdentityMaterial(
            identity_provider=self.identity_provider,
            email=requested_email or email,
            mailbox_account=mail_acct,
            before_ids=before_ids,
        )


class BrowserOAuthIdentityProvider(BaseIdentityProvider):
    identity_provider = "oauth_browser"

    def resolve(self, requested_email: Optional[str] = None) -> IdentityMaterial:
        email = (requested_email or self.extra.get("oauth_email_hint", "") or "").strip()
        mailbox_account = None
        source = str(self.extra.get("oauth_account_source") or "").strip().lower()
        if source in {"mailbox", "mail_provider", "provider"} and self.mailbox:
            mailbox_account = self.mailbox.get_email()
            provider_email = getattr(mailbox_account, "email", "") or ""
            if requested_email and provider_email and requested_email.strip().lower() != provider_email.strip().lower():
                raise ValueError(f"?? OAuth ?? {requested_email} ???????? {provider_email} ???")
            email = provider_email or email
        oauth_provider = normalize_oauth_provider(
            self.extra.get("oauth_provider") or self.extra.get("default_oauth_provider")
        )
        return IdentityMaterial(
            identity_provider=self.identity_provider,
            email=email,
            mailbox_account=mailbox_account,
            before_ids=set(),
            oauth_provider=oauth_provider,
            chrome_user_data_dir=self.extra.get("chrome_user_data_dir", ""),
            chrome_cdp_url=self.extra.get("chrome_cdp_url", ""),
            metadata={
                "oauth_email_hint": self.extra.get("oauth_email_hint", ""),
                "oauth_account_source": source,
            },
        )


class PhoneIdentityProvider(BaseIdentityProvider):
    identity_provider = "phone"

    def resolve(self, requested_email: Optional[str] = None) -> IdentityMaterial:
        return IdentityMaterial(
            identity_provider=self.identity_provider,
            email=(requested_email or self.extra.get("phone_email_hint", "") or "").strip(),
            metadata={"phone_provider": self.extra.get("phone_provider", "")},
        )


# Backward-compat alias
ManualOAuthIdentityProvider = BrowserOAuthIdentityProvider


def create_identity_provider(mode: Optional[str], *, mailbox=None, extra: dict = None) -> BaseIdentityProvider:
    normalized = normalize_identity_provider(mode)
    if normalized == "mailbox":
        return MailboxIdentityProvider(mailbox=mailbox, extra=extra)
    if normalized == "oauth_browser":
        return BrowserOAuthIdentityProvider(mailbox=mailbox, extra=extra)
    if normalized == "phone":
        return PhoneIdentityProvider(mailbox=mailbox, extra=extra)
    raise ValueError(f"未知 identity_provider: {mode}")
