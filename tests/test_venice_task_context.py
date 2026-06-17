import importlib
import sys
import types
import unittest
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


def _ensure_config_store_stub() -> None:
    module = sys.modules.get("core.config_store") or types.ModuleType("core.config_store")

    class _ConfigStore:
        def get(self, _key, default=""):
            return default

    module.config_store = _ConfigStore()
    sys.modules["core.config_store"] = module


def _import_tasks_module():
    _ensure_sqlalchemy_stub()
    _ensure_sqlmodel_stub()
    _ensure_config_store_stub()
    if "application.tasks" in sys.modules:
        return importlib.reload(sys.modules["application.tasks"])
    return importlib.import_module("application.tasks")


class _DummyLogger:
    def __init__(self):
        self.messages: list[str] = []

    def log(self, message: str) -> None:
        self.messages.append(message)


class _DummyTaskLogger:
    def __init__(self):
        self.task_id = "task_test"
        self.messages: list[str] = []
        self.progress_updates: list[tuple[int, int]] = []
        self.success_count = 0
        self.error_messages: list[str] = []
        self.finished: tuple[str, str] | None = None

    def log(self, message: str, **_kwargs) -> None:
        self.messages.append(message)

    def set_progress(self, current: int, total: int) -> None:
        self.progress_updates.append((current, total))

    def is_cancel_requested(self) -> bool:
        return False

    def record_success(self) -> None:
        self.success_count += 1

    def record_error(self, message: str) -> None:
        self.error_messages.append(message)

    def add_cashier_url(self, _url: str) -> None:
        return None

    def finish(self, status: str, error: str = "", **_kwargs) -> None:
        self.finished = (status, error)


class _DummyPlatform:
    def __init__(self, config=None, mailbox=None):
        self.config = config
        self.mailbox = mailbox
        self.logger = None

    def set_logger(self, logger):
        self.logger = logger


