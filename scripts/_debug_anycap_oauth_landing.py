"""诊断 AnyCap OAuth 登录页加载现场。

复现 platforms/anycap/browser_oauth.py 的前几步（打开 LOGIN_URL → 点 Google），
但打印 URL/body/按钮现场并截图，定位 drive_google_oauth 找不到 google 页面的原因。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    for _st_name in ("stdout", "stderr"):
        _st = getattr(sys, _st_name, None)
        if _st and hasattr(_st, "reconfigure"):
            try:
                _st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

LOGIN_URL = "https://anycap.ai/api/auth/login?returnTo=%2Fauth%2Fcallback"
SHOT_DIR = ROOT / "scripts"


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy", default="http://127.0.0.1:7897")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--chrome-cdp-url", default="")
    parser.add_argument("--chrome-user-data-dir", default="")
    parser.add_argument("--url", default=LOGIN_URL, help="要打开的登录页 URL")
    parser.add_argument("--stay", type=int, default=20, help="点击 Google 后观察秒数")
    args = parser.parse_args()

    from core.oauth_browser import OAuthBrowser, try_click_provider_on_page

    proxy = (args.proxy or "").strip() or None
    log(f"打开 AnyCap 登录页: {args.url} proxy={proxy} headless={args.headless}")

    with OAuthBrowser(
        proxy=proxy,
        headless=args.headless,
        chrome_user_data_dir=args.chrome_user_data_dir,
        chrome_cdp_url=args.chrome_cdp_url,
        log_fn=log,
    ) as browser:
        page = browser.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log(f"goto 失败: {exc!r}")
            return 1
        time.sleep(3)

        def snap(tag: str) -> None:
            url = page.url or ""
            try:
                body = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
            except Exception as exc:
                body = f"<eval failed: {exc!r}>"
            log(f"--- snap[{tag}] url={url[:120]}")
            log(f"--- snap[{tag}] body[:400]={body[:400]!r}")
            # 列出页面上所有按钮文本，看 Google 按钮是否存在
            try:
                buttons = page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('button,a,[role="button"],input[type="submit"]'))
                      .map(n => ({tag: n.tagName, text: (n.innerText||n.textContent||n.value||n.getAttribute('aria-label')||'').trim().slice(0,60), href: n.getAttribute('href')||''}))
                      .filter(x => x.text).slice(0, 25)
                    """
                )
            except Exception as exc:
                buttons = f"<eval failed: {exc!r}>"
            log(f"--- snap[{tag}] buttons={buttons}")
            try:
                page.screenshot(path=str(SHOT_DIR / f"_anycap_diag_{tag}.png"), full_page=True)
            except Exception as exc:
                log(f"截图失败[{tag}]: {exc!r}")

        snap("after_load")

        log("尝试点击 Google 按钮...")
        clicked = try_click_provider_on_page(page, "google")
        log(f"try_click_provider_on_page(google) -> {clicked}")
        time.sleep(5)
        snap("after_click_google")

        # 观察 stay 秒，记录每 2s 的 URL
        deadline = time.time() + args.stay
        while time.time() < deadline:
            urls = [p.url or "" for p in browser.pages() if not p.is_closed()]
            log(f"pages urls={urls}")
            time.sleep(2)

        snap("final")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
