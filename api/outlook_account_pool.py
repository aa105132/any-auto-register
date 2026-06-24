from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.outlook_account_pool import OutlookAccountPool, parse_outlook_pool_line

router = APIRouter(prefix="/outlook-account-pool", tags=["outlook-account-pool"])


class OutlookAccountImportRequest(BaseModel):
    lines: list[str] = Field(default_factory=list)
    source: str = "manual"


class OutlookAccountStatusRequest(BaseModel):
    reason: str = ""


@router.get("")
def list_outlook_account_pool():
    pool = OutlookAccountPool()
    accounts = pool.list_all()
    stats = pool.stats()
    return {
        "stats": stats,
        "items": [
            {
                "email": account.email,
                "password": account.password,
                "client_id": account.client_id,
                "refresh_token": account.refresh_token,
                "access_token": account.access_token,
                "expires_at": account.expires_at,
                "added_at": account.added_at,
                "source": account.source,
                "status": account.status or "valid",
                "notes": account.notes,
                "used_platforms": list(account.used_platforms or []),
            }
            for account in accounts
        ],
    }


@router.post("/import")
def import_outlook_account_pool(body: OutlookAccountImportRequest):
    pool = OutlookAccountPool()
    return pool.import_lines(body.lines, source=body.source or "manual")


@router.post("/parse")
def parse_outlook_account_lines(body: OutlookAccountImportRequest):
    """预览解析结果，不写入池。用于前端校验导入行格式。"""
    parsed_rows = []
    invalid = 0
    for raw in body.lines or []:
        parsed = parse_outlook_pool_line(raw)
        if not parsed:
            invalid += 1
            continue
        email, password, client_id, refresh_token = parsed
        parsed_rows.append({
            "email": email,
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh_token,
        })
    return {"parsed": parsed_rows, "invalid": invalid, "total": len(body.lines or [])}


@router.post("/{email}/invalid")
def mark_outlook_account_invalid(email: str, body: OutlookAccountStatusRequest):
    pool = OutlookAccountPool()
    return {"ok": pool.mark_invalid(email, reason=body.reason)}


@router.post("/{email}/valid")
def mark_outlook_account_valid(email: str):
    pool = OutlookAccountPool()
    return {"ok": pool.mark_valid(email)}


@router.delete("/invalid")
def delete_invalid_outlook_accounts():
    pool = OutlookAccountPool()
    return pool.delete_invalid()
