"""AIHubMix 浏览器注册 worker（浏览器 sign_up + 协议/浏览器拿 key）。

链路：
1. 浏览器（真实 Chrome + CDP）加载 /sign-up，填邮箱+密码，点 Continue。
   Clerk 组件自动拿 Turnstile token 发 sign_up + prepare_verification（给邮箱发 OTP）。
   aihubmix 的 firstName/lastName 在 Clerk environment 里是 "off"，不填。
2. 等浏览器出现 OTP 输入框，用 otp_callback（IMAP 收码，扫 INBOX+Junk）拿验证码，
   填进浏览器点验证，等跳转进 console landing。
3. 提取浏览器 Clerk session cookie（__client/__session）+ access_token。
4. 协议拿 key：先尝试 _KeyFetchWorker（动态提取 createApiKey action ID + Server Action），
   失败回退 _fetch_key_via_browser（浏览器 DOM 点 Create Key 读 sk-）。

为何需要浏览器：Clerk smart captcha 的 bot detection 在 Playwright headless 跑不过
（Turnstile widget 不渲染，Continue 按钮永久 disabled），需真实 Chrome（带
--disable-blink-features=AutomationControlled）让 Clerk 内部 bot detection 通过。
"""

from __future__ import annotations

import random
import re
import shutil
import socket
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
from platforms.aihubmix.core import (
    API_KEY_PATTERN,
    DEFAULT_USER_AGENT,
    DASHBOARD_URL,
    KEYS_DASHBOARD_URL,
    SIGN_UP_URL,
)
from platforms.aihubmix.protocol_register import (
    _KeyFetchWorker,
    _extract_api_key,
)


SIGN_UP_PAGE_URL = "https://console.aihubmix.com/sign-up"

DEFAULT_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


