import unittest
from unittest.mock import patch

from core.base_mailbox import GptMailMailbox, MailboxAccount
from core.provider_drivers import BUILTIN_PROVIDER_DEFINITIONS, MAILBOX_DRIVER_TEMPLATES


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class GptMailMailboxTests(unittest.TestCase):
    def test_provider_definitions_include_gptmail(self):
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

        self.assertIn("gptmail", mailbox_keys)
        self.assertIn("gptmail_api", driver_types)

    @patch("requests.get")
    def test_get_email_uses_api_key_and_parses_email(self, get_mock):
        get_mock.return_value = _Response(
            {
                "email": "demo@gptmail.test",
            }
        )
        mailbox = GptMailMailbox(
            api_base_url="https://mail.chatgpt.org.uk",
            api_key="gpt-key",
            prefix="demo",
            domain="gptmail.test",
        )

        account = mailbox.get_email()

        self.assertEqual(account.email, "demo@gptmail.test")
        self.assertEqual(account.account_id, "demo@gptmail.test")
        args, kwargs = get_mock.call_args
        self.assertTrue(args[0].endswith("/api/generate-email"))
        self.assertEqual(kwargs["headers"]["X-API-Key"], "gpt-key")
        self.assertEqual(kwargs["params"]["prefix"], "demo")
        self.assertEqual(kwargs["params"]["domain"], "gptmail.test")

    @patch("requests.get")
    def test_get_current_ids_reads_email_list(self, get_mock):
        get_mock.return_value = _Response(
            {
                "emails": [
                    {"id": "e1", "subject": "One"},
                    {"id": "e2", "subject": "Two"},
                ]
            }
        )
        mailbox = GptMailMailbox(
            api_base_url="https://mail.chatgpt.org.uk",
            api_key="gpt-key",
        )
        account = MailboxAccount(email="demo@gptmail.test", account_id="demo@gptmail.test")

        ids = mailbox.get_current_ids(account)

        self.assertEqual(ids, {"e1", "e2"})
        args, kwargs = get_mock.call_args
        self.assertTrue(args[0].endswith("/api/emails"))
        self.assertEqual(kwargs["params"]["email"], "demo@gptmail.test")
        self.assertEqual(kwargs["headers"]["X-API-Key"], "gpt-key")

    @patch("requests.get")
    def test_wait_for_code_reads_email_detail_and_extracts_code(self, get_mock):
        get_mock.side_effect = [
            _Response({"emails": [{"id": "e1", "subject": "Your code"}]}),
            _Response({"subject": "Your code", "text": "Your verification code is 123456"}),
        ]
        mailbox = GptMailMailbox(
            api_base_url="https://mail.chatgpt.org.uk",
            api_key="gpt-key",
        )
        account = MailboxAccount(email="demo@gptmail.test", account_id="demo@gptmail.test")

        code = mailbox.wait_for_code(account, keyword="code", timeout=1)

        self.assertEqual(code, "123456")
        self.assertTrue(get_mock.call_args_list[1].args[0].endswith("/api/email/e1"))

    @patch("requests.get")
    def test_wait_for_link_reads_email_detail_and_extracts_url(self, get_mock):
        get_mock.side_effect = [
            _Response({"emails": [{"id": "e1", "subject": "Magic sign in link"}]}),
            _Response(
                {
                    "subject": "Magic sign in link",
                    "html": "<a href='https://auth.example.com/magic?token=xyz'>Continue</a>",
                }
            ),
        ]
        mailbox = GptMailMailbox(
            api_base_url="https://mail.chatgpt.org.uk",
            api_key="gpt-key",
        )
        account = MailboxAccount(email="demo@gptmail.test", account_id="demo@gptmail.test")

        link = mailbox.wait_for_link(account, keyword="magic", timeout=1)

        self.assertEqual(link, "https://auth.example.com/magic?token=xyz")


if __name__ == "__main__":
    unittest.main()
