"""Vellum 协议注册 worker（CDP 驱动 AuthKit 表单 + REST 闭环）。

协议执行器路径：用 CDP 连接 Chrome，通过 page.evaluate() 以 JS 驱动 WorkOS AuthKit
表单（Name/Email→Password→Email OTP→Phone OTP），注册成功后在同会话内走纯 REST
ensure-registration 签发 assistant_api_key（复用 session_api.extract_on_page）。

为什么不是纯 HTTP 协议：
- login.platform.vellum.ai 有 Cloudflare JS challenge（"Just a moment..."），纯 HTTP 会被 403
- AuthKit 表单用 Next.js Server Actions，POST body 是加密/签名的 payload，需 JS 运行时生成
- 所以协议执行器 = CDP 过 CF + JS 驱动表单 + REST 闭环，比完整浏览器自动化更轻量

配置：
- 邮箱：yyds_mail（yyds.mail.13140905.xyz 域名，已验证能过 WorkOS Radar）
- 手机：豪猪接码（项目 ID 从 phone_provider 配置读取）
- 验证码：yescaptcha（如遇 Turnstile）
- 代理：resin 轮换（vellum 专属池，内部多次换 session 找干净 IP）
"""
from __future__ import annotations

import random
import re
import string
import time
from typing import Any, Callable

from core.config_store import config_store
from core.resin_proxy import resolve_resin_proxy_config
from core.proxy_utils import build_playwright_proxy_settings

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36"
INVITE_BASE = "https://www.vellum.ai/r/"
DEFAULT_INVITE_CODE = "H5QJRV"
APP_URL = "https://www.vellum.ai/assistant"


def _resin_proxy(account: str) -> str | None:
    from core.registry import load_all
    load_all()
    if str(config_store.get("resin_enabled", "false")).strip().lower() not in {"1", "true", "yes", "on", "enabled"}:
        return None
    token = config_store.get("resin_token", "") or config_store.get("resin_password", "")
    resolved = resolve_resin_proxy_config(
        {
            "resin_enabled": "true",
            "resin_scheme": config_store.get("resin_scheme", ""),
            "resin_host": config_store.get("resin_host", ""),
            "resin_port": config_store.get("resin_port", ""),
            "resin_token": token,
            "resin_default_platform": config_store.get("resin_default_platform", "Default"),
            "resin_platform_map": config_store.get("resin_platform_map", ""),
        },
        task_platform="vellum",
        account=account,
        require_enabled=True,
    )
    return str(resolved.get("proxy_url") or "").strip() or None


def _probe_ip(purl: str) -> str:
    import requests

    s = requests.Session()
    s.trust_env = False
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = s.get(url, proxies={"http": purl, "https": purl}, timeout=12)
            t = (r.text or "").strip()
            if t and "." in t and len(t) < 64:
                return t
        except Exception:
            pass
    return ""


def _vellum_reachable(purl: str) -> bool:
    """Quick check: can this proxy reach vellum.ai AND login.platform.vellum.ai?"""
    import requests

    s = requests.Session()
    s.trust_env = False
    hdrs = {"User-Agent": UA}
    try:
        r1 = s.get("https://www.vellum.ai/account/signup", proxies={"http": purl, "https": purl},
                    timeout=12, allow_redirects=False, headers=hdrs)
        if r1.status_code not in (200, 301, 302, 303, 307):
            return False
    except Exception:
        return False
    try:
        r2 = s.get("https://login.platform.vellum.ai/", proxies={"http": purl, "https": purl},
                    timeout=12, allow_redirects=False, headers=hdrs)
        return r2.status_code in (200, 301, 302, 303, 307)
    except Exception:
        return False


