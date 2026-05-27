"""ChatGPT protocol mailbox registration worker."""
from __future__ import annotations

import inspect
import random
import string
from typing import Callable

from platforms.chatgpt.register import RegistrationEngine


_ALIAS_MODE_MAP = {
    "": "none",
    "none": "none",
    "off": "none",
    "disabled": "none",
    "\u65e0": "none",
    "plus": "plus",
    "dot": "dot",
    "\u539f\u90ae\u7bb1+": "plus",
    "\u539f\u90ae\u7bb1.": "dot",
}
_ALIAS_CHARSET = string.ascii_lowercase + string.digits
_DEFAULT_ALIAS_LENGTH = 4
_MAX_ALIAS_ATTEMPTS = 3


class ChatGPTRegistrationError(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__(result.error_message if result else "registration failed")


class _MailboxEmailService:
    def __init__(self, *, mailbox, mailbox_account, provider: str, log_fn: Callable[[str], None] | None = None):
        self.service_type = type("ST", (), {"value": provider})()
        self._mailbox = mailbox
        self._mailbox_account = mailbox_account
        self._acct = None
        self._otp_before_ids: set[str] = set()
        self._log_fn = log_fn or (lambda _message: None)

    def create_email(self, config=None):
        self._acct = self._mailbox_account
        return {
            "email": self._mailbox_account.email,
            "service_id": getattr(self._mailbox_account, "account_id", ""),
            "token": getattr(self._mailbox_account, "account_id", ""),
        }

    def _load_current_ids(self) -> set[str]:
        acct = self._acct or self._mailbox_account
        getter = getattr(self._mailbox, "get_current_ids", None)
        if not callable(getter):
            return set()
        ids = getter(acct) or set()
        return {str(item) for item in ids if item not in (None, "")}

    def set_otp_before_ids(self, ids):
        self._otp_before_ids = {str(item) for item in (ids or set()) if item not in (None, "")}

    def capture_otp_snapshot(self):
        ids = self._load_current_ids()
        self.set_otp_before_ids(ids)
        return len(self._otp_before_ids)

    def get_verification_code(
        self,
        email=None,
        email_id=None,
        timeout=120,
        pattern=None,
        otp_sent_at=None,
        strict_otp_sent_at: bool = False,
    ):
        acct = self._acct or self._mailbox_account
        kwargs = {
            "keyword": "",
            "timeout": timeout,
            "code_pattern": pattern,
            "before_ids": set(self._otp_before_ids),
        }
        try:
            signature = inspect.signature(self._mailbox.wait_for_code)
            if "otp_sent_at" in signature.parameters:
                kwargs["otp_sent_at"] = otp_sent_at
            if "strict_otp_sent_at" in signature.parameters:
                kwargs["strict_otp_sent_at"] = strict_otp_sent_at
            if "debug_callback" in signature.parameters:
                kwargs["debug_callback"] = self._log_fn
        except (TypeError, ValueError):
            pass
        return self._mailbox.wait_for_code(acct, **kwargs)

    def update_status(self, success, error=None):
        return None

    @property
    def status(self):
        return None


def _normalize_sub_mail_mode(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    return _ALIAS_MODE_MAP.get(raw, raw if raw in {"plus", "dot"} else "none")


def _normalize_sub_mail_length(value: int | str | None) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = _DEFAULT_ALIAS_LENGTH
    return max(1, min(resolved, 16))


def _build_alias_email(base_email: str, mode: str, length: int) -> str:
    base_email = str(base_email or "").strip()
    if "@" not in base_email:
        raise ValueError(f"invalid base email for alias retry: {base_email}")
    local, domain = base_email.split("@", 1)
    local = local.split("+", 1)[0].rstrip(".")
    suffix = "".join(random.choice(_ALIAS_CHARSET) for _ in range(length))
    if mode == "plus":
        return f"{local}+{suffix}@{domain}"
    if mode == "dot":
        return f"{local}.{suffix}@{domain}"
    raise ValueError(f"unsupported alias mode: {mode}")


def _should_retry_with_alias(result) -> bool:
    if not result:
        return False
    metadata = dict(getattr(result, "metadata", {}) or {})
    disposition = str(metadata.get("registration_disposition", "") or "").strip()
    return disposition == "existing_account"


class ChatGPTProtocolMailboxWorker:
    def __init__(
        self,
        *,
        mailbox,
        mailbox_account,
        provider: str,
        proxy_url: str | None = None,
        log_fn: Callable[[str], None] = print,
        sub_mail_mode: str | None = None,
        sub_mail_length: int | str | None = None,
        otp_timeout: int | str | None = None,
        otp_resend_interval: int | str | None = None,
        login_otp_timeout: int | str | None = None,
    ):
        if not mailbox or not mailbox_account:
            raise ValueError("ChatGPT mailbox registration requires mailbox provider account")
        self._email_service = _MailboxEmailService(
            mailbox=mailbox,
            mailbox_account=mailbox_account,
            provider=provider,
            log_fn=log_fn,
        )
        self._mailbox_account = mailbox_account
        self._proxy_url = proxy_url
        self._log_fn = log_fn
        self._sub_mail_mode = _normalize_sub_mail_mode(sub_mail_mode)
        self._sub_mail_length = _normalize_sub_mail_length(sub_mail_length)
        self._otp_timeout = otp_timeout
        self._otp_resend_interval = otp_resend_interval
        self._login_otp_timeout = login_otp_timeout

    def _create_engine(self, *, target_email: str, password: str, alias_parent_email: str = "") -> RegistrationEngine:
        engine = RegistrationEngine(
            email_service=self._email_service,
            proxy_url=self._proxy_url,
            callback_logger=self._log_fn,
            otp_total_timeout=self._otp_timeout,
            otp_resend_interval=self._otp_resend_interval,
            login_otp_total_timeout=self._login_otp_timeout,
        )
        engine.email = target_email
        engine.password = password
        engine.prefer_alias_on_existing = self._sub_mail_mode in {"plus", "dot"}
        engine._alias_parent_email = alias_parent_email
        if alias_parent_email:
            engine._mailbox_alias_mode = self._sub_mail_mode
        return engine

    def _apply_retry_metadata(self, result, *, base_email: str, alias_attempts: int):
        metadata = dict(getattr(result, "metadata", {}) or {})
        metadata["alias_retry_attempts"] = alias_attempts
        if alias_attempts > 0:
            metadata["mailbox_base_email"] = base_email
            metadata["mailbox_alias_mode"] = self._sub_mail_mode
        result.metadata = metadata
        return result

    def run(self, *, email: str, password: str):
        base_email = str(email or getattr(self._mailbox_account, "email", "") or "").strip()
        current_email = base_email
        alias_attempts = 0
        combined_logs: list[str] = []

        while True:
            alias_parent_email = base_email if current_email != base_email else ""
            engine = self._create_engine(
                target_email=current_email,
                password=password,
                alias_parent_email=alias_parent_email,
            )
            result = engine.run()
            combined_logs.extend(list(getattr(result, "logs", []) or []))
            self._apply_retry_metadata(result, base_email=base_email, alias_attempts=alias_attempts)
            result.logs = list(combined_logs)

            if result.success and not (
                self._sub_mail_mode in {"plus", "dot"}
                and str(getattr(result, "source", "") or "").strip() == "login"
            ):
                return result

            if self._sub_mail_mode not in {"plus", "dot"} or not _should_retry_with_alias(result):
                raise ChatGPTRegistrationError(result)

            if alias_attempts >= _MAX_ALIAS_ATTEMPTS:
                if not result.error_message:
                    result.error_message = "alias retry attempts exhausted"
                raise ChatGPTRegistrationError(result)

            alias_attempts += 1
            current_email = _build_alias_email(base_email, self._sub_mail_mode, self._sub_mail_length)
            self._log_fn(
                f"[alias] existing/suspected account detected; retry with alias ({alias_attempts}/{_MAX_ALIAS_ATTEMPTS}): {current_email}"
            )
