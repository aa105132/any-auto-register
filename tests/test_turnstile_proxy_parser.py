import unittest

from services.turnstile_solver.proxy_utils import parse_playwright_proxy


class TurnstileProxyParserTests(unittest.TestCase):
    def test_parse_proxy_url_with_auth(self):
        self.assertEqual(
            parse_playwright_proxy("http://demo:secret@31.59.20.176:6754"),
            {
                "server": "http://31.59.20.176:6754",
                "username": "demo",
                "password": "secret",
            },
        )

    def test_parse_proxy_with_explicit_scheme_segments(self):
        self.assertEqual(
            parse_playwright_proxy("http:31.59.20.176:6754:demo:secret"),
            {
                "server": "http://31.59.20.176:6754",
                "username": "demo",
                "password": "secret",
            },
        )

    def test_parse_proxy_with_host_port_username_password(self):
        self.assertEqual(
            parse_playwright_proxy("31.59.20.176:6754:demo:secret"),
            {
                "server": "socks5://31.59.20.176:6754",
                "username": "demo",
                "password": "secret",
            },
        )

    def test_parse_proxy_url_without_auth(self):
        self.assertEqual(
            parse_playwright_proxy("http://31.59.20.176:6754"),
            {"server": "http://31.59.20.176:6754"},
        )

    def test_invalid_proxy_format_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_playwright_proxy("31.59.20.176:6754")


if __name__ == "__main__":
    unittest.main()
