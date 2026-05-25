from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.provider_drivers import get_driver_template, list_builtin_provider_definitions


class HaozhuPhoneProviderCatalogTests(unittest.TestCase):
    def test_haozhu_driver_template_exposes_required_fields(self):
        template = get_driver_template("phone", "haozhu_sms_api")

        self.assertIsNotNone(template)
        self.assertEqual(template["provider_type"], "phone")
        self.assertEqual(template["default_auth_mode"], "username_password")
        fields = {str(item.get("key")): item for item in template.get("fields", [])}
        self.assertIn("haozhu_api_base_url", fields)
        self.assertIn("haozhu_username", fields)
        self.assertIn("haozhu_password", fields)
        self.assertIn("haozhu_project_id", fields)
        self.assertIn("haozhu_token", fields)
        self.assertEqual(fields["haozhu_project_id"].get("category"), "task")

    def test_builtin_phone_definitions_include_haozhu(self):
        definitions = {
            str(item.get("provider_key")): item
            for item in list_builtin_provider_definitions("phone")
        }

        self.assertIn("haozhu", definitions)
        self.assertEqual(definitions["haozhu"]["driver_type"], "haozhu_sms_api")
        self.assertEqual(definitions["haozhu"]["label"], "豪猪")



    def test_qianchuan_driver_template_exposes_required_fields(self):
        template = get_driver_template("phone", "qianchuan_sms_api")

        self.assertIsNotNone(template)
        self.assertEqual(template["provider_type"], "phone")
        self.assertEqual(template["default_auth_mode"], "username_password")
        fields = {str(item.get("key")): item for item in template.get("fields", [])}
        self.assertIn("qianchuan_api_base_url", fields)
        self.assertIn("qianchuan_username", fields)
        self.assertIn("qianchuan_password", fields)
        self.assertIn("qianchuan_token", fields)
        self.assertIn("qianchuan_channel_id", fields)
        self.assertIn("qianchuan_phone_num", fields)
        self.assertIn("qianchuan_operator", fields)
        self.assertIn("qianchuan_scope", fields)
        for key in ("qianchuan_channel_id", "qianchuan_phone_num", "qianchuan_operator", "qianchuan_scope"):
            self.assertEqual(fields[key].get("category"), "task")

    def test_builtin_phone_definitions_include_qianchuan(self):
        definitions = {
            str(item.get("provider_key")): item
            for item in list_builtin_provider_definitions("phone")
        }

        self.assertIn("qianchuan", definitions)
        self.assertEqual(definitions["qianchuan"]["driver_type"], "qianchuan_sms_api")
        self.assertEqual(definitions["qianchuan"]["label"], "千川")

    def test_config_options_include_phone_catalog_and_settings(self):
        from application.config import ConfigService

        class _Repository:
            def get_flat(self):
                return {}

        class _Definitions:
            def list_definitions(self, provider_type, enabled_only=False):
                return [{"value": "haozhu", "label": "豪猪", "fields": []}] if provider_type == "phone" else []

            def list_driver_templates(self, provider_type):
                return [{"driver_type": "haozhu_sms_api", "fields": []}] if provider_type == "phone" else []

        class _Settings:
            def get_captcha_policy(self):
                return {}

            def list_settings(self, provider_type):
                return [{"provider_key": "haozhu"}] if provider_type == "phone" else []

        service = ConfigService(repository=_Repository())
        service.provider_definitions = _Definitions()
        service.provider_settings = _Settings()

        options = service.get_options()

        self.assertEqual(options["phone_providers"][0]["value"], "haozhu")
        self.assertEqual(options["phone_drivers"][0]["driver_type"], "haozhu_sms_api")
        self.assertEqual(options["phone_settings"][0]["provider_key"], "haozhu")


