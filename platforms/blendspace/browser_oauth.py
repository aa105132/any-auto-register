"""BlendSpace OAuth 浏览器流程。"""
from __future__ import annotations

import time
from pathlib import Path

from core.oauth_browser import (
    OAuthBrowser,
    browser_login_method_text,
    finalize_oauth_email,
)
from platforms.blendspace.core import BASE_URL, SESSION_STORAGE_KEY

GOOGLE_OAUTH_LOGIN_URL = "https://api.blendspace.ai/auth/google/login"

GOOGLE_PROMPT_LABELS = (
    "Continue", "Allow", "I agree", "I understand", "Next",
    "\u7ee7\u7eed", "\u5141\u8bb8", "\u6211\u540c\u610f", "\u6211\u660e\u767d", "\u6211\u4e86\u89e3", "\u4e0b\u4e00\u6b65",
)
GOOGLE_CREDENTIAL_INPUT_SELECTOR = (
    'input[type="email"], input[name="identifier"], #identifierId, '
    'input[type="password"], input[name="Passwd"]'
)


def _has_google_credential_input(page) -> bool:
    try:
        return bool(page.query_selector(GOOGLE_CREDENTIAL_INPUT_SELECTOR))
    except Exception:
        return False


def _read_local_storage_session(page) -> str:
    """读取 BlendSpace 前端写入的 wasp:sessionId。"""
    return _read_storage_session(page)[0]


def _read_storage_session(page) -> tuple[str, str]:
    """从 localStorage/sessionStorage 扫描 BlendSpace session。"""
    try:
        value = page.evaluate(
            """
            (preferredKey) => {
                const decode = (raw) => {
                    if (!raw) return "";
                    try {
                        const parsed = JSON.parse(raw);
                        return typeof parsed === "string" ? parsed : raw;
                    } catch (_) {
                        return raw;
                    }
                };
                const stores = [window.localStorage, window.sessionStorage];
                for (const store of stores) {
                    const direct = decode(store.getItem(preferredKey));
                    if (direct) return { key: preferredKey, value: direct };
                }
                for (const store of stores) {
                    for (let i = 0; i < store.length; i++) {
                        const key = store.key(i) || "";
                        if (!/wasp:sessionId|session/i.test(key)) continue;
                        const candidate = decode(store.getItem(key));
                        if (candidate && /^[A-Za-z0-9_-]{20,}$/.test(candidate)) {
                            return { key, value: candidate };
                        }
                    }
                }
                return { key: "", value: "" };
            }
            """,
            SESSION_STORAGE_KEY,
        )
    except Exception:
        return "", ""
    if isinstance(value, dict):
        return str(value.get("value") or "").strip().strip('"'), str(value.get("key") or "")
    return str(value or "").strip().strip('"'), SESSION_STORAGE_KEY


def _open_blendspace_google_oauth(browser: OAuthBrowser, *, log_fn=print) -> None:
    """Open the BlendSpace auth dialog and start Google OAuth."""
    page = browser.active_page()
    # Prefer the direct Google OAuth link when it is present.
    for selector in (
        'a[href="https://api.blendspace.ai/auth/google/login"]',
        'a[href$="/auth/google/login"]',
    ):
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.click(timeout=5000)
                return
        except Exception:
            pass

    for name in ("Log in", "Get Started"):
        try:
            page.get_by_role("button", name=name).click(timeout=8000)
            page.wait_for_timeout(1200)
            break
        except Exception:
            continue

    for selector in (
        'a[href="https://api.blendspace.ai/auth/google/login"]',
        'a[href$="/auth/google/login"]',
    ):
        try:
            if page.locator(selector).count() > 0:
                page.locator(selector).first.click(timeout=8000)
                return
        except Exception:
            pass

    # Fallback to generic provider button detection.
    if not browser.try_click_provider("google"):
        raise RuntimeError("BlendSpace 未找到 Google OAuth 入口")


