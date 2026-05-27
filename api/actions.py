from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from application.actions import ActionsService
from core.db import AccountModel, engine
from domain.actions import ActionExecutionCommand

router = APIRouter(prefix="/actions", tags=["actions"])
service = ActionsService()


class ActionRequest(BaseModel):
    params: dict = Field(default_factory=dict)


class BatchActionRequest(BaseModel):
    ids: list[int] = Field(default_factory=list)
    select_all: bool = False
    status_filter: Optional[str] = None
    search_filter: Optional[str] = None
    params: dict = Field(default_factory=dict)


def _resolve_batch_account_ids(
    platform: str,
    ids: list[int],
    select_all: bool,
    status_filter: str,
    search_filter: str,
) -> list[int]:
    if not select_all and ids:
        return ids
    with Session(engine) as session:
        q = select(AccountModel.id).where(AccountModel.platform == platform)
        if status_filter:
            q = q.where(AccountModel.display_status == status_filter)
        if search_filter:
            q = q.where(AccountModel.email.contains(search_filter))
        rows = session.exec(q.order_by(AccountModel.id)).all()
        return [int(row) for row in rows]


@router.get("/{platform}")
def list_actions(platform: str):
    return service.list_actions(platform)


@router.post("/{platform}/batch/{action_id}")
def execute_batch_action(platform: str, action_id: str, body: BatchActionRequest):
    account_ids = _resolve_batch_account_ids(
        platform,
        body.ids,
        body.select_all,
        body.status_filter or "",
        body.search_filter or "",
    )
    if not account_ids:
        raise HTTPException(400, "没有匹配的账号")
    task = service.execute_batch_action(
        platform=platform,
        action_id=action_id,
        account_ids=account_ids,
        params=body.params,
    )
    if not task:
        raise HTTPException(400, "任务创建失败")
    return task


@router.post("/{platform}/{account_id}/{action_id}")
def execute_action(platform: str, account_id: int, action_id: str, body: ActionRequest):
    task = service.execute_action(
        ActionExecutionCommand(
            platform=platform,
            account_id=account_id,
            action_id=action_id,
            params=body.params,
        )
    )
    if not task:
        raise HTTPException(400, "任务创建失败")
    return task
