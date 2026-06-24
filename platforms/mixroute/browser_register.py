"""MixRoute CDP 混合协议注册 worker（真实 Chrome 填表 + 协议拿 key）。

链路：
1. 真实 Chrome（带 --disable-blink-features=AutomationControlled）加载
   console.mixroute.ai/register。
2. 填 username/password/confirm/email，Cloudflare Turnstile widget 在真实
   Chrome 下自动通过（managed/invisible 模式），写 cf-turnstile-response 隐藏域。
3. 点 "Get Verify Code" → /api/verification 发送邮箱验证码。
4. 等 otp_callback（IMAP 收码）拿到验证码，填入 "Email Verification Code" 输入框。
5. 点 "Sign Up" → /api/user/register 提交，成功后跳转 /login（new-api 注册后
   不直接落地 dashboard，需用 username+password 登录）。
6. 导航到 /login，用 username+password 登录，落地 /dashboard。
7. 从浏览器 localStorage 读 token + user（new-api 会话），同步到协议 session。
8. 协议 POST /api/token/ 创建 API Key（复用 core.create_api_key_http）。

为何需要浏览器：MixRoute 的 Turnstile 在部分 IP 下对纯 HTTP 强校验
（remote 打码 token 可能被拒），真实 Chrome 让 Turnstile widget 自然通过。
注册成功后拿 key 仍是纯协议（POST /api/token/），比浏览器 DOM 点 Create Key 更稳。
"""
from __future__ import annotations

import random
import re
import shutil
import socket
import string
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import requests

try:
    from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ModuleNotFoundError:
    Browser = Any
    BrowserContext = Any
    Page = Any
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

from core.cancel_token import CancelToken, check_cancel
from platforms.mixroute.core import (
    API_BASE,
    CONSOLE_URL,
    DASHBOARD_URL,
    LOGIN_URL,
    REGISTER_URL,
    SITE_URL,
    TOKEN_URL,
    _build_session,
    _cookie_header,
    _normalize_api_key,
    _session_cookie_dict,
    apply_session_auth,
    create_api_key_http,
    get_user_self_http,
    login_http,
    verify_api_key_http,
)


REGISTER_PAGE_URL = f"{CONSOLE_URL}/register"
LOGIN_PAGE_URL = f"{CONSOLE_URL}/login"

DEFAULT_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def _random_username(email: str = "") -> str:
    base = re.sub(r"[^A-Za-z0-9]", "", (email.split("@", 1)[0] if email else "")) or "user"
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{base[:8]}{suffix}"


