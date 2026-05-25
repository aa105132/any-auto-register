from __future__ import annotations

import importlib
import json
import unittest

import requests


class ScdnRuntimeProxyTests(unittest.TestCase):
    def test_runtime_source_treats_origin_payload_as_valid_even_when_status_is_400(self):
        module = importlib.import_module("core.scdn_runtime_proxy")

        class _FakeResponse:
            def __init__(self, payload=None, *, status_code: int = 200, text: str = ""):
                self._payload = payload or {}
                self.status_code = status_code
                self.text = text

            def json(self):
                return self._payload

        class _FakeSession:
            def __init__(self):
                self.calls = []
                self.trust_env = True

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "proxy.scdn.io" in url:
                    return _FakeResponse(
                        {
                            "code": 200,
                            "message": "success",
                            "data": {"proxies": ["95.40.79.184:25448"], "count": 1},
                        }
                    )
                return _FakeResponse(
                    status_code=400,
                    text=json.dumps({"origin": "95.40.79.184"}),
                )

        runtime = module.ScdnRuntimeProxySource(session=_FakeSession(), time_fn=lambda: 100.0)

        proxy_url = runtime.acquire_proxy(
            protocol="https",
            country_code="HK",
            count=1,
            validate_url="http://httpbin.org/ip",
            validate_timeout_sec=5,
            cache_ttl_sec=120,
            cache_size=10,
        )

        self.assertEqual(proxy_url, "https://95.40.79.184:25448")

    def test_runtime_source_downgrades_https_validate_url_for_socks_protocols(self):
        module = importlib.import_module("core.scdn_runtime_proxy")

        class _FakeResponse:
            def __init__(self, payload=None, *, status_code: int = 200, text: str = ""):
                self._payload = payload or {}
                self.status_code = status_code
                self.text = text

            def json(self):
                return self._payload

        class _FakeSession:
            def __init__(self):
                self.calls = []
                self.trust_env = True

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "proxy.scdn.io" in url:
                    return _FakeResponse(
                        {
                            "code": 200,
                            "message": "success",
                            "data": {"proxies": ["43.198.99.209:30011"], "count": 1},
                        }
                    )
                if url != "http://httpbin.org/ip":
                    raise AssertionError(f"unexpected validate url: {url}")
                return _FakeResponse(status_code=200, text='{"origin":"43.198.99.209"}')

        runtime = module.ScdnRuntimeProxySource(session=_FakeSession(), time_fn=lambda: 100.0)

        proxy_url = runtime.acquire_proxy(
            protocol="socks5",
            country_code="HK",
            count=1,
            validate_url="https://httpbin.org/ip",
            validate_timeout_sec=5,
            cache_ttl_sec=120,
            cache_size=10,
        )

        self.assertEqual(proxy_url, "socks5://43.198.99.209:30011")
        validate_calls = [call for call in runtime._session.calls if "proxy.scdn.io" not in call[0]]
        self.assertEqual(validate_calls[0][0], "http://httpbin.org/ip")

    def test_runtime_source_logs_validate_failure_details(self):
        module = importlib.import_module("core.scdn_runtime_proxy")

        class _FakeResponse:
            def __init__(self, payload=None, *, status_code: int = 200, text: str = ""):
                self._payload = payload or {}
                self.status_code = status_code
                self.text = text

            def json(self):
                return self._payload

        class _FakeSession:
            def __init__(self):
                self.trust_env = True

            def get(self, url, **kwargs):
                if "proxy.scdn.io" in url:
                    return _FakeResponse(
                        {
                            "code": 200,
                            "message": "success",
                            "data": {"proxies": ["43.198.99.209:30011"], "count": 1},
                        }
                    )
                raise requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED")

        class _Logger:
            def __init__(self):
                self.messages = []

            def log(self, message: str, **_kwargs):
                self.messages.append(message)

        logger = _Logger()
        runtime = module.ScdnRuntimeProxySource(session=_FakeSession(), time_fn=lambda: 100.0)

        proxy_url = runtime.acquire_proxy(
            protocol="socks5",
            country_code="HK",
            count=1,
            validate_url="https://httpbin.org/ip",
            validate_timeout_sec=5,
            cache_ttl_sec=120,
            cache_size=10,
            logger=logger,
        )

        self.assertIsNone(proxy_url)
        self.assertTrue(any("SCDN 代理验证失败" in message for message in logger.messages))
        self.assertTrue(any("socks5://43.198.99.209:30011" in message for message in logger.messages))
        self.assertTrue(any("SSLError" in message for message in logger.messages))

    def test_runtime_source_logs_guarded_fetch_response(self):
        module = importlib.import_module("core.scdn_runtime_proxy")

        class _FakeResponse:
            def __init__(self, *, status_code: int = 456, text: str = ""):
                self.status_code = status_code
                self.text = text

            def json(self):
                raise ValueError("not json")

        class _FakeSession:
            def __init__(self):
                self.trust_env = True

            def get(self, url, **kwargs):
                return _FakeResponse(
                    status_code=456,
                    text='<script src="/_guard/html.js?js=p456"></script>',
                )

        class _Logger:
            def __init__(self):
                self.messages = []

            def log(self, message: str, **_kwargs):
                self.messages.append(message)

        logger = _Logger()
        runtime = module.ScdnRuntimeProxySource(session=_FakeSession(), time_fn=lambda: 100.0)

        proxy_url = runtime.acquire_proxy(
            protocol="socks5",
            country_code="HK",
            count=2,
            validate_url="https://httpbin.org/ip",
            validate_timeout_sec=5,
            cache_ttl_sec=120,
            cache_size=10,
            logger=logger,
        )

        self.assertIsNone(proxy_url)
        self.assertTrue(any("status=456" in message for message in logger.messages))
        self.assertTrue(any("/_guard/html.js?js=p456" in message for message in logger.messages))

    def test_runtime_source_fetches_candidates_and_returns_first_valid_proxy(self):
        module = importlib.import_module("core.scdn_runtime_proxy")

        class _FakeResponse:
            def __init__(self, payload=None, *, status_code: int = 200, text: str = ""):
                self._payload = payload or {}
                self.status_code = status_code
                self.text = text

            def json(self):
                return self._payload

        class _FakeSession:
            def __init__(self):
                self.calls = []
                self.trust_env = True

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "proxy.scdn.io" in url:
                    return _FakeResponse(
                        {
                            "code": 200,
                            "message": "success",
                            "data": {"proxies": ["1.1.1.1:80", "2.2.2.2:81"], "count": 2},
                        }
                    )
                if kwargs.get("proxies", {}).get("https") == "http://1.1.1.1:80":
                    return _FakeResponse(status_code=502, text="bad gateway")
                return _FakeResponse(status_code=200, text='{"origin":"2.2.2.2"}')

        runtime = module.ScdnRuntimeProxySource(session=_FakeSession(), time_fn=lambda: 100.0)

        proxy_url = runtime.acquire_proxy(
            protocol="http",
            country_code="HK",
            count=2,
            validate_url="https://httpbin.org/ip",
            validate_timeout_sec=5,
            cache_ttl_sec=120,
            cache_size=10,
        )

        self.assertEqual(proxy_url, "http://2.2.2.2:81")
        self.assertFalse(runtime._session.trust_env)

        fetch_call = runtime._session.calls[0]
        self.assertEqual(fetch_call[0], "https://proxy.scdn.io/api/get_proxy.php")
        self.assertEqual(fetch_call[1]["params"]["protocol"], "http")
        self.assertEqual(fetch_call[1]["params"]["country_code"], "HK")
        self.assertEqual(fetch_call[1]["params"]["count"], 2)

    def test_runtime_source_uses_cached_valid_proxy_before_refetch(self):
        module = importlib.import_module("core.scdn_runtime_proxy")

        class _FakeResponse:
            def __init__(self, payload=None, *, status_code: int = 200, text: str = ""):
                self._payload = payload or {}
                self.status_code = status_code
                self.text = text

            def json(self):
                return self._payload

        class _FakeSession:
            def __init__(self):
                self.calls = []
                self.trust_env = True

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if "proxy.scdn.io" in url:
                    return _FakeResponse(
                        {
                            "code": 200,
                            "message": "success",
                            "data": {"proxies": ["3.3.3.3:80", "4.4.4.4:80"], "count": 2},
                        }
                    )
                return _FakeResponse(status_code=200, text='{"origin":"ok"}')

        runtime = module.ScdnRuntimeProxySource(session=_FakeSession(), time_fn=lambda: 100.0)

        first = runtime.acquire_proxy(
            protocol="http",
            country_code="US",
            count=2,
            validate_url="https://httpbin.org/ip",
            validate_timeout_sec=5,
            cache_ttl_sec=120,
            cache_size=10,
        )
        second = runtime.acquire_proxy(
            protocol="http",
            country_code="US",
            count=2,
            validate_url="https://httpbin.org/ip",
            validate_timeout_sec=5,
            cache_ttl_sec=120,
            cache_size=10,
        )

        self.assertEqual(first, "http://3.3.3.3:80")
        self.assertEqual(second, "http://4.4.4.4:80")
        fetch_calls = [call for call in runtime._session.calls if "proxy.scdn.io" in call[0]]
        self.assertEqual(len(fetch_calls), 1)
