"""CodeWords 浏览器注册 (Playwright / Chrome Profile / CDP)。

支持两种模式:
  - browser mailbox: 浏览器打开 → 填邮箱 → Turnstile → 点发送 → 等邮箱链接
  - browser OAuth:   浏览器打开 → 点 Google → OAuth 登录 → callback → session token
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core.oauth_browser import (
    OAuthBrowser,
    browser_login_method_text,
    finalize_oauth_email,
)
from core.google_oauth import drive_google_oauth
from platforms.codewords.core import CodewordsClient


class CodewordsBrowserRegister:
    """Playwright 浏览器邮箱注册 worker。"""

    def __init__(
        self,
        *,
        captcha: Any = None,
        headless: bool = True,
        proxy: str | None = None,
        verification_link_callback: Callable[[], str] | None = None,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.captcha = captcha
        self.headless = headless
        self.proxy = proxy
        self.verification_link_callback = verification_link_callback
        self.log = log_fn or (lambda _: None)

    def _log(self, msg: str) -> None:
        try:
            self.log(f"[CodeWords Browser] {msg}")
        except Exception:
            pass

    def run(self, *, email: str) -> dict[str, Any]:
        import requests as _requests

        client = CodewordsClient(proxy=self.proxy, log_fn=self.log)
        csrf_token = client.get_csrf_token()

        if self.captcha is None:
            raise RuntimeError("CodeWords 浏览器注册需要 captcha")

        self._log("解决 Turnstile...")
        turnstile_token = self.captcha.solve_turnstile(
            f"{CodewordsClient.BASE_URL}/login",
            CodewordsClient.TURNSTILE_SITEKEY,
        )
        if not turnstile_token or turnstile_token == "CAPTCHA_FAIL":
            raise RuntimeError("Turnstile 解决失败")

        client.verify_turnstile(email, turnstile_token)
        client.send_verification_email(email, csrf_token)

        if not self.verification_link_callback:
            raise RuntimeError("未配置邮箱验证链接回调")
        link = self.verification_link_callback()
        if not link:
            raise RuntimeError("未收到验证链接")

        cookies = client.visit_verification_link(link)
        session_token = CodewordsClient.extract_session_token(cookies)
        session_data = client.get_session()
        resolved_email = CodewordsClient.ensure_authenticated_session(session_data)
        user = session_data.get("user") or {}
        if not session_token:
            raise RuntimeError("CodeWords 邮箱登录未拿到真实 Session Token")

        return {
            "email": resolved_email or email,
            "password": "",
            "user_id": str(user.get("email") or user.get("id") or email),
            "token": session_token,
            "session": session_data,
            "cookies": cookies,
        }


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    timeout: int = 300,
    log_fn: Callable[[str], None] = print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    google_password: str = "",
) -> dict[str, Any]:
    """通过浏览器 Google OAuth 完成 CodeWords 注册/登录。

    使用 OAuthBrowser 打开 CodeWords 登录页, 点击 Google OAuth,
    等待完成登录后提取 session token。
    """
    method_text = browser_login_method_text(oauth_provider)
    resolved_email = ""

    with OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        log_fn=log_fn,
    ) as browser:
        browser.goto(f"{CodewordsClient.BASE_URL}/login")
        time.sleep(2)
        if oauth_provider and not browser.try_click_provider(oauth_provider):
            browser.goto(f"{CodewordsClient.BASE_URL}/login")
            time.sleep(2)
            browser.try_click_provider(oauth_provider)

        if (oauth_provider or "google").strip().lower() == "google":
            drive_google_oauth(
                browser,
                email=email_hint,
                password=google_password,
                timeout=min(timeout, 180),
                log_fn=log_fn,
                stop_when=lambda b: bool(b.cookie_value(
                    "__Secure-authjs.session-token",
                    "authjs.session-token",
                    "next-auth.session-token",
                    "__Secure-next-auth.session-token",
                    domain_substrings=("codewords.agemo.ai", "codewords.ai", ".agemo.ai"),
                )),
            )
        if chrome_user_data_dir or chrome_cdp_url:
            browser.auto_select_google_account()
        else:
            log_fn(
                f"请在浏览器中完成登录，可使用 {method_text}，最长等待 {timeout} 秒"
            )
            if email_hint:
                log_fn(f"请确认最终登录账号邮箱为: {email_hint}")

        # Wait for session token cookie from codewords domain
        session_cookie = browser.wait_for_cookie_value(
            [
                "__Secure-authjs.session-token",
                "authjs.session-token",
                "next-auth.session-token",
                "__Secure-next-auth.session-token",
            ],
            timeout=timeout,
            domain_substrings=("codewords.agemo.ai", "codewords.ai", ".agemo.ai"),
        )

        if not session_cookie:
            # Try getting all cookies from the domain
            all_cookies = browser.cookie_dict(
                domain_substrings=("codewords.agemo.ai", "codewords.ai")
            )
            session_cookie = CodewordsClient.extract_session_token(all_cookies)

        if not session_cookie:
            raise RuntimeError(
                f"CodeWords 浏览器登录未在 {timeout} 秒内拿到 Session Token"
            )

        # Get cookies and session info
        all_cookies = browser.cookie_dict(
            domain_substrings=("codewords.agemo.ai", "codewords.ai")
        )

        # Try to resolve email from session; empty session is not a success.
        import requests as _requests
        try:
            s = _requests.Session()
            s.headers.update({
                "User-Agent": CodewordsClient.UA,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": f"{CodewordsClient.BASE_URL}/",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "sec-ch-ua": '"Chromium";v="146", "Google Chrome";v="146", "Not_A Brand";v="99"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
            })
            for k, v in all_cookies.items():
                s.cookies.set(k, str(v), domain="codewords.agemo.ai")
            r = s.get(
                f"{CodewordsClient.BASE_URL}/api/auth/session", timeout=15
            )
            session_data = r.json() if r.ok else {}
            resolved_email = CodewordsClient.ensure_authenticated_session(session_data)
        except Exception as exc:
            raise RuntimeError(f"CodeWords OAuth 未形成有效登录会话: {exc}") from exc

        resolved_email = finalize_oauth_email(resolved_email, email_hint, "CodeWords")

        return {
            "email": resolved_email,
            "password": "",
            "token": session_cookie,
            "session": session_data,
            "cookies": all_cookies,
        }