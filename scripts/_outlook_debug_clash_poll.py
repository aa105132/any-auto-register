"""Clash 节点轮换 + 密码修复验证诊断。

遍历 Clash 节点池，切到能加载 Outlook 注册页的节点，
然后跑到密码提交，观察 30s 看是否跳到出生日期页（验证 fill 密码修复）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

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

CLASH_API = "http://127.0.0.1:9097"
CLASH_SECRET = "set-your-secret"
CLASH_SELECTOR = "🔰 选择节点"
CLASH_PROXY = "http://127.0.0.1:7897"

CLASH_NODES = [
    "🇯🇵 日本W02 | IEPL",
    "🇯🇵 日本W03 | IEPL",
    "🇯🇵 日本W07 | IEPL",
    "🇯🇵 日本W08 | IEPL",
    "🇯🇵 日本W09 | IEPL",
    "🇯🇵 日本W10 | IEPL",
    "🇯🇵 日本W11 | IEPL",
    "🇸🇬 新加坡W01 | IEPL | x2",
    "🇸🇬 新加坡W02 | IEPL | x2",
    "🇨🇳 台湾W01 | IEPL | x2",
    "🇺🇲 美国W01 | IEPL | x1.5",
    "🇺🇲 美国W02 | IEPL | x1.5",
    "🇰🇷 韩国W01",
    "🇭🇰 香港W01",
    "🇭🇰 香港W02 | IEPL",
    "🇭🇰 香港W03 | IEPL",
    "🇬🇧 英国W01",
    "🇩🇪 德国W01",
    "🇨🇦 加拿大W01",
    "🇦🇺 澳大利亚W01",
    "🇫🇷 法国W01",
]


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def switch_node(node: str) -> bool:
    try:
        H = {"Authorization": f"Bearer {CLASH_SECRET}", "Content-Type": "application/json"}
        r = requests.put(
            f"{CLASH_API}/proxies/{requests.utils.quote(CLASH_SELECTOR)}",
            headers=H, json={"name": node}, timeout=8,
        )
        if r.status_code in (204, 200):
            time.sleep(1.5)
            return True
        log(f"switch {node} status={r.status_code}")
    except Exception as e:
        log(f"switch {node} fail: {e}")
    return False


def main():
    out_dir = Path(__file__).resolve().parent
    for node in CLASH_NODES:
        log(f"=== 尝试节点 {node} ===")
        if not switch_node(node):
            continue
        reg = OutlookBrowserRegister(
            headless=False,
            proxy=CLASH_PROXY,
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
            year, month, day = _random_birthdate()
            log(f"准备: {email_local}@outlook.com  pwd={password}  birth={year}-{month}-{day}")

            if not reg._open_signup_page(page):
                log("open_signup 失败，换节点")
                continue
            if not reg._fill_email_and_password(page, email_local, password):
                log("fill_email_password 失败，换节点")
                continue

            log("密码已提交，开始 30s 诊断窗口...")
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
            log("诊断结束（这个节点成功跑到密码提交）")
            return
        finally:
            reg._close_browser(pw_or_camoufox, ctx, owns_camoufox)
    log("所有节点都没能加载到密码提交阶段")


if __name__ == "__main__":
    main()
