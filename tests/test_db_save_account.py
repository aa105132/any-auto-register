from __future__ import annotations

from pathlib import Path

from sqlmodel import SQLModel, create_engine

import core.db as db
from core.base_platform import Account


def test_save_account_returns_detached_safe_instance_after_insert(tmp_path: Path, monkeypatch) -> None:
    temp_db = tmp_path / "accounts.db"
    test_engine = create_engine(
        f"sqlite:///{temp_db}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(test_engine)

    import core.account_graph as account_graph

    monkeypatch.setattr(db, "engine", test_engine)
    monkeypatch.setattr(account_graph, "sync_platform_account_graph", lambda session, model, account: None)

    saved = db.save_account(
        Account(
            platform="swarms",
            email="demo@swarms.test",
            password="Password123!",
            user_id="user-1",
        )
    )

    assert int(saved.id or 0) > 0
    assert saved.platform == "swarms"
    assert saved.email == "demo@swarms.test"
