from __future__ import annotations

import importlib
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _ensure_sqlalchemy_stub() -> None:
    class UniqueConstraint:
        def __init__(self, *_args, **_kwargs):
            pass

    def inspect(*_args, **_kwargs):
        return None

    class _Event:
        def listens_for(self, *_args, **_kwargs):
            def decorator(fn):
                return fn
            return decorator

    module = sys.modules.get("sqlalchemy") or types.ModuleType("sqlalchemy")
    module.UniqueConstraint = UniqueConstraint
    module.inspect = inspect
    module.event = _Event()
    sys.modules["sqlalchemy"] = module


def _ensure_sqlmodel_stub() -> None:
    class SQLModel:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    class Session:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def Field(default=None, default_factory=None, **_kwargs):
        if default_factory is not None and default is None:
            return default_factory()
        return default

    def create_engine(*_args, **_kwargs):
        return object()

    def select(*_args, **_kwargs):
        return ("select", _args, _kwargs)

    def delete(*_args, **_kwargs):
        return ("delete", _args, _kwargs)

    class _Func:
        def __getattr__(self, name):
            return lambda *args, **kwargs: ("func", name, args, kwargs)

    module = sys.modules.get("sqlmodel") or types.ModuleType("sqlmodel")
    module.Field = Field
    module.SQLModel = SQLModel
    module.Session = Session
    module.create_engine = create_engine
    module.select = select
    module.delete = delete
    module.func = _Func()
    sys.modules["sqlmodel"] = module


class _ConfigStoreStub:
    def __init__(self, data: dict[str, str] | None = None):
        self.data = dict(data or {})

    def get(self, key: str, default: str = "") -> str:
        return self.data.get(key, default)


def _ensure_config_store_stub(data: dict[str, str] | None = None) -> _ConfigStoreStub:
    module = sys.modules.get("core.config_store") or types.ModuleType("core.config_store")
    store = _ConfigStoreStub(data)
    module.config_store = store
    module._ConfigStoreStub = store
    sys.modules["core.config_store"] = module
    return store


def _import_tasks_module(data: dict[str, str] | None = None):
    _ensure_sqlalchemy_stub()
    _ensure_sqlmodel_stub()
    store = _ensure_config_store_stub(data)
    if "application.tasks" in sys.modules:
        module = importlib.reload(sys.modules["application.tasks"])
    else:
        module = importlib.import_module("application.tasks")
    # reload 之后 config_store 引用会重新绑定到 stub，确保后续 patch 用的是同一份
    module.config_store = store
    return module


class _DummyTaskLogger:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []  # (message, level)

    def log(self, message: str, *, level: str = "info", **_kwargs) -> None:
        self.messages.append((str(message), level))


def _make_account(platform: str = "grok", api_key: str = "sk-aaa", **extra_overrides):
    extra = {"api_key": api_key}
    extra.update(extra_overrides)
    return SimpleNamespace(
        platform=platform,
        email="demo@example.com",
        token="",
        extra=extra,
    )


