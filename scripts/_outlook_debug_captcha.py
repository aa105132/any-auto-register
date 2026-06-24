"""诊断 Arkose 验证码 iframe 结构：跑到验证码阶段后 dump 所有 frame 的 HTML。

跑一次完整流程到验证码，然后打印：
  - 所有 frame 的 URL
  - Arkose outer/inner iframe 的 outerHTML（前 2000 字）
  - 所有可见按钮的 aria-label/text/selector
  - 截图

用 Clash 当前节点。
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
    SEL_ARKOSE_OUTER_IFRAME,
    SEL_ARKOSE_OUTER_IFRAME_EN,
    SEL_ENFORCEMENT_FRAME,
)

CLASH_API = "http://127.0.0.1:9097"
CLASH_SECRET = "set-your-secret"
CLASH_SELECTOR = "🔰 选择节点"
CLASH_PROXY = "http://127.0.0.1:7897"


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
    except Exception as e:
        log(f"switch {node} fail: {e}")
    return False


NODES = [
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
    "🇯🇵 日本W07 | IEPL",
    "🇯🇵 日本W08 | IEPL",
    "🇯🇵 日本W09 | IEPL",
    "🇯🇵 日本W10 | IEPL",
    "🇯🇵 日本W11 | IEPL",
]


def main():
    out_dir = Path(__file__).resolve().parent
    for node in NODES:
        log(f"=== 尝试节点 {node} ===")
        if not switch_node(node):
            continue
        reg = OutlookBrowserRegister(
            headless=False,
            proxy=CLASH_PROXY,
            email_suffix="@outlook.com",
            bot_protection_wait=11,
            max_captcha_retries=4,
            use_camoufox=False,
            use_protocol_proof=True,  # 开协议层
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
                log("open_signup 失败，换节点")
                continue
            if not reg._fill_email_and_password(page, email_local, password):
                log("fill_email_password 失败，换节点")
                continue
            if not reg._fill_birthdate(page, year, month, day):
                log("fill_birthdate 失败，换节点")
                continue
            if not reg._fill_name_and_submit(page, first, last):
                log("fill_name 失败，换节点")
                continue

            # 等验证码 iframe 出现
            log("等验证码 iframe 出现...")
            captcha_found = False
            for _ in range(30):
                for sel in (SEL_ARKOSE_OUTER_IFRAME, SEL_ARKOSE_OUTER_IFRAME_EN, SEL_ENFORCEMENT_FRAME):
                    try:
                        if page.locator(sel).count() > 0:
                            log(f"验证码 iframe 出现: {sel}")
                            captcha_found = True
                            break
                    except Exception:
                        pass
                if captcha_found:
                    break
                page.wait_for_timeout(1000)

            if not captcha_found:
                log("未等到验证码 iframe")
                try:
                    log(f"body: {' '.join(page.inner_text('body').split())[:600]}")
                except Exception:
                    pass
                continue

            # 截图验证码前
            try:
                page.screenshot(path=str(out_dir / f"_outlook_captcha_before_{node[:6]}.png"), full_page=True)
            except Exception:
                pass

            # 尝试用 solver 过验证码
            log("=== 尝试 solver 过验证码 ===")
            from platforms.outlook.arkose_proof import ArkoseLongPressSolver
            solver = ArkoseLongPressSolver(
                page,
                max_retries=4,
                use_protocol_proof=True,
                log_fn=log,
            )
            ok = solver.solve()
            log(f"solver 返回: {ok}")

            # 截图验证码后状态
            try:
                page.screenshot(path=str(out_dir / f"_outlook_captcha_after_{node[:6]}.png"), full_page=True)
                log("截图 _outlook_captcha_after.png")
            except Exception:
                pass

            # 检查结果
            try:
                body = page.inner_text("body", timeout=3000)
                log(f"solver 后 body: {' '.join(body.split())[:400]}")
            except Exception:
                pass

            if ok:
                log(f"✅ 验证码通过！节点 {node} 成功")
                return
            log(f"节点 {node} 验证码未过，换下一个节点")
        finally:
            reg._close_browser(pw_or_camoufox, ctx, owns_camoufox)
    log("所有节点都未通过验证码")


if __name__ == "__main__":
    main()
