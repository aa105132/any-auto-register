from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_TSX = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
REGISTER_TSX = ROOT / "frontend" / "src" / "pages" / "Register.tsx"
SETTINGS_TSX = ROOT / "frontend" / "src" / "pages" / "Settings.tsx"


class AtxpFrontendConfigTests(unittest.TestCase):
    def test_settings_exposes_codebanana2api_config(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")
        self.assertIn("id: 'codebanana'", source)
        self.assertIn("CodeBanana2API", source)
        self.assertIn("codebanana2api_url", source)
        self.assertIn("codebanana2api_enabled", source)
        self.assertIn("启用自动导入", source)

    def test_settings_exposes_resin_proxy_config(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")
        self.assertIn("id: 'proxy'", source)
        self.assertIn("Resin 统一代理入口", source)
        self.assertIn("resin_enabled", source)
        self.assertIn("resin_proxy_url", source)
        self.assertIn("resin_scheme", source)
        self.assertIn("resin_host", source)
        self.assertIn("resin_port", source)
        self.assertIn("resin_token", source)
        self.assertIn("resin_default_platform", source)
        self.assertIn("resin_platform_map", source)
        self.assertIn("测试 Resin 连通性", source)
        self.assertIn("填充同名模板", source)
        self.assertIn("填充示例模板", source)

    def test_settings_exposes_scdn_runtime_proxy_config(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")
        self.assertIn("SCDN 运行时来源", source)
        self.assertIn("scdn_runtime_enabled", source)
        self.assertIn("scdn_runtime_protocol", source)
        self.assertIn("scdn_runtime_country_code", source)
        self.assertIn("scdn_runtime_count", source)
        self.assertIn("scdn_runtime_validate_url", source)
        self.assertIn("scdn_runtime_validate_timeout_sec", source)
        self.assertIn("scdn_runtime_cache_ttl_sec", source)
        self.assertIn("scdn_runtime_cache_size", source)

    def test_register_shows_resin_platform_preview_copy(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")
        self.assertIn("当前命中 Resin Platform 预览", source)
        self.assertIn("沿用全局 Resin 代理", source)
        self.assertIn("任务代理已覆盖全局 Resin", source)

    def test_accounts_export_config_declares_atxp_platform(self):
        source = ACCOUNTS_TSX.read_text(encoding="utf-8")
        self.assertIn("atxp:", source)
        self.assertIn("connection_string", source)
        self.assertIn("clowdbot_status", source)
        self.assertIn("claimed_agent_email", source)

    def test_register_fallback_platforms_include_atxp(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")
        self.assertIn("{ name: 'atxp', display_name: 'ATXP' }", source)

    def test_register_fallback_platforms_include_venice(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")
        self.assertIn("{ name: 'venice', display_name: 'Venice' }", source)

    def test_register_platform_default_mail_provider_is_soft_default(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")
        self.assertIn("const previousPlatformRef = useRef<string>('')", source)
        self.assertIn("const platformChanged = previousPlatformRef.current !== platformName", source)
        self.assertIn("const mailProviderInvalid = Boolean(currentMailProvider", source)
        self.assertIn("platformChanged || !currentMailProvider || mailProviderInvalid", source)
        self.assertIn("避免覆盖用户手动选择", source)
        self.assertNotIn("platformDefaultMailProvider && form.identity_provider === 'mailbox' && form.mail_provider !== platformDefaultMailProvider)", source)

    def test_accounts_export_config_declares_venice_platform(self):
        source = ACCOUNTS_TSX.read_text(encoding="utf-8")
        self.assertIn("venice:", source)
        self.assertIn("api_key", source)
        self.assertIn("api_key_description", source)
        self.assertIn("access_token", source)

    def test_settings_mailbox_inventory_shows_used_platforms(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")
        self.assertIn("metadata?: Record<string, unknown>", source)
        self.assertIn("已用平台", source)
        self.assertIn("used_platforms", source)

    def test_settings_mailbox_inventory_reset_button_uses_utf8_labels(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")
        self.assertIn("重置中...", source)
        self.assertIn("重置为未使用", source)
        self.assertNotIn("������...", source)
        self.assertNotIn("����Ϊδ��", source)
    def test_settings_mailbox_inventory_blacklisted_rows_offer_restore_action(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")
        self.assertIn("item.status === 'blacklisted'", source)
        self.assertIn("从黑名单拉回", source)


if __name__ == "__main__":
    unittest.main()
