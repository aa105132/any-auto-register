"""CodeBanana 协议邮箱注册 worker。"""

from __future__ import annotations

import random
import string
from typing import Callable, Optional

from platforms.codebanana.core import CodeBananaClient


_USERNAME_CHARS = string.ascii_lowercase + string.digits


class CodeBananaProtocolMailboxWorker:
    def __init__(
        self,
        *,
        base_url: str = "https://www.codebanana.com",
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.client = CodeBananaClient(base_url=base_url, proxy=proxy, log_fn=log_fn)
        self.log = log_fn

    def _generate_username(self, length: int = 12) -> str:
        if length < 3:
            length = 3
        return "cb" + "".join(random.choice(_USERNAME_CHARS) for _ in range(length - 2))

    def _reserve_username(self, max_attempts: int = 5) -> str:
        last_error: Exception | None = None
        for _ in range(max_attempts):
            username = self._generate_username()
            try:
                if self.client.ensure_username_available(username):
                    return username
            except ValueError as exc:
                last_error = exc
                continue
        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"CodeBanana 连续 {max_attempts} 次生成用户名都不可用{detail}")

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> dict:
        username = self._reserve_username()
        self.client.send_verification_code(email=email, username=username)
        otp = otp_callback() if otp_callback else ""
        if not otp:
            raise RuntimeError("未获取到 CodeBanana 验证码")
        self.log(f"CodeBanana 验证码: {otp}")

        register_payload = self.client.verify_and_register(
            email=email,
            username=username,
            password=password,
            code=otp,
        )
        login_payload = self.client.login_and_fetch_session(email=email, password=password)
        session_json = dict(login_payload.get("session_json") or {})
        session_user = dict(session_json.get("user") or {})
        registered_user = dict(register_payload.get("user") or {})

        return {
            "email": email,
            "password": password,
            "username": username,
            "user_id": str(
                register_payload.get("userId")
                or register_payload.get("user_id")
                or registered_user.get("id")
                or session_user.get("id")
                or ""
            ),
            "session_token": login_payload["session_token"],
            "jwtToken": session_json.get("jwtToken", ""),
            "cookies": login_payload["cookies"],
            "session_json": session_json,
            "csrf_token": login_payload["csrf_token"],
        }
