import importlib
import unittest
from unittest.mock import patch

from core.base_platform import Account, RegisterConfig
from core.registration import RegistrationContext


def _load_attr(testcase: unittest.TestCase, module_name: str, attr_name: str):
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - RED 阶段用于把导入错误转成失败
        testcase.fail(f"导入 {module_name} 失败: {exc}")
    if not hasattr(module, attr_name):
        testcase.fail(f"{module_name} 缺少 {attr_name}")
    return getattr(module, attr_name)


class _FakeClient:
    def __init__(self, *, clowdbot_error: Exception | None = RuntimeError("claim_email failed")):
        self.calls = []
        self.clowdbot_error = clowdbot_error

    def send_privy_code(self, email, ca_id):
        self.calls.append(("send_privy_code", email, ca_id))
        return {"ok": True}

    def authenticate_privy(self, email, code, ca_id):
        self.calls.append(("authenticate_privy", email, code, ca_id))
        return {"token": "privy-token", "refresh_token": "refresh-token"}

    def fetch_atxp_bundle(self, token):
        self.calls.append(("fetch_atxp_bundle", token))
        return {
            "me": {"accountId": "acct-1"},
            "wallet_info": {"wallet": {"address": "0xabc"}},
            "account_id": "acct-1",
            "wallet_address": "0xabc",
            "connection_token": "conn-1",
        }

    def check_balance_via_connection(self, connection_token):
        self.calls.append(("check_balance_via_connection", connection_token))
        # chat.atxp.ai/api/balance returns {"balance": <number>}
        return {"balance": 3.0}

    def complete_clowdbot_tasks(self, privy_token, account_id, email):
        self.calls.append(("complete_clowdbot_tasks", privy_token, account_id, email))
        if self.clowdbot_error is not None:
            raise self.clowdbot_error
        return {
            "instance_id": "clowd-1",
            "claimed_agent_email": "agent@example.com",
            "create_clowdbot_completed": True,
            "claim_email_completed": True,
            "reward_progress": {"claimed": 1, "total": 2},
        }


