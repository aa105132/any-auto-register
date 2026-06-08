from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class AnyCapRegistrationTests(unittest.TestCase):
    def test_frontend_discovered_routes_are_encoded_in_oauth_worker(self):
        from platforms.anycap import browser_oauth

        self.assertEqual(browser_oauth.API_BASE, "https://api.anycap.ai")
        self.assertEqual(browser_oauth.ACCESS_TOKEN_URL, "https://anycap.ai/auth/access-token")
        self.assertEqual(browser_oauth.API_KEYS_URL, "https://api.anycap.ai/v1/api-keys")
        self.assertIn("/api/auth/login", browser_oauth.LOGIN_URL)

    def test_plugin_maps_created_api_key_as_primary_token(self):
        from platforms.anycap.plugin import AnyCapPlatform

        result = AnyCapPlatform()._map_result({
            "email": "demo@anycap.test",
            "api_key": "ak_demo_anycap_key_123456",
            "access_token": "access-token",
            "api_key_info": {"id": "key_1"},
        })
        self.assertEqual(result.token, "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["api_key"], "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["ai_api_token"], "ak_demo_anycap_key_123456")
        self.assertEqual(result.extra["native_api_base"], "https://api.anycap.ai")

    def test_oauth_worker_uses_browser_token_then_protocol_key_create(self):
        from platforms.anycap import browser_oauth

        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_get_access_token_http", source)
        self.assertIn("_create_api_key_http", source)
        self.assertIn("_verify_api_key_http", source)
    def test_mailbox_flow_blacklists_auth0_signup_blocked_domain(self):
        import inspect
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        source = inspect.getsource(AnyCapMailboxRegistrar)
        self.assertIn("add_mailbox_domain_blacklist", source)
        self.assertIn('platform="anycap"', source)
        self.assertIn("too many signup attempts", source)
        self.assertIn("please try again later", source)
        self.assertIn("domain is not allowed", source)

    def test_signup_block_detector_normalizes_auth0_limit_text(self):
        from platforms.anycap.browser_oauth import AnyCapMailboxRegistrar

        class FakePage:
            def evaluate(self, _script):
                return "Too many signup attempts.\nPlease try again later"

        registrar = AnyCapMailboxRegistrar(log_fn=lambda _msg: None)
        self.assertEqual(
            registrar._detect_signup_block_reason(page=FakePage()),
            "anycap_signup_attempts_limited",
        )



class AnyCapTwoAPITests(unittest.TestCase):
    def test_anycap_plugin_imports_and_serves_static_schema(self):
        from services.twoapi.plugins.anycap import AnyCapTwoAPIPlugin

        with tempfile.TemporaryDirectory() as tmp:
            plugin = AnyCapTwoAPIPlugin(data_dir=Path(tmp), account_db_path=Path(tmp) / "account_manager.db")
            result = plugin.import_accounts(records=[{"email": "demo@anycap.test", "api_key": "ak_demo_anycap_key_123456"}], source="unit")
            self.assertGreaterEqual(result.get("accepted", 0), 1)
            self.assertEqual(plugin.models("image").json()["capability"], "image")
            schema = plugin.schema("video", "kling-v1", mode="image-to-video").json()
            self.assertEqual(schema["schemas"][0]["mode"], "image-to-video")
            self.assertIn("prompt", schema["schemas"][0]["schema"]["model_params"])

    def test_anycap_generate_forwards_native_rest_payload(self):
        from services.twoapi.models import TwoAPIAccount, TwoAPISettings
        from services.twoapi.plugins.anycap import AnyCapTwoAPIPlugin

        transport = Mock()
        transport.post.return_value = Mock(status_code=200, ok=True, content=b'{"status":"success"}', text='{"status":"success"}', headers={"content-type": "application/json"})
        plugin = AnyCapTwoAPIPlugin(settings=TwoAPISettings(), transport=transport)
        plugin.accounts = [TwoAPIAccount(plugin="anycap", email="demo@anycap.test", base_url="https://api.anycap.ai", api_key="ak_demo_anycap_key_123456", credit_amount=100, credit_ok=True)]

        response = plugin.forward_generate("image", {"model": "gpt-image-1", "prompt": "cat"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(transport.post.call_args.args[0], "https://api.anycap.ai/v1/image/generate")
        self.assertEqual(transport.post.call_args.kwargs["headers"]["Authorization"], "Bearer ak_demo_anycap_key_123456")
        self.assertEqual(transport.post.call_args.kwargs["json"]["prompt"], "cat")

    def test_registration_success_hooks_export_and_push_local(self):
        from types import SimpleNamespace
        from application.tasks import _auto_export_anycap_key, _auto_push_anycap_twoapi

        class Logger:
            def __init__(self):
                self.lines = []
            def log(self, message, level="info", **kwargs):
                self.lines.append((level, message))

        class FakeManager:
            def __init__(self):
                self.calls = []
            def import_plugin_accounts(self, plugin, *, records=None, lines=None, source="external"):
                self.calls.append({"plugin": plugin, "records": records or [], "source": source})
                return {"ok": True, "created": 1, "updated": 0}

        account = SimpleNamespace(platform="anycap", email="demo@anycap.test", password="", user_id="", token="ak_demo_anycap_key_123456", extra={"api_key": "ak_demo_anycap_key_123456"})
        logger = Logger()
        manager = FakeManager()
        with patch("services.twoapi.manager.get_twoapi_manager", return_value=manager):
            _auto_push_anycap_twoapi(logger, account, {"twoapi_push_mode": "local"})
        self.assertEqual(manager.calls[0]["plugin"], "anycap")
        self.assertEqual(manager.calls[0]["records"][0]["api_key"], "ak_demo_anycap_key_123456")

        with tempfile.TemporaryDirectory() as tmp:
            import os
            old = Path.cwd()
            os.chdir(tmp)
            try:
                _auto_export_anycap_key(logger, account)
                rows = json.loads((Path(tmp) / "output" / "anycap_credentials.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(old)
        self.assertEqual(rows[0]["api_key"], "ak_demo_anycap_key_123456")


if __name__ == "__main__":
    unittest.main()
