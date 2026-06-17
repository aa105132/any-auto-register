from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_thesys_plugin_does_not_import_task_runtime_at_module_import():
    source = (ROOT / "services" / "twoapi" / "plugins" / "thesys.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_modules = {"application.tasks", "services.task_runtime"}
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in forbidden_modules:
                imported.append(module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in forbidden_modules:
                    imported.append(alias.name)
    assert imported == []


def test_main_does_not_auto_start_standalone_twoapi_process_on_lifespan_start():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "twoapi_server_runtime.ensure_running" not in source
