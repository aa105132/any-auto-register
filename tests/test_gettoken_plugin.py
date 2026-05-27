import unittest
from unittest.mock import patch

from core.base_phone import PhoneAccount
from core.base_platform import Account, AccountStatus, RegisterConfig
from core.registry import get, list_platforms, load_all
from platforms.gettoken.plugin import GetTokenPlatform
from platforms.gettoken.protocol_oauth import GetTokenProtocolOAuthWorker, GetTokenProtocolPhoneWorker
from platforms.gettoken import browser_oauth as gettoken_browser_oauth
import core.oauth_browser as oauth_browser


class GetTokenPluginTests(unittest.TestCase):
    def test_registry_loads_gettoken_platform(self):
        load_all()
        names = {item["name"] for item in list_platforms()}
        self.assertIn("gettoken", names)
        self.assertIs(get("gettoken"), GetTokenPlatform)

    def test_protocol_oauth_requires_portal_login_token_before_consuming_browser_flow(self):
        worker = GetTokenProtocolOAuthWorker()
        with patch.object(worker, "check_user", return_value={"success": True, "data": {"isLoggedIn": False, "user": None}}):
            with self.assertRaisesRegex(RuntimeError, "gettoken_portal_login_token"):
                worker.run(email_hint="user@gmail.com")
        self.assertTrue(any(item["method"] == "OPEN" and "pay.imgto.link" in item["url"] for item in worker.request_trace))


    def test_gettoken_declares_phone_identity_mode(self):
        self.assertIn("phone", GetTokenPlatform.supported_identity_modes)

    def test_phone_identity_routes_to_protocol_sms_worker(self):
        class FakePhoneProvider:
            pass

        platform = GetTokenPlatform(RegisterConfig(
            executor_type="protocol",
            extra={
                "identity_provider": "phone",
                "phone_provider_enabled": True,
                "phone_otp_timeout": 9,
                "phone_poll_interval": 2,
                "phone_code_pattern": r"(\d{6})",
            },
        ))
        platform.phone_provider = FakePhoneProvider()

        with patch("platforms.gettoken.protocol_oauth.GetTokenProtocolPhoneWorker") as worker_cls:
            worker = worker_cls.return_value
            worker.run.return_value = {
                "email": "+8613800138000",
                "user_id": "u-phone",
                "api_key": "gt-phone-key",
                "api_key_info": {"source": "protocol_create"},
                "account_info": {"id": "u-phone", "phoneE164": "+8613800138000"},
                "cookies": {"auth": "cookie"},
                "session_cookie": "auth=cookie",
                "request_trace": [],
                "registration_note": "protocol_phone_sms",
                "phone": {"phone": "13800138000", "country_code": "+86"},
            }

            account = platform.register()

        self.assertEqual(account.email, "+8613800138000")
        self.assertEqual(account.token, "gt-phone-key")
        worker_cls.assert_called_once()
        worker.run.assert_called_once()
        kwargs = worker.run.call_args.kwargs
        self.assertIs(kwargs["phone_provider"], platform.phone_provider)
        self.assertEqual(kwargs["otp_timeout"], 9)
        self.assertEqual(kwargs["poll_interval"], 2)
        self.assertEqual(kwargs["code_pattern"], r"(\d{6})")

    def test_phone_worker_uses_provider_sms_code_and_portal_login(self):
        class FakePhoneProvider:
            def __init__(self):
                self.account = PhoneAccount(
                    phone="8613800138000",
                    project_id="114190",
                    provider_name="haozhu",
                )
                self.wait_args = None

            def get_phone(self):
                return self.account

            def wait_for_code(self, account, timeout=180, poll_interval=15, code_pattern=None):
                self.wait_args = (account, timeout, poll_interval, code_pattern)
                return "123456"

        class FakePhoneWorker(GetTokenProtocolPhoneWorker):
            def __init__(self):
                super().__init__(log_fn=lambda message: None)
                self.created_payload = None
                self.completed = None
                self.portal_token = None

            def check_user(self):
                return {"success": True, "data": {"isLoggedIn": False, "user": None}}

            def fetch_login_providers(self, *, app_id, origin):
                return {"providers": [{"channel": "PHONE_SMS"}]}

            def create_sms_attempt(self, *, app_id, origin, locale, phone_country_code, phone_number):
                self.created_payload = {
                    "app_id": app_id,
                    "origin": origin,
                    "locale": locale,
                    "phone_country_code": phone_country_code,
                    "phone_number": phone_number,
                }
                return {"action": "sms_code", "attemptId": "attempt-1", "phoneNumber": phone_number}

            def complete_sms_attempt(self, *, attempt_id, code):
                self.completed = (attempt_id, code)
                return {"result": {"loginToken": "portal-token", "phoneE164": "+8613800138000"}}

            def portal_login(self, *, login_token, referral_code="", referral_slug=""):
                self.portal_token = login_token
                return {"success": True, "data": {"user": {"id": "u-phone", "phoneE164": "+8613800138000"}}}

            def extract_or_create_api_key(self, *, create_api_key=True):
                return "gt-phone-key", {"source": "protocol_create"}

        phone_provider = FakePhoneProvider()
        worker = FakePhoneWorker()

        result = worker.run(phone_provider=phone_provider, otp_timeout=11, poll_interval=3, code_pattern=r"(\d{6})")

        self.assertEqual(phone_provider.wait_args, (phone_provider.account, 11, 3, r"(\d{6})"))
        self.assertEqual(worker.created_payload["phone_number"], "13800138000")
        self.assertEqual(worker.created_payload["phone_country_code"], "+86")
        self.assertEqual(worker.completed, ("attempt-1", "123456"))
        self.assertEqual(worker.portal_token, "portal-token")
        self.assertEqual(result["api_key"], "gt-phone-key")
        self.assertEqual(result["email"], "+8613800138000")
        self.assertEqual(result["registration_note"], "protocol_phone_sms")
        self.assertEqual(result["phone"]["project_id"], "114190")

    def test_google_prompt_labels_include_chinese_without_mojibake(self):
        labels = list(gettoken_browser_oauth.GOOGLE_TOS_LABELS) + list(gettoken_browser_oauth.GOOGLE_CONSENT_LABELS)
        self.assertIn("我同意", labels)
        self.assertIn("我明白", labels)
        self.assertIn("继续", labels)
        self.assertIn("允许", labels)
        self.assertNotIn("???", labels)
        self.assertNotIn("??", labels)
        for label in labels:
            self.assertFalse(set(label) == {"?"}, label)

    def test_external_chromium_cdp_args_are_visible_and_gpu_safe(self):
        args = oauth_browser._build_external_chromium_args(9333, r"D:\tmp\oauth-profile", "about:blank")
        self.assertIn("--remote-debugging-port=9333", args)
        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--new-window", args)
        self.assertNotIn("--headless", args)
        self.assertFalse(any(arg.startswith("--use-gl=") for arg in args), args)
        self.assertFalse(any(arg.startswith("--use-angle=") for arg in args), args)

    def test_google_prompt_clicker_has_no_mojibake_regex(self):
        import inspect
        source = inspect.getsource(gettoken_browser_oauth._click_google_prompt_button)
        self.assertNotIn("??", source)
        self.assertNotIn("privacy|terms|??", source)

    def test_fill_google_credentials_checks_inputs_before_consent(self):
        import inspect
        source = inspect.getsource(gettoken_browser_oauth._fill_google_credentials)
        challenge_pos = source.index("_handle_google_identifier_challenge")
        email_pos = source.index("input[type=\"email\"]")
        consent_pos = source.rindex("_handle_oauth_consent")
        self.assertLess(challenge_pos, consent_pos)
        self.assertLess(email_pos, consent_pos)

    def test_gettoken_map_result_preserves_phone_metadata(self):
        platform = GetTokenPlatform(RegisterConfig(executor_type="protocol"))
        result = platform._map_result({
            "email": "+8613800138000",
            "api_key": "sk-phone",
            "phone": {"phone": "13800138000", "project_id": "114190"},
            "portal_result": {"phoneE164": "+8613800138000"},
        })
        self.assertEqual(result.extra["phone"]["project_id"], "114190")
        self.assertEqual(result.extra["portal_result"]["phoneE164"], "+8613800138000")

    def test_map_result_marks_api_key_as_ai_token(self):
        platform = GetTokenPlatform(RegisterConfig(executor_type="protocol"))
        result = platform._map_result({
            "email": "user@gmail.com",
            "user_id": "u1",
            "api_key": "sk-test-gettoken",
            "request_trace": [{"method": "GET", "url": "/api/user/me", "status": 200}],
        })
        self.assertEqual(result.email, "user@gmail.com")
        self.assertEqual(result.token, "sk-test-gettoken")
        self.assertEqual(result.extra["ai_api_token"], "sk-test-gettoken")
        self.assertEqual(result.extra["request_trace"][0]["url"], "/api/user/me")

    def test_export_one_writes_gettoken_and_common_key_files(self):
        account = Account(
            platform="gettoken",
            email="user@gmail.com",
            password="",
            token="gt-test-key",
            status=AccountStatus.REGISTERED,
            extra={"api_key": "gt-test-key"},
        )
        result = GetTokenPlatform._export_one(account)
        self.assertTrue(result["ok"])
        self.assertIn("gettoken_keys.txt", result["data"]["message"])


if __name__ == "__main__":
    unittest.main()
