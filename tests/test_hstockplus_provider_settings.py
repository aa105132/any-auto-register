import unittest
from unittest.mock import patch

from application.provider_settings import ProviderSettingsService


class _Repo:
    def list_by_type(self, provider_type):
        return []

    def delete(self, setting_id):
        return True


class _Definitions:
    def get_by_key(self, provider_type, provider_key):
        return None


class HStockPlusProviderSettingsTests(unittest.TestCase):
    @patch("application.provider_settings.HStockPlusGoogleAccountProvider")
    def test_list_hstockplus_google_products_uses_saved_provider_setting(self, provider_cls):
        client = provider_cls.return_value
        client.list_google_products.return_value = [
            {"service": 10, "name": "Google account", "rate": "1.20", "stock": 3},
        ]
        service = ProviderSettingsService(repository=_Repo())
        service.definitions = _Definitions()
        service.repository.resolve_runtime_settings = lambda provider_type, provider_key, overrides=None: {
            "hstockplus_api_key": "hsp-key",
            "hstockplus_api_url": "https://hstockplus.com/api/v2",
        }

        result = service.list_hstockplus_google_products(lang="zh")

        self.assertEqual(result["products"][0]["service"], 10)
        provider_cls.assert_called_once_with(
            api_base_url="https://hstockplus.com/api/v2",
            api_key="hsp-key",
        )
        client.list_google_products.assert_called_once_with(lang="zh")


if __name__ == "__main__":
    unittest.main()
