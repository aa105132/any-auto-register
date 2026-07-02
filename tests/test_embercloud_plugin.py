from __future__ import annotations

import inspect
import json
import unittest
from unittest.mock import patch


def _ensure_platforms_importable() -> None:
    # 触发 platforms 包扫描，让 @register 生效。
    import core.registry as registry

    try:
        registry.load_all()
    except Exception:
        # 测试环境下部分平台依赖缺失不影响 embercloud 自身的断言。
        pass


class EmberCloudCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_frontend_api_and_clerk_constants_are_encoded_in_core(self):
        from platforms.embercloud import core

        self.assertEqual(core.CLERK_FRONTEND_BASE, "https://clerk.embercloud.ai")
        self.assertEqual(core.API_BASE, "https://api.embercloud.ai")
        self.assertEqual(core.MODELS_URL, "https://api.embercloud.ai/v1/models")
        self.assertEqual(core.SIGN_IN_URL, "https://www.embercloud.ai/sign-in")
        self.assertEqual(core.KEYS_DASHBOARD_URL, "https://www.embercloud.ai/dashboard/keys")
        # Clerk instance environment 抓取得到的 Turnstile sitekey。
        self.assertEqual(core.TURNSTILE_SITEKEY, "0x4AAAAAAAWXJGBD7bONzLBd")
        self.assertEqual(core.TURNSTILE_SITEKEY_INVISIBLE, "0x4AAAAAAAFV93qQdS0ycilX")
        self.assertEqual(core.CAPTCHA_PROVIDER, "turnstile")
        self.assertEqual(core.CAPTCHA_WIDGET_TYPE, "smart")
        # API key 前缀校验：实测 ek_live_，不是文档示例里的 ember_sk_。
        self.assertTrue(core.API_KEY_PATTERN.search("ek_live_hGEwR7pk_UpaBruXeIraxSkX4tAPJDb9qNPIlN81o"))
        self.assertFalse(core.API_KEY_PATTERN.search("ember_sk_xxx"))

    def test_extract_api_key_finds_ek_live_prefix_in_variants(self):
        from platforms.embercloud.core import _extract_api_key

        self.assertEqual(_extract_api_key({"api_key": "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"}), "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        self.assertEqual(_extract_api_key({"key": "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"}), "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        self.assertEqual(_extract_api_key({"data": {"raw_key": "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"}}), "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        self.assertEqual(_extract_api_key("blah ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789 done"), "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        self.assertEqual(_extract_api_key({"key_prefix": "ek_live_abc", "id": "key_1"}), "")

    def test_decode_jwt_payload_reads_clerk_session_fields(self):
        import base64

        from platforms.embercloud.core import decode_jwt_payload

        payload = {"sub": "user_123", "sid": "sess_abc", "id": "client_xyz", "rotating_token": "rot_1"}
        segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"hdr.{segment}.sig"
        decoded = decode_jwt_payload(token)
        self.assertEqual(decoded["sub"], "user_123")
        self.assertEqual(decoded["sid"], "sess_abc")
        self.assertEqual(decoded["rotating_token"], "rot_1")

    def test_client_builds_proxy_candidates_with_socks5h_fallback(self):
        from platforms.embercloud.core import EmberCloudClient

        client = EmberCloudClient(proxy="http://user:pass@host:8080", log_fn=lambda _msg: None)
        self.assertEqual(client._proxy_candidates[0], "http://user:pass@host:8080")
        self.assertEqual(client._proxy_candidates[1], "socks5h://user:pass@host:8080")

    def test_needs_captcha_retry_flags_clerk_security_text(self):
        from platforms.embercloud.core import EmberCloudClient

        self.assertTrue(EmberCloudClient._needs_captcha_retry({"errors": [{"code": "captcha_missing_token"}]}))
        self.assertTrue(EmberCloudClient._needs_captcha_retry({}, RuntimeError("failed security validations")))
        self.assertFalse(EmberCloudClient._needs_captcha_retry({"errors": [{"code": "form_param_format_invalid"}]}))


class EmberCloudBrowserRegistrarTests(unittest.TestCase):
    """浏览器 sign_up + 协议拿 key 混合架构断言。"""

    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_browser_registrar_drives_signup_then_protocol_key_fetch(self):
        from platforms.embercloud.browser_register import EmberCloudBrowserRegistrar

        source = inspect.getsource(EmberCloudBrowserRegistrar)
        # 浏览器 sign_up：填邮箱+密码+点 Continue，等 OTP 输入框，填 OTP，等 dashboard。
        self.assertIn("SIGN_UP_URL", source)
        self.assertIn("#emailAddress-field", source)
        self.assertIn("#password-field", source)
        self.assertIn('_click_continue', source)
        self.assertIn('_wait_for_otp_input', source)
        self.assertIn('_fill_otp', source)
        self.assertIn('_wait_for_dashboard', source)
        # 协议拿 key：注册完成后调 _KeyFetchWorker.fetch（credit 预热 + Server Action）。
        self.assertIn("_KeyFetchWorker", source)
        self.assertIn("auth_state", source)

    def test_browser_registrar_uses_otp_callback_for_imap_code(self):
        from platforms.embercloud.browser_register import EmberCloudBrowserRegistrar

        source = inspect.getsource(EmberCloudBrowserRegistrar.run)
        # otp_callback 由 ProtocolMailboxAdapter 注入（IMAP 收码，扫 INBOX+Junk），
        # 浏览器里收到 OTP 后填进 Clerk 的 OTP 输入框。
        self.assertIn("self._otp_callback", source)
        self.assertIn("_fill_otp", source)

    def test_key_fetch_worker_warms_credit_then_server_action_creates_key(self):
        from platforms.embercloud.browser_register import _KeyFetchWorker

        source = inspect.getsource(_KeyFetchWorker.fetch)
        # 实地验证：新用户 $1 credit 由 dashboard 首页服务端渲染时入账，不预热会导致
        # chat 接口 402。fetch 必须先 GET /dashboard 预热再创建 key。
        self.assertIn("DASHBOARD_URL", source)
        self.assertIn("KEYS_DASHBOARD_URL", source)
        self.assertLess(source.index("DASHBOARD_URL"), source.index("KEYS_DASHBOARD_URL"))
        # Server Action：Next-Action header + `["name"]` body。
        self.assertIn("Next-Action", source)
        self.assertIn("fullKey", source)


class EmberCloudProtocolKeyTests(unittest.TestCase):
    """协议拿 key 的 Server Action 解析断言（被 browser_register._KeyFetchWorker 复用）。"""

    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_server_action_regex_maps_create_api_key_name_to_id(self):
        from platforms.embercloud.protocol_mailbox import (
            DEFAULT_CREATE_API_KEY_ACTION_ID,
            SERVER_ACTION_RE,
            _extract_create_api_key_action_id,
        )

        # 实地抓包得到的 createApiKey action ID。
        self.assertEqual(DEFAULT_CREATE_API_KEY_ACTION_ID, "4085a688fe9ba267a0255d3c9f7e0ced3173698a77")
        # chunk 里的真实片段：createServerReference)("ID",...,"createApiKey")
        chunk = (
            'let x=(0,o.createServerReference)("4085a688fe9ba267a0255d3c9f7e0ced3173698a77",'
            'o.callServer,void 0,o.findSourceMapURL,"createApiKey"),'
            'm=(0,o.createServerReference)("40fd2d5643c727cec1477111d5abda257dc52e8d28",'
            'o.callServer,void 0,o.findSourceMapURL,"revokeApiKey")'
        )
        self.assertEqual(
            _extract_create_api_key_action_id(chunk),
            "4085a688fe9ba267a0255d3c9f7e0ced3173698a77",
        )
        # 正则按 action 名区分 create/revoke/rename。
        names = {m.group(2) for m in SERVER_ACTION_RE.finditer(chunk)}
        self.assertEqual(names, {"createApiKey", "revokeApiKey"})

    def test_extract_keys_page_chunks_filters_baseline_shared_chunks(self):
        from platforms.embercloud.protocol_mailbox import _extract_keys_page_chunks

        # 登录后 keys 页 HTML 同时含基线共享 chunk 和该页专属 chunk，提取时只留专属。
        html = (
            '<script src="/_next/static/chunks/0d61b6be3eaf0a6f.js"></script>'
            '<script src="/_next/static/chunks/2430c82f3a96a21c.js"></script>'
            '<script src="/_next/static/chunks/8927b669529c2341.js"></script>'
            '<script src="/_next/static/chunks/turbopack-15066a061a8e6115.js"></script>'
        )
        chunks = _extract_keys_page_chunks(html)
        self.assertIn("2430c82f3a96a21c", chunks)
        self.assertIn("8927b669529c2341", chunks)
        self.assertNotIn("0d61b6be3eaf0a6f", chunks)
        self.assertNotIn("turbopack-15066a061a8e6115", chunks)

    def test_key_fetch_worker_parses_full_key_from_rsc_stream(self):
        from platforms.embercloud.browser_register import _KeyFetchWorker
        from platforms.embercloud.protocol_mailbox import (
            DEFAULT_CREATE_API_KEY_ACTION_ID,
            DEFAULT_DEPLOYMENT_ID,
        )

        worker = _KeyFetchWorker(log_fn=lambda _msg: None)
        sess = worker._dashboard_session(
            {"client_cookie": "c", "session_cookie": "s", "access_token": "a"}
        )

        class FakeResponse:
            def __init__(self, status: int, text: str = "", url: str = ""):
                self.status_code = status
                self.text = text
                self.url = url or "https://www.embercloud.ai/dashboard/keys"

            @property
            def ok(self) -> bool:
                return 200 <= self.status_code < 300

        # dashboard 预热 + keys 页 + chunk 都用默认值；POST 创建 key 返回 RSC 流。
        rsc_body = (
            '0:{"a":"$@1","f":"","b":"el04d2W3qH22fOTugQ8_o","q":"","i":false}\n'
            '1:{"success":true,"fullKey":"ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",'
            '"key":{"id":"8da192ed","name":"auto-register","maskedKey":"ek_live_aBcDeFgHi_****"}}\n'
        )
        keys_html = '<script src="/_next/static/chunks/0d61b6be3eaf0a6f.js"></script>'  # 无专属 chunk → 用默认 action ID
        dashboard_html = '<script src="/x?dpl=dpl_CAtMvMugA2EUK6rRaektDyn1QnCe"></script>'

        def fake_get(url, **_kwargs):
            if url.endswith("/dashboard"):
                return FakeResponse(200, dashboard_html, url)
            if url.endswith("/dashboard/keys"):
                return FakeResponse(200, keys_html, url)
            return FakeResponse(200, "", url)

        captured: dict = {}

        def fake_post(url, *, headers, data, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            return FakeResponse(200, rsc_body, url)

        # fetch 内部会调 _dashboard_session 创建新 session；mock 它返回测试构造的 sess。
        with patch.object(worker, "_dashboard_session", return_value=sess), \
             patch.object(sess, "get", side_effect=fake_get), \
             patch.object(sess, "post", side_effect=fake_post):
            result = worker.fetch(
                {"client_cookie": "c", "session_cookie": "s", "access_token": "a"},
                name="auto-register",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["api_key"], "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        # 无专属 chunk 时回退默认 action ID。
        self.assertEqual(result["action_id"], DEFAULT_CREATE_API_KEY_ACTION_ID)
        # 请求形态：Next-Action header + `["name"]` body。
        self.assertEqual(captured["headers"]["Next-Action"], DEFAULT_CREATE_API_KEY_ACTION_ID)
        self.assertEqual(captured["headers"]["x-deployment-id"], DEFAULT_DEPLOYMENT_ID)
        self.assertEqual(captured["data"], json.dumps(["auto-register"]))


class EmberCloudPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_plugin_registered_and_defaults_to_outlook_token(self):
        from core.registry import get
        from platforms.embercloud.plugin import EmberCloudPlatform

        self.assertIs(get("embercloud"), EmberCloudPlatform)
        self.assertEqual(EmberCloudPlatform.default_mail_provider, "outlook_token")
        self.assertIn("protocol", EmberCloudPlatform.supported_executors)
        self.assertEqual(EmberCloudPlatform.supported_identity_modes, ["mailbox"])

    def test_map_result_promotes_ek_live_key_to_primary_token(self):
        from platforms.embercloud.plugin import EmberCloudPlatform

        result = EmberCloudPlatform()._map_result(
            {
                "email": "demo@embercloud.test",
                "api_key": "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",
                "api_key_source": "protocol",
                "access_token": "access-token",
                "user_id": "user_1",
            }
        )
        self.assertEqual(result.token, "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        self.assertEqual(result.extra["api_key"], "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        self.assertEqual(result.extra["ai_api_token"], "ek_live_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789")
        self.assertEqual(result.extra["native_api_base"], "https://api.embercloud.ai")
        self.assertEqual(result.extra["api_base"], "https://api.embercloud.ai")

    def test_build_platform_instance_uses_platform_default_mail_provider(self):
        import application.tasks as tasks
        from platforms.embercloud.plugin import EmberCloudPlatform

        class DummyLogger:
            def log(self, *_args, **_kwargs):
                pass

        captured = {}

        def fake_create_mailbox(provider, extra, proxy):
            captured["provider"] = provider
            captured["extra"] = dict(extra)
            captured["proxy"] = proxy
            return None

        with patch("core.base_mailbox.create_mailbox", fake_create_mailbox), patch.object(tasks, "get", return_value=EmberCloudPlatform):
            tasks._build_platform_instance(
                "embercloud",
                {
                    "executor_type": "protocol",
                    "captcha_solver": "auto",
                    "proxy": "",
                    "extra": {"identity_provider": "mailbox"},
                },
                DummyLogger(),
            )

        self.assertEqual(captured["provider"], "outlook_token")
        self.assertEqual(captured["extra"]["mail_provider"], "outlook_token")

    def test_protocol_mailbox_adapter_uses_protocol_worker_with_local_solver(self):
        from core.registration import ProtocolMailboxAdapter
        from platforms.embercloud.plugin import EmberCloudPlatform
        from platforms.embercloud.protocol_mailbox import EmberCloudProtocolMailboxWorker

        adapter = EmberCloudPlatform().build_protocol_mailbox_adapter()
        self.assertIsInstance(adapter, ProtocolMailboxAdapter)
        # 纯协议注册：Clerk sign_up + 本地 solver 解 Turnstile + 邮箱 OTP + 协议拿 key。
        # 实测浏览器内 Clerk managed Turnstile 自动化过不了，改走纯协议 + captcha_solver。
        self.assertTrue(adapter.use_captcha)
        # 注册前自动拉起本地 Turnstile solver 服务（local_solver 模式需要）。
        self.assertIsNotNone(adapter.preflight)
        # local_solver 优先于远程打码（无需外部 API key）。
        self.assertEqual(
            EmberCloudPlatform.protocol_captcha_order,
            ("local_solver", "yescaptcha", "2captcha"),
        )
        # OTP 规格保留（IMAP 收码，扫 INBOX + Junk）。
        self.assertEqual(adapter.otp_spec.code_pattern, r"(?<!\d)(\d{6})(?!\d)")
        self.assertEqual(adapter.otp_spec.keyword, "Ember")

        # worker_builder 构造的是纯协议 worker（不是浏览器 registrar）。
        class _Ctx:
            class identity:
                email = "x@y.com"
            password = "pw"
            proxy = None
            extra = {}
            log = lambda *a, **k: None
        worker = adapter.worker_builder(_Ctx(), type("A", (), {"otp_callback": None})())
        self.assertIsInstance(worker, EmberCloudProtocolMailboxWorker)


if __name__ == "__main__":
    unittest.main()
