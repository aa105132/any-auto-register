from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests

from services.twoapi.importer import TwoAPIImportSchema, import_twoapi_accounts
from services.twoapi.models import TwoAPIAccount, TwoAPISettings, mask_secret_in_text
from services.twoapi.plugins.zo import LocalJSONResponse, StreamingSSEOpenAIResponse


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
SWARMS_NATIVE_BASE_URL = "https://api.swarms.world/v1"
SWARMS_IMPORT_SCHEMA = TwoAPIImportSchema(
    plugin="swarms",
    platform="swarms",
    token_fields=("api_key", "ai_api_token", "token", "key", "legacy_token"),
    email_fields=("email", "account", "username", "user_email"),
    user_id_fields=("user_id", "uid", "id"),
    base_url_fields=("native_api_base", "openai_base_url", "base_url"),
    default_base_url=SWARMS_NATIVE_BASE_URL,
    token_prefixes=("sk-",),
    min_token_length=32,
    credential_aliases=("api_key", "ai_api_token", "token", "key"),
    primary_token_field="api_key",
    metadata_defaults={"native_openai": True},
)

# 仅暴露实测能通过 Swarms 原生 /v1/swarm/completions 返回非空文本的模型。
# Swarms /models/available 会返回大量图像、音频、embedding 或别名模型；其中不少
# 对 OpenAI 直连 /chat/completions 会 200 空回；2API 统一用原生 Swarm 包装。
SWARMS_MODEL_IDS = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4o-2024-08-06",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-5.5",
    "gpt-5.5-2026-04-23",
    "gpt-5.4",
    "gpt-5.4-2026-03-05",
    "gpt-5.4-mini",
    "gpt-5.4-mini-2026-03-17",
    "gemini/gemini-3.5-flash",
    "gemini-3.5-flash",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-opus-4-5-20251101",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-20250514",
]
SWARMS_PREMIUM_OR_EMPTY_MODEL_IDS = {
    "gpt-4.1",
    # /models/available 已上架，但 2026-05-30 实测 Agent API 与 /v1/swarm/completions 均 200 空回。
    "claude-opus-4-8",
    "claude-opus-4.8",
    "claude-opus-4-7",
    "claude-opus-4-7-20260416",
    "claude-sonnet-4-5",
    "claude-opus-4.5",
    "claude-sonnet-4.5",
    "chatgpt-4o-latest",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-chat-latest",
    "gpt-5.1",
    "gpt-5.1-chat-latest",
    "gpt-5.2",
    "gpt-5.2-chat-latest",
    "gpt-5.5",
    "gpt5.5",
    "gpt5.4",
    "gpt-5.5-pro",
    "gpt-5.5-pro-2026-04-23",
    "gpt-5.4-pro",
    "gpt-5.4-pro-2026-03-05",
    "claude-opus-4-6-20260205",
}


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


def _credit_amount_from_record(record: dict[str, Any], *, default: float = 100.0) -> float:
    credit = record.get("credit_result") or record.get("balance_result") or record.get("credit")
    if isinstance(credit, dict):
        for key in ("amount", "balance", "credits", "total"):
            if key in credit:
                return _safe_float(credit.get(key), default)
        data = credit.get("data")
        if isinstance(data, dict):
            cents = data.get("available_balance_cents")
            if isinstance(cents, (int, float)) and not isinstance(cents, bool):
                return round(float(cents) / 100.0, 6)
    for key in ("credit_amount", "credits", "balance"):
        if key in record:
            return _safe_float(record.get(key), default)
    return default


def _extract_api_key(record: dict[str, Any]) -> str:
    for key in ("api_key", "ai_api_token", "token", "key", "legacy_token"):
        value = _safe_text(record.get(key))
        if value.startswith("sk-") and len(value) >= 32:
            return value
    return ""


def _normalize_base_url(value: Any) -> str:
    text = _safe_text(value).rstrip("/")
    if not text:
        return SWARMS_NATIVE_BASE_URL
    if text.endswith("/chat/completions"):
        text = text[: -len("/chat/completions")]
    if text.endswith("/models"):
        text = text[: -len("/models")]
    if text == "https://api.swarms.world":
        return SWARMS_NATIVE_BASE_URL
    if text.startswith("https://api.swarms.world/v1"):
        return text
    return SWARMS_NATIVE_BASE_URL


