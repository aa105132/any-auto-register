"""MixRoute Google OAuth 浏览器注册 worker。

MixRoute 的 Google 登录走 new-api 的 OIDC provider（/api/status.oidc_*）：
登录页 → 点 "Log in with Google" → 前端先 GET /api/oauth/state 取 state，
再构造 accounts.google.com/o/oauth2/v2/auth?client_id=...&redirect_uri=
<origin>/oauth/oidc&response_type=code&scope=openid profile email&state=...
→ Google 登录 → 回调 /oauth/oidc?code=...&state=... → GET
/api/oauth/oidc?code=...&state=... 换会话 → 落地 /dashboard。

本 worker 用 OAuthBrowser（支持 Camoufox 反检测）打开登录页，点 Google 按钮
触发 OIDC 跳转，复用 core/google_oauth.drive_google_oauth 驱动 Google 登录
（邮箱→密码→2FA→consent→账号选择），落地 /dashboard 后从 localStorage 读
new-api 会话 token，再走协议 POST /api/token/ 创建 API Key。

Gmail 账号由 Google 账号池（hstockplus）或 mailbox_account 传入，密码随
google_password 传入；totp_secret 非空时自动处理 Google 2FA。
"""
from __future__ import annotations

import time
import uuid as _uuid
from typing import Any, Callable

from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, try_click_provider_on_page

SITE_URL = "https://mixroute.ai/"
CONSOLE_URL = "https://console.mixroute.ai"
LOGIN_URL = f"{CONSOLE_URL}/login"
DASHBOARD_URL = f"{CONSOLE_URL}/dashboard"


def _is_real_console_landing(url: str) -> bool:
    """判定 URL 是否为 MixRoute console 真实落地页（非登录页、非 Google、非 OIDC 中转）。"""
    if not url:
        return False
    if "console.mixroute.ai" not in url:
        return False
    if "accounts.google.com" in url:
        return False
    low = url.lower()
    # 排除登录/注册/OAuth 回调中转页（流程起点，不是落地）
    if "/login" in low or "/register" in low or "/oauth/" in low:
        return False
    return True


def _landed_on_console(browser: OAuthBrowser) -> bool:
    for page in browser.pages():
        if page.is_closed():
            continue
        if _is_real_console_landing(page.url or ""):
            return True
    return False


