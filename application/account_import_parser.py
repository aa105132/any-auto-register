from __future__ import annotations

import ast
import csv
import json
import re

from domain.accounts import AccountImportLine


IMPORT_LINE_RE = re.compile(
    r'^\s*(?P<email>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'\s+(?P<password>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\S+)'
    r'(?:\s+(?P<extra>.*))?\s*$'
)

def _decode_import_token(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(text)
            return decoded if isinstance(decoded, str) else str(decoded)
        except Exception:
            return text[1:-1]
    return text


def _parse_csv_row(raw: str) -> list[str]:
    return next(csv.reader([raw]))


def build_luckmail_provider_payload(email: str, token: str) -> dict:
    provider_account = {
        "provider_type": "mailbox",
        "provider_name": "luckmail",
        "login_identifier": email,
        "display_name": email,
        "credentials": {
            "purchase_token": token,
        },
        "metadata": {
            "email": email,
            "purchase_token": token,
            "api_base_url": "https://mails.luckyous.com",
            "source": "luckmail_purchase",
        },
    }
    provider_resource = {
        "provider_type": "mailbox",
        "provider_name": "luckmail",
        "resource_type": "mailbox",
        "resource_identifier": token,
        "handle": email,
        "display_name": email,
        "metadata": {
            "email": email,
            "purchase_token": token,
            "api_base_url": "https://mails.luckyous.com",
            "source": "luckmail_purchase",
        },
    }
    return {
        "mail_provider": "luckmail",
        "luckmail_email": email,
        "luckmail_purchase_token": token,
        "provider_accounts": [provider_account],
        "provider_resources": [provider_resource],
        "overview": {
            "remote_email": email,
            "mail_source": "luckmail_purchase",
        },
    }


def _merge_dict(base: dict, incoming: dict) -> dict:
    merged = dict(base)
    for key, value in (incoming or {}).items():
        if key in {"provider_accounts", "provider_resources"}:
            current = list(merged.get(key) or [])
            current.extend(list(value or []))
            merged[key] = current
            continue
        if key == "overview":
            merged[key] = {
                **dict(merged.get(key) or {}),
                **dict(value or {}),
            }
            continue
        merged[key] = value
    return merged


def _parse_luckmail_tail(raw_tail: str) -> tuple[str, dict]:
    tail = str(raw_tail or "").strip()
    if not tail:
        return "", {}
    if tail.startswith("{") and tail.endswith("}"):
        try:
            decoded = json.loads(tail)
            if isinstance(decoded, dict):
                password = str(decoded.pop("password", "") or "")
                return password, decoded
        except Exception:
            return "", {"note": tail}
    return _decode_import_token(tail), {}


def parse_account_import_lines(lines: list[str]) -> list[AccountImportLine]:
    parsed: list[AccountImportLine] = []
    csv_header: list[str] | None = None
    for line in lines:
        raw = str(line or "").strip()
        if not raw:
            continue

        if "----" in raw:
            parts = [part.strip() for part in raw.split("----", 2)]
            email = _decode_import_token(parts[0]) if len(parts) >= 1 else ""
            token = _decode_import_token(parts[1]) if len(parts) >= 2 else ""
            tail = parts[2] if len(parts) >= 3 else ""
            if email and token.startswith("tok_"):
                password, tail_extra = _parse_luckmail_tail(tail)
                extra = _merge_dict(build_luckmail_provider_payload(email, token), tail_extra)
                parsed.append(AccountImportLine(email=email, password=password, extra=extra))
                continue

        if csv_header is None and "," in raw:
            try:
                header_candidate = [item.strip().lower() for item in _parse_csv_row(raw)]
            except Exception:
                header_candidate = []
            if "email" in header_candidate and "password" in header_candidate:
                csv_header = header_candidate
                continue
        if csv_header is not None:
            try:
                values = _parse_csv_row(raw)
            except Exception:
                values = []
            if values:
                row = {
                    csv_header[index]: values[index]
                    for index in range(min(len(csv_header), len(values)))
                }
                email = str(row.get("email", "") or "").strip()
                password = str(row.get("password", "") or "")
                if email and password and "@" in email and " " not in email:
                    extra = {}
                    cashier_url = str(row.get("cashier_url", "") or "").strip()
                    if cashier_url:
                        extra["cashier_url"] = cashier_url
                    parsed.append(AccountImportLine(email=email, password=password, extra=extra))
                    continue

        match = IMPORT_LINE_RE.match(raw)
        if not match:
            continue
        email = _decode_import_token(match.group("email"))
        password = _decode_import_token(match.group("password"))
        extra = {}
        payload = (match.group("extra") or "").strip()
        if payload:
            try:
                decoded = json.loads(payload)
                if isinstance(decoded, dict):
                    extra = decoded
                elif decoded not in (None, ""):
                    extra = {"cashier_url": str(decoded)}
            except Exception:
                extra = {"cashier_url": _decode_import_token(payload)}
        parsed.append(AccountImportLine(email=email, password=password, extra=extra))
    return parsed
