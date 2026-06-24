"""诊断：填邮箱+密码提交后，页面到底变成了什么状态？

跑一次，填完密码点 primaryButton 后等待 25s，期间每 2s 打印：
  - 当前 URL
  - body innerText 前 600 字
  - 关键 selector 是否出现（BirthYear / lastNameInput / enforcementFrame / 错误文案 / 验证码 iframe）
  - 截图保存到 scripts/_outlook_debug_*.png

用 resin 单 slot 跑，便于复现。
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


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)


def resolve_resin(slot: int) -> str | None:
    from core.config_store import config_store
    from core.resin_proxy import resolve_resin_proxy_config
    cfg = {
        "resin_enabled": "true",
        "resin_scheme": config_store.get("resin_scheme", ""),
        "resin_host": config_store.get("resin_host", ""),
        "resin_port": config_store.get("resin_port", ""),
        "resin_token": config_store.get("resin_token", ""),
        "resin_default_platform": config_store.get("resin_default_platform", "Default"),
        "resin_platform_map": config_store.get("resin_platform_map", ""),
    }
    resolved = resolve_resin_proxy_config(cfg, task_platform="outlook", account=f"ol{slot}", require_enabled=True)
    return str(resolved.get("proxy_url") or "").strip() or None


def main():
    # 试多个 slot 直到有一个能加载页面并填到密码提交
    for slot in [10, 14, 1, 4, 8, 16, 20, 22, 24]:
        proxy = resolve_resin(slot)
        if not proxy:
            log(f"slot {slot} 无 resin 代理，跳过")
            continue
        log(f"=== 尝试 slot {slot} proxy={proxy} ===")
        reg = OutlookBrowserRegister(
            headless=False,  # 用有头模式便于观察
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
                log("open_signup 失败，换 slot")
                continue
            if not reg._fill_email_and_password(page, email_local, password):
                log("fill_email_password 失败，换 slot")
                continue

            log("密码已提交，开始 30s 诊断窗口...")
            out_dir = Path(__file__).resolve().parent
            for i in range(15):  # 30s, 每 ~2s 一次
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
                    page.screenshot(path=str(out_dir / f"_outlook_debug_{i:02d}.png"), full_page=False)
                except Exception:
                    pass
            log("诊断结束（这个 slot 成功跑到密码提交）")
            return  # 成功跑完诊断就退出
        finally:
            reg._close_browser(pw_or_camoufox, ctx, owns_camoufox)
    log("所有 slot 都没能加载到密码提交阶段")


if __name__ == "__main__":
    main()
