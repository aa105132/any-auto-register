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


    def test_twoapi_page_keeps_swarms_settings_schema_separate_from_zo(self):
        source = TWOAPI_TSX.read_text(encoding="utf-8")
        self.assertIn("PLUGIN_SETTING_FIELDS", source)
        swarms_start = source.index("swarms: [")
        swarms_end = source.index("],", swarms_start)
        swarms_schema = source[swarms_start:swarms_end]

        self.assertIn("auto_refill", swarms_schema)
        self.assertIn("request_timeout", swarms_schema)
        self.assertIn("max_retries", swarms_schema)
        self.assertNotIn("auto_wake", swarms_schema)
        self.assertNotIn("wake_timeout", swarms_schema)
        self.assertNotIn("keepalive_space_fallback", swarms_schema)
        self.assertNotIn("minimize_ask_context", swarms_schema)
        self.assertIn("canRecoverPlugin = selectedPlugin === 'zo'", source)

    def test_twoapi_page_has_remote_push_controls_for_zo_and_swarms(self):
        source = TWOAPI_TSX.read_text(encoding="utf-8")
        self.assertIn("apiFetch(`/2api/plugins/", source)
        self.assertIn("encodeURIComponent(selectedPlugin)", source)
        self.assertIn("/push", source)
        self.assertIn("const result = await apiFetch", source)
        self.assertIn("canPushRemote = selectedPlugin === 'zo' || selectedPlugin === 'swarms'", source)
        self.assertIn("setRemotePushResult(`已推送 ${result?.pushed ?? 0} 个 ${currentLabel} 账号到远端`)", source)
        self.assertIn("推送到远端 Linux", source)
        self.assertIn("远端后端地址", source)
        self.assertIn("只推送最新账号", source)
        self.assertNotIn("/2api/plugins//push", source)

    def test_register_page_reuses_twoapi_push_options_for_swarms(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")
        self.assertIn("TWOAPI_PUSH_PLATFORMS", source)
        self.assertIn("swarms", source)
        self.assertIn("注册完成后 2API 推送", source)
        self.assertIn("helper={`会自动拼接 /api/2api/plugins/${form.platform}/import`}", source)
        self.assertIn("twoapi_push_mode: isTwoApiPushPlatform ?", source)
        self.assertIn("twoapi_push_target_url: isTwoApiPushPlatform ?", source)

    def test_register_page_has_supported_platform_twoapi_push_options(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")
        self.assertIn("twoapi_push_mode", source)
        self.assertIn("twoapi_push_target_url", source)
        self.assertIn("TWOAPI_PUSH_PLATFORMS", source)
        self.assertIn("'zo'", source)
        self.assertIn("'swarms'", source)
        self.assertIn("twoApiPushPlatformLabel", source)
        self.assertIn("不推送", source)
        self.assertIn("导入本地 2API", source)
        self.assertIn("推送远端 Linux 2API", source)
        self.assertIn("请先填写远端 2API 后端地址", source)
        self.assertIn("twoapi_push_mode: isTwoApiPushPlatform ?", source)

if __name__ == "__main__":
    unittest.main()