class MixRouteBrowserRegistrar:
    """浏览器填表注册 + 协议拿 key 的混合 worker。"""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        key_name: str = "auto-register",
        timeout: int = 240,
        chrome_path: str = "",
        cdp_url: str = "",
        headless: bool = False,
        profile_root_dir: str = "output/browser_auth_profiles",
        cleanup_profile_dir: bool = True,
        log_fn: Callable[[str], None] | None = None,
        cancel_token: CancelToken | None = None,
    ):
        self._proxy = proxy
        self._otp_callback = otp_callback
        self._key_name = key_name
        self._timeout = timeout
        self._chrome_path = chrome_path
        self._cdp_url = cdp_url
        self._headless = headless
        self._profile_root_dir = profile_root_dir
        self._cleanup_profile_dir = cleanup_profile_dir
        self._log = log_fn or (lambda msg: None)
        self._cancel_token = cancel_token

    def _poll_cancel(self) -> None:
        check_cancel(self._cancel_token)

    def _l(self, msg: str) -> None:
        self._log(f"[mixroute:browser] {msg}")

    # --- Chrome 启动/清理 ---

    def _resolve_chrome_path(self) -> str:
        if self._chrome_path and Path(self._chrome_path).exists():
            return self._chrome_path
        for candidate in DEFAULT_CHROME_PATHS:
            if Path(candidate).exists():
                return candidate
        raise RuntimeError("MixRoute 浏览器注册需要 Chrome，未找到 chrome 可执行文件")

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            return int(s.getsockname()[1])

    @staticmethod
    def _wait_for_cdp(port: int, timeout: int = 30) -> None:
        import urllib.request

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                    if getattr(r, "status", 0) == 200:
                        return
            except Exception:
                time.sleep(0.5)
        raise RuntimeError(f"MixRoute Chrome CDP 未就绪 port={port}")

    def _prepare_chrome(self) -> dict[str, Any]:
        if self._cdp_url:
            return {"cdp_url": self._cdp_url, "process": None, "profile_dir": None}
        port = self._find_free_port()
        profile_dir = Path(self._profile_root_dir).resolve() / f"mixroute-{int(time.time()*1000)}-{random.randint(1000,9999)}"
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
            "--disable-blink-features=AutomationControlled",
            "--disable-features=Translate,TranslateUI",
            "--window-position=-32000,-32000" if self._headless else "",
        ]
        args = [a for a in args if a]
        if self._headless:
            args.append("--headless=new")
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
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass

    # --- 浏览器填表辅助 ---

    @staticmethod
    def _set_input(page: Page, selectors: list[str], value: str) -> bool:
        """用 JS 设值（兼容 React 受控输入）。"""
        return bool(page.evaluate(
            """({sels, val}) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                for (const sel of sels) {
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {
                        setter.call(el, val);
                        el.dispatchEvent(new Event('input', {bubbles: true}));
                        el.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                }
                return false;
            }""",
            {"sels": selectors, "val": value},
        ))

    def _fill_register_form(self, page: Page, *, username: str, password: str, email: str) -> None:
        self._l(f"填写注册表单: username={username} email={email}")
        self._set_input(page, ["input[placeholder='Enter your username']", "input[name='username']"], username)
        page.wait_for_timeout(300)
        self._set_input(page, ["input[placeholder='Enter password, at least 8 characters']", "input[type='password']:first-of-type"], password)
        page.wait_for_timeout(300)
        # Confirm Password 是第二个 password 输入
        page.evaluate(
            """(val) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                const pws = Array.from(document.querySelectorAll("input[type='password']"));
                if (pws.length >= 2) {
                    setter.call(pws[1], val);
                    pws[1].dispatchEvent(new Event('input', {bubbles: true}));
                    pws[1].dispatchEvent(new Event('change', {bubbles: true}));
                }
            }""",
            password,
        )
        page.wait_for_timeout(300)
        self._set_input(page, ["input[placeholder='Enter your email']", "input[type='email']", "input[name='email']"], email)
        page.wait_for_timeout(500)

    def _click_button(self, page: Page, texts: list[str]) -> bool:
        return bool(page.evaluate(
            """(texts) => {
                const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                for (const text of texts) {
                    const btn = btns.find(b => (b.textContent || '').trim().includes(text) && b.offsetParent !== null && !b.disabled);
                    if (btn) { btn.click(); return true; }
                }
                return false;
            }""",
            texts,
        ))

    def _wait_turnstile_ready(self, page: Page, deadline: float) -> bool:
        """等 Turnstile widget 自动通过，cf-turnstile-response 隐藏域有值。"""
        while time.time() < deadline:
            self._poll_cancel()
            try:
                token = str(page.evaluate(
                    """() => {
                      const el = document.querySelector("input[name='cf-turnstile-response'], textarea[name='cf-turnstile-response']");
                      return el ? (el.value || '') : '';
                    }"""
                ) or "")
                if token:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def _wait_for_redirect(self, page: Page, deadline: float, url_contains: str) -> bool:
        while time.time() < deadline:
            self._poll_cancel()
            if url_contains in (page.url or ""):
                return True
            time.sleep(1)
        return False

    def _read_session(self, context: BrowserContext) -> dict[str, str]:
        """从浏览器 localStorage 读 new-api 会话 token/user。"""
        token = ""
        user_json = ""
        for page in context.pages:
            if page.is_closed():
                continue
            try:
                token = token or str(page.evaluate("() => localStorage.getItem('token') || ''") or "")
                user_json = user_json or str(page.evaluate("() => localStorage.getItem('user') || ''") or "")
            except Exception:
                continue
            if token and user_json:
                break
        user: dict[str, Any] = {}
        if user_json:
            try:
                import json
                user = json.loads(user_json) if isinstance(json.loads(user_json), dict) else {}
            except Exception:
                user = {}
        return {"token": token, "user": user}

    def run(self, *, email: str, password: str, username: str = "") -> dict[str, Any]:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("MixRoute 浏览器注册需要 Playwright：pip install playwright && playwright install chromium")
        if not self._otp_callback:
            raise RuntimeError("MixRoute 浏览器注册需要 otp_callback（IMAP 收码）")

        username = str(username or "").strip() or _random_username(email)
        launch_meta = self._prepare_chrome()
        browser: Browser | None = None
        page: Page | None = None
        session_info: dict[str, Any] = {}
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
                )

                self._l(f"打开注册页 {REGISTER_PAGE_URL}")
                page.goto(REGISTER_PAGE_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(2500)

                self._fill_register_form(page, username=username, password=password, email=email)

                # Turnstile 在真实 Chrome 下自动通过（invisible/managed 模式）
                self._l("等待 Turnstile 自动通过...")
                deadline = time.time() + self._timeout
                ts_ok = self._wait_turnstile_ready(page, min(deadline, time.time() + 60))
                if ts_ok:
                    self._l("Turnstile 已通过")
                else:
                    self._l("Turnstile 未就绪（可能 invisible 自动过或被拒），继续尝试发送验证码")

                # 点 Get Verify Code 发送邮箱验证码
                self._l("点击 Get Verify Code 发送验证码")
                self._click_button(page, ["Get Verify Code", "获取验证码", "Send Code"])
                page.wait_for_timeout(3000)

                # 等邮箱 OTP
                self._l("等待邮箱 OTP...")
                otp = str(self._otp_callback() or "").strip()
                if not otp:
                    raise RuntimeError("MixRoute 未收到邮箱验证码")
                self._l(f"收到 OTP: {otp}")
                self._set_input(page, ["input[placeholder='Enter Email Verification Code']", "input[name='verification_code']"], otp)
                page.wait_for_timeout(400)

                # 点 Sign Up 提交注册
                self._l("点击 Sign Up 提交注册")
                self._click_button(page, ["Sign Up", "注册"])
                page.wait_for_timeout(5000)

                # new-api 注册成功后跳转 /login（不直接落地 dashboard），用 username+password 登录
                cur_url = page.url or ""
                if "/login" not in cur_url and "/dashboard" not in cur_url:
                    # 等跳转
                    self._wait_for_redirect(page, time.time() + 20, "/login") or self._wait_for_redirect(page, time.time() + 10, "/dashboard")

                cur_url = page.url or ""
                if "/login" in cur_url and "/dashboard" not in cur_url:
                    self._l("注册成功，跳转登录页，用 username+password 登录")
                    # 填登录表单
                    self._set_input(page, ["input[placeholder='Enter your username or email']", "input[name='username']"], username)
                    page.wait_for_timeout(300)
                    self._set_input(page, ["input[type='password']", "input[name='password']"], password)
                    page.wait_for_timeout(300)
                    # 登录也需要 Turnstile
                    self._wait_turnstile_ready(page, time.time() + 30)
                    self._click_button(page, ["Log In", "登录"])
                    page.wait_for_timeout(5000)

                # 等落地 dashboard
                if not self._wait_for_redirect(page, time.time() + 30, "/dashboard"):
                    self._l(f"未落地 dashboard，当前 URL: {page.url}")
                else:
                    self._l("已落地 dashboard")

                session_info = self._read_session(context)
                if not session_info.get("token"):
                    raise RuntimeError(f"MixRoute 浏览器注册完成但未读到 localStorage token，URL: {page.url}")
                self._l("会话 token 已读取")
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

        # 协议拿 key：用浏览器拿到的 token 走 POST /api/token/
        token = str(session_info.get("token") or "").strip()
        user = dict(session_info.get("user") or {})
        user_id = str(user.get("id") or "")
        session = _build_session(self._proxy)
        # 同步浏览器 cookie 到协议 session（部分部署用 cookie 鉴权而非 header）
        try:
            with sync_playwright() as pw:
                browser2 = pw.chromium.connect_over_cdp(launch_meta["cdp_url"] if launch_meta.get("cdp_url") else "", timeout=10_000)
                ctx2 = browser2.contexts[0] if browser2.contexts else browser2.new_context()
                for c in ctx2.cookies():
                    if "mixroute.ai" in c.get("domain", ""):
                        session.cookies.set(c["name"], c["value"], domain=c["domain"])
                browser2.close()
        except Exception:
            pass
        apply_session_auth(session, token, user_id)

        # 补全 user 信息
        try:
            self_info = get_user_self_http(session, token)
            from platforms.mixroute.core import _response_success
            if _response_success(self_info):
                from platforms.mixroute.core import _extract_user
                self_user = _extract_user(self_info.get("data"))
                if self_user:
                    user = self_user
                    user_id = str(user.get("id") or user_id)
                    apply_session_auth(session, token, user_id)
        except Exception:
            pass

        key_result = create_api_key_http(session, token=token, key_name=self._key_name, log_fn=self._log)
        api_key = _normalize_api_key(key_result.get("api_key") or "")
        if not api_key:
            raise RuntimeError("MixRoute 浏览器注册后创建 API Key 失败：未返回明文 key")
        api_verification = verify_api_key_http(api_key, proxy=self._proxy)
        session_cookies = _session_cookie_dict(session)

        return {
            "email": str(user.get("email") or email),
            "password": password,
            "username": username,
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
            "auth_method": "email",
            "api_key_source": "browser_protocol",
            "site_url": SITE_URL,
            "dashboard_url": DASHBOARD_URL,
            "api_base": API_BASE,
        }