class AtxpWorkerTests(unittest.TestCase):
    def test_worker_skips_clowdbot_by_default(self):
        worker_cls = _load_attr(
            self,
            "platforms.atxp.protocol_mailbox",
            "AtxpProtocolMailboxWorker",
        )
        fake_client = _FakeClient()
        worker = worker_cls(client=fake_client, log_fn=lambda _message: None)

        with patch("platforms.atxp.protocol_mailbox.uuid.uuid4", return_value="ca-fixed-id"):
            result = worker.run(
                email="demo@example.com",
                password="mailbox-pass",
                otp_callback=lambda: "123456",
            )

        self.assertEqual(
            fake_client.calls,
            [
                ("send_privy_code", "demo@example.com", "ca-fixed-id"),
                ("authenticate_privy", "demo@example.com", "123456", "ca-fixed-id"),
                ("fetch_atxp_bundle", "privy-token"),
                ("check_balance_via_connection", "conn-1"),
            ],
        )
        self.assertEqual(result["account_id"], "acct-1")
        self.assertEqual(result["privy_token"], "privy-token")
        self.assertEqual(result["refresh_token"], "refresh-token")
        self.assertEqual(result["connection_token"], "conn-1")
        self.assertEqual(result["wallet_address"], "0xabc")
        self.assertEqual(
            result["connection_string"],
            "https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1",
        )
        self.assertEqual(result["clowdbot_status"], "skipped")

    def test_worker_fails_when_balance_insufficient(self):
        worker_cls = _load_attr(
            self,
            "platforms.atxp.protocol_mailbox",
            "AtxpProtocolMailboxWorker",
        )

        class _PoorClient(_FakeClient):
            def check_balance_via_connection(self, connection_token):
                self.calls.append(("check_balance_via_connection", connection_token))
                return {"balance": 0.0}

        fake_client = _PoorClient()
        worker = worker_cls(client=fake_client, log_fn=lambda _message: None)

        with patch("platforms.atxp.protocol_mailbox.uuid.uuid4", return_value="ca-fixed-id"):
            with self.assertRaises(RuntimeError) as ctx:
                worker.run(
                    email="demo@example.com",
                    password="mailbox-pass",
                    otp_callback=lambda: "123456",
                )
        self.assertIn("余额不足", str(ctx.exception))
        self.assertIn("$0.00", str(ctx.exception))

    def test_worker_keeps_atxp_credentials_when_clowdbot_fails(self):
        worker_cls = _load_attr(
            self,
            "platforms.atxp.protocol_mailbox",
            "AtxpProtocolMailboxWorker",
        )
        fake_client = _FakeClient()
        worker = worker_cls(client=fake_client, log_fn=lambda _message: None)

        with patch("platforms.atxp.protocol_mailbox.uuid.uuid4", return_value="ca-fixed-id"):
            result = worker.run(
                email="demo@example.com",
                password="mailbox-pass",
                otp_callback=lambda: "123456",
                enable_clowdbot=True,
            )

        self.assertEqual(
            fake_client.calls,
            [
                ("send_privy_code", "demo@example.com", "ca-fixed-id"),
                ("authenticate_privy", "demo@example.com", "123456", "ca-fixed-id"),
                ("fetch_atxp_bundle", "privy-token"),
                ("check_balance_via_connection", "conn-1"),
                ("complete_clowdbot_tasks", "privy-token", "acct-1", "demo@example.com"),
            ],
        )
        self.assertEqual(result["clowdbot_status"], "failed")
        self.assertIn("claim_email failed", result["task_error"])

    def test_worker_records_successful_clowdbot_payload(self):
        worker_cls = _load_attr(
            self,
            "platforms.atxp.protocol_mailbox",
            "AtxpProtocolMailboxWorker",
        )
        fake_client = _FakeClient(clowdbot_error=None)
        worker = worker_cls(client=fake_client, log_fn=lambda _message: None)

        result = worker.run(
            email="demo@example.com",
            password="mailbox-pass",
            otp_callback=lambda: "123456",
            enable_clowdbot=True,
        )

        self.assertEqual(result["clowdbot_status"], "completed")
        self.assertEqual(result["clowdbot_instance_id"], "clowd-1")
        self.assertEqual(result["claimed_agent_email"], "agent@example.com")
        self.assertTrue(result["create_clowdbot_completed"])
        self.assertTrue(result["claim_email_completed"])
        self.assertEqual(result["reward_progress"], {"claimed": 1, "total": 2})
        self.assertEqual(
            result["clowdbot_result"]["instance_id"],
            "clowd-1",
        )


