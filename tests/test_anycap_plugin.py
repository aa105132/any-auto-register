from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class AnyCapRegistrationTests(unittest.TestCase):
    def test_frontend_discovered_routes_are_encoded_in_oauth_worker(self):
        from platforms.anycap import browser_oauth

        self.assertEqual(browser_oauth.API_BASE, "https://api.anycap.ai")
        self.assertEqual(browser_oauth.ACCESS_TOKEN_URL, "https://anycap.ai/auth/access-token")
        self.assertEqual(browser_oauth.API_KEYS_URL, "https://api.anycap.ai/v1/api-keys")
        self.assertIn("/api/auth/login", browser_oauth.LOGIN_URL)

    def test_plugin_maps_created_api_key_as_primary_token(self):
        from platforms.anycap.plugin import AnyCapPlatform

        result = AnyCapPlatform()._map_result({
            "email": "demo@anycap.test",
            "api_key": "ak_demo_anycap_key_123456",
            "access_token": "access-token",
            "api_key_info": {"id": "key_1"},
        })
        self.assertEqual(result.token, "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["api_key"], "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["ai_api_token"], "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["native_api_base"], "https://api.anycap.ai")

    def test_oauth_worker_uses_browser_token_then_protocol_key_create(self):
        from platforms.anycap import browser_oauth

        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_get_access_token_http", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_verify_api_key_http", source)
    def test_mailbox_flow_blacklists_auth0_signup_blocked_domain(self):
        import inspect
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        source = inspect.getsource(AnyCapMailboxRegistrar)
        self.assertIn("add_mailbox_domain_blacklist", source)
        self.assertIn('platform="anycap"', source)
        self.assertIn("too many signup attempts", source)
        self.assertIn("please try again later", source)
        self.assertIn("domain is not allowed", source)

    def test_signup_block_detector_normalizes_auth0_limit_text(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def evaluate(self, _script):
                return "Too many signup attempts.\nPlease try again later"

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        self.assertEqual(
            registrar._detect_signup_block_reason(page=FakePage()),
            "anycap_signup_attempts_limited",
        )



if __name__ == "__main__":
    unittest.main()
