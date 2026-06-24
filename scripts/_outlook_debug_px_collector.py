"""诊断 PerimeterX px-captcha collector 请求结构。

跑到验证码阶段，拦截所有 HTTP 请求，打印含 px/collector/hsprotect 的 URL + 请求体。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests as req_lib

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
        r = req_lib.put(
            f"{CLASH_API}/proxies/{req_lib.utils.quote(CLASH_SELECTOR)}",
            headers=H, json={"name": node}, timeout=8,
        )
        if r.status_code in (204, 200):
            time.sleep(1.5)
            return True
    except Exception:
        pass
    return False


def main():
    node = "🇯🇵 日本W09 | IEPL"
    log(f"切换到 {node}")
    switch_node(node)

    reg = OutlookBrowserRegister(
        headless=False,
        proxy=CLASH_PROXY,
        email_suffix="@outlook.com",
        bot_protection_wait=11,
        max_captcha_retries=4,
        use_camoufox=False,
        use_protocol_proof=False,  # 关掉协议层拦截，看原始请求
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

        # 拦截所有请求，记录 PX 相关的
        captured = []

        def on_request(request):
            try:
                url = request.url or ""
            except Exception:
                return
            # 过滤 PX/collector/hsprotect/px-cdn 相关
            keywords = ("px-client", "px-cdn", "hsprotect", "/collect", "/collector",
                        "px-captcha", "captcha.js", "main.min.js", "/api/v1/collector")
            if any(kw in url.lower() for kw in keywords):
                try:
                    body = request.post_data or ""
                except Exception:
                    body = ""
                entry = {
                    "url": url[:200],
                    "method": request.method,
                    "body_len": len(body),
                    "body_preview": body[:500],
                    "resource_type": request.resource_type,
                }
                captured.append(entry)
                log(f"[PX] {request.method} {url[:120]} body_len={len(body)}")
                if body and len(body) < 600:
                    log(f"  body: {body[:500]}")

        page.on("request", on_request)

        email_local = random_email_local()
        password = generate_strong_password()
        year, month, day = _random_birthdate()
        first = _random_first_name()
        last = _random_last_name()
        log(f"准备: {email_local}@outlook.com  pwd={password}")

        if not reg._open_signup_page(page):
            log("open_signup 失败")
            return
        if not reg._fill_email_and_password(page, email_local, password):
            log("fill_email_password 失败")
            return
        if not reg._fill_birthdate(page, year, month, day):
            log("fill_birthdate 失败")
            return
        if not reg._fill_name_and_submit(page, first, last):
            log("fill_name 失败")
            return

        # 等验证码 iframe 出现
        log("等验证码 iframe...")
        for _ in range(30):
            for sel in (SEL_ARKOSE_OUTER_IFRAME, SEL_ARKOSE_OUTER_IFRAME_EN, SEL_ENFORCEMENT_FRAME):
                if page.locator(sel).count() > 0:
                    log(f"验证码 iframe 出现: {sel}")
                    break
            else:
                page.wait_for_timeout(1000)
                continue
            break

        # 等内层加载
        log("等内层加载 5s...")
        page.wait_for_timeout(5000)

        # 手动短按两次，观察 collector 请求
        from platforms.outlook.arkose_proof import ArkoseLongPressSolver
        solver = ArkoseLongPressSolver(page, max_retries=2, use_protocol_proof=False, log_fn=log)
        log("=== 手动短按（观察 PX collector）===")
        solver._wait_first_press_ready(timeout_ms=15000)
        solver._short_press()
        log("短按完成，等 15s 观察 collector 请求...")
        page.wait_for_timeout(15000)

        # 汇总
        log(f"\n=== 共捕获 {len(captured)} 个 PX 相关请求 ===")
        for i, e in enumerate(captured):
            log(f"  [{i}] {e['method']} {e['url']}")
            log(f"      body_len={e['body_len']} type={e['resource_type']}")
            if e["body_preview"]:
                log(f"      body: {e['body_preview'][:300]}")

        # 保存到文件
        out = Path(__file__).resolve().parent / "_px_collector_dump.json"
        import json
        out.write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已保存到 {out}")

    finally:
        reg._close_browser(pw_or_camoufox, ctx, owns_camoufox)


if __name__ == "__main__":
    main()
