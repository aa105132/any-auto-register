import unittest
from unittest.mock import patch

from core.base_identity import BrowserOAuthIdentityProvider
from core.base_mailbox import HStockPlusGoogleAccountProvider, MailboxAccount
from core.provider_drivers import BUILTIN_PROVIDER_DEFINITIONS, MAILBOX_DRIVER_TEMPLATES


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _StaticMailbox:
    def __init__(self):
        self.account = MailboxAccount(
            email="google.user@gmail.com",
            account_id="23501",
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "hstockplus_google",
                    "credentials": {"password": "pw123"},
                    "metadata": {"order_id": "23501"},
                }
            },
        )

    def get_email(self):
        return self.account

    def get_current_ids(self, account):
        return set()


class HStockPlusGoogleAccountProviderTests(unittest.TestCase):
    def test_provider_definitions_include_hstockplus_google(self):
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

        self.assertIn("hstockplus_google", mailbox_keys)
        self.assertIn("hstockplus_google_account", driver_types)

    @patch("requests.post")
    def test_get_email_places_google_account_order_and_parses_delivered_account(self, post_mock):
        post_mock.side_effect = [
            _Response({"order": 23501}),
            _Response(
                {
                    "status": "Completed",
                    "accounts": ["user@gmail.com:mail-pass:recovery@example.com"],
                    "charge": "10.00",
                    "currency": "USD",
                }
            ),
        ]
        provider = HStockPlusGoogleAccountProvider(
            api_key="hsp-key",
            service_id="123",
            quantity=1,
            poll_interval=0,
            delivery_timeout=1,
        )

        account = provider.get_email()

        self.assertEqual(account.email, "user@gmail.com")
        self.assertEqual(account.account_id, "23501")
        self.assertEqual(account.extra["provider_account"]["credentials"]["password"], "mail-pass")
        self.assertEqual(account.extra["provider_account"]["metadata"]["recovery"], "recovery@example.com")
        add_call = post_mock.call_args_list[0]
        self.assertEqual(add_call.kwargs["data"]["action"], "add")
        self.assertEqual(add_call.kwargs["data"]["key"], "hsp-key")
        self.assertEqual(add_call.kwargs["data"]["service"], "123")
        status_call = post_mock.call_args_list[1]
        self.assertEqual(status_call.kwargs["data"]["action"], "status")
        self.assertEqual(status_call.kwargs["data"]["order"], "23501")

    @patch("requests.post")
    def test_get_email_supports_pipe_delimited_account_payload(self, post_mock):
        post_mock.side_effect = [
            _Response({"order": 99}),
            _Response({"status": "Completed", "accounts": ["google.user@gmail.com|pw123|backup@example.com"]}),
        ]
        provider = HStockPlusGoogleAccountProvider(
            api_key="hsp-key",
            service_id="123",
            poll_interval=0,
            delivery_timeout=1,
        )

        account = provider.get_email()

        self.assertEqual(account.email, "google.user@gmail.com")
        self.assertEqual(account.extra["provider_account"]["credentials"]["password"], "pw123")
        self.assertEqual(account.extra["provider_resource"]["metadata"]["raw_account"], "google.user@gmail.com|pw123|backup@example.com")


    @patch("core.google_account_pool.GoogleAccountPool.add_account")
    @patch("requests.post")
    def test_get_email_saves_all_delivered_accounts_to_google_pool(self, post_mock, add_account_mock):
        post_mock.side_effect = [
            _Response({"order": 90018}),
            _Response(
                {
                    "status": "Completed",
                    "accounts": [
                        "first@example.com----pw1",
                        "second@example.com----pw2",
                        "third@example.com----pw3",
                    ],
                }
            ),
        ]
        provider = HStockPlusGoogleAccountProvider(
            api_key="hsp-key",
            service_id="123",
            quantity=3,
            poll_interval=0,
            delivery_timeout=1,
        )

        account = provider.get_email()

        self.assertEqual(account.email, "first@example.com")
        self.assertEqual(
            [call.args[:2] for call in add_account_mock.call_args_list],
            [("first@example.com", "pw1"), ("second@example.com", "pw2"), ("third@example.com", "pw3")],
        )
        self.assertEqual(
            [call.kwargs.get("source_order_id") for call in add_account_mock.call_args_list],
            ["90018", "90018", "90018"],
        )


    @patch("core.google_account_pool.GoogleAccountPool.add_account")
    @patch("requests.post")
    def test_get_email_keeps_polling_when_order_status_temporarily_fails(self, post_mock, add_account_mock):
        post_mock.side_effect = [
            _Response({"order": 90023}),
            TimeoutError("temporary status timeout"),
            _Response({"status": "Completed", "accounts": ["later@example.com----pw"]}),
        ]
        provider = HStockPlusGoogleAccountProvider(
            api_key="hsp-key",
            service_id="123",
            poll_interval=0.01,
            delivery_timeout=1,
        )

        account = provider.get_email()

        self.assertEqual(account.email, "later@example.com")
        add_account_mock.assert_called_once_with(
            "later@example.com",
            "pw",
            source="hstockplus",
            source_order_id="90023",
        )


    def test_browser_oauth_identity_can_use_purchased_google_account_as_email_hint(self):
        provider = BrowserOAuthIdentityProvider(
            mailbox=_StaticMailbox(),
            extra={
                "oauth_provider": "google",
                "oauth_account_source": "mailbox",
            },
        )

        identity = provider.resolve()

        self.assertEqual(identity.identity_provider, "oauth_browser")
        self.assertEqual(identity.oauth_provider, "google")
        self.assertEqual(identity.email, "google.user@gmail.com")
        self.assertIs(identity.mailbox_account, provider.mailbox.account)
        self.assertEqual(identity.metadata["oauth_account_source"], "mailbox")

    def test_get_current_ids_is_empty_and_code_wait_is_unsupported(self):
        provider = HStockPlusGoogleAccountProvider(api_key="hsp-key", service_id="123")
        account = MailboxAccount(email="user@gmail.com", account_id="1")

        self.assertEqual(provider.get_current_ids(account), set())
        with self.assertRaises(NotImplementedError):
            provider.wait_for_code(account)


if __name__ == "__main__":
    unittest.main()
