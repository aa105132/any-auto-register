from __future__ import annotations

import html
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests

from services.twoapi.importer import TwoAPIImportSchema, import_twoapi_accounts
from services.twoapi.models import TwoAPIAccount, TwoAPISettings, mask_secret_in_text


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


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    from application.tasks import create_register_task as _create_register_task

    return _create_register_task(payload)


class _LazyTaskRuntime:
    def wake_up(self) -> None:
        from services.task_runtime import task_runtime as _task_runtime

        _task_runtime.wake_up()


task_runtime = _LazyTaskRuntime()

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "output"
ACCOUNT_DB_PATH = ROOT / "account_manager.db"

THESYS_OPENAI_BASE_URL = "https://api.thesys.dev/v1/embed"
THESYS_CHAT_COMPLETIONS_URL = f"{THESYS_OPENAI_BASE_URL}/chat/completions"
THESYS_MODELS_URL = f"{THESYS_OPENAI_BASE_URL}/models"
THESYS_DEFAULT_MODEL = "c1/google/gemini-3.1-pro-free/v-20260331"
THESYS_FREE_MODELS = [
    "c1/google/gemini-3.5-flash-free/v-20260331",
    THESYS_DEFAULT_MODEL,
    "c1/google/gemini-3.1-flash-lite-free/v-20260331",
]

THESYS_IMPORT_SCHEMA = TwoAPIImportSchema(
    plugin="thesys",
    platform="thesys",
    token_fields=("api_key", "ai_api_token", "thesys_api_key", "token", "key"),
    email_fields=("email", "account", "username", "user_email"),
    user_id_fields=("user_id", "uid", "id"),
    base_url_fields=("openai_base_url", "openai_compatible_api_base", "llm_api_base", "api_base", "base_url"),
    default_base_url=THESYS_OPENAI_BASE_URL,
    # Thesys 实测 key 可能不是 sk- 前缀，因此只按长度和字段判断。
    token_prefixes=(),
    min_token_length=32,
    credential_aliases=("api_key", "ai_api_token", "thesys_api_key", "token"),
    primary_token_field="api_key",
    metadata_defaults={"openai_compatible": True, "native_thesys": True},
)


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


def _looks_like_thesys_key(value: Any) -> bool:
    text = _safe_text(value)
    if len(text) < 32:
        return False
    if "@" in text or "://" in text or any(ch.isspace() for ch in text):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_\-.]+", text))


def _extract_api_key(record: dict[str, Any]) -> str:
    for key in ("api_key", "ai_api_token", "thesys_api_key", "token", "key", "primary_token"):
        value = _safe_text(record.get(key))
        if _looks_like_thesys_key(value):
            return value
    credentials = record.get("credentials")
    if isinstance(credentials, dict):
        for key in ("api_key", "ai_api_token", "thesys_api_key", "token", "key"):
            value = _safe_text(credentials.get(key))
            if _looks_like_thesys_key(value):
                return value
    return ""


