from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TwoAPISettings:
    enabled: bool = True
    min_credit: float = 1.0
    auto_wake: bool = True
    auto_refill: bool = False
    request_timeout: float = 90.0
    wake_timeout: float = 60.0
    max_retries: int = 2
    keepalive_space_fallback: bool = False
    minimize_ask_context: bool = True


@dataclass
class TwoAPIAccount:
    plugin: str
    email: str
    base_url: str
    api_key: str = ""
    handle: str = ""
    credit_amount: float = 0.0
    credit_ok: bool = True
    enabled: bool = True
    last_status: str = "unknown"
    last_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def public_metadata(self) -> dict[str, Any]:
        hidden = {
            "cookies",
            "cookie",
            "cookie_header",
            "access_token",
            "refresh_token",
            "api_key",
            "authorization",
            "firebase_api_key",
            "firebase_refresh_token",
            "firebase_id_token",
            "auth_headers",
            "captcha_token",
            "turnstile_token",
        }
        return {
            key: value
            for key, value in dict(self.metadata or {}).items()
            if str(key).lower() not in hidden
        }

    def to_public(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "email": self.email,
            "base_url_preview": mask_secret_in_text(self.base_url),
            "api_key_preview": mask_secret(self.api_key),
            "handle": self.handle,
            "credit_amount": self.credit_amount,
            "credit_ok": self.credit_ok,
            "enabled": self.enabled,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "metadata": self.public_metadata(),
        }


def mask_secret(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 16:
        return "***"
    return f"{text[:10]}...{text[-6:]}"


def mask_secret_in_text(value: str) -> str:
    import re

    text = str(value or "")
    text = re.sub(r"zo_sk_[A-Za-z0-9_\-.]+", lambda m: mask_secret(m.group(0)), text)
    text = re.sub(r"cwk-[A-Za-z0-9_\-.]+", lambda m: mask_secret(m.group(0)), text)
    text = re.sub(r"sk-[A-Za-z0-9_\-.]{12,}", lambda m: mask_secret(m.group(0)), text)
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", lambda m: mask_secret(m.group(0)), text)
    # Thesys 等平台的 API Key 不一定有固定前缀；日志里出现长 token 时统一脱敏。
    text = re.sub(r"\b[A-Za-z0-9_\-.]{48,}\b", lambda m: mask_secret(m.group(0)), text)
    return text
