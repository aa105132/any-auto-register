from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from typing import Any

from core.base_platform import RegisterConfig
from platforms.grok import core
from platforms.grok.plugin import GrokPlatform
from platforms.grok.protocol_mailbox import GrokProtocolMailboxWorker


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, headers: dict[str, str] | None = None, content: bytes = b"", text: str = "") -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.text = text if text else content.decode("utf-8", errors="ignore")
        self.url = "https://accounts.x.ai/sign-up"
        self.ok = 200 <= status_code < 400


class _FakeSession:
    def __init__(self, response: _FakeResponse, *, set_cookies_on_get: dict[str, str] | None = None) -> None:
        self.response = response
        self.set_cookies_on_get = dict(set_cookies_on_get or {})
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.posts.append({"url": url, **kwargs})
        return self.response

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.gets.append({"url": url, **kwargs})
        self.cookies.update(self.set_cookies_on_get)
        return self.response


class _FakeSolver:
    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        return "turnstile-token"


class _FailingSolver:
    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        raise RuntimeError("2Captcha 创建任务失败: ERROR_ZERO_BALANCE")


class GrokProtocolChainTests(unittest.TestCase):
    def test_frontend_contract_constants_are_current(self) -> None:
        self.assertEqual(core.RPC_BASE_PATH, "")
        self.assertEqual(core.NEXT_ACTION, "7f16f8dd3aab1de7d10bcc2b117e6c24c0e38a935a")
        self.assertEqual(core.TURNSTILE_SITEKEY, "0x4AAAAAAAhr9JGVDZbrZOo0")
        self.assertIn("sign-up", core.RSC_SIGNUP_STATE)

    def test_otp_rpc_uses_root_auth_mgmt_endpoint(self) -> None:
        client = core.GrokRegister(log_fn=lambda _msg: None)
        fake = _FakeSession(_FakeResponse(
            headers={"content-type": "application/grpc-web+proto"},
            content=bytes.fromhex("800000000f") + b"grpc-status:0\r\n",
        ))
        client.s = fake  # type: ignore[assignment]

        client.step1_send_otp("demo@example.com")

        self.assertTrue(fake.posts)
        self.assertEqual(
            fake.posts[0]["url"],
            "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateEmailValidationCode",
        )
        self.assertEqual(fake.posts[0]["headers"].get("X-Grpc-Web"), "1")

    def test_grpc_post_rejects_cloudflare_challenge_html(self) -> None:
        client = core.GrokRegister(log_fn=lambda _msg: None)
        client.s = _FakeSession(_FakeResponse(
            status_code=403,
            headers={"content-type": "text/html; charset=UTF-8"},
            text="<html><title>Just a moment...</title>Cloudflare challenge-platform</html>",
        ))  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "Cloudflare.*cdp_protocol"):
            client._grpc_post("/xai-account/auth_mgmt.AuthManagement/CreateEmailValidationCode", b"abc")

    def test_verify_otp_accepts_grpc_status_zero_in_response_headers(self) -> None:
        client = core.GrokRegister(log_fn=lambda _msg: None)
        fake = _FakeSession(_FakeResponse(
            headers={"content-type": "application/grpc-web+proto", "grpc-status": "0"},
            content=b"",
        ))
        client.s = fake  # type: ignore[assignment]

        self.assertTrue(client.step2_verify_otp("demo@example.com", "ABC-123"))

        self.assertEqual(
            fake.posts[0]["url"],
            "https://accounts.x.ai/auth_mgmt.AuthManagement/VerifyEmailValidationCode",
        )

    def test_verify_otp_submits_six_alnum_code_without_display_separator(self) -> None:
        client = core.GrokRegister(log_fn=lambda _msg: None)
        fake = _FakeSession(_FakeResponse(
            headers={"content-type": "application/grpc-web+proto", "grpc-status": "0"},
            content=b"",
        ))
        client.s = fake  # type: ignore[assignment]

        self.assertTrue(client.step2_verify_otp("demo@example.com", "yk9-gfe"))

        body = fake.posts[0]["data"]
        self.assertIn(b"YK9GFE", body)
        self.assertNotIn(b"YK9-GFE", body)

    def test_signup_payload_submits_six_alnum_code_without_display_separator(self) -> None:
        client = core.GrokRegister(
            captcha_solver=_FakeSolver(),
            log_fn=lambda _msg: None,
            castle_request_token_provider=lambda: "castle-token",
        )
        fake = _FakeSession(_FakeResponse(
            headers={"content-type": "text/x-component"},
            text='0:"https://auth.x.ai/set-cookie?state=ok"',
        ))
        client.s = fake  # type: ignore[assignment]

        client.step3_signup("demo@example.com", "Aa123456,,,aA1", "yk9-gfe", "Demo", "User")

        payload = json.loads(fake.posts[0]["data"])[0]
        self.assertEqual(payload["emailValidationCode"], "YK9GFE")

    def test_signup_payload_uses_current_action_and_anti_abuse_fields(self) -> None:
        client = core.GrokRegister(
            captcha_solver=_FakeSolver(),
            log_fn=lambda _msg: None,
            castle_request_token_provider=lambda: "castle-token",
        )
        fake = _FakeSession(_FakeResponse(
            headers={"content-type": "text/x-component"},
            text='0:"https://auth.x.ai/set-cookie?state=ok"',
        ))
        client.s = fake  # type: ignore[assignment]

        client.step3_signup("demo@example.com", "Aa123456,,,aA1", "ABC-123", "Demo", "User")

        request = fake.posts[0]
        self.assertEqual(request["url"], "https://accounts.x.ai/sign-up")
        self.assertEqual(request["headers"].get("next-action"), "7f16f8dd3aab1de7d10bcc2b117e6c24c0e38a935a")
        self.assertEqual(request["headers"].get("Accept"), "text/x-component")
        raw_payload = request.get("json") if request.get("json") is not None else json.loads(request.get("data") or "[]")
        payload = raw_payload[0]
        self.assertEqual(payload["turnstileToken"], "turnstile-token")
        self.assertEqual(payload["castleRequestToken"], "castle-token")
        self.assertRegex(payload["conversionId"], r"^[0-9a-f-]{36}$")
        self.assertEqual(payload["createUserAndSessionRequest"]["tosAcceptedVersion"], 1)

    def test_set_cookie_url_extraction_preserves_escaped_query_params(self) -> None:
        body = '0:"https://auth.grokipedia.com/set-cookie?q=abc\u0026state=def\u0026redirect=https%3A%2F%2Fgrok.com"'

        urls = core.extract_set_cookie_urls(body)

        self.assertEqual(urls, [
            "https://auth.grokipedia.com/set-cookie?q=abc&state=def&redirect=https%3A%2F%2Fgrok.com"
        ])

    def test_step4_set_cookies_uses_full_unescaped_set_cookie_url(self) -> None:
        client = core.GrokRegister(log_fn=lambda _msg: None)
        fake = _FakeSession(
            _FakeResponse(headers={"set-cookie": "sso=sso-token; Domain=.x.ai; Path=/"}),
            set_cookies_on_get={"sso": "sso-token"},
        )
        client.s = fake  # type: ignore[assignment]
        body = '0:"https://auth.grokipedia.com/set-cookie?q=abc\u0026state=def"'

        client.step4_set_cookies(body)

        self.assertEqual(fake.gets[0]["url"], "https://auth.grokipedia.com/set-cookie?q=abc&state=def")

    def test_step4_set_cookies_expands_nested_success_url_chain(self) -> None:
        def token(payload: dict[str, Any]) -> str:
            import base64

            raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
            return f"hdr.{encoded}.sig"

        final = "https://auth.x.ai/set-cookie?q=final"
        middle = "https://auth.grok.com/set-cookie?q=" + token({"config": {"success_url": final}})
        first = "https://auth.grokipedia.com/set-cookie?q=" + token({"config": {"success_url": middle}})
        client = core.GrokRegister(log_fn=lambda _msg: None)
        fake = _FakeSession(_FakeResponse(), set_cookies_on_get={"sso": "sso-token"})
        client.s = fake  # type: ignore[assignment]

        self.assertEqual(core.expand_set_cookie_redirect_chain(first), [first, middle, final])
        client.step4_set_cookies(f'0:"{first}"')

        self.assertEqual(fake.gets[0]["url"], first)
        self.assertEqual(client.cookies.get("sso"), "sso-token")

    def test_step4_set_cookies_raises_when_chain_does_not_set_sso(self) -> None:
        client = core.GrokRegister(log_fn=lambda _msg: None)
        fake = _FakeSession(_FakeResponse(status_code=400))
        client.s = fake  # type: ignore[assignment]
        body = '0:"https://auth.grokipedia.com/set-cookie?q=abc"'

        with self.assertRaisesRegex(RuntimeError, "未下发 sso"):
            client.step4_set_cookies(body)

    def test_cdp_protocol_is_supported_and_result_keeps_cookie_material(self) -> None:
        platform = GrokPlatform(RegisterConfig(executor_type="cdp_protocol"))
        self.assertIn("cdp_protocol", platform.supported_executors)

        mapped = platform._map_grok_result({
            "email": "demo@example.com",
            "password": "pw",
            "sso": "sso-value",
            "sso_rw": "rw-value",
            "cookies": {"sso": "sso-value", "cf_clearance": "cf"},
            "cookie_header": "sso=sso-value; cf_clearance=cf",
        })
        self.assertEqual(mapped.extra["cookies"]["cf_clearance"], "cf")
        self.assertIn("cf_clearance=cf", mapped.extra["cookie_header"])

    def test_grok_browser_register_supports_current_localized_email_entry(self) -> None:
        from platforms.grok import browser_register

        joined = "\n".join(browser_register.EMAIL_SIGNUP_BUTTON_SELECTORS)
        self.assertIn("使用邮箱注册", joined)
        self.assertIn("Sign up with email", joined)

    def test_grok_browser_cookie_snapshot_builds_cookie_header(self) -> None:
        from platforms.grok import browser_register

        class _Context:
            def cookies(self):
                return [
                    {"name": "sso", "value": "sso-token"},
                    {"name": "sso-rw", "value": "rw-token"},
                ]

        class _Page:
            context = _Context()

        snapshot = browser_register._cookie_snapshot(_Page())
        self.assertEqual(snapshot["cookies"]["sso"], "sso-token")
        self.assertIn("sso-rw=rw-token", snapshot["cookie_header"])

    def test_grok_browser_registration_adapter_does_not_build_captcha_solver(self) -> None:
        platform = GrokPlatform(RegisterConfig(executor_type="headed", captcha_solver="auto"))
        adapter = platform.build_browser_registration_adapter()

        self.assertFalse(adapter.use_captcha_for_mailbox)

    def test_grok_browser_result_keeps_cookie_material(self) -> None:
        platform = GrokPlatform(RegisterConfig(executor_type="headed"))
        mapped = platform._map_grok_result({
            "email": "browser@example.com",
            "password": "pw",
            "sso": "browser-sso",
            "sso_rw": "browser-rw",
            "cookies": {"sso": "browser-sso", "sso-rw": "browser-rw"},
            "cookie_header": "sso=browser-sso; sso-rw=browser-rw",
        })

        self.assertEqual(mapped.extra["cookies"]["sso"], "browser-sso")
        self.assertIn("sso-rw=browser-rw", mapped.extra["cookie_header"])

    def test_grok_cdp_protocol_auto_captcha_prefers_cdp_turnstile(self) -> None:
        platform = GrokPlatform(RegisterConfig(executor_type="cdp_protocol", captcha_solver="auto"))
        with mock.patch.object(platform, "_has_configured_captcha", side_effect=lambda name: name in {"2captcha", "yescaptcha", "cdp_turnstile"}), \
             mock.patch("infrastructure.provider_settings_repository.ProviderSettingsRepository.get_enabled_captcha_order", return_value=["2captcha", "yescaptcha"]):
            self.assertEqual(platform._resolve_captcha_solver(), "cdp_turnstile")

    def test_grok_cdp_protocol_auto_captcha_does_not_create_paid_remote_solver(self) -> None:
        platform = GrokPlatform(RegisterConfig(executor_type="cdp_protocol", captcha_solver="auto"))
        with mock.patch("platforms.grok.plugin.create_captcha_solver", return_value=_FakeSolver()) as create_solver:
            solver = platform._make_captcha()

        self.assertIsInstance(solver, _FakeSolver)
        create_solver.assert_called_once_with("cdp_turnstile", platform.config.extra)

    def test_grok_explicit_remote_captcha_solver_still_respects_user_choice(self) -> None:
        platform = GrokPlatform(RegisterConfig(executor_type="cdp_protocol", captcha_solver="2captcha"))
        with mock.patch("core.base_captcha.create_captcha_solver", return_value=_FakeSolver()) as create_solver, \
             mock.patch.object(platform, "_has_configured_captcha", return_value=True):
            solver = platform._make_captcha()

        self.assertIsInstance(solver, _FakeSolver)
        create_solver.assert_called_once_with("2captcha", platform.config.extra)


    def test_grok_cdp_protocol_browser_mode_uses_browser_worker_without_captcha(self) -> None:
        platform = GrokPlatform(RegisterConfig(
            executor_type="cdp_protocol",
            captcha_solver="auto",
            extra={"grok_registration_mode": "browser"},
        ))
        platform.mailbox = object()

        class _Identity:
            identity_provider = "mailbox"
            email = "browser-mode@example.com"
            mailbox_account = object()
            before_ids = set()
            has_mailbox = True
            chrome_user_data_dir = ""
            chrome_cdp_url = ""
            metadata = {}

        calls: list[tuple[str, str]] = []

        class _Worker:
            def __init__(self, **kwargs: Any) -> None:
                calls.append(("init", str(kwargs.get("chrome_cdp_url", ""))))

            def run(self, email: str, password: str) -> dict[str, Any]:
                calls.append(("run", email))
                return {
                    "email": email,
                    "password": password,
                    "sso": "browser-sso",
                    "cookies": {"sso": "browser-sso"},
                    "cookie_header": "sso=browser-sso",
                }

        with mock.patch.object(platform, "_resolve_identity", return_value=_Identity()), \
             mock.patch.object(platform, "_make_captcha", side_effect=AssertionError("should not build captcha")), \
             mock.patch("platforms.grok.browser_register.GrokBrowserRegister", _Worker):
            account = platform.register(password="Aa123456,,,aA1")

        self.assertEqual(account.email, "browser-mode@example.com")
        self.assertEqual(account.extra["sso"], "browser-sso")
        self.assertEqual(calls, [("init", ""), ("run", "browser-mode@example.com")])

    def test_grok_register_uses_windows_user_proxy_when_proxy_is_not_explicit(self) -> None:
        with mock.patch("platforms.grok.core.detect_windows_user_proxy", return_value="http://127.0.0.1:7897"):
            client = core.GrokRegister(log_fn=lambda _msg: None)
        self.assertEqual(client.proxy, "http://127.0.0.1:7897")
        self.assertEqual(client.s.proxies.get("https"), "http://127.0.0.1:7897")

    def test_explicit_proxy_overrides_windows_user_proxy(self) -> None:
        with mock.patch("platforms.grok.core.detect_windows_user_proxy", return_value="http://127.0.0.1:7897"):
            client = core.GrokRegister(proxy="http://proxy.local:8080", log_fn=lambda _msg: None)
        self.assertEqual(client.proxy, "http://proxy.local:8080")
        self.assertEqual(client.s.proxies.get("https"), "http://proxy.local:8080")

    def test_grok_register_imports_fresh_cf_session_cache_for_same_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            proxy = "http://127.0.0.1:7897"
            core.save_cf_session_cache(
                proxy=proxy,
                user_agent="UA-from-cdp",
                cookies={"cf_clearance": "cf-token", "__cf_bm": "bm-token"},
                cache_dir=cache_dir,
                ttl_seconds=120,
            )

            client = core.GrokRegister(
                proxy=proxy,
                log_fn=lambda _msg: None,
                cf_cache_dir=cache_dir,
            )

        self.assertEqual(client.s.headers.get("user-agent"), "UA-from-cdp")
        self.assertEqual(client.cookies.get("cf_clearance"), "cf-token")
        self.assertTrue(client._cf_cache_loaded)

    def test_grok_register_ignores_expired_cf_session_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            proxy = "http://127.0.0.1:7897"
            core.save_cf_session_cache(
                proxy=proxy,
                user_agent="expired-UA",
                cookies={"cf_clearance": "expired-token"},
                cache_dir=cache_dir,
                ttl_seconds=-1,
            )

            client = core.GrokRegister(
                proxy=proxy,
                log_fn=lambda _msg: None,
                cf_cache_dir=cache_dir,
            )

        self.assertNotEqual(client.s.headers.get("user-agent"), "expired-UA")
        self.assertNotEqual(client.cookies.get("cf_clearance"), "expired-token")
        self.assertFalse(client._cf_cache_loaded)

    def test_cdp_bootstrap_saves_cf_session_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            client = core.GrokRegister(
                proxy="http://127.0.0.1:7897",
                log_fn=lambda _msg: None,
                cf_cache_dir=cache_dir,
            )
            client.import_cookies({"cf_clearance": "fresh-cf", "__cf_bm": "fresh-bm"})
            client.s.headers.update({"user-agent": "fresh-UA"})
            client._save_current_cf_session_cache()

            cached = core.load_cf_session_cache(
                proxy="http://127.0.0.1:7897",
                cache_dir=cache_dir,
            )

        self.assertEqual(cached["cookies"]["cf_clearance"], "fresh-cf")
        self.assertEqual(cached["user_agent"], "fresh-UA")

    def test_worker_accepts_cdp_bridge_options(self) -> None:
        worker = GrokProtocolMailboxWorker(
            log_fn=lambda _msg: None,
            use_cdp_bridge=True,
            chrome_cdp_url="http://127.0.0.1:9222",
            chrome_user_data_dir="",
        )
        self.assertTrue(worker.use_cdp_bridge)
        self.assertTrue(worker.client.use_cdp_bridge)
        self.assertEqual(worker.client.chrome_cdp_url, "http://127.0.0.1:9222")


if __name__ == "__main__":
    unittest.main()
