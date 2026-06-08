import importlib
import json
import sys
import types
import unittest
from unittest.mock import Mock
from requests import exceptions as req_exc

from core.base_platform import Account, RegisterConfig


def _ensure_sqlalchemy_stub() -> None:
    if "sqlalchemy" in sys.modules:
        return

    module = types.ModuleType("sqlalchemy")

    class UniqueConstraint:
        def __init__(self, *_args, **_kwargs):
            pass

    def inspect(*_args, **_kwargs):
        return None

    module.UniqueConstraint = UniqueConstraint
    module.inspect = inspect
    sys.modules["sqlalchemy"] = module


def _ensure_sqlmodel_stub() -> None:
    if "sqlmodel" in sys.modules:
        return

    module = types.ModuleType("sqlmodel")

    class SQLModel:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    class Session:
        pass

    def Field(default=None, default_factory=None, **_kwargs):
        if default_factory is not None and default is None:
            return default_factory()
        return default

    def create_engine(*_args, **_kwargs):
        return object()

    def select(*_args, **_kwargs):
        return ("select", _args, _kwargs)

    def delete(*_args, **_kwargs):
        return ("delete", _args, _kwargs)

    module.Field = Field
    module.SQLModel = SQLModel
    module.Session = Session
    module.create_engine = create_engine
    module.select = select
    module.delete = delete
    sys.modules["sqlmodel"] = module


def _import_module(module_name: str):
    _ensure_sqlalchemy_stub()
    _ensure_sqlmodel_stub()
    return importlib.import_module(module_name)


def _load_attr(testcase: unittest.TestCase, module_name: str, attr_name: str):
    try:
        module = _import_module(module_name)
    except Exception as exc:  # pragma: no cover
        testcase.fail(f"瀵煎叆 {module_name} 澶辫触: {exc}")
    if not hasattr(module, attr_name):
        testcase.fail(f"{module_name} 缂哄皯 {attr_name}")
    return getattr(module, attr_name)


