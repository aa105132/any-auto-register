from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from domain.accounts import AccountImportLine

if TYPE_CHECKING:
    from infrastructure.accounts_repository import AccountsRepository


@dataclass(slots=True)
class TwoAPIImportSchema:
    """2API 外部账号导入声明。

    平台插件只需要声明 token/email/base_url 等字段别名，通用导入器负责把
    文本行或 JSON 记录转换为账号库可识别的 AccountImportLine。
    """

    plugin: str
    platform: str
    token_fields: tuple[str, ...] = ("api_key", "token", "key")
    email_fields: tuple[str, ...] = ("email", "account", "username")
    password_fields: tuple[str, ...] = ("password", "pass")
    user_id_fields: tuple[str, ...] = ("user_id", "uid")
    base_url_fields: tuple[str, ...] = ("base_url", "openai_base_url", "native_api_base")
    default_base_url: str = ""
    token_prefixes: tuple[str, ...] = ()
    min_token_length: int = 1
    credential_aliases: tuple[str, ...] = ("api_key",)
    primary_token_field: str = "api_key"
    default_credit_amount: float = 100.0
    metadata_defaults: dict[str, Any] = field(default_factory=dict)


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _lower_record(record: dict[str, Any]) -> dict[str, Any]:
    return {str(key).strip().lower(): value for key, value in dict(record or {}).items()}


def _looks_like_token(value: Any, schema: TwoAPIImportSchema) -> bool:
    text = _safe_text(value)
    if not text:
        return False
    if len(text) < max(1, int(schema.min_token_length or 1)):
        return False
    if not schema.token_prefixes:
        return True
    return any(text.startswith(prefix) for prefix in schema.token_prefixes)


def _first_value(record: dict[str, Any], fields: tuple[str, ...]) -> str:
    lowered = _lower_record(record)
    for field_name in fields:
        value = _safe_text(lowered.get(field_name.lower()))
        if value:
            return value
    return ""


