import unittest
from unittest.mock import patch

from core.base_mailbox import HStockPlusGoogleAccountProvider


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class HStockPlusCatalogTests(unittest.TestCase):
    @patch("requests.post")
    def test_list_google_products_passes_lang_and_filters_google_like_names(self, post_mock):
        post_mock.return_value = _Response(
            {
                "services": [
                    {"service": 10, "name": "Google account", "entityType": "product", "rate": "1.20", "stock": 3},
                    {"service": 11, "name": "Instagram Followers", "entityType": "smm", "rate": "0.10", "stock": 1000},
                    {"service": 12, "name": "Gmail EDU mailbox", "entityType": "product", "rate": "2.00", "stock": 8},
                ]
            }
        )
        client = HStockPlusGoogleAccountProvider(api_key="hsp-key")

        products = client.list_google_products(lang="zh")

        self.assertEqual([item["service"] for item in products], [10, 12])
        self.assertEqual(post_mock.call_args.kwargs["data"]["action"], "services")
        self.assertEqual(post_mock.call_args.kwargs["data"]["lang"], "zh")
        self.assertEqual(post_mock.call_args.kwargs["data"]["limit"], "0")

    @patch("requests.post")
    def test_get_email_requires_enterprise_contract_when_enabled(self, post_mock):
        provider = HStockPlusGoogleAccountProvider(
            api_key="hsp-key",
            service_id="123",
            enterprise_contract_required=True,
            enterprise_contract_accepted=False,
            poll_interval=0,
            delivery_timeout=1,
        )

        with self.assertRaises(ValueError):
            provider.get_email()

        post_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
