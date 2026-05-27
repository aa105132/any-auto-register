from __future__ import annotations

import inspect
import json
import unittest

from core.base_platform import AccountStatus, RegisterConfig
from core.registry import get, load_all


class JiekouPluginTests(unittest.TestCase):
    def test_registry_loads_jiekou_platform(self):
        load_all()
        cls = get("jiekou")
        self.assertEqual(cls.name, "jiekou")
        self.assertEqual(cls.display_name, "Jiekou AI")

    def test_capabilities_include_mailbox_and_google_oauth(self):
        from platforms.jiekou.plugin import JiekouPlatform

        platform = JiekouPlatform(RegisterConfig(executor_type="protocol"))
        self.assertIn("mailbox", platform.supported_identity_modes)
        self.assertIn("oauth_browser", platform.supported_identity_modes)
        self.assertIn("google", platform.supported_oauth_providers)
        self.assertIn("protocol", platform.supported_executors)
        self.assertIn("cdp_protocol", platform.supported_executors)

    def test_cdp_protocol_uses_local_turnstile_bridge(self):
        from platforms.jiekou.plugin import JiekouPlatform
        from platforms.jiekou import protocol_mailbox

        platform = JiekouPlatform(RegisterConfig(executor_type="cdp_protocol", captcha_solver="auto"))
        self.assertEqual(platform._resolve_captcha_solver(), "cdp_turnstile")
        source = inspect.getsource(protocol_mailbox.JiekouProtocolMailboxWorker)
        self.assertIn("use_cdp_bridge", source)
        self.assertIn("_solve_turnstile_cdp", source)
        self.assertIn("cdp_bootstrap", source)

    def test_prepare_password_replaces_known_weak_password(self):
        from platforms.jiekou.plugin import JiekouPlatform

        platform = JiekouPlatform(RegisterConfig(executor_type="protocol"))
        password = platform._prepare_registration_password("Phan9999")
        self.assertNotEqual(password, "Phan9999")
        self.assertGreaterEqual(len(password), 14)
        self.assertRegex(password, r"[a-z]")
        self.assertRegex(password, r"[A-Z]")
        self.assertRegex(password, r"\d")
        self.assertRegex(password, r"[^A-Za-z0-9]")

    def test_static_protocol_endpoints_match_frontend_bundle(self):
        from platforms.jiekou import protocol_mailbox

        self.assertEqual(protocol_mailbox.SITE_URL, "https://jiekou.ai")
        self.assertEqual(protocol_mailbox.CONTROL_API_BASE, "https://api-server.jiekou.ai")
        self.assertEqual(protocol_mailbox.LLM_API_BASE, "https://api.jiekou.ai")
        self.assertEqual(protocol_mailbox.OPENAI_API_BASE, "https://api.jiekou.ai/v1")
        self.assertEqual(protocol_mailbox.TURNSTILE_SITE_KEY, "0x4AAAAAAB1sNhmgzD9Pm-oE")
        self.assertEqual(protocol_mailbox.REGISTER_PATH, "/v1/user/register")
        self.assertEqual(protocol_mailbox.LOGIN_PATH, "/v1/user/login")
        self.assertEqual(protocol_mailbox.EMAIL_VERIFY_PATH, "/v1/user/email/verify")
        self.assertEqual(protocol_mailbox.QUESTIONNAIRE_PATH, "/v1/user/questionnaire")
        self.assertEqual(protocol_mailbox.API_KEYS_PATH, "/v2/user/key")

    def test_mailbox_worker_runs_full_reward_and_key_flow(self):
        from platforms.jiekou import protocol_mailbox

        source = inspect.getsource(protocol_mailbox.JiekouProtocolMailboxWorker.run)
        for name in (
            "_register_email_http",
            "_verify_email_http",
            "_login_email_http",
            "_submit_questionnaire_http",
            "_verify_voucher_reward",
            "_create_api_key_http",
            "_verify_api_key_http",
        ):
            self.assertIn(name, source)

    def test_questionnaire_defaults_include_runtime_required_name(self):
        from platforms.jiekou import protocol_mailbox

        payloads = protocol_mailbox._questionnaire_payloads()
        self.assertEqual(payloads[0], {"name": "Auto Register"})

    def test_openai_models_url_does_not_duplicate_v1(self):
        from platforms.jiekou import protocol_mailbox

        self.assertEqual(
            protocol_mailbox._llm_api_url(protocol_mailbox.MODELS_PATH),
            "https://api.jiekou.ai/openai/v1/models",
        )

    def test_questionnaire_submit_does_not_treat_validator_message_as_success(self):
        from platforms.jiekou import protocol_mailbox

        original = protocol_mailbox._request_json
        try:
            protocol_mailbox._request_json = lambda *args, **kwargs: {
                "ok": False,
                "status": 400,
                "data": {
                    "code": 400,
                    "reason": "VALIDATOR",
                    "message": "invalid UserQuestionnaireRequest.Name: value length must be between 1 and 128 runes, inclusive",
                },
            }
            result = protocol_mailbox._submit_questionnaire_http(object(), "demo-token")
            self.assertFalse(result["ok"])
            self.assertTrue(result["attempts"])
        finally:
            protocol_mailbox._request_json = original

    def test_voucher_detector_accepts_raw_cents_and_decimal_shapes(self):
        from platforms.jiekou import protocol_mailbox

        self.assertTrue(protocol_mailbox._has_usd_one_voucher({"voucherBalance": 10000}))
        self.assertTrue(protocol_mailbox._has_usd_one_voucher({"voucherBalance": "1.0000"}))
        self.assertTrue(protocol_mailbox._has_usd_one_voucher({"vouchers": [{"amountOff": "10000"}]}))
        self.assertFalse(protocol_mailbox._has_usd_one_voucher({"voucherBalance": 9999}))


    def test_voucher_amount_walker_ignores_circular_shapes(self):
        from platforms.jiekou import protocol_mailbox

        payload = {"voucherBalance": 10000}
        payload["self"] = payload
        self.assertTrue(protocol_mailbox._has_usd_one_voucher(payload))


    def test_api_key_verification_requires_real_chat_completion(self):
        from platforms.jiekou import protocol_mailbox

        calls = []

        class FakeResponse:
            ok = False
            status_code = 403
            url = "https://api.jiekou.ai/openai/v1/chat/completions"
            text = '{"code":403,"reason":"NOT_ENOUGH_BALANCE","message":"not enough balance"}'

            def json(self):
                return {"code": 403, "reason": "NOT_ENOUGH_BALANCE", "message": "not enough balance"}

        class FakeSession:
            def __init__(self):
                self.proxies = {}

            def post(self, url, headers=None, json=None, timeout=None):
                calls.append({"method": "POST", "url": url, "headers": headers, "json": json, "timeout": timeout})
                return FakeResponse()

        original_session = protocol_mailbox.requests.Session
        try:
            protocol_mailbox.requests.Session = FakeSession
            result = protocol_mailbox._verify_api_key_http("sk-demo")
        finally:
            protocol_mailbox.requests.Session = original_session

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "NOT_ENOUGH_BALANCE")
        self.assertEqual(calls[0]["method"], "POST")
        self.assertTrue(calls[0]["url"].endswith("/chat/completions"))
        self.assertEqual(calls[0]["json"]["model"], protocol_mailbox.API_VERIFICATION_MODEL)

    def test_result_mapping_requires_successful_api_chat_verification(self):
        from platforms.jiekou.plugin import JiekouPlatform

        platform = JiekouPlatform(RegisterConfig(executor_type="protocol"))
        result = platform._map_result({
            "email": "a@example.com",
            "api_key": "sk-jiekou-demo",
            "voucher_result": {"ok": True, "amount": 1.0},
            "api_verification": {"ok": False, "reason": "NOT_ENOUGH_BALANCE"},
        })

        self.assertEqual(result.status, AccountStatus.INVALID)
        self.assertEqual(result.extra["api_verification"]["reason"], "NOT_ENOUGH_BALANCE")

    def test_result_mapping_requires_api_key_and_verified_voucher(self):
        from platforms.jiekou.plugin import JiekouPlatform

        platform = JiekouPlatform(RegisterConfig(executor_type="protocol"))
        missing_voucher = platform._map_result({"email": "a@example.com", "api_key": "sk-jiekou-demo"})
        self.assertEqual(missing_voucher.status, AccountStatus.INVALID)

        result = platform._map_result({
            "email": "a@example.com",
            "password": "pw",
            "user": {"uuid": "u_1", "email": "a@example.com"},
            "api_key": "sk-jiekou-demo",
            "voucher_result": {"ok": True, "amount": 1.0},
            "api_verification": {"ok": True, "url": "https://api.jiekou.ai/openai/v1/chat/completions", "content": "pong"},
        })
        self.assertEqual(result.status, AccountStatus.REGISTERED)
        self.assertEqual(result.token, "sk-jiekou-demo")
        self.assertEqual(result.extra["api_key"], "sk-jiekou-demo")
        self.assertEqual(result.extra["ai_api_token"], "sk-jiekou-demo")
        self.assertEqual(result.extra["api_base"], "https://api.jiekou.ai/openai/v1")
        self.assertEqual(result.extra["legacy_api_base"], "https://api.jiekou.ai/v1")
        self.assertEqual(result.extra["llm_api_base"], "https://api.jiekou.ai/openai/v1")
        self.assertEqual(result.extra["control_api_base"], "https://api-server.jiekou.ai")
        self.assertEqual(result.extra["auth_header"], "Authorization")
        self.assertEqual(result.extra["auth_scheme"], "Bearer")

    def test_mapping_is_json_serializable(self):
        from platforms.jiekou.plugin import JiekouPlatform

        circular = {}
        circular["self"] = circular
        result = JiekouPlatform()._map_result({
            "email": "a@example.com",
            "api_key": "sk-jiekou-demo",
            "voucher_result": {"ok": True, "amount": 1.0, "raw": circular},
        })
        json.dumps(result.extra, ensure_ascii=False)
        self.assertEqual(result.extra["voucher_result"]["raw"]["self"], "<circular>")

    def test_token_extractor_does_not_treat_email_as_token(self):
        from platforms.jiekou import protocol_mailbox

        self.assertEqual(protocol_mailbox._extract_token({"metadata": {"0": "VeryLongUserName123@outlook.com"}}), "")
        self.assertEqual(protocol_mailbox._extract_token("VeryLongUserName123@outlook.com"), "")


    def test_token_extractor_accepts_jiekou_base64_session_token(self):
        from platforms.jiekou import protocol_mailbox

        token = "ZCmEZZRdA0OmVFvBKPwoxHMmjHr5dBDdlRl9xAGWZlwHZrRbImI0Miw9corToRvSUaQXmjt-WAD2dCRDAndrQQ=="
        self.assertEqual(protocol_mailbox._extract_token({"token": token}), token)
        self.assertEqual(protocol_mailbox._extract_token({"data": {"token": token}}), token)

    def test_verification_link_extractor_ignores_xhtml_dtd_decoys(self):
        from platforms.jiekou import protocol_mailbox

        html = """
        <!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
          "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
        <a href="https://api-server.jiekou.ai/v1/user/email/verify?token=abc123XYZ4567890&email=user%40example.com">Verify</a>
        """
        link = protocol_mailbox._extract_jiekou_verification_link(html)
        self.assertIn("api-server.jiekou.ai", link)
        self.assertIn("token=abc123XYZ4567890", link)
        self.assertNotIn("w3.org", link)

        from core.base_mailbox import _extract_verification_link

        generic_link = _extract_verification_link(html, "")
        self.assertIn("api-server.jiekou.ai", generic_link)
        self.assertNotIn("w3.org", generic_link)


    def test_generic_verification_link_extractor_ignores_static_assets(self):
        from core.base_mailbox import _extract_verification_link
        from platforms.jiekou import protocol_mailbox

        html = """
        <html><body>
          <img src="https://jiekou.ai/logo/jiekou-logo.png">
          <a href="https://jiekou.ai/user/email/verify?token=abc123XYZ4567890&email=user%40example.com">Verify</a>
        </body></html>
        """

        generic_link = _extract_verification_link(html, "")
        jiekou_link = protocol_mailbox._extract_jiekou_verification_link(html)
        self.assertIn("email/verify", generic_link)
        self.assertIn("token=abc123XYZ4567890", generic_link)
        self.assertNotIn("logo", generic_link)
        self.assertIn("email/verify", jiekou_link)
        self.assertNotIn("logo", jiekou_link)

    def test_cdp_turnstile_bootstrap_imports_browser_cookies_and_user_agent(self):
        from platforms.jiekou import protocol_mailbox

        class FakeSolver:
            def solve_turnstile(self, page_url, site_key):
                return {
                    "token": "ts-token",
                    "cookies": {"cf_clearance": "cf-token", "__cf_bm": "bm-token"},
                    "user_agent": "UA-from-cdp",
                    "mode": "cdp_protocol",
                }

        worker = protocol_mailbox.JiekouProtocolMailboxWorker(
            proxy=None,
            log_fn=lambda _msg: None,
            use_cdp_bridge=True,
        )
        token, bootstrap = worker._solve_turnstile(FakeSolver(), protocol_mailbox.REGISTER_URL)

        self.assertEqual(token, "ts-token")
        self.assertEqual(bootstrap["mode"], "cdp_protocol")
        self.assertEqual(protocol_mailbox._session_cookie_map(worker.session).get("cf_clearance"), "cf-token")
        self.assertEqual(worker.session.headers.get("User-Agent"), "UA-from-cdp")


    def test_cdp_solver_filters_cookies_for_target_domain(self):
        from core.base_captcha import CdpTurnstileSolver

        class FakeContext:
            def cookies(self):
                return [
                    {"name": "cf_clearance", "value": "cf", "domain": ".jiekou.ai"},
                    {"name": "other", "value": "skip", "domain": ".example.com"},
                ]

        cookies = CdpTurnstileSolver._cookie_dict_for_url(FakeContext(), "https://jiekou.ai/user/register")
        self.assertEqual(cookies, {"cf_clearance": "cf"})

    def test_oauth_uses_jiekou_google_url_and_waits_for_token_cookie(self):
        from platforms.jiekou import browser_oauth

        self.assertIn("674520143921", browser_oauth.GOOGLE_CLIENT_ID)
        self.assertEqual(browser_oauth.OAUTH_CALLBACK_PATH, "/api/auth")
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("drive_google_oauth", source)
        self.assertIn("_wait_for_browser_token", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_verify_voucher_reward", source)

    def test_task_success_auto_exports_jiekou_key(self):
        import application.tasks as tasks

        self.assertTrue(hasattr(tasks, "_auto_export_jiekou_key"))
        source = inspect.getsource(tasks)
        self.assertIn("_auto_export_jiekou_key(logger, account)", source)


if __name__ == "__main__":
    unittest.main()
