from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select, func

from application.mailbox_inventory_support import (
    OUTLOOK_TOKEN_PROVIDER_KEY,
    build_outlook_alias_inventory_entry,
    inventory_platform_already_used,
    is_mailbox_domain_blacklisted,
    parse_mailbox_inventory_import_lines,
    resolve_inventory_registration_success,
    resolve_inventory_timeout_result,
)
from core.db import MailboxInventoryModel, engine


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)



def _is_outlook_alias_inventory_item(email: str, metadata: dict[str, Any]) -> bool:
    source = str(metadata.get("source") or "").strip().lower()
    if source == "outlook_alias_auto":
        return True
    if str(metadata.get("alias_parent_email") or metadata.get("outlook_login_email") or "").strip():
        return True
    local = str(email or "").split("@", 1)[0]
    return "+" in local


class MailboxInventoryRepository:
    def list_by_provider(self, provider_key: str, *, status: str = "") -> list[dict[str, Any]]:
        normalized_provider = str(provider_key or "").strip()
        if not normalized_provider:
            return []
        with Session(engine) as session:
            statement = (
                select(MailboxInventoryModel)
                .where(MailboxInventoryModel.provider_key == normalized_provider)
                .order_by(MailboxInventoryModel.id.desc())
            )
            if status:
                statement = statement.where(MailboxInventoryModel.status == str(status).strip())
            items = session.exec(statement).all()
            return [self._serialize(item) for item in items]

    def count_available(self, provider_key: str, *, include_outlook_aliases: bool = False) -> int:
        normalized_provider = str(provider_key or "").strip()
        if not normalized_provider:
            return 0
        with Session(engine) as session:
            candidates = session.exec(
                select(MailboxInventoryModel)
                .where(MailboxInventoryModel.provider_key == normalized_provider)
                .where(MailboxInventoryModel.status == "unused")
            ).all()
            available = 0
            for item in candidates:
                metadata = item.get_metadata()
                if (
                    normalized_provider == OUTLOOK_TOKEN_PROVIDER_KEY
                    and not include_outlook_aliases
                    and _is_outlook_alias_inventory_item(item.email, metadata)
                ):
                    continue
                if is_mailbox_domain_blacklisted(item.email):
                    continue
                available += 1
            return available

    def get_status_counts(self, provider_key: str) -> dict[str, int]:
        normalized_provider = str(provider_key or "").strip()
        if not normalized_provider:
            return {}
        with Session(engine) as session:
            rows = session.exec(
                select(MailboxInventoryModel.status, func.count())
                .where(MailboxInventoryModel.provider_key == normalized_provider)
                .group_by(MailboxInventoryModel.status)
            ).all()
        return {str(status or ""): int(count or 0) for status, count in rows}

    def import_lines(self, provider_key: str, lines: list[str]) -> dict[str, int]:
        normalized_provider = str(provider_key or "").strip()
        if not normalized_provider:
            raise ValueError("provider_key 不能为空")
        parsed = parse_mailbox_inventory_import_lines(normalized_provider, lines or [])
        created = 0
        updated = 0
        skipped = 0
        with Session(engine) as session:
            for line in parsed:
                email = str(line.get("email") or "").strip()
                purchase_token = str(line.get("token") or "").strip()
                if not email or not purchase_token:
                    skipped += 1
                    continue
                note = str(line.get("note") or "").strip()
                metadata_updates = dict(line.get("metadata") or {})
                existing = session.exec(
                    select(MailboxInventoryModel)
                    .where(MailboxInventoryModel.provider_key == normalized_provider)
                    .where(MailboxInventoryModel.email == email)
                ).first()
                if existing:
                    existing.purchase_token = purchase_token
                    if note:
                        existing.note = note
                    metadata = existing.get_metadata()
                    metadata.update(metadata_updates)
                    existing.set_metadata(metadata)
                    existing.updated_at = _utcnow()
                    session.add(existing)
                    updated += 1
                    continue

                item = MailboxInventoryModel(
                    provider_key=normalized_provider,
                    email=email,
                    purchase_token=purchase_token,
                    status="unused",
                    note=note,
                )
                item.set_metadata(metadata_updates)
                session.add(item)
                created += 1
            session.commit()
        return {
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "total": created + updated,
        }

    def claim_available(
        self,
        provider_key: str,
        *,
        count: int,
        task_id: str = "",
        platform: str = "",
        include_outlook_aliases: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_provider = str(provider_key or "").strip()
        if not normalized_provider:
            return []
        requested = max(int(count or 0), 0)
        if requested <= 0:
            return []
        with Session(engine) as session:
            candidates = session.exec(
                select(MailboxInventoryModel)
                .where(MailboxInventoryModel.provider_key == normalized_provider)
                .where(MailboxInventoryModel.status == "unused")
                # 失败降权排序：fail_count 少的优先，同 fail_count 时 last_failed_at 早的优先
                # （最近没失败的 / 失败过但等最久的先领），最后 id 兜底稳定序。
                .order_by(
                    MailboxInventoryModel.fail_count.asc(),
                    MailboxInventoryModel.last_failed_at.asc(),
                    MailboxInventoryModel.id.asc(),
                )
            ).all()
            now = _utcnow()
            serialized: list[dict[str, Any]] = []
            for item in candidates:
                metadata = item.get_metadata()
                if (
                    normalized_provider == OUTLOOK_TOKEN_PROVIDER_KEY
                    and not include_outlook_aliases
                    and _is_outlook_alias_inventory_item(item.email, metadata)
                ):
                    continue
                if inventory_platform_already_used(normalized_provider, metadata, str(platform or "")):
                    continue
                if is_mailbox_domain_blacklisted(item.email, platform=str(platform or "")):
                    continue
                item.status = "running"
                item.last_task_id = str(task_id or "")
                item.last_platform = str(platform or "")
                item.last_error = ""
                item.updated_at = now
                session.add(item)
                serialized.append(self._serialize(item))
                if len(serialized) >= requested:
                    break
            session.commit()
        return serialized

    def update_item(
        self,
        item_id: int,
        *,
        status: str | None = None,
        note: str | None = None,
        last_error: str | None = None,
        task_id: str | None = None,
        platform: str | None = None,
        metadata_updates: dict[str, Any] | None = None,
        bump_fail: bool = False,
    ) -> dict[str, Any] | None:
        normalized_id = int(item_id or 0)
        if normalized_id <= 0:
            return None
        with Session(engine) as session:
            item = session.get(MailboxInventoryModel, normalized_id)
            if not item:
                return None
            metadata = item.get_metadata()
            if status is not None:
                item.status = str(status or "").strip() or item.status
                if item.status == "unused":
                    metadata.pop("blacklist_reason", None)
                    metadata.pop("blacklisted_at", None)
            if note is not None:
                item.note = str(note or "")
            if last_error is not None:
                item.last_error = str(last_error or "")
            if task_id is not None:
                item.last_task_id = str(task_id or "")
            if platform is not None:
                item.last_platform = str(platform or "")
            if bump_fail:
                # 失败降权：累加 fail_count + 记最近失败时间，claim_available 排序时失败少/早的优先
                item.fail_count = int(item.fail_count or 0) + 1
                item.last_failed_at = _utcnow()
            if metadata_updates:
                metadata.update(metadata_updates)
            item.set_metadata(metadata)
            item.updated_at = _utcnow()
            session.add(item)
            session.commit()
            session.refresh(item)
            return self._serialize(item)

    def mark_registration_success(
        self,
        item_id: int,
        *,
        registered_email: str = "",
        task_id: str = "",
        platform: str = "",
    ) -> dict[str, Any] | None:
        normalized_id = int(item_id or 0)
        if normalized_id <= 0:
            return None
        with Session(engine) as session:
            item = session.get(MailboxInventoryModel, normalized_id)
            if not item:
                return None
            result = resolve_inventory_registration_success(
                item.provider_key,
                item.get_metadata(),
                registered_email=registered_email,
                platform=platform,
            )
            item.set_metadata(dict(result.get("metadata") or {}))
            item.last_task_id = str(task_id or "")
            item.last_platform = str(platform or "")
            item.last_error = ""
            item.status = str(result.get("status") or item.status or "unused")
            item.note = str(result.get("note") or item.note or "")
            # 注册成功清零失败计数，让该邮箱重新回到队首优先级
            item.fail_count = 0
            item.last_failed_at = None
            item.updated_at = _utcnow()
            session.add(item)
            session.commit()
            session.refresh(item)
            return self._serialize(item)

    def mark_verification_timeout_blacklisted(
        self,
        item_id: int,
        *,
        error: str = "",
        task_id: str = "",
        platform: str = "",
        registered_email: str = "",
        metadata_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        normalized_id = int(item_id or 0)
        if normalized_id <= 0:
            return None
        with Session(engine) as session:
            item = session.get(MailboxInventoryModel, normalized_id)
            if not item:
                return None
            metadata = item.get_metadata()
            if metadata_updates:
                metadata.update(metadata_updates)
            result = resolve_inventory_timeout_result(
                item.provider_key,
                metadata,
                registered_email=registered_email,
                platform=platform,
            )
            item.set_metadata(dict(result.get("metadata") or {}))
            item.status = str(result.get("status") or item.status or "unused")
            item.note = str(result.get("note") or item.note or "")
            item.last_error = str(error or "")
            item.last_task_id = str(task_id or "")
            item.last_platform = str(platform or "")
            # 超时也算一次失败：累加 fail_count + 记最近失败时间（outlook 超时仍回收 unused，
            # 但失败计数让下次领号降权）
            item.fail_count = int(item.fail_count or 0) + 1
            item.last_failed_at = _utcnow()
            item.updated_at = _utcnow()
            session.add(item)
            session.commit()
            session.refresh(item)
            return self._serialize(item)

    def reset_many(self, item_ids: list[int], *, note: str = "") -> None:
        normalized_ids = [int(item_id or 0) for item_id in item_ids if int(item_id or 0) > 0]
        if not normalized_ids:
            return
        with Session(engine) as session:
            items = session.exec(
                select(MailboxInventoryModel)
                .where(MailboxInventoryModel.id.in_(normalized_ids))
            ).all()
            now = _utcnow()
            for item in items:
                item.status = "unused"
                item.last_error = ""
                # 手动重置清零失败计数，让邮箱回到最高优先级
                item.fail_count = 0
                item.last_failed_at = None
                if note:
                    item.note = note
                item.updated_at = now
                session.add(item)
            session.commit()

    def upsert_outlook_alias(self, parent_item: dict[str, Any], *, alias_email: str, platform: str = "") -> dict[str, Any]:
        normalized_alias = str(alias_email or "").strip()
        if not normalized_alias:
            raise ValueError("alias_email 不能为空")
        entry = build_outlook_alias_inventory_entry(parent_item, alias_email=normalized_alias, platform=platform)
        provider_key = str(entry.get("provider_key") or OUTLOOK_TOKEN_PROVIDER_KEY).strip()
        with Session(engine) as session:
            existing = session.exec(
                select(MailboxInventoryModel)
                .where(MailboxInventoryModel.provider_key == provider_key)
                .where(MailboxInventoryModel.email == normalized_alias)
            ).first()
            if existing:
                existing.purchase_token = str(entry.get("purchase_token") or existing.purchase_token or "")
                existing.status = str(entry.get("status") or existing.status or "unused")
                existing.note = str(entry.get("note") or existing.note or "")
                metadata = existing.get_metadata()
                metadata.update(dict(entry.get("metadata") or {}))
                existing.set_metadata(metadata)
                existing.updated_at = _utcnow()
                session.add(existing)
                session.commit()
                session.refresh(existing)
                return self._serialize(existing)

            item = MailboxInventoryModel(
                provider_key=provider_key,
                email=normalized_alias,
                purchase_token=str(entry.get("purchase_token") or ""),
                status=str(entry.get("status") or "unused"),
                note=str(entry.get("note") or ""),
            )
            item.set_metadata(dict(entry.get("metadata") or {}))
            session.add(item)
            session.commit()
            session.refresh(item)
            return self._serialize(item)

    @staticmethod
    def _serialize(item: MailboxInventoryModel) -> dict[str, Any]:
        token = str(item.purchase_token or "")
        token_preview = ""
        if token:
            token_preview = token if len(token) <= 10 else f"{token[:6]}...{token[-4:]}"
        return {
            "id": int(item.id or 0),
            "provider_key": item.provider_key,
            "email": item.email,
            "purchase_token": token,
            "token_preview": token_preview,
            "status": item.status,
            "note": item.note,
            "last_error": item.last_error,
            "last_task_id": item.last_task_id,
            "last_platform": item.last_platform,
            "metadata": item.get_metadata(),
            "fail_count": int(item.fail_count or 0),
            "last_failed_at": item.last_failed_at.isoformat() if item.last_failed_at else None,
            "created_at": item.created_at.isoformat() if item.created_at else None,
            "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        }
