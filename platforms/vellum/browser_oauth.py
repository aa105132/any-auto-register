"""Vellum Google OAuth 浏览器注册 worker。

绕开 WorkOS AuthKit 邮箱+密码注册（受 Radar policy_denied 拦截），改走 Google OAuth 登录：
邀请页 → Continue with Google → Google 账号登录（复用 core/google_oauth.drive_google_oauth）
→ 回调落地 www.vellum.ai → session_api.extract_on_page 签发 assistant_api_key。

Gmail 账号由 hstockplus 购买（HStockPlusGoogleAccountProvider），密码随 mailbox_account 传入。
"""
from __future__ import annotations

import re
import time
import uuid as _uuid
from typing import Any, Callable

from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, try_click_provider_on_page

SITE_URL = "https://www.vellum.ai/"
APP_URL = "https://www.vellum.ai/assistant"
INVITE_BASE = "https://www.vellum.ai/r/"


def _is_real_vellum_landing(url: str) -> bool:
    """判定 URL 是否为真正的 Vellum 落地页（非中转页、非 AuthKit、非 Google SSO、非 Google）。

    点击 Google 按钮后跳转 accounts.google.com 有 2-3s 延迟，期间 URL 仍是
    vellum.ai/account/signup 中转页——必须排除，否则 stop_when 会误判提前结束。
    """
    if not url:
        return False
    if "vellum.ai" not in url:
        return False
    if "login.platform.vellum.ai" in url or "auth.platform.vellum.ai" in url or "accounts.google.com" in url:
        return False
    # 排除中转/登录/注册入口页（这些是流程起点，不是落地）
    low = url.lower()
    if "/account/signup" in low or "/account/login" in low or "/account/provider" in low:
        return False
    return True


def _landed_on_vellum(browser: OAuthBrowser) -> bool:
    """Google 登录完成、回调落地 www.vellum.ai 真实应用页的判定。"""
    for page in browser.pages():
        if page.is_closed():
            continue
        if _is_real_vellum_landing(page.url or ""):
            return True
    return False


