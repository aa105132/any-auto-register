"""Grok (x.ai) 浏览器注册流程。"""
from __future__ import annotations

import random
import string
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

from core.oauth_browser import OAuthBrowser

ACCOUNTS_URL = "https://accounts.x.ai"
SIGNUP_URL = f"{ACCOUNTS_URL}/sign-up"

EMAIL_SIGNUP_BUTTON_SELECTORS = [
    'button:has-text("使用邮箱注册")',
    'button:has-text("Sign up with email")',
    'button:has-text("Continue with email")',
    'button:has-text("Email")',
    'button:has(svg.lucide-mail)',
]
EMAIL_INPUT_SELECTORS = [
    'input[type="email"]',
    'input[name="email"]',
    'input[autocomplete="email"]',
    'input[name="username"]',
]
OTP_INPUT_SELECTORS = [
    'input[data-input-otp="true"]',
    'input[autocomplete="one-time-code"]',
    'input[name="code"]',
    'input[inputmode="text"]',
    'input[inputmode="numeric"]',
    'input[maxlength="6"]',
]
PASSWORD_INPUT_SELECTORS = [
    'input[data-testid="password"]',
    'input[name="password"]',
    'input[type="password"]',
    'input[autocomplete="new-password"]',
]
FIRST_NAME_SELECTORS = [
    'input[name="givenName"]',
    'input[name="given_name"]',
    'input[autocomplete="given-name"]',
    'input[placeholder*="First"]',
    'input[placeholder*="名字"]',
]
LAST_NAME_SELECTORS = [
    'input[name="familyName"]',
    'input[name="family_name"]',
    'input[autocomplete="family-name"]',
    'input[placeholder*="Last"]',
    'input[placeholder*="姓"]',
]
CONTINUE_BUTTON_SELECTORS = [
    'button[type="submit"]',
    'button:has-text("继续")',
    'button:has-text("Continue")',
    'button:has-text("验证")',
    'button:has-text("Verify")',
    'button:has-text("确认邮箱")',
    'button:has-text("Confirm email")',
    'button:has-text("完成注册")',
    'button:has-text("Complete sign up")',
    'button:has-text("Sign up")',
]


def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _normalize_code(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isalnum()).upper()[:6]


def _rand_name(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n)).capitalize()


def _all_cookies(page) -> dict[str, str]:
    return {str(c.get("name") or ""): str(c.get("value") or "") for c in page.context.cookies() if c.get("name")}


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items() if name and value)


def _cookie_snapshot(page) -> dict[str, object]:
    cookies = _all_cookies(page)
    return {"cookies": cookies, "cookie_header": _cookie_header(cookies)}


def _first_visible(page, selectors: list[str], *, timeout_ms: int = 0):
    deadline = time.time() + max(timeout_ms, 0) / 1000
    while True:
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible(timeout=300):
                    return loc
            except Exception:
                continue
        if timeout_ms <= 0 or time.time() >= deadline:
            return None
        time.sleep(0.3)


def _click_first(page, selectors: list[str], *, timeout_ms: int = 0) -> bool:
    loc = _first_visible(page, selectors, timeout_ms=timeout_ms)
    if not loc:
        return False
    try:
        loc.click(timeout=5000)
    except Exception:
        loc.click(force=True, timeout=5000)
    return True


def _fill_first(page, selectors: list[str], value: str, *, timeout_ms: int = 0) -> bool:
    loc = _first_visible(page, selectors, timeout_ms=timeout_ms)
    if not loc:
        return False
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        loc.click(timeout=3000)
    except Exception:
        pass
    try:
        loc.fill(value, timeout=5000)
    except Exception:
        loc.press_sequentially(value, timeout=10000)
    try:
        actual = loc.input_value(timeout=1000)
    except Exception:
        actual = value
    return actual == value


