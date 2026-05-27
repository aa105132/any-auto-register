from __future__ import annotations

import base64
import csv
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

from core.datetime_utils import serialize_datetime
from domain.accounts import AccountExportSelection, AccountRecord
from infrastructure.accounts_repository import AccountsRepository


CHATGPT_PLATFORM = "chatgpt"
CODEBANANA_PLATFORM = "codebanana"
ATXP_PLATFORM = "atxp"
VENICE_PLATFORM = "venice"
DEFAULT_CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

CHATGPT_EXPORT_FIELDS = [
    "email",
    "password",
    "client_id",
    "account_id",
    "workspace_id",
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "email_service",
    "registered_at",
    "last_refresh",
    "expires_at",
    "status",
]

CODEBANANA_EXPORT_FIELDS = [
    "email",
    "password",
    "user_id",
    "cbbot_key",
    "session_token",
    "jwtToken",
    "csrf_token",
    "cookies",
    "mailbox_jwt",
    "address_password",
    "address_id",
    "api_url",
    "auth_mode",
]

ATXP_EXPORT_FIELDS = [
    "email",
    "password",
    "account_id",
    "privy_token",
    "refresh_token",
    "connection_token",
    "connection_string",
    "wallet_address",
    "gateway_health_alive",
    "gateway_health_model",
    "clowdbot_status",
    "clowdbot_instance_id",
    "claimed_agent_email",
    "create_clowdbot_completed",
    "claim_email_completed",
    "reward_progress",
    "task_error",
    "balance_iou",
    "balance_usdc",
    "balance_restriction",
    "mailbox_jwt",
    "address_password",
    "address_id",
    "api_url",
    "auth_mode",
]

VENICE_EXPORT_FIELDS = [
    "email",
    "password",
    "user_id",
    "access_token",
    "refresh_token",
    "session_token",
    "client_id",
    "api_key",
    "api_key_description",
    "credits",
    "plan_state",
]

ENTER_EXPORT_FIELDS = [
    "email",
    "password",
    "access_token",
    "refresh_token",
    "id_token",
    "workspace_id",
    "project_id",
    "project_name",
    "ai_api_token",
    "ai_connection_state",
    "entercloud_enabled",
    "entercloud_provider",
    "entercloud_api_url",
    "entercloud_anon_key",
    "balance",
    "plan_type",
    "subscription_status",
]

DEFAULT_EXPORT_FIELDS = {
    CHATGPT_PLATFORM: CHATGPT_EXPORT_FIELDS,
    CODEBANANA_PLATFORM: [
        "email",
        "password",
        "cbbot_key",
        "session_token",
        "jwtToken",
        "csrf_token",
        "mailbox_jwt",
    ],
    ATXP_PLATFORM: [
        "email",
        "password",
        "account_id",
        "connection_string",
        "privy_token",
        "refresh_token",
        "clowdbot_status",
    ],
    VENICE_PLATFORM: [
        "api_key",
    ],
    "enter": [
        "email",
        "ai_api_token",
    ],
}

SUPPORTED_CUSTOM_EXPORTS = {
    CHATGPT_PLATFORM: set(CHATGPT_EXPORT_FIELDS),
    CODEBANANA_PLATFORM: set(CODEBANANA_EXPORT_FIELDS),
    ATXP_PLATFORM: set(ATXP_EXPORT_FIELDS),
    VENICE_PLATFORM: set(VENICE_EXPORT_FIELDS),
    "enter": set(ENTER_EXPORT_FIELDS),
}


@dataclass(slots=True)
class ExportArtifact:
    filename: str
    media_type: str
    content: str | bytes | io.BytesIO


def _decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return {}


