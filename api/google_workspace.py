"""Google Workspace 批量用户管理 API。

提供 Web 界面可调用的端点：
- 生成用户清单 + 辅助邮箱
- 批量创建用户（通过 task 队列异步执行）
- 列出当前 Workspace 用户
- 批量删除用户
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/google-workspace", tags=["google-workspace"])

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
USERS_JSON = SCRIPTS / "_google_admin_bulk_users.json"
RECOVERY_JSON = SCRIPTS / "_google_admin_recovery_emails.json"


# ─── 生成用户清单 ───

class GenUsersRequest(BaseModel):
    count: int = 50
    recovery_domain: str = "bufan.de5.net"
    password: str = "Bufan123456"
    one_per_user: bool = True


@router.post("/gen-users")
def gen_users(body: GenUsersRequest):
    """生成用户清单 + 创建辅助邮箱（同步，可能耗时较长）。"""
    cmd = [
        sys.executable,
        str(SCRIPTS / "_google_admin_bulk_users_gen.py"),
        "--count", str(body.count),
        "--recovery-domain", body.recovery_domain,
    ]
    if body.one_per_user:
        cmd.append("--one-per-user")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, cwd=str(ROOT))
    if proc.returncode != 0:
        return {"ok": False, "stderr": proc.stderr[-500:]}
    # 读取生成结果
    if USERS_JSON.is_file():
        users = json.loads(USERS_JSON.read_text(encoding="utf-8"))
        has_recovery = sum(1 for u in users if u.get("recovery_email"))
        return {"ok": True, "total": len(users), "has_recovery": has_recovery, "users": users[:5]}
    return {"ok": False, "error": "users json not found"}


# ─── 批量创建用户（异步 task） ───

class BulkCreateRequest(BaseModel):
    limit: int = 0  # 0=全部
    offset: int = 0
    users_json: str = ""  # 自定义路径，空=默认


@router.post("/bulk-create")
def bulk_create(body: BulkCreateRequest):
    """创建一个异步 task 执行批量创建用户。返回 task_id。"""
    from application.tasks import create_task, TASK_STATUS_PENDING
    from services.task_runtime import task_runtime

    users_path = Path(body.users_json) if body.users_json else USERS_JSON
    if not users_path.is_file():
        raise HTTPException(400, f"用户清单不存在: {users_path}")

    users = json.loads(users_path.read_text(encoding="utf-8"))
    offset = max(0, body.offset)
    limit = body.limit if body.limit > 0 else len(users)
    target_users = users[offset:offset + limit]
    if not target_users:
        raise HTTPException(400, "没有待创建的用户（offset/limit 范围内为空）")

    task = create_task(
        task_type="google_workspace_bulk_create",
        platform="google_workspace",
        payload={
            "users_json": str(users_path),
            "offset": offset,
            "limit": limit,
            "count": len(target_users),
        },
        progress_total=len(target_users),
    )
    task_runtime.wake_up()
    return task


# ─── 批量删除用户 ───

class BulkDeleteRequest(BaseModel):
    user_ids: list[str] = Field(default_factory=list)
    delete_all_non_admin: bool = False


@router.post("/bulk-delete")
def bulk_delete(body: BulkDeleteRequest):
    """批量删除 Workspace 用户（通过 task 队列异步执行）。

    delete_all_non_admin=true 时，先从用户列表页抓取所有非管理员 userId。
    """
    from application.tasks import create_task
    from services.task_runtime import task_runtime

    target_ids = body.user_ids
    if body.delete_all_non_admin:
        # 交给 task handler 动态获取
        target_ids = []  # handler 里会自动抓取

    task = create_task(
        task_type="google_workspace_bulk_delete",
        platform="google_workspace",
        payload={
            "user_ids": target_ids,
            "delete_all_non_admin": body.delete_all_non_admin,
        },
        progress_total=max(len(target_ids), 1),
    )
    task_runtime.wake_up()
    return task


# ─── 列出用户清单 JSON ───

@router.get("/users-json")
def get_users_json():
    """返回已生成的用户清单 JSON。"""
    if not USERS_JSON.is_file():
        return {"ok": False, "users": [], "error": "not found"}
    users = json.loads(USERS_JSON.read_text(encoding="utf-8"))
    has_recovery = sum(1 for u in users if u.get("recovery_email"))
    return {
        "ok": True,
        "total": len(users),
        "has_recovery": has_recovery,
        "users": users,
    }


# ─── 列出辅助邮箱 ───

@router.get("/recovery-emails")
def get_recovery_emails():
    """返回已创建的辅助邮箱列表。"""
    if not RECOVERY_JSON.is_file():
        return {"ok": False, "emails": [], "error": "not found"}
    emails = json.loads(RECOVERY_JSON.read_text(encoding="utf-8"))
    return {"ok": True, "total": len(emails), "emails": emails}
