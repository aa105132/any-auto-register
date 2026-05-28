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

COMMON_PLUGIN_SETTING_KEYS = (
    "enabled",
    "min_credit",
    "auto_refill",
    "request_timeout",
    "max_retries",
)
PLUGIN_SETTING_KEYS: dict[str, tuple[str, ...]] = {
    "zo": (
        "enabled",
        "min_credit",
        "auto_wake",
        "auto_refill",
        "request_timeout",
        "wake_timeout",
        "max_retries",
        "keepalive_space_fallback",
        "minimize_ask_context",
    ),
    "swarms": COMMON_PLUGIN_SETTING_KEYS,
}


class TwoAPIManager:
    def __init__(self, *, data_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "twoapi_settings.json"
        self.key_store = TwoAPIKeyStore(self.data_dir / "twoapi_keys.json")
        self.settings = self._load_settings()
        self.plugin_settings = self._load_plugin_settings()
        self.plugins = {
            "zo": ZoTwoAPIPlugin(settings=self.get_plugin_settings("zo"), data_dir=self.data_dir),
            "swarms": SwarmsTwoAPIPlugin(settings=self.get_plugin_settings("swarms"), data_dir=self.data_dir),
        }
        self._keepalive_thread: threading.Thread | None = None
        self._keepalive_stop = threading.Event()
        self._keepalive_running = False

    def _settings_from_mapping(self, raw: Any, *, base: TwoAPISettings | None = None) -> TwoAPISettings:
        current = (base or TwoAPISettings()).__dict__.copy()
        for key, value in dict(raw or {}).items():
            if key in TwoAPISettings.__annotations__:
                current[key] = value
        return TwoAPISettings(**current)

    def _load_settings_payload(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {}
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        return dict(raw or {}) if isinstance(raw, dict) else {}

    def _write_settings_payload(self) -> None:
        payload = {
            **self.settings.__dict__,
            "plugins": {name: self.serialize_plugin_settings(name) for name in sorted(self.plugin_settings)},
        }
        self.settings_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_settings(self) -> TwoAPISettings:
        raw = self._load_settings_payload()
        # 旧版本直接把 settings 字段写在根对象；继续作为默认设置读取。
        return self._settings_from_mapping(raw)

    def _load_plugin_settings(self) -> dict[str, TwoAPISettings]:
        raw = self._load_settings_payload()
        plugins_raw = raw.get("plugins") if isinstance(raw.get("plugins"), dict) else {}
        result: dict[str, TwoAPISettings] = {}
        for name in ("zo", "swarms"):
            result[name] = self._settings_from_mapping(dict(plugins_raw.get(name) or {}), base=self.settings)
        return result

    def get_plugin_settings(self, plugin: str) -> TwoAPISettings:
        name = str(plugin or "").strip()
        if not name:
            raise KeyError(plugin)
        if name not in self.plugin_settings:
            self.plugin_settings[name] = self._settings_from_mapping({}, base=self.settings)
        return self.plugin_settings[name]

    def plugin_setting_keys(self, plugin: str) -> tuple[str, ...]:
        name = str(plugin or "").strip()
        if not name:
            raise KeyError(plugin)
        return PLUGIN_SETTING_KEYS.get(name, COMMON_PLUGIN_SETTING_KEYS)

    def serialize_plugin_settings(self, plugin: str) -> dict[str, Any]:
        settings = self.get_plugin_settings(plugin)
        keys = self.plugin_setting_keys(plugin)
        return {key: getattr(settings, key) for key in keys if hasattr(settings, key)}

    def save_plugin_settings(self, plugin: str, data: dict[str, Any]) -> dict[str, Any]:
        if plugin not in self.plugins and plugin not in self.plugin_settings:
            raise KeyError(plugin)
        allowed_keys = set(self.plugin_setting_keys(plugin))
        current = self.get_plugin_settings(plugin).__dict__.copy()
        for key in TwoAPISettings.__annotations__:
            if key in allowed_keys and key in data:
                current[key] = data[key]
        settings = TwoAPISettings(**current)
        self.plugin_settings[plugin] = settings
        if plugin in self.plugins:
            self.plugins[plugin].settings = settings
        self._write_settings_payload()
        return self.serialize_plugin_settings(plugin)

    def save_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        current = self.settings.__dict__.copy()
        for key in TwoAPISettings.__annotations__:
            if key in data:
                current[key] = data[key]
        self.settings = TwoAPISettings(**current)
        for name in list(self.plugin_settings):
            self.plugin_settings[name] = self._settings_from_mapping(self.plugin_settings[name].__dict__, base=self.settings)
            if name in self.plugins:
                self.plugins[name].settings = self.plugin_settings[name]
        self._write_settings_payload()
        return self.settings.__dict__

    def list_plugins(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for plugin in self.plugins.values():
            row = plugin.status()
            row["settings"] = self.serialize_plugin_settings(plugin.name)
            rows.append(row)
        return rows

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
            "plugin_settings": {name: self.serialize_plugin_settings(name) for name in sorted(self.plugin_settings)},
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
