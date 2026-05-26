from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from core.db import AccountModel, AccountOverviewModel
from domain.accounts import AccountQuery


def _load_accounts_repository_module():
    # 部分旧测试会向 sys.modules 注入轻量 stub；这里强制拿真实仓库模块。
    existing = sys.modules.get("infrastructure.accounts_repository")
    if existing is not None and not hasattr(existing, "engine"):
        sys.modules.pop("infrastructure.accounts_repository", None)
    return importlib.import_module("infrastructure.accounts_repository")


def test_stats_uses_overview_columns_without_parsing_summary_json(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as session:
        swarms = AccountModel(
            platform="swarms",
            email="swarms@example.com",
            password="secret",
        )
        zo = AccountModel(
            platform="zo",
            email="zo@example.com",
            password="secret",
        )
        session.add(swarms)
        session.add(zo)
        session.commit()
        session.refresh(swarms)
        session.refresh(zo)

        session.add(
            AccountOverviewModel(
                account_id=int(swarms.id or 0),
                lifecycle_status="registered",
                validity_status="unknown",
                plan_state="free",
                display_status="registered",
                summary_json="{not-json",
            )
        )
        session.add(
            AccountOverviewModel(
                account_id=int(zo.id or 0),
                lifecycle_status="invalid",
                validity_status="invalid",
                plan_state="expired",
                display_status="invalid",
                summary_json="[not-json",
            )
        )
        session.commit()

    repository_module = _load_accounts_repository_module()
    monkeypatch.setattr(repository_module, "engine", test_engine)

    stats = repository_module.AccountsRepository().stats()

    assert stats.total == 2
    assert stats.by_platform == {"swarms": 1, "zo": 1}
    assert stats.by_status == {"registered": 1, "invalid": 1}
    assert stats.by_lifecycle_status == {"registered": 1, "invalid": 1}
    assert stats.by_plan_state == {"free": 1, "expired": 1}
    assert stats.by_validity_status == {"unknown": 1, "invalid": 1}
    assert stats.by_display_status == {"registered": 1, "invalid": 1}


def test_list_paginates_before_loading_account_graphs(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as session:
        old_1 = AccountModel(platform="heavy", email="old1@example.com", password="secret")
        old_2 = AccountModel(platform="heavy", email="old2@example.com", password="secret")
        newest = AccountModel(platform="heavy", email="newest@example.com", password="secret")
        session.add(old_1)
        session.add(old_2)
        session.add(newest)
        session.commit()
        session.refresh(old_1)
        session.refresh(old_2)
        session.refresh(newest)

        for account in (old_1, old_2):
            session.add(
                AccountOverviewModel(
                    account_id=int(account.id or 0),
                    display_status="registered",
                    summary_json="{off-page-bad-json",
                )
            )
        session.add(
            AccountOverviewModel(
                account_id=int(newest.id or 0),
                display_status="registered",
                summary_json='{"note": "only current page should be parsed"}',
            )
        )
        session.commit()

    repository_module = _load_accounts_repository_module()
    monkeypatch.setattr(repository_module, "engine", test_engine)

    total, items = repository_module.AccountsRepository().list(
        AccountQuery(platform="heavy", page=1, page_size=1)
    )

    assert total == 3
    assert [item.email for item in items] == ["newest@example.com"]


def test_list_status_filter_uses_overview_columns_before_loading_graphs(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as session:
        invalid = AccountModel(platform="heavy", email="invalid@example.com", password="secret")
        registered = AccountModel(platform="heavy", email="registered@example.com", password="secret")
        session.add(invalid)
        session.add(registered)
        session.commit()
        session.refresh(invalid)
        session.refresh(registered)

        session.add(
            AccountOverviewModel(
                account_id=int(invalid.id or 0),
                lifecycle_status="invalid",
                validity_status="invalid",
                plan_state="expired",
                display_status="invalid",
                summary_json="[filtered-out-bad-json",
            )
        )
        session.add(
            AccountOverviewModel(
                account_id=int(registered.id or 0),
                lifecycle_status="registered",
                validity_status="unknown",
                plan_state="free",
                display_status="registered",
                summary_json='{"note": "matched row"}',
            )
        )
        session.commit()

    repository_module = _load_accounts_repository_module()
    monkeypatch.setattr(repository_module, "engine", test_engine)

    total, items = repository_module.AccountsRepository().list(
        AccountQuery(platform="heavy", status="registered", page=1, page_size=20)
    )

    assert total == 1
    assert [item.email for item in items] == ["registered@example.com"]


def test_scheduler_trial_expiry_uses_lightweight_columns_without_parsing_summary_json(monkeypatch):
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as session:
        expired_trial = AccountModel(platform="heavy", email="trial@example.com", password="secret")
        registered = AccountModel(platform="heavy", email="registered2@example.com", password="secret")
        session.add(expired_trial)
        session.add(registered)
        session.commit()
        session.refresh(expired_trial)
        session.refresh(registered)

        session.add(
            AccountOverviewModel(
                account_id=int(expired_trial.id or 0),
                lifecycle_status="trial",
                validity_status="unknown",
                plan_state="trial",
                display_status="trial",
                summary_json='{"trial_end_time": 1}',
            )
        )
        session.add(
            AccountOverviewModel(
                account_id=int(registered.id or 0),
                lifecycle_status="registered",
                validity_status="unknown",
                plan_state="unknown",
                display_status="registered",
                summary_json="[bad-registered-json",
            )
        )
        session.commit()

    import core.scheduler as scheduler_module

    monkeypatch.setattr(scheduler_module, "engine", test_engine)
    monkeypatch.setattr(
        scheduler_module,
        "_utc_timestamp",
        lambda: int(datetime(2026, 5, 26, tzinfo=timezone.utc).timestamp()),
    )

    scheduler = scheduler_module.Scheduler()
    scheduler.check_trial_expiry()

    expired_trial_id = 1
    with Session(test_engine) as session:
        overview = session.get(AccountOverviewModel, expired_trial_id)
        assert overview is not None
        assert overview.lifecycle_status == "expired"
        assert overview.display_status == "expired"
