import unittest
from unittest.mock import patch

from core.base_mailbox import CFWorkerMailbox, MailboxAccount


class _Response:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text or str(payload)

    def json(self):
        return self._payload


class CFWorkerMailboxTests(unittest.TestCase):
    @patch("requests.post")
    def test_public_jwt_get_email_uses_public_endpoint_and_records_credentials(self, post_mock):
        post_mock.return_value = _Response(
            {
                "address": "demo@example.com",
                "jwt": "mailbox-jwt",
                "password": "addr-pass",
                "address_id": "addr-1",
            }
        )
        mailbox = CFWorkerMailbox(
            api_url="https://apimail.example.com",
            auth_mode="public_jwt",
            domain="example.com",
            fingerprint="fp-123",
        )

        account = mailbox.get_email()

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.account_id, "addr-1")
        self.assertEqual(account.extra["provider_account"]["credentials"]["mailbox_jwt"], "mailbox-jwt")
        self.assertEqual(account.extra["provider_account"]["credentials"]["address_password"], "addr-pass")
        self.assertEqual(account.extra["provider_resource"]["metadata"]["auth_mode"], "public_jwt")
        self.assertNotIn("token", account.extra["provider_resource"]["metadata"])
        self.assertNotIn("address_password", account.extra["provider_resource"]["metadata"])

        args, kwargs = post_mock.call_args
        self.assertTrue(args[0].endswith("/api/new_address"))
        self.assertEqual(kwargs["json"]["cf_token"], "fp-123")
        self.assertNotIn("x-admin-auth", kwargs["headers"])

    @patch("requests.get")
    def test_public_jwt_get_current_ids_uses_bearer_token(self, get_mock):
        get_mock.return_value = _Response({"results": [{"id": "1"}, {"id": "2"}]})
        mailbox = CFWorkerMailbox(api_url="https://apimail.example.com", auth_mode="public_jwt")
        account = MailboxAccount(
            email="demo@example.com",
            account_id="addr-1",
            extra={"provider_account": {"credentials": {"mailbox_jwt": "mailbox-jwt"}}},
        )

        ids = mailbox.get_current_ids(account)

        self.assertEqual(ids, {"1", "2"})
        args, kwargs = get_mock.call_args
        self.assertTrue(args[0].endswith("/api/mails"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer mailbox-jwt")

    @patch("requests.get")
    def test_public_jwt_wait_for_code_fetches_mail_detail_and_matches_four_digit_code(self, get_mock):
        get_mock.side_effect = [
            _Response({"results": [{"id": "10", "subject": "Welcome to CodeBanana"}]}),
            _Response(
                {
                    "raw": (
                        "Subject: Welcome to CodeBanana - Email Verification Code\\r\\n\\r\\n"
                        "Your Verification Code is 9687"
                    )
                }
            ),
        ]
        mailbox = CFWorkerMailbox(api_url="https://apimail.example.com", auth_mode="public_jwt")
        account = MailboxAccount(
            email="demo@example.com",
            account_id="addr-1",
            extra={"provider_account": {"credentials": {"mailbox_jwt": "mailbox-jwt"}}},
        )

        code = mailbox.wait_for_code(
            account,
            keyword="CodeBanana",
            timeout=1,
            code_pattern=r"(?<!\d)(\d{4})(?!\d)",
        )

        self.assertEqual(code, "9687")
        self.assertTrue(get_mock.call_args_list[1].args[0].endswith("/api/mail/10"))

    @patch("requests.get")
    def test_public_jwt_wait_for_code_prefers_real_code_over_css_color_digits_in_raw_mime(self, get_mock):
        raw_mime = (
            "Subject: Welcome to CodeBanana - Email Verification Code\r\n"
            "Content-Type: text/html; charset=UTF-8\r\n"
            "\r\n"
            "<html><head><style>.btn{color:#2563eb}</style></head>"
            "<body><div>Verification Code: <strong>7672</strong></div></body></html>"
        )
        get_mock.side_effect = [
            _Response({"results": [{"id": "11", "subject": "CodeBanana"}]}),
            _Response({"raw": raw_mime}),
        ]
        mailbox = CFWorkerMailbox(api_url="https://apimail.example.com", auth_mode="public_jwt")
        account = MailboxAccount(
            email="demo@example.com",
            account_id="addr-1",
            extra={"provider_account": {"credentials": {"mailbox_jwt": "mailbox-jwt"}}},
        )

        code = mailbox.wait_for_code(
            account,
            keyword="CodeBanana",
            timeout=1,
            code_pattern=r"(?<!\d)(\d{4})(?!\d)",
        )

        self.assertEqual(code, "7672")


    @patch("requests.post")
    def test_admin_token_mode_still_uses_admin_endpoint(self, post_mock):
        post_mock.return_value = _Response({"email": "demo@example.com", "token": "admin-token"})
        mailbox = CFWorkerMailbox(
            api_url="https://apimail.example.com",
            auth_mode="admin_token",
            admin_token="secret-admin",
        )

        account = mailbox.get_email()

        self.assertEqual(account.email, "demo@example.com")
        self.assertNotIn("token", account.extra["provider_resource"]["metadata"])
        self.assertNotIn("address_password", account.extra["provider_resource"]["metadata"])
        args, kwargs = post_mock.call_args
        self.assertTrue(args[0].endswith("/admin/new_address"))
        self.assertEqual(kwargs["headers"]["x-admin-auth"], "secret-admin")

    @patch("requests.post")
    def test_admin_token_mode_keeps_token_semantics_when_address_id_present(self, post_mock):
        post_mock.return_value = _Response(
            {
                "email": "demo@example.com",
                "token": "admin-token",
                "address_id": "addr-1",
                "id": "addr-1",
            }
        )
        mailbox = CFWorkerMailbox(
            api_url="https://apimail.example.com",
            auth_mode="admin_token",
            admin_token="secret-admin",
        )

        account = mailbox.get_email()

        self.assertEqual(account.account_id, "admin-token")
        self.assertEqual(
            account.extra["provider_resource"]["resource_identifier"],
            "admin-token",
        )

    @patch("requests.get")
    def test_public_jwt_get_current_ids_prefers_mailbox_jwt_sources_when_extra_missing(self, get_mock):
        get_mock.return_value = _Response({"results": [{"id": "1"}]})
        mailbox = CFWorkerMailbox(api_url="https://apimail.example.com", auth_mode="public_jwt")
        mailbox._token = "runtime-mailbox-jwt"
        account = MailboxAccount(
            email="demo@example.com",
            account_id="addr-legacy",
            extra=None,
        )

        ids = mailbox.get_current_ids(account)

        self.assertEqual(ids, {"1"})
        args, kwargs = get_mock.call_args
        self.assertTrue(args[0].endswith("/api/mails"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer runtime-mailbox-jwt")

    @patch("requests.get")
    def test_public_jwt_get_current_ids_does_not_use_account_id_as_bearer_fallback(self, get_mock):
        get_mock.return_value = _Response({"results": [{"id": "1"}]})
        mailbox = CFWorkerMailbox(api_url="https://apimail.example.com", auth_mode="public_jwt")
        account = MailboxAccount(
            email="demo@example.com",
            account_id="addr-legacy",
            extra=None,
        )

        ids = mailbox.get_current_ids(account)

        self.assertEqual(ids, {"1"})
        args, kwargs = get_mock.call_args
        self.assertTrue(args[0].endswith("/api/mails"))
        self.assertNotIn("Authorization", kwargs["headers"])

    @patch("requests.get")
    def test_public_jwt_wait_for_link_can_use_subject_and_html_detail_content(self, get_mock):
        get_mock.side_effect = [
            _Response({"results": [{"id": "10"}]}),
            _Response(
                {
                    "subject": "CodeBanana login link",
                    "html": "<a href='https://auth.example.com/verify?token=abc123'>Verify</a>",
                }
            ),
        ]
        mailbox = CFWorkerMailbox(api_url="https://apimail.example.com", auth_mode="public_jwt")
        account = MailboxAccount(
            email="demo@example.com",
            account_id="addr-1",
            extra={"provider_account": {"credentials": {"mailbox_jwt": "mailbox-jwt"}}},
        )

        link = mailbox.wait_for_link(
            account,
            keyword="CodeBanana",
            timeout=1,
        )

        self.assertEqual(link, "https://auth.example.com/verify?token=abc123")


if __name__ == "__main__":
    unittest.main()
