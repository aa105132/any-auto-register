from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.base_platform import RegisterConfig


class CometAPITests(unittest.TestCase):
    def test_platform_declares_google_oauth_and_mailbox_protocol(self):
        from platforms.cometapi.plugin import CometAPIPlatform

        platform = CometAPIPlatform()
        self.assertEqual(platform.name, "cometapi")
        self.assertIn("protocol", platform.supported_executors)
        self.assertIn("mailbox", platform.supported_identity_modes)
        self.assertIn("oauth_browser", platform.supported_identity_modes)
        self.assertIn("google", platform.supported_oauth_providers)
        self.assertEqual(platform.default_mail_provider, "outlook_token")

    def test_browser_module_contains_protocol_first_cometapi_endpoints(self):
        from platforms.cometapi import browser_oauth

        self.assertEqual(browser_oauth.SITE_URL, "https://www.cometapi.com")
        self.assertEqual(browser_oauth.CONSOLE_URL, "https://www.cometapi.com/console")
        self.assertEqual(browser_oauth.API_BASE, "https://api.cometapi.com/v1")
        self.assertEqual(browser_oauth.TOKEN_PATH, "/api/token/")
        self.assertTrue(hasattr(browser_oauth, "register_with_email_otp"))
        self.assertTrue(hasattr(browser_oauth, "register_with_browser_oauth"))
        self.assertTrue(hasattr(browser_oauth, "_create_api_key_http"))
        self.assertTrue(hasattr(browser_oauth, "_claim_newbie_rewards_http"))

    def test_email_otp_flow_uses_check_send_login_key_and_rewards(self):
        from platforms.cometapi import browser_oauth

        source = inspect.getsource(browser_oauth.register_with_email_otp)
        self.assertIn("/api/user/check?key=", source)
        self.assertIn("/api/verification?email=", source)
        self.assertIn("/api/user/login?turnstile=", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_claim_newbie_rewards_http", source)

    def test_google_oauth_flow_uses_state_precheck_and_cdp_capable_browser(self):
        from platforms.cometapi import browser_oauth

        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("/api/oauth/state", source)
        self.assertIn("/api/oauth/pre-check", source)
        self.assertIn("accounts.google.com/o/oauth2/v2/auth", source)
        self.assertIn("OAuthBrowser", source)
        self.assertIn("drive_google_oauth", source)
        self.assertIn("chrome_cdp_url", source)
        self.assertIn("_create_api_key_http", source)

    def test_api_key_http_create_and_verify_shapes(self):
        from platforms.cometapi import browser_oauth

        create_source = inspect.getsource(browser_oauth._create_api_key_http)
        verify_source = inspect.getsource(browser_oauth._verify_api_key_http)
        self.assertIn("post", create_source)
        self.assertIn("/api/token/", create_source)
        self.assertIn("unlimited_quota", create_source)
        self.assertIn("sk-", create_source)
        self.assertIn("https://api.cometapi.com/v1/models", verify_source)
        self.assertIn("Authorization", verify_source)
        self.assertIn("Bearer", verify_source)

    def test_plugin_maps_api_key_rewards_balance_and_session_fields(self):
        from platforms.cometapi.plugin import CometAPIPlatform

        result = CometAPIPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "sk-demo",
            "api_key_info": {"name": "default"},
            "api_verification": {"ok": True},
            "key_create_result": {"ok": True},
            "user": {"id": 123, "email": "demo@example.com", "quota": 2000000},
            "newbie_rewards": {"tasks": {"first_create_token": True}, "claimed_bonus": 1},
            "cookies": {"session": "abc"},
        })
        self.assertEqual(result.email, "demo@example.com")
        self.assertEqual(result.user_id, "123")
        self.assertEqual(result.token, "sk-demo")
        self.assertEqual(result.extra["api_key"], "sk-demo")
        self.assertEqual(result.extra["ai_api_token"], "sk-demo")
        self.assertEqual(result.extra["api_base"], "https://api.cometapi.com/v1")
        self.assertEqual(result.extra["auth_header"], "Authorization")
        self.assertEqual(result.extra["newbie_rewards"]["claimed_bonus"], 1)
        self.assertEqual(result.extra["account_overview"]["balance_quota"], 2000000)

    def test_protocol_mailbox_register_passes_otp_callback(self):
        from core.base_mailbox import MailboxAccount
        from platforms.cometapi.plugin import CometAPIPlatform

        captured: dict = {}

        class DummyMailbox:
            def get_email(self):
                return MailboxAccount(email="outlook@example.com", extra={})

            def get_current_ids(self, _account):
                return set()

            def wait_for_code(self, account, **kwargs):
                captured["mailbox_account"] = account
                captured["otp_kwargs"] = kwargs
                return "123456"

        def fake_register_with_email_otp(**kwargs):
            captured.update(kwargs)
            code = kwargs["otp_callback"]()
            return {
                "email": kwargs["email"],
                "api_key": "sk-mailbox",
                "email_otp": code,
                "user": {"id": 456, "email": kwargs["email"]},
            }

        platform = CometAPIPlatform(
            config=RegisterConfig(extra={"identity_provider": "mailbox"}),
            mailbox=DummyMailbox(),
        )
        with patch("platforms.cometapi.browser_oauth.register_with_email_otp", fake_register_with_email_otp):
            account = platform.register()

        self.assertEqual(account.email, "outlook@example.com")
        self.assertEqual(account.token, "sk-mailbox")
        self.assertEqual(captured["email"], "outlook@example.com")
        self.assertEqual(captured["email_otp"], "123456")
        self.assertIn("CometAPI", captured["otp_kwargs"]["keyword"])

    def test_oauth_runner_passes_cdp_and_password_options(self):
        from platforms.cometapi.plugin import CometAPIPlatform

        captured: dict = {}

        def fake_register_with_browser_oauth(**kwargs):
            captured.update(kwargs)
            return {"email": kwargs["email_hint"], "api_key": "sk-oauth"}

        ctx = SimpleNamespace(
            proxy="http://proxy.local:8080",
            executor_type="headed",
            extra={"browser_oauth_timeout": 123, "google_password": "pw"},
            log=lambda _msg: None,
            identity=SimpleNamespace(
                oauth_provider="google",
                email="google@example.com",
                chrome_user_data_dir="C:/ChromeProfile",
                chrome_cdp_url="http://127.0.0.1:9222",
                mailbox_account=None,
            ),
        )
        with patch("platforms.cometapi.browser_oauth.register_with_browser_oauth", fake_register_with_browser_oauth):
            result = CometAPIPlatform()._run_oauth(ctx)

        self.assertEqual(result["api_key"], "sk-oauth")
        self.assertEqual(captured["oauth_provider"], "google")
        self.assertEqual(captured["email_hint"], "google@example.com")
        self.assertEqual(captured["google_password"], "pw")
        self.assertEqual(captured["chrome_user_data_dir"], "C:/ChromeProfile")
        self.assertEqual(captured["chrome_cdp_url"], "http://127.0.0.1:9222")


if __name__ == "__main__":
    unittest.main()
