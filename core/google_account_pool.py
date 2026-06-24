"""Google 账号池 — 多 OAuth 平台共享复用。

池文件: output/google_accounts_pool.json
账号格式: HStockPlus 返回的 email----password 或 email|password

使用方式:
  pool = GoogleAccountPool()
  acct = pool.acquire(exclude_platforms=["gettoken"])  # 取一个未注册过 gettoken 的
  if not acct:
      raise ...  # 池子空了，需要购买新账号
  # ... 完成 OAuth 注册后 ...
  pool.mark_registered(acct.email, "gettoken")
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path


POOL_PATH = Path("output").joinpath("google_accounts_pool.json")
_POOL_LOCKS: dict[str, threading.RLock] = {}
_POOL_LOCKS_GUARD = threading.Lock()


@dataclass
class GooglePoolAccount:
    email: str
    password: str
    added_at: str = ""
    expires_at: str = ""
    source: str = ""
    source_order_id: str = ""
    registered_platforms: list[str] = field(default_factory=list)
    notes: str = ""
    status: str = "valid"
    reserved_platforms: list[str] = field(default_factory=list)
    totp_secret: str = ""


class GoogleAccountPool:
    """线程安全的 Google 账号池。"""

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

    def list_all(self) -> list[GooglePoolAccount]:
        data = self._read()
        return [GooglePoolAccount(**item) for item in data.get("accounts", [])]

    @staticmethod
    def _platform_key(platform: str) -> str:
        return str(platform or "").strip().lower()

    @staticmethod
    def _normalize_platforms(platforms) -> list[str]:
        """归一化平台列表：传字符串时自动包成单元素列表，避免 set("vellum")
        把平台名拆成单字符污染 reserved_platforms。"""
        if platforms is None:
            return []
        if isinstance(platforms, str):
            return [platforms]
        return list(platforms or [])

    @staticmethod
    def _reserved_platforms(item: dict) -> list[str]:
        reserved = item.get("reserved_platforms", [])
        return reserved if isinstance(reserved, list) else []

    def acquire(self, exclude_platforms: list[str] | None = None) -> GooglePoolAccount | None:
        """取一个可用账号，排除已注册或正在分配给指定平台的账号。"""
        return self.reserve(exclude_platforms=exclude_platforms)

    def reserve(self, exclude_platforms: list[str] | None = None) -> GooglePoolAccount | None:
        """原子占用一个可用账号，避免并发任务拿到同一 Google 账号。"""
        with self._lock:
            data = self._read()
            exclude = {self._platform_key(platform) for platform in self._normalize_platforms(exclude_platforms) if self._platform_key(platform)}
            accounts = data.get("accounts", [])
            for item in accounts:
                if str(item.get("status") or "valid").strip().lower() == "invalid":
                    continue
                registered = {self._platform_key(platform) for platform in item.get("registered_platforms", [])}
                reserved = {self._platform_key(platform) for platform in self._reserved_platforms(item)}
                if registered & exclude or reserved & exclude:
                    continue
                if exclude:
                    merged_reserved = list(self._reserved_platforms(item))
                    existing = {self._platform_key(platform) for platform in merged_reserved}
                    for platform in exclude:
                        if platform not in existing:
                            merged_reserved.append(platform)
                    item["reserved_platforms"] = merged_reserved
                    self._write(data)
                return GooglePoolAccount(**{key: value for key, value in item.items() if key in GooglePoolAccount.__dataclass_fields__})
        return None

    def release(self, email: str, platform: str = "") -> bool:
        """释放账号占用；注册失败或取消后调用。"""
        email_lower = (email or "").strip().lower()
        if not email_lower:
            return False
        platform_norm = self._platform_key(platform)
        with self._lock:
            data = self._read()
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() != email_lower:
                    continue
                reserved = self._reserved_platforms(item)
                if platform_norm:
                    reserved = [p for p in reserved if self._platform_key(p) != platform_norm]
                else:
                    reserved = []
                if reserved:
                    item["reserved_platforms"] = reserved
                else:
                    item.pop("reserved_platforms", None)
                self._write(data)
                return True
        return False


    def get_by_email(self, email: str, exclude_platforms: list[str] | None = None) -> GooglePoolAccount | None:
        """按邮箱指定并原子占用账号，可选择排除已注册或正在分配的平台。"""
        email_lower = (email or "").strip().lower()
        if not email_lower:
            return None
        with self._lock:
            data = self._read()
            exclude = {self._platform_key(platform) for platform in self._normalize_platforms(exclude_platforms) if self._platform_key(platform)}
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() != email_lower:
                    continue
                if str(item.get("status") or "valid").strip().lower() == "invalid":
                    return None
                registered = {self._platform_key(platform) for platform in item.get("registered_platforms", [])}
                reserved = {self._platform_key(platform) for platform in self._reserved_platforms(item)}
                if registered & exclude or reserved & exclude:
                    return None
                if exclude:
                    merged_reserved = list(self._reserved_platforms(item))
                    existing = {self._platform_key(platform) for platform in merged_reserved}
                    for platform in exclude:
                        if platform not in existing:
                            merged_reserved.append(platform)
                    item["reserved_platforms"] = merged_reserved
                    self._write(data)
                return GooglePoolAccount(**item)
        return None

    def mark_registered(self, email: str, platform: str) -> bool:
        """标记一个账号已在指定平台注册完成。"""
        email_lower = (email or "").strip().lower()
        if not email_lower:
            return False
        with self._lock:
            data = self._read()
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() == email_lower:
                    registered = list(item.get("registered_platforms", []))
                    platform_norm = self._platform_key(platform)
                    if platform_norm not in [self._platform_key(p) for p in registered]:
                        registered.append(platform)
                    item["registered_platforms"] = registered
                    reserved = [p for p in self._reserved_platforms(item) if self._platform_key(p) != platform_norm]
                    if reserved:
                        item["reserved_platforms"] = reserved
                    else:
                        item.pop("reserved_platforms", None)
                    self._write(data)
                    return True
        return False

    def mark_invalid(self, email: str, reason: str = "") -> bool:
        """标记 Google 账号失效；失效账号不会被复用。"""
        email_lower = (email or "").strip().lower()
        if not email_lower:
            return False
        with self._lock:
            data = self._read()
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() != email_lower:
                    continue
                item["status"] = "invalid"
                note = str(reason or "").strip()
                if note:
                    item["notes"] = note
                self._write(data)
                return True
        return False

    def mark_valid(self, email: str) -> bool:
        """恢复 Google 账号为有效。"""
        email_lower = (email or "").strip().lower()
        if not email_lower:
            return False
        with self._lock:
            data = self._read()
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() != email_lower:
                    continue
                item["status"] = "valid"
                self._write(data)
                return True
        return False

    def delete_invalid(self) -> dict:
        """删除所有已标记失效的 Google 账号。"""
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

    def add_account(
        self,
        email: str,
        password: str,
        *,
        source: str = "manual",
        source_order_id: str = "",
        expires_at: str = "",
        registered_platforms: list[str] | None = None,
    ) -> None:
        """添加账号到池中。已存在的会跳过（不覆盖密码）。"""
        email_lower = (email or "").strip().lower()
        if not email_lower or not password:
            return
        with self._lock:
            data = self._read()
            for item in data.get("accounts", []):
                if (item.get("email") or "").strip().lower() == email_lower:
                    return
            accounts = data.get("accounts", [])
            from datetime import datetime, timezone
            accounts.append({
                "email": email.strip(),
                "password": password,
                "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at": expires_at or "",
                "source": source,
                "source_order_id": str(source_order_id or ""),
                "registered_platforms": list(registered_platforms or []),
                "notes": "",
                "status": "valid",
            })
            data["accounts"] = accounts
            self._write(data)


    @staticmethod
    def parse_account_line(line: str) -> tuple[str, str] | None:
        """解析手动导入行，支持 email|password、email----password、email,password、email password。"""
        value = (line or "").strip()
        if not value or value.startswith("#"):
            return None
        separators = ["----", "|", ",", "	", " "]
        parts: list[str] = []
        for sep in separators:
            if sep in value:
                parts = [part.strip() for part in value.split(sep) if part.strip()]
                break
        if not parts:
            parts = [value]
        email = next((part for part in parts if "@" in part), "")
        if not email:
            return None
        email_index = parts.index(email)
        password = ""
        if email_index + 1 < len(parts):
            password = parts[email_index + 1]
        else:
            password = next((part for part in parts if part != email), "")
        if not password:
            return None
        return email, password

    def import_lines(self, lines: list[str], *, source: str = "manual", source_order_id: str = "", expires_at: str = "") -> dict:
        created = 0
        skipped = 0
        duplicates = 0
        invalid = 0
        for raw in lines or []:
            parsed = self.parse_account_line(raw)
            if not parsed:
                invalid += 1
                continue
            email, password = parsed
            before = len(self.list_all())
            self.add_account(email, password, source=source or "manual", source_order_id=source_order_id, expires_at=expires_at)
            after = len(self.list_all())
            if after > before:
                created += 1
            else:
                duplicates += 1
                skipped += 1
        return {"created": created, "duplicates": duplicates, "invalid": invalid, "skipped": skipped, "total": created + duplicates + invalid}

    def add_from_hstockplus_line(self, line: str, order_id: str = "") -> bool:
        """从 HStockPlus 返回的原始行添加账号。格式: email----password 或 email|password"""
        value = (line or "").strip()
        if not value:
            return False
        if "----" in value:
            parts = [p.strip() for p in value.split("----")]
        elif "|" in value:
            parts = [p.strip() for p in value.split("|")]
        else:
            return False
        email = next((p for p in parts if "@" in p), "")
        password = next((p for p in parts if p != email), "")
        if not email or not password:
            return False
        self.add_account(email, password, source="hstockplus", source_order_id=str(order_id or ""))
        return True

    def stats(self) -> dict:
        """返回池统计信息。"""
        accounts = self.list_all()
        total = len(accounts)
        platform_counts: dict[str, int] = {}
        for a in accounts:
            for p in a.registered_platforms:
                platform_counts[p] = platform_counts.get(p, 0) + 1
        unused = sum(1 for a in accounts if not a.registered_platforms)
        return {"total": total, "unused": unused, "by_platform": platform_counts}