def _click_google_prompt(page) -> str:
    """Click Google OAuth prompt / ToS buttons without mojibake selectors."""
    labels = GOOGLE_PROMPT_LABELS

    for selector in ("#confirm", 'input[name="confirm"]', 'input[value="\u6211\u540c\u610f"]', 'input[value="\u6211\u660e\u767d"]', 'input[value="\u6211\u4e86\u89e3"]'):
        try:
            el = page.locator(selector).first
            if el.count() > 0:
                el.click(timeout=5000)
                return selector
        except Exception:
            pass
    for name in labels:
        for role in ("button", "link"):
            try:
                locator = page.get_by_role(role, name=name)
                if locator.count() > 0:
                    locator.first.click(timeout=5000)
                    return name
            except Exception:
                pass
    try:
        clicked = page.evaluate(
            """
            (labels) => {
                const nodes = Array.from(document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]'));
                for (const node of nodes) {
                    const text = (node.innerText || node.textContent || node.getAttribute('aria-label') || node.value || '').trim();
                    if (!text) continue;
                    if (labels.some(label => text.includes(label))) {
                        node.click();
                        return text;
                    }
                }
                return '';
            }
            """,
            list(labels),
        )
        return str(clicked or "")
    except Exception:
        return ""


def _try_google_password_login(browser: OAuthBrowser, *, email: str, password: str, log_fn=print, timeout: int = 90) -> bool:
    """? Google OAuth ????????/???????????????"""
    if not email and not password:
        return False
    deadline = time.time() + timeout
    email_submitted = False
    password_submitted = False
    last_action = 0.0
    while time.time() < deadline:
        progressed = False
        for page in browser.pages():
            if page.is_closed() or "accounts.google.com" not in str(page.url or ""):
                continue
            try:
                body_text = ""
                try:
                    body_text = page.locator("body").inner_text(timeout=1500)
                except Exception:
                    body_text = ""

                if email and not email_submitted and "accountchooser" in str(page.url or ""):
                    try:
                        candidate = page.get_by_text(email, exact=False)
                        if candidate.count() > 0:
                            candidate.first.click(timeout=5000)
                            email_submitted = True
                            progressed = True
                            log_fn("[BlendSpace] selected Google OAuth account")
                            time.sleep(2)
                            continue
                    except Exception:
                        pass
                    for label in ("Use another account", "\u4f7f\u7528\u5176\u4ed6\u8d26\u53f7"):
                        try:
                            loc = page.get_by_text(label, exact=False)
                            if loc.count() > 0:
                                loc.first.click(timeout=5000)
                                progressed = True
                                time.sleep(1.5)
                                break
                        except Exception:
                            pass

                if email and not email_submitted:
                    email_input = page.query_selector('input[type="email"], input[name="identifier"], #identifierId')
                    if email_input:
                        email_input.fill(email)
                        page.keyboard.press("Enter")
                        email_submitted = True
                        progressed = True
                        log_fn("[BlendSpace] submitted Google OAuth email")
                        time.sleep(3)
                        continue

                if password and email_submitted and not password_submitted:
                    if "\u627e\u4e0d\u5230\u60a8\u7684 Google \u8d26\u53f7" in body_text or "Couldn't find your Google Account" in body_text:
                        raise RuntimeError(f"Google account not found or not loginable: {email}")
                    password_input = page.query_selector('input[type="password"], input[name="Passwd"]')
                    if password_input:
                        password_input.fill(password)
                        page.keyboard.press("Enter")
                        password_submitted = True
                        progressed = True
                        log_fn("[BlendSpace] submitted Google OAuth password")
                        time.sleep(5)
                        continue

                # Only click generic Google prompts after credential inputs are gone.
                # Identifier pages also contain “Continue/Next”, so clicking first can loop forever.
                if not _has_google_credential_input(page):
                    clicked = _click_google_prompt(page)
                    if clicked:
                        progressed = True
                        log_fn(f"[BlendSpace] clicked Google OAuth prompt: {clicked}")
                        time.sleep(4)
                        continue
            except RuntimeError:
                raise
            except Exception:
                continue
        if progressed:
            last_action = time.time()
        if not password and time.time() - last_action > 8:
            return bool(last_action)
        time.sleep(0.5)
    return password_submitted or bool(last_action)