def _find_landed_page(browser: OAuthBrowser):
    """返回已落地 www.vellum.ai 真实应用页的页面（供 extract_on_page 用），找不到返回 None。"""
    for page in browser.pages():
        if page.is_closed():
            continue
        if _is_real_vellum_landing(page.url or ""):
            return page
    return None


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    google_password: str = "",
    totp_secret: str = "",
    invite_code: str = "H5QJRV",
    timeout: int = 300,
    log_fn: Callable[[str], None] = print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    use_camoufox: bool = False,
    cancel_token=None,
) -> dict[str, Any]:
    """Vellum Google OAuth 注册：邀请页 → Google 登录 → 落地签发 API Key。

    use_camoufox=True 时用 Camoufox（反检测 Firefox）启动浏览器，绕过 Google 对
    Vellum OAuth app 的自动化浏览器安全检测（Playwright Chromium 会 signin/rejected）。
    totp_secret 非空时，密码提交后自动处理 Google 2FA/TOTP 验证码页。
    """
    from core.base_platform import make_google_oauth_stop_when
    from core.cancel_token import check_cancel

    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("Vellum 当前只支持 Google OAuth 登录")

    invite_code = str(invite_code or "H5QJRV").strip() or "H5QJRV"
    invite_url = INVITE_BASE + invite_code

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

        # Step 1: 打开邀请页 → 落到 vellum.ai/account/signup 中转页
        # 该中转页直接有 "Continue with Google" 按钮，点击触发 Google OAuth SSO
        # （走 auth.platform.vellum.ai/sso/oauth/google/.../callback，绕开 WorkOS Radar）
        log_fn(f"[vellum-oauth] 打开邀请页: {invite_url}")
        page.goto(invite_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)

        # Step 2: 在中转页点击 "Continue with Google"
        # Vellum 的 Google 按钮是 <button class="signup__btn">，点击触发整页导航到 accounts.google.com。
        # Camoufox 渲染较慢，先 wait_for_selector 确保按钮就绪，再用 Playwright click + expect_navigation
        # 等跳转完成（evaluate 内 node.click() 不等导航，且 Camoufox 下可能开新 tab 而非当前页跳转）。
        cur_url = page.url or ""
        log_fn(f"[vellum-oauth] 当前 url={cur_url[:90]}")
        clicked = False
        try:
            # 先等 Google 按钮出现（Camoufox 渲染慢，给足 30s）
            page.wait_for_selector("button.signup__btn", state="visible", timeout=30000)
            google_locator = page.locator("button.signup__btn").first
            if google_locator.count() == 0:
                # 兜底：按文本找
                google_locator = page.get_by_role("button", name=re.compile("google", re.I)).first
            if google_locator.count() > 0:
                with page.expect_navigation(timeout=30000, wait_until="domcontentloaded"):
                    google_locator.click()
                clicked = True
        except Exception as exc:
            log_fn(f"[vellum-oauth] Playwright 点击 Google 异常: {repr(exc)[:80]}")
        if not clicked:
            # 回退到 evaluate 点击
            clicked = try_click_provider_on_page(page, "google")
            log_fn(f"[vellum-oauth] 回退 evaluate 点击: {clicked}")
            if clicked:
                time.sleep(5)
        log_fn(f"[vellum-oauth] 点击 Continue with Google: {clicked}")
        if not clicked:
            raise RuntimeError("Vellum 中转页未找到 Continue with Google 按钮")

        # 等待跳转到 Google 登录页（点击后跳转有延迟，避免 drive_google_oauth 立刻退出）
        for _ in range(15):
            cur = page.url or ""
            if "accounts.google.com" in cur or "auth.platform.vellum.ai" in cur:
                break
            time.sleep(1)
        log_fn(f"[vellum-oauth] Google 登录页 url={(page.url or '')[:90]}")

        # Step 3: 驱动 Google 登录流程（邮箱→密码→同意页→账号选择）
        log_fn(f"[vellum-oauth] 驱动 Google 登录: email={email_hint}")
        result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            totp_secret=totp_secret,
            timeout=min(timeout, 220),
            log_fn=log_fn,
            stop_when=make_google_oauth_stop_when(cancel_token, _landed_on_vellum),
        )
        log_fn(
            f"[vellum-oauth] Google 登录结果: email_submitted={result.email_submitted} "
            f"password_submitted={result.password_submitted} last_url={(result.last_url or '')[:80]}"
        )

        # CDP/复用模式：账号已在 Chrome profile 登录，自动选号
        if chrome_cdp_url or chrome_user_data_dir:
            try:
                browser.auto_select_google_account(timeout=8)
                time.sleep(2)
            except Exception:
                pass

        # Step 4: 等待落地 www.vellum.ai
        deadline = time.time() + max(60, min(timeout, 120))
        landed_page = None
        while time.time() < deadline:
            check_cancel(cancel_token)
            landed_page = _find_landed_page(browser)
            if landed_page:
                break
            # 未落地：若仍在 Google/AuthKit，等待回调；否则尝试导航到 APP_URL
            time.sleep(2)
        if not landed_page:
            # 兜底：Step 4 循环按 2s 间隔轮询 browser.pages()，Camoufox 下落地页
            # 可能在 deadline 末尾才出现（Google 2FA/回调链路慢），循环刚好错过。
            # raise 前用 snapshot 再扫一次所有页面，命中真实 vellum 落地页就继续
            # provision，避免"已落地却报未落地"误判导致不去取 key。
            snap = google_oauth_snapshot(browser)
            for p in snap:
                if not isinstance(p, dict):
                    continue
                if _is_real_vellum_landing(str(p.get("url", ""))):
                    for page in browser.pages():
                        if page.is_closed():
                            continue
                        if _is_real_vellum_landing(page.url or ""):
                            landed_page = page
                            log_fn(f"[vellum-oauth] snapshot 兜底命中落地页: {(page.url or '')[:90]}")
                            break
                    if landed_page:
                        break
        if not landed_page:
            snap = google_oauth_snapshot(browser)
            # google_oauth_snapshot 返回 list[dict]（每页 {"url","body"}），不是 dict
            page_urls = [str(p.get("url", "")) for p in snap if isinstance(p, dict)][:3]
            # 提取首页可见文本，便于判断 Google 拦截/账号不存在等具体原因
            first_body = ""
            for p in snap:
                if isinstance(p, dict):
                    first_body = str(p.get("body", ""))[:200]
                    if first_body:
                        break
            raise RuntimeError(
                f"Vellum Google OAuth 未落地 www.vellum.ai: last_url={(result.last_url or '')[:80]} "
                f"email_submitted={result.email_submitted} password_submitted={result.password_submitted} "
                f"pages={page_urls} body={first_body!r}"
            )
        log_fn(f"[vellum-oauth] 落地: {landed_page.url[:90]}")

        # 落地后可能在 onboarding/consent 页，导航到 APP_URL 确保会话就绪
        try:
            if "vellum.ai/assistant" not in (landed_page.url or ""):
                landed_page.goto(APP_URL, wait_until="domcontentloaded", timeout=45000)
                time.sleep(3)
        except Exception as exc:
            log_fn(f"[vellum-oauth] 导航 APP_URL 异常（继续尝试 provision）: {repr(exc)[:80]}")

        # Step 5: REST 闭环签发 assistant_api_key（复用 session_api.extract_on_page）
        from platforms.vellum.session_api import extract_on_page

        cid = str(_uuid.uuid4())
        rid = str(_uuid.uuid4())
        log_fn("[vellum-oauth] 走 REST 闭环签发 API Key...")
        prov = extract_on_page(
            landed_page,
            provision_key=True,
            client_installation_id=cid,
            runtime_assistant_id=rid,
            log=log_fn,
        )
        api_key = str(prov.get("assistant_api_key") or "").strip()
        if not api_key:
            raise RuntimeError(
                f"Vellum Google OAuth 落地但未签发 API Key: step={prov.get('step')} "
                f"provision_status={prov.get('provision_status')} code={prov.get('provision_code')}"
            )

        # Vellum platform_user_id 不是邮箱格式，这里直接用购买的 Gmail 作为账号邮箱
        actual_email = email_hint

    return {
        "email": email_hint,
        "password": google_password,
        "api_key": api_key,
        "ai_api_token": api_key,
        "assistant_api_key": api_key,
        "webhook_secret": str(prov.get("webhook_secret") or ""),
        "platform_assistant_id": str(prov.get("platform_assistant_id") or ""),
        "platform_user_id": str(prov.get("platform_user_id") or ""),
        "platform_organization_id": str(prov.get("platform_organization_id") or ""),
        "balance_usd": str(prov.get("balance_usd") or ""),
        "own_invite_code": str(prov.get("own_invite_code") or ""),
        "referral_url": str(prov.get("referral_url") or ""),
        "client_installation_id": cid,
        "runtime_assistant_id": rid,
        "local_assistant_id": str(prov.get("local_assistant_id") or ""),
        "phone_verified": True,
        "landed_url": str(landed_page.url if landed_page else ""),
        "site_url": SITE_URL,
    }
