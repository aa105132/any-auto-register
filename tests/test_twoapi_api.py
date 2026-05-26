from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from main import app
from services.twoapi.manager import TwoAPIManager
from services.twoapi.models import TwoAPIAccount


class TwoAPIRouterTests(unittest.TestCase):
    def test_management_routes_and_openai_routes_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.joinpath("zo_proxy_urls.txt").write_text(
                "zo|demo@example.com|https://demo.zo.space/v1/zo_sk_demo|zo_api_key=zo_sk_demo|api_key=dummy\n",
                encoding="utf-8",
            )
            manager = TwoAPIManager(data_dir=root)
            key = manager.create_key(plugin="zo", note="test")["key"]
            response = Mock(status_code=200, ok=True, content=b'{"object":"list","data":[]}', text='{"object":"list","data":[]}', headers={"content-type": "application/json"})
            transport = Mock()
            transport.get.return_value = response
            manager.plugins["zo"].transport = transport
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                status = client.get("/api/2api/status")
                self.assertEqual(status.status_code, 200)
                self.assertIn("plugins", status.json())
                models = client.get("/zo/v1/models", headers={"Authorization": f"Bearer {key}"})
                self.assertEqual(models.status_code, 200)


    def test_plugins_route_returns_navigation_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["zo"].accounts = [
                TwoAPIAccount(plugin="zo", email="demo@example.com", base_url="https://demo.zo.space/v1/zo_sk_demo", api_key="zo_sk_demo", credit_amount=100, credit_ok=True)
            ]
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.get("/api/2api/plugins")
                self.assertEqual(response.status_code, 200)
                item = response.json()["items"][0]
                self.assertEqual(item["name"], "zo")
                self.assertIn("display_name", item)
                self.assertIn("accounts", item)
                self.assertIn("settings", item)

    def test_openai_route_rejects_missing_twoapi_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.get("/zo/v1/models")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["type"], "invalid_request_error")


    def test_refresh_credits_route_returns_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["zo"].accounts = [
                TwoAPIAccount(plugin="zo", email="demo@example.com", base_url="https://demo.zo.space/v1/zo_sk_demo", api_key="zo_sk_demo", credit_amount=1.0, credit_ok=True)
            ]
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.post("/api/2api/plugins/zo/refresh-credits")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["plugin"], "zo")
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
            key = manager.create_key(plugin="zo", note="stream")['key']
            fake = FakeSSE()
            manager.plugins["zo"].forward_chat = Mock(return_value=fake)
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                with client.stream(
                    "POST",
                    "/zo/v1/chat/completions",
                    json={"model": "zo:openai/gpt-5.5", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {key}"},
                ) as response:
                    body = b"".join(response.iter_bytes())
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/event-stream", response.headers.get("content-type", ""))
                self.assertEqual(body, b'data: {"id":"1","choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n')
                manager.plugins["zo"].forward_chat.assert_called_once()
                self.assertTrue(manager.plugins["zo"].forward_chat.call_args.kwargs["stream"])
                self.assertTrue(fake.closed)



    def test_swarms_openai_routes_exist_with_plugin_scoped_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            key = manager.create_key(plugin="swarms", note="swarms")['key']
            manager.plugins["swarms"].forward_models = Mock(return_value=Mock(
                status_code=200,
                ok=True,
                content=b'{"object":"list","data":[]}',
                text='{"object":"list","data":[]}',
                headers={"content-type": "application/json"},
            ))
            manager.plugins["swarms"].forward_chat = Mock(return_value=Mock(
                status_code=200,
                ok=True,
                content=b'{"choices":[{"message":{"content":"pong"}}]}',
                text='{"choices":[{"message":{"content":"pong"}}]}',
                headers={"content-type": "application/json"},
            ))
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                models = client.get("/swarms/v1/models", headers={"Authorization": f"Bearer {key}"})
                self.assertEqual(models.status_code, 200)
                chat = client.post(
                    f"/swarms/v1/{key}/chat/completions",
                    json={"model": "gpt-4o", "messages": [{"role": "user", "content": "ping"}]},
                )
                self.assertEqual(chat.status_code, 200)
                manager.plugins["swarms"].forward_chat.assert_called_once()

    def test_swarms_route_rejects_other_plugin_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            zo_key = manager.create_key(plugin="zo", note="zo")['key']
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.get("/swarms/v1/models", headers={"Authorization": f"Bearer {zo_key}"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error"]["code"], "invalid_twoapi_key")



    def test_zo_push_management_route_dispatches_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["zo"].push_accounts = Mock(return_value={"ok": True, "pushed": 1})
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.post(
                    "/api/2api/plugins/zo/push",
                    json={
                        "target_url": "http://linux.example:8000",
                        "source": "windows-register",
                        "emails": ["push@example.com"],
                        "latest_only": False,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["pushed"], 1)
                manager.plugins["zo"].push_accounts.assert_called_once_with(
                    "http://linux.example:8000",
                    source="windows-register",
                    emails=["push@example.com"],
                    latest_only=False,
                    timeout=30.0,
                )

    def test_swarms_push_management_route_dispatches_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["swarms"].push_accounts = Mock(return_value={"ok": True, "pushed": 1})
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                response = client.post(
                    "/api/2api/plugins/swarms/push",
                    json={
                        "target_url": "http://linux.example:8000",
                        "source": "windows-register",
                        "emails": ["push@swarms.test"],
                        "latest_only": True,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["pushed"], 1)
                manager.plugins["swarms"].push_accounts.assert_called_once_with(
                    "http://linux.example:8000",
                    source="windows-register",
                    emails=["push@swarms.test"],
                    latest_only=True,
                    timeout=30.0,
                )

    def test_swarms_import_and_refill_management_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["swarms"].import_accounts = Mock(return_value={"plugin": "swarms", "created": 1})
            manager.plugins["swarms"].refill_accounts = Mock(return_value={"ok": True, "task": {"id": "task_demo"}})
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                imported = client.post(
                    "/api/2api/plugins/swarms/import",
                    json={"lines": ["demo@swarms.test|sk-import-demo-12345678901234567890"], "source": "unit-test"},
                )
                self.assertEqual(imported.status_code, 200)
                self.assertEqual(imported.json()["created"], 1)
                manager.plugins["swarms"].import_accounts.assert_called_once()

                refill = client.post(
                    "/api/2api/plugins/swarms/refill",
                    json={"count": 2, "concurrency": 1, "extra": {"mail_provider": "luckmail"}},
                )
                self.assertEqual(refill.status_code, 200)
                self.assertTrue(refill.json()["ok"])
                manager.plugins["swarms"].refill_accounts.assert_called_once()

    def test_streaming_response_sends_heartbeat_during_empty_upstream_gap(self):
        import time

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

        from api.twoapi import _iter_upstream_bytes

        fake = SlowSSE()
        body = b"".join(_iter_upstream_bytes(fake, heartbeat_interval=0.01))
        self.assertIn(b": ping\n\n", body)
        self.assertIn(b'data: {"first": true}\n\n', body)
        self.assertTrue(fake.closed)

    def test_status_includes_twoapi_server_state_and_start_stop_routes(self):
        fake_runtime = Mock()
        fake_runtime.status.return_value = {"running": False, "listen": "http://127.0.0.1:6543/zo/v1"}
        fake_runtime.ensure_running.return_value = {"running": True, "started": True, "listen": "http://127.0.0.1:6543/zo/v1", "pid": 1234}
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
                self.assertEqual(server_status.json()["listen"], "http://127.0.0.1:6543/zo/v1")

                stopped = client.post("/api/2api/server/stop")
                self.assertEqual(stopped.status_code, 200)
                self.assertTrue(stopped.json()["stopped"])


if __name__ == "__main__":
    unittest.main()
