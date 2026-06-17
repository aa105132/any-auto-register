import unittest

from platforms.airouter.core import AiRouterClient, build_airouter_browser_fingerprint
from platforms.airouter.browser_turnstile import AiRouterTurnstileHarvester
from platforms.airouter.protocol_mailbox import AiRouterMailboxRegistrar


class AirouterFingerprintTests(unittest.TestCase):
    def test_client_uses_supplied_browser_fingerprint_headers(self):
        fp = build_airouter_browser_fingerprint("seed-a")
        client = AiRouterClient(browser_fingerprint=fp, log_fn=lambda _m: None)

        self.assertEqual(client.session.headers.get("User-Agent"), fp["user_agent"])
        self.assertEqual(client.session.headers.get("Accept-Language"), fp["accept_language"])
        self.assertIn(str(fp["chrome_major"]), client.session.headers.get("Sec-CH-UA", ""))
        self.assertEqual(client.session.headers.get("Sec-CH-UA-Mobile"), "?0")
        self.assertFalse(client.session.trust_env)

    def test_registrar_creates_isolated_fingerprint_per_instance(self):
        one = AiRouterMailboxRegistrar(log_fn=lambda _m: None)
        two = AiRouterMailboxRegistrar(log_fn=lambda _m: None)

        self.assertTrue(one.browser_fingerprint.get("user_agent"))
        self.assertTrue(two.browser_fingerprint.get("user_agent"))
        # time_ns 参与 seed，同一批注册每个实例应有独立指纹。
        self.assertNotEqual(one.browser_fingerprint, two.browser_fingerprint)
        self.assertEqual(one.client.session.headers.get("User-Agent"), one.browser_fingerprint["user_agent"])

    def test_external_cdp_is_disabled_by_default_for_airouter(self):
        harvester = AiRouterTurnstileHarvester(
            proxy="http://user:pass@proxy.example:8080",
            cdp_url="http://127.0.0.1:9222",
            log_fn=lambda _m: None,
        )

        self.assertFalse(harvester.allow_external_cdp)


if __name__ == "__main__":
    unittest.main()
