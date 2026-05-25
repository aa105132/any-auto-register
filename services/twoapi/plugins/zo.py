from __future__ import annotations

import json
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
    ) -> None:
        self.settings = settings or TwoAPISettings()
        self.transport = transport or requests.Session()
        self.data_dir = Path(data_dir or OUT_DIR)
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
        self.accounts = parse_zo_proxy_lines(lines)
        self._enrich_accounts_from_registration_results(self.accounts)
        self.log(f"加载 Zo 账号 {len(self.accounts)} 个")
        return self.accounts

    def status(self) -> dict[str, Any]:
        if not self.accounts:
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
        for account in list(self.accounts):
            if not account.enabled or not account.credit_ok or float(account.credit_amount or 0.0) < self.settings.min_credit:
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
        return {"checked": checked, "alive": alive, "recovered": recovered, "failed": failed}

    def forward_models(self) -> Any:
        last_error: Exception | None = None
        for _ in range(max(1, self.settings.max_retries)):
            account = self.select_account()
            try:
                self._ensure_alive(account)
                return self.transport.get(f"{account.base_url}/models", timeout=self.settings.request_timeout)
            except Exception as exc:
                last_error = exc
                account.last_error = repr(exc)
                self.log(f"models 转发失败: {account.email} {exc!r}")
        raise RuntimeError(str(last_error or "models 转发失败"))

    def forward_chat(self, payload: dict[str, Any], *, stream: bool = False) -> Any:
        last_error: Exception | None = None
        for _ in range(max(1, self.settings.max_retries)):
            account = self.select_account()
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
                self.log(f"chat 转发失败: {account.email} {exc!r}")
        raise RuntimeError(str(last_error or "chat 转发失败"))
