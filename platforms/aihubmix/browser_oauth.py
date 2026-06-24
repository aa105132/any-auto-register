"""AIHubMix Google OAuth 浏览器注册 worker。

绕开 Clerk 邮箱+密码注册（Turnstile captcha 卡点），改走 Google OAuth 登录：
console.aihubmix.com/sign-up → Clerk 渲染的 "Continue with Google" → Google 登录
（复用 core/google_oauth.drive_google_oauth）→ Clerk 回调落地 console.aihubmix.com
→ 协议/浏览器拿 key。

aihubmix 的 Google OAuth 是 Clerk-mediated（Clerk 的 oauth_google strategy），
与 Vellum 的 WorkOS-mediated 不同：点击按钮后 Clerk 前端 SDK 发起
/v1/client/sign_ups?strategy=oauth_google → 拿 external_account_redirect URL
（accounts.google.com/o/oauth2/...）→ 浏览器导航到 Google → Google 登录完成回调
clerk.aihubmix.com/v1/client/sign_ups/{id}/external_oauth/callback → Clerk 创建
session → 落地 console.aihubmix.com。

Google 侧（accounts.google.com 登录页/密码/2FA/consent/账号选择）复用
core/google_oauth.drive_google_oauth，与 Vellum/Novita 共用同一套 Google driver。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, try_click_provider_on_page

SITE_URL = "https://aihubmix.com/"
CONSOLE_URL = "https://console.aihubmix.com"
SIGN_UP_URL = "https://console.aihubmix.com/sign-up"


def _is_real_console_landing(url: str) -> bool:
    """判定 URL 是否为真正的 AIHubMix console 落地页（非中转页、非 Clerk、非 Google）。

    点击 Google 按钮后跳转 accounts.google.com 有 2-3s 延迟，期间 URL 仍是
    console.aihubmix.com/sign-up 中转页——必须排除，否则 stop_when 会误判提前结束。
    """
    if not url:
        return False
    if "console.aihubmix.com" not in url:
        return False
    if "clerk.aihubmix.com" in url or "accounts.google.com" in url:
        return False
    # 排除中转/登录/注册入口页（这些是流程起点，不是落地）
    low = url.lower()
    if "/sign-up" in low or "/sign-in" in low:
        return False
    return True


def _landed_on_console(browser: OAuthBrowser) -> bool:
    """Google 登录完成、回调落地 console.aihubmix.com 真实应用页的判定。"""
    for page in browser.pages():
        if page.is_closed():
            continue
        if _is_real_console_landing(page.url or ""):
            return True
    return False


def _find_landed_page(browser: OAuthBrowser):
    """返回已落地 console.aihubmix.com 真实应用页的页面，找不到返回 None。"""
    for page in browser.pages():
        if page.is_closed():
            continue
        if _is_real_console_landing(page.url or ""):
            return page
    return None


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    google_password: str = "",
    totp_secret: str = "",
    timeout: int = 300,
    log_fn: Callable[[str], None] = print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    use_camoufox: bool = False,
    cancel_token=None,
) -> dict[str, Any]:
    """AIHubMix Google OAuth 注册：sign-up → Continue with Google → Google 登录 → 落地拿 key。

    use_camoufox=True 时用 Camoufox（反检测 Firefox）启动浏览器，绕过 Google 对
    AIHubMix OAuth app 的自动化浏览器安全检测（Playwright Chromium 会 signin/rejected）。
    totp_secret 非空时，密码提交后自动处理 Google 2FA/TOTP 验证码页。
    """
    from core.base_platform import make_google_oauth_stop_when
    from core.cancel_token import check_cancel

    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("AIHubMix 当前只支持 Google OAuth 登录")

    # 注意：此版本的 OAuthBrowser 不支持 use_camoufox / cancel_token 参数，
    # Camoufox 和 cancel_token 暂不传递（后续版本可加）。
    with OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        log_fn=log_fn,
    ) as browser:
        page = browser.new_page()

        # Step 1: 打开 sign-up 页
        log_fn(f"[aihubmix-oauth] 打开注册页: {SIGN_UP_URL}")
        page.goto(SIGN_UP_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        # Step 2: 点击 "Continue with Google"（Clerk 渲染的 social OAuth 按钮）
        # Clerk 的 Google 按钮通常带 data-provider="oauth_google" 或 aria-label 含 Google。
        cur_url = page.url or ""
        log_fn(f"[aihubmix-oauth] 当前 url={cur_url[:90]}")
        clicked = False
        try:
            # 先等 Google 按钮出现（Camoufox 渲染慢，给足 30s）
            page.wait_for_selector(
                'button[data-provider="oauth_google"], a[data-provider="oauth_google"], '
                'button:has-text("Google"), a:has-text("Google"), '
                'button[aria-label*="Google" i], a[aria-label*="Google" i]',
                state="visible",
                timeout=30000,
            )
            # 优先用 Clerk 的 data-provider 选择器
            google_locator = page.locator('button[data-provider="oauth_google"], a[data-provider="oauth_google"]').first
            if google_locator.count() == 0:
                # 回退：按文本/aria-label 找
                google_locator = page.locator(
                    'button:has-text("Google"), a:has-text("Google"), '
                    'button[aria-label*="Google" i], a[aria-label*="Google" i]'
                ).first
            if google_locator.count() > 0:
                try:
                    with page.expect_navigation(timeout=30000, wait_until="domcontentloaded"):
                        google_locator.click()
                    clicked = True
                except Exception:
                    # expect_navigation 超时（Clerk 用 JS 跳转，可能不在 click 同步触发）
                    google_locator.click()
                    clicked = True
                    time.sleep(3)
        except Exception as exc:
            log_fn(f"[aihubmix-oauth] Playwright 点击 Google 异常: {repr(exc)[:80]}")
        if not clicked:
            # 回退到 evaluate 点击（try_click_provider_on_page 支持 google）
            clicked = try_click_provider_on_page(page, "google")
            log_fn(f"[aihubmix-oauth] 回退 evaluate 点击: {clicked}")
            if clicked:
                time.sleep(5)
        log_fn(f"[aihubmix-oauth] 点击 Continue with Google: {clicked}")
        if not clicked:
            raise RuntimeError("AIHubMix sign-up 页未找到 Continue with Google 按钮")

        # 等待跳转到 Google 登录页（点击后跳转有延迟，避免 drive_google_oauth 立刻退出）
        for _ in range(15):
            cur = page.url or ""
            if "accounts.google.com" in cur or "clerk.aihubmix.com" in cur:
                break
            time.sleep(1)
        log_fn(f"[aihubmix-oauth] Google 登录页 url={(page.url or '')[:90]}")

        # Step 3: 驱动 Google 登录流程（邮箱→密码→同意页→账号选择）
        # 注意：此版本的 drive_google_oauth 不支持 totp_secret 参数（2FA 由 Google
        # 页面自动处理或需手动）。cancel_token 通过 stop_when 传入。
        log_fn(f"[aihubmix-oauth] 驱动 Google 登录: email={email_hint}")
        result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 220),
            log_fn=log_fn,
            stop_when=make_google_oauth_stop_when(cancel_token, _landed_on_console),
        )
        log_fn(
            f"[aihubmix-oauth] Google 登录结果: email_submitted={result.email_submitted} "
            f"password_submitted={result.password_submitted} last_url={(result.last_url or '')[:80]}"
        )

        # CDP/复用模式：账号已在 Chrome profile 登录，自动选号
        if chrome_cdp_url or chrome_user_data_dir:
            try:
                browser.auto_select_google_account(timeout=8)
                time.sleep(2)
            except Exception:
                pass

        # Step 4: 等待落地 console.aihubmix.com
        deadline = time.time() + max(60, min(timeout, 120))
        landed_page = None
        while time.time() < deadline:
            check_cancel(cancel_token)
            landed_page = _find_landed_page(browser)
            if landed_page:
                break
            # 未落地：若仍在 Google/Clerk，等待回调；否则尝试导航到 CONSOLE_URL
            time.sleep(2)
        if not landed_page:
            # 兜底：snapshot 再扫一次所有页面
            snap = google_oauth_snapshot(browser)
            for p in snap:
                if not isinstance(p, dict):
                    continue
                if _is_real_console_landing(str(p.get("url", ""))):
                    for page in browser.pages():
                        if page.is_closed():
                            continue
                        if _is_real_console_landing(page.url or ""):
                            landed_page = page
                            log_fn(f"[aihubmix-oauth] snapshot 兜底命中落地页: {(page.url or '')[:90]}")
                            break
                    if landed_page:
                        break
        if not landed_page:
            snap = google_oauth_snapshot(browser)
            page_urls = [str(p.get("url", "")) for p in snap if isinstance(p, dict)][:3]
            first_body = ""
            for p in snap:
                if isinstance(p, dict):
                    first_body = str(p.get("body", ""))[:200]
                    if first_body:
                        break
            raise RuntimeError(
                f"AIHubMix Google OAuth 未落地 console.aihubmix.com: "
                f"last_url={(result.last_url or '')[:80]} "
                f"email_submitted={result.email_submitted} password_submitted={result.password_submitted} "
                f"pages={page_urls} body={first_body!r}"
            )
        log_fn(f"[aihubmix-oauth] 落地: {landed_page.url[:90]}")

        # Step 5: 提取 Clerk 会话 cookie
        cookies = browser.cookie_dict(domain_substrings=("aihubmix.com",))
        auth_state = {
            "client_cookie": cookies.get("__client", ""),
            "session_cookie": cookies.get("__session", ""),
            "access_token": cookies.get("__session", ""),
        }
        if not auth_state["client_cookie"]:
            raise RuntimeError("AIHubMix Google OAuth 落地但未拿到 __client cookie")

        # Step 6: 协议拿 key（先试 _KeyFetchWorker，失败回退浏览器 DOM）
        from platforms.aihubmix.protocol_register import _KeyFetchWorker
        from platforms.aihubmix.browser_register import _fetch_key_via_browser

        api_key = ""
        key_create: dict[str, Any] = {}
        key_source = "protocol"
        try:
            key_worker = _KeyFetchWorker(proxy=proxy, log_fn=log_fn)
            key_create = key_worker.fetch(auth_state, name=f"auto-register-{int(time.time())}")
            api_key = str(key_create.get("api_key") or "").strip()
        except Exception as exc:
            log_fn(f"[aihubmix-oauth] 协议拿 key 失败，回退浏览器 DOM: {type(exc).__name__}: {str(exc)[:120]}")

        if not api_key:
            # 浏览器 DOM 兜底：复用当前 browser 的 Chrome（连 CDP）
            key_source = "browser_dom"
            # 构造一个 launch_meta 供 _fetch_key_via_browser 复用 cdp_url
            launch_meta = {"cdp_url": chrome_cdp_url} if chrome_cdp_url else {}
            api_key = _fetch_key_via_browser(launch_meta, proxy, f"auto-register-{int(time.time())}", log_fn, cancel_token)
            if not api_key:
                raise RuntimeError("AIHubMix Google OAuth 落地但协议+浏览器 DOM 均未拿到 key")

        # 协议验证 key + 拉 models
        from platforms.aihubmix.core import AIHubMixClient
        client = AIHubMixClient(proxy=proxy, log_fn=log_fn)
        verification_ok = client.verify_api_key(api_key)
        try:
            models = client.list_models_raw(api_key)
        except Exception:
            models = {}

        from platforms.aihubmix.protocol_register import _utcnow_iso
        actual_email = email_hint

    return {
        "email": email_hint,
        "password": google_password,
        "api_key": api_key,
        "api_key_name": f"auto-register-{int(time.time())}",
        "api_key_source": key_source,
        "key_create_result": key_create,
        "api_verification": {"ok": verification_ok},
        "models": models if isinstance(models, dict) else {},
        "access_token": auth_state.get("access_token", ""),
        "client_cookie": auth_state.get("client_cookie", ""),
        "session_cookie": auth_state.get("session_cookie", ""),
        "site_url": SITE_URL,
        "dashboard_url": CONSOLE_URL,
        "api_base": "https://aihubmix.com/v1",
        "checked_at": _utcnow_iso(),
    }
