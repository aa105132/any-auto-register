from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from main import app
from services.twoapi.manager import TwoAPIManager
from services.twoapi.models import TwoAPIAccount, TwoAPISettings, mask_secret_in_text
from services.twoapi.plugins.thesys import (
    THESYS_CHAT_COMPLETIONS_URL,
    THESYS_DEFAULT_MODEL,
    THESYS_OPENAI_BASE_URL,
    ThesysTwoAPIPlugin,
    unwrap_thesys_openui_content,
)


class FakeJSONResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self.headers = {"content-type": "application/json"}
        self.text = json.dumps(payload, ensure_ascii=False)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload

    def close(self):
        return None


class ThesysTwoAPITests(unittest.TestCase):
    def test_unwraps_thesys_openui_textcontent(self):
        raw = """<content thesys="true" version="2">
```openui-lang
root = Card([content])
content = TextContent(&quot;我是一个人工智能助手。\n可以正常对话。&quot;)
```
</content>"""

        self.assertEqual(unwrap_thesys_openui_content(raw), "我是一个人工智能助手。\n可以正常对话。")

    def test_forward_chat_injects_defaults_and_unwraps_response(self):
        key = "t" * 64
        transport = Mock()
        transport.post.return_value = FakeJSONResponse(
            {
                "id": "chatcmpl-thesys-test",
                "object": "chat.completion",
                "model": THESYS_DEFAULT_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '<content thesys="true" version="2">content = TextContent(&quot;正常回答&quot;)</content>',
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        )
        plugin = ThesysTwoAPIPlugin(settings=TwoAPISettings(max_retries=1), transport=transport, data_dir=Path(tempfile.mkdtemp()))
        plugin.accounts = [
            TwoAPIAccount(
                plugin="thesys",
                email="demo@thesys.test",
                base_url=THESYS_OPENAI_BASE_URL,
                api_key=key,
                credit_amount=100,
                credit_ok=True,
            )
        ]

        response = plugin.forward_chat({"messages": [{"role": "user", "content": "hi"}]}, stream=False)
        data = response.json()

        self.assertEqual(data["choices"][0]["message"]["content"], "正常回答")
        self.assertEqual(transport.post.call_args.args[0], THESYS_CHAT_COMPLETIONS_URL)
        request_body = transport.post.call_args.kwargs["json"]
        self.assertEqual(request_body["model"], THESYS_DEFAULT_MODEL)
        self.assertEqual(request_body["reasoning_effort"], "minimal")
        self.assertFalse(request_body["stream"])

    def test_manager_registers_thesys_plugin_and_listen_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            self.assertIn("thesys", manager.plugins)
            self.assertIn("http://127.0.0.1:6543/thesys/v1", manager.status()["listen_urls"])

    def test_thesys_routes_use_plugin_scoped_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            key = manager.create_key(plugin="thesys", note="thesys")["key"]
            manager.plugins["thesys"].forward_models = Mock(return_value=FakeJSONResponse({"object": "list", "data": []}))
            manager.plugins["thesys"].forward_chat = Mock(
                return_value=FakeJSONResponse({"choices": [{"message": {"content": "pong"}}]})
            )
            with patch("api.twoapi.get_twoapi_manager", return_value=manager):
                client = TestClient(app)
                models = client.get("/thesys/v1/models", headers={"Authorization": f"Bearer {key}"})
                self.assertEqual(models.status_code, 200)
                chat = client.post(
                    f"/thesys/v1/{key}/chat/completions",
                    json={"model": THESYS_DEFAULT_MODEL, "messages": [{"role": "user", "content": "ping"}]},
                )
                self.assertEqual(chat.status_code, 200)
                manager.plugins["thesys"].forward_chat.assert_called_once()

    def test_long_unprefixed_thesys_key_is_masked_in_logs(self):
        key = "A" * 80
        rendered = mask_secret_in_text(f"key={key}")
        self.assertNotIn(key, rendered)
        self.assertIn("...", rendered)


if __name__ == "__main__":
    unittest.main()
