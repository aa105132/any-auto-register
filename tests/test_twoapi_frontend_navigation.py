import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_TSX = ROOT / "frontend" / "src" / "App.tsx"
TWOAPI_TSX = ROOT / "frontend" / "src" / "pages" / "TwoAPI.tsx"
REGISTER_TSX = ROOT / "frontend" / "src" / "pages" / "Register.tsx"


class TwoAPIFrontendNavigationTests(unittest.TestCase):
    def test_app_exposes_twoapi_plugin_subnav_and_dynamic_route(self):
        source = APP_TSX.read_text(encoding="utf-8")
        self.assertIn("function TwoAPISubNav", source)
        self.assertIn("/2api/plugins", source)
        self.assertIn('to={`/twoapi/${plugin.key}`}', source)
        self.assertIn('path="/twoapi/:plugin"', source)
        self.assertIn("<TwoAPISubNav />", source)

    def test_twoapi_page_uses_plugin_route_for_settings_accounts_and_logs(self):
        source = TWOAPI_TSX.read_text(encoding="utf-8")
        self.assertIn("useParams", source)
        self.assertIn("selectedPlugin", source)
        self.assertIn("/2api/logs?plugin=${encodeURIComponent(selectedPlugin)}", source)
        self.assertIn("pluginKeys", source)
        self.assertIn("selectedPluginStatus?.accounts", source)
        self.assertIn("账号池", source)

    def test_twoapi_page_keeps_only_thesys_settings_schema(self):
        source = TWOAPI_TSX.read_text(encoding="utf-8")
        self.assertIn("PLUGIN_SETTING_FIELDS", source)
        self.assertIn("thesys: [", source)
        self.assertNotIn("zo: [", source)
        self.assertNotIn("swarms: [", source)
        self.assertIn("启用 Thesys 2API", source)
        self.assertIn("canRecoverPlugin = false", source)

    def test_twoapi_page_has_remote_push_controls_for_thesys_only(self):
        source = TWOAPI_TSX.read_text(encoding="utf-8")
        self.assertIn("apiFetch(`/2api/plugins/", source)
        self.assertIn("encodeURIComponent(selectedPlugin)", source)
        self.assertIn("/push", source)
        self.assertIn("const result = await apiFetch", source)
        self.assertIn("canPushRemote = selectedPlugin === 'thesys'", source)
        self.assertIn("推送到远端 Linux", source)
        self.assertNotIn("selectedPlugin === 'zo'", source)
        self.assertNotIn("selectedPlugin === 'swarms'", source)
        self.assertNotIn("selectedPlugin === 'anycap'", source)

    def test_register_page_reuses_twoapi_push_options_for_thesys_only(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")
        self.assertIn("TWOAPI_PUSH_PLATFORMS", source)
        self.assertIn("'thesys'", source)
        self.assertNotIn("'zo'", source[source.index("TWOAPI_PUSH_PLATFORMS"):source.index("FALLBACK_PLATFORMS")])
        self.assertNotIn("'swarms'", source[source.index("TWOAPI_PUSH_PLATFORMS"):source.index("FALLBACK_PLATFORMS")])
        self.assertNotIn("'anycap'", source[source.index("TWOAPI_PUSH_PLATFORMS"):source.index("FALLBACK_PLATFORMS")])
        self.assertIn("注册完成后 2API 推送", source)
        self.assertIn("helper={`会自动拼接 /api/2api/plugins/${form.platform}/import`}", source)
        self.assertIn("twoapi_push_mode: isTwoApiPushPlatform ?", source)
        self.assertIn("twoapi_push_target_url: isTwoApiPushPlatform ?", source)


if __name__ == "__main__":
    unittest.main()
