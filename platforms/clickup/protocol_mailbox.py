"""ClickUp 浏览器驱动注册 Worker（patchright + 住宅代理 + reCAPTCHA v3 + session/JWT 抓取）。

流程：
  1. patchright 打开 https://app.clickup.com/signup。
  2. 填 username/email/password。
  3. reCAPTCHA v3 浏览器内自动解（sitekey 6Lf6D0YoAAAAAEgVBxwLwC_gxFaDBPyYZX19ocU1，
     action=signup，页面 JS 在 submit 时调 grecaptcha.execute 自动出 token，无需 solver）。
  4. click "Sign up" → POST /user/v1/user{username,email,password,recaptchaV3,...}
     → 自动 login → session cookie → 跳 dashboard。
  5. 无邮件确认（直接登录），跳 dashboard。
  6. 拿 session cookie + workspace JWT（cu_jwt）作 token。
     - cookies 里找 cu_jwt / cu_self（session）等。
     - 从 dashboard URL 提取 workspace_id（app.clickup.com/{workspaceId}/...）。
     - 用 ClickUpClient.generate_workspace_token(workspace_id) 换 cu_jwt（若 cookie 里没有）。

注：reCAPTCHA v3 是 score-based 隐形验证，grecaptcha.execute 由页面 JS 在 submit 时自动调用，
浏览器内无需手动解；patchright 过反检测后 v3 score 通常足够。纯协议模式才需外部 solver 传 token。
"""
from __future__ import annotations

# BLAS 单线程（Windows OOM 防护），必须在 playwright import 前设。
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import re
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from platforms.clickup.core import APP_URL, ClickUpClient, log

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

SIGNUP_URL = f"{APP_URL}/signup"


def _gen_username(provided: str = "") -> str:
    """生成用户名（clickup signup 的 name 字段）。"""
    if provided:
        return provided
    import random
    import string
    seed = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"AutoReg{seed}"


