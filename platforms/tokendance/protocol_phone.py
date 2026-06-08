"""Tokendance 纯协议手机号注册 / API Key 创建。"""
from __future__ import annotations

import random
import re
import string
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

TOKENDANCE_BASE_URL = "https://tokendance.space"
TOKENDANCE_PORTAL_API = f"{TOKENDANCE_BASE_URL}/portal/api"
TOKENDANCE_API_BASE = f"{TOKENDANCE_BASE_URL}/api/v1"
WATCHA_BASE_URL = "https://watcha.cn"
WATCHA_API_BASE = f"{WATCHA_BASE_URL}/api/v2"
WATCHA_CLIENT_ID = "adb9GvUqH1wRfI1m"
WATCHA_REDIRECT_URI = f"{TOKENDANCE_BASE_URL}/auth/watcha/callback"
WATCHA_SCOPE = "read phone"
WATCHA_REGISTER_SCENE_ID = "1wsn666v"
WATCHA_SIGNIN_CODE_SCENE_ID = "1jr8d9gx"
DEFAULT_PROJECT_ID = "108963"


def _normalize_china_phone(value: str) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) > 11:
        digits = digits[2:]
    return digits[:11]


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Td" + "".join(random.choice(alphabet) for _ in range(12)) + "9"


def _json_or_text(response: requests.Response) -> dict:
    try:
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"raw": response.text[:1000]}


