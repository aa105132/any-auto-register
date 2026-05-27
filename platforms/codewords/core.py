"""CodeWords (codewords.agemo.ai) 协议客户端。

Auth.js v5 邮箱验证链接 + Google OAuth 双路径流程:

Magic Link 路径:
  1. GET  /api/auth/csrf            → csrfToken
  2. 解 Turnstile (sitekey: 0x4AAAAAADMJ8CJc5hLBHf2s)
  3. POST /api/auth/turnstile       → 验证 Turnstile, 获得 cw_ts cookie
  4. POST /api/auth/signin/email    → 发验证邮件
  5. 邮箱提取验证链接
  6. GET  验证链接                  → 完成登录, 写 session cookie
  7. GET  /api/auth/session         → 验证已登录

Google OAuth 路径:
  使用标准 browser OAuth → NextAuth callback → session
"""

from __future__ import annotations

from typing import Any, Callable

import requests as _requests


class CodewordsClient:
    BASE_URL = "https://codewords.agemo.ai"
    TURNSTILE_SITEKEY = "0x4AAAAAADMJ8CJc5hLBHf2s"
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        proxy: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        session: _requests.Session | None = None,
    ) -> None:
        self.proxy = proxy
        self.log_fn = log_fn or (lambda _: None)
        self.session = session or _requests.Session()
        self.session.headers.update({
            "User-Agent": self.UA,
            "Origin": self.BASE_URL,
        })
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _log(self, msg: str) -> None:
        try:
            self.log_fn(f"[CodeWords] {msg}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 步骤 1: 获取 CSRF token
    # ------------------------------------------------------------------
    def get_csrf_token(self) -> str:
        self._log("获取 CSRF token...")
        r = self.session.get(f"{self.BASE_URL}/api/auth/csrf", timeout=15)
        r.raise_for_status()
        token = str(r.json().get("csrfToken", ""))
        if not token:
            raise RuntimeError("未获取到 CSRF token")
        self._log(f"CSRF: {token[:8]}...")
        return token

    # ------------------------------------------------------------------
    # 步骤 3: 验证 Turnstile (获取 cw_ts cookie)
    # ------------------------------------------------------------------
    def verify_turnstile(self, email: str, turnstile_token: str) -> dict[str, Any]:
        self._log("验证 Turnstile...")
        r = self.session.post(
            f"{self.BASE_URL}/api/auth/turnstile",
            json={"email": email, "token": turnstile_token},
            headers={
                "Content-Type": "application/json",
                "Referer": f"{self.BASE_URL}/login",
            },
            timeout=30,
        )
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        cw_ts = self.session.cookies.get("cw_ts", "")
        self._log(f"Turnstile ok={data.get('ok')} mode={data.get('mode')} cw_ts={'有' if cw_ts else '无'}")
        return data

    # ------------------------------------------------------------------
    # 步骤 4: 发送验证邮件
    # ------------------------------------------------------------------
    def send_verification_email(self, email: str, csrf_token: str) -> dict[str, Any]:
        self._log(f"发送验证邮件到 {email}...")
        r = self.session.post(
            f"{self.BASE_URL}/api/auth/signin/email",
            data={
                "email": email,
                "csrfToken": csrf_token,
                "callbackUrl": f"{self.BASE_URL}/",
                "redirect": "false",
                "json": "true",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": f"{self.BASE_URL}/login",
            },
            timeout=30,
        )
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        url = str(data.get("url", ""))
        if "error=AccessDenied" in url:
            raise RuntimeError(
                "CodeWords 邮箱登录未在服务端配置 (AccessDenied)。"
                " 请使用 Google OAuth 路径注册。"
            )
        self._log(f"验证邮件已发送: {url[:80] if url else 'OK'}")
        return data

    # ------------------------------------------------------------------
    # 步骤 6: 访问验证链接完成登录
    # ------------------------------------------------------------------
    def visit_verification_link(self, link: str) -> dict[str, str]:
        self._log(f"访问验证链接: {link[:80]}...")
        r = self.session.get(link, allow_redirects=True, timeout=15)
        r.raise_for_status()
        cookies = dict(self.session.cookies.get_dict())
        self._log(f"session cookies: {list(cookies.keys())}")
        return cookies

    # ------------------------------------------------------------------
    # 步骤 7: 获取会话信息
    # ------------------------------------------------------------------
    def get_session(self) -> dict[str, Any]:
        self._log("获取会话信息...")
        r = self.session.get(f"{self.BASE_URL}/api/auth/session", timeout=15)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        user = data.get("user") or {}
        self._log(f"会话用户: {user.get('email', '(无)')}")
        return data

    # ------------------------------------------------------------------
    # 便捷方法: 提取 session token
    # ------------------------------------------------------------------
    @staticmethod
    def extract_session_token(cookies: dict[str, str]) -> str:
        """只提取真实 Auth.js/NextAuth session cookie。

        不能在缺失 session cookie 时退回到任意普通 cookie；否则 csrf、
        tracking cookie 会被误判成登录成功，OAuth 注册链路会产生假阳性。
        """
        for key in ("__Secure-authjs.session-token", "authjs.session-token",
                     "next-auth.session-token", "__Secure-next-auth.session-token"):
            value = str(cookies.get(key) or "")
            if value:
                return value
        return ""

    @staticmethod
    def session_email(session_data: dict[str, Any]) -> str:
        """从 /api/auth/session 响应中提取已认证邮箱。"""
        if not isinstance(session_data, dict):
            return ""
        user = session_data.get("user") or {}
        if not isinstance(user, dict):
            return ""
        return str(user.get("email") or "").strip()

    @classmethod
    def ensure_authenticated_session(cls, session_data: dict[str, Any]) -> str:
        """要求会话中存在 user.email，否则视为未登录。"""
        email = cls.session_email(session_data)
        if not email:
            raise RuntimeError("CodeWords 会话未认证：/api/auth/session 未返回 user.email")
        return email

    # ------------------------------------------------------------------
    # 便捷方法: 从 curl_cffi 兼容
    # ------------------------------------------------------------------
    def _try_impersonate(self, email: str, password: str = "") -> _requests.Session:
        """尝试用 curl_cffi 做 TLS 指纹伪装 (如果可用)"""
        try:
            from curl_cffi import requests as _curl_req  # type: ignore[import-untyped]
            sess: Any = _curl_req.Session(impersonate="chrome131")
            for k, v in self.session.headers.items():
                sess.headers[k] = v
            for k, v in self.session.cookies.items():
                sess.cookies.set(k, v)
            if self.proxy:
                sess.proxies.update({"http": self.proxy, "https": self.proxy})
            return sess
        except Exception:
            return self.session