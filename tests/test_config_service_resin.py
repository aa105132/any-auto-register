from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "application" / "config.py"


provider_defs_module = types.ModuleType("application.provider_definitions")


class _ProviderDefinitionsService:
    def list_definitions(self, *_args, **_kwargs):
        return []

    def list_driver_templates(self, *_args, **_kwargs):
        return []


provider_defs_module.ProviderDefinitionsService = _ProviderDefinitionsService
sys.modules["application.provider_definitions"] = provider_defs_module

provider_settings_module = types.ModuleType("application.provider_settings")


class _ProviderSettingsService:
    def get_captcha_policy(self):
        return {}

    def list_settings(self, *_args, **_kwargs):
        return []


provider_settings_module.ProviderSettingsService = _ProviderSettingsService
sys.modules["application.provider_settings"] = provider_settings_module

config_repo_module = types.ModuleType("infrastructure.config_repository")


class _ConfigRepository:
    def get_flat(self) -> dict[str, str]:
        return {}


config_repo_module.ConfigRepository = _ConfigRepository
sys.modules["infrastructure.config_repository"] = config_repo_module

resin_runtime_module = types.ModuleType("infrastructure.resin_runtime")


class _ResinRuntime:
    def probe(self, data: dict[str, str], task_platform: str = "") -> dict:
        return {"ok": True, "data": dict(data), "task_platform": task_platform}


resin_runtime_module.ResinRuntime = _ResinRuntime
sys.modules["infrastructure.resin_runtime"] = resin_runtime_module

SPEC = importlib.util.spec_from_file_location("application.config", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
for module_name in (
    "application.provider_definitions",
    "application.provider_settings",
    "infrastructure.config_repository",
    "infrastructure.resin_runtime",
):
    sys.modules.pop(module_name, None)

ConfigService = MODULE.ConfigService


class _DummyRepository:
    def get_flat(self) -> dict[str, str]:
        return {
            "resin_enabled": "false",
            "resin_host": "resin.local",
            "resin_port": "2260",
            "resin_token": "base-token",
            "resin_default_platform": "Default",
        }


class _DummyRuntime:
    def __init__(self):
        self.calls: list[tuple[dict[str, str], str]] = []

    def probe(self, data: dict[str, str], task_platform: str = "") -> dict:
        self.calls.append((dict(data), task_platform))
        return {"ok": True, "task_platform": task_platform, "proxy_url": "http://checked"}


class ConfigServiceResinTests(unittest.TestCase):
    def test_check_resin_merges_runtime_override_before_probe(self):
        runtime = _DummyRuntime()
        service = ConfigService(repository=_DummyRepository(), resin_runtime=runtime)

        result = service.check_resin(
            {
                "resin_enabled": "true",
                "resin_token": "override-token",
                "resin_platform_map": "venice=SeedancePool",
            },
            task_platform="venice",
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["task_platform"], "venice")
        self.assertEqual(len(runtime.calls), 1)
        merged, platform = runtime.calls[0]
        self.assertEqual(platform, "venice")
        self.assertEqual(merged["resin_token"], "override-token")
        self.assertEqual(merged["resin_platform_map"], "venice=SeedancePool")
        self.assertEqual(merged["resin_host"], "resin.local")


if __name__ == "__main__":
    unittest.main()
