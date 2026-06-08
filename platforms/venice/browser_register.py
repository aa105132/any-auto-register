"""Venice Seedance 浏览器注册流程。"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


SEEDANCE_LANDING_URL = "https://venice.ai/lp/seedance"
SEEDANCE_SIGNUP_URL = (
    "https://venice.ai/sign-up?redirect_url=%2Flp%2Fseedance%2Fgenerate&source=seedance-landing"
)
SEEDANCE_GENERATE_URL = "https://venice.ai/lp/seedance/generate"
API_SETTINGS_URL = "https://venice.ai/settings/api"

TURNSTILE_SITEKEY = "0x4AAAAAAAWXJGBD7bONzLBd"
TURNSTILE_SITEKEY_INVISIBLE = "0x4AAAAAAAFV93qQdS0ycilX"

API_KEY_PATTERN = re.compile(r"VENICE_INFERENCE_KEY_[A-Za-z0-9_-]+")
TURNSTILE_SITEKEY_PATTERN = re.compile(r"/(0x4[A-Za-z0-9_-]+)(?:/|\\?|$)")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_page_feedback(page) -> str:
    selectors = [
        '[role="alert"]',
        '[data-localization-key]',
        '.cl-formFieldErrorText',
        '.cl-alertText',
        '.cl-formFieldError',
        '.cf-turnstile-error',
    ]
    messages: list[str] = []
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 5)
            for idx in range(count):
                text = (locator.nth(idx).inner_text() or "").strip()
                if text and text not in messages:
                    messages.append(text)
        except Exception:
            continue
    return " | ".join(messages)


def _extract_api_key_from_text(text: str) -> str:
    match = API_KEY_PATTERN.search(text or "")
    return match.group(0) if match else ""


def _click_first(page, selectors: list[str], *, timeout: int = 2000) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            locator.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def _button_disabled(page, selector: str) -> bool:
    try:
        return bool(page.locator(selector).first.is_disabled())
    except Exception:
        return False


class VeniceBrowserRegister:
    def __init__(
        self,
        *,
        captcha,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        api_key_description: str = "seedance-auto",
        expected_credits: int = 500,
        log_fn: Callable[[str], None] = print,
    ):
        self.captcha = captcha
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.api_key_description = api_key_description
        self.expected_credits = expected_credits
        self.log = log_fn

    def _wait_for_signup_form(self, page) -> None:
        page.goto(SEEDANCE_SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector('input[type="email"], input[name="email"]', timeout=30000)

    def _open_signup(self, page) -> None:
        self.log("Open Venice Seedance landing page")
        page.goto(SEEDANCE_LANDING_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)

        if "/sign-up" not in page.url:
            clicked = _click_first(
                page,
                [
                    'button:has-text("Try Seedance Free")',
                    'button:has-text("Claim Your $5 Credit")',
                    'a:has-text("Try Seedance Free")',
                    'a:has-text("Claim Your $5 Credit")',
                ],
                timeout=3000,
            )
            if clicked:
                try:
                    page.wait_for_url("**/sign-up**", timeout=10000)
                except Exception:
                    pass

        if "/sign-up" not in page.url:
            self.log("CTA did not reach sign-up; falling back to Seedance-specific sign-up URL")
        self._wait_for_signup_form(page)

    def _fill_signup_form(self, page, email: str, password: str) -> None:
        self.log(f"Fill Venice sign-up form: {email}")
        page.fill('input[type="email"], input[name="email"]', email)

        password_selectors = 'input[type="password"], input[name="password"]'
        for _ in range(10):
            try:
                if page.locator(password_selectors).count() > 0:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        page.fill(password_selectors, password)

    def _submit_signup(self, page) -> None:
        if not _click_first(
            page,
            [
                'button:has-text("Sign up")',
                'button[type="submit"]',
            ],
            timeout=5000,
        ):
            raise RuntimeError("Could not find the Venice Sign up button")

    def _has_turnstile(self, page) -> bool:
        selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
            '.cf-turnstile',
            '[data-sitekey]',
        ]
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _extract_turnstile_sitekey(self, page) -> str:
        try:
            sitekey = page.evaluate(
                """
                () => {
                    const widget = document.querySelector('[data-sitekey], [data-captcha-sitekey], .cf-turnstile');
                    if (!widget) return '';
                    return widget.getAttribute('data-sitekey') || widget.getAttribute('data-captcha-sitekey') || '';
                }
                """
            )
            if sitekey:
                return str(sitekey).strip()
        except Exception:
            pass

        iframe_selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
        ]
        for selector in iframe_selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() <= 0:
                    continue
                src = locator.get_attribute("src") or ""
                match = TURNSTILE_SITEKEY_PATTERN.search(src)
                if match:
                    return match.group(1)
            except Exception:
                continue
        return ""

    def _solve_turnstile(self, page, sitekey: str) -> Optional[str]:
        if not self.captcha:
            return None
        effective_sitekey = sitekey or TURNSTILE_SITEKEY_INVISIBLE
        try:
            self.log(f"Solving Turnstile: sitekey={effective_sitekey}")
            token = self.captcha.solve_turnstile(page.url, effective_sitekey)
            if token:
                return token
            if effective_sitekey == TURNSTILE_SITEKEY_INVISIBLE and effective_sitekey != TURNSTILE_SITEKEY:
                self.log("Invisible sitekey failed, fallback to visible sitekey")
                return self.captcha.solve_turnstile(page.url, TURNSTILE_SITEKEY)
            return None
        except Exception as exc:
            self.log(f"Turnstile solve failed: {exc}")
            if effective_sitekey == TURNSTILE_SITEKEY_INVISIBLE:
                try:
                    self.log("Retrying with visible sitekey")
                    return self.captcha.solve_turnstile(page.url, TURNSTILE_SITEKEY)
                except Exception:
                    pass
            return None

    def _inject_turnstile_token(self, page, token: str) -> bool:
        safe_token = token.replace("\\", "\\\\").replace("'", "\\'")
        script = f"""
        (function() {{
            const token = '{safe_token}';
            const form = document.querySelector('form') || document.body;
            const names = ['cf-turnstile-response', 'captcha'];
            names.forEach((name) => {{
                let field = document.querySelector(`textarea[name="${{name}}"], input[name="${{name}}"]`);
                if (!field) {{
                    field = document.createElement(name.includes('response') ? 'textarea' : 'input');
                    if (field.tagName === 'INPUT') {{
                        field.type = 'hidden';
                    }}
                    field.name = name;
                    form.appendChild(field);
                }}
                field.value = token;
                field.dispatchEvent(new Event('input', {{ bubbles: true }}));
                field.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }});
            if (typeof window._turnstileTokenCallback === 'function') {{
                window._turnstileTokenCallback(token);
            }}
            if (typeof window.turnstileCallback === 'function') {{
                window.turnstileCallback(token);
            }}
            return true;
        }})();
        """
        try:
            return bool(page.evaluate(script))
        except Exception:
            return False

    def _click_turnstile_checkbox(self, page) -> bool:
        iframe_selectors = [
            'iframe[src*="challenges.cloudflare.com"]',
            'iframe[src*="turnstile"]',
        ]
        for selector in iframe_selectors:
            try:
                frame = page.frame_locator(selector).first
                checkbox = frame.locator('input[type="checkbox"]').first
                checkbox.click(timeout=5000)
                return True
            except Exception:
                continue
        return False

    def _handle_turnstile_if_needed(self, page) -> None:
        time.sleep(2)
        if not self._has_turnstile(page):
            return

        sitekey = self._extract_turnstile_sitekey(page)
        solved = False
        token = self._solve_turnstile(page, sitekey)
        if token:
            solved = self._inject_turnstile_token(page, token)

        if not solved and not self.headless:
            self.log("No injected token was found; trying direct Turnstile checkbox click")
            solved = self._click_turnstile_checkbox(page)

        if solved:
            time.sleep(2)
            if "/sign-up" in page.url and not _button_disabled(page, 'button:has-text("Sign up"), button[type="submit"]'):
                _click_first(page, ['button:has-text("Sign up")', 'button[type="submit"]'], timeout=3000)

    def _otp_inputs_ready(self, page) -> bool:
        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[name*="code"]',
            'input[data-input-otp="true"]',
        ]
        for selector in selectors:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        body = (page.locator("body").inner_text() or "").lower()
        return "verification code" in body or "enter the code" in body or "check your email" in body

    def _fill_otp(self, page, code: str) -> None:
        clean = re.sub(r"\D", "", code or "")
        if not clean:
            raise RuntimeError("Venice OTP is empty")

        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[name*="code"]',
            'input[data-input-otp="true"]',
        ]
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = locator.count()
                if count <= 0:
                    continue
                if count >= len(clean):
                    for idx, ch in enumerate(clean):
                        locator.nth(idx).fill(ch)
                    return
                locator.first.fill(clean)
                return
            except Exception:
                continue

        try:
            page.keyboard.type(clean, delay=60)
        except Exception as exc:
            raise RuntimeError(f"Failed to fill Venice OTP: {exc}") from exc

    def _wait_for_otp_or_login(self, page, timeout: int = 120) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._has_session_cookie(page):
                return "session"
            if self._otp_inputs_ready(page):
                return "otp"
            feedback = _extract_page_feedback(page).lower()
            if any(marker in feedback for marker in ("already", "taken", "exists", "invalid")):
                raise RuntimeError(f"Venice registration page returned an error: {feedback}")
            time.sleep(1)
        raise RuntimeError(f"Venice registration timed out before entering verification or login state: {page.url}")

    def _has_session_cookie(self, page) -> bool:
        try:
            cookies = page.context.cookies()
        except Exception:
            return False
        return any(cookie.get("name", "").startswith("__session") for cookie in cookies)

    def _complete_otp_if_needed(self, page) -> None:
        mode = self._wait_for_otp_or_login(page)
        if mode == "session":
            return
        if not self.otp_callback:
            raise RuntimeError("Venice registration requires an OTP, but otp_callback was not provided")

        self.log("Waiting for Venice email OTP")
        code = self.otp_callback()
        self.log(f"Received Venice OTP: {code}")
        self._fill_otp(page, code)
        _click_first(
            page,
            [
                'button:has-text("Continue")',
                'button:has-text("Verify")',
                'button:has-text("Complete")',
                'button[type="submit"]',
            ],
            timeout=3000,
        )

    def _wait_for_logged_in(self, page, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._has_session_cookie(page):
                try:
                    if "/lp/seedance/generate" not in page.url:
                        page.goto(SEEDANCE_GENERATE_URL, wait_until="domcontentloaded", timeout=30000)
                    return
                except Exception:
                    return
            time.sleep(1)
        raise RuntimeError("Venice registration completed without a logged-in session")

    def _collect_auth_state(self, page) -> dict[str, Any]:
        try:
            access_token = page.evaluate(
                """
                async () => {
                    if (!window.Clerk || !window.Clerk.session) return '';
                    return await window.Clerk.session.getToken();
                }
                """
            )
        except Exception:
            access_token = ""

        cookies = {item["name"]: item["value"] for item in page.context.cookies()}
        session_cookie = str(cookies.get("__session") or cookies.get("__session_aKq7rGhf") or access_token or "")
        client_cookie = str(cookies.get("__client") or "")
        session_payload = _decode_jwt_payload(session_cookie or access_token)
        client_payload = _decode_jwt_payload(client_cookie)
        refresh_token = str(client_payload.get("rotating_token") or "")
        return {
            "access_token": str(access_token or session_cookie or ""),
            "session_token": str(session_cookie or access_token or ""),
            "refresh_token": refresh_token,
            "refresh_token_source": "clerk.__client.rotating_token" if refresh_token else "",
            "client_id": str(client_payload.get("id") or ""),
            "client_cookie": client_cookie,
            "session_cookie": session_cookie,
            "session_id": str(session_payload.get("sid") or ""),
            "user_id": str(session_payload.get("sub") or ""),
        }

    def _fetch_outerface_state(self, page, access_token: str) -> dict[str, Any]:
        self.log("Fetch Venice session, credits, and API Key list")
        return page.evaluate(
            """
            async ({ accessToken }) => {
                const headers = {
                    Authorization: `Bearer ${accessToken}`,
                    Accept: 'application/json',
                    'Content-Type': 'application/json',
                };
                const fetchJson = async (url) => {
                    const resp = await fetch(url, { headers, credentials: 'include' });
                    let body = {};
                    try {
                        body = await resp.json();
                    } catch {
                        body = {};
                    }
                    return { status: resp.status, body };
                };

                const [userSession, apiUsage, apiKeys] = await Promise.all([
                    fetchJson('https://outerface.venice.ai/api/user/session'),
                    fetchJson('https://outerface.venice.ai/api/app/user/api/usage?lookback=7d'),
                    fetchJson('https://outerface.venice.ai/api/app/user/api/api_keys'),
                ]);
                return { userSession, apiUsage, apiKeys };
            }
            """,
            {"accessToken": access_token},
        )

    def _assert_seedance_bonus(self, page, state: dict[str, Any]) -> int:
        user_session = dict((state.get("userSession") or {}).get("body") or {})
        credits = int(user_session.get("veniceCredits") or 0)
        if credits >= self.expected_credits:
            return credits

        body_text = (page.locator("body").inner_text() or "").lower()
        if f"{self.expected_credits} credits".lower() in body_text or "credits added" in body_text:
            return self.expected_credits
        raise RuntimeError(
            f"Seedance 注册未拿到预期积分，当前 credits={credits}，要求至少 {self.expected_credits}"
        )

    def _close_optional_dialogs(self, page) -> None:
        for _ in range(3):
            changed = False
            for selector in (
                'button:has-text("Enable this later")',
                'button:has-text("Maybe later")',
                'button:has-text("Dismiss")',
                'button:has-text("Close")',
                'button:has-text("Skip")',
                'button[aria-label="Close"]',
            ):
                try:
                    locator = page.locator(selector).first
                    if locator.count() <= 0:
                        continue
                    locator.click(timeout=1000)
                    changed = True
                    time.sleep(0.5)
                except Exception:
                    continue
            if not changed:
                return

    def _create_api_key(self, page) -> str:
        self.log("Open Venice API settings page and create API Key")
        page.goto(API_SETTINGS_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        self._close_optional_dialogs(page)

        if not _click_first(
            page,
            ['button:has-text("Generate API Key")', 'button:has-text("Generate Key")'],
            timeout=5000,
        ):
            raise RuntimeError("Could not find the Venice Generate API Key button")

        page.wait_for_selector('text="Generate New API Key"', timeout=15000)
        page.fill('input[aria-label="Description"], input[placeholder*="Description"], input[name="description"]', self.api_key_description)
        if _button_disabled(page, 'button:has-text("Create")'):
            raise RuntimeError("Venice API Key modal input is disabled; the page or key list may not be fully loaded yet")
        _click_first(page, ['button:has-text("Create")'], timeout=5000)

        page.wait_for_selector('text="API Key Created"', timeout=20000)
        content = page.content()
        api_key = _extract_api_key_from_text(content)
        if not api_key:
            try:
                api_key = _extract_api_key_from_text(page.locator("code").first.inner_text())
            except Exception:
                api_key = ""
        if not api_key:
            raise RuntimeError("Venice API Key was created, but the plaintext key could not be extracted from the page")

        verify = page.evaluate(
            """
            async ({ apiKey }) => {
                const resp = await fetch('https://api.venice.ai/api/v1/models', {
                    headers: { Authorization: `Bearer ${apiKey}` },
                });
                return resp.status;
            }
            """,
            {"apiKey": api_key},
        )
        if int(verify or 0) != 200:
            raise RuntimeError(f"Venice API Key verification failed; models endpoint status={verify}")

        _click_first(page, ['button:has-text("Done")', 'button[aria-label="Close"]'], timeout=3000)
        return api_key

    def _trim_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "email": payload.get("email", ""),
            "user_id": payload.get("userId", ""),
            "user_name": payload.get("userName", ""),
            "user_type": payload.get("userType", ""),
            "user_country": payload.get("userCountry", ""),
            "venice_credits": int(payload.get("veniceCredits") or 0),
            "venice_mode": payload.get("veniceMode", ""),
            "referral_code": payload.get("referralCode", ""),
            "rate_limits": payload.get("rateLimits") or {},
        }

    def _trim_api_usage(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "lookback": payload.get("lookback", ""),
            "byKey": payload.get("byKey") or [],
            "topKeyNames": payload.get("topKeyNames") or [],
        }

    def run(self, email: str, password: str) -> dict[str, Any]:
        proxy = _build_proxy_config(self.proxy)
        with sync_playwright() as pw:
            launch_options = {
                "headless": self.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            }
            if proxy:
                launch_options["proxy"] = proxy

            browser = pw.chromium.launch(**launch_options)
            context = browser.new_context(
                viewport={"width": 1400, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/147.0.0.0 Safari/537.36"
                ),
            )
            context.set_default_timeout(60000)
            page = context.new_page()

            try:
                self._open_signup(page)
                self._fill_signup_form(page, email, password)
                self._submit_signup(page)
                self._handle_turnstile_if_needed(page)
                self._complete_otp_if_needed(page)
                self._wait_for_logged_in(page)

                auth_state = self._collect_auth_state(page)
                if not auth_state["access_token"]:
                    raise RuntimeError("Venice registration succeeded but access_token was not captured")
                if not auth_state["refresh_token"]:
                    self.log("Warning: rotating_token was not extracted from the __client cookie")

                outerface_state = self._fetch_outerface_state(page, auth_state["access_token"])
                credits = self._assert_seedance_bonus(page, outerface_state)
                api_key = self._create_api_key(page)
                refreshed_state = self._fetch_outerface_state(page, auth_state["access_token"])

                user_session = dict((refreshed_state.get("userSession") or {}).get("body") or {})
                api_usage = dict((refreshed_state.get("apiUsage") or {}).get("body") or {})
                api_keys = list(((refreshed_state.get("apiKeys") or {}).get("body") or {}).get("data") or [])

                return {
                    "email": email,
                    "password": password,
                    "user_id": auth_state["user_id"] or str(user_session.get("userId") or ""),
                    "session_id": auth_state["session_id"],
                    "access_token": auth_state["access_token"],
                    "refresh_token": auth_state["refresh_token"],
                    "refresh_token_source": auth_state["refresh_token_source"],
                    "session_token": auth_state["session_token"],
                    "client_id": auth_state["client_id"],
                    "client_cookie": auth_state["client_cookie"],
                    "session_cookie": auth_state["session_cookie"],
                    "api_key": api_key,
                    "api_key_description": self.api_key_description,
                    "venice_token": str(user_session.get("token") or ""),
                    "credits": credits,
                    "profile": self._trim_profile(user_session),
                    "api_usage": self._trim_api_usage(api_usage),
                    "api_keys": api_keys,
                    "seedance_bonus_verified": True,
                    "seedance_landing_url": SEEDANCE_LANDING_URL,
                    "seedance_generate_url": SEEDANCE_GENERATE_URL,
                    "checked_at": _utcnow_iso(),
                }
            except PlaywrightTimeoutError as exc:
                feedback = _extract_page_feedback(page)
                raise RuntimeError(f"Venice browser flow timed out: {exc}; feedback={feedback}; url={page.url}") from exc
            finally:
                context.close()
                browser.close()
