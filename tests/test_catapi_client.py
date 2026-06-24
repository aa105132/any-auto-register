from __future__ import annotations

import importlib
import unittest
from unittest.mock import Mock, patch

from core import catapi_client as catapi


class CatAPIClientTests(unittest.TestCase):
    def _ok_response(self, payload, status_code=200):
        response = Mock(status_code=status_code, text="")
        response.json.return_value = payload
        response.ok = 200 <= status_code < 400
        return response

    def _error_response(self, payload, status_code=400):
        response = Mock(status_code=status_code, text=str(payload))
        response.json.return_value = payload
        response.ok = False
        return response

    def test_list_channel_keys_parses_api_keys(self):
        response = self._ok_response({
            "success": True,
            "channel": {"id": 3, "slug": "grok", "name": "Grok"},
            "total": 2,
            "keys": [
                {"id": 1, "api_key": "sk-aaa", "key_preview": "sk-aaa...aaa"},
                {"id": 2, "api_key": "sk-bbb", "key_preview": "sk-bbb...bbb"},
            ],
        })
        with patch.object(catapi.requests, "get", return_value=response) as mock_get:
            keys = catapi.list_channel_keys(
                "http://20.193.157.62/",
                "grok",
                admin_username="a105132",
                admin_password="secret",
            )

        self.assertEqual(keys, ["sk-aaa", "sk-bbb"])
        mock_get.assert_called_once()
        call = mock_get.call_args
        self.assertEqual(call.args[0], "http://20.193.157.62/api/external/channels/grok/keys")
        self.assertEqual(call.kwargs["headers"]["X-Admin-Username"], "a105132")
        self.assertEqual(call.kwargs["headers"]["X-Admin-Password"], "secret")

    def test_list_channel_keys_raises_on_error_response(self):
        response = self._error_response({"detail": "渠道不存在"}, status_code=404)
        with patch.object(catapi.requests, "get", return_value=response):
            with self.assertRaises(catapi.CatAPIError) as ctx:
                catapi.list_channel_keys(
                    "http://20.193.157.62",
                    "missing",
                    admin_username="a105132",
                    admin_password="secret",
                )
        self.assertIn("渠道不存在", str(ctx.exception))

    def test_list_channel_keys_rejects_empty_credentials(self):
        with self.assertRaises(catapi.CatAPIError):
            catapi.list_channel_keys(
                "http://20.193.157.62",
                "grok",
                admin_username="",
                admin_password="",
            )

    def test_push_channel_keys_posts_with_correct_body(self):
        response = self._ok_response({
            "success": True,
            "channel": {"id": 3, "slug": "grok", "name": "Grok"},
            "received": 2,
            "added": 1,
            "skipped": 1,
            "before_total": 10,
            "after_total": 11,
        })
        with patch.object(catapi.requests, "post", return_value=response) as mock_post:
            result = catapi.push_channel_keys(
                "http://20.193.157.62",
                "grok",
                ["sk-xxx", "sk-yyy"],
                admin_username="a105132",
                admin_password="secret",
                name_prefix="external",
            )

        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["after_total"], 11)
        mock_post.assert_called_once()
        call = mock_post.call_args
        self.assertEqual(call.args[0], "http://20.193.157.62/api/external/channels/grok/keys")
        self.assertEqual(call.kwargs["headers"]["X-Admin-Username"], "a105132")
        self.assertEqual(call.kwargs["headers"]["X-Admin-Password"], "secret")
        self.assertEqual(call.kwargs["headers"]["Content-Type"], "application/json")
        self.assertEqual(call.kwargs["json"]["api_keys"], ["sk-xxx", "sk-yyy"])
        self.assertEqual(call.kwargs["json"]["name_prefix"], "external")

    def test_push_channel_keys_deduplicates_input(self):
        response = self._ok_response({
            "success": True,
            "received": 1,
            "added": 1,
            "skipped": 0,
            "before_total": 0,
            "after_total": 1,
        })
        with patch.object(catapi.requests, "post", return_value=response) as mock_post:
            catapi.push_channel_keys(
                "http://20.193.157.62",
                "grok",
                ["sk-xxx", "sk-xxx", ""],
                admin_username="a105132",
                admin_password="secret",
            )
        body = mock_post.call_args.kwargs["json"]
        self.assertEqual(body["api_keys"], ["sk-xxx"])

    def test_push_channel_keys_raises_on_error_response(self):
        response = self._error_response({"detail": "缺少 api_keys/keys/api_key"}, status_code=400)
        with patch.object(catapi.requests, "post", return_value=response):
            with self.assertRaises(catapi.CatAPIError) as ctx:
                catapi.push_channel_keys(
                    "http://20.193.157.62",
                    "grok",
                    ["sk-xxx"],
                    admin_username="a105132",
                    admin_password="secret",
                )
        self.assertIn("缺少 api_keys", str(ctx.exception))

    def test_push_channel_keys_rejects_empty_input(self):
        with patch.object(catapi.requests, "post") as mock_post:
            with self.assertRaises(catapi.CatAPIError):
                catapi.push_channel_keys(
                    "http://20.193.157.62",
                    "grok",
                    ["", ""],
                    admin_username="a105132",
                    admin_password="secret",
                )
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
