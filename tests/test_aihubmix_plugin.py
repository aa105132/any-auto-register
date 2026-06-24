from __future__ import annotations

import base64
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
        # 测试环境下部分平台依赖缺失不影响 aihubmix 自身的断言。
        pass


class AIHubMixCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_clerk_and_api_constants_are_encoded_in_core(self):
        from platforms.aihubmix import core

        # Clerk Frontend API base（实地 /v1/environment 抓取确认）。
        self.assertEqual(core.CLERK_FRONTEND_BASE, "https://clerk.aihubmix.com")
        self.assertEqual(core.CLERK_API_VERSION, "2025-11-10")
        self.assertEqual(core.CLERK_JS_VERSION, "5.125.13")
        # OpenAI 兼容 API base（/v1/chat/completions + /v1/models）。
        self.assertEqual(core.API_BASE, "https://aihubmix.com/v1")
        self.assertEqual(core.MODELS_URL, "https://aihubmix.com/v1/models")
        # 注册入口与控制台。
        self.assertEqual(core.SIGN_UP_URL, "https://console.aihubmix.com/sign-up")
        self.assertEqual(core.SIGN_IN_URL, "https://console.aihubmix.com/sign-in")
        self.assertEqual(core.CONSOLE_URL, "https://console.aihubmix.com")
        self.assertEqual(core.KEYS_DASHBOARD_URL, "https://console.aihubmix.com/token")
        # Clerk instance environment 抓取得到的 Turnstile sitekey。
        self.assertEqual(core.TURNSTILE_SITEKEY, "0x4AAAAAAAWXJGBD7bONzLBd")
        self.assertEqual(core.TURNSTILE_SITEKEY_INVISIBLE, "0x4AAAAAAAFV93qQdS0ycilX")
        self.assertEqual(core.CAPTCHA_PROVIDER, "turnstile")
        self.assertEqual(core.CAPTCHA_WIDGET_TYPE, "smart")
        # API key 前缀校验：实测 sk-（OpenAI 兼容），不是 ek_live_。
        self.assertTrue(core.API_KEY_PATTERN.search("sk-abcdefghij1234567890uvwxyz"))
        self.assertFalse(core.API_KEY_PATTERN.search("ek_live_xxx"))
        self.assertFalse(core.API_KEY_PATTERN.search("sk-short"))

    def test_extract_api_key_finds_sk_prefix_in_variants(self):
        from platforms.aihubmix.core import _extract_api_key

        self.assertEqual(_extract_api_key({"api_key": "sk-abcdefghij1234567890uvwxyz"}), "sk-abcdefghij1234567890uvwxyz")
        self.assertEqual(_extract_api_key({"key": "sk-abcdefghij1234567890uvwxyz"}), "sk-abcdefghij1234567890uvwxyz")
        self.assertEqual(_extract_api_key({"data": {"raw_key": "sk-abcdefghij1234567890uvwxyz"}}), "sk-abcdefghij1234567890uvwxyz")
        self.assertEqual(_extract_api_key("blah sk-abcdefghij1234567890uvwxyz done"), "sk-abcdefghij1234567890uvwxyz")
        # fullKey 是 Next.js Server Action RSC 流里的字段名。
        self.assertEqual(_extract_api_key({"fullKey": "sk-abcdefghij1234567890uvwxyz"}), "sk-abcdefghij1234567890uvwxyz")
        self.assertEqual(_extract_api_key({"key_prefix": "sk-abc", "id": "key_1"}), "")

    def test_decode_jwt_payload_reads_clerk_session_fields(self):
        from platforms.aihubmix.core import decode_jwt_payload

        payload = {"sub": "user_123", "sid": "sess_abc", "id": "client_xyz", "rotating_token": "rot_1"}
        segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        token = f"hdr.{segment}.sig"
        decoded = decode_jwt_payload(token)
        self.assertEqual(decoded["sub"], "user_123")
        self.assertEqual(decoded["sid"], "sess_abc")
        self.assertEqual(decoded["rotating_token"], "rot_1")

    def test_client_builds_proxy_candidates_with_socks5h_fallback(self):
        from platforms.aihubmix.core import AIHubMixClient

        client = AIHubMixClient(proxy="http://user:pass@host:8080", log_fn=lambda _msg: None)
        self.assertEqual(client._proxy_candidates[0], "http://user:pass@host:8080")
        self.assertEqual(client._proxy_candidates[1], "socks5h://user:pass@host:8080")

    def test_needs_captcha_retry_flags_clerk_security_text(self):
        from platforms.aihubmix.core import AIHubMixClient

        self.assertTrue(AIHubMixClient._needs_captcha_retry({"errors": [{"code": "captcha_missing_token"}]}))
        self.assertTrue(AIHubMixClient._needs_captcha_retry({}, RuntimeError("failed security validations")))
        self.assertFalse(AIHubMixClient._needs_captcha_retry({"errors": [{"code": "form_param_format_invalid"}]}))