def _isoformat(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _timestamp_name(prefix: str, suffix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}.{suffix}"


def _credential_value(item: AccountRecord, *keys: str) -> str:
    for key in keys:
        for credential in item.credentials or []:
            if credential.get("scope") == "platform" and credential.get("key") == key and credential.get("value"):
                return str(credential["value"])
    return ""


def _provider_account_credential(item: AccountRecord, *keys: str) -> str:
    for provider_account in item.provider_accounts or []:
        credentials = provider_account.get("credentials") or {}
        for key in keys:
            value = credentials.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _provider_metadata(item: AccountRecord, *keys: str) -> str:
    for resource in item.provider_resources or []:
        metadata = resource.get("metadata") or {}
        for key in keys:
            value = metadata.get(key)
            if value not in (None, ""):
                return str(value)
    for provider_account in item.provider_accounts or []:
        metadata = provider_account.get("metadata") or {}
        for key in keys:
            value = metadata.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _mailbox_provider_name(item: AccountRecord) -> str:
    for resource in item.provider_resources or []:
        if resource.get("resource_type") == "mailbox" and resource.get("provider_name"):
            return str(resource["provider_name"])
    for provider_account in item.provider_accounts or []:
        if provider_account.get("provider_type") == "mailbox" and provider_account.get("provider_name"):
            return str(provider_account["provider_name"])
    return ""


def _overview_value(item: AccountRecord, key: str) -> object:
    overview = item.overview or {}
    return overview.get(key)


def _credential_keys(item: AccountRecord) -> set[str]:
    return {str(credential.get("key") or "").strip() for credential in item.credentials or [] if str(credential.get("key") or "").strip()}


def _overview_keys(item: AccountRecord) -> set[str]:
    return {str(key).strip() for key in (item.overview or {}).keys() if str(key).strip()}


def _generic_field_value(item: AccountRecord, field_key: str) -> object:
    if field_key == "id":
        return item.id
    if field_key == "platform":
        return item.platform
    if field_key == "email":
        return item.email
    if field_key == "password":
        return item.password
    if field_key == "user_id":
        return item.user_id
    if field_key in {"primary_token", "token"}:
        return item.primary_token
    if field_key == "cashier_url":
        return item.cashier_url
    if field_key == "lifecycle_status":
        return item.lifecycle_status
    if field_key == "validity_status":
        return item.validity_status
    if field_key == "plan_state":
        return item.plan_state
    if field_key == "plan_name":
        return item.plan_name
    if field_key == "display_status":
        return item.display_status
    if field_key == "trial_end_time":
        return item.trial_end_time
    if field_key == "created_at":
        return _isoformat(item.created_at)
    if field_key == "updated_at":
        return _isoformat(item.updated_at)
    if field_key == "mailbox_provider":
        return _mailbox_provider_name(item)
    if field_key == "mailbox_email":
        return _provider_metadata(item, "email", "remote_email") or ""
    if field_key == "provider_account":
        for provider_account in item.provider_accounts or []:
            if provider_account.get("login_identifier"):
                return provider_account.get("login_identifier")
        return ""
    credential = _credential_value(item, field_key)
    if credential not in (None, ""):
        return credential
    overview = _overview_value(item, field_key)
    if overview not in (None, ""):
        return overview
    provider_credential = _provider_account_credential(item, field_key)
    if provider_credential not in (None, ""):
        return provider_credential
    provider_meta = _provider_metadata(item, field_key)
    if provider_meta not in (None, ""):
        return provider_meta
    return ""


def _generic_export_payload(item: AccountRecord, field_keys: list[str]) -> dict[str, object]:
    return {field_key: _generic_field_value(item, field_key) for field_key in field_keys}


COMMON_EXPORT_FIELDS = [
    "email",
    "password",
    "user_id",
    "primary_token",
    "api_key",
    "ai_api_token",
    "access_token",
    "refresh_token",
    "session_token",
    "cookies",
    "cashier_url",
    "display_status",
    "lifecycle_status",
    "plan_state",
    "validity_status",
    "created_at",
    "updated_at",
]

FIELD_LABELS = {
    "id": "ID",
    "platform": "平台",
    "email": "邮箱",
    "password": "密码",
    "user_id": "用户 ID",
    "primary_token": "主 Token",
    "token": "Token",
    "api_key": "API Key",
    "ai_api_token": "AI API Token",
    "access_token": "Access Token",
    "refresh_token": "Refresh Token",
    "session_token": "Session Token",
    "cookies": "Cookies",
    "cashier_url": "订阅链接",
    "display_status": "显示状态",
    "lifecycle_status": "生命周期",
    "plan_state": "套餐状态",
    "validity_status": "有效性",
    "created_at": "创建时间",
    "updated_at": "更新时间",
    "mailbox_provider": "邮箱 Provider",
    "mailbox_email": "验证邮箱",
    "provider_account": "Provider 账号",
}


def _codebanana_export_payload(item: AccountRecord) -> dict[str, object]:
    return {
        "email": item.email,
        "password": item.password,
        "user_id": item.user_id or None,
        "cbbot_key": _credential_value(item, "cbbot_key") or item.user_id or None,
        "session_token": _credential_value(item, "session_token", "sessionToken") or None,
        "jwtToken": _credential_value(item, "jwtToken") or None,
        "csrf_token": _credential_value(item, "csrf_token") or None,
        "cookies": _credential_value(item, "cookies", "cookie") or None,
        "mailbox_jwt": _provider_account_credential(item, "mailbox_jwt") or None,
        "address_password": _provider_account_credential(item, "address_password") or None,
        "address_id": _provider_metadata(item, "address_id") or None,
        "api_url": _provider_metadata(item, "api_url") or None,
        "auth_mode": _provider_metadata(item, "auth_mode") or None,
    }


def _atxp_export_payload(item: AccountRecord) -> dict[str, object]:
    return {
        "email": item.email,
        "password": item.password,
        "account_id": item.user_id or _credential_value(item, "account_id") or None,
        "privy_token": _credential_value(item, "privy_token") or None,
        "refresh_token": _credential_value(item, "refresh_token", "refreshToken") or None,
        "connection_token": _credential_value(item, "connection_token") or None,
        "connection_string": _credential_value(item, "connection_string") or item.primary_token or None,
        "wallet_address": _credential_value(item, "wallet_address") or None,
        "gateway_health_alive": _overview_value(item, "gateway_health_alive"),
        "gateway_health_model": _overview_value(item, "gateway_health_model"),
        "clowdbot_status": _overview_value(item, "clowdbot_status") or None,
        "clowdbot_instance_id": _credential_value(item, "clowdbot_instance_id") or None,
        "claimed_agent_email": _credential_value(item, "claimed_agent_email") or None,
        "create_clowdbot_completed": _overview_value(item, "create_clowdbot_completed"),
        "claim_email_completed": _overview_value(item, "claim_email_completed"),
        "reward_progress": _overview_value(item, "reward_progress"),
        "task_error": _overview_value(item, "task_error") or None,
        "balance_iou": _overview_value(item, "balance_iou") or None,
        "balance_usdc": _overview_value(item, "balance_usdc") or None,
        "balance_restriction": _overview_value(item, "balance_restriction") or None,
        "mailbox_jwt": _provider_account_credential(item, "mailbox_jwt") or None,
        "address_password": _provider_account_credential(item, "address_password") or None,
        "address_id": _provider_metadata(item, "address_id") or None,
        "api_url": _provider_metadata(item, "api_url") or None,
        "auth_mode": _provider_metadata(item, "auth_mode") or None,
    }


def _venice_export_payload(item: AccountRecord) -> dict[str, object]:
    return {
        "email": item.email,
        "password": item.password,
        "user_id": item.user_id or None,
        "access_token": _credential_value(item, "access_token") or item.primary_token or None,
        "refresh_token": _credential_value(item, "refresh_token") or None,
        "session_token": _credential_value(item, "session_token") or None,
        "client_id": _credential_value(item, "client_id") or None,
        "api_key": _credential_value(item, "api_key") or None,
        "api_key_description": _credential_value(item, "api_key_description") or None,
        "credits": _overview_value(item, "credits"),
        "plan_state": _overview_value(item, "plan_state") or None,
    }


def _chatgpt_export_payload(item: AccountRecord) -> dict:
    access_token = _credential_value(item, "access_token", "accessToken", "legacy_token")
    refresh_token = _credential_value(item, "refresh_token", "refreshToken")
    id_token = _credential_value(item, "id_token", "idToken")
    session_token = _credential_value(item, "session_token", "sessionToken")
    workspace_id = _credential_value(item, "workspace_id", "workspaceId")
    client_id = _credential_value(item, "client_id", "clientId") or DEFAULT_CHATGPT_CLIENT_ID
    cookies = _credential_value(item, "cookies", "cookie")
    account_id = item.user_id or ""
    email_service = _mailbox_provider_name(item)

    payload = _decode_jwt_payload(access_token) if access_token else {}
    auth_info = payload.get("https://api.openai.com/auth", {})
    if not account_id:
        account_id = auth_info.get("chatgpt_account_id", "") or ""
    expires_at = None
    exp_timestamp = payload.get("exp")
    if isinstance(exp_timestamp, int) and exp_timestamp > 0:
        expires_at = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)

    return {
        "id": item.id,
        "email": item.email,
        "password": item.password,
        "client_id": client_id,
        "account_id": account_id,
        "workspace_id": workspace_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": id_token,
        "session_token": session_token,
        "cookies": cookies,
        "email_service": email_service,
        "registered_at": _isoformat(item.created_at),
        "last_refresh": _isoformat(item.updated_at),
        "expires_at": _isoformat(expires_at),
        "status": item.display_status,
        "expires_at_unix": int(expires_at.timestamp()) if expires_at else 0,
    }


