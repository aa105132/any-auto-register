from __future__ import annotations

from application.account_import_parser import build_luckmail_provider_payload, parse_account_import_lines
from domain.accounts import AccountImportLine


LUCKMAIL_PROVIDER_KEY = "luckmail"
OUTLOOK_TOKEN_PROVIDER_KEY = "outlook_token"
INVENTORY_REUSABLE_PROVIDER_KEYS = {
    LUCKMAIL_PROVIDER_KEY,
    OUTLOOK_TOKEN_PROVIDER_KEY,
}
_LUCKMAIL_REUSE_LIMIT = 4
_TIMEOUT_BLACKLIST_PLATFORM = "chatgpt"


def supports_mailbox_inventory(provider_key: str) -> bool:
    return str(provider_key or "").strip() in INVENTORY_REUSABLE_PROVIDER_KEYS


def inventory_provider_label(provider_key: str) -> str:
    normalized = str(provider_key or "").strip()
    if normalized == LUCKMAIL_PROVIDER_KEY:
        return "LuckMail"
    if normalized == OUTLOOK_TOKEN_PROVIDER_KEY:
        return "Outlook 令牌"
    return normalized or "邮箱池"


def inventory_platform_already_used(provider_key: str, metadata: dict | None, platform: str) -> bool:
    normalized_provider = str(provider_key or "").strip()
    normalized_platform = str(platform or "").strip()
    if normalized_provider != OUTLOOK_TOKEN_PROVIDER_KEY or not normalized_platform:
        return False
    used_platforms = [str(item or "").strip() for item in list((metadata or {}).get("used_platforms") or [])]
    return normalized_platform in used_platforms


def should_blacklist_inventory_timeout(provider_key: str, platform: str) -> bool:
    normalized_provider = str(provider_key or "").strip()
    normalized_platform = str(platform or "").strip()
    return normalized_provider == LUCKMAIL_PROVIDER_KEY and normalized_platform == _TIMEOUT_BLACKLIST_PLATFORM


def parse_mailbox_inventory_import_lines(provider_key: str, lines: list[str]) -> list[dict]:
    normalized = str(provider_key or "").strip()
    if normalized == LUCKMAIL_PROVIDER_KEY:
        return _parse_luckmail_inventory_lines(lines)
    if normalized == OUTLOOK_TOKEN_PROVIDER_KEY:
        return _parse_outlook_inventory_lines(lines)
    raise ValueError(f"暂不支持导入该邮箱池 provider: {provider_key}")


def export_mailbox_inventory_lines(provider_key: str, items: list[dict]) -> str:
    normalized = str(provider_key or "").strip()
    lines: list[str] = []
    for item in items or []:
        email = str(item.get("email") or "").strip()
        token = str(item.get("purchase_token") or item.get("token") or "").strip()
        metadata = dict(item.get("metadata") or {})
        if not email or not token:
            continue
        if normalized == OUTLOOK_TOKEN_PROVIDER_KEY:
            password = str(metadata.get("password") or "").strip()
            client_id = str(metadata.get("client_id") or "").strip()
            if not client_id:
                continue
            lines.append(f"{email}----{password}----{client_id}----{token}")
            continue
        if normalized == LUCKMAIL_PROVIDER_KEY:
            lines.append(f"{email}----{token}")
            continue
        raise ValueError(f"暂不支持导出该邮箱池 provider: {provider_key}")
    return "\n".join(lines)


def build_mailbox_inventory_seed(provider_key: str, item: dict) -> AccountImportLine | None:
    normalized = str(provider_key or "").strip()
    if normalized == LUCKMAIL_PROVIDER_KEY:
        email = str(item.get("email") or "").strip()
        purchase_token = str(item.get("purchase_token") or "").strip()
        if not email or not purchase_token:
            return None
        return AccountImportLine(
            email=email,
            password="",
            extra=build_luckmail_provider_payload(email, purchase_token),
        )
    if normalized == OUTLOOK_TOKEN_PROVIDER_KEY:
        return _build_outlook_inventory_seed(item)
    return None


