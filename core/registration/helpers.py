from __future__ import annotations

from typing import Any

from .errors import BrowserReuseRequiredError, IdentityResolutionError, RegistrationUnsupportedError
from .models import RegistrationContext


def has_reusable_oauth_browser(identity: Any) -> bool:
    return bool((getattr(identity, "chrome_user_data_dir", "") or "").strip() or (getattr(identity, "chrome_cdp_url", "") or "").strip())


def resolve_timeout(extra: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return int(value)
    return int(default)


def ensure_identity_email(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "email", ""):
        raise IdentityResolutionError(message)


def ensure_mailbox_identity(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "has_mailbox", False):
        raise IdentityResolutionError(message)


def ensure_oauth_executor_allowed(ctx: RegistrationContext, allowed_executor_types: tuple[str, ...] | None, message: str | None = None) -> None:
    if not allowed_executor_types:
        return
    if ctx.executor_type not in allowed_executor_types:
        expected = ", ".join(allowed_executor_types)
        raise RegistrationUnsupportedError(message or f"{ctx.platform_display_name} 当前 OAuth 仅支持 executor_type={expected}")


def ensure_oauth_browser_reuse(ctx: RegistrationContext, message: str) -> None:
    if not has_reusable_oauth_browser(ctx.identity):
        raise BrowserReuseRequiredError(message)


def build_otp_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    code_pattern: str | None = None,
    wait_message: str = "等待验证码...",
    success_label: str = "验证码",
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    def otp_cb():
        ctx.log(wait_message)
        kwargs = {"keyword": keyword, "before_ids": getattr(ctx.identity, "before_ids", set())}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if code_pattern:
            kwargs["code_pattern"] = code_pattern
        code = mailbox.wait_for_code(mail_acct, **kwargs)
        if code:
            ctx.log(f"{success_label}: {code}")
        return code

    return otp_cb


def build_link_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    wait_message: str = "等待验证链接邮件...",
    success_label: str = "验证链接",
    preview_chars: int = 80,
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    def link_cb():
        ctx.log(wait_message)
        before_ids = getattr(ctx.identity, "before_ids", set())
        kwargs = {"keyword": keyword, "before_ids": before_ids}
        if timeout is not None:
            kwargs["timeout"] = timeout
        link = mailbox.wait_for_link(mail_acct, **kwargs)
        if link:
            preview = link if len(link) <= preview_chars else f"{link[:preview_chars]}..."
            ctx.log(f"{success_label}: {preview}")
        return link

    return link_cb


def build_phone_callback(ctx: RegistrationContext):
    phone_provider = getattr(ctx.platform, "phone_provider", None)
    if not phone_provider:
        return None

    state = {"account": None, "stage": "phone"}

    def phone_cb():
        if state["account"] is None:
            ctx.log("等待手机号来源分配号码...")
            account = phone_provider.get_phone()
            state["account"] = account
            state["stage"] = "code"
            phone = str(getattr(account, "phone", "") or "")
            if phone:
                ctx.log(f"手机号来源返回号码: {phone[:4]}****")
            return phone

        ctx.log("等待短信验证码...")
        timeout = resolve_timeout(ctx.extra, ("phone_otp_timeout", "haozhu_phone_timeout"), 180)
        poll_interval = resolve_timeout(ctx.extra, ("phone_poll_interval", "haozhu_poll_interval"), 15)
        code_pattern = str(ctx.extra.get("phone_code_pattern") or "").strip() or None
        code = phone_provider.wait_for_code(
            state["account"],
            timeout=timeout,
            poll_interval=poll_interval,
            code_pattern=code_pattern,
        )
        if code:
            ctx.log(f"短信验证码: {code}")
        return code

    return phone_cb
