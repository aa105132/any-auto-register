from __future__ import annotations

import inspect
import json
import unittest

from core.base_platform import AccountStatus, RegisterConfig
from core.registry import get, load_all


class HpcAiPluginTests(unittest.TestCase):
    def test_registry_loads_hpcai_platform(self):
        load_all()
        cls = get("hpcai")
        self.assertEqual(cls.name, "hpcai")
        self.assertEqual(cls.display_name, "HPC-AI")

    def test_capabilities_include_protocol_mailbox_and_cdp_protocol(self):
        from platforms.hpcai.plugin import HpcAiPlatform

        platform = HpcAiPlatform(RegisterConfig(executor_type="protocol"))
        self.assertIn("mailbox", platform.supported_identity_modes)
        self.assertIn("protocol", platform.supported_executors)
        self.assertIn("cdp_protocol", platform.supported_executors)

    def test_cdp_protocol_uses_local_turnstile_bridge(self):
        from platforms.hpcai.plugin import HpcAiPlatform
        from platforms.hpcai import protocol_mailbox

        platform = HpcAiPlatform(RegisterConfig(executor_type="cdp_protocol", captcha_solver="auto"))
        self.assertEqual(platform._resolve_captcha_solver(), "cdp_turnstile")
        source = inspect.getsource(protocol_mailbox.HpcAiProtocolMailboxWorker)
        self.assertIn("use_cdp_bridge", source)
        self.assertIn("_solve_turnstile_cdp", source)
        self.assertIn("cdp_bootstrap", source)

    def test_static_protocol_endpoints_match_frontend_bundle(self):
        from platforms.hpcai import protocol_mailbox

        self.assertEqual(protocol_mailbox.SITE_URL, "https://www.hpc-ai.com")
        self.assertEqual(protocol_mailbox.SIGNUP_URL, "https://www.hpc-ai.com/account/signup")
        self.assertEqual(protocol_mailbox.OPENAI_COMPAT_API_BASE, "https://api.hpc-ai.com/inference/v1")
        self.assertEqual(protocol_mailbox.TURNSTILE_SITE_KEY, "0x4AAAAAAC_4lIrK2LRHBJfe")
        self.assertEqual(protocol_mailbox.OTP_PATH, "/api/user/otp")
        self.assertEqual(protocol_mailbox.REGISTER_PATH, "/api/user/register")
        self.assertEqual(protocol_mailbox.LOGIN_PATH, "/api/user/login")
        self.assertEqual(protocol_mailbox.API_KEY_CREATE_PATH, "/api/user/maas/key/create")
        self.assertEqual(protocol_mailbox.API_KEY_LIST_PATH, "/api/user/maas/key/list")
        self.assertEqual(protocol_mailbox.WELCOME_VOUCHER_CHECK_PATH, "/api/voucher/maas/welcome/check")
        self.assertEqual(protocol_mailbox.WELCOME_VOUCHER_CLAIM_PATH, "/api/voucher/maas/welcome/claim")

    def test_mailbox_worker_runs_full_credit_and_key_flow(self):
        from platforms.hpcai import protocol_mailbox

        source = inspect.getsource(protocol_mailbox.HpcAiProtocolMailboxWorker.run)
        for name in (
            "_send_register_otp_http",
            "_register_email_http",
            "_login_email_http",
            "_claim_welcome_voucher_http",
            "_verify_credit_reward",
            "_create_api_key_http",
            "_verify_api_key_http",
        ):
            self.assertIn(name, source)

    def test_cdp_turnstile_bootstrap_imports_browser_cookies_and_user_agent(self):
        from platforms.hpcai import protocol_mailbox

        class FakeSolver:
            def solve_turnstile_with_session(self, page_url, site_key):
                return {
                    "token": "ts-token",
                    "cookies": {"cf_clearance": "cf-token", "__cf_bm": "bm-token"},
                    "user_agent": "UA-from-cdp",
                    "mode": "cdp_protocol",
                }

        worker = protocol_mailbox.HpcAiProtocolMailboxWorker(
            proxy=None,
            log_fn=lambda _msg: None,
            use_cdp_bridge=True,
        )
        token, bootstrap = worker._solve_turnstile(FakeSolver(), protocol_mailbox.SIGNUP_URL)

        self.assertEqual(token, "ts-token")
        self.assertEqual(bootstrap["mode"], "cdp_protocol")
        self.assertEqual(protocol_mailbox._session_cookie_map(worker.session).get("cf_clearance"), "cf-token")
        self.assertEqual(worker.session.headers.get("User-Agent"), "UA-from-cdp")

    def test_credit_detector_accepts_two_dollar_shapes(self):
        from platforms.hpcai import protocol_mailbox

        self.assertTrue(protocol_mailbox._has_minimum_credit({"availableCreditAmount": 2}, 2.0))
        self.assertTrue(protocol_mailbox._has_minimum_credit({"availableVoucherAmount": "2.0000"}, 2.0))
        self.assertTrue(protocol_mailbox._has_minimum_credit({"amount": 20000}, 2.0))
        self.assertFalse(protocol_mailbox._has_minimum_credit({"availableBalance": 1.99}, 2.0))

    def test_api_key_extractor_prefers_full_key_over_key_id(self):
        from platforms.hpcai import protocol_mailbox

        payload = {
            "key": {
                "id": "d3b2d77d-0000-4000-8000-e7064e97cac3",
                "name": "auto-register",
                "fullKey": "sk-6e841-demo-secret-c40e02",
                "lastFour": "0e02",
            }
        }
        self.assertEqual(protocol_mailbox._find_api_key(payload), "sk-6e841-demo-secret-c40e02")

    def test_result_mapping_requires_api_key_credit_and_verified_api_call(self):
        from platforms.hpcai.plugin import HpcAiPlatform

        platform = HpcAiPlatform(RegisterConfig(executor_type="protocol"))
        missing_credit = platform._map_result({
            "email": "a@example.com",
            "api_key": "sk-hpc-demo",
            "credit_result": {"ok": False, "amount": 0},
            "api_verification": {"ok": True},
        })
        self.assertEqual(missing_credit.status, AccountStatus.INVALID)

        result = platform._map_result({
            "email": "a@example.com",
            "password": "pw",
            "user": {"userId": "u_1", "email": "a@example.com"},
            "api_key": "sk-hpc-demo",
            "credit_result": {"ok": True, "amount": 2.0},
            "api_verification": {"ok": True, "url": "https://api.hpc-ai.com/inference/v1/models"},
        })
        self.assertEqual(result.status, AccountStatus.REGISTERED)
        self.assertEqual(result.token, "sk-hpc-demo")
        self.assertEqual(result.extra["api_key"], "sk-hpc-demo")
        self.assertEqual(result.extra["ai_api_token"], "sk-hpc-demo")
        self.assertEqual(result.extra["api_base"], "https://api.hpc-ai.com/inference/v1")
        self.assertEqual(result.extra["auth_header"], "Authorization")
        self.assertEqual(result.extra["auth_scheme"], "Bearer")

    def test_mapping_is_json_serializable(self):
        from platforms.hpcai.plugin import HpcAiPlatform

        circular = {}
        circular["self"] = circular
        result = HpcAiPlatform()._map_result({
            "email": "a@example.com",
            "api_key": "sk-hpc-demo",
            "credit_result": {"ok": True, "amount": 2.0, "raw": circular},
            "api_verification": {"ok": True},
        })
        json.dumps(result.extra, ensure_ascii=False)
        self.assertEqual(result.extra["credit_result"]["raw"]["self"], "<circular>")


if __name__ == "__main__":
    unittest.main()
