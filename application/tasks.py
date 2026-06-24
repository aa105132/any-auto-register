"""Task orchestration and persistence helpers."""
from __future__ import annotations

import sys as _sys
if _sys.platform == "win32":
    for _sn in ("stdout", "stderr"):
        _st = getattr(_sys, _sn, None)
        if _st and hasattr(_st, "reconfigure"):
            try:
                _st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

import json
import random
import string
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, select, func

from application.account_import_parser import parse_account_import_lines
from application.mailbox_inventory_support import (
    build_mailbox_inventory_seed,
    inventory_provider_label,
    resolve_inventory_register_failure,
    supports_mailbox_inventory,
)
from core.account_graph import patch_account_graph
from core.base_platform import AccountStatus, RegisterConfig
from core.config_store import config_store
from core.datetime_utils import format_local_clock, serialize_datetime
from core.db import AccountModel, AccountOverviewModel, TaskEventModel, TaskLog, TaskModel, engine, save_account
from core.platform_accounts import build_platform_account
from core.proxy_utils import normalize_proxy_url
from core.resin_proxy import resolve_resin_proxy_config
from core.scdn_runtime_proxy import ScdnRuntimeProxySource
from core.subscription_proxy import subscription_proxy_manager as _subscription_proxy_manager
from core.registry import get
from domain.accounts import AccountImportLine
from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository
from infrastructure.platform_runtime import PlatformRuntime

TASK_TYPE_REGISTER = "register"
TASK_TYPE_ACCOUNT_CHECK = "account_check"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"
TASK_TYPE_PLATFORM_ACTION = "platform_action"
TASK_TYPE_BATCH_ACTION = "batch_action"
TASK_TYPE_GOOGLE_WORKSPACE_BULK_CREATE = "google_workspace_bulk_create"
TASK_TYPE_GOOGLE_WORKSPACE_BULK_DELETE = "google_workspace_bulk_delete"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"
TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"
TASK_STATUS_CANCELLED = "cancelled"

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_CANCELLED,
}
ACTIVE_TASK_STATUSES = {
    TASK_STATUS_CLAIMED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_CANCEL_REQUESTED,
}

_INVENTORY_REUSE_LIMIT = 4
_VENICE_PROXY_SUCCESS_LIMIT = 3
_ATXP_IP_SUCCESS_LIMIT = 1
_AIROUTER_IP_SUCCESS_LIMIT = 3
_ALIAS_CHARSET = string.ascii_lowercase + string.digits

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()
_scdn_runtime_proxy_source = ScdnRuntimeProxySource()

_venice_proxy_successes: dict[str, int] = {}
# AI-ROUTER 会在代理选择流程里做 IP 预占，部分调用点已经持有该锁；
# 使用 RLock 避免同线程二次进入时卡死。
_venice_proxy_successes_lock = threading.RLock()
_venice_resin_slot = [0]
_venice_resin_ip_successes: dict[str, int] = {}
_venice_resin_ip_banned: set[str] = set()
_atxp_ip_banned: set[str] = set()
_atxp_ip_successes: dict[str, int] = {}
_venice_resin_slot_to_ip: dict[int, str] = {}
_venice_decodo_slot = [0]
_venice_decodo_port_to_ip: dict[int, str] = {}
_venice_brightdata_slot = [0]
_venice_brightdata_session_to_ip: dict[int, str] = {}
_airouter_ip_successes: dict[str, int] = {}
_airouter_ip_inflight: set[str] = set()


def _warm_resin_ip_state() -> None:
    """从历史日志恢复 Resin IP 的成功计数和拉黑状态"""
    import re as _re
    try:
        with Session(engine) as s:
            ban_rows = s.exec(
                select(TaskEventModel).where(
                    TaskEventModel.message.like("%Resin IP%banned%")
                )
            ).all()
            for row in ban_rows:
                m = _re.search(r"Resin IP (\S+) banned", row.message)
                if m:
                    _venice_resin_ip_banned.add(m.group(1))


            ip_pat = _re.compile(r"Resin slot (vs\d+): IP (\S+) \(\d+/3\)")
            success_pat = _re.compile(r"注册成功")
            tasks_with_resin: dict[str, list[str]] = {}

            # AI-ROUTER 旧版曾写入 “banned” 日志；新版规则改为每个 IP 可成功 3 个账号，
            # 因此这里不再预热旧 banned 记录，等价于释放历史误拉黑 IP。
            airouter_success_pat = _re.compile(r"AI-ROUTER IP (\S+) \((\d+)/3\)")
            airouter_success_rows = s.exec(
                select(TaskEventModel).where(
                    TaskEventModel.message.like("%AI-ROUTER IP%")
                )
            ).all()
            for row in airouter_success_rows:
                m = airouter_success_pat.search(row.message)
                if m:
                    try:
                        _airouter_ip_successes[m.group(1)] = max(
                            _airouter_ip_successes.get(m.group(1), 0),
                            min(int(m.group(2)), _AIROUTER_IP_SUCCESS_LIMIT),
                        )
                    except Exception:
                        pass
            slot_events = s.exec(
                select(TaskEventModel).where(
                    TaskEventModel.message.like("%Resin slot%IP%")
                )
            ).all()
            for row in slot_events:
                m = ip_pat.search(row.message)
                if m:
                    tasks_with_resin.setdefault(row.task_id, []).append(m.group(2))

            success_events = s.exec(
                select(TaskEventModel).where(
                    TaskEventModel.message.like("%注册成功%")
                )
            ).all()
            success_task_ids = {e.task_id for e in success_events}

            from collections import Counter
            ip_success_count: Counter[str] = Counter()
            for task_id, ips in tasks_with_resin.items():
                if task_id in success_task_ids:
                    for ip in ips:
                        ip_success_count[ip] += 1

            for ip, count in ip_success_count.items():
                _venice_resin_ip_successes[ip] = count

        banned = len(_venice_resin_ip_banned)
        warmed = len(_venice_resin_ip_successes)
        over_limit = sum(1 for c in _venice_resin_ip_successes.values() if c >= _VENICE_PROXY_SUCCESS_LIMIT)
        if banned or warmed:
            import logging
            logging.getLogger(__name__).info(
                "Resin IP warmup: %d banned, %d tracked (%d over limit)", banned, warmed, over_limit
            )
    except Exception:
        pass


def _warm_resin_ip_state_deferred() -> None:
    """延迟 3 秒后在后台线程执行 Resin IP 状态预热，避免阻塞模块导入。"""
    import threading as _threading
    def _run():
        import time as _time
        _time.sleep(3)
        _warm_resin_ip_state()
    _t = _threading.Thread(target=_run, daemon=True, name="resin-warmup")
    _t.start()


_warm_resin_ip_state_deferred()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _serialize_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _task_lock(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _task_locks[task_id] = lock
        return lock


def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:
    with _task_lock(task_id):
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            if not task:
                return None
            fn(task)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task


def _save_task_log(platform: str, email: str, status: str, error: str = "", detail: dict | None = None) -> None:
    with Session(engine) as session:
        log = TaskLog(
            platform=platform,
            email=email,
            status=status,
            error=error,
            detail_json=_dump_json(detail or {}),
        )
        session.add(log)
        session.commit()


def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"errors": [], "cashier_urls": [], "data": None}
    if result:
        base.update(result)
    return base


def _resolve_register_lines(payload: dict[str, Any]) -> list:
    raw_lines = list(payload.get("lines") or [])
    if not raw_lines:
        return []
    return parse_account_import_lines(raw_lines)


def _resolve_inventory_provider_key(payload: dict[str, Any]) -> str:
    extra = dict(payload.get("extra") or {})
    return str(
        payload.get("inventory_provider_key")
        or extra.get("inventory_provider_key")
        or extra.get("mail_provider")
        or ""
    ).strip()


def _available_inventory_register_count(payload: dict[str, Any]) -> int:
    if str(payload.get("email") or "").strip():
        return 0
    provider_key = _resolve_inventory_provider_key(payload)
    if not supports_mailbox_inventory(provider_key):
        return 0
    requested = max(int(payload.get("count", 1) or 1), 1)
    available = MailboxInventoryRepository().count_available(
        provider_key,
        **_inventory_claim_options(payload),
    )
    return min(requested, available)


def _claim_inventory_register_lines(payload: dict[str, Any], logger: "TaskLogger") -> list[AccountImportLine]:
    if str(payload.get("email") or "").strip():
        return []
    provider_key = _resolve_inventory_provider_key(payload)
    if not supports_mailbox_inventory(provider_key):
        return []
    requested = max(int(payload.get("count", 1) or 1), 1)
    claimed = MailboxInventoryRepository().claim_available(
        provider_key,
        count=requested,
        task_id=logger.task_id,
        platform=str(payload.get("platform", "") or ""),
        **_inventory_claim_options(payload),
    )
    seeds: list[AccountImportLine] = []
    for item in claimed:
        seed = build_mailbox_inventory_seed(provider_key, item)
        if not seed:
            continue
        extra = dict(seed.extra or {})
        extra["_inventory"] = {
            "id": int(item.get("id") or 0),
            "provider_key": provider_key,
            "metadata": dict(item.get("metadata") or {}),
        }
        if item.get("note"):
            extra["inventory_note"] = str(item.get("note") or "")
            extra.setdefault("overview", {})["inventory_note"] = str(item.get("note") or "")
        seeds.append(AccountImportLine(email=seed.email, password=seed.password, extra=extra))
    return seeds


def _seed_inventory_id(seed: Any | None) -> int:
    if not seed:
        return 0
    extra = dict(getattr(seed, "extra", {}) or {})
    inventory = dict(extra.get("_inventory") or {})
    return int(inventory.get("id", 0) or 0)


def _seed_inventory_metadata(seed: Any | None) -> dict[str, Any]:
    if not seed:
        return {}
    extra = dict(getattr(seed, "extra", {}) or {})
    inventory = dict(extra.get("_inventory") or {})
    return dict(inventory.get("metadata") or {})


def _normalize_sub_mail_mode(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"plus", "原邮箱+"}:
        return "plus"
    if raw in {"dot", "原邮箱."}:
        return "dot"
    return "none"


def _normalize_sub_mail_length(value: Any, fallback: int = 4) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        resolved = fallback
    return max(1, min(resolved, 16))


def _normalize_optional_positive_int(value: Any) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return 0
    return resolved if resolved > 0 else 0


def _resolve_outlook_alias_max_count(extra: dict[str, Any], inventory_metadata: dict[str, Any]) -> int:
    for value in (
        inventory_metadata.get("outlook_alias_max_count"),
        inventory_metadata.get("alias_max_count"),
        inventory_metadata.get("sub_mail_max_count"),
        extra.get("outlook_alias_max_count"),
        extra.get("alias_max_count"),
        extra.get("sub_mail_max_count"),
    ):
        resolved = _normalize_optional_positive_int(value)
        if resolved > 0:
            return resolved
    return 0


def _resolve_outlook_alias_created_count(inventory_metadata: dict[str, Any]) -> int:
    for value in (
        inventory_metadata.get("outlook_alias_created_count"),
        inventory_metadata.get("alias_created_count"),
    ):
        resolved = _normalize_optional_positive_int(value)
        if resolved > 0:
            return resolved
    return 0


def _outlook_alias_limit_reached(extra: dict[str, Any], inventory_metadata: dict[str, Any]) -> bool:
    max_count = _resolve_outlook_alias_max_count(extra, inventory_metadata)
    if max_count <= 0:
        return False
    return _resolve_outlook_alias_created_count(inventory_metadata) >= max_count