class VeniceTaskContextTests(unittest.TestCase):
    def test_create_register_task_snapshots_venice_extra_fields(self):
        tasks = _import_tasks_module()
        captured: dict[str, object] = {}
        payload = {
            "platform": "venice",
            "count": 1,
            "executor_type": "protocol",
            "captcha_solver": "auto",
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "cfworker",
                "venice_expected_credits": 500,
                "venice_api_key_description": "seedance-auto",
            },
        }

        def fake_create_task(**kwargs):
            captured.update(kwargs)
            return {"task_id": "task_1", **kwargs}

        with patch.object(tasks, "_available_inventory_register_count", return_value=0), patch.object(
            tasks,
            "_resolve_inventory_provider_key",
            return_value="",
        ), patch.object(tasks, "create_task", side_effect=fake_create_task):
            tasks.create_register_task(payload)

        payload["extra"]["venice_expected_credits"] = 0
        payload["extra"]["venice_api_key_description"] = "mutated"

        created_payload = captured["payload"]
        self.assertEqual(created_payload["extra"]["venice_expected_credits"], 500)
        self.assertEqual(created_payload["extra"]["venice_api_key_description"], "seedance-auto")

    def test_build_platform_instance_keeps_venice_extra_in_register_config(self):
        tasks = _import_tasks_module()
        logger = _DummyLogger()
        payload = {
            "executor_type": "protocol",
            "captcha_solver": "auto",
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "cfworker",
                "venice_expected_credits": 500,
                "venice_api_key_description": "seedance-auto",
            },
        }
        mailbox_calls: dict[str, object] = {}

        def fake_create_mailbox(provider, extra, proxy):
            mailbox_calls["provider"] = provider
            mailbox_calls["extra"] = dict(extra)
            mailbox_calls["proxy"] = proxy
            return {"provider": provider, "extra": dict(extra), "proxy": proxy}

        with patch("core.base_mailbox.create_mailbox", side_effect=fake_create_mailbox), patch.object(
            tasks,
            "get",
            return_value=_DummyPlatform,
        ), patch.object(tasks, "normalize_proxy_url", side_effect=lambda value: value), patch.object(
            tasks.config_store,
            "get",
            return_value="",
        ):
            platform = tasks._build_platform_instance(
                "venice",
                payload,
                logger=logger,
                resolved_proxy="http://proxy.local:8080",
            )

        self.assertEqual(platform.config.extra["venice_expected_credits"], 500)
        self.assertEqual(platform.config.extra["venice_api_key_description"], "seedance-auto")
        self.assertEqual(mailbox_calls["extra"]["venice_expected_credits"], 500)
        self.assertEqual(mailbox_calls["extra"]["venice_api_key_description"], "seedance-auto")
        self.assertEqual(mailbox_calls["provider"], "cfworker")

    def test_execute_register_task_stops_reusing_venice_proxy_after_three_successes(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "venice",
            "count": 4,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }
        used_proxies: list[str] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def __init__(self):
                self.get_next_calls: list[set[str]] = []
                self.success_urls: list[str] = []
                self.fail_urls: list[str] = []

            def get_next(self, region: str = "", exclude_urls=None):
                excluded = set(exclude_urls or set())
                self.get_next_calls.append(excluded)
                if "proxy-1" in excluded:
                    return "proxy-2"
                return "proxy-1"

            def report_success(self, url: str) -> None:
                self.success_urls.append(url)

            def report_fail(self, url: str) -> None:
                self.fail_urls.append(url)

        fake_proxy_pool = _FakeProxyPool()

        class _FakePlatform:
            def __init__(self, resolved_proxy: str):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                index = len(used_proxies)
                return types.SimpleNamespace(
                    email=f"user{index}@example.com",
                    extra={},
                )

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        with patch("core.proxy_pool.proxy_pool", fake_proxy_pool), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[None, None, None, None],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(used_proxies, ["proxy-1", "proxy-1", "proxy-1", "proxy-2"])
        self.assertEqual(
            fake_proxy_pool.get_next_calls,
            [set(), set(), set(), {"proxy-1"}],
        )
        self.assertEqual(
            fake_proxy_pool.success_urls,
            ["proxy-1", "proxy-1", "proxy-1", "proxy-2"],
        )
        self.assertTrue(
            any("代理成功次数已达上限(3)" in message and "proxy-1" in message for message in logger.messages)
        )

    def test_execute_register_task_probes_global_resin_proxy_for_swarms(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "swarms",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }
        used_proxies: list[str | None] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def __init__(self):
                self.get_next_calls = 0
                self.success_urls: list[str] = []
                self.fail_urls: list[str] = []

            def get_next(self, region: str = "", exclude_urls=None):
                self.get_next_calls += 1
                return "pool-proxy"

            def report_success(self, url: str) -> None:
                self.success_urls.append(url)

            def report_fail(self, url: str) -> None:
                self.fail_urls.append(url)

        fake_proxy_pool = _FakeProxyPool()

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                return types.SimpleNamespace(email="user@example.com", extra={})

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "true",
                "resin_proxy_url": "http://Default:token@127.0.0.1:2260",
            }
            return config.get(key, default)

        probe_calls: list[str] = []

        def fake_probe(proxy_url: str) -> str:
            probe_calls.append(proxy_url)
            return "198.51.100.8"

        with patch("core.proxy_pool.proxy_pool", fake_proxy_pool), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[None],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ), patch.object(tasks, "_probe_proxy_ip", side_effect=fake_probe), patch.object(
            tasks,
            "_probe_swarms_signup_page",
            return_value=True,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(used_proxies, ["http://Default.vs0:token@127.0.0.1:2260"])
        self.assertEqual(probe_calls, ["http://Default.vs0:token@127.0.0.1:2260"])
        self.assertEqual(fake_proxy_pool.get_next_calls, 0)
        self.assertTrue(any("Resin IP 198.51.100.8" in message for message in logger.messages))


    def test_execute_register_task_prefers_global_resin_proxy_over_proxy_pool(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }
        used_proxies: list[str | None] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def __init__(self):
                self.get_next_calls = 0
                self.success_urls: list[str] = []
                self.fail_urls: list[str] = []

            def get_next(self, region: str = "", exclude_urls=None):
                self.get_next_calls += 1
                return "pool-proxy"

            def report_success(self, url: str) -> None:
                self.success_urls.append(url)

            def report_fail(self, url: str) -> None:
                self.fail_urls.append(url)

        fake_proxy_pool = _FakeProxyPool()

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                return types.SimpleNamespace(email="user@example.com", extra={})

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "true",
                "resin_proxy_url": "http://Default:token@127.0.0.1:2260",
            }
            return config.get(key, default)

        with patch("core.proxy_pool.proxy_pool", fake_proxy_pool), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[None],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ), patch.object(tasks, "_probe_proxy_ip", return_value="198.51.100.8"):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(used_proxies, ["http://Default:token@127.0.0.1:2260"])
        self.assertEqual(fake_proxy_pool.get_next_calls, 0)
        self.assertEqual(fake_proxy_pool.success_urls, [])
        self.assertEqual(fake_proxy_pool.fail_urls, [])

    def test_execute_register_task_explicit_proxy_overrides_global_resin_proxy(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "proxy": "http://task-proxy.local:8080",
            "extra": {},
        }
        used_proxies: list[str | None] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def __init__(self):
                self.get_next_calls = 0
                self.success_urls: list[str] = []
                self.fail_urls: list[str] = []

            def get_next(self, region: str = "", exclude_urls=None):
                self.get_next_calls += 1
                return "pool-proxy"

            def report_success(self, url: str) -> None:
                self.success_urls.append(url)

            def report_fail(self, url: str) -> None:
                self.fail_urls.append(url)

        fake_proxy_pool = _FakeProxyPool()

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                return types.SimpleNamespace(email="user@example.com", extra={})

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "true",
                "resin_proxy_url": "http://Default:token@127.0.0.1:2260",
            }
            return config.get(key, default)

        with patch("core.proxy_pool.proxy_pool", fake_proxy_pool), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[None],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(used_proxies, ["http://task-proxy.local:8080"])
        self.assertEqual(fake_proxy_pool.get_next_calls, 0)
        self.assertEqual(fake_proxy_pool.success_urls, [])
        self.assertEqual(fake_proxy_pool.fail_urls, [])

    def test_execute_register_task_prefers_scdn_runtime_proxy_over_pool(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }
        used_proxies: list[str | None] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def __init__(self):
                self.get_next_calls = 0
                self.success_urls: list[str] = []
                self.fail_urls: list[str] = []

            def get_next(self, region: str = "", exclude_urls=None):
                self.get_next_calls += 1
                return "pool-proxy"

            def report_success(self, url: str) -> None:
                self.success_urls.append(url)

            def report_fail(self, url: str) -> None:
                self.fail_urls.append(url)

        fake_proxy_pool = _FakeProxyPool()

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                return types.SimpleNamespace(email="user@example.com", extra={})

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "false",
                "scdn_runtime_enabled": "true",
            }
            return config.get(key, default)

        with patch("core.proxy_pool.proxy_pool", fake_proxy_pool), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(
            tasks,
            "_get_scdn_runtime_proxy",
            return_value="http://scdn-runtime-proxy:8080",
            create=True,
        ), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[None],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(used_proxies, ["http://scdn-runtime-proxy:8080"])
        self.assertEqual(fake_proxy_pool.get_next_calls, 0)
        self.assertEqual(fake_proxy_pool.success_urls, [])
        self.assertEqual(fake_proxy_pool.fail_urls, [])
        self.assertTrue(any("代理来源: SCDN 运行时来源" in message for message in logger.messages))

    def test_execute_register_task_falls_back_to_pool_when_scdn_runtime_proxy_missing(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }
        used_proxies: list[str | None] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def __init__(self):
                self.get_next_calls = 0
                self.success_urls: list[str] = []
                self.fail_urls: list[str] = []

            def get_next(self, region: str = "", exclude_urls=None):
                self.get_next_calls += 1
                return "pool-proxy"

            def report_success(self, url: str) -> None:
                self.success_urls.append(url)

            def report_fail(self, url: str) -> None:
                self.fail_urls.append(url)

        fake_proxy_pool = _FakeProxyPool()

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                return types.SimpleNamespace(email="user@example.com", extra={})

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "false",
                "scdn_runtime_enabled": "true",
            }
            return config.get(key, default)

        with patch("core.proxy_pool.proxy_pool", fake_proxy_pool), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(
            tasks,
            "_get_scdn_runtime_proxy",
            return_value=None,
            create=True,
        ), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[None],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(used_proxies, ["pool-proxy"])
        self.assertEqual(fake_proxy_pool.get_next_calls, 1)
        self.assertEqual(fake_proxy_pool.success_urls, ["pool-proxy"])
        self.assertEqual(fake_proxy_pool.fail_urls, [])
        self.assertTrue(any("SCDN 运行时来源未命中" in message for message in logger.messages))
        self.assertTrue(any("代理来源: 后端代理池" in message for message in logger.messages))

    def test_execute_register_task_fails_when_scdn_enabled_and_pool_empty(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "chatgpt",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def __init__(self):
                self.get_next_calls = 0

            def get_next(self, region: str = "", exclude_urls=None):
                self.get_next_calls += 1
                return None

            def report_success(self, url: str) -> None:
                raise AssertionError("should not report success when pool is empty")

            def report_fail(self, url: str) -> None:
                raise AssertionError("should not report fail when pool is empty")

        fake_proxy_pool = _FakeProxyPool()

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "false",
                "scdn_runtime_enabled": "true",
            }
            return config.get(key, default)

        with patch("core.proxy_pool.proxy_pool", fake_proxy_pool), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(
            tasks,
            "_get_scdn_runtime_proxy",
            return_value=None,
            create=True,
        ), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[None],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=AssertionError("should not build platform when no proxy is available"),
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(fake_proxy_pool.get_next_calls, 1)
        self.assertEqual(logger.finished[0], "failed")
        self.assertIn("SCDN 已启用，但未命中可用代理，且后端代理池为空", logger.finished[1])
        self.assertTrue(any("SCDN 运行时来源未命中" in message for message in logger.messages))


    def test_outlook_inventory_claim_options_follow_alias_checkbox(self):
        tasks = _import_tasks_module()

        disabled = tasks._inventory_claim_options({
            "platform": "freemodel",
            "extra": {
                "mail_provider": "outlook_token",
                "outlook_alias_enabled": False,
            },
        })
        enabled = tasks._inventory_claim_options({
            "platform": "freemodel",
            "extra": {
                "mail_provider": "outlook_token",
                "outlook_alias_enabled": True,
            },
        })
        explicit_disabled = tasks._inventory_claim_options({
            "platform": "freemodel",
            "extra": {
                "mail_provider": "outlook_token",
                "outlook_alias_enabled": False,
                "outlook_sub_mail_enabled": True,
            },
        })

        self.assertEqual(disabled, {"include_outlook_aliases": False})
        self.assertEqual(enabled, {"include_outlook_aliases": True})
        self.assertEqual(explicit_disabled, {"include_outlook_aliases": False})

    def test_build_inventory_target_email_creates_outlook_alias_when_enabled(self):
        tasks = _import_tasks_module()

        with patch.object(tasks.random, "choice", side_effect=list("abcd")):
            alias = tasks._build_inventory_target_email(
                "demo@outlook.com",
                {
                    "outlook_alias_enabled": True,
                    "sub_mail_mode": "plus",
                    "sub_mail_length": 4,
                },
                {"successful_registrations": 0},
                provider_key="outlook_token",
            )

        self.assertEqual(alias, "demo+abcd@outlook.com")

    def test_build_inventory_target_email_skips_outlook_alias_when_parent_limit_reached(self):
        tasks = _import_tasks_module()

        with patch.object(tasks.random, "choice", side_effect=list("abcd")):
            alias = tasks._build_inventory_target_email(
                "demo@outlook.com",
                {
                    "outlook_alias_enabled": True,
                    "sub_mail_mode": "plus",
                    "sub_mail_length": 4,
                    "outlook_alias_max_count": 2,
                },
                {
                    "successful_registrations": 1,
                    "outlook_alias_created_count": 2,
                },
                provider_key="outlook_token",
            )

        self.assertEqual(alias, "demo@outlook.com")


    def test_execute_register_task_adds_generated_outlook_alias_to_inventory_pool(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "freemodel",
            "count": 1,
            "concurrency": 1,
            "extra": {
                "mail_provider": "outlook_token",
                "outlook_alias_enabled": True,
                "sub_mail_mode": "plus",
                "sub_mail_length": 4,
            },
        }
        seed = types.SimpleNamespace(
            email="demo@outlook.com",
            password="mail-pass",
            extra={
                "mail_provider": "outlook_token",
                "outlook_email": "demo@outlook.com",
                "outlook_password": "mail-pass",
                "outlook_client_id": "client-123",
                "outlook_refresh_token": "refresh-456",
                "_inventory": {
                    "id": 42,
                    "provider_key": "outlook_token",
                    "metadata": {
                        "password": "mail-pass",
                        "client_id": "client-123",
                    },
                },
            },
        )
        build_payloads: list[dict] = []

        class _DummyInventoryRepository:
            def __init__(self):
                self.alias_calls: list[dict] = []
                self.success_calls: list[dict] = []

            def mark_registration_success(self, item_id, **kwargs):
                self.success_calls.append({"item_id": item_id, **kwargs})
                return None

            def upsert_outlook_alias(self, parent_item, *, alias_email, platform=""):
                self.alias_calls.append({
                    "parent_item": dict(parent_item),
                    "alias_email": alias_email,
                    "platform": platform,
                })
                return {"email": alias_email, "metadata": {}}

            def reset_many(self, *_args, **_kwargs):
                return None

            def update_item(self, item_id, **kwargs):
                self.parent_updates.append({"item_id": item_id, **kwargs})
                return None

        inventory_repository = _DummyInventoryRepository()
        inventory_repository.parent_updates = []

        class _FakeProxyPool:
            def get_next(self, region: str = "", exclude_urls=None):
                return None

            def report_success(self, url: str) -> None:
                return None

            def report_fail(self, url: str) -> None:
                return None

        class _FakePlatform:
            def register(self, email=None, password=None):
                self.email = email
                return types.SimpleNamespace(email=email, extra={})

        def fake_build_platform_instance(_platform_name, seed_payload, _logger, resolved_proxy=None):
            build_payloads.append(seed_payload)
            return _FakePlatform()

        with patch("core.proxy_pool.proxy_pool", _FakeProxyPool()), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[seed],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "_latest_freemodel_referral_code",
            return_value="",
        ), patch.object(
            tasks,
            "MailboxInventoryRepository",
            return_value=inventory_repository,
        ), patch.object(tasks, "_build_platform_instance", side_effect=fake_build_platform_instance), patch.object(
            tasks.random,
            "choice",
            side_effect=list("abcd"),
        ), patch.object(tasks, "save_account", return_value=None), patch.object(
            tasks,
            "_save_task_log",
            return_value=None,
        ), patch.object(tasks, "_auto_upload_cpa", return_value=None), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ), patch.object(tasks, "_auto_import_anuma2api", return_value=None), patch.object(
            tasks,
            "_auto_import_enter2api",
            return_value=None,
        ), patch.object(tasks, "_auto_import_blendspace2api", return_value=None), patch.object(
            tasks,
            "_auto_export_fireworks_key",
            return_value=None,
        ), patch.object(tasks, "_auto_export_gettoken_key", return_value=None), patch.object(
            tasks,
            "_auto_export_lemondata_key",
            return_value=None,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(build_payloads[0]["email"], "demo+abcd@outlook.com")
        self.assertEqual(build_payloads[0]["extra"]["outlook_registration_email"], "demo+abcd@outlook.com")
        self.assertEqual(build_payloads[0]["extra"]["outlook_alias_parent_email"], "demo@outlook.com")
        self.assertEqual(inventory_repository.success_calls[0]["item_id"], 42)
        self.assertEqual(inventory_repository.alias_calls[0]["alias_email"], "demo+abcd@outlook.com")
        self.assertEqual(inventory_repository.alias_calls[0]["platform"], "freemodel")
        parent_item = inventory_repository.alias_calls[0]["parent_item"]
        self.assertEqual(parent_item["email"], "demo@outlook.com")
        self.assertEqual(parent_item["purchase_token"], "refresh-456")
        self.assertEqual(parent_item["metadata"]["client_id"], "client-123")

        self.assertEqual(
            inventory_repository.parent_updates[0]["metadata_updates"]["outlook_alias_created_count"],
            1,
        )




    def test_execute_register_task_adds_returned_outlook_alias_to_inventory_pool(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "komilion",
            "count": 1,
            "concurrency": 1,
            "extra": {"mail_provider": "outlook_token"},
        }
        seed = types.SimpleNamespace(
            email="demo@outlook.com",
            password="mail-pass",
            extra={
                "mail_provider": "outlook_token",
                "outlook_email": "demo@outlook.com",
                "outlook_password": "mail-pass",
                "outlook_client_id": "client-123",
                "outlook_refresh_token": "refresh-456",
                "_inventory": {
                    "id": 42,
                    "provider_key": "outlook_token",
                    "metadata": {
                        "password": "mail-pass",
                        "client_id": "client-123",
                    },
                },
            },
        )

        class _DummyInventoryRepository:
            def __init__(self):
                self.alias_calls: list[dict] = []
                self.success_calls: list[dict] = []

            def mark_registration_success(self, item_id, **kwargs):
                self.success_calls.append({"item_id": item_id, **kwargs})
                return None

            def upsert_outlook_alias(self, parent_item, *, alias_email, platform=""):
                self.alias_calls.append({
                    "parent_item": dict(parent_item),
                    "alias_email": alias_email,
                    "platform": platform,
                })
                return {"email": alias_email, "metadata": {}}

            def reset_many(self, *_args, **_kwargs):
                return None

            def update_item(self, *_args, **_kwargs):
                return None

        inventory_repository = _DummyInventoryRepository()

        class _FakeProxyPool:
            def get_next(self, region: str = "", exclude_urls=None):
                return None

            def report_success(self, url: str) -> None:
                return None

            def report_fail(self, url: str) -> None:
                return None

        class _FakePlatform:
            def register(self, email=None, password=None):
                return types.SimpleNamespace(
                    email="demo+komilionsub@outlook.com",
                    extra={},
                )

        with patch("core.proxy_pool.proxy_pool", _FakeProxyPool()), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[seed],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "MailboxInventoryRepository",
            return_value=inventory_repository,
        ), patch.object(tasks, "_build_platform_instance", return_value=_FakePlatform()), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(tasks, "_auto_import_codebanana2api", return_value=None), patch.object(
            tasks,
            "_auto_import_anuma2api",
            return_value=None,
        ), patch.object(tasks, "_auto_import_enter2api", return_value=None), patch.object(
            tasks,
            "_auto_import_blendspace2api",
            return_value=None,
        ), patch.object(tasks, "_auto_export_fireworks_key", return_value=None), patch.object(
            tasks,
            "_auto_export_gettoken_key",
            return_value=None,
        ), patch.object(tasks, "_auto_export_lemondata_key", return_value=None):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(
            inventory_repository.success_calls[0]["registered_email"],
            "demo+komilionsub@outlook.com",
        )
        self.assertEqual(inventory_repository.alias_calls[0]["alias_email"], "demo+komilionsub@outlook.com")
        self.assertEqual(inventory_repository.alias_calls[0]["platform"], "komilion")
        parent_item = inventory_repository.alias_calls[0]["parent_item"]
        self.assertEqual(parent_item["email"], "demo@outlook.com")
        self.assertEqual(parent_item["purchase_token"], "refresh-456")
        self.assertEqual(parent_item["metadata"]["client_id"], "client-123")



    def test_execute_register_task_lemondata_requires_resin_when_enabled(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "lemondata",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }
        build_calls: list[object] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def get_next(self, region: str = "", exclude_urls=None):
                return None

            def report_success(self, url: str) -> None:
                return None

            def report_fail(self, url: str) -> None:
                return None

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "true",
                "resin_scheme": "http",
                "resin_host": "127.0.0.1",
                "resin_port": "2260",
                "resin_token": "token-123",
                "resin_default_platform": "Default",
            }
            return config.get(key, default)

        def fake_build_platform_instance(*_args, **_kwargs):
            build_calls.append(_kwargs.get("resolved_proxy"))
            raise AssertionError("LemonData Resin 不可用时不应进入注册")

        with patch("core.proxy_pool.proxy_pool", _FakeProxyPool()), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(tasks, "_resolve_register_lines", return_value=[None]), patch.object(
            tasks,
            "get",
            return_value=object(),
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "_probe_proxy_ip", return_value=""), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None):
            tasks._execute_register_task(payload, logger)

        self.assertFalse(build_calls)
        self.assertTrue(any("LemonData Resin 未命中可用 IP" in message for message in logger.messages))
        self.assertEqual(logger.success_count, 0)
        self.assertTrue(logger.error_messages)


    def test_execute_register_task_retries_lemondata_connection_reset_with_new_resin_session(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "lemondata",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {"lemondata_proxy_retry_attempts": "3"},
        }
        used_proxies: list[str | None] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def get_next(self, region: str = "", exclude_urls=None):
                return None

            def report_success(self, url: str) -> None:
                return None

            def report_fail(self, url: str) -> None:
                return None

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                if self._resolved_proxy and "Default.vs0" in self._resolved_proxy:
                    raise RuntimeError(
                        "('Connection aborted.', ConnectionResetError(10054, "
                        "'远程主机强迫关闭了一个现有的连接。', None, 10054, None))"
                    )
                return types.SimpleNamespace(
                    platform="lemondata",
                    email=email or "demo@example.com",
                    token="ld_sk_demo",
                    extra={"api_key": "ld_sk_demo"},
                )

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "true",
                "resin_scheme": "http",
                "resin_host": "127.0.0.1",
                "resin_port": "2260",
                "resin_token": "token-123",
                "resin_default_platform": "Default",
            }
            return config.get(key, default)

        def fake_probe_ip(proxy_url: str) -> str:
            if "Default.vs0" in str(proxy_url):
                return "198.51.100.10"
            if "Default.vs1" in str(proxy_url):
                return "198.51.100.11"
            return "198.51.100.99"

        from contextlib import ExitStack

        with ExitStack() as stack:
            stack.enter_context(patch("core.proxy_pool.proxy_pool", _FakeProxyPool()))
            stack.enter_context(patch.object(tasks.config_store, "get", side_effect=fake_config_get))
            stack.enter_context(patch.object(tasks, "_resolve_register_lines", return_value=[None]))
            stack.enter_context(patch.object(tasks, "get", return_value=object()))
            stack.enter_context(patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()))
            stack.enter_context(patch.object(tasks, "_build_platform_instance", side_effect=fake_build_platform_instance))
            stack.enter_context(patch.object(tasks, "_probe_proxy_ip", side_effect=fake_probe_ip))
            stack.enter_context(patch.object(tasks, "save_account", return_value=None))
            stack.enter_context(patch.object(tasks, "_save_task_log", return_value=None))
            for helper_name in (
                "_auto_upload_cpa",
                "_auto_import_codebanana2api",
                "_auto_import_anuma2api",
                "_auto_import_enter2api",
                "_auto_import_blendspace2api",
                "_auto_export_fireworks_key",
                "_auto_export_gettoken_key",
                "_auto_export_lemondata_key",
                "_auto_export_zo_key",
                "_auto_push_zo_twoapi",
                "_auto_push_swarms_twoapi",
                "_auto_push_anycap_twoapi",
                "_auto_export_swarms_key",
                "_auto_export_anycap_key",
                "_auto_export_featherless_key",
                "_auto_export_jiekou_key",
            ):
                stack.enter_context(patch.object(tasks, helper_name, return_value=None))
            stack.enter_context(patch.object(tasks.time, "sleep", return_value=None))
            tasks._execute_register_task(payload, logger)

        self.assertEqual(
            used_proxies,
            [
                "http://Default.vs0:token-123@127.0.0.1:2260",
                "http://Default.vs1:token-123@127.0.0.1:2260",
            ],
        )
        self.assertEqual(logger.success_count, 1)
        self.assertTrue(any("LemonData 代理连接异常，换 IP 重试 2/3" in message for message in logger.messages))
        self.assertTrue(any("Resin IP 198.51.100.10 banned" in message for message in logger.messages))


    def test_execute_register_task_uses_distinct_resin_sessions_for_swarms_concurrency(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "swarms",
            "count": 3,
            "concurrency": 3,
            "extra": {},
        }
        used_proxies: list[str | None] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def get_next(self, region: str = "", exclude_urls=None):
                return None

            def report_success(self, url: str) -> None:
                return None

            def report_fail(self, url: str) -> None:
                return None

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                return types.SimpleNamespace(email=email or f"user{len(used_proxies)}@example.com", extra={})

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "true",
                "resin_scheme": "http",
                "resin_host": "127.0.0.1",
                "resin_port": "2260",
                "resin_token": "token-123",
                "resin_default_platform": "Default",
            }
            return config.get(key, default)

        with patch("core.proxy_pool.proxy_pool", _FakeProxyPool()), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(tasks, "_resolve_register_lines", return_value=[None, None, None]), patch.object(
            tasks,
            "get",
            return_value=object(),
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "_probe_proxy_ip", return_value="198.51.100.8"), patch.object(
            tasks,
            "_probe_swarms_signup_page",
            return_value=True,
        ), patch.object(
            tasks,
            "save_account",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None), patch.object(
            tasks,
            "_auto_upload_cpa",
            return_value=None,
        ), patch.object(tasks, "_auto_import_codebanana2api", return_value=None):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(
            sorted(used_proxies),
            [
                "http://Default.vs0:token-123@127.0.0.1:2260",
                "http://Default.vs1:token-123@127.0.0.1:2260",
                "http://Default.vs2:token-123@127.0.0.1:2260",
            ],
        )


    def test_execute_register_task_skips_bad_swarms_resin_session_before_register(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "swarms",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }
        used_proxies: list[str | None] = []
        preflight_calls: list[str] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def get_next(self, region: str = "", exclude_urls=None):
                return None

            def report_success(self, url: str) -> None:
                return None

            def report_fail(self, url: str) -> None:
                return None

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                return types.SimpleNamespace(email=email or "demo@example.com", extra={})

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "true",
                "resin_scheme": "http",
                "resin_host": "127.0.0.1",
                "resin_port": "2260",
                "resin_token": "token-123",
                "resin_default_platform": "Default",
            }
            return config.get(key, default)

        def fake_preflight(proxy_url: str) -> bool:
            preflight_calls.append(proxy_url)
            return proxy_url.endswith("Default.vs1:token-123@127.0.0.1:2260")

        with patch("core.proxy_pool.proxy_pool", _FakeProxyPool()), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(tasks, "_resolve_register_lines", return_value=[None]), patch.object(
            tasks,
            "get",
            return_value=object(),
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "_probe_proxy_ip", return_value="198.51.100.8"), patch.object(
            tasks,
            "_probe_swarms_signup_page",
            side_effect=fake_preflight,
        ), patch.object(tasks, "save_account", return_value=None), patch.object(
            tasks,
            "_save_task_log",
            return_value=None,
        ), patch.object(tasks, "_auto_upload_cpa", return_value=None), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(
            preflight_calls,
            [
                "http://Default.vs0:token-123@127.0.0.1:2260",
                "http://Default.vs1:token-123@127.0.0.1:2260",
            ],
        )
        self.assertEqual(used_proxies, ["http://Default.vs1:token-123@127.0.0.1:2260"])
        self.assertTrue(any("Swarms 注册页预检失败" in message for message in logger.messages))

    def test_execute_register_task_stops_swarms_resin_after_repeated_ip_probe_failures(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "swarms",
            "count": 1,
            "concurrency": 1,
            "email": "demo@example.com",
            "password": "pw",
            "extra": {},
        }
        used_proxies: list[str | None] = []

        class _DummyInventoryRepository:
            def reset_many(self, *_args, **_kwargs):
                return None

        class _FakeProxyPool:
            def get_next(self, region: str = "", exclude_urls=None):
                return None

            def report_success(self, url: str) -> None:
                return None

            def report_fail(self, url: str) -> None:
                return None

        class _FakePlatform:
            def __init__(self, resolved_proxy: str | None):
                self._resolved_proxy = resolved_proxy

            def register(self, email=None, password=None):
                used_proxies.append(self._resolved_proxy)
                return types.SimpleNamespace(email=email or "demo@example.com", extra={})

        def fake_build_platform_instance(_platform_name, _seed_payload, _logger, resolved_proxy=None):
            return _FakePlatform(resolved_proxy)

        def fake_config_get(key: str, default=""):
            config = {
                "resin_enabled": "true",
                "resin_scheme": "http",
                "resin_host": "127.0.0.1",
                "resin_port": "2260",
                "resin_token": "token-123",
                "resin_default_platform": "Default",
            }
            return config.get(key, default)

        probe_calls: list[str] = []

        def fake_probe_ip(proxy_url: str) -> str:
            probe_calls.append(proxy_url)
            return ""

        with patch("core.proxy_pool.proxy_pool", _FakeProxyPool()), patch.object(
            tasks.config_store,
            "get",
            side_effect=fake_config_get,
        ), patch.object(tasks, "_resolve_register_lines", return_value=[None]), patch.object(
            tasks,
            "get",
            return_value=object(),
        ), patch.object(tasks, "MailboxInventoryRepository", return_value=_DummyInventoryRepository()), patch.object(
            tasks,
            "_build_platform_instance",
            side_effect=fake_build_platform_instance,
        ), patch.object(tasks, "_probe_proxy_ip", side_effect=fake_probe_ip), patch.object(
            tasks,
            "_probe_swarms_signup_page",
            return_value=False,
        ), patch.object(tasks, "save_account", return_value=None), patch.object(
            tasks,
            "_save_task_log",
            return_value=None,
        ), patch.object(tasks, "_auto_upload_cpa", return_value=None), patch.object(
            tasks,
            "_auto_import_codebanana2api",
            return_value=None,
        ):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(len(probe_calls), 3)
        self.assertEqual(used_proxies, [None])
        self.assertTrue(any("连续 3 次 IP 探测失败" in message for message in logger.messages))

    def test_execute_register_task_recycles_outlook_inventory_on_swarms_proxy_failure(self):
        tasks = _import_tasks_module()
        logger = _DummyTaskLogger()
        payload = {
            "platform": "swarms",
            "count": 1,
            "concurrency": 1,
            "extra": {"mail_provider": "outlook_token"},
        }
        seed = types.SimpleNamespace(
            email="demo@outlook.com",
            password="mail-pass",
            extra={
                "mail_provider": "outlook_token",
                "outlook_email": "demo@outlook.com",
                "outlook_password": "mail-pass",
                "outlook_client_id": "client-123",
                "outlook_refresh_token": "refresh-456",
                "_inventory": {
                    "id": 42,
                    "provider_key": "outlook_token",
                    "metadata": {"password": "mail-pass", "client_id": "client-123"},
                },
            },
        )

        class _DummyInventoryRepository:
            def __init__(self):
                self.update_calls: list[dict] = []

            def update_item(self, item_id, **kwargs):
                self.update_calls.append({"item_id": item_id, **kwargs})
                return None

            def reset_many(self, *_args, **_kwargs):
                return None

        inventory_repository = _DummyInventoryRepository()

        class _FakeProxyPool:
            def get_next(self, region: str = "", exclude_urls=None):
                return None

            def report_success(self, url: str) -> None:
                return None

            def report_fail(self, url: str) -> None:
                return None

        class _FakePlatform:
            def register(self, email=None, password=None):
                raise RuntimeError("Swarms 注册请求失败: ProxyError: Tunnel connection failed: 504 Gateway Timeout")

        with patch("core.proxy_pool.proxy_pool", _FakeProxyPool()), patch.object(
            tasks,
            "_resolve_register_lines",
            return_value=[seed],
        ), patch.object(tasks, "get", return_value=object()), patch.object(
            tasks,
            "MailboxInventoryRepository",
            return_value=inventory_repository,
        ), patch.object(tasks, "_build_platform_instance", return_value=_FakePlatform()), patch.object(
            tasks,
            "_persist_registration_snapshot",
            return_value=None,
        ), patch.object(tasks, "_save_task_log", return_value=None):
            tasks._execute_register_task(payload, logger)

        self.assertEqual(inventory_repository.update_calls[0]["item_id"], 42)
        self.assertEqual(inventory_repository.update_calls[0]["status"], "unused")
        self.assertIn("注册失败", inventory_repository.update_calls[0]["note"])
        self.assertIn("Gateway Timeout", inventory_repository.update_calls[0]["last_error"])

    def test_create_register_task_maps_swarms_browser_mode_to_headed_executor(self):
        tasks = _import_tasks_module()
        captured: dict[str, object] = {}
        payload = {
            "platform": "swarms",
            "count": 1,
            "executor_type": "protocol",
            "captcha_solver": "auto",
            "extra": {
                "identity_provider": "mailbox",
                "mail_provider": "yyds_mail",
                "swarms_registration_mode": "browser",
            },
        }

        def fake_create_task(**kwargs):
            captured.update(kwargs)
            return {"task_id": "task_swarms", **kwargs}

        with patch.object(tasks, "_available_inventory_register_count", return_value=0), patch.object(
            tasks,
            "_resolve_inventory_provider_key",
            return_value="",
        ), patch.object(tasks, "create_task", side_effect=fake_create_task):
            tasks.create_register_task(payload)

        self.assertEqual(captured["payload"]["executor_type"], "headed")

if __name__ == "__main__":
    unittest.main()