class AIHubMixProtocolRegisterTests(unittest.TestCase):
    """协议注册链路与 Server Action 解析断言。"""

    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_server_action_regex_maps_create_api_key_name_to_id(self):
        from platforms.aihubmix.protocol_register import SERVER_ACTION_RE, _extract_create_api_key_action_id

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

    def test_extract_deployment_id_from_html(self):
        from platforms.aihubmix.protocol_register import _extract_deployment_id

        html = '<script src="/x?dpl=dpl_AbCdEf123456"></script>'
        self.assertEqual(_extract_deployment_id(html), "dpl_AbCdEf123456")
        # 无 dpl_ 时回退默认（空字符串，强制动态提取失败时由浏览器兜底）。
        self.assertEqual(_extract_deployment_id("<html></html>"), "")

    def test_extract_keys_page_chunks_dedupes(self):
        from platforms.aihubmix.protocol_register import _extract_keys_page_chunks

        html = (
            '<script src="/_next/static/chunks/aaaa1111.js"></script>'
            '<script src="/_next/static/chunks/bbbb2222.js"></script>'
            '<script src="/_next/static/chunks/aaaa1111.js"></script>'
        )
        chunks = _extract_keys_page_chunks(html)
        self.assertEqual(chunks, ["aaaa1111", "bbbb2222"])

    def test_protocol_register_run_drives_clerk_signup_chain(self):
        from platforms.aihubmix.protocol_register import AIHubMixProtocolRegister

        source = inspect.getsource(AIHubMixProtocolRegister.run)
        # Clerk 注册 4 步链路：init → sign_up → prepare → attempt → session token → collect auth → key fetch
        self.assertIn("init_clerk_client", source)
        self.assertIn("_create_sign_up_with_captcha", source)
        self.assertIn("prepare_email_verification", source)
        self.assertIn("attempt_email_verification", source)
        self.assertIn("create_session_token", source)
        self.assertIn("collect_auth_state", source)
        self.assertIn("_KeyFetchWorker", source)

    def test_key_fetch_worker_warms_console_then_server_action_creates_key(self):
        from platforms.aihubmix.protocol_register import _KeyFetchWorker

        source = inspect.getsource(_KeyFetchWorker.fetch)
        # 预热 console 首页（提取 deployment_id）→ 提取 chunk / action ID → POST Server Action。
        self.assertIn("DASHBOARD_URL", source)
        self.assertIn("KEYS_DASHBOARD_URL", source)
        self.assertLess(source.index("DASHBOARD_URL"), source.index("KEYS_DASHBOARD_URL"))
        # Server Action：Next-Action header + `["name"]` body。
        self.assertIn("Next-Action", source)
        self.assertIn("fullKey", source)
        self.assertIn("createApiKey", inspect.getsource(_KeyFetchWorker))


class AIHubMixBrowserRegistrarTests(unittest.TestCase):
    """浏览器 sign_up + 协议/浏览器拿 key 混合架构断言。"""

    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_browser_registrar_drives_signup_then_key_fetch(self):
        from platforms.aihubmix.browser_register import AIHubMixBrowserRegistrar

        source = inspect.getsource(AIHubMixBrowserRegistrar)
        # 浏览器 sign_up：填邮箱+密码+点 Continue，等 OTP 输入框，填 OTP，等 console landing。
        self.assertIn("SIGN_UP_PAGE_URL", source)
        self.assertIn("#emailAddress-field", source)
        self.assertIn("#password-field", source)
        self.assertIn("_click_continue", source)
        self.assertIn("_click_turnstile_until_token", source)
        self.assertIn("_wait_for_otp_input", source)
        self.assertIn("_fill_otp", source)
        self.assertIn("_wait_for_console_landing", source)
        # 协议拿 key：注册完成后调 _KeyFetchWorker.fetch，失败回退 _fetch_key_via_browser。
        self.assertIn("_KeyFetchWorker", source)
        self.assertIn("_fetch_key_via_browser", source)
        self.assertIn("auth_state", source)

    def test_browser_registrar_uses_otp_callback_for_imap_code(self):
        from platforms.aihubmix.browser_register import AIHubMixBrowserRegistrar

        source = inspect.getsource(AIHubMixBrowserRegistrar.run)
        # otp_callback 由 ProtocolMailboxAdapter 注入（IMAP 收码，扫 INBOX+Junk），
        # 浏览器里收到 OTP 后填进 Clerk 的 OTP 输入框。
        self.assertIn("self._otp_callback", source)
        self.assertIn("_fill_otp", source)

    def test_fetch_key_via_browser_falls_back_to_dom_when_protocol_fails(self):
        from platforms.aihubmix.browser_register import _fetch_key_via_browser

        source = inspect.getsource(_fetch_key_via_browser)
        # 浏览器 DOM 兜底：导航到 /token，点 Create/Add Key，从网络/DOM 读 sk-。
        self.assertIn("KEYS_DASHBOARD_URL", source)
        self.assertIn("Create Key", source)
        self.assertIn("_extract_api_key", source)


