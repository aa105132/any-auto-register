"""AnyCap Google OAuth 注册测试（用 Google 账号池里的非-gmail 号）。

流程：从 output/google_accounts_pool.json 取一个未注册 anycap 的非-gmail 谷歌号
→ 走 Clash 代理（默认 127.0.0.1:7897）→ AnyCap 登录页 → Continue with Google
→ Google 登录（复用 core/google_oauth.drive_google_oauth）→ 回调落地 anycap.ai
→ /auth/access-token → POST /v1/api-keys 拿 API Key。

用法：
    python scripts/test_anycap_google_oauth.py
可选：
    --proxy http://127.0.0.1:7897 --headless --reuse <email> --reuse-password <pw>
    --chrome-cdp-url http://127.0.0.1:9222 --chrome-user-data-dir <dir> --timeout 300
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    for _sn in ("stdout", "stderr"):
        _st = getattr(sys, _sn, None)
        if _st and hasattr(_st, "reconfigure"):
            try:
                _st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

DEFAULT_PROXY = "http://127.0.0.1:7897"
OUT = ROOT / "scripts" / "_anycap_google_oauth_result.json"


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


def _is_non_gmail(email: str) -> bool:
    return "gmail.com" not in (email or "").strip().lower()


def _pick_pool_account(exclude_platforms: list[str]) -> tuple[str, str, str] | None:
    """从 Google 账号池挑一个非-gmail、valid、未注册指定平台的账号。

    返回 (email, password, totp_secret) 或 None。
    """
    from core.google_account_pool import GoogleAccountPool

    pool = GoogleAccountPool()
    # 先按邮箱顺序找候选，再用 get_by_email 原子占用，避免串号。
    for acct in pool.list_all():
        if str(acct.status or "valid").strip().lower() == "invalid":
            continue
        if not _is_non_gmail(acct.email):
            continue
        registered = {p.lower() for p in (acct.registered_platforms or [])}
        if any(p.lower() in registered for p in exclude_platforms):
            continue
        reserved = pool.get_by_email(acct.email, exclude_platforms=exclude_platforms)
        if reserved is None:
            continue
        return reserved.email, reserved.password or "", reserved.totp_secret or ""
    return None


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default=DEFAULT_PROXY, help="出口代理（默认 Clash 127.0.0.1:7897），传空禁用")
    parser.add_argument("--headless", action="store_true", help="无头模式（默认有头便于观察）")
    parser.add_argument("--reuse", default="", help="复用指定邮箱（跳过池子筛选）")
    parser.add_argument("--reuse-password", default="", help="复用号的 Google 密码")
    parser.add_argument("--timeout", type=int, default=300, help="OAuth 超时秒数")
    parser.add_argument("--chrome-cdp-url", default="", help="复用本机已登录 Chrome（CDP URL）")
    parser.add_argument("--chrome-user-data-dir", default="", help="复用 Chrome profile 目录")
    parser.add_argument("--api-key-name", default="", help="AnyCap API Key 名称（默认 auto-register-<ts>）")
    parser.add_argument("--camoufox", action="store_true", default=True, help="用 Camoufox 反检测浏览器（默认开启，绕过 Google signin/rejected）")
    parser.add_argument("--no-camoufox", dest="camoufox", action="store_false", help="禁用 Camoufox，回退 Playwright Chromium")
    args = parser.parse_args()

    from core.registry import load_all
    load_all()

    # Step 1: 取非-gmail 谷歌账号
    if args.reuse:
        email = args.reuse.strip()
        google_password = args.reuse_password
        log(f"复用指定邮箱: {email}")
    else:
        log("从 Google 账号池筛选非-gmail 账号...")
        picked = _pick_pool_account(["anycap"])
        if not picked:
            log("[FAIL] 池子没有可用的非-gmail 谷歌账号")
            return 2
        email, google_password, totp_secret = picked
        log(f"选中池子账号: {email} (pw_len={len(google_password)} totp={bool(totp_secret)})")

    api_key_name = args.api_key_name or f"auto-register-{int(time.time())}"
    proxy = (args.proxy or "").strip() or None

    # Step 2: 跑 AnyCap Google OAuth
    log(f"=== 开始 AnyCap Google OAuth: {email} proxy={proxy} headless={args.headless} ===")
    from platforms.anycap.browser_oauth import register_with_browser_oauth

    start = time.time()
    try:
        result = register_with_browser_oauth(
            proxy=proxy,
            oauth_provider="google",
            email_hint=email,
            google_password=google_password,
            timeout=args.timeout,
            log_fn=log,
            headless=args.headless,
            chrome_user_data_dir=args.chrome_user_data_dir,
            chrome_cdp_url=args.chrome_cdp_url,
            api_key_name=api_key_name,
            use_camoufox=args.camoufox,
        )
    except KeyboardInterrupt:
        log("\n[STOP] 用户中断")
        return 130
    except Exception as exc:
        log(f"\n[FAIL] AnyCap OAuth 抛异常: {type(exc).__name__}: {repr(exc)[:400]}")
        traceback.print_exc()
        OUT.write_text(json.dumps({
            "ok": False,
            "email": email,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"失败现场已保存 -> {OUT}")
        return 1

    elapsed = time.time() - start
    api_key = str(result.get("api_key") or "").strip()
    log(f"[INFO] OAuth 流程耗时 {elapsed:.1f}s")
    log(f"[INFO] email={result.get('email')}")
    log(f"[INFO] api_key={api_key[:10]}...{api_key[-4:] if len(api_key)>14 else api_key}")
    log(f"[INFO] access_token={(result.get('access_token') or '')[:20]}...")
    log(f"[INFO] api_verification={result.get('api_verification')}")

    summary = {
        "ok": bool(api_key),
        "email": result.get("email"),
        "api_key": api_key,
        "access_token": result.get("access_token"),
        "api_key_info": result.get("api_key_info"),
        "api_verification": result.get("api_verification"),
        "key_create_result": result.get("key_create_result"),
        "profile": result.get("profile"),
        "cookies": result.get("cookies"),
        "elapsed_sec": round(elapsed, 1),
    }
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"完整结果保存 -> {OUT}")

    if api_key:
        log("★ AnyCap Google OAuth 实测成功，已拿到 API Key")
        # 标记池子账号注册完成
        try:
            from core.google_account_pool import GoogleAccountPool
            GoogleAccountPool().mark_registered(email, "anycap")
            log(f"已标记池子账号 {email} 完成 anycap 注册")
        except Exception as exc:
            log(f"标记池子注册失败（不影响结果）: {exc}")
        return 0
    log("[FAIL] OAuth 流程结束但未拿到 API Key")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