def _wait_for_local_storage_session(browser: OAuthBrowser, *, timeout: int = 300) -> tuple[str, str]:
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        pages = browser.pages()
        # 优先检查已经进入 BlendSpace app 的页面；OAuth 完成后 active_page
        # 有时不是最新 app 页，必须扫描整个 context。
        pages = sorted(
            pages,
            key=lambda page: 0 if "blendspace.ai" in str(getattr(page, "url", "") or "") else 1,
        )
        for page in pages:
            if page.is_closed():
                continue
            url = str(page.url or "")
            if "blendspace.ai" not in url:
                continue
            last_url = url or last_url
            try:
                if page.url.endswith("/chat") or "/chat" in page.url:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            session_id, storage_key = _read_storage_session(page)
            if session_id:
                if storage_key != SESSION_STORAGE_KEY:
                    print(f"[BlendSpace] found session from storage key: {storage_key}")
                return session_id, last_url
        time.sleep(0.5)
    return "", last_url



def _start_blendspace_oauth(browser: OAuthBrowser, *, log_fn=print) -> None:
    """启动 BlendSpace Google OAuth。

    直接访问 api.blendspace.ai 的 OAuth 入口有时会长时间等待文档加载，
    但浏览器其实可能已经跳转到了 Google。这里把超时作为可恢复状态，
    只在既没到 Google、也没拿到 BlendSpace 页面时才回首页点击登录入口。
    """
    page = browser.active_page()
    try:
        page.goto(GOOGLE_OAUTH_LOGIN_URL, wait_until="commit", timeout=90000)
    except Exception as exc:
        current_url = str(getattr(page, "url", "") or "")
        log_fn(f"[BlendSpace] OAuth 入口加载未完成，继续检查当前页面: {exc}")
        if "accounts.google.com" in current_url or "blendspace.ai" in current_url:
            return
    time.sleep(2)
    current_url = str(browser.active_page().url or "")
    if "accounts.google.com" in current_url:
        return
    if "blendspace.ai" in current_url and not _read_local_storage_session(browser.active_page()):
        _open_blendspace_google_oauth(browser, log_fn=log_fn)
        time.sleep(2)
        return
    try:
        browser.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
        _open_blendspace_google_oauth(browser, log_fn=log_fn)
        time.sleep(2)
    except Exception as exc:
        log_fn(f"[BlendSpace] 回退到首页点击 OAuth 入口失败: {exc}")


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    timeout: int = 300,
    log_fn=print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    google_password: str = "",
    reuse_existing_cdp: bool = False,
) -> dict:
    method_text = browser_login_method_text(oauth_provider)

    with OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        reuse_existing_cdp=reuse_existing_cdp,
        log_fn=log_fn,
    ) as browser:
        provider = (oauth_provider or "google").strip().lower()
        if provider and provider != "google":
            raise RuntimeError(f"BlendSpace 当前只支持 Google OAuth: {oauth_provider}")

        # Start from BlendSpace Google OAuth endpoint and then finish Google credential pages in the browser.
        _start_blendspace_oauth(browser, log_fn=log_fn)

        if email_hint and google_password:
            login_timeout = min(max(timeout // 2, 60), 180)
            auto_logged_in = _try_google_password_login(
                browser,
                email=email_hint,
                password=google_password,
                log_fn=log_fn,
                timeout=login_timeout,
            )
            if auto_logged_in:
                log_fn("[BlendSpace] Google OAuth 凭据已自动提交，等待站点写入 session")

        if chrome_user_data_dir or chrome_cdp_url:
            browser.auto_select_google_account()
        else:
            log_fn(f"请在浏览器中完成 BlendSpace OAuth 登录，可使用 {method_text}，最长等待 {timeout} 秒")
            if email_hint:
                log_fn(f"请确认最终登录账号邮箱为: {email_hint}")

        session_id, final_url = _wait_for_local_storage_session(browser, timeout=timeout)
        if not session_id:
            raise RuntimeError(f"BlendSpace OAuth 登录未在 {timeout} 秒内拿到 {SESSION_STORAGE_KEY}")

    resolved_email = finalize_oauth_email("", email_hint, "BlendSpace")
    return {
        "email": resolved_email,
        "session_id": session_id,
        "final_url": final_url,
    }


# Backward-compat alias
register_with_manual_oauth = register_with_browser_oauth