class AIHubMixOAuthTests(unittest.TestCase):
    """Google OAuth 浏览器流程断言。"""

    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_browser_oauth_drives_clerk_google_then_landing(self):
        from platforms.aihubmix.browser_oauth import register_with_browser_oauth

        source = inspect.getsource(register_with_browser_oauth)
        # OAuth 链路：打开 sign-up → 点 Continue with Google → drive_google_oauth → 等落地 → 拿 key。
        self.assertIn("SIGN_UP_URL", source)
        self.assertIn("try_click_provider_on_page", source)
        self.assertIn("drive_google_oauth", source)
        self.assertIn("_landed_on_console", source)
        self.assertIn("_KeyFetchWorker", source)

    def test_is_real_console_landing_excludes_transit_and_google(self):
        from platforms.aihubmix.browser_oauth import _is_real_console_landing

        # 真实落地页。
        self.assertTrue(_is_real_console_landing("https://console.aihubmix.com/token"))
        self.assertTrue(_is_real_console_landing("https://console.aihubmix.com/dashboard"))
        # 排除中转/登录/注册页。
        self.assertFalse(_is_real_console_landing("https://console.aihubmix.com/sign-up"))
        self.assertFalse(_is_real_console_landing("https://console.aihubmix.com/sign-in"))
        # 排除 Clerk / Google。
        self.assertFalse(_is_real_console_landing("https://clerk.aihubmix.com/v1/client/sign_ups/abc"))
        self.assertFalse(_is_real_console_landing("https://accounts.google.com/o/oauth2/auth"))
        # 排除非 aihubmix 域。
        self.assertFalse(_is_real_console_landing("https://example.com/"))


class AIHubMixPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_platforms_importable()

    def test_plugin_registered_and_advertises_capabilities(self):
        from core.registry import get
        from platforms.aihubmix.plugin import AIHubMixPlatform

        self.assertIs(get("aihubmix"), AIHubMixPlatform)
        self.assertEqual(AIHubMixPlatform.display_name, "AIHubMix")
        self.assertEqual(AIHubMixPlatform.default_mail_provider, "outlook_token")
        # 支持协议 + 浏览器 + CDP 混合三种执行器。
        self.assertIn("protocol", AIHubMixPlatform.supported_executors)
        self.assertIn("cdp_protocol", AIHubMixPlatform.supported_executors)
        self.assertIn("headless", AIHubMixPlatform.supported_executors)
        self.assertIn("headed", AIHubMixPlatform.supported_executors)
        # 支持 mailbox + oauth_browser（Google OAuth）两种身份模式。
        self.assertEqual(AIHubMixPlatform.supported_identity_modes, ["mailbox", "oauth_browser"])
        self.assertEqual(AIHubMixPlatform.supported_oauth_providers, ["google"])

    def test_map_result_promotes_sk_key_to_primary_token(self):
        from platforms.aihubmix.plugin import AIHubMixPlatform

        result = AIHubMixPlatform()._map_result(
            {
                "email": "demo@aihubmix.test",
                "api_key": "sk-abcdefghij1234567890uvwxyz",
                "api_key_source": "protocol",
                "access_token": "access-token",
                "user_id": "user_1",
            }
        )
        self.assertEqual(result.token, "sk-abcdefghij1234567890uvwxyz")
        self.assertEqual(result.extra["api_key"], "sk-abcdefghij1234567890uvwxyz")
        self.assertEqual(result.extra["ai_api_token"], "sk-abcdefghij1234567890uvwxyz")
        self.assertEqual(result.extra["native_api_base"], "https://aihubmix.com/v1")
        self.assertEqual(result.extra["api_base"], "https://aihubmix.com/v1")
        self.assertEqual(result.extra["auth_header"], "Authorization")
        self.assertEqual(result.extra["auth_scheme"], "Bearer sk-...")

    def test_build_platform_instance_uses_platform_default_mail_provider(self):
        import application.tasks as tasks
        from platforms.aihubmix.plugin import AIHubMixPlatform

        class DummyLogger:
            def log(self, *_args, **_kwargs):
                pass

        captured = {}

        def fake_create_mailbox(provider, extra, proxy):
            captured["provider"] = provider
            captured["extra"] = dict(extra)
            captured["proxy"] = proxy
            return None

        with patch("core.base_mailbox.create_mailbox", fake_create_mailbox), patch.object(tasks, "get", return_value=AIHubMixPlatform):
            tasks._build_platform_instance(
                "aihubmix",
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

    def test_protocol_mailbox_adapter_enables_captcha_and_otp_spec(self):
        from core.registration import ProtocolMailboxAdapter
        from platforms.aihubmix.plugin import AIHubMixPlatform

        adapter = AIHubMixPlatform().build_protocol_mailbox_adapter()
        self.assertIsInstance(adapter, ProtocolMailboxAdapter)
        # Clerk smart captcha 需打码（protocol 路径用远程打码拿 Turnstile token）。
        self.assertTrue(adapter.use_captcha)
        # OTP 规格保留（IMAP 收码填进 Clerk）。
        self.assertEqual(adapter.otp_spec.code_pattern, r"(?<!\d)(\d{6})(?!\d)")
        # keyword 留空：Clerk 验证邮件主题/正文可能不含 "AIHubMix" 字样，
        # 用空 keyword 匹配所有邮件，靠 6 位数字 pattern 提取验证码。
        self.assertEqual(adapter.otp_spec.keyword, "")

    def test_protocol_oauth_adapter_wires_run_oauth(self):
        from core.registration import ProtocolOAuthAdapter
        from platforms.aihubmix.plugin import AIHubMixPlatform

        adapter = AIHubMixPlatform().build_protocol_oauth_adapter()
        self.assertIsInstance(adapter, ProtocolOAuthAdapter)
        self.assertIsNotNone(adapter.oauth_runner)

    def test_browser_registration_adapter_allows_headed_headless_cdp(self):
        from core.registration import BrowserRegistrationAdapter
        from platforms.aihubmix.plugin import AIHubMixPlatform

        adapter = AIHubMixPlatform().build_browser_registration_adapter()
        self.assertIsInstance(adapter, BrowserRegistrationAdapter)
        self.assertEqual(
            adapter.capability.oauth_allowed_executor_types,
            ("headed", "headless", "cdp_protocol"),
        )
        self.assertIsNotNone(adapter.oauth_runner)
        self.assertIsNotNone(adapter.browser_worker_builder)

    def test_should_use_browser_flow_routes_oauth_browser_to_browser_for_cdp(self):
        from platforms.aihubmix.plugin import AIHubMixPlatform

        class Identity:
            identity_provider = "oauth_browser"

        # oauth_browser + cdp_protocol → 浏览器 adapter（与 vellum 一致）。
        for executor in ("headless", "headed", "cdp_protocol"):
            cfg = type("Cfg", (), {"executor_type": executor})()
            self.assertTrue(AIHubMixPlatform(cfg)._should_use_browser_registration_flow(Identity()))
        # oauth_browser + protocol → 走 ProtocolOAuthFlow（非浏览器）。
        cfg = type("Cfg", (), {"executor_type": "protocol"})()
        self.assertFalse(AIHubMixPlatform(cfg)._should_use_browser_registration_flow(Identity()))

        class MailboxIdentity:
            identity_provider = "mailbox"

        # mailbox + headless/headed → 浏览器流程；protocol → 协议邮箱流程。
        for executor in ("headless", "headed"):
            cfg = type("Cfg", (), {"executor_type": executor})()
            self.assertTrue(AIHubMixPlatform(cfg)._should_use_browser_registration_flow(MailboxIdentity()))
        cfg = type("Cfg", (), {"executor_type": "protocol"})()
        self.assertFalse(AIHubMixPlatform(cfg)._should_use_browser_registration_flow(MailboxIdentity()))


if __name__ == "__main__":
    unittest.main()