class VellumProtocolRegister:
    """CDP 驱动的协议注册 worker。

    比 VellumBrowserRegister 更轻量：用 page.evaluate() 以 JS 填表+提交，
    而不是 Playwright locator 操作。过 CF challenge 仍需真实 Chrome。
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        phone_callback: Callable[[], str] | None = None,
        invite_code: str = DEFAULT_INVITE_CODE,
        country_code: str = "+1",
        nav_attempts: int = 8,
        phone_wait_attempts: int = 20,
        resin_rotate: bool = True,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.phone_callback = phone_callback
        self.invite_code = str(invite_code or DEFAULT_INVITE_CODE).strip() or DEFAULT_INVITE_CODE
        self.country_code = str(country_code or "+1").strip() or "+1"
        self.nav_attempts = max(1, int(nav_attempts))
        self.phone_wait_attempts = max(1, int(phone_wait_attempts))
        self.resin_rotate = resin_rotate
        self.log = log_fn
        self._slot = random.randint(1000, 9999)

    def _l(self, msg: str) -> None:
        self.log(f"[vellum] {msg}")

    def _next_proxy(self) -> tuple[str | None, str]:
        """获取一个干净的 resin 代理（同时通 vellum.ai + WorkOS AuthKit）。"""
        if self.proxy:
            self._l("使用外层显式代理，跳过内部 resin 轮换")
            return self.proxy, ""
        if not self.resin_rotate:
            return None, ""
        self._l("内部 resin 轮换探测（需同时通 vellum.ai + login.platform.vellum.ai）...")
        for _ in range(12):
            self._slot += 1
            purl = _resin_proxy(f"vp{self._slot}")
            if not purl:
                break
            if _vellum_reachable(purl):
                ip = _probe_ip(purl)
                if ip:
                    self._l(f"resin 命中干净 IP: {ip}")
                    return purl, ip
            time.sleep(0.8)
        return None, ""

    @staticmethod
    def _set_input_js(page, selectors: list[str], value: str) -> bool:
        """用 JS 设置 input 值（兼容 React 受控输入）。"""
        return page.evaluate(
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
        )

    @staticmethod
    def _click_js(page, selectors: list[str]) -> bool:
        return page.evaluate(
            r"""(sels) => {
                // Try CSS selectors first
                for (const sel of sels) {
                    try {
                        const el = document.querySelector(sel);
                        if (el && el.offsetParent !== null) { el.click(); return true; }
                    } catch(e) {}
                }
                // Fallback: match by button text content
                const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                for (const sel of sels) {
                    // Extract text from has-text('...') or just use the string
                    let text = sel;
                    const m = sel.match(/has-text\(['"](.+?)['"]\)/);
                    if (m) text = m[1];
                    if (text.includes(':') || text.includes('[')) continue; // skip CSS selectors
                    const btn = btns.find(b => (b.textContent || '').trim().includes(text));
                    if (btn && btn.offsetParent !== null) { btn.click(); return true; }
                }
                return false;
            }""",
            selectors,
        )

    def _state(self, page) -> dict:
        try:
            txt = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            txt = ""
        low = txt.lower()
        url = page.url or ""
        network_markers = (
            "err_connection_reset", "err_connection_refused", "err_timed_out",
            "连接已重置", "无法访问此网站", "the connection was reset",
        )
        return {
            "url": url, "text": txt,
            "cf": ("just a moment" in low) or ("__cf_chl" in url.lower()) or ("performing security" in low),
            "forbidden": ("does not have permission" in low) or ("error 1020" in low),
            "policy_denied": ("access blocked" in low) or ("policy_denied" in low),
            "email_taken": ("this email is not available" in low or "email is not available" in low or "already registered" in low),
            "signup_closed": ("error=signup_closed" in url.lower()) or ("signup_closed" in low),
            "network_error": any(m in low for m in network_markers) or url.startswith("chrome-error:"),
            "password_screen": "/sign-up/password" in url.lower(),
            "email_verify": "email-verification" in url.lower() or "enter the code" in low or "verification code" in low,
            "phone_step": ("radar-challenge" in url.lower()) or ("verify your phone" in low) or ("valid mobile phone" in low),
            "dashboard": ("vellum.ai/dashboard" in url.lower()) or ("/onboarding" in url.lower())
                         or ("app.vellum.ai" in url.lower()) or url.rstrip("/").endswith("vellum.ai"),
        }

    def _recover_network(self, page, max_reloads: int = 2) -> bool:
        for _ in range(max_reloads):
            st = self._state(page)
            if not st["network_error"]:
                return True
            self._l(f"网络错误页，刷新重试: {(st['text'] or '')[:80]}")
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception:
                return False
            page.wait_for_timeout(2000)
        return not self._state(page)["network_error"]

    def _open_email_entry(self, page) -> None:
        """vellum /account/signup 中转页需要先点 Continue with Email。"""
        try:
            url_l = (page.url or "").lower()
            if "/account/signup" not in url_l and "/account/login" not in url_l:
                return
            # 等待 SPA 渲染 Continue with Email 按钮（最多 20s）
            for wait in range(10):
                has_email = page.evaluate("() => document.querySelectorAll(\"input[type='email'],input[name='email']\").length > 0")
                if has_email:
                    return
                has_btn = page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button, a, [role="button"]'));
                    return btns.some(b => (b.textContent || '').includes('Continue with Email') && b.offsetParent !== null);
                }""")
                if has_btn:
                    self._l("点击 Continue with Email 入口")
                    self._click_js(page, ["button:has-text('Continue with Email')", "a:has-text('Continue with Email')"])
                    page.wait_for_timeout(5000)
                    return
                page.wait_for_timeout(2000)
        except Exception:
            pass

    def _fetch_signup_url_via_js(self, page) -> str:
        """在浏览器内用 fetch 发起 allauth provider redirect，获取 WorkOS AuthKit sign-up URL。

        这绕过了浏览器导航到 login.platform.vellum.ai 时的连接重置问题：
        allauth POST 返回 302 → WorkOS authorize → bootstrap → sign-up
        我们只跟到 sign-up URL，然后让浏览器直接导航到它（而不是跟随 vellum.ai 的重定向链）。
        """
        try:
            # 用 XMLHttpRequest 跟踪重定向（fetch redirect:manual 返回 opaqueredirect 无法读 Location）
            result = page.evaluate(
                """async () => {
                    // 1. Get CSRF from cookie
                    const cookies = document.cookie.split('; ');
                    let csrf = '';
                    for (const c of cookies) {
                        if (c.startsWith('__Secure-csrftoken=')) {
                            csrf = c.split('=').slice(1).join('=');
                            break;
                        }
                        if (c.startsWith('csrftoken=')) {
                            csrf = c.split('=').slice(1).join('=');
                        }
                    }
                    if (!csrf) return {error: 'no_csrf'};

                    // 2. POST allauth provider redirect — use XHR to read redirect Location
                    const formData = new URLSearchParams();
                    formData.set('provider', 'workos');
                    formData.set('callback_url', 'https://www.vellum.ai/account/provider/callback?authIntent=signup');
                    formData.set('process', 'login');
                    formData.set('intent', 'signup');
                    formData.set('csrfmiddlewaretoken', csrf);

                    // XHR with withCredentials to follow same-origin, but we need the Location header
                    // WorkOS redirect is cross-origin so we can't read it from fetch.
                    // Instead, use window.location to navigate — but that triggers browser navigation which may reset.
                    // Alternative: use the redirect URL pattern directly.
                    // The POST returns 302 to api.workos.com/user_management/authorize?...
                    // We can construct the WorkOS authorize URL ourselves since client_id is fixed.
                    return {csrf: csrf};
                }"""
            )
            csrf = result.get("csrf", "") if isinstance(result, dict) else ""
            if not csrf:
                self._l("JS fetch: 无 CSRF cookie")
                return ""

            # 用 Python curl_cffi 走协议链（不走浏览器导航）
            from curl_cffi import requests as creq

            s = creq.Session(impersonate="chrome131")
            s.proxies = {"http": self._current_proxy, "https": self._current_proxy} if hasattr(self, "_current_proxy") and self._current_proxy else {}
            # Copy cookies from browser context
            cookies = page.context.cookies()
            for c in cookies:
                if "vellum.ai" in c.get("domain", ""):
                    s.cookies.set(c["name"], c["value"], domain=c["domain"])

            # POST allauth provider redirect
            r = s.post(
                "https://www.vellum.ai/_allauth/browser/v1/auth/provider/redirect",
                data={
                    "provider": "workos",
                    "callback_url": "https://www.vellum.ai/account/provider/callback?authIntent=signup",
                    "process": "login",
                    "intent": "signup",
                    "csrfmiddlewaretoken": csrf,
                },
                headers={"X-CSRFToken": csrf, "Referer": "https://www.vellum.ai/account/signup"},
                timeout=15,
                allow_redirects=True,
            )
            final_url = str(r.url)
            if "sign-up" in final_url or "login.platform.vellum.ai" in final_url:
                return final_url
            # If it redirected to WorkOS authorize, follow manually
            if "api.workos.com" in final_url:
                r2 = s.get(final_url, timeout=15, allow_redirects=True)
                return str(r2.url)
            return final_url if "vellum" in final_url else ""
        except Exception as exc:
            self._l(f"JS fetch sign-up URL 失败: {type(exc).__name__}: {str(exc)[:120]}")
            return ""

    def _do_phone(self, page) -> None:
        if not self.phone_callback:
            raise RuntimeError("Vellum 手机验证需要 phone_callback（phone_provider）")
        phone = ""
        for i in range(self.phone_wait_attempts):
            try:
                phone = (self.phone_callback() or "").strip()
            except Exception as exc:
                if i % 5 == 0:
                    self._l(f"取号失败重试 {i+1}: {str(exc)[:80]}")
                phone = ""
            if phone:
                break
            time.sleep(15)
        if not phone:
            raise RuntimeError("Vellum 手机验证未取到号码（豪猪库存为空）")
        raw = re.sub(r"\D", "", phone)
        if self.country_code == "+1" and raw.startswith("1") and len(raw) == 11:
            national = raw[1:]
        else:
            national = raw
        self._l(f"phone {phone} -> {self.country_code} {national}")
        self._set_input_js(page, ["input[name='country_code']", "input[autocomplete='tel-country-code']"], self.country_code)
        page.wait_for_timeout(500)
        self._set_input_js(page, ["input[name='local_number']", "input[type='tel']", "input[autocomplete='tel-national']"], national)
        page.wait_for_timeout(700)
        self._click_js(page, ["button:has-text('Send verification code')", "button[type=submit]"])
        page.wait_for_timeout(5000)
        code = (self.phone_callback() or "").strip()
        if not code:
            raise RuntimeError("Vellum 未收到手机短信验证码")
        self._l(f"短信验证码 {code}")
        self._set_input_js(page, ["input[autocomplete='one-time-code']", "input[inputmode='numeric']", "input[name='code']", "input#code"], code)
        page.wait_for_timeout(800)
        self._click_js(page, ["button:has-text('Verify')", "button[type=submit]"])
        page.wait_for_timeout(8000)

    def _attempt(self, pw, email: str, password: str) -> tuple[str, dict]:
        from playwright.sync_api import sync_playwright

        proxy_url, ip = self._next_proxy()
        if self.resin_rotate and not proxy_url:
            return "resin_error", {}
        self._current_proxy = proxy_url  # 供 _fetch_signup_url_via_js 使用
        self._l(f"尝试 IP={ip or '-'}")
        launch_opts: dict[str, Any] = {
            "headless": self.headless,
            "timeout": 45000,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        }
        if proxy_url:
            launch_opts["proxy"] = build_playwright_proxy_settings(proxy_url)
        self._l("启动 Chromium...")
        try:
            browser = pw.chromium.launch(**launch_opts)
        except Exception as exc:
            self._l(f"启动 Chromium 失败: {type(exc).__name__}: {str(exc)[:120]}")
            return "browser_launch_error", {"error": str(exc)[:500]}
        try:
            ctx = browser.new_context(viewport={"width": 1366, "height": 800}, user_agent=UA, locale="en-US")
            ctx.set_default_timeout(45000)
            page = ctx.new_page()

            # 打开邀请页（带重试）
            invite_url = INVITE_BASE + self.invite_code
            for nav_try in range(3):
                try:
                    self._l(f"打开邀请页: {invite_url}")
                    page.goto(invite_url, wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(3000)
                    if "chrome-error" not in (page.url or ""):
                        break
                except Exception:
                    time.sleep(2)

            # 网络错误恢复
            if self._state(page)["network_error"]:
                if not self._recover_network(page):
                    return "resin_error", {}

            # 进入 Email 入口
            self._open_email_entry(page)
            page.wait_for_timeout(3000)

            # 检查是否到了 AuthKit 页面；如果连接重置（chrome-error），
            # 用 JS fetch 在浏览器内发起 allauth provider redirect（不导航），拿到 WorkOS URL 后直接导航
            cur = page.url or ""
            if "chrome-error" in cur or ("vellum.ai" in cur and "login.platform" not in cur):
                # 还在 vellum.ai 或连接重置：用协议方式获取 WorkOS sign-up URL
                self._l("浏览器导航到 AuthKit 失败，用 JS fetch 获取 WorkOS sign-up URL...")
                signup_url = self._fetch_signup_url_via_js(page)
                if signup_url:
                    self._l(f"获取到 AuthKit sign-up URL: {signup_url[:120]}")
                    for nav_retry in range(3):
                        try:
                            page.goto(signup_url, wait_until="domcontentloaded", timeout=45000)
                            page.wait_for_timeout(5000)
                            if "chrome-error" not in (page.url or ""):
                                break
                        except Exception:
                            time.sleep(3)
                else:
                    self._l("JS fetch 未获取到 sign-up URL")
                    if "chrome-error" in (page.url or ""):
                        return "resin_error", {}
            page.wait_for_timeout(3000)

            # 等 AuthKit signup 表单
            try:
                page.wait_for_selector("input[name='firstName'], input[name='first_name'], input[autocomplete='given-name'], input[type='email']", timeout=35000)
            except Exception:
                st = self._state(page)
                if st["cf"]:
                    return "cloudflare_challenge", st
                if st["forbidden"]:
                    return "forbidden", st
                if st["network_error"]:
                    return "resin_error", st
                return "signup_no_email_form", st

            # 填 First/Last/Email
            first_name = random.choice(["Aaron", "Brian", "Chloe", "Diane", "Ethan", "Grace"])
            last_name = random.choice(["Mitchell", "Parker", "Reed", "Sawyer", "Turner", "Walsh"])
            self._set_input_js(page, ["input[name='firstName']", "input[name='first_name']", "input[autocomplete='given-name']"], first_name)
            self._set_input_js(page, ["input[name='lastName']", "input[name='last_name']", "input[autocomplete='family-name']"], last_name)
            self._set_input_js(page, ["input[type='email']", "input[name='email']", "input#email"], email)
            self._l(f"填写: {first_name} {last_name} {email}")
            page.wait_for_timeout(800)

            # 提交 page 1
            self._click_js(page, ["button[type=submit]", "button:has-text('Continue')"])
            page.wait_for_timeout(8000)
            self._l(f"page1 后 URL: {(page.url or '')[:100]}")

            st = self._state(page)
            if st["email_taken"]:
                return "email_taken", st
            if st["signup_closed"]:
                return "signup_closed", st
            if st["cf"]:
                return "cloudflare_challenge", st
            if st["forbidden"]:
                return "forbidden", st
            if st["network_error"]:
                if not self._recover_network(page):
                    return "resin_error", st
                st = self._state(page)

            # 等 password 字段
            try:
                page.wait_for_selector("input[type='password'], input#password", timeout=25000)
            except Exception:
                self._l(f"未等到密码框，当前状态: {st.get('url','')[:80]}")

            pwd_count = page.evaluate("() => document.querySelectorAll(\"input[type='password']\").length")
            if pwd_count > 0:
                self._set_input_js(page, ["input[type='password']", "input#password"], password)
                self._l("填写密码")
                page.wait_for_timeout(800)
                self._click_js(page, ["button[type=submit]", "button:has-text('Continue')"])
                page.wait_for_timeout(10000)
                self._l(f"密码提交后 URL: {(page.url or '')[:100]}")

            st = self._state(page)
            if st["email_taken"]:
                return "email_taken", st
            if st["signup_closed"]:
                return "signup_closed", st
            if st["policy_denied"]:
                return "policy_denied", st
            if st["network_error"]:
                if not self._recover_network(page):
                    return "resin_error", st
                st = self._state(page)

            # Email 验证码
            if st["email_verify"]:
                self._l("进入邮箱验证码步骤")
                if not self.otp_callback:
                    raise RuntimeError("Vellum 邮箱验证需要 otp_callback（mailbox provider）")
                code = (self.otp_callback() or "").strip()
                if not code:
                    raise RuntimeError("Vellum 未收到邮箱验证码")
                self._l(f"邮箱验证码 {code}")
                self._set_input_js(page, ["input[autocomplete='one-time-code']", "input[inputmode='numeric']", "input[name='code']", "input#code"], code)
                page.wait_for_timeout(800)
                self._click_js(page, ["button[type=submit]", "button:has-text('Continue')", "button:has-text('Verify')"])
                page.wait_for_timeout(8000)
                st = self._state(page)
                self._l(f"邮箱验证后 URL: {st['url'][:100]}")

            # Phone 验证
            if st["phone_step"]:
                self._l("进入手机验证步骤")
                self._do_phone(page)
                st = self._state(page)
                self._l(f"手机验证后 URL: {st['url'][:100]} dashboard={st['dashboard']}")
                if st["email_taken"]:
                    return "email_taken", st
                if st["signup_closed"]:
                    return "signup_closed", st

            # 落地判断
            url_l = (page.url or "").lower()
            landed = ("www.vellum.ai/assistant" in url_l) or ("vellum.ai/dashboard" in url_l) or ("/onboarding" in url_l)
            if not landed:
                final_st = self._state(page)
                if final_st["email_taken"]:
                    return "email_taken", final_st
                if final_st["signup_closed"]:
                    return "signup_closed", final_st
                self._l(f"未落地 App（仍在 {url_l[:60]}），判 not_landed")
                return "not_landed", st

            # 落地：纯 REST 闭环签发 api_key
            self._l("已落地 App，走 REST 闭环签发...")
            from platforms.vellum.session_api import extract_on_page
            import uuid as _uuid

            result = extract_on_page(
                page,
                provision_key=True,
                client_installation_id=str(_uuid.uuid4()),
                runtime_assistant_id=str(_uuid.uuid4()),
                log=self._l,
            )
            if result.get("ok") and (result.get("assistant_api_key") or result.get("balance_usd")):
                result["email"] = email
                result["password"] = password
                result["phone_verified"] = True
                result["resin_ip"] = ip
                result["landed_url"] = page.url
                return "done", result
            self._l(f"REST 闭环未拿到 key: step={result.get('step')}")
            return "not_landed", {"url": page.url, **result}
        finally:
            try:
                browser.close()
            except Exception:
                pass

    def run(self, *, email: str, password: str) -> dict:
        from playwright.sync_api import sync_playwright

        retryable = {"cloudflare_challenge", "forbidden", "resin_error", "not_landed", "signup_no_email_form"}
        with sync_playwright() as pw:
            for attempt in range(self.nav_attempts):
                self._l(f"注册尝试 {attempt + 1}/{self.nav_attempts}")
                outcome, data = self._attempt(pw, email, password)
                self._l(f"结果: {outcome} url={data.get('url') or data.get('landed_url', '')}")
                if outcome == "done":
                    return data
                if outcome == "policy_denied":
                    raise RuntimeError("Vellum 被 WorkOS Radar 拦截 (policy_denied)：检查邮箱域名信誉")
                if outcome == "email_taken":
                    raise RuntimeError(
                        f"vellum_email_unavailable: 邮箱已被注册或不可用 email={email} url={data.get('url', '')}"
                    )
                if outcome == "signup_closed":
                    raise RuntimeError(
                        f"vellum_email_unavailable: Vellum 注册已关闭(signup_closed) email={email} url={data.get('url', '')}"
                    )
                if outcome not in retryable:
                    break
                time.sleep(3)
        raise RuntimeError(f"Vellum 注册未完成：用尽 {self.nav_attempts} 次尝试 last={data.get('url', '')}")
