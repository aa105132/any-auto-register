import unittest
from unittest.mock import patch

from core.base_phone import PhoneAccount
from core.base_platform import RegisterConfig, AccountStatus


class TokendancePluginTests(unittest.TestCase):
    def test_tokendance_declares_phone_identity_mode(self):
        from platforms.tokendance.plugin import TokendancePlatform

        self.assertIn("phone", TokendancePlatform.supported_identity_modes)
        self.assertEqual(TokendancePlatform.default_phone_project_id, "108963")

    def test_phone_identity_routes_to_protocol_phone_worker_with_haozhu_project(self):
        from platforms.tokendance.plugin import TokendancePlatform

        class FakePhoneProvider:
            project_id = ""

        platform = TokendancePlatform(RegisterConfig(
            executor_type="protocol",
            extra={
                "identity_provider": "phone",
                "phone_provider_enabled": True,
                "phone_otp_timeout": 12,
                "phone_poll_interval": 3,
                "phone_code_pattern": r"(\d{6})",
            },
        ))
        platform.phone_provider = FakePhoneProvider()

        with patch("platforms.tokendance.protocol_phone.TokendanceProtocolPhoneWorker") as worker_cls:
            worker = worker_cls.return_value
            worker.run.return_value = {
                "email": "+8613800138000",
                "user_id": "td-user",
                "api_key": "td_sk_demo",
                "api_key_info": {"id": "key-1"},
                "account_info": {"id": "td-user", "phone": "+8613800138000"},
                "cookies": {"sid": "cookie"},
                "session_cookie": "sid=cookie",
                "request_trace": [],
                "phone": {"phone": "13800138000", "project_id": "108963"},
            }

            account = platform.register()

        self.assertEqual(account.status, AccountStatus.REGISTERED)
        self.assertEqual(account.token, "td_sk_demo")
        self.assertEqual(account.extra["ai_api_token"], "td_sk_demo")
        self.assertEqual(platform.phone_provider.project_id, "108963")
        kwargs = worker.run.call_args.kwargs
        self.assertIs(kwargs["phone_provider"], platform.phone_provider)
        self.assertEqual(kwargs["otp_timeout"], 12)
        self.assertEqual(kwargs["poll_interval"], 3)
        self.assertEqual(kwargs["code_pattern"], r"(\d{6})")

    def test_worker_uses_watcha_phone_oauth_then_tokendance_keys(self):
        from platforms.tokendance.protocol_phone import TokendanceProtocolPhoneWorker

        class FakePhoneProvider:
            def __init__(self):
                self.account = PhoneAccount(phone="8613800138000", project_id="108963", provider_name="haozhu")
                self.wait_args = None

            def get_phone(self):
                return self.account

            def wait_for_code(self, account, timeout=180, poll_interval=15, code_pattern=None):
                self.wait_args = (account, timeout, poll_interval, code_pattern)
                return "123456"

        class FakeWorker(TokendanceProtocolPhoneWorker):
            def __init__(self):
                super().__init__(log_fn=lambda _message: None)
                self.sent_phone = None
                self.signed_payload = None
                self.authorized = False
                self.callback_code = None
                self.created_body = None

            def request_watcha_signin_code(self, *, phone, captcha=""):
                self.sent_phone = phone
                return {"ok": True}

            def signin_watcha_phone_code(self, *, phone, code):
                self.signed_payload = {"phone": phone, "code": code}
                return {"access_token": "watcha-access", "refresh_token": "watcha-refresh", "profile": {"id": 7, "phone": phone}}

            def authorize_watcha_oauth(self, *, access_token):
                self.authorized = access_token == "watcha-access"
                return "oauth-code"

            def tokendance_callback(self, *, code):
                self.callback_code = code
                return {"user": {"id": "td-user", "phone": "+8613800138000"}}

            def extract_or_create_api_key(self, *, name):
                self.created_body = {"name": name}
                return "td_sk_demo", {"id": "key-1", "key": "td_sk_demo"}

        phone_provider = FakePhoneProvider()
        worker = FakeWorker()
        result = worker.run(phone_provider=phone_provider, otp_timeout=10, poll_interval=2, code_pattern=r"(\d{6})", key_name="auto-key")

        self.assertEqual(phone_provider.wait_args, (phone_provider.account, 10, 2, r"(\d{6})"))
        self.assertEqual(worker.sent_phone, "13800138000")
        self.assertEqual(worker.signed_payload["code"], "123456")
        self.assertTrue(worker.authorized)
        self.assertEqual(worker.callback_code, "oauth-code")
        self.assertEqual(worker.created_body["name"], "auto-key")
        self.assertEqual(result["api_key"], "td_sk_demo")
        self.assertEqual(result["phone"]["project_id"], "108963")

    def test_worker_uses_cdp_captcha_solver_for_signin_code(self):
        from platforms.tokendance.protocol_phone import TokendanceProtocolPhoneWorker

        class FakePhoneProvider:
            def __init__(self):
                self.account = PhoneAccount(phone="13800138000", project_id="108963", provider_name="haozhu")

            def get_phone(self):
                return self.account

            def wait_for_code(self, account, timeout=180, poll_interval=15, code_pattern=None):
                return "654321"

        class FakeCaptchaSolver:
            def __init__(self):
                self.calls = []

            def solve_aliyun(self, *, page_url, scene_id, button_selector=""):
                self.calls.append({"page_url": page_url, "scene_id": scene_id, "button_selector": button_selector})
                return "aliyun-token"

        class FakeWorker(TokendanceProtocolPhoneWorker):
            def __init__(self):
                super().__init__(log_fn=lambda _message: None)
                self.verify_payload = None

            def request_watcha_signin_code(self, *, phone, captcha=""):
                self.verify_payload = {"phone": phone, "captcha": captcha}
                return {"ok": True}

            def signin_watcha_phone_code(self, *, phone, code):
                return {"access_token": "watcha-access", "refresh_token": "watcha-refresh"}

            def authorize_watcha_oauth(self, *, access_token):
                return "oauth-code"

            def tokendance_callback(self, *, code):
                return {"user": {"id": "td-user", "phone": "+8613800138000"}}

            def extract_or_create_api_key(self, *, name):
                return "td_sk_demo", {"id": "key-1", "key": "td_sk_demo"}

        solver = FakeCaptchaSolver()
        worker = FakeWorker()
        result = worker.run(phone_provider=FakePhoneProvider(), captcha_solver=solver)

        self.assertEqual(worker.verify_payload, {"phone": "13800138000", "captcha": "aliyun-token"})
        self.assertEqual(solver.calls[0]["scene_id"], "1jr8d9gx")
        self.assertEqual(result["api_key"], "td_sk_demo")

    def test_request_watcha_verify_code_reports_captcha_scene(self):
        from platforms.tokendance.protocol_phone import TokendanceProtocolPhoneWorker, WatchaCaptchaRequired

        class FakeResponse:
            status_code = 449
            ok = False
            text = "captcha"

            def json(self):
                return {"statusCode": 449, "code": "RETRY_CAPTCHA", "captchaContext": {"sceneId": "1wsn666v"}}

        worker = TokendanceProtocolPhoneWorker(log_fn=lambda _message: None)
        worker.watcha.request = lambda *args, **kwargs: FakeResponse()

        with self.assertRaises(WatchaCaptchaRequired) as raised:
            worker.request_watcha_verify_code(phone="13800138000")

        self.assertEqual(raised.exception.scene_id, "1wsn666v")

    def test_worker_can_opt_into_legacy_register_then_signin_fallback(self):
        from platforms.tokendance.protocol_phone import TokendanceProtocolPhoneWorker

        class FakePhoneProvider:
            def __init__(self):
                self.account = PhoneAccount(phone="13800138000", project_id="108963", provider_name="haozhu")
                self.codes = ["111111", "222222"]
                self.wait_calls = 0

            def get_phone(self):
                return self.account

            def wait_for_code(self, account, timeout=180, poll_interval=15, code_pattern=None):
                value = self.codes[self.wait_calls]
                self.wait_calls += 1
                return value

        class FakeCaptchaSolver:
            def __init__(self):
                self.scenes = []

            def solve_aliyun(self, *, page_url, scene_id, button_selector=""):
                self.scenes.append(scene_id)
                return f"captcha-{scene_id}"

        class FakeWorker(TokendanceProtocolPhoneWorker):
            def __init__(self):
                super().__init__(log_fn=lambda _message: None)
                self.register_captcha = None
                self.signin_captcha = None
                self.signin_code = None

            def request_watcha_verify_code(self, *, phone, captcha=""):
                self.register_captcha = captcha
                return {"ok": True}

            def request_watcha_signin_code(self, *, phone, captcha=""):
                self.signin_captcha = captcha
                return {"ok": True}

            def signup_watcha_phone(self, *, phone, code, password, invitation_code=""):
                raise RuntimeError("Watcha API failed: 邮箱或手机号已注册")

            def signin_watcha_phone_code(self, *, phone, code):
                self.signin_code = code
                return {"access_token": "watcha-access", "refresh_token": "watcha-refresh"}

            def authorize_watcha_oauth(self, *, access_token):
                return "oauth-code"

            def tokendance_callback(self, *, code):
                return {"user": {"id": "td-user", "phone": "+8613800138000"}}

            def extract_or_create_api_key(self, *, name):
                return "td_sk_demo", {"id": "key-1", "key": "td_sk_demo"}

        solver = FakeCaptchaSolver()
        provider = FakePhoneProvider()
        worker = FakeWorker()
        result = worker.run(phone_provider=provider, captcha_solver=solver, use_legacy_register_flow=True)

        self.assertEqual(provider.wait_calls, 2)
        self.assertEqual(worker.signin_code, "222222")
        self.assertIn("1wsn666v", solver.scenes)
        self.assertIn("1jr8d9gx", solver.scenes)
        self.assertEqual(worker.signin_captcha, "captcha-1jr8d9gx")
        self.assertEqual(result["api_key"], "td_sk_demo")

    def test_worker_default_signin_flow_uses_only_one_sms_code(self):
        from platforms.tokendance.protocol_phone import TokendanceProtocolPhoneWorker

        class FakePhoneProvider:
            def __init__(self):
                self.account = PhoneAccount(phone="13800138000", project_id="108963", provider_name="haozhu")
                self.wait_calls = 0

            def get_phone(self):
                return self.account

            def wait_for_code(self, account, timeout=180, poll_interval=15, code_pattern=None):
                self.wait_calls += 1
                return "333333"

        class FakeWorker(TokendanceProtocolPhoneWorker):
            def __init__(self):
                super().__init__(log_fn=lambda _message: None)
                self.register_called = False
                self.signin_request_called = False
                self.signin_code = None

            def request_watcha_verify_code(self, *, phone, captcha=""):
                self.register_called = True
                return {"ok": True}

            def request_watcha_signin_code(self, *, phone, captcha=""):
                self.signin_request_called = True
                return {"ok": True}

            def signup_watcha_phone(self, *, phone, code, password, invitation_code=""):
                self.register_called = True
                return {"access_token": "unexpected"}

            def signin_watcha_phone_code(self, *, phone, code):
                self.signin_code = code
                return {"access_token": "watcha-access", "refresh_token": "watcha-refresh"}

            def authorize_watcha_oauth(self, *, access_token):
                return "oauth-code"

            def tokendance_callback(self, *, code):
                return {"user": {"id": "td-user", "phone": "+8613800138000"}}

            def extract_or_create_api_key(self, *, name):
                return "td_sk_demo", {"id": "key-1", "key": "td_sk_demo"}

        provider = FakePhoneProvider()
        worker = FakeWorker()
        result = worker.run(phone_provider=provider)

        self.assertEqual(provider.wait_calls, 1)
        self.assertTrue(worker.signin_request_called)
        self.assertFalse(worker.register_called)
        self.assertEqual(worker.signin_code, "333333")
        self.assertEqual(result["api_key"], "td_sk_demo")

    def test_worker_logs_after_captcha_before_waiting_sms(self):
        from platforms.tokendance.protocol_phone import TokendanceProtocolPhoneWorker

        messages = []

        class FakePhoneProvider:
            def __init__(self):
                self.account = PhoneAccount(phone="13800138000", project_id="108963", provider_name="haozhu")

            def get_phone(self):
                return self.account

            def wait_for_code(self, account, timeout=180, poll_interval=15, code_pattern=None):
                return "444444"

        class FakeCaptchaSolver:
            def solve_aliyun(self, *, page_url, scene_id, button_selector=""):
                return "aliyun-token"

        class FakeWorker(TokendanceProtocolPhoneWorker):
            def __init__(self):
                super().__init__(log_fn=messages.append)

            def request_watcha_signin_code(self, *, phone, captcha=""):
                return {"statusCode": 200, "message": "验证码已发送", "data": None}

            def signin_watcha_phone_code(self, *, phone, code):
                return {"access_token": "watcha-access", "refresh_token": "watcha-refresh"}

            def authorize_watcha_oauth(self, *, access_token):
                return "oauth-code"

            def tokendance_callback(self, *, code):
                return {"user": {"id": "td-user", "phone": "+8613800138000"}}

            def extract_or_create_api_key(self, *, name):
                return "td_sk_demo", {"id": "key-1", "key": "td_sk_demo"}

        FakeWorker().run(phone_provider=FakePhoneProvider(), captcha_solver=FakeCaptchaSolver())

        joined = "\n".join(messages)
        self.assertIn("Watcha 短信请求已提交", joined)
        self.assertIn("等待豪猪短信验证码", joined)
        self.assertIn("已收到短信验证码", joined)

    def test_map_result_preserves_api_key_and_phone_metadata(self):
        from platforms.tokendance.plugin import TokendancePlatform

        result = TokendancePlatform(RegisterConfig(executor_type="protocol"))._map_result({
            "email": "+8613800138000",
            "user_id": "td-user",
            "api_key": "td_sk_demo",
            "api_key_info": {"id": "key-1"},
            "phone": {"phone": "13800138000", "project_id": "108963"},
        })

        self.assertEqual(result.token, "td_sk_demo")
        self.assertEqual(result.extra["api_key"], "td_sk_demo")
        self.assertEqual(result.extra["ai_api_token"], "td_sk_demo")
        self.assertEqual(result.extra["phone"]["project_id"], "108963")


if __name__ == "__main__":
    unittest.main()
