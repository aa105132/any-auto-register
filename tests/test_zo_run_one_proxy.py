from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

import scripts.run_zo_one as run_zo_one


class ZoRunOneProxyTests(unittest.TestCase):
    def test_run_one_defaults_to_post_registration_proxy_deploy(self):
        signature = inspect.signature(run_zo_one.run_one)
        self.assertIn("deploy_proxy_after", signature.parameters)
        self.assertTrue(signature.parameters["deploy_proxy_after"].default)
        source = inspect.getsource(run_zo_one.run_one)
        self.assertIn("_deploy_proxy_for_record", source)

    def test_write_proxy_files_saves_base_url_and_key_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_proxy_path = run_zo_one.PROXY_URLS_PATH
            original_openai_path = run_zo_one.OPENAI_PROXY_URLS_PATH
            try:
                run_zo_one.PROXY_URLS_PATH = Path(tmp) / "zo_proxy_urls.txt"
                run_zo_one.OPENAI_PROXY_URLS_PATH = Path(tmp) / "openai_proxy_urls.txt"
                run_zo_one._write_proxy_files(
                    "demo@example.com",
                    "zo_sk_demo",
                    {"ok": True, "base_url": "https://demo.zo.space/v1/zo_sk_demo"},
                )
                run_zo_one._write_proxy_files(
                    "demo@example.com",
                    "zo_sk_new",
                    {"ok": True, "base_url": "https://demo.zo.space/v1/zo_sk_new"},
                )
                proxy_lines = run_zo_one.PROXY_URLS_PATH.read_text(encoding="utf-8").splitlines()
                openai_lines = run_zo_one.OPENAI_PROXY_URLS_PATH.read_text(encoding="utf-8").splitlines()
            finally:
                run_zo_one.PROXY_URLS_PATH = original_proxy_path
                run_zo_one.OPENAI_PROXY_URLS_PATH = original_openai_path
        self.assertEqual(len(proxy_lines), 1)
        self.assertIn("https://demo.zo.space/v1/zo_sk_new", proxy_lines[0])
        self.assertIn("zo_api_key=zo_sk_new", proxy_lines[0])
        self.assertEqual(openai_lines, ["zo|demo@example.com|https://demo.zo.space/v1/zo_sk_new|api_key=dummy"])


if __name__ == "__main__":
    unittest.main()
