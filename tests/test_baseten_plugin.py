from __future__ import annotations

import inspect
import unittest

from core.base_platform import AccountStatus
from core.registration import LinkSpec, OtpSpec
from platforms.baseten import browser_oauth, protocol_mailbox
from platforms.baseten.plugin import BasetenPlatform


class BasetenPluginTests(unittest.TestCase):
    def test_platform_declares_mailbox_and_google_oauth(self):
        platform = BasetenPlatform()
        self.assertEqual(platform.name, "baseten")
        self.assertIn("protocol", platform.supported_executors)
        self.assertIn("headed", platform.supported_executors)
        self.assertEqual(platform.supported_identity_modes, ["mailbox", "oauth_browser"])
        self.assertIn("google", platform.supported_oauth_providers)

    def test_mailbox_adapter_exists_and_waits_for_workos_mail(self):
        adapter = BasetenPlatform().build_protocol_mailbox_adapter()
        self.assertIsNotNone(adapter)
        self.assertTrue(isinstance(adapter.otp_spec, OtpSpec) or isinstance(adapter.link_spec, LinkSpec))
        source = inspect.getsource(protocol_mailbox.BasetenProtocolMailboxWorker.run)
        self.assertIn("workspace", source.lower())
        self.assertIn("api_key", source)

    def test_browser_oauth_uses_workos_google_and_http_key_creation(self):
        self.assertEqual(browser_oauth.AUTH_SIGNUP_URL, "https://login.baseten.co/sign-up")
        self.assertEqual(browser_oauth.DASHBOARD_URL, "https://app.baseten.co/")
        self.assertTrue(hasattr(browser_oauth, "_create_api_key_http"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("GoogleOAuth", source)
        self.assertIn("_create_api_key_http", source)

    def test_result_mapping_saves_llm_credentials_and_workspace(self):
        result = BasetenPlatform()._map_result({
            "email": "demo@example.com",
            "password": "secret",
            "user_id": "user_123",
            "api_key": "b10-demo",
            "api_key_info": {"id": "key_123"},
            "workspace_id": "workspace_123",
            "workspace_info": {"name": "Auto Register"},
            "credits": 6,
            "api_verification": {"ok": True},
            "key_create_result": {"ok": True},
            "cookies": {"session": "abc"},
        })
        self.assertEqual(result.email, "demo@example.com")
        self.assertEqual(result.password, "secret")
        self.assertEqual(result.user_id, "user_123")
        self.assertEqual(result.token, "b10-demo")
        self.assertEqual(result.status, AccountStatus.REGISTERED)
        self.assertEqual(result.extra["api_key"], "b10-demo")
        self.assertEqual(result.extra["ai_api_token"], "b10-demo")
        self.assertEqual(result.extra["workspace_id"], "workspace_123")
        self.assertEqual(result.extra["credits"], 6)
        self.assertEqual(result.extra["api_base"], "https://app.baseten.co")
        self.assertEqual(result.extra["auth_provider"], "workos")


if __name__ == "__main__":
    unittest.main()
