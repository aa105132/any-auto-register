"""CodeWords 协议邮箱注册 Worker。

依赖:
  - captcha: BaseCaptcha 实例 (solve_turnstile 方法)
  - verification_link_callback: 从邮箱获取验证链接的回调函数

流程:
  1. GET  /api/auth/csrf            → csrfToken
  2. 解 Turnstile                   → turnstile token
  3. POST /api/auth/turnstile       → 验证 Turnstile, 获得 cw_ts cookie
  4. POST /api/auth/signin/email    → 触发验证邮件
  5. 轮询邮箱                       → 获取验证链接
  6. GET  验证链接                  → 完成登录
  7. GET  /api/auth/session         → 验证会话
"""

from __future__ import annotations

from typing import Any, Callable

from platforms.codewords.core import CodewordsClient


class CodewordsProtocolMailboxWorker:
    def __init__(
        self,
        client: CodewordsClient | None = None,
        captcha: Any = None,
        proxy: str | None = None,
        log_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client or CodewordsClient(proxy=proxy, log_fn=log_fn)
        self.captcha = captcha
        self.log = log_fn or (lambda _: None)
        self.proxy = proxy

    def _log(self, msg: str) -> None:
        try:
            self.log(f"[CodeWords] {msg}")
        except Exception:
            pass

    def run(
        self,
        *,
        email: str,
        verification_link_callback: Callable[[], str] | None = None,
    ) -> dict[str, Any]:
        # ---- 步骤 1: CSRF token -------------------------------------------
        csrf_token = self.client.get_csrf_token()

        # ---- 步骤 2: Turnstile --------------------------------------------
        if self.captcha is None:
            raise RuntimeError("CodeWords 需要验证码解决器 (captcha)")
        self._log("解决 Turnstile...")
        turnstile_token = self.captcha.solve_turnstile(
            f"{self.client.BASE_URL}/login",
            self.client.TURNSTILE_SITEKEY,
        )
        if not turnstile_token or turnstile_token == "CAPTCHA_FAIL":
            raise RuntimeError("Turnstile 解决失败")
        self._log(f"Turnstile token: {turnstile_token[:12]}...")

        # ---- 步骤 3: 验证 Turnstile (获取 cw_ts cookie) -------------------
        self.client.verify_turnstile(email, turnstile_token)

        # ---- 步骤 4: 发送验证邮件 -------------------------------------------
        self.client.send_verification_email(email, csrf_token)

        # ---- 步骤 5: 等待邮箱验证链接 ---------------------------------------
        if not verification_link_callback:
            raise RuntimeError("未配置邮箱验证链接回调")
        link = verification_link_callback()
        if not link:
            raise RuntimeError("未收到验证链接")

        # ---- 步骤 6: 访问验证链接完成登录 -----------------------------------
        cookies = self.client.visit_verification_link(link)
        session_token = CodewordsClient.extract_session_token(cookies)

        # ---- 步骤 7: 获取会话信息 -------------------------------------------
        session_data = self.client.get_session()
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