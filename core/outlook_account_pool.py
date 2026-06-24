"""Outlook 账号池 — 自注册产出的 Outlook/Hotmail 邮箱资产。

池文件: output/outlook_accounts_pool.json
账号格式（导入/导出行）: email----password----client_id----refresh_token

与 mailbox_inventory(outlook_token) 的关系：
  - mailbox_inventory 是给注册任务"领用"的运行时池，状态机驱动（unused/running/blacklisted）。
  - 本池是离线导出/手动导入的稳定资产视图，和 GoogleAccountPool 平行，
    便于不依赖数据库直接拷贝/备份/批量导入导出。
注册成功时由 OutlookBrowserRegister 双写：先写 mailbox_inventory 供复用，再写本池供导出。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path


POOL_PATH = Path("output").joinpath("outlook_accounts_pool.json")
_POOL_LOCKS: dict[str, threading.RLock] = {}
_POOL_LOCKS_GUARD = threading.Lock()


@dataclass
class OutlookPoolAccount:
    email: str
    password: str
    client_id: str = ""
    refresh_token: str = ""
    access_token: str = ""
    expires_at: str = ""
    added_at: str = ""
    source: str = "manual"
    status: str = "valid"
    notes: str = ""
    used_platforms: list[str] = field(default_factory=list)


class OutlookAccountPool:
    """线程安全的 Outlook 账号池。"""

    def __init__(self, pool_path: str | Path = ""):
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

    def _read(self) -> dict:
        if not self._path.is_file():
            return {"version": 1, "accounts": []}
        with self._path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def list_all(self) -> list[OutlookPoolAccount]:
        data = self._read()
        return [OutlookPoolAccount(**item) for item in data.get("accounts", [])]

    def get_by_email(self, email: str) -> OutlookPoolAccount | None:
        email_lower = (email or "").strip().lower()
        if not email_lower:
            return None
        with self._lock:
            data = self._read()
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() == email_lower:
                    return OutlookPoolAccount(**item)
        return None

    def add_account(
        self,
        email: str,
        password: str,
        *,
        client_id: str = "",
        refresh_token: str = "",
        access_token: str = "",
        expires_at: str = "",
        source: str = "manual",
        used_platforms: list[str] | None = None,
    ) -> bool:
        """添加账号到池中。已存在的邮箱会更新令牌信息（refresh_token/client_id/access_token/expires_at）。

        返回 True 表示新增，False 表示已存在并已更新。
        """
        email_lower = (email or "").strip().lower()
        if not email_lower or not password:
            return False
        with self._lock:
            data = self._read()
            accounts = data.get("accounts", [])
            for item in accounts:
                if (item.get("email") or "").strip().lower() == email_lower:
                    # 更新令牌信息（不覆盖密码为空、不覆盖 source）
                    if client_id:
                        item["client_id"] = client_id
                    if refresh_token:
                        item["refresh_token"] = refresh_token
                    if access_token:
                        item["access_token"] = access_token
                    if expires_at:
                        item["expires_at"] = expires_at
                    if used_platforms is not None:
                        item["used_platforms"] = list(used_platforms or [])
                    self._write(data)
                    return False
            from datetime import datetime, timezone
            accounts.append({
                "email": email.strip(),
                "password": password,
                "client_id": str(client_id or ""),
                "refresh_token": str(refresh_token or ""),
                "access_token": str(access_token or ""),
                "expires_at": str(expires_at or ""),
                "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": str(source or "manual"),
                "status": "valid",
                "notes": "",
                "used_platforms": list(used_platforms or []),
            })
            data["accounts"] = accounts
            self._write(data)
            return True

    def mark_invalid(self, email: str, reason: str = "") -> bool:
        email_lower = (email or "").strip().lower()
        if not email_lower:
            return False
        with self._lock:
            data = self._read()
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() == email_lower:
                    item["status"] = "invalid"
                    note = str(reason or "").strip()
                    if note:
                        item["notes"] = note
                    self._write(data)
                    return True
        return False

    def mark_valid(self, email: str) -> bool:
        email_lower = (email or "").strip().lower()
        if not email_lower:
            return False
        with self._lock:
            data = self._read()
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() == email_lower:
                    item["status"] = "valid"
                    self._write(data)
                    return True
        return False

    def delete_invalid(self) -> dict:
        with self._lock:
            data = self._read()
            accounts = list(data.get("accounts", []))
            kept = [
                item for item in accounts
                if str(item.get("status") or "valid").strip().lower() != "invalid"
            ]
            deleted = len(accounts) - len(kept)
            if deleted:
                data["accounts"] = kept
                self._write(data)
            return {
                "ok": True,
                "deleted": deleted,
                "remaining": len(kept),
                "total_before": len(accounts),
            }

    def import_lines(self, lines: list[str], *, source: str = "manual") -> dict:
        created = 0
        updated = 0
        invalid = 0
        for raw in lines or []:
            parsed = parse_outlook_pool_line(raw)
            if not parsed:
                invalid += 1
                continue
            email, password, client_id, refresh_token = parsed
            added = self.add_account(
                email, password,
                client_id=client_id, refresh_token=refresh_token,
                source=source,
            )
            if added:
                created += 1
            else:
                updated += 1
        return {
            "created": created,
            "updated": updated,
            "invalid": invalid,
            "total": created + updated + invalid,
        }

    def stats(self) -> dict:
        accounts = self.list_all()
        total = len(accounts)
        invalid = sum(1 for a in accounts if (a.status or "valid").lower() == "invalid")
        return {
            "total": total,
            "valid": total - invalid,
            "invalid": invalid,
        }


def parse_outlook_pool_line(line: str) -> tuple[str, str, str, str] | None:
    """解析导入行，返回 (email, password, client_id, refresh_token)。

    格式：email----password----client_id----refresh_token（四段，---- 分隔）。
    缺少 client_id 或 refresh_token 的行视为无效（无法用于 IMAP 收件）。
    """
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None
    parts = [part.strip() for part in text.split("----")]
    if len(parts) != 4:
        return None
    email, password, client_id, refresh_token = parts
    if not email or not password or not client_id or not refresh_token:
        return None
    if "@" not in email:
        return None
    return email, password, client_id, refresh_token