class HaozhuPhoneProviderRuntimeTests(unittest.TestCase):
    def test_create_phone_provider_uses_definition_and_settings(self):
        from core.base_phone import PHONE_FACTORY_REGISTRY, create_phone_provider

        captured = {}

        def fake_factory(extra, proxy):
            captured["extra"] = dict(extra)
            captured["proxy"] = proxy
            return object()

        definition_repo = Mock()
        definition_repo.get_by_key.return_value = SimpleNamespace(
            driver_type="haozhu_sms_api",
            get_fields=lambda: [
                {"key": "haozhu_api_base_url", "category": "connection"},
                {"key": "haozhu_username", "category": "auth"},
                {"key": "haozhu_project_id", "category": "task"},
            ],
        )
        settings_repo = Mock()
        settings_repo.resolve_runtime_settings.return_value = {
            "haozhu_api_base_url": "https://api.haozhuma.com",
            "haozhu_username": "demo-user",
            "haozhu_project_id": "stored-project-should-not-be-used",
        }
        settings_repo.get_by_key.return_value = SimpleNamespace(auth_mode="username_password")

        with patch.dict(PHONE_FACTORY_REGISTRY, {"haozhu_sms_api": fake_factory}, clear=False):
            with patch("core.base_phone._get_provider_definitions_repository", return_value=definition_repo), patch(
                "core.base_phone._get_provider_settings_repository", return_value=settings_repo
            ):
                create_phone_provider(
                    "haozhu",
                    {"haozhu_uid": "u1", "haozhu_project_id": "task-project-1000"},
                    proxy="http://127.0.0.1:8080",
                )

        self.assertEqual(captured["extra"]["haozhu_username"], "demo-user")
        self.assertEqual(captured["extra"]["haozhu_project_id"], "task-project-1000")
        self.assertEqual(captured["extra"]["haozhu_uid"], "u1")
        self.assertEqual(captured["extra"]["phone_auth_mode"], "username_password")
        self.assertEqual(captured["extra"]["haozhu_auth_mode"], "username_password")
        self.assertEqual(captured["proxy"], "http://127.0.0.1:8080")

    @patch("requests.Session")
    def test_haozhu_get_phone_logs_in_and_returns_phone_number(self, session_cls):
        from core.base_phone import HaozhuPhoneProvider

        session = session_cls.return_value
        session.get.side_effect = [
            _Response({"code": 0, "token": "token-123", "msg": "success"}),
            _Response({"code": "0", "sid": "1000", "phone": "13800138000", "country_qu": "+86", "sp": "移动", "phone_gsd": "广东"}),
        ]
        provider = HaozhuPhoneProvider(
            api_base_url="https://api.haozhuma.com",
            username="demo-user",
            password="demo-pass",
            project_id="1000",
        )

        account = provider.get_phone()

        self.assertEqual(account.phone, "13800138000")
        self.assertEqual(account.project_id, "1000")
        self.assertEqual(account.token, "token-123")
        first_params = session.get.call_args_list[0].kwargs["params"]
        second_params = session.get.call_args_list[1].kwargs["params"]
        self.assertEqual(first_params["api"], "login")
        self.assertEqual(second_params["api"], "getPhone")
        self.assertEqual(second_params["sid"], "1000")

    @patch("time.sleep", return_value=None)
    @patch("requests.Session")
    def test_haozhu_wait_for_code_polls_until_yzm_available(self, session_cls, _sleep):
        from core.base_phone import HaozhuPhoneProvider, PhoneAccount

        session = session_cls.return_value
        session.get.side_effect = [
            _Response({"code": "-1", "msg": "等待验证码"}),
            _Response({"code": "0", "msg": "成功", "sms": "您的验证码为 654321", "yzm": "654321"}),
        ]
        provider = HaozhuPhoneProvider(api_base_url="https://api.haozhuma.com", token="token-123", project_id="1000")
        account = PhoneAccount(phone="13800138000", project_id="1000", token="token-123")

        code = provider.wait_for_code(account, timeout=20, poll_interval=1)

        self.assertEqual(code, "654321")
        self.assertEqual(session.get.call_count, 2)
        params = session.get.call_args_list[-1].kwargs["params"]
        self.assertEqual(params["api"], "getMessage")
        self.assertEqual(params["phone"], "13800138000")

    @patch("requests.Session")
    def test_haozhu_blacklist_and_release_use_documented_api_names(self, session_cls):
        from core.base_phone import HaozhuPhoneProvider, PhoneAccount

        session = session_cls.return_value
        session.get.side_effect = [
            _Response({"code": "0", "msg": "释放成功"}),
            _Response({"code": "0", "msg": "success"}),
        ]
        provider = HaozhuPhoneProvider(api_base_url="https://api.haozhuma.com", token="token-123", project_id="1000")
        account = PhoneAccount(phone="13800138000", project_id="1000", token="token-123")

        self.assertTrue(provider.release_phone(account))
        self.assertTrue(provider.blacklist_phone(account))
        self.assertEqual(session.get.call_args_list[0].kwargs["params"]["api"], "cancelRecv")
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["api"], "addBlacklist")


