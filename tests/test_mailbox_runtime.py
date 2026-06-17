import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.base_mailbox import MAILBOX_FACTORY_REGISTRY, MailboxAccount, create_mailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig


class _DummyPlatform(BasePlatform):
    name = "dummy"
    display_name = "Dummy"
    supported_executors = ["protocol"]

    def check_valid(self, account):
        return True


class MailboxRuntimeTests(unittest.TestCase):
    def test_create_mailbox_injects_provider_auth_mode_into_runtime_extra(self):
        captured = {}

        def fake_factory(extra, proxy):
            captured["extra"] = dict(extra)
            captured["proxy"] = proxy
            return object()

        definition_repo = Mock()
        definition_repo.get_by_key.return_value = SimpleNamespace(driver_type="cfworker_admin_api")
        settings_repo = Mock()
        settings_repo.resolve_runtime_settings.return_value = {
            "cfworker_api_url": "https://apimail.example.com"
        }
        settings_repo.get_by_key.return_value = SimpleNamespace(auth_mode="public_jwt")

        with patch.dict(MAILBOX_FACTORY_REGISTRY, {"cfworker_admin_api": fake_factory}, clear=False):
            with patch(
                "core.base_mailbox._get_provider_definitions_repository",
                return_value=definition_repo,
            ), patch(
                "core.base_mailbox._get_provider_settings_repository",
                return_value=settings_repo,
            ), patch(
                "core.base_mailbox.socket.getaddrinfo",
                return_value=[(None, None, None, None, ("93.184.216.34", 443))],
            ):
                create_mailbox(
                    "cfworker",
                    {"cfworker_domain": "example.com"},
                    proxy="http://127.0.0.1:8080",
                )

        self.assertEqual(captured["extra"]["cfworker_auth_mode"], "public_jwt")
        self.assertEqual(captured["extra"]["mailbox_auth_mode"], "public_jwt")
        self.assertEqual(captured["extra"]["cfworker_domain"], "example.com")
        self.assertEqual(captured["proxy"], "http://127.0.0.1:8080")

    def test_create_mailbox_auto_bypasses_proxy_for_internal_api_host(self):
        captured = {}

        def fake_factory(extra, proxy):
            captured["extra"] = dict(extra)
            captured["proxy"] = proxy
            return object()

        definition_repo = Mock()
        definition_repo.get_by_key.return_value = SimpleNamespace(driver_type="cfworker_admin_api")
        settings_repo = Mock()
        settings_repo.resolve_runtime_settings.return_value = {
            "cfworker_api_url": "https://apimail.bufan.de5.net"
        }
        settings_repo.get_by_key.return_value = SimpleNamespace(auth_mode="admin_token")

        with patch.dict(MAILBOX_FACTORY_REGISTRY, {"cfworker_admin_api": fake_factory}, clear=False):
            with patch(
                "core.base_mailbox._get_provider_definitions_repository",
                return_value=definition_repo,
            ), patch(
                "core.base_mailbox._get_provider_settings_repository",
                return_value=settings_repo,
            ), patch(
                "core.base_mailbox.socket.getaddrinfo",
                return_value=[(None, None, None, None, ("198.18.0.13", 443))],
            ):
                create_mailbox(
                    "cfworker",
                    {},
                    proxy="socks5://demo:secret@31.59.20.176:6754",
                )

        self.assertIsNone(captured["proxy"])

    def test_create_mailbox_can_force_proxy_even_for_internal_api_host(self):
        captured = {}

        def fake_factory(extra, proxy):
            captured["extra"] = dict(extra)
            captured["proxy"] = proxy
            return object()

        definition_repo = Mock()
        definition_repo.get_by_key.return_value = SimpleNamespace(driver_type="cfworker_admin_api")
        settings_repo = Mock()
        settings_repo.resolve_runtime_settings.return_value = {
            "cfworker_api_url": "https://apimail.bufan.de5.net",
            "mailbox_proxy_mode": "inherit",
        }
        settings_repo.get_by_key.return_value = SimpleNamespace(auth_mode="admin_token")

        with patch.dict(MAILBOX_FACTORY_REGISTRY, {"cfworker_admin_api": fake_factory}, clear=False):
            with patch(
                "core.base_mailbox._get_provider_definitions_repository",
                return_value=definition_repo,
            ), patch(
                "core.base_mailbox._get_provider_settings_repository",
                return_value=settings_repo,
            ), patch(
                "core.base_mailbox.socket.getaddrinfo",
                return_value=[(None, None, None, None, ("198.18.0.13", 443))],
            ):
                create_mailbox(
                    "cfworker",
                    {},
                    proxy="socks5://demo:secret@31.59.20.176:6754",
                )

        self.assertEqual(captured["proxy"], "socks5://demo:secret@31.59.20.176:6754")

    def test_create_mailbox_auto_bypasses_proxy_for_outlook_token_provider(self):
        captured = {}

        def fake_factory(extra, proxy):
            captured["extra"] = dict(extra)
            captured["proxy"] = proxy
            return object()

        definition_repo = Mock()
        definition_repo.get_by_key.return_value = SimpleNamespace(driver_type="outlook_token_imap")
        settings_repo = Mock()
        settings_repo.resolve_runtime_settings.return_value = {
            "outlook_email": "demo@outlook.com",
            "outlook_client_id": "client-123",
            "outlook_refresh_token": "refresh-123",
        }
        settings_repo.get_by_key.return_value = SimpleNamespace(auth_mode="")

        with patch.dict(MAILBOX_FACTORY_REGISTRY, {"outlook_token_imap": fake_factory}, clear=False):
            with patch(
                "core.base_mailbox._get_provider_definitions_repository",
                return_value=definition_repo,
            ), patch(
                "core.base_mailbox._get_provider_settings_repository",
                return_value=settings_repo,
            ):
                create_mailbox(
                    "outlook_token",
                    {},
                    proxy="socks5://demo:secret@31.59.20.176:6754",
                )

        self.assertIsNone(captured["proxy"])


    def test_yyds_mail_mailbox_can_disable_environment_proxy_and_keep_direct_mode(self):
        from core.base_mailbox import YydsMailMailbox

        mailbox = YydsMailMailbox(api_base_url="https://maliapi.example.com", api_key="demo-key", proxy=None)
        self.assertFalse(getattr(mailbox._session, "trust_env", True))
        self.assertEqual(getattr(mailbox._session, "proxies", {}), {})

    def test_attach_identity_metadata_merges_mailbox_credentials_without_overwrite(self):
        platform = _DummyPlatform(RegisterConfig(extra={"mail_provider": "cfworker"}))
        account = Account(
            platform="dummy",
            email="user@example.com",
            password="pw",
            status=AccountStatus.REGISTERED,
            extra={
                "verification_mailbox": {
                    "from_platform": "keep-me",
                    "provider": "stale-provider",
                    "email": "stale@example.com",
                    "account_id": "stale-account",
                    "api_url": "https://existing.example.com",
                    "auth_mode": "legacy_mode",
                    "mailbox_jwt": "legacy-jwt",
                }
            },
        )
        identity = SimpleNamespace(
            email="user@example.com",
            metadata={},
            oauth_provider="",
            chrome_user_data_dir="",
            chrome_cdp_url="",
            mailbox_account=MailboxAccount(
                email="user@example.com",
                account_id="mailbox-id",
                extra={
                    "provider_account": {
                        "credentials": {
                            "mailbox_jwt": "jwt-123",
                            "address_password": "pass-456",
                        }
                    },
                    "provider_resource": {
                        "metadata": {
                            "address_id": "addr-789",
                            "api_url": "https://apimail.example.com",
                            "auth_mode": "public_jwt",
                        }
                    },
                },
            ),
        )

        merged = platform._attach_identity_metadata(account, identity)
        mailbox = merged.extra["verification_mailbox"]

        self.assertEqual(mailbox["from_platform"], "keep-me")
        self.assertEqual(mailbox["provider"], "cfworker")
        self.assertEqual(mailbox["email"], "user@example.com")
        self.assertEqual(mailbox["account_id"], "mailbox-id")
        self.assertEqual(mailbox["mailbox_jwt"], "legacy-jwt")
        self.assertEqual(mailbox["address_password"], "pass-456")
        self.assertEqual(mailbox["address_id"], "addr-789")
        self.assertEqual(mailbox["api_url"], "https://existing.example.com")
        self.assertEqual(mailbox["auth_mode"], "legacy_mode")


if __name__ == "__main__":
    unittest.main()
