"""用已注册账号 wknfpiprcdgno@outlook.com 登录 Outlook Web，去设置页启用 POP/IMAP。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests as req_lib

CLASH_API = "http://127.0.0.1:9097"
CLASH_SECRET = "set-your-secret"
CLASH_SELECTOR = "🔰 选择节点"
CLASH_PROXY = "http://127.0.0.1:7897"

EMAIL = "wknfpiprcdgno@outlook.com"
PASSWORD = "ESRyI^2zQSwy@E"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def main():
    node = "🇯🇵 日本W09 | IEPL"
    log(f"切换到 {node}")
    H = {"Authorization": f"Bearer {CLASH_SECRET}", "Content-Type": "application/json"}
    req_lib.put(f"{CLASH_API}/proxies/{req_lib.utils.quote(CLASH_SELECTOR)}", headers=H, json={"name": node}, timeout=8)
    time.sleep(1.5)

    from patchright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False, args=["--lang=zh-CN"], proxy={"server": CLASH_PROXY})
    ctx = browser.new_context(viewport={"width": 1366, "height": 800}, locale="zh-CN")
    page = ctx.new_page()
    ctx.set_default_timeout(30000)

    out_dir = Path(__file__).resolve().parent

    try:
        # 登录 Outlook — 用 login.live.com 直接到登录表单
        log("=== 登录 Outlook ===")
        page.goto("https://login.live.com/login.srf?wa=wsignin1.0&rpsnv=13&ct=1&rver=7.0.6737.0&wp=MBI_SSL&wreply=https%3A%2F%2Foutlook.live.com%2Fmail%2F%3Fauth%3D1%26rru%3D%2Fmail%2F", timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        log(f"初始 URL: {page.url[:120]}")

        # 等登录页渲染
        for _ in range(20):
            try:
                if page.locator('[name="loginfmt"]').count() > 0:
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000)
        log(f"登录页 URL: {page.url[:120]}")

        # 填邮箱
        try:
            page.locator('[name="loginfmt"]').wait_for(state="visible", timeout=15000)
            page.locator('[name="loginfmt"]').fill(EMAIL, timeout=10000)
            page.wait_for_timeout(500)
            page.locator('#idSIButton9').click(timeout=7000)
            log("已填邮箱并提交")
            page.wait_for_timeout(4000)
        except Exception as e:
            log(f"邮箱步骤: {e}")

        # 填密码
        try:
            page.locator('[type="password"]').wait_for(state="visible", timeout=15000)
            page.locator('[type="password"]').fill(PASSWORD, timeout=10000)
            page.wait_for_timeout(500)
            page.locator('#idSIButton9').click(timeout=7000)
            log("已填密码并提交")
            page.wait_for_timeout(4000)
        except Exception as e:
            log(f"密码步骤: {e}")

        # KMSI（保持登录）
        try:
            kmsi = page.locator('#idSIButton9')
            kmsi.wait_for(state="visible", timeout=10000)
            if kmsi.count() > 0:
                kmsi.first.click(timeout=5000)
                log("已点击保持登录")
                page.wait_for_timeout(5000)
        except Exception:
            pass

        log(f"登录后 URL: {page.url[:120]}")
        page.screenshot(path=str(out_dir / "_imap_debug_01_login.png"))

        # 等收件箱加载
        log("等收件箱加载...")
        page.wait_for_timeout(8000)
        log(f"收件箱 URL: {page.url[:120]}")
        page.screenshot(path=str(out_dir / "_imap_debug_02_inbox.png"))

        # 去 POP/IMAP 设置页
        log("=== 去 POP/IMAP 设置页 ===")
        page.goto("https://outlook.live.com/mail/0/options/accounts/pop-imap", timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        log(f"设置页 URL: {page.url[:120]}")
        page.screenshot(path=str(out_dir / "_imap_debug_02_settings.png"))

        # dump body
        try:
            body = page.inner_text("body", timeout=5000)
            log(f"设置页 body: {' '.join(body.split())[:600]}")
        except Exception:
            pass

        # 找 POP 启用选项
        log("=== 找 POP 启用选项 ===")
        # 截图当前状态
        page.screenshot(path=str(out_dir / "_imap_debug_03_before_enable.png"))

        # 尝试各种 selector
        for sel_text in ("启用", "是", "Yes", "Enable", "On", "开启", "Let devices and apps use POP", "让设备和应用使用 POP"):
            try:
                btn = page.get_by_role("button", name=sel_text).first
                if btn.count() > 0:
                    btn.click(timeout=5000)
                    log(f"点击按钮: {sel_text}")
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # 尝试 select
        for sel_text in ("是", "Yes", "启用", "Enable"):
            try:
                sel = page.locator('select').first
                if sel.count() > 0:
                    sel.select_option(label=sel_text, timeout=5000)
                    log(f"select 选: {sel_text}")
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # 尝试 radio
        for sel_text in ("是", "Yes", "启用", "Enable"):
            try:
                radio = page.get_by_label(sel_text, exact=False).first
                if radio.count() > 0:
                    radio.click(timeout=5000)
                    log(f"radio 选: {sel_text}")
                    page.wait_for_timeout(2000)
                    break
            except Exception:
                continue

        # 保存
        for sel_text in ("保存", "Save", "确定", "OK"):
            try:
                save_btn = page.get_by_role("button", name=sel_text).first
                if save_btn.count() > 0:
                    save_btn.click(timeout=5000)
                    log(f"点击保存: {sel_text}")
                    page.wait_for_timeout(3000)
                    break
            except Exception:
                continue

        page.screenshot(path=str(out_dir / "_imap_debug_04_after_enable.png"))

        # dump body again
        try:
            body = page.inner_text("body", timeout=5000)
            log(f"启用后 body: {' '.join(body.split())[:600]}")
        except Exception:
            pass

        log("=== 完成 ===")

    finally:
        ctx.close()
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
