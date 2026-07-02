from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


class AnyCapRegistrationTests(unittest.TestCase):
    def test_frontend_discovered_routes_are_encoded_in_oauth_worker(self):
        from platforms.anycap import browser_oauth

        self.assertEqual(browser_oauth.API_BASE, "https://api.anycap.ai")
        self.assertEqual(browser_oauth.ACCESS_TOKEN_URL, "https://anycap.ai/auth/access-token")
        self.assertEqual(browser_oauth.API_KEYS_URL, "https://api.anycap.ai/v1/api-keys")
        self.assertIn("/api/auth/login", browser_oauth.LOGIN_URL)

    def test_plugin_maps_created_api_key_as_primary_token(self):
        from platforms.anycap.plugin import AnyCapPlatform

        result = AnyCapPlatform()._map_result({
            "email": "demo@anycap.test",
            "api_key": "ak_demo_anycap_key_123456",
            "access_token": "access-token",
            "api_key_info": {"id": "key_1"},
        })
        self.assertEqual(result.token, "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["api_key"], "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["ai_api_token"], "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["native_api_base"], "https://api.anycap.ai")

    def test_oauth_worker_uses_browser_token_then_protocol_key_create(self):
        from platforms.anycap import browser_oauth

        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_get_access_token_http", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_verify_api_key_http", source)
    def test_mailbox_flow_blacklists_auth0_signup_blocked_domain(self):
        import inspect
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        source = inspect.getsource(AnyCapMailboxRegistrar)
        self.assertIn("add_mailbox_domain_blacklist", source)
        self.assertIn('platform="anycap"', source)
        self.assertIn("too many signup attempts", source)
        self.assertIn("please try again later", source)
        self.assertIn("domain is not allowed", source)

    def test_signup_block_detector_normalizes_auth0_limit_text(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def evaluate(self, _script):
                return "Too many signup attempts.\nPlease try again later"

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        self.assertEqual(
            registrar._detect_signup_block_reason(page=FakePage()),
            "anycap_signup_attempts_limited",
        )

    def test_protocol_mailbox_adapter_enables_captcha_and_passes_solver(self):
        from core.base_platform import RegisterConfig
        from platforms.anycap.plugin import AnyCapPlatform

        platform = AnyCapPlatform(config=RegisterConfig(executor_type="cdp_protocol", extra={}))
        adapter = platform.build_protocol_mailbox_adapter()
        self.assertTrue(adapter.use_captcha)

        ctx = SimpleNamespace(proxy=None, extra={}, log=lambda _m: None)
        artifacts = SimpleNamespace(captcha_solver="SENTINEL_SOLVER", otp_callback=lambda: "123456")
        worker = adapter.worker_builder(ctx, artifacts)
        self.assertEqual(worker.captcha_solver, "SENTINEL_SOLVER")

    def test_run_uses_solver_injection_with_click_fallback(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        source = inspect.getsource(AnyCapMailboxRegistrar.run)
        self.assertIn("_solve_turnstile_via_solver", source)
        self.assertIn("self.captcha_solver is not None", source)
        # 无 solver / 打码失败必须回退浏览器点击，不能直接抛错中断
        self.assertIn("_click_turnstile_until_token", source)
        # 注入 token 后必须检查 Continue 按钮启用；disabled 时清空注入值回退点击
        self.assertIn("_submit_button_enabled", source)
        self.assertIn("_clear_turnstile_field", source)
        # solver 走子进程隔离独立 launch Chrome（不复用注册浏览器 CDP，避免拿不到 token）
        self.assertIn("_call_solver_subprocess", inspect.getsource(AnyCapMailboxRegistrar._call_solver))

    def test_extract_turnstile_sitekey_reads_dom_attribute(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def __init__(self, sitekey):
                self._sitekey = sitekey

            def evaluate(self, script, *args):
                if "data-captcha-sitekey" in str(script):
                    return self._sitekey
                return ""

            def content(self):
                return f'<div data-captcha-sitekey="{self._sitekey}"></div>'

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        self.assertEqual(registrar._extract_turnstile_sitekey(FakePage("0x4AAAA-dom")), "0x4AAAA-dom")

    def test_extract_turnstile_sitekey_falls_back_to_html_regex(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def evaluate(self, _script, *_args):
                return ""  # DOM 取不到，走 HTML 正则兜底

            def content(self):
                return '<div data-captcha-sitekey="0x4AAAA-html"></div>'

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        self.assertEqual(registrar._extract_turnstile_sitekey(FakePage()), "0x4AAAA-html")

    def test_solve_turnstile_via_solver_calls_solver_and_injects_token(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakeSolver:
            def __init__(self, token):
                self.token = token
                self.calls = []

            def solve_turnstile(self, url, sitekey, **kwargs):
                self.calls.append({"url": url, "sitekey": sitekey, "kwargs": kwargs})
                return self.token

        class FakePage:
            def __init__(self, sitekey):
                self.sitekey = sitekey
                self.inject_scripts = []
                self.url = "https://auth.converge.ai/u/signup?state=xyz123"

            def evaluate(self, script, *args):
                s = str(script)
                if "ulp-auth0-v2-captcha" in s or "data-captcha-sitekey" in s:
                    return self.sitekey
                if "window.turnstile" in s and "captcha" in s:
                    self.inject_scripts.append(s)
                    return True
                return ""

            def content(self):
                return f'<div data-captcha-sitekey="{self.sitekey}"></div>'

        solver = FakeSolver("tok_injected_abc")
        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None, captcha_solver=solver)
        page = FakePage("0x4AAAA-solve")

        token = registrar._solve_turnstile_via_solver(page)

        self.assertEqual(token, "tok_injected_abc")
        self.assertEqual(len(solver.calls), 1)
        self.assertEqual(solver.calls[0]["sitekey"], "0x4AAAA-solve")
        # solver 用新建的 Auth0 signup URL（Turnstile widget 在 signup 首页，token sitekey 级有效）
        self.assertIn("auth.converge.ai", solver.calls[0]["url"])
        self.assertIn("screen_hint=signup", solver.calls[0]["url"])
        # 注入脚本被调用，含 token + override window.turnstile + 触发回调
        self.assertTrue(page.inject_scripts)
        inject = page.inject_scripts[0]
        self.assertIn("tok_injected_abc", inject)
        self.assertIn("window.turnstile", inject)
        self.assertIn("getResponse", inject)
        self.assertIn("_turnstileTokenCallback", inject)

    def test_solve_turnstile_via_solver_always_uses_signup_url_regardless_of_page_url(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakeSolver:
            def __init__(self):
                self.calls = []

            def solve_turnstile(self, url, sitekey, **kwargs):
                self.calls.append({"url": url, "sitekey": sitekey})
                return "tok"

        class FakePage:
            # page.url 是后续页（email-identifier，无 Turnstile widget），但 solver 应始终用 signup 首页
            url = "https://auth.converge.ai/u/email-identifier/challenge?state=xyz"

            def evaluate(self, script, *args):
                s = str(script)
                if "ulp-auth0-v2-captcha" in s or "data-captcha-sitekey" in s:
                    return "0x4AAAA-x"
                if "window.turnstile" in s:
                    return True
                return ""

            def content(self):
                return ""

        solver = FakeSolver()
        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None, captcha_solver=solver)
        registrar._solve_turnstile_via_solver(FakePage())
        # 始终用新建 signup URL（screen_hint=signup），不是 page.url 的 email-identifier
        self.assertEqual(len(solver.calls), 1)
        self.assertIn("auth.converge.ai", solver.calls[0]["url"])
        self.assertIn("screen_hint=signup", solver.calls[0]["url"])
        self.assertNotIn("email-identifier", solver.calls[0]["url"])

    def test_solve_turnstile_via_solver_returns_empty_when_sitekey_missing(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakeSolver:
            def solve_turnstile(self, url, sitekey, **kwargs):
                raise AssertionError("solver 不应在无 sitekey 时被调用")

        class FakePage:
            url = "https://auth.converge.ai/u/signup?state=none"

            def evaluate(self, _script, *_args):
                return ""

            def content(self):
                return "<html>no captcha here</html>"

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None, captcha_solver=FakeSolver())
        self.assertEqual(registrar._solve_turnstile_via_solver(FakePage()), "")

    def test_call_solver_passes_proxy_only_when_signature_accepts(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class SolverWithProxy:
            def __init__(self):
                self.calls = []

            def solve_turnstile(self, url, sitekey, proxy=None, user_agent=""):
                self.calls.append({"proxy": proxy, "user_agent": user_agent})
                return "tok"

        class SolverWithoutProxy:
            def __init__(self):
                self.calls = []

            def solve_turnstile(self, url, sitekey):
                self.calls.append({})
                return "tok"

        s1 = SolverWithProxy()
        AnyCapMailboxRegistrar(proxy="http://resin:2260", captcha_solver=s1)._call_solver("u", "k")
        self.assertEqual(s1.calls[0]["proxy"], "http://resin:2260")
        self.assertTrue(s1.calls[0]["user_agent"])

        s2 = SolverWithoutProxy()
        AnyCapMailboxRegistrar(proxy="http://resin:2260", captcha_solver=s2)._call_solver("u", "k")
        self.assertEqual(s2.calls, [{}])  # 不接受 proxy 的 solver 不被传 proxy

    def test_inject_turnstile_token_overrides_window_turnstile_and_fires_callbacks(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def __init__(self):
                self.scripts = []

            def evaluate(self, script, *args):
                self.scripts.append(str(script))
                return True

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        page = FakePage()
        ok = registrar._inject_turnstile_token(page, "tok_xyz")
        self.assertTrue(ok)
        inject = page.scripts[-1]
        # token 必须被注入到脚本里
        self.assertIn("tok_xyz", inject)
        # 镜像 Cursor：override window.turnstile（getResponse）+ 触发回调 + 建/填 captcha 隐藏域
        self.assertIn("window.turnstile", inject)
        self.assertIn("getResponse", inject)
        self.assertIn("_turnstileTokenCallback", inject)
        self.assertIn("turnstileCallback", inject)
        self.assertIn("captcha", inject)

    def test_submit_button_enabled_detects_disabled_state(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def __init__(self, disabled):
                self._disabled = disabled

            def evaluate(self, script, *args):
                if "disabled" in str(script):
                    return not self._disabled
                return ""

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        self.assertFalse(registrar._submit_button_enabled(FakePage(True)))
        self.assertTrue(registrar._submit_button_enabled(FakePage(False)))

    def test_signup_block_detector_flags_captcha_rejection(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def __init__(self, text):
                self._text = text

            def evaluate(self, _script):
                return self._text

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        self.assertEqual(
            registrar._detect_signup_block_reason(page=FakePage("Security checks failed. Please verify you are human.")),
            "anycap_captcha_rejected",
        )
        # 裸 "captcha" 不应误判（页面正常 Turnstile 区也含该词）
        self.assertEqual(registrar._detect_signup_block_reason(page=FakePage("Please enter your email")), "")

    def test_captcha_rejected_raises_without_blacklisting(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def evaluate(self, _script):
                return "Security checks failed"

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        # captcha 被拒只 raise（让上层换 IP/session 重试），不能拉黑邮箱/域名
        with self.assertRaises(RuntimeError) as cm:
            registrar._raise_if_signup_blocked("x@outlook.com", page=FakePage())
        self.assertIn("captcha", str(cm.exception).lower())

    def test_make_captcha_wires_anycap_chrome_config_to_solver(self):
        from core.base_platform import RegisterConfig
        from platforms.anycap.plugin import AnyCapPlatform

        captured: dict = {}

        class FakeSolver:
            def solve_turnstile(self, *a, **k):
                return ""

        def fake_create(key, extra):
            captured["key"] = key
            captured["chrome_path"] = extra.get("chrome_path")
            captured["chrome_cdp_url"] = extra.get("chrome_cdp_url")
            return FakeSolver()

        platform = AnyCapPlatform(config=RegisterConfig(
            executor_type="cdp_protocol",
            extra={"anycap_chrome_path": "C:/Chrome/chrome.exe", "anycap_cdp_url": "http://127.0.0.1:9222"},
        ))
        with patch("core.base_captcha.create_captcha_solver", side_effect=fake_create):
            platform._make_captcha()
        # anycap 专属 chrome/cdp 配置必须透传到 CdpTurnstileSolver 读取的通用键
        self.assertEqual(captured["chrome_path"], "C:/Chrome/chrome.exe")
        self.assertEqual(captured["chrome_cdp_url"], "http://127.0.0.1:9222")

    def test_dead_parse_auth_code_helper_removed(self):
        from platforms.anycap import browser_oauth

        # _drive_post_identifier_steps 用 Enter 的副本；anycap 本地不应再留死代码
        self.assertFalse(hasattr(browser_oauth, "_parse_auth_code_from_url"))

    def test_signup_block_detector_flags_already_registered(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def __init__(self, text):
                self._text = text
            def evaluate(self, _script):
                return self._text

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        self.assertEqual(
            registrar._detect_signup_block_reason(page=FakePage("This email is already registered. Please log in instead")),
            "anycap_email_already_registered",
        )
        self.assertEqual(
            registrar._detect_signup_block_reason(page=FakePage("You have already signed up")),
            "anycap_email_already_registered",
        )

    def test_already_registered_raises_and_marks_platform_used(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def evaluate(self, _script):
                return "This email is already registered. Please log in instead"

        marked = {"called": False}
        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None, inventory_id=0)
        registrar._mark_inventory_platform_used = lambda email, platform="anycap": marked.update(called=True, email=email, platform=platform)
        registrar._blacklist_single_mailbox = lambda email, reason: marked.update(blacked=email)
        with self.assertRaises(RuntimeError) as cm:
            registrar._raise_if_signup_blocked("x@outlook.com", page=FakePage())
        self.assertIn("已注册", str(cm.exception))
        self.assertTrue(marked["called"])  # 必须补记 used_platforms
        self.assertEqual(marked["email"], "x@outlook.com")

    def test_call_solver_isolates_browser_solvers_in_subprocess(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar
        from core.base_captcha import CdpTurnstileSolver, YesCaptcha

        # CdpTurnstileSolver（本地浏览器类）必须走进程隔离
        source = inspect.getsource(AnyCapMailboxRegistrar._call_solver)
        self.assertIn("needs_isolation", source)
        self.assertIn("_call_solver_subprocess", source)
        self.assertIn("CdpTurnstileSolver", source)
        # 远程 YesCaptcha（纯 HTTP）不走进程隔离，直接调
        self.assertIn("YesCaptcha", source)

    def test_run_uses_already_registered_guard(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        source = inspect.getsource(AnyCapMailboxRegistrar.run)
        self.assertIn("_drive_post_identifier_with_already_registered_guard", source)
        # guard 方法本身必须扫 already registered body 早失败
        guard_src = inspect.getsource(AnyCapMailboxRegistrar._drive_post_identifier_with_already_registered_guard)
        self.assertIn("already registered", guard_src)
        self.assertIn("please log in instead", guard_src)

    # --- 纯协议注册路径（零浏览器，AnyCapProtocolRegister）测试 ---

    def test_protocol_executor_type_uses_pure_protocol_worker(self):
        from core.base_platform import RegisterConfig
        from platforms.anycap.plugin import AnyCapPlatform

        platform = AnyCapPlatform(config=RegisterConfig(executor_type="protocol", extra={}))
        adapter = platform.build_protocol_mailbox_adapter()
        # 纯协议路径必须 use_executor=True（ProtocolExecutor=curl_cffi）
        self.assertTrue(adapter.use_executor)
        self.assertTrue(adapter.use_captcha)

    def test_cdp_protocol_executor_type_uses_browser_worker(self):
        from core.base_platform import RegisterConfig
        from platforms.anycap.plugin import AnyCapPlatform

        platform = AnyCapPlatform(config=RegisterConfig(executor_type="cdp_protocol", extra={}))
        adapter = platform.build_protocol_mailbox_adapter()
        # 浏览器路径不 use_executor（AnyCapMailboxRegistrar 自己开 Chrome）
        self.assertFalse(adapter.use_executor)
        self.assertTrue(adapter.use_captcha)

    def test_protocol_path_make_captcha_returns_yescaptcha(self):
        from core.base_platform import RegisterConfig
        from platforms.anycap.plugin import AnyCapPlatform

        captured: dict = {}

        class FakeSolver:
            def solve_turnstile(self, *a, **k):
                return ""

        def fake_create(key, extra):
            captured["key"] = key
            return FakeSolver()

        platform = AnyCapPlatform(config=RegisterConfig(executor_type="protocol", extra={}))
        with patch("core.base_captcha.create_captcha_solver", side_effect=fake_create):
            platform._make_captcha()
        # 纯协议路径默认 yescaptcha（纯 HTTP 带代理），不开 Chrome
        self.assertEqual(captured["key"], "yescaptcha")

    def test_protocol_register_steps_use_auth0_universal_login_endpoints(self):
        from platforms.anycap.protocol_register import AnyCapProtocolRegister, ANYCAP_TURNSTILE_SITEKEY

        # sitekey 必须是 AnyCap Auth0 实测值
        self.assertEqual(ANYCAP_TURNSTILE_SITEKEY, "0x4AAAAAACwSuI5jPtwnNwc5")
        source = inspect.getsource(AnyCapProtocolRegister)
        # 必须用 Auth0 Universal Login 表单端点（参照 tavily）
        self.assertIn("/u/signup/identifier", source)
        self.assertIn("/u/email-identifier/challenge", source)
        self.assertIn("/u/signup/password", source)
        self.assertIn("/authorize/resume", source)
        # 必须 YesCaptcha 带代理解（proxy 透传给 solver）
        self.assertIn("proxy", source)

    def test_protocol_register_detects_rate_limit_and_already_registered(self):
        from platforms.anycap.protocol_register import AnyCapProtocolRegister

        class FakeResp:
            def __init__(self, text, location=""):
                self.text = text
                self.headers = {"location": location}

        reg = AnyCapProtocolRegister(executor=None, captcha=None, log_fn=lambda _m: None)
        self.assertEqual(
            reg._detect_block_from_response(FakeResp("Too many signup attempts. Please try again later")),
            "anycap_signup_attempts_limited",
        )
        self.assertEqual(
            reg._detect_block_from_response(FakeResp("This email is already registered. Please log in instead")),
            "anycap_email_already_registered",
        )
        self.assertEqual(reg._detect_block_from_response(FakeResp("normal page")), "")



if __name__ == "__main__":
    unittest.main()
