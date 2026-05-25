from __future__ import annotations

from application.mailbox_inventory_support import export_mailbox_inventory_lines
from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository


class MailboxInventoryService:
    def __init__(self, repository: MailboxInventoryRepository | None = None):
        self.repository = repository or MailboxInventoryRepository()

    def list_items(self, provider_key: str, *, status: str = "") -> dict:
        return {
            "items": self.repository.list_by_provider(provider_key, status=status),
            "counts": self.repository.get_status_counts(provider_key),
        }

    def import_items(self, provider_key: str, lines: list[str]) -> dict:
        result = self.repository.import_lines(provider_key, lines)
        result["counts"] = self.repository.get_status_counts(provider_key)
        return result

    def export_items(self, provider_key: str, *, status: str = "") -> str:
        items = self.repository.list_by_provider(provider_key, status=status)
        return export_mailbox_inventory_lines(provider_key, items)

    def update_item(self, item_id: int, payload: dict) -> dict | None:
        return self.repository.update_item(
            item_id,
            status=payload.get("status"),
            note=payload.get("note"),
            last_error=payload.get("last_error"),
        )
