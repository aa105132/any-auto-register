"""Hex 浏览器驱动注册 Worker（patchright + 住宅代理 + magic link 确认链接，无密码无 captcha）。

流程：
  1. patchright 打开 https://app.hex.tech/signup。
  2. 填 email + name → click 发 magic link → POST /auth/magic/signup{email,name} → {success}
     （app.hex.tech 自有 magic-link，见 core.py HexClient.magic_signup）。
  3. mailbox.wait_for_link(keyword="Hex") 收 magic link 邮件链接（before_ids 基线避免旧邮件）。
  4. patchright navigate magic link → /auth/magic/callback → session cookie。
  5. 拿 session cookie 作 token（无密码无 captcha，email-only magic link）。

注：hex 无 captcha widget，magic-link 确认链接非 OTP，用 wait_for_link 提取。
name 字段可经 extra.hex_name 配置（默认 "Auto Register"）。
"""
from __future__ import annotations

# BLAS 单线程（Windows OOM 防护），必须在 playwright import 前设。
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from platforms.hex.core import APP_URL, HexClient, log

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

SIGNUP_URL = f"{APP_URL}/signup"


def _wait_magic_link(mailbox, account, *, keyword: str = "Hex", timeout: int = 240,
                     before_ids: set | None = None, log_fn=print) -> str:
    """等 hex magic link 邮件链接。keyword='Hex' 过滤，wait_for_link 提取 magic link。"""
    log_fn(f"[hex] 等待 magic link 邮件链接 (keyword={keyword!r} timeout={timeout}s)...")
    try:
        link = mailbox.wait_for_link(
            account, keyword=keyword, timeout=timeout, before_ids=before_ids,
        )
        return link or ""
    except Exception as exc:
        log_fn(f"[hex] 收 magic link 异常: {exc!r}")
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
                log_fn(f"[hex] 已填 {label} (sel={sel})")
                return True
        except Exception:
            continue
    log_fn(f"[hex] 未找到 {label} 输入框")
    return False


