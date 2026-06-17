from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from main import app
from services.twoapi.manager import TwoAPIManager
from services.twoapi.models import TwoAPIAccount
from services.twoapi.plugins.thesys import THESYS_DEFAULT_MODEL, THESYS_OPENAI_BASE_URL


class TwoAPIRouterTests(unittest.TestCase):
    def test_management_routes_and_thesys_openai_routes_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            key = manager.create_key(plugin="thesys", note="test")["key"]
            manager.plugins["thesys"].forward_models = Mock(return_value=Mock(
                status_code=200,
                ok=True,
                content=b'{"object":"list","data":[]}',
                text='{"object":"list","data":[]}',
                headers={"content-type": "application/json"},
            ))
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                status = client.get("/api/2api/status")
                self.assertEqual(status.status_code, 200)
                self.assertEqual([item["name"] for item in status.json()["plugins"]], ["thesys"])
                self.assertEqual(status.json()["listen"], "http://127.0.0.1:6543/thesys/v1")
                models = client.get("/thesys/v1/models", headers={"Authorization": f"Bearer {key}"})
                self.assertEqual(models.status_code, 200)

    def test_plugins_route_returns_thesys_navigation_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["thesys"].accounts = [
                TwoAPIAccount(
                    plugin="thesys",
                    email="demo@thesys.test",
                    base_url=THESYS_OPENAI_BASE_URL,
                    api_key="T" * 64,
                    credit_amount=100,
                    credit_ok=True,
                )
            ]
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.get("/api/2api/plugins")
                self.assertEqual(response.status_code, 200)
                item = response.json()["items"][0]
                self.assertEqual(item["name"], "thesys")
                self.assertIn("display_name", item)
                self.assertIn("accounts", item)
                self.assertIn("settings", item)

    def test_openai_route_rejects_missing_twoapi_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.get("/thesys/v1/models")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_refresh_credits_route_returns_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["thesys"].accounts = [
                TwoAPIAccount(plugin="thesys", email="demo@thesys.test", base_url=THESYS_OPENAI_BASE_URL, api_key="T" * 64, credit_amount=1.0, credit_ok=True)
            ]
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.post("/api/2api/plugins/thesys/refresh-credits")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["plugin"], "thesys")
                self.assertEqual(len(response.json()["accounts"]), 1)

    def test_streaming_chat_proxies_sse_bytes(self):
        class FakeSSE:
            status_code = 200
            ok = True
            headers = {"content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache"}
            text = ""
            closed = False

            def iter_content(self, chunk_size=None):
                yield b'data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}\n\n'
                yield b'data: [DONE]\n\n'

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            key = manager.create_key(plugin="thesys", note="stream")["key"]
            fake = FakeSSE()
            manager.plugins["thesys"].forward_chat = Mock(return_value=fake)
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                with client.stream(
                    "POST",
                    "/thesys/v1/chat/completions",
                    json={"model": THESYS_DEFAULT_MODEL, "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {key}"},
                ) as response:
                    body = b"".join(response.iter_bytes())
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/event-stream", response.headers.get("content-type", ""))
                self.assertEqual(body, b'data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n')
                manager.plugins["thesys"].forward_chat.assert_called_once()
                self.assertTrue(manager.plugins["thesys"].forward_chat.call_args.kwargs["stream"])
                self.assertTrue(fake.closed)

    def test_thesys_route_rejects_other_plugin_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            other_key = manager.key_store.create(plugin="other", note="other")["key"]
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.get("/thesys/v1/models", headers={"Authorization": f"Bearer {other_key}"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "invalid_twoapi_key")

    def test_thesys_push_import_and_refill_management_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["thesys"].push_accounts = Mock(return_value={"ok": True, "pushed": 1})
            manager.plugins["thesys"].import_accounts = Mock(return_value={"plugin": "thesys", "created": 1})
            manager.plugins["thesys"].refill_accounts = Mock(return_value={"ok": True, "task": {"id": "task_demo"}})
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                pushed = client.post(
                    "/api/2api/plugins/thesys/push",
                    json={"target_url": "http://linux.example:8000", "source": "windows-register", "emails": ["push@thesys.test"], "latest_only": True},
                )
                self.assertEqual(pushed.status_code, 200)
                self.assertEqual(pushed.json()["pushed"], 1)

                imported = client.post(
                    "/api/2api/plugins/thesys/import",
                    json={"lines": ["demo@thesys.test|" + "T" * 64], "source": "unit-test"},
                )
                self.assertEqual(imported.status_code, 200)
                self.assertEqual(imported.json()["created"], 1)

                refill = client.post("/api/2api/plugins/thesys/refill", json={"count": 2, "concurrency": 1})
                self.assertEqual(refill.status_code, 200)
                self.assertTrue(refill.json()["ok"])

    def test_streaming_response_sends_heartbeat_during_empty_upstream_gap(self):
        import time
        from api.twoapi import _iter_upstream_bytes

        class SlowSSE:
            status_code = 200
            ok = True
            headers = {"content-type": "text/event-stream; charset=utf-8"}
            text = ""
            closed = False

            def iter_content(self, chunk_size=None):
                time.sleep(0.05)
                yield b'data: {"first": true}\n\n'

            def close(self):
                self.closed = True

        fake = SlowSSE()
        body = b"".join(_iter_upstream_bytes(fake, heartbeat_interval=0.01))
        self.assertIn(b": ping\n\n", body)
        self.assertIn(b'data: {"first": true}\n\n', body)
        self.assertTrue(fake.closed)

    def test_status_includes_twoapi_server_state_and_start_stop_routes(self):
        fake_runtime = Mock()
        fake_runtime.status.return_value = {"running": False, "listen": "http://127.0.0.1:6543/thesys/v1"}
        fake_runtime.ensure_running.return_value = {"running": True, "started": True, "listen": "http://127.0.0.1:6543/thesys/v1", "pid": 1234}
        fake_runtime.stop_owned.return_value = {"running": False, "stopped": True}
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            with patch("api.twoapi.get_twoapi_manager", return_value=manager), patch("api.twoapi.twoapi_server_runtime", fake_runtime):
                client = TestClient(app)
                status = client.get("/api/2api/status")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(status.json()["server"]["running"], False)

                started = client.post("/api/2api/server/start")
                self.assertEqual(started.status_code, 200)
                self.assertTrue(started.json()["running"])
                self.assertTrue(started.json()["started"])

                server_status = client.get("/api/2api/server")
                self.assertEqual(server_status.status_code, 200)
                self.assertEqual(server_status.json()["listen"], "http://127.0.0.1:6543/thesys/v1")

                stopped = client.post("/api/2api/server/stop")
                self.assertEqual(stopped.status_code, 200)
                self.assertTrue(stopped.json()["stopped"])


if __name__ == "__main__":
    unittest.main()
