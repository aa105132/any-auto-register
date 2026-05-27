from __future__ import annotations

import inspect
import unittest

from platforms.evolink import browser_oauth
from platforms.evolink.plugin import EvolinkPlatform


class EvolinkOAuthTests(unittest.TestCase):
    def test_google_start_does_not_click_terms_or_privacy(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_start_google_oauth", source)
        self.assertNotIn('["I agree", "Terms", "Privacy"]', source)

    def test_create_key_uses_http_protocol_after_oauth(self):
        self.assertTrue(hasattr(browser_oauth, "_create_key_http"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_create_key_http", source)
        self.assertNotIn("_create_key(dash_page", source)

    def test_plugin_maps_protocol_fields(self):
        result = EvolinkPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "sk-demo",
            "api_verification": {"ok": True},
            "key_create_result": {"ok": True},
        })
        self.assertEqual(result.extra["api_verification"], {"ok": True})
        self.assertEqual(result.extra["key_create_result"], {"ok": True})

    def test_google_popup_is_captured_and_stop_uses_auth_state(self):
        start_source = inspect.getsource(browser_oauth._start_google_oauth)
        self.assertIn("expect_popup", start_source)
        self.assertIn("Firebase signInWithPopup", start_source)
        register_source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("stop_when=_evolink_oauth_done", register_source)

    def test_evolink_error_message_is_not_mojibake(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("未拿到 routerapi_token", source)
        self.assertNotIn("ЮДФ", source)


if __name__ == "__main__":
    unittest.main()
