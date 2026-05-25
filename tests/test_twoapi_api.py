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
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.plugins["zo"].accounts = [
                TwoAPIAccount(plugin="zo", email="demo@example.com", base_url="https://demo.zo.space/v1/zo_sk_demo", api_key="zo_sk_demo", credit_amount=100, credit_ok=True)
            ]
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


if __name__ == "__main__":
    unittest.main()