class CatAPIRefillHookTests(unittest.TestCase):
    def setUp(self):
        # 每个测试独立加载 tasks 模块，避免 config_store stub 互相污染
        self.tasks = _import_tasks_module(
            {
                "catapi_enabled": "true",
                "catapi_base_url": "http://20.193.157.62",
                "catapi_admin_username": "a105132",
                "catapi_admin_password": "secret",
                "catapi_platform_map": "grok=grok\nthesys=grok",
                "catapi_name_prefix": "external",
            }
        )

    def test_refill_skips_when_disabled(self):
        self.tasks.config_store.data["catapi_enabled"] = "false"
        logger = _DummyTaskLogger()
        with patch("core.catapi_client.list_channel_keys") as mock_list, patch(
            "core.catapi_client.push_channel_keys"
        ) as mock_push:
            self.tasks._auto_refill_catapi(logger, _make_account("grok", "sk-aaa"))
        mock_list.assert_not_called()
        mock_push.assert_not_called()
        self.assertEqual(logger.messages, [])

    def test_refill_skips_when_platform_not_in_map(self):
        logger = _DummyTaskLogger()
        with patch("core.catapi_client.list_channel_keys") as mock_list, patch(
            "core.catapi_client.push_channel_keys"
        ) as mock_push:
            self.tasks._auto_refill_catapi(logger, _make_account("venice", "sk-aaa"))
        mock_list.assert_not_called()
        mock_push.assert_not_called()
        # 未命中映射不算错误，不打 warning
        self.assertEqual(logger.messages, [])

    def test_refill_pushes_when_key_not_exists(self):
        logger = _DummyTaskLogger()
        with patch("core.catapi_client.list_channel_keys", return_value=["sk-other"]) as mock_list, patch(
            "core.catapi_client.push_channel_keys",
            return_value={"added": 1, "skipped": 0, "after_total": 5, "received": 1, "before_total": 4},
        ) as mock_push:
            self.tasks._auto_refill_catapi(logger, _make_account("grok", "sk-aaa"))

        mock_list.assert_called_once()
        self.assertEqual(mock_list.call_args.args[1], "grok")
        mock_push.assert_called_once()
        pushed_keys = mock_push.call_args.args[2]
        self.assertEqual(pushed_keys, ["sk-aaa"])
        self.assertEqual(mock_push.call_args.kwargs["name_prefix"], "external")
        # 至少包含一条 info 级别的"已推送"日志
        self.assertTrue(any("已推送" in msg and level == "info" for msg, level in logger.messages))

    def test_refill_skips_when_key_already_exists(self):
        logger = _DummyTaskLogger()
        with patch("core.catapi_client.list_channel_keys", return_value=["sk-aaa"]) as mock_list, patch(
            "core.catapi_client.push_channel_keys"
        ) as mock_push:
            self.tasks._auto_refill_catapi(logger, _make_account("grok", "sk-aaa"))

        mock_list.assert_called_once()
        mock_push.assert_not_called()
        self.assertTrue(any("已存在" in msg for msg, _ in logger.messages))

    def test_refill_logs_warning_when_list_fails(self):
        from core.catapi_client import CatAPIError

        logger = _DummyTaskLogger()
        with patch("core.catapi_client.list_channel_keys", side_effect=CatAPIError("渠道不存在")), patch(
            "core.catapi_client.push_channel_keys"
        ) as mock_push:
            # 不应抛异常
            self.tasks._auto_refill_catapi(logger, _make_account("grok", "sk-aaa"))

        mock_push.assert_not_called()
        self.assertTrue(any(level == "warning" and "CatAPI" in msg for msg, level in logger.messages))

    def test_refill_logs_warning_when_push_fails(self):
        from core.catapi_client import CatAPIError

        logger = _DummyTaskLogger()
        with patch("core.catapi_client.list_channel_keys", return_value=[]), patch(
            "core.catapi_client.push_channel_keys", side_effect=CatAPIError("推送失败")
        ):
            # 不应抛异常
            self.tasks._auto_refill_catapi(logger, _make_account("grok", "sk-aaa"))

        self.assertTrue(any(level == "warning" and "推送" in msg for msg, level in logger.messages))

    def test_refill_skips_when_no_api_key(self):
        logger = _DummyTaskLogger()
        account = SimpleNamespace(platform="grok", email="demo@example.com", token="", extra={})
        with patch("core.catapi_client.list_channel_keys") as mock_list, patch(
            "core.catapi_client.push_channel_keys"
        ) as mock_push:
            self.tasks._auto_refill_catapi(logger, account)

        mock_list.assert_not_called()
        mock_push.assert_not_called()
        self.assertTrue(any(level == "warning" and "api_key" in msg for msg, level in logger.messages))

    def test_parse_platform_map_skips_comments_and_blank_lines(self):
        mapping = self.tasks._parse_catapi_platform_map(
            "# 注释行\n\n  grok = grok  \nthesys=grok\nbadline\n=no\nfoo="
        )
        self.assertEqual(mapping, {"grok": "grok", "thesys": "grok"})


if __name__ == "__main__":
    unittest.main()
