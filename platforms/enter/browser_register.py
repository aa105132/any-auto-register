"""Enter platform - browser registration via Playwright CDP with real Chrome.

Launches a real Chrome instance with remote debugging, connects Playwright
over CDP, fills the Auth0 signup/login form, clicks through Turnstile,
and extracts the authorization code from the callback URL.
"""

from __future__ import annotations

import random
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import requests
from platforms.enter.core import (
    AUTH0_DOMAIN,
    API_AUDIENCE,
    CLIENT_ID,
    CODE_CHALLENGE,
    CODE_VERIFIER,
    REDIRECT_URI,
    EnterClient,
    extract_ai_api_token,
    is_success_response,
    _parse_auth_code_from_url,
)

try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ModuleNotFoundError:
    Browser = Any
    BrowserContext = Any
    Page = Any
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

DEFAULT_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


class EnterBrowserRegistrar:

    _cleanup_lock = threading.Lock()
    _cleanup_inflight: set[str] = set()

    def __init__(
        self,
        captcha: Any = None,
        headless: bool = False,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        referrer_code: str = "",
        workspace_id: str = "10000010136",
        timeout: int = 120,
        chrome_path: str = "",
        cdp_url: str = "",
        profile_root_dir: str = "output/browser_auth_profiles",
        cleanup_profile_dir: bool = True,
        log_fn: Any = None,
    ):
        self._captcha = captcha
        self._headless = headless
        self._proxy = proxy
        self._otp_callback = otp_callback
        self._referrer_code = referrer_code
        self._workspace_id = workspace_id
        self._timeout = timeout
        self._chrome_path = chrome_path
        self._cdp_url = cdp_url
        self._profile_root_dir = profile_root_dir
        self._cleanup_profile_dir = cleanup_profile_dir
        self._log = log_fn or (lambda msg: None)
        self._session = requests.Session()

    def _l(self, msg: str) -> None:
        self._log(f"[enter:browser] {msg}")

    def run(self, email: str, password: str) -> dict[str, Any]:
        if not PLAYWRIGHT_AVAILABLE:
            self._l("Playwright not available, cannot run browser flow")
            raise RuntimeError("Enter browser registration requires Playwright. pip install playwright && playwright install chromium")

        launch_meta = self._prepare_chrome()
        browser: Browser | None = None
        page: Page | None = None
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                if not browser.contexts:
                    raise RuntimeError("CDP connected but no browser context found")
                context = browser.contexts[0]
                page = context.new_page()

                auth_code = self._run_auth_flow(page, email, password)

        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            self._teardown_chrome(launch_meta)

        if not auth_code:
            raise RuntimeError("Failed to obtain authorization code from browser flow")

        self._l(f"got auth_code, exchanging for tokens...")
        return self._exchange_and_enrich(email, password, auth_code)

    def _build_signup_url(self, state: str = "") -> str:
        state = state or f"signup-{random.randint(10000, 99999)}"
        params = {
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "response_mode": "query",
            "scope": "openid profile email offline_access",
            "audience": API_AUDIENCE,
            "state": state,
            "code_challenge": CODE_CHALLENGE,
            "code_challenge_method": "S256",
            "screen_hint": "signup",
        }
        return f"https://{AUTH0_DOMAIN}/authorize?{urllib.parse.urlencode(params)}"

    def _run_auth_flow(self, page: Page, email: str, password: str) -> str | None:
        signup_url = self._build_signup_url()

        self._l("navigating to Auth0 signup...")
        page.goto(signup_url, wait_until="domcontentloaded", timeout=self._timeout * 1000)

        # New Auth0 Universal Login is identifier-first: first screen has email + captcha only.
        email_selector = "input[name='email'], input[type='email'], input#username"
        page.wait_for_selector(email_selector, timeout=30_000)
        page.locator(email_selector).first.fill(email)
        page.wait_for_timeout(800)

        self._l("solving turnstile via CDP click...")
        self._click_turnstile_until_token(page)

        self._l("clicking Continue on identifier step...")
        self._click_submit_no_wait(page)

        return self._drive_post_identifier_steps(page, password)

    def _drive_post_identifier_steps(self, page: Page, password: str) -> str | None:
        password_selector = "input[name='password'], input[type='password'], input#password"
        otp_selectors = [
            "input[name='code']",
            "input[name='verification_code']",
            "input[name='otp']",
            "input[inputmode='numeric']",
            "input[autocomplete='one-time-code']",
            "input[placeholder*='code' i]",
            "input[aria-label*='code' i]",
        ]
        password_entered = False
        otp_submitted_count = 0
        deadline = time.time() + max(90, self._timeout)
        while time.time() < deadline:
            auth_code = _parse_auth_code_from_url(page.url)
            if auth_code:
                return auth_code
            if self._has_forbidden_email_domain_error(page):
                raise RuntimeError("email domain is not allowed: enter_email_domain_not_allowed")

            password_input = self._first_visible_locator(page, [password_selector])
            if password_input and not password_entered:
                if self._has_forbidden_email_domain_error(page):
                    raise RuntimeError("email domain is not allowed: enter_email_domain_not_allowed")
                self._l("password step found, entering password...")
                password_input.fill(password)
                page.wait_for_timeout(800)
                self._click_submit_no_wait(page)
                password_entered = True
                page.wait_for_timeout(1500)
                continue

            # AnyCap/Auth0 注册可能有多步 OTP（首次邮箱验证 + 登录/设备验证）。
            # 旧逻辑用 otp_entered 单次标志，第二次 OTP 输入框出现后不再填码，
            # 导致 120s 等不到 auth_code timeout。改成每次出现新 OTP 输入框都
            # 重新收码填入：wait_for_code 的 before_ids 已排除已读邮件，不会读到旧码。
            otp_input = self._first_visible_locator(page, otp_selectors)
            if otp_input:
                if not self._otp_callback:
                    self._l("OTP input found but no OTP callback configured")
                    return None
                otp_submitted_count += 1
                self._l(f"OTP input found (step {otp_submitted_count}), getting code from mailbox...")
                otp = self._otp_callback()
                if not otp:
                    self._l("OTP callback returned empty code")
                    return None
                self._l("entering OTP...")
                otp_input.fill(otp)
                page.wait_for_timeout(800)
                self._click_submit_no_wait(page)
                page.wait_for_timeout(2000)
                continue

            # Some Auth0 paths show password after OTP; some redirect directly.
            auth_code = self._wait_for_auth_code(page, timeout=3)
            if auth_code:
                return auth_code
            page.wait_for_timeout(1000)
        self._l(f"auth flow timed out at url={page.url}")
        return None


    def _body_text(self, page: Page) -> str:
        try:
            return str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
        except Exception:
            return ""

    def _normalized_body_text(self, page: Page) -> str:
        return " ".join(self._body_text(page).lower().split())

    def _has_forbidden_email_domain_error(self, page: Page) -> bool:
        text = self._normalized_body_text(page)
        return (
            "email domain is not allowed" in text
            or "domain is not allowed" in text
            or "not allowed to sign up" in text
        )

    def _first_visible_locator(self, page: Page, selectors: list[str]):
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible(timeout=500):
                    return loc
            except Exception:
                continue
        return None

    def _wait_for_any_selector(self, page: Page, selectors: list[str], timeout_ms: int = 30_000):
        deadline = time.time() + timeout_ms / 1000
        last_error = None
        while time.time() < deadline:
            for selector in selectors:
                try:
                    loc = page.locator(selector).first
                    if loc.count() > 0 and loc.is_visible(timeout=500):
                        return loc
                except Exception as exc:
                    last_error = exc
            page.wait_for_timeout(500)
        return None

    def _click_submit_no_wait(self, page: Page) -> None:
        """Click the visible Auth0 submit button without waiting for implicit navigation."""
        selectors = [
            "button[type='submit']:not([aria-hidden='true']):has-text('Continue')",
            "button[type='submit']:not([aria-hidden='true']):has-text('Sign up')",
            "button[type='submit']:not([aria-hidden='true'])",
        ]
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    try:
                        loc.click(timeout=10_000, no_wait_after=True)
                    except TypeError:
                        loc.click(timeout=10_000)
                    return
            except Exception:
                continue
        page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button[type="submit"]'));
            const btn = buttons.find(b => b.offsetParent !== null && b.getAttribute('aria-hidden') !== 'true');
            if (!btn) throw new Error('visible submit button not found');
            btn.click();
        }""")

    def _click_turnstile_until_token(self, page: Page) -> str:
        for attempt in range(6):
            box = page.locator("#ulp-auth0-v2-captcha").bounding_box()
            if not box:
                token = self._read_turnstile_token(page)
                if token:
                    self._l(f"turnstile token found (length={len(token)})")
                    return token
                continue

            x = box["x"] + min(22, max(16, box["width"] * 0.08))
            y = box["y"] + min(26, max(18, box["height"] * 0.45))
            page.mouse.move(x - 20, y - 8, steps=12)
            page.wait_for_timeout(120)
            page.mouse.click(x, y, delay=120)
            page.wait_for_timeout(4000)

            token = self._read_turnstile_token(page)
            if token:
                self._l(f"turnstile token obtained (length={len(token)})")
                return token
        return ""

    def _read_turnstile_token(self, page: Page) -> str:
        return str(
            page.evaluate("""
                () => {
                  const f = document.querySelector(
                    "input[name='captcha'], textarea[name='captcha'], input[name='cf-turnstile-response'], textarea[name='cf-turnstile-response']"
                  );
                  return f ? (f.value || '') : '';
                }
            """)
            or ""
        )

    def _wait_for_auth_code(self, page: Page, timeout: int = 30) -> str | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = _parse_auth_code_from_url(page.url)
            if code:
                return code
            page.wait_for_timeout(1000)
        return None

    def _exchange_and_enrich(self, email: str, password: str, auth_code: str) -> dict[str, Any]:
        client = EnterClient(proxy=self._proxy, log_fn=self._log)
        tokens = client.exchange_code_for_tokens(auth_code)
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")

        if not access_token:
            raise RuntimeError(f"Token exchange failed: {tokens}")

        result: dict[str, Any] = {
            "email": email,
            "password": password,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": tokens.get("id_token", ""),
            "expires_in": tokens.get("expires_in", 0),
            "token_type": tokens.get("token_type", ""),
        }

        ws_id = self._workspace_id
        ws_info = client.get_workspaces(access_token)
        if isinstance(ws_info, dict):
            ws_list = (ws_info.get("data") or {}).get("workspaces") or []
            if ws_list:
                ws = ws_list[0]
                ws_id = ws.get("id", ws_id)
                result["workspace_id"] = ws_id
                result["plan_type"] = ws.get("plan_type", "")
                credits = ws.get("credits_balance", {})
                result["balance"] = credits.get("total", 0)
                breakdown = credits.get("breakdown") or {}
                result["balance_bonus"] = breakdown.get("bonus", 0)
                result["balance_daily"] = breakdown.get("daily", 0)
                result["balance_monthly"] = breakdown.get("monthly", 0)
                result["balance_purchase"] = breakdown.get("purchase", 0)
                ent = ws.get("entitlement") or {}
                result["entitlement_daily_credits"] = ent.get("daily_credits", 0)
                result["entitlement_monthly_build"] = ent.get("monthly_build_credits", 0)
                result["entitlement_monthly_ai"] = ent.get("monthly_ai_credits", 0)
                result["entitlement_plan_name"] = ent.get("name", "Free")
                result["subscription_status"] = ws.get("subscription_status", "")
                result["enter_ai_credits_status"] = ws.get("enter_ai_credits_status", "")

        if self._referrer_code:
            try:
                ref = client.claim_referral(access_token, self._referrer_code)
                result["referral_claimed"] = isinstance(ref, dict) and ref.get("code") == 0
                if result["referral_claimed"]:
                    self._l("referral claimed (+100 bonus)")
                    ws_after = client.get_workspaces(access_token)
                    if isinstance(ws_after, dict):
                        ws_a = ((ws_after.get("data") or {}).get("workspaces") or [None])[0]
                        if ws_a:
                            cb = ws_a.get("credits_balance", {})
                            result["balance"] = cb.get("total", result.get("balance", 0))
                            result["balance_bonus"] = (cb.get("breakdown") or {}).get("bonus", result.get("balance_bonus", 0))
            except Exception as exc:
                self._l(f"referral claim failed (non-fatal): {exc}")

        user_info = client.get_user_info(access_token)
        if isinstance(user_info, dict):
            udata = (user_info.get("data") or {}).get("user") or {}
            result["user_id"] = udata.get("user_id", "")
            result["referral_code_self"] = udata.get("referral_code", "")

        import uuid
        project_name = f"enter-project-{uuid.uuid4().hex[:6]}"
        try:
            proj = client.get_or_create_project(access_token, ws_id, project_name, "Create a minimal hello world web app.")
            if isinstance(proj, dict):
                pdata = (proj.get("data") or {}).get("project") or {}
                result["project_id"] = pdata.get("project_id", "")
                result["project_name"] = project_name
                result["preview_url"] = pdata.get("preview_url", "")
                result["thread_id"] = pdata.get("thread_id", "")
            self._l(f"project: {result.get('project_id', 'FAILED')}")
        except Exception as exc:
            self._l(f"project create failed (non-fatal): {exc}")

        project_id = result.get("project_id", "")

        if project_id:
            try:
                client.enable_entercloud(access_token, project_id)
                for _ in range(10):
                    ec = client.get_entercloud_status(access_token, project_id)
                    if isinstance(ec, dict) and (ec.get("data") or {}).get("enabled"):
                        ec_data = ec["data"]
                        binding = ec_data.get("binding") or {}
                        instance = ec_data.get("instance") or {}
                        result["entercloud_enabled"] = True
                        result["entercloud_setup_completed"] = binding.get("setup_completed", False)
                        result["entercloud_provider"] = instance.get("provider", "")
                        result["entercloud_cloud_ref"] = instance.get("cloud_ref", "")
                        result["entercloud_api_url"] = instance.get("api_url", "")
                        result["entercloud_anon_key"] = instance.get("anon_key", "")
                        break
                    time.sleep(3.0)
                self._l(f"entercloud enabled={result.get('entercloud_enabled', False)}")
            except Exception as exc:
                self._l(f"entercloud failed (non-fatal): {exc}")

        if project_id:
            try:
                client.connect_ai_capability(access_token, project_id)
                for _ in range(10):
                    stats = client.get_ai_capability_stats(access_token, ws_id, project_id)
                    if isinstance(stats, dict) and is_success_response(stats):
                        ai_data = stats.get("data") or {}
                        result["ai_api_token"] = extract_ai_api_token(ai_data) or extract_ai_api_token(stats)
                        result["ai_connection_state"] = (
                            ai_data.get("aiConnectionState")
                            or ai_data.get("ai_connection_state")
                            or ai_data.get("connectionState")
                            or ""
                        )
                        if result["ai_api_token"]:
                            break
                    time.sleep(3.0)
                self._l(f"ai_token={'ok' if result.get('ai_api_token') else 'FAILED'}")
            except Exception as exc:
                self._l(f"ai capability failed (non-fatal): {exc}")

        if project_id:
            try:
                remix = client.remix_project(access_token, ws_id, project_id)
                if isinstance(remix, dict):
                    rcode = remix.get("code", -1)
                    rdata = remix.get("data") or {}
                    remix_pid = rdata.get("project_id", "") if isinstance(rdata, dict) else ""
                    if rcode == 0 and remix_pid:
                        self._l(f"remix quest ok -> {remix_pid}")
                        result["remixed_project_id"] = remix_pid
            except Exception as exc:
                self._l(f"remix quest failed (non-fatal): {exc}")

        try:
            quests = client.get_classroom_quests(access_token)
            if isinstance(quests, dict) and quests.get("code") == 0:
                qdata = quests.get("data", {}).get("quests", {})
                claimed = quests.get("data", {}).get("total_claimed_credits", 0)
                completed = []
                for category, items in qdata.items():
                    if isinstance(items, list):
                        for q in items:
                            if isinstance(q, dict) and q.get("status") == "completed":
                                completed.append(f"{q.get('quest_id')}(+{q.get('reward_amount', 0)})")
                self._l(f"quests: total_claimed={claimed}, completed={completed}")
                result["quest_credits_claimed"] = claimed
                result["quests_completed"] = completed
        except Exception as exc:
            self._l(f"quests status check failed (non-fatal): {exc}")

        return result

    def _prepare_chrome(self) -> dict[str, Any]:
        if self._cdp_url:
            return {"cdp_url": self._cdp_url, "process": None, "profile_dir": None}

        port = self._find_free_port()
        profile_dir = Path(self._profile_root_dir).resolve() / f"enter-{int(time.time() * 1000)}-{random.randint(1000, 9999)}"
        profile_dir.mkdir(parents=True, exist_ok=True)

        chrome_path = self._resolve_chrome_path()
        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1440,960",
            "--lang=en-US",
            "--disable-translate",
            "--disable-features=Translate,TranslateUI",
        ]
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_cdp(port)

        return {"cdp_url": f"http://127.0.0.1:{port}", "process": process, "profile_dir": profile_dir}

    def _teardown_chrome(self, meta: dict[str, Any]) -> None:
        process = meta.get("process")
        if process:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                process.wait(timeout=10)
            except Exception:
                pass

        profile_dir = meta.get("profile_dir")
        if self._cleanup_profile_dir and profile_dir is not None and profile_dir.exists():
            # Chrome 子进程可能仍占着 profile 文件锁（SQLite/GPU cache），
            # taskkill /T 后需等文件锁释放再删。ignore_errors=True 会静默失败
            # 导致 profile 堆积（每个 80-100MB，几十个就占数 GB）。这里改成
            # 带重试的删除：先等 1.5s，删失败再等 2s 重试，最后兜底 onerror 强删。
            removed = False
            for attempt in range(3):
                try:
                    shutil.rmtree(profile_dir, ignore_errors=False)
                    removed = True
                    break
                except Exception:
                    time.sleep(1.5 if attempt == 0 else 2.0)
            if not removed:
                try:
                    # 兜底：忽略错误强删（保留旧静默行为，至少尽力清一部分）
                    shutil.rmtree(profile_dir, ignore_errors=True)
                except Exception:
                    pass

    def _resolve_chrome_path(self) -> str:
        if self._chrome_path and Path(self._chrome_path).exists():
            return self._chrome_path
        for candidate in DEFAULT_CHROME_PATHS:
            if Path(candidate).exists():
                return candidate
        raise RuntimeError("Chrome not found. Set chrome_path or install Chrome.")

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            return int(s.getsockname()[1])

    def _wait_for_cdp(self, port: int) -> None:
        deadline = time.time() + 30
        url = f"http://127.0.0.1:{port}/json/version"
        while time.time() < deadline:
            try:
                r = requests.get(url, timeout=1.5)
                if r.ok:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"Chrome CDP port {port} not ready")