def _record_from_key_value_parts(parts: list[str]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    positional: list[str] = []
    for part in parts:
        text = part.strip()
        if not text:
            continue
        if "=" in text:
            key, value = text.split("=", 1)
            record[key.strip()] = value.strip()
        else:
            positional.append(text)
    if positional:
        record["_positional"] = positional
    return record


def _csv_parts(line: str) -> list[str]:
    try:
        return next(csv.reader(io.StringIO(line)))
    except Exception:
        return [line]


def records_from_external_lines(lines: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in lines:
        text = _safe_text(raw)
        if not text or text.startswith("#"):
            continue
        if text.startswith("{") or text.startswith("["):
            try:
                data = json.loads(text)
            except Exception:
                data = None
            if isinstance(data, dict):
                records.append(data)
                continue
            if isinstance(data, list):
                records.extend(item for item in data if isinstance(item, dict))
                continue
        if "|" in text:
            records.append(_record_from_key_value_parts([part.strip() for part in text.split("|")]))
            continue
        records.append(_record_from_key_value_parts([part.strip() for part in _csv_parts(text)]))
    return records


def _token_from_record(record: dict[str, Any], schema: TwoAPIImportSchema) -> str:
    token = _first_value(record, schema.token_fields)
    if _looks_like_token(token, schema):
        return token
    for value in dict(record or {}).values():
        if isinstance(value, (dict, list, tuple, set)):
            continue
        if _looks_like_token(value, schema):
            return _safe_text(value)
    for value in list(record.get("_positional") or []):
        if _looks_like_token(value, schema):
            return _safe_text(value)
    return ""


def _email_from_record(record: dict[str, Any], schema: TwoAPIImportSchema, token: str, index: int) -> str:
    email = _first_value(record, schema.email_fields)
    if email:
        return email
    for value in list(record.get("_positional") or []):
        text = _safe_text(value)
        if "@" in text and not text.startswith("sk-"):
            return text
    digest = hashlib.sha1(token.encode("utf-8", errors="ignore")).hexdigest()[:10] if token else str(index)
    return f"{schema.plugin}-import-{digest}@local"


def _normalized_line(record: dict[str, Any], schema: TwoAPIImportSchema, *, source: str, index: int) -> AccountImportLine | None:
    token = _token_from_record(record, schema)
    if not token:
        return None
    email = _email_from_record(record, schema, token, index)
    password = _first_value(record, schema.password_fields)
    user_id = _first_value(record, schema.user_id_fields)
    base_url = _first_value(record, schema.base_url_fields) or schema.default_base_url

    credentials = dict(record.get("credentials") or {}) if isinstance(record.get("credentials"), dict) else {}
    for alias in schema.credential_aliases:
        credentials.setdefault(alias, token)
    credentials.setdefault(schema.primary_token_field, token)

    overview = dict(record.get("overview") or record.get("summary") or {}) if isinstance(record.get("overview") or record.get("summary"), dict) else {}
    overview.update({key: value for key, value in schema.metadata_defaults.items() if key not in overview})
    overview.setdefault("twoapi_import_source", source)
    if base_url:
        overview.setdefault("openai_base_url", base_url)

    extra = {
        **{key: value for key, value in dict(record or {}).items() if key != "_positional"},
        "credentials": credentials,
        "primary_token": token,
        "token": token,
        schema.primary_token_field: token,
        "lifecycle_status": _safe_text(record.get("lifecycle_status") or record.get("status") or "registered"),
        "overview": overview,
        "source": source,
    }
    if user_id:
        extra["user_id"] = user_id
    if base_url:
        extra["openai_base_url"] = base_url
        extra["base_url"] = base_url
    if "credit_amount" not in extra:
        extra["credit_amount"] = schema.default_credit_amount
    return AccountImportLine(email=email, password=password, extra=extra)


def build_import_lines(
    schema: TwoAPIImportSchema,
    *,
    records: list[dict[str, Any]] | None = None,
    lines: list[str] | None = None,
    source: str = "external",
) -> tuple[list[AccountImportLine], list[dict[str, Any]]]:
    raw_records: list[dict[str, Any]] = []
    raw_records.extend(item for item in list(records or []) if isinstance(item, dict))
    raw_records.extend(records_from_external_lines(list(lines or [])))

    parsed: list[AccountImportLine] = []
    errors: list[dict[str, Any]] = []
    seen_tokens: set[str] = set()
    for index, record in enumerate(raw_records, start=1):
        line = _normalized_line(record, schema, source=source, index=index)
        if line is None:
            errors.append({"index": index, "error": "missing_token"})
            continue
        token = _safe_text(line.extra.get(schema.primary_token_field) or line.extra.get("token"))
        if token in seen_tokens:
            errors.append({"index": index, "email": line.email, "error": "duplicate_token_in_request"})
            continue
        seen_tokens.add(token)
        parsed.append(line)
    return parsed, errors


def import_twoapi_accounts(
    schema: TwoAPIImportSchema,
    *,
    records: list[dict[str, Any]] | None = None,
    lines: list[str] | None = None,
    source: str = "external",
    repository: "AccountsRepository | None" = None,
) -> dict[str, Any]:
    parsed, errors = build_import_lines(schema, records=records, lines=lines, source=source)
    if not parsed:
        return {
            "plugin": schema.plugin,
            "platform": schema.platform,
            "created": 0,
            "accepted": 0,
            "skipped": len(errors),
            "errors": errors,
        }
    if repository is None:
        from infrastructure.accounts_repository import AccountsRepository

        repo = AccountsRepository()
    else:
        repo = repository
    created = repo.import_lines(schema.platform, parsed)
    return {
        "plugin": schema.plugin,
        "platform": schema.platform,
        "created": created,
        "accepted": len(parsed),
        "skipped": len(errors),
        "errors": errors,
        "accounts": [
            {
                "email": item.email,
                "has_token": bool(item.extra.get(schema.primary_token_field) or item.extra.get("token")),
                schema.primary_token_field: item.extra.get(schema.primary_token_field) or item.extra.get("token"),
                "token": item.extra.get("token") or item.extra.get(schema.primary_token_field),
                "base_url": item.extra.get("base_url") or item.extra.get("openai_base_url") or schema.default_base_url,
                "credit_amount": item.extra.get("credit_amount") or schema.default_credit_amount,
            }
            for item in parsed
        ],
    }
