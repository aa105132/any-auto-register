"""Runbear 浏览器驱动注册 Worker（patchright + 住宅代理 + Turnstile + combobox + 邮件确认链接）。

流程：
  1. patchright 打开 auth.runbear.io/en/signup?rt={base64(app.runbear.io/overview)}&signedUp=true。
  2. 填 First name/Last name/Email/Password/How did you hear(combobox 选 "Search engine")/勾 ToS。
  3. Cloudflare Turnstile widget 自动解（浏览器内渲染，sitekey 0x4AAAAAADrn0IM-tpRSsa_-）。
  4. click "Sign up with email" → POST /api/fe/v2/signup{email,pwd,turnstile_token,first_name,
     last_name,properties{referral_source,tos}} → 200 → /en/login/confirm_email。
  5. mailbox.wait_for_link(keyword="Runbear") 收确认邮件链接（before_ids 基线避免旧邮件）。
  6. patchright navigate 确认链接 → 邮箱确认 → 跳 app.runbear.io。
  7. 拿 PropelAuth access_token（cookie 或 /api/fe/v2/login_state）→ 返回 result dict。

注：Turnstile widget 在浏览器内自动解（patchright 过 Cloudflare WAF），无需 solver。
combobox 必须真实点击选项触发 React onChange（evaluate 设值不触发 referral_source 校验）。
"""
from __future__ import annotations

# BLAS 单线程（Windows OOM 防护），必须在 playwright import 前设。
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import base64
import time
from typing import Any, Callable
from urllib.parse import urlparse

from platforms.runbear.core import APP_URL, AUTH_BASE, RunbearClient, log

ROOT = Path(__file__).resolve().parents[2] if False else __import__("pathlib").Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))


def _wait_confirm_link(mailbox, account, *, keyword: str = "Runbear", timeout: int = 240,
                       before_ids: set | None = None, log_fn=print) -> str:
    """等 runbear 确认邮件链接。keyword='Runbear' 过滤，wait_for_link 提取 confirm 链接。"""
    log_fn(f"[runbear] 等待确认邮件链接 (keyword={keyword!r} timeout={timeout}s)...")
    try:
        link = mailbox.wait_for_link(
            account, keyword=keyword, timeout=timeout, before_ids=before_ids,
        )
        return link or ""
    except Exception as exc:
        log_fn(f"[runbear] 收确认链接异常: {exc!r}")
        return ""