class AIHubMixBrowserRegistrar:
    """浏览器 sign_up + 协议/浏览器拿 key 的混合注册 worker。"""

    _cleanup_lock = threading.Lock()
    _cleanup_inflight: set[str] = set()

    def __init__(
        self,
        *,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        api_key_name: str = "auto-register",
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
        self._api_key_name = api_key_name
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
        self._log(f"[aihubmix:browser] {msg}")

    # --- Chrome 启动/清理 ---

    def _resolve_chrome_path(self) -> str:
        if self._chrome_path and Path(self._chrome_path).exists():
            return self._chrome_path
        for candidate in DEFAULT_CHROME_PATHS:
            if Path(candidate).exists():
                return candidate
        raise RuntimeError("AIHubMix 浏览器注册需要 Chrome，未找到 chrome 可执行文件")

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
        raise RuntimeError(f"AIHubMix Chrome CDP 未就绪 port={port}")

    def _prepare_chrome(self) -> dict[str, Any]:
        if self._cdp_url:
            return {"cdp_url": self._cdp_url, "process": None, "profile_dir": None}
        port = self._find_free_port()
        profile_dir = Path(self._profile_root_dir).resolve() / f"aihubmix-{int(time.time()*1000)}-{random.randint(1000,9999)}"
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
            # 注意：不传 --disable-blink-features=AutomationControlled。实测传了这个 flag
            # 反而让 Clerk Turnstile 报 "The CAPTCHA failed to load"（Turnstile iframe 不注入）。
            # Cloudflare Turnstile 可能把这个反自动化 flag 当作自动化信号。webdriver 由
            # add_init_script 在 JS 层 patch 掉即可，不需要 Chrome flag。
            # 注意：不要 --disable-features=Translate,TranslateUI，同样会导致 Turnstile 不加载。
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

    # --- 浏览器注册流程 ---

    def _fill_signup_form(self, page: Page, email: str, password: str) -> None:
        self._l(f"填写注册表单: {email}")
        # aihubmix 的 firstName/lastName 在 Clerk environment 里是 "off"，
        # Clerk 不渲染这俩字段，不填（填了会因字段不存在而报错）。
        page.locator("#emailAddress-field").first.fill(email)
        page.wait_for_timeout(400)
        page.locator("#password-field").first.fill(password)
        page.wait_for_timeout(400)

    def _click_continue(self, page: Page) -> None:
        page.locator('button:has-text("Continue")').first.click(timeout=10_000)

    def _read_turnstile_token(self, page: Page) -> str:
        """读 cf-turnstile-response / Clerk captcha token 隐藏 input。"""
        try:
            return str(page.evaluate(
                """() => {
                  const sel = "input[name='cf-turnstile-response'], textarea[name='cf-turnstile-response'],"
                    + " input[id^='cf-chl-widget'][name$='_response'],"
                    + " input[name='captcha'], textarea[name='captcha']";
                  const f = document.querySelector(sel);
                  return f ? (f.value || '') : '';
                }"""
            ) or "")
        except Exception:
            return ""

    def _click_turnstile_until_token(self, page: Page, deadline: float) -> str:
        """点击 Clerk 嵌入的 Cloudflare Turnstile 可见 checkbox，直到拿到 token。

        真实 Chrome（带 --disable-blink-features=AutomationControlled）下 Clerk 会渲染
        smart/managed widget 的可见 checkbox iframe；需要模拟鼠标移动 + 点击触发验证。
        """
        logged_diag = False
        while time.time() < deadline:
            self._poll_cancel()
            # 先看 token 是否已就位（invisible widget 可能自动通过）。
            token = self._read_turnstile_token(page)
            if token:
                return token
            # 诊断：dump frames + turnstile DOM（只打一次，看清真实结构）。
            if not logged_diag:
                try:
                    frames_info = [
                        {"url": (f.url or "")[:120], "name": (f.name or "")[:40]}
                        for f in page.frames
                    ]
                    ts_dom = str(page.evaluate(
                        """() => {
                          const ifs = Array.from(document.querySelectorAll('iframe')).map(f => ({
                            src: (f.src||'').slice(0,140), w: f.offsetWidth, h: f.offsetHeight,
                            visible: f.offsetParent !== null, parent: f.parentElement ? f.parentElement.className : ''
                          }));
                          const cf = Array.from(document.querySelectorAll('[id*="cf-chl"],[id*="turnstile"],.cf-turnstile,[data-sitekey]')).map(e => ({
                            tag: e.tagName, id: e.id, rect: (() => { const r = e.getBoundingClientRect(); return [Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]; })()
                          }));
                          return JSON.stringify({iframes: ifs, cfEls: cf, webdriver: navigator.webdriver});
                        }"""
                    ) or "")
                    self._l(f"frames={frames_info}")
                    self._l(f"turnstile_dom={ts_dom[:600]}")
                except Exception as exc:
                    self._l(f"diag err: {exc}")
                logged_diag = True
            # 找 Turnstile checkbox iframe：Clerk 嵌入的 iframe src 含 challenges.cloudflare.com
            try:
                frames = page.frames
                for frame in frames:
                    if "challenges.cloudflare.com" not in (frame.url or ""):
                        continue
                    try:
                        box = frame.frame_element().bounding_box()
                    except Exception:
                        box = None
                    if not box:
                        continue
                    x = box["x"] + min(28, max(18, box["width"] * 0.12))
                    y = box["y"] + min(28, max(20, box["height"] * 0.5))
                    page.mouse.move(x - 30, y - 10, steps=14)
                    page.wait_for_timeout(150)
                    page.mouse.click(x, y, delay=140)
                    page.wait_for_timeout(3500)
                    token = self._read_turnstile_token(page)
                    if token:
                        self._l("Turnstile token 已获取")
                        return token
                    break  # 点了一次，等下一轮再读
            except Exception:
                pass
            time.sleep(1.5)
        return ""

    def _wait_for_otp_input(self, page: Page, deadline: float) -> bool:
        """等 Clerk 出现 OTP 输入框（6 位数字输入）。

        必须有真实的 OTP input 元素才算进入 OTP 阶段，不能只靠文本匹配——
        Clerk 的 CAPTCHA 错误消息里也可能含 "verification code" 字样，导致误判。
        """
        while time.time() < deadline:
            self._poll_cancel()
            try:
                selectors = (
                    'input[autocomplete="one-time-code"]',
                    'input[inputmode="numeric"]',
                    'input[name*="code"]',
                    'input[data-input-otp="true"]',
                )
                for sel in selectors:
                    if page.locator(sel).count() > 0:
                        return True
                body = (page.locator("body").inner_text() or "").lower()
                # Clerk 在邮箱已被注册时报错，立即失败，避免傻等满 timeout。
                if "already in use" in body or "already registered" in body or "is in use" in body:
                    raise RuntimeError("AIHubMix 邮箱已被占用（Clerk: already in use）")
                # CAPTCHA 加载失败：立即失败，不要傻等 OTP（Clerk 没提交 sign_up）
                if "captcha failed to load" in body or "captcha" in body and "failed" in body:
                    raise RuntimeError("AIHubMix Clerk CAPTCHA 加载失败（Turnstile 未注入），真实浏览器指纹被识别")
                # 临时邮箱被拒：立即失败
                if "temporary email" in body:
                    raise RuntimeError("AIHubMix 拒绝临时邮箱域名，需用真实邮箱（Hotmail/Gmail/Outlook）")
            except RuntimeError:
                raise
            except Exception:
                pass
            time.sleep(1)
        return False

    def _fill_otp(self, page: Page, code: str) -> None:
        clean = re.sub(r"\D", "", code or "")
        if not clean:
            raise RuntimeError("AIHubMix OTP 为空")
        selectors = (
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[name*="code"]',
            'input[data-input-otp="true"]',
        )
        for sel in selectors:
            try:
                locator = page.locator(sel)
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
            raise RuntimeError(f"AIHubMix OTP 填写失败: {exc}") from exc

    def _wait_for_console_landing(self, page: Page, deadline: float) -> bool:
        """等跳转进 console 真实落地页（非 sign-up/sign-in 中转页）。"""
        while time.time() < deadline:
            self._poll_cancel()
            url = page.url or ""
            if "console.aihubmix.com" in url and "/sign-up" not in url and "/sign-in" not in url:
                return True
            time.sleep(1)
        return False

    def _extract_auth_state(self, context: BrowserContext) -> dict[str, str]:
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        client_cookie = cookies.get("__client", "")
        session_cookie = cookies.get("__session", "")
        # access_token 从 __session JWT 或 Clerk client 取；session_cookie 本身就是 Clerk session JWT
        return {
            "client_cookie": client_cookie,
            "session_cookie": session_cookie,
            "access_token": session_cookie,  # Clerk __session cookie 即 session JWT，可作 Bearer
        }

    def run(self, *, email: str, password: str) -> dict[str, Any]:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("AIHubMix 浏览器注册需要 Playwright：pip install playwright && playwright install chromium")
        if not self._otp_callback:
            raise RuntimeError("AIHubMix 浏览器注册需要 otp_callback（IMAP 收码）")

        # 仅当外部传了 cdp_url（复用已登录 Chrome profile）时才启动系统 Chrome；
        # 否则用 Playwright 内置 Chromium launch（Turnstile 渲染更可靠，见 run 内逻辑）。
        launch_meta: dict[str, Any] = {}
        if self._cdp_url:
            launch_meta = self._prepare_chrome()
        browser: Browser | None = None
        page: Page | None = None
        auth_state: dict[str, str] = {}
        own_launch = False  # 用 pw.chromium.launch() 而非 connect_over_cdp 时为 True
        try:
            with sync_playwright() as pw:
                # 优先用 Playwright 内置 Chromium（pw.chromium.launch）——实测它的指纹能让
                # Clerk Turnstile 正常渲染（MCP browser 验证过）。系统 Chrome + connect_over_cdp
                # 反而让 Turnstile 报 "CAPTCHA failed to load"（iframe 不注入）。
                # 仅当外部传了 cdp_url（复用已登录 Chrome profile）时才走 connect_over_cdp。
                if launch_meta.get("cdp_url") and not launch_meta.get("process"):
                    # 外部 cdp_url（复用模式）
                    browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                else:
                    # Playwright 内置 Chromium launch（最可靠，Turnstile 能渲染）
                    self._l("用 Playwright Chromium launch 启动浏览器（Turnstile 渲染最可靠）")
                    launch_args = [
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--lang=en-US",
                        # 关键：--disable-blink-features=AutomationControlled 让 navigator.webdriver=false。
                        # Cloudflare Turnstile smart captcha 检查 webdriver：true 时拒绝加载 iframe
                        # （报 "CAPTCHA failed to load"），false 时 invisible captcha 自动通过。
                        # 实测 MCP browser（webdriver=false）能过 Turnstile，Playwright launch 默认
                        # （webdriver=true）不能。此 flag 是 Playwright launch 过 Turnstile 的必要条件。
                        "--disable-blink-features=AutomationControlled",
                    ]
                    if self._proxy:
                        from core.proxy_utils import build_playwright_proxy_settings
                        launch_opts = {"headless": self._headless, "args": launch_args,
                                       "proxy": build_playwright_proxy_settings(self._proxy)}
                    else:
                        launch_opts = {"headless": self._headless, "args": launch_args}
                    browser = pw.chromium.launch(**launch_opts)
                    own_launch = True
                    # 注意：不设自定义 user_agent。实测设 Chrome/147 但 Playwright Chromium 实际是
                    # Chrome/145，UA 与真实版本不一致会被 Cloudflare Turnstile 拒绝（captcha_invalid）。
                    # 用默认 UA（与浏览器真实版本匹配），Turnstile 才认。
                    context = browser.new_context(
                        viewport={"width": 1366, "height": 800},
                        locale="en-US",
                    )
                page = context.new_page()
                # 注意：不要 add_init_script patch navigator.webdriver。实测 patch webdriver
                # 会让 Cloudflare Turnstile 报 "CAPTCHA failed to load"（Turnstile 检测到属性
                # 描述符被篡改，当作自动化信号）。MCP browser（不 patch webdriver）能正常加载
                # Turnstile。Cloudflare Turnstile 对 webdriver=true 有容忍度，但对篡改行为零容忍。
                # context.add_init_script(
                #     "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                # )

                self._l(f"打开注册页 {SIGN_UP_PAGE_URL}")
                # 监听控制台消息 + 网络响应，诊断 Turnstile/Clerk 错误
                console_errors: list[str] = []
                def _on_console(msg):
                    try:
                        if msg.type in ("error", "warning"):
                            console_errors.append(f"[{msg.type}] {msg.text[:200]}")
                    except Exception:
                        pass
                page.on("console", _on_console)
                # 用 networkidle（而非 domcontentloaded）等 Clerk SDK + Turnstile 完全初始化。
                # domcontentloaded 时 Clerk JS 还在加载，captcha div 为空，Continue 点击会失败。
                page.goto(SIGN_UP_PAGE_URL, wait_until="networkidle", timeout=60_000)
                # 额外等 Clerk captcha widget 注入（Turnstile invisible 模式异步初始化）
                page.wait_for_timeout(3000)

                # 诊断：dump 浏览器指纹 + Clerk captcha div 状态 + 控制台错误
                try:
                    diag = page.evaluate(
                        """() => ({
                          webdriver: navigator.webdriver,
                          languages: navigator.languages,
                          plugins: navigator.plugins.length,
                          userAgent: navigator.userAgent.slice(0, 80),
                          clerkCaptcha: (() => {
                            const el = document.getElementById('clerk-captcha');
                            return el ? {children: el.children.length, iframes: el.querySelectorAll('iframe').length, visible: el.offsetParent !== null, html: el.innerHTML.slice(0,200)} : null;
                          })(),
                          turnstileIframes: document.querySelectorAll('iframe[src*="challenges.cloudflare"]').length,
                          continueDisabled: (() => {
                            const btn = Array.from(document.querySelectorAll('button')).find(b => /Continue/i.test(b.textContent||''));
                            return btn ? {disabled: btn.disabled, ariaDisabled: btn.getAttribute('aria-disabled')} : null;
                          })(),
                          captchaErrors: Array.from(document.querySelectorAll('[class*="error"], [class*="Error"], [role="alert"]')).map(e => (e.textContent||'').trim().slice(0,150)).filter(Boolean),
                        })"""
                    )
                    self._l(f"指纹/captcha 诊断: {diag}")
                    if console_errors:
                        self._l(f"控制台错误/警告: {console_errors[:8]}")
                except Exception as exc:
                    self._l(f"诊断 evaluate 失败: {exc}")

                self._fill_signup_form(page, email, password)
                self._click_continue(page)

                deadline = time.time() + self._timeout
                # aihubmix Clerk 用 invisible/managed Turnstile（无可见 checkbox）。
                # 点击 Continue 后 Clerk 内部提交 sign_up，Turnstile invisible widget
                # 自动求解 token，Clerk 验证 token 后发 OTP 邮件并跳转 #/verify-email-address。
                # 不需要点击 Turnstile checkbox（invisible 模式没有 checkbox）。
                self._l("Continue 已点击，等 Clerk invisible Turnstile 自动求解 + 跳转 verify-email...")
                # 短暂等 Turnstile token 就位（若 invisible widget 需要点时间）
                ts_deadline = min(deadline, time.time() + 30)
                token = self._click_turnstile_until_token(page, ts_deadline)
                if token:
                    self._l(f"Turnstile token 长度={len(token)}，等 Clerk 提交 sign_up")
                else:
                    self._l("未拿到 Turnstile token（invisible 自动通过或被拒），继续等 OTP 输入框")

                # 等 OTP 输入框（Clerk invisible captcha 通过后会发 sign_up + 发 OTP + 跳转）
                self._l("等待 OTP 输入框出现...")
                if not self._wait_for_otp_input(page, deadline):
                    # 诊断：dump 当前页面错误信息 + captcha 状态，判断 Turnstile 未通过还是邮箱被拒
                    try:
                        diag2 = page.evaluate(
                            """() => {
                              const body = document.body.innerText.slice(0, 800);
                              const errors = Array.from(document.querySelectorAll('[class*="error"], [class*="Error"], [role="alert"]'))
                                .map(e => (e.textContent||'').trim().slice(0,120)).filter(Boolean);
                              const captcha = document.getElementById('clerk-captcha');
                              const captchaInfo = captcha ? {children: captcha.children.length, iframes: captcha.querySelectorAll('iframe').length} : null;
                              const turnstile = document.querySelectorAll('iframe[src*="challenges.cloudflare"]').length;
                              return {url: location.href, errors, captchaInfo, turnstileIframes: turnstile, bodySnippet: body.slice(-400)};
                            }"""
                        )
                        self._l(f"OTP 未出现诊断: {diag2}")
                    except Exception as exc:
                        self._l(f"诊断 evaluate 失败: {exc}")
                    raise RuntimeError("AIHubMix 注册后未出现 OTP 输入框（可能 Turnstile 未通过或邮箱被拒）")

                self._l("等待邮箱 OTP（IMAP 扫 INBOX+Junk）...")
                otp = str(self._otp_callback() or "").strip()
                if not otp:
                    raise RuntimeError("未收到 AIHubMix 邮箱验证码")
                self._l(f"收到 OTP: {otp}")
                self._fill_otp(page, otp)
                # OTP 填完后 Clerk 可能自动验证，或需点 Continue/Verify
                try:
                    page.wait_for_timeout(800)
                    for sel in ('button:has-text("Continue")', 'button:has-text("Verify")', 'button[type="submit"]'):
                        loc = page.locator(sel).first
                        if loc.count() > 0 and not loc.is_disabled():
                            loc.click(timeout=4000)
                            break
                except Exception:
                    pass

                self._l("等待跳转 console landing...")
                if not self._wait_for_console_landing(page, deadline):
                    raise RuntimeError(f"AIHubMix OTP 验证后未进 console landing，当前 URL: {page.url}")

                auth_state = self._extract_auth_state(context)
                if not auth_state["client_cookie"]:
                    raise RuntimeError("AIHubMix 浏览器注册完成但未拿到 __client cookie")
                self._l("Clerk 会话提取成功")
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
            # 仅当用了 _prepare_chrome 启动的系统 Chrome（有 process）时才 teardown；
            # own_launch（Playwright launch）的浏览器由 browser.close() 自动清理。
            if launch_meta.get("process"):
                self._teardown_chrome(launch_meta)

        # 协议拿 key：先试 _KeyFetchWorker（动态提取 action ID），失败回退浏览器 DOM。
        api_key = ""
        key_create: dict[str, Any] = {}
        key_fetch_error = ""
        try:
            key_worker = _KeyFetchWorker(proxy=self._proxy, log_fn=self._log)
            key_create = key_worker.fetch(auth_state, name=self._api_key_name)
            api_key = str(key_create.get("api_key") or "").strip()
        except Exception as exc:
            key_fetch_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._l(f"协议拿 key 失败，回退浏览器 DOM: {key_fetch_error}")

        if not api_key:
            # 浏览器 DOM 兜底：重新连 CDP 拿页面，导航到 /token 点 Create Key 读 sk-
            self._l("浏览器 DOM 兜底拿 key...")
            api_key = _fetch_key_via_browser(launch_meta, self._proxy, self._api_key_name, self._log, self._cancel_token)
            if not api_key:
                raise RuntimeError(
                    f"AIHubMix 协议+浏览器 DOM 均未拿到 key（协议错误: {key_fetch_error}）"
                )

        # 协议验证 key + 拉 models
        from platforms.aihubmix.core import AIHubMixClient
        client = AIHubMixClient(proxy=self._proxy, log_fn=self._log)
        verification_ok = client.verify_api_key(api_key)
        try:
            models = client.list_models_raw(api_key)
        except Exception:
            models = {}

        from platforms.aihubmix.protocol_register import _utcnow_iso
        return {
            "email": email,
            "password": password,
            "api_key": api_key,
            "api_key_name": self._api_key_name,
            "api_key_source": "browser_dom" if not key_create.get("ok") else "protocol",
            "key_create_result": key_create,
            "api_verification": {"ok": verification_ok},
            "models": models if isinstance(models, dict) else {},
            "access_token": auth_state.get("access_token", ""),
            "client_cookie": auth_state.get("client_cookie", ""),
            "session_cookie": auth_state.get("session_cookie", ""),
            "site_url": "https://aihubmix.com/",
            "dashboard_url": DASHBOARD_URL,
            "api_base": "https://aihubmix.com/v1",
            "checked_at": _utcnow_iso(),
        }


def _fetch_key_via_browser(
    launch_meta: dict[str, Any],
    proxy: str | None,
    api_key_name: str,
    log_fn: Callable[[str], None],
    cancel_token: CancelToken | None = None,
) -> str:
    """浏览器 DOM 兜底拿 key：连 CDP，导航到 /token，点 Create/Add Key，从 DOM/网络读 sk-。

    用于 _KeyFetchWorker 协议拿 key 失败时（aihubmix console Next.js 部署结构未知，
    动态提取 action ID 可能失败）。此函数会复用 launch_meta 里的 cdp_url（如已启动 Chrome），
    或在 cdp_url 为空时自己启动一个新 Chrome。
    """
    if not PLAYWRIGHT_AVAILABLE:
        return ""
    log_fn("[aihubmix:browser-key] 浏览器 DOM 兜底拿 key")
    # 复用已有 launch_meta（cdp_url）；若 cdp_url 为空（外部直接调），启动新 Chrome。
    own_chrome = False
    cdp_url = launch_meta.get("cdp_url") if launch_meta else ""
    if not cdp_url:
        registrar = AIHubMixBrowserRegistrar(
            proxy=proxy, otp_callback=lambda: "", api_key_name=api_key_name,
            headless=False, log_fn=log_fn, cancel_token=cancel_token,
        )
        launch_meta = registrar._prepare_chrome()
        cdp_url = launch_meta["cdp_url"]
        own_chrome = True
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=30_000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            # 监听网络响应，捕获创建 key 的 API 响应里的 sk-
            captured_keys: list[str] = []
            def _on_response(response):
                try:
                    url = response.url or ""
                    if "token" not in url.lower() and "key" not in url.lower():
                        return
                    body = response.text() or ""
                    found = _extract_api_key(body)
                    if found:
                        captured_keys.append(found)
                except Exception:
                    pass
            page.on("response", _on_response)
            try:
                page.goto(KEYS_DASHBOARD_URL, wait_until="domcontentloaded", timeout=60_000)
                time.sleep(3)
                # 点 Create Key / Add Key / 新建密钥 按钮
                clicked = False
                for label in ("Create Key", "Add Key", "新建密钥", "创建密钥", "Create", "Add"):
                    try:
                        loc = page.locator(f'button:has-text("{label}")').first
                        if loc.count() > 0:
                            loc.click(timeout=5000)
                            clicked = True
                            log_fn(f"[aihubmix:browser-key] 点击 {label} 按钮")
                            break
                    except Exception:
                        continue
                if clicked:
                    time.sleep(3)
                    # 若弹出输入 key name 的 modal，填名字点确认。
                    # 关键：input/button 必须限定在 modal（[role=dialog]/.ant-modal）内——
                    # /token 页本身有同 placeholder="please enter the key name" 的搜索框，
                    # 用 .first 会选到搜索框，导致 key name 填不进 modal、submit 点了表单为空。
                    try:
                        name_input = page.locator(
                            '[role="dialog"] input[placeholder*="name" i],'
                            ' .ant-modal input[placeholder*="name" i],'
                            ' [role="dialog"] input[name*="name" i],'
                            ' .ant-modal input[name*="name" i]'
                        ).first
                        if name_input.count() > 0:
                            name_input.fill(api_key_name)
                            page.wait_for_timeout(400)
                            for confirm in ("submit", "Create", "Confirm", "OK", "确定", "创建"):
                                loc = page.locator(
                                    f'[role="dialog"] button:has-text("{confirm}"),'
                                    f' .ant-modal button:has-text("{confirm}")'
                                ).first
                                if loc.count() > 0 and not loc.is_disabled():
                                    loc.click(timeout=5000)
                                    break
                            time.sleep(3)
                    except Exception:
                        pass
                # 从网络捕获读 sk-
                if captured_keys:
                    return captured_keys[0]
                # DOM 兜底：先扫 modal 对话框（key 创建后会弹 "Your new key:" 对话框），
                # 再扫全页面文本。aihubmix 的 key 显示在 [role=dialog] 里，全页面文本会被
                # dashboard 骨架稀释，dialog 优先更稳。
                try:
                    found = page.evaluate(
                        """() => {
                          const dialogs = document.querySelectorAll('[role="dialog"],.ant-modal,.ant-modal-content');
                          for (const d of dialogs) {
                            const m = (d.innerText || '').match(/sk-[A-Za-z0-9_-]{20,}/);
                            if (m) return m[0];
                          }
                          const m = (document.body.innerText || '').match(/sk-[A-Za-z0-9_-]{20,}/);
                          return m ? m[0] : '';
                        }"""
                    )
                    if found:
                        return str(found)
                except Exception:
                    pass
                # 再兜底：扫 body inner_text（含未渲染但 DOM 里有的文本）
                try:
                    body_text = page.locator("body").inner_text() or ""
                    found = _extract_api_key(body_text)
                    if found:
                        return found
                except Exception:
                    pass
            finally:
                try:
                    page.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
        return ""
    finally:
        if own_chrome:
            AIHubMixBrowserRegistrar(log_fn=log_fn)._teardown_chrome(launch_meta)
