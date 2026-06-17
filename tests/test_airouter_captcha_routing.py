import unittest

from core.base_platform import RegisterConfig
from platforms.airouter.plugin import AiRouterPlatform
from platforms.airouter.protocol_mailbox import AiRouterMailboxRegistrar


class AirouterCaptchaRoutingTests(unittest.TestCase):
    def test_cdp_protocol_auto_uses_cdp_turnstile(self):
        platform = AiRouterPlatform(RegisterConfig(executor_type="cdp_protocol", captcha_solver="auto", extra={}))
        self.assertEqual(platform._resolve_captcha_solver(), "cdp_turnstile")

        adapter = platform.build_protocol_mailbox_adapter()
        captured = {}

        class _Artifacts:
            otp_callback = lambda self: "123456"

        class _Ctx:
            proxy = "http://proxy.example:8080"
            extra = {}
            log = lambda self, _msg: None

        worker = adapter.worker_builder(_Ctx(), _Artifacts())
        captured["solver"] = worker.captcha_solver
        self.assertEqual(captured["solver"], "cdp_turnstile")

    def test_protocol_auto_keeps_protocol_solver_auto(self):
        platform = AiRouterPlatform(RegisterConfig(executor_type="protocol", captcha_solver="auto", extra={}))
        self.assertIn(platform._resolve_captcha_solver(), {"yescaptcha", "2captcha", "auto"})

    def test_harvest_cdp_solver_bypasses_yescaptcha_branch(self):
        worker = AiRouterMailboxRegistrar(captcha_solver="cdp_turnstile", log_fn=lambda _m: None)
        calls = []
        worker._harvest_turnstile_cdp = lambda **kwargs: calls.append(kwargs) or "cdp-token"

        token = worker._harvest_turnstile(email="a@example.com", password="pw", site_key="site")

        self.assertEqual(token, "cdp-token")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