def resolve_inventory_registration_success(
    provider_key: str,
    metadata: dict | None,
    *,
    registered_email: str = "",
    platform: str = "",
) -> dict:
    normalized = str(provider_key or "").strip()
    current_metadata = dict(metadata or {})
    success_count = int(current_metadata.get("successful_registrations", 0) or 0) + 1
    current_metadata["successful_registrations"] = success_count
    current_metadata["last_registered_email"] = str(registered_email or "")
    current_metadata["last_registered_at"] = _utcnow_iso()
    current_metadata.pop("blacklist_reason", None)
    current_metadata.pop("blacklisted_at", None)

    if normalized == OUTLOOK_TOKEN_PROVIDER_KEY:
        normalized_platform = str(platform or "").strip()
        used_platforms = [
            str(item or "").strip()
            for item in list(current_metadata.get("used_platforms") or [])
            if str(item or "").strip()
        ]
        if normalized_platform and normalized_platform not in used_platforms:
            used_platforms.append(normalized_platform)
        if used_platforms:
            current_metadata["used_platforms"] = used_platforms
        return {
            "status": "unused",
            "note": f"注册成功 {success_count} 次，Outlook 邮箱可跨站复用，已回收到邮箱池",
            "metadata": current_metadata,
        }

    current_metadata["reuse_limit"] = _LUCKMAIL_REUSE_LIMIT
    status = "registered" if success_count >= _LUCKMAIL_REUSE_LIMIT else "unused"
    note = (
        f"注册成功 {success_count}/{_LUCKMAIL_REUSE_LIMIT} 次，已达到复用上限"
        if status == "registered"
        else f"注册成功 {success_count}/{_LUCKMAIL_REUSE_LIMIT} 次，可继续复用"
    )
    return {
        "status": status,
        "note": note,
        "metadata": current_metadata,
    }


def resolve_inventory_timeout_result(
    provider_key: str,
    metadata: dict | None,
    *,
    registered_email: str = "",
    platform: str = "",
) -> dict:
    normalized = str(provider_key or "").strip()
    current_metadata = dict(metadata or {})
    if registered_email:
        current_metadata["remote_email"] = str(registered_email)

    if normalized == OUTLOOK_TOKEN_PROVIDER_KEY:
        return {
            "status": "unused",
            "note": "验证码超时，但 Outlook 邮箱仍可复用，已回收到邮箱池",
            "metadata": current_metadata,
        }

    if not should_blacklist_inventory_timeout(normalized, platform):
        current_metadata.pop("blacklist_reason", None)
        current_metadata.pop("blacklisted_at", None)
        return {
            "status": "unused",
            "note": "验证码超时，邮箱已回收到邮箱池，可再次分配",
            "metadata": current_metadata,
        }

    current_metadata["blacklist_reason"] = "verification_code_timeout"
    current_metadata["blacklisted_at"] = _utcnow_iso()
    return {
        "status": "blacklisted",
        "note": "验证码超时，已拉黑",
        "metadata": current_metadata,
    }


def build_outlook_alias_inventory_entry(parent_item: dict, *, alias_email: str, platform: str = "") -> dict:
    """构建 Outlook 别名邮箱池条目。

    别名本身作为注册邮箱入池；IMAP/OAuth 登录仍复用父 Outlook 邮箱的
    refresh token、client_id 和密码。
    """
    parent_email = str(parent_item.get("email") or "").strip()
    alias = str(alias_email or "").strip()
    refresh_token = str(parent_item.get("purchase_token") or parent_item.get("token") or "").strip()
    metadata = dict(parent_item.get("metadata") or {})
    client_id = str(metadata.get("client_id") or "").strip()
    password = str(metadata.get("password") or "")
    if not parent_email or not alias or not refresh_token or not client_id:
        raise ValueError("Outlook 别名入池缺少 parent_email、alias_email、client_id 或 refresh_token")
    if parent_email.lower() == alias.lower():
        raise ValueError("Outlook 别名邮箱不能等于父邮箱")
    used_platforms = []
    normalized_platform = str(platform or "").strip()
    if normalized_platform:
        used_platforms.append(normalized_platform)
    return {
        "provider_key": OUTLOOK_TOKEN_PROVIDER_KEY,
        "email": alias,
        "purchase_token": refresh_token,
        "status": "unused",
        "note": f"Outlook 别名，父邮箱 {parent_email}",
        "metadata": {
            "password": password,
            "client_id": client_id,
            "source": "outlook_alias_auto",
            "alias_parent_email": parent_email,
            "outlook_login_email": parent_email,
            "remote_email": alias,
            "used_platforms": used_platforms,
            "parent_used_platforms": list(metadata.get("used_platforms") or []),
            "parent_inventory_id": int(parent_item.get("id") or 0),
            "created_from_platform": normalized_platform,
            "created_at": _utcnow_iso(),
        },
    }


