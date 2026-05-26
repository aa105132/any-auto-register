from __future__ import annotations

import ast
import json
import sqlite3
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests

from services.twoapi.models import TwoAPIAccount, TwoAPISettings, mask_secret_in_text

try:
    from platforms.zo.core import ZoClient
except Exception:  # pragma: no cover - Zo 平台模块不可用时仅禁用实时余额刷新
    ZoClient = None  # type: ignore[assignment]

try:
    from scripts.deploy_zo_openai_proxy import DEFAULT_SOURCE_DIR, deploy_proxy, load_result_context
except Exception:  # pragma: no cover - 部署模块不可用时禁用自动重部署
    DEFAULT_SOURCE_DIR = None  # type: ignore[assignment]
    deploy_proxy = None  # type: ignore[assignment]
    load_result_context = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "output"
ZO_PROXY_URLS_PATH = OUT_DIR / "zo_proxy_urls.txt"
ZO_E2E_RESULT_PATH = OUT_DIR / "zo_e2e_result.json"
ACCOUNT_DB_PATH = ROOT / "account_manager.db"


MINIMAL_PERSONA_NAME = "2API Minimal"
MINIMAL_PERSONA_PROMPT = "."

ZO_MODEL_IDS = [
    "zo:openai/gpt-5.3-codex",
    "zo:openai/gpt-5.4",
    "zo:openai/gpt-5.5",
    "zo:openai/gpt-5.4-mini",
    "zo:anthropic/claude-opus-4-7",
    "zo:deepseek/deepseek-v4-pro",
    "zo:zai/glm-5",
]


class LocalJSONResponse:
    def __init__(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self) -> dict[str, Any]:
        return json.loads(self.text)

    def close(self) -> None:
        return None


class LocalSSEOpenAIResponse:
    def __init__(self, chunks: list[bytes], *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.headers = {"content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache"}
        self._chunks = chunks
        self.content = b"".join(chunks)
        self.text = self.content.decode("utf-8", errors="replace")

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self) -> None:
        return None


class StreamingSSEOpenAIResponse:
    def __init__(self, chunk_factory, *, upstream: Any | None = None, status_code: int = 200) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.headers = {"content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache"}
        self.content = b""
        self.text = ""
        self._chunk_factory = chunk_factory
        self._upstream = upstream
        self._closed = False

    def iter_content(self, chunk_size=None):
        try:
            yield from self._chunk_factory()
        finally:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._upstream, "close", None)
        if callable(close):
            close()


def _extract_key(text: str) -> str:
    match = re.search(r"zo_sk_[A-Za-z0-9_\-.]+", text or "")
    return match.group(0) if match else ""


def _extract_handle(base_url: str) -> str:
    match = re.search(r"https://([a-z0-9]+)\.zo\.space/", base_url or "")
    return match.group(1) if match else ""



def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _credit_amount_from_result(result: dict[str, Any]) -> float:
    data = result.get("data") if isinstance(result, dict) else {}
    if isinstance(data, dict):
        cents = data.get("available_balance_cents")
        if isinstance(cents, (int, float)) and not isinstance(cents, bool):
            return round(float(cents) / 100.0, 6)
    return _safe_float(result.get("amount") if isinstance(result, dict) else 0.0)


def _record_workspace(record: dict[str, Any]) -> dict[str, str]:
    workspace_result = dict(record.get("workspace_result") or {})
    workspace = dict(workspace_result.get("workspace") or {})
    handle = str(workspace.get("handle") or workspace_result.get("handle") or "").strip()
    origin = str(
        workspace.get("origin")
        or workspace.get("url")
        or workspace_result.get("workspace_origin")
        or workspace_result.get("workspace_url")
        or ""
    ).strip().rstrip("/")
    if not origin and handle:
        origin = f"https://{handle}.zo.computer"
    return {"handle": handle, "origin": origin}


def _record_matches_account(record: dict[str, Any], account: TwoAPIAccount) -> bool:
    email = str(record.get("email") or "").strip().lower()
    api_key = str(record.get("api_key") or "").strip()
    base_url = str(record.get("openai_proxy_base_url") or "").strip().rstrip("/")
    proxy_result = dict(record.get("proxy_deploy_result") or {})
    deployed_base_url = str(proxy_result.get("base_url") or "").strip().rstrip("/")
    workspace = _record_workspace(record)
    if email and email == account.email.lower():
        return True
    if api_key and api_key == account.api_key:
        return True
    if base_url and base_url == account.base_url:
        return True
    if deployed_base_url and deployed_base_url == account.base_url:
        return True
    return bool(account.handle and workspace.get("handle") == account.handle)


def _load_json_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


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
        try:
            data = ast.literal_eval(text)
        except Exception:
            return {}
    return data if isinstance(data, dict) else {}


def _legacy_extra_from_summary(summary: dict[str, Any]) -> dict[str, Any]:
    legacy = summary.get("legacy_extra")
    return legacy if isinstance(legacy, dict) else summary


def _extract_proxy_base_url(record: dict[str, Any]) -> str:
    base_url = str(record.get("openai_proxy_base_url") or "").strip().rstrip("/")
    if base_url:
        return base_url
    proxy_result = dict(record.get("proxy_deploy_result") or {})
    return str(proxy_result.get("base_url") or "").strip().rstrip("/")


