from __future__ import annotations

import unittest
import urllib.parse


class EnterConvergeMigrationTests(unittest.TestCase):
    def test_enter_constants_match_live_converge_auth0_config(self):
        from platforms.enter import core

        self.assertEqual(core.AUTH0_DOMAIN, "auth.converge.ai")
        self.assertEqual(core.APP_ORIGIN, "https://enter.converge.ai")
        self.assertEqual(core.REDIRECT_URI, "https://enter.converge.ai")
        self.assertEqual(core.API_DOMAIN, "api.enter.pro")
        self.assertEqual(core.API_AUDIENCE, "https://api.enter.pro")
        self.assertEqual(core.CLIENT_ID, "anCisSaaIA36fTZ2DUMiTMro3bYuptrf")

    def test_signup_url_uses_new_auth_domain_client_and_redirect(self):
        from platforms.enter.core import EnterClient

        url = EnterClient().build_signup_url(state="state-x", nonce="nonce-y")
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(parsed.netloc, "auth.converge.ai")
        self.assertEqual(parsed.path, "/authorize")
        self.assertEqual(qs["client_id"], ["anCisSaaIA36fTZ2DUMiTMro3bYuptrf"])
        self.assertEqual(qs["redirect_uri"], ["https://enter.converge.ai"])
        self.assertEqual(qs["audience"], ["https://api.enter.pro"])
        self.assertEqual(qs["screen_hint"], ["signup"])
        self.assertNotIn("connection", qs)

    def test_browser_registrar_builds_authorize_url_from_core_config(self):
        from platforms.enter.browser_register import EnterBrowserRegistrar

        registrar = EnterBrowserRegistrar(log_fn=lambda _msg: None)
        url = registrar._build_signup_url(state="state-z")
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)

        self.assertEqual(parsed.netloc, "auth.converge.ai")
        self.assertEqual(qs["client_id"], ["anCisSaaIA36fTZ2DUMiTMro3bYuptrf"])
        self.assertEqual(qs["redirect_uri"], ["https://enter.converge.ai"])
        self.assertEqual(qs["audience"], ["https://api.enter.pro"])
        self.assertNotIn("connection", qs)

    def test_browser_flow_is_identifier_first(self):
        import inspect
        from platforms.enter.browser_register import EnterBrowserRegistrar

        source = inspect.getsource(EnterBrowserRegistrar._run_auth_flow)
        self.assertIn("identifier-first", source)
        self.assertLess(source.index("input[name='email']"), source.index("_drive_post_identifier_steps"))
        self.assertIn("_drive_post_identifier_steps", source)

    def test_submit_click_ignores_hidden_auth0_submit_button(self):
        import inspect
        from platforms.enter.browser_register import EnterBrowserRegistrar

        source = inspect.getsource(EnterBrowserRegistrar._click_submit_no_wait)
        self.assertIn("not([aria-hidden='true'])", source)
        self.assertIn("visible submit button not found", source)


    def test_cdp_protocol_uses_continuous_cdp_auth_then_http_exchange(self):
        import inspect
        from platforms.enter.protocol_mailbox import EnterProtocolMailboxWorker

        run_source = inspect.getsource(EnterProtocolMailboxWorker.run)
        self.assertIn("_run_auth0_protocol_flow", run_source)
        self.assertNotIn("browser.run", run_source)

        flow_source = inspect.getsource(EnterProtocolMailboxWorker._run_auth0_protocol_flow)
        self.assertIn("_run_auth_flow", flow_source)
        self.assertIn("got auth_code by CDP", flow_source)
        self.assertIn("exchange_code_for_tokens", flow_source)
        self.assertIn("Token exchange failed", flow_source)

    def test_browser_flow_detects_forbidden_email_domain(self):
        import inspect
        from platforms.enter.browser_register import EnterBrowserRegistrar

        source = inspect.getsource(EnterBrowserRegistrar._drive_post_identifier_steps)
        self.assertIn("email domain is not allowed", source)
        self.assertIn("enter_email_domain_not_allowed", source)

    def test_protocol_blacklists_forbidden_email_domain(self):
        import inspect
        from platforms.enter.protocol_mailbox import EnterProtocolMailboxWorker

        source = inspect.getsource(EnterProtocolMailboxWorker.run)
        self.assertIn("add_mailbox_domain_blacklist", source)
        self.assertIn("email domain blacklisted", source)


    def test_enter_password_generation_upgrades_weak_password(self):
        from core.base_platform import RegisterConfig
        from platforms.enter.plugin import EnterPlatform

        platform = EnterPlatform(RegisterConfig(executor_type="cdp_protocol"))
        weak = platform._prepare_registration_password("OnlyLettersOnly")
        self.assertGreaterEqual(len(weak), 8)
        classes = [
            any(ch.islower() for ch in weak),
            any(ch.isupper() for ch in weak),
            any(ch.isdigit() for ch in weak),
            any(not ch.isalnum() for ch in weak),
        ]
        self.assertGreaterEqual(sum(classes), 3)
        self.assertNotEqual(weak, "OnlyLettersOnly")

    def test_enter_password_generation_keeps_strong_password(self):
        from core.base_platform import RegisterConfig
        from platforms.enter.plugin import EnterPlatform

        platform = EnterPlatform(RegisterConfig(executor_type="cdp_protocol"))
        strong = "Aa1!Bb2@Cc3#"
        self.assertEqual(platform._prepare_registration_password(strong), strong)


    def test_forbidden_domain_detector_normalizes_line_breaks(self):
        from platforms.enter.browser_register import EnterBrowserRegistrar

        class FakePage:
            def evaluate(self, _script):
                return "This email domain is not allowed to sign\nup"

        registrar = EnterBrowserRegistrar(log_fn=lambda _msg: None)
        self.assertTrue(registrar._has_forbidden_email_domain_error(FakePage()))



if __name__ == "__main__":
    unittest.main()
