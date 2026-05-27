from __future__ import annotations

import inspect
import unittest

from core.base_identity import normalize_oauth_provider
from platforms.komilion import browser_oauth
from platforms.komilion.plugin import KomilionPlatform


class KomilionOAuthTests(unittest.TestCase):
    def test_declares_oauth_providers_and_aliases_outlook(self):
        platform = KomilionPlatform()
        self.assertIn("oauth_browser", platform.supported_identity_modes)
        self.assertIn("google", platform.supported_oauth_providers)
        self.assertIn("microsoft", platform.supported_oauth_providers)
        self.assertIn("outlook", platform.supported_oauth_providers)
        self.assertEqual(normalize_oauth_provider("outlook"), "microsoft")

    def test_nextauth_google_oauth_is_protocol_started(self):
        self.assertEqual(browser_oauth.AUTH_BASE, "https://www.komilion.com/api/auth")
        self.assertEqual(browser_oauth.KEYS_PATH, "/api/user/api-keys")
        source = inspect.getsource(browser_oauth._start_nextauth_oauth_protocol)
        self.assertIn("/api/auth/csrf", source)
        self.assertIn("/api/auth/signin/", source)
        self.assertIn("application/x-www-form-urlencoded", source)
        self.assertIn("redirect", source)

    def test_api_key_is_created_and_verified_by_http(self):
        self.assertTrue(hasattr(browser_oauth, "_create_api_key_http"))
        self.assertTrue(hasattr(browser_oauth, "_verify_api_key_http"))
        create_source = inspect.getsource(browser_oauth._create_api_key_http)
        verify_source = inspect.getsource(browser_oauth._verify_api_key_http)
        self.assertIn('"action": "generate"', create_source)
        self.assertIn('"action": "regenerate"', create_source)
        self.assertIn("/api/v1/models", verify_source)
        self.assertIn("Authorization", verify_source)
        self.assertIn("Bearer", verify_source)

    def test_register_uses_oauth_then_protocol_key_flow(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_start_nextauth_oauth_protocol", source)
        self.assertIn("drive_google_oauth", source)
        self.assertIn("_get_session_http", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_verify_api_key_http", source)
        self.assertIn("Microsoft/Outlook", source)


    def test_declares_outlook_mailbox_protocol_flow(self):
        platform = KomilionPlatform()
        self.assertIn("mailbox", platform.supported_identity_modes)
        self.assertEqual(platform.default_mail_provider, "outlook_token")
        adapter = platform.build_protocol_mailbox_adapter()
        self.assertIsNotNone(adapter)
        self.assertIsNotNone(adapter.link_spec)
        self.assertIn("Komilion", adapter.link_spec.keyword)

    def test_mailbox_protocol_uses_signup_verify_credentials_and_key_http(self):
        self.assertTrue(hasattr(browser_oauth, "register_with_email_verification"))
        self.assertTrue(hasattr(browser_oauth, "_signup_email_http"))
        self.assertTrue(hasattr(browser_oauth, "_visit_verification_link_http"))
        self.assertTrue(hasattr(browser_oauth, "_credentials_login_http"))
        source = inspect.getsource(browser_oauth.register_with_email_verification)
        self.assertIn("_signup_email_http", source)
        self.assertIn("verification_link_callback", source)
        self.assertIn("_visit_verification_link_http", source)
        self.assertIn("_credentials_login_http", source)
        self.assertIn("_create_api_key_http", source)

    def test_protocol_mailbox_register_passes_outlook_link_callback(self):
        from core.base_mailbox import MailboxAccount
        from core.base_platform import RegisterConfig
        from unittest.mock import patch

        captured = {}

        class DummyMailbox:
            def get_email(self):
                return MailboxAccount(email="outlook@example.com", extra={})

            def get_current_ids(self, _account):
                return set()

            def wait_for_link(self, account, **kwargs):
                captured["mailbox_account"] = account
                captured["link_kwargs"] = kwargs
                return "https://www.komilion.com/api/auth/verify-email?token=tok"

        def fake_register_with_email_verification(**kwargs):
            captured.update(kwargs)
            link = kwargs["verification_link_callback"]()
            return {
                "email": kwargs["email"],
                "password": kwargs["password"],
                "api_key": "ck_mailbox",
                "verification_link": link,
                "key_create_result": {"ok": True},
            }

        platform = KomilionPlatform(
            config=RegisterConfig(extra={"identity_provider": "mailbox"}),
            mailbox=DummyMailbox(),
        )
        with patch("platforms.komilion.browser_oauth.register_with_email_verification", fake_register_with_email_verification):
            account = platform.register(password="Passw0rd-demo")

        self.assertEqual(account.email, "outlook@example.com")
        self.assertEqual(account.token, "ck_mailbox")
        self.assertEqual(captured["email"], "outlook@example.com")
        self.assertEqual(captured["password"], "Passw0rd-demo")
        self.assertIn("Komilion", captured["link_kwargs"]["keyword"])

    def test_plugin_maps_api_key_and_session_fields(self):
        result = KomilionPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "ck_demo",
            "api_verification": {"ok": True},
            "key_create_result": {"ok": True},
            "session": {"user": {"email": "demo@example.com"}},
            "cookies": {"__Secure-next-auth.session-token": "session"},
        })
        self.assertEqual(result.token, "ck_demo")
        self.assertEqual(result.extra["api_key"], "ck_demo")
        self.assertEqual(result.extra["ai_api_token"], "ck_demo")
        self.assertEqual(result.extra["auth_header"], "Authorization")
        self.assertEqual(result.extra["api_base"], "https://www.komilion.com/api/v1")
        self.assertEqual(result.extra["api_verification"], {"ok": True})


if __name__ == "__main__":
    unittest.main()
