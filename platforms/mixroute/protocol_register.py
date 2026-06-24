"""MixRoute 纯 HTTP 协议注册 worker（无浏览器，new-api JSON 协议）。

链路（全协议，无浏览器渲染）：
1. GET /api/status → 读 turnstile_site_key / turnstile_check（运行时下发，兜底默认值）。
2. 解 Turnstile（remote 打码 或 cdp_protocol 的 CDP 桥）→ token。
3. GET /api/verification?email=...&turnstile=... → 发送邮箱验证码。
4. 等待邮箱 OTP（otp_callback）。
5. POST /api/user/register?turnstile=... → 注册即登录，响应 data 含 token/user。
6. POST /api/token/ → 创建 API Key（new-api 标准 payload）。
7. GET /v1/models 验证 key。

执行器：
- protocol：remote 打码（yescaptcha/2captcha 等 solve_turnstile）。
- cdp_protocol：CDP 桥，真实 Chrome 过 Turnstile，同步 cookie/UA 到协议 session
  （与 hpcai cdp_protocol 一致，适用于 Cloudflare 对机房 IP 强校验的场景）。

注意：MixRoute 的 username 是独立字段（不是邮箱前缀），由调用方传入或自动生成。
aff_code 推广码可选，从 extra.mixroute_aff_code / aff 读取。
"""
from __future__ import annotations

import random
import re
import string
import time
from typing import Any, Callable

import requests

from platforms.mixroute.core import (
    API_BASE,
    CONSOLE_URL,
    DASHBOARD_URL,
    LOGIN_URL,
    REGISTER_URL,
    SITE_URL,
    TOKEN_URL,
    TURNSTILE_SITEKEY,
    _build_session,
    _cookie_header,
    _extract_token,
    _extract_user,
    _normalize_api_key,
    _response_data,
    _response_message,
    _response_success,
    apply_session_auth,
    create_api_key_http,
    get_status_http,
    get_user_self_http,
    import_cookies,
    login_http,
    register_http,
    send_verification_http,
    verify_api_key_http,
)


def _random_username(email: str = "") -> str:
    """MixRoute username 独立于邮箱；生成 8-12 位字母数字用户名，避免重名。"""
    base = re.sub(r"[^A-Za-z0-9]", "", (email.split("@", 1)[0] if email else "")) or "user"
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{base[:8]}{suffix}"


def _solver_supports_cdp_bridge(captcha_solver: Any) -> bool:
    return hasattr(captcha_solver, "solve_turnstile_with_session") or hasattr(captcha_solver, "solve_turnstile")


def _solve_turnstile_remote(captcha_solver: Any, page_url: str, sitekey: str) -> str:
    if captcha_solver is None or not hasattr(captcha_solver, "solve_turnstile"):
        raise RuntimeError("MixRoute 协议注册需要 Turnstile token，但未配置 captcha_solver")
    token = str(captcha_solver.solve_turnstile(page_url, sitekey) or "").strip()
    if not token or token == "CAPTCHA_FAIL":
        raise RuntimeError("MixRoute Turnstile 远程打码失败")
    return token


def _solve_turnstile_cdp(
    captcha_solver: Any,
    page_url: str,
    sitekey: str,
    session: requests.Session,
) -> dict[str, Any]:
    """CDP 混合链路：真实 Chrome 过 Turnstile，同步 token/Cookie/UA 到协议 session。"""
    if captcha_solver is None:
        raise RuntimeError("MixRoute cdp_protocol 需要 cdp_turnstile solver，但未配置 captcha_solver")
    if hasattr(captcha_solver, "solve_turnstile_with_session"):
        solved = captcha_solver.solve_turnstile_with_session(page_url, sitekey)
    elif hasattr(captcha_solver, "solve_turnstile"):
        solved = captcha_solver.solve_turnstile(page_url, sitekey)
    else:
        raise RuntimeError("MixRoute cdp_protocol 需要支持 solve_turnstile 的 captcha_solver")

    cookies: dict[str, str] = {}
    user_agent = ""
    if isinstance(solved, dict):
        solution = solved.get("solution") if isinstance(solved.get("solution"), dict) else {}
        token = str(
            solved.get("turnstile_token")
            or solved.get("token")
            or solved.get("value")
            or solution.get("token")
            or solution.get("value")
            or ""
        ).strip()
        raw_cookies = solved.get("cookies") or {}
        if isinstance(raw_cookies, dict):
            cookies = {str(k): str(v) for k, v in raw_cookies.items() if k and v is not None}
        user_agent = str(solved.get("user_agent") or solved.get("userAgent") or "").strip()
    else:
        token = str(solved or "").strip()

    if not token or token == "CAPTCHA_FAIL":
        raise RuntimeError("MixRoute CDP Turnstile token 为空")
    if cookies:
        import_cookies(session, cookies)
    if user_agent:
        session.headers.update({"User-Agent": user_agent})
    return {
        "ok": True,
        "turnstile_token": token,
        "sitekey": sitekey,
        "page_url": page_url,
        "mode": "cdp_protocol",
        "cookie_names": sorted(cookies.keys()),
        "user_agent_synced": bool(user_agent),
    }


