from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.twoapi.manager import TwoAPIManager
from services.twoapi.models import TwoAPIAccount, TwoAPISettings
from services.twoapi.plugins.thesys import THESYS_DEFAULT_MODEL, THESYS_OPENAI_BASE_URL, ThesysTwoAPIPlugin


class TwoAPIManagerSettingsTests(unittest.TestCase):
    def test_manager_exposes_only_thesys_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            self.assertEqual(list(manager.plugins.keys()), ["thesys"])
            self.assertEqual(manager.status()["listen_urls"], ["http://127.0.0.1:6543/thesys/v1"])
            self.assertEqual(manager.status()["listen"], "http://127.0.0.1:6543/thesys/v1")

    def test_manager_filters_thesys_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            settings = manager.save_plugin_settings(
                "thesys",
                {"enabled": True, "auto_refill": True, "auto_wake": False, "wake_timeout": 3, "min_credit": 9},
            )
            self.assertTrue(settings["enabled"])
            self.assertTrue(settings["auto_refill"])
            self.assertEqual(settings["min_credit"], 9)
            self.assertNotIn("auto_wake", settings)
            self.assertNotIn("wake_timeout", settings)

            raw_settings = json.loads((Path(tmp) / "twoapi_settings.json").read_text(encoding="utf-8"))
            self.assertEqual(list(raw_settings["plugins"].keys()), ["thesys"])
            self.assertNotIn("auto_wake", raw_settings["plugins"]["thesys"])

    def test_key_store_creates_multiple_enabled_thesys_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TwoAPIManager(data_dir=Path(tmp))
            first = manager.create_key(plugin="thesys", note="one")
            second = manager.create_key(plugin="thesys", note="two")
            self.assertNotEqual(first["key"], second["key"])
            keys = manager.list_keys()
            self.assertEqual(len(keys), 2)
            self.assertTrue(all(item["enabled"] for item in keys))
            self.assertTrue(manager.verify_key(first["key"], plugin="thesys"))
            self.assertFalse(manager.verify_key(first["key"], plugin="zo"))


class ThesysTwoAPIFrameworkTests(unittest.TestCase):
    def test_plugin_loads_keys_from_credentials_file_and_hides_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "T" * 64
            root.joinpath("thesys_credentials.json").write_text(
                json.dumps([
                    {
                        "email": "demo@thesys.test",
                        "api_key": key,
                        "credit_amount": 12.5,
                        "user_id": "user-demo",
                        "ok": True,
                    }
                ]),
                encoding="utf-8",
            )
            plugin = ThesysTwoAPIPlugin(settings=TwoAPISettings(min_credit=1.0), data_dir=root)
            accounts = plugin.load_accounts()

            self.assertEqual(len(accounts), 1)
            account = accounts[0]
            self.assertEqual(account.plugin, "thesys")
            self.assertEqual(account.email, "demo@thesys.test")
            self.assertEqual(account.api_key, key)
            self.assertEqual(account.base_url, THESYS_OPENAI_BASE_URL)
            self.assertEqual(account.credit_amount, 12.5)
            rendered = json.dumps(account.to_public(), ensure_ascii=False)
            self.assertNotIn(key, rendered)

    def test_forward_models_uses_local_free_catalog(self):
        plugin = ThesysTwoAPIPlugin(settings=TwoAPISettings(max_retries=1), transport=Mock())
        response = plugin.forward_models()
        data = response.json()
        model_ids = [item["id"] for item in data["data"]]
        self.assertEqual(data["object"], "list")
        self.assertIn(THESYS_DEFAULT_MODEL, model_ids)

    def test_import_accounts_persists_external_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "T" * 64
            plugin = ThesysTwoAPIPlugin(settings=TwoAPISettings(), data_dir=root, account_db_path=root / "account_manager.db")
            repository = Mock()
            repository.import_lines.return_value = 1
            result = plugin.import_accounts(
                records=[{"email": "imported@thesys.test", "api_key": key}],
                source="unit-test",
                repository=repository,
            )

            self.assertGreaterEqual(result["accepted"], 1)
            saved = json.loads((root / "thesys_credentials.json").read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["email"], "imported@thesys.test")
            self.assertEqual(saved[0]["api_key"], key)

    def test_push_accounts_posts_local_records_to_remote_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "T" * 64
            (root / "thesys_credentials.json").write_text(
                json.dumps([{"email": "push@thesys.test", "api_key": key, "credit_amount": 100, "ok": True}], ensure_ascii=False),
                encoding="utf-8",
            )
            response = Mock(status_code=200, ok=True, text='{"ok":true,"imported":1}')
            response.json.return_value = {"ok": True, "imported": 1}
            transport = Mock()
            transport.post.return_value = response
            plugin = ThesysTwoAPIPlugin(settings=TwoAPISettings(), data_dir=root, account_db_path=root / "account_manager.db", transport=transport)

            result = plugin.push_accounts("http://linux.example:8000", source="windows-register", emails=["push@thesys.test"])

            self.assertTrue(result["ok"])
            self.assertEqual(result["pushed"], 1)
            self.assertEqual(transport.post.call_args.args[0], "http://linux.example:8000/api/2api/plugins/thesys/import")
            body = transport.post.call_args.kwargs["json"]
            self.assertEqual(body["source"], "windows-register")
            self.assertEqual(body["records"][0]["email"], "push@thesys.test")
            self.assertEqual(body["records"][0]["api_key"], key)

    def test_refill_accounts_creates_thesys_register_task(self):
        plugin = ThesysTwoAPIPlugin(settings=TwoAPISettings())
        with patch("services.twoapi.plugins.thesys.create_register_task", create=True) as create_mock, patch(
            "services.twoapi.plugins.thesys.task_runtime", create=True
        ) as runtime_mock:
            create_mock.return_value = {"id": "task_1"}
            result = plugin.refill_accounts(count=2, concurrency=1, executor_type="protocol", extra={"mail_provider": "cfworker"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["payload"]["platform"], "thesys")
        self.assertEqual(result["payload"]["count"], 2)
        self.assertEqual(result["payload"]["extra"]["mail_provider"], "cfworker")
        runtime_mock.wake_up.assert_called_once()


if __name__ == "__main__":
    unittest.main()
