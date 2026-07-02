from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.google_account_pool import GoogleAccountPool

router = APIRouter(prefix="/google-account-pool", tags=["google-account-pool"])


class GoogleAccountImportRequest(BaseModel):
    lines: list[str] = Field(default_factory=list)
    source: str = "manual"
    source_order_id: str = ""
    expires_at: str = ""


class GoogleAccountStatusRequest(BaseModel):
    reason: str = ""



@router.get("")
def list_google_account_pool():
    pool = GoogleAccountPool()
    accounts = pool.list_all()
    stats = pool.stats()
    return {
        "stats": stats,
        "items": [
            {
                "email": account.email,
                "added_at": account.added_at,
                "expires_at": account.expires_at,
                "source": account.source,
                "password": account.password,
                "source_order_id": account.source_order_id,
                "registered_platforms": account.registered_platforms,
                "registered_count": len(account.registered_platforms or []),
                "notes": account.notes,
                "status": account.status or "valid",
            }
            for account in accounts
        ],
    }


@router.post("/import")
def import_google_account_pool(body: GoogleAccountImportRequest):
    pool = GoogleAccountPool()
    return pool.import_lines(
        body.lines,
        source=body.source or "manual",
        source_order_id=body.source_order_id,
        expires_at=body.expires_at,
    )


# 注意：/release-stale 必须在 /{email}/... 路由之前声明，否则 "release-stale" 会被当成 email 捕获。
@router.post("/release-stale")
def release_stale_reserved(body: GoogleAccountStatusRequest | None = None):
    """批量释放陈旧 reserved_platforms 锁（reserved 含但 registered 不含）。

    body.reason 传平台名做过滤（留空则清所有陈旧锁）。执行前需确认无对应平台任务在跑。
    """
    pool = GoogleAccountPool()
    platform_filter = str(getattr(body, "reason", "") or "").strip() if body else ""
    affected = pool.release_stale(platform=platform_filter)
    released = sum(len(platforms) for _, platforms in affected)
    return {
        "ok": True,
        "released": released,
        "affected": [{"email": email, "platform": p} for email, platforms in affected for p in platforms],
    }


@router.post("/{email}/invalid")
def mark_google_account_invalid(email: str, body: GoogleAccountStatusRequest):
    pool = GoogleAccountPool()
    return {"ok": pool.mark_invalid(email, reason=body.reason)}


@router.post("/{email}/valid")
def mark_google_account_valid(email: str):
    pool = GoogleAccountPool()
    return {"ok": pool.mark_valid(email)}


@router.delete("/invalid")
def delete_invalid_google_accounts():
    pool = GoogleAccountPool()
    return pool.delete_invalid()
