import unittest
from unittest.mock import patch

from core.base_mailbox import OutlookTokenMailbox


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeImap:
    def __init__(self, *_args, **_kwargs):
        self.closed = False

    def authenticate(self, mechanism, callback):
        payload = callback(None).decode("utf-8")
        if "Bearer access-123" not in payload:
            raise AssertionError(payload)
        return "OK", [b""]

    def select(self, folder, readonly=True):
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b"1"]
        if command == "fetch":
            raw = (
                "Subject: CodeBanana verification\r\n"
                "From: Bot <noreply@example.com>\r\n"
                "To: demo@outlook.com\r\n"
                "Date: Thu, 17 Apr 2026 12:00:00 +0000\r\n"
                "Content-Type: text/plain; charset=UTF-8\r\n"
                "\r\n"
                "Your verification code is 246810."
            ).encode("utf-8")
            return "OK", [(b"1 (RFC822 {120})", raw)]
        raise AssertionError((command, args))

    def close(self):
        self.closed = True

    def logout(self):
        self.closed = True


class _FakeJunkImap(_FakeImap):
    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.selected_folder = ""

    def select(self, folder, readonly=True):
        self.selected_folder = str(folder or "").strip('"')
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            if self.selected_folder.upper() == "INBOX":
                return "OK", [b""]
            if self.selected_folder == "Junk":
                return "OK", [b"9"]
            return "OK", [b""]
        if command == "fetch":
            raw = (
                "Subject: Your FreeModel code: 135790\r\n"
                "From: FreeModel <noreply@freemodel.dev>\r\n"
                "To: demo@outlook.com\r\n"
                "Date: Fri, 22 May 2026 05:15:50 +0000\r\n"
                "Content-Type: text/plain; charset=UTF-8\r\n"
                "\r\n"
                "Your verification code is 135790."
            ).encode("utf-8")
            return "OK", [(b"9 (RFC822 {120})", raw)]
        raise AssertionError((command, args))

class OutlookTokenMailboxTests(unittest.TestCase):
    @patch("imaplib.IMAP4_SSL", return_value=_FakeImap())
    @patch("requests.post")
    def test_wait_for_code_refreshes_token_and_persists_rotated_refresh_token(self, post_mock, _imap_mock):
        post_mock.return_value = _Response(
            {
                "access_token": "access-123",
                "refresh_token": "refresh-new",
            }
        )
        persisted_tokens = []
        mailbox = OutlookTokenMailbox(
            email="demo@outlook.com",
            password="mail-pass",
            client_id="client-123",
            refresh_token="refresh-old",
            token_update_hook=persisted_tokens.append,
        )
        account = mailbox.get_email()

        code = mailbox.wait_for_code(account, keyword="CodeBanana", timeout=1)

        self.assertEqual(code, "246810")
        self.assertEqual(persisted_tokens, ["refresh-new"])
        self.assertEqual(
            account.extra["provider_account"]["credentials"]["refresh_token"],
            "refresh-new",
        )
        self.assertEqual(
            account.extra["provider_resource"]["metadata"]["client_id"],
            "client-123",
        )

    @patch("imaplib.IMAP4_SSL", return_value=_FakeJunkImap())
    @patch("requests.post")
    def test_get_current_ids_includes_junk_folder_ids(self, post_mock, _imap_mock):
        post_mock.return_value = _Response({"access_token": "access-123"})
        mailbox = OutlookTokenMailbox(
            email="demo@outlook.com",
            password="mail-pass",
            client_id="client-123",
            refresh_token="refresh-old",
        )
        account = mailbox.get_email()

        ids = mailbox.get_current_ids(account)

        self.assertIn("Junk:9", ids)

    @patch("imaplib.IMAP4_SSL", return_value=_FakeJunkImap())
    @patch("requests.post")
    def test_wait_for_code_scans_junk_folder_for_freemodel_mail(self, post_mock, _imap_mock):
        post_mock.return_value = _Response({"access_token": "access-123"})
        mailbox = OutlookTokenMailbox(
            email="demo@outlook.com",
            password="mail-pass",
            client_id="client-123",
            refresh_token="refresh-old",
        )
        account = mailbox.get_email()

        code = mailbox.wait_for_code(account, keyword="FreeModel", timeout=1)

        self.assertEqual(code, "135790")

    @patch("requests.post")
    def test_wait_for_code_matches_real_atxp_mail_subject_and_body(self, post_mock):
        post_mock.return_value = _Response(
            {
                "access_token": "access-123",
                "refresh_token": "refresh-new",
            }
        )
        mailbox = OutlookTokenMailbox(
            email="demo@outlook.com",
            password="mail-pass",
            client_id="client-123",
            refresh_token="refresh-old",
        )
        account = mailbox.get_email()

        with patch.object(
            mailbox,
            "_fetch_recent_messages",
            return_value=[
                {
                    "uid": "28",
                    "subject": "Your login code for ATXP",
                    "body_text": "ATXP\nYour code is\n758660\nThis code expires in 10 minutes.",
                    "body_html": "<html><body><p>ATXP</p><p>Your code is <strong>758660</strong></p></body></html>",
                }
            ],
        ):
            code = mailbox.wait_for_code(account, keyword="ATXP", timeout=1)

        self.assertEqual(code, "758660")


    def test_get_email_can_return_outlook_alias_registration_address(self):
        mailbox = OutlookTokenMailbox(
            email="demo@outlook.com",
            password="mail-pass",
            client_id="client-123",
            refresh_token="refresh-old",
            registration_email="demo.alias01@outlook.com",
            alias_parent_email="demo@outlook.com",
        )

        account = mailbox.get_email()

        self.assertEqual(account.email, "demo.alias01@outlook.com")
        self.assertEqual(account.account_id, "demo@outlook.com")
        self.assertEqual(account.extra["provider_account"]["login_identifier"], "demo@outlook.com")
        self.assertEqual(account.extra["provider_resource"]["handle"], "demo.alias01@outlook.com")
        self.assertEqual(account.extra["provider_resource"]["metadata"]["alias_parent_email"], "demo@outlook.com")
        self.assertEqual(account.extra["provider_resource"]["metadata"]["outlook_login_email"], "demo@outlook.com")

    def test_open_imap_uses_parent_outlook_login_email_for_alias(self):
        mailbox = OutlookTokenMailbox(
            email="demo@outlook.com",
            password="mail-pass",
            client_id="client-123",
            refresh_token="refresh-old",
            registration_email="demo.alias01@outlook.com",
            alias_parent_email="demo@outlook.com",
        )

        with patch("imaplib.IMAP4_SSL", return_value=_FakeImap()) as imap_mock:
            conn = mailbox._open_imap_connection("access-123")

        self.assertTrue(conn.closed is False)
        imap_mock.assert_called_once_with("outlook.live.com", 993)


if __name__ == "__main__":
    unittest.main()