def _to_cpa_account(item: AccountRecord) -> SimpleNamespace:
    payload = _chatgpt_export_payload(item)
    return SimpleNamespace(
        email=payload["email"],
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        id_token=payload["id_token"],
    )


def _generate_cpa_token_json(item: AccountRecord) -> dict:
    from platforms.chatgpt.cpa_upload import generate_token_json

    return generate_token_json(_to_cpa_account(item))


def _make_sub2api_json(item: AccountRecord) -> dict:
    payload = _chatgpt_export_payload(item)
    return {
        "proxies": [],
        "accounts": [
            {
                "name": payload["email"],
                "platform": "openai",
                "type": "oauth",
                "credentials": {
                    "access_token": payload["access_token"],
                    "chatgpt_account_id": payload["account_id"],
                    "chatgpt_user_id": "",
                    "client_id": payload["client_id"],
                    "expires_at": payload["expires_at_unix"],
                    "expires_in": 863999,
                    "model_mapping": {
                        "gpt-5.1": "gpt-5.1",
                        "gpt-5.1-codex": "gpt-5.1-codex",
                        "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                        "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
                        "gpt-5.2": "gpt-5.2",
                        "gpt-5.2-codex": "gpt-5.2-codex",
                    },
                    "organization_id": payload["workspace_id"],
                    "refresh_token": payload["refresh_token"],
                },
                "extra": {},
                "concurrency": 10,
                "priority": 1,
                "rate_multiplier": 1,
                "auto_pause_on_expired": True,
            }
        ],
    }


class AccountExportsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def list_export_fields(self, platform: str) -> dict:
        normalized_platform = platform or ""
        selection = AccountExportSelection(platform=normalized_platform, select_all=True)
        items = self.repository.select_for_export(selection)
        discovered = set(COMMON_EXPORT_FIELDS)
        for item in items[:500]:
            discovered.update(_credential_keys(item))
            discovered.update(_overview_keys(item))
            for provider_account in item.provider_accounts or []:
                discovered.update(str(key).strip() for key in (provider_account.get("credentials") or {}).keys() if str(key).strip())
                discovered.update(str(key).strip() for key in (provider_account.get("metadata") or {}).keys() if str(key).strip())
            for resource in item.provider_resources or []:
                discovered.update(str(key).strip() for key in (resource.get("metadata") or {}).keys() if str(key).strip())
        if normalized_platform in SUPPORTED_CUSTOM_EXPORTS:
            discovered.update(SUPPORTED_CUSTOM_EXPORTS[normalized_platform])
        preferred = DEFAULT_EXPORT_FIELDS.get(normalized_platform) or [field for field in ("api_key", "ai_api_token", "primary_token", "email", "password") if field in discovered]
        if not preferred:
            preferred = ["email"]
        ordered = []
        for field in [*COMMON_EXPORT_FIELDS, *sorted(discovered)]:
            if field and field not in ordered:
                ordered.append(field)
        return {
            "platform": normalized_platform,
            "default_fields": [field for field in preferred if field in discovered or field in ordered],
            "fields": [{"key": field, "label": FIELD_LABELS.get(field, field)} for field in ordered],
        }

    def export_json(self, selection: AccountExportSelection) -> ExportArtifact:
        platform = self._normalize_platform(selection)
        items = self._load_supported_items(selection)
        field_keys = self._resolve_field_keys(platform, selection.field_keys)
        rows = [
            {field_key: self._normalize_json_value(payload.get(field_key)) for field_key in field_keys}
            for payload in self._payloads_for_items(platform, items, field_keys)
        ]
        return ExportArtifact(
            filename=_timestamp_name(self._export_prefix(platform), "json"),
            media_type="application/json",
            content=json.dumps(rows, ensure_ascii=False, indent=2),
        )

    def export_csv(self, selection: AccountExportSelection) -> ExportArtifact:
        platform = self._normalize_platform(selection)
        items = self._load_supported_items(selection)
        field_keys = self._resolve_field_keys(platform, selection.field_keys)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(field_keys)
        for payload in self._payloads_for_items(platform, items, field_keys):
            writer.writerow([self._normalize_text_value(payload.get(field_key)) for field_key in field_keys])
        return ExportArtifact(
            filename=_timestamp_name(self._export_prefix(platform), "csv"),
            media_type="text/csv",
            content=output.getvalue(),
        )

    def export_txt(self, selection: AccountExportSelection) -> ExportArtifact:
        platform = self._normalize_platform(selection)
        items = self._load_supported_items(selection)
        field_keys = self._resolve_field_keys(platform, selection.field_keys)
        lines = []
        for payload in self._payloads_for_items(platform, items, field_keys):
            lines.append("----".join(self._normalize_text_value(payload.get(field_key)) for field_key in field_keys))
        content = "\n".join(lines)
        if lines:
            content += "\n"
        return ExportArtifact(
            filename=_timestamp_name(self._export_prefix(platform), "txt"),
            media_type="text/plain",
            content=content,
        )

    def export_chatgpt_json(self, selection: AccountExportSelection) -> ExportArtifact:
        return self.export_json(selection)

    def export_chatgpt_csv(self, selection: AccountExportSelection) -> ExportArtifact:
        return self.export_csv(selection)

    def export_chatgpt_sub2api(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        if len(items) == 1:
            item = items[0]
            content = json.dumps(_make_sub2api_json(item), ensure_ascii=False, indent=2)
            return ExportArtifact(
                filename=f"{item.email}_sub2api.json",
                media_type="application/json",
                content=content,
            )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in items:
                archive.writestr(
                    f"{item.email}_sub2api.json",
                    json.dumps(_make_sub2api_json(item), ensure_ascii=False, indent=2),
                )
        buffer.seek(0)
        return ExportArtifact(
            filename=_timestamp_name("sub2api_tokens", "zip"),
            media_type="application/zip",
            content=buffer,
        )

    def export_chatgpt_cpa(self, selection: AccountExportSelection) -> ExportArtifact:
        items = self._load_chatgpt_items(selection)
        if len(items) == 1:
            item = items[0]
            content = json.dumps(_generate_cpa_token_json(item), ensure_ascii=False, indent=2)
            return ExportArtifact(
                filename=f"{item.email}.json",
                media_type="application/json",
                content=content,
            )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in items:
                archive.writestr(
                    f"{item.email}.json",
                    json.dumps(_generate_cpa_token_json(item), ensure_ascii=False, indent=2),
                )
        buffer.seek(0)
        return ExportArtifact(
            filename=_timestamp_name("cpa_tokens", "zip"),
            media_type="application/zip",
            content=buffer,
        )

    def _normalize_platform(self, selection: AccountExportSelection) -> str:
        selection.platform = selection.platform or CHATGPT_PLATFORM
        return selection.platform

    def _load_supported_items(self, selection: AccountExportSelection) -> list[AccountRecord]:
        platform = self._normalize_platform(selection)
        items = self.repository.select_for_export(selection)
        if selection.exclude_zero_balance and platform == ATXP_PLATFORM:
            items = [item for item in items if self._has_positive_balance(item)]
        return items

    @staticmethod
    def _has_positive_balance(item: AccountRecord) -> bool:
        overview = item.overview or {}
        for key in ("balance_iou", "balance_usdc"):
            raw = overview.get(key)
            if raw is None:
                continue
            try:
                if float(raw) > 0:
                    return True
            except (ValueError, TypeError):
                continue
        return False

    def _resolve_field_keys(self, platform: str, field_keys: list[str]) -> list[str]:
        available = {item["key"] for item in self.list_export_fields(platform).get("fields", [])}
        resolved = list(field_keys or DEFAULT_EXPORT_FIELDS.get(platform, []) or ["email"])
        if not resolved:
            raise ValueError("at least one field is required")
        for field_key in resolved:
            if field_key not in available:
                raise ValueError(f"unsupported export field: {field_key}")
        return resolved

    def _payloads_for_items(self, platform: str, items: list[AccountRecord], field_keys: list[str]) -> list[dict[str, object]]:
        if platform == CHATGPT_PLATFORM:
            base_payloads = [_chatgpt_export_payload(item) for item in items]
        elif platform == CODEBANANA_PLATFORM:
            base_payloads = [_codebanana_export_payload(item) for item in items]
        elif platform == ATXP_PLATFORM:
            base_payloads = [_atxp_export_payload(item) for item in items]
        elif platform == VENICE_PLATFORM:
            base_payloads = [_venice_export_payload(item) for item in items]
        else:
            base_payloads = [{} for _ in items]
        rows = []
        for item, base_payload in zip(items, base_payloads):
            generic_payload = _generic_export_payload(item, field_keys)
            generic_payload.update({key: value for key, value in base_payload.items() if value not in (None, "")})
            rows.append(generic_payload)
        return rows
    def _export_prefix(self, platform: str) -> str:
        if platform == CHATGPT_PLATFORM:
            return "accounts"
        return f"{platform}_accounts"

    @staticmethod
    def _normalize_json_value(value: object) -> object:
        return None if value in ("", None) else value

    @staticmethod
    def _normalize_text_value(value: object) -> str:
        if value in ("", None):
            return ""
        return str(value)

    def _load_chatgpt_items(self, selection: AccountExportSelection) -> list[AccountRecord]:
        if self._normalize_platform(selection) != CHATGPT_PLATFORM:
            raise ValueError("仅支持 ChatGPT 账号导出")
        return self.repository.select_for_export(selection)
