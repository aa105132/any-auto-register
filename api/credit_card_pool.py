from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.credit_card_pool import CreditCardPool

router = APIRouter(prefix="/credit-card-pool", tags=["credit-card-pool"])


class CreditCardImportRequest(BaseModel):
    lines: list[str] = Field(default_factory=list)
    source: str = "manual"


class CreditCardStatusRequest(BaseModel):
    reason: str = ""


class CreditCardUsedRequest(BaseModel):
    platform: str = ""
    account_email: str = ""


@router.get("")
def list_credit_card_pool(status: str = ""):
    pool = CreditCardPool()
    return {
        "stats": pool.stats(),
        "items": pool.list_all(status=status),
        "source": str(pool.path),
    }


@router.post("/import")
def import_credit_card_pool(body: CreditCardImportRequest):
    pool = CreditCardPool()
    return pool.import_lines(body.lines, source=body.source or "manual")


@router.post("/{card_id}/invalid")
def mark_credit_card_invalid(card_id: str, body: CreditCardStatusRequest):
    pool = CreditCardPool()
    ok = pool.mark_invalid(card_id, reason=body.reason)
    if not ok:
        raise HTTPException(404, "信用卡不存在")
    return {"ok": True}


@router.post("/{card_id}/valid")
def mark_credit_card_valid(card_id: str):
    pool = CreditCardPool()
    ok = pool.mark_valid(card_id)
    if not ok:
        raise HTTPException(404, "信用卡不存在")
    return {"ok": True}


@router.post("/{card_id}/used")
def mark_credit_card_used(card_id: str, body: CreditCardUsedRequest):
    pool = CreditCardPool()
    ok = pool.mark_used(card_id, platform=body.platform, account_email=body.account_email)
    if not ok:
        raise HTTPException(404, "信用卡不存在")
    return {"ok": True}


@router.delete("/invalid")
def delete_invalid_credit_cards():
    pool = CreditCardPool()
    return pool.delete_invalid()