from __future__ import annotations

import inspect
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.base_platform import AccountStatus, RegisterConfig
from core.registration import LinkSpec
from core.registry import get, list_platforms, load_all
from platforms.featherless import browser_oauth, protocol_mailbox
from platforms.featherless.plugin import FeatherlessPlatform
import core.google_oauth as google_oauth


class FeatherlessPluginTests(unittest.TestCase):
    def test_registry_loads_featherless_platform(self):
        load_all()
        names = {item["name"] for item in list_platforms()}
        self.assertIn("featherless", names)
        self.assertIs(get("featherless"), FeatherlessPlatform)

    def test_capabilities_include_mailbox_and_google_oauth(self):
        platform = FeatherlessPlatform(RegisterConfig(executor_type="protocol"))
        self.assertEqual(platform.name, "featherless")
        self.assertEqual(platform.display_name, "Featherless")
        self.assertIn("protocol", platform.supported_executors)
        self.assertIn("headed", platform.supported_executors)
        self.assertIn("mailbox", platform.supported_identity_modes)
        self.assertIn("oauth_browser", platform.supported_identity_modes)
        self.assertIn("google", platform.supported_oauth_providers)

    def test_llm_api_urls_keep_v1_prefix(self):
        self.assertEqual(protocol_mailbox._llm_api_url(protocol_mailbox.MODELS_PATH), "https://api.featherless.ai/v1/models")
        self.assertEqual(protocol_mailbox._llm_api_url(protocol_mailbox.CHAT_COMPLETIONS_PATH), "https://api.featherless.ai/v1/chat/completions")

    def test_static_protocol_endpoints_match_frontend_bundle(self):
        self.assertEqual(protocol_mailbox.SITE_URL, "https://featherless.ai")
        self.assertEqual(protocol_mailbox.API_ORIGIN, "https://api.featherless.ai")
        self.assertEqual(protocol_mailbox.LLM_API_BASE, "https://api.featherless.ai/v1")
        self.assertEqual(protocol_mailbox.REGISTER_PATH, "/auth/register")
        self.assertEqual(protocol_mailbox.LOGIN_PATH, "/auth/login")
        self.assertEqual(protocol_mailbox.ME_PATH, "/auth/me")
        self.assertEqual(protocol_mailbox.EMAIL_VERIFY_PATH, "/auth/email-verification")
        self.assertEqual(protocol_mailbox.API_KEYS_PATH, "/api-keys")

    def test_mailbox_adapter_uses_email_verification_and_key_creation(self):
        adapter = FeatherlessPlatform().build_protocol_mailbox_adapter()
        self.assertIsNotNone(adapter)
        self.assertIsInstance(adapter.link_spec, LinkSpec)
        source = inspect.getsource(protocol_mailbox.FeatherlessProtocolMailboxWorker.run)
        self.assertIn("_register_email_http", source)
        self.assertIn("verification_link_callback", source)
        self.assertIn("_verify_email_http", source)
        self.assertIn("_login_email_http", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_verify_api_key_http", source)

    def test_oauth_uses_backend_google_callback_and_creates_key(self):
        self.assertEqual(browser_oauth.GOOGLE_OAUTH_URL, "https://api.featherless.ai/auth/google/callback")
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("drive_google_oauth", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_get_me_http", source)

    def test_featherless_auth_detection_ignores_google_url_query_params(self):
        class FakePage:
            url = "https://accounts.google.com/v3/signin/identifier?app_domain=https%3A%2F%2Ffeatherless.ai&redirect_uri=https%3A%2F%2Ffeatherless.ai%2Fauth%2Fgoogle%2Fcallback"

            def is_closed(self):
                return False

        class FakeBrowser:
            def pages(self):
                return [FakePage()]

        with patch.object(browser_oauth, "_get_me_from_browser", return_value={"ok": False}):
            self.assertFalse(browser_oauth._is_authenticated(FakeBrowser()))

    def test_google_driver_email_step_has_playwright_fallback(self):
        source = inspect.getsource(google_oauth.drive_google_oauth)
        self.assertIn("_fill_google_input_playwright(page, ['input[type=\"email\"]", source)

    def test_google_driver_recognizes_featherless_identifier_page(self):
        class FakePage:
            url = "https://accounts.google.com/v3/signin/identifier?client_id=305547909718-chi7cgvo7on8abgma2vefk72t8m9tebd.apps.googleusercontent.com&app_domain=https%3A%2F%2Ffeatherless.ai"

            def is_closed(self):
                return False

        self.assertTrue(google_oauth._is_google_oauth_page(FakePage()))

    def test_oauth_reuses_session_me_result_and_creates_key(self):
        class FakePage:
            url = "about:blank"

            def goto(self, url, **_kwargs):
                self.url = url

            def is_closed(self):
                return False

        class FakeBrowser:
            def __init__(self, **_kwargs):
                self.page = FakePage()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def new_page(self):
                return self.page

            def pages(self):
                return [self.page]

        class FakeGoogleResult:
            blocked_on_password = False
            last_url = ""
            last_body = ""

        user = {"id": "user_oauth", "email": "oauth@example.com", "email_verified": True}
        with patch.object(browser_oauth, "OAuthBrowser", FakeBrowser), patch.object(
            browser_oauth, "drive_google_oauth", return_value=FakeGoogleResult()
        ), patch.object(
            browser_oauth, "_get_me_from_browser", return_value={"ok": True, "user": user, "cookies": {"sid": "cookie"}}
        ), patch.object(
            browser_oauth, "_get_me_http", return_value={"ok": True, "user": user}
        ), patch.object(
            browser_oauth, "_create_api_key_http", return_value={"ok": True, "api_key": "rc_abcdefghijklmnopqrstuvwxyz123456", "api_key_info": {"id": "key_1"}, "result": {"ok": True}}
        ), patch.object(
            browser_oauth, "_verify_api_key_http", return_value={"ok": True, "status": 200, "url": "https://api.featherless.ai/v1/models"}
        ):
            result = browser_oauth.register_with_browser_oauth(email_hint="oauth@example.com", timeout=5)

        self.assertEqual(result["email"], "oauth@example.com")
        self.assertEqual(result["user"], user)
        self.assertEqual(result["api_key"], "rc_abcdefghijklmnopqrstuvwxyz123456")
        self.assertEqual(result["api_verification"]["url"], "https://api.featherless.ai/v1/models")


    def test_existing_verified_email_logs_in_without_waiting_for_new_verification_mail(self):
        worker = protocol_mailbox.FeatherlessProtocolMailboxWorker(log_fn=lambda _msg: None)
        user = {"id": "user_existing", "email": "demo@example.com", "email_verified": True}
        callback_called = {"value": False}

        def verification_link_callback():
            callback_called["value"] = True
            raise AssertionError("不应等待新验证邮件")

        with patch.object(
            protocol_mailbox,
            "_register_email_http",
            return_value={
                "ok": True,
                "status": 400,
                "data": {"code": "user_already_exists"},
                "already_exists": True,
                "needs_verify": False,
                "user": {},
            },
        ), patch.object(
            protocol_mailbox,
            "_login_email_http",
            return_value={"ok": True, "user": user},
        ), patch.object(
            protocol_mailbox,
            "_get_me_http",
            side_effect=[{"ok": False, "status": 401, "user": {}}, {"ok": True, "user": user}],
        ), patch.object(
            protocol_mailbox,
            "_verify_email_http",
        ) as verify_email, patch.object(
            protocol_mailbox,
            "_create_api_key_http",
            return_value={"ok": True, "api_key": "rc_existing_key_abcdefghijklmnopqrstuvwxyz", "api_key_info": {"id": "key_1"}, "result": {"ok": True}},
        ), patch.object(
            protocol_mailbox,
            "_verify_api_key_http",
            return_value={"ok": True, "status": 200},
        ):
            result = worker.run(
                email="demo@example.com",
                password="secret",
                verification_link_callback=verification_link_callback,
            )

        self.assertFalse(callback_called["value"])
        verify_email.assert_not_called()
        self.assertEqual(result["user"], user)
        self.assertEqual(result["api_key"], "rc_existing_key_abcdefghijklmnopqrstuvwxyz")

    def test_verification_link_timeout_is_treated_as_mailbox_timeout(self):
        import application.tasks as tasks

        self.assertTrue(tasks._is_verification_timeout_failure("等待验证链接超时 (180s)"))
        self.assertTrue(tasks._is_verification_timeout_failure("Featherless: 未获取到验证链接"))

    def test_task_success_auto_exports_featherless_key(self):
        import application.tasks as tasks

        self.assertTrue(hasattr(tasks, "_auto_export_featherless_key"))
        source = inspect.getsource(tasks)
        self.assertIn("_auto_export_featherless_key(logger, account)", source)

    def test_result_mapping_saves_openai_compatible_credentials(self):
        result = FeatherlessPlatform()._map_result({
            "email": "demo@example.com",
            "password": "secret",
            "user": {"id": "user_123", "email": "demo@example.com", "email_verified": True},
            "api_key": "fls_demo_key",
            "api_key_info": {"id": "key_123", "name": "auto"},
            "api_verification": {"ok": True, "status": 200},
            "key_create_result": {"ok": True},
            "auth_method": "email",
        })
        self.assertEqual(result.email, "demo@example.com")
        self.assertEqual(result.password, "secret")
        self.assertEqual(result.user_id, "user_123")
        self.assertEqual(result.token, "fls_demo_key")
        self.assertEqual(result.status, AccountStatus.REGISTERED)
        self.assertEqual(result.extra["api_key"], "fls_demo_key")
        self.assertEqual(result.extra["ai_api_token"], "fls_demo_key")
        self.assertEqual(result.extra["api_base"], "https://api.featherless.ai/v1")
        self.assertEqual(result.extra["control_api_base"], "https://api.featherless.ai")
        self.assertEqual(result.extra["auth_header"], "Authorization")
        self.assertEqual(result.extra["auth_scheme"], "Bearer")

    def test_result_mapping_is_json_serializable(self):
        circular = {"ok": True, "status": 200}
        circular["attempts"] = [circular]
        result = FeatherlessPlatform()._map_result({
            "email": "demo@example.com",
            "password": "secret",
            "user": {"id": "user_123", "email": "demo@example.com", "email_verified": True},
            "api_key": "rc_demo_abcdefghijklmnopqrstuvwxyz",
            "register_result": circular,
            "verify_result": circular,
            "login_result": circular,
            "key_create_result": circular,
        })

        json.dumps(result.extra, ensure_ascii=False)

    def test_attached_mailbox_identity_metadata_is_json_serializable(self):
        provider_account = {"provider_type": "mailbox", "provider_name": "outlook_token"}
        provider_account["self"] = provider_account
        provider_resource = {"provider_type": "mailbox", "provider_name": "outlook_token", "metadata": {}}
        provider_resource["metadata"]["resource"] = provider_resource
        identity = SimpleNamespace(
            identity_provider="mailbox",
            email="demo@example.com",
            oauth_provider="",
            chrome_user_data_dir="",
            chrome_cdp_url="",
            metadata={},
            mailbox_account=SimpleNamespace(
                email="demo@example.com",
                account_id="mail_1",
                extra={
                    "provider_account": provider_account,
                    "provider_resource": provider_resource,
                },
            ),
        )
        platform = FeatherlessPlatform()
        mapped = platform._map_result({
            "email": "demo@example.com",
            "password": "secret",
            "user": {"id": "user_123", "email": "demo@example.com", "email_verified": True},
            "api_key": "rc_demo_abcdefghijklmnopqrstuvwxyz",
        })
        account = platform._attach_identity_metadata(
            platform._account_from_registration_result(mapped),
            identity,
        )

        json.dumps(account.extra, ensure_ascii=False)

    def test_api_key_extractor_handles_create_response_shape(self):
        payload = {"key": "fls_demo_abcdefghijklmnopqrstuvwxyz", "id": "key_123"}
        self.assertEqual(protocol_mailbox._find_api_key(payload), "fls_demo_abcdefghijklmnopqrstuvwxyz")


if __name__ == "__main__":
    unittest.main()
