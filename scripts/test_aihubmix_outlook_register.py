"""实测：outlook 邮箱注册 aihubmix（Clerk email_code signup 全流程）。

用 OutlookTokenMailbox IMAP 收 Clerk 验证码，AIHubMixBrowserRegistrar 跑浏览器注册。
成功标准：拿到 sk- API key 且 verify_api_key 返回 True。

前置：output/outlook_accounts_pool.json 里有带 refresh_token 的 outlook 账号。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.outlook_account_pool import OutlookAccountPool
from core.base_mailbox import OutlookTokenMailbox
from platforms.aihubmix.browser_register import AIHubMixBrowserRegistrar
from platforms.aihubmix.core import AIHubMixClient


EMAIL = "wknfpiprcdgno@outlook.com"
PASSWORD = "AiHubMix-Outlook-Test-2026!"  # 注册用的 Clerk 密码（非 outlook 邮箱密码）
API_KEY_NAME = "outlook-test-key"


def main() -> int:
    pool = OutlookAccountPool()
    acct = pool.get_by_email(EMAIL)
    if not acct or not acct.refresh_token or not acct.client_id:
        print(f"[FAIL] outlook 账号 {EMAIL} 缺少 refresh_token/client_id，无法 IMAP 收件")
        return 1

    print(f"[step] outlook 账号: {EMAIL}, client_id={acct.client_id[:8]}..., rt_len={len(acct.refresh_token)}")

    mailbox = OutlookTokenMailbox(
        email=acct.email,
        password=acct.password,
        client_id=acct.client_id,
        refresh_token=acct.refresh_token,
    )
    # 预检：IMAP token 刷新 + 当前邮箱基线
    mail_account = mailbox.get_email()
    print(f"[step] IMAP 账号建立: {mail_account.email}")
    try:
        token = mailbox._refresh_access_token(mail_account)
        print(f"[step] access_token 刷新成功, len={len(token)}")
    except Exception as exc:
        print(f"[FAIL] outlook IMAP token 刷新失败: {exc}")
        return 2
    before_ids = mailbox.get_current_ids(mail_account)
    print(f"[step] 当前邮箱基线邮件数: {len(before_ids)}")

    # otp_callback：等 Clerk 发验证码到 outlook，扫 INBOX+Junk
    def otp_callback() -> str:
        print("[step] 等 Clerk 验证码邮件（IMAP 扫 INBOX+Junk，最长 180s）...")
        code = mailbox.wait_for_code(
            mail_account,
            keyword="",  # Clerk 邮件主题/正文可能不含 "AIHubMix"，空 keyword 全扫
            timeout=180,
            before_ids=before_ids,
            code_pattern=r"(?<!\d)(\d{6})(?!\d)",
        )
        return code or ""

    def log_fn(msg: str) -> None:
        print(msg)

    registrar = AIHubMixBrowserRegistrar(
        proxy=None,
        otp_callback=otp_callback,
        api_key_name=API_KEY_NAME,
        timeout=300,
        headless=False,
        log_fn=log_fn,
    )

    print(f"[step] 启动浏览器注册: email={EMAIL}, password={PASSWORD}")
    t0 = time.time()
    try:
        result = registrar.run(email=EMAIL, password=PASSWORD)
    except Exception as exc:
        print(f"[FAIL] 注册失败: {type(exc).__name__}: {exc}")
        return 3

    elapsed = time.time() - t0
    print(f"[step] 注册流程耗时: {elapsed:.1f}s")
    api_key = str(result.get("api_key") or "").strip()
    if not api_key:
        print(f"[FAIL] 未拿到 api_key, result keys={list(result.keys())}")
        return 4

    print(f"[result] api_key = {api_key}")
    print(f"[result] api_key_source = {result.get('api_key_source')}")
    print(f"[result] api_verification = {result.get('api_verification')}")

    # 独立验证 key
    client = AIHubMixClient(proxy=None, log_fn=log_fn)
    ok = client.verify_api_key(api_key)
    print(f"[verify] verify_api_key = {ok}")
    if not ok:
        print("[FAIL] API key 验证失败")
        return 5

    try:
        models = client.list_models_raw(api_key)
        data = models.get("data", []) if isinstance(models, dict) else []
        print(f"[verify] list_models_raw 返回 {len(data)} 个模型")
        if data:
            print(f"[verify] 前 3 个模型: {[m.get('id') for m in data[:3]]}")
    except Exception as exc:
        print(f"[warn] list_models_raw 失败（不影响注册成功判定）: {exc}")

    print("[DONE] outlook 邮箱注册 aihubmix 实测成功 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
