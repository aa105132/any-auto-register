from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.twoapi.manager import TwoAPIManager
from services.twoapi.models import TwoAPIAccount, TwoAPISettings
from services.twoapi.plugins.zo import ZoTwoAPIPlugin, parse_zo_proxy_lines
from services.twoapi.plugins.swarms import SwarmsTwoAPIPlugin


class TwoAPIManagerSettingsTests(unittest.TestCase):
    def test_manager_filters_unsupported_plugin_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))

            swarms_settings = manager.save_plugin_settings(
                "swarms",
                {
                    "enabled": True,
                    "auto_refill": True,
                    "auto_wake": False,
                    "wake_timeout": 3,
                    "keepalive_space_fallback": True,
                    "minimize_ask_context": False,
                },
            )

            self.assertTrue(swarms_settings["enabled"])
            self.assertTrue(swarms_settings["auto_refill"])
            self.assertNotIn("auto_wake", swarms_settings)
            self.assertNotIn("wake_timeout", swarms_settings)
            self.assertNotIn("keepalive_space_fallback", swarms_settings)
            self.assertNotIn("minimize_ask_context", swarms_settings)

            raw_settings = json.loads((Path(tmp) / "twoapi_settings.json").read_text(encoding="utf-8"))
            persisted_swarms_settings = raw_settings["plugins"]["swarms"]
            self.assertNotIn("auto_wake", persisted_swarms_settings)
            self.assertNotIn("wake_timeout", persisted_swarms_settings)
            self.assertNotIn("keepalive_space_fallback", persisted_swarms_settings)
            self.assertNotIn("minimize_ask_context", persisted_swarms_settings)

            zo_settings = manager.save_plugin_settings(
                "zo",
                {
                    "auto_wake": False,
                    "wake_timeout": 3,
                    "keepalive_space_fallback": True,
                    "minimize_ask_context": False,
                },
            )

            self.assertFalse(zo_settings["auto_wake"])
            self.assertEqual(zo_settings["wake_timeout"], 3)
            self.assertTrue(zo_settings["keepalive_space_fallback"])
            self.assertFalse(zo_settings["minimize_ask_context"])

    def test_manager_keeps_plugin_settings_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))

            zo_settings = manager.save_plugin_settings("zo", {"enabled": False, "min_credit": 9})
            swarms_settings = manager.save_plugin_settings("swarms", {"enabled": True, "min_credit": 1})

            self.assertFalse(zo_settings["enabled"])
            self.assertEqual(zo_settings["min_credit"], 9)
            self.assertTrue(swarms_settings["enabled"])
            self.assertEqual(swarms_settings["min_credit"], 1)
            self.assertIs(manager.plugins["zo"].settings, manager.get_plugin_settings("zo"))
            self.assertIs(manager.plugins["swarms"].settings, manager.get_plugin_settings("swarms"))
            self.assertNotEqual(
                manager.plugins["zo"].settings.enabled,
                manager.plugins["swarms"].settings.enabled,
            )

            reloaded = TwoAPIManager(data_dir=Path(tmp))
            self.assertFalse(reloaded.get_plugin_settings("zo").enabled)
            self.assertEqual(reloaded.get_plugin_settings("zo").min_credit, 9)
            self.assertTrue(reloaded.get_plugin_settings("swarms").enabled)
            self.assertEqual(reloaded.get_plugin_settings("swarms").min_credit, 1)


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


    def test_zo_plugin_merges_registered_accounts_from_account_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "account_manager.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, platform TEXT, email TEXT, password TEXT, user_id TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("CREATE TABLE account_credentials (id INTEGER PRIMARY KEY, account_id INTEGER, scope TEXT, provider_name TEXT, credential_type TEXT, key TEXT, value TEXT, is_primary INTEGER, source TEXT, metadata_json TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("CREATE TABLE account_overviews (account_id INTEGER PRIMARY KEY, lifecycle_status TEXT, validity_status TEXT, plan_state TEXT, plan_name TEXT, display_status TEXT, remote_email TEXT, checked_at TEXT, summary_json TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("INSERT INTO accounts VALUES (1, 'zo', 'first@example.com', '', '', '', '')")
                con.execute("INSERT INTO accounts VALUES (2, 'zo', 'second@example.com', '', '', '', '')")
                con.execute("INSERT INTO account_credentials VALUES (1, 1, 'platform', 'zo', 'secret', 'api_key', 'zo_sk_first', 1, 'test', '{}', '', '')")
                con.execute("INSERT INTO account_credentials VALUES (2, 2, 'platform', 'zo', 'secret', 'api_key', 'zo_sk_second', 1, 'test', '{}', '', '')")
                con.execute(
                    "INSERT INTO account_overviews VALUES (1, 'registered', 'unknown', 'unknown', '', 'registered', '', NULL, ?, '', '')",
                    (json.dumps({"legacy_extra": {"openai_proxy_base_url": "https://first.zo.space/v1/zo_sk_first", "credit_result": {"ok": True, "amount": 100}}}),),
                )
                con.commit()
            finally:
                con.close()

            plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(min_credit=1.0), data_dir=root, account_db_path=db_path)
            accounts = plugin.load_accounts()

            emails = {item.email for item in accounts}
            self.assertEqual(emails, {"first@example.com", "second@example.com"})
            first = next(item for item in accounts if item.email == "first@example.com")
            second = next(item for item in accounts if item.email == "second@example.com")
            self.assertEqual(first.base_url, "https://first.zo.space/v1/zo_sk_first")
            self.assertTrue(first.credit_ok)
            self.assertFalse(second.enabled)
            self.assertEqual(second.last_status, "proxy_missing")

    def test_zo_import_accounts_persists_external_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(min_credit=1.0), data_dir=root, account_db_path=root / "account_manager.db")
            record = {
                "email": "imported@example.com",
                "api_key": "zo_sk_imported",
                "credit_result": {"ok": True, "amount": 100},
                "workspace_result": {"workspace": {"handle": "imported", "origin": "https://imported.zo.computer"}},
                "cookies": {"access_token": "access-imported", "refresh_token": "refresh-imported"},
            }

            result = plugin.import_accounts(records=[record], source="unit-test")

            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["skipped"], 0)
            saved = json.loads((root / "zo_e2e_result.json").read_text(encoding="utf-8"))
            self.assertIsInstance(saved, list)
            self.assertEqual(saved[0]["email"], "imported@example.com")
            self.assertEqual(saved[0]["import_source"], "unit-test")
            accounts = plugin.load_accounts()
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0].email, "imported@example.com")
            self.assertTrue(accounts[0].enabled)
            self.assertEqual(accounts[0].last_status, "direct_ready")
            self.assertEqual(accounts[0].metadata["cookies"]["access_token"], "access-imported")

    def test_zo_push_accounts_posts_local_records_to_remote_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = {
                "email": "push@example.com",
                "api_key": "zo_sk_push",
                "credit_result": {"ok": True, "amount": 100},
                "workspace_result": {"workspace": {"handle": "push", "origin": "https://push.zo.computer"}},
                "cookies": {"access_token": "access-push", "refresh_token": "refresh-push"},
            }
            (root / "zo_e2e_result.json").write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
            response = Mock(status_code=200, ok=True, text='{"ok":true,"imported":1}')
            response.json.return_value = {"ok": True, "imported": 1}
            transport = Mock()
            transport.post.return_value = response
            plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(), data_dir=root, account_db_path=root / "account_manager.db", transport=transport)

            result = plugin.push_accounts("http://linux.example:8000", source="windows-register", emails=["push@example.com"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["pushed"], 1)
            self.assertEqual(transport.post.call_args.args[0], "http://linux.example:8000/api/2api/plugins/zo/import")
            body = transport.post.call_args.kwargs["json"]
            self.assertEqual(body["source"], "windows-register")
            self.assertEqual(body["records"][0]["email"], "push@example.com")
            self.assertEqual(body["records"][0]["cookies"]["access_token"], "access-push")
            self.assertEqual(transport.post.call_args.kwargs["headers"]["Content-Type"], "application/json")

    def test_zo_load_accounts_enables_token_only_direct_account_without_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "account_manager.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, platform TEXT, email TEXT, password TEXT, user_id TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("CREATE TABLE account_credentials (id INTEGER PRIMARY KEY, account_id INTEGER, scope TEXT, provider_name TEXT, credential_type TEXT, key TEXT, value TEXT, is_primary INTEGER, source TEXT, metadata_json TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("CREATE TABLE account_overviews (account_id INTEGER PRIMARY KEY, lifecycle_status TEXT, validity_status TEXT, plan_state TEXT, plan_name TEXT, display_status TEXT, remote_email TEXT, checked_at TEXT, summary_json TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("INSERT INTO accounts VALUES (1, 'zo', 'direct-only@example.com', '', '', '', '')")
                con.execute("INSERT INTO account_credentials VALUES (1, 1, 'platform', 'zo', 'secret', 'api_key', 'zo_sk_direct', 1, 'test', '{}', '', '')")
                con.execute(
                    "INSERT INTO account_overviews VALUES (1, 'registered', 'unknown', 'unknown', '', 'registered', '', NULL, ?, '', '')",
                    (json.dumps({
                        "legacy_extra": {
                            "api_key": "zo_sk_direct",
                            "credit_result": {"ok": True, "amount": 100},
                            "workspace_result": {"workspace": {"handle": "directonly", "origin": "https://directonly.zo.computer"}},
                            "cookies": {"access_token": "access-direct", "refresh_token": "refresh-direct"},
                        }
                    }),),
                )
                con.commit()
            finally:
                con.close()

            plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(min_credit=1.0), data_dir=root, account_db_path=db_path)
            accounts = plugin.load_accounts()

        self.assertEqual(len(accounts), 1)
        account = accounts[0]
        self.assertEqual(account.email, "direct-only@example.com")
        self.assertEqual(account.base_url, "")
        self.assertTrue(account.enabled)
        self.assertTrue(account.credit_ok)
        self.assertEqual(account.last_status, "direct_ready")
        self.assertEqual(account.metadata["cookies"]["access_token"], "access-direct")
        self.assertIs(plugin.select_direct_account(), account)



    def test_swarms_plugin_loads_keys_from_credentials_file_and_hides_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.joinpath("swarms_credentials.json").write_text(
                json.dumps([
                    {
                        "email": "demo@swarms.test",
                        "api_key": "sk-secret-swarms-demo-12345678901234567890",
                        "credit_result": {"ok": True, "amount": 12.5},
                        "user_id": "user-demo",
                        "ok": True,
                    }
                ]),
                encoding="utf-8",
            )
            plugin = SwarmsTwoAPIPlugin(settings=TwoAPISettings(min_credit=1.0), data_dir=root)
            accounts = plugin.load_accounts()

            self.assertEqual(len(accounts), 1)
            account = accounts[0]
            self.assertEqual(account.plugin, "swarms")
            self.assertEqual(account.email, "demo@swarms.test")
            self.assertEqual(account.api_key, "sk-secret-swarms-demo-12345678901234567890")
            self.assertEqual(account.base_url, "https://api.swarms.world/v1")
            self.assertEqual(account.credit_amount, 12.5)
            rendered = json.dumps(account.to_public(), ensure_ascii=False)
            self.assertNotIn("sk-secret-swarms-demo-12345678901234567890", rendered)

    def test_swarms_forward_models_converts_available_models_to_openai_catalog(self):
        upstream = Mock(
            status_code=200,
            ok=True,
            headers={"content-type": "application/json"},
        )
        upstream.json.return_value = {"success": True, "models": ["gpt-4o", "claude-opus-4-6"]}
        transport = Mock()
        transport.get.return_value = upstream
        plugin = SwarmsTwoAPIPlugin(settings=TwoAPISettings(max_retries=1), transport=transport)
        plugin.accounts = [
            TwoAPIAccount(
                plugin="swarms",
                email="demo@swarms.test",
                base_url="https://api.swarms.world/v1",
                api_key="sk-secret-swarms-demo-12345678901234567890",
                credit_amount=100,
                credit_ok=True,
            )
        ]

        response = plugin.forward_models()
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["object"], "list")
        self.assertEqual(transport.get.call_args.args[0], "https://api.swarms.world/v1/models/available")
        model_ids = {item["id"] for item in data["data"]}
        self.assertIn("gpt-4o", model_ids)
        self.assertIn("claude-opus-4-6", model_ids)

    def test_swarms_forward_models_returns_local_catalog_when_available_models_unavailable(self):
        transport = Mock()
        transport.get.side_effect = TimeoutError("models timeout")
        plugin = SwarmsTwoAPIPlugin(settings=TwoAPISettings(max_retries=1), transport=transport)
        plugin.accounts = [
            TwoAPIAccount(
                plugin="swarms",
                email="demo@swarms.test",
                base_url="https://api.swarms.world/v1",
                api_key="sk-secret-swarms-demo-12345678901234567890",
                credit_amount=100,
                credit_ok=True,
            )
        ]

        response = plugin.forward_models()
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["object"], "list")
        self.assertIn("claude-opus-4-6", {item["id"] for item in data["data"]})

    def test_swarms_import_accounts_uses_common_external_import_framework(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = SwarmsTwoAPIPlugin(settings=TwoAPISettings(), data_dir=Path(tmp))
            repository = Mock()
            repository.import_lines.return_value = 1
            result = plugin.import_accounts(
                lines=["demo@swarms.test|sk-import-swarms-demo-12345678901234567890|base_url=https://api.swarms.world/v1"],
                source="unit-test",
                repository=repository,
            )

            self.assertEqual(result["created"], 1)
            self.assertEqual(result["accepted"], 1)
            accounts = plugin.load_accounts()
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0].email, "demo@swarms.test")
            self.assertEqual(accounts[0].api_key, "sk-import-swarms-demo-12345678901234567890")
            self.assertEqual(accounts[0].base_url, "https://api.swarms.world/v1")
            repository.import_lines.assert_called_once()

    def test_swarms_push_accounts_posts_local_records_to_remote_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = {
                "email": "push@swarms.test",
                "api_key": "sk-push-swarms-demo-12345678901234567890",
                "credit_amount": 100,
                "openai_base_url": "https://api.swarms.world/v1",
                "user_id": "user-push",
                "ok": True,
            }
            (root / "swarms_credentials.json").write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
            response = Mock(status_code=200, ok=True, text='{"ok":true,"created":1}')
            response.json.return_value = {"ok": True, "created": 1}
            transport = Mock()
            transport.post.return_value = response
            plugin = SwarmsTwoAPIPlugin(settings=TwoAPISettings(), data_dir=root, account_db_path=root / "account_manager.db", transport=transport)

            result = plugin.push_accounts("http://linux.example:8000", source="windows-register", emails=["push@swarms.test"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["pushed"], 1)
            self.assertEqual(transport.post.call_args.args[0], "http://linux.example:8000/api/2api/plugins/swarms/import")
            body = transport.post.call_args.kwargs["json"]
            self.assertEqual(body["source"], "windows-register")
            self.assertEqual(body["records"][0]["email"], "push@swarms.test")
            self.assertEqual(body["records"][0]["api_key"], "sk-push-swarms-demo-12345678901234567890")
            self.assertEqual(body["records"][0]["openai_base_url"], "https://api.swarms.world/v1")
            self.assertEqual(body["records"][0]["user_id"], "user-push")
            self.assertEqual(transport.post.call_args.kwargs["headers"]["Content-Type"], "application/json")

    def test_swarms_refill_accounts_creates_existing_register_task(self):
        plugin = SwarmsTwoAPIPlugin(settings=TwoAPISettings())
        fake_task = {"id": "task_swarms_refill", "platform": "swarms", "status": "pending"}
        with patch("services.twoapi.plugins.swarms.create_register_task", create=True) as create_mock, patch(
            "services.twoapi.plugins.swarms.task_runtime", create=True
        ) as runtime_mock:
            create_mock.return_value = fake_task
            result = plugin.refill_accounts(count=3, concurrency=2, extra={"mail_provider": "luckmail"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["task"], fake_task)
        payload = create_mock.call_args.args[0]
        self.assertEqual(payload["platform"], "swarms")
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["concurrency"], 2)
        self.assertTrue(payload["extra"]["twoapi_auto_refill"])
        self.assertEqual(payload["extra"]["mail_provider"], "luckmail")
        runtime_mock.wake_up.assert_called_once()

    def test_swarms_forward_chat_calls_native_openai_compatible_endpoint(self):
        transport = Mock()
        transport.post.return_value = Mock(
            status_code=200,
            ok=True,
            content=b'{"choices":[{"message":{"content":"OK"}}]}',
            text='{"choices":[{"message":{"content":"OK"}}]}',
            headers={"content-type": "application/json"},
            close=Mock(),
        )
        plugin = SwarmsTwoAPIPlugin(settings=TwoAPISettings(), transport=transport)
        plugin.accounts = [
            TwoAPIAccount(
                plugin="swarms",
                email="demo@swarms.test",
                base_url="https://api.swarms.world/v1",
                api_key="sk-secret-swarms-demo-12345678901234567890",
                credit_amount=100,
                credit_ok=True,
            )
        ]

        response = plugin.forward_chat({"model": "gpt-4o", "messages": [{"role": "user", "content": "ping"}]})

        self.assertEqual(response.status_code, 200)
        args, kwargs = transport.post.call_args
        self.assertEqual(args[0], "https://api.swarms.world/v1/chat/completions")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer sk-secret-swarms-demo-12345678901234567890")
        self.assertEqual(kwargs["headers"]["x-api-key"], "sk-secret-swarms-demo-12345678901234567890")
        self.assertEqual(kwargs["json"]["model"], "gpt-4o")
        self.assertFalse(kwargs["stream"])


    def test_swarms_twoapi_push_local_imports_current_registration_account(self):
        from types import SimpleNamespace
        from application.tasks import _auto_push_swarms_twoapi

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

        account = SimpleNamespace(
            platform="swarms",
            email="local@swarms.test",
            password="pw",
            user_id="user-local",
            token="sk-local-swarms-demo-12345678901234567890",
            extra={"api_key": "sk-local-swarms-demo-12345678901234567890"},
        )
        logger = Logger()
        manager = FakeManager()

        with patch("services.twoapi.manager.get_twoapi_manager", return_value=manager):
            _auto_push_swarms_twoapi(logger, account, {"twoapi_push_mode": "local"})

        self.assertEqual(manager.calls[0]["plugin"], "swarms")
        self.assertEqual(manager.calls[0]["source"], "registration-local")
        self.assertEqual(manager.calls[0]["records"][0]["email"], "local@swarms.test")
        self.assertEqual(manager.calls[0]["records"][0]["api_key"], "sk-local-swarms-demo-12345678901234567890")
        self.assertTrue(any("本地导入完成" in message for _, message in logger.lines))

    def test_swarms_twoapi_push_remote_posts_current_registration_account(self):
        from types import SimpleNamespace
        from application.tasks import _auto_push_swarms_twoapi

        class Logger:
            def __init__(self):
                self.lines = []

            def log(self, message, level="info", **kwargs):
                self.lines.append((level, message))

        response = Mock(status_code=200, ok=True, text='{"ok":true,"created":1}')
        response.json.return_value = {"ok": True, "created": 1}
        plugin = Mock()
        plugin._push_target_import_url.return_value = "http://linux.example:8000/api/2api/plugins/swarms/import"
        plugin.transport.post.return_value = response
        manager = Mock()
        manager.get_plugin.return_value = plugin
        account = SimpleNamespace(
            platform="swarms",
            email="remote@swarms.test",
            password="pw",
            user_id="user-remote",
            token="sk-remote-swarms-demo-12345678901234567890",
            extra={"api_key": "sk-remote-swarms-demo-12345678901234567890"},
        )
        logger = Logger()

        with patch("services.twoapi.manager.get_twoapi_manager", return_value=manager):
            _auto_push_swarms_twoapi(
                logger,
                account,
                {"twoapi_push_mode": "remote", "twoapi_push_target_url": "http://linux.example:8000"},
            )

        plugin.transport.post.assert_called_once()
        body = plugin.transport.post.call_args.kwargs["json"]
        self.assertEqual(body["source"], "registration-remote")
        self.assertEqual(body["records"][0]["email"], "remote@swarms.test")
        self.assertEqual(body["records"][0]["api_key"], "sk-remote-swarms-demo-12345678901234567890")
        self.assertTrue(any("远端推送完成" in message for _, message in logger.lines))

    def test_swarms_auto_export_after_registration_writes_twoapi_credentials(self):
        from types import SimpleNamespace
        import os
        from application.tasks import _auto_export_swarms_key

        class Logger:
            def __init__(self):
                self.lines = []

            def log(self, message, level="info", **kwargs):
                self.lines.append((level, message))

        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                logger = Logger()
                account = SimpleNamespace(
                    platform="swarms",
                    email="auto@swarms.test",
                    password="pw",
                    user_id="user-auto",
                    token="sk-auto-swarms-demo-12345678901234567890",
                    extra={"api_key": "sk-auto-swarms-demo-12345678901234567890"},
                )
                _auto_export_swarms_key(logger, account)
                credentials = json.loads((Path(tmp) / "output" / "swarms_credentials.json").read_text(encoding="utf-8"))
                keys_text = (Path(tmp) / "output" / "swarms_keys.txt").read_text(encoding="utf-8")
            finally:
                os.chdir(old_cwd)

        self.assertEqual(credentials[0]["email"], "auto@swarms.test")
        self.assertEqual(credentials[0]["api_key"], "sk-auto-swarms-demo-12345678901234567890")
        self.assertIn("sk-auto-swarms-demo-12345678901234567890", keys_text)

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


    def test_zo_plugin_recovers_proxy_url_from_deploy_verify_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "account_manager.db"
            con = sqlite3.connect(db_path)
            try:
                con.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, platform TEXT, email TEXT, password TEXT, user_id TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("CREATE TABLE account_credentials (id INTEGER PRIMARY KEY, account_id INTEGER, scope TEXT, provider_name TEXT, credential_type TEXT, key TEXT, value TEXT, is_primary INTEGER, source TEXT, metadata_json TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("CREATE TABLE account_overviews (account_id INTEGER PRIMARY KEY, lifecycle_status TEXT, validity_status TEXT, plan_state TEXT, plan_name TEXT, display_status TEXT, remote_email TEXT, checked_at TEXT, summary_json TEXT, created_at TEXT, updated_at TEXT)")
                con.execute("INSERT INTO accounts VALUES (1, 'zo', 'recover@example.com', '', '', '', '')")
                con.execute("INSERT INTO account_credentials VALUES (1, 1, 'platform', 'zo', 'secret', 'api_key', 'zo_sk_recover', 1, 'test', '{}', '', '')")
                con.commit()
            finally:
                con.close()
            (root / "zo_proxy_direct_deploy_verify.json").write_text(
                json.dumps({
                    "handle": "recoverhandle",
                    "persona_id": "persona-1",
                    "models": {"url": "https://recoverhandle.zo.space/v1/zo_sk_recover/models", "ok": True},
                    "chat": {"url": "https://recoverhandle.zo.space/v1/zo_sk_recover/chat/completions", "ok": True},
                }),
                encoding="utf-8",
            )

            plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(min_credit=1.0), data_dir=root, account_db_path=db_path)
            accounts = plugin.load_accounts()

            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0].base_url, "https://recoverhandle.zo.space/v1/zo_sk_recover")
            self.assertTrue(accounts[0].enabled)
            self.assertTrue(accounts[0].enabled)
            self.assertTrue(root.joinpath("zo_proxy_urls.txt").exists())

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


    def test_zo_forward_chat_uses_direct_ask_with_access_token(self):
        class FakeAskResponse:
            status_code = 200
            ok = True
            headers = {"content-type": "text/event-stream; charset=utf-8", "x-conversation-id": "con_test"}
            text = ""

            def iter_lines(self, decode_unicode=False):
                lines = [
                    'event: PartStartEvent',
                    'data: {"index": 0, "part": {"content": "pong", "part_kind": "text"}}',
                    '',
                    'event: End',
                    'data: {"data": {"output": "pong"}}',
                    '',
                ]
                for line in lines:
                    yield line if decode_unicode else line.encode("utf-8")

        transport = Mock()
        transport.post.return_value = FakeAskResponse()
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(max_retries=1), transport=transport)
        plugin.accounts = [
            TwoAPIAccount(
                plugin="zo",
                email="demo@example.com",
                base_url="https://demo.zo.space/v1/zo_sk_demo",
                api_key="zo_sk_demo",
                credit_amount=100.0,
                credit_ok=True,
                metadata={
                    "cookies": {"access_token": "access-demo"},
                    "workspace_origin": "https://demo.zo.computer",
                    "workspace_handle": "demo",
                },
            )
        ]

        response = plugin.forward_chat({"model": "zo:openai/gpt-5.5", "messages": [{"role": "user", "content": "只回复 pong"}], "max_tokens": 16})

        self.assertEqual(response.status_code, 200)
        self.assertIn("chat.completion", response.text)
        self.assertIn("pong", response.text)
        args, kwargs = transport.post.call_args
        self.assertEqual(args[0], "https://api.zo.computer/ask")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer access-demo")
        self.assertEqual(kwargs["headers"]["X-Zo-Workspace-Origin"], "https://demo.zo.computer")
        self.assertEqual(kwargs["json"]["q"], "User: 只回复 pong")
        self.assertEqual(kwargs["json"]["model_name"], "zo:openai/gpt-5.5")
        self.assertEqual(kwargs["json"]["mode"], "chat")
        self.assertEqual(kwargs["json"]["context_paths"], [])
        self.assertEqual(kwargs["json"]["command_paths"], [])

    def test_zo_direct_ask_creates_minimal_persona_before_request(self):
        class FakeJSONResponse:
            def __init__(self, status_code: int, data: object) -> None:
                self.status_code = status_code
                self.ok = 200 <= status_code < 400
                self._data = data
                self.text = json.dumps(data, ensure_ascii=False)
                self.headers = {"content-type": "application/json"}

            def json(self):
                return self._data

        class FakeAskResponse:
            status_code = 200
            ok = True
            headers = {"content-type": "text/event-stream; charset=utf-8"}
            text = ""

            def iter_lines(self, decode_unicode=False):
                rows = [
                    'event: PartStartEvent',
                    'data: {"index": 0, "part": {"content": "pong", "part_kind": "text"}}',
                    '',
                    'event: End',
                    'data: {"data": {"output": "pong"}}',
                    '',
                ]
                for row in rows:
                    yield row if decode_unicode else row.encode("utf-8")

        transport = Mock()
        transport.get.return_value = FakeJSONResponse(200, [])
        transport.post.side_effect = [
            FakeJSONResponse(200, {"id": "persona-min", "name": "2API Minimal", "prompt": ".", "scopes": ["all"]}),
            FakeJSONResponse(200, {"success": True}),
            FakeAskResponse(),
        ]
        transport.put.return_value = FakeJSONResponse(200, {"id": "persona-min", "name": "2API Minimal", "prompt": ".", "scopes": []})
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(max_retries=1, minimize_ask_context=True), transport=transport)
        account = TwoAPIAccount(
            plugin="zo",
            email="minimal@example.com",
            base_url="",
            api_key="zo_sk_minimal",
            credit_amount=100.0,
            credit_ok=True,
            metadata={
                "cookies": {"access_token": "access-minimal"},
                "workspace_origin": "https://minimal.zo.computer",
                "workspace_handle": "minimal",
            },
        )

        response = plugin._direct_ask(account, {"model": "zo:openai/gpt-5.5", "messages": [{"role": "user", "content": "ping"}]})

        self.assertEqual(response.status_code, 200)
        self.assertIn("pong", response.text)
        transport.get.assert_called_once()
        self.assertEqual(transport.get.call_args.args[0], "https://api.zo.computer/personas/")
        self.assertEqual(transport.post.call_args_list[0].args[0], "https://api.zo.computer/personas/")
        self.assertEqual(transport.put.call_args.args[0], "https://api.zo.computer/personas/persona-min")
        self.assertEqual(transport.put.call_args.kwargs["json"]["scopes"], [])
        self.assertEqual(transport.post.call_args_list[1].args[0], "https://api.zo.computer/personas/active/persona-min")
        self.assertEqual(transport.post.call_args_list[1].kwargs["json"], {"conversation_type": "main"})
        self.assertEqual(transport.post.call_args_list[2].args[0], "https://api.zo.computer/ask")
        self.assertEqual(account.metadata["zo_minimal_persona_id"], "persona-min")
        self.assertTrue(account.metadata["zo_minimal_persona_active"])

    def test_zo_direct_ask_refreshes_access_token_after_401(self):
        class FakeAskResponse:
            def __init__(self, status_code: int, ok: bool, text: str = "") -> None:
                self.status_code = status_code
                self.ok = ok
                self.text = text
                self.headers = {"content-type": "text/event-stream; charset=utf-8"}

            def iter_lines(self, decode_unicode=False):
                rows = [
                    'event: PartStartEvent',
                    'data: {"index": 0, "part": {"content": "pong", "part_kind": "text"}}',
                    '',
                    'event: End',
                    'data: {"data": {"output": "pong"}}',
                    '',
                ]
                for row in rows:
                    yield row if decode_unicode else row.encode("utf-8")

        transport = Mock()
        transport.post.side_effect = [
            FakeAskResponse(401, False, "expired"),
            Mock(status_code=200, ok=True, text=json.dumps({"access_token": "access-fresh", "refresh_token": "refresh-new"}), json=Mock(return_value={"access_token": "access-fresh", "refresh_token": "refresh-new"})),
            FakeAskResponse(200, True),
        ]
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(max_retries=1), transport=transport)
        account = TwoAPIAccount(
            plugin="zo",
            email="refresh@example.com",
            base_url="",
            api_key="zo_sk_refresh",
            credit_amount=100.0,
            credit_ok=True,
            metadata={
                "cookies": {"access_token": "access-old", "refresh_token": "refresh-old"},
                "workspace_origin": "https://refresh.zo.computer",
                "workspace_handle": "refresh",
            },
        )

        response = plugin._direct_ask(account, {"model": "zo:openai/gpt-5.5", "messages": [{"role": "user", "content": "ping"}]})

        self.assertEqual(response.status_code, 200)
        self.assertIn("pong", response.text)
        self.assertEqual(account.metadata["cookies"]["access_token"], "access-fresh")
        self.assertEqual(account.metadata["cookies"]["refresh_token"], "refresh-new")
        self.assertEqual(transport.post.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer access-old")
        self.assertIn("refresh_token", transport.post.call_args_list[1].kwargs["json"])
        self.assertEqual(transport.post.call_args_list[2].kwargs["headers"]["Authorization"], "Bearer access-fresh")


    def test_zo_forward_chat_skips_accounts_without_access_token_for_direct_mode(self):
        class FakeAskResponse:
            status_code = 200
            ok = True
            headers = {"content-type": "text/event-stream; charset=utf-8"}
            text = ""

            def iter_lines(self, decode_unicode=False):
                lines = [
                    'event: PartStartEvent',
                    'data: {"index": 0, "part": {"content": "pong", "part_kind": "text"}}',
                    '',
                    'event: End',
                    'data: {"data": {"output": "pong"}}',
                    '',
                ]
                for line in lines:
                    yield line if decode_unicode else line.encode("utf-8")

        transport = Mock()
        transport.post.return_value = FakeAskResponse()
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(max_retries=2, wake_timeout=60), transport=transport)
        plugin._ensure_alive = Mock(side_effect=AssertionError("不应唤醒缺少 access_token 的 Space fallback"))
        plugin.accounts = [
            TwoAPIAccount(
                plugin="zo",
                email="no-token@example.com",
                base_url="https://slow.zo.space/v1/zo_sk_slow",
                api_key="zo_sk_slow",
                credit_amount=100.0,
                credit_ok=True,
            ),
            TwoAPIAccount(
                plugin="zo",
                email="direct@example.com",
                base_url="",
                api_key="zo_sk_direct",
                credit_amount=100.0,
                credit_ok=True,
                metadata={
                    "cookies": {"access_token": "access-direct"},
                    "workspace_origin": "https://direct.zo.computer",
                    "workspace_handle": "direct",
                },
            ),
        ]

        response = plugin.forward_chat({"model": "zo:openai/gpt-5.5", "messages": [{"role": "user", "content": "只回复 pong"}]})

        self.assertEqual(response.status_code, 200)
        self.assertIn("pong", response.text)
        plugin._ensure_alive.assert_not_called()
        self.assertEqual(transport.post.call_args.args[0], "https://api.zo.computer/ask")
        self.assertEqual(transport.post.call_args.kwargs["headers"]["Authorization"], "Bearer access-direct")


    def test_zo_forward_models_skips_proxy_only_accounts_before_direct_catalog(self):
        class FakeJSONResponse:
            status_code = 200
            ok = True
            headers = {"content-type": "application/json"}
            text = '{"models":[{"model_name":"zo:test/direct-model","label":"Direct Model"}]}'

            def json(self):
                return {"models": [{"model_name": "zo:test/direct-model", "label": "Direct Model"}]}

        transport = Mock()
        transport.get.return_value = FakeJSONResponse()
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(max_retries=1), transport=transport)
        plugin.accounts = [
            TwoAPIAccount(
                plugin="zo",
                email="proxy-only@example.com",
                base_url="https://proxyonly.zo.space/v1/zo_sk_proxy",
                api_key="zo_sk_proxy",
                credit_amount=100.0,
                credit_ok=True,
            ),
            TwoAPIAccount(
                plugin="zo",
                email="direct@example.com",
                base_url="",
                api_key="zo_sk_direct",
                credit_amount=100.0,
                credit_ok=True,
                metadata={
                    "cookies": {"access_token": "access-direct"},
                    "workspace_origin": "https://direct.zo.computer",
                    "workspace_handle": "direct",
                },
            ),
        ]

        response = plugin.forward_models()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], "zo:test/direct-model")
        self.assertEqual(transport.get.call_count, 1)
        self.assertEqual(transport.get.call_args.args[0], "https://api.zo.computer/models/available")

    def test_zo_forward_models_prefers_direct_available_models(self):
        class FakeJSONResponse:
            status_code = 200
            ok = True
            headers = {"content-type": "application/json"}
            text = '{"models":[{"model_name":"zo:test/model-a","label":"Model A"}]}'

            def json(self):
                return {"models": [{"model_name": "zo:test/model-a", "label": "Model A"}]}

        transport = Mock()
        transport.get.return_value = FakeJSONResponse()
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(max_retries=1), transport=transport)
        plugin.accounts = [
            TwoAPIAccount(
                plugin="zo",
                email="models@example.com",
                base_url="https://models.zo.space/v1/zo_sk_models",
                api_key="zo_sk_models",
                credit_amount=100.0,
                credit_ok=True,
                metadata={
                    "cookies": {"access_token": "access-models"},
                    "workspace_origin": "https://models.zo.computer",
                    "workspace_handle": "models",
                },
            )
        ]

        response = plugin.forward_models()

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["object"], "list")
        self.assertEqual(data["data"][0]["id"], "zo:test/model-a")
        self.assertEqual(transport.get.call_args.args[0], "https://api.zo.computer/models/available")
        self.assertNotIn(".zo.space", transport.get.call_args.args[0])

    def test_zo_forward_models_uses_fast_local_catalog_without_wake_loop(self):
        transport = Mock()
        transport.get.side_effect = TimeoutError("models timeout")
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(auto_wake=True, wake_timeout=60, request_timeout=90, max_retries=1), transport=transport)
        plugin._wake_account = Mock()
        plugin._redeploy_account_proxy = Mock()
        plugin.accounts = [
            TwoAPIAccount(plugin="zo", email="demo@example.com", base_url="https://demo.zo.space/v1/zo_sk_demo", api_key="zo_sk_demo", credit_amount=100.0, credit_ok=True)
        ]

        response = plugin.forward_models()

        self.assertEqual(response.status_code, 200)
        self.assertIn("zo:openai/gpt-5.5", {item["id"] for item in response.json()["data"]})
        plugin._wake_account.assert_not_called()
        plugin._redeploy_account_proxy.assert_not_called()
        self.assertLessEqual(transport.get.call_args.kwargs.get("timeout"), 5.0)

    def test_zo_forward_models_returns_local_catalog_when_upstream_times_out(self):
        transport = Mock()
        transport.get.side_effect = TimeoutError("models timeout")
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(auto_wake=False, max_retries=1), transport=transport)
        plugin.accounts = [
            TwoAPIAccount(plugin="zo", email="demo@example.com", base_url="https://demo.zo.space/v1/zo_sk_demo", api_key="zo_sk_demo", credit_amount=100.0, credit_ok=True)
        ]

        response = plugin.forward_models()
        data = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["object"], "list")
        self.assertIn("zo:openai/gpt-5.5", {item["id"] for item in data["data"]})
        self.assertIn("本地模型目录", "\n".join(plugin.recent_logs(limit=20)))

    def test_zo_keepalive_skips_space_fallback_by_default(self):
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(auto_wake=True))
        plugin._ensure_alive = Mock(side_effect=AssertionError("默认不应保活 Zo Space fallback"))
        plugin.accounts = [
            TwoAPIAccount(
                plugin="zo",
                email="direct@example.com",
                base_url="https://direct.zo.space/v1/zo_sk_direct",
                api_key="zo_sk_direct",
                credit_amount=100.0,
                credit_ok=True,
                metadata={"cookies": {"access_token": "access-direct"}},
            ),
            TwoAPIAccount(
                plugin="zo",
                email="fallback@example.com",
                base_url="https://fallback.zo.space/v1/zo_sk_fallback",
                api_key="zo_sk_fallback",
                credit_amount=100.0,
                credit_ok=True,
            ),
        ]

        result = plugin.keepalive_once()

        self.assertEqual(result, {"checked": 0, "alive": 0, "recovered": 0, "failed": 0})
        plugin._ensure_alive.assert_not_called()
        self.assertIn("跳过 Zo Space 保活", "\n".join(plugin.recent_logs(limit=20)))

    def test_zo_keepalive_can_probe_space_fallback_when_explicitly_enabled(self):
        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(auto_wake=True, keepalive_space_fallback=True))
        plugin._ensure_alive = Mock()
        plugin.accounts = [
            TwoAPIAccount(
                plugin="zo",
                email="fallback@example.com",
                base_url="https://fallback.zo.space/v1/zo_sk_fallback",
                api_key="zo_sk_fallback",
                credit_amount=100.0,
                credit_ok=True,
            )
        ]

        result = plugin.keepalive_once()

        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["alive"], 1)
        plugin._ensure_alive.assert_called_once_with(plugin.accounts[0])

    def test_zo_streaming_direct_ask_yields_first_chunk_before_upstream_finishes(self):
        class FakeStreamingAskResponse:
            status_code = 200
            ok = True
            headers = {"content-type": "text/event-stream; charset=utf-8"}
            text = ""

            def __init__(self):
                self.iter_started = False
                self.closed = False

            def iter_lines(self, decode_unicode=False):
                self.iter_started = True
                rows = [
                    'event: PartStartEvent',
                    'data: {"index": 0, "part": {"content": "PO", "part_kind": "text"}}',
                    '',
                    'event: PartDeltaEvent',
                    'data: {"index": 0, "delta": {"content_delta": "NG", "part_delta_kind": "text"}}',
                    '',
                    'event: End',
                    'data: {"data": {"output": "PONG"}}',
                    '',
                ]
                for row in rows:
                    yield row if decode_unicode else row.encode("utf-8")

            def close(self):
                self.closed = True

        plugin = ZoTwoAPIPlugin(settings=TwoAPISettings(max_retries=1))
        upstream = FakeStreamingAskResponse()
        response = plugin._openai_stream_response_from_zo("zo:openai/gpt-5.5", upstream)

        self.assertFalse(upstream.iter_started)
        iterator = response.iter_content(chunk_size=None)
        first = next(iterator).decode("utf-8")
        self.assertIn('"role": "assistant"', first)
        self.assertFalse(upstream.iter_started)

        rest = b"".join(iterator).decode("utf-8")
        self.assertTrue(upstream.iter_started)
        self.assertIn('"content": "PO"', rest)
        self.assertIn('"content": "NG"', rest)
        self.assertIn("data: [DONE]", rest)
        self.assertTrue(upstream.closed)


    def test_zo_import_account_record_from_registration_account(self):
        from application.tasks import _build_zo_twoapi_record_from_account
        from core.base_platform import Account

        account = Account(
            platform="zo",
            email="reg@example.com",
            password="pw",
            token="zo_sk_reg",
            extra={
                "api_key": "zo_sk_reg",
                "cookies": {"access_token": "access-reg", "refresh_token": "refresh-reg"},
                "workspace_result": {"workspace": {"handle": "regspace", "origin": "https://regspace.zo.computer"}},
                "credit_result": {"ok": True, "amount": 100.0},
                "card_binding_result": {"ok": True},
            },
        )

        record = _build_zo_twoapi_record_from_account(account, source="registration-local")

        self.assertEqual(record["email"], "reg@example.com")
        self.assertEqual(record["api_key"], "zo_sk_reg")
        self.assertEqual(record["cookies"]["access_token"], "access-reg")
        self.assertEqual(record["workspace_result"]["workspace"]["handle"], "regspace")
        self.assertEqual(record["import_source"], "registration-local")
        self.assertIn("saved_at", record)

    def test_zo_auto_push_local_imports_current_registration_account(self):
        from application.tasks import _auto_push_zo_twoapi
        from core.base_platform import Account

        class FakeLogger:
            task_id = "task-test"

            def __init__(self):
                self.messages = []

            def log(self, message, *, level="info", event_type="log", detail=None):
                self.messages.append((level, message))

        class FakeManager:
            def __init__(self):
                self.calls = []

            def import_plugin_accounts(self, plugin, *, records=None, lines=None, source="external"):
                self.calls.append({"plugin": plugin, "records": records or [], "lines": lines or [], "source": source})
                return {"ok": True, "imported": 1, "updated": 0}

        account = Account(
            platform="zo",
            email="local@example.com",
            password="pw",
            token="zo_sk_local",
            extra={
                "cookies": {"access_token": "access-local"},
                "workspace_result": {"workspace": {"handle": "local", "origin": "https://local.zo.computer"}},
                "credit_result": {"ok": True, "amount": 100.0},
            },
        )
        fake_manager = FakeManager()
        logger = FakeLogger()

        with patch("services.twoapi.manager.get_twoapi_manager", return_value=fake_manager):
            _auto_push_zo_twoapi(logger, account, {"twoapi_push_mode": "local"})

        self.assertEqual(len(fake_manager.calls), 1)
        call = fake_manager.calls[0]
        self.assertEqual(call["plugin"], "zo")
        self.assertEqual(call["source"], "registration-local")
        self.assertEqual(call["records"][0]["email"], "local@example.com")
        self.assertEqual(call["records"][0]["cookies"]["access_token"], "access-local")
        self.assertTrue(any("本地导入完成" in message for _, message in logger.messages))


    def test_zo_import_account_record_returns_empty_for_non_zo(self):
        from application.tasks import _build_zo_twoapi_record_from_account
        from core.base_platform import Account

        account = Account(platform="chatgpt", email="x@example.com", password="pw", token="sk-x")

        self.assertEqual(_build_zo_twoapi_record_from_account(account), {})


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
