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
        self.assertIn("haozhu_isp", fields)
        self.assertIn("haozhu_province", fields)
        self.assertIn("haozhu_ascription", fields)
        self.assertIn("haozhu_paragraph", fields)
        self.assertIn("haozhu_exclude", fields)
        self.assertIn("phone_number", fields)
        self.assertIn("phone_segment", fields)
        self.assertIn("phone_filter_attempts", fields)
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
        self.assertIn("phone_number", fields)
        self.assertIn("phone_segment", fields)
        self.assertIn("phone_filter_attempts", fields)
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

    @patch("requests.Session")
    def test_haozhu_get_phone_uses_documented_filter_params(self, session_cls):
        from core.base_phone import HaozhuPhoneProvider

        session = session_cls.return_value
        session.get.return_value = _Response({"code": "0", "sid": "1000", "phone": "16200138000"})
        provider = HaozhuPhoneProvider(
            api_base_url="https://api.haozhuma.com",
            token="token-123",
            project_id="1000",
            isp="1",
            province="44",
            ascription="2",
            paragraph="162",
            exclude="170",
        )

        account = provider.get_phone()

        self.assertEqual(account.phone, "16200138000")
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["api"], "getPhone")
        self.assertEqual(params["sid"], "1000")
        self.assertEqual(params["isp"], "1")
        self.assertEqual(params["Province"], "44")
        self.assertEqual(params["ascription"], "2")
        self.assertEqual(params["paragraph"], "162")
        self.assertEqual(params["exclude"], "170")
        self.assertNotIn("phone", params)

    def test_filtered_phone_provider_releases_unmatched_segment_and_retries(self):
        from core.base_phone import FilteredPhoneProvider, PhoneAccount

        class FakeProvider:
            def __init__(self):
                self.phones = ["13800138000", "16200138000"]
                self.released = []

            def get_phone(self):
                return PhoneAccount(phone=self.phones.pop(0), project_id="p1", token="t1")

            def wait_for_code(self, account, timeout=180, poll_interval=15, code_pattern=None):
                return "123456"

            def release_phone(self, account):
                self.released.append(account.phone)
                return True

            def blacklist_phone(self, account):
                return True

        provider = FakeProvider()
        wrapped = FilteredPhoneProvider(provider, exact_numbers=[], segments=["162"], attempts=2)

        account = wrapped.get_phone()

        self.assertEqual(account.phone, "16200138000")
        self.assertEqual(provider.released, ["13800138000"])

    def test_filtered_phone_provider_accepts_country_code_exact_number(self):
        from core.base_phone import FilteredPhoneProvider, PhoneAccount

        class FakeProvider:
            def get_phone(self):
                return PhoneAccount(phone="15600000000", project_id="p1", token="t1")

            def wait_for_code(self, account, timeout=180, poll_interval=15, code_pattern=None):
                return "123456"

            def release_phone(self, account):
                return True

            def blacklist_phone(self, account):
                return True

        wrapped = FilteredPhoneProvider(
            FakeProvider(),
            exact_numbers=["+86 15600000000"],
            segments=[],
            attempts=1,
        )

        account = wrapped.get_phone()

        self.assertEqual(account.phone, "15600000000")

    @patch("requests.Session")
    def test_haozhu_single_generic_segment_maps_to_documented_paragraph(self, _session_cls):
        from core.base_phone import _create_haozhu

        provider = _create_haozhu({"phone_segment": "162"}, proxy=None)

        self.assertEqual(provider.paragraph, "162")

    @patch("requests.Session")
    def test_haozhu_multiple_generic_segments_use_filter_without_paragraph(self, _session_cls):
        from core.base_phone import _create_haozhu

        provider = _create_haozhu({"phone_segment": "162,165"}, proxy=None)

        self.assertEqual(provider.paragraph, "")

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
    def test_qianchuan_generic_phone_number_maps_to_native_phone_num(self, _session_cls):
        from core.base_phone import _create_qianchuan

        provider = _create_qianchuan({"phone_number": "+86 15600000000"}, proxy=None)

        self.assertEqual(provider.phone_num, "15600000000")

    def test_create_phone_provider_wraps_generic_segment_filter(self):
        from core.base_phone import FilteredPhoneProvider, PHONE_FACTORY_REGISTRY, create_phone_provider

        class FakeProvider:
            pass

        definition_repo = Mock()
        definition_repo.get_by_key.return_value = SimpleNamespace(
            driver_type="qianchuan_sms_api",
            get_fields=lambda: [
                {"key": "phone_segment", "category": "task"},
                {"key": "phone_filter_attempts", "category": "task"},
            ],
        )
        settings_repo = Mock()
        settings_repo.resolve_runtime_settings.return_value = {}
        settings_repo.get_by_key.return_value = SimpleNamespace(auth_mode="")

        with patch.dict(PHONE_FACTORY_REGISTRY, {"qianchuan_sms_api": lambda extra, proxy: FakeProvider()}, clear=False):
            with patch("core.base_phone._get_provider_definitions_repository", return_value=definition_repo), patch(
                "core.base_phone._get_provider_settings_repository", return_value=settings_repo
            ):
                provider = create_phone_provider(
                    "qianchuan",
                    {"phone_segment": "162", "phone_filter_attempts": "3"},
                )

        self.assertIsInstance(provider, FilteredPhoneProvider)
        self.assertEqual(provider._segments, ["162"])
        self.assertEqual(provider._attempts, 3)

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


