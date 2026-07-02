"""PromptQL 浏览器驱动注册 Worker（patchright + 住宅代理 + Turnstile widget + OTP 邮件码）。

流程：
  1. patchright 打开 https://prompt.ql.app/login。
  2. 填 email。
  3. Cloudflare Turnstile widget 自动解（浏览器内渲染，sitekey 0x4AAAAAADsy_TOiX96NjTFT）。
  4. 拦截 SPA fetch 响应：POST auth.pro.ql.app/otp/send → {message,nonce}，POST /otp/verify → 200 set cookie。
  5. click "Continue with email"/"Send code" → SPA 发 /otp/send（带 captcha_token）。
  6. mailbox.wait_for_code(keyword="PromptQL", code_pattern=6位) 收 OTP。
  7. 填 OTP → click "Verify"/"Continue" → SPA 发 /otp/verify → 200 set session cookie。
  8. 读浏览器 session cookie 作 access_token（Hasura OIDC session）。

注：promptql 是 OTP 登录无密码字段；password 参数仅作签名兼容，不使用。
Turnstile widget 在浏览器内自动解（patchright 过 Cloudflare WAF），无需 solver 传 token。
OTP 用 wait_for_code（非 wait_for_link），before_ids 取注册前基线避免旧 OTP 干扰。
"""
from __future__ import annotations

# BLAS 单线程（Windows OOM 防护），必须在 playwright import 前设。
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from platforms.promptql.core import APP_URL, log

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

LOGIN_URL = f"{APP_URL}/login"
OTP_KEYWORD = "PromptQL"
OTP_CODE_PATTERN = r"(?<!\d)(\d{6})(?!\d)"


def _wait_otp_code(mailbox, account, *, keyword: str = OTP_KEYWORD, timeout: int = 240,
                   before_ids: set | None = None, log_fn=print) -> str:
    """等 promptql OTP 邮件码。keyword='PromptQL' 过滤，wait_for_code 用 6 位数字正则提取。"""
    log_fn(f"[promptql] 等待 OTP 邮件码 (keyword={keyword!r} timeout={timeout}s)...")
    try:
        code = mailbox.wait_for_code(
            account, keyword=keyword, timeout=timeout, before_ids=before_ids,
            code_pattern=OTP_CODE_PATTERN,
        )
        return str(code or "").strip()
    except Exception as exc:
        log_fn(f"[promptql] 收 OTP 异常: {exc!r}")
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
                log_fn(f"[promptql] 已填 {label} (sel={sel})")
                return True
        except Exception:
            continue
    log_fn(f"[promptql] 未找到 {label} 输入框")
    return False


def _click_button(page, candidates: tuple[str, ...], log_fn=print) -> str:
    """按 candidates 顺序点第一个可见按钮，返回命中的 selector。"""
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                log_fn(f"[promptql] 点击按钮 (sel={sel})")
                return sel
        except Exception:
            continue
    return ""


def _page_state(page) -> dict:
    """读页面状态：url/登录页/OTP 输入态/dashboard/错误。"""
    try:
        txt = page.inner_text("body", timeout=3000)
    except Exception:
        txt = ""
    low = txt.lower()
    url = (page.url or "").lower()
    app_low = APP_URL.lower().rstrip("/")
    return {
        "url": page.url or "",
        "text": txt,
        "login": "/login" in url,
        "otp_input": ("enter the code" in low) or ("verification code" in low)
                      or ("enter code" in low) or ("check your email" in low)
                      or ("we sent" in low) or ("otp" in low),
        # dashboard：已离开 /login 且仍在 app 域内（首页/项目/wiki 都算）
        "dashboard": ("/login" not in url) and url.startswith(app_low) and (url.rstrip("/") != app_low),
        "error": ("error" in low) or ("invalid" in low) or ("failed" in low)
                 or ("too many" in low) or ("expired" in low) or ("not found" in low),
    }