def _parse_luckmail_inventory_lines(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    for parsed in parse_account_import_lines(lines or []):
        extra = dict(parsed.extra or {})
        if str(extra.get("mail_provider") or "").strip() != LUCKMAIL_PROVIDER_KEY:
            continue
        email = str(parsed.email or "").strip()
        purchase_token = str(extra.get("luckmail_purchase_token") or "").strip()
        if not email or not purchase_token:
            continue
        note = str(extra.get("note") or parsed.password or "").strip()
        entries.append(
            {
                "email": email,
                "token": purchase_token,
                "note": note,
                "metadata": {
                    "source": "luckmail_import",
                    "remote_email": email,
                },
            }
        )
    return entries


def _parse_outlook_inventory_lines(lines: list[str]) -> list[dict]:
    entries: list[dict] = []
    for raw in lines or []:
        text = str(raw or "").strip()
        if not text:
            continue
        parts = [part.strip() for part in text.split("----")]
        if len(parts) != 4:
            continue
        email, password, client_id, refresh_token = parts
        if not email or not client_id or not refresh_token:
            continue
        entries.append(
            {
                "email": email,
                "token": refresh_token,
                "note": "",
                "metadata": {
                    "password": password,
                    "client_id": client_id,
                    "source": "outlook_token_import",
                    "remote_email": email,
                },
            }
        )
    return entries


def _build_outlook_inventory_seed(item: dict) -> AccountImportLine | None:
    email = str(item.get("email") or "").strip()
    refresh_token = str(item.get("purchase_token") or "").strip()
    metadata = dict(item.get("metadata") or {})
    password = str(metadata.get("password") or "").strip()
    client_id = str(metadata.get("client_id") or "").strip()
    login_email = str(
        metadata.get("outlook_login_email")
        or metadata.get("alias_parent_email")
        or metadata.get("parent_email")
        or email
    ).strip()
    is_alias = bool(login_email and login_email.lower() != email.lower())
    if not email or not login_email or not client_id or not refresh_token:
        return None
    source = str(metadata.get("source") or ("outlook_alias_auto" if is_alias else "outlook_token_import"))
    provider_account_metadata = {
        "email": login_email,
        "client_id": client_id,
        "auth_mode": "refresh_token",
        "source": source,
    }
    provider_resource_metadata = {
        "email": email,
        "client_id": client_id,
        "auth_mode": "refresh_token",
        "source": source,
        "outlook_login_email": login_email,
    }
    if is_alias:
        provider_account_metadata["alias_parent_email"] = login_email
        provider_account_metadata["registration_email"] = email
        provider_resource_metadata["alias_parent_email"] = login_email
        provider_resource_metadata["registration_email"] = email
    provider_account = {
        "provider_type": "mailbox",
        "provider_name": OUTLOOK_TOKEN_PROVIDER_KEY,
        "login_identifier": login_email,
        "display_name": login_email,
        "credentials": {
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        "metadata": provider_account_metadata,
    }
    provider_resource = {
        "provider_type": "mailbox",
        "provider_name": OUTLOOK_TOKEN_PROVIDER_KEY,
        "resource_type": "mailbox",
        "resource_identifier": email,
        "handle": email,
        "display_name": email,
        "metadata": provider_resource_metadata,
    }
    extra = {
        "mail_provider": OUTLOOK_TOKEN_PROVIDER_KEY,
        "outlook_email": login_email,
        "outlook_password": password,
        "outlook_client_id": client_id,
        "outlook_refresh_token": refresh_token,
        "provider_accounts": [provider_account],
        "provider_resources": [provider_resource],
        "overview": {
            "remote_email": email,
            "mail_source": source,
        },
    }
    if is_alias:
        extra["outlook_registration_email"] = email
        extra["outlook_alias_parent_email"] = login_email
        extra["overview"]["alias_parent_email"] = login_email
    return AccountImportLine(
        email=email,
        password=password,
        extra=extra,
    )


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
