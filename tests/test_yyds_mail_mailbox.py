import unittest
from unittest.mock import patch

from core.base_mailbox import MailboxAccount, YydsMailMailbox
from core.provider_drivers import BUILTIN_PROVIDER_DEFINITIONS, MAILBOX_DRIVER_TEMPLATES


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class YydsMailMailboxTests(unittest.TestCase):
    def test_provider_definitions_include_yyds_mail(self):
        mailbox_keys = {
            item["provider_key"]
            for item in BUILTIN_PROVIDER_DEFINITIONS
            if item.get("provider_type") == "mailbox"
        }
        driver_types = {
            item["driver_type"]
            for item in MAILBOX_DRIVER_TEMPLATES
            if item.get("provider_type") == "mailbox"
        }

        self.assertIn("yyds_mail", mailbox_keys)
        self.assertIn("yyds_mail_api", driver_types)

    @patch("requests.post")
    def test_get_email_uses_api_key_and_parses_address(self, post_mock):
        post_mock.return_value = _Response(
            {
                "address": "demo@215.test",
                "token": "mail-token",
            }
        )
        mailbox = YydsMailMailbox(
            api_base_url="https://maliapi.215.im",
            api_key="AC-test",
            prefix="demo",
            domain="215.test",
        )

        account = mailbox.get_email()

        self.assertEqual(account.email, "demo@215.test")
        self.assertEqual(account.account_id, "demo@215.test")
        self.assertEqual(
            account.extra["provider_account"]["credentials"]["mailbox_token"],
            "mail-token",
        )
        args, kwargs = post_mock.call_args
        self.assertTrue(args[0].endswith("/v1/accounts"))
        self.assertEqual(kwargs["headers"]["X-API-Key"], "AC-test")
        self.assertEqual(kwargs["json"]["prefix"], "demo")
        self.assertEqual(kwargs["json"]["domain"], "215.test")

    @patch("requests.get")
    def test_get_current_ids_reads_message_list(self, get_mock):
        get_mock.return_value = _Response(
            {
                "messages": [
                    {"id": "m1", "subject": "One"},
                    {"id": "m2", "subject": "Two"},
                ]
            }
        )
        mailbox = YydsMailMailbox(
            api_base_url="https://maliapi.215.im",
            api_key="AC-test",
        )
        account = MailboxAccount(email="demo@215.test", account_id="demo@215.test")

        ids = mailbox.get_current_ids(account)

        self.assertEqual(ids, {"m1", "m2"})
        args, kwargs = get_mock.call_args
        self.assertTrue(args[0].endswith("/v1/messages"))
        self.assertEqual(kwargs["params"]["address"], "demo@215.test")
        self.assertEqual(kwargs["headers"]["X-API-Key"], "AC-test")

    @patch("requests.get")
    def test_wait_for_code_reads_message_detail_and_extracts_code(self, get_mock):
        get_mock.side_effect = [
            _Response({"messages": [{"id": "m1", "subject": "Your code"}]}),
            _Response({"subject": "Your code", "text": "验证码：654321"}),
        ]
        mailbox = YydsMailMailbox(
            api_base_url="https://maliapi.215.im",
            api_key="AC-test",
        )
        account = MailboxAccount(email="demo@215.test", account_id="demo@215.test")

        code = mailbox.wait_for_code(account, keyword="code", timeout=1)

        self.assertEqual(code, "654321")
        self.assertTrue(get_mock.call_args_list[1].args[0].endswith("/v1/messages/m1"))

    @patch("requests.get")
    def test_wait_for_link_reads_message_detail_and_extracts_url(self, get_mock):
        get_mock.side_effect = [
            _Response({"messages": [{"id": "m1", "subject": "Verify your sign in"}]}),
            _Response(
                {
                    "subject": "Verify your sign in",
                    "html": "<a href='https://auth.example.com/verify?token=abc123'>Verify</a>",
                }
            ),
        ]
        mailbox = YydsMailMailbox(
            api_base_url="https://maliapi.215.im",
            api_key="AC-test",
        )
        account = MailboxAccount(email="demo@215.test", account_id="demo@215.test")

        link = mailbox.wait_for_link(account, keyword="verify", timeout=1)

        self.assertEqual(link, "https://auth.example.com/verify?token=abc123")


if __name__ == "__main__":
    unittest.main()
