"""AnyCap 纯协议注册核心（Auth0 Universal Login 全 HTTP，零浏览器）。

参照 platforms/tavily/core.py 的 Auth0 纯协议链路，适配 AnyCap 的 auth.converge.ai：
  1. GET /authorize → 跟随重定向到 /u/signup/identifier?state=... → 从最终 URL 提取 state
  2. YesCaptcha.solve_turnstile(url, sitekey, proxy=...) 带代理解 Turnstile（纯 HTTP，零浏览器）
  3. POST /u/signup/identifier?state → 提交 state/email/captcha/action=default → 302 → 拿 challenge state
  4. POST /u/email-identifier/challenge?state → 提交 OTP → 拿 password state
  5. POST /u/signup/password?state → 设密码 → 拿 resume state
  6. GET /authorize/resume?state → 302 跳 redirect → 从 Location 解析 auth_code
  7. POST /oauth/token → 换 access_token（复用 _exchange_auth_code_for_tokens）
  8. POST /v1/api-keys → 创建 API key（复用 _create_api_key_http）

关键实测细节（converge.ai 租户）：
- authorize GET 跟随重定向后最终 URL 是 /u/signup/identifier?state={transaction_state}，
  state 从最终 URL 提取（不是 Location 头，因为跟随了）。
- POST 必须 allow_redirects=False（才能从 302 Location 拿下一跳 state），
  带 action=default + Origin + Referer + Content-Type: application/x-www-form-urlencoded。
- captcha 必须是真实 Turnstile token（fake token 会被拒，报 "Please enter an email address"）。
全程单个 curl_cffi Session（impersonate=chrome，保持 cookie + 走 resin 代理），
打码用 YesCaptcha 带代理，token 与注册同出口 IP，彻底换 IP。
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Callable

from platforms.anycap.browser_oauth import (
    API_BASE,
    AUTH0_AUDIENCE,
    AUTH0_CLIENT_ID,
    AUTH0_CODE_VERIFIER,
    AUTH0_DOMAIN,
    AUTH0_REDIRECT_URI,
    SITE_URL,
    _build_auth0_signup_url,
    _create_api_key_http,
    _exchange_auth_code_for_tokens,
    _verify_api_key_http,
)

# AnyCap Auth0 Universal Login Turnstile sitekey（从 auth.converge.ai signup 页 HTML 实测提取）
ANYCAP_TURNSTILE_SITEKEY = "0x4AAAAAACwSuI5jPtwnNwc5"

_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _extract_state(location: str, fallback: str = "") -> str:
    """从 302 Location 头或 URL 提取 state 参数（URL 解码）。"""
    if not location:
        return fallback
    m = re.search(r"[?&]state=([^&]+)", location)
    return urllib.parse.unquote(m.group(1)) if m else fallback


def _extract_auth_code(location: str) -> str:
    """从 redirect Location 头解析 code 参数。"""
    if not location:
        return ""
    m = re.search(r"[?&]code=([^&]+)", location)
    return urllib.parse.unquote(m.group(1)) if m else ""


class AnyCapProtocolRegister:
    """AnyCap 纯协议注册（Auth0 Universal Login，零浏览器）。"""

    def __init__(self, *, executor, captcha, proxy: str | None = None, log_fn: Callable[[str], None] = print):
        # AnyCap 纯协议用 requests.Session（标准 TLS）。实测 curl_cffi 走 resin 代理 TLS 握手
        # 失败（curl: (35) TLS connect error，BoringSSL + resin 代理兼容性问题），requests 库
        # 走 resin 代理 authorize 200 通。需要精细控制 allow_redirects（authorize 跟随、POST
        # 不跟随）+ cookies，requests.Session 完全支持。
        self.ex = executor
        self.captcha = captcha
        self.proxy = proxy
        self.log = log_fn
        import requests as _requests
        self.s = _requests.Session()
        # 完整浏览器 headers：Auth0 Universal Login 检测 Sec-Fetch-* + Sec-CH-UA 判断真浏览器
        # 表单提交（缺这些可能不触发 OTP 邮件发送）。Sec-Fetch-Site=same-origin 表示同站表单提交。
        self.s.headers.update({
            "User-Agent": _DEFAULT_UA,
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Upgrade-Insecure-Requests": "1",
        })
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.s.trust_env = False

    def _form_headers(self, url: str) -> dict[str, str]:
        """Auth0 表单 POST 的完整浏览器 headers（Sec-Fetch-Site=same-origin 触发 OTP 发送）。"""
        return {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"https://{AUTH0_DOMAIN}",
            "Referer": url,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

    def _l(self, msg: str) -> None:
        self.log(f"[AnyCap-Proto] {msg}")

    def step1_authorize(self) -> str:
        """GET /authorize?screen_hint=signup → 跟随重定向到 /u/signup/identifier?state=... → 提取 state。"""
        signup_url = _build_auth0_signup_url()
        # 跟随重定向（curl_cffi 默认跟随），最终 URL 含 transaction state
        r = self.s.get(signup_url, timeout=30)
        final_url = str(getattr(r, "url", "") or r.headers.get("location", "") or "")
        state = _extract_state(final_url)
        self._l(f"authorize → state={state[:20]}… final={final_url[:80]}")
        return state

    def step2_solve_captcha(self, page_url: str = "") -> str:
        """YesCaptcha 带代理解 Turnstile（纯 HTTP，零浏览器）。"""
        self._l(f"解 Turnstile（sitekey={ANYCAP_TURNSTILE_SITEKEY[:12]}… 带代理）")
        url = page_url or _build_auth0_signup_url()
        import inspect
        params = inspect.signature(self.captcha.solve_turnstile).parameters
        kwargs: dict[str, Any] = {}
        if "proxy" in params and self.proxy:
            kwargs["proxy"] = self.proxy
        if "user_agent" in params:
            kwargs["user_agent"] = _DEFAULT_UA
        token = str(self.captcha.solve_turnstile(url, ANYCAP_TURNSTILE_SITEKEY, **kwargs) or "").strip()
        if token:
            self._l(f"Turnstile token (len={len(token)})")
        else:
            self._l("Turnstile 打码返回空 token")
        return token

    def step3_submit_email(self, email: str, state: str, captcha_token: str) -> str:
        """POST /u/signup/identifier?state → 提交 state/email/captcha/action=default → 302 → 拿 challenge state。"""
        self._l(f"提交邮箱: {email}")
        url = f"https://{AUTH0_DOMAIN}/u/signup/identifier?state={state}"
        r = self.s.post(
            url,
            # 浏览器路径抓包：identifier POST body 只有 state/email/captcha，不带 action=default
            # （带 action=default 会导致 Auth0 不发 OTP 邮件，纯协议实测 OTP 全超时）。
            data={"state": state, "email": email, "captcha": captcha_token},
            headers=self._form_headers(url),
            allow_redirects=False,
            timeout=30,
        )
        location = r.headers.get("location", "") or ""
        next_state = _extract_state(location, state)
        self._l(f"identifier → status={r.status_code} next={location[:80]}")
        return next_state

    def step4_submit_otp(self, otp: str, challenge_state: str) -> str:
        """POST /u/email-identifier/challenge?state → 提交 OTP → 302 → 拿 password state。"""
        self._l(f"提交 OTP: {otp}")
        url = f"https://{AUTH0_DOMAIN}/u/email-identifier/challenge?state={challenge_state}"
        r = self.s.post(
            url,
            data={"state": challenge_state, "code": otp},
            headers=self._form_headers(url),
            allow_redirects=False,
            timeout=30,
        )
        location = r.headers.get("location", "") or ""
        next_state = _extract_state(location, challenge_state)
        self._l(f"challenge → status={r.status_code} next={location[:80]}")
        return next_state

    def step5_submit_password(self, email: str, password: str, pw_state: str) -> str:
        """POST /u/signup/password?state → 设密码 → 302 → 拿 resume state。"""
        self._l("设置密码")
        url = f"https://{AUTH0_DOMAIN}/u/signup/password?state={pw_state}"
        r = self.s.post(
            url,
            data={
                "state": pw_state,
                "email": email,
                "password": password,
                "passwordPolicy.isFlexible": "false",
                "strengthPolicy": "good",
                "complexityOptions.minLength": "8",
                "action": "default",
            },
            headers=self._form_headers(url),
            allow_redirects=False,
            timeout=30,
        )
        location = r.headers.get("location", "") or ""
        next_state = _extract_state(location, pw_state)
        self._l(f"password → status={r.status_code} next={location[:80]}")
        return next_state

    def step6_resume_get_auth_code(self, resume_state: str) -> str:
        """GET /authorize/resume?state → 302 跳 redirect_uri?code=... → 解析 auth_code。"""
        self._l("resume 拿 auth_code")
        r = self.s.get(
            f"https://{AUTH0_DOMAIN}/authorize/resume",
            params={"state": resume_state},
            allow_redirects=False,
            timeout=30,
        )
        location = r.headers.get("location", "") or ""
        code = _extract_auth_code(location)
        if code:
            self._l(f"auth_code (len={len(code)})")
            return code
        # 兜底：response body 里找 code=
        body = r.text or ""
        m = re.search(r"[?&]code=([^&\"'\s]+)", body)
        code = urllib.parse.unquote(m.group(1)) if m else ""
        if code:
            self._l(f"auth_code from body (len={len(code)})")
        else:
            self._l(f"未拿到 auth_code，location={location[:120]} status={r.status_code}")
        return code

    def run(self, *, email: str, password: str, otp_callback: Callable[[], str]) -> dict[str, Any]:
        """完整全协议注册链路：authorize → captcha → email → OTP → password → resume → token → API key。"""
        state = self.step1_authorize()
        if not state:
            raise RuntimeError("AnyCap 全协议注册：authorize 未拿到 state（可能被 Auth0 拦截/403）")
        captcha_token = self.step2_solve_captcha()
        if not captcha_token:
            raise RuntimeError("AnyCap 全协议注册：Turnstile 打码失败，未拿到 token")
        challenge_state = self.step3_submit_email(email, state, captcha_token)
        if challenge_state == state:
            # identifier 没推进（可能 captcha 被拒/邮箱已注册/IP 风控），检测拦截
            self._detect_and_raise_block(email, None)
            raise RuntimeError("AnyCap 全协议注册：提交邮箱未推进，可能 captcha 被拒或邮箱已注册")
        otp = str(otp_callback() or "").strip()
        if not otp:
            raise RuntimeError("AnyCap 全协议注册：未获取到 OTP")
        self._l(f"OTP: {otp}")
        pw_state = self.step4_submit_otp(otp, challenge_state)
        resume_state = self.step5_submit_password(email, password, pw_state)
        auth_code = self.step6_resume_get_auth_code(resume_state)
        if not auth_code:
            raise RuntimeError("AnyCap 全协议注册：未获得 authorization code")
        self._l("auth_code OK，换 token")
        tokens = _exchange_auth_code_for_tokens(auth_code, proxy=self.proxy)
        access_token = str(tokens.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"AnyCap 全协议注册：token 响应缺少 access_token: {tokens}")
        import time
        key_result = _create_api_key_http(access_token, name=f"auto-register-{int(time.time())}", proxy=self.proxy)
        if not key_result.get("ok"):
            raise RuntimeError(f"AnyCap 全协议注册：创建 API Key 失败: {key_result}")
        api_key = str(key_result.get("api_key") or "").strip()
        self._l(f"API Key: {api_key[:14]}…")
        return {
            "email": email,
            "password": password,
            "api_key": api_key,
            "api_key_info": key_result.get("data") or {},
            "api_verification": _verify_api_key_http(api_key, proxy=self.proxy),
            "key_create_result": key_result,
            "access_token": access_token,
            "refresh_token": str(tokens.get("refresh_token") or ""),
            "id_token": str(tokens.get("id_token") or ""),
            "token_type": str(tokens.get("token_type") or ""),
            "expires_in": tokens.get("expires_in", 0),
            "api_base": API_BASE,
            "native_api_base": API_BASE,
            "site_url": SITE_URL,
            "dashboard_url": "https://anycap.ai/dashboard",
        }

    # --- 协议层风控/已注册检测（从响应 body/location 判断）---

    def _detect_and_raise_block(self, email: str, last_response) -> None:
        """检测 AnyCap/Auth0 拦截并 raise 对应异常（不拉黑邮箱，让上层换 IP/换号重试）。

        纯协议层：从 last_response（如有）或重新 GET 当前 identifier 页 body 检测。
        - too many signup attempts → AnyCapSignupRateLimited（换 IP）
        - already registered → RuntimeError（换号，worker 层补记 used_platforms）
        - email domain not allowed → RuntimeError（拉黑域名由上层处理）
        - captcha rejected → RuntimeError（换 IP/session）
        """
        from platforms.anycap.browser_oauth import AnyCapSignupRateLimited
        text = ""
        if last_response is not None:
            try:
                text = (last_response.text or "")[:2000]
            except Exception:
                text = ""
            loc = last_response.headers.get("location", "") or ""
            text = f"{text} {loc}"
        if not text:
            # 兜底：重新 GET identifier 页扫 body（用当前 session cookies）
            try:
                r = self.s.get(f"https://{AUTH0_DOMAIN}/u/signup/identifier", timeout=15)
                text = (r.text or "")[:2000]
            except Exception:
                text = ""
        reason = self._detect_block_from_text(text)
        if not reason:
            return
        if reason == "anycap_signup_attempts_limited":
            self._l(f"AnyCap IP 维度注册频率受限(too many signup attempts)，换 IP 重试: {email}")
            raise AnyCapSignupRateLimited(f"AnyCap IP 维度注册频率受限(too many signup attempts)，请换 IP 重试: {email}")
        if reason == "anycap_email_already_registered":
            self._l(f"AnyCap 邮箱已注册过，换号: {email}")
            raise RuntimeError(f"AnyCap 邮箱已注册过，已注册: {email}")
        if reason == "anycap_captcha_rejected":
            self._l(f"AnyCap Turnstile captcha 被拒，换 IP/session 重试: {email}")
            raise RuntimeError(f"AnyCap Turnstile captcha 被拒，请换 IP/session 重试: {email}")
        if reason == "anycap_email_domain_not_allowed":
            raise RuntimeError(f"AnyCap 邮箱域名不允许注册: {email}")

    def _detect_block_from_text(self, text: str) -> str:
        """从文本检测拦截原因。"""
        t = str(text or "").lower()
        if "too many signup attempts" in t or "please try again later" in t:
            return "anycap_signup_attempts_limited"
        if "already registered" in t or "please log in instead" in t or "already signed up" in t:
            return "anycap_email_already_registered"
        if "email domain is not allowed" in t or "domain is not allowed" in t or "not allowed to sign up" in t:
            return "anycap_email_domain_not_allowed"
        if "security check" in t or "complete the captcha" in t or "verify you are human" in t or "invalid captcha" in t:
            return "anycap_captcha_rejected"
        return ""

    def _detect_block_from_response(self, r) -> str:
        """从协议响应（status/body/location）检测 AnyCap/Auth0 拦截。返回 reason 或空。"""
        text = ""
        try:
            text = (r.text or "")[:2000].lower()
        except Exception:
            text = ""
        loc = (r.headers.get("location", "") or "").lower()
        combined = f"{text} {loc}"
        if "too many signup attempts" in combined or "please try again later" in combined:
            return "anycap_signup_attempts_limited"
        if (
            "already registered" in combined
            or "please log in instead" in combined
            or "already signed up" in combined
        ):
            return "anycap_email_already_registered"
        if (
            "email domain is not allowed" in combined
            or "domain is not allowed" in combined
            or "not allowed to sign up" in combined
        ):
            return "anycap_email_domain_not_allowed"
        if (
            "security check" in combined
            or "complete the captcha" in combined
            or "verify you are human" in combined
            or "invalid captcha" in combined
        ):
            return "anycap_captcha_rejected"
        return ""


class AnyCapProtocolMailboxWorker:
    """AnyCap 纯协议邮箱注册 worker（零浏览器，ProtocolExecutor + YesCaptcha 带代理）。"""

    def __init__(self, *, executor, captcha, proxy: str | None = None, log_fn: Callable[[str], None] = print):
        self.executor = executor
        self.captcha = captcha
        self.proxy = proxy
        self.log = log_fn
        self.client = AnyCapProtocolRegister(executor=executor, captcha=captcha, proxy=proxy, log_fn=log_fn)

    def _l(self, msg: str) -> None:
        self.log(f"[AnyCap-Worker] {msg}")

    def run(self, *, email: str, password: str, otp_callback: Callable[[], str] = None) -> dict[str, Any]:
        from platforms.anycap.browser_oauth import AnyCapSignupRateLimited
        try:
            return self.client.run(email=email, password=password, otp_callback=otp_callback or (lambda: ""))
        except AnyCapSignupRateLimited:
            raise
        except Exception as exc:
            # 协议层无法像浏览器那样扫页面 body，但 step3/step4/step5 的响应可能含拦截提示；
            # AnyCapProtocolRegister.run 各 step 已记录 location，这里把异常信息透传，让上层
            # （application.tasks 的 anycap resin 换 IP 重试逻辑）按需换 IP。
            msg = str(exc or "")
            if "too many signup attempts" in msg.lower():
                raise AnyCapSignupRateLimited(f"AnyCap IP 维度注册频率受限(too many signup attempts)，请换 IP 重试: {email}")
            raise