def _fill_text_field(page, selectors: tuple[str, ...], value: str, label: str, log_fn=print) -> bool:
    """填一个文本字段，按 selectors 顺序试。"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                try:
                    loc.press("Control+a")
                    loc.press("Delete")
                except Exception:
                    pass
                loc.fill(value, timeout=8000)
                log_fn(f"[clickup] 已填 {label} (sel={sel})")
                return True
        except Exception:
            continue
    log_fn(f"[clickup] 未找到 {label} 输入框")
    return False


def _page_state(page) -> dict:
    """读页面状态：url/dashboard/signup/错误。"""
    try:
        txt = page.inner_text("body", timeout=3000)
    except Exception:
        txt = ""
    low = txt.lower()
    url = page.url or ""
    return {
        "url": url,
        "text": txt,
        "signup": "/signup" in url.lower(),
        "login": "/login" in url.lower() and "confirm" not in url.lower(),
        # dashboard：app.clickup.com 后非 signup/login 路径（带 workspace_id 或 /home 等）
        "dashboard": ("app.clickup.com" in url.lower()) and ("/signup" not in url.lower()) and ("/login" not in url.lower()),
        "error": ("error" in low) or ("invalid" in low) or ("already" in low) or ("failed" in low) or ("captcha" in low),
    }


def _extract_workspace_id(url: str) -> str:
    """从 dashboard URL 提取 workspace_id（app.clickup.com/{workspaceId}/...）。

    clickup dashboard URL 形如 app.clickup.com/1234567/home、app.clickup.com/1234567/space/...
    第一段纯数字即 team/workspace id。
    """
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return ""
    if not path:
        return ""
    first = path.split("/", 1)[0]
    # workspace_id 通常是纯数字
    if first.isdigit():
        return first
    return ""


def _pick_cookie_value(cookies: dict[str, str], names: tuple[str, ...]) -> str:
    """从 cookies dict 按候选名取值。"""
    for name in names:
        if name in cookies and cookies[name]:
            return cookies[name]
    return ""


class ClickUpProtocolMailboxWorker:
    """ClickUp 浏览器驱动注册 Worker（patchright + reCAPTCHA v3 + session/JWT 抓取）。

    构造参数兼容框架 ProtocolMailboxAdapter。run(email, password, username, mailbox,
    mailbox_account) 执行完整链路。clickup 无邮件确认，mailbox 仅用于框架注入（填表用邮箱）。
    """

    def __init__(self, *, proxy: str | None = None, timeout: int = 300,
                 log_fn=print, otp_callback: Callable[[], str] | None = None,
                 captcha_solver=None, username: str = "", **_kwargs):
        self.proxy = proxy
        self.timeout = timeout
        self.otp_callback = otp_callback  # 兼容框架，clickup 无 OTP 不用此
        self.captcha_solver = captcha_solver  # 兼容框架，reCAPTCHA v3 浏览器内自动解
        self.username = username or ""
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
            username: str = "",
            link_callback: Callable[[], str] | None = None,
            mailbox=None, mailbox_account=None) -> dict[str, Any]:
        """执行 ClickUp 注册。email/password/username 由框架注入；mailbox 框架按 mail_provider 建好传入。"""
        if not email:
            raise RuntimeError("ClickUp 注册缺少邮箱")
        if not password:
            raise RuntimeError("ClickUp 注册缺少密码")
        # mailbox 可为 None（clickup 无邮件确认），但框架通常仍注入；不强校验。

        display_name = _gen_username(username)
        self.log(f"[clickup] 使用邮箱: {email} | 用户名: {display_name}")

        result: dict[str, Any] = {
            "email": email,
            "password": password,
            "username": display_name,
            "proxy": self.proxy,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stages": [],
            "api_base": APP_URL,
        }

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
                if p.username:
                    pcfg["username"] = p.username
                if p.password:
                    pcfg["password"] = p.password
                launch_opts["proxy"] = pcfg
            browser = pw.chromium.launch(**launch_opts)
            try:
                yield browser, pw
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                try:
                    pw.stop()
                except Exception:
                    pass

        with _patchright_ctx() as (browser, pw):
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = ctx.new_page()

            # 1. 打开 signup
            opened = False
            for _g in range(3):
                try:
                    page.goto(SIGNUP_URL, wait_until="commit", timeout=60000)
                    opened = True
                    break
                except Exception as exc:
                    self.log(f"[clickup] goto signup 失败重试 {_g+1}: {str(exc)[:120]}")
                    page.wait_for_timeout(2000)
            if not opened:
                raise RuntimeError(f"打开 signup 多次失败: {SIGNUP_URL}")
            page.wait_for_timeout(4000)
            self.log(f"[clickup] signup 页 url={page.url[:80]}")

            # 2. 填表单（username/email/password）
            # username/name 字段：clickup signup 可能是 "Full name" 或 "Username"
            _fill_text_field(
                page,
                ("input[name='name']", "input[name='username']", "input[placeholder*='name' i]",
                 "input[placeholder*='Full' i]", "input[autocomplete='name']"),
                display_name, "Username/Name", log_fn=self.log,
            )
            _fill_text_field(
                page,
                ("input[type='email']", "input[name='email']", "input#email", "input[autocomplete='email']"),
                email, "Email", log_fn=self.log,
            )
            _fill_text_field(
                page,
                ("input[type='password']", "input[name='password']", "input#password",
                 "input[autocomplete='new-password']"),
                password, "Password", log_fn=self.log,
            )
            page.wait_for_timeout(1000)
            result["stages"].append({"stage": "fill_signup", "ok": True})

            # 3. reCAPTCHA v3 浏览器内自动解（grecaptcha.execute 由页面 JS 在 submit 时调用）。
            # 等待 grecaptcha 脚本加载（最多 20s），无需手动出 token。
            self.log("[clickup] 等 reCAPTCHA v3 脚本加载（浏览器内自动解）...")
            recaptcha_ready = False
            for _ in range(20):
                try:
                    ready = page.evaluate(
                        "() => typeof window.grecaptcha === 'object' && !!window.grecaptcha.execute"
                    )
                    if ready:
                        recaptcha_ready = True
                        self.log("[clickup] grecaptcha 已就绪（v3 浏览器内自动解）")
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            result["stages"].append({"stage": "recaptcha_v3_ready", "ok": recaptcha_ready})

            # 4. click "Sign up"
            clicked = False
            for sel in ("button:has-text('Sign up')", "button:has-text('Sign Up')",
                        "button[type='submit']:has-text('Sign')", "button:has-text('Get started')",
                        "button:has-text('Create')", "form button[type='submit']"):
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        loc.click(timeout=8000)
                        clicked = True
                        self.log(f"[clickup] 点击 Sign up (sel={sel})")
                        break
                except Exception:
                    continue
            if not clicked:
                try:
                    page.keyboard.press("Enter")
                    clicked = True
                    self.log("[clickup] Enter 键提交")
                except Exception:
                    pass
            if not clicked:
                result["stages"].append({"stage": "signup_submit", "ok": False})
                raise RuntimeError("未找到 ClickUp 注册提交按钮")

            # 5. 等跳 dashboard（clickup 注册成功自动 login，无邮件确认）
            page.wait_for_timeout(5000)
            st = _page_state(page)
            self.log(f"[clickup] 注册提交后 url={st['url'][:80]} dashboard={st['dashboard']} error={st['error']}")
            if not st["dashboard"]:
                for _ in range(12):
                    if st["dashboard"]:
                        break
                    # 错误页且非 dashboard 则报错
                    if st["error"] and not st["signup"]:
                        break
                    page.wait_for_timeout(2500)
                    st = _page_state(page)
            if st["error"] and not st["dashboard"]:
                result["stages"].append({"stage": "signup_submit", "ok": False, "state": st})
                raise RuntimeError(f"注册被拒: {st['text'][:200]}")
            if not st["dashboard"]:
                result["stages"].append({"stage": "signup_submit", "ok": False, "state": st})
                raise RuntimeError(f"注册后未进 dashboard: url={st['url'][:80]}")
            result["stages"].append({"stage": "signup_submit", "ok": True})
            result["signup_result"] = {"status": 200, "dashboard_url": st["url"]}

            # 6. 抓 session cookie + cu_jwt
            try:
                cookies_list = ctx.cookies()
                cookies = {c["name"]: c["value"] for c in cookies_list}
                result["cookies"] = cookies
                # cu_jwt 可能直接在 cookie 里
                cu_jwt = _pick_cookie_value(cookies, ("cu_jwt", "cu-jwt", "jwt"))
                # session cookie 候选名
                session_cookie = _pick_cookie_value(cookies, ("cu_self", "cu-session", "session", "__cf_bm", "cu_self_v2"))
                # 从 dashboard URL 提取 workspace_id
                workspace_id = _extract_workspace_id(st["url"])
                self.log(
                    f"[clickup] cookies 数={len(cookies)} cu_jwt={'有' if cu_jwt else '无'} "
                    f"session={'有' if session_cookie else '无'} ws={workspace_id[:12] or '无'}"
                )
                result["cu_jwt"] = cu_jwt
                result["session_cookie"] = session_cookie
                result["workspace_id"] = workspace_id
                result["stages"].append({"stage": "grab_cookies", "ok": bool(cookies)})
            except Exception as exc:
                self.log(f"[clickup] 抓 cookies 异常: {exc!r}")
                result["stages"].append({"stage": "grab_cookies", "ok": False, "error": str(exc)[:200]})

        # 7. 若 cookie 里没 cu_jwt 但有 session + workspace_id，用 ClickUpClient 换 cu_jwt
        if not result.get("cu_jwt") and result.get("session_cookie") and result.get("workspace_id"):
            try:
                client = ClickUpClient(proxy=self.proxy, log_fn=self.log)
                # 把 session cookie 注入 client.session（复用浏览器登录态）
                for name, value in (result.get("cookies") or {}).items():
                    client.session.cookies.set(name, value)
                ws_token_resp = client.generate_workspace_token(workspace_id=str(result["workspace_id"]))
                if ws_token_resp.get("ok"):
                    # generate-token 返回结构待实测，常见 {token: "..."} 或 {jwt: "..."}
                    ws_jwt = str(
                        ws_token_resp.get("token") or ws_token_resp.get("jwt") or ws_token_resp.get("cu_jwt") or ""
                    ).strip()
                    if ws_jwt:
                        result["cu_jwt"] = ws_jwt
                        self.log(f"[clickup] generate_workspace_token 拿到 cu_jwt={ws_jwt[:16]}...")
                        result["stages"].append({"stage": "generate_workspace_token", "ok": True})
                    else:
                        result["stages"].append({"stage": "generate_workspace_token", "ok": False, "resp": str(ws_token_resp)[:200]})
                else:
                    result["stages"].append({"stage": "generate_workspace_token", "ok": False, "status": ws_token_resp.get("status")})
            except Exception as exc:
                self.log(f"[clickup] generate_workspace_token 异常（非阻塞）: {exc!r}")
                result["stages"].append({"stage": "generate_workspace_token", "ok": False, "error": str(exc)[:200]})

        # token = cu_jwt 优先，其次 session cookie
        token = str(result.get("cu_jwt") or result.get("session_cookie") or "").strip()
        result["token"] = token
        result["api_key"] = token
        result["status"] = "registered" if token else "no_token"
        result["registered"] = bool(token)
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if token:
            self.log(f"[clickup] [OK] 注册成功 {email} token={token[:16]}...")
        else:
            self.log(f"[clickup] [WARN] 注册完成但未取到 token {email}")
        return result
