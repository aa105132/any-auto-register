from __future__ import annotations

import inspect
import unittest

from core.base_platform import AccountStatus, RegisterConfig
from core.registry import get, list_platforms, load_all
from platforms.lemondata import browser_oauth, protocol_mailbox
from platforms.lemondata.plugin import LemonDataPlatform


class LemonDataPlatformTests(unittest.TestCase):
    def test_registry_loads_lemondata_platform(self):
        load_all()
        names = {item["name"] for item in list_platforms()}
        self.assertIn("lemondata", names)
        self.assertIs(get("lemondata"), LemonDataPlatform)

    def test_capabilities_include_mailbox_and_google_oauth(self):
        platform = LemonDataPlatform(RegisterConfig(executor_type="protocol"))
        self.assertIn("mailbox", platform.supported_identity_modes)
        self.assertIn("oauth_browser", platform.supported_identity_modes)
        self.assertIn("google", platform.supported_oauth_providers)
        self.assertIn("cdp_protocol", platform.supported_executors)

    def test_protocol_urls_use_tokenlab_domain(self):
        from platforms.lemondata.core import API_BASE, DASHBOARD_URL, LLM_API_BASE, SIGNIN_URL, SITE_URL

        self.assertEqual(SITE_URL, "https://tokenlab.sh")
        self.assertEqual(SIGNIN_URL, "https://tokenlab.sh/signin")
        self.assertEqual(DASHBOARD_URL, "https://tokenlab.sh/dashboard/api")
        self.assertEqual(API_BASE, "https://api.tokenlab.sh")
        self.assertEqual(LLM_API_BASE, "https://api.tokenlab.sh/v1")

    def test_mailbox_flow_uses_turnstile_and_authjs_magic_link(self):
        self.assertEqual(protocol_mailbox.TURNSTILE_SITEKEY, "0x4AAAAAACgPfXQhg8TKlBOO")
        source = inspect.getsource(protocol_mailbox.LemonDataProtocolMailboxWorker.run)
        self.assertIn("verify_captcha", source)
        self.assertIn("send_email_signin", source)
        self.assertIn("visit_verification_link", source)
        self.assertIn("create_or_find_api_key", source)
        self.assertIn("bootstrap_cdp_challenge", source)

    def test_mailbox_link_spec_does_not_filter_authjs_generic_subject(self):
        adapter = LemonDataPlatform().build_protocol_mailbox_adapter()
        self.assertEqual(adapter.link_spec.keyword, "")

    def test_generic_extractor_accepts_lemondata_authjs_magic_link(self):
        from core.base_mailbox import _extract_verification_link

        body = (
            'Sign in to your account '
            '<a href="https://tokenlab.sh/api/auth/callback/email?'
            'callbackUrl=https%3A%2F%2Ftokenlab.sh%2Fdashboard%2Fapi'
            '&token=abc123&email=demo%40example.com">Sign in</a>'
        )

        self.assertEqual(
            _extract_verification_link(body, keyword=""),
            "https://tokenlab.sh/api/auth/callback/email?"
            "callbackUrl=https%3A%2F%2Ftokenlab.sh%2Fdashboard%2Fapi"
            "&token=abc123&email=demo%40example.com",
        )

    def test_oauth_defaults_to_isolated_profile_when_cdp_url_is_shared(self):
        self.assertTrue(hasattr(browser_oauth, "isolated_oauth_browser_options"))
        with browser_oauth.isolated_oauth_browser_options(
            chrome_user_data_dir="",
            chrome_cdp_url="http://127.0.0.1:9222",
            allow_shared_cdp=False,
        ) as options:
            self.assertEqual(options["chrome_cdp_url"], "")
            self.assertIn("lemondata_oauth_", options["chrome_user_data_dir"])

    def test_balance_parser_and_registration_gate(self):
        from platforms.lemondata.core import extract_balance_amount

        self.assertGreaterEqual(extract_balance_amount({"data": {"balance": "$1.00"}}), 1.0)
        self.assertLess(extract_balance_amount({"credits": {"available": "0.50"}}), 1.0)

        result = LemonDataPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "ld_sk_demo",
            "balance_result": {"ok": False, "amount": 0.5},
        })
        self.assertEqual(result.status, AccountStatus.INVALID)


    def test_balance_parser_does_not_treat_org_ids_as_balance(self):
        from platforms.lemondata.core import extract_balance_amount

        amount, evidence = extract_balance_amount({"organizations": [{"id": "123", "name": "demo"}]}, with_evidence=True)
        self.assertEqual(amount, 0.0)
        self.assertFalse(evidence)

    def test_mailbox_and_oauth_flows_require_one_dollar_balance(self):
        mailbox_source = inspect.getsource(protocol_mailbox.LemonDataProtocolMailboxWorker.run)
        oauth_source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("require_min_balance", mailbox_source)
        self.assertIn("require_min_balance", oauth_source)

    def test_oauth_flow_prefers_http_key_creation_after_google_oauth(self):
        self.assertTrue(hasattr(browser_oauth, "_create_api_key_http"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("drive_google_oauth", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_collect_dashboard_api_requests", source)


    def test_task_auto_export_hook_includes_lemondata(self):
        import application.tasks as tasks

        self.assertTrue(hasattr(tasks, "_auto_export_lemondata_key"))
        source = inspect.getsource(tasks)
        self.assertIn("_auto_export_lemondata_key(logger, account)", source)

    def test_plugin_maps_api_key_as_ai_token(self):
        result = LemonDataPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "ld_sk_demo",
            "api_verification": {"ok": True},
            "key_create_result": {"ok": True, "status": 200},
            "account_info": {"id": "u1"},
        })
        self.assertEqual(result.email, "demo@example.com")
        self.assertEqual(result.token, "ld_sk_demo")
        self.assertEqual(result.extra["ai_api_token"], "ld_sk_demo")
        self.assertEqual(result.extra["api_base"], "https://api.tokenlab.sh/v1")
        self.assertEqual(result.extra["auth_header"], "Authorization: Bearer")

    def test_oauth_register_accepts_allow_shared_cdp_flag(self):
        import inspect

        signature = inspect.signature(browser_oauth.register_with_browser_oauth)
        self.assertIn("allow_shared_cdp", signature.parameters)
        self.assertFalse(signature.parameters["allow_shared_cdp"].default)

    def test_oauth_has_browser_api_key_creation_fallback(self):
        self.assertTrue(hasattr(browser_oauth, "_create_api_key_in_browser"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("HTTP 创建 API Key 失败，改用浏览器同源 fetch", source)
        self.assertIn("_create_api_key_in_browser", source)

    def test_oauth_http_replay_uses_browser_user_agent(self):
        signature = inspect.signature(browser_oauth._create_api_key_http)
        self.assertIn("user_agent", signature.parameters)
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("navigator.userAgent", source)
        self.assertIn("user_agent=browser_user_agent", source)
        mapped = LemonDataPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "ld_sk_demo",
            "balance_result": {"ok": True, "amount": 1.0},
            "browser_user_agent": "UA-from-browser",
        })
        self.assertEqual(mapped.extra["browser_user_agent"], "UA-from-browser")


if __name__ == "__main__":
    unittest.main()