class PromptQLProtocolMailboxWorker:
    """PromptQL 浏览器驱动注册 Worker（patchright + Turnstile widget + OTP 邮件码）。

    构造参数兼容框架 ProtocolMailboxAdapter。run(email, password, otp_callback,
    mailbox, mailbox_account) 执行完整链路。password 不使用（OTP 登录无密码）。
    """

    def __init__(self, *, proxy: str | None = None, timeout: int = 300,
                 log_fn=print, otp_callback: Callable[[], str] | None = None,
                 captcha_solver=None, **_kwargs):
        self.proxy = proxy
        self.timeout = timeout
        self.otp_callback = otp_callback  # 框架注入的 OTP 回调；worker 直接用 mailbox.wait_for_code 取最新基线
        self.captcha_solver = captcha_solver
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
            otp_callback: Callable[[], str] | None = None,
            mailbox=None, mailbox_account=None) -> dict[str, Any]:
        """执行 PromptQL 注册。email 由框架注入；mailbox/mailbox_account 框架按 mail_provider 建好传入。"""
        if not email:
            raise RuntimeError("PromptQL 注册缺少邮箱")
        if mailbox is None or mailbox_account is None:
            raise RuntimeError("PromptQL 注册需 mailbox + mailbox_account（框架按 mail_provider 注入）")

        account = mailbox_account
        before_ids = mailbox.get_current_ids(account) if mailbox is not None else set()
        self.log(f"[promptql] 使用邮箱: {email} | 注册前邮件基线: {len(before_ids)} 封")

        result: dict[str, Any] = {
            "email": email,
            "password": "",  # OTP 登录无密码
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

            # 拦截 SPA fetch 响应：/otp/send 拿 nonce，/otp/verify 拿状态+body（用于诊断 + 校验成功）
            captured: dict[str, Any] = {
                "nonce": "", "send_status": 0, "verify_status": 0, "verify_body": None,
            }

            def _on_response(resp) -> None:
                try:
                    url = resp.url or ""
                    method = (resp.request.method or "").upper()
                    if method != "POST":
                        return
                    if "/otp/send" in url:
                        captured["send_status"] = int(resp.status or 0)
                        try:
                            data = resp.json()
                            if isinstance(data, dict):
                                captured["nonce"] = str(data.get("nonce") or "")
                        except Exception:
                            pass
                    elif "/otp/verify" in url:
                        captured["verify_status"] = int(resp.status or 0)
                        try:
                            captured["verify_body"] = resp.json()
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on("response", _on_response)

            # 1. 打开 login
            opened = False
            for _g in range(3):
                try:
                    page.goto(LOGIN_URL, wait_until="commit", timeout=60000)
                    opened = True
                    break
                except Exception as exc:
                    self.log(f"[promptql] goto login 失败重试 {_g+1}: {str(exc)[:120]}")
                    page.wait_for_timeout(2000)
            if not opened:
                raise RuntimeError(f"打开 login 多次失败: {LOGIN_URL}")
            page.wait_for_timeout(4000)
            result["stages"].append({"stage": "open_login", "ok": True})

            # 2. 填 email
            if not _fill_text_field(
                page,
                ("input[type='email']", "input[name='email']", "input#email",
                 "input[autocomplete='email']", "input[placeholder*='email' i]"),
                email, "Email", log_fn=self.log,
            ):
                # 兜底：按 label 找
                try:
                    page.get_by_label("Email").first.fill(email, timeout=5000)
                    self.log("[promptql] 兜底按 label 填 Email")
                except Exception:
                    pass
            page.wait_for_timeout(800)
            result["stages"].append({"stage": "fill_email", "ok": True})

            # 3. 等 Turnstile widget 渲染+自动解（最多 30s）
            self.log("[promptql] 等 Turnstile widget 自动解...")
            turnstile_ok = False
            for _ in range(30):
                try:
                    val = page.evaluate(
                        "() => { const i=document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]');"
                        " const hid=document.querySelector('[name=\"cf-turnstile-response\"]');"
                        " return {hasIframe: !!i, hiddenVal: hid?hid.value:''}; }"
                    )
                    if val.get("hiddenVal"):
                        turnstile_ok = True
                        self.log(f"[promptql] Turnstile 已解 token={str(val.get('hiddenVal'))[:24]}...")
                        break
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            result["stages"].append({"stage": "turnstile", "ok": turnstile_ok})

            # 4. click 发 OTP 按钮
            send_clicked = _click_button(
                page,
                (
                    "button:has-text('Continue with email')",
                    "button:has-text('Send code')",
                    "button:has-text('Send OTP')",
                    "button:has-text('Continue')",
                    "button:has-text('Sign in')",
                    "button:has-text('Log in')",
                    "button[type='submit']",
                    "form button[type='submit']",
                ),
                log_fn=self.log,
            )
            if not send_clicked:
                try:
                    page.keyboard.press("Enter")
                    send_clicked = "Enter"
                except Exception:
                    pass
            if not send_clicked:
                result["stages"].append({"stage": "click_send_otp", "ok": False})
                raise RuntimeError("未找到 PromptQL 发 OTP 按钮")
            result["stages"].append({"stage": "click_send_otp", "ok": True, "sel": send_clicked})

            # 5. 等 /otp/send 响应（最多 30s）
            for _ in range(30):
                if captured["send_status"] or captured["nonce"]:
                    break
                page.wait_for_timeout(1000)
            self.log(f"[promptql] /otp/send status={captured['send_status']} nonce={captured['nonce'][:12]}...")
            if captured["send_status"] and captured["send_status"] != 200:
                result["stages"].append({"stage": "otp_send", "ok": False, "status": captured["send_status"]})
                st = _page_state(page)
                raise RuntimeError(f"PromptQL /otp/send 被拒 status={captured['send_status']}: {st['text'][:200]}")
            result["stages"].append({"stage": "otp_send", "ok": True, "nonce": captured["nonce"][:16]})

            # 6. 收 OTP 邮件码
            otp = _wait_otp_code(
                mailbox, account, keyword=OTP_KEYWORD,
                timeout=min(self.timeout, 240), before_ids=before_ids, log_fn=self.log,
            )
            if not otp:
                # 兜底：试框架注入的 otp_callback（参数版 + self 版）
                for cb in (otp_callback, self.otp_callback):
                    if callable(cb):
                        try:
                            otp = str(cb() or "").strip()
                            if otp:
                                break
                        except Exception as exc:
                            self.log(f"[promptql] otp_callback 兜底异常: {exc!r}")
            if not otp:
                result["stages"].append({"stage": "wait_otp", "ok": False})
                raise RuntimeError("未收到 PromptQL OTP 验证码")
            result["stages"].append({"stage": "wait_otp", "ok": True, "otp": otp})
            self.log(f"[promptql] 收到 OTP: {otp}")

            # 7. 填 OTP
            page.wait_for_timeout(1500)
            otp_filled = _fill_text_field(
                page,
                (
                    "input[name='otp']", "input[name='code']", "input[name='otpCode']",
                    "input[autocomplete='one-time-code']",
                    "input[type='text'][maxlength='6']",
                    "input[placeholder*='code' i]", "input[placeholder*='OTP' i]",
                    "input[inputmode='numeric']",
                ),
                otp, "OTP", log_fn=self.log,
            )
            if not otp_filled:
                # 兜底：找所有可见 text/tel/number input 填第一个
                try:
                    inputs = page.locator("input[type='text'], input[type='tel'], input[type='number']").all()
                    for inp in inputs:
                        try:
                            if inp.is_visible():
                                inp.fill(otp, timeout=5000)
                                otp_filled = True
                                self.log("[promptql] 兜底填 OTP 到第一个可见 input")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass
            if not otp_filled:
                result["stages"].append({"stage": "fill_otp", "ok": False})
                raise RuntimeError("未找到 PromptQL OTP 输入框")
            result["stages"].append({"stage": "fill_otp", "ok": True})

            # 8. click verify
            page.wait_for_timeout(800)
            verify_clicked = _click_button(
                page,
                (
                    "button:has-text('Verify')",
                    "button:has-text('Continue')",
                    "button:has-text('Submit')",
                    "button:has-text('Confirm')",
                    "button[type='submit']",
                    "form button[type='submit']",
                ),
                log_fn=self.log,
            )
            if not verify_clicked:
                try:
                    page.keyboard.press("Enter")
                    verify_clicked = "Enter"
                except Exception:
                    pass
            if not verify_clicked:
                result["stages"].append({"stage": "click_verify", "ok": False})
                raise RuntimeError("未找到 PromptQL verify 按钮")
            result["stages"].append({"stage": "click_verify", "ok": True, "sel": verify_clicked})

            # 9. 等 /otp/verify 响应或跳 dashboard（最多 40s）
            for _ in range(40):
                st = _page_state(page)
                if captured["verify_status"] or st["dashboard"]:
                    break
                page.wait_for_timeout(1000)
            st = _page_state(page)
            verify_ok = (captured["verify_status"] == 200) or st["dashboard"]
            self.log(
                f"[promptql] /otp/verify status={captured['verify_status']} "
                f"dashboard={st['dashboard']} url={st['url'][:80]}"
            )
            result["stages"].append({
                "stage": "verify_otp", "ok": verify_ok,
                "verify_status": captured["verify_status"], "state": st,
            })
            result["verify_result"] = {
                "status": captured["verify_status"],
                "body": captured["verify_body"],
                "final_url": st["url"],
            }

            if captured["verify_status"] and captured["verify_status"] != 200 and not st["dashboard"]:
                raise RuntimeError(f"PromptQL /otp/verify 被拒 status={captured['verify_status']}: {st['text'][:200]}")

            # 10. 读 session cookie 作 access_token
            cookies = ctx.cookies()
            result["cookies"] = {c["name"]: c["value"] for c in cookies}
            session_cookie = ""
            # 优先精确名匹配（Hasura OIDC / Next.js 常见 session cookie 名）
            preferred = (
                "__session", "session", "__Secure-session", "sb-access-token",
                "access_token", "auth", "token", "hasura-auth-session",
            )
            for c in cookies:
                if c["name"] in preferred:
                    session_cookie = c["value"]
                    self.log(f"[promptql] 选中 session cookie (name={c['name']})")
                    break
            if not session_cookie:
                # 模糊名匹配
                for c in cookies:
                    lname = str(c["name"]).lower()
                    if "session" in lname or "auth" in lname or "token" in lname:
                        session_cookie = c["value"]
                        self.log(f"[promptql] 选中 session cookie (name={c['name']} 模糊匹配)")
                        break
            if not session_cookie and cookies:
                # 兜底取最长 cookie 值
                longest = max(cookies, key=lambda c: len(str(c.get("value") or "")))
                session_cookie = str(longest.get("value") or "")
                self.log(f"[promptql] 兜底取最长 cookie (name={longest['name']} len={len(session_cookie)})")

            if session_cookie:
                result["access_token"] = session_cookie
                result["token"] = session_cookie
                result["session_cookie"] = session_cookie
                result["email_confirmed"] = True
                self.log(f"[promptql] 拿到 session cookie={session_cookie[:16]}...")
            else:
                result["email_confirmed"] = bool(verify_ok)
                self.log(f"[promptql] 未取到 session cookie（verify_ok={verify_ok}）")

        result["status"] = "registered" if result.get("access_token") else (
            "pending" if result.get("email_confirmed") else "failed"
        )
        result["registered"] = bool(result.get("access_token"))
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log(
            f"[promptql] [OK] 注册 {email} token={str(result.get('access_token') or '')[:16]}..."
        )
        return result