def _fill_text_field(page, selectors: tuple[str, ...], value: str, label: str, log_fn=print) -> bool:
    """填一个文本字段，按 selectors 顺序试。"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                try:
                    loc.press("Control+a"); loc.press("Delete")
                except Exception:
                    pass
                loc.fill(value, timeout=8000)
                log_fn(f"[runbear] 已填 {label} (sel={sel})")
                return True
        except Exception:
            continue
    log_fn(f"[runbear] 未找到 {label} 输入框")
    return False


def _select_combobox(page, option_text: str, log_fn=print) -> bool:
    """选 combobox 选项（真实点击触发 React onChange）。

    Mantine Select combobox：点 combobox 展开 → 点 option。
    """
    try:
        # 点 combobox 展开（多 selector 兜底）
        for sel in ("input[role='combobox']", "[role='combobox']", "div[class*='Select'] input"):
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.click(timeout=5000)
                    break
            except Exception:
                continue
        page.wait_for_timeout(500)
        # 点 option
        opt = page.get_by_role("option", name=option_text).first
        if opt.count() > 0:
            opt.click(timeout=5000)
            log_fn(f"[runbear] 选 How did you hear = {option_text}")
            return True
    except Exception as exc:
        log_fn(f"[runbear] 选 combobox 异常: {exc!r}")
    return False


def _check_tos(page, log_fn=print) -> bool:
    """勾 ToS checkbox。"""
    try:
        chk = page.get_by_role("checkbox").first
        if chk.count() > 0:
            is_checked = chk.is_checked()
            if not is_checked:
                chk.click(timeout=5000)
            log_fn(f"[runbear] ToS 已勾")
            return True
    except Exception as exc:
        log_fn(f"[runbear] 勾 ToS 异常: {exc!r}")
    return False


def _page_state(page) -> dict:
    """读页面状态：url/确认页/登录页/dashboard/错误。"""
    try:
        txt = page.inner_text("body", timeout=3000)
    except Exception:
        txt = ""
    low = txt.lower()
    url = (page.url or "").lower()
    return {
        "url": page.url or "",
        "text": txt,
        "confirm_email": ("confirm your email" in low) or ("/confirm_email" in url) or ("check your email" in low),
        "login": "/login" in url and "confirm" not in url,
        "dashboard": ("app.runbear.io" in url) or ("/overview" in url) or ("/agents" in url),
        "signup": "/signup" in url,
        "challenge": "/challenge" in url,  # PropelAuth 人机挑战页（Turnstile widget）
        "error": ("error" in low) or ("invalid" in low) or ("already" in low) or ("failed" in low) or ("missing required" in low),
    }


def _build_signup_url() -> str:
    """构造注册 URL：rt=base64(app.runbear.io/overview)&signedUp=true。"""
    rt = base64.b64encode(f"{APP_URL}/overview".encode("utf-8")).decode("ascii")
    return f"{AUTH_BASE}/en/signup?rt={rt}&signedUp=true"


class RunbearProtocolMailboxWorker:
    """Runbear 浏览器驱动注册 Worker（patchright + Turnstile + combobox + 确认链接）。

    构造参数兼容框架 ProtocolMailboxAdapter。run(email, password, first_name, last_name,
    mailbox, mailbox_account) 执行完整链路。
    """

    def __init__(self, *, proxy: str | None = None, timeout: int = 300,
                 log_fn=print, otp_callback: Callable[[], str] | None = None,
                 captcha_solver=None, referral_source: str = "Search engine", **_kwargs):
        self.proxy = proxy
        self.timeout = timeout
        self.otp_callback = otp_callback  # 兼容框架，runbear 用 wait_for_link
        self.captcha_solver = captcha_solver
        self.referral_source = referral_source or "Search engine"
        raw_log = log_fn or log
        self.log = lambda msg: self._safe_log(raw_log, msg)

    @staticmethod
    def _safe_log(raw_log, msg: str) -> None:
        try:
            raw_log(msg)
        except UnicodeEncodeError:
            try:
                import sys
                sys.stdout.buffer.write((str(msg) + "\n").encode("utf-8", "replace"))
                sys.stdout.buffer.flush()
            except Exception:
                pass

    def run(self, *, email: str = "", password: str = "",
            first_name: str = "Auto", last_name: str = "Register",
            link_callback: Callable[[], str] | None = None,
            mailbox=None, mailbox_account=None) -> dict[str, Any]:
        """执行 Runbear 注册。"""
        from core.base_mailbox import MailboxAccount

        if not email:
            raise RuntimeError("Runbear 注册缺少邮箱")
        if not password:
            raise RuntimeError("Runbear 注册缺少密码")
        if mailbox is None or mailbox_account is None:
            raise RuntimeError("Runbear 注册需 mailbox + mailbox_account（框架按 mail_provider 注入）")

        account = mailbox_account
        before_ids = mailbox.get_current_ids(account) if mailbox is not None else set()
        self.log(f"[runbear] 使用邮箱: {email} | 注册前邮件基线: {len(before_ids)} 封")

        result: dict[str, Any] = {
            "email": email,
            "password": password,
            "first_name": first_name,
            "last_name": last_name,
            "referral_source": self.referral_source,
            "proxy": self.proxy,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stages": [],
            "api_base": APP_URL,
        }

        signup_url = _build_signup_url()

        from contextlib import contextmanager

        @contextmanager
        def _patchright_ctx():
            from patchright.sync_api import sync_playwright
            pw = sync_playwright().start()
            launch_args = [
                "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                "--disable-default-apps", "--disable-extensions", "--disable-sync",
                "--disable-translate", "--disable-hang-monitor", "--disable-domain-reliability",
                "--no-first-run", "--no-default-browser-check", "--no-sandbox",
                "--metrics-recording-only", "--mute-audio",
            ]
            launch_opts = {"headless": False, "args": launch_args}
            if self.proxy:
                p = urlparse(self.proxy)
                pcfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
                if p.username: pcfg["username"] = p.username
                if p.password: pcfg["password"] = p.password
                launch_opts["proxy"] = pcfg
            browser = pw.chromium.launch(**launch_opts)
            try:
                yield browser, pw
            finally:
                try: browser.close()
                except Exception: pass
                try: pw.stop()
                except Exception: pass

        with _patchright_ctx() as (browser, pw):
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = ctx.new_page()

            opened = False
            for _g in range(3):
                try:
                    page.goto(signup_url, wait_until="commit", timeout=60000)
                    opened = True
                    break
                except Exception as exc:
                    self.log(f"[runbear] goto signup 失败重试 {_g+1}: {str(exc)[:120]}")
                    page.wait_for_timeout(2000)
            if not opened:
                raise RuntimeError(f"打开 signup 多次失败: {signup_url[:80]}")
            page.wait_for_timeout(4000)

            # 填表单
            if not _fill_text_field(page, ("input[name='firstName']", "input[autocomplete='given-name']", "input[placeholder*='First' i]"), first_name, "First name", log_fn=self.log):
                # 兜底：按 label 找
                try:
                    page.get_by_label("First name").first.fill(first_name, timeout=5000)
                except Exception:
                    pass
            if not _fill_text_field(page, ("input[name='lastName']", "input[autocomplete='family-name']", "input[placeholder*='Last' i]"), last_name, "Last name", log_fn=self.log):
                try:
                    page.get_by_label("Last name").first.fill(last_name, timeout=5000)
                except Exception:
                    pass
            _fill_text_field(page, ("input[type='email']", "input[name='email']", "input#email", "input[autocomplete='email']"), email, "Email", log_fn=self.log)
            _fill_text_field(page, ("input[type='password']", "input[name='password']", "input#password", "input[autocomplete='new-password']"), password, "Password", log_fn=self.log)
            _select_combobox(page, self.referral_source, log_fn=self.log)
            _check_tos(page, log_fn=self.log)
            page.wait_for_timeout(1000)
            result["stages"].append({"stage": "fill_signup", "ok": True})

            # 等 Turnstile widget 渲染+自动解（最多 30s）
            self.log("[runbear] 等 Turnstile widget 自动解...")
            turnstile_ok = False
            for _ in range(30):
                try:
                    # Turnstile 解成功后 iframe 有 [name="cf-turnstile-response"] 隐藏 input 有值
                    val = page.evaluate("""() => { const i=document.querySelector('iframe[src*="challenges.cloudflare.com"]'); const hid=document.querySelector('[name="cf-turnstile-response"]'); return {hasIframe: !!i, hiddenVal: hid?hid.value:''}; }""")
                    if val.get("hiddenVal"):
                        turnstile_ok = True
                        self.log(f"[runbear] Turnstile 已解 token={val['hiddenVal'][:24]}...")
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            result["stages"].append({"stage": "turnstile", "ok": turnstile_ok})

            # click "Sign up with email"
            clicked = False
            for sel in ("button:has-text('Sign up with email')", "button[type='submit']:has-text('Sign up')", "button:has-text('Sign up')", "form button[type='submit']"):
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        loc.click(timeout=8000)
                        clicked = True
                        self.log(f"[runbear] 点击 Sign up with email (sel={sel})")
                        break
                except Exception:
                    continue
            if not clicked:
                try:
                    page.keyboard.press("Enter")
                    clicked = True
                except Exception:
                    pass

            # 等跳 confirm_email 页（可能先经 /challenge 人机挑战页，Turnstile widget 自动解）
            page.wait_for_timeout(5000)
            st = _page_state(page)
            self.log(f"[runbear] 注册提交后 url={st['url'][:80]} confirm={st['confirm_email']} challenge={st['challenge']} error={st['error']}")
            if not st["confirm_email"]:
                for _ in range(24):
                    if st["confirm_email"] or st["dashboard"]:
                        break
                    page.wait_for_timeout(2500)
                    st = _page_state(page)
            if st["error"] and not st["confirm_email"] and not st["challenge"]:
                result["stages"].append({"stage": "signup_submit", "ok": False, "state": st})
                raise RuntimeError(f"注册被拒: {st['text'][:200]}")
            if not st["confirm_email"] and not st["dashboard"]:
                result["stages"].append({"stage": "signup_submit", "ok": False, "state": st})
                raise RuntimeError(f"注册后未进 confirm_email/dashboard: url={st['url'][:80]}")
            result["stages"].append({"stage": "signup_submit", "ok": True})
            result["signup_result"] = {"status": 200, "confirm_email": st["confirm_email"]}

            # 收确认邮件链接
            if st["confirm_email"]:
                link = _wait_confirm_link(
                    mailbox, account, keyword="Runbear",
                    timeout=min(self.timeout, 240), before_ids=before_ids, log_fn=self.log,
                )
                if not link:
                    result["stages"].append({"stage": "wait_confirm_link", "ok": False})
                    raise RuntimeError("未收到 Runbear 确认邮件链接")
                result["stages"].append({"stage": "wait_confirm_link", "ok": True, "link": link[:80]})

                # navigate 确认链接
                try:
                    page.goto(link, wait_until="commit", timeout=60000)
                    page.wait_for_timeout(5000)
                except Exception as exc:
                    self.log(f"[runbear] navigate 确认链接异常: {str(exc)[:120]}")
                st2 = _page_state(page)
                self.log(f"[runbear] 确认链接后 url={st2['url'][:80]} dashboard={st2['dashboard']}")
                result["email_confirmed"] = True
                result["confirm_result"] = {"final_url": st2["url"], "dashboard": st2["dashboard"]}
                result["stages"].append({"stage": "confirm_email", "ok": True, "state": st2})
            else:
                result["email_confirmed"] = True
                result["stages"].append({"stage": "confirm_email", "ok": True, "skipped": True})

            # 拿 access_token：从 cookie 或 /api/fe/v2/login_state
            try:
                cookies = ctx.cookies()
                result["cookies"] = {c["name"]: c["value"] for c in cookies}
                # PropelAuth access_token 通常在 cookie __pa_session 或 access_token
                access_token = ""
                for c in cookies:
                    if c["name"] in ("access_token", "__pa_session", "pa_session", "token"):
                        access_token = c["value"]
                        break
                if access_token:
                    result["access_token"] = access_token
                    result["token"] = access_token
                    result["api_key"] = access_token
                    self.log(f"[runbear] 拿到 access_token={access_token[:16]}...")
                else:
                    # 兜底：调 login_state
                    client = RunbearClient(proxy=self.proxy, log_fn=self.log)
                    state = client.get_login_state()
                    result["login_state"] = state
                    if state.get("is_authenticated"):
                        result["access_token"] = str(state.get("access_token") or "")
            except Exception as exc:
                self.log(f"[runbear] 拿 access_token 异常: {exc!r}")

        result["status"] = "registered"
        result["registered"] = True
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log(f"[runbear] [OK] 注册成功 {email} token={str(result.get('access_token') or '')[:16]}...")
        return result
