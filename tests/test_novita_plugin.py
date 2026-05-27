from __future__ import annotations

import inspect
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from core.base_platform import RegisterConfig


class NovitaPluginTests(unittest.TestCase):
    def test_platform_declares_mailbox_and_google_oauth(self):
        from platforms.novita.plugin import NovitaPlatform

        platform = NovitaPlatform()
        self.assertEqual(platform.name, "novita")
        self.assertIn("mailbox", platform.supported_identity_modes)
        self.assertIn("oauth_browser", platform.supported_identity_modes)
        self.assertIn("google", platform.supported_oauth_providers)
        self.assertIn("protocol", platform.supported_executors)
        self.assertEqual(platform.default_mail_provider, "outlook_token")

    def test_static_protocol_endpoints_are_declared(self):
        from platforms.novita import browser_oauth

        self.assertEqual(browser_oauth.SITE_URL, "https://novita.ai/")
        self.assertEqual(browser_oauth.LOGIN_URL, "https://novita.ai/user/login")
        self.assertEqual(browser_oauth.KEYS_PATH, "/v2/user/key")
        self.assertEqual(browser_oauth.QUESTIONNAIRE_PATH, "/v1/user/questionnaire")
        self.assertEqual(browser_oauth.VERIFY_PATH, "/v3/model")

    def test_console_protocol_uses_api_server_and_bearer_session_token(self):
        from platforms.novita import browser_oauth

        self.assertEqual(browser_oauth.API_ORIGIN, "https://api-server.novita.ai")
        session = browser_oauth._cookie_session({}, token="demo-token")
        self.assertEqual(session.headers.get("Authorization"), "Bearer demo-token")
        bearer_session = browser_oauth._cookie_session({}, token="Bearer existing-token")
        self.assertEqual(bearer_session.headers.get("Authorization"), "Bearer existing-token")

    def test_oauth_failed_reason_detects_auth_res_failed_callback(self):
        from platforms.novita import browser_oauth

        class Page:
            def __init__(self, url, body=""):
                self.url = url
                self._body = body

            def is_closed(self):
                return False

            def locator(self, _selector):
                page = self

                class Locator:
                    def inner_text(self, timeout=0):
                        return page._body

                return Locator()

        class Browser:
            def pages(self):
                return [Page("https://novita.ai/?auth_res=failed", "")]

        self.assertIn("auth_res=failed", browser_oauth._oauth_failed_reason(Browser()))

    def test_browser_oauth_runner_stops_on_novita_failed_callback(self):
        from platforms.novita import browser_oauth

        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_oauth_failed_reason", source)
        self.assertIn("Novita OAuth 回调失败", source)

    def test_oauth_done_waits_for_novita_token_not_plain_callback_url(self):
        from platforms.novita import browser_oauth

        class Page:
            def __init__(self, url):
                self.url = url

            def is_closed(self):
                return False

        class Browser:
            def __init__(self, cookies, urls):
                self._cookies = cookies
                self._pages = [Page(url) for url in urls]

            def cookie_dict(self, domain_substrings=()):
                return dict(self._cookies)

            def pages(self):
                return list(self._pages)

        self.assertFalse(browser_oauth._oauth_done(Browser({}, ["https://novita.ai/api/auth?code=abc"])))
        self.assertFalse(browser_oauth._oauth_done(Browser({}, ["https://novita.ai/console"])))
        self.assertTrue(browser_oauth._oauth_done(Browser({"token": "novita-session-token"}, ["https://novita.ai/console"])))

    def test_mailbox_flow_uses_register_verify_login_questionnaire_and_key_protocols(self):
        from platforms.novita import browser_oauth

        self.assertTrue(hasattr(browser_oauth, "register_with_email_verification"))
        source = inspect.getsource(browser_oauth.register_with_email_verification)
        self.assertIn("_register_email_http", source)
        self.assertIn("verification_link_callback", source)
        self.assertIn("_verify_email_http", source)
        self.assertIn("_login_email_http", source)
        self.assertIn("_submit_questionnaire_http", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_verify_api_key_http", source)


    def test_novita_google_start_has_dom_fallback_for_chinese_button(self):
        from platforms.novita import browser_oauth

        source = inspect.getsource(browser_oauth._click_novita_google_button)
        self.assertIn("使用 Google 登录", source)
        self.assertIn("closest", source)
        self.assertIn("mouse.click", source)
        self.assertIn("boundingBox", source)
        start_source = inspect.getsource(browser_oauth._start_google_oauth)
        self.assertIn("_open_google_oauth_protocol", start_source)
        self.assertIn("_click_novita_google_button", start_source)

    def test_oauth_callback_cookie_matches_native_js_cookie_double_encoding(self):
        from platforms.novita import browser_oauth

        self.assertEqual(
            browser_oauth._encoded_oauth_callback_cookie_value(),
            "https%253A%252F%252Fnovita.ai%252Fapi%252Fauth",
        )

    def test_oauth_cookie_writer_uses_encoded_value_for_context_and_document_cookie(self):
        from platforms.novita import browser_oauth

        captured = {}

        class Context:
            def add_cookies(self, cookies):
                captured["cookies"] = cookies

        class Page:
            context = Context()

            def evaluate(self, script, payload):
                captured["script"] = script
                captured["payload"] = payload

        browser_oauth._set_novita_oauth_cookies(Page())
        cookie_value = browser_oauth._encoded_oauth_callback_cookie_value()
        callback_cookie = next(item for item in captured["cookies"] if item["name"] == browser_oauth.NOVITA_AUTH_CALLBACK_COOKIE)
        self.assertEqual(callback_cookie["value"], cookie_value)
        self.assertEqual(captured["payload"]["callbackValue"], cookie_value)

    def test_novita_google_oauth_protocol_url_matches_frontend_bundle(self):
        from platforms.novita import browser_oauth

        url = browser_oauth._build_google_oauth_url()
        parsed = urlparse(url)
        query = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "accounts.google.com")
        self.assertEqual(parsed.path, "/o/oauth2/v2/auth")
        self.assertEqual(query["client_id"], [browser_oauth.GOOGLE_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], ["https://novita.ai/api/auth"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["scope"], ["email profile openid"])

        source = inspect.getsource(browser_oauth._open_google_oauth_protocol)
        cookie_source = inspect.getsource(browser_oauth._set_novita_oauth_cookies)
        open_source = inspect.getsource(browser_oauth._open_oauth_url_nonblocking)
        self.assertIn("_set_novita_oauth_cookies", source)
        self.assertEqual(browser_oauth.NOVITA_AUTH_CALLBACK_COOKIE, "auth_callback_url")
        self.assertEqual(browser_oauth.NOVITA_AUTH_TYPE_COOKIE, "auth_type")
        self.assertIn("window.location.assign", open_source)

    def test_oauth_flow_uses_google_driver_then_protocol_questionnaire_and_key(self):
        from platforms.novita import browser_oauth

        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_start_google_oauth", source)
        self.assertIn("drive_google_oauth", source)
        self.assertIn("_get_user_info_http", source)
        self.assertIn("_submit_questionnaire_http", source)
        self.assertIn("_create_api_key_http", source)

    def test_plugin_maps_key_credit_and_questionnaire_fields(self):
        from platforms.novita.plugin import NovitaPlatform

        result = NovitaPlatform()._map_result({
            "email": "demo@example.com",
            "password": "pw",
            "api_key": "novita-demo-key",
            "api_verification": {"ok": True},
            "key_create_result": {"ok": True},
            "questionnaire_result": {"ok": True, "reward": "$1"},
            "balance": {"ok": True, "data": {"balance": 1}},
            "voucher": {"ok": True, "data": []},
            "session_token": "session-token",
        })

        self.assertEqual(result.email, "demo@example.com")
        self.assertEqual(result.password, "pw")
        self.assertEqual(result.token, "novita-demo-key")
        self.assertEqual(result.extra["api_key"], "novita-demo-key")
        self.assertEqual(result.extra["ai_api_token"], "novita-demo-key")
        self.assertEqual(result.extra["questionnaire_result"], {"ok": True, "reward": "$1"})
        self.assertEqual(result.extra["balance"], {"ok": True, "data": {"balance": 1}})
        self.assertEqual(result.extra["auth_header"], "Authorization")
        self.assertEqual(result.extra["api_base"], "https://api.novita.ai")


    def test_api_key_extractor_handles_nested_novita_key_shapes(self):
        from platforms.novita import browser_oauth

        payload = {
            "data": {
                "items": [
                    {"id": "old", "key": "masked"},
                    {"id": "new", "apiKey": "novita_demo_abcdefghijklmnopqrstuvwxyz123456"},
                ]
            }
        }
        self.assertEqual(
            browser_oauth._find_api_key(payload),
            "novita_demo_abcdefghijklmnopqrstuvwxyz123456",
        )

    def test_questionnaire_payload_contains_reward_related_profile_fields(self):
        from platforms.novita import browser_oauth

        payload = browser_oauth._default_questionnaire_payload("demo@example.com")
        self.assertEqual(payload["role"], "Developer")
        self.assertEqual(payload["companyName"], "Individual")
        self.assertIn("currentMonthlySpendOnAiModels", payload)
        self.assertIn("useCase", payload)

    def test_protocol_mailbox_register_passes_link_callback(self):
        from core.base_mailbox import MailboxAccount
        from platforms.novita.plugin import NovitaPlatform

        captured = {}

        class DummyMailbox:
            def get_email(self):
                return MailboxAccount(email="outlook@example.com", extra={})

            def get_current_ids(self, _account):
                return set()

            def wait_for_link(self, account, **kwargs):
                captured["mailbox_account"] = account
                captured["link_kwargs"] = kwargs
                return "https://novita.ai/user/verify?token=tok"

        def fake_register_with_email_verification(**kwargs):
            captured.update(kwargs)
            link = kwargs["verification_link_callback"]()
            return {
                "email": kwargs["email"],
                "password": kwargs["password"],
                "api_key": "novita_mail_key",
                "verification_link": link,
            }

        platform = NovitaPlatform(
            config=RegisterConfig(extra={"identity_provider": "mailbox"}),
            mailbox=DummyMailbox(),
        )
        with patch("platforms.novita.browser_oauth.register_with_email_verification", fake_register_with_email_verification):
            account = platform.register(password="Passw0rd-demo")

        self.assertEqual(account.email, "outlook@example.com")
        self.assertEqual(account.token, "novita_mail_key")
        self.assertEqual(captured["email"], "outlook@example.com")
        self.assertEqual(captured["password"], "Passw0rd-demo")
        self.assertIn("Novita", captured["link_kwargs"]["keyword"])


if __name__ == "__main__":
    unittest.main()
