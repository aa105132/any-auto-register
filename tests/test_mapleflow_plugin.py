from __future__ import annotations

import inspect
import unittest

from platforms.mapleflow import browser_oauth
from platforms.mapleflow.plugin import MapleFlowPlatform


class MapleFlowOAuthTests(unittest.TestCase):
    def test_oauth_start_is_protocol_url(self):
        self.assertEqual(browser_oauth.GOOGLE_OAUTH_URL, "https://api.mapleflow.io/auth/google")
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("page.goto(GOOGLE_OAUTH_URL", source)

    def test_create_key_uses_http_protocol(self):
        self.assertTrue(hasattr(browser_oauth, "_create_api_key_http"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_get_me_http", source)

    def test_verify_uses_x_api_key(self):
        source = inspect.getsource(browser_oauth._verify_api_key_http)
        self.assertIn("X-API-Key", source)

    def test_plugin_maps_protocol_fields(self):
        result = MapleFlowPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "mk-demo",
            "api_verification": {"ok": True},
            "key_create_result": {"ok": True},
        })
        self.assertEqual(result.extra["api_verification"], {"ok": True})
        self.assertEqual(result.extra["auth_header"], "X-API-Key")


if __name__ == "__main__":
    unittest.main()