def _fill_exact_visible_input(page, selector: str, value: str, *, timeout_ms: int = 0) -> bool:
    deadline = time.time() + max(timeout_ms, 0) / 1000
    while True:
        try:
            locators = page.locator(selector)
            count = locators.count()
        except Exception:
            count = 0
        for idx in range(count):
            loc = locators.nth(idx)
            try:
                if not loc.is_visible(timeout=300):
                    continue
            except Exception:
                continue
            try:
                loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                loc.click(timeout=3000)
            except Exception:
                pass
            try:
                loc.fill(value, timeout=5000)
            except Exception:
                try:
                    loc.press_sequentially(value, timeout=10000)
                except Exception:
                    continue
            try:
                if loc.input_value(timeout=1000) == value:
                    return True
            except Exception:
                return True
        if timeout_ms <= 0 or time.time() >= deadline:
            return False
        time.sleep(0.3)


def _has_password_required_error(page) -> bool:
    try:
        body = page.inner_text("body", timeout=1000) or ""
    except Exception:
        return False
    lowered = body.lower()
    return "您必须提供密码" in body or "password is required" in lowered or "must provide a password" in lowered


def _turnstile_response_value(page) -> str:
    try:
        value = page.evaluate(
            """
            () => {
                const nodes = Array.from(document.querySelectorAll(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                ));
                for (const node of nodes) {
                    const value = String(node.value || node.getAttribute('value') || '').trim();
                    if (value) {
                        return value;
                    }
                }
                return '';
            }
            """
        )
    except Exception:
        value = ""
    return str(value or "").strip()


def _has_turnstile_widget(page) -> bool:
    if _turnstile_response_value(page):
        return True
    try:
        if page.locator('input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]').count() > 0:
            return True
    except Exception:
        pass
    try:
        return any("challenges.cloudflare.com" in str(getattr(frame, "url", "")) for frame in page.frames)
    except Exception:
        return False


def _wait_for_turnstile_ready(
    page,
    *,
    log_fn: Callable[[str], None] = print,
    timeout: int = 120,
) -> bool:
    if not _has_turnstile_widget(page):
        return True

    log_fn("等待 Grok Turnstile 验证通过")
    deadline = time.time() + timeout
    last_click = 0.0
    while time.time() < deadline:
        if _turnstile_response_value(page):
            log_fn("Grok Turnstile 验证已通过")
            return True

        has_frame = False
        try:
            has_frame = any("challenges.cloudflare.com" in str(getattr(frame, "url", "")) for frame in page.frames)
        except Exception:
            has_frame = False

        if (_looks_like_cloudflare(page) or has_frame) and time.time() - last_click >= 4:
            _click_cloudflare_turnstile(page, log_fn=log_fn)
            last_click = time.time()

        time.sleep(1)

    return bool(_turnstile_response_value(page))


def _wait_for_turnstile_widget(page, *, timeout: int = 15) -> bool:
    deadline = time.time() + max(timeout, 0)
    while time.time() < deadline:
        if _has_turnstile_widget(page):
            return True
        time.sleep(0.5)
    return _has_turnstile_widget(page)


def _click_complete_signup_after_challenge(page, *, log_fn: Callable[[str], None] = print) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    if _wait_for_turnstile_widget(page, timeout=15):
        if not _wait_for_turnstile_ready(page, log_fn=log_fn, timeout=120):
            debug_base = _save_debug(page, "turnstile_not_ready")
            raise RuntimeError(f"Grok Turnstile 未通过，已阻止提前提交: {page.url}; debug={debug_base}")
    else:
        log_fn("未检测到 Grok Turnstile，继续提交")

    _click_first(page, CONTINUE_BUTTON_SELECTORS, timeout_ms=5000) or page.keyboard.press("Enter")