class QianchuanPhoneProviderRuntimeTests(unittest.TestCase):
    def test_create_phone_provider_filters_stored_task_fields_and_uses_task_overrides(self):
        from core.base_phone import PHONE_FACTORY_REGISTRY, create_phone_provider

        captured = {}

        def fake_factory(extra, proxy):
            captured["extra"] = dict(extra)
            return object()

        definition_repo = Mock()
        definition_repo.get_by_key.return_value = SimpleNamespace(
            driver_type="qianchuan_sms_api",
            get_fields=lambda: [
                {"key": "qianchuan_api_base_url", "category": "connection"},
                {"key": "qianchuan_username", "category": "auth"},
                {"key": "qianchuan_channel_id", "category": "task"},
                {"key": "qianchuan_phone_num", "category": "task"},
                {"key": "qianchuan_operator", "category": "task"},
                {"key": "qianchuan_scope", "category": "task"},
            ],
        )
        settings_repo = Mock()
        settings_repo.resolve_runtime_settings.return_value = {
            "qianchuan_api_base_url": "https://api.qc86.shop/api",
            "qianchuan_username": "demo-user",
            "qianchuan_channel_id": "stored-channel-should-not-be-used",
            "qianchuan_phone_num": "stored-phone-should-not-be-used",
            "qianchuan_operator": "stored-operator-should-not-be-used",
            "qianchuan_scope": "stored-scope-should-not-be-used",
        }
        settings_repo.get_by_key.return_value = SimpleNamespace(auth_mode="username_password")

        with patch.dict(PHONE_FACTORY_REGISTRY, {"qianchuan_sms_api": fake_factory}, clear=False):
            with patch("core.base_phone._get_provider_definitions_repository", return_value=definition_repo), patch(
                "core.base_phone._get_provider_settings_repository", return_value=settings_repo
            ):
                create_phone_provider(
                    "qianchuan",
                    {
                        "qianchuan_channel_id": "task-channel-123",
                        "qianchuan_phone_num": "15600000000",
                        "qianchuan_operator": "4",
                        "qianchuan_scope": "广东",
                    },
                )

        self.assertEqual(captured["extra"]["qianchuan_username"], "demo-user")
        self.assertEqual(captured["extra"]["qianchuan_channel_id"], "task-channel-123")
        self.assertEqual(captured["extra"]["qianchuan_phone_num"], "15600000000")
        self.assertEqual(captured["extra"]["qianchuan_operator"], "4")
        self.assertEqual(captured["extra"]["qianchuan_scope"], "广东")

    @patch("requests.Session")
    def test_qianchuan_get_phone_logs_in_and_returns_phone_number(self, session_cls):
        from core.base_phone import QianchuanPhoneProvider

        session = session_cls.return_value
        session.get.side_effect = [
            _Response({"status": 200, "success": True, "data": {"token": "qc-token"}, "msg": "操作成功"}),
            _Response({
                "status": 200,
                "success": True,
                "data": {
                    "mobile": "15664864435",
                    "refreshTime": 5000,
                    "smsTask": {"id": 1234789, "phoneNo": "15664864435", "projectId": 89371, "status": 0, "uid": 4064},
                },
                "msg": "操作成功",
            }),
        ]
        provider = QianchuanPhoneProvider(
            api_base_url="https://api.qc86.shop/api",
            username="demo-user",
            password="demo-pass",
            channel_id="1237436366606831616",
            operator="4",
            scope="广东",
        )

        account = provider.get_phone()

        self.assertEqual(account.phone, "15664864435")
        self.assertEqual(account.project_id, "1237436366606831616")
        self.assertEqual(account.token, "qc-token")
        first_call = session.get.call_args_list[0]
        second_call = session.get.call_args_list[1]
        self.assertTrue(first_call.args[0].endswith("/login"))
        self.assertEqual(first_call.kwargs["params"]["username"], "demo-user")
        self.assertEqual(first_call.kwargs["params"]["password"], "demo-pass")
        self.assertTrue(second_call.args[0].endswith("/getPhone"))
        self.assertEqual(second_call.kwargs["params"]["channelId"], "1237436366606831616")
        self.assertEqual(second_call.kwargs["params"]["operator"], "4")
        self.assertEqual(second_call.kwargs["params"]["scope"], "广东")

    @patch("time.sleep", return_value=None)
    @patch("requests.Session")
    def test_qianchuan_wait_for_code_polls_until_code_available(self, session_cls, _sleep):
        from core.base_phone import PhoneAccount, QianchuanPhoneProvider

        session = session_cls.return_value
        session.get.side_effect = [
            _Response({"status": 200, "success": True, "data": {"code": "", "message": "waiting"}, "msg": "操作成功"}),
            _Response({"status": 200, "success": True, "data": {"code": "893134", "modle": "共享数据：893134"}, "msg": "操作成功"}),
        ]
        provider = QianchuanPhoneProvider(api_base_url="https://api.qc86.shop/api", token="qc-token", channel_id="123")
        account = PhoneAccount(phone="15664864435", project_id="123", token="qc-token")

        code = provider.wait_for_code(account, timeout=20, poll_interval=1)

        self.assertEqual(code, "893134")
        self.assertEqual(session.get.call_count, 2)
        params = session.get.call_args_list[-1].kwargs["params"]
        self.assertEqual(params["channelId"], "123")
        self.assertEqual(params["phoneNum"], "15664864435")

    @patch("requests.Session")
    def test_qianchuan_blacklist_and_release_use_documented_endpoints(self, session_cls):
        from core.base_phone import PhoneAccount, QianchuanPhoneProvider

        session = session_cls.return_value
        session.get.side_effect = [
            _Response({"status": 200, "success": True, "msg": "操作成功"}),
            _Response({"status": 200, "success": True, "msg": "操作成功"}),
        ]
        provider = QianchuanPhoneProvider(api_base_url="https://api.qc86.shop/api", token="qc-token", channel_id="123")
        account = PhoneAccount(phone="15664864435", project_id="123", token="qc-token")

        self.assertTrue(provider.release_phone(account))
        self.assertTrue(provider.blacklist_phone(account))

        release_call = session.get.call_args_list[0]
        blacklist_call = session.get.call_args_list[1]
        self.assertTrue(release_call.args[0].endswith("/release"))
        self.assertEqual(release_call.kwargs["params"]["status"], 2)
        self.assertTrue(blacklist_call.args[0].endswith("/phoneCollectAdd"))
        self.assertEqual(blacklist_call.kwargs["params"]["type"], 0)


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


if __name__ == "__main__":
    unittest.main()
