from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any


class TwoAPIKeyStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except Exception:
            return []
        return list(data) if isinstance(data, list) else []

    def _save(self, rows: list[dict[str, Any]]) -> None:
        self.path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    def list(self) -> list[dict[str, Any]]:
        return self._load()

    def create(self, *, plugin: str = "thesys", note: str = "") -> dict[str, Any]:
        rows = self._load()
        key = "twoapi_" + secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
        row = {
            "id": secrets.token_hex(8),
            "key": key,
            "plugin": plugin,
            "note": note,
            "enabled": True,
            "created_at": int(time.time()),
        }
        rows.append(row)
        self._save(rows)
        return row

    def delete(self, key_id: str) -> bool:
        rows = self._load()
        next_rows = [row for row in rows if str(row.get("id")) != str(key_id)]
        if len(next_rows) == len(rows):
            return False
        self._save(next_rows)
        return True

    def verify(self, key: str, *, plugin: str = "") -> bool:
        for row in self._load():
            if not row.get("enabled"):
                continue
            if row.get("key") != key:
                continue
            return not plugin or row.get("plugin") in (plugin, "*")
        return False
