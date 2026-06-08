import unittest

from core.proxy_utils import build_playwright_proxy_settings, normalize_proxy_url


class ProxyUtilsTests(unittest.TestCase):
    def test_normalize_raw_auth_proxy_defaults_to_http(self):
        self.assertEqual(
            normalize_proxy_url("31.59.20.176:6754:demo:secret"),
            "http://demo:secret@31.59.20.176:6754",
        )

    def test_normalize_explicit_scheme_segments(self):
        self.assertEqual(
            normalize_proxy_url("http:31.59.20.176:6754:demo:secret"),
            "http://demo:secret@31.59.20.176:6754",
        )

    def test_build_playwright_proxy_settings_splits_auth_fields(self):
        self.assertEqual(
            build_playwright_proxy_settings("31.59.20.176:6754:demo:secret"),
            {
                "server": "http://31.59.20.176:6754",
                "username": "demo",
                "password": "secret",
            },
        )

    def test_build_playwright_proxy_settings_normalizes_socks5h(self):
        self.assertEqual(
            build_playwright_proxy_settings("socks5h://demo:secret@proxy.local:1080"),
            {
                "server": "socks5://proxy.local:1080",
                "username": "demo",
                "password": "secret",
            },
        )


if __name__ == "__main__":
    unittest.main()