def _click_send_magic_link(page, log_fn=print) -> bool:
    """点 发 magic link / Sign up / Continue 按钮。"""
    for sel in ("button:has-text('Send magic link')",
                "button:has-text('Sign up')",
                "button:has-text('Continue')",
                "button:has-text('Get started')",
                "button[type='submit']",
                "form button[type='submit']"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                log_fn(f"[hex] 点击 发 magic link (sel={sel})")
                return True
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        log_fn("[hex] 兜底 Enter 提交")
        return True
    except Exception:
        return False


def _page_state(page) -> dict:
    """读页面状态：url/magic_sent/callback/dashboard/错误。"""
    try:
        txt = page.inner_text("body", timeout=3000)
    except Exception:
        txt = ""
    low = txt.lower()
    url = (page.url or "").lower()
    return {
        "url": page.url or "",
        "text": txt,
        "magic_sent": ("check your email" in low) or ("magic link" in low) or ("we sent" in low) or ("check your inbox" in low),
        "callback": "/auth/magic/callback" in url,
        "dashboard": ("/home" in url) or ("/projects" in url) or ("/workspaces" in url),
        "login": "/login" in url and "magic" not in url,
        "signup": "/signup" in url,
        "error": ("error" in low) or ("invalid" in low) or ("already" in low) or ("failed" in low) or ("enter a valid" in low),
    }


class HexProtocolMailboxWorker:
    """Hex 浏览器驱动注册 Worker（patchright + magic link + session cookie）。

    构造参数兼容框架 ProtocolMailboxAdapter（link_callback 由框架注入，hex 用 wait_for_link
    直接收链接，link_callback 仅作兜底）。run(email, name, mailbox, mailbox_account) 执行完整链路。
    """

    def __init__(self, *, proxy: str | None = None, timeout: int = 300,
                 log_fn=print, otp_callback: Callable[[], str] | None = None,
                 captcha_solver=None, **_kwargs):
        self.proxy = proxy
        self.timeout = timeout
        self.otp_callback = otp_callback  # 兼容框架，hex 用 wait_for_link 不用此
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

    def run(self, *, email: str = "", name: str = "Auto Register",
            link_callback: Callable[[], str] | None = None,
            mailbox=None, mailbox_account=None) -> dict[str, Any]:
        """执行 Hex 注册。email/name 由框架注入；mailbox/mailbox_account 框架按 mail_provider 建好传入。"""
        from core.base_mailbox import MailboxAccount

        if not email:
            raise RuntimeError("Hex 注册缺少邮箱")
        if mailbox is None or mailbox_account is None:
            raise RuntimeError("Hex 注册需 mailbox + mailbox_account（框架按 mail_provider 注入）")

        account = mailbox_account
        before_ids = mailbox.get_current_ids(account) if mailbox is not None else set()
        self.log(f"[hex] 使用邮箱: {email} name={name} | 注册前邮件基线: {len(before_ids)} 封")

        result: dict[str, Any] = {
            "email": email,
            "name": name,
            "password": "",  # magic-link 无密码
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

            # 1. 打开 signup
            opened = False
            for _g in range(3):
                try:
                    page.goto(SIGNUP_URL, wait_until="commit", timeout=60000)
                    opened = True
                    break
                except Exception as exc:
                    self.log(f"[hex] goto signup 失败重试 {_g+1}: {str(exc)[:120]}")
                    page.wait_for_timeout(2000)
            if not opened:
                raise RuntimeError(f"打开 signup 多次失败: {SIGNUP_URL}")
            page.wait_for_timeout(4000)
            st = _page_state(page)
            self.log(f"[hex] signup 页 url={st['url'][:80]}")

            # 2. 填 email + name + click 发 magic link
            # name 字段可能不存在（部分 magic-link 表单只要 email），填不到不报错
            _fill_text_field(page, ("input[name='name']", "input[autocomplete='name']", "input[placeholder*='Name' i]", "input#name"), name, "Name", log_fn=self.log)
            if not _fill_text_field(page, ("input[type='email']", "input[name='email']", "input#email", "input[autocomplete='email']"), email, "Email", log_fn=self.log):
                result["stages"].append({"stage": "fill_signup", "ok": False, "state": st})
                raise RuntimeError("未找到 Hex 邮箱输入框")
            page.wait_for_timeout(700)
            if not _click_send_magic_link(page, log_fn=self.log):
                result["stages"].append({"stage": "send_magic_link", "ok": False})
                raise RuntimeError("未找到 Hex 发 magic link 按钮")
            result["stages"].append({"stage": "send_magic_link", "ok": True})

            # 等发 magic link 成功（"check your email" 状态）或 callback/dashboard
            page.wait_for_timeout(4000)
            st = _page_state(page)
            self.log(f"[hex] 发 magic link 后 url={st['url'][:80]} magic_sent={st['magic_sent']} error={st['error']}")
            if not st["magic_sent"] and not st["callback"] and not st["dashboard"]:
                for _ in range(8):
                    if st["magic_sent"] or st["callback"] or st["dashboard"]:
                        break
                    page.wait_for_timeout(2500)
                    st = _page_state(page)
            if st["error"] and not st["magic_sent"] and not st["dashboard"]:
                result["stages"].append({"stage": "magic_link_sent", "ok": False, "state": st})
                raise RuntimeError(f"发 magic link 被拒: {st['text'][:200]}")
            result["stages"].append({"stage": "magic_link_sent", "ok": True})
            result["signup_result"] = {"magic_sent": st["magic_sent"], "state_url": st["url"]}

            # 3. 收 magic link 邮件链接
            link = _wait_magic_link(
                mailbox, account, keyword="Hex",
                timeout=min(self.timeout, 240), before_ids=before_ids, log_fn=self.log,
            )
            if not link:
                # 兜底：用框架注入的 link_callback（otp_callback 槽）
                if callable(self.otp_callback):
                    try:
                        link = self.otp_callback() or ""
                    except Exception as exc:
                        self.log(f"[hex] otp_callback 兜底异常: {exc!r}")
            if not link:
                result["stages"].append({"stage": "wait_magic_link", "ok": False})
                raise RuntimeError("未收到 Hex magic link 邮件链接")
            result["stages"].append({"stage": "wait_magic_link", "ok": True, "link": link[:80]})

            # 4. navigate magic link → /auth/magic/callback → session cookie
            try:
                page.goto(link, wait_until="commit", timeout=60000)
                page.wait_for_timeout(6000)
            except Exception as exc:
                self.log(f"[hex] navigate magic link 异常（可能跳 app.hex.tech 深链）: {str(exc)[:120]}")
            st2 = _page_state(page)
            self.log(f"[hex] magic link 回调后 url={st2['url'][:80]} dashboard={st2['dashboard']} callback={st2['callback']}")
            result["email_confirmed"] = True
            result["callback_result"] = {"final_url": st2["url"], "dashboard": st2["dashboard"], "callback": st2["callback"]}
            result["stages"].append({"stage": "magic_callback", "ok": True, "state": st2})

            # 5. 拿 session cookie
            try:
                cookies = ctx.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}
                result["session_cookies"] = cookie_dict
                # 选主 session cookie 作 token：优先常见 session cookie 名，否则取最长，否则 JSON 全量
                token = ""
                for cand in ("sb-app.hex.tech-auth-token", "__session", "session", "sb", "_hex_session", "hex_session", "hex-auth-token"):
                    if cand in cookie_dict and cookie_dict[cand]:
                        token = cookie_dict[cand]
                        break
                if not token and cookie_dict:
                    token = max(cookie_dict.values(), key=len)
                if not token:
                    token = json.dumps(cookie_dict, ensure_ascii=False)
                result["token"] = token
                result["session_token"] = token
                result["api_key"] = token
                self.log(f"[hex] 拿到 session cookies={len(cookie_dict)} 个 token={token[:16]}...")
                result["stages"].append({"stage": "session_cookies", "ok": True, "count": len(cookie_dict)})
            except Exception as exc:
                self.log(f"[hex] 拿 session cookie 异常: {exc!r}")
                result["stages"].append({"stage": "session_cookies", "ok": False, "error": str(exc)[:200]})

        result["status"] = "registered"
        result["registered"] = True
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log(f"[hex] [OK] 注册成功 {email} session_cookies={len(result.get('session_cookies') or {})} 个")
        return result
