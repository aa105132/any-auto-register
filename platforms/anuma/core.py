"""Anuma HTTP / Privy 协议客户端。"""

from __future__ import annotations

import base64
import json
import time
import random
import logging as _logging
from typing import Any, Callable

import requests
from curl_cffi import requests as cffi_requests

from core.base_captcha import BaseCaptcha
from core.privy_throttle import acquire_send_slot, execute_with_429_retry
from ._anuma_fingerprint import build_anuma_fingerprint

PRIVY_APP_ID = "cmjrfihuc03h8l10ca0bi9o2y"
PRIVY_TURNSTILE_SITEKEY = "0x4AAAAAAAM8ceq5KhP1uJBt"
PRIVY_CLIENT = "react-auth:3.14.1"
ANUMUA_BASE = "https://chat.anuma.ai"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

_TURNSTILE_TS_PAGE = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<div id="ts-container"></div>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"></script>
<script>
window.turnstile.ready(function() {{
    window.turnstile.render("#ts-container", {{
        sitekey: "{PRIVY_TURNSTILE_SITEKEY}",
        callback: function(t) {{ document.querySelector("#ts-container").setAttribute("data-token", t); }},
        "error-callback": function(e) {{ document.querySelector("#ts-container").setAttribute("data-error", e || "err"); }},
        "timeout-callback": function() {{ document.querySelector("#ts-container").setAttribute("data-timeout", "1"); }},
    }});
}});
</script></body></html>"""
_TURNSTILE_LOOSE_CSP = (
    "default-src * 'unsafe-inline' 'unsafe-eval' data: blob:; "
    "script-src * 'unsafe-inline' 'unsafe-eval'; "
    "style-src * 'unsafe-inline'; frame-src *;"
)

_log = _logging.getLogger("anuma_browser")


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class AnumaClient:
    """Anuma / Privy 协议客户端。"""

    def __init__(
        self,
        timeout: float = 30.0,
        proxy: str | None = None,
        log_fn: Callable[[str], None] | None = None,
        session: requests.Session | None = None,
        captcha_solver: BaseCaptcha | None = None,
    ) -> None:
        self.timeout = timeout
        self.proxy = proxy
        self.log_fn = log_fn
        self.session = session or cffi_requests.Session(impersonate="chrome")
        self.fingerprint = build_anuma_fingerprint()
        self.captcha_solver = captcha_solver
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    def _privy_headers(self, ca_id: str) -> dict[str, str]:
        fp = self.fingerprint
        return {
            "privy-client": fp["privy_client"],
            "privy-app-id": PRIVY_APP_ID,
            "privy-ca-id": ca_id,
            "privy-ui": "t",
            "Origin": ANUMUA_BASE,
            "Referer": f"{ANUMUA_BASE}/",
            "User-Agent": fp["ua"],
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": fp["accept_language"],
            "sec-ch-ua": fp["sec_ch_ua"],
            "sec-ch-ua-platform": fp["sec_ch_ua_platform"],
            "sec-ch-ua-mobile": "?0",
        }

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict[str, Any] | None = None,
        max_retries: int = 3,
        label: str = "",
    ) -> requests.Response:
        last_exc = None
        for attempt in range(max_retries + 1):
            try:
                if method == "GET":
                    resp = self.session.get(url, headers=headers, timeout=self.timeout)
                else:
                    resp = self.session.post(url, headers=headers, json=json_body or {}, timeout=self.timeout)
                return resp
            except (requests.RequestException, cffi_requests.RequestsError) as exc:
                last_exc = exc
                if attempt < max_retries:
                    wait = min(2 ** attempt + random.uniform(0, 1), 10)
                    self._log(f"Privy {label} 网络异常 attempt={attempt + 1}/{max_retries + 1} wait={wait:.1f}s")
                    time.sleep(wait)
                continue

        if last_exc:
            raise last_exc
        raise RuntimeError(f"Privy {label}: max retries exceeded")

    def send_privy_code(self, email: str, ca_id: str) -> dict[str, Any]:
        self._log("Privy 发送验证码")
        slept = acquire_send_slot()
        if slept > 0:
            self._log(f"Privy init: throttle wait {slept:.2f}s")

        token = self._get_turnstile_token(ca_id)

        headers = self._privy_headers(ca_id)
        body = {"email": email, "token": token}
        resp = execute_with_429_retry(
            lambda: self._request_with_retry(
                "POST",
                "https://auth.privy.io/api/v1/passwordless/init",
                headers=headers,
                json_body=body,
                label="init",
            ),
            log_fn=self._log,
            label="anuma Privy init",
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise TypeError(f"Privy init 响应格式异常: {type(data).__name__}")
        return data

    def _get_turnstile_token(self, ca_id: str) -> str:
        """获取 Privy Turnstile token。

        纯协议模式：使用 captcha_solver（打码平台）获取 token。
        半 CDP 模式：Playwright headless 渲染 Turnstile widget 获取 token，
                    其余流程全走 HTTP 协议。
        """
        if self.captcha_solver is not None:
            page_url = (
                f"https://auth.privy.io/apps/{PRIVY_APP_ID}/embedded-wallets"
                f"?caid={ca_id}"
            )
            self._log(f"Privy Turnstile: 获取 token sitekey={PRIVY_TURNSTILE_SITEKEY}")
            token = self.captcha_solver.solve_turnstile(page_url, PRIVY_TURNSTILE_SITEKEY)
            if not token or token in ("CAPTCHA_FAIL",):
                raise RuntimeError("Privy Turnstile: 获取 token 失败")
            self._log(f"Privy Turnstile: token 获取成功 ({len(token)} chars)")
            return token

        return self._harvest_turnstile_via_playwright(ca_id)

    def _harvest_turnstile_via_playwright(self, ca_id: str) -> str:
        """半 CDP 浏览器方案：Camoufox headless 获取 Turnstile token。

        导航到 auth.privy.io，拦截页面替换为 Turnstile 专用模板（宽松 CSP），
        显式渲染 Turnstile widget 获取 token。Camoufox 内置反检测能力可
        绕过 Cloudflare JSD + Turnstile challenge。
        """
        _ts_log = _logging.getLogger("anuma_turnstile")
        target_url = (
            f"https://auth.privy.io/apps/{PRIVY_APP_ID}/embedded-wallets"
            f"?caid={ca_id}"
        )

        from camoufox.sync_api import Camoufox
        from urllib.parse import urlparse

        fox_opts: dict = {"headless": True}
        if self.proxy:
            parsed = urlparse(self.proxy)
            fox_opts["proxy"] = {
                "server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}",
            }
            if parsed.username:
                fox_opts["proxy"]["username"] = parsed.username
            if parsed.password:
                fox_opts["proxy"]["password"] = parsed.password

        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with Camoufox(**fox_opts) as browser:
                    page = browser.new_page()
                    try:
                        page.route(
                            "**/auth.privy.io/**",
                            lambda route: (
                                route.fulfill(
                                    body=_TURNSTILE_TS_PAGE,
                                    content_type="text/html",
                                    headers={"content-security-policy": _TURNSTILE_LOOSE_CSP},
                                )
                                if route.request.resource_type == "document"
                                else route.continue_()
                            ),
                        )
                        page.goto(target_url, wait_until="commit", timeout=15000)

                        deadline = time.monotonic() + 60.0
                        while time.monotonic() < deadline:
                            token = page.evaluate("""() => {
                                const el = document.querySelector("#ts-container");
                                return el ? (el.getAttribute("data-token") || "") : "";
                            }""")
                            if token:
                                elapsed = 60.0 - (deadline - time.monotonic())
                                _ts_log.info("anuma Turnstile: token (%d chars) after %.1fs (attempt %d)", len(token), elapsed, attempt)
                                return token
                            error = page.evaluate("""() => {
                                const el = document.querySelector("#ts-container");
                                return el ? (el.getAttribute("data-error") || "") : "";
                            }""")
                            if error:
                                _ts_log.warning("anuma Turnstile: error=%s attempt %d/3", error, attempt)
                                break
                            page.wait_for_timeout(2000)
                        else:
                            _ts_log.warning("anuma Turnstile: timeout attempt %d/3", attempt)
                            last_error = RuntimeError("anuma Turnstile: timeout")
                    finally:
                        page.close()
            except Exception as exc:
                _ts_log.warning("anuma Turnstile: exception attempt %d/3: %s", attempt, exc)
                last_error = exc
                continue

        raise last_error or RuntimeError("anuma Turnstile: all attempts failed")

    def authenticate_privy(self, email: str, code: str, ca_id: str) -> dict[str, Any]:
        self._log("Privy 验证 OTP")
        resp = self._request_with_retry(
            "POST",
            "https://auth.privy.io/api/v1/passwordless/authenticate",
            headers=self._privy_headers(ca_id),
            json_body={"email": email, "code": code, "mode": "login-or-sign-up"},
            label="authenticate",
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise TypeError(f"Privy authenticate 响应格式异常: {type(data).__name__}")
        if not data.get("refresh_token"):
            for cookie_name in ("privy-refresh-token", "refresh_token"):
                cookie_val = resp.cookies.get(cookie_name) or self.session.cookies.get(cookie_name)
                if cookie_val:
                    data["refresh_token"] = cookie_val
                    break
        return data

    def _pat_headers(self, pat: str, ca_id: str) -> dict[str, str]:
        return {
            **self._privy_headers(ca_id),
            "Authorization": f"Bearer {pat}",
        }

    def accept_terms(self, pat: str, ca_id: str) -> dict[str, Any]:
        self._log("Privy 接受条款")
        resp = self.session.post(
            "https://auth.privy.io/api/v1/users/me/accept_terms",
            headers=self._pat_headers(pat, ca_id),
            json={},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise TypeError(f"accept_terms 响应格式异常: {type(data).__name__}")
        return data

    def create_session(self, pat: str, ca_id: str, refresh_token: str) -> dict[str, Any]:
        self._log("Privy 创建会话")
        resp = self.session.post(
            "https://auth.privy.io/api/v1/sessions",
            headers=self._pat_headers(pat, ca_id),
            json={"refresh_token": refresh_token},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            data = {}
        return data

    def create_wallet(self, pat: str, ca_id: str) -> dict[str, Any]:
        self._log("Privy 创建嵌入式钱包")
        resp = self.session.post(
            "https://auth.privy.io/api/v1/wallets",
            headers=self._pat_headers(pat, ca_id),
            json={"chain_type": "ethereum"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise TypeError(f"wallets 响应格式异常: {type(data).__name__}")
        return data
