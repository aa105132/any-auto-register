import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.google_account_pool import GoogleAccountPool


def _write_pool(path: Path, count: int) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "accounts": [
                    {
                        "email": f"user{index}@example.com",
                        "password": f"pw{index}",
                        "registered_platforms": [],
                        "status": "valid",
                    }
                    for index in range(count)
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_concurrent_acquire_reserves_distinct_accounts(tmp_path: Path):
    pool_path = tmp_path.joinpath("google_accounts_pool.json")
    _write_pool(pool_path, 3)

    def acquire_email() -> str:
        account = GoogleAccountPool(str(pool_path)).acquire(exclude_platforms=["gettoken"])
        return account.email if account else ""

    with ThreadPoolExecutor(max_workers=3) as executor:
        emails = list(executor.map(lambda _: acquire_email(), range(3)))

    assert len(set(emails)) == 3
    data = json.loads(pool_path.read_text(encoding="utf-8"))
    assert all(item["reserved_platforms"] == ["gettoken"] for item in data["accounts"])


def test_mark_registered_clears_reservation(tmp_path: Path):
    pool_path = tmp_path.joinpath("google_accounts_pool.json")
    _write_pool(pool_path, 1)
    pool = GoogleAccountPool(str(pool_path))

    account = pool.acquire(exclude_platforms=["gettoken"])
    assert account is not None
    assert pool.mark_registered(account.email, "gettoken") is True

    data = json.loads(pool_path.read_text(encoding="utf-8"))
    item = data["accounts"][0]
    assert item["registered_platforms"] == ["gettoken"]
    assert "reserved_platforms" not in item


def test_release_allows_reuse_after_failure(tmp_path: Path):
    pool_path = tmp_path.joinpath("google_accounts_pool.json")
    _write_pool(pool_path, 1)
    pool = GoogleAccountPool(str(pool_path))

    account = pool.acquire(exclude_platforms=["gettoken"])
    assert account is not None
    assert pool.acquire(exclude_platforms=["gettoken"]) is None
    assert pool.release(account.email, "gettoken") is True

    reacquired = pool.acquire(exclude_platforms=["gettoken"])
    assert reacquired is not None
    assert reacquired.email == account.email


def test_delete_invalid_removes_only_invalid_accounts(tmp_path: Path):
    pool_path = tmp_path.joinpath("google_accounts_pool.json")
    _write_pool(pool_path, 3)
    pool = GoogleAccountPool(str(pool_path))

    assert pool.mark_invalid("user1@example.com", reason="bad credentials") is True
    result = pool.delete_invalid()

    assert result["deleted"] == 1
    data = json.loads(pool_path.read_text(encoding="utf-8"))
    emails = [item["email"] for item in data["accounts"]]
    assert emails == ["user0@example.com", "user2@example.com"]
    assert all(item["status"] == "valid" for item in data["accounts"])
