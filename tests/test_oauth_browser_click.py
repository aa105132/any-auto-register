import unittest
from unittest.mock import Mock

from core.oauth_browser import try_click_provider_on_page


class OAuthBrowserClickTests(unittest.TestCase):
    def test_try_click_provider_rejects_empty_invisible_page(self):
        page = Mock()
        page.evaluate.return_value = False
        self.assertFalse(try_click_provider_on_page(page, 'google'))


if __name__ == '__main__':
    unittest.main()