def _unwrap_watcha_data(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def _extract_token_payload(payload: dict) -> dict:
    data = _unwrap_watcha_data(payload)
    if isinstance(data, dict) and ("access_token" in data or "refresh_token" in data):
        return data
    nested = data.get("data") if isinstance(data, dict) else None
    if isinstance(nested, dict):
        return nested
    return data if isinstance(data, dict) else {}


def _find_api_key(payload: Any) -> tuple[str, dict]:
    if isinstance(payload, dict):
        for key in ("key", "api_key", "apiKey", "token", "secret"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), payload
        for key in ("items", "data", "result", "key"):
            found, info = _find_api_key(payload.get(key))
            if found:
                return found, info
        for value in payload.values():
            found, info = _find_api_key(value)
            if found:
                return found, info
    elif isinstance(payload, list):
        for item in payload:
            found, info = _find_api_key(item)
            if found:
                return found, info
    return "", {}


class WatchaCaptchaRequired(RuntimeError):
    """Watcha 要求先完成 Aliyun CAPTCHA。"""

    def __init__(self, scene_id: str = "", payload: dict | None = None):
        self.scene_id = str(scene_id or "").strip()
        self.payload = dict(payload or {})
        scene_label = f" scene_id={self.scene_id}" if self.scene_id else ""
        super().__init__(f"Watcha 需要通过 Aliyun CAPTCHA{scene_label}")


class TokendanceAliyunCaptchaSolver:
    """用真实 Chrome 从 Tokendance OAuth 入口完成 Watcha Aliyun CAPTCHA。"""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn=print,
        chrome_user_data_dir: str = "",
        chrome_cdp_url: str = "",
        reuse_existing_cdp: bool = False,
        timeout: int = 120,
    ):
        self.proxy = proxy
        self.log = log_fn
        self.chrome_user_data_dir = str(chrome_user_data_dir or "").strip()
        self.chrome_cdp_url = str(chrome_cdp_url or "").strip()
        self.reuse_existing_cdp = bool(reuse_existing_cdp)
        self.timeout = max(30, int(timeout or 120))

    def solve_aliyun(self, *, page_url: str, scene_id: str, button_selector: str = "") -> Any:
        from core.oauth_browser import OAuthBrowser

        target_url = str(page_url or f"{TOKENDANCE_BASE_URL}/portal/auth/watcha").strip()
        scene = str(scene_id or WATCHA_REGISTER_SCENE_ID).strip()
        if not scene:
            raise RuntimeError("Tokendance Aliyun CAPTCHA 缺少 scene_id")
        self.log(f"[Tokendance] 打开真实 Chrome 通过 Aliyun CAPTCHA: {scene}")
        with OAuthBrowser(
            proxy=self.proxy,
            headless=False,
            chrome_user_data_dir=self.chrome_user_data_dir,
            chrome_cdp_url=self.chrome_cdp_url,
            reuse_existing_cdp=self.reuse_existing_cdp,
            log_fn=self.log,
        ) as browser:
            page = browser.active_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            self._wait_for_watcha_or_oauth_page(page)
            self._install_captcha_widget(page, scene_id=scene, button_selector=button_selector)
            token = self._click_and_wait_for_result(page)
            if token in (None, ""):
                raise RuntimeError("Tokendance Aliyun CAPTCHA 未返回有效 token")
            if isinstance(token, str):
                self.log(f"[Tokendance] Aliyun CAPTCHA 通过，token_len={len(token)}")
            else:
                self.log("[Tokendance] Aliyun CAPTCHA 通过")
            return token

    def _wait_for_watcha_or_oauth_page(self, page) -> None:
        deadline = time.time() + min(self.timeout, 60)
        while time.time() < deadline:
            current = str(getattr(page, "url", "") or "")
            if "watcha.cn" in current or "/portal/auth/watcha" in current:
                return
            page.wait_for_timeout(500)

    @staticmethod
    def _install_captcha_widget(page, *, scene_id: str, button_selector: str = "") -> None:
        page.evaluate(
            """
            async ({sceneId, buttonSelector}) => {
                window.__tokendanceAliyunCaptchaResult = null;
                window.__tokendanceAliyunCaptchaError = null;
                window.AliyunCaptchaConfig = {region: 'cn', prefix: 'ynhh3s'};
                if (!window.__aliyunCaptchaScriptLoaded) {
                    await new Promise((resolve, reject) => {
                        const existing = document.querySelector('script[data-tokendance-aliyun-captcha]');
                        if (existing) existing.remove();
                        const script = document.createElement('script');
                        script.dataset.tokendanceAliyunCaptcha = '1';
                        script.src = 'https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js';
                        script.async = true;
                        script.onload = () => { window.__aliyunCaptchaScriptLoaded = true; resolve(); };
                        script.onerror = () => { window.__tokendanceAliyunCaptchaError = 'script-load-failed'; reject(new Error('script-load-failed')); };
                        document.head.appendChild(script);
                    });
                }
                let container = document.querySelector('#tokendance-captcha-container');
                if (!container) {
                    container = document.createElement('div');
                    container.id = 'tokendance-captcha-container';
                    document.body.appendChild(container);
                }
                let button = buttonSelector ? document.querySelector(buttonSelector) : null;
                if (!button) {
                    button = document.querySelector('#tokendance-captcha-button');
                }
                if (!button) {
                    button = document.createElement('button');
                    button.id = 'tokendance-captcha-button';
                    button.textContent = 'verify';
                    button.style.position = 'fixed';
                    button.style.left = '20px';
                    button.style.top = '20px';
                    button.style.zIndex = '2147483647';
                    document.body.appendChild(button);
                }
                const buttonRef = buttonSelector || '#tokendance-captcha-button';
                await new Promise((resolve) => {
                    window.initAliyunCaptcha({
                        SceneId: sceneId,
                        mode: 'popup',
                        element: '#tokendance-captcha-container',
                        button: buttonRef,
                        success: (result) => { window.__tokendanceAliyunCaptchaResult = result; resolve(result); },
                        fail: (error) => { window.__tokendanceAliyunCaptchaError = error || 'fail'; },
                        getInstance: (instance) => { window.__tokendanceAliyunCaptchaInstance = instance; resolve('init'); },
                        onClose: () => { window.__tokendanceAliyunCaptchaClosed = true; },
                        slideStyle: { width: 360, height: 40 },
                        language: 'cn'
                    });
                    setTimeout(() => resolve('init-timeout'), 10000);
                });
            }
            """,
            {"sceneId": scene_id, "buttonSelector": button_selector},
        )

    def _click_and_wait_for_result(self, page) -> Any:
        try:
            page.click("#tokendance-captcha-button", timeout=10_000)
        except Exception:
            page.evaluate("() => document.querySelector('#tokendance-captcha-button')?.click()")
        deadline = time.time() + self.timeout
        last_error = None
        while time.time() < deadline:
            result = page.evaluate("() => window.__tokendanceAliyunCaptchaResult || null")
            if result:
                return result
            last_error = page.evaluate("() => window.__tokendanceAliyunCaptchaError || null")
            if isinstance(last_error, dict) and last_error.get("success") and last_error.get("verifyResult") is False:
                last_error = None
                page.evaluate("""
                    () => {
                        window.__tokendanceAliyunCaptchaError = null;
                        document.querySelector('#aliyunCaptcha-mask')?.remove();
                        document.querySelector('#aliyunCaptcha-window-popup')?.remove();
                        document.querySelector('#tokendance-captcha-button')?.click();
                    }
                """)
                page.wait_for_timeout(1000)
                continue
            self._try_slide_once(page)
            page.wait_for_timeout(1500)
        if last_error:
            raise RuntimeError(f"Tokendance Aliyun CAPTCHA 超时: {last_error}")
        raise TimeoutError("Tokendance Aliyun CAPTCHA 超时")

    @staticmethod
    def _try_slide_once(page) -> None:
        try:
            candidates = page.evaluate(
                """
                () => Array.from(document.querySelectorAll('body *')).map((el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    const text = (el.innerText || el.textContent || '').trim();
                    const label = [el.id || '', String(el.className || ''), text].join(' ').toLowerCase();
                    return {
                        label,
                        rect: [rect.x, rect.y, rect.width, rect.height],
                        visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden'
                    };
                }).filter((item) => item.visible && /slider|slide|aliyun|drag|滑块|拖动/.test(item.label)).slice(-8)
                """
            )
            if not candidates:
                return
            rect = candidates[-1].get("rect") or []
            if len(rect) != 4:
                return
            x, y, width, height = [float(v or 0) for v in rect]
            if width <= 0 or height <= 0:
                return
            start_x = x + min(max(width * 0.15, 8), 40)
            start_y = y + height / 2
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            page.mouse.move(x + width + 260, start_y, steps=32)
            page.mouse.up()
        except Exception:
            return


