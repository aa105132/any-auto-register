"""End-to-end EmberCloud test: 邮箱池领号 → 平台 register() → chat 200.

驱动 application.tasks._build_platform_instance + platform.register()，
走和正式批量任务一致的链路：领 outlook_token 邮箱、构建 mailbox provider、
浏览器 sign_up + 协议拿 key，再实测 /v1/models + /v1/chat/completions。

需要本机装了 Chrome（浏览器会真实打开窗口）。 Outlook 邮箱池必须可用。
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

import requests


PLATFORM_NAME = "embercloud"
CHAT_URL = "https://api.embercloud.ai/v1/chat/completions"
MODELS_URL = "https://api.embercloud.ai/v1/models"


class _StdoutLogger:
    """最小 TaskLogger 替身：和 application.tasks.TaskLogger 同接口，但只打印。"""

    def __init__(self) -> None:
        self.task_id = f"e2e-{int(time.time())}"

    def log(
        self,
        message: str,
        *,
        level: str = "info",
        event_type: str = "log",
        detail: dict | None = None,
    ) -> None:
        line = f"[{level}] {message}"
        try:
            sys.stdout.buffer.write((line + "\n").encode("utf-8", "replace"))
            sys.stdout.buffer.flush()
        except Exception:
            print(line)


def _claim_outlook_seed() -> dict[str, Any]:
    """从 outlook_token 邮箱池领一个邮箱，构造 platform.register 的 seed payload。"""
    from application.mailbox_inventory_support import (
        OUTLOOK_TOKEN_PROVIDER_KEY,
        build_mailbox_inventory_seed,
        supports_mailbox_inventory,
    )
    from application.tasks import _claim_inventory_register_lines, _merge_register_extra
    from domain.accounts import AccountImportLine

    assert supports_mailbox_inventory(OUTLOOK_TOKEN_PROVIDER_KEY), "outlook_token 邮箱池不可用"

    payload = {
        "platform": PLATFORM_NAME,
        "count": 1,
        "extra": {"mail_provider": OUTLOOK_TOKEN_PROVIDER_KEY},
    }
    seeds = _claim_inventory_register_lines(payload, _StdoutLogger())
    if not seeds:
        raise RuntimeError("outlook_token 邮箱池没有可领的邮箱；先导入池子再跑 e2e")
    seed: AccountImportLine = seeds[0]
    base_extra = {
        "mail_provider": OUTLOOK_TOKEN_PROVIDER_KEY,
        # executor_type 必须是 protocol/cdp_protocol：EmberCloud 走 ProtocolMailboxFlow，
        # 浏览器 sign_up 是 worker 内部行为（build_protocol_mailbox_adapter），不走 BrowserRegistrationFlow。
        "executor_type": "protocol",
        "platform": PLATFORM_NAME,
        "platform_name": PLATFORM_NAME,
    }
    extra = _merge_register_extra(base_extra, dict(seed.extra or {}))
    inv = dict(extra.get("_inventory") or {})
    return {
        "email": seed.email,
        "password": "",
        "extra": extra,
        "inventory_id": int(inv.get("id") or 0),
    }


def _release_inventory(inventory_id: int, *, last_error: str = "") -> None:
    """把领到的邮箱槽位释放回 unused，避免失败时永久占用。"""
    if not inventory_id:
        return
    from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository

    MailboxInventoryRepository().update_item(
        inventory_id,
        status="unused",
        last_error=last_error or None,
    )


def _build_platform(seed: dict[str, Any]):
    import core.registry as registry

    registry.load_all()
    from application.tasks import _build_platform_instance

    payload = {
        "platform": PLATFORM_NAME,
        "email": seed["email"],
        "password": seed["password"],
        "executor_type": "protocol",
        "extra": seed["extra"],
    }
    return _build_platform_instance(PLATFORM_NAME, payload, _StdoutLogger())


def _verify_models(api_key: str) -> dict[str, Any]:
    r = requests.get(
        MODELS_URL,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        timeout=30,
    )
    return {"status": r.status_code, "ok": r.ok, "body_head": (r.text or "")[:400]}


def _verify_chat(api_key: str) -> dict[str, Any]:
    r = requests.post(
        CHAT_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            # EmberCloud 实际可用模型由 /v1/models 返回（glm-5.2 等），
            # gpt-4.1-mini 不存在会 400 invalid_request_error。
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Say only the word pong."}],
            "max_tokens": 16,
            "stream": False,
        },
        timeout=60,
    )
    body_head = (r.text or "")[:400]
    out: dict[str, Any] = {"status": r.status_code, "ok": r.ok, "body_head": body_head}
    try:
        j = r.json()
        if isinstance(j, dict):
            choices = j.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                out["answer"] = str(msg.get("content") or "")
    except Exception:
        pass
    return out


def _is_email_taken_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "already in use" in msg or "already registered" in msg or "已被占用" in msg or "is in use" in msg


def main() -> int:
    account = None
    seed: dict[str, Any] = {}
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        print("=" * 60)
        print(f"STEP 1: 从 outlook_token 邮箱池领号 (attempt {attempt}/{max_attempts})")
        seed = _claim_outlook_seed()
        print(f"  Email: {seed['email']}")
        print(f"  Extra keys: {sorted(seed['extra'].keys())}")

        print("=" * 60)
        print("STEP 2: 构建 EmberCloudPlatform 实例")
        platform = _build_platform(seed)
        print(f"  platform={platform.__class__.__name__} mail_provider={platform.default_mail_provider}")

        print("=" * 60)
        print("STEP 3: platform.register() —— 浏览器 sign_up + 协议拿 key")
        t0 = time.time()
        try:
            account = platform.register(email=seed["email"], password=seed["password"] or None)
            break  # 成功
        except Exception as exc:
            print(f"  FAIL: register 抛异常: {type(exc).__name__}: {exc}")
            _release_inventory(seed.get("inventory_id", 0), last_error=str(exc)[:200])
            if _is_email_taken_error(exc) and attempt < max_attempts:
                print("  邮箱已被占用，自动换号重试...")
                continue
            return 1
        finally:
            print(f"  耗时: {int(time.time() - t0)}s")

    if account is None:
        print("\nFAIL: 多次尝试后仍未注册成功")
        return 1

    api_key = str((account.extra or {}).get("api_key") or account.token or "")
    print(f"  Email:   {account.email}")
    print(f"  Status:  {account.status}")
    print(f"  API Key: {api_key[:14]}...{api_key[-4:] if len(api_key) > 4 else ''}")
    if not api_key.startswith("ek_live_"):
        print("\nFAIL: 没拿到 ek_live_ key")
        print(json.dumps({"extra": dict(account.extra or {})}, indent=2)[:1200])
        return 1

    print("=" * 60)
    print("STEP 4: /v1/models 验证 key")
    models = _verify_models(api_key)
    print(f"  {models['status']} ok={models['ok']} body={models['body_head'][:200]}")
    if not models["ok"]:
        print("FAIL: /v1/models 不通")
        _release_inventory(seed.get("inventory_id", 0), last_error="models verify failed")
        return 1

    print("=" * 60)
    print("STEP 5: /v1/chat/completions 实测（期望 200 + pong）")
    chat = _verify_chat(api_key)
    print(f"  {chat['status']} ok={chat['ok']}")
    print(f"  body: {chat.get('body_head', '')[:200]}")
    if "answer" in chat:
        print(f"  answer: {chat['answer']!r}")
    if chat["status"] != 200:
        print(f"\nFAIL: chat 返回 {chat['status']}")
        _release_inventory(seed.get("inventory_id", 0), last_error=f"chat {chat['status']}")
        return 1

    # 全链路通过：把邮箱槽位标成功
    inv_id = seed.get("inventory_id", 0)
    if inv_id:
        from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository

        MailboxInventoryRepository().mark_registration_success(
            inv_id,
            registered_email=account.email,
            task_id=f"e2e-{int(time.time())}",
            platform=PLATFORM_NAME,
        )

    print("=" * 60)
    print("STEP 6: 持久化结果")
    os.makedirs("output", exist_ok=True)
    with open("output/embercloud_keys.txt", "a", encoding="utf-8") as f:
        f.write(
            f"{account.email} | {account.password or ''} | "
            f"{api_key} | models={models['status']} chat={chat['status']}\n"
        )
    summary = {
        "email": account.email,
        "api_key": api_key,
        "models_status": models["status"],
        "chat_status": chat["status"],
        "chat_answer": chat.get("answer", ""),
    }
    with open("output/embercloud_e2e_result.json", "a", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    print("\nPASS: 浏览器注册 + 协议拿 key + chat 200 全链路通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