def _click_cloudflare_turnstile(page, *, log_fn: Callable[[str], None] = print) -> bool:
    """在当前浏览器上下文里点击可见 Turnstile/CF iframe，不抽取 token。"""
    clicked = False
    for frame in list(getattr(page, "frames", []) or []):
        frame_url = str(getattr(frame, "url", "") or "")
        if "challenges.cloudflare.com" not in frame_url and "turnstile" not in frame_url:
            continue
        try:
            iframe = frame.frame_element()
            box = iframe.bounding_box()
            if box:
                page.mouse.move(box["x"] + min(32, box["width"] / 2), box["y"] + box["height"] / 2)
                page.mouse.down()
                time.sleep(0.12)
                page.mouse.up()
                log_fn("已在浏览器内点击 Turnstile iframe")
                clicked = True
                break
        except Exception:
            pass
        for selector in ('input[type="checkbox"]', '[role="checkbox"]', 'label', 'button', 'body'):
            try:
                loc = frame.locator(selector).first
                loc.click(timeout=1500, force=True)
                log_fn(f"已在浏览器内点击 Turnstile 元素: {selector}")
                clicked = True
                break
            except Exception:
                pass
        if clicked:
            break
    return clicked


def _looks_like_cloudflare(page) -> bool:
    try:
        body = (page.inner_text("body", timeout=1000) or "").lower()
    except Exception:
        body = ""
    return any(x in body for x in ("just a moment", "checking your browser", "verify you are human", "确认您是真人"))


def _wait_for_form_or_cf_clear(page, *, log_fn: Callable[[str], None], timeout: int = 120) -> None:
    deadline = time.time() + timeout
    clicked = False
    while time.time() < deadline:
        if _first_visible(page, EMAIL_SIGNUP_BUTTON_SELECTORS + EMAIL_INPUT_SELECTORS, timeout_ms=200):
            return
        if _looks_like_cloudflare(page) or any("challenges.cloudflare.com" in str(getattr(f, "url", "")) for f in page.frames):
            if not clicked:
                clicked = _click_cloudflare_turnstile(page, log_fn=log_fn)
            time.sleep(2)
            continue
        time.sleep(0.5)