class AtxpPlatformTests(unittest.TestCase):
    def test_map_result_uses_connection_string_as_primary_token(self):
        platform_cls = _load_attr(self, "platforms.atxp.plugin", "AtxpPlatform")
        platform = platform_cls(
            RegisterConfig(proxy="http://proxy.local:8080", extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )
        raw = {
            "email": "demo@example.com",
            "password": "mailbox-pass",
            "account_id": "acct-1",
            "privy_token": "privy-token",
            "refresh_token": "refresh-token",
            "connection_token": "conn-1",
            "connection_string": "https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1",
            "wallet_address": "0xabc",
            "gateway_health": {
                "success": True,
                "model": "gpt-4.1-mini",
                "checked_at": "2026-04-17T12:00:00Z",
            },
            "clowdbot_status": "failed",
            "create_clowdbot_completed": True,
            "claim_email_completed": False,
            "reward_progress": {"claimed": 0, "total": 2},
            "task_error": "claim_email failed",
            "me": {"accountId": "acct-1"},
            "wallet_info": {"wallet": {"address": "0xabc"}},
            "clowdbot_result": {"stage": "claim_email"},
        }

        mapped = platform._map_atxp_result(raw, password="mailbox-pass")

        self.assertEqual(mapped.email, "demo@example.com")
        self.assertEqual(mapped.password, "mailbox-pass")
        self.assertEqual(mapped.user_id, "acct-1")
        self.assertEqual(mapped.token, raw["connection_string"])
        self.assertEqual(mapped.extra["privy_token"], "privy-token")
        self.assertEqual(mapped.extra["refresh_token"], "refresh-token")
        self.assertEqual(mapped.extra["connection_token"], "conn-1")
        self.assertEqual(mapped.extra["wallet_address"], "0xabc")
        overview = mapped.extra["account_overview"]
        self.assertEqual(overview["gateway_health"], raw["gateway_health"])
        self.assertTrue(overview["gateway_health_alive"])
        self.assertEqual(overview["gateway_health_model"], "gpt-4.1-mini")
        self.assertEqual(overview["gateway_health_checked_at"], "2026-04-17T12:00:00Z")
        self.assertEqual(overview["clowdbot_status"], "failed")
        self.assertTrue(overview["create_clowdbot_completed"])
        self.assertFalse(overview["claim_email_completed"])
        self.assertEqual(overview["reward_progress"], {"claimed": 0, "total": 2})
        self.assertEqual(overview["task_error"], "claim_email failed")
        self.assertEqual(overview["atxp_me"], {"accountId": "acct-1"})
        self.assertEqual(overview["wallet_info"], {"wallet": {"address": "0xabc"}})
        self.assertEqual(overview["clowdbot_result"], {"stage": "claim_email"})

    def test_platform_adapter_action_and_valid_contracts(self):
        platform_cls = _load_attr(self, "platforms.atxp.plugin", "AtxpPlatform")
        platform = platform_cls(
            RegisterConfig(proxy="http://proxy.local:8080", extra={"mail_provider": "cfworker"}),
            mailbox=None,
        )

        adapter = platform.build_protocol_mailbox_adapter()
        self.assertEqual(adapter.otp_spec.keyword, "ATXP")
        self.assertEqual(adapter.otp_spec.code_pattern, r"(?<!\d)(\d{6})(?!\d)")
        self.assertEqual(adapter.otp_spec.wait_message, "等待 ATXP 验证码...")
        self.assertEqual(adapter.otp_spec.success_label, "ATXP 验证码")

        actions = platform.get_platform_actions()
        self.assertEqual(
            actions,
            [
                {"id": "reauth_privy", "label": "重新认证 Privy (邮箱OTP)", "params": []},
                {"id": "retry_clowdbot_tasks", "label": "一键领取 Clowdbot 奖励", "params": []},
                {"id": "check_balance", "label": "查询余额", "params": []},
            ],
        )

        account = Account(
            platform="atxp",
            email="demo@example.com",
            password="mailbox-pass",
            user_id="acct-1",
            token="token-1",
            extra={"privy_token": "privy-token", "refresh_token": "refresh-1", "account_id": "acct-1"},
        )
        self.assertTrue(platform.check_valid(account))
        self.assertFalse(platform.check_valid(Account(platform="atxp", email="x", password="y", token="")))

        captured = {}

        class _RetryClient:
            def __init__(self, **kwargs):
                captured["init_kwargs"] = kwargs

            def refresh_privy_token(self, refresh_token):
                captured["refresh"] = refresh_token
                return {"token": "refreshed-privy-token", "refresh_token": "refreshed-refresh-1"}

            def complete_clowdbot_tasks(self, privy_token, account_id, email):
                captured["call"] = (privy_token, account_id, email)
                return {
                    "instance_id": "clowd-1",
                    "claimed_agent_email": "agent@example.com",
                    "create_clowdbot_completed": True,
                    "claim_email_completed": True,
                    "reward_progress": {"claimed": 1, "total": 2},
                }

        with patch("platforms.atxp.plugin.AtxpClient", _RetryClient):
            action_result = platform.execute_action("retry_clowdbot_tasks", account, {})

        # refresh_privy_token succeeds → complete_clowdbot_tasks called with refreshed token
        self.assertEqual(captured["refresh"], "refresh-1")
        self.assertEqual(captured["call"], ("refreshed-privy-token", "acct-1", "demo@example.com"))
        self.assertEqual(captured["init_kwargs"]["proxy"], "http://proxy.local:8080")
        self.assertTrue(callable(captured["init_kwargs"]["log_fn"]))
        self.assertEqual(
            action_result,
            {
                "ok": True,
                "data": {
                    "message": "Clowdbot 任务补跑完成",
                    "credential_updates": {
                        "privy_token": "refreshed-privy-token",
                        "refresh_token": "refreshed-refresh-1",
                        "clowdbot_instance_id": "clowd-1",
                        "claimed_agent_email": "agent@example.com",
                    },
                    "account_overview": {
                        "clowdbot_status": "completed",
                        "create_clowdbot_completed": True,
                        "claim_email_completed": True,
                        "reward_progress": {"claimed": 1, "total": 2},
                        "task_error": "",
                        "clowdbot_result": {
                            "instance_id": "clowd-1",
                            "claimed_agent_email": "agent@example.com",
                            "create_clowdbot_completed": True,
                            "claim_email_completed": True,
                            "reward_progress": {"claimed": 1, "total": 2},
                        },
                    },
                },
            },
        )
        with self.assertRaises(NotImplementedError):
            platform.execute_action("unsupported_action", account, {})

    def test_platform_adapter_register_runner_and_result_mapper_wire_ctx_fields(self):
        platform_cls = _load_attr(self, "platforms.atxp.plugin", "AtxpPlatform")
        platform = platform_cls(RegisterConfig(extra={"mail_provider": "cfworker"}), mailbox=None)
        adapter = platform.build_protocol_mailbox_adapter()

        class _FakeWorker:
            def __init__(self):
                self.calls = []

            def run(self, **kwargs):
                self.calls.append(kwargs)
                return {
                    "email": kwargs["email"],
                    "password": kwargs["password"],
                    "account_id": "acct-1",
                    "connection_string": "https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1",
                    "connection_token": "conn-1",
                    "privy_token": "privy-token",
                    "refresh_token": "refresh-token",
                }

        worker = _FakeWorker()
        otp_callback = lambda: "123456"
        ctx = RegistrationContext(
            platform_name="atxp",
            platform_display_name="ATXP",
            platform=platform,
            identity=type("Identity", (), {"email": "demo@example.com"})(),
            config=RegisterConfig(proxy="http://proxy.local:8080"),
            email="demo@example.com",
            password="mailbox-pass",
            log_fn=lambda _message: None,
        )
        artifacts = type("Artifacts", (), {})()
        artifacts.otp_callback = otp_callback

        raw = adapter.register_runner(worker, ctx, artifacts)
        mapped = adapter.result_mapper(ctx, raw)

        self.assertEqual(
            worker.calls,
            [
                {
                    "email": "demo@example.com",
                    "password": "mailbox-pass",
                    "otp_callback": otp_callback,
                    "enable_clowdbot": False,
                }
            ],
        )
        self.assertEqual(mapped.email, "demo@example.com")
        self.assertEqual(mapped.password, "mailbox-pass")
        self.assertEqual(mapped.user_id, "acct-1")
        self.assertEqual(
            mapped.token,
            "https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1",
        )

    def test_load_register_only_falls_back_for_missing_optional_dependency(self):
        plugin_module = importlib.import_module("platforms.atxp.plugin")

        register = plugin_module._load_register(
            import_module=lambda _name: (_ for _ in ()).throw(
                ModuleNotFoundError("No module named 'sqlmodel'", name="sqlmodel")
            )
        )

        class _Demo:
            pass

        self.assertIs(register(_Demo), _Demo)

    def test_load_register_reraises_unexpected_import_failure(self):
        plugin_module = importlib.import_module("platforms.atxp.plugin")

        with self.assertRaises(RuntimeError):
            plugin_module._load_register(
                import_module=lambda _name: (_ for _ in ()).throw(RuntimeError("boom"))
            )

    def test_package_exports_expected_symbols(self):
        atxp_pkg = importlib.import_module("platforms.atxp")
        self.assertTrue(hasattr(atxp_pkg, "AtxpClient"))
        self.assertTrue(hasattr(atxp_pkg, "AtxpProtocolMailboxWorker"))
        self.assertTrue(hasattr(atxp_pkg, "AtxpPlatform"))


if __name__ == "__main__":
    unittest.main()
