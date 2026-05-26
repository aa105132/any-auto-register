from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from services.twoapi.key_store import TwoAPIKeyStore
from services.twoapi.models import TwoAPISettings, mask_secret
from services.twoapi.plugins.swarms import SwarmsTwoAPIPlugin
from services.twoapi.plugins.zo import ZoTwoAPIPlugin

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "output"


class TwoAPIManager:
    def __init__(self, *, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "twoapi_settings.json"
        self.key_store = TwoAPIKeyStore(self.data_dir / "twoapi_keys.json")
        self.settings = self._load_settings()
        self.plugins = {
            "zo": ZoTwoAPIPlugin(settings=self.settings, data_dir=self.data_dir),
            "swarms": SwarmsTwoAPIPlugin(settings=self.settings, data_dir=self.data_dir),
        }
        self._keepalive_thread: threading.Thread | None = None
        self._keepalive_stop = threading.Event()
        self._keepalive_running = False

    def _load_settings(self) -> TwoAPISettings:
        if not self.settings_path.exists():
            return TwoAPISettings()
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        return TwoAPISettings(**{k: v for k, v in dict(raw or {}).items() if k in TwoAPISettings.__annotations__})

    def save_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        current = self.settings.__dict__.copy()
        for key in TwoAPISettings.__annotations__:
            if key in data:
                current[key] = data[key]
        self.settings = TwoAPISettings(**current)
        self.settings_path.write_text(json.dumps(self.settings.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        for plugin in self.plugins.values():
            plugin.settings = self.settings
        return self.settings.__dict__

    def list_plugins(self) -> list[dict[str, Any]]:
        return [plugin.status() for plugin in self.plugins.values()]

    def get_plugin(self, plugin: str):
        if plugin not in self.plugins:
            raise KeyError(plugin)
        return self.plugins[plugin]

    def status(self) -> dict[str, Any]:
        plugins = self.list_plugins()
        return {
            "ok": True,
            "listen": "http://127.0.0.1:6543/zo/v1",
            "listen_urls": ["http://127.0.0.1:6543/zo/v1", "http://127.0.0.1:6543/swarms/v1"],
            "settings": self.settings.__dict__,
            "plugins": plugins,
            "key_count": len(self.key_store.list()),
        }

    def list_keys(self) -> list[dict[str, Any]]:
        rows = []
        for row in self.key_store.list():
            item = dict(row)
            item["key_preview"] = mask_secret(str(item.get("key") or ""))
            rows.append(item)
        return rows

    def import_plugin_accounts(
        self,
        plugin: str,
        *,
        records: list[dict[str, Any]] | None = None,
        lines: list[str] | None = None,
        source: str = "external",
    ) -> dict[str, Any]:
        item = self.get_plugin(plugin)
        if not hasattr(item, "import_accounts"):
            raise NotImplementedError(f"插件不支持外部账号导入: {plugin}")
        return item.import_accounts(records=records, lines=lines, source=source)

    def push_plugin_accounts(
        self,
        plugin: str,
        *,
        target_url: str,
        source: str = "external-push",
        emails: list[str] | None = None,
        latest_only: bool = False,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        item = self.get_plugin(plugin)
        if not hasattr(item, "push_accounts"):
            raise NotImplementedError(f"插件不支持外部账号推送: {plugin}")
        return item.push_accounts(
            target_url,
            source=source,
            emails=emails or [],
            latest_only=latest_only,
            timeout=timeout,
        )

    def refill_plugin_accounts(
        self,
        plugin: str,
        *,
        count: int = 1,
        concurrency: int = 1,
        executor_type: str = "protocol",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = self.get_plugin(plugin)
        if not hasattr(item, "refill_accounts"):
            raise NotImplementedError(f"插件不支持自动补号: {plugin}")
        return item.refill_accounts(count=count, concurrency=concurrency, executor_type=executor_type, extra=extra or {})

    def create_key(self, *, plugin: str = "zo", note: str = "") -> dict[str, Any]:
        row = self.key_store.create(plugin=plugin, note=note)
        out = dict(row)
        out["key_preview"] = mask_secret(str(out.get("key") or ""))
        return out

    def delete_key(self, key_id: str) -> bool:
        return self.key_store.delete(key_id)

    def verify_key(self, key: str, *, plugin: str = "") -> bool:
        return self.key_store.verify(key, plugin=plugin)

    def keepalive_once(self) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for name, plugin in self.plugins.items():
            if hasattr(plugin, "keepalive_once"):
                results[name] = plugin.keepalive_once()
        return results

    def start_keepalive(self, *, interval_seconds: float = 300.0) -> None:
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._keepalive_stop.clear()
        self._keepalive_running = True
        interval = max(30.0, float(interval_seconds or 300.0))

        def loop() -> None:
            while not self._keepalive_stop.is_set():
                try:
                    self.keepalive_once()
                except Exception:
                    pass
                self._keepalive_stop.wait(interval)
            self._keepalive_running = False

        self._keepalive_thread = threading.Thread(target=loop, name="twoapi-keepalive", daemon=True)
        self._keepalive_thread.start()

    def stop_keepalive(self) -> None:
        self._keepalive_stop.set()
        thread = self._keepalive_thread
        if thread and thread.is_alive():
            thread.join(timeout=5.0)
        self._keepalive_running = False


_twoapi_manager: TwoAPIManager | None = None


def get_twoapi_manager() -> TwoAPIManager:
    global _twoapi_manager
    if _twoapi_manager is None:
        _twoapi_manager = TwoAPIManager()
    return _twoapi_manager
