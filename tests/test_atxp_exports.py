import json
import sys
import types
import unittest

repo_module = types.ModuleType("infrastructure.accounts_repository")
repo_module.AccountsRepository = type("AccountsRepository", (), {})
sys.modules.setdefault("infrastructure.accounts_repository", repo_module)

from application.account_exports import AccountExportsService
from domain.accounts import AccountExportSelection, AccountRecord


def platform_credential(key: str, value: str, *, is_primary: bool = False) -> dict:
    return {
        "scope": "platform",
        "provider_name": "atxp",
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


def mailbox_resource(provider_name: str = "cfworker") -> dict:
    return {
        "provider_type": "mailbox",
        "provider_name": provider_name,
        "resource_type": "mailbox",
        "resource_identifier": "demo@example.com",
        "handle": "demo@example.com",
        "display_name": "demo@example.com",
        "metadata": {
            "email": "demo@example.com",
            "address_id": "127961",
            "api_url": "https://apimail.example.com",
            "auth_mode": "public_jwt",
        },
    }


class StubRepository:
    def __init__(self, items: list[AccountRecord]):
        self.items = items
        self.last_selection: AccountExportSelection | None = None

    def select_for_export(self, selection: AccountExportSelection) -> list[AccountRecord]:
        self.last_selection = selection
        return list(self.items)


class AtxpAccountExportsServiceTests(unittest.TestCase):
    def setUp(self):
        self.item = AccountRecord(
            id=1,
            platform="atxp",
            email="demo@example.com",
            password="mailbox-pass",
            user_id="acct-1",
            primary_token="https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1",
            overview={
                "gateway_health_alive": True,
                "gateway_health_model": "gpt-4.1-mini",
                "clowdbot_status": "failed",
                "create_clowdbot_completed": True,
                "claim_email_completed": False,
                "reward_progress": {"claimed": 1, "total": 2},
                "task_error": "claim_email failed",
            },
            credentials=[
                platform_credential(
                    "connection_string",
                    "https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1",
                    is_primary=True,
                ),
                platform_credential("connection_token", "conn-1"),
                platform_credential("privy_token", "privy-token"),
                platform_credential("refresh_token", "refresh-token"),
                platform_credential("wallet_address", "0xabc"),
                platform_credential("clowdbot_instance_id", "clowd-1"),
                platform_credential("claimed_agent_email", "agent@example.com"),
            ],
            provider_accounts=[mailbox_account(mailbox_jwt="mailbox-jwt", address_password="addr-pass")],
            provider_resources=[mailbox_resource()],
        )
        self.service = AccountExportsService(StubRepository([self.item]))

    def test_export_atxp_json_uses_requested_fields(self):
        artifact = self.service.export_json(
            AccountExportSelection(
                platform="atxp",
                select_all=True,
                field_keys=[
                    "email",
                    "account_id",
                    "connection_string",
                    "clowdbot_status",
                    "task_error",
                ],
            )
        )

        self.assertEqual(
            json.loads(artifact.content),
            [
                {
                    "email": "demo@example.com",
                    "account_id": "acct-1",
                    "connection_string": "https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1",
                    "clowdbot_status": "failed",
                    "task_error": "claim_email failed",
                }
            ],
        )

    def test_export_atxp_txt_respects_field_order(self):
        artifact = self.service.export_txt(
            AccountExportSelection(
                platform="atxp",
                select_all=True,
                field_keys=["email", "connection_string", "wallet_address"],
            )
        )

        self.assertEqual(
            artifact.content.splitlines(),
            ["demo@example.com----https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1----0xabc"],
        )

    def test_export_atxp_json_uses_null_for_missing_values(self):
        item = AccountRecord(
            id=2,
            platform="atxp",
            email="missing@example.com",
            password="mailbox-pass",
        )
        service = AccountExportsService(StubRepository([item]))

        artifact = service.export_json(
            AccountExportSelection(
                platform="atxp",
                select_all=True,
                field_keys=["email", "gateway_health_model", "mailbox_jwt", "reward_progress"],
            )
        )

        self.assertEqual(
            json.loads(artifact.content),
            [
                {
                    "email": "missing@example.com",
                    "gateway_health_model": None,
                    "mailbox_jwt": None,
                    "reward_progress": None,
                }
            ],
        )

    def test_export_atxp_json_supports_gateway_clowdbot_and_mailbox_fields(self):
        artifact = self.service.export_json(
            AccountExportSelection(
                platform="atxp",
                select_all=True,
                field_keys=[
                    "gateway_health_alive",
                    "gateway_health_model",
                    "create_clowdbot_completed",
                    "claim_email_completed",
                    "mailbox_jwt",
                    "address_password",
                    "address_id",
                    "api_url",
                    "auth_mode",
                ],
            )
        )

        self.assertEqual(
            json.loads(artifact.content),
            [
                {
                    "gateway_health_alive": True,
                    "gateway_health_model": "gpt-4.1-mini",
                    "create_clowdbot_completed": True,
                    "claim_email_completed": False,
                    "mailbox_jwt": "mailbox-jwt",
                    "address_password": "addr-pass",
                    "address_id": "127961",
                    "api_url": "https://apimail.example.com",
                    "auth_mode": "public_jwt",
                }
            ],
        )

    def test_export_atxp_rejects_unknown_field(self):
        with self.assertRaisesRegex(ValueError, "unsupported export field"):
            self.service.export_json(
                AccountExportSelection(
                    platform="atxp",
                    select_all=True,
                    field_keys=["email", "not_exists"],
                )
            )


if __name__ == "__main__":
    unittest.main()
