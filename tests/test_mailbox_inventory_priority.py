"""邮箱池失败降权 + AnyCap OTP 超时回归测试。"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from core.db import MailboxInventoryModel


def _build_engine():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    return engine


def _insert(engine, *, email: str, status: str = "unused", fail_count: int = 0,
            last_failed_at: datetime | None = None, provider: str = "outlook_token",
            client_id: str = "cid", token: str = "rt") -> MailboxInventoryModel:
    item = MailboxInventoryModel(
        provider_key=provider,
        email=email,
        purchase_token=token,
        status=status,
        metadata_json=f'{{"client_id":"{client_id}"}}',
        fail_count=fail_count,
        last_failed_at=last_failed_at or datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    with Session(engine) as session:
        session.add(item)
        session.commit()
        session.refresh(item)
        return item


class MailboxInventoryPriorityTests(unittest.TestCase):
    def _repo_with_engine(self, engine):
        from infrastructure import mailbox_inventory_repository as repo_mod
        patcher = patch.object(repo_mod, "engine", engine)
        patcher.start()
        self.addCleanup(patcher.stop)
        return repo_mod.MailboxInventoryRepository()

    def test_claim_orders_by_fail_count_then_last_failed_then_id(self):
        engine = _build_engine()
        # 三个 unused 邮箱：A 从未失败、B 失败 1 次、C 失败 3 次。
        # 领取顺序应为 A -> B -> C（失败少的优先，id 仅作兜底）。
        _insert(engine, email="a@outlook.com", fail_count=0)
        _insert(engine, email="b@outlook.com", fail_count=1)
        _insert(engine, email="c@outlook.com", fail_count=3)
        repo = self._repo_with_engine(engine)

        claimed = repo.claim_available("outlook_token", count=3, platform="anycap")
        emails = [item["email"] for item in claimed]
        self.assertEqual(emails, ["a@outlook.com", "b@outlook.com", "c@outlook.com"])

    def test_claim_prefers_older_failure_when_fail_counts_equal(self):
        engine = _build_engine()
        # 两个邮箱都失败 1 次：D 失败更早、E 失败更晚。
        # D 应排在 E 前面（最近失败越早越优先）。
        _insert(
            engine, email="d@outlook.com", fail_count=1,
            last_failed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        _insert(
            engine, email="e@outlook.com", fail_count=1,
            last_failed_at=datetime(2026, 6, 20, tzinfo=timezone.utc),
        )
        repo = self._repo_with_engine(engine)

        claimed = repo.claim_available("outlook_token", count=2, platform="anycap")
        self.assertEqual([item["email"] for item in claimed], ["d@outlook.com", "e@outlook.com"])

    def test_timeout_increments_fail_count_and_updates_last_failed_at(self):
        engine = _build_engine()
        item = _insert(engine, email="t@outlook.com", fail_count=0)
        repo = self._repo_with_engine(engine)

        before = datetime.now(timezone.utc)
        result = repo.mark_verification_timeout_blacklisted(
            item.id, error="timeout", task_id="t1", platform="anycap",
            registered_email="t@outlook.com",
        )
        self.assertEqual(result["status"], "unused")  # outlook 超时仍回收
        self.assertEqual(result["fail_count"], 1)
        self.assertTrue(result["last_failed_at"])
        self.assertGreaterEqual(result["last_failed_at"][:19], before.strftime("%Y-%m-%dT%H:%M:%S"))

    def test_register_failure_bumps_fail_count_via_update_item(self):
        engine = _build_engine()
        item = _insert(engine, email="f@outlook.com", fail_count=0)
        repo = self._repo_with_engine(engine)

        result = repo.update_item(
            item.id, status="unused", note="注册失败但回收", last_error="ProxyError 504",
            task_id="t2", platform="anycap", bump_fail=True,
        )
        self.assertEqual(result["status"], "unused")
        self.assertEqual(result["fail_count"], 1)
        # 再失败一次应累加
        result2 = repo.update_item(item.id, status="unused", last_error="504", bump_fail=True)
        self.assertEqual(result2["fail_count"], 2)

    def test_registration_success_resets_fail_count(self):
        engine = _build_engine()
        item = _insert(engine, email="s@outlook.com", fail_count=5,
                       last_failed_at=datetime(2026, 6, 19, tzinfo=timezone.utc))
        repo = self._repo_with_engine(engine)

        result = repo.mark_registration_success(
            item.id, registered_email="s@outlook.com", task_id="t3", platform="anycap",
        )
        self.assertEqual(result["fail_count"], 0)
        # 清零后该邮箱应重新回到队首
        _insert(engine, email="other@outlook.com", fail_count=1)
        claimed = repo.claim_available("outlook_token", count=2, platform="chatgpt")
        self.assertEqual(claimed[0]["email"], "s@outlook.com")

    def test_reset_many_clears_fail_count(self):
        engine = _build_engine()
        item = _insert(engine, email="r@outlook.com", fail_count=4,
                       last_failed_at=datetime(2026, 6, 19, tzinfo=timezone.utc))
        repo = self._repo_with_engine(engine)

        repo.reset_many([item.id], note="手动重置")
        with Session(engine) as session:
            refreshed = session.get(MailboxInventoryModel, item.id)
        self.assertEqual(refreshed.status, "unused")
        self.assertEqual(refreshed.fail_count, 0)

    def test_cancel_path_does_not_bump_fail_count(self):
        # tasks.py 取消分支调用 update_item 时不传 bump_fail，不应累加。
        engine = _build_engine()
        item = _insert(engine, email="c@outlook.com", fail_count=0)
        repo = self._repo_with_engine(engine)

        result = repo.update_item(
            item.id, status="unused", note="任务已取消，邮箱已回收",
            last_error="", task_id="t4", platform="anycap",
        )
        self.assertEqual(result["fail_count"], 0)


class AnyCapOtpTimeoutTests(unittest.TestCase):
    def test_protocol_mailbox_adapter_otp_timeout_defaults_to_180(self):
        from platforms.anycap.plugin import AnyCapPlatform
        from core.base_platform import RegisterConfig

        adapter = AnyCapPlatform(config=RegisterConfig(extra={})).build_protocol_mailbox_adapter()
        self.assertIsNotNone(adapter.otp_spec)
        self.assertEqual(adapter.otp_spec.timeout, 180)
        # keyword 不变：实测邮件正文含 auth0/converge/anycap，保持匹配。
        self.assertEqual(adapter.otp_spec.keyword, "Auth0")

    def test_protocol_mailbox_adapter_otp_timeout_honors_anycap_otp_timeout(self):
        from platforms.anycap.plugin import AnyCapPlatform
        from core.base_platform import RegisterConfig

        adapter = AnyCapPlatform(
            config=RegisterConfig(extra={"anycap_otp_timeout": 300})
        ).build_protocol_mailbox_adapter()
        self.assertEqual(adapter.otp_spec.timeout, 300)


if __name__ == "__main__":
    unittest.main()