class FiveSimPhoneProviderCatalogTests(unittest.TestCase):
    def test_5sim_driver_template_exposes_required_fields(self):
        template = get_driver_template("phone", "5sim_api")

        self.assertIsNotNone(template)
        self.assertEqual(template["provider_type"], "phone")
        self.assertEqual(template["default_auth_mode"], "api_key")
        fields = {str(item.get("key")): item for item in template.get("fields", [])}
        self.assertIn("5sim_api_base_url", fields)
        self.assertIn("5sim_api_token", fields)
        self.assertIn("5sim_country", fields)
        self.assertIn("5sim_operator", fields)
        self.assertIn("5sim_product", fields)
        self.assertIn("phone_number", fields)
        self.assertIn("phone_segment", fields)
        self.assertIn("phone_filter_attempts", fields)
        self.assertEqual(fields["5sim_product"].get("category"), "task")

    def test_builtin_phone_definitions_include_5sim(self):
        definitions = {
            str(item.get("provider_key")): item
            for item in list_builtin_provider_definitions("phone")
        }

        self.assertIn("5sim", definitions)
        self.assertEqual(definitions["5sim"]["driver_type"], "5sim_api")
        self.assertEqual(definitions["5sim"]["label"], "5sim")


class FiveSimPhoneProviderRuntimeTests(unittest.TestCase):
    def test_create_phone_provider_uses_5sim_definition_and_task_overrides(self):
        from core.base_phone import PHONE_FACTORY_REGISTRY, create_phone_provider

        captured = {}

        def fake_factory(extra, proxy):
            captured["extra"] = dict(extra)
            captured["proxy"] = proxy
            return object()

        definition_repo = Mock()
        definition_repo.get_by_key.return_value = SimpleNamespace(
            driver_type="5sim_api",
            get_fields=lambda: [
                {"key": "5sim_api_base_url", "category": "connection"},
                {"key": "5sim_api_token", "category": "auth"},
                {"key": "5sim_country", "category": "task"},
                {"key": "5sim_operator", "category": "task"},
                {"key": "5sim_product", "category": "task"},
            ],
        )
        settings_repo = Mock()
        settings_repo.resolve_runtime_settings.return_value = {
            "5sim_api_base_url": "https://5sim.net",
            "5sim_api_token": "token-from-settings",
            "5sim_country": "stored-country-should-not-be-used",
            "5sim_operator": "stored-operator-should-not-be-used",
            "5sim_product": "stored-product-should-not-be-used",
        }
        settings_repo.get_by_key.return_value = SimpleNamespace(auth_mode="api_key")

        with patch.dict(PHONE_FACTORY_REGISTRY, {"5sim_api": fake_factory}, clear=False):
            with patch("core.base_phone._get_provider_definitions_repository", return_value=definition_repo), patch(
                "core.base_phone._get_provider_settings_repository", return_value=settings_repo
            ):
                create_phone_provider(
                    "5sim",
                    {
                        "5sim_country": "china",
                        "5sim_operator": "any",
                        "5sim_product": "openai",
                    },
                    proxy="http://127.0.0.1:8080",
                )

        self.assertEqual(captured["extra"]["5sim_api_token"], "token-from-settings")
        self.assertEqual(captured["extra"]["5sim_country"], "china")
        self.assertEqual(captured["extra"]["5sim_operator"], "any")
        self.assertEqual(captured["extra"]["5sim_product"], "openai")
        self.assertEqual(captured["extra"]["phone_auth_mode"], "api_key")
        self.assertEqual(captured["extra"]["5sim_auth_mode"], "api_key")
        self.assertEqual(captured["proxy"], "http://127.0.0.1:8080")

    @patch("requests.Session")
    def test_5sim_get_phone_buys_activation_and_returns_phone_number(self, session_cls):
        from core.base_phone import FiveSimPhoneProvider

        session = session_cls.return_value
        session.get.return_value = _Response({
            "id": 12345,
            "phone": "+8613800138000",
            "operator": "any",
            "product": "openai",
            "country": "china",
            "price": 10,
            "status": "PENDING",
        })
        provider = FiveSimPhoneProvider(
            api_base_url="https://5sim.net",
            api_token="sim-token",
            country="china",
            operator="any",
            product="openai",
            max_price="20",
        )

        account = provider.get_phone()

        self.assertEqual(account.phone, "+8613800138000")
        self.assertEqual(account.project_id, "openai")
        self.assertEqual(account.provider_name, "5sim")
        self.assertEqual(account.extra["metadata"]["order_id"], "12345")
        call = session.get.call_args
        self.assertTrue(call.args[0].endswith("/v1/user/buy/activation/china/any/openai"))
        self.assertEqual(call.kwargs["params"]["maxPrice"], "20")
        self.assertEqual(session.headers.update.call_args_list[-1].args[0]["Authorization"], "Bearer sim-token")

    @patch("time.sleep", return_value=None)
    @patch("requests.Session")
    def test_5sim_wait_for_code_polls_until_sms_code_available(self, session_cls, _sleep):
        from core.base_phone import FiveSimPhoneProvider, PhoneAccount

        session = session_cls.return_value
        session.get.side_effect = [
            _Response({"id": 12345, "status": "PENDING", "sms": []}),
            _Response({"id": 12345, "status": "RECEIVED", "sms": [{"code": "654321", "text": "Code 654321"}]}),
        ]
        provider = FiveSimPhoneProvider(api_base_url="https://5sim.net", api_token="sim-token", product="openai")
        account = PhoneAccount(phone="+8613800138000", project_id="openai", token="sim-token", extra={"metadata": {"order_id": "12345"}})

        code = provider.wait_for_code(account, timeout=20, poll_interval=1)

        self.assertEqual(code, "654321")
        self.assertEqual(session.get.call_count, 2)
        self.assertTrue(session.get.call_args_list[-1].args[0].endswith("/v1/user/check/12345"))

    @patch("requests.Session")
    def test_5sim_blacklist_and_release_use_order_actions(self, session_cls):
        from core.base_phone import FiveSimPhoneProvider, PhoneAccount

        session = session_cls.return_value
        session.get.side_effect = [
            _Response({"id": 12345, "status": "CANCELED"}),
            _Response({"id": 12345, "status": "BANNED"}),
        ]
        provider = FiveSimPhoneProvider(api_base_url="https://5sim.net", api_token="sim-token", product="openai")
        account = PhoneAccount(phone="+8613800138000", project_id="openai", token="sim-token", extra={"metadata": {"order_id": "12345"}})

        self.assertTrue(provider.release_phone(account))
        self.assertTrue(provider.blacklist_phone(account))
        self.assertTrue(session.get.call_args_list[0].args[0].endswith("/v1/user/cancel/12345"))
        self.assertTrue(session.get.call_args_list[1].args[0].endswith("/v1/user/ban/12345"))