def _find_landed_page(browser: OAuthBrowser):
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
    key_name: str = "auto-register",
    timeout: int = 300,
    log_fn: Callable[[str], None] = print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    use_camoufox: bool = False,
    cancel_token=None,
) -> dict[str, Any]:
    """MixRoute Google OAuth 注册：登录页 → Google OIDC 登录 → 落地拿 key。

    use_camoufox=True 时用 Camoufox（反检测 Firefox）启动浏览器，绕过 Google 对
    MixRoute OIDC app 的自动化浏览器安全检测（Playwright Chromium 会 signin/rejected）。
    totp_secret 非空时，密码提交后自动处理 Google 2FA/TOTP 验证码页。
    """
    from core.base_platform import make_google_oauth_stop_when
    from core.cancel_token import check_cancel

    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("MixRoute 当前只支持 Google OAuth 登录")

    with OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        use_camoufox=use_camoufox,
        log_fn=log_fn,
    ) as browser:
        browser.set_cancel_token(cancel_token)
        page = browser.new_page()

        # Step 1: 打开登录页（登录页与注册页都有 "Log in with Google" 按钮）
        log_fn(f"[mixroute-oauth] 打开登录页: {LOGIN_URL}")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        # Step 2: 点击 "Log in with Google"（new-api 前端会先取 /api/oauth/state 再跳 Google）
        cur_url = page.url or ""
        log_fn(f"[mixroute-oauth] 当前 url={cur_url[:90]}")
        clicked = False
        try:
            # 等 Google 按钮出现（Camoufox 渲染慢，给足 30s）
            page.wait_for_selector(
                'button:has-text("Google"), a:has-text("Google"), '
                'button[aria-label*="Google" i], a[aria-label*="Google" i]',
                state="visible",
                timeout=30000,
            )
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
                    # new-api 用 JS 跳转（先 /api/oauth/state 再 assign），可能不在 click 同步触发
                    google_locator.click()
                    clicked = True
                    time.sleep(3)
        except Exception as exc:
            log_fn(f"[mixroute-oauth] Playwright 点击 Google 异常: {repr(exc)[:80]}")
        if not clicked:
            clicked = try_click_provider_on_page(page, "google")
            log_fn(f"[mixroute-oauth] 回退 evaluate 点击: {clicked}")
            if clicked:
                time.sleep(5)
        log_fn(f"[mixroute-oauth] 点击 Log in with Google: {clicked}")
        if not clicked:
            raise RuntimeError("MixRoute 登录页未找到 Log in with Google 按钮")

        # 等待跳转到 Google 登录页
        for _ in range(15):
            cur = page.url or ""
            if "accounts.google.com" in cur:
                break
            time.sleep(1)
        log_fn(f"[mixroute-oauth] Google 登录页 url={(page.url or '')[:90]}")

        # Step 3: 驱动 Google 登录流程（邮箱→密码→2FA→consent→账号选择）
        log_fn(f"[mixroute-oauth] 驱动 Google 登录: email={email_hint}")
        result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            totp_secret=totp_secret,
            timeout=min(timeout, 220),
            log_fn=log_fn,
            stop_when=make_google_oauth_stop_when(cancel_token, _landed_on_console),
        )
        log_fn(
            f"[mixroute-oauth] Google 登录结果: email_submitted={result.email_submitted} "
            f"password_submitted={result.password_submitted} last_url={(result.last_url or '')[:80]}"
        )

        # CDP/复用模式：账号已在 Chrome profile 登录，自动选号
        if chrome_cdp_url or chrome_user_data_dir:
            try:
                browser.auto_select_google_account(timeout=8)
                time.sleep(2)
            except Exception:
                pass

        # Step 4: 等待落地 console.mixroute.ai
        deadline = time.time() + max(60, min(timeout, 120))
        landed_page = None
        while time.time() < deadline:
            check_cancel(cancel_token)
            landed_page = _find_landed_page(browser)
            if landed_page:
                break
            time.sleep(2)
        if not landed_page:
            # 兜底：snapshot 再扫一次所有页面
            snap = google_oauth_snapshot(browser)
            for p in snap:
                if not isinstance(p, dict):
                    continue
                if _is_real_console_landing(str(p.get("url", ""))):
                    for pg in browser.pages():
                        if pg.is_closed():
                            continue
                        if _is_real_console_landing(pg.url or ""):
                            landed_page = pg
                            log_fn(f"[mixroute-oauth] snapshot 兜底命中落地页: {(pg.url or '')[:90]}")
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
                f"MixRoute Google OAuth 未落地 console.mixroute.ai: "
                f"last_url={(result.last_url or '')[:80]} "
                f"email_submitted={result.email_submitted} password_submitted={result.password_submitted} "
                f"pages={page_urls} body={first_body!r}"
            )
        log_fn(f"[mixroute-oauth] 落地: {landed_page.url[:90]}")

        # 落地后可能在 onboarding/consent 页，导航到 dashboard 确保会话就绪
        try:
            if "/dashboard" not in (landed_page.url or ""):
                landed_page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
                time.sleep(3)
        except Exception as exc:
            log_fn(f"[mixroute-oauth] 导航 dashboard 异常（继续尝试拿 key）: {repr(exc)[:80]}")

        # Step 5: 从浏览器 localStorage 读 new-api 会话 token + user
        token = ""
        user: dict[str, Any] = {}
        for pg in browser.pages():
            if pg.is_closed():
                continue
            try:
                token = token or str(pg.evaluate("() => localStorage.getItem('token') || ''") or "")
                user_json = str(pg.evaluate("() => localStorage.getItem('user') || ''") or "")
                if user_json and not user:
                    import json
                    parsed = json.loads(user_json)
                    if isinstance(parsed, dict):
                        user = parsed
            except Exception:
                continue
            if token and user:
                break
        if not token:
            raise RuntimeError("MixRoute Google OAuth 落地但未读到 localStorage token")
        user_id = str(user.get("id") or "")
        log_fn(f"[mixroute-oauth] 会话 token 已读取, user_id={user_id}")

        # 同步浏览器 cookie 到协议 session
        cookies = browser.cookie_dict(domain_substrings=("mixroute.ai", "console.mixroute.ai"))

    # Step 6: 协议 POST /api/token/ 创建 API Key
    from platforms.mixroute.core import (
        API_BASE,
        _build_session,
        _cookie_header,
        _normalize_api_key,
        _session_cookie_dict,
        apply_session_auth,
        create_api_key_http,
        get_user_self_http,
        import_cookies,
        verify_api_key_http,
    )

    session = _build_session(proxy)
    import_cookies(session, cookies)
    apply_session_auth(session, token, user_id)

    # 补全 user 信息
    try:
        from platforms.mixroute.core import _extract_user, _response_success
        self_info = get_user_self_http(session, token)
        if _response_success(self_info):
            self_user = _extract_user(self_info.get("data"))
            if self_user:
                user = self_user
                user_id = str(user.get("id") or user_id)
                apply_session_auth(session, token, user_id)
    except Exception:
        pass

    key_result = create_api_key_http(session, token=token, key_name=key_name, log_fn=log_fn)
    api_key = _normalize_api_key(key_result.get("api_key") or "")
    if not api_key:
        raise RuntimeError(
            f"MixRoute Google OAuth 落地但创建 API Key 失败: {key_result}"
        )
    api_verification = verify_api_key_http(api_key, proxy=proxy)
    session_cookies = _session_cookie_dict(session)
    actual_email = str(user.get("email") or email_hint or "").strip()

    return {
        "email": actual_email,
        "password": google_password,
        "user": user,
        "user_id": user_id,
        "session_token": token,
        "access_token": token,
        "api_key": api_key,
        "ai_api_token": api_key,
        "api_key_info": dict(key_result.get("api_key_info") or {}),
        "key_create_result": key_result,
        "api_verification": api_verification,
        "cookies": session_cookies,
        "cookie_header": _cookie_header(session_cookies),
        "auth_method": "google_oauth",
        "oauth_provider": "google",
        "api_key_source": "browser_protocol",
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "api_base": API_BASE,
    }
