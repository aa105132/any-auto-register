from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_TSX = ROOT / "frontend" / "src" / "App.tsx"
ACCOUNTS_TSX = ROOT / "frontend" / "src" / "pages" / "Accounts.tsx"
PROXIES_TSX = ROOT / "frontend" / "src" / "pages" / "Proxies.tsx"
TASK_HISTORY_TSX = ROOT / "frontend" / "src" / "pages" / "TaskHistory.tsx"
SETTINGS_TSX = ROOT / "frontend" / "src" / "pages" / "Settings.tsx"
REGISTER_TSX = ROOT / "frontend" / "src" / "pages" / "Register.tsx"
TASK_LOG_PANEL_TSX = ROOT / "frontend" / "src" / "components" / "tasks" / "TaskLogPanel.tsx"
INDEX_CSS = ROOT / "frontend" / "src" / "index.css"


class FrontendScrollLayoutTests(unittest.TestCase):
    def test_shell_main_allows_page_content_to_escape_horizontally(self):
        source = APP_TSX.read_text(encoding="utf-8")
        match = re.search(r'<main className="([^"]+)"', source)
        self.assertIsNotNone(match, "expected to find <main className=...> in App.tsx")
        classes = match.group(1)

        self.assertIn("overflow-y-auto", classes)
        self.assertIn("overflow-x-visible", classes)
        self.assertNotIn("overflow-hidden", classes)

    def test_dialog_panel_does_not_force_hide_vertical_overflow(self):
        source = INDEX_CSS.read_text(encoding="utf-8")
        match = re.search(r"\.dialog-panel\s*\{(?P<body>.*?)\n\}", source, re.S)
        self.assertIsNotNone(match, "expected to find .dialog-panel block in index.css")
        body = match.group("body")

        self.assertIn("overflow-x: hidden;", body)
        self.assertNotIn("overflow: hidden;", body)

    def test_accounts_table_wrap_keeps_horizontal_scroll_enabled(self):
        source = ACCOUNTS_TSX.read_text(encoding="utf-8")
        self.assertIn('className="glass-table-wrap workspace-table-scroll"', source)
        self.assertIn('className="workspace-table min-w-[1240px] w-full text-sm"', source)
        self.assertNotIn("glass-table-wrap overflow-x-hidden", source)

    def test_accounts_export_dialog_uses_two_column_workbench_without_inner_scroll_box(self):
        source = ACCOUNTS_TSX.read_text(encoding="utf-8")
        css = INDEX_CSS.read_text(encoding="utf-8")

        self.assertIn('createPortal(dialog, document.body)', source)
        self.assertIn('className="dialog-backdrop accounts-export-backdrop z-[70]"', source)
        self.assertIn('className="dialog-panel dialog-panel-lg accounts-export-panel flex flex-col"', source)
        self.assertIn('className="accounts-export-body min-h-0 flex-1 overflow-y-auto px-6 py-5"', source)
        self.assertIn('className="accounts-export-sidebar"', source)
        self.assertIn('className="accounts-export-format-grid mt-4"', source)
        self.assertIn('className="accounts-export-group-stack mt-4"', source)
        self.assertNotIn("max-h-[340px] space-y-4 overflow-y-auto pr-1", source)

        self.assertIn(".accounts-export-backdrop {", css)
        self.assertIn("overflow-y: auto;", css)
        self.assertIn(".accounts-export-body {", css)
        self.assertIn(".accounts-export-sidebar {", css)
        self.assertIn(".accounts-export-field-grid {", css)

    def test_other_data_heavy_pages_keep_horizontal_scroll_wrappers(self):
        proxies_source = PROXIES_TSX.read_text(encoding="utf-8")
        history_source = TASK_HISTORY_TSX.read_text(encoding="utf-8")

        self.assertIn('className="glass-table-wrap workspace-table-scroll"', proxies_source)
        self.assertIn('className="workspace-table min-w-[920px] w-full text-sm"', proxies_source)
        self.assertIn('className="glass-table-wrap workspace-table-scroll"', history_source)
        self.assertIn('className="workspace-table min-w-[1160px] w-full text-sm"', history_source)

    def test_settings_inventory_and_provider_tables_are_scrollable(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")
        self.assertIn('className="glass-table-wrap workspace-table-scroll"', source)
        self.assertIn('className="workspace-table min-w-[1120px] w-full text-xs"', source)
        self.assertIn('className="workspace-table w-full min-w-[1040px] text-sm"', source)

    def test_register_page_uses_structured_two_column_layout_and_lazy_log_panel(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")
        self.assertIn("const TaskLogPanel = lazy", source)
        self.assertIn('xl:grid-cols-[minmax(0,1.38fr)_minmax(320px,0.62fr)]', source)
        self.assertIn('className="rounded-[24px] border border-[var(--border)] bg-[var(--bg-pane)]/62 xl:sticky xl:top-4"', source)
        self.assertIn('Suspense fallback={<div className="empty-state-panel">日志面板加载中...</div>}', source)

    def test_workbench_pages_drop_verbose_design_explanations(self):
        accounts_source = ACCOUNTS_TSX.read_text(encoding="utf-8")
        register_source = REGISTER_TSX.read_text(encoding="utf-8")
        proxies_source = PROXIES_TSX.read_text(encoding="utf-8")

        self.assertNotIn("本页优先保证", accounts_source)
        self.assertNotIn("更多操作已从表格裁切层脱离", accounts_source)
        self.assertNotIn("横向滚动已开启", accounts_source)
        self.assertIn("支持搜索、筛选、导出与批量操作。", accounts_source)

        self.assertNotIn("这个页面现在优先保证", register_source)
        self.assertIn("集中配置注册输入、身份方式和执行通道。", register_source)

        self.assertNotIn("这里优先保证录入、状态判断与表格浏览清楚分层", proxies_source)
        self.assertIn("集中管理代理池、批量导入、启停和连通性巡检。", proxies_source)

    def test_settings_outlook_inventory_marks_alias_children(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")

        self.assertIn("isOutlookAliasInventoryItem", source)
        self.assertIn("子邮箱", source)
        self.assertIn("父邮箱", source)

    def test_settings_outlook_inventory_groups_alias_children_as_nested_list(self):
        source = SETTINGS_TSX.read_text(encoding="utf-8")

        self.assertIn("groupOutlookAliasInventoryItems", source)
        self.assertIn("getOutlookAliasParentKey", source)
        self.assertIn("子邮箱列表", source)

    def test_register_form_controls_are_not_nested_component_definitions(self):
        source = REGISTER_TSX.read_text(encoding="utf-8")

        self.assertIn("function RegisterTextInput", source)
        self.assertIn("function RegisterSelect", source)
        self.assertNotIn("  const Input = (", source)
        self.assertNotIn("  const Select = (", source)

    def test_task_log_panel_only_autoscrolls_when_user_is_near_bottom(self):
        source = TASK_LOG_PANEL_TSX.read_text(encoding="utf-8")

        self.assertIn("const viewportRef = useRef<HTMLDivElement>(null)", source)
        self.assertIn("const followOutputRef = useRef(true)", source)
        self.assertIn("const distanceFromBottom = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight", source)
        self.assertIn("followOutputRef.current = distanceFromBottom <= 24", source)
        self.assertIn("if (!followOutputRef.current) return", source)
        self.assertIn("viewport.scrollTop = viewport.scrollHeight", source)
        self.assertNotIn("scrollIntoView({ behavior: 'smooth' })", source)

if __name__ == "__main__":
    unittest.main()
