from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests

from services.twoapi.importer import TwoAPIImportSchema, import_twoapi_accounts
from services.twoapi.models import TwoAPIAccount, TwoAPISettings, mask_secret_in_text
from services.twoapi.plugins.zo import LocalJSONResponse

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "output"
ACCOUNT_DB_PATH = ROOT / "account_manager.db"
ANYCAP_NATIVE_BASE_URL = "https://api.anycap.ai"

ANYCAP_IMPORT_SCHEMA = TwoAPIImportSchema(
    plugin="anycap",
    platform="anycap",
    token_fields=("api_key", "ai_api_token", "token", "key", "anycap_api_key"),
    email_fields=("email", "account", "username", "user_email"),
    user_id_fields=("user_id", "uid", "id"),
    base_url_fields=("native_api_base", "base_url", "api_base"),
    default_base_url=ANYCAP_NATIVE_BASE_URL,
    token_prefixes=("ak_", "sk-"),
    min_token_length=24,
    credential_aliases=("api_key", "ai_api_token", "anycap_api_key"),
    primary_token_field="api_key",
    metadata_defaults={"native_anycap": True},
)

ANYCAP_MODELS: dict[str, list[dict[str, str]]] = {
    "image": [
        {"model": "gpt-image-1", "name": "gpt-image-1"},
        {"model": "flux-pro", "name": "flux-pro"},
    ],
    "video": [
        {"model": "kling-v1", "name": "kling-v1"},
        {"model": "runway-gen4", "name": "runway-gen4"},
    ],
    "music": [
        {"model": "suno-v4", "name": "suno-v4"},
    ],
}

SCHEMA_COMMON: dict[str, dict[str, Any]] = {
    "image": {
        "prompt": {"type": "string", "required": True},
        "aspect_ratio": {"type": "string", "required": False},
        "images": {"type": "array", "required": False},
        "mode": {"type": "string", "required": False},
    },
    "video": {
        "prompt": {"type": "string", "required": True},
        "aspect_ratio": {"type": "string", "required": False},
        "duration": {"type": "number", "required": False},
        "images": {"type": "array", "required": False},
        "mode": {"type": "string", "required": False},
    },
    "music": {
        "prompt": {"type": "string", "required": True},
        "lyrics": {"type": "string", "required": False},
        "style": {"type": "string", "required": False},
        "mode": {"type": "string", "required": False},
    },
}


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    from application.tasks import create_register_task as _create_register_task

    return _create_register_task(payload)


class _LazyTaskRuntime:
    def wake_up(self) -> None:
        from services.task_runtime import task_runtime as _task_runtime

        _task_runtime.wake_up()


task_runtime = _LazyTaskRuntime()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    text = value.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_json_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace") or "[]")
    except Exception:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _extract_api_key(record: dict[str, Any]) -> str:
    for key in ("api_key", "ai_api_token", "anycap_api_key", "token", "key"):
        value = _safe_text(record.get(key))
        if value.startswith(("ak_", "sk-")) and len(value) >= 24:
            return value
    return ""


def _normalize_base_url(value: Any) -> str:
    text = _safe_text(value).rstrip("/")
    if not text:
        return ANYCAP_NATIVE_BASE_URL
    if text.endswith("/v1"):
        return text[: -len("/v1")]
    if text.startswith("https://api.anycap.ai"):
        return ANYCAP_NATIVE_BASE_URL
    return text


def _credit_amount_from_record(record: dict[str, Any], *, default: float = 100.0) -> float:
    for key in ("credit_amount", "credits", "balance"):
        if key in record:
            return _safe_float(record.get(key), default)
    credit = record.get("credit_result") or record.get("balance_result")
    if isinstance(credit, dict):
        for key in ("amount", "balance", "credits", "total"):
            if key in credit:
                return _safe_float(credit.get(key), default)
    return default


