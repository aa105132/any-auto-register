from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "platforms" / "codebanana" / "codebanana2api_upload.py"
SPEC = importlib.util.spec_from_file_location("platforms.codebanana.codebanana2api_upload", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

build_codebanana2api_payload = MODULE.build_codebanana2api_payload
upload_to_codebanana2api = MODULE.upload_to_codebanana2api


class CodeBanana2ApiUploadTests(unittest.TestCase):
    def _make_account(self, **overrides):
        extra = {
            "username": "cbdemo",
            "cookies": {
                "__Secure-next-auth.session-token": "session-1",
                "__Secure-next-auth.callback-url": "https://www.codebanana.com/en",
            },
            "jwtToken": "jwt-1",
            "chat_id": "chat-1",
            "agent_id": "agent-1",
            "workspace": "workspace-1",
        }
        extra.update(overrides.pop("extra", {}))
        token = overrides.pop("token", "session-1")
        return SimpleNamespace(
            platform="codebanana",
            email="demo@example.com",
            user_id="uuid-1",
            token=token,
            extra=extra,
            **overrides,
        )

    def test_build_payload_contains_supported_import_fields(self):
        payload = build_codebanana2api_payload(self._make_account())

        self.assertEqual(payload["name"], "demo@example.com")
        self.assertEqual(payload["cookie"]["__Secure-next-auth.session-token"], "session-1")
        self.assertEqual(payload["session_token"], "session-1")
        self.assertEqual(payload["jwt_token"], "jwt-1")
        self.assertEqual(payload["chat_id"], "chat-1")
        self.assertEqual(payload["agent_id"], "agent-1")
        self.assertEqual(payload["workspace"], "workspace-1")

    def test_upload_to_codebanana2api_posts_to_single_account_endpoint(self):
        response = Mock(status_code=200)
        response.json.return_value = {"summary": {"account_count": 1}}
        with patch.object(MODULE.requests, "post", return_value=response) as mock_post:
            ok, message = upload_to_codebanana2api(
                self._make_account(),
                api_url="http://127.0.0.1:8080",
            )

        self.assertTrue(ok)
        self.assertIn("导入成功", message)
        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args.kwargs["url"],
            "http://127.0.0.1:8080/api/admin/accounts",
        )
        self.assertEqual(
            mock_post.call_args.kwargs["json"]["session_token"],
            "session-1",
        )

    def test_upload_to_codebanana2api_accepts_full_endpoint_url(self):
        response = Mock(status_code=200)
        response.json.return_value = {}
        with patch.object(MODULE.requests, "post", return_value=response) as mock_post:
            ok, _ = upload_to_codebanana2api(
                self._make_account(),
                api_url="http://127.0.0.1:8080/api/admin/accounts/import",
            )

        self.assertTrue(ok)
        self.assertEqual(
            mock_post.call_args.kwargs["url"],
            "http://127.0.0.1:8080/api/admin/accounts/import",
        )

    def test_upload_to_codebanana2api_rejects_account_without_auth(self):
        account = self._make_account(token="", extra={"cookies": {}, "jwtToken": ""})

        with patch.object(MODULE.requests, "post") as mock_post:
            ok, message = upload_to_codebanana2api(
                account,
                api_url="http://127.0.0.1:8080",
            )

        self.assertFalse(ok)
        self.assertIn("缺少可导入认证信息", message)
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
