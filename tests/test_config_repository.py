from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "infrastructure" / "config_repository.py"


config_store_module = types.ModuleType("core.config_store")


class _StubConfigStore:
    def get(self, _key: str, default: str = "") -> str:
        return default

    def set_many(self, _data: dict[str, str]) -> None:
        pass


config_store_module.config_store = _StubConfigStore()
sys.modules.setdefault("core.config_store", config_store_module)

provider_defs_module = types.ModuleType("infrastructure.provider_definitions_repository")


class _ProviderDefinitionsRepository:
    def list_by_type(self, provider_type: str, enabled_only: bool = False):
        return []


provider_defs_module.ProviderDefinitionsRepository = _ProviderDefinitionsRepository
sys.modules.setdefault("infrastructure.provider_definitions_repository", provider_defs_module)

SPEC = importlib.util.spec_from_file_location("infrastructure.config_repository", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

ConfigRepository = MODULE.ConfigRepository


class _DummyDefinitions:
    def list_by_type(self, provider_type: str, enabled_only: bool = False):
        return []


class _DummyConfigStore:
    def __init__(self):
        self.saved = None

    def set_many(self, data: dict[str, str]) -> None:
        self.saved = dict(data)


class ConfigRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = ConfigRepository(definitions=_DummyDefinitions())

    def test_allowed_keys_include_codebanana2api_config(self):
        allowed = self.repository.get_allowed_keys()

        self.assertIn("codebanana2api_url", allowed)
        self.assertIn("codebanana2api_enabled", allowed)
        self.assertIn("resin_enabled", allowed)
        self.assertIn("resin_proxy_url", allowed)
        self.assertIn("resin_scheme", allowed)
        self.assertIn("resin_host", allowed)
        self.assertIn("resin_port", allowed)
        self.assertIn("resin_token", allowed)
        self.assertIn("resin_default_platform", allowed)
        self.assertIn("resin_platform_map", allowed)
        self.assertIn("scdn_runtime_enabled", allowed)
        self.assertIn("scdn_runtime_protocol", allowed)
        self.assertIn("scdn_runtime_country_code", allowed)
        self.assertIn("scdn_runtime_count", allowed)
        self.assertIn("scdn_runtime_validate_url", allowed)
        self.assertIn("scdn_runtime_validate_timeout_sec", allowed)
        self.assertIn("scdn_runtime_cache_ttl_sec", allowed)
        self.assertIn("scdn_runtime_cache_size", allowed)

    def test_update_flat_persists_codebanana2api_config(self):
        store = _DummyConfigStore()
        original_store = MODULE.config_store
        MODULE.config_store = store
        try:
            updated = self.repository.update_flat(
                {
                    "codebanana2api_url": "http://127.0.0.1:8080",
                    "codebanana2api_enabled": "true",
                }
            )
        finally:
            MODULE.config_store = original_store

        self.assertIn("codebanana2api_url", updated)
        self.assertIn("codebanana2api_enabled", updated)
        self.assertEqual(
            store.saved,
            {
                "codebanana2api_url": "http://127.0.0.1:8080",
                "codebanana2api_enabled": "true",
            },
        )

    def test_update_flat_persists_resin_config(self):
        store = _DummyConfigStore()
        original_store = MODULE.config_store
        MODULE.config_store = store
        try:
            updated = self.repository.update_flat(
                {
                    "resin_enabled": "true",
                    "resin_proxy_url": "http://Default:token@127.0.0.1:2260",
                }
            )
        finally:
            MODULE.config_store = original_store

        self.assertIn("resin_enabled", updated)
        self.assertIn("resin_proxy_url", updated)
        self.assertEqual(
            store.saved,
            {
                "resin_enabled": "true",
                "resin_proxy_url": "http://Default:token@127.0.0.1:2260",
            },
        )

    def test_update_flat_persists_scdn_runtime_config(self):
        store = _DummyConfigStore()
        original_store = MODULE.config_store
        MODULE.config_store = store
        try:
            updated = self.repository.update_flat(
                {
                    "scdn_runtime_enabled": "true",
                    "scdn_runtime_protocol": "http",
                    "scdn_runtime_country_code": "HK",
                    "scdn_runtime_count": "10",
                    "scdn_runtime_validate_url": "https://httpbin.org/ip",
                    "scdn_runtime_validate_timeout_sec": "8",
                    "scdn_runtime_cache_ttl_sec": "120",
                    "scdn_runtime_cache_size": "20",
                }
            )
        finally:
            MODULE.config_store = original_store

        self.assertIn("scdn_runtime_enabled", updated)
        self.assertIn("scdn_runtime_protocol", updated)
        self.assertIn("scdn_runtime_country_code", updated)
        self.assertEqual(
            store.saved,
            {
                "scdn_runtime_enabled": "true",
                "scdn_runtime_protocol": "http",
                "scdn_runtime_country_code": "HK",
                "scdn_runtime_count": "10",
                "scdn_runtime_validate_url": "https://httpbin.org/ip",
                "scdn_runtime_validate_timeout_sec": "8",
                "scdn_runtime_cache_ttl_sec": "120",
                "scdn_runtime_cache_size": "20",
            },
        )


if __name__ == "__main__":
    unittest.main()
