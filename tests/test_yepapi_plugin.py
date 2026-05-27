from __future__ import annotations

import inspect
import unittest

from platforms.yepapi import browser_oauth
from platforms.yepapi.plugin import YepAPIPlatform


class YepAPITests(unittest.TestCase):
    def test_uses_better_auth_social_oauth(self):
        source = inspect.getsource(browser_oauth._start_google_oauth_protocol)
        self.assertIn("/api/auth/sign-in/social", source)
        self.assertIn("provider", source)
        self.assertIn("google", source)

    def test_verifies_with_x_api_key(self):
        source = inspect.getsource(browser_oauth._verify_api_key_http)
        self.assertIn("x-api-key", source)
        self.assertEqual(browser_oauth.VERIFY_PATH, "/v1/ai/models")

    def test_cloudflare_is_not_fake_success(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("Cloudflare", source)
        self.assertIn("未执行 OAuth", source)


    def test_oauth_url_open_is_nonblocking_new_tab(self):
        source = inspect.getsource(browser_oauth._open_oauth_url_nonblocking)
        self.assertIn("context.new_page", source)
        self.assertIn("location.assign", source)
        self.assertIn('wait_until="commit"', source)
        self.assertNotIn('wait_until="load"', source)
        self.assertNotIn('wait_until="networkidle"', source)
        self.assertNotIn('wait_until="domcontentloaded"', source)

    def test_register_requires_google_page_after_oauth_start(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("未打开 Google 授权页", source)
        self.assertIn("_wait_google_page", source)

    def test_browser_only_click_uses_deep_dom_provider_detection(self):
        source = inspect.getsource(browser_oauth._click_google_login_browser_only)
        self.assertIn("_click_yepapi_google_entry", source)
        helper = inspect.getsource(browser_oauth._click_yepapi_google_entry)
        self.assertIn("querySelectorAll", helper)
        self.assertIn("alt", helper)
        self.assertIn("dataset", helper)
        self.assertIn("page.mouse.click", helper)

    def test_missing_login_entry_logs_page_diagnostics(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_dump_yepapi_login_diagnostics", source)
        helper = inspect.getsource(browser_oauth._dump_yepapi_login_diagnostics)
        self.assertIn("buttons", helper)
        self.assertIn("output", helper)

    def test_plugin_maps_api_key(self):
        result = YepAPIPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "yep_sk_demo",
            "api_verification": {"ok": True},
        })
        self.assertEqual(result.token, "yep_sk_demo")
        self.assertEqual(result.extra["auth_header"], "x-api-key")


if __name__ == "__main__":
    unittest.main()
