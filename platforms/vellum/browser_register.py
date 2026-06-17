"""Vellum (Vellum Assistant) 浏览器注册 worker。

vellum.ai 注册走 WorkOS AuthKit：名/姓/邮箱 → 密码 → 邮箱验证码 → 手机验证(WorkOS Radar)。
要点（实测结论，详见会话记忆 vellum-registration-workos）：
- 邮箱必须用信誉较好的自有域名（cfworker / pangxie888.com）；yyds 临时邮域名会被 Radar policy_denied。
- 出口 IP 用 resin；多数 resin 出口会被 Cloudflare 挑战或被 vellum 边缘 403，需轮换 session 直到命中干净 IP。
- 注册页是 SPA，会跳转到 login.platform.vellum.ai，需等真正的表单字段出现再填。
- 手机号来自豪猪(美国号)，country_code 填 +1，local_number 填 10 位国内号。
"""
from __future__ import annotations

import random
import re
import string
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

INVITE_BASE = "https://www.vellum.ai/r/"
DEFAULT_INVITE_CODE = "H5QJRV"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")


def _resin_proxy(account: str) -> str | None:
    """从 config_store 解析一个 resin 代理 URL（带 session 后缀）。"""
    from core.config_store import config_store
    from core.resin_proxy import resolve_resin_proxy_config

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


def _probe_ip(proxy_url: str) -> str:
    import requests

    s = requests.Session(); s.trust_env = False
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = s.get(url, proxies={"http": proxy_url, "https": proxy_url}, timeout=12)
            t = (r.text or "").strip()
            if t and "." in t and len(t) < 64:
                return t
        except Exception:
            pass
    return ""


class VellumBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        otp_callback: Callable[[], str] | None = None,
        phone_callback: Callable[[], str] | None = None,
        invite_code: str = DEFAULT_INVITE_CODE,
        country_code: str = "+86",
        first_name: str = "",
        last_name: str = "",
        nav_attempts: int = 6,
        resin_rotate: bool = True,
        phone_wait_attempts: int = 20,
        log_fn: Callable[[str], None] = print,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.phone_callback = phone_callback
        self.invite_code = str(invite_code or DEFAULT_INVITE_CODE).strip() or DEFAULT_INVITE_CODE
        self.country_code = str(country_code or "+86").strip() or "+86"
        self.first_name = first_name or random.choice(["Aaron", "Brian", "Chloe", "Diane", "Ethan", "Grace"])
        self.last_name = last_name or random.choice(["Mitchell", "Parker", "Reed", "Sawyer", "Turner", "Walsh"])
        self.nav_attempts = max(1, int(nav_attempts))
        self.resin_rotate = resin_rotate
        self.phone_wait_attempts = max(1, int(phone_wait_attempts))
        self.log = log_fn
        self._slot = random.randint(1000, 9999)

    # ---- low-level helpers ----
    def _next_proxy(self) -> tuple[str | None, str]:
        if self.proxy:
            self.log("[vellum] 使用外层显式代理，跳过内部 resin 轮换和 IP 预探测")
            return self.proxy, ""
        if self.resin_rotate:
            self.log("[vellum] 未传入显式代理，开始内部 resin 轮换探测...")
            for _ in range(8):
                self._slot += 1
                purl = _resin_proxy(f"vr{self._slot}")
                if not purl:
                    break
                self.log(f"[vellum] resin 探测 slot={self._slot}")
                ip = _probe_ip(purl)
                if ip:
                    return purl, ip
                time.sleep(1.2)
            return None, ""
        return self.proxy, (_probe_ip(self.proxy) if self.proxy else "")

    def _playwright_proxy_url(self, proxy_url: str | None) -> str | None:
        """修正 Chromium/Playwright 不支持的代理形态。

        Chromium 不支持带账号密码的 socks5 代理认证。Webshare 的 p.webshare.io:80
        入口本身可按 HTTP 代理使用；如果外层代理池给成 socks5h://user:pass@p.webshare.io:80，
        这里转换为 http://，避免 BrowserType.launch 阶段直接失败。
        """
        if not proxy_url:
            return proxy_url
        try:
            u = urlsplit(proxy_url)
            scheme = (u.scheme or "").lower()
            host = (u.hostname or "").lower()
            has_auth = bool(u.username or u.password)
            if scheme in {"socks5", "socks5h"} and has_auth and host.endswith("webshare.io"):
                self.log("[vellum] 检测到 Webshare socks5 认证代理，按 HTTP 代理交给 Chromium")
                return urlunsplit(("http", u.netloc, u.path, u.query, u.fragment))
        except Exception:
            pass
        return proxy_url

    @staticmethod
    def _fill_first(page, selectors: list[str], value: str) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.fill(value, timeout=8000)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _click(page, selectors: list[str]) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.click(timeout=8000)
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _has_email_input(page) -> bool:
        try:
            return page.locator("input[type='email'], input[name='email'], input#email").count() > 0
        except Exception:
            return False

    def _open_email_entry_if_needed(self, page) -> bool:
        """Vellum 自有 /account/signup 中转页需要先点 Email 入口进入 WorkOS 表单。"""
        url_l = (page.url or "").lower()
        if self._has_email_input(page):
            return True
        if "/account/signup" not in url_l and "/account/login" not in url_l:
            return False
        self.log("[vellum] 当前是账号中转页，尝试进入 Email 注册入口...")
        clicked = self._click(page, [
            "button:has-text('Continue with Email')",
            "a:has-text('Continue with Email')",
            "button:has-text('Continue with email')",
            "a:has-text('Continue with email')",
            "button:has-text('Sign up with Email')",
            "a:has-text('Sign up with Email')",
            "button:has-text('Sign up with email')",
            "a:has-text('Sign up with email')",
            "button:has-text('Email')",
            "a:has-text('Email')",
        ])
        self.log(f"[vellum] Email 入口点击结果: {clicked}")
        if not clicked:
            return False
        try:
            page.wait_for_selector("input[type='email'], input[name='email'], input#email", timeout=30000)
            self.log("[vellum] 进入 Email 表单成功")
            return True
        except Exception as exc:
            try:
                preview = " ".join(page.inner_text("body", timeout=3000).split())[:220]
            except Exception:
                preview = ""
            self.log(
                "[vellum] 点击 Email 入口后仍无邮箱框: "
                f"{type(exc).__name__}: {str(exc)[:120]} body={preview!r}"
            )
            return False

    @staticmethod
    def _set_field(page, selector: str, value: str) -> bool:
        """键盘逐字输入，触发 React/Radix 受控组件 onChange（.fill 可能不更新状态）。"""
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                return False
            loc.click()
            try:
                loc.press("Control+a"); loc.press("Delete")
            except Exception:
                pass
            loc.press_sequentially(value, delay=60)
            return True
        except Exception:
            return False

    def _enter_otp(self, page, code: str) -> None:
        target = None
        for s in ("input[autocomplete='one-time-code']", "input[inputmode='numeric']",
                  "input[name='code']", "input#code", "input[type='tel']", "input[type='text']", "input"):
            try:
                loc = page.locator(s)
                if loc.count() > 0:
                    target = loc; break
            except Exception:
                continue
        if target is None:
            page.keyboard.type(code, delay=90)
        else:
            cnt = target.count()
            if cnt >= len(code):
                for i, ch in enumerate(code):
                    try:
                        target.nth(i).fill(ch)
                    except Exception:
                        pass
            else:
                try:
                    target.first.click(); target.first.fill("")
                except Exception:
                    pass
                page.keyboard.type(code, delay=90)
        page.wait_for_timeout(1000)
        self._click(page, ["button[type=submit]", "button:has-text('Continue')",
                            "button:has-text('Verify')", "form button"])

    @staticmethod
    def _state(page) -> dict:
        try:
            txt = page.inner_text("body", timeout=5000)
        except Exception:
            txt = ""
        low = txt.lower(); url = page.url or ""
        return {
            "url": url, "text": txt,
            "cf": ("just a moment" in low) or ("__cf_chl" in url.lower()) or ("performing security verification" in low),
            "forbidden": ("does not have permission" in low) or ("error 1020" in low),
            "policy_denied": ("access blocked" in low) or ("policy_denied" in low),
            "password_screen": "/sign-up/password" in url.lower(),
            "email_verify": "email-verification" in url.lower(),
            "phone_step": ("radar-challenge" in url.lower()) or ("verify your phone" in low) or ("valid mobile phone" in low),
            "dashboard": ("vellum.ai/dashboard" in url.lower()) or ("/onboarding" in url.lower())
                         or ("app.vellum.ai" in url.lower()) or url.rstrip("/").endswith("vellum.ai"),
        }

    # ---- phone ----
    def _do_phone(self, page) -> None:
        if not self.phone_callback:
            raise RuntimeError("Vellum 手机验证需要 phone_callback（启用 phone_provider）")
        # get a number (retry while haozhu pool is empty)
        phone = ""
        for i in range(self.phone_wait_attempts):
            try:
                phone = (self.phone_callback() or "").strip()
            except Exception as exc:
                if i % 5 == 0:
                    self.log(f"[vellum] 取号失败重试 {i+1}: {str(exc)[:80]}")
                phone = ""
            if phone:
                break
            time.sleep(15)
        if not phone:
            raise RuntimeError("Vellum 手机验证未取到号码（豪猪库存为空）")
        raw = re.sub(r"\D", "", phone)
        if self.country_code == "+1" and raw.startswith("1") and len(raw) == 11:
            national = raw[1:]   # US: strip leading country digit -> 10-digit national
        else:
            national = raw
        self.log(f"[vellum] phone {phone} -> {self.country_code} {national}")
        # Radix 受控输入：用键盘逐字输入，否则 country_code 停留默认 +1 导致 "phone number is invalid"
        self._set_field(page, "input[name='country_code']", self.country_code)
        if not self._set_field(page, "input[name='local_number']", national):
            self._set_field(page, "input[type='tel']", national)
        page.wait_for_timeout(700)
        try:
            cc = page.locator("input[name='country_code']").first.input_value()
            ln = page.locator("input[name='local_number']").first.input_value()
            self.log(f"[vellum] phone fields cc={cc!r} local={ln!r}")
        except Exception:
            pass
        self._click(page, ["button:has-text('Send verification code')", "button[type=submit]", "form button"])
        page.wait_for_timeout(4500)
        # second callback call -> poll SMS code
        code = (self.phone_callback() or "").strip()
        if not code:
            raise RuntimeError("Vellum 未收到手机短信验证码")
        self.log(f"[vellum] 短信验证码 {code}")
        self._enter_otp(page, code)
        try:
            page.wait_for_url(lambda u: "radar-challenge" not in u, timeout=35000)
        except Exception:
            pass
        page.wait_for_timeout(9000)

    # ---- one full attempt on one browser/IP ----
    def _attempt(self, pw, email: str, password: str) -> tuple[str, dict]:
        from core.proxy_utils import build_playwright_proxy_settings

        proxy_url, ip = self._next_proxy()
        if self.resin_rotate and not proxy_url:
            return "resin_error", {}
        self.log(f"[vellum] 尝试 IP={ip or '-'}")
        launch_opts: dict[str, Any] = {
            "headless": self.headless,
            "timeout": 45000,
            "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        }
        if proxy_url:
            launch_opts["proxy"] = build_playwright_proxy_settings(self._playwright_proxy_url(proxy_url))
        self.log("[vellum] 启动 Chromium...")
        try:
            browser = pw.chromium.launch(**launch_opts)
        except Exception as exc:
            self.log(f"[vellum] 启动 Chromium 失败: {type(exc).__name__}: {str(exc)[:160]}")
            return "browser_launch_error", {"url": "", "error": str(exc)[:500]}
        self.log("[vellum] Chromium 已启动，创建上下文...")
        try:
            ctx = browser.new_context(viewport={"width": 1366, "height": 800}, user_agent=UA, locale="en-US")
            ctx.set_default_timeout(45000)
            page = ctx.new_page()
            try:
                self.log(f"[vellum] 打开邀请页: {INVITE_BASE}{self.invite_code}")
                page.goto(INVITE_BASE + self.invite_code, wait_until="domcontentloaded", timeout=60000)
                self.log(f"[vellum] 邀请页 domcontentloaded: {(page.url or '')[:120]}")
            except Exception as exc:
                self.log(f"[vellum] 打开邀请页失败: {type(exc).__name__}: {str(exc)[:160]}")
                return "resin_error", {"url": getattr(page, "url", "") or "", "error": str(exc)[:500]}
            page.wait_for_timeout(1200)
            self._open_email_entry_if_needed(page)
            try:
                self.log("[vellum] 等待邮箱输入框...")
                page.wait_for_selector("input[type='email'], input[name='email'], input#email", timeout=35000)
                self.log("[vellum] 已看到邮箱输入框")
            except Exception as exc:
                self.log(f"[vellum] 未等到邮箱输入框，继续状态识别: {type(exc).__name__}: {str(exc)[:120]}")
                # 2026-06 起邀请页可能先落到 /account/signup 中转页；
                # 该页需要先点 Continue/Sign up with Email，才会进入 WorkOS 邮箱表单。
                self._open_email_entry_if_needed(page)
            page.wait_for_timeout(1500)
            cur = page.url or ""
            if cur.startswith("chrome-error") or cur in ("", "about:blank"):
                self.log(f"[vellum] 浏览器错误页: {cur}")
                return "resin_error", {}
            st = self._state(page)
            self.log(
                "[vellum] 邀请页状态 "
                f"url={st['url'][:100]} cf={st['cf']} forbidden={st['forbidden']} "
                f"policy_denied={st['policy_denied']}"
            )
            if st["cf"]:
                return "cloudflare_challenge", st
            if st["forbidden"]:
                return "forbidden", st
            if not self._has_email_input(page):
                self.log("[vellum] 当前页面没有邮箱输入框，跳过本次 IP，避免卡在错误表单")
                return "signup_no_email_form", st

            # page 1: name + email
            self.log("[vellum] 填写姓名/邮箱并提交...")
            first_ok = self._fill_first(page, ["input[autocomplete='given-name']", "input[name='first_name']", "input[name='given_name']", "input#given_name", "input[type='text']"], self.first_name)
            last_ok = self._fill_first(page, ["input[autocomplete='family-name']", "input[name='last_name']", "input[name='family_name']", "input#family_name"], self.last_name)
            email_ok = self._fill_first(page, ["input[type='email']", "input[name='email']", "input#email"], email)
            click_ok = self._click(page, ["button[type=submit]", "button:has-text('Continue')", "form button"])
            self.log(f"[vellum] 表单提交动作: first={first_ok} last={last_ok} email={email_ok} click={click_ok}")
            if not email_ok or not click_ok:
                return "signup_no_email_form", st
            try:
                self.log("[vellum] 等待密码输入框...")
                page.wait_for_selector("input[type='password'], input#password", timeout=25000)
                self.log("[vellum] 已看到密码输入框")
            except Exception as exc:
                self.log(f"[vellum] 未等到密码输入框，继续状态识别: {type(exc).__name__}: {str(exc)[:120]}")
                pass
            page.wait_for_timeout(2000)
            st = self._state(page)
            self.log(
                "[vellum] 邮箱提交后状态 "
                f"url={st['url'][:100]} password={st['password_screen']} "
                f"email_verify={st['email_verify']} policy_denied={st['policy_denied']}"
            )
            if st["cf"]:
                return "cloudflare_challenge", st
            if st["forbidden"]:
                return "forbidden", st

            # password
            if st["password_screen"] or page.locator("input[type='password']").count() > 0:
                self.log("[vellum] 填写密码并提交...")
                self._fill_first(page, ["input[type='password']", "input#password"], password)
                self._click(page, ["button[type=submit]", "button:has-text('Continue')", "form button"])
                page.wait_for_timeout(7000)
                st = self._state(page)
                self.log(
                    "[vellum] 密码提交后状态 "
                    f"url={st['url'][:100]} email_verify={st['email_verify']} "
                    f"phone_step={st['phone_step']} policy_denied={st['policy_denied']}"
                )
            if st["policy_denied"]:
                return "policy_denied", st

            # email verification
            if st["email_verify"]:
                self.log("[vellum] 进入邮箱验证码步骤")
                if not self.otp_callback:
                    raise RuntimeError("Vellum 邮箱验证需要 otp_callback（mailbox provider）")
                code = (self.otp_callback() or "").strip()
                if not code:
                    raise RuntimeError("Vellum 未收到邮箱验证码")
                self._enter_otp(page, code)
                try:
                    page.wait_for_url(lambda u: "email-verification" not in u, timeout=25000)
                except Exception:
                    pass
                page.wait_for_timeout(6000)
                st = self._state(page)
                self.log(
                    "[vellum] 邮箱验证码后状态 "
                    f"url={st['url'][:100]} phone_step={st['phone_step']} dashboard={st['dashboard']}"
                )

            # phone verification (WorkOS radar challenge)
            if st["phone_step"]:
                self.log("[vellum] 进入手机验证步骤")
                self._do_phone(page)
                st = self._state(page)
                self.log(
                    "[vellum] 手机验证后状态 "
                    f"url={st['url'][:100]} dashboard={st['dashboard']}"
                )

            # 必须真正进入 App（www.vellum.ai/assistant 或 onboarding/dashboard）才算落地；
            # 否则仍停在 login.platform.vellum.ai / sign-up 表单（无会话），不能误判 done。
            url_l = (page.url or "").lower()
            landed = ("www.vellum.ai/assistant" in url_l) or ("vellum.ai/dashboard" in url_l) or ("/onboarding" in url_l)
            if not landed:
                self.log(f"[vellum] 未真正落地 App（仍在 {url_l[:60]}），判 not_landed 重试")
                return "not_landed", st

            # landed: capture session + ensure-registration 签发 api_key / referral
            self.log("[vellum] 已落地 App，开始闭环签发/采集结果...")
            result = self._collect_result(page, ctx, email, password, ip)
            return "done", result
        finally:
            try:
                browser.close()
            except Exception:
                pass

    def _collect_result(self, page, ctx, email: str, password: str, ip: str) -> dict:
        cookies = {}
        try:
            cookies = {c["name"]: c["value"] for c in ctx.cookies() if "vellum" in c.get("domain", "")}
        except Exception:
            pass
        st = self._state(page)
        # 邀请码：注册成功后账号自身的 referral 码（用于链式给下一个号）。
        # 主源走 API（GET /v1/referral-codes/me/，见 extract_on_page）；HTML 兜底（落地页通常没有）。
        own_invite = ""
        try:
            m = re.search(r"/r/([A-Za-z0-9]{4,})", page.content())
            if m:
                own_invite = m.group(1)
        except Exception:
            pass
        # 闭环：注册落地后同会话(已登录)直接 ensure-registration 签发 assistant_api_key + 查余额 + 拿本号邀请码，无需重登。
        creds = {"api_key": "", "platform_assistant_id": "", "webhook_secret": "", "platform_user_id": "", "balance_usd": "",
                 "platform_organization_id": "", "local_assistant_id": "", "client_installation_id": "", "runtime_assistant_id": "",
                 "own_invite_code": "", "referral_url": ""}
        try:
            from platforms.vellum.session_api import extract_on_page
            data = extract_on_page(page, provision_key=True, log=self.log)
            creds.update({
                "api_key": data.get("assistant_api_key", ""),
                "platform_assistant_id": data.get("platform_assistant_id", ""),
                "webhook_secret": data.get("webhook_secret", ""),
                "platform_user_id": data.get("platform_user_id", ""),
                "balance_usd": data.get("balance_usd", ""),
                "platform_organization_id": data.get("platform_organization_id", ""),
                "local_assistant_id": data.get("local_assistant_id", ""),
                "client_installation_id": data.get("client_installation_id", ""),
                "runtime_assistant_id": data.get("runtime_assistant_id", ""),
                "own_invite_code": data.get("own_invite_code", ""),
                "referral_url": data.get("referral_url", ""),
            })
            if creds["own_invite_code"]:
                own_invite = creds["own_invite_code"]
            self.log(f"[vellum] 闭环签发: api_key={'yes' if creds['api_key'] else 'no'} balance={creds['balance_usd'] or '-'} invite={own_invite or '-'}")
        except Exception as e:
            self.log(f"[vellum] 闭环凭据签发失败(不影响注册落地): {repr(e)[:120]}")
        return {
            "email": email,
            "password": password,
            "resin_ip": ip,
            "landed_url": st["url"],
            "phone_verified": not st["phone_step"],
            "cookies": cookies,
            "own_invite_code": own_invite,
            "referral_url": creds["referral_url"],
            "api_key": creds["api_key"],
            "platform_assistant_id": creds["platform_assistant_id"],
            "webhook_secret": creds["webhook_secret"],
            "platform_user_id": creds["platform_user_id"],
            "balance_usd": creds["balance_usd"],
            "platform_organization_id": creds["platform_organization_id"],
            "local_assistant_id": creds["local_assistant_id"],
            "client_installation_id": creds["client_installation_id"],
            "runtime_assistant_id": creds["runtime_assistant_id"],
        }

    def run(self, *, email: str, password: str) -> dict:
        from playwright.sync_api import sync_playwright

        retryable = {"cloudflare_challenge", "forbidden", "resin_error", "not_landed", "signup_no_email_form"}
        last_st: dict = {}
        with sync_playwright() as pw:
            for attempt in range(self.nav_attempts):
                self.log(f"[vellum] 注册尝试 {attempt + 1}/{self.nav_attempts}")
                outcome, data = self._attempt(pw, email, password)
                self.log(f"[vellum] 结果: {outcome} url={data.get('url') or data.get('landed_url', '')}")
                if outcome == "done":
                    return data
                if outcome == "policy_denied":
                    raise RuntimeError("Vellum 被 WorkOS Radar 拦截 (policy_denied)：检查邮箱域名信誉")
                last_st = data if isinstance(data, dict) else {}
                if outcome not in retryable:
                    break
                time.sleep(3)
        raise RuntimeError(f"Vellum 注册未完成：用尽 {self.nav_attempts} 次尝试 last={last_st.get('url', '')}")
