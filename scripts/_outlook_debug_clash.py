"""诊断（Clash 代理版）：验证 fill 密码修复后页面能跳到出生日期页。

跑一次，填完密码点 primaryButton 后等待 30s，期间每 2s 打印页面状态。
用 Clash 当前节点（日本W01 IEPL 住宅 IP）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platforms.outlook.browser_register import (
    OutlookBrowserRegister,
    _random_birthdate,
    _random_first_name,
    _random_last_name,
    generate_strong_password,
    random_email_local,
)
from platforms.outlook.constants import (
    ACCOUNT_BLOCKED_TEXTS,
    RATE_LIMIT_TEXTS,
    SEL_ARKOSE_OUTER_IFRAME,
    SEL_ARKOSE_OUTER_IFRAME_EN,
    SEL_BIRTH_YEAR,
    SEL_ENFORCEMENT_FRAME,
    SEL_LAST_NAME,
    SEL_NEW_MAIL_BUTTON,
    SEL_NEW_MAIL_BUTTON_EN,
    SEL_PASSWORD_INPUT,
    SEL_PRIMARY_BUTTON,
)

CLASH_PROXY = "http://127.0.0.1:7897"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def main():
    proxy = CLASH_PROXY
    log(f"用 Clash 代理 {proxy}")
    reg = OutlookBrowserRegister(
        headless=False,  # 有头便于观察
        proxy=proxy,
        email_suffix="@outlook.com",
        bot_protection_wait=11,
        max_captcha_retries=3,
        use_camoufox=False,
        use_protocol_proof=True,
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
        first = _random_first_name()
        last = _random_last_name()
        year, month, day = _random_birthdate()
        log(f"准备: {email_local}@outlook.com  pwd={password}  birth={year}-{month}-{day}")

        if not reg._open_signup_page(page):
            log("open_signup 失败")
            return
        if not reg._fill_email_and_password(page, email_local, password):
            log("fill_email_password 失败")
            return

        log("密码已提交，开始 30s 诊断窗口...")
        out_dir = Path(__file__).resolve().parent
        for i in range(15):
            time.sleep(2)
            try:
                url = page.url
            except Exception:
                url = "<closed>"
            try:
                body = page.inner_text("body", timeout=1500)
            except Exception:
                body = "<err>"
            body_short = " ".join(body.split())[:400]
            checks = {}
            for name, sel in [
                ("BirthYear", SEL_BIRTH_YEAR),
                ("lastName", SEL_LAST_NAME),
                ("enforcement", SEL_ENFORCEMENT_FRAME),
                ("arkoseOuter", SEL_ARKOSE_OUTER_IFRAME),
                ("arkoseOuterEN", SEL_ARKOSE_OUTER_IFRAME_EN),
                ("newMail", SEL_NEW_MAIL_BUTTON),
                ("newMailEN", SEL_NEW_MAIL_BUTTON_EN),
                ("pwdInput", SEL_PASSWORD_INPUT),
                ("primaryBtn", SEL_PRIMARY_BUTTON),
            ]:
                try:
                    checks[name] = page.locator(sel).count()
                except Exception:
                    checks[name] = "err"
            block_texts = []
            for t in ACCOUNT_BLOCKED_TEXTS + RATE_LIMIT_TEXTS:
                try:
                    if page.get_by_text(t).count() > 0:
                        block_texts.append(t)
                except Exception:
                    pass
            log(f"  [{i}] url={url[:90]}")
            log(f"      checks={checks} block_texts={block_texts}")
            log(f"      body={body_short}")
            try:
                page.screenshot(path=str(out_dir / f"_outlook_clash_debug_{i:02d}.png"), full_page=False)
            except Exception:
                pass
        log("诊断结束")
    finally:
        reg._close_browser(pw_or_camoufox, ctx, owns_camoufox)


if __name__ == "__main__":
    main()
