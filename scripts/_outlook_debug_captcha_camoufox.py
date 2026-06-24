"""诊断 camoufox (Firefox) 下 PX 验证码 iframe 结构。

用 resin IP 跑到验证码阶段，dump 所有 frame + 按钮结构，找出 Firefox 下的正确 selector。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_store import config_store
from core.resin_proxy import resolve_resin_proxy_config
from core.proxy_utils import build_playwright_proxy_settings
from platforms.outlook.browser_register import (
    OutlookBrowserRegister,
    _random_birthdate,
    _random_first_name,
    _random_last_name,
    generate_strong_password,
    random_email_local,
)
from platforms.outlook.constants import (
    SEL_ARKOSE_OUTER_IFRAME,
    SEL_ARKOSE_OUTER_IFRAME_EN,
    SEL_ENFORCEMENT_FRAME,
)


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    cfg = {
        "resin_enabled": "true",
        "resin_scheme": config_store.get("resin_scheme", ""),
        "resin_host": config_store.get("resin_host", ""),
        "resin_port": config_store.get("resin_port", ""),
        "resin_token": config_store.get("resin_token", ""),
        "resin_default_platform": "Default",
        "resin_platform_map": config_store.get("resin_platform_map", ""),
    }

    out_dir = Path(__file__).resolve().parent

    for slot in [1, 2, 4, 5, 8, 10, 14, 20]:
        resolved = resolve_resin_proxy_config(cfg, task_platform="outlook", account=f"ol{slot}", require_enabled=True)
        proxy = resolved.get("proxy_url")
        if not proxy:
            continue
        log(f"=== slot {slot} proxy={proxy[:50]}... ===")

        reg = OutlookBrowserRegister(
            headless=False,
            proxy=proxy,
            email_suffix="@outlook.com",
            bot_protection_wait=11,
            max_captcha_retries=3,
            use_camoufox=True,
            use_protocol_proof=False,
            register_timeout=300,
            oauth_timeout=120,
            extra={},
            log_fn=log,
        )
        pw_or_camoufox, browser, ctx, owns_camoufox = reg._launch_browser()
        page = None
        try:
            page = ctx.new_page()
            ctx.set_default_timeout(45000)
            email_local = random_email_local()
            password = generate_strong_password()
            year, month, day = _random_birthdate()
            first = _random_first_name()
            last = _random_last_name()

            if not reg._open_signup_page(page):
                log("open_signup 失败，换 slot")
                continue
            if not reg._fill_email_and_password(page, email_local, password):
                log("fill_email_password 失败，换 slot")
                continue
            if not reg._fill_birthdate(page, year, month, day):
                log("fill_birthdate 失败，换 slot")
                continue
            if not reg._fill_name_and_submit(page, first, last):
                log("fill_name 失败，换 slot")
                continue

            # 等验证码 iframe
            log("等验证码 iframe...")
            captcha_found = False
            for _ in range(30):
                for sel in (SEL_ARKOSE_OUTER_IFRAME, SEL_ARKOSE_OUTER_IFRAME_EN, SEL_ENFORCEMENT_FRAME):
                    if page.locator(sel).count() > 0:
                        log(f"验证码 iframe: {sel}")
                        captcha_found = True
                        break
                if captcha_found:
                    break
                page.wait_for_timeout(1000)

            if not captcha_found:
                log("未等到验证码 iframe")
                continue

            # 等内层加载
            log("等内层加载 10s...")
            page.wait_for_timeout(10000)

            # 截图
            page.screenshot(path=str(out_dir / f"_camoufox_captcha_{slot}.png"), full_page=True)

            # dump 所有 frame
            log("=== 所有 frame ===")
            for i, frame in enumerate(page.frames):
                try:
                    log(f"  frame[{i}] url={frame.url[:150]}")
                except Exception:
                    log(f"  frame[{i}] url=<err>")

            # dump Arkose outer iframe 内容
            for outer_sel in (SEL_ARKOSE_OUTER_IFRAME, SEL_ARKOSE_OUTER_IFRAME_EN):
                try:
                    if page.locator(outer_sel).count() == 0:
                        continue
                    log(f"=== outer iframe {outer_sel} ===")
                    outer = page.frame_locator(outer_sel)
                    try:
                        html = outer.locator("body").inner_html(timeout=5000)
                        log(f"  outer body html (前1500字): {html[:1500]}")
                    except Exception as e:
                        log(f"  outer body html 失败: {e}")

                    # 列出所有按钮
                    btns = outer.locator("button, [role='button'], a, [aria-label]").all()
                    log(f"  outer 元素数: {len(btns)}")
                    for j, btn in enumerate(btns[:20]):
                        try:
                            aria = btn.get_attribute("aria-label", timeout=2000)
                            text = btn.inner_text(timeout=2000)
                            tag = btn.evaluate("el => el.tagName")
                            log(f"    outer[{j}] tag={tag} aria={aria!r} text={text!r}")
                        except Exception:
                            pass

                    # 内层 iframe
                    from platforms.outlook.constants import SEL_ARKOSE_INNER_IFRAME
                    try:
                        inner = outer.frame_locator(SEL_ARKOSE_INNER_IFRAME)
                        html = inner.locator("body").inner_html(timeout=5000)
                        log(f"  inner body html (前1500字): {html[:1500]}")
                        btns = inner.locator("button, [role='button'], a, [aria-label]").all()
                        log(f"  inner 元素数: {len(btns)}")
                        for j, btn in enumerate(btns[:20]):
                            try:
                                aria = btn.get_attribute("aria-label", timeout=2000)
                                text = btn.inner_text(timeout=2000)
                                tag = btn.evaluate("el => el.tagName")
                                log(f"    inner[{j}] tag={tag} aria={aria!r} text={text!r}")
                            except Exception:
                                pass
                    except Exception as e:
                        log(f"  inner iframe dump 失败: {e}")
                except Exception as e:
                    log(f"  outer {outer_sel} 枚举失败: {e}")

            log("诊断完成（这个 slot 跑到验证码了）")
            return
        finally:
            reg._close_browser(pw_or_camoufox, ctx, owns_camoufox)
    log("所有 slot 都没到验证码阶段")


if __name__ == "__main__":
    main()