class ApiccPhoneProviderCatalogTests(unittest.TestCase):
    def test_apicc_driver_template_exposes_required_fields(self):
        template = get_driver_template("phone", "apicc_sms_api")

        self.assertIsNotNone(template)
        self.assertEqual(template["provider_type"], "phone")
        self.assertEqual(template["default_auth_mode"], "public_free")
        fields = {str(item.get("key")): item for item in template.get("fields", [])}
        self.assertIn("apicc_api_base_url", fields)
        self.assertIn("apicc_phone_number", fields)
        self.assertIn("apicc_country_code", fields)
        self.assertIn("apicc_sender", fields)
        self.assertIn("apicc_poll_interval", fields)
        self.assertIn("apicc_phone_timeout", fields)
        self.assertEqual(fields["apicc_phone_number"].get("category"), "task")
        # 号码本身是来源（非随机取号后过滤），不应混入通用号段过滤字段以免误包 FilteredPhoneProvider
        self.assertNotIn("phone_segment", fields)

    def test_builtin_phone_definitions_include_apicc(self):
        definitions = {
            str(item.get("provider_key")): item
            for item in list_builtin_provider_definitions("phone")
        }

        self.assertIn("apicc", definitions)
        self.assertEqual(definitions["apicc"]["driver_type"], "apicc_sms_api")
        self.assertEqual(definitions["apicc"]["label"], "api.cc 免费接码")