class TokendanceProtocolPhoneWorker:
    def __init__(self, *, proxy: str | None = None, log_fn=print, watcha_cookies: dict | None = None):
        self.proxy = proxy
        self.log = log_fn
        self.request_trace: list[dict[str, Any]] = []
        self.watcha = requests.Session()
        self.tokendance = requests.Session()
        browser_headers = {
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json; charset=UTF-8",
        }
        self.watcha.headers.update({**browser_headers, "origin": WATCHA_BASE_URL, "referer": f"{WATCHA_BASE_URL}/login?type=oauth"})
        self.tokendance.headers.update({**browser_headers, "origin": TOKENDANCE_BASE_URL, "referer": f"{TOKENDANCE_BASE_URL}/keys"})
        for name, value in dict(watcha_cookies or {}).items():
            self.watcha.cookies.set(str(name), str(value), domain="watcha.cn", path="/")
        if proxy:
            proxies = {"http": proxy, "https": proxy}
            self.watcha.proxies.update(proxies)
            self.tokendance.proxies.update(proxies)

    def _record(self, method: str, url: str, *, status: int | None = None, note: str = "") -> None:
        self.request_trace.append({"method": method, "url": url, "status": status, "note": note})

    def _request_watcha(self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None, token: str = "") -> dict:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = self.watcha.request(method, f"{WATCHA_API_BASE}{path}", json=json_body, params=params, headers=headers, timeout=30)
        self._record(method.upper(), f"/api/v2{path}", status=response.status_code, note="Watcha API")
        data = _json_or_text(response)
        context = data.get("captchaContext") if isinstance(data.get("captchaContext"), dict) else {}
        scene_id = str(context.get("sceneId") or "").strip()
        is_captcha = response.status_code == 449 or str(data.get("code") or "").upper() == "RETRY_CAPTCHA"
        if is_captcha:
            raise WatchaCaptchaRequired(scene_id=scene_id, payload=data)
        if not response.ok:
            raise RuntimeError(f"Watcha API failed: {response.status_code} {data}")
        status_code = data.get("statusCode")
        if status_code not in (None, 200, "200") and data.get("message"):
            raise RuntimeError(f"Watcha API failed: {data}")
        return data

    def request_watcha_verify_code(self, *, phone: str, captcha: Any = "") -> dict:
        payload = {"phone": phone, "captcha": captcha, "type": "register"}
        return self._request_watcha("POST", "/auth/request-verify-code", json_body=payload)

    def request_watcha_signin_code(self, *, phone: str, captcha: Any = "") -> dict:
        payload = {"phone": phone, "captcha": captcha, "type": "signin"}
        return self._request_watcha("POST", "/auth/request-verify-code", json_body=payload)

    @staticmethod
    def _is_registered_error(error: Exception) -> bool:
        text = str(error)
        return "已注册" in text or "already registered" in text.lower()

    def signup_watcha_phone(self, *, phone: str, code: str, password: str, invitation_code: str = "") -> dict:
        payload = {
            "nickname": f"观猹员{code}",
            "email": "",
            "phone": phone,
            "verify_code": code,
            "password": password,
            "confirmPassword": password,
            "invitation_code": invitation_code,
            "confirmUserAgreement": True,
        }
        return _extract_token_payload(self._request_watcha("POST", "/auth/signup", json_body=payload))

    def signin_watcha_phone_code(self, *, phone: str, code: str) -> dict:
        payload = {"phone": phone, "code": code}
        return _extract_token_payload(self._request_watcha("POST", "/auth/signin-code", json_body=payload))

    def start_tokendance_oauth(self) -> dict:
        response = self.tokendance.get(f"{TOKENDANCE_BASE_URL}/portal/auth/watcha", allow_redirects=False, timeout=30)
        self._record("GET", "/portal/auth/watcha", status=response.status_code, note="启动 Tokendance Watcha OAuth")
        location = response.headers.get("Location") or response.headers.get("location") or ""
        if not location:
            raise RuntimeError(f"Tokendance 未返回 Watcha OAuth 跳转: {response.status_code}")
        query = parse_qs(urlparse(location).query)
        return {
            "location": location,
            "client_id": (query.get("client_id") or [WATCHA_CLIENT_ID])[0],
            "redirect_uri": (query.get("redirect_uri") or [WATCHA_REDIRECT_URI])[0],
            "response_type": (query.get("response_type") or ["code"])[0],
            "scope": (query.get("scope") or [WATCHA_SCOPE])[0],
            "state": (query.get("state") or [""])[0],
        }

    def authorize_watcha_oauth(self, *, access_token: str) -> str:
        oauth = self.start_tokendance_oauth()
        params = {
            "client_id": oauth.get("client_id") or WATCHA_CLIENT_ID,
            "redirect_uri": oauth.get("redirect_uri") or WATCHA_REDIRECT_URI,
            "response_type": oauth.get("response_type") or "code",
            "scope": oauth.get("scope") or WATCHA_SCOPE,
            "state": oauth.get("state") or "",
        }
        self._request_watcha("GET", "/oauth2-server/authorize/info", params=params, token=access_token)
        result = self._request_watcha("POST", "/oauth2-server/authorize", json_body=params, token=access_token)
        data = _unwrap_watcha_data(result)
        redirect_uri = str(data.get("redirect_uri") or data.get("redirectUri") or "").strip()
        if not redirect_uri:
            raise RuntimeError(f"Watcha OAuth 未返回 redirect_uri: {result}")
        query = parse_qs(urlparse(redirect_uri).query)
        code = (query.get("code") or [""])[0]
        if not code:
            raise RuntimeError(f"Watcha OAuth redirect_uri 缺少 code: {redirect_uri}")
        return code

    def tokendance_callback(self, *, code: str) -> dict:
        params = {"code": code}
        response = self.tokendance.get(f"{TOKENDANCE_PORTAL_API}/auth/watcha/callback?{urlencode(params)}", timeout=30)
        self._record("GET", "/portal/api/auth/watcha/callback", status=response.status_code, note="Tokendance OAuth callback")
        data = _json_or_text(response)
        if not response.ok:
            raise RuntimeError(f"Tokendance callback failed: {response.status_code} {data}")
        return data

    def list_api_keys(self) -> dict:
        response = self.tokendance.get(f"{TOKENDANCE_PORTAL_API}/keys", params={"limit": 20}, timeout=30)
        self._record("GET", "/portal/api/keys", status=response.status_code, note="Tokendance 列出 API Key")
        data = _json_or_text(response)
        if not response.ok:
            raise RuntimeError(f"Tokendance list keys failed: {response.status_code} {data}")
        return data

    def create_api_key(self, *, name: str) -> dict:
        response = self.tokendance.post(f"{TOKENDANCE_PORTAL_API}/keys", json={"name": name}, timeout=30)
        self._record("POST", "/portal/api/keys", status=response.status_code, note="Tokendance 创建 API Key")
        data = _json_or_text(response)
        if not response.ok:
            raise RuntimeError(f"Tokendance create key failed: {response.status_code} {data}")
        return data

    def extract_or_create_api_key(self, *, name: str) -> tuple[str, dict]:
        listed = self.list_api_keys()
        key, info = _find_api_key(listed)
        if key:
            return key, {"source": "list", **(info if isinstance(info, dict) else {})}
        created = self.create_api_key(name=name)
        key, info = _find_api_key(created)
        if key:
            return key, {"source": "create", **(info if isinstance(info, dict) else {})}
        raise RuntimeError(f"Tokendance 创建 API Key 后未返回 key: {created}")

    @staticmethod
    def _watcha_message(payload: Any) -> str:
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("msg") or payload.get("code") or "").strip()
            status = str(payload.get("statusCode") or payload.get("status") or "").strip()
            if status and message:
                return f"{status} {message}"
            return message or status or "ok"
        return "ok"

    @staticmethod
    def _phone_metadata(account) -> dict:
        return {
            "phone": str(getattr(account, "phone", "") or ""),
            "project_id": str(getattr(account, "project_id", "") or DEFAULT_PROJECT_ID),
            "provider_name": str(getattr(account, "provider_name", "") or ""),
        }

    def _solve_watcha_captcha(self, captcha_solver: Any, *, scene_id: str = WATCHA_REGISTER_SCENE_ID) -> Any:
        if captcha_solver is None:
            return ""
        resolved_scene_id = scene_id or WATCHA_REGISTER_SCENE_ID
        if hasattr(captcha_solver, "solve_aliyun"):
            return captcha_solver.solve_aliyun(
                page_url=f"{TOKENDANCE_BASE_URL}/portal/auth/watcha",
                scene_id=resolved_scene_id,
            )
        if callable(captcha_solver):
            return captcha_solver(scene_id=resolved_scene_id, page_url=f"{TOKENDANCE_BASE_URL}/portal/auth/watcha")
        raise RuntimeError("Tokendance cdp_protocol 需要支持 solve_aliyun 的 Aliyun CAPTCHA solver")

    def run(
        self,
        *,
        phone_provider,
        otp_timeout: int = 180,
        poll_interval: int = 15,
        code_pattern: str | None = None,
        key_name: str = "auto-register",
        watcha_invitation_code: str = "",
        watcha_password: str = "",
        captcha_solver: Any = None,
        use_legacy_register_flow: bool = False,
    ) -> dict:
        if phone_provider is None:
            raise RuntimeError("Tokendance 手机号注册需要配置 phone_provider")
        if not str(getattr(phone_provider, "project_id", "") or "").strip():
            try:
                setattr(phone_provider, "project_id", DEFAULT_PROJECT_ID)
            except Exception:
                pass

        self.log("[Tokendance] 从豪猪获取手机号")
        phone_account = phone_provider.get_phone()
        if not str(getattr(phone_account, "project_id", "") or "").strip():
            try:
                setattr(phone_account, "project_id", DEFAULT_PROJECT_ID)
            except Exception:
                pass
        phone = _normalize_china_phone(str(getattr(phone_account, "phone", "") or ""))
        if len(phone) != 11:
            raise RuntimeError(f"Tokendance/Watcha 手机号注册需要 11 位中国大陆手机号，当前号码无效: {getattr(phone_account, 'phone', '')}")

        captcha_scene_id = WATCHA_REGISTER_SCENE_ID if use_legacy_register_flow else WATCHA_SIGNIN_CODE_SCENE_ID
        self.log(f"[Tokendance] 请求 Watcha 短信验证码: {phone[:4]}****")
        captcha_payload = ""
        if captcha_solver is not None:
            captcha_payload = self._solve_watcha_captcha(captcha_solver, scene_id=captcha_scene_id)
        try:
            if use_legacy_register_flow:
                send_result = self.request_watcha_verify_code(phone=phone, captcha=captcha_payload)
            else:
                send_result = self.request_watcha_signin_code(phone=phone, captcha=captcha_payload)
            self.log(f"[Tokendance] Watcha 短信请求已提交: {self._watcha_message(send_result)}")
        except WatchaCaptchaRequired as captcha_error:
            if captcha_solver is None:
                raise
            self.log("[Tokendance] Watcha 要求 CAPTCHA，重新通过真实浏览器验证")
            captcha_payload = self._solve_watcha_captcha(
                captcha_solver,
                scene_id=captcha_error.scene_id or captcha_scene_id,
            )
            if use_legacy_register_flow:
                send_result = self.request_watcha_verify_code(phone=phone, captcha=captcha_payload)
            else:
                send_result = self.request_watcha_signin_code(phone=phone, captcha=captcha_payload)
            self.log(f"[Tokendance] Watcha 短信请求已提交: {self._watcha_message(send_result)}")
        self.log(f"[Tokendance] 等待豪猪短信验证码，超时 {int(otp_timeout or 180)}s")
        code = phone_provider.wait_for_code(
            phone_account,
            timeout=int(otp_timeout or 180),
            poll_interval=int(poll_interval or 15),
            code_pattern=code_pattern,
        )
        if not code:
            raise RuntimeError("Tokendance 手机号 provider 未返回短信验证码")
        self.log("[Tokendance] 已收到短信验证码")

        password = watcha_password or _random_password()
        if use_legacy_register_flow:
            self.log("[Tokendance] 提交 Watcha 手机号注册/登录")
            try:
                watcha_tokens = self.signup_watcha_phone(
                    phone=phone,
                    code=str(code).strip(),
                    password=password,
                    invitation_code=watcha_invitation_code,
                )
            except Exception as first_error:
                self.log(f"[Tokendance] Watcha 注册失败，尝试短信登录: {first_error}")
                if self._is_registered_error(first_error):
                    signin_captcha = ""
                    if captcha_solver is not None:
                        signin_captcha = self._solve_watcha_captcha(captcha_solver, scene_id=WATCHA_SIGNIN_CODE_SCENE_ID)
                    try:
                        self.request_watcha_signin_code(phone=phone, captcha=signin_captcha)
                    except WatchaCaptchaRequired as captcha_error:
                        if captcha_solver is None:
                            raise
                        signin_captcha = self._solve_watcha_captcha(
                            captcha_solver,
                            scene_id=captcha_error.scene_id or WATCHA_SIGNIN_CODE_SCENE_ID,
                        )
                        self.request_watcha_signin_code(phone=phone, captcha=signin_captcha)
                    code = phone_provider.wait_for_code(
                        phone_account,
                        timeout=int(otp_timeout or 180),
                        poll_interval=int(poll_interval or 15),
                        code_pattern=code_pattern,
                    )
                    if not code:
                        raise RuntimeError("Tokendance 手机号 provider 未返回短信登录验证码")
                watcha_tokens = self.signin_watcha_phone_code(phone=phone, code=str(code).strip())
        else:
            self.log("[Tokendance] 提交 Watcha 手机号短信登录/注册")
            watcha_tokens = self.signin_watcha_phone_code(phone=phone, code=str(code).strip())
        access_token = str(watcha_tokens.get("access_token") or watcha_tokens.get("accessToken") or "").strip()
        if not access_token:
            raise RuntimeError(f"Watcha 手机号流程未返回 access_token: {watcha_tokens}")

        self.log("[Tokendance] 通过 Watcha OAuth 授权 Tokendance")
        code_value = self.authorize_watcha_oauth(access_token=access_token)
        callback_result = self.tokendance_callback(code=code_value)
        user = dict(callback_result.get("user") or callback_result.get("data") or {})

        self.log("[Tokendance] 创建或读取 API Key")
        api_key, api_key_info = self.extract_or_create_api_key(name=key_name)
        phone_e164 = f"+86{phone}"
        return {
            "email": str(user.get("email") or user.get("phone") or phone_e164),
            "user_id": str(user.get("id") or ""),
            "api_key": api_key,
            "api_key_info": api_key_info,
            "account_info": user,
            "cookies": self.tokendance.cookies.get_dict(),
            "session_cookie": "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.tokendance.cookies),
            "request_trace": self.request_trace,
            "phone": self._phone_metadata(phone_account),
            "watcha": {
                "phone": phone_e164,
                "access_token_present": bool(access_token),
                "refresh_token_present": bool(watcha_tokens.get("refresh_token") or watcha_tokens.get("refreshToken")),
            },
        }