SWARMS_AGENT_NAME = "Swarms Assistant"
SWARMS_AGENT_DESCRIPTION = (
    "A helpful AI assistant powered by Swarms. This agent can help you with a wide range of tasks "
    "including answering questions, writing, analysis, coding, and more."
)
SWARMS_DEFAULT_SYSTEM_PROMPT = "You are Swarms Assistant, a helpful assistant."
SWARMS_DEFAULT_SWARM_TYPE = "ConcurrentWorkflow"
SWARMS_DEFAULT_MAX_TOKENS = 8192
SWARMS_DEFAULT_TEMPERATURE = 0.7


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text") is not None:
                parts.append(str(item.get("text") or ""))
            elif item.get("text") is not None:
                parts.append(str(item.get("text") or ""))
            elif item.get("content") is not None:
                parts.append(str(item.get("content") or ""))
        return "\n".join(part.strip() for part in parts if part and part.strip())
    if content is None:
        return ""
    return str(content).strip()


def _messages_to_system_and_task(messages: Any) -> tuple[str, str]:
    if not isinstance(messages, list):
        return SWARMS_DEFAULT_SYSTEM_PROMPT, str(messages or "").strip()
    system_parts: list[str] = []
    dialogue: list[str] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower() or "user"
        text = _message_content_to_text(item.get("content"))
        if not text:
            continue
        if role in {"system", "developer"}:
            system_parts.append(text)
            continue
        if len(messages) == 1 and role == "user":
            dialogue.append(text)
        else:
            dialogue.append(f"{role}: {text}")
    system_prompt = "\n\n".join(system_parts).strip() or SWARMS_DEFAULT_SYSTEM_PROMPT
    task = "\n".join(dialogue).strip()
    return system_prompt, task


def _extract_swarms_output(data: Any) -> str:
    if isinstance(data, dict):
        if isinstance(data.get("choices"), list):
            parts: list[str] = []
            for choice in data.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                delta = choice.get("delta")
                if isinstance(message, dict):
                    parts.append(_message_content_to_text(message.get("content")))
                elif isinstance(delta, dict):
                    parts.append(_message_content_to_text(delta.get("content")))
            return "\n".join(part for part in parts if part)
        for key in ("output", "outputs"):
            value = data.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, (list, dict)):
                return _extract_swarms_output(value)
        for key in ("content", "message", "response", "text"):
            value = data.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, (list, dict)):
                return _extract_swarms_output(value)
        return ""
    if isinstance(data, list):
        parts = [_extract_swarms_output(item) for item in data]
        return "\n".join(part for part in parts if part)
    if data is None:
        return ""
    return str(data)


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
        return {"output": raw}


def _payload_requests_tool_call(payload: dict[str, Any]) -> bool:
    tools = payload.get("tools")
    functions = payload.get("functions")
    has_tools = isinstance(tools, list) and bool(tools)
    has_functions = isinstance(functions, list) and bool(functions)
    if not has_tools and not has_functions:
        return False
    tool_choice = _safe_text(payload.get("tool_choice")).lower()
    function_call = _safe_text(payload.get("function_call")).lower()
    # 显式禁用工具时按普通聊天处理；否则 OpenAI 客户端会期待 tool_calls，Swarms hosted API 实测不会返回。
    return tool_choice != "none" and function_call != "none"


