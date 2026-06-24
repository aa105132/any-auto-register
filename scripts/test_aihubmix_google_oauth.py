"""AIHubMix Google OAuth 注册测试（用本地 Chrome 已登录的 Google profile）。

用 chrome_user_data_dir 复用本机 Chrome 的 Default profile，如果该 profile 已登录 Google，
drive_google_oauth 会自动选号（auto_select_google_account），跳过 Google 登录页 + reCAPTCHA。

用法：
    python scripts/test_aihubmix_google_oauth.py
"""
from __future__ import annotations

import os
import sys
import time
import traceback

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

if sys.platform == "win32":
    for _sn in ("stdout", "stderr"):
        _st = getattr(sys, _sn, None)
        if _st and hasattr(_st, "reconfigure"):
            try:
                _st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


class _Logger:
    def log(self, msg: str, *args, **kwargs):
        try:
            print(msg, *args, **kwargs)
        except UnicodeEncodeError:
            sys.stdout.buffer.write(str(msg).encode("utf-8", errors="replace") + b"\n")
            sys.stdout.buffer.flush()


def main() -> int:
    from core.registry import load_all
    load_all()

    # 本机 Chrome Default profile（可能已登录 Google）
    chrome_user_data_dir = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    logger = _Logger()

    print("=" * 70)
    print("AIHubMix Google OAuth 注册测试 (chrome_user_data_dir 复用本机 Chrome)")
    print(f"chrome_user_data_dir={chrome_user_data_dir}")
    print("=" * 70)

    from platforms.aihubmix.browser_oauth import register_with_browser_oauth

    # 先从 Google 账号池取一个 Gmail（如果 Chrome profile 已登录该号则自动选号，
    # 否则 drive_google_oauth 会尝试登录该号——可能触发 reCAPTCHA）
    from core.google_account_pool import GoogleAccountPool
    pool = GoogleAccountPool()
    acct = pool.acquire(exclude_platforms=["aihubmix"])
    if not acct:
        print("[FAIL] Google 账号池无可用账号")
        return 2
    email_hint = acct.email
    google_password = acct.password or ""
    print(f"[INFO] Google 账号池: email={email_hint} has_password={bool(google_password)}")

    start = time.time()
    try:
        result = register_with_browser_oauth(
            proxy=None,
            oauth_provider="google",
            email_hint=email_hint,
            google_password=google_password,
            totp_secret="",
            timeout=180,
            log_fn=logger.log,
            headless=False,
            chrome_user_data_dir=chrome_user_data_dir,
            chrome_cdp_url="",
            use_camoufox=False,
            cancel_token=None,
        )
    except KeyboardInterrupt:
        print("\n[STOP] 用户中断")
        return 130
    except Exception as exc:
        print(f"\n[FAIL] Google OAuth 抛异常: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1

    elapsed = time.time() - start
    print(f"\n[INFO] Google OAuth 流程耗时 {elapsed:.1f}s")
    api_key = str(result.get("api_key") or "").strip()
    print(f"[INFO] email={result.get('email')}")
    print(f"[INFO] api_key={api_key[:12]}...{api_key[-4:] if len(api_key)>16 else api_key}")
    print(f"[INFO] api_key_source={result.get('api_key_source')}")

    if not api_key or not api_key.startswith("sk-"):
        print(f"\n[FAIL] 未拿到 sk- API key (got: {api_key[:20]!r})")
        return 1

    # 验证 key
    from platforms.aihubmix.core import AIHubMixClient
    client = AIHubMixClient(proxy=None, log_fn=lambda m: print(f"  [verify] {m}"))
    try:
        valid = client.verify_api_key(api_key)
    except Exception as exc:
        print(f"\n[FAIL] verify_api_key 抛异常: {type(exc).__name__}: {exc}")
        return 1
    if not valid:
        print(f"\n[FAIL] verify_api_key 返回 False")
        return 1

    print(f"\n[SUCCESS] aihubmix.com Google OAuth 注册实测成功!")
    print(f"[SUCCESS] email={result.get('email')}")
    print(f"[SUCCESS] api_key={api_key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
