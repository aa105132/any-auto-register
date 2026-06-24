"""AIHubMix Google OAuth 注册测试（复制本机 Chrome Default profile 到临时目录）。

本机 Chrome Default profile 可能已登录 Google，但 launch_persistent_context 不允许
用默认 User Data 目录开 remote debugging。这里先复制 Default profile 到临时目录，
再用临时目录启动 Playwright，保留 Google 登录会话。

用法：
    python scripts/test_aihubmix_google_oauth_copy.py
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import tempfile
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


def _copy_chrome_default_profile() -> str:
    """复制本机 Chrome Default profile 到临时目录，返回临时 user-data-dir 路径。

    只复制 Cookies / Login Data 等关键文件（不复制 Cache/Code Cache 等大目录），
    保留 Google 登录会话但避免复制几 GB 的缓存。
    """
    src_root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    src_default = os.path.join(src_root, "Default")
    if not os.path.isdir(src_default):
        raise RuntimeError(f"本机 Chrome Default profile 不存在: {src_default}")

    tmp_root = tempfile.mkdtemp(prefix="aihubmix_chrome_")
    tmp_default = os.path.join(tmp_root, "Default")
    os.makedirs(tmp_default, exist_ok=True)

    # 复制 Default profile 里保留登录态的关键文件（跳过大目录）
    keep_files = {
        "Cookies", "Login Data", "Login Data For Account", "Web Data",
        "Preferences", "Secure Preferences", "Local State",
    }
    # Local State 在 User Data 根目录
    local_state = os.path.join(src_root, "Local State")
    if os.path.isfile(local_state):
        shutil.copy2(local_state, os.path.join(tmp_root, "Local State"))
    for name in keep_files:
        src = os.path.join(src_default, name)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(tmp_default, name))
            except Exception:
                pass
    # 也复制整个 Default 目录（除了大目录），保留所有登录态
    skip_dirs = {"Cache", "Code Cache", "GPUCache", "Service Worker", "Storage", "IndexedDB", "Sessions", "File System"}
    try:
        for item in os.listdir(src_default):
            s = os.path.join(src_default, item)
            d = os.path.join(tmp_default, item)
            if os.path.isdir(s):
                if item in skip_dirs:
                    continue
                try:
                    shutil.copytree(s, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns("Cache*", "*.log"))
                except Exception:
                    pass
    except Exception:
        pass

    return tmp_root


def main() -> int:
    from core.registry import load_all
    load_all()

    logger = _Logger()

    print("=" * 70)
    print("AIHubMix Google OAuth 注册测试 (复制本机 Chrome Default profile)")
    print("=" * 70)

    # 复制本机 Chrome Default profile（保留 Google 登录态）
    print("[INFO] 复制本机 Chrome Default profile 到临时目录...")
    try:
        chrome_user_data_dir = _copy_chrome_default_profile()
    except Exception as exc:
        print(f"[FAIL] 复制 Chrome profile 失败: {exc}")
        return 2
    print(f"[INFO] 临时 profile 目录: {chrome_user_data_dir}")

    from platforms.aihubmix.browser_oauth import register_with_browser_oauth

    # 不从 Google 账号池取号——直接用复制 profile 里已登录的 Google 账号。
    # email_hint 留空，让 drive_google_oauth 从浏览器读取已登录账号。
    # 但 drive_google_oauth 需要 email_hint 来验证，所以先试空，失败再传池账号。
    start = time.time()
    try:
        result = register_with_browser_oauth(
            proxy=None,
            oauth_provider="google",
            email_hint="",  # 让浏览器自动选已登录的 Google 账号
            google_password="",
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
    finally:
        # 清理临时 profile
        try:
            shutil.rmtree(chrome_user_data_dir, ignore_errors=True)
        except Exception:
            pass

    elapsed = time.time() - start
    print(f"\n[INFO] Google OAuth 流程耗时 {elapsed:.1f}s")
    api_key = str(result.get("api_key") or "").strip()
    print(f"[INFO] email={result.get('email')}")
    print(f"[INFO] api_key={api_key[:12]}...{api_key[-4:] if len(api_key)>16 else api_key}")

    if not api_key or not api_key.startswith("sk-"):
        print(f"\n[FAIL] 未拿到 sk- API key (got: {api_key[:20]!r})")
        return 1

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
