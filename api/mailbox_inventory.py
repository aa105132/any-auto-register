from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from application.mailbox_inventory import MailboxInventoryService

router = APIRouter(prefix="/mailbox-inventory", tags=["mailbox-inventory"])
service = MailboxInventoryService()


class MailboxInventoryImportRequest(BaseModel):
    provider_key: str
    lines: list[str] = Field(default_factory=list)


class MailboxInventoryUpdateRequest(BaseModel):
    status: str | None = None
    note: str | None = None
    last_error: str | None = None


@router.get("")
def list_mailbox_inventory(provider_key: str, status: str = ""):
    try:
        return service.list_items(provider_key, status=status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/import")
def import_mailbox_inventory(body: MailboxInventoryImportRequest):
    try:
        return service.import_items(body.provider_key, body.lines)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/export")
def export_mailbox_inventory(provider_key: str, status: str = ""):
    try:
        content = service.export_items(provider_key, status=status)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    file_name = f"{provider_key or 'mailbox_inventory'}_inventory.txt"
    return PlainTextResponse(
        content=content,
        headers={"Content-Disposition": f'attachment; filename="{file_name}"'},
    )


@router.patch("/{item_id}")
def update_mailbox_inventory(item_id: int, body: MailboxInventoryUpdateRequest):
    item = service.update_item(item_id, body.model_dump(exclude_none=True))
    if not item:
        raise HTTPException(404, "邮箱资产不存在")
    return item
