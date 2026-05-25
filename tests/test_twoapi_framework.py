from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from services.twoapi.manager import TwoAPIManager
from services.twoapi.models import TwoAPIAccount, TwoAPISettings
from services.twoapi.plugins.zo import ZoTwoAPIPlugin, parse_zo_proxy_lines


class TwoAPIFrameworkTests(unittest.TestCase):
    def test_parse_zo_proxy_lines_extracts_base_url_and_key(self):
        accounts = parse_zo_proxy_lines([
            "zo|demo@example.com|https://demo.zo.space/v1/zo_sk_demo|zo_api_key=zo_sk_demo|api_key=dummy",
            "bad-line",
        ])
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].email, "demo@example.com")
        self.assertEqual(accounts[0].base_url, "https://demo.zo.space/v1/zo_sk_demo")
        self.assertEqual(accounts[0].api_key, "zo_sk_demo")
        self.assertEqual(accounts[0].plugin, "zo")

    def test_key_store_creates_multiple_enabled_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            first = manager.create_key(plugin="zo", note="one")
            second = manager.create_key(plugin="zo", note="two")
            self.assertNotEqual(first["key"], second["key"])
            keys = manager.list_keys()
            self.assertEqual(len(keys), 2)
            self.assertTrue(all(item["enabled"] for item in keys))
            self.assertTrue(manager.verify_key(first["key"], plugin="zo"))

    def test_zo_plugin_skips_empty_balance_and_selects_next(self):
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(min_credit=1.0))
        plugin.accounts = [
            TwoAPIAccount(plugin="zo", email="empty@example.com", base_url="https://empty/v1/k", api_key="k1", credit_amount=0.0, credit_ok=False),
            TwoAPIAccount(plugin="zo", email="ok@example.com", base_url="https://ok/v1/k", api_key="k2", credit_amount=100.0, credit_ok=True),
        ]
        selected = plugin.select_account()
        self.assertEqual(selected.email, "ok@example.com")
        self.assertIn("跳过空额度账号", "\n".join(plugin.recent_logs(limit=20)))

    def test_zo_plugin_wakes_sleeping_account_before_forward(self):
        transport = Mock()
        sleeping = Mock(status_code=503, ok=False, text="sleeping", headers={"content-type": "text/plain"})
        alive = Mock(status_code=200, ok=True, text='{"object":"list","data":[]}', headers={"content-type": "application/json"})
        chat = Mock(status_code=200, ok=True, text='{"choices":[]}', headers={"content-type": "application/json"})
        transport.get.side_effect = [sleeping, alive]
        transport.post.return_value = chat
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(auto_wake=True), transport=transport)
        plugin.accounts = [TwoAPIAccount(plugin="zo", email="demo@example.com", base_url="https://demo.zo.space/v1/zo_sk_demo", api_key="zo_sk_demo", credit_amount=100.0, credit_ok=True)]
        response = plugin.forward_chat({"model": "zo:openai/gpt-5.5", "messages": []})
        self.assertEqual(response.status_code, 200)
        self.assertIn("自动唤醒", "\n".join(plugin.recent_logs(limit=20)))


    def test_zo_plugin_enriches_credit_from_e2e_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "zo_proxy_urls.txt").write_text(
                "zo|demo@example.com|https://demo.zo.space/v1/zo_sk_demo|zo_api_key=zo_sk_demo|api_key=dummy\n",
                encoding="utf-8",
            )
            (root / "zo_e2e_result.json").write_text(
                json.dumps({
                    "email": "demo@example.com",
                    "credit_result": {"ok": True, "amount": 0.5, "source": "/billing/credit-balance?testmode=false"},
                    "workspace_result": {"workspace": {"handle": "demo", "origin": "https://demo.zo.computer"}},
                    "cookies": {"access_token": "token-demo", "refresh_token": "refresh-demo"},
                }),
                encoding="utf-8",
            )
            plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(min_credit=1.0), data_dir=root)
            accounts = plugin.load_accounts()
            self.assertEqual(accounts[0].credit_amount, 0.5)
            self.assertFalse(accounts[0].credit_ok)
            self.assertEqual(accounts[0].metadata["credit_source"], "/billing/credit-balance?testmode=false")
            with self.assertRaisesRegex(RuntimeError, "没有可用 Zo 账号"):
                plugin.select_account()

    def test_twoapi_server_script_defaults_to_6543(self):
        script = Path("scripts") / "run_twoapi_server.py"
        self.assertTrue(script.exists())
        source = script.read_text(encoding="utf-8")
        self.assertIn("port=6543", source)


    def test_account_public_view_does_not_expose_cookies(self):
        account = TwoAPIAccount(
            plugin="zo",
            email="demo@example.com",
            base_url="https://demo.zo.space/v1/zo_sk_demo",
            api_key="zo_sk_demo",
            metadata={"cookies": {"access_token": "secret-access", "refresh_token": "secret-refresh"}, "credit_source": "snapshot"},
        )
        public = account.to_public()
        rendered = json.dumps(public, ensure_ascii=False)
        self.assertNotIn("secret-access", rendered)
        self.assertNotIn("secret-refresh", rendered)
        self.assertNotIn("cookies", public["metadata"])
        self.assertEqual(public["metadata"]["credit_source"], "snapshot")


    def test_ensure_alive_rejects_html_200_from_sleeping_or_missing_route(self):
        transport = Mock()
        html = Mock(status_code=200, ok=True, text="<!doctype html><html></html>", content=b"<!doctype html>", headers={"content-type": "text/html; charset=UTF-8"})
        transport.get.return_value = html
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(auto_wake=False), transport=transport)
        account = TwoAPIAccount(plugin="zo", email="demo@example.com", base_url="https://demo.zo.space/v1/zo_sk_demo", api_key="zo_sk_demo", credit_amount=100.0, credit_ok=True)
        with self.assertRaisesRegex(RuntimeError, "Zo Space 不可用"):
            plugin._ensure_alive(account)
        self.assertIn("invalid_response", account.last_status)


    def test_ensure_alive_redeploys_when_restart_does_not_restore_route(self):
        transport = Mock()
        html = Mock(status_code=200, ok=True, text="<!doctype html><html></html>", headers={"content-type": "text/html; charset=UTF-8"})
        alive = Mock(status_code=200, ok=True, text='{"object":"list","data":[]}', headers={"content-type": "application/json"})
        transport.get.side_effect = [html, html, alive]
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(auto_wake=True, wake_timeout=0.1), transport=transport)
        plugin._wake_account = Mock()
        plugin._redeploy_account_proxy = Mock(return_value={"ok": True})
        account = TwoAPIAccount(plugin="zo", email="demo@example.com", base_url="https://demo.zo.space/v1/zo_sk_demo", api_key="zo_sk_demo", credit_amount=100.0, credit_ok=True)
        plugin._ensure_alive(account)
        plugin._wake_account.assert_called_once_with(account)
        plugin._redeploy_account_proxy.assert_called_once_with(account)
        self.assertEqual(account.last_status, "alive")

    def test_manager_keepalive_calls_plugin_keepalive(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            plugin = manager.get_plugin("zo")
            plugin.keepalive_once = Mock(return_value={"checked": 1, "recovered": 0})
            result = manager.keepalive_once()
            plugin.keepalive_once.assert_called_once()
            self.assertEqual(result["zo"]["checked"], 1)


    def test_manager_keepalive_lifecycle_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            manager.start_keepalive(interval_seconds=3600)
            first_thread = manager._keepalive_thread
            manager.start_keepalive(interval_seconds=3600)
            self.assertIs(first_thread, manager._keepalive_thread)
            self.assertTrue(first_thread.is_alive())
            manager.stop_keepalive()
            self.assertFalse(manager._keepalive_running)


if __name__ == "__main__":
    unittest.main()
