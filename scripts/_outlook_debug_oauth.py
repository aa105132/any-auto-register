"""OAuth 流程诊断：用已注册账号 xaielmiwjtjusm@outlook.com 测试 OAuth2 PKCE 拿 token。

打开浏览器 → 登录 Outlook → 跳 authorize → 每步打印 URL + body + 截图。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from platforms.outlook.outlook_oauth import (
    _build_authorize_url,
    _resolve_oauth_config,
    generate_code_challenge,
    generate_code_verifier,
)
from platforms.outlook.constants import (
    SEL_OAUTH_CONSENT_BUTTON,
    SEL_OAUTH_LOGINFMT,
    SEL_OAUTH_SIGNIN_BUTTON,
    SEL_PASSWORD_INPUT,
)

CLASH_API = "http://127.0.0.1:9097"
CLASH_SECRET = "set-your-secret"
CLASH_SELECTOR = "🔰 选择节点"
CLASH_PROXY = "http://127.0.0.1:7897"

EMAIL = "xaielmiwjtjusm@outlook.com"
PASSWORD = "#*NiHpB@3I012Mz"


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


def main():
    # 用日本W09（注册时用的节点）
    node = "🇯🇵 日本W09 | IEPL"
    log(f"切换到 {node}")
    switch_node(node)

    from patchright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=False, args=["--lang=zh-CN"], proxy={"server": CLASH_PROXY})
    ctx = browser.new_context(viewport={"width": 1366, "height": 800}, locale="zh-CN")
    page = ctx.new_page()
    ctx.set_default_timeout(30000)

    out_dir = Path(__file__).resolve().parent

    # 生成 PKCE
    client_id, redirect_url, scopes, authorize_url, token_url = _resolve_oauth_config({})
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    auth_url = _build_authorize_url(client_id, redirect_url, scopes, code_challenge)
    log(f"authorize_url: {auth_url[:150]}...")
    log(f"redirect_url: {redirect_url}")

    captured_url = None
    def _on_request(request):
        nonlocal captured_url
        try:
            req_url = request.url or ""
        except Exception:
            req_url = ""
        if redirect_url in req_url and "code=" in req_url:
            captured_url = req_url
            log(f"*** 捕获到 redirect 请求: {req_url[:200]}")

    page.on("request", _on_request)

    # 也监听 response（有些 redirect 在 response 里）
    def _on_response(response):
        nonlocal captured_url
        try:
            resp_url = response.url or ""
        except Exception:
            resp_url = ""
        if redirect_url in resp_url and "code=" in resp_url:
            captured_url = resp_url
            log(f"*** 捕获到 redirect response: {resp_url[:200]}")

    page.on("response", _on_response)

    try:
        log("=== goto authorize ===")
        try:
            page.goto(auth_url, timeout=30000, wait_until="domcontentloaded")
        except Exception as exc:
            log(f"goto 异常: {repr(exc)[:120]}")

        log(f"goto 后 URL: {page.url[:150]}")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        log(f"load_state 后 URL: {page.url[:150]}")

        # 截图
        page.screenshot(path=str(out_dir / "_oauth_debug_01_after_goto.png"))

        # 邮箱页
        try:
            loc = page.locator(SEL_OAUTH_LOGINFMT)
            if loc.count() > 0:
                loc.first.fill(EMAIL, timeout=20000)
                page.locator(SEL_OAUTH_SIGNIN_BUTTON).first.click(timeout=7000)
                log(f"已填邮箱并提交: {EMAIL}")
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                log(f"邮箱提交后 URL: {page.url[:150]}")
                page.screenshot(path=str(out_dir / "_oauth_debug_02_after_email.png"))
        except Exception as exc:
            log(f"邮箱步骤失败: {repr(exc)[:120]}")

        # 密码页
        try:
            pwd_loc = page.locator(SEL_PASSWORD_INPUT).first
            pwd_loc.wait_for(state="visible", timeout=10000)
            pwd_loc.fill(PASSWORD, timeout=10000)
            page.locator(SEL_OAUTH_SIGNIN_BUTTON).first.click(timeout=7000)
            log("已填密码并提交")
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            log(f"密码提交后 URL: {page.url[:150]}")
            page.screenshot(path=str(out_dir / "_oauth_debug_03_after_password.png"))
        except Exception as exc:
            log(f"密码步骤失败: {repr(exc)[:120]}")

        # KMSI 页
        try:
            kmsi = page.locator(SEL_OAUTH_SIGNIN_BUTTON)
            if kmsi.count() > 0:
                kmsi.first.click(timeout=5000)
                log("已点击保持登录")
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                log(f"KMSI 后 URL: {page.url[:150]}")
                page.screenshot(path=str(out_dir / "_oauth_debug_04_after_kmsi.png"))
        except Exception as exc:
            log(f"KMSI 步骤失败: {repr(exc)[:120]}")

        # 同意页
        try:
            consent = page.locator(SEL_OAUTH_CONSENT_BUTTON)
            if consent.count() > 0:
                consent.click(timeout=10000)
                log("已点击同意按钮")
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                log(f"同意后 URL: {page.url[:150]}")
                page.screenshot(path=str(out_dir / "_oauth_debug_05_after_consent.png"))
        except Exception:
            pass

        # 等 redirect
        log("=== 等 redirect 30s ===")
        deadline = time.time() + 30
        while time.time() < deadline:
            if captured_url:
                break
            try:
                cur = page.url or ""
                if redirect_url in cur and "code=" in cur:
                    captured_url = cur
                    log(f"*** 从 page.url 捕获: {cur[:200]}")
                    break
            except Exception:
                pass
            page.wait_for_timeout(500)

        log(f"最终 URL: {page.url[:150]}")
        log(f"captured_url: {captured_url}")
        page.screenshot(path=str(out_dir / "_oauth_debug_06_final.png"))

        # dump body
        try:
            body = page.inner_text("body", timeout=3000)
            log(f"body: {' '.join(body.split())[:400]}")
        except Exception:
            pass

        if captured_url and "code=" in captured_url:
            log("✅ OAuth 成功捕获 auth code！")
            # 换 token
            from platforms.outlook.outlook_oauth import _exchange_code_for_tokens
            from urllib.parse import parse_qs
            query_idx = captured_url.find("?")
            qs = parse_qs(captured_url[query_idx + 1:])
            auth_code = qs.get("code", [""])[0]
            log(f"auth_code: {auth_code[:40]}...")
            data = _exchange_code_for_tokens(
                token_url=token_url, client_id=client_id, code=auth_code,
                redirect_url=redirect_url, code_verifier=code_verifier,
                scopes=scopes, proxy=CLASH_PROXY,
            )
            log(f"✅ 换 token 成功！refresh_token={'yes' if data.get('refresh_token') else 'no'}")
            log(f"access_token: {str(data.get('access_token',''))[:40]}...")
        else:
            log("❌ 未捕获到 auth code")

    finally:
        try:
            page.remove_listener("request", _on_request)
        except Exception:
            pass
        ctx.close()
        browser.close()
        pw.stop()


if __name__ == "__main__":
    main()
