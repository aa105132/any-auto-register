import json
import sys
import types
import unittest
from datetime import datetime, timezone

repo_module = types.ModuleType("infrastructure.accounts_repository")
repo_module.AccountsRepository = type("AccountsRepository", (), {})
sys.modules.setdefault("infrastructure.accounts_repository", repo_module)

from application.account_exports import AccountExportsService
from domain.accounts import AccountExportSelection, AccountRecord


def platform_credential(key: str, value: str, *, is_primary: bool = False) -> dict:
    return {
        "scope": "platform",
        "provider_name": "codebanana",
        "credential_type": "token",
        "key": key,
        "value": value,
        "is_primary": is_primary,
        "source": "test",
        "metadata": {},
    }


def mailbox_account(*, mailbox_jwt: str = "", address_password: str = "") -> dict:
    return {
        "provider_type": "mailbox",
        "provider_name": "cfworker",
        "login_identifier": "demo@example.com",
        "display_name": "demo@example.com",
        "credentials": {
            "mailbox_jwt": mailbox_jwt,
            "address_password": address_password,
        },
        "metadata": {
            "address_id": "127961",
            "api_url": "https://apimail.example.com",
            "auth_mode": "public_jwt",
        },
    }


def mailbox_resource(*, address_id: str = "", api_url: str = "", auth_mode: str = "") -> dict:
    return {
        "provider_type": "mailbox",
        "provider_name": "cfworker",
        "resource_type": "mailbox",
        "resource_identifier": address_id,
        "handle": "demo@example.com",
        "display_name": "demo@example.com",
        "metadata": {
            "address_id": address_id,
            "api_url": api_url,
            "auth_mode": auth_mode,
            "email": "demo@example.com",
        },
    }


class StubRepository:
    def __init__(self, items: list[AccountRecord]):
        self.items = items
        self.last_selection: AccountExportSelection | None = None

    def select_for_export(self, selection: AccountExportSelection) -> list[AccountRecord]:
        self.last_selection = selection
        return list(self.items)


class AccountExportsServiceTests(unittest.TestCase):
    def setUp(self):
        self.item = AccountRecord(
            id=1,
            platform="codebanana",
            email="demo@example.com",
            password="secret",
            user_id="uuid-1",
            display_status="registered",
            credentials=[
                platform_credential("session_token", "session-1", is_primary=True),
                platform_credential("jwtToken", "jwt-1"),
                platform_credential("csrf_token", "csrf-1"),
                platform_credential("cbbot_key", "uuid-1"),
                platform_credential("cookies", '{"__Secure-next-auth.session-token":"session-1"}'),
            ],
            provider_accounts=[
                mailbox_account(mailbox_jwt="mailbox-jwt", address_password="addr-pass"),
            ],
            provider_resources=[
                mailbox_resource(
                    address_id="127961",
                    api_url="https://apimail.example.com",
                    auth_mode="public_jwt",
                )
            ],
            created_at=datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 4, 17, 11, 0, tzinfo=timezone.utc),
        )
        self.repository = StubRepository([self.item])
        self.service = AccountExportsService(self.repository)

    def test_export_codebanana_json_uses_requested_fields(self):
        artifact = self.service.export_json(
            AccountExportSelection(
                platform="codebanana",
                select_all=True,
                field_keys=["email", "cbbot_key", "session_token", "mailbox_jwt", "address_id"],
            )
        )

        payload = json.loads(artifact.content)
        self.assertEqual(
            payload,
            [
                {
                    "email": "demo@example.com",
                    "cbbot_key": "uuid-1",
                    "session_token": "session-1",
                    "mailbox_jwt": "mailbox-jwt",
                    "address_id": "127961",
                }
            ],
        )

    def test_export_codebanana_txt_uses_requested_field_order(self):
        artifact = self.service.export_txt(
            AccountExportSelection(
                platform="codebanana",
                select_all=True,
                field_keys=["email", "password", "cbbot_key", "session_token"],
            )
        )

        self.assertEqual(
            artifact.content.splitlines(),
            ["demo@example.com----secret----uuid-1----session-1"],
        )

    def test_export_codebanana_json_uses_null_for_missing_values(self):
        item = AccountRecord(
            id=2,
            platform="codebanana",
            email="missing@example.com",
            password="secret",
        )
        service = AccountExportsService(StubRepository([item]))

        artifact = service.export_json(
            AccountExportSelection(
                platform="codebanana",
                select_all=True,
                field_keys=["email", "jwtToken", "mailbox_jwt"],
            )
        )

        self.assertEqual(
            json.loads(artifact.content),
            [
                {
                    "email": "missing@example.com",
                    "jwtToken": None,
                    "mailbox_jwt": None,
                }
            ],
        )

    def test_export_codebanana_rejects_unknown_field(self):
        with self.assertRaisesRegex(ValueError, "unsupported export field"):
            self.service.export_json(
                AccountExportSelection(
                    platform="codebanana",
                    select_all=True,
                    field_keys=["email", "not_exists"],
                )
            )

    def test_export_venice_txt_uses_requested_field_order(self):
        venice_item = AccountRecord(
            id=3,
            platform="venice",
            email="venice@example.com",
            password="Venice!2026",
            user_id="user_venice",
            primary_token="access-token-1",
            overview={"credits": 500, "plan_state": "free"},
            credentials=[
                platform_credential("access_token", "access-token-1", is_primary=True),
                platform_credential("refresh_token", "refresh-token-1"),
                platform_credential("session_token", "session-token-1"),
                platform_credential("client_id", "client_1"),
                platform_credential("api_key", "VENICE_INFERENCE_KEY_demo123"),
                platform_credential("api_key_description", "seedance-auto"),
            ],
        )
        service = AccountExportsService(StubRepository([venice_item]))

        artifact = service.export_txt(
            AccountExportSelection(
                platform="venice",
                select_all=True,
                field_keys=["email", "api_key", "credits"],
            )
        )

        self.assertEqual(
            artifact.content.splitlines(),
            ["venice@example.com----VENICE_INFERENCE_KEY_demo123----500"],
        )

    def test_export_venice_json_supports_api_key_and_token_fields(self):
        venice_item = AccountRecord(
            id=4,
            platform="venice",
            email="venice@example.com",
            password="Venice!2026",
            user_id="user_venice",
            primary_token="access-token-1",
            overview={"credits": 500, "plan_state": "free"},
            credentials=[
                platform_credential("access_token", "access-token-1", is_primary=True),
                platform_credential("refresh_token", "refresh-token-1"),
                platform_credential("api_key", "VENICE_INFERENCE_KEY_demo123"),
            ],
        )
        service = AccountExportsService(StubRepository([venice_item]))

        artifact = service.export_json(
            AccountExportSelection(
                platform="venice",
                select_all=True,
                field_keys=["email", "access_token", "refresh_token", "api_key", "credits"],
            )
        )

        self.assertEqual(
            json.loads(artifact.content),
            [
                {
                    "email": "venice@example.com",
                    "access_token": "access-token-1",
                    "refresh_token": "refresh-token-1",
                    "api_key": "VENICE_INFERENCE_KEY_demo123",
                    "credits": 500,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
