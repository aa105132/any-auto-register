from __future__ import annotations

import unittest


class VellumInventoryProviderKeyTests(unittest.TestCase):
    """多 mail_provider 来源（逗号分隔）时，_resolve_inventory_provider_key 应挑出
    支持 inventory 的 outlook_token/luckmail，否则 vellum 等平台明明有 outlook 池
    却不领邮箱、回退到全局配置（outlook_email 为空时报“Outlook 邮箱不能为空”）。
    """

    def _resolve(self, payload):
        from application.tasks import _resolve_inventory_provider_key
        return _resolve_inventory_provider_key(payload)

    def test_multi_provider_picks_outlook_token(self):
        self.assertEqual(
            self._resolve({"extra": {"mail_provider": "outlook_token,cfworker,yyds_mail"}}),
            "outlook_token",
        )

    def test_multi_provider_picks_luckmail(self):
        self.assertEqual(
            self._resolve({"extra": {"mail_provider": "cfworker,luckmail,yyds_mail"}}),
            "luckmail",
        )

    def test_multi_provider_without_inventory_returns_first(self):
        self.assertEqual(
            self._resolve({"extra": {"mail_provider": "cfworker,yyds_mail"}}),
            "cfworker",
        )

    def test_single_outlook_token(self):
        self.assertEqual(self._resolve({"extra": {"mail_provider": "outlook_token"}}), "outlook_token")

    def test_single_cfworker(self):
        self.assertEqual(self._resolve({"extra": {"mail_provider": "cfworker"}}), "cfworker")

    def test_empty(self):
        self.assertEqual(self._resolve({"extra": {}}), "")

    def test_explicit_inventory_provider_key_wins(self):
        self.assertEqual(
            self._resolve({
                "inventory_provider_key": "outlook_token",
                "extra": {"mail_provider": "cfworker,yyds_mail"},
            }),
            "outlook_token",
        )


class VellumResinRotateTests(unittest.TestCase):
    """Vellum 在 ctx.proxy 为空时必须启用内部 resin 轮换（resin_rotate=True），
    这样浏览器内部会多次轮换 resin session 找干净 IP，而不是依赖外层单个 resin。
    """

    def test_vellum_adapter_enables_resin_rotate_when_no_proxy(self):
        from core.base_platform import RegisterConfig
        from platforms.vellum.plugin import VellumPlatform

        platform = VellumPlatform(config=RegisterConfig(executor_type="headless"))
        adapter = platform.build_browser_registration_adapter()

        class FakeCtx:
            executor_type = "headless"
            proxy = None
            identity = type("I", (), {"email": "x@example.com"})()
            password = ""
            extra = {}
            log = staticmethod(lambda _m: None)

        worker = adapter.browser_worker_builder(FakeCtx(), type("A", (), {"otp_callback": None, "phone_callback": None})())
        self.assertTrue(worker.resin_rotate)

    def test_vellum_adapter_disables_resin_rotate_when_explicit_proxy(self):
        from core.base_platform import RegisterConfig
        from platforms.vellum.plugin import VellumPlatform

        platform = VellumPlatform(config=RegisterConfig(executor_type="headless"))
        adapter = platform.build_browser_registration_adapter()

        class FakeCtx:
            executor_type = "headless"
            proxy = "http://user:pass@webshare.io:80"
            identity = type("I", (), {"email": "x@example.com"})()
            password = ""
            extra = {}
            log = staticmethod(lambda _m: None)

        worker = adapter.browser_worker_builder(FakeCtx(), type("A", (), {"otp_callback": None, "phone_callback": None})())
        self.assertFalse(worker.resin_rotate)


if __name__ == "__main__":
    unittest.main()
