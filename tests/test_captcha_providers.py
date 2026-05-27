from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.base_captcha import YesCaptcha, create_captcha_solver
from core.provider_drivers import get_driver_template, list_builtin_provider_definitions


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class CaptchaProviderDriverTests(unittest.TestCase):
    def test_yescaptcha_driver_exposes_custom_api_url_field(self):
        template = get_driver_template("captcha", "yescaptcha_api")

        self.assertIsNotNone(template)
        field_keys = {str(item.get("key")) for item in template.get("fields", [])}
        self.assertIn("yescaptcha_key", field_keys)
        self.assertIn("yescaptcha_api_url", field_keys)

    def test_builtin_captcha_definitions_include_ohmycaptcha(self):
        definitions = {
            str(item.get("provider_key")): item
            for item in list_builtin_provider_definitions("captcha")
        }

        self.assertIn("ohmycaptcha", definitions)
        self.assertEqual(definitions["ohmycaptcha"]["driver_type"], "yescaptcha_api")


class YesCaptchaCompatibilityTests(unittest.TestCase):
    def test_yescaptcha_can_use_custom_base_url(self):
        solver = YesCaptcha("client-key", "http://localhost:8000/")
        responses = [
            _FakeResponse({"taskId": "task-1"}),
            _FakeResponse({"status": "ready", "solution": {"token": "turnstile-token"}}),
        ]

        with (
            patch("requests.post", side_effect=responses) as mock_post,
            patch("time.sleep", return_value=None),
            patch("urllib3.disable_warnings", return_value=None),
        ):
            token = solver.solve_turnstile("https://example.com/signup", "site-key")

        self.assertEqual(token, "turnstile-token")
        first_url = mock_post.call_args_list[0].args[0]
        second_url = mock_post.call_args_list[1].args[0]
        self.assertEqual(first_url, "http://localhost:8000/createTask")
        self.assertEqual(second_url, "http://localhost:8000/getTaskResult")

    def test_create_captcha_solver_supports_ohmycaptcha_provider(self):
        definitions_module = types.ModuleType("infrastructure.provider_definitions_repository")
        settings_module = types.ModuleType("infrastructure.provider_settings_repository")

        class _DefinitionsRepository:
            def get_by_key(self, provider_type: str, provider_key: str):
                self.last_request = (provider_type, provider_key)
                return SimpleNamespace(driver_type="yescaptcha_api")

        class _SettingsRepository:
            def resolve_runtime_settings(self, provider_type: str, provider_key: str, overrides=None):
                self.last_request = (provider_type, provider_key, overrides)
                return {
                    "yescaptcha_key": "client-key",
                    "yescaptcha_api_url": "http://localhost:8000/",
                }

        definitions_module.ProviderDefinitionsRepository = _DefinitionsRepository
        settings_module.ProviderSettingsRepository = _SettingsRepository

        with patch.dict(
            sys.modules,
            {
                "infrastructure.provider_definitions_repository": definitions_module,
                "infrastructure.provider_settings_repository": settings_module,
            },
        ):
            solver = create_captcha_solver("ohmycaptcha")

        self.assertIsInstance(solver, YesCaptcha)
        self.assertEqual(solver.client_key, "client-key")
        self.assertEqual(solver.api, "http://localhost:8000")


if __name__ == "__main__":
    unittest.main()