def _wait_for_sso(page, *, timeout: int = 90) -> dict[str, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cookies = _all_cookies(page)
        if cookies.get("sso"):
            return cookies
        time.sleep(1)
    return _all_cookies(page)


def _save_debug(page, label: str) -> Path:
    root = Path(__file__).resolve().parents[2] / "output" / "grok_browser_debug"
    root.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    base = root / f"{label}_{stamp}"
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
    except Exception:
        pass
    try:
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    return base


class GrokBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
        chrome_user_data_dir: str = "",
        chrome_cdp_url: str = "",
        use_oauth_browser: bool = False,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.log = log_fn
        self.chrome_user_data_dir = str(chrome_user_data_dir or "")
        self.chrome_cdp_url = str(chrome_cdp_url or "")
        self.use_oauth_browser = bool(use_oauth_browser or self.chrome_user_data_dir or self.chrome_cdp_url)

    def _run_on_page(self, page, email: str, password: str) -> dict:
        self.log("打开 Grok 注册页（浏览器模式）")
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        _wait_for_form_or_cf_clear(page, log_fn=self.log, timeout=120)

        _click_first(page, EMAIL_SIGNUP_BUTTON_SELECTORS, timeout_ms=8000)
        if not _fill_first(page, EMAIL_INPUT_SELECTORS, email, timeout_ms=30000):
            debug_base = _save_debug(page, "email_input_missing")
            raise RuntimeError(f"未找到 Grok 邮箱输入框: {page.url}; debug={debug_base}")
        self.log(f"填写 Grok 邮箱: {email}")
        _click_first(page, CONTINUE_BUTTON_SELECTORS, timeout_ms=5000) or page.keyboard.press("Enter")

        if not _first_visible(page, OTP_INPUT_SELECTORS, timeout_ms=45000):
            debug_base = _save_debug(page, "otp_input_missing")
            raise RuntimeError(f"未进入 Grok 验证码页面: {page.url}; debug={debug_base}")
        self.log("等待 Grok 邮箱验证码")
        code = _normalize_code(self.otp_callback())
        if not code:
            raise RuntimeError("未获取到 Grok 验证码")
        self.log(f"填写 Grok 验证码: {code}")
        if not _fill_first(page, OTP_INPUT_SELECTORS, code, timeout_ms=10000):
            debug_base = _save_debug(page, "otp_fill_failed")
            raise RuntimeError(f"Grok 验证码输入失败: debug={debug_base}")
        _click_first(page, CONTINUE_BUTTON_SELECTORS, timeout_ms=5000) or page.keyboard.press("Enter")

        first = _rand_name()
        last = _rand_name()
        if _first_visible(page, FIRST_NAME_SELECTORS + LAST_NAME_SELECTORS + PASSWORD_INPUT_SELECTORS, timeout_ms=30000):
            _fill_first(page, FIRST_NAME_SELECTORS, first, timeout_ms=1000)
            _fill_first(page, LAST_NAME_SELECTORS, last, timeout_ms=1000)
            password_filled = _fill_exact_visible_input(page, 'input[name="password"]', password, timeout_ms=5000)
            if not password_filled:
                password_filled = _fill_first(page, PASSWORD_INPUT_SELECTORS, password, timeout_ms=5000)
            if not password_filled:
                debug_base = _save_debug(page, "password_fill_failed")
                raise RuntimeError(f"Grok 密码输入失败: {page.url}; debug={debug_base}")
            self.log("填写 Grok 姓名/密码")
            for checkbox in page.locator('input[type="checkbox"]').all():
                try:
                    if checkbox.is_visible(timeout=200):
                        checkbox.check(force=True)
                except Exception:
                    pass
            _click_complete_signup_after_challenge(page, log_fn=self.log)
            time.sleep(1)
            if _has_password_required_error(page):
                password_filled = _fill_exact_visible_input(page, 'input[name="password"]', password, timeout_ms=5000)
                if not password_filled:
                    debug_base = _save_debug(page, "password_required_after_submit")
                    raise RuntimeError(f"Grok 密码提交后仍为空: {page.url}; debug={debug_base}")
                self.log("重新填写 Grok 密码并等待验证后提交")
                _click_complete_signup_after_challenge(page, log_fn=self.log)

        if _looks_like_cloudflare(page) or any("challenges.cloudflare.com" in str(getattr(f, "url", "")) for f in page.frames):
            if _wait_for_turnstile_ready(page, log_fn=self.log, timeout=120):
                _click_first(page, CONTINUE_BUTTON_SELECTORS, timeout_ms=5000) or page.keyboard.press("Enter")

        self.log("等待 Grok sso cookie")
        cookies = _wait_for_sso(page, timeout=120)
        sso = cookies.get("sso", "")
        if not sso:
            debug_base = _save_debug(page, "sso_missing")
            raise RuntimeError(f"未获取到 Grok sso cookie: {page.url}; debug={debug_base}")
        snapshot = _cookie_snapshot(page)
        self.log(f"注册成功: {email}")
        return {
            "email": email,
            "password": password,
            "given_name": first,
            "family_name": last,
            "sso": sso,
            "sso_rw": cookies.get("sso-rw", ""),
            "cookies": snapshot["cookies"],
            "cookie_header": snapshot["cookie_header"],
        }

    def run(self, email: str, password: str) -> dict:
        if not self.otp_callback:
            raise RuntimeError("Grok 浏览器注册需要邮箱验证码但未提供 otp_callback")

        if self.use_oauth_browser:
            with OAuthBrowser(
                proxy=self.proxy,
                headless=self.headless,
                chrome_user_data_dir=self.chrome_user_data_dir,
                chrome_cdp_url=self.chrome_cdp_url,
                reuse_existing_cdp=bool(self.chrome_cdp_url),
                log_fn=self.log,
            ) as browser:
                page = browser.new_page()
                return self._run_on_page(page, email, password)

        proxy = _build_proxy_config(self.proxy)
        launch_opts = {"headless": self.headless}
        if proxy:
            launch_opts["proxy"] = proxy

        with Camoufox(**launch_opts) as browser:
            page = browser.new_page()
            return self._run_on_page(page, email, password)
