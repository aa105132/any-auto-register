import inspect
import tempfile
import unittest
from pathlib import Path

import core.google_oauth as google_oauth
import core.oauth_browser as oauth_browser
from platforms.tryblend.plugin import TryBlendPlatform


class OAuthRegressionTests(unittest.TestCase):
    def test_google_deleted_account_page_is_detected(self):
        body = """
        使用 Google 账号登录
        账号已被删除
        trann@example.com
        此账号最近已被删除，但或许可以再恢复。要尝试恢复此账号，请点击下一步。
        """
        self.assertTrue(google_oauth._is_google_deleted_account_page(body))

    def test_google_deleted_account_marks_pool_invalid(self):
        drive_source = inspect.getsource(google_oauth.drive_google_oauth)
        helper_source = inspect.getsource(google_oauth._mark_google_account_invalid)
        self.assertIn("_is_google_deleted_account_page", drive_source)
        self.assertIn("_mark_google_account_invalid", drive_source)
        self.assertIn("google_account_deleted", drive_source)
        self.assertIn("mark_invalid", helper_source)
        self.assertIn("GoogleAccountPool", helper_source)

    def test_mark_google_account_invalid_updates_pool_file(self):
        import json
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "google_accounts_pool.json"
            pool_path.write_text(
                json.dumps({
                    "version": 1,
                    "accounts": [{
                        "email": "deleted@example.com",
                        "password": "pw",
                        "registered_platforms": [],
                        "status": "valid",
                    }],
                }),
                encoding="utf-8",
            )
            from core.google_account_pool import GoogleAccountPool

            with patch("core.google_oauth.GoogleAccountPool", lambda: GoogleAccountPool(pool_path)):
                self.assertTrue(google_oauth._mark_google_account_invalid(
                    "deleted@example.com",
                    reason="google_account_deleted",
                    log_fn=lambda _msg: None,
                ))

            data = json.loads(pool_path.read_text(encoding="utf-8"))
            self.assertEqual(data["accounts"][0]["status"], "invalid")
            self.assertEqual(data["accounts"][0]["notes"], "google_account_deleted")

    def test_google_prompt_clicker_has_no_bottom_right_fallback(self):
        source = inspect.getsource(google_oauth._click_text_or_prompt)
        self.assertNotIn("bottom-right", source)
        self.assertIn("privacy", source.lower())
        self.assertIn("terms", source.lower())

    def test_oauth_browser_tracks_external_chrome_process(self):
        source = inspect.getsource(oauth_browser._launch_external_chromium_cdp)
        self.assertIn("return", source)
        self.assertIn("process", source)
        cls = inspect.getsource(oauth_browser.OAuthBrowser.__exit__)
        helper = inspect.getsource(oauth_browser._terminate_process_tree)
        self.assertIn("_external_chromium_process", cls)
        self.assertIn("_terminate_process_tree", cls)
        self.assertIn("taskkill", helper)
        self.assertIn("/T", helper)
        self.assertIn("/F", helper)

    def test_oauth_browser_no_profile_prefers_external_chrome_before_playwright(self):
        source = inspect.getsource(oauth_browser.OAuthBrowser.__enter__)
        launch_idx = source.index('_launch_external_chromium_cdp(temp_profile')
        fallback_idx = source.index('未找到系统 Chrome，使用 Playwright Chromium')
        self.assertLess(launch_idx, fallback_idx)
        self.assertIn('any_auto_register_chrome_', source)

    def test_oauth_browser_default_does_not_auto_reuse_running_cdp(self):
        import inspect as _inspect

        signature = _inspect.signature(oauth_browser.OAuthBrowser.__init__)
        self.assertIn("reuse_existing_cdp", signature.parameters)
        self.assertFalse(signature.parameters["reuse_existing_cdp"].default)

        source = _inspect.getsource(oauth_browser.OAuthBrowser.__enter__)
        detect_pos = source.index("if self.reuse_existing_cdp:")
        launch_pos = source.index("_launch_external_chromium_cdp(temp_profile")
        self.assertLess(detect_pos, launch_pos)
        self.assertIn("cdp_url = \"\"", source)
        self.assertIn("if self.reuse_existing_cdp:", source)
        self.assertIn("self.chrome_cdp_url = cdp_url", source)

    def test_temp_cdp_profile_names_are_uuid_based_for_parallel_workers(self):
        source = inspect.getsource(oauth_browser.OAuthBrowser.__enter__)
        self.assertIn("uuid.uuid4", source)
        self.assertNotIn("int(time.time() * 1000)", source)

    def test_oauth_browser_cleans_owned_temp_chrome_profile(self):
        source = inspect.getsource(oauth_browser.OAuthBrowser.__exit__)
        self.assertIn('_cleanup_owned_temp_profile', source)
        helper = inspect.getsource(oauth_browser._cleanup_owned_temp_profile)
        self.assertIn('any_auto_register_chrome_', helper)
        self.assertIn('shutil.rmtree', helper)

        with tempfile.TemporaryDirectory() as tmp:
            owned_profile = Path(tmp) / 'any_auto_register_chrome_123'
            owned_profile.mkdir()
            (owned_profile / 'cache-file').write_text('cache', encoding='utf-8')
            oauth_browser._cleanup_owned_temp_profile(str(owned_profile), log_fn=lambda _msg: None)
            self.assertFalse(owned_profile.exists())

            manual_profile = Path(tmp) / 'manual_chrome_profile'
            manual_profile.mkdir()
            oauth_browser._cleanup_owned_temp_profile(str(manual_profile), log_fn=lambda _msg: None)
            self.assertTrue(manual_profile.exists())

    def test_blendspace_oauth_entry_uses_recoverable_commit_navigation(self):
        import platforms.blendspace.browser_oauth as blendspace_oauth

        source = inspect.getsource(blendspace_oauth._start_blendspace_oauth)
        self.assertIn('wait_until="commit"', source)
        self.assertIn('timeout=90000', source)
        self.assertIn('OAuth 入口加载未完成', source)
        register_source = inspect.getsource(blendspace_oauth.register_with_browser_oauth)
        self.assertIn('_start_blendspace_oauth', register_source)
        self.assertNotIn('timeout=30000', register_source)

    def test_register_task_parallel_oauth_pool_drops_shared_cdp_url(self):
        import application.tasks as tasks

        source = inspect.getsource(tasks._execute_register_task)
        self.assertIn("_sanitize_parallel_oauth_browser_extra", source)
        helper = inspect.getsource(tasks._sanitize_parallel_oauth_browser_extra)
        self.assertIn("_task_concurrency", helper)
        self.assertIn("chrome_cdp_url", helper)
        self.assertIn("oauth_reuse_existing_cdp", helper)
        self.assertIn("> 1", helper)
        extra = {
            "identity_provider": "oauth_browser",
            "oauth_account_source": "provider",
            "chrome_cdp_url": "http://127.0.0.1:9222",
        }
        sanitized = tasks._sanitize_parallel_oauth_browser_extra(extra, concurrency=5)
        self.assertEqual(sanitized["chrome_cdp_url"], "")
        self.assertEqual(sanitized["_task_concurrency"], 5)

    def test_register_task_can_explicitly_reuse_shared_cdp(self):
        import application.tasks as tasks

        extra = {
            "identity_provider": "oauth_browser",
            "oauth_account_source": "provider",
            "chrome_cdp_url": "http://127.0.0.1:9222",
            "oauth_reuse_existing_cdp": "true",
        }
        sanitized = tasks._sanitize_parallel_oauth_browser_extra(extra, concurrency=5)
        self.assertEqual(sanitized["chrome_cdp_url"], "http://127.0.0.1:9222")

    def test_tryblend_maps_expiry_fields(self):
        platform = TryBlendPlatform()
        result = platform._map_result({
            "email": "demo@example.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 1770000000,
            "expires_in": 3600,
            "token_type": "bearer",
        })
        self.assertEqual(result.extra["expires_at"], 1770000000)
        self.assertEqual(result.extra["expires_in"], 3600)
        self.assertEqual(result.extra["token_type"], "bearer")


if __name__ == '__main__':
    unittest.main()