def _response_json_or_text(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": str(getattr(response, "text", "") or "")[:2000]}


class AnyCapTwoAPIPlugin:
    name = "anycap"
    display_name = "AnyCap 原生 2API 代理"

    def __init__(
        self,
        *,
        settings: TwoAPISettings | None = None,
        transport: requests.Session | Any | None = None,
        data_dir: Path | None = None,
        account_db_path: Path | None = None,
    ) -> None:
        self.settings = settings or TwoAPISettings(request_timeout=180.0)
        if float(getattr(self.settings, "request_timeout", 0) or 0) < 180.0:
            self.settings.request_timeout = 180.0
        self.transport = transport or requests.Session()
        self.data_dir = Path(data_dir or OUT_DIR)
        if account_db_path is not None:
            self.account_db_path = Path(account_db_path)
        elif data_dir is None or Path(data_dir).resolve() == OUT_DIR.resolve():
            self.account_db_path = ACCOUNT_DB_PATH
        else:
            self.account_db_path = self.data_dir / "account_manager.db"
        self.accounts: list[TwoAPIAccount] = []
        self._logs: list[str] = []
        self._cursor = 0

    def log(self, message: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {mask_secret_in_text(message)}"
        self._logs.append(line)
        if len(self._logs) > 1000:
            self._logs = self._logs[-1000:]
        log_dir = self.data_dir / "twoapi_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "anycap.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def recent_logs(self, *, limit: int = 200) -> list[str]:
        file_path = self.data_dir / "twoapi_logs" / "anycap.log"
        rows = list(self._logs)
        if file_path.exists():
            try:
                rows = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                pass
        return rows[-max(1, min(limit, 1000)):]

    def _credential_paths(self) -> list[Path]:
        return [self.data_dir / "anycap_credentials.json", self.data_dir / "anycap_e2e_result.json"]

    def _load_accounts_from_files(self) -> list[TwoAPIAccount]:
        records: list[dict[str, Any]] = []
        for path in self._credential_paths():
            records.extend(_load_json_records(path))
        keys_path = self.data_dir / "anycap_keys.txt"
        if keys_path.exists():
            for index, raw in enumerate(keys_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                parts = [part.strip() for part in raw.strip().split("|") if part.strip()]
                key = next((part for part in parts if part.startswith(("ak_", "sk-"))), "")
                if key:
                    email = next((part for part in parts if "@" in part), "") or f"anycap-key-{index}@local"
                    records.append({"email": email, "api_key": key, "source": "anycap_keys"})
        return self._accounts_from_records(records, source="file")

    def _load_accounts_from_database(self) -> list[TwoAPIAccount]:
        db_path = self.account_db_path
        if not db_path.exists():
            return []
        try:
            connection = sqlite3.connect(str(db_path))
            connection.row_factory = sqlite3.Row
        except Exception as exc:
            self.log(f"读取 AnyCap 账号数据库失败: {exc!r}")
            return []
        try:
            rows = connection.execute(
                "SELECT id, email FROM accounts WHERE lower(platform)='anycap' ORDER BY id ASC"
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                account_id = int(row["id"] or 0)
                email = _safe_text(row["email"])
                if not email:
                    continue
                credential_rows = connection.execute(
                    "SELECT key, value FROM account_credentials WHERE account_id=? AND provider_name='anycap'",
                    (account_id,),
                ).fetchall()
                credentials = {str(item["key"] or ""): str(item["value"] or "") for item in credential_rows}
                overview = connection.execute(
                    "SELECT summary_json FROM account_overviews WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                summary = _safe_dict(str(overview["summary_json"] or "")) if overview else {}
                legacy_extra = _safe_dict(summary.get("legacy_extra"))
                records.append({**legacy_extra, **credentials, "email": email, "account_id": account_id, "source": "account_database"})
            return self._accounts_from_records(records, source="account_database")
        except Exception as exc:
            self.log(f"读取 AnyCap 数据库账号失败: {exc!r}")
            return []
        finally:
            connection.close()

    def _accounts_from_records(self, records: list[dict[str, Any]], *, source: str) -> list[TwoAPIAccount]:
        accounts: list[TwoAPIAccount] = []
        for index, record in enumerate(records, start=1):
            api_key = _extract_api_key(record)
            if not api_key:
                continue
            email = _safe_text(record.get("email")) or f"anycap-key-{index}@local"
            amount = _credit_amount_from_record(record, default=100.0)
            base_url = _normalize_base_url(record.get("native_api_base") or record.get("api_base") or record.get("base_url"))
            metadata = {
                "source": _safe_text(record.get("source")) or source,
                "account_id": record.get("account_id"),
                "user_id": _safe_text(record.get("user_id")),
                "native_anycap": True,
            }
            accounts.append(
                TwoAPIAccount(
                    plugin=self.name,
                    email=email,
                    base_url=base_url,
                    api_key=api_key,
                    handle=_safe_text(record.get("user_id")),
                    credit_amount=amount,
                    credit_ok=amount >= float(self.settings.min_credit or 0.0),
                    enabled=bool(api_key),
                    last_status="native_ready",
                    metadata={key: value for key, value in metadata.items() if value not in (None, "")},
                )
            )
        return accounts

    def _merge_accounts(self, *groups: list[TwoAPIAccount]) -> list[TwoAPIAccount]:
        merged: list[TwoAPIAccount] = []
        by_key: dict[str, TwoAPIAccount] = {}
        by_email: dict[str, TwoAPIAccount] = {}
        for group in groups:
            for account in group:
                api_key = _safe_text(account.api_key)
                email_key = _safe_text(account.email).lower()
                existing = (by_key.get(api_key) if api_key else None) or by_email.get(email_key)
                if existing is None:
                    merged.append(account)
                    if api_key:
                        by_key[api_key] = account
                    if email_key:
                        by_email[email_key] = account
                    continue
                existing.enabled = existing.enabled or account.enabled
                if account.credit_amount > existing.credit_amount:
                    existing.credit_amount = account.credit_amount
                    existing.credit_ok = account.credit_ok
                existing.metadata = {**dict(account.metadata or {}), **dict(existing.metadata or {})}
        return merged

    def load_accounts(self) -> list[TwoAPIAccount]:
        file_accounts = self._load_accounts_from_files()
        db_accounts = self._load_accounts_from_database()
        self.accounts = self._merge_accounts(db_accounts, file_accounts)
        self.log(f"加载 AnyCap 账号 {len(self.accounts)} 个，其中 db={len(db_accounts)} file={len(file_accounts)}")
        return self.accounts

    def status(self) -> dict[str, Any]:
        self.load_accounts()
        available = [item for item in self.accounts if item.enabled and item.credit_ok and item.credit_amount >= self.settings.min_credit]
        return {
            "name": self.name,
            "display_name": self.display_name,
            "enabled": self.settings.enabled,
            "account_count": len(self.accounts),
            "available_count": len(available),
            "settings": self.settings.__dict__,
            "accounts": [item.to_public() for item in self.accounts],
            "recent_logs": self.recent_logs(limit=80),
        }

    def _account_is_eligible(self, account: TwoAPIAccount) -> bool:
        return bool(account.enabled and account.api_key and account.credit_ok and float(account.credit_amount or 0.0) >= self.settings.min_credit)

    def select_account(self) -> TwoAPIAccount:
        if not self.settings.enabled:
            raise RuntimeError("AnyCap 2API 已禁用")
        if not self.accounts:
            self.load_accounts()
        if not self.accounts and self.settings.auto_refill:
            self.refill_accounts(count=1, concurrency=1)
            self.load_accounts()
        if not self.accounts:
            raise RuntimeError("AnyCap 账号池为空")
        total = len(self.accounts)
        for offset in range(total):
            idx = (self._cursor + offset) % total
            account = self.accounts[idx]
            if not self._account_is_eligible(account):
                continue
            self._cursor = (idx + 1) % total
            self.log(f"选中 AnyCap 账号: {account.email}")
            return account
        raise RuntimeError("没有可用 AnyCap 账号：全部为空额度或凭据不可用")

    def _headers(self, account: TwoAPIAccount) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {account.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "any-auto-register-2api/1.0",
        }

    def _url(self, account: TwoAPIAccount, path: str) -> str:
        return f"{_normalize_base_url(account.base_url).rstrip('/')}/{path.lstrip('/')}"

    def local_status(self) -> LocalJSONResponse:
        account_count = len(self.accounts) if self.accounts else len(self.load_accounts())
        return LocalJSONResponse({"status": "success", "authenticated": True, "user": {"id": "local-adapter"}, "credits": 999999, "account_count": account_count})

    def models(self, capability: str) -> LocalJSONResponse:
        cap = _safe_text(capability).lower()
        return LocalJSONResponse({"status": "success", "capability": cap, "models": ANYCAP_MODELS.get(cap, [])})

    def schema(self, capability: str, model: str, *, mode: str = "") -> LocalJSONResponse:
        cap = _safe_text(capability).lower()
        default_mode = {"image": "text-to-image", "video": "text-to-video", "music": "text-to-music"}.get(cap, "generate")
        return LocalJSONResponse(
            {
                "status": "success",
                "schemas": [
                    {
                        "operation": "generate",
                        "mode": mode or default_mode,
                        "schema": {"model_params": SCHEMA_COMMON.get(cap, {"prompt": {"type": "string", "required": True}})},
                    }
                ],
                "model": model,
            }
        )

    def forward_generate(self, capability: str, payload: dict[str, Any]) -> Any:
        if not isinstance(payload, dict):
            raise RuntimeError("AnyCap payload 必须是 JSON object")
        account = self.select_account()
        path = f"/v1/{_safe_text(capability).lower()}/generate"
        response = self.transport.post(
            self._url(account, path),
            headers=self._headers(account),
            json=payload,
            timeout=self.settings.request_timeout,
        )
        account.last_status = "generate_alive" if getattr(response, "ok", False) else f"generate_error:{getattr(response, 'status_code', 0)}"
        return response

    def forward_capability_read(self, capability: str, payload: dict[str, Any]) -> Any:
        account = self.select_account()
        path = f"/v1/capabilities/{_safe_text(capability).lower()}/read"
        return self.transport.post(self._url(account, path), headers=self._headers(account), json=payload, timeout=self.settings.request_timeout)

    @property
    def import_schema(self) -> TwoAPIImportSchema:
        return ANYCAP_IMPORT_SCHEMA

    def _append_imported_credentials_file(self, result: dict[str, Any]) -> None:
        rows = []
        for item in list(result.get("accounts") or []):
            if not isinstance(item, dict):
                continue
            key = _safe_text(item.get("api_key") or item.get("token"))
            if not key:
                continue
            rows.append({"email": _safe_text(item.get("email")) or "anycap-import@local", "api_key": key, "source": _safe_text(result.get("source")) or "external_import", "credit_amount": _safe_float(item.get("credit_amount"), 100.0), "native_api_base": _safe_text(item.get("base_url")) or ANYCAP_NATIVE_BASE_URL})
        if not rows:
            return
        path = self.data_dir / "anycap_credentials.json"
        existing = _load_json_records(path)
        existing.extend(rows)
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in existing:
            key = _extract_api_key(row)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")

    def import_accounts(self, *, records: list[dict[str, Any]] | None = None, lines: list[str] | None = None, source: str = "external", repository: Any | None = None) -> dict[str, Any]:
        result = import_twoapi_accounts(self.import_schema, records=records, lines=lines, source=source, repository=repository)
        result["source"] = source
        self._append_imported_credentials_file(result)
        self.accounts = []
        self.log(f"导入 AnyCap 外部账号: created={result.get('created')} accepted={result.get('accepted')} skipped={result.get('skipped')}")
        return result

    def _push_target_import_url(self, target_url: str) -> str:
        base = str(target_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("target_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise ValueError("target_url 必须以 http:// 或 https:// 开头")
        if base.endswith("/2api/plugins/anycap/import") or base.endswith("/api/2api/plugins/anycap/import"):
            return base
        if base.endswith("/api"):
            return f"{base}/2api/plugins/anycap/import"
        return f"{base}/api/2api/plugins/anycap/import"

    def _account_to_push_record(self, account: TwoAPIAccount) -> dict[str, Any]:
        metadata = dict(account.metadata or {})
        return {
            "email": account.email,
            "api_key": account.api_key,
            "ai_api_token": account.api_key,
            "base_url": account.base_url or ANYCAP_NATIVE_BASE_URL,
            "native_api_base": account.base_url or ANYCAP_NATIVE_BASE_URL,
            "credit_amount": float(account.credit_amount or 0.0),
            "source": _safe_text(metadata.get("source")) or "anycap_local",
            "native_anycap": True,
            "ok": bool(account.enabled and account.api_key),
        }

    def _select_push_records(self, *, emails: list[str] | None = None, latest_only: bool = False) -> list[dict[str, Any]]:
        accounts = self.load_accounts()
        if emails:
            wanted = {str(email or "").strip().lower() for email in emails if str(email or "").strip()}
            accounts = [item for item in accounts if str(item.email or "").strip().lower() in wanted]
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for account in accounts:
            key = _safe_text(account.api_key)
            if not key or key in seen:
                continue
            seen.add(key)
            records.append(self._account_to_push_record(account))
        return records[-1:] if latest_only and records else records

    def push_accounts(self, target_url: str, *, source: str = "external-push", emails: list[str] | None = None, latest_only: bool = False, timeout: float = 30.0) -> dict[str, Any]:
        import_url = self._push_target_import_url(target_url)
        records = self._select_push_records(emails=emails, latest_only=latest_only)
        if not records:
            return {"ok": False, "pushed": 0, "target_url": import_url, "error": "没有匹配的 AnyCap 账号可推送"}
        response = self.transport.post(import_url, headers={"Content-Type": "application/json", "Accept": "application/json"}, json={"source": source or "external-push", "records": records}, timeout=max(1.0, float(timeout or 30.0)))
        data = _response_json_or_text(response)
        if not getattr(response, "ok", False):
            raise ValueError(f"推送 AnyCap 账号失败: status={getattr(response, 'status_code', 0)} body={str(data)[:500]}")
        self.log(f"推送 AnyCap 账号到远端完成: pushed={len(records)} target={import_url}")
        return {"ok": True, "pushed": len(records), "target_url": import_url, "remote": data}

    def refill_accounts(self, *, count: int = 1, concurrency: int = 1, executor_type: str = "cdp_protocol", extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "platform": "anycap",
            "count": max(1, min(int(count or 1), 100)),
            "concurrency": max(1, min(int(concurrency or 1), 20)),
            "executor_type": executor_type or "cdp_protocol",
            "identity_mode": "oauth_browser",
            "oauth_provider": "google",
            "extra": {**dict(extra or {}), "twoapi_auto_refill": True},
        }
        task = create_register_task(payload)
        task_runtime.wake_up()
        return {"ok": True, "task": task, "payload": payload}
