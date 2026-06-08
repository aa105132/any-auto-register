import importlib
import sys
import types
import unittest

from core.base_platform import RegisterConfig


def _ensure_sqlalchemy_stub() -> None:
    if "sqlalchemy" in sys.modules:
        return

    module = types.ModuleType("sqlalchemy")

    class UniqueConstraint:
        def __init__(self, *_args, **_kwargs):
            pass

    def inspect(*_args, **_kwargs):
        return None

    module.UniqueConstraint = UniqueConstraint
    module.inspect = inspect
    sys.modules["sqlalchemy"] = module


def _ensure_sqlmodel_stub() -> None:
    if "sqlmodel" in sys.modules:
        return

    module = types.ModuleType("sqlmodel")

    class SQLModel:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    class Session:
        pass

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

    module.Field = Field
    module.SQLModel = SQLModel
    module.Session = Session
    module.create_engine = create_engine
    module.select = select
    module.delete = delete
    sys.modules["sqlmodel"] = module


def _import_module(module_name: str):
    _ensure_sqlalchemy_stub()
    _ensure_sqlmodel_stub()
    return importlib.import_module(module_name)


def _load_attr(testcase: unittest.TestCase, module_name: str, attr_name: str):
    try:
        module = _import_module(module_name)
    except Exception as exc:  # pragma: no cover
        testcase.fail(f"导入 {module_name} 失败: {exc}")
    if not hasattr(module, attr_name):
        testcase.fail(f"{module_name} 缺少 {attr_name}")
    return getattr(module, attr_name)


class AnumaPlatformTests(unittest.TestCase):
    def test_browser_adapter_uses_mailbox_otp_contract(self):
        platform_cls = _load_attr(self, "platforms.anuma.plugin", "AnumaPlatform")
        platform = platform_cls(
            RegisterConfig(executor_type="headless", extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )

        adapter = platform.build_browser_registration_adapter()

        self.assertEqual(platform.supported_executors, ["headless", "headed"])
        self.assertEqual(platform.supported_identity_modes, ["mailbox"])
        self.assertEqual(adapter.otp_spec.keyword, "Anuma")
        self.assertEqual(adapter.otp_spec.code_pattern, r"(?<!\d)(\d{6})(?!\d)")
        self.assertEqual(adapter.otp_spec.wait_message, "等待 Anuma 验证码...")
        self.assertEqual(adapter.otp_spec.success_label, "Anuma 验证码")

    def test_map_result_extracts_privy_credentials_and_wallet(self):
        platform_cls = _load_attr(self, "platforms.anuma.plugin", "AnumaPlatform")
        platform = platform_cls(RegisterConfig(executor_type="headless"), mailbox=None)

        raw = {
            "email": "demo@example.com",
            "password": "",
            "url": "https://chat.anuma.ai/zh-CN",
            "title": "Anuma.ai",
            "cookies": [
                {"name": "privy-session", "value": "t"},
                {
                    "name": "privy-token",
                    "value": "eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJkaWQ6cHJpdnk6ZGVtby11c2VyIn0.sig",
                },
                {
                    "name": "privy-id-token",
                    "value": (
                        "eyJhbGciOiJFUzI1NiJ9."
                        "eyJzdWIiOiJkaWQ6cHJpdnk6ZGVtby11c2VyIiwi"
                        "bGlua2VkX2FjY291bnRzIjoiW3tcInR5cGVcIjpcImVtYWlsXCIsXCJhZGRyZXNzXCI6XCJkZW1vQGV4YW1wbGUuY29tXCJ9LHtcInR5cGVcIjpcIndhbGxldFwiLFwiYWRkcmVzc1wiOlwiMHhBQkNcIn1dIn0."
                        "sig"
                    ),
                },
            ],
            "local_storage": {
                "privy:refresh_token": "\"refresh-demo\"",
                "privy:caid": "\"caid-demo\"",
                "privy:connections": '[{"type":"wallet","address":"0xABC"}]',
            },
            "session_storage": {"anuma:app-visit-tracked": "1"},
        }

        mapped = platform._map_anuma_result(raw)

        self.assertEqual(mapped.email, "demo@example.com")
        self.assertEqual(mapped.user_id, "did:privy:demo-user")
        self.assertEqual(mapped.token, raw["cookies"][1]["value"])
        self.assertEqual(mapped.extra["privy_session"], "t")
        self.assertEqual(mapped.extra["privy_refresh_token"], "refresh-demo")
        self.assertEqual(mapped.extra["privy_caid"], "caid-demo")
        self.assertEqual(mapped.extra["wallet_address"], "0xABC")
        self.assertEqual(mapped.extra["account_overview"]["privy_did"], "did:privy:demo-user")
        self.assertIn("privy-token=", mapped.extra["cookies"])

    def test_package_exports_expected_symbols(self):
        package = _import_module("platforms.anuma")
        self.assertTrue(hasattr(package, "AnumaPlatform"))
        self.assertTrue(hasattr(package, "AnumaBrowserRegister"))


if __name__ == "__main__":
    unittest.main()
