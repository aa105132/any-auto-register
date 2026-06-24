"""EmberCloud 浏览器注册 worker（浏览器 sign_up + 协议拿 key）。

链路：
1. 浏览器（真实 Chrome + CDP）加载 /sign-in/create，填邮箱+密码，点 Continue。
   Clerk 组件自动拿 Turnstile token 发 sign_up + prepare_verification（给邮箱发 OTP）。
2. 等浏览器出现 OTP 输入框，用 otp_callback（IMAP 收码，扫 INBOX+Junk）拿验证码，
   填进浏览器点验证，等跳转进 /dashboard。
3. 提取浏览器 Clerk session cookie（__client/__session）+ access_token。
4. 协议拿 key：GET /dashboard 预热 credit → 动态提取 createApiKey Server Action ID
   → POST /dashboard/keys 创建 ek_live_ key。

为何不走纯协议：Clerk 的 sign_up 状态绑定在 frontend SDK 内存（非 cookie），协议层
attempt_verification 接不了手；且 Clerk Turnstile 在 embercloud 页面懒加载，CDP solver
拿不到 token。浏览器里 Clerk 自动处理 Turnstile + sign_up，是最可靠路径。
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
from platforms.embercloud.core import (
    DASHBOARD_URL,
    DEFAULT_USER_AGENT,
    KEYS_DASHBOARD_URL,
    SIGN_IN_URL,
)
from platforms.embercloud.protocol_mailbox import (
    DEFAULT_CREATE_API_KEY_ACTION_ID,
    DEFAULT_DEPLOYMENT_ID,
    EmberCloudProtocolMailboxWorker,
    _extract_create_api_key_action_id,
    _extract_deployment_id,
    _extract_keys_page_chunks,
    _solve_turnstile,
)


SIGN_UP_URL = "https://www.embercloud.ai/sign-in/create"

DEFAULT_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


class EmberCloudBrowserRegistrar:
    """浏览器 sign_up + 协议拿 key 的混合注册 worker。"""

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
        self._log(f"[embercloud:browser] {msg}")

    # --- Chrome 启动/清理（参考 Enter browser_register）---

    def _resolve_chrome_path(self) -> str:
        if self._chrome_path and Path(self._chrome_path).exists():
            return self._chrome_path
        for candidate in DEFAULT_CHROME_PATHS:
            if Path(candidate).exists():
                return candidate
        raise RuntimeError("EmberCloud 浏览器注册需要 Chrome，未找到 chrome 可执行文件")

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
        raise RuntimeError(f"EmberCloud Chrome CDP 未就绪 port={port}")

    def _prepare_chrome(self) -> dict[str, Any]:
        if self._cdp_url:
            return {"cdp_url": self._cdp_url, "process": None, "profile_dir": None}
        port = self._find_free_port()
        profile_dir = Path(self._profile_root_dir).resolve() / f"embercloud-{int(time.time()*1000)}-{random.randint(1000,9999)}"
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
            # 反自动化检测：隐藏 navigator.webdriver，否则 Cloudflare Turnstile 拒绝下发 token。
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

    # --- 浏览器注册流程 ---

    def _fill_signup_form(self, page: Page, email: str, password: str) -> None:
        self._l(f"填写注册表单: {email}")
        # firstName/lastName 标注 Optional，但 Clerk 部分实例对空值会静默卡住，
        # 用邮箱 local-part 派生一个非空名字，避免 Clerk 客户端校验阻断提交。
        local = (email or "").split("@", 1)[0] or "user"
        try:
            page.locator("#firstName-field").first.fill(local[:20])
            page.wait_for_timeout(200)
            page.locator("#lastName-field").first.fill("Auto")
            page.wait_for_timeout(200)
        except Exception:
            pass  # 某些 Clerk 实例不渲染 firstName/lastName，忽略
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
        """等 Clerk 出现 OTP 输入框（6 位数字输入）。"""
        while time.time() < deadline:
            self._poll_cancel()
            try:
                # Clerk OTP 输入框形态：多个单字符 input 或一个数字 input
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
                # Clerk 在邮箱已被注册时报错（"That email address is already in use."），
                # 此时不会进 OTP 阶段；立即失败，避免傻等满 timeout。
                if "already in use" in body or "already registered" in body or "is in use" in body:
                    raise RuntimeError("EmberCloud 邮箱已被占用（Clerk: already in use）")
                if "verification code" in body or "enter the code" in body or "check your email" in body:
                    return True
            except RuntimeError:
                raise
            except Exception:
                pass
            time.sleep(1)
        return False

    def _fill_otp(self, page: Page, code: str) -> None:
        clean = re.sub(r"\D", "", code or "")
        if not clean:
            raise RuntimeError("EmberCloud OTP 为空")
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
            raise RuntimeError(f"EmberCloud OTP 填写失败: {exc}") from exc

    def _wait_for_dashboard(self, page: Page, deadline: float) -> bool:
        while time.time() < deadline:
            self._poll_cancel()
            url = page.url or ""
            if "/dashboard" in url and "/sign-in" not in url:
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
            raise RuntimeError("EmberCloud 浏览器注册需要 Playwright：pip install playwright && playwright install chromium")
        if not self._otp_callback:
            raise RuntimeError("EmberCloud 浏览器注册需要 otp_callback（IMAP 收码）")

        launch_meta = self._prepare_chrome()
        browser: Browser | None = None
        page: Page | None = None
        auth_state: dict[str, str] = {}
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                # 在任何脚本执行前 patch navigator.webdriver，规避 Cloudflare 自动化检测。
                context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                    "Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});"
                    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
                )

                self._l(f"打开注册页 {SIGN_UP_URL}")
                page.goto(SIGN_UP_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(2500)

                self._fill_signup_form(page, email, password)
                self._click_continue(page)

                deadline = time.time() + self._timeout
                # 真实 Chrome 下 Clerk 嵌入的 Turnstile 会渲染可见 checkbox，
                # 需要主动点击触发验证拿 token，Clerk 才会发 sign_up 请求。
                self._l("点击 Turnstile checkbox（直到拿到 token）...")
                ts_deadline = min(deadline, time.time() + 90)
                token = self._click_turnstile_until_token(page, ts_deadline)
                if token:
                    self._l(f"Turnstile token 长度={len(token)}，等 Clerk 提交 sign_up")
                else:
                    self._l("未拿到 Turnstile token（可能 invisible 自动通过或被拒），继续等 OTP")

                # 等 OTP 输入框（Clerk 拿到 Turnstile token 后发 sign_up + 发 OTP）
                self._l("等待 OTP 输入框出现...")
                if not self._wait_for_otp_input(page, deadline):
                    raise RuntimeError("EmberCloud 注册后未出现 OTP 输入框（可能 Turnstile 未通过或邮箱被拒）")

                self._l("等待邮箱 OTP（IMAP 扫 INBOX+Junk）...")
                otp = str(self._otp_callback() or "").strip()
                if not otp:
                    raise RuntimeError("未收到 EmberCloud 邮箱验证码")
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

                self._l("等待跳转 dashboard...")
                if not self._wait_for_dashboard(page, deadline):
                    raise RuntimeError(f"EmberCloud OTP 验证后未进 dashboard，当前 URL: {page.url}")

                auth_state = self._extract_auth_state(context)
                if not auth_state["client_cookie"]:
                    raise RuntimeError("EmberCloud 浏览器注册完成但未拿到 __client cookie")
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
            self._teardown_chrome(launch_meta)

        # 协议拿 key：credit 预热 + Server Action 创建
        key_worker = _KeyFetchWorker(proxy=self._proxy, log_fn=self._log)
        key_result = key_worker.fetch(auth_state, name=self._api_key_name)
        api_key = key_result.get("api_key") or ""
        if not api_key:
            raise RuntimeError("EmberCloud 协议拿 key 失败")

        # 协议验证 key + 拉 models
        from platforms.embercloud.core import EmberCloudClient
        client = EmberCloudClient(proxy=self._proxy, log_fn=self._log)
        verification_ok = client.verify_api_key(api_key)
        try:
            models = client.list_models_raw(api_key)
        except Exception:
            models = {}

        from platforms.embercloud.protocol_mailbox import _utcnow_iso
        return {
            "email": email,
            "password": password,
            "api_key": api_key,
            "api_key_name": self._api_key_name,
            "api_key_source": "protocol",
            "key_create_result": key_result,
            "api_verification": {"ok": verification_ok},
            "models": models if isinstance(models, dict) else {},
            "access_token": auth_state.get("access_token", ""),
            "client_cookie": auth_state.get("client_cookie", ""),
            "session_cookie": auth_state.get("session_cookie", ""),
            "site_url": "https://www.embercloud.ai/",
            "dashboard_url": DASHBOARD_URL,
            "api_base": "https://api.embercloud.ai",
            "native_api_base": "https://api.embercloud.ai",
            "checked_at": _utcnow_iso(),
        }


class _KeyFetchWorker:
    """协议拿 key 子流程：credit 预热 + 动态提取 action ID + Server Action 创建。

    从 EmberCloudProtocolMailboxWorker 抽取拿 key 部分，供浏览器注册完成后复用。
    """

    def __init__(self, *, proxy: str | None = None, log_fn: Callable[[str], None] | None = None):
        self._proxy = proxy
        self._log = log_fn or (lambda m: None)

    def _l(self, msg: str) -> None:
        self._log(f"[embercloud:key] {msg}")

    def _dashboard_session(self, auth_state: dict[str, str]) -> requests.Session:
        sess = requests.Session()
        sess.trust_env = False
        for name, value in (
            ("__client", auth_state.get("client_cookie") or ""),
            ("__session", auth_state.get("session_cookie") or auth_state.get("access_token") or ""),
            ("__client_uat", "1"),
        ):
            value = str(value or "").strip()
            if value:
                sess.cookies.set(name, value, domain=".embercloud.ai")
        if self._proxy:
            sess.proxies.update({"http": self._proxy, "https": self._proxy})
        return sess

    def fetch(self, auth_state: dict[str, str], *, name: str) -> dict[str, Any]:
        sess = self._dashboard_session(auth_state)

        # 1. 预热 dashboard 首页触发 credit 入账
        self._l("预热 dashboard 首页（触发 credit 入账）")
        headers = {"Accept": "text/html,application/xhtml+xml", "User-Agent": DEFAULT_USER_AGENT}
        resp = sess.get(DASHBOARD_URL, headers=headers, timeout=30, allow_redirects=True)
        if "/sign-in" in (resp.url or ""):
            raise RuntimeError("EmberCloud Clerk 会话未被 dashboard 接受（重定向回登录）")
        deployment_id = _extract_deployment_id(resp.text or "")
        self._l(f"deployment_id={deployment_id}")

        # 2. 动态提取 createApiKey action ID
        resp_keys = sess.get(KEYS_DASHBOARD_URL, headers=headers, timeout=30, allow_redirects=True)
        if "/sign-in" in (resp_keys.url or ""):
            raise RuntimeError("EmberCloud Clerk 会话未被 dashboard 接受")
        chunk_names = _extract_keys_page_chunks(resp_keys.text or "")
        action_id = DEFAULT_CREATE_API_KEY_ACTION_ID
        for chunk_name in chunk_names:
            chunk_url = f"https://www.embercloud.ai/_next/static/chunks/{chunk_name}.js?dpl={deployment_id}"
            try:
                chunk_resp = sess.get(chunk_url, headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=30)
            except Exception:
                continue
            if not chunk_resp.ok:
                continue
            aid = _extract_create_api_key_action_id(chunk_resp.text or "")
            if aid:
                action_id = aid
                self._l(f"从 chunk {chunk_name} 提取 createApiKey action ID: {aid}")
                break
        else:
            self._l(f"未动态提取到 action ID，用默认: {action_id}")

        # 3. POST Server Action 创建 key
        from platforms.embercloud.protocol_mailbox import KEYS_ROUTER_STATE_TREE
        sa_headers = {
            "Next-Action": action_id,
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "text/x-component",
            "Referer": KEYS_DASHBOARD_URL,
            "Next-Router-State-Tree": KEYS_ROUTER_STATE_TREE,
            "x-deployment-id": deployment_id,
            "User-Agent": DEFAULT_USER_AGENT,
        }
        body = __import__("json").dumps([name])
        r = sess.post(KEYS_DASHBOARD_URL, headers=sa_headers, data=body, timeout=30)
        text = r.text or ""
        if not r.ok:
            raise RuntimeError(f"EmberCloud 创建 key 失败 status={r.status_code}: {text[:400]}")
        m = re.search(r'"fullKey"\s*:\s*"(ek_live_[A-Za-z0-9_-]+)"', text)
        api_key = m.group(1) if m else ""
        if not api_key:
            raise RuntimeError(f"EmberCloud 创建 key 响应未含 ek_live_: {text[:400]}")
        self._l(f"协议创建 key 成功: {api_key[:14]}...{api_key[-4:]}")
        return {"ok": True, "api_key": api_key, "action_id": action_id, "deployment_id": deployment_id}
