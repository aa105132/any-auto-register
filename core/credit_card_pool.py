"""本地信用卡池。用于靶场平台注册后的绑卡步骤。"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POOL_PATH = Path("output").joinpath("credit_cards_pool.json")
_POOL_LOCKS: dict[str, threading.RLock] = {}
_POOL_LOCKS_GUARD = threading.Lock()


CARD_FIELD_KEYS = (
    "number",
    "exp_month",
    "exp_year",
    "cvv",
    "country",
    "address",
    "city",
    "postal_code",
    "state",
    "name",
)

LABEL_ALIASES = {
    "number": {"number", "card", "card_number", "cardno", "卡号", "银行卡号", "信用卡号"},
    "expiry": {"expiry", "expire", "exp", "有效期", "过期时间", "到期时间"},
    "exp_month": {"exp_month", "month", "月份", "月"},
    "exp_year": {"exp_year", "year", "年份", "年"},
    "cvv": {"cvv", "cvc", "security_code", "安全码"},
    "country": {"country", "country/region", "国家", "国家/地区", "地区"},
    "address": {"address", "billing_address", "billing address", "账单地址", "地址"},
    "city": {"city", "城市"},
    "postal_code": {"postal_code", "zip", "zip_code", "postcode", "邮编", "邮政编码"},
    "state": {"state", "province", "州", "州/省", "省"},
    "name": {"name", "cardholder", "cardholder_name", "持卡人", "姓名"},
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_digits(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _normalize_country(value: Any) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower()
    if lowered in {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a."}:
        return "US"
    return raw


def _normalize_exp_year(value: Any) -> str:
    year = _clean_digits(value)
    if len(year) == 2:
        return f"20{year}"
    return year


def _parse_expiry(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    parts = [part for part in re.split(r"[^0-9]+", raw) if part]
    if len(parts) >= 2:
        return _clean_digits(parts[0]), _normalize_exp_year(parts[1])
    digits = _clean_digits(raw)
    if len(digits) >= 6:
        return digits[:2], _normalize_exp_year(digits[2:6])
    if len(digits) >= 4:
        return digits[:2], _normalize_exp_year(digits[2:])
    return "", ""


def _brand_hint(number: str) -> str:
    digits = _clean_digits(number)
    if digits.startswith("4"):
        return "visa"
    if digits.startswith(("51", "52", "53", "54", "55")):
        return "mastercard"
    if digits.startswith(("34", "37")):
        return "amex"
    if digits.startswith("6"):
        return "discover"
    return "unknown"


def _normalize_label(label: str) -> str:
    text = re.sub(r"\s+", " ", str(label or "").strip().lower())
    text = text.replace("：", ":")
    text = text.replace("/", "/")
    for key, aliases in LABEL_ALIASES.items():
        if text in aliases:
            return key
        for alias in aliases:
            if alias and alias in text:
                return key
    return ""


def _split_structured_line(value: str) -> list[str]:
    for sep in ("----", "|", ",", "\t"):
        if sep in value:
            return [part.strip() for part in value.split(sep)]
    return []


def _normalize_card_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    data = dict(payload or {})
    nested = data.get("zo_card") or data.get("card")
    if isinstance(nested, dict):
        data = {**data, **nested}

    exp_month = _clean_digits(data.get("exp_month") or data.get("month") or data.get("expiry_month"))
    exp_year = _normalize_exp_year(data.get("exp_year") or data.get("year") or data.get("expiry_year"))
    expiry = data.get("expiry") or data.get("exp")
    if expiry and (not exp_month or not exp_year):
        parsed_month, parsed_year = _parse_expiry(expiry)
        exp_month = exp_month or parsed_month
        exp_year = exp_year or parsed_year

    card = {
        "number": _clean_digits(data.get("number") or data.get("card_number") or data.get("cardNo")),
        "exp_month": exp_month,
        "exp_year": exp_year,
        "cvv": _clean_digits(data.get("cvv") or data.get("cvc") or data.get("security_code")),
        "country": _normalize_country(data.get("country") or data.get("billing_country") or ""),
        "address": str(data.get("address") or data.get("billing_address") or data.get("line1") or "").strip(),
        "city": str(data.get("city") or data.get("billing_city") or "").strip(),
        "postal_code": str(data.get("postal_code") or data.get("zip") or data.get("billing_zip") or "").strip(),
        "state": str(data.get("state") or data.get("province") or data.get("billing_state") or "").strip(),
        "name": str(data.get("name") or data.get("cardholder") or data.get("cardholder_name") or "").strip(),
    }
    required = ("number", "exp_month", "exp_year", "cvv", "country", "address", "city", "postal_code", "state")
    if any(not card.get(key) for key in required):
        return None
    if not (13 <= len(card["number"]) <= 19):
        return None
    return card


def parse_credit_card_line(line: str) -> dict[str, str] | None:
    value = str(line or "").strip()
    if not value or value.startswith("#"):
        return None
    if value.startswith("{") and value.endswith("}"):
        try:
            data = json.loads(value)
        except Exception:
            return None
        return _normalize_card_payload(data if isinstance(data, dict) else {})

    parts = _split_structured_line(value)
    if not parts:
        return None

    payload: dict[str, Any] = {"number": parts[0] if parts else ""}
    if len(parts) >= 4 and ("/" in parts[1] or len(_clean_digits(parts[1])) in {4, 6}):
        payload.update(
            {
                "expiry": parts[1],
                "cvv": parts[2] if len(parts) > 2 else "",
                "country": parts[3] if len(parts) > 3 else "",
                "address": parts[4] if len(parts) > 4 else "",
                "city": parts[5] if len(parts) > 5 else "",
                "postal_code": parts[6] if len(parts) > 6 else "",
                "state": parts[7] if len(parts) > 7 else "",
                "name": parts[8] if len(parts) > 8 else "",
            }
        )
    else:
        for index, key in enumerate(CARD_FIELD_KEYS):
            if index < len(parts):
                payload[key] = parts[index]
    return _normalize_card_payload(payload)


def parse_credit_card_import_lines(lines: list[str]) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    current_block: dict[str, str] = {}

    def flush_block() -> None:
        nonlocal current_block
        if current_block:
            card = _normalize_card_payload(current_block)
            if card:
                cards.append(card)
            current_block = {}

    for raw in lines or []:
        line = str(raw or "").strip()
        if not line:
            flush_block()
            continue

        label = ""
        value = ""
        if "：" in line or ":" in line:
            separator = "：" if "：" in line else ":"
            left, right = line.split(separator, 1)
            label = _normalize_label(left)
            value = right.strip()
        if label:
            if label == "expiry":
                current_block["expiry"] = value
            else:
                current_block[label] = value
            continue

        parsed = parse_credit_card_line(line)
        if parsed:
            flush_block()
            cards.append(parsed)

    flush_block()
    return cards


class CreditCardPool:
    """线程安全的本地信用卡池。"""

    def __init__(self, pool_path: str | Path = "") -> None:
        self._path = Path(pool_path) if pool_path else POOL_PATH
        self._lock = self._get_lock(self._path)

    @staticmethod
    def _get_lock(pool_path: str | Path) -> threading.RLock:
        lock_key = str(Path(pool_path or POOL_PATH).resolve())
        with _POOL_LOCKS_GUARD:
            lock = _POOL_LOCKS.get(lock_key)
            if lock is None:
                lock = threading.RLock()
                _POOL_LOCKS[lock_key] = lock
            return lock

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {"version": 1, "cards": []}
        with self._path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, dict):
            return {"version": 1, "cards": []}
        cards = data.get("cards")
        if not isinstance(cards, list):
            data["cards"] = []
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)

    @staticmethod
    def _serialize(item: dict[str, Any]) -> dict[str, Any]:
        card = _normalize_card_payload(item) or {key: str(item.get(key) or "") for key in CARD_FIELD_KEYS}
        number = _clean_digits(card.get("number"))
        return {
            "id": str(item.get("id") or ""),
            **card,
            "last4": number[-4:] if len(number) >= 4 else "",
            "brand_hint": _brand_hint(number),
            "source": str(item.get("source") or "manual"),
            "status": str(item.get("status") or "valid"),
            "note": str(item.get("note") or ""),
            "usage_count": int(item.get("usage_count") or 0),
            "used_platforms": list(item.get("used_platforms") or []),
            "last_used_at": str(item.get("last_used_at") or ""),
            "last_used_platform": str(item.get("last_used_platform") or ""),
            "last_used_email": str(item.get("last_used_email") or ""),
            "added_at": str(item.get("added_at") or ""),
            "updated_at": str(item.get("updated_at") or ""),
        }

    def list_all(self, *, status: str = "") -> list[dict[str, Any]]:
        target_status = str(status or "").strip().lower()
        with self._lock:
            items = [self._serialize(item) for item in self._read().get("cards", [])]
        if target_status:
            items = [item for item in items if str(item.get("status") or "").strip().lower() == target_status]
        return items

    def stats(self) -> dict[str, Any]:
        items = self.list_all()
        total = len(items)
        invalid = sum(1 for item in items if str(item.get("status") or "valid").lower() == "invalid")
        valid = total - invalid
        used = sum(1 for item in items if int(item.get("usage_count") or 0) > 0)
        by_brand: dict[str, int] = {}
        for item in items:
            brand = str(item.get("brand_hint") or "unknown")
            by_brand[brand] = by_brand.get(brand, 0) + 1
        return {"total": total, "valid": valid, "invalid": invalid, "used": used, "by_brand": by_brand}

    def import_lines(self, lines: list[str], *, source: str = "manual") -> dict[str, int]:
        parsed = parse_credit_card_import_lines(lines or [])
        created = 0
        updated = 0
        duplicates = 0
        invalid = max(len([line for line in lines or [] if str(line or "").strip()]) - len(parsed), 0)
        now = _utcnow()
        with self._lock:
            data = self._read()
            cards = list(data.get("cards", []))
            for card in parsed:
                number = _clean_digits(card.get("number"))
                existing = next((item for item in cards if _clean_digits(item.get("number")) == number), None)
                if existing:
                    existing.update(card)
                    existing["source"] = str(source or existing.get("source") or "manual")
                    existing["status"] = str(existing.get("status") or "valid")
                    existing["updated_at"] = now
                    updated += 1
                    duplicates += 1
                    continue
                cards.append(
                    {
                        "id": uuid.uuid4().hex,
                        **card,
                        "source": str(source or "manual"),
                        "status": "valid",
                        "note": "",
                        "usage_count": 0,
                        "used_platforms": [],
                        "last_used_at": "",
                        "last_used_platform": "",
                        "last_used_email": "",
                        "added_at": now,
                        "updated_at": now,
                    }
                )
                created += 1
            data["version"] = 1
            data["cards"] = cards
            self._write(data)
        return {"created": created, "updated": updated, "duplicates": duplicates, "invalid": invalid, "skipped": invalid, "total": created + updated + invalid}

    def get_default(self) -> dict[str, Any] | None:
        items = [item for item in self.list_all() if str(item.get("status") or "valid").strip().lower() != "invalid"]
        if not items:
            return None
        items.sort(key=lambda item: (int(item.get("usage_count") or 0), str(item.get("added_at") or "")))
        selected = dict(items[0])
        selected["_pool_id"] = selected.get("id") or ""
        selected["_pool_path"] = str(self._path)
        return selected

    def mark_used(self, card_id: str, *, platform: str = "", account_email: str = "") -> bool:
        normalized_id = str(card_id or "").strip()
        if not normalized_id:
            return False
        now = _utcnow()
        platform_key = str(platform or "").strip()
        with self._lock:
            data = self._read()
            for item in data.get("cards", []):
                if str(item.get("id") or "") != normalized_id:
                    continue
                item["usage_count"] = int(item.get("usage_count") or 0) + 1
                item["last_used_at"] = now
                item["last_used_platform"] = platform_key
                item["last_used_email"] = str(account_email or "").strip()
                platforms = [str(value) for value in item.get("used_platforms") or [] if str(value or "").strip()]
                if platform_key and platform_key.lower() not in {value.lower() for value in platforms}:
                    platforms.append(platform_key)
                item["used_platforms"] = platforms
                item["updated_at"] = now
                self._write(data)
                return True
        return False

    def mark_invalid(self, card_id: str, *, reason: str = "") -> bool:
        return self._set_status(card_id, "invalid", note=reason)

    def mark_valid(self, card_id: str) -> bool:
        return self._set_status(card_id, "valid")

    def _set_status(self, card_id: str, status: str, *, note: str = "") -> bool:
        normalized_id = str(card_id or "").strip()
        if not normalized_id:
            return False
        with self._lock:
            data = self._read()
            for item in data.get("cards", []):
                if str(item.get("id") or "") != normalized_id:
                    continue
                item["status"] = status
                if note:
                    item["note"] = note
                item["updated_at"] = _utcnow()
                self._write(data)
                return True
        return False

    def delete_invalid(self) -> dict[str, int | bool]:
        with self._lock:
            data = self._read()
            cards = list(data.get("cards", []))
            kept = [item for item in cards if str(item.get("status") or "valid").strip().lower() != "invalid"]
            deleted = len(cards) - len(kept)
            if deleted:
                data["cards"] = kept
                self._write(data)
            return {"ok": True, "deleted": deleted, "remaining": len(kept), "total_before": len(cards)}