class VenicePluginTests(unittest.TestCase):
    def test_map_venice_result_uses_access_token_as_primary_token(self):
        platform_cls = _load_attr(self, "platforms.venice.plugin", "VenicePlatform")
        platform = platform_cls(
            RegisterConfig(executor_type="headless", extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )
        raw = {
            "email": "demo@example.com",
            "password": "Venice!2026",
            "user_id": "user_demo",
            "access_token": "access-token-1",
            "refresh_token": "refresh-token-1",
            "session_token": "session-token-1",
            "client_id": "client_demo",
            "api_key": "VENICE_INFERENCE_KEY_demo123",
            "api_key_description": "seedance-auto",
            "refresh_token_source": "clerk.__client.rotating_token",
            "venice_token": "venice-user-token",
            "profile": {
                "email": "demo@example.com",
                "user_id": "user_demo",
                "user_name": "VenetianDemo",
                "user_type": "FREE",
                "venice_credits": 500,
            },
            "api_usage": {"lookback": "7d", "byKey": []},
            "api_keys": [{"description": "seedance-auto", "last6Chars": "mo123"}],
            "credits": 500,
            "seedance_bonus_verified": True,
            "checked_at": "2026-04-23T00:00:00Z",
        }

        mapped = platform._map_venice_result(raw, password="Venice!2026")

        self.assertEqual(mapped.email, "demo@example.com")
        self.assertEqual(mapped.user_id, "user_demo")
        self.assertEqual(mapped.token, "access-token-1")
        self.assertEqual(mapped.extra["access_token"], "access-token-1")
        self.assertEqual(mapped.extra["refresh_token"], "refresh-token-1")
        self.assertEqual(mapped.extra["session_token"], "session-token-1")
        self.assertEqual(mapped.extra["client_id"], "client_demo")
        self.assertEqual(mapped.extra["api_key"], "VENICE_INFERENCE_KEY_demo123")
        self.assertTrue(mapped.extra["account_overview"]["seedance_bonus_verified"])
        self.assertEqual(mapped.extra["account_overview"]["credits"], 500)
        self.assertEqual(mapped.extra["account_overview"]["plan_state"], "free")
        self.assertEqual(mapped.extra["account_overview"]["promo_source"], "seedance")

    def test_account_graph_marks_venice_access_token_as_primary(self):
        account_graph = _import_module("core.account_graph")
        PRIMARY_TOKEN_WRITE_KEYS = getattr(account_graph, "PRIMARY_TOKEN_WRITE_KEYS")
        _platform_credentials_from_extra = getattr(account_graph, "_platform_credentials_from_extra")
        extra = {
            "platform": "venice",
            "access_token": "access-token-1",
            "refresh_token": "refresh-token-1",
            "session_token": "session-token-1",
            "client_id": "client_demo",
            "api_key": "VENICE_INFERENCE_KEY_demo123",
        }

        rows = _platform_credentials_from_extra(extra)
        primary = next(item["key"] for item in rows if item["is_primary"])
        keys = {item["key"] for item in rows}

        self.assertEqual(PRIMARY_TOKEN_WRITE_KEYS["venice"], "access_token")
        self.assertEqual(primary, "access_token")
        self.assertIn("refresh_token", keys)
        self.assertIn("session_token", keys)
        self.assertIn("api_key", keys)

    def test_browser_adapter_uses_venice_otp_and_browser_only(self):
        platform_cls = _load_attr(self, "platforms.venice.plugin", "VenicePlatform")
        platform = platform_cls(
            RegisterConfig(executor_type="headless", extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )

        adapter = platform.build_browser_registration_adapter()

        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.use_captcha_for_mailbox)
        self.assertEqual(adapter.otp_spec.keyword, "Venice")
        self.assertEqual(adapter.otp_spec.code_pattern, r"(?<!\d)(\d{6})(?!\d)")
        self.assertEqual(adapter.otp_spec.wait_message, "Waiting for Venice OTP...")
        self.assertEqual(adapter.otp_spec.success_label, "Venice OTP")

    def test_protocol_adapter_uses_venice_otp_and_captcha(self):
        platform_cls = _load_attr(self, "platforms.venice.plugin", "VenicePlatform")
        platform = platform_cls(
            RegisterConfig(executor_type="protocol", extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )

        adapter = platform.build_protocol_mailbox_adapter()

        self.assertIsNotNone(adapter)
        self.assertTrue(adapter.use_captcha)
        self.assertEqual(adapter.otp_spec.keyword, "Venice")
        self.assertEqual(adapter.otp_spec.code_pattern, r"(?<!\d)(\d{6})(?!\d)")
        self.assertEqual(adapter.otp_spec.wait_message, "Waiting for Venice OTP...")
        self.assertEqual(adapter.otp_spec.success_label, "Venice OTP")

    def test_check_valid_prefers_api_key(self):
        platform_cls = _load_attr(self, "platforms.venice.plugin", "VenicePlatform")
        platform = platform_cls(RegisterConfig(executor_type="headless"), mailbox=None)
        platform.log = lambda _message: None

        fake_client = Mock()
        fake_client.verify_api_key.return_value = True

        module = importlib.import_module("platforms.venice.plugin")
        original_client = module.VeniceClient
        module.VeniceClient = lambda **kwargs: fake_client
        try:
            account = Account(
                platform="venice",
                email="demo@example.com",
                password="pw",
                token="",
                extra={"api_key": "VENICE_INFERENCE_KEY_demo123"},
            )
            self.assertTrue(platform.check_valid(account))
            fake_client.verify_api_key.assert_called_once_with("VENICE_INFERENCE_KEY_demo123")
        finally:
            module.VeniceClient = original_client


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class _FakeCaptcha:
    def __init__(self, token="turnstile-token"):
        self.token = token
        self.calls = []

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        self.calls.append((page_url, site_key))
        return self.token


class _FakeVeniceClient:
    def __init__(self):
        self.calls = []

    def probe_proxy_origins(self, urls=None):
        self.calls.append(("probe_proxy_origins", tuple(urls or ())))
        return [
            {"url": "https://api.ipify.org?format=json", "ip": "145.223.54.72", "status": 200},
            {"url": "https://httpbin.org/ip", "ip": "145.223.54.72", "status": 200},
            {"url": "https://ifconfig.me/ip", "ip": "145.223.54.72", "status": 200},
        ]

    def open_seedance_landing(self):
        self.calls.append(("open_seedance_landing",))
        return {"ok": True}

    def get_encrypted_models(self, access_token: str, *, mature_filter: bool = True, only_safe_venice: bool = True):
        self.calls.append(("get_encrypted_models", access_token, mature_filter, only_safe_venice))
        return {"data": []}

    def init_clerk_client(self):
        self.calls.append(("init_clerk_client",))
        return {"response": {"id": "client_1"}}

    def create_sign_up(self, *, email: str, password: str, captcha_token: str | None = None):
        self.calls.append(("create_sign_up", email, password, captcha_token or ""))
        return {"id": "su_1", "status": "missing_requirements"}

    def prepare_email_verification(self, sign_up_id: str):
        self.calls.append(("prepare_email_verification", sign_up_id))
        return {"status": "pending"}

    def attempt_email_verification(self, sign_up_id: str, *, code: str):
        self.calls.append(("attempt_email_verification", sign_up_id, code))
        return {
            "status": "complete",
            "created_session_id": "sess_1",
            "created_user_id": "user_1",
        }

    def create_session_token(self, session_id: str):
        self.calls.append(("create_session_token", session_id))
        return {"jwt": "session-jwt"}

    def get_cookie(self, name: str) -> str:
        cookies = {
            "__client": "header.eyJpZCI6ICJjbGllbnRfMSIsICJyb3RhdGluZ190b2tlbiI6ICJyZWZyZXNoLTEifQ.sig",
            "__session": "header.eyJzaWQiOiAic2Vzc18xIiwgInN1YiI6ICJ1c2VyXzEifQ.sig",
        }
        return cookies.get(name, "")

    def claim_landing_credit(self, access_token: str):
        self.calls.append(("claim_landing_credit", access_token))
        return {"alreadyRedeemed": False, "creditsAmount": 500}

    def get_user_session(self, access_token: str):
        self.calls.append(("get_user_session", access_token))
        return {
            "email": "demo@example.com",
            "userId": "user_1",
            "userName": "Venice Demo",
            "userType": "FREE",
            "veniceCredits": 500,
            "token": "venice-user-token",
        }

    def get_api_usage(self, access_token: str, *, lookback: str = "7d"):
        self.calls.append(("get_api_usage", access_token, lookback))
        return {"lookback": lookback, "byKey": []}

    def list_api_keys(self, access_token: str):
        self.calls.append(("list_api_keys", access_token))
        return {"data": []}

    def create_api_key(self, access_token: str, *, description: str, api_key_type: str = "INFERENCE"):
        self.calls.append(("create_api_key", access_token, description, api_key_type))
        return {
            "data": {
                "apiKey": "VENICE_INFERENCE_KEY_demo123",
                "description": description,
                "apiKeyType": api_key_type,
            }
        }


class _CaptchaRetryVeniceClient(_FakeVeniceClient):
    def __init__(self):
        super().__init__()
        self._attempts = 0

    def create_sign_up(self, *, email: str, password: str, captcha_token: str | None = None):
        self.calls.append(("create_sign_up", email, password, captcha_token or ""))
        self._attempts += 1
        if self._attempts == 1:
            raise RuntimeError(
                "Venice HTTP 閿欒 [POST https://clerk.venice.ai/v1/client/sign_ups] "
                "status=422: {\"errors\":[{\"code\":\"captcha_missing_token\"}]}"
            )
        return {"id": "su_1", "status": "missing_requirements"}


class _MissingIdVeniceClient(_FakeVeniceClient):
    def create_sign_up(self, *, email: str, password: str, captcha_token: str | None = None):
        self.calls.append(("create_sign_up", email, password, captcha_token or ""))
        return {
            "status": "missing_requirements",
            "errors": [{"code": "captcha_invalid"}],
            "response": {
                "status": "missing_requirements",
            },
        }


class _NestedSignUpIdVeniceClient(_FakeVeniceClient):
    def create_sign_up(self, *, email: str, password: str, captcha_token: str | None = None):
        self.calls.append(("create_sign_up", email, password, captcha_token or ""))
        return {
            "status": "missing_requirements",
            "response": {
                "id": "sua_nested_1",
                "status": "missing_requirements",
            },
        }


class _NestedVerificationSessionVeniceClient(_FakeVeniceClient):
    def attempt_email_verification(self, sign_up_id: str, *, code: str):
        self.calls.append(("attempt_email_verification", sign_up_id, code))
        return {
            "status": "complete",
            "response": {
                "created_session_id": "sess_nested_1",
                "created_user_id": "user_nested_1",
            },
        }


class _DelayedCreditsVeniceClient(_FakeVeniceClient):
    def __init__(self):
        super().__init__()
        self._session_payloads = [
            {
                "email": "demo@example.com",
                "userId": "user_1",
                "userName": "Venice Demo",
                "userType": "FREE",
                "veniceCredits": 0,
                "token": "venice-user-token",
            },
            {
                "email": "demo@example.com",
                "userId": "user_1",
                "userName": "Venice Demo",
                "userType": "FREE",
                "veniceCredits": 0,
                "token": "venice-user-token",
            },
            {
                "email": "demo@example.com",
                "userId": "user_1",
                "userName": "Venice Demo",
                "userType": "FREE",
                "veniceCredits": 500,
                "token": "venice-user-token",
            },
        ]

    def get_user_session(self, access_token: str):
        self.calls.append(("get_user_session", access_token))
        if self._session_payloads:
            return self._session_payloads.pop(0)
        return {
            "email": "demo@example.com",
            "userId": "user_1",
            "userName": "Venice Demo",
            "userType": "FREE",
            "veniceCredits": 500,
            "token": "venice-user-token",
        }


class _TokenOnlySessionVeniceClient(_FakeVeniceClient):
    def claim_landing_credit(self, access_token: str):
        self.calls.append(("claim_landing_credit", access_token))
        return {"alreadyRedeemed": False, "creditsAmount": 5}

    def get_user_session(self, access_token: str):
        self.calls.append(("get_user_session", access_token))
        return {"token": "venice-session-token"}


class _LowPromoTokenOnlySessionVeniceClient(_FakeVeniceClient):
    def claim_landing_credit(self, access_token: str):
        self.calls.append(("claim_landing_credit", access_token))
        return {"alreadyRedeemed": False, "creditsAmount": 1}

    def get_user_session(self, access_token: str):
        self.calls.append(("get_user_session", access_token))
        return {"token": "venice-session-token"}


class _DriftProxyProbeVeniceClient(_FakeVeniceClient):
    def probe_proxy_origins(self, urls=None):
        self.calls.append(("probe_proxy_origins", tuple(urls or ())))
        return [
            {"url": "https://api.ipify.org?format=json", "ip": "142.111.113.184", "status": 200},
            {"url": "https://httpbin.org/ip", "ip": "154.29.87.148", "status": 200},
            {"url": "https://ifconfig.me/ip", "ip": "149.57.17.122", "status": 200},
        ]


class _SingleSuccessProxyProbeVeniceClient(_FakeVeniceClient):
    def probe_proxy_origins(self, urls=None):
        self.calls.append(("probe_proxy_origins", tuple(urls or ())))
        return [
            {"url": "https://api.ipify.org?format=json", "ip": "142.111.113.184", "status": 200},
            {"url": "https://httpbin.org/ip", "error": "timeout"},
            {"url": "https://ifconfig.me/ip", "body_preview": "bad gateway", "status": 502},
        ]


class _ProxyFallbackSession:
    def __init__(self):
        self.proxies = {}
        self.trust_env = True
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        snapshot = dict(self.proxies or {})
        self.calls.append((method, url, snapshot))
        active_proxy = str(snapshot.get("https") or snapshot.get("http") or "")
        if active_proxy.startswith("http://"):
            raise req_exc.ProxyError("Tunnel connection failed: 400 Bad Request")
        return _FakeResponse({"data": []})


class VeniceCoreAndWorkerTests(unittest.TestCase):
    def test_protocol_worker_allows_single_successful_proxy_precheck_probe(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _SingleSuccessProxyProbeVeniceClient()
        logs: list[str] = []
        worker = worker_cls(
            client=fake_client,
            proxy_precheck_enabled=True,
            log_fn=logs.append,
        )

        worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=None,
        )

        self.assertIn(("init_clerk_client",), fake_client.calls)
        self.assertTrue(any("proxy precheck summary" in entry for entry in logs), logs)

    def test_protocol_worker_runs_proxy_precheck_before_signup(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _FakeVeniceClient()
        logs: list[str] = []
        worker = worker_cls(
            client=fake_client,
            proxy_precheck_enabled=True,
            log_fn=logs.append,
        )

        worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=None,
        )

        probe_call = (
            "probe_proxy_origins",
            (
                "https://api.ipify.org?format=json",
                "https://httpbin.org/ip",
                "https://ifconfig.me/ip",
            ),
        )
        self.assertIn(probe_call, fake_client.calls)
        self.assertLess(
            fake_client.calls.index(probe_call),
            fake_client.calls.index(("init_clerk_client",)),
        )
        self.assertTrue(any("proxy precheck summary" in entry for entry in logs), logs)

    def test_protocol_worker_fails_fast_when_proxy_precheck_detects_ip_drift(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _DriftProxyProbeVeniceClient()
        logs: list[str] = []
        worker = worker_cls(
            client=fake_client,
            proxy_precheck_enabled=True,
            log_fn=logs.append,
        )

        with self.assertRaises(RuntimeError) as ctx:
            worker.run(
                email="demo@example.com",
                password="Venice!2026",
                otp_callback=lambda: "123456",
                captcha_solver=None,
            )

        self.assertIn("IP drift", str(ctx.exception))
        self.assertNotIn(("init_clerk_client",), fake_client.calls)
        self.assertTrue(any("proxy precheck summary" in entry for entry in logs), logs)

    def test_client_disables_env_proxy_and_sets_explicit_proxy_mapping(self):
        client_cls = _load_attr(self, "platforms.venice.core", "VeniceClient")
        fake_session = Mock()
        fake_session.proxies = {"http": "stale-http", "https": "stale-https"}
        fake_session.request.return_value = _FakeResponse({"data": []})

        client = client_cls(
            session=fake_session,
            proxy="http://demo:secret@proxy.local:8080",
        )

        self.assertIs(client.session, fake_session)
        self.assertFalse(client.session.trust_env)
        self.assertEqual(
            client.session.proxies,
            {
                "http": "http://demo:secret@proxy.local:8080",
                "https": "http://demo:secret@proxy.local:8080",
            },
        )

    def test_client_retries_http_proxy_with_socks5h_fallback(self):
        client_cls = _load_attr(self, "platforms.venice.core", "VeniceClient")
        fake_session = _ProxyFallbackSession()

        client = client_cls(
            session=fake_session,
            proxy="http://demo:secret@proxy.local:8080",
        )
        payload = client.verify_api_key("VENICE_INFERENCE_KEY_demo123")

        self.assertTrue(payload)
        self.assertEqual(len(fake_session.calls), 2)
        self.assertEqual(
            fake_session.calls[0][2],
            {
                "http": "http://demo:secret@proxy.local:8080",
                "https": "http://demo:secret@proxy.local:8080",
            },
        )
        self.assertEqual(
            fake_session.calls[1][2],
            {
                "http": "socks5h://demo:secret@proxy.local:8080",
                "https": "socks5h://demo:secret@proxy.local:8080",
            },
        )

    def test_probe_proxy_origins_does_not_retry_with_socks5h_fallback(self):
        client_cls = _load_attr(self, "platforms.venice.core", "VeniceClient")
        fake_session = _ProxyFallbackSession()

        client = client_cls(
            session=fake_session,
            proxy="http://demo:secret@proxy.local:8080",
        )
        results = client.probe_proxy_origins(["https://api.ipify.org?format=json"])

        self.assertEqual(len(fake_session.calls), 1)
        self.assertEqual(
            fake_session.calls[0][2],
            {
                "http": "http://demo:secret@proxy.local:8080",
                "https": "http://demo:secret@proxy.local:8080",
            },
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://api.ipify.org?format=json")
        self.assertIn("Tunnel connection failed", results[0]["error"])

    def test_claim_landing_credit_posts_to_outerface_endpoint(self):
        client_cls = _load_attr(self, "platforms.venice.core", "VeniceClient")
        fake_session = Mock()
        fake_session.request.return_value = _FakeResponse({"alreadyRedeemed": False, "creditsAmount": 500})

        client = client_cls(session=fake_session)
        payload = client.claim_landing_credit("session-jwt")

        self.assertEqual(payload["creditsAmount"], 500)
        args = fake_session.request.call_args.args
        kwargs = fake_session.request.call_args.kwargs
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://outerface.venice.ai/api/app/user/landing-credit")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer session-jwt")

    def test_create_api_key_posts_expected_payload(self):
        client_cls = _load_attr(self, "platforms.venice.core", "VeniceClient")
        fake_session = Mock()
        fake_session.request.return_value = _FakeResponse({"data": {"apiKey": "VENICE_INFERENCE_KEY_demo123"}})

        client = client_cls(session=fake_session)
        payload = client.create_api_key("session-jwt", description="seedance-auto")

        self.assertEqual(payload["data"]["apiKey"], "VENICE_INFERENCE_KEY_demo123")
        args = fake_session.request.call_args.args
        kwargs = fake_session.request.call_args.kwargs
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://outerface.venice.ai/api/app/user/api/api_keys")
        self.assertEqual(
            kwargs["json"],
            {
                "description": "seedance-auto",
                "apiKeyType": "INFERENCE",
                "consumptionLimit": {"diem": None, "usd": None},
            },
        )

    def test_protocol_worker_claims_credit_before_creating_api_key(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _FakeVeniceClient()
        fake_captcha = _FakeCaptcha()
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=lambda _message: None,
        )

        result = worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=fake_captcha,
        )

        self.assertEqual(result["email"], "demo@example.com")
        self.assertEqual(result["user_id"], "user_1")
        self.assertEqual(result["session_id"], "sess_1")
        self.assertEqual(result["access_token"], "session-jwt")
        self.assertEqual(result["refresh_token"], "refresh-1")
        self.assertEqual(result["credits"], 500)
        self.assertTrue(result["seedance_bonus_verified"])
        self.assertEqual(result["api_key"], "VENICE_INFERENCE_KEY_demo123")
        self.assertEqual(fake_captcha.calls, [])
        self.assertIn(("create_sign_up", "demo@example.com", "Venice!2026", ""), fake_client.calls)
        self.assertLess(
            fake_client.calls.index(("claim_landing_credit", "session-jwt")),
            fake_client.calls.index(("create_api_key", "session-jwt", "seedance-auto", "INFERENCE")),
        )

    def test_protocol_worker_bootstraps_seedance_context_before_claiming_credit(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _FakeVeniceClient()
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=lambda _message: None,
            credits_poll_attempts=1,
            credits_poll_interval_sec=0.0,
        )

        worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=None,
        )

        self.assertLess(
            fake_client.calls.index(("open_seedance_landing",)),
            fake_client.calls.index(("claim_landing_credit", "session-jwt")),
        )
        self.assertLess(
            fake_client.calls.index(("get_encrypted_models", "session-jwt", True, True)),
            fake_client.calls.index(("claim_landing_credit", "session-jwt")),
        )
        first_user_session_call = fake_client.calls.index(("get_user_session", "session-jwt"))
        self.assertLess(
            first_user_session_call,
            fake_client.calls.index(("claim_landing_credit", "session-jwt")),
        )

    def test_protocol_worker_skips_captcha_when_sign_up_succeeds_without_it(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _FakeVeniceClient()
        fake_captcha = _FakeCaptcha()
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=lambda _message: None,
        )

        worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=fake_captcha,
        )

        self.assertEqual(fake_captcha.calls, [])
        self.assertIn(("create_sign_up", "demo@example.com", "Venice!2026", ""), fake_client.calls)

    def test_protocol_worker_retries_with_captcha_only_when_required(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _CaptchaRetryVeniceClient()
        fake_captcha = _FakeCaptcha(token="turnstile-token")
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=lambda _message: None,
        )

        result = worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=fake_captcha,
        )

        self.assertEqual(result["api_key"], "VENICE_INFERENCE_KEY_demo123")
        self.assertEqual(
            fake_client.calls[:3],
            [
                ("init_clerk_client",),
                ("create_sign_up", "demo@example.com", "Venice!2026", ""),
                ("create_sign_up", "demo@example.com", "Venice!2026", "turnstile-token"),
            ],
        )
        self.assertEqual(
            fake_captcha.calls,
            [("https://venice.ai/sign-up?redirect_url=%2Flp%2Fseedance%2Fgenerate&source=seedance-landing", "0x4AAAAAAAWXJGBD7bONzLBd")],
        )

    def test_protocol_worker_emits_sign_up_debug_summary_when_id_missing(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _MissingIdVeniceClient()
        logs: list[str] = []
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=logs.append,
        )

        with self.assertRaises(RuntimeError) as ctx:
            worker.run(
                email="demo@example.com",
                password="Venice!2026",
                otp_callback=lambda: "123456",
                captcha_solver=None,
            )

        message = str(ctx.exception)
        self.assertIn("sign_up_id", message)
        self.assertIn("captcha_invalid", message)
        self.assertTrue(any("Venice sign_up" in entry for entry in logs), logs)
        self.assertTrue(any("captcha_invalid" in entry for entry in logs), logs)

    def test_protocol_worker_accepts_nested_response_sign_up_id(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _NestedSignUpIdVeniceClient()
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=lambda _message: None,
        )

        result = worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=None,
        )

        self.assertEqual(result["session_id"], "sess_1")
        self.assertIn(("prepare_email_verification", "sua_nested_1"), fake_client.calls)
        self.assertIn(("attempt_email_verification", "sua_nested_1", "123456"), fake_client.calls)

    def test_protocol_worker_accepts_nested_verification_session_id(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _NestedVerificationSessionVeniceClient()
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=lambda _message: None,
        )

        result = worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=None,
        )

        self.assertEqual(result["access_token"], "session-jwt")
        self.assertIn(("create_session_token", "sess_nested_1"), fake_client.calls)

    def test_protocol_worker_polls_until_expected_credits_arrive(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _DelayedCreditsVeniceClient()
        logs: list[str] = []
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=logs.append,
            credits_poll_attempts=3,
            credits_poll_interval_sec=0.0,
        )

        result = worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=None,
        )

        self.assertEqual(result["credits"], 500)
        self.assertEqual(fake_client.calls.count(("get_user_session", "session-jwt")), 3)
        self.assertTrue(any("landing-credit" in entry for entry in logs), logs)
        self.assertTrue(any("bootstrap-1" in entry for entry in logs), logs)
        self.assertTrue(any("poll-2" in entry for entry in logs), logs)

    def test_protocol_worker_accepts_token_only_user_session_when_landing_credit_confirms_bonus(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _TokenOnlySessionVeniceClient()
        logs: list[str] = []
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=logs.append,
            credits_poll_attempts=1,
            credits_poll_interval_sec=0.0,
        )

        result = worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=None,
        )

        self.assertEqual(result["credits"], 500)
        self.assertEqual(result["venice_token"], "venice-session-token")
        self.assertTrue(any("landing-credit" in entry and "expected_credits=500" in entry for entry in logs), logs)

    def test_protocol_worker_short_circuits_token_only_polling_after_positive_landing_credit(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _TokenOnlySessionVeniceClient()
        logs: list[str] = []
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=logs.append,
            credits_poll_attempts=8,
            credits_poll_interval_sec=0.0,
        )

        result = worker.run(
            email="demo@example.com",
            password="Venice!2026",
            otp_callback=lambda: "123456",
            captcha_solver=None,
        )

        self.assertEqual(result["credits"], 500)
        self.assertEqual(fake_client.calls.count(("get_user_session", "session-jwt")), 2)
        self.assertTrue(any("poll-1" in entry for entry in logs), logs)
        self.assertFalse(any("poll-2" in entry for entry in logs), logs)

    def test_protocol_worker_rejects_token_only_user_session_when_landing_credit_promo_value_is_too_low(self):
        worker_cls = _load_attr(self, "platforms.venice.protocol_mailbox", "VeniceProtocolMailboxWorker")
        fake_client = _LowPromoTokenOnlySessionVeniceClient()
        worker = worker_cls(
            client=fake_client,
            api_key_description="seedance-auto",
            expected_credits=500,
            log_fn=lambda _message: None,
            credits_poll_attempts=1,
            credits_poll_interval_sec=0.0,
        )

        with self.assertRaises(RuntimeError) as ctx:
            worker.run(
                email="demo@example.com",
                password="Venice!2026",
                otp_callback=lambda: "123456",
                captcha_solver=None,
            )

        self.assertIn("credits=0", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
