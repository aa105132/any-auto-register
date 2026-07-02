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
        fallback_idx = source.index('未找到系统 Chrome或需代理，使用 Playwright Chromium')
        self.assertLess(launch_idx, fallback_idx)
        self.assertIn('any_auto_register_chrome_', source)

    def test_oauth_browser_with_proxy_skips_external_cdp_launch(self):
        source = inspect.getsource(oauth_browser.OAuthBrowser.__enter__)
        self.assertIn('proxy configured; skip external CDP', source)
        self.assertIn('需代理', source)
        proxy_idx = source.index('proxy configured; skip external CDP')
        cdp_idx = source.index('_launch_external_chromium_cdp(temp_profile')
        self.assertLess(proxy_idx, cdp_idx)

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

class GooglePoolReleaseRegressions(unittest.TestCase):
    """注册失败释放 reserved_platforms 的回归测试。

    覆盖补丁A（base_platform.register raise 路径释放）与补丁C（release_stale 清陈旧锁）。
    """

    def _write_pool(self, pool_path: Path, accounts: list) -> None:
        import json

        pool_path.write_text(
            json.dumps({"version": 1, "accounts": accounts}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _pool_with_reserved(self, pool_path: Path) -> "GoogleAccountPool":
        from core.google_account_pool import GoogleAccountPool

        return GoogleAccountPool(pool_path)

    def test_register_raises_releases_reserved_via_identity(self):
        """register 中途抛异常（account 未返回、_attach_identity_metadata 没机会透传）时，
        base_platform.register 必须从 identity.mailbox_account.extra 读 reserved 邮箱并 release，
        否则 reserved_platforms 永久残留、号被耗光。"""
        import json
        from unittest.mock import patch
        from types import SimpleNamespace

        from core.base_platform import RegisterConfig

        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "google_accounts_pool.json"
            self._write_pool(pool_path, [{
                "email": "stale1@gmail.com",
                "password": "pw",
                "registered_platforms": [],
                "reserved_platforms": ["tryblend"],
                "status": "valid",
            }])
            pool = self._pool_with_reserved(pool_path)

            platform = TryBlendPlatform(config=RegisterConfig(executor_type="headless"))
            # 造一个带 reserved 标记的 identity，模拟 _reuse_existing_account 已占用
            identity = SimpleNamespace(
                identity_provider="mailbox",
                email="stale1@gmail.com",
                oauth_provider="",
                chrome_user_data_dir="",
                chrome_cdp_url="",
                metadata={},
                mailbox_account=SimpleNamespace(extra={"google_pool_reserved_email": "stale1@gmail.com"}),
            )
            with patch.object(platform, "_resolve_identity", return_value=identity), \
                 patch("core.base_platform.BrowserRegistrationFlow") as flow_cls, \
                 patch("core.google_account_pool.GoogleAccountPool", lambda: pool):
                flow_cls.return_value.run.side_effect = RuntimeError("OAuth 半路失败")
                with self.assertRaises(RuntimeError):
                    platform.register(email="stale1@gmail.com", password="p")

            # raise 前应已释放，reserved_platforms 清空
            data = json.loads(pool_path.read_text(encoding="utf-8"))
            self.assertNotIn("reserved_platforms", data["accounts"][0])

    def test_register_raises_releases_via_mailbox_cached_account(self):
        """_resolve_identity 在 acquire 后、返回前抛异常（identity 未赋值）时，
        从 mailbox._account.extra 兜底读 reserved 邮箱释放。"""
        import json
        from unittest.mock import patch
        from types import SimpleNamespace

        from core.base_platform import RegisterConfig

        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "google_accounts_pool.json"
            self._write_pool(pool_path, [{
                "email": "stale2@gmail.com",
                "password": "pw",
                "registered_platforms": [],
                "reserved_platforms": ["tryblend"],
                "status": "valid",
            }])
            pool = self._pool_with_reserved(pool_path)

            platform = TryBlendPlatform(config=RegisterConfig(executor_type="headless"))
            # _resolve_identity 自己抛（identity 拿不到），但 mailbox._account 已缓存 reserved
            cached_account = SimpleNamespace(extra={"google_pool_reserved_email": "stale2@gmail.com"})
            mailbox = SimpleNamespace(_account=cached_account)
            platform.mailbox = mailbox
            with patch.object(platform, "_resolve_identity", side_effect=RuntimeError("resolve 失败")), \
                 patch("core.google_account_pool.GoogleAccountPool", lambda: pool):
                with self.assertRaises(RuntimeError):
                    platform.register(email="stale2@gmail.com", password="p")

            data = json.loads(pool_path.read_text(encoding="utf-8"))
            self.assertNotIn("reserved_platforms", data["accounts"][0])

    def test_release_stale_clears_residual_locks(self):
        """release_stale 清理 reserved 含但 registered 不含的陈旧锁，registered 中的平台不动。"""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "google_accounts_pool.json"
            self._write_pool(pool_path, [
                {"email": "a@gmail.com", "password": "pw", "registered_platforms": [], "reserved_platforms": ["vellum"], "status": "valid"},
                {"email": "b@gmail.com", "password": "pw", "registered_platforms": ["vellum"], "reserved_platforms": ["vellum"], "status": "valid"},
                {"email": "c@gmail.com", "password": "pw", "registered_platforms": ["anycap"], "reserved_platforms": ["anycap"], "status": "valid"},
                {"email": "invalid@gmail.com", "password": "pw", "registered_platforms": [], "reserved_platforms": ["vellum"], "status": "invalid"},
            ])
            pool = self._pool_with_reserved(pool_path)

            stale = pool.release_stale(platform="")
            affected_emails = {email for email, _ in stale}
            # 只有 a 是陈旧锁（reserved vellum 但 registered 空）；b/c registered 已含不陈旧；invalid 跳过
            self.assertEqual(affected_emails, {"a@gmail.com"})
            data = json.loads(pool_path.read_text(encoding="utf-8"))
            by_email = {a["email"]: a for a in data["accounts"]}
            self.assertNotIn("reserved_platforms", by_email["a@gmail.com"])
            self.assertEqual(by_email["b@gmail.com"].get("reserved_platforms"), ["vellum"])
            self.assertEqual(by_email["c@gmail.com"].get("reserved_platforms"), ["anycap"])

    def test_release_stale_filters_by_platform(self):
        """release_stale(platform='vellum') 只清 vellum 陈旧锁，不动其他平台。"""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            pool_path = Path(tmp) / "google_accounts_pool.json"
            self._write_pool(pool_path, [
                {"email": "a@gmail.com", "password": "pw", "registered_platforms": [], "reserved_platforms": ["vellum", "anycap"], "status": "valid"},
            ])
            pool = self._pool_with_reserved(pool_path)

            stale = pool.release_stale(platform="vellum")
            self.assertEqual(stale, [("a@gmail.com", ["vellum"])])
            data = json.loads(pool_path.read_text(encoding="utf-8"))
            # vellum 清了，anycap 保留
            self.assertEqual(data["accounts"][0].get("reserved_platforms"), ["anycap"])


if __name__ == '__main__':
    unittest.main()