def _normalize_bool_flag(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on", "enabled"}:
        return True
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    return default



def _outlook_alias_enabled(extra: dict[str, Any]) -> bool:
    for key in (
        "outlook_alias_enabled",
        "outlook_alias_register_enabled",
        "outlook_sub_mail_enabled",
    ):
        if key in extra:
            return _normalize_bool_flag(extra.get(key), default=False)
    return False


def _inventory_claim_options(payload: dict[str, Any]) -> dict[str, Any]:
    extra = dict(payload.get("extra") or {})
    provider_key = _resolve_inventory_provider_key(payload)
    if provider_key == "outlook_token":
        return {"include_outlook_aliases": _outlook_alias_enabled(extra)}
    return {}


def _build_alias_email(base_email: str, mode: str, length: int) -> str:
    normalized = str(base_email or "").strip()
    if "@" not in normalized:
        return normalized
    local, domain = normalized.split("@", 1)
    local = local.split("+", 1)[0].rstrip(".")
    suffix = "".join(random.choice(_ALIAS_CHARSET) for _ in range(length))
    if mode == "plus":
        return f"{local}+{suffix}@{domain}"
    if mode == "dot":
        return f"{local}.{suffix}@{domain}"
    return normalized


def _build_inventory_target_email(
    base_email: str,
    extra: dict[str, Any],
    inventory_metadata: dict[str, Any],
    *,
    provider_key: str = "",
) -> str:
    normalized_provider = str(provider_key or "").strip()
    if normalized_provider == "outlook_token":
        alias_enabled = _outlook_alias_enabled(extra)
        if not alias_enabled:
            return base_email
        if _outlook_alias_limit_reached(extra, inventory_metadata):
            return base_email
        parent_email = str(
            extra.get("outlook_email")
            or inventory_metadata.get("outlook_login_email")
            or inventory_metadata.get("alias_parent_email")
            or inventory_metadata.get("parent_email")
            or base_email
        ).strip()
        if not parent_email:
            return base_email
        mode = _normalize_sub_mail_mode(extra.get("sub_mail_mode"))
        if mode == "none":
            mode = "plus"
        length = _normalize_sub_mail_length(extra.get("sub_mail_length"), fallback=4)
        return _build_alias_email(parent_email, mode, length)

    if normalized_provider != "luckmail":
        return base_email
    success_count = int(inventory_metadata.get("successful_registrations", 0) or 0)
    use_alias_on_first_register = _normalize_bool_flag(extra.get("sub_mail_use_on_first_register"), default=False)
    if success_count <= 0 and not use_alias_on_first_register:
        return base_email
    mode = _normalize_sub_mail_mode(extra.get("sub_mail_mode"))
    if mode == "none":
        return base_email
    length = _normalize_sub_mail_length(extra.get("sub_mail_length"), fallback=4)
    return _build_alias_email(base_email, mode, length)


def _build_outlook_alias_parent_item(seed: Any | None, parent_email: str, metadata: dict[str, Any]) -> dict[str, Any]:
    seed_extra = dict(getattr(seed, "extra", {}) or {})
    inventory = dict(seed_extra.get("_inventory") or {})
    resolved_metadata = dict(metadata or {})
    password = str(seed_extra.get("outlook_password") or resolved_metadata.get("password") or "")
    client_id = str(seed_extra.get("outlook_client_id") or resolved_metadata.get("client_id") or "").strip()
    refresh_token = str(
        seed_extra.get("outlook_refresh_token")
        or seed_extra.get("purchase_token")
        or inventory.get("purchase_token")
        or resolved_metadata.get("refresh_token")
        or ""
    ).strip()
    if password:
        resolved_metadata["password"] = password
    if client_id:
        resolved_metadata["client_id"] = client_id
    return {
        "id": int(inventory.get("id", 0) or 0),
        "provider_key": "outlook_token",
        "email": str(parent_email or "").strip(),
        "purchase_token": refresh_token,
        "metadata": resolved_metadata,
    }


def _resolve_outlook_alias_parent_email(
    seed: Any | None,
    extra: dict[str, Any],
    inventory_metadata: dict[str, Any],
    preferred_parent_email: str = "",
) -> str:
    seed_email = str(getattr(seed, "email", "") or "").strip()
    return str(
        preferred_parent_email
        or extra.get("outlook_alias_parent_email")
        or extra.get("outlook_email")
        or inventory_metadata.get("outlook_login_email")
        or inventory_metadata.get("alias_parent_email")
        or inventory_metadata.get("parent_email")
        or seed_email
        or ""
    ).strip()


def _select_outlook_alias_email(
    *,
    parent_email: str,
    generated_outlook_alias: str = "",
    registered_email: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    normalized_parent = str(parent_email or "").strip().lower()
    extra = dict(extra or {})
    candidates = [
        generated_outlook_alias,
        extra.get("outlook_registration_email"),
        extra.get("outlook_alias_email"),
        registered_email,
    ]
    for candidate in candidates:
        alias_email = str(candidate or "").strip()
        if not alias_email:
            continue
        if normalized_parent and alias_email.lower() == normalized_parent:
            continue
        return alias_email
    return ""


def _upsert_successful_outlook_alias(
    *,
    inventory_repository: Any,
    seed: Any | None,
    extra: dict[str, Any],
    inventory_metadata: dict[str, Any],
    platform_name: str,
    logger: Any,
    registered_email: str,
    generated_outlook_alias: str = "",
    preferred_parent_email: str = "",
) -> bool:
    if _outlook_alias_limit_reached(extra, inventory_metadata):
        logger.log("  [邮箱池] Outlook 父邮箱别名上限已达，跳过本次入池", level="warning")
        return False
    parent_email = _resolve_outlook_alias_parent_email(
        seed,
        extra,
        inventory_metadata,
        preferred_parent_email,
    )
    alias_email = _select_outlook_alias_email(
        parent_email=parent_email,
        generated_outlook_alias=generated_outlook_alias,
        registered_email=registered_email,
        extra=extra,
    )
    if not parent_email or not alias_email:
        return False
    parent_item = _build_outlook_alias_parent_item(seed, parent_email, inventory_metadata)
    alias_item = inventory_repository.upsert_outlook_alias(
        parent_item,
        alias_email=alias_email,
        platform=platform_name,
    )
    parent_item_id = int(parent_item.get("id", 0) or 0)
    if parent_item_id > 0:
        base_metadata = dict(parent_item.get("metadata") or {})
        created_count = _resolve_outlook_alias_created_count(base_metadata) + 1
        metadata_updates = {"outlook_alias_created_count": created_count}
        alias_parent_email = str(base_metadata.get("outlook_login_email") or parent_email or "").strip()
        if alias_parent_email:
            metadata_updates["outlook_login_email"] = alias_parent_email
            metadata_updates["alias_parent_email"] = alias_parent_email
        try:
            inventory_repository.update_item(
                parent_item_id,
                metadata_updates=metadata_updates,
                note=str(parent_item.get("note") or ""),
            )
        except Exception:
            pass
        try:
            parent_item.setdefault("metadata", {}).update(metadata_updates)
        except Exception:
            pass
        if isinstance(alias_item, dict):
            alias_metadata = dict(alias_item.get("metadata") or {})
            alias_metadata.update(metadata_updates)
            alias_item["metadata"] = alias_metadata
    logger.log(f"  [邮箱池] Outlook 别名已加入邮箱池: {alias_email}")
    return True


def _is_verification_timeout_failure(error: str, result: Any | None = None) -> bool:
    raw_error = str(error or "")
    lowered = raw_error.lower()
    metadata = dict(getattr(result, "metadata", {}) or {})
    stage = str(metadata.get("last_stage", "") or "").strip().lower()
    if any(token in raw_error or token in lowered for token in ("luckmail_rate_limited", "请求过于频繁", "http 429", "rate limit")):
        return False
    explicit_tokens = (
        "等待验证码超时",
        "验证码超时",
        "otp timeout",
        "verification code timeout",
        "未获取到验证码",
        "等待验证链接超时",
        "验证链接超时",
        "未获取到验证链接",
        "verification link timeout",
    )
    if any(token in raw_error or token in lowered for token in explicit_tokens):
        return True
    if ("timeout" in lowered or "超时" in raw_error) and any(token in lowered or token in raw_error for token in ("otp", "验证码", "verification code", "code")):
        return True
    if "获取验证码失败" in raw_error and stage in {"otp_sent", "otp_received"}:
        return True
    return False


def _merge_register_extra(base_extra: dict[str, Any], line_extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_extra or {})
    incoming = dict(line_extra or {})
    for key, value in incoming.items():
        if key in {"provider_accounts", "provider_resources"}:
            current = list(merged.get(key) or [])
            current.extend(list(value or []))
            merged[key] = current
            continue
        if key == "overview":
            merged[key] = {
                **dict(merged.get(key) or {}),
                **dict(value or {}),
            }
            continue
        merged[key] = value
    return merged



def _sanitize_parallel_oauth_browser_extra(extra: dict[str, Any], *, concurrency: int) -> dict[str, Any]:
    """并发 OAuth 账号池默认禁用共享 CDP 端口。

    chrome_cdp_url 表示“连接一个已经存在的浏览器端口”。如果并发 worker
    全部复用同一个 9222，它们会共享 Chrome context/profile，导致账号串号、
    标签页互抢焦点。除非显式设置 oauth_reuse_existing_cdp=true，否则并发时
    清空 chrome_cdp_url，让每个 worker 走独立临时 CDP/profile。
    """
    sanitized = dict(extra or {})
    sanitized["_task_concurrency"] = int(concurrency or 1)
    identity_provider = str(sanitized.get("identity_provider") or "").strip().lower()
    account_source = str(sanitized.get("oauth_account_source") or "").strip().lower()
    is_oauth_pool = identity_provider == "oauth_browser" and account_source in {"mailbox", "mail_provider", "provider"}
    if int(concurrency or 1) > 1 and is_oauth_pool and not _is_truthy_config(sanitized.get("oauth_reuse_existing_cdp")):
        if str(sanitized.get("chrome_cdp_url") or "").strip():
            sanitized["_shared_chrome_cdp_url_disabled"] = sanitized.get("chrome_cdp_url")
        sanitized["chrome_cdp_url"] = ""
        sanitized["reuse_existing_cdp"] = False
    return sanitized



def _is_freemodel_platform(platform_name: str) -> bool:
    return str(platform_name or "").strip().lower() == "freemodel"


def _extract_freemodel_next_invite_code(account: Any) -> str:
    """从 FreeModel 注册结果里提取下一个账号要使用的邀请码。"""
    extra = dict(getattr(account, "extra", {}) or {})
    return str(extra.get("referral_code") or extra.get("invite_code") or "").strip()


def _latest_freemodel_referral_code() -> str:
    """读取最近一个已注册 FreeModel 账号的邀请码，作为新任务的邀请链链头。"""
    with Session(engine) as session:
        rows = session.exec(
            select(AccountOverviewModel, AccountModel)
            .join(AccountModel, AccountOverviewModel.account_id == AccountModel.id)
            .where(AccountModel.platform == "freemodel")
            .where(AccountOverviewModel.lifecycle_status == "registered")
            .order_by(AccountModel.id.desc())
            .limit(20)
        ).all()
    for overview, _account in rows:
        try:
            summary = overview.get_summary()
        except Exception:
            summary = {}
        legacy_extra = dict(summary.get("legacy_extra") or {})
        invite_code = str(
            summary.get("referral_code")
            or summary.get("invite_code")
            or legacy_extra.get("referral_code")
            or legacy_extra.get("invite_code")
            or ""
        ).strip()
        if invite_code:
            return invite_code
    return ""


def _resolve_freemodel_initial_invite_code(platform_name: str, extra: dict[str, Any]) -> str:
    """解析 FreeModel 批量注册的初始邀请码；未填写时自动接上最新成功账号。"""
    if not _is_freemodel_platform(platform_name):
        return ""
    configured = str(
        dict(extra or {}).get("freemodel_invite_code")
        or dict(extra or {}).get("invite_code")
        or ""
    ).strip()
    return configured or _latest_freemodel_referral_code()


def _apply_freemodel_chain_invite(platform_name: str, extra: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """给 FreeModel 当前注册参数注入链式邀请码。"""
    resolved = dict(extra or {})
    if not _is_freemodel_platform(platform_name):
        return resolved
    invite_code = str(
        state.get("next_invite_code")
        or resolved.get("freemodel_invite_code")
        or resolved.get("invite_code")
        or ""
    ).strip()
    if invite_code:
        resolved["freemodel_invite_code"] = invite_code
    return resolved


def _resolve_freemodel_chain_concurrency(platform_name: str, concurrency: int, logger: Any | None = None) -> int:
    """FreeModel 邀请链依赖上一个账号结果，强制串行避免并发打乱顺序。"""
    resolved = max(int(concurrency or 1), 1)
    if _is_freemodel_platform(platform_name) and resolved > 1:
        if logger is not None:
            try:
                logger.log("FreeModel 邀请链需要按注册成功顺序串行执行，已将并发降为 1", level="warning")
            except Exception:
                pass
        return 1
    return resolved


# === 通用邀请码链式注册（配置驱动；freemodel 保留专用函数以兼容既有测试）===
# field: 注入注册参数 extra 的字段名；keys: 从账号 extra / overview summary 提取邀请码的键；
# lifecycle: 跨任务查最新号时的 lifecycle_status 过滤（空=不过滤）。
_INVITE_CHAIN_CONFIG: dict[str, dict[str, Any]] = {
    "vellum": {"field": "vellum_invite_code", "keys": ("own_invite_code",), "lifecycle": ""},
}


def _generic_chain_config(platform_name: str) -> dict[str, Any] | None:
    return _INVITE_CHAIN_CONFIG.get(str(platform_name or "").strip().lower())


def _latest_generic_referral_code(platform_name: str) -> str:
    """读取最近一个已注册账号的邀请码（summary 或 legacy_extra），作为新任务链头。"""
    cfg = _generic_chain_config(platform_name)
    if not cfg:
        return ""
    platform_key = str(platform_name or "").strip().lower()
    with Session(engine) as session:
        query = (
            select(AccountOverviewModel, AccountModel)
            .join(AccountModel, AccountOverviewModel.account_id == AccountModel.id)
            .where(AccountModel.platform == platform_key)
        )
        if cfg["lifecycle"]:
            query = query.where(AccountOverviewModel.lifecycle_status == cfg["lifecycle"])
        rows = session.exec(query.order_by(AccountModel.id.desc()).limit(20)).all()
    for overview, _account in rows:
        try:
            summary = overview.get_summary()
        except Exception:
            summary = {}
        legacy_extra = dict(summary.get("legacy_extra") or {})
        for key in cfg["keys"]:
            code = str(summary.get(key) or legacy_extra.get(key) or "").strip()
            if code:
                return code
    return ""


def _resolve_generic_initial_invite_code(platform_name: str, extra: dict[str, Any]) -> str:
    cfg = _generic_chain_config(platform_name)
    if not cfg:
        return ""
    src = dict(extra or {})
    configured = str(src.get(cfg["field"]) or src.get("invite_code") or "").strip()
    return configured or _latest_generic_referral_code(platform_name)


def _apply_generic_chain_invite(platform_name: str, extra: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """并发安全：从邀请码池取最新可用码注入注册参数（无则回退 extra 内配置/默认）。"""
    cfg = _generic_chain_config(platform_name)
    resolved = dict(extra or {})
    if not cfg:
        return resolved
    codes = state.get("codes") or []
    invite_code = str(
        (codes[-1] if codes else "")
        or resolved.get(cfg["field"])
        or resolved.get("invite_code")
        or ""
    ).strip()
    if invite_code:
        resolved[cfg["field"]] = invite_code
    return resolved


def _extract_generic_next_invite_code(platform_name: str, account: Any) -> str:
    cfg = _generic_chain_config(platform_name)
    if not cfg:
        return ""
    extra = dict(getattr(account, "extra", {}) or {})
    for key in cfg["keys"]:
        val = str(extra.get(key) or "").strip()
        if val:
            return val
    return ""


# --- 统一 dispatcher：注册循环调用，按平台路由到 freemodel 专用或通用实现 ---
def _chain_invite_enabled(platform_name: str) -> bool:
    return _is_freemodel_platform(platform_name) or _generic_chain_config(platform_name) is not None


def _chain_apply_invite(platform_name: str, extra: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    if _is_freemodel_platform(platform_name):
        return _apply_freemodel_chain_invite(platform_name, extra, state)
    return _apply_generic_chain_invite(platform_name, extra, state)


def _chain_extract_next_invite(platform_name: str, account: Any) -> str:
    if _is_freemodel_platform(platform_name):
        return _extract_freemodel_next_invite_code(account)
    return _extract_generic_next_invite_code(platform_name, account)


def _chain_resolve_concurrency(platform_name: str, concurrency: int, logger: Any | None = None) -> int:
    # freemodel 维持严格串行；通用(vellum)用邀请码池，支持并发，不再强制降并发。
    if _is_freemodel_platform(platform_name):
        return _resolve_freemodel_chain_concurrency(platform_name, concurrency, logger)
    return max(int(concurrency or 1), 1)


def _chain_init_state(platform_name: str, extra: dict[str, Any]) -> dict[str, Any]:
    """初始化链式状态。freemodel: 串行单值 next_invite_code；通用: 并发邀请码池 codes(种子=最新已注册号/配置码)。"""
    if _is_freemodel_platform(platform_name):
        return {"next_invite_code": _resolve_freemodel_initial_invite_code(platform_name, extra)}
    seed = _resolve_generic_initial_invite_code(platform_name, extra)
    return {"codes": [seed] if seed else []}


def _chain_state_initial_code(platform_name: str, state: dict[str, Any]) -> str:
    if _is_freemodel_platform(platform_name):
        return str(state.get("next_invite_code") or "")
    codes = state.get("codes") or []
    return str(codes[-1]) if codes else ""


def _chain_record_success(platform_name: str, account: Any, state: dict[str, Any]) -> str:
    """注册成功后登记本号邀请码供后续号链式使用。freemodel 覆盖单值；通用追加进池。需在锁内调用。"""
    code = _chain_extract_next_invite(platform_name, account)
    if not code:
        return ""
    if _is_freemodel_platform(platform_name):
        state["next_invite_code"] = code
    else:
        state.setdefault("codes", []).append(code)
    return code


def _derive_partial_lifecycle(result: Any | None, error: str) -> str:
    metadata = dict(getattr(result, "metadata", {}) or {})
    stage = str(metadata.get("last_stage", "") or "").strip()
    if stage in {"workspace_loaded", "workspace_selected", "redirect_followed", "oauth_callback"}:
        return "oauth_pending"
    if any(token in str(error or "") for token in ("选择 Workspace 失败", "跟随重定向链失败", "处理 OAuth 回调失败")):
        return "oauth_pending"
    disposition = str(metadata.get("registration_disposition", "") or "").strip()
    if disposition == "existing_suspected":
        return "existing_suspected"
    if disposition == "existing_account" or bool(metadata.get("is_existing_account")):
        return "existing_account"
    if "Failed to register username" in str(error or ""):
        return "existing_suspected"
    return "register_failed"


def _persist_registration_snapshot(
    *,
    platform: str,
    email: str,
    password: str,
    lifecycle_status: str,
    extra: dict[str, Any] | None = None,
    error: str = "",
    result: Any | None = None,
) -> None:
    normalized_email = str(email or "").strip()
    if not normalized_email:
        return

    merged_extra = dict(extra or {})
    result_metadata = dict(getattr(result, "metadata", {}) or {})
    overview = {
        **dict(merged_extra.get("overview") or {}),
        "remote_email": normalized_email,
        "register_error": str(error or ""),
        "register_stage": str(result_metadata.get("last_stage", "") or ""),
        "register_source": str(getattr(result, "source", "") or ""),
        "signup_page_type": str(result_metadata.get("signup_page_type", "") or ""),
        "registration_disposition": str(result_metadata.get("registration_disposition", "") or ""),
        "password_register_status_code": int(result_metadata.get("password_register_status_code", 0) or 0),
        "password_register_error_code": str(result_metadata.get("password_register_error_code", "") or ""),
        "password_register_error_message": str(result_metadata.get("password_register_error_message", "") or ""),
        "last_checked_reason": "register_failure",
    }
    if result_metadata.get("registered_at"):
        overview["registered_at"] = result_metadata["registered_at"]
    if lifecycle_status == "oauth_pending":
        overview["oauth_status"] = "pending"

    with Session(engine) as session:
        model = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == platform)
            .where(AccountModel.email == normalized_email)
        ).first()
        if not model:
            model = AccountModel(
                platform=platform,
                email=normalized_email,
                password=password or "",
                user_id=str(getattr(result, "account_id", "") or ""),
            )
            session.add(model)
            session.commit()
            session.refresh(model)
        else:
            if password:
                model.password = password
            if getattr(result, "account_id", ""):
                model.user_id = str(getattr(result, "account_id", "") or "")
            model.updated_at = _utcnow()
            session.add(model)
            session.commit()
            session.refresh(model)

        credential_updates: dict[str, Any] = {}
        for key in ("access_token", "refresh_token", "id_token", "session_token"):
            value = str(getattr(result, key, "") or "")
            if value:
                credential_updates[key] = value

        primary_token = credential_updates.get("access_token") or ""
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            primary_token=primary_token or None,
            summary_updates=overview,
            credential_updates=credential_updates or None,
            provider_accounts=list(merged_extra.get("provider_accounts") or []) or None,
            provider_resources=list(merged_extra.get("provider_resources") or []) or None,
            replace_provider_accounts=bool(merged_extra.get("provider_accounts")),
            replace_provider_resources=bool(merged_extra.get("provider_resources")),
        )
        session.commit()


def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    if task_type in {TASK_TYPE_ACCOUNT_CHECK, TASK_TYPE_PLATFORM_ACTION}:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    return []


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING, TASK_STATUS_CANCEL_REQUESTED},
        "progress": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        "progress_detail": {
            "current": progress_current,
            "total": progress_total,
            "label": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        },
        "success": int(task.success_count or 0),
        "error_count": int(task.error_count or 0),
        "errors": list(result.get("errors", [])),
        "cashier_urls": list(result.get("cashier_urls", [])),
        "data": result.get("data"),
        "result": result,
        "error": task.error,
        "created_at": _serialize_datetime(task.created_at),
        "started_at": _serialize_datetime(task.started_at),
        "finished_at": _serialize_datetime(task.finished_at),
        "updated_at": _serialize_datetime(task.updated_at),
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
        "detail": event.get_detail(),
        "created_at": _serialize_datetime(event.created_at),
    }


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_PENDING,
        payload_json=_dump_json(payload),
        result_json=_dump_json(_task_result_seed(result_seed)),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    normalized_payload = dict(payload)
    normalized_payload["extra"] = dict(payload.get("extra") or {})
    if (
        str(normalized_payload.get("platform") or "").strip().lower() == "swarms"
        and str(normalized_payload["extra"].get("swarms_registration_mode") or "").strip().lower() == "browser"
    ):
        normalized_payload["executor_type"] = "headed"
    raw_lines = list(payload.get("lines") or [])
    if raw_lines:
        parsed_lines = parse_account_import_lines(raw_lines)
        if not parsed_lines:
            raise ValueError("导入文本未解析出有效账号行")
        count = len(parsed_lines)
    else:
        inventory_count = _available_inventory_register_count(payload)
        if inventory_count > 0:
            count = inventory_count
        else:
            count = max(int(payload.get("count", 1) or 1), 1)
            extra = dict(payload.get("extra") or {})
            if (
                _resolve_inventory_provider_key(payload) == "luckmail"
                and str(extra.get("luckmail_email") or "").strip()
                and str(extra.get("luckmail_purchase_token") or "").strip()
            ):
                count = 1
    normalized_payload["count"] = count
    return create_task(
        task_type=TASK_TYPE_REGISTER,
        platform=str(payload.get("platform", "")),
        payload=normalized_payload,
        progress_total=count,
    )


