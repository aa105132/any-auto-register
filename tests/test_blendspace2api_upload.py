from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "platforms" / "blendspace" / "blendspace2api_upload.py"
SPEC = importlib.util.spec_from_file_location("platforms.blendspace.blendspace2api_upload", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

build_blendspace2api_payload = MODULE.build_blendspace2api_payload
upload_to_blendspace2api = MODULE.upload_to_blendspace2api


class BlendSpace2ApiUploadTests(unittest.TestCase):
    def _make_account(self, **overrides):
        extra = {"session_id": '"session-1"'}
        extra.update(overrides.pop("extra", {}))
        return SimpleNamespace(
            platform="blendspace",
            email="demo@example.com",
            token=overrides.pop("token", ""),
            extra=extra,
            **overrides,
        )

    def test_build_payload_contains_session_and_label(self):
        payload = build_blendspace2api_payload(self._make_account())

        self.assertEqual(payload["accounts"][0]["sessionId"], "session-1")
        self.assertEqual(payload["accounts"][0]["label"], "demo@example.com")

    def test_upload_posts_to_admin_import_endpoint_with_bearer_key(self):
        response = Mock(status_code=200, text="{}")
        response.json.return_value = {"ok": True}
        with patch.object(MODULE.requests, "post", return_value=response) as mock_post:
            ok, message = upload_to_blendspace2api(
                self._make_account(),
                api_url="http://127.0.0.1:7860",
                admin_api_key="sk-admin",
            )

        self.assertTrue(ok)
        self.assertIn("导入成功", message)
        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args.kwargs["url"],
            "http://127.0.0.1:7860/admin/accounts/import",
        )
        self.assertEqual(
            mock_post.call_args.kwargs["headers"]["Authorization"],
            "Bearer sk-admin",
        )
        self.assertEqual(
            mock_post.call_args.kwargs["json"]["accounts"][0]["sessionId"],
            "session-1",
        )

    def test_upload_rejects_missing_session(self):
        account = self._make_account(token="", extra={"session_id": ""})

        with patch.object(MODULE.requests, "post") as mock_post:
            ok, message = upload_to_blendspace2api(
                account,
                api_url="http://127.0.0.1:7860",
                admin_api_key="sk-admin",
            )

        self.assertFalse(ok)
        self.assertIn("缺少可导入", message)
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
