"""Outlook 注册流程逐步调试探针。

逐步执行注册流程，每步后截图 + dump URL + body 文本片段，便于定位真实页面结构。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OUT_DIR = Path(__file__).resolve().parent / "_outlook_debug"
OUT_DIR.mkdir(exist_ok=True)


def dump(page, name: str):
    url = page.url or ""
    try:
        body = page.inner_text("body", timeout=3000)[:400].replace("\n", " | ")
    except Exception:
        body = "(body read fail)"
    shot = OUT_DIR / f"{name}.png"
    try:
        page.screenshot(path=str(shot), full_page=False)
    except Exception as e:
        print(f"[{name}] screenshot fail: {e}")
    print(f"[{name}] url={url[:120]}", flush=True)
    print(f"[{name}] body={body[:300]}", flush=True)
    # dump 所有可见 input/button 的 selector 摘要
    try:
        info = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const out = [];
              for (const el of document.querySelectorAll('input, button, [role="button"], select, [data-testid]')) {
                if (!visible(el)) continue;
                out.push({
                  tag: el.tagName,
                  type: el.getAttribute('type') || '',
                  name: el.getAttribute('name') || '',
                  id: el.id || '',
                  aria: el.getAttribute('aria-label') || '',
                  testid: el.getAttribute('data-testid') || '',
                  role: el.getAttribute('role') || '',
                  text: (el.innerText || el.value || '').slice(0, 40),
                });
              }
              return out.slice(0, 30);
            }
            """
        )
        print(f"[{name}] elements:", flush=True)
        for el in info:
            print(f"    {el}", flush=True)
    except Exception as e:
        print(f"[{name}] eval fail: {e}", flush=True)
    print("---", flush=True)


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
        print("[debug] goto signup...", flush=True)
        page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=30000, wait_until="domcontentloaded")
        time.sleep(2)
        dump(page, "01_landed")

        # 同意并继续
        try:
            page.get_by_text("同意并继续").wait_for(timeout=30000)
            page.get_by_text("同意并继续").click(timeout=10000)
            print("[debug] 已点击同意并继续", flush=True)
        except Exception as e:
            print(f"[debug] 同意并继续未出现: {e}", flush=True)
        time.sleep(3)
        dump(page, "02_after_agree")

        # 切后缀 + 填邮箱
        try:
            page.locator('[aria-label="新建电子邮件"]').type("debugtest" + str(int(time.time()))[-6:], delay=60, timeout=10000)
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            print("[debug] 邮箱已填并提交", flush=True)
        except Exception as e:
            print(f"[debug] 填邮箱失败: {e}", flush=True)
        time.sleep(3)
        dump(page, "03_after_email")

        # 填密码
        try:
            page.locator('[type="password"]').type("DebugP@ss1234!", delay=40, timeout=10000)
            page.wait_for_timeout(300)
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            print("[debug] 密码已填并提交", flush=True)
        except Exception as e:
            print(f"[debug] 填密码失败: {e}", flush=True)
        time.sleep(3)
        dump(page, "04_after_password")

        # 填生日
        try:
            page.locator('[name="BirthYear"]').fill("1990", timeout=10000)
            try:
                page.locator('[name="BirthMonth"]').select_option(value="5", timeout=2000)
                page.locator('[name="BirthDay"]').select_option(value="15", timeout=2000)
            except Exception:
                page.locator('[name="BirthMonth"]').click()
                time.sleep(0.3)
                page.locator('[role="option"]:text-is("5月")').click(timeout=5000)
                page.locator('[name="BirthDay"]').click()
                time.sleep(0.3)
                page.locator('[role="option"]:text-is("15日")').click(timeout=5000)
            print("[debug] 生日已填", flush=True)
        except Exception as e:
            print(f"[debug] 填生日失败: {e}", flush=True)
        time.sleep(1)
        dump(page, "05_after_birthdate_fill")

        # 点 primaryButton 提交生日
        try:
            page.locator('[data-testid="primaryButton"]').click(timeout=5000)
            print("[debug] 生日提交", flush=True)
        except Exception as e:
            print(f"[debug] 生日提交失败: {e}", flush=True)
        time.sleep(5)
        dump(page, "06_after_birthdate_submit")

        # 等等再看（可能是姓名页或验证码）
        time.sleep(5)
        dump(page, "07_after_wait")

        # 再等更久看验证码
        time.sleep(10)
        dump(page, "08_after_long_wait")

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