def _normalize_proxy_base_url(value: Any) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text or "<" in text or "..." in text:
        return ""
    for suffix in ("/models", "/chat/completions"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if ".zo.space/v1/" not in text:
        return ""
    return text


def _walk_strings(value: Any):
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_strings(child)
        return
    if isinstance(value, list):
        for child in value:
            yield from _walk_strings(child)
        return
    if isinstance(value, str):
        yield value


def parse_zo_proxy_lines(lines: list[str]) -> list[TwoAPIAccount]:
    accounts: list[TwoAPIAccount] = []
    seen: set[tuple[str, str]] = set()
    for raw in lines:
        line = str(raw or "").strip()
        if not line or not line.startswith("zo|"):
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        email = parts[1].strip()
        base_url = parts[2].strip().rstrip("/")
        key = ""
        for part in parts[3:]:
            if part.startswith("zo_api_key="):
                key = part.split("=", 1)[1].strip()
        key = key or _extract_key(base_url)
        if not email or not base_url:
            continue
        dedupe = (email, base_url)
        if dedupe in seen:
            continue
        seen.add(dedupe)
        accounts.append(
            TwoAPIAccount(
                plugin="zo",
                email=email,
                base_url=base_url,
                api_key=key,
                handle=_extract_handle(base_url),
                credit_amount=100.0,
                credit_ok=True,
                metadata={"source": "zo_proxy_urls"},
            )
        )
    return accounts


class ZoTwoAPIPlugin:
    name = "zo"
    display_name = "Zo OpenAI 兼容代理"

    def __init__(
        self,
        *,
        settings: TwoAPISettings | None = None,
        transport: requests.Session | Any | None = None,
        data_dir: Path | None = None,
        account_db_path: Path | None = None,
    ) -> None:
        self.settings = settings or TwoAPISettings()
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
        with (log_dir / "zo.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def recent_logs(self, *, limit: int = 200) -> list[str]:
        file_path = self.data_dir / "twoapi_logs" / "zo.log"
        rows = list(self._logs)
        if file_path.exists():
            try:
                rows = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                pass
        return rows[-max(1, min(limit, 1000)):]


    def _result_paths(self) -> list[Path]:
        return [self.data_dir / "zo_e2e_result.json"]

    def _load_registration_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in self._result_paths():
            records.extend(_load_json_records(path))
        return records

    def _record_for_account(self, account: TwoAPIAccount) -> tuple[dict[str, Any], Path]:
        for path in self._result_paths():
            for record in _load_json_records(path):
                if _record_matches_account(record, account):
                    return record, path
        return {}, self.data_dir / "zo_e2e_result.json"

    def _apply_registration_snapshot(self, account: TwoAPIAccount, record: dict[str, Any]) -> None:
        credit_result = dict(record.get("credit_result") or record.get("balance_result") or {})
        amount = _credit_amount_from_result(credit_result)
        if credit_result:
            account.credit_amount = amount
            account.credit_ok = bool(credit_result.get("ok")) and amount >= float(self.settings.min_credit or 0.0)
        workspace = _record_workspace(record)
        if workspace.get("handle") and not account.handle:
            account.handle = workspace["handle"]
        metadata = dict(account.metadata or {})
        metadata.update(
            {
                "credit_source": str(credit_result.get("source") or "registration_snapshot"),
                "credit_checked_at": int(record.get("saved_at") or 0),
                "workspace_origin": workspace.get("origin") or "",
                "workspace_handle": workspace.get("handle") or account.handle,
            }
        )
        cookies = dict(record.get("cookies") or {})
        if cookies:
            metadata["cookies"] = cookies
        account.metadata = metadata

    def _enrich_accounts_from_registration_results(self, accounts: list[TwoAPIAccount]) -> None:
        records = self._load_registration_records()
        if not records:
            return
        for account in accounts:
            match = next((record for record in records if _record_matches_account(record, account)), None)
            if match:
                self._apply_registration_snapshot(account, match)

    def _load_proxy_deploy_artifacts(self) -> dict[str, dict[str, str]]:
        artifacts: dict[str, dict[str, str]] = {}
        for path in self.data_dir.glob("zo_proxy*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            handle = str(data.get("handle") or data.get("workspace_handle") or "").strip() if isinstance(data, dict) else ""
            persona_id = str(data.get("persona_id") or "").strip() if isinstance(data, dict) else ""
            for value in _walk_strings(data):
                for match in re.findall(r"https://[^\"'\\\s]+\.zo\.space/v1/[^\"'\\\s]+", value):
                    if "<" in match or "..." in match:
                        continue
                    base_url = _normalize_proxy_base_url(match)
                    api_key = _extract_key(base_url)
                    if not base_url or not api_key:
                        continue
                    if not handle:
                        handle = _extract_handle(base_url)
                    artifacts[api_key] = {
                        "base_url": base_url,
                        "handle": handle or _extract_handle(base_url),
                        "persona_id": persona_id,
                        "source": path.name,
                    }
        return artifacts

    def _load_accounts_from_database(self) -> list[TwoAPIAccount]:
        db_path = self.account_db_path
        if not db_path.exists():
            return []
        try:
            connection = sqlite3.connect(str(db_path))
            connection.row_factory = sqlite3.Row
        except Exception as exc:
            self.log(f"读取 Zo 账号数据库失败: {exc!r}")
            return []
        try:
            account_rows = connection.execute(
                "SELECT id, email FROM accounts WHERE lower(platform)='zo' ORDER BY id ASC"
            ).fetchall()
            accounts: list[TwoAPIAccount] = []
            for row in account_rows:
                account_id = int(row["id"] or 0)
                email = str(row["email"] or "").strip()
                if not email:
                    continue
                credential_rows = connection.execute(
                    "SELECT key, value, credential_type FROM account_credentials WHERE account_id=? AND provider_name='zo'",
                    (account_id,),
                ).fetchall()
                credentials = {str(item["key"] or ""): str(item["value"] or "") for item in credential_rows}
                api_key = credentials.get("api_key") or credentials.get("legacy_token") or ""
                overview = connection.execute(
                    "SELECT summary_json FROM account_overviews WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                summary = _safe_dict(str(overview["summary_json"] or "")) if overview else {}
                record = _legacy_extra_from_summary(summary)
                if not api_key:
                    api_key = str(record.get("api_key") or "").strip()
                base_url = _extract_proxy_base_url(record)
                workspace = _record_workspace(record)
                handle = _extract_handle(base_url) or workspace.get("handle") or ""
                account = TwoAPIAccount(
                    plugin="zo",
                    email=email,
                    base_url=base_url,
                    api_key=api_key,
                    handle=handle,
                    credit_amount=0.0,
                    credit_ok=False,
                    enabled=bool(base_url),
                    last_status="registered" if base_url else "proxy_missing",
                    metadata={
                        "source": "account_database",
                        "account_id": account_id,
                        "workspace_origin": workspace.get("origin") or "",
                        "workspace_handle": workspace.get("handle") or handle,
                    },
                )
                if record:
                    self._apply_registration_snapshot(account, record)
                    metadata = dict(account.metadata or {})
                    cookies = dict(metadata.get("cookies") or {})
                    has_access_token = bool(str(cookies.get("access_token") or metadata.get("access_token") or "").strip())
                    if base_url:
                        account.enabled = True
                    elif has_access_token and account.credit_ok:
                        account.enabled = True
                        account.last_status = "direct_ready"
                    else:
                        account.enabled = False
                        account.credit_ok = False
                        account.last_status = "proxy_missing"
                accounts.append(account)
            return accounts
        except Exception as exc:
            self.log(f"读取 Zo 数据库账号失败: {exc!r}")
            return []
        finally:
            connection.close()

    def _accounts_from_registration_records(self) -> list[TwoAPIAccount]:
        accounts: list[TwoAPIAccount] = []
        for record in self._load_registration_records():
            if not isinstance(record, dict):
                continue
            email = str(record.get("email") or "").strip()
            if not email:
                continue
            api_key = str(record.get("api_key") or record.get("ai_api_token") or "").strip()
            base_url = _extract_proxy_base_url(record)
            workspace = _record_workspace(record)
            handle = _extract_handle(base_url) or workspace.get("handle") or ""
            cookies = dict(record.get("cookies") or {})
            has_access_token = bool(str(cookies.get("access_token") or record.get("access_token") or "").strip())
            account = TwoAPIAccount(
                plugin="zo",
                email=email,
                base_url=base_url,
                api_key=api_key,
                handle=handle,
                credit_amount=0.0,
                credit_ok=False,
                enabled=bool(base_url or has_access_token),
                last_status="direct_ready" if has_access_token else ("proxy_imported" if base_url else "imported"),
                metadata={
                    "source": str(record.get("import_source") or "registration_snapshot"),
                    "workspace_origin": workspace.get("origin") or "",
                    "workspace_handle": workspace.get("handle") or handle,
                },
            )
            self._apply_registration_snapshot(account, record)
            if has_access_token and account.credit_ok:
                account.enabled = True
                account.last_status = "direct_ready"
            accounts.append(account)
        return accounts

    def _merge_accounts(self, accounts: list[TwoAPIAccount], extra_accounts: list[TwoAPIAccount]) -> list[TwoAPIAccount]:
        merged: list[TwoAPIAccount] = []
        by_email: dict[str, TwoAPIAccount] = {}
        by_key: dict[str, TwoAPIAccount] = {}

        def add(account: TwoAPIAccount) -> None:
            email_key = account.email.strip().lower()
            api_key = str(account.api_key or "").strip()
            existing = by_email.get(email_key) or (by_key.get(api_key) if api_key else None)
            if existing is None:
                merged.append(account)
                if email_key:
                    by_email[email_key] = account
                if api_key:
                    by_key[api_key] = account
                return
            if not existing.base_url and account.base_url:
                existing.base_url = account.base_url
                existing.handle = account.handle or existing.handle
                existing.enabled = account.enabled
                existing.last_status = account.last_status
            if not existing.api_key and account.api_key:
                existing.api_key = account.api_key
            if account.credit_amount > existing.credit_amount:
                existing.credit_amount = account.credit_amount
                existing.credit_ok = account.credit_ok
            existing.metadata = {**dict(account.metadata or {}), **dict(existing.metadata or {})}

        for item in accounts:
            add(item)
        for item in extra_accounts:
            add(item)
        return merged

    def _parse_import_line(self, line: str) -> dict[str, Any]:
        text = str(line or "").strip()
        if not text:
            return {}
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
        parts = [part.strip() for part in text.split("|")]
        if parts and parts[0].lower() == "zo":
            parts = parts[1:]
        record: dict[str, Any] = {}
        if parts:
            record["email"] = parts[0]
        for part in parts[1:]:
            if "=" not in part:
                if part.startswith("https://") and ".zo.space/" in part:
                    record["openai_proxy_base_url"] = part.rstrip("/")
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key in {"api_key", "zo_api_key", "ai_api_token"}:
                record["api_key"] = value
            elif key in {"access_token", "refresh_token"}:
                cookies = dict(record.get("cookies") or {})
                cookies[key] = value
                record["cookies"] = cookies
            elif key in {"handle", "workspace_handle"}:
                workspace_result = dict(record.get("workspace_result") or {})
                workspace = dict(workspace_result.get("workspace") or {})
                workspace["handle"] = value
                workspace_result["workspace"] = workspace
                record["workspace_result"] = workspace_result
            elif key in {"origin", "workspace_origin"}:
                workspace_result = dict(record.get("workspace_result") or {})
                workspace = dict(workspace_result.get("workspace") or {})
                workspace["origin"] = value.rstrip("/")
                workspace_result["workspace"] = workspace
                record["workspace_result"] = workspace_result
            elif key in {"credit", "balance"}:
                try:
                    amount = float(value)
                except ValueError:
                    amount = 0.0
                record["credit_result"] = {"ok": amount >= float(self.settings.min_credit or 0.0), "amount": amount}
        return record

    def _normalize_import_record(self, record: dict[str, Any], *, source: str) -> dict[str, Any]:
        item = dict(record or {})
        if not item:
            return {}
        email = str(item.get("email") or item.get("account") or "").strip()
        api_key = str(item.get("api_key") or item.get("ai_api_token") or item.get("zo_api_key") or "").strip()
        cookies = dict(item.get("cookies") or {})
        access = str(item.get("access_token") or cookies.get("access_token") or "").strip()
        refresh = str(item.get("refresh_token") or cookies.get("refresh_token") or "").strip()
        if access:
            cookies["access_token"] = access
        if refresh:
            cookies["refresh_token"] = refresh
        workspace_result = dict(item.get("workspace_result") or {})
        workspace = dict(workspace_result.get("workspace") or item.get("workspace") or {})
        handle = str(item.get("handle") or item.get("workspace_handle") or workspace.get("handle") or "").strip()
        origin = str(item.get("workspace_origin") or item.get("origin") or workspace.get("origin") or workspace.get("url") or "").strip().rstrip("/")
        if not origin and handle:
            origin = f"https://{handle}.zo.computer"
        if handle:
            workspace["handle"] = handle
        if origin:
            workspace["origin"] = origin
        if workspace:
            workspace_result["workspace"] = workspace
        base_url = str(item.get("openai_proxy_base_url") or item.get("base_url") or "").strip().rstrip("/")
        if not api_key and base_url:
            api_key = _extract_key(base_url)
        if not handle and base_url:
            handle = _extract_handle(base_url)
            if handle:
                workspace["handle"] = handle
                workspace_result["workspace"] = workspace
        if not email:
            email = str(item.get("username") or item.get("user") or "").strip()
        if not email and handle:
            email = f"{handle}@imported.local"
        if not email or not (api_key or access or base_url):
            return {}
        normalized = dict(item)
        normalized["email"] = email
        if api_key:
            normalized["api_key"] = api_key
        if cookies:
            normalized["cookies"] = cookies
        if workspace_result:
            normalized["workspace_result"] = workspace_result
        if base_url:
            normalized["openai_proxy_base_url"] = base_url
            normalized.setdefault("proxy_deploy_result", {"ok": True, "base_url": base_url})
        credit_result = dict(normalized.get("credit_result") or normalized.get("balance_result") or {})
        if not credit_result:
            amount = normalized.get("credit_amount") or normalized.get("credit") or normalized.get("balance")
            try:
                amount_float = float(amount)
            except (TypeError, ValueError):
                amount_float = 100.0 if access else 0.0
            credit_result = {"ok": amount_float >= float(self.settings.min_credit or 0.0), "amount": amount_float}
        normalized["credit_result"] = credit_result
        normalized["import_source"] = str(source or "external")
        normalized["imported_at"] = int(time.time())
        return normalized

    def _record_import_key(self, record: dict[str, Any]) -> str:
        email = str(record.get("email") or "").strip().lower()
        api_key = str(record.get("api_key") or record.get("ai_api_token") or "").strip()
        workspace = _record_workspace(record)
        handle = str(workspace.get("handle") or "").strip().lower()
        if email:
            return f"email:{email}"
        if api_key:
            return f"api:{api_key}"
        if handle:
            return f"handle:{handle}"
        return json.dumps(record, sort_keys=True, ensure_ascii=False)[:200]

    def import_accounts(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        lines: list[str] | None = None,
        source: str = "external",
    ) -> dict[str, Any]:
        incoming: list[dict[str, Any]] = []
        for record in records or []:
            if isinstance(record, dict):
                incoming.append(record)
        for line in lines or []:
            parsed = self._parse_import_line(line)
            if parsed:
                incoming.append(parsed)
        normalized = [self._normalize_import_record(item, source=source) for item in incoming]
        normalized = [item for item in normalized if item]
        if not normalized:
            return {"ok": True, "imported": 0, "updated": 0, "skipped": len(incoming), "account_count": len(self.accounts)}
        path = self.data_dir / "zo_e2e_result.json"
        existing = _load_json_records(path)
        by_key = {self._record_import_key(item): dict(item) for item in existing if isinstance(item, dict)}
        imported = updated = 0
        for item in normalized:
            key = self._record_import_key(item)
            if key in by_key:
                merged = {**by_key[key], **item}
                old_cookies = dict(by_key[key].get("cookies") or {})
                new_cookies = dict(item.get("cookies") or {})
                if old_cookies or new_cookies:
                    merged["cookies"] = {**old_cookies, **new_cookies}
                by_key[key] = merged
                updated += 1
            else:
                by_key[key] = item
                imported += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(by_key.values()), ensure_ascii=False, indent=2), encoding="utf-8")
        self.load_accounts()
        self.log(f"外部导入 Zo 账号完成: imported={imported} updated={updated} source={source}")
        return {
            "ok": True,
            "imported": imported,
            "updated": updated,
            "skipped": len(incoming) - len(normalized),
            "account_count": len(self.accounts),
        }

    def _push_target_import_url(self, target_url: str) -> str:
        base = str(target_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("target_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise ValueError("target_url 必须以 http:// 或 https:// 开头")
        if base.endswith("/2api/plugins/zo/import") or base.endswith("/api/2api/plugins/zo/import"):
            return base
        if base.endswith("/api"):
            return f"{base}/2api/plugins/zo/import"
        return f"{base}/api/2api/plugins/zo/import"

    def _select_push_records(self, *, emails: list[str] | None = None, latest_only: bool = False) -> list[dict[str, Any]]:
        records = [dict(item) for item in self._load_registration_records() if isinstance(item, dict)]
        if emails:
            wanted = {str(email or "").strip().lower() for email in emails if str(email or "").strip()}
            records = [item for item in records if str(item.get("email") or "").strip().lower() in wanted]
        if latest_only and records:
            records.sort(key=lambda item: int(item.get("saved_at") or item.get("imported_at") or 0), reverse=True)
            records = records[:1]
        return records

    def push_accounts(
        self,
        target_url: str,
        *,
        source: str = "external-push",
        emails: list[str] | None = None,
        latest_only: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        import_url = self._push_target_import_url(target_url)
        records = self._select_push_records(emails=emails, latest_only=latest_only)
        if not records:
            return {"ok": False, "pushed": 0, "target_url": import_url, "error": "没有匹配的 Zo 注册记录可推送"}
        payload = {"source": source or "external-push", "records": records}
        response = self.transport.post(
            import_url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json=payload,
            timeout=max(1.0, float(timeout or 30.0)),
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": str(getattr(response, "text", "") or "")[:1000]}
        if not getattr(response, "ok", False):
            raise ValueError(f"推送 Zo 账号失败: status={getattr(response, 'status_code', 0)} body={str(data)[:500]}")
        self.log(f"推送 Zo 账号到远端完成: pushed={len(records)} target={import_url}")
        return {"ok": True, "pushed": len(records), "target_url": import_url, "remote": data}

    def refresh_credits(self) -> list[TwoAPIAccount]:
        if not self.accounts:
            self.load_accounts()
        for account in self.accounts:
            self._refresh_account_credit(account)
        return self.accounts

    def _refresh_account_credit(self, account: TwoAPIAccount) -> None:
        cookies = dict((account.metadata or {}).get("cookies") or {})
        access = str(cookies.get("access_token") or "")
        if not access or ZoClient is None:
            return
        try:
            client = ZoClient(timeout=min(float(self.settings.request_timeout or 30.0), 30.0))
            client.import_cookies(cookies)
            client.set_workspace(handle=account.handle, url=str((account.metadata or {}).get("workspace_origin") or ""))
            result = client.check_credits(min_amount=float(self.settings.min_credit or 1.0))
            amount = _credit_amount_from_result(dict(result or {}))
            account.credit_amount = amount
            account.credit_ok = bool(result.get("ok")) and amount >= float(self.settings.min_credit or 0.0)
            account.metadata = {
                **dict(account.metadata or {}),
                "credit_source": str(result.get("source") or "runtime_refresh"),
                "credit_checked_at": int(time.time()),
            }
            self.log(f"刷新余额: {account.email} credit={amount}")
        except Exception as exc:
            account.last_error = repr(exc)
            self.log(f"刷新余额失败: {account.email} {exc!r}")

    def load_accounts(self) -> list[TwoAPIAccount]:
        paths = [self.data_dir / "zo_proxy_urls.txt"]
        lines: list[str] = []
        for path in paths:
            if path.exists():
                lines.extend(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        proxy_accounts = parse_zo_proxy_lines(lines)
        db_accounts = self._load_accounts_from_database()
        imported_accounts = self._accounts_from_registration_records()
        deploy_artifacts = self._load_proxy_deploy_artifacts()
        for account in db_accounts:
            api_key = str(account.api_key or "").strip()
            artifact = deploy_artifacts.get(api_key) if api_key else None
            if not artifact or account.base_url:
                continue
            account.base_url = artifact["base_url"]
            account.handle = artifact.get("handle") or account.handle
            account.enabled = True
            account.last_status = "proxy_recovered"
            account.metadata = {**dict(account.metadata or {}), **artifact}
            self._write_proxy_url_line(account.email, account.base_url, account.api_key)
        base_accounts = parse_zo_proxy_lines((self.data_dir / "zo_proxy_urls.txt").read_text(encoding="utf-8", errors="ignore").splitlines()) if (self.data_dir / "zo_proxy_urls.txt").exists() else proxy_accounts
        self.accounts = self._merge_accounts(self._merge_accounts(base_accounts, db_accounts), imported_accounts)
        self._enrich_accounts_from_registration_results(self.accounts)
        self.log(f"加载 Zo 账号 {len(self.accounts)} 个，其中 proxy={len(proxy_accounts)} db={len(db_accounts)} imported={len(imported_accounts)}")
        return self.accounts

    def status(self) -> dict[str, Any]:
        # 状态页刷新时重新合并 output 代理池和账号数据库，避免新注册账号被旧缓存遮住。
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

    def select_account(self) -> TwoAPIAccount:
        if not self.settings.enabled:
            raise RuntimeError("Zo 2API 已禁用")
        if not self.accounts:
            self.load_accounts()
        if not self.accounts:
            if self.settings.auto_refill:
                self.refill_account()
                self.load_accounts()
        if not self.accounts:
            raise RuntimeError("Zo 账号池为空")
        total = len(self.accounts)
        for offset in range(total):
            idx = (self._cursor + offset) % total
            account = self.accounts[idx]
            if not account.enabled:
                continue
            if not account.credit_ok or float(account.credit_amount or 0.0) < self.settings.min_credit:
                self.log(f"跳过空额度账号: {account.email} credit={account.credit_amount}")
                continue
            self._cursor = (idx + 1) % total
            self.log(f"选中账号: {account.email}")
            return account
        if self.settings.auto_refill:
            self.refill_account()
            self.load_accounts()
        raise RuntimeError("没有可用 Zo 账号：全部为空额度或不可用")

    def _has_access_token(self, account: TwoAPIAccount) -> bool:
        metadata = dict(account.metadata or {})
        cookies = dict(metadata.get("cookies") or {})
        return bool(str(cookies.get("access_token") or metadata.get("access_token") or "").strip())

    def _account_is_credit_eligible(self, account: TwoAPIAccount) -> bool:
        if not account.enabled:
            return False
        if not account.credit_ok or float(account.credit_amount or 0.0) < self.settings.min_credit:
            self.log(f"跳过空额度账号: {account.email} credit={account.credit_amount}")
            return False
        return True

    def select_direct_account(self) -> TwoAPIAccount:
        if not self.settings.enabled:
            raise RuntimeError("Zo 2API 已禁用")
        if not self.accounts:
            self.load_accounts()
        total = len(self.accounts)
        if total <= 0:
            raise RuntimeError("Zo 账号池为空")
        skipped_without_token = 0
        for offset in range(total):
            idx = (self._cursor + offset) % total
            account = self.accounts[idx]
            if not self._account_is_credit_eligible(account):
                continue
            if not self._has_access_token(account):
                skipped_without_token += 1
                account.last_status = "missing_access_token"
                self.log(f"跳过缺少 access_token 账号: {account.email}")
                continue
            self._cursor = (idx + 1) % total
            self.log(f"选中直连账号: {account.email}")
            return account
        if skipped_without_token:
            raise RuntimeError("没有可直连 Zo 账号：可用账号缺少 access_token")
        raise RuntimeError("没有可直连 Zo 账号：全部为空额度或不可用")

    def refill_account(self) -> None:
        self.log("触发 Zo 自动补号")
        cmd = [sys.executable, str(ROOT / "scripts" / "run_zo_one.py"), "--timeout", "420", "--require-proxy-deploy"]
        subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _wake_account(self, account: TwoAPIAccount) -> None:
        self.log(f"自动唤醒 Zo Space: {account.email}")
        result_path = self.data_dir / "zo_e2e_result.json"
        if not result_path.exists() and ZO_E2E_RESULT_PATH.exists():
            result_path = ZO_E2E_RESULT_PATH
        try:
            data = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
            cookies = dict(data.get("cookies") or {})
            access = str(cookies.get("access_token") or "")
            if access and account.handle:
                headers = {
                    "Authorization": f"Bearer {access}",
                    "Origin": f"https://{account.handle}.zo.computer",
                    "Referer": f"https://{account.handle}.zo.computer/",
                    "X-Zo-Workspace-Origin": f"https://{account.handle}.zo.computer",
                    "x-zo-host-key": account.handle,
                    "Content-Type": "application/json",
                }
                self.transport.post("https://api.zo.computer/host/restart", headers=headers, json={}, timeout=20)
        except Exception as exc:
            self.log(f"唤醒接口失败: {exc!r}")


    def _redeploy_account_proxy(self, account: TwoAPIAccount) -> dict[str, Any]:
        if deploy_proxy is None or load_result_context is None or DEFAULT_SOURCE_DIR is None:
            raise RuntimeError("Zo proxy 自动重部署模块不可用")
        record, result_path = self._record_for_account(account)
        if not record:
            raise RuntimeError(f"找不到 {account.email} 对应的 zo_e2e_result.json 记录，无法自动重部署")
        self.log(f"Zo proxy 路由未恢复，开始自动重部署: {account.email}")
        ctx = load_result_context(result_path)
        result = deploy_proxy(
            ctx,
            Path(DEFAULT_SOURCE_DIR),
            persona_id=str((record.get("proxy_deploy_result") or {}).get("persona_id") or ""),
            verify=True,
            verify_chat=True,
            timeout=max(60.0, float(self.settings.request_timeout or 90.0)),
        )
        if not result.get("ok"):
            raise RuntimeError(f"Zo proxy 自动重部署失败: {result.get('base_url_preview') or ''}")
        account.base_url = str(result.get("base_url") or account.base_url).rstrip("/")
        account.handle = str(result.get("handle") or account.handle)
        account.metadata = {
            **dict(account.metadata or {}),
            "workspace_origin": str(result.get("workspace_origin") or (account.metadata or {}).get("workspace_origin") or ""),
            "redeployed_at": int(time.time()),
            "redeploy_source": "auto_recover",
        }
        self._persist_proxy_result(account, record, result_path, result)
        self.log(f"Zo proxy 自动重部署完成: {account.email}")
        return result

    def _persist_proxy_result(self, account: TwoAPIAccount, record: dict[str, Any], result_path: Path, result: dict[str, Any]) -> None:
        if not record or not result_path.exists():
            return
        updated = dict(record)
        updated["proxy_deploy_result"] = result
        updated["openai_proxy_base_url"] = str(result.get("base_url") or account.base_url)
        updated["openai_proxy_api_key"] = "dummy"
        updated["openai_proxy_models_url"] = str(result.get("models_url") or "")
        updated["openai_proxy_chat_url"] = str(result.get("chat_url") or "")
        updated["proxy_deployed"] = bool(result.get("ok"))
        try:
            result_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log(f"写回 Zo 重部署结果失败: {exc!r}")
        self._write_proxy_url_line(account.email, str(result.get("base_url") or account.base_url), account.api_key)

    def _write_proxy_url_line(self, email: str, base_url: str, api_key: str) -> None:
        if not email or not base_url:
            return
        path = self.data_dir / "zo_proxy_urls.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines = [line for line in lines if not (line.startswith("zo|") and f"|{email}|" in line)]
        lines.append(f"zo|{email}|{base_url.rstrip('/')}|zo_api_key={api_key}|api_key=dummy")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _is_models_response_alive(self, response: Any) -> bool:
        if not getattr(response, "ok", False):
            return False
        headers = getattr(response, "headers", {}) or {}
        try:
            content_type = str(headers.get("content-type") or headers.get("Content-Type") or "").lower()
        except Exception:
            content_type = ""
        text = str(getattr(response, "text", "") or "").lstrip()
        if "text/html" in content_type or text.lower().startswith("<!doctype html") or text.lower().startswith("<html"):
            return False
        data: Any = None
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                data = json_method()
            except Exception:
                data = None
        if isinstance(data, dict):
            return data.get("object") == "list" or isinstance(data.get("data"), list)
        return "application/json" in content_type and bool(text)

    def _ensure_alive(self, account: TwoAPIAccount) -> None:
        try:
            response = self.transport.get(f"{account.base_url}/models", timeout=min(self.settings.wake_timeout, 30.0))
            if self._is_models_response_alive(response):
                account.last_status = "alive"
                return
            account.last_status = f"invalid_response:{getattr(response, 'status_code', 0)}"
        except Exception as exc:
            account.last_status = "sleeping"
            account.last_error = repr(exc)
        if not self.settings.auto_wake:
            raise RuntimeError(f"Zo Space 不可用且未启用自动唤醒: {account.email}")
        self._wake_account(account)
        deadline = time.time() + max(0.1, self.settings.wake_timeout)
        while time.time() < deadline:
            try:
                response = self.transport.get(f"{account.base_url}/models", timeout=20)
                if self._is_models_response_alive(response):
                    account.last_status = "alive"
                    self.log(f"唤醒成功: {account.email}")
                    return
            except Exception:
                pass
            time.sleep(min(2.0, max(0.05, self.settings.wake_timeout)))
        try:
            self._redeploy_account_proxy(account)
            response = self.transport.get(f"{account.base_url}/models", timeout=20)
            if self._is_models_response_alive(response):
                account.last_status = "alive"
                self.log(f"重部署后恢复成功: {account.email}")
                return
        except Exception as exc:
            account.last_error = repr(exc)
            self.log(f"Zo proxy 自动恢复失败: {account.email} {exc!r}")
        raise RuntimeError(f"Zo Space 唤醒/重部署恢复超时: {account.email}")

    def keepalive_once(self) -> dict[str, int]:
        if not self.settings.enabled:
            return {"checked": 0, "alive": 0, "recovered": 0, "failed": 0}
        if not self.accounts:
            self.load_accounts()
        checked = alive = recovered = failed = 0
        allow_space_keepalive = bool(getattr(self.settings, "keepalive_space_fallback", False))
        skipped_space = 0
        for account in list(self.accounts):
            if not account.enabled or not account.credit_ok or float(account.credit_amount or 0.0) < self.settings.min_credit:
                continue
            if self._has_access_token(account):
                account.last_status = account.last_status if account.last_status != "unknown" else "direct_ready"
                skipped_space += 1
                continue
            if not account.base_url:
                skipped_space += 1
                continue
            if not allow_space_keepalive:
                account.last_status = "space_keepalive_skipped"
                skipped_space += 1
                continue
            checked += 1
            before = int((account.metadata or {}).get("redeployed_at") or 0)
            try:
                self._ensure_alive(account)
                alive += 1
                after = int((account.metadata or {}).get("redeployed_at") or 0)
                if after and after != before:
                    recovered += 1
            except Exception as exc:
                failed += 1
                account.last_error = repr(exc)
                self.log(f"保活失败: {account.email} {exc!r}")
        if skipped_space:
            self.log(f"跳过 Zo Space 保活: {skipped_space} 个账号，直连 /ask 或 Space fallback 未显式启用")
        return {"checked": checked, "alive": alive, "recovered": recovered, "failed": failed}

    def _available_models_response(self, response: Any) -> LocalJSONResponse | None:
        if not getattr(response, "ok", False):
            return None
        data: Any = None
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                data = json_method()
            except Exception:
                data = None
        if data is None:
            try:
                data = json.loads(str(getattr(response, "text", "") or "{}"))
            except Exception:
                data = None
        models = data.get("models") if isinstance(data, dict) else data if isinstance(data, list) else None
        if not isinstance(models, list) or not models:
            return None
        items: list[dict[str, Any]] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("model_name") or item.get("id") or "").strip()
            if not model_id:
                continue
            items.append({
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": str(item.get("vendor") or item.get("owned_by") or "zo"),
            })
        if not items:
            return None
        return LocalJSONResponse({"object": "list", "data": items})

    def _direct_available_models(self, account: TwoAPIAccount) -> LocalJSONResponse | None:
        metadata = dict(account.metadata or {})
        cookies = dict(metadata.get("cookies") or {})
        access_token = str(cookies.get("access_token") or metadata.get("access_token") or "").strip()
        if not access_token:
            return None
        origin = str(metadata.get("workspace_origin") or "").rstrip("/")
        handle = str(metadata.get("workspace_handle") or account.handle or "").strip()
        if not origin and handle:
            origin = f"https://{handle}.zo.computer"
        if not origin:
            origin = "https://www.zo.computer"
        try:
            response, _data = self._zo_api_json("GET", "/models/available", token=access_token, origin=origin, handle=handle, timeout=8.0)
            return self._available_models_response(response)
        except Exception as exc:
            account.last_error = repr(exc)
            self.log(f"models 直连 available 失败: {account.email} {exc!r}")
            return None

    def _local_models_response(self) -> LocalJSONResponse:
        payload = {
            "object": "list",
            "data": [
                {"id": model_id, "object": "model", "created": 0, "owned_by": "zo"}
                for model_id in ZO_MODEL_IDS
            ],
        }
        return LocalJSONResponse(payload)

    def forward_models(self) -> Any:
        # /models 是客户端初始化高频请求，优先用 access_token 直连官方模型目录，
        # 不再等待旧 *.zo.space 兼容代理唤醒/超时。真实可用性由 chat 请求验证。
        last_error: Exception | None = None
        if not self.accounts:
            self.load_accounts()
        direct_accounts = [
            account
            for account in self.accounts
            if self._account_is_credit_eligible(account) and self._has_access_token(account)
        ]
        for account in direct_accounts:
            direct = self._direct_available_models(account)
            if direct is not None:
                account.last_status = "models_direct_available"
                self.log(f"models 直连 available 成功: {account.email}")
                return direct
            last_error = RuntimeError(account.last_error or "models_direct_available_failed")
        for _ in range(max(1, self.settings.max_retries)):
            account = self.select_account()
            if self._has_access_token(account):
                last_error = RuntimeError(account.last_error or "models_direct_available_failed")
                continue
            if not account.base_url:
                last_error = RuntimeError("missing_proxy_base_url")
                continue
            try:
                response = self.transport.get(f"{account.base_url}/models", timeout=min(5.0, float(self.settings.request_timeout or 5.0)))
                if self._is_models_response_alive(response):
                    account.last_status = "alive"
                    return response
                account.last_status = f"models_invalid:{getattr(response, 'status_code', 0)}"
                last_error = RuntimeError(account.last_status)
            except Exception as exc:
                last_error = exc
                account.last_status = "models_probe_failed"
                account.last_error = repr(exc)
                self.log(f"models 快速探测失败: {account.email} {exc!r}")
        self.log(f"models 上游不可用，返回本地模型目录: {last_error!r}")
        return self._local_models_response()

    def _message_text(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") if item.get("type") == "text" else ""
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(part for part in parts if part)
        return ""

    def _messages_to_prompt(self, messages: Any) -> str:
        if not isinstance(messages, list):
            return ""
        system_parts: list[str] = []
        convo: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            text = self._message_text(message.get("content")).strip()
            if not text:
                continue
            if role == "system":
                system_parts.append(text)
            elif role == "assistant":
                convo.append(f"Assistant: {text}")
            elif role == "tool":
                convo.append(f"Tool result: {text}")
            else:
                convo.append(f"User: {text}")
        prefix = f"System instructions:\n{'\n\n'.join(system_parts)}\n\n---\n\n" if system_parts else ""
        return prefix + "\n\n".join(convo)

    def _openai_chat_payload(self, model: str, content: str) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def _extract_zo_sse_output(self, upstream: Any) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
        events: list[tuple[str, dict[str, Any]]] = []
        event_type = ""
        data_lines: list[str] = []

        def flush() -> None:
            nonlocal event_type, data_lines
            if not data_lines:
                event_type = ""
                return
            raw = "\n".join(data_lines).strip()
            try:
                data = json.loads(raw)
            except Exception:
                data = {"raw": raw}
            events.append((event_type or "message", data if isinstance(data, dict) else {"data": data}))
            event_type = ""
            data_lines = []

        iterator = getattr(upstream, "iter_lines", None)
        if callable(iterator):
            source = iterator(decode_unicode=True)
        else:
            source = str(getattr(upstream, "text", "") or "").splitlines()
        for raw_line in source:
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line or "")
            if not line:
                flush()
                continue
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        flush()

        output = ""
        for event, data in events:
            if event == "PartStartEvent":
                part = data.get("part") if isinstance(data.get("part"), dict) else {}
                if part.get("part_kind") == "text" and isinstance(part.get("content"), str):
                    output += part.get("content") or ""
            elif event == "PartDeltaEvent":
                delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
                if delta.get("part_delta_kind") == "text" and isinstance(delta.get("content_delta"), str):
                    output += delta.get("content_delta") or ""
            elif event == "End":
                nested = data.get("data") if isinstance(data.get("data"), dict) else {}
                if isinstance(nested.get("output"), str) and nested.get("output"):
                    output = nested["output"]
        return output, events

    def _sse_chunk(self, chat_id: str, model: str, delta: dict[str, Any], finish: str | None = None) -> bytes:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

    def _zo_text_delta(self, event: str, data: dict[str, Any]) -> str:
        if event == "PartStartEvent":
            part = data.get("part") if isinstance(data.get("part"), dict) else {}
            if part.get("part_kind") == "text" and isinstance(part.get("content"), str):
                return str(part.get("content") or "")
        if event == "PartDeltaEvent":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            if delta.get("part_delta_kind") == "text" and isinstance(delta.get("content_delta"), str):
                return str(delta.get("content_delta") or "")
        return ""

    def _iter_zo_sse_events(self, upstream: Any):
        event_type = ""
        data_lines: list[str] = []

        def flush():
            nonlocal event_type, data_lines
            if not data_lines:
                event_type = ""
                return None
            raw = "\n".join(data_lines).strip()
            try:
                data = json.loads(raw)
            except Exception:
                data = {"raw": raw}
            event = event_type or "message"
            event_type = ""
            data_lines = []
            return event, data if isinstance(data, dict) else {"data": data}

        iterator = getattr(upstream, "iter_lines", None)
        if callable(iterator):
            source = iterator(decode_unicode=True)
        else:
            source = str(getattr(upstream, "text", "") or "").splitlines()
        for raw_line in source:
            line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line or "")
            if not line:
                item = flush()
                if item:
                    yield item
                continue
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        item = flush()
        if item:
            yield item

    def _openai_stream_response_from_zo(self, model: str, upstream: Any) -> StreamingSSEOpenAIResponse:
        chat_id = f"chatcmpl-{int(time.time() * 1000)}"

        def chunks():
            yield self._sse_chunk(chat_id, model, {"role": "assistant"})
            try:
                for event, data in self._iter_zo_sse_events(upstream):
                    delta = self._zo_text_delta(event, data)
                    if delta:
                        yield self._sse_chunk(chat_id, model, {"content": delta})
            except GeneratorExit:
                raise
            except Exception as exc:
                yield self._sse_chunk(chat_id, model, {"content": f"\n[zo stream error: {exc}]"}, "stop")
                yield b"data: [DONE]\n\n"
                return
            yield self._sse_chunk(chat_id, model, {}, "stop")
            yield b"data: [DONE]\n\n"

        return StreamingSSEOpenAIResponse(chunks, upstream=upstream)

    def _zo_api_json(
        self,
        method: str,
        path: str,
        *,
        token: str,
        origin: str,
        handle: str = "",
        json_payload: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> tuple[Any, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": origin or "https://www.zo.computer",
            "Referer": f"{(origin or 'https://www.zo.computer').rstrip('/')}/",
            "X-Zo-Workspace-Origin": origin or "https://www.zo.computer",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        }
        if handle:
            headers["x-zo-host-key"] = handle
        url = f"https://api.zo.computer{path}"
        request_timeout = min(float(timeout or self.settings.request_timeout or 30.0), 45.0)
        verb = method.upper()
        if verb == "GET":
            response = self.transport.get(url, headers=headers, timeout=request_timeout)
        elif verb == "PUT":
            response = self.transport.put(url, headers=headers, json=json_payload or {}, timeout=request_timeout)
        elif verb == "DELETE":
            delete = getattr(self.transport, "delete", None)
            if callable(delete):
                response = delete(url, headers=headers, json=json_payload or {}, timeout=request_timeout)
            else:
                response = self.transport.request(verb, url, headers=headers, json=json_payload or {}, timeout=request_timeout)
        else:
            response = self.transport.post(url, headers=headers, json=json_payload or {}, timeout=request_timeout)
        data: Any = None
        json_method = getattr(response, "json", None)
        if callable(json_method):
            try:
                data = json_method()
            except Exception:
                data = None
        if data is None:
            try:
                data = json.loads(str(getattr(response, "text", "") or "{}"))
            except Exception:
                data = {}
        return response, data

    def _ensure_minimal_ask_persona(self, account: TwoAPIAccount, *, token: str, origin: str, handle: str) -> None:
        if not bool(getattr(self.settings, "minimize_ask_context", True)):
            return
        metadata = dict(account.metadata or {})
        existing_id = str(metadata.get("zo_minimal_persona_id") or "").strip()
        if existing_id and bool(metadata.get("zo_minimal_persona_active")):
            return
        try:
            response, data = self._zo_api_json("GET", "/personas/", token=token, origin=origin, handle=handle, timeout=20.0)
            if getattr(response, "ok", False) is not True:
                return
            personas = data if isinstance(data, list) else data.get("personas") if isinstance(data, dict) else []
            persona_id = ""
            if isinstance(personas, list):
                for item in personas:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    if name == MINIMAL_PERSONA_NAME:
                        persona_id = str(item.get("id") or item.get("persona_id") or "").strip()
                        break
            if not persona_id:
                create_payload = {"name": MINIMAL_PERSONA_NAME, "prompt": MINIMAL_PERSONA_PROMPT}
                response, data = self._zo_api_json(
                    "POST",
                    "/personas/",
                    token=token,
                    origin=origin,
                    handle=handle,
                    json_payload=create_payload,
                    timeout=30.0,
                )
                if getattr(response, "ok", False) is not True or not isinstance(data, dict):
                    return
                persona_id = str(data.get("id") or data.get("persona_id") or "").strip()
            if not persona_id:
                return
            update_payload = {"name": MINIMAL_PERSONA_NAME, "prompt": MINIMAL_PERSONA_PROMPT, "scopes": []}
            response, _data = self._zo_api_json(
                "PUT",
                f"/personas/{persona_id}",
                token=token,
                origin=origin,
                handle=handle,
                json_payload=update_payload,
                timeout=30.0,
            )
            if getattr(response, "ok", False) is not True:
                return
            response, _data = self._zo_api_json(
                "POST",
                f"/personas/active/{persona_id}",
                token=token,
                origin=origin,
                handle=handle,
                json_payload={"conversation_type": "main"},
                timeout=30.0,
            )
            if getattr(response, "ok", False) is not True:
                return
            metadata["zo_minimal_persona_id"] = persona_id
            metadata["zo_minimal_persona_active"] = True
            metadata["zo_minimal_persona_checked_at"] = int(time.time())
            account.metadata = metadata
            self.log(f"Zo /ask 已启用极简 persona 降上下文: {account.email}")
        except Exception as exc:
            self.log(f"Zo /ask 极简 persona 准备失败，继续直连: {account.email} {exc!r}")

    def _refresh_access_token(self, account: TwoAPIAccount, *, origin: str = "") -> str:
        metadata = dict(account.metadata or {})
        cookies = dict(metadata.get("cookies") or {})
        refresh_token = str(cookies.get("refresh_token") or metadata.get("refresh_token") or "").strip()
        if not refresh_token:
            raise RuntimeError("缺少 Zo refresh_token，无法刷新 access_token")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": origin or "https://www.zo.computer",
            "Referer": f"{(origin or 'https://www.zo.computer').rstrip('/')}/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        }
        response = self.transport.post(
            "https://api.zo.computer/auth/token",
            headers=headers,
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
            timeout=min(float(self.settings.request_timeout or 30.0), 30.0),
        )
        if not getattr(response, "ok", False):
            raise RuntimeError(f"Zo refresh_token 刷新失败: status={getattr(response, 'status_code', 0)} body={str(getattr(response, 'text', '') or '')[:300]}")
        try:
            data = response.json()
        except Exception:
            data = json.loads(str(getattr(response, "text", "") or "{}"))
        access_token = str(data.get("access_token") or data.get("access") or "").strip() if isinstance(data, dict) else ""
        new_refresh = str(data.get("refresh_token") or data.get("refresh") or refresh_token).strip() if isinstance(data, dict) else refresh_token
        if not access_token:
            raise RuntimeError("Zo refresh_token 响应缺少 access_token")
        cookies["access_token"] = access_token
        cookies["refresh_token"] = new_refresh
        metadata["cookies"] = cookies
        metadata["access_token_refreshed_at"] = int(time.time())
        account.metadata = metadata
        account.last_status = "access_token_refreshed"
        self.log(f"刷新 Zo access_token 成功: {account.email}")
        return access_token

    def _direct_ask(self, account: TwoAPIAccount, payload: dict[str, Any], *, stream: bool = False) -> Any:
        cookies = dict((account.metadata or {}).get("cookies") or {})
        access_token = str(cookies.get("access_token") or (account.metadata or {}).get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("缺少 Zo access_token，无法直连 /ask")
        model = str(payload.get("model") or "zo:openai/gpt-5.5")
        prompt = self._messages_to_prompt(payload.get("messages"))
        if not prompt:
            raise RuntimeError("messages 为空")
        origin = str((account.metadata or {}).get("workspace_origin") or "").rstrip("/")
        handle = str((account.metadata or {}).get("workspace_handle") or account.handle or "").strip()
        if not origin and handle:
            origin = f"https://{handle}.zo.computer"
        if not origin:
            origin = "https://www.zo.computer"
        ask_payload: dict[str, Any] = {"q": prompt, "model_name": model, "mode": "chat", "context_paths": [], "command_paths": [], "expanded_paths": []}
        if isinstance(payload.get("conversation_id"), str):
            ask_payload["conversation_id"] = payload["conversation_id"]
        def build_headers(token: str) -> dict[str, str]:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json, */*",
                "x-zo-streaming-version": "2",
                "Origin": origin,
                "Referer": f"{origin}/",
                "X-Zo-Workspace-Origin": origin,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            }
            if handle:
                headers["x-zo-host-key"] = handle
            return headers

        self._ensure_minimal_ask_persona(account, token=access_token, origin=origin, handle=handle)
        upstream = self.transport.post(
            "https://api.zo.computer/ask",
            headers=build_headers(access_token),
            json=ask_payload,
            timeout=self.settings.request_timeout,
            stream=True,
        )
        if not getattr(upstream, "ok", False) and int(getattr(upstream, "status_code", 0) or 0) in (401, 403):
            access_token = self._refresh_access_token(account, origin=origin)
            self._ensure_minimal_ask_persona(account, token=access_token, origin=origin, handle=handle)
            upstream = self.transport.post(
                "https://api.zo.computer/ask",
                headers=build_headers(access_token),
                json=ask_payload,
                timeout=self.settings.request_timeout,
                stream=True,
            )
        if not getattr(upstream, "ok", False):
            status = int(getattr(upstream, "status_code", 500) or 500)
            text = str(getattr(upstream, "text", "") or "")[:500]
            raise RuntimeError(f"Zo /ask error ({status}): {text}")
        account.last_status = "direct_ask_alive"
        if stream:
            return self._openai_stream_response_from_zo(model, upstream)
        text, _events = self._extract_zo_sse_output(upstream)
        return LocalJSONResponse(self._openai_chat_payload(model, text))

    def forward_chat(self, payload: dict[str, Any], *, stream: bool = False) -> Any:
        last_error: Exception | None = None
        for _ in range(max(1, self.settings.max_retries)):
            try:
                account = self.select_direct_account()
            except Exception as exc:
                last_error = exc
                break
            try:
                return self._direct_ask(account, payload, stream=stream)
            except Exception as exc:
                last_error = exc
                account.last_error = repr(exc)
                self.log(f"direct ask 转发失败: {account.email} {exc!r}")
                continue

        # 兼容兜底：只有完全没有可用 access_token 直连账号时，才走旧 Space 代理。
        # 这样不会因为池子里混入旧账号而每次请求卡在 60s 唤醒超时。
        if last_error and "access_token" not in str(last_error):
            raise RuntimeError(str(last_error))
        for _ in range(max(1, self.settings.max_retries)):
            account = self.select_account()
            if self._has_access_token(account):
                continue
            try:
                self._ensure_alive(account)
                return self.transport.post(
                    f"{account.base_url}/chat/completions",
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    json=payload,
                    timeout=self.settings.request_timeout,
                    stream=stream,
                )
            except Exception as exc:
                last_error = exc
                account.last_error = repr(exc)
                self.log(f"space chat 转发失败: {account.email} {exc!r}")
        raise RuntimeError(str(last_error or "chat 转发失败"))