class MixRouteProtocolRegister:
    """MixRoute 纯 HTTP 协议注册 worker（protocol / cdp_protocol 共用）。"""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        use_cdp_bridge: bool = False,
    ) -> None:
        self.session = _build_session(proxy)
        self.proxy = proxy
        self.log = log_fn
        self.use_cdp_bridge = bool(use_cdp_bridge)

    def _l(self, msg: str) -> None:
        self.log(f"[mixroute] {msg}")

    def _resolve_sitekey(self) -> str:
        """优先从 /api/status 读 turnstile_site_key，失败回退默认值。"""
        try:
            status = get_status_http(self.session)
            data = status.get("data") if isinstance(status.get("data"), dict) else {}
            sitekey = str(data.get("turnstile_site_key") or "").strip()
            if sitekey:
                return sitekey
        except Exception as exc:
            self._l(f"读取 /api/status 失败，用默认 sitekey: {type(exc).__name__}: {str(exc)[:80]}")
        return TURNSTILE_SITEKEY

    def _solve_turnstile(self, captcha_solver: Any, page_url: str, sitekey: str) -> tuple[str, dict[str, Any]]:
        if self.use_cdp_bridge:
            bootstrap = _solve_turnstile_cdp(captcha_solver, page_url, sitekey, self.session)
            return str(bootstrap.get("turnstile_token") or "").strip(), bootstrap
        return _solve_turnstile_remote(captcha_solver, page_url, sitekey), {
            "ok": True, "turnstile_token": "", "sitekey": sitekey, "page_url": page_url, "mode": "remote",
        }

    def run(
        self,
        *,
        email: str,
        password: str,
        username: str = "",
        otp_callback: Callable[[], str] | None = None,
        captcha_solver: Any = None,
        key_name: str = "auto-register",
        aff_code: str = "",
    ) -> dict[str, Any]:
        email = str(email or "").strip()
        if not email:
            raise RuntimeError("MixRoute 协议注册缺少邮箱")
        if otp_callback is None:
            raise RuntimeError("MixRoute 协议注册缺少 OTP 回调")
        username = str(username or "").strip() or _random_username(email)
        sitekey = self._resolve_sitekey()

        # 1. 发送邮箱验证码（需要 Turnstile）
        self._l("请求 MixRoute 邮箱验证码")
        verify_turnstile, verify_bootstrap = self._solve_turnstile(captcha_solver, REGISTER_URL, sitekey)
        send_result = send_verification_http(self.session, email, turnstile=verify_turnstile)
        if not _response_success(send_result):
            # Turnstile 可能只在注册端点强制，发送验证码端点部分部署不校验；打码失败时再试一次空 token。
            msg = _response_message(send_result).lower()
            if "turnstile" in msg or "captcha" in msg:
                raise RuntimeError(f"MixRoute 发送验证码被 Turnstile 拦截: {send_result}")
            if not verify_turnstile:
                pass
            else:
                raise RuntimeError(f"MixRoute 发送邮箱验证码失败: {send_result}")
        self._l("邮箱验证码已发送，等待收件...")

        # 2. 等待邮箱 OTP（等待期间发送验证码用的 Turnstile token 可能已过期）
        otp = str(otp_callback() or "").strip()
        if not otp:
            raise RuntimeError("MixRoute 未收到邮箱验证码")
        self._l(f"邮箱验证码: {otp}")

        # 3. 注册：复用发送验证码的 Turnstile token（浏览器实测同一 token 可用于
        #    verification + register，new-api Turnstile token 有效期约 300s，足够覆盖 OTP 等待）。
        #    遇 429 限流时退避重试（同 IP 短时间多次注册会触发 new-api 限流）。
        register_turnstile = verify_turnstile
        register_bootstrap = verify_bootstrap
        register_result = None
        for register_attempt in range(3):
            register_result = register_http(
                self.session,
                username=username,
                password=password,
                email=email,
                verification_code=otp,
                aff_code=aff_code,
                turnstile=register_turnstile,
            )
            if _response_success(register_result):
                break
            status = register_result.get("status")
            msg = _response_message(register_result).lower()
            if status == 429:
                wait = 30 * (register_attempt + 1)
                self._l(f"注册被限流(429)，等待 {wait}s 后重试 ({register_attempt+1}/3)")
                time.sleep(wait)
                # 限流后 Turnstile token 可能过期，重新打码
                register_turnstile, register_bootstrap = self._solve_turnstile(captcha_solver, REGISTER_URL, sitekey)
                continue
            if "turnstile" in msg or "captcha" in msg:
                # Turnstile 过期/被拒，重新打码再试一次
                self._l("注册被 Turnstile 拦截，重新打码重试")
                register_turnstile, register_bootstrap = self._solve_turnstile(captcha_solver, REGISTER_URL, sitekey)
                continue
            break
        if not _response_success(register_result):
            raise RuntimeError(f"MixRoute 注册失败: {_response_message(register_result)} (HTTP {register_result.get('status')})")
        self._l("注册成功")

        # 4. 登录拿会话（new-api 是 cookie session，不是 localStorage token）。
        #    注册响应只返回 {success:true}（无 token/user），需用 username+password 走
        #    /api/user/login?turnstile=... 登录，session 自动捕获 httpOnly 会话 cookie。
        self._l("登录获取会话 cookie")
        login_turnstile = register_turnstile
        login_result = login_http(self.session, username=username, password=password, turnstile=login_turnstile)
        if not _response_success(login_result):
            # 登录 Turnstile 可能独立校验，重新打码
            self._l("登录 Turnstile 被拒，重新打码")
            login_turnstile, _ = self._solve_turnstile(captcha_solver, LOGIN_URL, sitekey)
            login_result = login_http(self.session, username=username, password=password, turnstile=login_turnstile)
            if not _response_success(login_result):
                raise RuntimeError(f"MixRoute 注册成功但登录失败: {_response_message(login_result)}")
        login_data = _response_data(login_result) or {}
        user = _extract_user(login_data) if isinstance(login_data, dict) else {}
        user_id = str(user.get("id") or "")
        if not user_id:
            raise RuntimeError(f"MixRoute 登录成功但未返回 user id: {login_data}")
        # new-api 会话：cookie（session）+ New-API-User 头。无 Bearer token。
        apply_session_auth(self.session, "", user_id)
        self._l(f"登录成功，user_id={user_id}")

        # 5. 拉用户信息（补全 user/quota）
        try:
            self_info = get_user_self_http(self.session, "")
            if _response_success(self_info):
                self_user = _extract_user(self_info.get("data"))
                if self_user:
                    user = self_user
                    user_id = str(user.get("id") or user_id)
                    apply_session_auth(self.session, "", user_id)
        except Exception:
            pass

        # 6. 创建 API Key（cookie session 鉴权）
        key_result = create_api_key_http(self.session, token="", key_name=key_name, log_fn=self.log)
        api_key = _normalize_api_key(key_result.get("api_key") or "")
        if not api_key:
            raise RuntimeError("MixRoute 创建 API Key 失败：未返回明文 key")

        # 7. 验证 key
        api_verification = verify_api_key_http(api_key, proxy=self.proxy)
        from platforms.mixroute.core import _session_cookie_dict
        session_cookies = _session_cookie_dict(self.session)

        return {
            "email": str(user.get("email") or email),
            "password": password,
            "username": username,
            "user": user,
            "user_id": user_id,
            "session_token": "",
            "access_token": "",
            "api_key": api_key,
            "ai_api_token": api_key,
            "api_key_info": dict(key_result.get("api_key_info") or {}),
            "key_create_result": key_result,
            "api_verification": api_verification,
            "register_result": register_result,
            "cdp_bootstrap": register_bootstrap,
            "cookies": session_cookies,
            "cookie_header": _cookie_header(session_cookies),
            "auth_method": "email",
            "site_url": SITE_URL,
            "dashboard_url": DASHBOARD_URL,
            "api_base": API_BASE,
        }
