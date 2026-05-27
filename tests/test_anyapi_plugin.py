from __future__ import annotations

import inspect
import unittest

from platforms.anyapi import browser_oauth
from platforms.anyapi.plugin import AnyAPIPlatform


class AnyAPIOAuthTests(unittest.TestCase):
    def test_create_key_uses_http_protocol_after_oauth(self):
        self.assertTrue(hasattr(browser_oauth, "_create_api_key_http"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_create_api_key_http", source)
        self.assertNotIn("goto(KEYS_URL", source)

    def test_api_key_http_verification_exists(self):
        self.assertTrue(hasattr(browser_oauth, "_verify_api_key_http"))

    def test_plugin_maps_protocol_fields(self):
        result = AnyAPIPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "sk-demo",
            "api_verification": {"ok": True},
            "key_create_result": {"ok": True, "status": 200},
        })
        self.assertEqual(result.extra["api_verification"], {"ok": True})
        self.assertEqual(result.extra["key_create_result"], {"ok": True, "status": 200})


if __name__ == "__main__":
    unittest.main()