def create_account_check_task(account_id: int) -> dict[str, Any]:
    platform = ""
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            platform = model.platform
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK,
        platform=platform,
        payload={"account_id": int(account_id)},
        progress_total=1,
    )


def create_account_check_all_task(platform: str = "", limit: int = 50) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform=platform,
        payload={"platform": platform, "limit": int(limit or 50)},
        progress_total=max(int(limit or 50), 1),
    )


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,
    )


def create_batch_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    account_ids = list(payload.get("account_ids") or [])
    return create_task(
        task_type=TASK_TYPE_BATCH_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=max(len(account_ids), 1),
    )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


def list_tasks(*, platform: str = "", status: str = "", page: int = 1, page_size: int = 50) -> dict[str, Any]:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    with Session(engine) as session:
        q = select(TaskModel)
        total_q = select(func.count()).select_from(TaskModel)
        if platform:
            q = q.where(TaskModel.platform == platform)
            total_q = total_q.where(TaskModel.platform == platform)
        if status:
            q = q.where(TaskModel.status == status)
            total_q = total_q.where(TaskModel.status == status)
        q = q.order_by(TaskModel.created_at.desc())
        total = int(session.exec(total_q).one() or 0)
        items = session.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "page": page, "items": [serialize_task(item) for item in items]}


def list_task_events(task_id: str, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    with Session(engine) as session:
        q = (
            select(TaskEventModel)
            .where(TaskEventModel.task_id == task_id)
            .where(TaskEventModel.id > since)
            .order_by(TaskEventModel.id)
            .limit(limit)
        )
        items = session.exec(q).all()
    return [serialize_event(item) for item in items]


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
    return serialize_event(event)


def mark_incomplete_tasks_interrupted() -> None:
    task_ids: list[str] = []
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel).where(TaskModel.status.in_(list(ACTIVE_TASK_STATUSES)))
        ).all()
        for task in tasks:
            task_ids.append(str(task.id))
            task.status = TASK_STATUS_INTERRUPTED
            task.error = task.error or "任务在服务重启后被中断"
            task.finished_at = _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
        session.commit()
    for task_id in task_ids:
        append_task_event(
            task_id,
            "任务在服务重启后被标记为中断",
            event_type="state",
            level="warning",
        )


def request_cancel(task_id: str) -> Optional[dict[str, Any]]:
    task = _mutate_task(
        task_id,
        lambda model: _request_cancel_mutation(model),
    )
    if not task:
        return None
    append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
    return serialize_task(task)


def _request_cancel_mutation(task: TaskModel) -> None:
    if task.status in TERMINAL_TASK_STATUSES:
        return
    if task.status == TASK_STATUS_PENDING:
        task.status = TASK_STATUS_CANCELLED
        task.finished_at = _utcnow()
        task.error = task.error or "任务在开始前被取消"
    else:
        task.status = TASK_STATUS_CANCEL_REQUESTED


def claim_next_runnable_task(
    *,
    running_platform_counts: dict[str, int] | None = None,
    busy_account_keys: set[str] | None = None,
    max_parallel_per_platform: int = 1,
) -> Optional[dict[str, Any]]:
    running_platform_counts = dict(running_platform_counts or {})
    busy_account_keys = set(busy_account_keys or set())
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel)
            .where(TaskModel.status == TASK_STATUS_PENDING)
            .order_by(TaskModel.created_at)
        ).all()
        for task in tasks:
            payload = task.get_payload()
            platform = task.platform or str(payload.get("platform", "") or "")
            account_keys = _task_account_keys(task.type, payload)
            if platform and running_platform_counts.get(platform, 0) >= max_parallel_per_platform:
                continue
            if account_keys and busy_account_keys.intersection(account_keys):
                continue
            task.status = TASK_STATUS_CLAIMED
            task.started_at = task.started_at or _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            return {"id": task.id, "platform": platform, "account_keys": account_keys}
    return None


class TaskLogger:
    def __init__(self, task_id: str):
        self.task_id = task_id

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        # 全程吃掉所有异常：日志写入失败永远不能阻塞业务线程
        # （Windows GBK 终端遇到 ✗/✓ 等字符会触发 UnicodeEncodeError）
        try:
            safe_message = str(message) if message is not None else ""
        except Exception:
            safe_message = repr(message)
        try:
            append_task_event(
                self.task_id,
                safe_message,
                event_type=event_type,
                level=level,
                detail=detail,
            )
        except Exception:
            pass
        try:
            line = f"[task:{self.task_id}] {safe_message}\n"
            buf = getattr(_sys.stdout, "buffer", None)
            if buf is not None:
                buf.write(line.encode("utf-8", errors="replace"))
                buf.flush()
            else:
                # 兜底：直接写入 stdout，让其按当前编码处理（errors=replace 已在 main.py 配置）
                try:
                    _sys.stdout.write(line)
                    _sys.stdout.flush()
                except UnicodeEncodeError:
                    enc = getattr(_sys.stdout, "encoding", "ascii") or "ascii"
                    _sys.stdout.write(line.encode(enc, errors="replace").decode(enc, errors="replace"))
                    _sys.stdout.flush()
        except Exception:
            pass

    def mark_running(self) -> None:
        def _update(task: TaskModel) -> None:
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()

        _mutate_task(self.task_id, _update)
        self.log("任务已开始执行", event_type="state")

    def is_cancel_requested(self) -> bool:
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return bool(task and task.status == TASK_STATUS_CANCEL_REQUESTED)

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        current = max(int(current), 0)

        def _update(task: TaskModel) -> None:
            task.progress_current = current
            if total is not None:
                task.progress_total = max(int(total), 0)

        _mutate_task(self.task_id, _update)

    def record_success(self) -> None:
        def _update(task: TaskModel) -> None:
            task.success_count += 1

        _mutate_task(self.task_id, _update)

    def record_error(self, error: str) -> None:
        def _update(task: TaskModel) -> None:
            task.error_count += 1
            result = task.get_result()
            errors = list(result.get("errors", []))
            errors.append(error)
            result["errors"] = errors
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def add_cashier_url(self, url: str) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            urls = list(result.get("cashier_urls", []))
            urls.append(url)
            result["cashier_urls"] = urls
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_result_data(self, data: Any) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        def _update(task: TaskModel) -> None:
            task.status = status
            task.finished_at = _utcnow()
            if error:
                task.error = error

        _mutate_task(self.task_id, _update)
        event_level = "error" if status == TASK_STATUS_FAILED else ("warning" if status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {status}",
            level=event_level,
            event_type="state",
            detail={"status": status, "error": error},
        )


