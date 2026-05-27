"""Grok 协议邮箱注册 worker。"""
from __future__ import annotations

from typing import Callable, Optional

from platforms.grok.core import GrokRegister, _rand_name, _rand_password


class GrokProtocolMailboxWorker:
    def __init__(
        self,
        *,
        captcha_solver=None,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        use_cdp_bridge: bool = False,
        chrome_cdp_url: str = "",
        chrome_user_data_dir: str = "",
    ):
        self.client = GrokRegister(
            captcha_solver=captcha_solver,
            proxy=proxy,
            log_fn=log_fn,
            use_cdp_bridge=use_cdp_bridge,
            chrome_cdp_url=chrome_cdp_url,
            chrome_user_data_dir=chrome_user_data_dir,
        )
        self.log = log_fn
        self.use_cdp_bridge = bool(use_cdp_bridge)

    def run(
        self,
        *,
        email: str,
        password: str | None = None,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> dict:
        use_password = password or _rand_password()
        given_name = _rand_name()
        family_name = _rand_name()

        self.client.step1_send_otp(email)
        code = otp_callback() if otp_callback else input("验证码: ")
        if not code:
            raise RuntimeError("未获取到验证码")

        if not self.client.step2_verify_otp(email, code):
            raise RuntimeError("Grok 邮箱验证码校验失败")
        signup_body = self.client.step3_signup(email, use_password, code, given_name, family_name)
        self.client.step4_set_cookies(signup_body)

        cookies = self.client.cookies
        sso = cookies.get("sso", "")
        if sso:
            self.log(f"  [OK] sso={sso[:40]}...")
        else:
            self.log("  ⚠️ 未获取到 sso cookie")

        return {
            "email": email,
            "password": use_password,
            "given_name": given_name,
            "family_name": family_name,
            "sso": sso,
            "sso_rw": cookies.get("sso-rw", ""),
            "cookies": cookies,
            "cookie_header": self.client.cookie_header,
            "cdp_bootstrap": self.client.cdp_bootstrap_result,
        }
