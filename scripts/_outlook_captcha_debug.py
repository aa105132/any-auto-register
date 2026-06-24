"""Outlook 注册验证码阶段调试探针。

跑到姓名提交后，dump Arkose 验证码 iframe 结构 + 截图，便于定位真实按钮选择器。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT_DIR = Path(__file__).resolve().parent / "_outlook_captcha_debug"
OUT_DIR.mkdir(exist_ok=True)


def main():
    from core.proxy_utils import build_playwright_proxy_settings
    from patchright.sync_api import sync_playwright

    proxy = "http://127.0.0.1:7897"
    proxy_cfg = build_playwright_proxy_settings(proxy)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--lang=zh-CN"],
        proxy=proxy_cfg,
    )
    ctx = browser.new_context(viewport={"width": 1366, "height": 800}, locale="zh-CN")
    page = ctx.new_page()
    ctx.set_default_timeout(20000)

    try:
        # 跑到姓名提交（复用 browser_register 的步骤函数）
        from platforms.outlook.browser_register import (
            OutlookBrowserRegister, _random_first_name, _random_last_name, _random_birthdate,
            random_email_local, generate_strong_password,
        )
        reg = OutlookBrowserRegister(
            headless=False, proxy=proxy, email_suffix="@outlook.com",
            bot_protection_wait=11, max_captcha_retries=0,
            use_protocol_proof=False, register_timeout=300, oauth_timeout=120,
            extra={}, log_fn=lambda m: print(m, flush=True),
        )
        # 复用 reg 的步骤方法，但停在姓名提交后
        page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=30000, wait_until="domcontentloaded")
        time.sleep(2)
        try:
            page.get_by_text("同意并继续").wait_for(timeout=30000)
            page.wait_for_timeout(1100)
            page.get_by_text("同意并继续").click(timeout=10000)
        except Exception as e:
            print(f"[captcha-debug] 同意按钮: {e}")
        time.sleep(3)

        email_local = "dbg" + str(int(time.time()))[-8:]
        password = generate_strong_password()
        first_name, last_name = _random_first_name(), _random_last_name()
        year, month, day = _random_birthdate()
        print(f"[captcha-debug] email={email_local}@outlook.com pwd={password} name={first_name} {last_name} bd={year}-{month}-{day}")

        reg._fill_email_and_password(page, email_local, password)
        time.sleep(3)
        reg._fill_birthdate(page, year, month, day)
        time.sleep(2)
        reg._fill_name_and_submit(page, first_name, last_name)

        # 现在应该在验证码页或收件页。等待并 dump
        print("[captcha-debug] 姓名提交后等待验证码加载...", flush=True)
        for i in range(12):
            time.sleep(2)
            url = page.url or ""
            shot = OUT_DIR / f"frame_{i:02d}.png"
            try:
                page.screenshot(path=str(shot), full_page=False)
            except Exception:
                pass
            # 检查页面文案
            try:
                body = page.inner_text("body", timeout=2000)[:300].replace("\n", " | ")
            except Exception:
                body = "(body fail)"
            # 检查 enforcement iframe
            try:
                enf_count = page.locator('iframe#enforcementFrame').count()
            except Exception:
                enf_count = -1
            # 检查所有 iframe
            try:
                frames_info = [(f.url or "")[:80] for f in page.frames if f.url and f.url != page.url]
            except Exception:
                frames_info = []
            # 检查验证质询 iframe
            try:
                outer_count = page.locator('iframe[title="验证质询"]').count()
            except Exception:
                outer_count = -1
            print(f"[frame_{i:02d}] url={url[:80]} enf={enf_count} outer_challenge={outer_count} body={body[:150]}", flush=True)
            if frames_info:
                print(f"          frames: {frames_info[:4]}", flush=True)
            # 如果检测到验证质询 iframe，深入 dump
            if outer_count > 0 or enf_count > 0:
                print(f"[frame_{i:02d}] 检测到验证码 iframe，深入 dump...", flush=True)
                try:
                    # 列出所有 frame 的 URL
                    for idx, f in enumerate(page.frames):
                        print(f"    frame[{idx}]: url={(f.url or '')[:120]}", flush=True)
                except Exception:
                    pass
                # 尝试定位验证质询 iframe 内部结构
                try:
                    outer = page.frame_locator('iframe[title="验证质询"]')
                    # 尝试找内层 iframe
                    inner = outer.frame_locator('iframe[style*="display: block"]')
                    # dump inner 的所有元素
                    inner_html = inner.locator("body").inner_html(timeout=5000)
                    print(f"    inner html len: {len(inner_html)}", flush=True)
                    print(f"    inner html: {inner_html[:600]}", flush=True)
                except Exception as e:
                    print(f"    inner dump fail: {e}", flush=True)
                # 也试 enforcementFrame
                try:
                    enf = page.frame_locator('iframe#enforcementFrame')
                    enf_html = enf.locator("body").inner_html(timeout=5000)
                    print(f"    enforcement html len: {len(enf_html)}", flush=True)
                    print(f"    enforcement html: {enf_html[:600]}", flush=True)
                except Exception as e:
                    print(f"    enforcement dump fail: {e}", flush=True)
                time.sleep(3)

    finally:
        time.sleep(5)
        try:
            ctx.close()
            browser.close()
        except Exception:
            pass
        pw.stop()


if __name__ == "__main__":
    main()
