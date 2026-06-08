"""Fireworks AI 协议邮箱注册 worker。"""

from __future__ import annotations

import time
from typing import Callable

from platforms.fireworks.core import FireworksClient


class FireworksProtocolMailboxWorker:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.client = FireworksClient(proxy=proxy, log_fn=log_fn)
        self.log = log_fn

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Callable[[], str] | None = None,
        verification_link_callback: Callable[[], str] | None = None,
    ) -> dict:
        # 1. 注册
        self.client.signup(email=email, password=password)

        # 2. 等待邮件验证链接
        if not verification_link_callback:
            raise RuntimeError("Fireworks 注册需要验证链接回调，但未提供 verification_link_callback")
        verify_url = verification_link_callback()
        if not verify_url:
            raise RuntimeError("Fireworks: 未获取到验证链接")

        # 3. 验证邮箱
        self.client.verify_email(verify_url)

        # 4. 登录
        login_result = self.client.login(email=email, password=password)
        cookies = login_result.get("cookies", {})

        # 5. Onboarding - 完成账户创建
        account_id = email.split("@")[0][:15] + str(int(time.time()))[-4:]
        try:
            onboarding_result = self.client.onboarding(account_id=account_id)
            if not onboarding_result.get("success"):
                self.log(f"Fireworks onboarding 未返回 hasAccount=True，继续...")
        except Exception as exc:
            self.log(f"Fireworks onboarding 失败（非阻塞）: {exc}")

        # 6. 获取账户信息
        try:
            account_info = self.client.get_account_info()
        except Exception:
            account_info = {}

        # 7. 创建 API key
        try:
            api_key_info = self.client.create_api_key(name="auto-register")
            api_key = api_key_info.get("api_key", "")
        except Exception as exc:
            self.log(f"Fireworks API key 创建失败（非阻塞）: {exc}")
            api_key = ""
            api_key_info = {}

        return {
            "email": email,
            "password": password,
            "user_id": account_info.get("user_id", ""),
            "api_key": api_key,
            "api_key_info": api_key_info,
            "account_info": account_info,
            "cookies": cookies,
            "session_cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v),
            "account_id": account_info.get("account_id", ""),
        }