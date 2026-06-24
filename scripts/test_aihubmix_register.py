"""AIHubMix 实地注册测试脚本。

用 headed 执行器 + yyds_mail 邮箱身份，真实跑一次 aihubmix.com 注册流程。
成功标准：拿到 sk- API key 且 verify_api_key 通过。

用法：
    python scripts/test_aihubmix_register.py

按 Ctrl+C 中断。
"""
from __future__ import annotations

import os
import sys
import time
import traceback

# 确保项目根目录在 sys.path 上（脚本从 scripts/ 子目录运行时 cwd 不自动加入）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Windows 控制台 UTF-8 输出
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
    # 显式触发 registry 扫描，确保 aihubmix @register 已生效
    from core.registry import load_all
    load_all()

    from application.tasks import _build_platform_instance

    logger = _Logger()
    payload = {
        "executor_type": "headed",
        "captcha_solver": "auto",
        "proxy": "",  # 用 resin 自动轮换（_build_platform_instance 内部会解析）
        "extra": {
            # Google OAuth 路径：绕开 Clerk 邮箱+密码+Turnstile captcha + 临时邮箱拦截。
            # aihubmix 实测拦截临时邮箱域名（yyds_mail/moemail 被拒），luckmail 账号已过期，
            # 所以走 Google OAuth 用 HStockPlus 购买的 Gmail 账号登录。
            "identity_provider": "oauth_browser",
            "oauth_provider": "google",
            "oauth_account_source": "mailbox",  # 从 mailbox provider（hstockplus）取 Google 账号
            "mail_provider": "hstockplus_google",
            "aihubmix_headless": "false",
            "aihubmix_oauth_use_camoufox": "false",  # 用 Playwright Chromium（已验证能过 Turnstile）
            "browser_oauth_timeout": "240",  # 4 分钟超时，避免 10 分钟卡死
        },
    }

    print("=" * 70)
    print("AIHubMix 实地注册测试 (headed + yyds_mail)")
    print("=" * 70)

    try:
        platform = _build_platform_instance("aihubmix", payload, logger)
    except Exception:
        print("[FAIL] 构建 platform 实例失败:")
        traceback.print_exc()
        return 2

    print(f"[INFO] platform={platform.display_name} executor={platform.config.executor_type}")
    print(f"[INFO] mailbox={type(platform.mailbox).__name__ if platform.mailbox else 'None'}")
    print(f"[INFO] 开始注册... (Ctrl+C 中断)")

    start = time.time()
    try:
        account = platform.register()  # 不传 email/password，让 mailbox provider 分配 + 生成强密码
    except KeyboardInterrupt:
        print("\n[STOP] 用户中断")
        return 130
    except Exception as exc:
        print(f"\n[FAIL] 注册抛异常: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1

    elapsed = time.time() - start
    print(f"\n[INFO] 注册流程耗时 {elapsed:.1f}s")
    print(f"[INFO] email={account.email}")
    print(f"[INFO] token={account.token[:12]}...{account.token[-4:] if len(account.token)>16 else account.token}")
    print(f"[INFO] status={account.status}")
    extra = dict(account.extra or {})
    print(f"[INFO] api_key={extra.get('api_key','')[:12]}...")
    print(f"[INFO] api_key_source={extra.get('api_key_source','')}")
    print(f"[INFO] api_verification={extra.get('api_verification',{})}")
    print(f"[INFO] api_base={extra.get('api_base','')}")

    # 成功标准：sk- API key 非空 + verify_api_key 通过
    api_key = str(extra.get("api_key") or account.token or "").strip()
    if not api_key or not api_key.startswith("sk-"):
        print(f"\n[FAIL] 未拿到 sk- API key (got: {api_key[:20]!r})")
        return 1

    # 用 AIHubMixClient.verify_api_key 二次验证 key 有效
    from platforms.aihubmix.core import AIHubMixClient
    client = AIHubMixClient(proxy=platform.config.proxy, log_fn=lambda m: print(f"  [verify] {m}"))
    try:
        valid = client.verify_api_key(api_key)
    except Exception as exc:
        print(f"\n[FAIL] verify_api_key 抛异常: {type(exc).__name__}: {exc}")
        return 1

    if not valid:
        print(f"\n[FAIL] verify_api_key 返回 False (key 无效或 /v1/models 未返回 data list)")
        return 1

    print(f"\n[SUCCESS] aihubmix.com 注册实测成功!")
    print(f"[SUCCESS] email={account.email}")
    print(f"[SUCCESS] api_key={api_key}")
    print(f"[SUCCESS] /v1/models 验证通过 (key 有效)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