class SwarmsTwoAPIPlugin:
    name = "swarms"
    display_name = "Swarms OpenAI 兼容代理"

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
        with (log_dir / "swarms.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def recent_logs(self, *, limit: int = 200) -> list[str]:
        file_path = self.data_dir / "twoapi_logs" / "swarms.log"
        rows = list(self._logs)
        if file_path.exists():
            try:
                rows = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                pass
        return rows[-max(1, min(limit, 1000)):]

    def _credential_paths(self) -> list[Path]:
        return [
            self.data_dir / "swarms_credentials.json",
            self.data_dir / "swarms_e2e_result.json",
        ]

    def _load_accounts_from_files(self) -> list[TwoAPIAccount]:
        records: list[dict[str, Any]] = []
        for path in self._credential_paths():
            records.extend(_load_json_records(path))
        keys_path = self.data_dir / "swarms_keys.txt"
        if keys_path.exists():
            for index, raw in enumerate(keys_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                text = raw.strip()
                if not text or text.startswith("#"):
                    continue
                parts = [part.strip() for part in text.split("|")]
                key = next((part for part in parts if part.startswith("sk-")), "")
                if key:
                    email = next((part for part in parts if "@" in part), "") or f"swarms-key-{index}@local"
                    records.append({"email": email, "api_key": key, "source": "swarms_keys"})
        return self._accounts_from_records(records, source="file")

    def _load_accounts_from_database(self) -> list[TwoAPIAccount]:
        db_path = self.account_db_path
        if not db_path.exists():
            return []
        try:
            connection = sqlite3.connect(str(db_path))
            connection.row_factory = sqlite3.Row
        except Exception as exc:
            self.log(f"读取 Swarms 账号数据库失败: {exc!r}")
            return []
        try:
            rows = connection.execute(
                "SELECT id, email FROM accounts WHERE lower(platform)='swarms' ORDER BY id ASC"
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                account_id = int(row["id"] or 0)
                email = _safe_text(row["email"])
                if not email:
                    continue
                credential_rows = connection.execute(
                    "SELECT key, value, credential_type FROM account_credentials WHERE account_id=? AND provider_name='swarms'",
                    (account_id,),
                ).fetchall()
                credentials = {str(item["key"] or ""): str(item["value"] or "") for item in credential_rows}
                overview = connection.execute(
                    "SELECT summary_json FROM account_overviews WHERE account_id=?",
                    (account_id,),
                ).fetchone()
                summary = _safe_dict(str(overview["summary_json"] or "")) if overview else {}
                legacy_extra = _safe_dict(summary.get("legacy_extra"))
                record: dict[str, Any] = {
                    **legacy_extra,
                    **credentials,
                    "email": email,
                    "account_id": account_id,
                    "source": "account_database",
                }
                records.append(record)
            return self._accounts_from_records(records, source="account_database")
        except Exception as exc:
            self.log(f"读取 Swarms 数据库账号失败: {exc!r}")
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
            email = _safe_text(record.get("email")) or f"swarms-key-{index}@local"
            amount = _credit_amount_from_record(record, default=100.0)
            enabled = bool(api_key)
            base_url = _normalize_base_url(record.get("native_api_base") or record.get("openai_base_url") or record.get("base_url"))
            metadata = {
                "source": _safe_text(record.get("source")) or source,
                "account_id": record.get("account_id"),
                "user_id": _safe_text(record.get("user_id")),
                "native_openai": True,
            }
            accounts.append(
                TwoAPIAccount(
                    plugin=self.name,
                    email=email,
                    base_url=base_url,
                    api_key=api_key,
                    handle=_safe_text(record.get("user_name") or record.get("user_id")),
                    credit_amount=amount if enabled else 0.0,
                    credit_ok=enabled and amount >= float(self.settings.min_credit or 0.0),
                    enabled=enabled,
                    last_status="native_ready" if enabled else "credential_incomplete",
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
                if not existing.email and account.email:
                    existing.email = account.email
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
        self.log(f"加载 Swarms 账号 {len(self.accounts)} 个，其中 db={len(db_accounts)} file={len(file_accounts)}")
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
        if not account.enabled:
            return False
        if not account.api_key:
            return False
        if not account.credit_ok or float(account.credit_amount or 0.0) < self.settings.min_credit:
            self.log(f"跳过不可用 Swarms 账号: {account.email} credit={account.credit_amount}")
            return False
        return True

    def select_account(self) -> TwoAPIAccount:
        if not self.settings.enabled:
            raise RuntimeError("Swarms 2API 已禁用")
        if not self.accounts:
            self.load_accounts()
        if not self.accounts:
            if self.settings.auto_refill:
                self.refill_accounts(count=1, concurrency=1)
                self.load_accounts()
        if not self.accounts:
            raise RuntimeError("Swarms 账号池为空")
        total = len(self.accounts)
        for offset in range(total):
            idx = (self._cursor + offset) % total
            account = self.accounts[idx]
            if not self._account_is_eligible(account):
                continue
            self._cursor = (idx + 1) % total
            self.log(f"选中 Swarms 账号: {account.email}")
            return account
        if self.settings.auto_refill:
            self.refill_accounts(count=1, concurrency=1)
            self.load_accounts()
        raise RuntimeError("没有可用 Swarms 账号：全部为空额度或凭据不可用")

    def _headers(self, account: TwoAPIAccount, *, stream: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {account.api_key}",
            "x-api-key": account.api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        }
        return headers

    def _models_catalog_response(self, model_ids: list[str]) -> LocalJSONResponse:
        seen: set[str] = set()
        cleaned: list[str] = []
        for model_id in model_ids:
            text = _safe_text(model_id)
            if not text or text in seen:
                continue
            seen.add(text)
            cleaned.append(text)
        return LocalJSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model_id, "object": "model", "created": 0, "owned_by": "swarms"}
                    for model_id in cleaned
                ],
            }
        )

    def _local_models_response(self) -> LocalJSONResponse:
        return self._models_catalog_response(SWARMS_MODEL_IDS)

    def _models_response_is_openai_catalog(self, response: Any) -> bool:
        if not getattr(response, "ok", False):
            return False
        try:
            data = response.json()
        except Exception:
            return False
        return isinstance(data, dict) and data.get("object") == "list" and isinstance(data.get("data"), list)

    def _available_model_ids_from_payload(self, payload: Any) -> list[str]:
        candidates: Any = payload
        if isinstance(payload, dict):
            candidates = (
                payload.get("models")
                or payload.get("data")
                or payload.get("available_models")
                or payload.get("model_names")
                or []
            )
        if isinstance(candidates, dict):
            candidates = (
                candidates.get("models")
                or candidates.get("data")
                or candidates.get("items")
                or candidates.get("available_models")
                or []
            )
        model_ids: list[str] = []
        if not isinstance(candidates, list):
            return model_ids
        for item in candidates:
            if isinstance(item, str):
                model_ids.append(item)
                continue
            if not isinstance(item, dict):
                continue
            for key in ("id", "name", "model", "model_id"):
                value = _safe_text(item.get(key))
                if value:
                    model_ids.append(value)
                    break
        return model_ids

    def _available_models_response_to_openai_catalog(self, response: Any) -> LocalJSONResponse | None:
        if not getattr(response, "ok", False):
            return None
        try:
            payload = response.json()
        except Exception:
            return None
        if isinstance(payload, dict) and payload.get("object") == "list" and isinstance(payload.get("data"), list):
            return LocalJSONResponse(payload)
        model_ids = self._available_model_ids_from_payload(payload)
        if not model_ids:
            return None
        return self._models_catalog_response(model_ids)

    def forward_models(self) -> Any:
        # 不直接透传 /models/available：该接口会返回 1000+ 混合模型，很多对
        # chat/completions 是 200 空内容。2API 只展示已验证的聊天文本模型。
        if not self.accounts:
            self.load_accounts()
        return self._local_models_response()

    def _swarm_completions_url(self, account: TwoAPIAccount) -> str:
        base_url = _normalize_base_url(account.base_url).rstrip("/")
        if base_url.endswith("/v1"):
            return f"{base_url}/swarm/completions"
        return f"{base_url}/v1/swarm/completions"

    def _build_swarm_payload(self, payload: dict[str, Any], *, stream: bool = False) -> dict[str, Any]:
        model = _safe_text(payload.get("model")) or "gpt-4o"
        system_prompt, task = _messages_to_system_and_task(payload.get("messages"))
        if not task:
            task = _safe_text(payload.get("prompt")) or " "
        max_tokens = _safe_int(payload.get("max_tokens"), SWARMS_DEFAULT_MAX_TOKENS) or SWARMS_DEFAULT_MAX_TOKENS
        temperature = _safe_float(payload.get("temperature"), SWARMS_DEFAULT_TEMPERATURE)
        return {
            "name": SWARMS_AGENT_NAME,
            "description": SWARMS_AGENT_DESCRIPTION,
            "swarm_type": SWARMS_DEFAULT_SWARM_TYPE,
            "task": task,
            "max_loops": 1,
            "stream": bool(stream or payload.get("stream")),
            "agents": [
                {
                    "agent_name": SWARMS_AGENT_NAME,
                    "description": SWARMS_AGENT_DESCRIPTION,
                    "system_prompt": system_prompt,
                    "model_name": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "role": "worker",
                    "max_loops": 1,
                    "auto_generate_prompt": False,
                    "dynamic_temperature_enabled": True,
                }
            ],
        }

    def _openai_chat_payload(self, model: str, upstream_payload: Any) -> dict[str, Any]:
        usage_raw = upstream_payload.get("usage", {}) if isinstance(upstream_payload, dict) else {}
        return {
            "id": f"chatcmpl-swarms-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": _extract_swarms_output(upstream_payload)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": _safe_int(usage_raw.get("input_tokens"), 0) if isinstance(usage_raw, dict) else 0,
                "completion_tokens": _safe_int(usage_raw.get("output_tokens"), 0) if isinstance(usage_raw, dict) else 0,
                "total_tokens": _safe_int(usage_raw.get("total_tokens"), 0) if isinstance(usage_raw, dict) else 0,
            },
        }

    def _sse_chunk(self, chat_id: str, model: str, delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        payload = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

    def _iter_swarms_sse_events(self, upstream: Any):
        event_type = "message"
        data_lines: list[str] = []

        def flush():
            nonlocal event_type, data_lines
            if not data_lines:
                event_type = "message"
                return None
            raw = "\n".join(data_lines).strip()
            event = event_type or "message"
            event_type = "message"
            data_lines = []
            return event, raw

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
                event_type = line.split(":", 1)[1].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        item = flush()
        if item:
            yield item

    def _openai_stream_response_from_swarms(self, model: str, upstream: Any) -> StreamingSSEOpenAIResponse:
        chat_id = f"chatcmpl-swarms-{int(time.time() * 1000)}"

        def chunks():
            emitted = ""
            yield self._sse_chunk(chat_id, model, {"role": "assistant"})
            try:
                for event, raw in self._iter_swarms_sse_events(upstream):
                    if event == "error":
                        if raw:
                            yield self._sse_chunk(chat_id, model, {"content": raw}, "stop")
                        yield b"data: [DONE]\n\n"
                        return
                    if event not in {"chunk", "message"} or not raw:
                        continue
                    try:
                        content_full = _extract_swarms_output(json.loads(raw))
                    except Exception:
                        content_full = raw
                    if not content_full:
                        continue
                    delta = content_full[len(emitted):] if content_full.startswith(emitted) else content_full
                    emitted = content_full if content_full.startswith(emitted) else emitted + delta
                    if delta:
                        yield self._sse_chunk(chat_id, model, {"content": delta})
            except GeneratorExit:
                raise
            except Exception as exc:
                yield self._sse_chunk(chat_id, model, {"content": f"\n[Swarms stream error: {exc}]"}, "stop")
                yield b"data: [DONE]\n\n"
                return
            yield self._sse_chunk(chat_id, model, {}, "stop")
            yield b"data: [DONE]\n\n"

        return StreamingSSEOpenAIResponse(chunks, upstream=upstream)

    def forward_chat(self, payload: dict[str, Any], *, stream: bool = False) -> Any:
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI payload 必须是 JSON object")
        if _payload_requests_tool_call(payload):
            return LocalJSONResponse(
                {
                    "error": {
                        "message": "Swarms hosted API 当前不支持 OpenAI tools/function calling 透传；请移除 tools/functions 或设置 tool_choice/function_call 为 none。",
                        "type": "unsupported_feature",
                        "code": "swarms_tools_unsupported",
                    }
                },
                status_code=400,
            )
        if not self.accounts:
            self.load_accounts()
        attempts = max(1, max(int(self.settings.max_retries or 1), len(self.accounts)))
        last_error: Exception | None = None
        last_response: Any | None = None
        model = _safe_text(payload.get("model")) or "gpt-4o"
        for _ in range(attempts):
            account = self.select_account()
            try:
                response = self.transport.post(
                    self._swarm_completions_url(account),
                    headers=self._headers(account, stream=stream),
                    json=self._build_swarm_payload(payload, stream=stream),
                    timeout=self.settings.request_timeout,
                    stream=stream,
                )
                status = int(getattr(response, "status_code", 200) or 200)
                if getattr(response, "ok", False) or status < 500:
                    account.last_status = "chat_alive" if status < 400 else f"chat_error:{status}"
                    if status >= 400:
                        return response
                    if stream:
                        return self._openai_stream_response_from_swarms(model, response)
                    return LocalJSONResponse(self._openai_chat_payload(model, _response_json_or_text(response)))
                last_response = response
                account.last_status = f"chat_retryable_error:{status}"
                self.log(f"Swarms chat 可重试错误: {account.email} status={status}")
            except Exception as exc:
                last_error = exc
                account.last_error = repr(exc)
                account.last_status = "chat_failed"
                self.log(f"Swarms chat 转发失败: {account.email} {exc!r}")
                continue
        if last_response is not None:
            return last_response
        raise RuntimeError(str(last_error or "Swarms chat 转发失败"))


    @property
    def import_schema(self) -> TwoAPIImportSchema:
        return SWARMS_IMPORT_SCHEMA

    def _append_imported_credentials_file(self, result: dict[str, Any]) -> None:
        rows = []
        for item in list(result.get("accounts") or []):
            if not isinstance(item, dict):
                continue
            key = _safe_text(item.get("api_key") or item.get("token"))
            if not key:
                continue
            rows.append({
                "email": _safe_text(item.get("email")) or "swarms-import@local",
                "api_key": key,
                "source": _safe_text(result.get("source")) or "external_import",
                "credit_amount": _safe_float(item.get("credit_amount"), 100.0),
                "openai_base_url": _safe_text(item.get("base_url")) or SWARMS_NATIVE_BASE_URL,
            })
        if not rows:
            return
        path = self.data_dir / "swarms_credentials.json"
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
        self.log(f"导入 Swarms 外部账号: created={result.get('created')} accepted={result.get('accepted')} skipped={result.get('skipped')}")
        return result

    def _push_target_import_url(self, target_url: str) -> str:
        base = str(target_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("target_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise ValueError("target_url 必须以 http:// 或 https:// 开头")
        if base.endswith("/2api/plugins/swarms/import") or base.endswith("/api/2api/plugins/swarms/import"):
            return base
        if base.endswith("/api"):
            return f"{base}/2api/plugins/swarms/import"
        return f"{base}/api/2api/plugins/swarms/import"

    def _account_to_push_record(self, account: TwoAPIAccount) -> dict[str, Any]:
        metadata = dict(account.metadata or {})
        record: dict[str, Any] = {
            "email": account.email,
            "api_key": account.api_key,
            "ai_api_token": account.api_key,
            "base_url": account.base_url or SWARMS_NATIVE_BASE_URL,
            "openai_base_url": account.base_url or SWARMS_NATIVE_BASE_URL,
            "native_api_base": account.base_url or SWARMS_NATIVE_BASE_URL,
            "credit_amount": float(account.credit_amount or 0.0),
            "source": _safe_text(metadata.get("source")) or "swarms_local",
            "native_openai": True,
            "ok": bool(account.enabled and account.api_key),
        }
        user_id = _safe_text(metadata.get("user_id"))
        if user_id:
            record["user_id"] = user_id
        account_id = metadata.get("account_id")
        if account_id not in (None, ""):
            record["account_id"] = account_id
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
            return {"ok": False, "pushed": 0, "target_url": import_url, "error": "没有匹配的 Swarms 账号可推送"}

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
            raise ValueError(f"推送 Swarms 账号失败: status={getattr(response, 'status_code', 0)} body={str(data)[:500]}")
        self.log(f"推送 Swarms 账号到远端完成: pushed={len(records)} target={import_url}")
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
            "extra": {
                "twoapi_auto_refill": True,
                **dict(extra or {}),
            },
        }
        task = create_register_task(payload)
        task_runtime.wake_up()
        self.log(f"已创建 Swarms 自动补号任务: task_id={task.get('id')} count={resolved_count}")
        return {"ok": True, "task": task, "payload": payload}

    def refresh_credits(self) -> list[TwoAPIAccount]:
        if not self.accounts:
            self.load_accounts()
        return self.accounts