class ApiccPhoneProviderRuntimeTests(unittest.TestCase):
    @patch("requests.Session")
    def test_create_apicc_reads_number_sender_country(self, _session_cls):
        from core.base_phone import _create_apicc

        provider = _create_apicc(
            {"apicc_phone_number": "+1 (819) 481-6943", "apicc_sender": "732873", "apicc_country_code": "+1"},
            proxy=None,
        )

        self.assertEqual(provider.phone_number, "18194816943")
        self.assertEqual(provider.senders, ["732873"])
        self.assertEqual(provider.country_code, "+1")

    @patch("requests.Session")
    def test_create_apicc_supports_generic_phone_number_and_multi_sender(self, _session_cls):
        from core.base_phone import _create_apicc

        provider = _create_apicc(
            {"phone_number": "18194816943", "apicc_sender": "732873, WorkOS"},
            proxy=None,
        )

        self.assertEqual(provider.phone_number, "18194816943")
        self.assertEqual(provider.senders, ["732873", "WorkOS"])

    @patch("requests.Session")
    def test_apicc_get_phone_sets_baseline_to_latest_matching_id(self, session_cls):
        from core.base_phone import ApiccPhoneProvider

        session = session_cls.return_value
        session.get.return_value = _Response({"data": [
            {"id": 164397850, "to": "18194816943", "from": "732873", "msg": "old code 111111"},
            {"id": 164397800, "to": "18194816943", "from": "999", "msg": "older 222222"},
            {"id": 164397999, "to": "19998887777", "from": "732873", "msg": "another number 333333"},
        ]})
        provider = ApiccPhoneProvider(phone_number="18194816943", country_code="+1")

        account = provider.get_phone()

        self.assertEqual(account.phone, "18194816943")
        self.assertEqual(account.project_id, "apicc_free")
        self.assertEqual(account.provider_name, "apicc")
        # 基线 = 该号当前最大 id（忽略发往别的号的短信）
        self.assertEqual(account.extra["apicc_baseline_id"], 164397850)

    @patch("time.sleep", return_value=None)
    @patch("requests.Session")
    def test_apicc_wait_for_code_returns_new_sms_from_target_sender(self, session_cls, _sleep):
        from core.base_phone import ApiccPhoneProvider, PhoneAccount

        session = session_cls.return_value
        session.get.return_value = _Response({"data": [
            {"id": 200, "to": "18194816943", "from": "111111", "msg": "999999 unrelated service code"},
            {"id": 199, "to": "18194816943", "from": "732873", "msg": "005429 is your verification code. Do not share it."},
            {"id": 100, "to": "18194816943", "from": "732873", "msg": "485873 old code below baseline"},
        ]})
        provider = ApiccPhoneProvider(phone_number="18194816943", sender="732873")
        account = PhoneAccount(phone="18194816943", project_id="apicc_free", extra={"apicc_baseline_id": 150})

        code = provider.wait_for_code(account, timeout=20, poll_interval=1)

        # 发送方过滤排除 111111(999999)，命中 732873 的新短信；id=100 在基线下被忽略
        self.assertEqual(code, "005429")

    @patch("time.sleep", return_value=None)
    @patch("requests.Session")
    def test_apicc_wait_for_code_times_out_when_only_old_sms(self, session_cls, _sleep):
        from core.base_phone import ApiccPhoneProvider, PhoneAccount

        session = session_cls.return_value
        session.get.return_value = _Response({"data": [
            {"id": 150, "to": "18194816943", "from": "732873", "msg": "485873 is your verification code."},
        ]})
        provider = ApiccPhoneProvider(phone_number="18194816943", sender="732873")
        account = PhoneAccount(phone="18194816943", project_id="apicc_free", extra={"apicc_baseline_id": 150})

        times = iter([1000.0, 1000.0, 1002.0])
        with patch("time.time", lambda: next(times, 1002.0)):
            with self.assertRaises(TimeoutError):
                provider.wait_for_code(account, timeout=1, poll_interval=1)

    @patch("time.sleep", return_value=None)
    @patch("requests.Session")
    def test_apicc_wait_for_code_sender_filter_blocks_other_services(self, session_cls, _sleep):
        from core.base_phone import ApiccPhoneProvider, PhoneAccount

        session = session_cls.return_value
        # 同一公共号同时收到别的服务的新短信，但发送方不匹配 → 被过滤，最终超时而非误返回
        session.get.return_value = _Response({"data": [
            {"id": 300, "to": "18194816943", "from": "888888", "msg": "654321 someone else code"},
        ]})
        provider = ApiccPhoneProvider(phone_number="18194816943", sender="732873")
        account = PhoneAccount(phone="18194816943", project_id="apicc_free", extra={"apicc_baseline_id": 150})

        times = iter([1000.0, 1000.0, 1002.0])
        with patch("time.time", lambda: next(times, 1002.0)):
            with self.assertRaises(TimeoutError):
                provider.wait_for_code(account, timeout=1, poll_interval=1)


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
