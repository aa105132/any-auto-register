"""LemonData 系统邮箱 Magic Link 注册 worker。"""
from __future__ import annotations

from typing import Any, Callable

from platforms.lemondata.core import DASHBOARD_URL, SIGNIN_URL, TURNSTILE_SITEKEY, LemonDataClient


class LemonDataProtocolMailboxWorker:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        use_cdp_bridge: bool = False,
    ) -> None:
        self.client = LemonDataClient(proxy=proxy, log_fn=log_fn)
        self.log = log_fn
        self.use_cdp_bridge = use_cdp_bridge

    def run(
        self,
        *,
        email: str,
        password: str = "",
        captcha_solver: Any = None,
        verification_link_callback: Callable[[], str] | None = None,
    ) -> dict:
        # 1. 纯 HTTP 探测 captcha policy；cdp_protocol 时只用 CDP 获取挑战 token。
        captcha_result: dict = {}
        cdp_bootstrap: dict = {}
        if self.client.captcha_required():
            if self.use_cdp_bridge:
                cdp_bootstrap = self.client.bootstrap_cdp_challenge(captcha_solver)
                turnstile_token = str(cdp_bootstrap.get("turnstile_token") or "").strip()
            else:
                if not captcha_solver or not hasattr(captcha_solver, "solve_turnstile"):
                    raise RuntimeError("LemonData 需要 Turnstile token，但未配置 captcha_solver")
                turnstile_token = str(captcha_solver.solve_turnstile(SIGNIN_URL, TURNSTILE_SITEKEY) or "").strip()
            captcha_result = self.client.verify_captcha(email=email, token=turnstile_token)

        # 2. Auth.js Email Provider 发送系统邮箱 Magic Link。
        signin_result = self.client.send_email_signin(email=email, callback_url=DASHBOARD_URL)
        self.log("LemonData magic link 已发送，等待邮箱验证链接...")

        # 3. 邮箱收取链接，HTTP 访问 callback 完成登录 session 写入。
        if not verification_link_callback:
            raise RuntimeError("LemonData 注册需要验证链接回调，但未提供 verification_link_callback")
        verification_link = verification_link_callback()
        if not verification_link:
            raise RuntimeError("LemonData: 未获取到验证链接")
        visit_result = self.client.visit_verification_link(verification_link)

        # 4. 登录后优先 HTTP 创建/提取 API key。
        session = self.client.get_session()
        key_create_result = self.client.create_or_find_api_key(name="auto-register")
        api_key = str(key_create_result.get("api_key") or "").strip()
        api_verification = self.client.verify_api_key(api_key)
        balance_result = self.client.require_min_balance(min_amount=1.0)

        return {
            "email": email,
            "password": password,
            "api_key": api_key,
            "api_key_info": key_create_result.get("api_key_info") or {},
            "api_verification": api_verification,
            "balance_result": balance_result,
            "key_create_result": key_create_result,
            "captcha_result": captcha_result,
            "cdp_bootstrap": cdp_bootstrap,
            "signin_result": signin_result,
            "visit_result": visit_result,
            "session": session.get("data") or {},
            "account_info": session.get("data") or {},
            "cookies": self.client.cookies,
            "cookie_header": self.client.cookie_header,
        }