def _auto_upload_cpa(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "chatgpt":
        return
    try:
        from core.config_store import config_store

        cpa_url = config_store.get("cpa_api_url", "")
        if cpa_url:
            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

            class _AccountProxy:
                pass

            target = _AccountProxy()
            target.email = account.email
            extra = account.extra or {}
            target.access_token = extra.get("access_token") or account.token
            target.refresh_token = extra.get("refresh_token", "")
            target.id_token = extra.get("id_token", "")

            token_data = generate_token_json(target)
            ok, msg = upload_to_cpa(token_data)
            task_logger.log(f"  [CPA] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
    except Exception as exc:
        task_logger.log(f"  [CPA] 自动上传异常: {exc}", level="warning")


def _auto_import_codebanana2api(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "codebanana":
        return
    try:
        enabled = _normalize_bool_flag(config_store.get("codebanana2api_enabled", "false"), default=False)
        if not enabled:
            return
        api_url = str(config_store.get("codebanana2api_url", "") or "").strip()
        if not api_url:
            return
        from platforms.codebanana.codebanana2api_upload import upload_to_codebanana2api

        ok, msg = upload_to_codebanana2api(account, api_url=api_url)
        task_logger.log(f"  [CodeBanana2API] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
    except Exception as exc:
        task_logger.log(f"  [CodeBanana2API] 自动导入异常: {exc}", level="warning")


def _auto_import_anuma2api(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "anuma":
        return
    try:
        enabled = _normalize_bool_flag(config_store.get("anuma2api_enabled", "false"), default=False)
        if not enabled:
            return
        api_url = str(config_store.get("anuma2api_url", "") or "").strip()
        if not api_url:
            return
        from platforms.anuma.anuma2api_upload import upload_to_anuma2api

        ok, msg = upload_to_anuma2api(account, api_url=api_url)
        task_logger.log(f"  [Anuma2API] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
    except Exception as exc:
        task_logger.log(f"  [Anuma2API] 自动导入异常: {exc}", level="warning")


def _auto_import_enter2api(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "enter":
        return
    try:
        enabled = _normalize_bool_flag(config_store.get("enter2api_enabled", "false"), default=False)
        if not enabled:
            return
        api_url = str(config_store.get("enter2api_url", "") or "").strip()
        if not api_url:
            return
        extra = account.extra or {}
        ai_api_token = str(extra.get("ai_api_token", "") or "").strip()

        # 导出 AI token 到 output/keys.txt
        if ai_api_token:
            _append_ai_token_to_file(ai_api_token, task_logger)

        # 推送到 enter2api
        import requests
        payload = {
            "mode": "append",
            "raw": {
                "accounts": [{
                    "email": account.email,
                    "access_token": extra.get("access_token", ""),
                    "refresh_token": extra.get("refresh_token", ""),
                    "workspace_id": extra.get("workspace_id", ""),
                    "project_id": extra.get("project_id", ""),
                    "default_project_name": extra.get("project_name", ""),
                    "ai_api_token": ai_api_token,
                    "ai_connection_state": extra.get("ai_connection_state", ""),
                    "entercloud_enabled": extra.get("entercloud_enabled", False),
                    "entercloud_setup_completed": extra.get("entercloud_setup_completed", False),
                    "entercloud_provider": extra.get("entercloud_provider", ""),
                    "entercloud_cloud_ref": extra.get("entercloud_cloud_ref", ""),
                    "entercloud_api_url": extra.get("entercloud_api_url", ""),
                    "entercloud_anon_key": extra.get("entercloud_anon_key", ""),
                }]
            },
        }
        r = requests.post(f"{api_url}/api/ui/accounts/import", json=payload, timeout=15)
        r.raise_for_status()
        rj = r.json() if r.text else {}
        added = (rj.get("result") or {}).get("added", "?")
        task_logger.log(f"  [Enter2API] [OK] added={added}")
    except Exception as exc:
        task_logger.log(f"  [Enter2API] 自动推送异常: {exc}", level="warning")



def _auto_import_blendspace2api(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "blendspace":
        return
    try:
        enabled = _normalize_bool_flag(config_store.get("blendspace2api_enabled", "false"), default=False)
        if not enabled:
            return
        api_url = str(config_store.get("blendspace2api_url", "") or "").strip()
        admin_api_key = str(config_store.get("blendspace2api_admin_api_key", "") or "").strip()
        if not api_url or not admin_api_key:
            return
        from platforms.blendspace.blendspace2api_upload import upload_to_blendspace2api

        ok, msg = upload_to_blendspace2api(account, api_url=api_url, admin_api_key=admin_api_key)
        task_logger.log(f"  [BlendSpace2API] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
    except Exception as exc:
        task_logger.log(f"  [BlendSpace2API] ??????: {exc}", level="warning")

def _append_ai_token_to_file(token: str, task_logger: TaskLogger) -> None:
    import os
    try:
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        keys_path = os.path.join(output_dir, "keys.txt")
        with open(keys_path, "a", encoding="utf-8") as f:
            f.write(token + "\n")
        ai_tokens_path = os.path.join(output_dir, "ai_api_tokens.txt")
        with open(ai_tokens_path, "a", encoding="utf-8") as f:
            f.write(token + "\n")
        task_logger.log(f"  [AI Token] saved to {keys_path}")
    except Exception as exc:
        task_logger.log(f"  [AI Token] write failed: {exc}", level="warning")


def _auto_export_fireworks_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "fireworks":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [Fireworks] 没有可导出的 API key", level="warning")
        return
    # 写入 output/fireworks_keys.txt（一行一个）
    import os
    try:
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        keys_path = os.path.join(output_dir, "fireworks_keys.txt")
        with open(keys_path, "a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        task_logger.log(f"  [Fireworks] API key saved to {keys_path}")
    except Exception as exc:
        task_logger.log(f"  [Fireworks] API key write failed: {exc}", level="warning")
    # 同时写入通用 keys.txt
    _append_ai_token_to_file(api_key, task_logger)


def _auto_export_gettoken_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "gettoken":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [GetToken] ?????? API key", level="warning")
        return
    import os
    try:
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        keys_path = os.path.join(output_dir, "gettoken_keys.txt")
        with open(keys_path, "a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        task_logger.log(f"  [GetToken] API key saved to {keys_path}")
    except Exception as exc:
        task_logger.log(f"  [GetToken] API key write failed: {exc}", level="warning")
    _append_ai_token_to_file(api_key, task_logger)


def _auto_export_lemondata_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "lemondata":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or extra.get("ai_api_token", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [LemonData] 没有可导出的 API key", level="warning")
        return
    try:
        from pathlib import Path

        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        lemon_path = output_dir / "lemondata_keys.txt"
        for target_path in (lemon_path, output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with target_path.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        task_logger.log(f"  [LemonData] API key saved to {lemon_path}")
    except Exception as exc:
        task_logger.log(f"  [LemonData] API key write failed: {exc}", level="warning")


def _auto_export_zo_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "zo":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or extra.get("ai_api_token", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [Zo] 没有可导出的 API key", level="warning")
        return
    try:
        from pathlib import Path

        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        zo_path = output_dir / "zo_keys.txt"
        for target_path in (zo_path, output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with target_path.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        task_logger.log(f"  [Zo] API key saved to {zo_path}")
    except Exception as exc:
        task_logger.log(f"  [Zo] API key write failed: {exc}", level="warning")


def _build_zo_twoapi_record_from_account(account: Any, *, source: str = "registration") -> dict[str, Any]:
    if str(getattr(account, "platform", "") or "").strip().lower() != "zo":
        return {}
    extra = dict(getattr(account, "extra", {}) or {})
    api_key = str(extra.get("api_key") or extra.get("ai_api_token") or getattr(account, "token", "") or "").strip()
    cookies = dict(extra.get("cookies") or {})
    access = str(extra.get("access_token") or cookies.get("access_token") or "").strip()
    refresh = str(extra.get("refresh_token") or cookies.get("refresh_token") or "").strip()
    if access:
        cookies["access_token"] = access
    if refresh:
        cookies["refresh_token"] = refresh
    workspace_result = dict(extra.get("workspace_result") or {})
    workspace = dict(workspace_result.get("workspace") or extra.get("workspace") or {})
    handle = str(extra.get("workspace_handle") or workspace.get("handle") or "").strip()
    origin = str(extra.get("workspace_origin") or workspace.get("origin") or workspace.get("url") or "").strip().rstrip("/")
    if handle:
        workspace["handle"] = handle
    if origin:
        workspace["origin"] = origin
    if workspace:
        workspace_result["workspace"] = workspace
    record: dict[str, Any] = {
        "email": str(getattr(account, "email", "") or extra.get("email", "") or "").strip(),
        "password": str(getattr(account, "password", "") or ""),
        "user_id": str(getattr(account, "user_id", "") or ""),
        "import_source": str(source or "registration"),
        "saved_at": int(time.time()),
    }
    if api_key:
        record["api_key"] = api_key
        record["ai_api_token"] = api_key
    if cookies:
        record["cookies"] = cookies
    if workspace_result:
        record["workspace_result"] = workspace_result
    for key in (
        "api_key_info",
        "api_verification",
        "key_create_result",
        "coupon_result",
        "credit_result",
        "balance_result",
        "card_binding_result",
        "onboarding_result",
        "phone_result",
        "proxy_deploy_result",
        "openai_proxy_base_url",
    ):
        value = extra.get(key)
        if value not in (None, "", {}, []):
            record[key] = value
    if not record.get("email") or not (api_key or cookies or record.get("openai_proxy_base_url")):
        return {}
    return record


def _auto_push_zo_twoapi(task_logger: TaskLogger, account, task_extra: dict[str, Any] | None = None) -> None:
    if str(getattr(account, "platform", "") or "").strip().lower() != "zo":
        return
    extra = dict(task_extra or {})
    mode = str(extra.get("twoapi_push_mode") or "none").strip().lower()
    if mode in {"", "none", "off", "disabled", "false", "0"}:
        return
    if mode not in {"local", "remote"}:
        task_logger.log(f"  [Zo2API] 未知推送模式: {mode}", level="warning")
        return
    record = _build_zo_twoapi_record_from_account(account, source=f"registration-{mode}")
    if not record:
        task_logger.log("  [Zo2API] 没有可导入的 Zo 账号记录", level="warning")
        return
    try:
        if mode == "local":
            from services.twoapi.manager import get_twoapi_manager

            result = get_twoapi_manager().import_plugin_accounts("zo", records=[record], source="registration-local")
            task_logger.log(f"  [Zo2API] 本地导入完成: imported={result.get('imported', 0)} updated={result.get('updated', 0)}")
            return
        target_url = str(extra.get("twoapi_push_target_url") or "").strip()
        if not target_url:
            task_logger.log("  [Zo2API] 远端推送已跳过: 未填写远端 2API 后端地址", level="warning")
            return
        from services.twoapi.manager import get_twoapi_manager

        plugin = get_twoapi_manager().get_plugin("zo")
        import_url = plugin._push_target_import_url(target_url)
        response = plugin.transport.post(
            import_url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"source": "registration-remote", "records": [record]},
            timeout=max(1.0, float(extra.get("twoapi_push_timeout") or 30.0)),
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": str(getattr(response, "text", "") or "")[:500]}
        if not getattr(response, "ok", False):
            raise RuntimeError(f"status={getattr(response, 'status_code', 0)} body={str(data)[:300]}")
        task_logger.log(f"  [Zo2API] 远端推送完成: pushed=1 target={import_url}")
    except Exception as exc:
        task_logger.log(f"  [Zo2API] 自动推送异常: {exc}", level="warning")


def _build_swarms_twoapi_record_from_account(account: Any, *, source: str = "registration") -> dict[str, Any]:
    if str(getattr(account, "platform", "") or "").strip().lower() != "swarms":
        return {}
    extra = dict(getattr(account, "extra", {}) or {})
    api_key = str(extra.get("api_key") or extra.get("ai_api_token") or getattr(account, "token", "") or "").strip()
    if not api_key:
        return {}
    user_info = extra.get("user_info") if isinstance(extra.get("user_info"), dict) else {}
    record: dict[str, Any] = {
        "email": str(getattr(account, "email", "") or extra.get("email", "") or "").strip(),
        "password": str(getattr(account, "password", "") or extra.get("password", "") or ""),
        "user_id": str(getattr(account, "user_id", "") or extra.get("user_id") or user_info.get("id") or ""),
        "api_key": api_key,
        "ai_api_token": api_key,
        "base_url": str(extra.get("base_url") or extra.get("openai_base_url") or "https://api.swarms.world/v1").strip(),
        "openai_base_url": str(extra.get("openai_base_url") or extra.get("base_url") or "https://api.swarms.world/v1").strip(),
        "native_api_base": str(extra.get("native_api_base") or extra.get("openai_base_url") or "https://api.swarms.world/v1").strip(),
        "credit_amount": float(extra.get("credit_amount") or 100.0),
        "native_openai": True,
        "ok": True,
        "source": str(source or "registration"),
        "import_source": str(source or "registration"),
        "saved_at": int(time.time()),
    }
    for key in ("api_key_info", "access_token", "refresh_token", "user_info", "cookie_map", "account_overview"):
        value = extra.get(key)
        if value not in (None, "", {}, []):
            record[key] = value
    cookies = extra.get("cookie_map") or extra.get("cookies")
    if isinstance(cookies, dict) and cookies:
        record["cookies"] = cookies
    return record if record.get("email") else {}


def _auto_push_swarms_twoapi(task_logger: TaskLogger, account, task_extra: dict[str, Any] | None = None) -> None:
    if str(getattr(account, "platform", "") or "").strip().lower() != "swarms":
        return
    extra = dict(task_extra or {})
    mode = str(extra.get("twoapi_push_mode") or "none").strip().lower()
    if mode in {"", "none", "off", "disabled", "false", "0"}:
        return
    if mode not in {"local", "remote"}:
        task_logger.log(f"  [Swarms2API] 未知推送模式: {mode}", level="warning")
        return
    record = _build_swarms_twoapi_record_from_account(account, source=f"registration-{mode}")
    if not record:
        task_logger.log("  [Swarms2API] 没有可导入的 Swarms 账号记录", level="warning")
        return
    try:
        from services.twoapi.manager import get_twoapi_manager

        manager = get_twoapi_manager()
        if mode == "local":
            result = manager.import_plugin_accounts("swarms", records=[record], source="registration-local")
            imported = result.get("imported", result.get("created", 0))
            updated = result.get("updated", 0)
            task_logger.log(f"  [Swarms2API] 本地导入完成: imported={imported} updated={updated}")
            return
        target_url = str(extra.get("twoapi_push_target_url") or "").strip()
        if not target_url:
            task_logger.log("  [Swarms2API] 远端推送已跳过: 未填写远端 2API 后端地址", level="warning")
            return
        plugin = manager.get_plugin("swarms")
        import_url = plugin._push_target_import_url(target_url)
        response = plugin.transport.post(
            import_url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"source": "registration-remote", "records": [record]},
            timeout=max(1.0, float(extra.get("twoapi_push_timeout") or 30.0)),
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": str(getattr(response, "text", "") or "")[:500]}
        if not getattr(response, "ok", False):
            raise RuntimeError(f"status={getattr(response, 'status_code', 0)} body={str(data)[:300]}")
        task_logger.log(f"  [Swarms2API] 远端推送完成: pushed=1 target={import_url}")
    except Exception as exc:
        task_logger.log(f"  [Swarms2API] 自动推送异常: {exc}", level="warning")


def _auto_export_swarms_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "swarms":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or extra.get("ai_api_token", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [Swarms] 没有可导出的 API key", level="warning")
        return
    try:
        from pathlib import Path
        import json

        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        keys_path = output_dir / "swarms_keys.txt"
        credentials_path = output_dir / "swarms_credentials.json"

        with keys_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{getattr(account, 'email', '')}|{api_key}\n")

        try:
            existing = json.loads(credentials_path.read_text(encoding="utf-8")) if credentials_path.exists() else []
        except Exception:
            existing = []
        rows = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
        rows.append({
            "email": getattr(account, "email", "") or "swarms-auto@local",
            "password": getattr(account, "password", "") or "",
            "user_id": getattr(account, "user_id", "") or str(extra.get("user_id", "") or ""),
            "api_key": api_key,
            "ai_api_token": api_key,
            "source": "registration_auto_export",
            "openai_base_url": "https://api.swarms.world/v1",
            "credit_amount": 100.0,
            "ok": True,
        })
        deduped = []
        seen = set()
        for row in rows:
            key = str(row.get("api_key") or row.get("ai_api_token") or row.get("token") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        credentials_path.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
        task_logger.log(f"  [Swarms] API key saved to {credentials_path}")
    except Exception as exc:
        task_logger.log(f"  [Swarms] API key write failed: {exc}", level="warning")


def _build_anycap_twoapi_record_from_account(account: Any, *, source: str = "registration") -> dict[str, Any]:
    if str(getattr(account, "platform", "") or "").strip().lower() != "anycap":
        return {}
    extra = dict(getattr(account, "extra", {}) or {})
    api_key = str(extra.get("api_key") or extra.get("ai_api_token") or getattr(account, "token", "") or "").strip()
    if not api_key:
        return {}
    record: dict[str, Any] = {
        "email": str(getattr(account, "email", "") or extra.get("email", "") or "").strip(),
        "password": str(getattr(account, "password", "") or extra.get("password", "") or ""),
        "user_id": str(getattr(account, "user_id", "") or extra.get("user_id") or ""),
        "api_key": api_key,
        "ai_api_token": api_key,
        "base_url": str(extra.get("native_api_base") or extra.get("api_base") or "https://api.anycap.ai").strip(),
        "native_api_base": str(extra.get("native_api_base") or extra.get("api_base") or "https://api.anycap.ai").strip(),
        "credit_amount": float(extra.get("credit_amount") or 100.0),
        "native_anycap": True,
        "ok": True,
        "source": str(source or "registration"),
        "import_source": str(source or "registration"),
        "saved_at": int(time.time()),
    }
    for key in ("api_key_info", "access_token", "profile", "cookies", "cookie_header", "api_verification", "account_overview"):
        value = extra.get(key)
        if value not in (None, "", {}, []):
            record[key] = value
    return record if record.get("email") else {}


def _auto_push_anycap_twoapi(task_logger: TaskLogger, account, task_extra: dict[str, Any] | None = None) -> None:
    if str(getattr(account, "platform", "") or "").strip().lower() != "anycap":
        return
    extra = dict(task_extra or {})
    mode = str(extra.get("twoapi_push_mode") or "none").strip().lower()
    if mode in {"", "none", "off", "disabled", "false", "0"}:
        return
    if mode not in {"local", "remote"}:
        task_logger.log(f"  [AnyCap2API] 未知推送模式: {mode}", level="warning")
        return
    record = _build_anycap_twoapi_record_from_account(account, source=f"registration-{mode}")
    if not record:
        task_logger.log("  [AnyCap2API] 没有可导入的 AnyCap 账号记录", level="warning")
        return
    try:
        from services.twoapi.manager import get_twoapi_manager

        manager = get_twoapi_manager()
        if mode == "local":
            result = manager.import_plugin_accounts("anycap", records=[record], source="registration-local")
            imported = result.get("imported", result.get("created", 0))
            updated = result.get("updated", 0)
            task_logger.log(f"  [AnyCap2API] 本地导入完成: imported={imported} updated={updated}")
            return
        target_url = str(extra.get("twoapi_push_target_url") or "").strip()
        if not target_url:
            task_logger.log("  [AnyCap2API] 远端推送已跳过: 未填写远端 2API 后端地址", level="warning")
            return
        plugin = manager.get_plugin("anycap")
        import_url = plugin._push_target_import_url(target_url)
        response = plugin.transport.post(
            import_url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"source": "registration-remote", "records": [record]},
            timeout=max(1.0, float(extra.get("twoapi_push_timeout") or 30.0)),
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": str(getattr(response, "text", "") or "")[:500]}
        if not getattr(response, "ok", False):
            raise RuntimeError(f"status={getattr(response, 'status_code', 0)} body={str(data)[:300]}")
        task_logger.log(f"  [AnyCap2API] 远端推送完成: pushed=1 target={import_url}")
    except Exception as exc:
        task_logger.log(f"  [AnyCap2API] 自动推送异常: {exc}", level="warning")


def _build_thesys_twoapi_record_from_account(account: Any, *, source: str = "registration") -> dict[str, Any]:
    if str(getattr(account, "platform", "") or "").strip().lower() != "thesys":
        return {}
    extra = dict(getattr(account, "extra", {}) or {})
    api_key = str(extra.get("api_key") or extra.get("ai_api_token") or getattr(account, "token", "") or "").strip()
    if not api_key:
        return {}
    record: dict[str, Any] = {
        "email": str(getattr(account, "email", "") or extra.get("email", "") or "").strip(),
        "password": str(getattr(account, "password", "") or extra.get("password", "") or ""),
        "user_id": str(getattr(account, "user_id", "") or extra.get("user_id") or ""),
        "api_key": api_key,
        "ai_api_token": api_key,
        "base_url": str(extra.get("openai_compatible_api_base") or extra.get("llm_api_base") or extra.get("api_base") or "https://api.thesys.dev/v1/embed").strip(),
        "openai_base_url": str(extra.get("openai_compatible_api_base") or extra.get("llm_api_base") or extra.get("api_base") or "https://api.thesys.dev/v1/embed").strip(),
        "credit_amount": float(extra.get("credit_amount") or 100.0),
        "native_thesys": True,
        "openai_compatible": True,
        "free_models": extra.get("free_models") if isinstance(extra.get("free_models"), list) else [],
        "ok": True,
        "source": str(source or "registration"),
        "import_source": str(source or "registration"),
        "saved_at": int(time.time()),
    }
    for key in ("api_key_info", "api_verification", "chat_verification", "billing", "user", "org", "orgs", "account_overview"):
        value = extra.get(key)
        if value not in (None, "", {}, []):
            record[key] = value
    return record if record.get("email") else {}


def _auto_push_thesys_twoapi(task_logger: TaskLogger, account, task_extra: dict[str, Any] | None = None) -> None:
    if str(getattr(account, "platform", "") or "").strip().lower() != "thesys":
        return
    extra = dict(task_extra or {})
    mode = str(extra.get("twoapi_push_mode") or "none").strip().lower()
    if mode in {"", "none", "off", "disabled", "false", "0"}:
        return
    if mode not in {"local", "remote"}:
        task_logger.log(f"  [Thesys2API] 未知推送模式: {mode}", level="warning")
        return
    record = _build_thesys_twoapi_record_from_account(account, source=f"registration-{mode}")
    if not record:
        task_logger.log("  [Thesys2API] 没有可导入的 Thesys 账号记录", level="warning")
        return
    try:
        from services.twoapi.manager import get_twoapi_manager

        manager = get_twoapi_manager()
        if mode == "local":
            result = manager.import_plugin_accounts("thesys", records=[record], source="registration-local")
            imported = result.get("imported", result.get("created", 0))
            updated = result.get("updated", 0)
            task_logger.log(f"  [Thesys2API] 本地导入完成: imported={imported} updated={updated}")
            return
        target_url = str(extra.get("twoapi_push_target_url") or "").strip()
        if not target_url:
            task_logger.log("  [Thesys2API] 远端推送已跳过: 未填写远端 2API 后端地址", level="warning")
            return
        plugin = manager.get_plugin("thesys")
        import_url = plugin._push_target_import_url(target_url)
        response = plugin.transport.post(
            import_url,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={"source": "registration-remote", "records": [record]},
            timeout=max(1.0, float(extra.get("twoapi_push_timeout") or 30.0)),
        )
        try:
            data = response.json()
        except Exception:
            data = {"raw": str(getattr(response, "text", "") or "")[:500]}
        if not getattr(response, "ok", False):
            raise RuntimeError(f"status={getattr(response, 'status_code', 0)} body={str(data)[:300]}")
        task_logger.log(f"  [Thesys2API] 远端推送完成: pushed=1 target={import_url}")
    except Exception as exc:
        task_logger.log(f"  [Thesys2API] 自动推送异常: {exc}", level="warning")


def _auto_export_thesys_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "thesys":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or extra.get("ai_api_token", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [Thesys] 没有可导出的 API key", level="warning")
        return
    try:
        from pathlib import Path
        import json

        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        keys_path = output_dir / "thesys_keys.txt"
        credentials_path = output_dir / "thesys_credentials.json"

        with keys_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{getattr(account, 'email', '')}|{api_key}\n")

        try:
            existing = json.loads(credentials_path.read_text(encoding="utf-8")) if credentials_path.exists() else []
        except Exception:
            existing = []
        rows = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
        rows.append({
            "email": getattr(account, "email", "") or "thesys-auto@local",
            "password": getattr(account, "password", "") or "",
            "user_id": getattr(account, "user_id", "") or str(extra.get("user_id", "") or ""),
            "api_key": api_key,
            "ai_api_token": api_key,
            "source": "registration_auto_export",
            "openai_base_url": "https://api.thesys.dev/v1/embed",
            "free_models": extra.get("free_models") if isinstance(extra.get("free_models"), list) else [],
            "credit_amount": 100.0,
            "ok": True,
        })
        deduped = []
        seen = set()
        for row in rows:
            key = str(row.get("api_key") or row.get("ai_api_token") or row.get("token") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        credentials_path.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
        task_logger.log(f"  [Thesys] API key saved to {credentials_path}")
    except Exception as exc:
        task_logger.log(f"  [Thesys] API key write failed: {exc}", level="warning")


def _auto_export_anycap_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "anycap":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or extra.get("ai_api_token", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [AnyCap] 没有可导出的 API key", level="warning")
        return
    try:
        from pathlib import Path
        import json

        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        keys_path = output_dir / "anycap_keys.txt"
        credentials_path = output_dir / "anycap_credentials.json"

        with keys_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{getattr(account, 'email', '')}|{api_key}\n")

        try:
            existing = json.loads(credentials_path.read_text(encoding="utf-8")) if credentials_path.exists() else []
        except Exception:
            existing = []
        rows = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
        rows.append({
            "email": getattr(account, "email", "") or "anycap-auto@local",
            "password": getattr(account, "password", "") or "",
            "user_id": getattr(account, "user_id", "") or str(extra.get("user_id", "") or ""),
            "api_key": api_key,
            "ai_api_token": api_key,
            "source": "registration_auto_export",
            "native_api_base": "https://api.anycap.ai",
            "credit_amount": 100.0,
            "ok": True,
        })
        deduped = []
        seen = set()
        for row in rows:
            key = str(row.get("api_key") or row.get("ai_api_token") or row.get("token") or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        credentials_path.write_text(json.dumps(deduped, ensure_ascii=False, indent=2), encoding="utf-8")
        task_logger.log(f"  [AnyCap] API key saved to {credentials_path}")
    except Exception as exc:
        task_logger.log(f"  [AnyCap] API key write failed: {exc}", level="warning")

def _auto_export_featherless_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "featherless":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or extra.get("ai_api_token", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [Featherless] 没有可导出的 API key", level="warning")
        return
    try:
        from pathlib import Path

        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        featherless_path = output_dir / "featherless_keys.txt"
        for target_path in (featherless_path, output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with target_path.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        task_logger.log(f"  [Featherless] API key saved to {featherless_path}")
    except Exception as exc:
        task_logger.log(f"  [Featherless] API key write failed: {exc}", level="warning")


def _auto_export_jiekou_key(task_logger: TaskLogger, account) -> None:
    if getattr(account, "platform", "") != "jiekou":
        return
    extra = account.extra or {}
    api_key = str(extra.get("api_key", "") or extra.get("ai_api_token", "") or account.token or "").strip()
    if not api_key:
        task_logger.log("  [Jiekou] 没有可导出的 API key", level="warning")
        return
    try:
        from pathlib import Path

        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        jiekou_path = output_dir / "jiekou_keys.txt"
        for target_path in (jiekou_path, output_dir / "keys.txt", output_dir / "ai_api_tokens.txt"):
            with target_path.open("a", encoding="utf-8") as handle:
                handle.write(api_key + "\n")
        task_logger.log(f"  [Jiekou] API key saved to {jiekou_path}")
    except Exception as exc:
        task_logger.log(f"  [Jiekou] API key write failed: {exc}", level="warning")



def _probe_proxy_ip(proxy_url: str) -> str:
    import requests as _req
    session = _req.Session()
    session.trust_env = False
    proxies = {"http": proxy_url, "https": proxy_url}
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://ip.decodo.com/json", "https://httpbin.org/ip"):
        try:
            resp = session.get(url, proxies=proxies, timeout=10)
            text = resp.text.strip()
            if "origin" in text:
                import json as _json
                text = _json.loads(text).get("origin", "").split(",")[0].strip()
            if text.startswith("{") and "ip" in text:
                import json as _json
                parsed = _json.loads(text)
                for key in ("ip", "origin"):
                    value = str(parsed.get(key) or "").strip()
                    if value:
                        text = value.split(",")[0].strip()
                        break
            if text and "." in text and len(text) < 64 and not text.startswith("{"):
                return text
        except Exception:
            continue
    return ""


def _probe_resin_ip(proxy_url: str) -> str:
    # 历史命名兼容：Resin/Decodo/BrightData 都用同一出口 IP 探测逻辑。
    return _probe_proxy_ip(proxy_url)


def _probe_airouter_proxy_routes(proxy_url: str) -> tuple[bool, str]:
    """AI-ROUTER 专用预检：IP 可用不代表目标站和 API 都可达。"""
    import requests as _req
    if not proxy_url:
        return False, "empty_proxy"
    session = _req.Session()
    session.trust_env = False
    proxies = {"http": proxy_url, "https": proxy_url}
    checks = (
        ("register", "https://ai-router.dev/register"),
        ("api_settings", "https://api.ai-router.dev/api/v1/settings/public"),
    )
    for label, url in checks:
        try:
            resp = session.get(
                url,
                proxies=proxies,
                timeout=20,
                headers={
                    "Accept": "text/html,application/json,*/*",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36",
                },
            )
            if int(getattr(resp, "status_code", 0) or 0) >= 500:
                return False, f"{label}_status_{resp.status_code}"
        except Exception as exc:
            return False, f"{label}_{type(exc).__name__}: {str(exc)[:120]}"
    return True, "ok"


def _probe_swarms_signup_page(proxy_url: str) -> bool:
    """用同一个代理探测 Swarms 注册页，IP 探测通过不代表目标站 CONNECT 可用。"""
    if not proxy_url:
        return True
    import requests as _req

    proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = _req.get(
            "https://swarms.world/signin/signup",
            proxies=proxies,
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        body = str(getattr(resp, "text", "") or "")
        status_code = int(getattr(resp, "status_code", 0) or 0)
        if status_code in (403, 429) and (
            "Vercel Security Checkpoint" in body
            or "Security Checkpoint" in body
            or "无法验证您的浏览器" in body
            or "正在验证您的浏览器" in body
        ):
            # 当前协议注册已经支持在 Vercel Security Checkpoint 下回退 Supabase Auth。
            # 这里的预检只判断目标 CONNECT 是否可达，不能把 checkpoint 当作代理不可用。
            return True
        return status_code < 400 and "signin" in str(getattr(resp, "url", "") or "signin")
    except Exception:
        return False


def _get_resin_proxy_url(task_platform: str = "", account: str = "") -> str | None:
    resolved = resolve_resin_proxy_config(
        {
            "resin_enabled": config_store.get("resin_enabled", "false"),
            "resin_proxy_url": config_store.get("resin_proxy_url", ""),
            "resin_scheme": config_store.get("resin_scheme", ""),
            "resin_host": config_store.get("resin_host", ""),
            "resin_port": config_store.get("resin_port", ""),
            "resin_token": config_store.get("resin_token", ""),
            "resin_default_platform": config_store.get("resin_default_platform", ""),
            "resin_platform_map": config_store.get("resin_platform_map", ""),
        },
        task_platform=task_platform,
        account=account,
        require_enabled=True,
    )
    return str(resolved.get("proxy_url") or "").strip() or None


def _is_truthy_config(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


def _read_int_config(key: str, default: int) -> int:
    try:
        parsed = int(config_store.get(key, default))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _get_scdn_runtime_proxy(task_platform: str = "", logger: "TaskLogger" | None = None) -> str | None:
    if not _is_truthy_config(config_store.get("scdn_runtime_enabled", "false")):
        return None

    protocol = str(config_store.get("scdn_runtime_protocol", "http") or "http").strip().lower() or "http"
    country_code = str(config_store.get("scdn_runtime_country_code", "") or "").strip().upper()
    count = _read_int_config("scdn_runtime_count", 10)
    validate_url = str(
        config_store.get("scdn_runtime_validate_url", "https://httpbin.org/ip")
        or "https://httpbin.org/ip"
    ).strip()
    validate_timeout_sec = _read_int_config("scdn_runtime_validate_timeout_sec", 8)
    cache_ttl_sec = _read_int_config("scdn_runtime_cache_ttl_sec", 120)
    cache_size = _read_int_config("scdn_runtime_cache_size", 20)

    proxy_url = _scdn_runtime_proxy_source.acquire_proxy(
        protocol=protocol,
        country_code=country_code,
        count=count,
        validate_url=validate_url,
        validate_timeout_sec=validate_timeout_sec,
        cache_ttl_sec=cache_ttl_sec,
        cache_size=cache_size,
        logger=logger,
    )
    if proxy_url and logger:
        scope = country_code or "ANY"
        logger.log(f"SCDN 运行时代理命中: {proxy_url} [{protocol.upper()} {scope}]")
    return proxy_url


def _get_subscription_proxy(logger: "TaskLogger | None" = None) -> str | None:
    if not _is_truthy_config(config_store.get("subscription_proxy_enabled", "false")):
        return None
    config = {"proxy_subscription": {
        "enabled": True,
        "url": str(config_store.get("subscription_proxy_url", "") or "").strip(),
        "kernel_path": str(config_store.get("subscription_proxy_kernel_path", "auto") or "auto").strip(),
        "listen": str(config_store.get("subscription_proxy_listen", "http://127.0.0.1:18080") or "http://127.0.0.1:18080").strip(),
        "strategy": str(config_store.get("subscription_proxy_strategy", "urltest") or "urltest").strip(),
        "check": str(config_store.get("subscription_proxy_check", "https://www.gstatic.com/generate_204") or "https://www.gstatic.com/generate_204").strip(),
        "check_interval": _read_int_config("subscription_proxy_check_interval", 30),
        "refresh_interval_min": _read_int_config("subscription_proxy_refresh_interval_min", 30),
        "max_nodes": _read_int_config("subscription_proxy_max_nodes", 50),
        "fetch_via_proxy": _is_truthy_config(config_store.get("subscription_proxy_fetch_via_proxy", "true")),
        "manual_node_tag": str(config_store.get("subscription_proxy_manual_node_tag", "") or "").strip(),
        "whitelist_tags": str(config_store.get("subscription_proxy_whitelist_tags", "") or "").strip(),
        "blacklist_tags": str(config_store.get("subscription_proxy_blacklist_tags", "") or "").strip(),
    }}
    if not config["proxy_subscription"]["url"]:
        return None
    try:
        listen_url = _subscription_proxy_manager.ensure_proxy(config)
        if listen_url:
            return listen_url
    except Exception as exc:
        if logger:
            logger.log(f"订阅代理启动失败: {exc}", level="warning")
    return None


def _build_platform_instance(platform_name: str, payload: dict[str, Any], logger: TaskLogger, resolved_proxy: str | None = None):
    from core.base_identity import normalize_identity_provider
    from core.base_mailbox import create_mailbox
    from core.base_phone import create_phone_provider

    normalized_proxy = normalize_proxy_url(resolved_proxy)
    executor_type = str(payload.get("executor_type", "protocol") or "protocol")
    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")
    extra = dict(payload.get("extra") or {})
    extra.setdefault("platform_name", platform_name)
    extra.setdefault("platform", platform_name)
    for key in (
        "registration.timeout",
        "registration.otp_timeout",
        "registration.otp_resend_interval",
        "registration.login_otp_timeout",
    ):
        if extra.get(key) not in (None, ""):
            continue
        stored = str(config_store.get(key, "") or "").strip()
        if stored:
            extra[key] = stored
    platform_cls = get(platform_name)
    default_mail_provider = str(getattr(platform_cls, "default_mail_provider", "") or "").strip()
    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
    if identity_provider == "mailbox" and default_mail_provider and not str(extra.get("mail_provider") or "").strip():
        extra["mail_provider"] = default_mail_provider

    config = RegisterConfig(
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=normalized_proxy,
        extra=extra,
    )
    mailbox = None
    if identity_provider == "mailbox" or str(extra.get("oauth_account_source") or "").strip().lower() in {"mailbox", "mail_provider", "provider"}:
        # 注入 platform 名，供 Google 账号池复用模式使用
        if "platform" not in extra:
            extra["platform"] = platform_name
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", "moemail"),
            extra=extra,
            proxy=normalized_proxy,
        )

    phone_provider = None
    if _is_truthy_config(extra.get("phone_provider_enabled", "false")):
        phone_provider = create_phone_provider(
            provider=extra.get("phone_provider", "haozhu"),
            extra=extra,
            proxy=normalized_proxy,
        )

    platform = platform_cls(config=config, mailbox=mailbox)
    if phone_provider is not None:
        platform.phone_provider = phone_provider
    if hasattr(platform, "set_logger"):
        platform.set_logger(logger.log)
    else:
        platform._log_fn = logger.log
    return platform


def _run_single_account_check(account_id: int, logger: TaskLogger | None = None) -> tuple[bool, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        plugin = get(model.platform)(config=RegisterConfig())
        account = build_platform_account(session, model)

    valid = plugin.check_valid(account)
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            summary_updates = {"checked_at": _utcnow_iso(), "valid": bool(valid)}
            patch_account_graph(
                session,
                model,
                lifecycle_status=None if valid else AccountStatus.INVALID.value,
                summary_updates=summary_updates,
            )
            session.add(model)
            session.commit()

    result = {"account_id": account_id, "valid": bool(valid), "platform": account.platform, "email": account.email}
    if logger:
        logger.log(f"{account.email}: {'有效' if valid else '失效'}")
    return valid, result


def execute_task(task_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        task_type = task.type
        payload = task.get_payload()

    logger = TaskLogger(task_id)
    logger.mark_running()

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        return

    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_ACCOUNT_CHECK: _execute_account_check_task,
        TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_account_check_all_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
        TASK_TYPE_BATCH_ACTION: _execute_batch_action_task,
        TASK_TYPE_GOOGLE_WORKSPACE_BULK_CREATE: _execute_google_workspace_bulk_create_task,
        TASK_TYPE_GOOGLE_WORKSPACE_BULK_DELETE: _execute_google_workspace_bulk_delete_task,
    }
    handler = handlers.get(task_type)
    if not handler:
        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
        return
    handler(payload, logger)


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.proxy_pool import proxy_pool

    parsed_lines = _resolve_register_lines(payload)
    if parsed_lines:
        seeds = parsed_lines
    else:
        seeds = _claim_inventory_register_lines(payload, logger)
        if seeds:
            logger.log(f"已从 {inventory_provider_label(_resolve_inventory_provider_key(payload))} 邮箱池领取 {len(seeds)} 个邮箱")
        else:
            inventory_provider_key = _resolve_inventory_provider_key(payload)
            if supports_mailbox_inventory(inventory_provider_key):
                logger.log(
                    f"{inventory_provider_label(inventory_provider_key)} 邮箱池为空，回退到当前 Provider 默认配置",
                    level="warning",
                )
            seeds = [None] * max(int(payload.get("count", 1) or 1), 1)
    count = len(seeds)
    concurrency = min(max(int(payload.get("concurrency", 1) or 1), 1), count)
    platform_name = str(payload.get("platform", ""))
    concurrency = _chain_resolve_concurrency(platform_name, concurrency, logger)
    payload_extra = dict(payload.get("extra") or {})
    chain_invite_state = _chain_init_state(platform_name, payload_extra)
    _initial_chain_code = _chain_state_initial_code(platform_name, chain_invite_state)
    if _chain_invite_enabled(platform_name) and _initial_chain_code:
        logger.log(f"{platform_name} 初始邀请码: {_initial_chain_code}")
    chain_invite_lock = threading.Lock()
    default_email = payload.get("email") or None
    default_password = payload.get("password") or None
    proxy = payload.get("proxy") or None
    inventory_repository = MailboxInventoryRepository()
    claimed_inventory_ids = [_seed_inventory_id(seed) for seed in seeds if _seed_inventory_id(seed) > 0]
    processed_inventory_ids: set[int] = set()
    processed_inventory_lock = threading.Lock()
    venice_proxy_successes = _venice_proxy_successes
    venice_proxy_successes_lock = _venice_proxy_successes_lock
    venice_resin_slot = _venice_resin_slot
    venice_resin_ip_successes = _venice_resin_ip_successes
    venice_resin_ip_banned = _venice_resin_ip_banned
    venice_resin_slot_to_ip = _venice_resin_slot_to_ip
    airouter_ip_successes = _airouter_ip_successes
    airouter_ip_inflight = _airouter_ip_inflight

    logger.set_progress(0, count)

    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    success = 0
    errors: list[str] = []

    def _is_airouter_platform() -> bool:
        return platform_name == "airouter"

    def _airouter_ip_used_count(ip: str) -> int:
        return int(airouter_ip_successes.get(ip, 0) or 0) if ip else 0

    def _claim_airouter_ip(ip: str, label: str = "AI-ROUTER") -> bool:
        """为一次 AI-ROUTER 注册预占出口 IP；同一 IP 最多成功 3 个账号。"""
        if not ip:
            return False
        with venice_proxy_successes_lock:
            used = _airouter_ip_used_count(ip)
            if used >= _AIROUTER_IP_SUCCESS_LIMIT:
                logger.log(
                    f"{label}: IP {ip} already used for AI-ROUTER "
                    f"({used}/{_AIROUTER_IP_SUCCESS_LIMIT}), skipping"
                )
                return False
            if ip in airouter_ip_inflight:
                logger.log(f"{label}: IP {ip} is AI-ROUTER-inflight, skipping")
                return False
            airouter_ip_inflight.add(ip)
            return True

    def _release_airouter_ip(ip: str) -> None:
        if not ip:
            return
        with venice_proxy_successes_lock:
            airouter_ip_inflight.discard(ip)

    def _record_airouter_ip_success(ip: str) -> None:
        if not ip:
            return
        with venice_proxy_successes_lock:
            airouter_ip_inflight.discard(ip)
            current = min(_airouter_ip_used_count(ip) + 1, _AIROUTER_IP_SUCCESS_LIMIT)
            airouter_ip_successes[ip] = current
        logger.log(f"AI-ROUTER IP {ip} ({current}/{_AIROUTER_IP_SUCCESS_LIMIT})")
        if current >= _AIROUTER_IP_SUCCESS_LIMIT:
            logger.log(f"AI-ROUTER IP {ip} reached {_AIROUTER_IP_SUCCESS_LIMIT}/{_AIROUTER_IP_SUCCESS_LIMIT}, will use new IP next")

    def _mark_airouter_ip_exhausted(ip: str, reason: str = "") -> None:
        if not ip:
            return
        with venice_proxy_successes_lock:
            airouter_ip_inflight.discard(ip)
            airouter_ip_successes[ip] = _AIROUTER_IP_SUCCESS_LIMIT
        suffix = f" ({reason})" if reason else ""
        logger.log(f"AI-ROUTER IP {ip} exhausted ({_AIROUTER_IP_SUCCESS_LIMIT}/{_AIROUTER_IP_SUCCESS_LIMIT}){suffix}")

    def _is_airouter_balance_insufficient_error(error: str) -> bool:
        return "注册余额不足" in error or ("balance=" in error and "required>=" in error)

    def _is_retryable_proxy_error(error: str) -> bool:
        if not error:
            return False
        text = error.lower()
        retry_tokens = (
            "connectionreseterror",
            "connection reset",
            "connection aborted",
            "远程主机强迫关闭",
            "10054",
            "proxyerror",
            "502 bad gateway",
            "tunnel connection failed",
            "unexpected_eof_while_reading_eof",
            "unexpected_eof_while_reading",
            "sslerror",
            "route precheck failed",
            "net::err_connection_reset",
            "net::err_tunnel_connection_failed",
        )
        return any(token in text for token in retry_tokens)

    def _is_airouter_retryable_proxy_error(error: str) -> bool:
        return _is_retryable_proxy_error(error)

    def _is_lemondata_retryable_proxy_error(error: str) -> bool:
        return _is_retryable_proxy_error(error)

    def _mark_inventory_processed(item_id: int) -> None:
        if item_id <= 0:
            return
        with processed_inventory_lock:
            processed_inventory_ids.add(item_id)

    def _release_unprocessed_inventory(note: str) -> None:
        pending_ids: list[int] = []
        with processed_inventory_lock:
            for item_id in claimed_inventory_ids:
                if item_id > 0 and item_id not in processed_inventory_ids:
                    pending_ids.append(item_id)
        if pending_ids:
            inventory_repository.reset_many(pending_ids, note=note)

    def _pick_resin_proxy_with_ip_check() -> tuple[str | None, int, str]:
        max_attempts = 20
        for _ in range(max_attempts):
            with venice_proxy_successes_lock:
                slot = venice_resin_slot[0]
                venice_resin_slot[0] = slot + 1
            account = f"vs{slot}"
            proxy_url = _get_resin_proxy_url(platform_name, account=account)
            if not proxy_url:
                return None, -1, ""
            ip = _probe_proxy_ip(proxy_url)
            if not ip:
                logger.log(f"Resin slot vs{slot}: IP probe failed, skipping")
                continue
            if _is_airouter_platform():
                routes_ok, route_reason = _probe_airouter_proxy_routes(proxy_url)
                if not routes_ok:
                    logger.log(f"Resin slot vs{slot}: AI-ROUTER route precheck failed ({route_reason}), skipping", level="warning")
                    continue
            with venice_proxy_successes_lock:
                venice_resin_slot_to_ip[slot] = ip
                if _is_airouter_platform():
                    if not _claim_airouter_ip(ip, f"Resin slot vs{slot}"):
                        continue
                elif platform_name == "atxp":
                    if ip in _atxp_ip_banned:
                        logger.log(f"Resin slot vs{slot}: IP {ip} is ATXP-banned, skipping")
                        continue
                    atxp_used = _atxp_ip_successes.get(ip, 0)
                    if atxp_used >= _ATXP_IP_SUCCESS_LIMIT:
                        logger.log(f"Resin slot vs{slot}: IP {ip} already used for ATXP ({atxp_used}/{_ATXP_IP_SUCCESS_LIMIT}), skipping")
                        continue
                else:
                    if ip in venice_resin_ip_banned:
                        logger.log(f"Resin slot vs{slot}: IP {ip} is banned, skipping")
                        continue
                    used = venice_resin_ip_successes.get(ip, 0)
                    if used >= _VENICE_PROXY_SUCCESS_LIMIT:
                        logger.log(f"Resin slot vs{slot}: IP {ip} already used {used}/{_VENICE_PROXY_SUCCESS_LIMIT}, skipping")
                        continue
            if _is_airouter_platform():
                logger.log(f"Resin slot vs{slot}: AI-ROUTER IP {ip} ({_airouter_ip_used_count(ip)}/{_AIROUTER_IP_SUCCESS_LIMIT})")
            elif platform_name == "atxp":
                logger.log(f"Resin slot vs{slot}: IP {ip} ({_atxp_ip_successes.get(ip, 0)}/{_ATXP_IP_SUCCESS_LIMIT})")
            else:
                logger.log(f"Resin slot vs{slot}: IP {ip} ({venice_resin_ip_successes.get(ip, 0)}/{_VENICE_PROXY_SUCCESS_LIMIT})")
            return proxy_url, slot, ip
        logger.log(f"Resin: exhausted {max_attempts} slots, no clean IP found", level="warning")
        return None, -1, ""

    def _record_resin_ip_success(ip: str) -> None:
        if not ip:
            return
        with venice_proxy_successes_lock:
            venice_resin_ip_successes[ip] = venice_resin_ip_successes.get(ip, 0) + 1
            current = venice_resin_ip_successes[ip]
        if current >= _VENICE_PROXY_SUCCESS_LIMIT:
            logger.log(f"Resin IP {ip} reached {_VENICE_PROXY_SUCCESS_LIMIT} successes, will use new IP next")

    def _ban_resin_ip(ip: str) -> None:
        if not ip:
            return
        with venice_proxy_successes_lock:
            if ip not in venice_resin_ip_banned:
                venice_resin_ip_banned.add(ip)
                logger.log(f"Resin IP {ip} banned (node quality issue)")

    venice_decodo_slot = _venice_decodo_slot
    venice_decodo_port_to_ip = _venice_decodo_port_to_ip

    def _get_decodo_proxy_url(slot: int = 0) -> str | None:
        from core.decodo_proxy import resolve_decodo_proxy_config
        keys = ("decodo_enabled", "decodo_host", "decodo_port",
                "decodo_username", "decodo_password", "decodo_port_base")
        resolved = resolve_decodo_proxy_config(
            {k: config_store.get(k, "") for k in keys},
            slot=slot,
            require_enabled=True,
        )
        return str(resolved.get("proxy_url") or "").strip() or None

    def _pick_decodo_proxy_with_ip_check() -> tuple[str | None, int, str]:
        max_attempts = 20
        consecutive_probe_failures = 0
        for _ in range(max_attempts):
            with venice_proxy_successes_lock:
                slot = venice_decodo_slot[0] + 1
                venice_decodo_slot[0] = slot
            proxy_url = _get_decodo_proxy_url(slot=slot)
            if not proxy_url:
                return None, -1, ""
            cached_ip = venice_decodo_port_to_ip.get(slot)
            ip = cached_ip or _probe_resin_ip(proxy_url)
            if not ip:
                consecutive_probe_failures += 1
                logger.log(f"Decodo slot {slot}: IP probe failed ({consecutive_probe_failures}/3), skipping")
                if consecutive_probe_failures >= 3:
                    logger.log("Decodo: 连续 3 次 IP 探测失败，代理可能不可用，跳过", level="warning")
                    return None, -1, ""
                continue
            consecutive_probe_failures = 0
            if _is_airouter_platform():
                routes_ok, route_reason = _probe_airouter_proxy_routes(proxy_url)
                if not routes_ok:
                    logger.log(f"Decodo slot {slot}: AI-ROUTER route precheck failed ({route_reason}), skipping", level="warning")
                    continue
            with venice_proxy_successes_lock:
                venice_decodo_port_to_ip[slot] = ip
                if _is_airouter_platform():
                    if not _claim_airouter_ip(ip, f"Decodo slot {slot}"):
                        continue
                elif platform_name == "atxp":
                    if ip in _atxp_ip_banned:
                        logger.log(f"Decodo slot {slot}: IP {ip} is ATXP-banned, skipping")
                        continue
                    atxp_used = _atxp_ip_successes.get(ip, 0)
                    if atxp_used >= _ATXP_IP_SUCCESS_LIMIT:
                        logger.log(f"Decodo slot {slot}: IP {ip} already used for ATXP ({atxp_used}/{_ATXP_IP_SUCCESS_LIMIT}), skipping")
                        continue
                else:
                    if ip in venice_resin_ip_banned:
                        logger.log(f"Decodo slot {slot}: IP {ip} is banned, skipping")
                        continue
                    used = venice_resin_ip_successes.get(ip, 0)
                    if used >= _VENICE_PROXY_SUCCESS_LIMIT:
                        logger.log(f"Decodo slot {slot}: IP {ip} already used {used}/{_VENICE_PROXY_SUCCESS_LIMIT}, skipping")
                        continue
            if _is_airouter_platform():
                logger.log(f"Decodo slot {slot}: AI-ROUTER IP {ip} ({_airouter_ip_used_count(ip)}/{_AIROUTER_IP_SUCCESS_LIMIT})")
            elif platform_name == "atxp":
                logger.log(f"Decodo slot {slot}: IP {ip} ({_atxp_ip_successes.get(ip, 0)}/{_ATXP_IP_SUCCESS_LIMIT})")
            else:
                logger.log(f"Decodo slot {slot}: IP {ip} ({venice_resin_ip_successes.get(ip, 0)}/{_VENICE_PROXY_SUCCESS_LIMIT})")
            return proxy_url, slot, ip
        logger.log(f"Decodo: exhausted {max_attempts} slots, no clean IP found", level="warning")
        return None, -1, ""

    venice_brightdata_slot = _venice_brightdata_slot
    venice_brightdata_session_to_ip = _venice_brightdata_session_to_ip

    def _get_brightdata_proxy_url(slot: int = 0) -> str | None:
        from core.brightdata_proxy import resolve_brightdata_proxy_config
        keys = ("brightdata_enabled", "brightdata_host", "brightdata_port",
                "brightdata_username", "brightdata_password")
        resolved = resolve_brightdata_proxy_config(
            {k: config_store.get(k, "") for k in keys},
            slot=slot,
            require_enabled=True,
        )
        return str(resolved.get("proxy_url") or "").strip() or None

    def _pick_brightdata_proxy_with_ip_check() -> tuple[str | None, int, str]:
        max_attempts = 20
        consecutive_probe_failures = 0
        for _ in range(max_attempts):
            with venice_proxy_successes_lock:
                slot = venice_brightdata_slot[0] + 1
                venice_brightdata_slot[0] = slot
            proxy_url = _get_brightdata_proxy_url(slot=slot)
            if not proxy_url:
                return None, -1, ""
            cached_ip = venice_brightdata_session_to_ip.get(slot)
            ip = cached_ip or _probe_resin_ip(proxy_url)
            if not ip:
                consecutive_probe_failures += 1
                logger.log(f"BrightData slot {slot}: IP probe failed ({consecutive_probe_failures}/3), skipping")
                if consecutive_probe_failures >= 3:
                    logger.log("BrightData: 连续 3 次 IP 探测失败，代理可能不可用，跳过", level="warning")
                    return None, -1, ""
                continue
            consecutive_probe_failures = 0
            if _is_airouter_platform():
                routes_ok, route_reason = _probe_airouter_proxy_routes(proxy_url)
                if not routes_ok:
                    logger.log(f"BrightData slot {slot}: AI-ROUTER route precheck failed ({route_reason}), skipping", level="warning")
                    continue
            with venice_proxy_successes_lock:
                venice_brightdata_session_to_ip[slot] = ip
                if _is_airouter_platform():
                    if not _claim_airouter_ip(ip, f"BrightData slot {slot}"):
                        continue
                elif platform_name == "atxp":
                    if ip in _atxp_ip_banned:
                        logger.log(f"BrightData slot {slot}: IP {ip} is ATXP-banned, skipping")
                        continue
                    atxp_used = _atxp_ip_successes.get(ip, 0)
                    if atxp_used >= _ATXP_IP_SUCCESS_LIMIT:
                        logger.log(f"BrightData slot {slot}: IP {ip} already used for ATXP ({atxp_used}/{_ATXP_IP_SUCCESS_LIMIT}), skipping")
                        continue
                else:
                    if ip in venice_resin_ip_banned:
                        logger.log(f"BrightData slot {slot}: IP {ip} is banned, skipping")
                        continue
                    used = venice_resin_ip_successes.get(ip, 0)
                    if used >= _VENICE_PROXY_SUCCESS_LIMIT:
                        logger.log(f"BrightData slot {slot}: IP {ip} already used {used}/{_VENICE_PROXY_SUCCESS_LIMIT}, skipping")
                        continue
            if _is_airouter_platform():
                logger.log(f"BrightData slot {slot}: AI-ROUTER IP {ip} ({_airouter_ip_used_count(ip)}/{_AIROUTER_IP_SUCCESS_LIMIT})")
            elif platform_name == "atxp":
                logger.log(f"BrightData slot {slot}: IP {ip} ({_atxp_ip_successes.get(ip, 0)}/{_ATXP_IP_SUCCESS_LIMIT})")
            else:
                logger.log(f"BrightData slot {slot}: IP {ip} ({venice_resin_ip_successes.get(ip, 0)}/{_VENICE_PROXY_SUCCESS_LIMIT})")
            return proxy_url, slot, ip
        logger.log(f"BrightData: exhausted {max_attempts} slots, no clean IP found", level="warning")
        return None, -1, ""

    def _resolve_task_proxy(worker_index: int = 0) -> tuple[str | None, str, str]:
        if proxy:
            if _is_airouter_platform():
                payload_ip = _probe_proxy_ip(proxy)
                if not payload_ip:
                    raise RuntimeError("任务内代理 IP 探测失败，AI-ROUTER 无法做 IP 黑名单判断，请更换代理或使用代理池")
                routes_ok, route_reason = _probe_airouter_proxy_routes(proxy)
                if not routes_ok:
                    raise RuntimeError(f"任务内代理 AI-ROUTER route precheck failed: {route_reason}")
                if not _claim_airouter_ip(payload_ip, "任务内代理"):
                    raise RuntimeError(f"任务内代理 IP {payload_ip} 已被 AI-ROUTER 拉黑或正在注册，请更换代理")
                logger.log(f"代理来源: 任务内代理 IP {payload_ip or '-'}")
                return proxy, "payload", payload_ip
            logger.log("代理来源: 任务内代理")
            return proxy, "payload", ""
        resin_enabled = _is_truthy_config(config_store.get("resin_enabled", "false"))
        if resin_enabled:
            if platform_name in {"venice", "airouter"}:
                resin_proxy, _slot, resin_ip = _pick_resin_proxy_with_ip_check()
            else:
                resin_proxy, resin_ip = None, ""
                max_resin_attempts = 8 if platform_name in {"swarms", "lemondata"} else 1
                resin_ip_probe_failures = 0
                for attempt in range(max_resin_attempts):
                    # Swarms 对同一 Resin 会话/IP 限制明显；无论是否并发都使用独立 session 用户名 Default.vsN，
                    # 并在注册前直连目标注册页预检，避免 api.ipify 可用但 swarms.world CONNECT 504。
                    resin_account = f"vs{worker_index + attempt}" if platform_name in {"swarms", "lemondata"} else ""
                    candidate_proxy = _get_resin_proxy_url(platform_name, account=resin_account)
                    candidate_ip = _probe_proxy_ip(candidate_proxy) if candidate_proxy else ""
                    if candidate_proxy and not candidate_ip:
                        resin_ip_probe_failures += 1
                        if platform_name == "swarms":
                            logger.log(
                                f"Resin session {resin_account or 'Default'} IP probe failed ({resin_ip_probe_failures}/3), skipping",
                                level="warning",
                            )
                            if resin_ip_probe_failures >= 3:
                                logger.log("Swarms Resin 连续 3 次 IP 探测失败，跳过 Resin，尝试后续代理来源/直连", level="warning")
                                break
                        else:
                            logger.log("Resin IP probe failed, skipping", level="warning")
                        continue
                    resin_ip_probe_failures = 0
                    if platform_name == "lemondata" and candidate_ip in venice_resin_ip_banned:
                        logger.log(f"LemonData Resin session {resin_account or 'Default'} IP {candidate_ip} is banned, skipping")
                        continue
                    if platform_name == "lemondata" and venice_resin_ip_successes.get(candidate_ip, 0) >= _VENICE_PROXY_SUCCESS_LIMIT:
                        used = venice_resin_ip_successes.get(candidate_ip, 0)
                        logger.log(f"LemonData Resin session {resin_account or 'Default'} IP {candidate_ip} already used {used}/{_VENICE_PROXY_SUCCESS_LIMIT}, skipping")
                        continue
                    if platform_name == "swarms" and candidate_proxy and not _probe_swarms_signup_page(candidate_proxy):
                        logger.log(f"Swarms 注册页预检失败，跳过 Resin session {resin_account or 'Default'}", level="warning")
                        continue
                    resin_proxy, resin_ip = candidate_proxy, candidate_ip
                    break
            if resin_proxy:
                if resin_ip:
                    logger.log(f"Resin IP {resin_ip}")
                logger.log("代理来源: Resin")
                return resin_proxy, "resin", resin_ip
            if _is_airouter_platform():
                raise RuntimeError("AI-ROUTER Resin 未命中可用 IP，已禁止回退其它代理或直连；请切换 Resin session/IP")
            if platform_name == "lemondata":
                raise RuntimeError("LemonData Resin 未命中可用 IP，已禁止回退其它代理或直连；请检查 Resin 配置或切换 Resin session/IP")
        if _is_truthy_config(config_store.get("decodo_enabled", "false")):
            decodo_proxy, _dslot, decodo_ip = _pick_decodo_proxy_with_ip_check()
            if decodo_proxy:
                return decodo_proxy, "decodo", decodo_ip
        if _is_truthy_config(config_store.get("brightdata_enabled", "false")):
            bd_proxy, _bd_slot, bd_ip = _pick_brightdata_proxy_with_ip_check()
            if bd_proxy:
                return bd_proxy, "brightdata", bd_ip
        scdn_runtime_enabled = _is_truthy_config(config_store.get("scdn_runtime_enabled", "false"))
        if scdn_runtime_enabled:
            logger.log("SCDN 运行时来源已启用，开始拉取可用代理")
        scdn_proxy = _get_scdn_runtime_proxy(platform_name, logger)
        if scdn_proxy:
            if _is_airouter_platform():
                scdn_ip = _probe_proxy_ip(scdn_proxy)
                if not scdn_ip:
                    logger.log("SCDN 运行时来源 IP 探测失败，跳过", level="warning")
                else:
                    routes_ok, route_reason = _probe_airouter_proxy_routes(scdn_proxy)
                    if not routes_ok:
                        logger.log(f"SCDN 运行时来源 AI-ROUTER route precheck failed ({route_reason}), skipping", level="warning")
                    elif _claim_airouter_ip(scdn_ip, "SCDN 运行时来源"):
                        logger.log(f"代理来源: SCDN 运行时来源 IP {scdn_ip or '-'}")
                        return scdn_proxy, "scdn", scdn_ip
            else:
                logger.log("代理来源: SCDN 运行时来源")
                return scdn_proxy, "scdn", ""
        if scdn_runtime_enabled:
            logger.log("SCDN 运行时来源未命中，尝试订阅代理")
        subscription_proxy = _get_subscription_proxy(logger)
        if subscription_proxy:
            if _is_airouter_platform():
                subscription_ip = _probe_proxy_ip(subscription_proxy)
                if not subscription_ip:
                    logger.log("订阅代理 IP 探测失败，跳过", level="warning")
                else:
                    routes_ok, route_reason = _probe_airouter_proxy_routes(subscription_proxy)
                    if not routes_ok:
                        logger.log(f"订阅代理 AI-ROUTER route precheck failed ({route_reason}), skipping", level="warning")
                    elif _claim_airouter_ip(subscription_ip, "订阅代理"):
                        logger.log(f"代理来源: 订阅代理 (sing-box) IP {subscription_ip or '-'}")
                        return subscription_proxy, "subscription", subscription_ip
            else:
                logger.log("代理来源: 订阅代理 (sing-box)")
                return subscription_proxy, "subscription", ""
        if platform_name != "venice":
            if _is_airouter_platform():
                excluded_urls: set[str] = set()
                for _attempt in range(20):
                    pool_proxy = proxy_pool.get_next(exclude_urls=excluded_urls)
                    if not pool_proxy:
                        break
                    pool_ip = _probe_proxy_ip(pool_proxy)
                    if not pool_ip:
                        logger.log("后端代理池 IP 探测失败，跳过", level="warning")
                        excluded_urls.add(pool_proxy)
                        proxy_pool.report_fail(pool_proxy)
                        continue
                    routes_ok, route_reason = _probe_airouter_proxy_routes(pool_proxy)
                    if not routes_ok:
                        logger.log(f"后端代理池 AI-ROUTER route precheck failed ({route_reason}), skipping", level="warning")
                        excluded_urls.add(pool_proxy)
                        continue
                    if not _claim_airouter_ip(pool_ip, "后端代理池"):
                        excluded_urls.add(pool_proxy)
                        continue
                    logger.log(f"代理来源: 后端代理池 IP {pool_ip}")
                    return pool_proxy, "pool", pool_ip
                if scdn_runtime_enabled:
                    raise RuntimeError("SCDN 已启用，但未命中可用代理，且后端代理池无 AI-ROUTER 可用 IP")
                raise RuntimeError("AI-ROUTER 未命中可用代理，已禁止回退直连；请切换 Resin session/IP 或配置可用代理")
            pool_proxy = proxy_pool.get_next()
            if pool_proxy:
                logger.log("代理来源: 后端代理池")
            elif scdn_runtime_enabled:
                raise RuntimeError("SCDN 已启用，但未命中可用代理，且后端代理池为空")
            return pool_proxy, "pool", ""
        with venice_proxy_successes_lock:
            excluded_urls = {
                url for url, success_count in venice_proxy_successes.items()
                if success_count >= _VENICE_PROXY_SUCCESS_LIMIT
            }
        pool_proxy = proxy_pool.get_next(exclude_urls=excluded_urls)
        if pool_proxy:
            logger.log("代理来源: 后端代理池")
        elif scdn_runtime_enabled:
            raise RuntimeError("SCDN 已启用，但未命中可用代理，且后端代理池为空")
        return pool_proxy, "pool", ""

    def _record_venice_proxy_success(resolved_proxy: str) -> None:
        if platform_name != "venice" or not resolved_proxy:
            return
        with venice_proxy_successes_lock:
            success_count = venice_proxy_successes.get(resolved_proxy, 0) + 1
            venice_proxy_successes[resolved_proxy] = success_count
        if success_count == _VENICE_PROXY_SUCCESS_LIMIT:
            logger.log(
                f"Venice 代理成功次数已达上限({_VENICE_PROXY_SUCCESS_LIMIT})，后续不再使用: {resolved_proxy}"
            )

    def _do_one(index: int) -> bool | str:
        seed = seeds[index] if index < len(seeds) else None
        inventory_item_id = _seed_inventory_id(seed)
        inventory_metadata = _seed_inventory_metadata(seed)
        inventory_provider_key = str(dict(getattr(seed, "extra", {}) or {}).get("_inventory", {}).get("provider_key") or "")
        if logger.is_cancel_requested():
            if inventory_item_id > 0:
                inventory_repository.update_item(
                    inventory_item_id,
                    status="unused",
                    note="任务已取消，邮箱已回收",
                    last_error="",
                    task_id=logger.task_id,
                    platform=platform_name,
                )
                _mark_inventory_processed(inventory_item_id)
            return "__cancel_requested__"
        extra = _merge_register_extra(dict(payload.get("extra") or {}), dict(getattr(seed, "extra", {}) or {}))
        if _chain_invite_enabled(platform_name):
            with chain_invite_lock:
                extra = _chain_apply_invite(platform_name, extra, chain_invite_state)
        extra = _sanitize_parallel_oauth_browser_extra(extra, concurrency=concurrency)
        email = (getattr(seed, "email", "") or default_email or None)
        password = (getattr(seed, "password", "") or default_password or None)
        generated_outlook_alias = ""
        outlook_alias_parent_email = ""
        if inventory_item_id > 0 and email:
            original_email = str(email)
            target_email = _build_inventory_target_email(
                original_email,
                extra,
                inventory_metadata,
                provider_key=inventory_provider_key,
            )
            if target_email and target_email != email:
                current_count = int(inventory_metadata.get("successful_registrations", 0) or 0)
                if inventory_provider_key == "outlook_token":
                    outlook_alias_parent_email = str(
                        extra.get("outlook_email")
                        or inventory_metadata.get("outlook_login_email")
                        or inventory_metadata.get("alias_parent_email")
                        or original_email
                    ).strip()
                    generated_outlook_alias = str(target_email).strip()
                    extra["outlook_registration_email"] = generated_outlook_alias
                    extra["outlook_alias_email"] = generated_outlook_alias
                    extra["outlook_alias_parent_email"] = outlook_alias_parent_email
                    extra.setdefault("overview", {})["alias_parent_email"] = outlook_alias_parent_email
                    logger.log(f"Outlook 别名注册已启用，本次使用别名邮箱: {target_email}")
                elif current_count > 0:
                    logger.log(
                        f"邮箱池复用: {email} 已成功 {current_count}/{_INVENTORY_REUSE_LIMIT} 次，本次改用别名 {target_email}"
                    )
                else:
                    logger.log(f"自动注册已启用默认别名策略，本次使用别名邮箱: {target_email}")
                email = target_email
        raw_mail_provider = str(extra.get("mail_provider") or "").strip()
        if "," in raw_mail_provider:
            candidates = [p.strip() for p in raw_mail_provider.split(",") if p.strip()]
            if candidates:
                chosen = random.choice(candidates)
                extra["mail_provider"] = chosen
                logger.log(f"邮箱来源随机选择: {chosen}（共 {len(candidates)} 个来源）")
        seed_payload = {
            **payload,
            "email": email,
            "password": password,
            "extra": extra,
        }
        max_proxy_attempts = 1
        if platform_name == "airouter":
            try:
                max_proxy_attempts = max(1, int(extra.get("airouter_proxy_retry_attempts") or 8))
            except Exception:
                max_proxy_attempts = 8
        elif platform_name == "lemondata":
            try:
                max_proxy_attempts = max(1, int(extra.get("lemondata_proxy_retry_attempts") or extra.get("proxy_retry_attempts") or 8))
            except Exception:
                max_proxy_attempts = 8
        for proxy_attempt in range(1, max_proxy_attempts + 1):
            resolved_proxy, proxy_source, resin_ip = None, "", ""
            try:
                resolved_proxy, proxy_source, resin_ip = _resolve_task_proxy(index)
                if platform_name == "airouter" and resin_ip:
                    # AI-ROUTER 前端注册请求会携带 webrtc_client_ip；这里用当前代理出口 IP 补齐，
                    # 保证 Turnstile、发码、注册体里的 IP 线索一致。
                    seed_payload = {
                        **seed_payload,
                        "extra": {
                            **dict(seed_payload.get("extra") or {}),
                            "airouter_webrtc_client_ip": resin_ip,
                            "webrtc_client_ip": resin_ip,
                        },
                    }
                    logger.log(f"AI-ROUTER webrtc_client_ip={resin_ip}")
                platform = _build_platform_instance(platform_name, seed_payload, logger, resolved_proxy=resolved_proxy)
                display_email = email or "(auto)"
                logger.log(f"开始注册第 {index + 1}/{count} 个账号: {display_email}")
                if resolved_proxy:
                    logger.log(f"使用代理: {resolved_proxy}")
                account = platform.register(email=email, password=password)
                reserved_google_email = str((account.extra or {}).get("google_pool_reserved_email") or "").strip()
                if reserved_google_email:
                    extra["_reserved_google_pool_email"] = reserved_google_email
                save_account(account)
                if _chain_invite_enabled(platform_name):
                    with chain_invite_lock:
                        recorded_invite_code = _chain_record_success(platform_name, account, chain_invite_state)
                    if recorded_invite_code:
                        logger.log(f"{platform_name} 邀请码入池供后续号链式: {recorded_invite_code}")
                if str(extra.get("oauth_account_source") or "").strip().lower() in {"mailbox", "mail_provider", "provider"}:
                    try:
                        from core.google_account_pool import GoogleAccountPool
                        pool_email = account.email or email or ""
                        GoogleAccountPool().mark_registered(pool_email, platform_name)
                        extra["_reserved_google_pool_email"] = ""
                    except Exception:
                        pass
                if resolved_proxy and proxy_source == "pool":
                    proxy_pool.report_success(resolved_proxy)
                    _record_venice_proxy_success(resolved_proxy)
                if platform_name == "airouter" and resin_ip:
                    _record_airouter_ip_success(resin_ip)
                if proxy_source in ("resin", "decodo", "brightdata") and platform_name != "airouter":
                    _record_resin_ip_success(resin_ip)
                    if platform_name == "atxp" and resin_ip:
                        with venice_proxy_successes_lock:
                            _atxp_ip_successes[resin_ip] = _atxp_ip_successes.get(resin_ip, 0) + 1
                if inventory_item_id > 0:
                    registered_email = str(getattr(account, "email", "") or email or "")
                    inventory_repository.mark_registration_success(
                        inventory_item_id,
                        registered_email=registered_email,
                        task_id=logger.task_id,
                        platform=platform_name,
                    )
                    if inventory_provider_key == "outlook_token":
                        try:
                            _upsert_successful_outlook_alias(
                                inventory_repository=inventory_repository,
                                seed=seed,
                                extra=extra,
                                inventory_metadata=inventory_metadata,
                                platform_name=platform_name,
                                logger=logger,
                                registered_email=registered_email,
                                generated_outlook_alias=generated_outlook_alias,
                                preferred_parent_email=outlook_alias_parent_email,
                            )
                        except Exception as alias_exc:
                            logger.log(f"  [邮箱池] Outlook 别名入池失败: {alias_exc}", level="warning")
                    _mark_inventory_processed(inventory_item_id)
                logger.record_success()
                logger.log(f"[OK] 注册成功: {account.email}")
                _save_task_log(platform_name, account.email, "success")
                _auto_upload_cpa(logger, account)
                _auto_import_codebanana2api(logger, account)
                _auto_import_anuma2api(logger, account)
                _auto_import_enter2api(logger, account)
                _auto_import_blendspace2api(logger, account)
                _auto_export_fireworks_key(logger, account)
                _auto_export_gettoken_key(logger, account)
                _auto_export_lemondata_key(logger, account)
                _auto_export_zo_key(logger, account)
                _auto_push_thesys_twoapi(logger, account, extra)
                _auto_export_swarms_key(logger, account)
                _auto_export_anycap_key(logger, account)
                _auto_export_thesys_key(logger, account)
                _auto_export_featherless_key(logger, account)
                _auto_export_jiekou_key(logger, account)
                cashier_url = (account.extra or {}).get("cashier_url", "")
                if cashier_url:
                    logger.log(f"  [升级链接] {cashier_url}")
                    logger.add_cashier_url(cashier_url)
                return True
            except Exception as exc:
                error = str(exc)
                airouter_balance_limited = platform_name == "airouter" and _is_airouter_balance_insufficient_error(error)
                airouter_retryable_proxy_error = (
                    platform_name == "airouter"
                    and not airouter_balance_limited
                    and _is_airouter_retryable_proxy_error(error)
                    and proxy_attempt < max_proxy_attempts
                )
                if airouter_retryable_proxy_error:
                    if resin_ip:
                        _release_airouter_ip(resin_ip)
                    if resolved_proxy and proxy_source == "pool":
                        proxy_pool.report_fail(resolved_proxy)
                    logger.log(
                        f"AI-ROUTER 代理连接异常，换 IP 重试 {proxy_attempt + 1}/{max_proxy_attempts}: {error}",
                        level="warning",
                    )
                    time.sleep(min(5, 1 + proxy_attempt))
                    continue
                lemondata_retryable_proxy_error = (
                    platform_name == "lemondata"
                    and _is_lemondata_retryable_proxy_error(error)
                    and proxy_attempt < max_proxy_attempts
                )
                if lemondata_retryable_proxy_error:
                    if proxy_source in ("resin", "decodo", "brightdata"):
                        _ban_resin_ip(resin_ip)
                    if resolved_proxy and proxy_source == "pool":
                        proxy_pool.report_fail(resolved_proxy)
                    logger.log(
                        f"LemonData 代理连接异常，换 IP 重试 {proxy_attempt + 1}/{max_proxy_attempts}: {error}",
                        level="warning",
                    )
                    time.sleep(min(5, 1 + proxy_attempt))
                    continue
                if airouter_balance_limited:
                    failure_ip = resin_ip
                    if not failure_ip and resolved_proxy:
                        failure_ip = _probe_proxy_ip(resolved_proxy)
                    if failure_ip:
                        _mark_airouter_ip_exhausted(failure_ip, "insufficient_balance")
                    else:
                        logger.log("AI-ROUTER 余额不足失败，但未探测到出口 IP，无法加入 IP 黑名单", level="warning")
                elif platform_name == "airouter" and resin_ip:
                    _release_airouter_ip(resin_ip)
                if proxy_source in ("resin", "decodo", "brightdata") and ("did not reach expected credits" in error or (platform_name == "lemondata" and "too_many_registrations" in error)):
                    _ban_resin_ip(resin_ip)
                if platform_name == "atxp" and "account_restricted: fraud_blocked" in error and resin_ip:
                    with venice_proxy_successes_lock:
                        if resin_ip not in _atxp_ip_banned:
                            _atxp_ip_banned.add(resin_ip)
                            logger.log(f"ATXP IP {resin_ip} banned (fraud_blocked)")
                if resolved_proxy and proxy_source == "pool" and not airouter_balance_limited:
                    proxy_pool.report_fail(resolved_proxy)
                result = getattr(exc, "result", None)
                # 并发注册时异常对象可能携带其它 worker 的结果/邮箱；失败归档必须优先使用当前 worker 的邮箱。
                persist_email = str(email or "").strip() or str(getattr(result, "email", "") or "").strip()
                reserved_google_email = str(extra.get("_reserved_google_pool_email") or persist_email or "").strip()
                if reserved_google_email and str(extra.get("oauth_account_source") or "").strip().lower() in {"mailbox", "mail_provider", "provider"}:
                    try:
                        from core.google_account_pool import GoogleAccountPool
                        GoogleAccountPool().release(reserved_google_email, platform_name)
                        extra["_reserved_google_pool_email"] = ""
                    except Exception:
                        pass
                lifecycle_status = _derive_partial_lifecycle(result, error)
                if persist_email:
                    try:
                        _persist_registration_snapshot(
                            platform=platform_name,
                            email=persist_email,
                            password=str(password or getattr(result, "password", "") or ""),
                            lifecycle_status=lifecycle_status,
                            extra=extra,
                            error=error,
                            result=result,
                        )
                        logger.log(f"  [状态] 已记录 {persist_email} -> {lifecycle_status}", level="warning")
                    except Exception as snapshot_exc:
                        logger.log(f"  [状态] 写入失败快照异常: {snapshot_exc}", level="warning")
                if inventory_item_id > 0:
                    metadata_updates = {
                        "remote_email": persist_email or email or "",
                        "last_stage": str((getattr(result, "metadata", {}) or {}).get("last_stage", "") or ""),
                    }
                    if _is_verification_timeout_failure(error, result):
                        timeout_result = inventory_repository.mark_verification_timeout_blacklisted(
                            inventory_item_id,
                            error=error,
                            task_id=logger.task_id,
                            platform=platform_name,
                            registered_email=persist_email or email or "",
                            metadata_updates=metadata_updates,
                        )
                        timeout_status = str((timeout_result or {}).get("status") or "")
                        timeout_note = str((timeout_result or {}).get("note") or "").strip()
                        if timeout_status == "blacklisted":
                            logger.log("  [邮箱池] 验证码超时，邮箱已拉黑，不再继续分配", level="warning")
                        else:
                            logger.log(
                                f"  [邮箱池] {timeout_note or '验证码超时，邮箱已回收到邮箱池，可再次分配'}",
                                level="warning",
                            )
                    else:
                        failure_result = resolve_inventory_register_failure(
                            inventory_provider_key,
                            inventory_metadata,
                            registered_email=persist_email or email or "",
                            platform=platform_name,
                            error=error,
                        )
                        failure_metadata = dict(failure_result.get("metadata") or {})
                        failure_metadata.update(metadata_updates)
                        inventory_repository.update_item(
                            inventory_item_id,
                            status=str(failure_result.get("status") or lifecycle_status),
                            note=str(failure_result.get("note") or persist_email or email or ""),
                            last_error=error,
                            task_id=logger.task_id,
                            platform=platform_name,
                            metadata_updates=failure_metadata,
                        )
                    _mark_inventory_processed(inventory_item_id)
                logger.record_error(error)
                logger.log(f"[FAIL] 注册失败: {error}", level="error")
                _save_task_log(platform_name, persist_email or email or "", "failed", error=error)
                return error

    try:
        submitted = 0
        completed = 0
        futures: dict[Any, int] = {}
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while submitted < count and len(futures) < concurrency and not logger.is_cancel_requested():
                futures[pool.submit(_do_one, submitted)] = submitted
                submitted += 1

            while futures:
                done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future, None)
                    try:
                        result = future.result()
                    except Exception as future_exc:
                        # 单个任务内部抛出未捕获异常（如 UnicodeEncodeError）不应阻塞整批任务。
                        try:
                            logger.log(
                                f"[FAIL] 子任务异常已隔离: {type(future_exc).__name__}: {future_exc}",
                                level="error",
                            )
                        except Exception:
                            pass
                        result = str(future_exc)
                    completed += 1
                    logger.set_progress(completed, count)
                    if result is True:
                        success += 1
                    elif result != "__cancel_requested__":
                        errors.append(str(result))
                while submitted < count and len(futures) < concurrency and not logger.is_cancel_requested():
                    futures[pool.submit(_do_one, submitted)] = submitted
                    submitted += 1
                if logger.is_cancel_requested() and not futures:
                    break
    except Exception as exc:
        _release_unprocessed_inventory("任务异常终止，邮箱已回收")
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    summary = f"完成: 成功 {success} 个, 失败 {len(errors)} 个"
    logger.log(summary, event_type="summary")
    if logger.is_cancel_requested():
        _release_unprocessed_inventory("任务取消，邮箱已回收")
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    _release_unprocessed_inventory("任务未执行，邮箱已回收")
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    final_error = "" if final_status == TASK_STATUS_SUCCEEDED else errors[0]
    logger.finish(final_status, error=final_error)


def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    command_platform = str(payload.get("platform", ""))
    account_id = int(payload.get("account_id", 0) or 0)
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    runtime = PlatformRuntime()
    result = runtime.execute_action(
        type("Command", (), {
            "platform": command_platform,
            "account_id": account_id,
            "action_id": action_id,
            "params": params,
        })(),
        log_fn=logger.log,
    )
    if not result.ok:
        logger.record_error(result.error)
        logger.finish(TASK_STATUS_FAILED, error=result.error)
        return
    logger.set_result_data(result.data)
    message = ""
    if isinstance(result.data, dict):
        message = str(result.data.get("message", "") or "")
    if message:
        logger.log(message, event_type="summary")
    logger.set_progress(1, 1)
    logger.finish(TASK_STATUS_SUCCEEDED)


_BATCH_ACTION_DEFAULT_CONCURRENCY = 3


def _execute_batch_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    command_platform = str(payload.get("platform", ""))
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    account_ids: list[int] = [int(x) for x in (payload.get("account_ids") or [])]
    concurrency = int(payload.get("concurrency") or _BATCH_ACTION_DEFAULT_CONCURRENCY)
    concurrency = max(1, min(concurrency, 20))

    if not account_ids:
        logger.finish(TASK_STATUS_FAILED, error="没有可执行的账号")
        return

    total = len(account_ids)
    logger.set_progress(0, total)
    logger.log(f"批量操作: {action_id} · 共 {total} 个账号 · 并发 {concurrency}", event_type="summary")

    runtime = PlatformRuntime()
    counter_lock = threading.Lock()
    succeeded = 0
    failed = 0
    completed = 0
    failed_ids: list[int] = []
    succeeded_ids: list[int] = []

    def _do_one(seq: int) -> tuple[bool, int]:
        """执行单个账号操作，返回 (成功?, account_id)。"""
        aid = account_ids[seq]
        if logger.is_cancel_requested():
            return False, aid

        def _account_log(message: str, **_kw: Any) -> None:
            logger.log(f"[#{aid}] {message}")

        try:
            result = runtime.execute_action(
                type("Command", (), {
                    "platform": command_platform,
                    "account_id": aid,
                    "action_id": action_id,
                    "params": params,
                })(),
                log_fn=_account_log,
            )
        except Exception as exc:
            logger.log(f"账号#{aid} 异常: {exc}", event_type="error")
            return False, aid

        if result.ok:
            msg = ""
            if isinstance(result.data, dict):
                msg = str(result.data.get("message", "") or "")
            logger.log(f"账号#{aid} 成功" + (f": {msg}" if msg else ""), event_type="info")
            return True, aid
        else:
            logger.log(f"账号#{aid} 失败: {result.error}", event_type="error")
            return False, aid

    try:
        submitted = 0
        futures: dict[Any, int] = {}
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            while submitted < total and len(futures) < concurrency and not logger.is_cancel_requested():
                futures[pool.submit(_do_one, submitted)] = submitted
                submitted += 1

            while futures:
                done, _ = wait(set(futures.keys()), return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future, None)
                    ok, aid = False, 0
                    try:
                        ok, aid = future.result()
                    except Exception:
                        pass
                    with counter_lock:
                        if ok:
                            succeeded += 1
                            succeeded_ids.append(aid)
                        else:
                            failed += 1
                            failed_ids.append(aid)
                        completed += 1
                        logger.set_progress(completed, total)

                while submitted < total and len(futures) < concurrency and not logger.is_cancel_requested():
                    futures[pool.submit(_do_one, submitted)] = submitted
                    submitted += 1

                if logger.is_cancel_requested() and not futures:
                    break
    except Exception as exc:
        logger.log(f"致命错误: {exc}", event_type="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    summary: dict[str, Any] = {
        "succeeded": succeeded,
        "failed": failed,
        "total": total,
        "failed_account_ids": failed_ids,
        "succeeded_account_ids": succeeded_ids,
    }
    logger.set_result_data(summary)
    logger.log(f"批量完成: 成功 {succeeded}, 失败 {failed}, 共 {total}", event_type="summary")

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
    elif failed == total:
        logger.finish(TASK_STATUS_FAILED, error=f"全部失败 ({total})")
    else:
        logger.finish(TASK_STATUS_SUCCEEDED)


def _execute_account_check_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    account_id = int(payload.get("account_id", 0) or 0)
    if account_id <= 0:
        logger.finish(TASK_STATUS_FAILED, error="缺少 account_id")
        return
    try:
        _, result = _run_single_account_check(account_id, logger)
        logger.set_result_data(result)
        logger.set_progress(1, 1)
        logger.finish(TASK_STATUS_SUCCEEDED)
    except Exception as exc:
        logger.record_error(str(exc))
        logger.finish(TASK_STATUS_FAILED, error=str(exc))


def _execute_account_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "") or "")
    limit = max(int(payload.get("limit", 50) or 50), 1)

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        accounts = session.exec(q.limit(limit)).all()

    total = len(accounts)
    logger.set_progress(0, total)
    if total == 0:
        logger.set_result_data({"valid": 0, "invalid": 0, "error": 0})
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    results = {"valid": 0, "invalid": 0, "error": 0}
    completed = 0
    for model in accounts:
        if logger.is_cancel_requested():
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        try:
            valid, _ = _run_single_account_check(int(model.id or 0), logger)
            if valid:
                results["valid"] += 1
            else:
                results["invalid"] += 1
        except Exception as exc:
            results["error"] += 1
            logger.record_error(str(exc))
            logger.log(f"{model.email}: 检测异常 {exc}", level="error")
        completed += 1
        logger.set_progress(completed, total)
    logger.set_result_data(results)
    logger.finish(TASK_STATUS_SUCCEEDED)


# ─── Google Workspace 批量创建用户 ───

def _execute_google_workspace_bulk_create_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """通过 Camoufox 脚本批量创建 Workspace 用户，stdout 转发到 task log。"""
    import subprocess as _sp
    import threading

    users_json = payload.get("users_json", "")
    offset = int(payload.get("offset", 0))
    limit = int(payload.get("limit", 0))
    count = int(payload.get("count", 0))

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "_google_admin_bulk_add.py"
    if not script.is_file():
        logger.finish(TASK_STATUS_FAILED, error=f"脚本不存在: {script}")
        return

    cmd = [sys.executable, str(script)]
    if limit > 0:
        cmd += ["--limit", str(limit)]
    if offset > 0:
        cmd += ["--offset", str(offset)]
    if users_json:
        cmd += ["--users-json", str(users_json)]

    logger.log(f"启动批量创建: {count} 个用户, cmd={' '.join(cmd)}")

    proc = _sp.Popen(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT, text=True, cwd=str(root))
    success = 0
    errors = 0

    def _read_output():
        nonlocal success, errors
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            logger.log(line)
            if "[OK]" in line or "成功" in line:
                success += 1
            if "[FAIL]" in line or "失败" in line or "抱歉" in line:
                errors += 1

    reader = threading.Thread(target=_read_output, daemon=True)
    reader.start()

    # 等待完成或取消
    while proc.poll() is None:
        if logger.is_cancel_requested():
            logger.log("收到取消请求，终止脚本进程...")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            logger.finish(TASK_STATUS_CANCELLED, error="用户取消")
            return
        time.sleep(1)

    reader.join(timeout=5)
    result_data = {"success": success, "errors": errors, "total": count, "returncode": proc.returncode}
    logger.set_result_data(result_data)
    logger.log(f"批量创建完成: 成功 {success}, 失败 {errors}, 退出码 {proc.returncode}")
    if proc.returncode == 0:
        logger.finish(TASK_STATUS_SUCCEEDED)
    else:
        logger.finish(TASK_STATUS_FAILED, error=f"脚本退出码 {proc.returncode}")


# ─── Google Workspace 批量删除用户 ───

def _execute_google_workspace_bulk_delete_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """批量删除 Workspace 用户。

    delete_all_non_admin=true 时，提示用户需要在浏览器中操作（GUI 批删）。
    提供 user_ids 时，用 Yo0aWe RPC 批量删除（需要在浏览器中执行）。
    """
    user_ids = payload.get("user_ids", [])
    delete_all = payload.get("delete_all_non_admin", False)

    if delete_all:
        logger.log("批量删除所有非管理员用户：请在 Google Admin 控制台用户列表页操作。")
        logger.log("1. 勾选所有非管理员用户")
        logger.log("2. 点「更多选项」→「删除所选用户」")
        logger.log("3. 选「不转移数据」→「删除用户」")
        logger.log("协议方式（Yo0aWe RPC）需要浏览器 session，暂不支持纯后端调用。")
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    if not user_ids:
        logger.finish(TASK_STATUS_FAILED, error="未提供 user_ids 且 delete_all_non_admin=false")
        return

    logger.log(f"批量删除 {len(user_ids)} 个用户: {user_ids[:5]}...")
    logger.log("Yo0aWe RPC 需要浏览器 session，暂不支持纯后端调用。请在 Web 界面用 GUI 批删。")
    logger.finish(TASK_STATUS_SUCCEEDED)