def _normalize_base_url(value: Any) -> str:
    text = _safe_text(value).rstrip("/")
    if not text:
        return THESYS_OPENAI_BASE_URL
    for suffix in ("/chat/completions", "/models"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    if text == "https://api.thesys.dev":
        return THESYS_OPENAI_BASE_URL
    if text == "https://api.thesys.dev/v1":
        return THESYS_OPENAI_BASE_URL
    if text.startswith("https://api.thesys.dev/v1/embed"):
        return THESYS_OPENAI_BASE_URL
    return THESYS_OPENAI_BASE_URL


def _credit_amount_from_record(record: dict[str, Any], *, default: float = 100.0) -> float:
    for key in ("credit_amount", "credits", "balance"):
        if key in record:
            return _safe_float(record.get(key), default)
    billing = record.get("billing") or record.get("credit_result") or record.get("balance_result")
    if isinstance(billing, dict):
        for key in ("amount", "balance", "credits", "total"):
            if key in billing:
                return _safe_float(billing.get(key), default)
        data = billing.get("data")
        if isinstance(data, dict):
            for key in ("amount", "balance", "credits", "total"):
                if key in data:
                    return _safe_float(data.get(key), default)
    return default


def _decode_literal_token(value: str) -> str:
    text = str(value or "")
    try:
        return json.loads(f'"{text}"')
    except Exception:
        return text.replace(r"\n", "\n").replace(r"\t", "\t").replace(r'\"', '"')


def _quoted_args(call_args: str) -> list[str]:
    result: list[str] = []
    pattern = re.compile(r'"((?:\\.|[^"\\])*)"|\'((?:\\.|[^\'\\])*)\'', re.DOTALL)
    for match in pattern.finditer(call_args or ""):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        result.append(_decode_literal_token(raw or ""))
    return result


def unwrap_thesys_openui_content(content: Any) -> str:
    """把 Thesys/OpenUI 组件 DSL 尽量还原为普通对话文本。"""
    text = html.unescape(str(content or "")).strip()
    if not text:
        return ""

    # 优先抽取最像自然语言内容的组件，避免把 root/Card/Button DSL 暴露给调用方。
    parts: list[str] = []
    for component in ("TextContent", "Markdown", "InlineHeader", "Header"):
        pattern = re.compile(rf"\b{component}\s*\((.*?)\)", re.DOTALL)
        for match in pattern.finditer(text):
            args = [item.strip() for item in _quoted_args(match.group(1)) if item and item.strip()]
            if not args:
                continue
            if component == "Header":
                parts.append("\n".join(args[:2]))
            else:
                parts.append(args[0])
    cleaned = [part.strip() for part in parts if part and part.strip()]
    if cleaned:
        deduped: list[str] = []
        seen: set[str] = set()
        for item in cleaned:
            if item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return "\n\n".join(deduped).strip()

    # 没有识别出组件时，退化为剥掉外层标签和 code fence。
    text = re.sub(r"</?content\b[^>]*>", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"```(?:openui-lang)?", "", text, flags=re.IGNORECASE).strip()
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("root ="):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _response_json_or_text(response: Any) -> Any:
    try:
        data = response.json()
        if isinstance(data, (dict, list)):
            return data
    except Exception:
        pass
    raw = getattr(response, "text", "") or ""
    if not raw and getattr(response, "content", None) is not None:
        try:
            raw = bytes(getattr(response, "content") or b"").decode("utf-8", errors="replace")
        except Exception:
            raw = ""
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {"raw": raw}


class ThesysTwoAPIPlugin:
    name = "thesys"
    display_name = "Thesys OpenAI 兼容代理"

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
        with (log_dir / "thesys.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def recent_logs(self, *, limit: int = 200) -> list[str]:
        file_path = self.data_dir / "twoapi_logs" / "thesys.log"
        rows = list(self._logs)
        if file_path.exists():
            try:
                rows = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                pass
        return rows[-max(1, min(limit, 1000)):]

    def _credential_paths(self) -> list[Path]:
        return [
            self.data_dir / "thesys_credentials.json",
            self.data_dir / "thesys_account_result.json",
            self.data_dir / "thesys_e2e_result.json",
        ]

    def _load_accounts_from_files(self) -> list[TwoAPIAccount]:
        records: list[dict[str, Any]] = []
        for path in self._credential_paths():
            records.extend(_load_json_records(path))
        keys_path = self.data_dir / "thesys_keys.txt"
        if keys_path.exists():
            for index, raw in enumerate(keys_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                text = raw.strip()
                if not text or text.startswith("#"):
                    continue
                parts = [part.strip() for part in text.split("|") if part.strip()]
                key = next((part for part in parts if _looks_like_thesys_key(part)), "")
                if key:
                    email = next((part for part in parts if "@" in part), "") or f"thesys-key-{index}@local"
                    records.append({"email": email, "api_key": key, "source": "thesys_keys"})
        return self._accounts_from_records(records, source="file")

    def _load_accounts_from_database(self) -> list[TwoAPIAccount]:
        db_path = self.account_db_path
        if not db_path.exists():
            return []
        try:
            connection = sqlite3.connect(str(db_path))
            connection.row_factory = sqlite3.Row
        except Exception as exc:
            self.log(f"读取 Thesys 账号数据库失败: {exc!r}")
            return []
        try:
            rows = connection.execute(
                "SELECT id, email, user_id FROM accounts WHERE lower(platform)='thesys' ORDER BY id ASC"
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                account_id = int(row["id"] or 0)
                email = _safe_text(row["email"])
                if not email:
                    continue
                credential_rows = connection.execute(
                    "SELECT key, value FROM account_credentials WHERE account_id=? AND provider_name='thesys'",
                    (account_id,),
                ).fetchall()
                credentials = {str(item["key"] or ""): str(item["value"] or "") for item in credential_rows}
                overview = connection.execute(
                    "SELECT summary_json FROM account_overviews WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                summary = _safe_dict(str(overview["summary_json"] or "")) if overview else {}
                legacy_extra = _safe_dict(summary.get("legacy_extra"))
                account_overview = _safe_dict(legacy_extra.get("account_overview")) or _safe_dict(summary.get("account_overview"))
                record: dict[str, Any] = {
                    **legacy_extra,
                    **credentials,
                    "email": email,
                    "user_id": _safe_text(row["user_id"]),
                    "account_id": account_id,
                    "account_overview": account_overview,
                    "source": "account_database",
                }
                records.append(record)
            return self._accounts_from_records(records, source="account_database")
        except Exception as exc:
            self.log(f"读取 Thesys 数据库账号失败: {exc!r}")
            return []
        finally:
            connection.close()

    def _accounts_from_records(self, records: list[dict[str, Any]], *, source: str) -> list[TwoAPIAccount]:
        accounts: list[TwoAPIAccount] = []
        for index, record in enumerate(records, start=1):
            if record.get("ok") is False:
                continue
            api_key = _extract_api_key(record)
            if not api_key:
                continue
            email = _safe_text(record.get("email")) or f"thesys-key-{index}@local"
            amount = _credit_amount_from_record(record, default=100.0)
            base_url = _normalize_base_url(
                record.get("openai_base_url")
                or record.get("openai_compatible_api_base")
                or record.get("llm_api_base")
                or record.get("api_base")
                or record.get("base_url")
            )
            user = _safe_dict(record.get("user"))
            org = _safe_dict(record.get("org"))
            overview = _safe_dict(record.get("account_overview"))
            metadata = {
                "source": _safe_text(record.get("source")) or source,
                "account_id": record.get("account_id"),
                "user_id": _safe_text(record.get("user_id") or user.get("id") or overview.get("user_id")),
                "org_id": _safe_text(record.get("org_id") or org.get("id") or overview.get("org_id")),
                "default_free_model": _safe_text(record.get("default_free_model")) or THESYS_DEFAULT_MODEL,
                "free_models": record.get("free_models") if isinstance(record.get("free_models"), list) else list(THESYS_FREE_MODELS),
                "openai_compatible": True,
                "native_thesys": True,
            }
            accounts.append(
                TwoAPIAccount(
                    plugin=self.name,
                    email=email,
                    base_url=base_url,
                    api_key=api_key,
                    handle=_safe_text(org.get("name") or metadata.get("org_id") or metadata.get("user_id")),
                    credit_amount=amount,
                    credit_ok=amount >= float(self.settings.min_credit or 0.0),
                    enabled=True,
                    last_status="native_ready",
                    metadata={key: value for key, value in metadata.items() if value not in (None, "", [], {})},
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
                if account.credit_amount > existing.credit_amount:
                    existing.credit_amount = account.credit_amount
                    existing.credit_ok = account.credit_ok
                existing.enabled = existing.enabled or account.enabled
                existing.metadata = {**dict(account.metadata or {}), **dict(existing.metadata or {})}
        return merged

    def load_accounts(self) -> list[TwoAPIAccount]:
        file_accounts = self._load_accounts_from_files()
        db_accounts = self._load_accounts_from_database()
        self.accounts = self._merge_accounts(db_accounts, file_accounts)
        self.log(f"加载 Thesys 账号 {len(self.accounts)} 个，其中 db={len(db_accounts)} file={len(file_accounts)}")
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
        if not account.enabled or not account.api_key:
            return False
        if not account.credit_ok or float(account.credit_amount or 0.0) < self.settings.min_credit:
            self.log(f"跳过不可用 Thesys 账号: {account.email} credit={account.credit_amount}")
            return False
        return True

    def select_account(self) -> TwoAPIAccount:
        if not self.settings.enabled:
            raise RuntimeError("Thesys 2API 已禁用")
        if not self.accounts:
            self.load_accounts()
        if not self.accounts and self.settings.auto_refill:
            self.refill_accounts(count=1, concurrency=1)
            self.load_accounts()
        if not self.accounts:
            raise RuntimeError("Thesys 账号池为空")
        total = len(self.accounts)
        for offset in range(total):
            idx = (self._cursor + offset) % total
            account = self.accounts[idx]
            if not self._account_is_eligible(account):
                continue
            self._cursor = (idx + 1) % total
            self.log(f"选中 Thesys 账号: {account.email}")
            return account
        if self.settings.auto_refill:
            self.refill_accounts(count=1, concurrency=1)
            self.load_accounts()
        raise RuntimeError("没有可用 Thesys 账号：全部为空额度或凭据不可用")

    def _headers(self, account: TwoAPIAccount, *, stream: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {account.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }

    def _chat_url(self, account: TwoAPIAccount) -> str:
        base = _normalize_base_url(account.base_url).rstrip("/")
        return f"{base}/chat/completions"

    def _build_chat_payload(self, payload: dict[str, Any], *, stream: bool = False) -> dict[str, Any]:
        body = dict(payload or {})
        body["model"] = _safe_text(body.get("model")) or THESYS_DEFAULT_MODEL
        body["stream"] = bool(stream or body.get("stream"))
        body.setdefault("reasoning_effort", "minimal")
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            prompt = _safe_text(body.get("prompt")) or "你好"
            body["messages"] = [{"role": "user", "content": prompt}]
        return body

    def _models_catalog_response(self) -> LocalJSONResponse:
        return LocalJSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model_id, "object": "model", "created": 0, "owned_by": "thesys"}
                    for model_id in THESYS_FREE_MODELS
                ],
            }
        )

    def forward_models(self) -> Any:
        if not self.accounts:
            self.load_accounts()
        return self._models_catalog_response()

    def _unwrap_openai_chat_payload(self, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for choice in list(data.get("choices") or []):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                message["content"] = unwrap_thesys_openui_content(message.get("content"))
            delta = choice.get("delta")
            if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                delta["content"] = unwrap_thesys_openui_content(delta.get("content"))
        return data

    def forward_chat(self, payload: dict[str, Any], *, stream: bool = False) -> Any:
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI payload 必须是 JSON object")
        if not self.accounts:
            self.load_accounts()
        attempts = max(1, max(int(self.settings.max_retries or 1), len(self.accounts)))
        last_error: Exception | None = None
        last_response: Any | None = None
        for _ in range(attempts):
            account = self.select_account()
            try:
                body = self._build_chat_payload(payload, stream=stream)
                response = self.transport.post(
                    self._chat_url(account),
                    headers=self._headers(account, stream=stream),
                    json=body,
                    timeout=self.settings.request_timeout,
                    stream=stream,
                )
                status = int(getattr(response, "status_code", 200) or 200)
                if getattr(response, "ok", False) or status < 500:
                    account.last_status = "chat_alive" if status < 400 else f"chat_error:{status}"
                    if status >= 400 or stream:
                        return response
                    data = self._unwrap_openai_chat_payload(_response_json_or_text(response))
                    if isinstance(data, dict):
                        return LocalJSONResponse(data, status_code=status)
                    return response
                last_response = response
                account.last_status = f"chat_retryable_error:{status}"
                self.log(f"Thesys chat 可重试错误: {account.email} status={status}")
            except Exception as exc:
                last_error = exc
                account.last_error = repr(exc)
                account.last_status = "chat_failed"
                self.log(f"Thesys chat 转发失败: {account.email} {exc!r}")
                continue
        if last_response is not None:
            return last_response
        raise RuntimeError(str(last_error or "Thesys chat 转发失败"))

    @property
    def import_schema(self) -> TwoAPIImportSchema:
        return THESYS_IMPORT_SCHEMA

    def _append_imported_credentials_file(self, result: dict[str, Any]) -> None:
        rows = []
        for item in list(result.get("accounts") or []):
            if not isinstance(item, dict):
                continue
            key = _safe_text(item.get("api_key") or item.get("token"))
            if not key:
                continue
            rows.append(
                {
                    "email": _safe_text(item.get("email")) or "thesys-import@local",
                    "api_key": key,
                    "ai_api_token": key,
                    "source": _safe_text(result.get("source")) or "external_import",
                    "credit_amount": _safe_float(item.get("credit_amount"), 100.0),
                    "openai_base_url": _safe_text(item.get("base_url")) or THESYS_OPENAI_BASE_URL,
                    "free_models": list(THESYS_FREE_MODELS),
                    "ok": True,
                }
            )
        if not rows:
            return
        path = self.data_dir / "thesys_credentials.json"
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

    def import_accounts(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        lines: list[str] | None = None,
        source: str = "external",
        repository: Any | None = None,
    ) -> dict[str, Any]:
        result = import_twoapi_accounts(
            self.import_schema,
            records=records,
            lines=lines,
            source=source,
            repository=repository,
        )
        result["source"] = source
        self._append_imported_credentials_file(result)
        self.accounts = []
        self.log(f"导入 Thesys 外部账号: created={result.get('created')} accepted={result.get('accepted')} skipped={result.get('skipped')}")
        return result

    def _push_target_import_url(self, target_url: str) -> str:
        base = str(target_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("target_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise ValueError("target_url 必须以 http:// 或 https:// 开头")
        if base.endswith("/2api/plugins/thesys/import") or base.endswith("/api/2api/plugins/thesys/import"):
            return base
        if base.endswith("/api"):
            return f"{base}/2api/plugins/thesys/import"
        return f"{base}/api/2api/plugins/thesys/import"

    def _account_to_push_record(self, account: TwoAPIAccount) -> dict[str, Any]:
        metadata = dict(account.metadata or {})
        record: dict[str, Any] = {
            "email": account.email,
            "api_key": account.api_key,
            "ai_api_token": account.api_key,
            "base_url": account.base_url or THESYS_OPENAI_BASE_URL,
            "openai_base_url": account.base_url or THESYS_OPENAI_BASE_URL,
            "credit_amount": float(account.credit_amount or 0.0),
            "source": _safe_text(metadata.get("source")) or "thesys_local",
            "native_thesys": True,
            "openai_compatible": True,
            "free_models": list(THESYS_FREE_MODELS),
            "ok": bool(account.enabled and account.api_key),
        }
        for key in ("user_id", "org_id", "account_id"):
            value = metadata.get(key)
            if value not in (None, ""):
                record[key] = value
        return record

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
        if latest_only and records:
            records = records[-1:]
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
            return {"ok": False, "pushed": 0, "target_url": import_url, "error": "没有匹配的 Thesys 账号可推送"}
        response = self.transport.post(
            import_url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"source": source or "external-push", "records": records},
            timeout=max(1.0, float(timeout or 30.0)),
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": str(getattr(response, "text", "") or "")[:1000]}
        if not getattr(response, "ok", False):
            raise ValueError(f"推送 Thesys 账号失败: status={getattr(response, 'status_code', 0)} body={str(data)[:500]}")
        self.log(f"推送 Thesys 账号到远端完成: pushed={len(records)} target={import_url}")
        return {"ok": True, "pushed": len(records), "target_url": import_url, "remote": data}

    def refill_accounts(
        self,
        *,
        count: int = 1,
        concurrency: int = 1,
        executor_type: str = "protocol",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved_count = max(1, min(int(count or 1), 100))
        payload = {
            "platform": self.name,
            "count": resolved_count,
            "concurrency": max(1, min(int(concurrency or 1), resolved_count)),
            "executor_type": executor_type or "protocol",
            "extra": {"twoapi_auto_refill": True, **dict(extra or {})},
        }
        task = create_register_task(payload)
        task_runtime.wake_up()
        self.log(f"已创建 Thesys 自动补号任务: task_id={task.get('id')} count={resolved_count}")
        return {"ok": True, "task": task, "payload": payload}

    def refresh_credits(self) -> list[TwoAPIAccount]:
        if not self.accounts:
            self.load_accounts()
        return self.accounts
