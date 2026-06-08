import unittest

from application.mailbox_inventory_support import (
    build_mailbox_inventory_seed,
    build_outlook_alias_inventory_entry,
    export_mailbox_inventory_lines,
    inventory_platform_already_used,
    parse_mailbox_inventory_import_lines,
    resolve_inventory_registration_success,
    resolve_inventory_timeout_result,
)


class MailboxInventorySupportTests(unittest.TestCase):
    def test_outlook_driver_template_exposes_alias_limit_field(self):
        from core.provider_drivers import get_driver_template

        template = get_driver_template("mailbox", "outlook_token_imap")
        fields = {field["key"]: field for field in template["fields"]}

        self.assertIn("outlook_alias_max_count", fields)
        self.assertEqual(fields["outlook_alias_max_count"].get("type"), "number")
        self.assertEqual(fields["outlook_alias_max_count"].get("category"), "config")


    def test_parse_outlook_inventory_import_lines_accepts_ms_mail_fetcher_format(self):
        items = parse_mailbox_inventory_import_lines(
            "outlook_token",
            ["demo@outlook.com----mail-pass----client-123----refresh-456"],
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["email"], "demo@outlook.com")
        self.assertEqual(items[0]["token"], "refresh-456")
        self.assertEqual(items[0]["metadata"]["password"], "mail-pass")
        self.assertEqual(items[0]["metadata"]["client_id"], "client-123")

    def test_build_outlook_inventory_seed_includes_runtime_credentials(self):
        seed = build_mailbox_inventory_seed(
            "outlook_token",
            {
                "email": "demo@outlook.com",
                "purchase_token": "refresh-456",
                "metadata": {
                    "password": "mail-pass",
                    "client_id": "client-123",
                },
            },
        )

        self.assertEqual(seed.email, "demo@outlook.com")
        self.assertEqual(seed.password, "mail-pass")
        self.assertEqual(seed.extra["mail_provider"], "outlook_token")
        self.assertEqual(seed.extra["outlook_email"], "demo@outlook.com")
        self.assertEqual(seed.extra["outlook_password"], "mail-pass")
        self.assertEqual(seed.extra["outlook_client_id"], "client-123")
        self.assertEqual(seed.extra["outlook_refresh_token"], "refresh-456")
        self.assertEqual(
            seed.extra["provider_accounts"][0]["credentials"]["refresh_token"],
            "refresh-456",
        )

    def test_resolve_outlook_inventory_registration_success_recycles_mailbox(self):
        result = resolve_inventory_registration_success(
            "outlook_token",
            {},
            registered_email="demo@outlook.com",
            platform="chatgpt",
        )

        self.assertEqual(result["status"], "unused")
        self.assertEqual(result["metadata"]["successful_registrations"], 1)
        self.assertEqual(result["metadata"]["last_registered_email"], "demo@outlook.com")
        self.assertEqual(result["metadata"]["used_platforms"], ["chatgpt"])
        self.assertIn("回收到邮箱池", result["note"])

    def test_resolve_outlook_inventory_timeout_recycles_mailbox_instead_of_blacklist(self):
        result = resolve_inventory_timeout_result(
            "outlook_token",
            {},
            registered_email="demo@outlook.com",
            platform="atxp",
        )

        self.assertEqual(result["status"], "unused")
        self.assertEqual(result["metadata"]["remote_email"], "demo@outlook.com")
        self.assertNotIn("blacklist_reason", result["metadata"])
        self.assertNotIn("used_platforms", result["metadata"])
        self.assertIn("已回收到邮箱池", result["note"])

    def test_resolve_outlook_inventory_register_failure_recycles_mailbox(self):
        from application.mailbox_inventory_support import resolve_inventory_register_failure

        result = resolve_inventory_register_failure(
            "outlook_token",
            {"used_platforms": ["chatgpt"], "blacklist_reason": "old"},
            registered_email="demo@outlook.com",
            platform="swarms",
            error="ProxyError: Tunnel connection failed: 504 Gateway Timeout",
        )

        self.assertEqual(result["status"], "unused")
        self.assertEqual(result["metadata"]["remote_email"], "demo@outlook.com")
        self.assertEqual(result["metadata"]["last_failed_platform"], "swarms")
        self.assertIn("last_register_error", result["metadata"])
        self.assertNotIn("blacklist_reason", result["metadata"])
        self.assertIn("注册失败", result["note"])
        self.assertIn("回收到邮箱池", result["note"])

    def test_resolve_luckmail_timeout_blacklists_only_for_chatgpt(self):
        blacklisted = resolve_inventory_timeout_result(
            "luckmail",
            {},
            registered_email="demo@hotmail.com",
            platform="chatgpt",
        )
        reusable = resolve_inventory_timeout_result(
            "luckmail",
            {},
            registered_email="demo@hotmail.com",
            platform="atxp",
        )

        self.assertEqual(blacklisted["status"], "blacklisted")
        self.assertEqual(blacklisted["metadata"]["blacklist_reason"], "verification_code_timeout")
        self.assertEqual(reusable["status"], "unused")
        self.assertNotIn("blacklist_reason", reusable["metadata"])
        self.assertIn("已回收到邮箱池", reusable["note"])

    def test_inventory_platform_already_used_only_blocks_same_platform_for_outlook(self):
        self.assertTrue(
            inventory_platform_already_used(
                "outlook_token",
                {"used_platforms": ["chatgpt", "cursor"]},
                "chatgpt",
            )
        )
        self.assertFalse(
            inventory_platform_already_used(
                "outlook_token",
                {"used_platforms": ["chatgpt", "cursor"]},
                "grok",
            )
        )
        self.assertFalse(
            inventory_platform_already_used(
                "outlook_token",
                {},
                "chatgpt",
            )
        )

    def test_export_outlook_inventory_lines_uses_email_password_client_id_refresh_token_order(self):
        content = export_mailbox_inventory_lines(
            "outlook_token",
            [
                {
                    "email": "demo@outlook.com",
                    "purchase_token": "refresh-456",
                    "metadata": {
                        "password": "mail-pass",
                        "client_id": "client-123",
                    },
                }
            ],
        )

        self.assertEqual(
            content,
            "demo@outlook.com----mail-pass----client-123----refresh-456",
        )

    def test_export_luckmail_inventory_lines_uses_email_and_token(self):
        content = export_mailbox_inventory_lines(
            "luckmail",
            [
                {
                    "email": "demo@hotmail.com",
                    "purchase_token": "tok_123",
                    "metadata": {},
                }
            ],
        )

        self.assertEqual(content, "demo@hotmail.com----tok_123")


    def test_build_outlook_alias_inventory_seed_uses_parent_login_credentials(self):
        seed = build_mailbox_inventory_seed(
            "outlook_token",
            {
                "email": "demo.alias01@outlook.com",
                "purchase_token": "refresh-456",
                "metadata": {
                    "password": "mail-pass",
                    "client_id": "client-123",
                    "alias_parent_email": "demo@outlook.com",
                    "outlook_login_email": "demo@outlook.com",
                    "source": "outlook_alias_auto",
                },
            },
        )

        self.assertEqual(seed.email, "demo.alias01@outlook.com")
        self.assertEqual(seed.extra["outlook_email"], "demo@outlook.com")
        self.assertEqual(seed.extra["outlook_registration_email"], "demo.alias01@outlook.com")
        self.assertEqual(seed.extra["outlook_alias_parent_email"], "demo@outlook.com")
        self.assertEqual(seed.extra["provider_accounts"][0]["login_identifier"], "demo@outlook.com")
        self.assertEqual(seed.extra["provider_resources"][0]["handle"], "demo.alias01@outlook.com")


    def test_build_outlook_alias_inventory_entry_inherits_parent_credentials_and_marks_platform(self):
        entry = build_outlook_alias_inventory_entry(
            {
                "id": 42,
                "email": "demo@outlook.com",
                "purchase_token": "refresh-456",
                "metadata": {
                    "password": "mail-pass",
                    "client_id": "client-123",
                    "used_platforms": ["chatgpt"],
                },
            },
            alias_email="demo+fm01@outlook.com",
            platform="freemodel",
        )

        self.assertEqual(entry["provider_key"], "outlook_token")
        self.assertEqual(entry["email"], "demo+fm01@outlook.com")
        self.assertEqual(entry["purchase_token"], "refresh-456")
        self.assertEqual(entry["status"], "unused")
        self.assertEqual(entry["metadata"]["password"], "mail-pass")
        self.assertEqual(entry["metadata"]["client_id"], "client-123")
        self.assertEqual(entry["metadata"]["alias_parent_email"], "demo@outlook.com")
        self.assertEqual(entry["metadata"]["outlook_login_email"], "demo@outlook.com")
        self.assertEqual(entry["metadata"]["used_platforms"], ["freemodel"])
        self.assertEqual(entry["metadata"]["parent_used_platforms"], ["chatgpt"])


if __name__ == "__main__":
    unittest.main()
