import unittest

from core.account_graph import (
    PRIMARY_TOKEN_WRITE_KEYS,
    _platform_credentials_from_extra,
    _provider_accounts_from_extra,
    _provider_resources_from_extra,
)
from core.base_platform import RegisterConfig
from platforms.codebanana.plugin import CodeBananaPlatform


class CodeBananaPluginTests(unittest.TestCase):
    def test_map_codebanana_result_uses_session_token_as_primary_token(self):
        platform = CodeBananaPlatform(RegisterConfig(extra={"mail_provider": "cfworker"}), mailbox=None)
        raw = {
            "email": "demo@example.com",
            "password": "secret",
            "username": "bananauser",
            "session_token": "session-1",
            "jwtToken": "jwt-1",
            "cookies": {"__Secure-next-auth.session-token": "session-1"},
            "session_json": {"user": {"id": "user-1"}, "jwtToken": "jwt-1"},
            "csrf_token": "csrf-1",
        }

        result = platform._map_codebanana_result(raw, password="secret")

        self.assertEqual(result.token, "session-1")
        self.assertEqual(result.user_id, "user-1")
        self.assertEqual(result.extra["jwtToken"], "jwt-1")
        self.assertEqual(result.extra["csrf_token"], "csrf-1")
        self.assertEqual(result.extra["cbbot_key"], "user-1")

    def test_codebanana_adapter_requests_four_digit_otp(self):
        platform = CodeBananaPlatform(RegisterConfig(extra={"mail_provider": "cfworker"}), mailbox=None)

        adapter = platform.build_protocol_mailbox_adapter()

        self.assertEqual(adapter.otp_spec.keyword, "CodeBanana")
        self.assertEqual(adapter.otp_spec.code_pattern, r"(?<!\d)(\d{4})(?!\d)")
        self.assertIn("CodeBanana", adapter.otp_spec.wait_message)

    def test_account_graph_writes_jwt_and_csrf_credentials_for_codebanana(self):
        extra = {
            "platform": "codebanana",
            "session_token": "session-1",
            "jwtToken": "jwt-1",
            "csrf_token": "csrf-1",
            "cbbot_key": "user-1",
        }

        rows = _platform_credentials_from_extra(extra)

        keys = {row["key"] for row in rows}
        primary = next(row["key"] for row in rows if row["is_primary"])
        self.assertEqual(primary, PRIMARY_TOKEN_WRITE_KEYS["codebanana"])
        self.assertIn("jwtToken", keys)
        self.assertIn("csrf_token", keys)
        self.assertIn("cbbot_key", keys)

    def test_verification_mailbox_preserves_cfworker_metadata_for_provider_rows(self):
        extra = {
            "verification_mailbox": {
                "provider": "cfworker",
                "email": "demo@example.com",
                "account_id": "127960",
                "mailbox_jwt": "mailbox-jwt",
                "address_password": "addr-pass",
                "address_id": "127960",
                "api_url": "https://apimail.example.com",
                "auth_mode": "public_jwt",
            }
        }

        provider_accounts = _provider_accounts_from_extra(extra)
        provider_resources = _provider_resources_from_extra(extra)

        self.assertEqual(len(provider_accounts), 1)
        self.assertEqual(len(provider_resources), 1)

        account_row = provider_accounts[0]
        resource_row = provider_resources[0]

        self.assertEqual(account_row["credentials"]["mailbox_jwt"], "mailbox-jwt")
        self.assertEqual(account_row["credentials"]["address_password"], "addr-pass")
        self.assertEqual(account_row["metadata"]["address_id"], "127960")
        self.assertEqual(account_row["metadata"]["api_url"], "https://apimail.example.com")
        self.assertEqual(account_row["metadata"]["auth_mode"], "public_jwt")

        self.assertEqual(resource_row["resource_identifier"], "127960")
        self.assertEqual(resource_row["metadata"]["address_id"], "127960")
        self.assertEqual(resource_row["metadata"]["api_url"], "https://apimail.example.com")
        self.assertEqual(resource_row["metadata"]["auth_mode"], "public_jwt")
        self.assertEqual(resource_row["metadata"]["email"], "demo@example.com")


if __name__ == "__main__":
    unittest.main()
