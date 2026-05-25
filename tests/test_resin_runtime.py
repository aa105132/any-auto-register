from __future__ import annotations

import unittest
from unittest.mock import patch

from infrastructure.resin_runtime import ResinRuntime


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self.ok = status_code == 200
        self._payload = payload or {}
        self.headers = {"content-type": "application/json"}

    def json(self):
        return dict(self._payload)


class ResinRuntimeTests(unittest.TestCase):
    def test_probe_returns_origin_ip_and_platform(self):
        runtime = ResinRuntime()
        with patch("infrastructure.resin_runtime.requests.get", return_value=_FakeResponse(payload={"origin": "1.2.3.4"})) as mocked:
            result = runtime.probe(
                {
                    "resin_scheme": "http",
                    "resin_host": "127.0.0.1",
                    "resin_port": "2260",
                    "resin_token": "my-token",
                    "resin_default_platform": "Default",
                    "resin_platform_map": "venice=SeedancePool",
                },
                task_platform="venice",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["origin_ip"], "1.2.3.4")
        self.assertEqual(result["resolved_platform"], "SeedancePool")
        self.assertEqual(result["source"], "structured")
        kwargs = mocked.call_args.kwargs
        self.assertEqual(kwargs["proxies"]["http"], "http://SeedancePool:my-token@127.0.0.1:2260")

    def test_probe_reports_missing_configuration(self):
        runtime = ResinRuntime()
        result = runtime.probe({})

        self.assertFalse(result["ok"])
        self.assertIn("未检测到可用的 Resin 代理配置", result["error"])


if __name__ == "__main__":
    unittest.main()
