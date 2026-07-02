"""Hex 2API 插件：GraphQL APQ thread LLM → OpenAI /v1/chat/completions 转换。

hex 无原生 OpenAI 兼容端点，2api 包装 app.hex.tech GraphQL Apollo APQ 的 AgentChatMessage
（主人指令："把里面 llm 包装成 OpenAI 兼容端点"）。注册后 magic-link session cookie + GraphQL
thread LLM（createAskProjectAndThread + AgentChatMessage + subscription）。

端点待 workflow 抓包确认（见 platforms/hex/core.py HexClient.chat TODO）。
本插件先搭骨架：账号加载 + forward_models + forward_chat（调 HexClient.chat）。
真实 chat variables shape 确认后补 core.py 的 chat 实现。

账号来源：account_manager.db（platform='hex'，token=session cookie）+
output/hex_credentials.json + hex_keys.txt。
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import requests

from platforms.hex.core import (
    APP_URL,
    DEFAULT_MODEL,
    FREE_MODELS,
    HEX_MODELS,
    HexClient,
    account_preview,
)
from services.twoapi.importer import TwoAPIImportSchema, import_twoapi_accounts
from services.twoapi.models import TwoAPIAccount, TwoAPISettings, mask_secret_in_text

ROOT = Path(__file__).resolve().parents[3]
OUT_DIR = ROOT / "output"
ACCOUNT_DB_PATH = ROOT / "account_manager.db"

HEX_OPENAI_BASE_URL = f"{APP_URL}/v1"
HEX_PUBLIC_MODELS = list(HEX_MODELS)
HEX_DEFAULT_MODEL = DEFAULT_MODEL

HEX_IMPORT_SCHEMA = TwoAPIImportSchema(
    plugin="hex",
    platform="hex",
    token_fields=("api_key", "ai_api_token", "session_token", "token", "key"),
    email_fields=("email", "account", "username", "user_email"),
    user_id_fields=("user_id", "uid", "id", "org_id"),
    base_url_fields=("api_base", "base_url", "openai_compatible_api_base"),
    default_base_url=HEX_OPENAI_BASE_URL,
    token_prefixes=(),
    min_token_length=16,
    credential_aliases=("api_key", "ai_api_token", "session_token", "token"),
    primary_token_field="session_token",
    metadata_defaults={"openai_compatible": False, "native_hex": True, "transport": "graphql"},
)


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


def _looks_like_hex_token(value: Any) -> bool:
    """hex token 是 session cookie（可能很长或 JSON）。宽松判定：够长、无空格/@/协议头。"""
    text = _safe_text(value)
    if len(text) < 16:
        return False
    if "@" in text or "://" in text or any(ch.isspace() for ch in text):
        return False
    return True


def _extract_token(record: dict[str, Any]) -> str:
    for key in ("session_token", "api_key", "ai_api_token", "token", "key", "primary_token"):
        value = _safe_text(record.get(key))
        if _looks_like_hex_token(value):
            return value
    credentials = record.get("credentials")
    if isinstance(credentials, dict):
        for key in ("session_token", "api_key", "ai_api_token", "token", "key"):
            value = _safe_text(credentials.get(key))
            if _looks_like_hex_token(value):
                return value
    return ""


def _extract_session_cookies(record: dict[str, Any]) -> dict[str, str]:
    """从 record 提取 session_cookies dict；兜底解析 token 字段的 JSON 字符串。"""
    cookies = record.get("session_cookies")
    if isinstance(cookies, dict):
        return {str(k): str(v) for k, v in cookies.items() if v not in (None, "")}
    # extra 里也可能有 session_cookies（_map_result 存的）
    extra = record.get("extra")
    if isinstance(extra, dict):
        cookies = extra.get("session_cookies")
        if isinstance(cookies, dict):
            return {str(k): str(v) for k, v in cookies.items() if v not in (None, "")}
    # 兜底：token 字段是 JSON 字符串（worker 无 session cookie 名命中时存全量 JSON）
    token = _extract_token(record)
    if token and token.startswith("{") and token.endswith("}"):
        try:
            parsed = json.loads(token)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items() if v not in (None, "")}
        except Exception:
            pass
    return {}


class HexTwoAPIPlugin:
    name = "hex"
    display_name = "Hex GraphQL→OpenAI 代理"

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
        with (log_dir / "hex.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def recent_logs(self, *, limit: int = 200) -> list[str]:
        file_path = self.data_dir / "twoapi_logs" / "hex.log"
        rows = list(self._logs)
        if file_path.exists():
            try:
                rows = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                pass
        return rows[-max(1, min(limit, 1000)):]

    def _credential_paths(self) -> list[Path]:
        return [
            self.data_dir / "hex_credentials.json",
            self.data_dir / "hex_account_result.json",
            self.data_dir / "hex_e2e_result.json",
        ]

    def _load_accounts_from_files(self) -> list[TwoAPIAccount]:
        records: list[dict[str, Any]] = []
        for path in self._credential_paths():
            records.extend(_load_json_records(path))
        keys_path = self.data_dir / "hex_keys.txt"
        if keys_path.exists():
            for index, raw in enumerate(keys_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
                text = raw.strip()
                if not text or text.startswith("#"):
                    continue
                parts = [part.strip() for part in text.split("|") if part.strip()]
                key = next((part for part in parts if _looks_like_hex_token(part)), "")
                if key:
                    email = next((part for part in parts if "@" in part), "") or f"hex-key-{index}@local"
                    records.append({"email": email, "session_token": key, "source": "hex_keys"})
        return self._accounts_from_records(records, source="file")

    def _load_accounts_from_database(self) -> list[TwoAPIAccount]:
        db_path = self.account_db_path
        if not db_path.exists():
            return []
        try:
            connection = sqlite3.connect(str(db_path))
            connection.row_factory = sqlite3.Row
        except Exception as exc:
            self.log(f"读取 Hex 账号数据库失败: {exc!r}")
            return []
        try:
            rows = connection.execute(
                "SELECT id, email, user_id FROM accounts WHERE lower(platform)='hex' ORDER BY id ASC"
            ).fetchall()
            records: list[dict[str, Any]] = []
            for row in rows:
                account_id = int(row["id"] or 0)
                email = _safe_text(row["email"])
                if not email:
                    continue
                credential_rows = connection.execute(
                    "SELECT key, value FROM account_credentials WHERE account_id=? AND provider_name='hex'",
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
            self.log(f"读取 Hex 数据库账号失败: {exc!r}")
            return []
        finally:
            connection.close()

    def _accounts_from_records(self, records: list[dict[str, Any]], *, source: str) -> list[TwoAPIAccount]:
        accounts: list[TwoAPIAccount] = []
        for index, record in enumerate(records, start=1):
            if record.get("ok") is False:
                continue
            token = _extract_token(record)
            if not token:
                continue
            email = _safe_text(record.get("email")) or f"hex-key-{index}@local"
            amount = _safe_float(record.get("credit_amount"), 100.0)
            base_url = _safe_text(record.get("api_base") or record.get("base_url")) or HEX_OPENAI_BASE_URL
            overview = _safe_dict(record.get("account_overview"))
            session_cookies = _extract_session_cookies(record)
            metadata = {
                "source": _safe_text(record.get("source")) or source,
                "account_id": record.get("account_id"),
                "user_id": _safe_text(record.get("user_id") or overview.get("user_id")),
                "org_id": _safe_text(record.get("org_id") or overview.get("org_id")),
                "session_cookies": session_cookies,
                "default_free_model": _safe_text(record.get("default_free_model")) or HEX_DEFAULT_MODEL,
                "free_models": record.get("free_models") if isinstance(record.get("free_models"), list) else list(HEX_PUBLIC_MODELS),
                "openai_compatible": False,
                "native_hex": True,
                "transport": "graphql",
            }
            accounts.append(
                TwoAPIAccount(
                    plugin=self.name,
                    email=email,
                    base_url=base_url,
                    api_key=token,
                    handle=_safe_text(metadata.get("org_id") or metadata.get("user_id")),
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
        self.log(f"加载 Hex 账号 {len(self.accounts)} 个，其中 db={len(db_accounts)} file={len(file_accounts)}")
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
            self.log(f"跳过不可用 Hex 账号: {account.email} credit={account.credit_amount}")
            return False
        return True

    def select_account(self) -> TwoAPIAccount:
        if not self.settings.enabled:
            raise RuntimeError("Hex 2API 已禁用")
        if not self.accounts:
            self.load_accounts()
        if not self.accounts and self.settings.auto_refill:
            self.refill_accounts(count=1, concurrency=1)
            self.load_accounts()
        if not self.accounts:
            raise RuntimeError("Hex 账号池为空")
        total = len(self.accounts)
        for offset in range(total):
            idx = (self._cursor + offset) % total
            account = self.accounts[idx]
            if not self._account_is_eligible(account):
                continue
            self._cursor = (idx + 1) % total
            self.log(f"选中 Hex 账号: {account.email}")
            return account
        if self.settings.auto_refill:
            self.refill_accounts(count=1, concurrency=1)
            self.load_accounts()
        raise RuntimeError("没有可用 Hex 账号：全部额度耗尽或凭据不可用")

    def _models_catalog_response(self) -> LocalJSONResponse:
        return LocalJSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model_id, "object": "model", "created": 0, "owned_by": "hex"}
                    for model_id in HEX_PUBLIC_MODELS
                ],
            }
        )

    def forward_models(self) -> Any:
        if not self.accounts:
            self.load_accounts()
        return self._models_catalog_response()

    def _build_openai_response(self, *, model: str, text: str, stop_reason: str, is_error: bool) -> dict[str, Any]:
        finish = "stop"
        if stop_reason in ("max_tokens", "length"):
            finish = "length"
        elif stop_reason in ("tool_use", "tool_calls"):
            finish = "tool_calls"
        return {
            "id": f"chatcmpl-hex-{uuid.uuid4().hex[:16]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model or HEX_DEFAULT_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text or ("[hex error] " + stop_reason) if is_error else text},
                    "finish_reason": finish,
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def forward_chat(self, payload: dict[str, Any], *, stream: bool = False) -> Any:
        """转 OpenAI chat → hex GraphQL thread LLM。

        TODO：core.py HexClient.chat variables shape 抓包确认后补全。当前抛 NotImplementedError 提示需抓包。
        """
        if not isinstance(payload, dict):
            raise RuntimeError("OpenAI payload 必须是 JSON object")
        if not self.accounts:
            self.load_accounts()
        account = self.select_account()
        client = HexClient(proxy=None, log_fn=self.log)
        model = _safe_text(payload.get("model")) or HEX_DEFAULT_MODEL
        messages = payload.get("messages") or []
        if not isinstance(messages, list) or not messages:
            prompt = _safe_text(payload.get("prompt")) or "Hello"
            messages = [{"role": "user", "content": prompt}]
        meta = dict(account.metadata or {})
        session_cookies = _safe_dict(meta.get("session_cookies"))
        if not session_cookies:
            # 兜底：token 是 JSON 字符串（worker 无 session cookie 名命中时存全量 JSON）
            session_cookies = _extract_session_cookies({"session_token": account.api_key})
        org_id = _safe_text(meta.get("org_id"))
        last_msg = messages[-1] if messages else {}
        prompt_text = _safe_text(last_msg.get("content")) if isinstance(last_msg, dict) else _safe_text(last_msg)
        try:
            result = client.chat(
                session_cookies=session_cookies,
                org_id=org_id,
                prompt=prompt_text or "Hello",
                model=model,
                stream=stream,
            )
            text = str(result.get("text") or "")
            stop_reason = str(result.get("stop_reason") or "end_turn")
            is_error = bool(result.get("is_error"))
            account.last_status = "chat_alive" if not is_error else f"chat_error:{stop_reason}"
            if stream:
                # TODO：流式响应格式确认后补 SSE 包装（subscription AgentChatUpdatedThreadById）
                from services.twoapi.plugins.kombai import _KombaiStreamResponse
                return _KombaiStreamResponse([], status_code=200)
            return LocalJSONResponse(
                self._build_openai_response(model=model, text=text, stop_reason=stop_reason, is_error=is_error)
            )
        except NotImplementedError as exc:
            account.last_status = "chat_not_impl"
            raise RuntimeError(f"Hex chat 端点未实现: {exc}")
        except Exception as exc:
            account.last_error = repr(exc)
            account.last_status = "chat_failed"
            self.log(f"Hex chat 转发失败: {account.email} {exc!r}")
            raise

    @property
    def import_schema(self) -> TwoAPIImportSchema:
        return HEX_IMPORT_SCHEMA

    def _append_imported_credentials_file(self, result: dict[str, Any]) -> None:
        rows = []
        for item in list(result.get("accounts") or []):
            if not isinstance(item, dict):
                continue
            key = _safe_text(item.get("session_token") or item.get("api_key") or item.get("token"))
            if not key:
                continue
            rows.append(
                {
                    "email": _safe_text(item.get("email")) or "hex-import@local",
                    "session_token": key,
                    "api_key": key,
                    "ai_api_token": key,
                    "source": _safe_text(result.get("source")) or "external_import",
                    "credit_amount": _safe_float(item.get("credit_amount"), 100.0),
                    "free_models": list(HEX_PUBLIC_MODELS),
                    "ok": True,
                }
            )
        if not rows:
            return
        path = self.data_dir / "hex_credentials.json"
        existing = _load_json_records(path)
        existing.extend(rows)
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in existing:
            key = _extract_token(row)
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
        self.log(f"导入 Hex 外部账号: created={result.get('created')} accepted={result.get('accepted')} skipped={result.get('skipped')}")
        return result

    def _push_target_import_url(self, target_url: str) -> str:
        base = str(target_url or "").strip().rstrip("/")
        if not base:
            raise ValueError("target_url 不能为空")
        if not base.startswith(("http://", "https://")):
            raise ValueError("target_url 必须以 http:// 或 https:// 开头")
        if base.endswith("/2api/plugins/hex/import") or base.endswith("/api/2api/plugins/hex/import"):
            return base
        if base.endswith("/api"):
            return f"{base}/2api/plugins/hex/import"
        return f"{base}/api/2api/plugins/hex/import"

    def _account_to_push_record(self, account: TwoAPIAccount) -> dict[str, Any]:
        metadata = dict(account.metadata or {})
        record: dict[str, Any] = {
            "email": account.email,
            "session_token": account.api_key,
            "api_key": account.api_key,
            "ai_api_token": account.api_key,
            "base_url": account.base_url or HEX_OPENAI_BASE_URL,
            "credit_amount": float(account.credit_amount or 0.0),
            "source": _safe_text(metadata.get("source")) or "hex_local",
            "native_hex": True,
            "openai_compatible": False,
            "transport": "graphql",
            "free_models": list(HEX_PUBLIC_MODELS),
            "ok": bool(account.enabled and account.api_key),
        }
        for key in ("user_id", "org_id", "account_id"):
            value = metadata.get(key)
            if value not in (None, ""):
                record[key] = value
        session_cookies = metadata.get("session_cookies")
        if isinstance(session_cookies, dict) and session_cookies:
            record["session_cookies"] = session_cookies
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
            return {"ok": False, "pushed": 0, "target_url": import_url, "error": "没有匹配的 Hex 账号可推送"}
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
            raise ValueError(f"推送 Hex 账号失败: status={getattr(response, 'status_code', 0)} body={str(data)[:500]}")
        self.log(f"推送 Hex 账号到远端完成: pushed={len(records)} target={import_url}")
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
        self.log(f"已创建 Hex 自动补号任务: task_id={task.get('id')} count={resolved_count}")
        return {"ok": True, "task": task, "payload": payload}

    def refresh_credits(self) -> list[TwoAPIAccount]:
        if not self.accounts:
            self.load_accounts()
        return self.accounts
