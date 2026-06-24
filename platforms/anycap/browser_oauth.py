"""AnyCap CDP/OAuth 注册与 API Key 创建。"""
from __future__ import annotations

import time
from typing import Any

import requests

from application.mailbox_inventory_support import add_mailbox_domain_blacklist, mailbox_domain
from core.google_oauth import drive_google_oauth
from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page

SITE_URL = "https://anycap.ai/"
DASHBOARD_URL = "https://anycap.ai/dashboard"
LOGIN_URL = "https://anycap.ai/api/auth/login?returnTo=%2Fauth%2Fcallback"
PROFILE_URL = "https://anycap.ai/auth/profile"
ACCESS_TOKEN_URL = "https://anycap.ai/auth/access-token"
API_BASE = "https://api.anycap.ai"
API_V1_BASE = f"{API_BASE}/v1"
API_KEYS_URL = f"{API_V1_BASE}/api-keys"
STATUS_URL = f"{API_V1_BASE}/status"


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {"raw": str(getattr(response, "text", "") or "")[:2000]}
    return data if isinstance(data, dict) else {"data": data}


def _extract_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _find_api_key(data: Any) -> str:
    if isinstance(data, str):
        text = data.strip()
        return text if text.startswith(("ak_", "sk-")) and len(text) >= 12 else ""
    if isinstance(data, dict):
        for key in ("api_key", "apiKey", "key", "token", "value", "secret"):
            found = _find_api_key(data.get(key))
            if found:
                return found
        for value in data.values():
            found = _find_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_api_key(item)
            if found:
                return found
    return ""


def _extract_logged_email(profile: Any, fallback: str = "") -> str:
    data = _extract_data(profile)
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates.extend([data, data.get("user"), data.get("profile"), data.get("account")])
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for key in ("email", "user_email", "preferred_username"):
            value = str(item.get(key) or "").strip()
            if "@" in value:
                return value
    return str(fallback or "").strip()


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookies.items())


def _session_from_cookies(cookies: dict[str, str], *, proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    for name, value in dict(cookies or {}).items():
        session.cookies.set(name, value, domain="anycap.ai")
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _get_access_token_http(cookies: dict[str, str], *, proxy: str | None = None) -> dict[str, Any]:
    session = _session_from_cookies(cookies, proxy=proxy)
    try:
        response = session.get(
            ACCESS_TOKEN_URL,
            headers={"Accept": "application/json", "Referer": DASHBOARD_URL},
            timeout=30,
        )
        payload = _safe_json(response)
        data = _extract_data(payload)
        token = str(payload.get("token") or (data.get("token") if isinstance(data, dict) else "") or "").strip()
        return {"ok": response.ok and bool(token), "status": response.status_code, "token": token, "data": payload}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "token": "", "data": {}}


def _get_profile_http(cookies: dict[str, str], *, proxy: str | None = None) -> dict[str, Any]:
    session = _session_from_cookies(cookies, proxy=proxy)
    try:
        response = session.get(
            PROFILE_URL,
            headers={"Accept": "application/json", "Referer": DASHBOARD_URL},
            timeout=30,
        )
        return {"ok": response.ok, "status": response.status_code, "data": _safe_json(response)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _create_api_key_http(access_token: str, *, name: str = "auto-register", proxy: str | None = None) -> dict[str, Any]:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": SITE_URL.rstrip("/"),
        "Referer": DASHBOARD_URL,
    }
    payload = {"name": name, "never_expires": True}
    try:
        response = requests.post(API_KEYS_URL, headers=headers, json=payload, proxies=proxies, timeout=30)
        data = _safe_json(response)
        created = _extract_data(data)
        api_key = ""
        key_id = ""
        if isinstance(created, dict):
            # AnyCap POST /v1/api-keys 会直接返回 raw_key；key_prefix 只是短前缀，不能作为 Bearer key。
            api_key = str(created.get("raw_key") or created.get("key") or created.get("api_key_secret") or "").strip()
            info = created.get("api_key") if isinstance(created.get("api_key"), dict) else {}
            key_id = str(created.get("id") or created.get("key_id") or created.get("api_key_id") or info.get("id") or "").strip()
        if not api_key:
            api_key = _find_api_key(created)
        # AnyCap 前端实际流程：POST 创建 key 记录，PATCH /v1/api-keys/{id} 才返回完整 secret。
        # POST 返回的 ak_xxx 短值只是 id，不能作为 Bearer key 调用生成接口。
        reveal_data: dict[str, Any] = {}
        if response.ok and key_id and (not api_key or len(api_key) < 24):
            reveal = requests.patch(
                f"{API_KEYS_URL}/{key_id}",
                headers=headers,
                json={"never_expires": True},
                proxies=proxies,
                timeout=30,
            )
            reveal_data = _safe_json(reveal)
            revealed_key = _find_api_key(_extract_data(reveal_data)) or _find_api_key(reveal_data)
            if revealed_key:
                api_key = revealed_key
            return {
                "ok": reveal.ok and bool(api_key) and len(api_key) >= 12,
                "status": reveal.status_code,
                "api_key": api_key,
                "key_id": key_id,
                "data": reveal_data,
                "create_data": data,
            }
        return {"ok": response.ok and bool(api_key), "status": response.status_code, "api_key": api_key, "key_id": key_id, "data": data}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "api_key": "", "data": {}}


def _verify_api_key_http(api_key: str, *, proxy: str | None = None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "reason": "missing_api_key"}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(
            STATUS_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            proxies=proxies,
            timeout=30,
        )
        return {"ok": response.ok, "status": response.status_code, "body": _safe_json(response)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def _click_login_prompts(browser: OAuthBrowser, *, timeout: int = 90, log_fn=print) -> bool:
    deadline = time.time() + max(1, timeout)
    clicked_any = False
    while time.time() < deadline:
        for page in browser.pages():
            if page.is_closed():
                continue
            url = page.url or ""
            try:
                if "accounts.google.com" in url:
                    clicked = page.evaluate(
                        """
                        () => {
                          const words = ['Continue','继续','Allow','允许','Next','下一步','I understand','我了解'];
                          const nodes = [...document.querySelectorAll('button,input[type=submit],div[role=button]')];
                          const node = nodes.find(n => words.some(w =>
                            ((n.innerText||n.textContent||n.value||n.getAttribute('aria-label')||'').includes(w))
                          ));
                          if (node) { node.click(); return (node.innerText||node.textContent||node.value||node.getAttribute('aria-label')||'clicked'); }
                          return '';
                        }
                        """
                    )
                    if clicked:
                        clicked_any = True
                        log_fn(f"[AnyCap] 点击 Google 授权提示: {clicked}")
                        time.sleep(3)
                        continue
                if "anycap.ai" in url and ("login" in url or "auth" in url):
                    if try_click_provider_on_page(page, "google"):
                        clicked_any = True
                        log_fn("[AnyCap] 点击 Google 登录")
                        time.sleep(4)
            except Exception:
                pass
        if any("anycap.ai" in (p.url or "") and "/dashboard" in (p.url or "") for p in browser.pages() if not p.is_closed()):
            return True
        time.sleep(0.8)
    return clicked_any


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    timeout: int = 300,
    log_fn=print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    google_password: str = "",
    api_key_name: str = "auto-register",
    use_camoufox: bool = True,
    cancel_token=None,
) -> dict[str, Any]:
    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("AnyCap 当前只支持 Google OAuth/CDP 注册")

    # AnyCap 的 Auth0 Google OAuth client (auth.converge.ai) 跟 Vellum 一样对浏览器做
    # 严格安全检测：Playwright Chromium 提交邮箱后会跳 accounts.google.com/v3/signin/rejected
    # （"此浏览器或应用可能不安全"）。默认用 Camoufox（反检测 Firefox）绕过该检测。
    with OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        use_camoufox=use_camoufox,
        log_fn=log_fn,
    ) as browser:
        browser.set_cancel_token(cancel_token)
        page = browser.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        # Auth0 Universal Login 页面渲染较慢，Google 按钮可能 2s 后仍未就绪；
        # 等按钮可见再点击，并验证跳转到 accounts.google.com，否则重点。
        clicked_google = False
        for attempt in range(3):
            try:
                page.wait_for_selector("button, [role='button'], a", state="attached", timeout=10000)
            except Exception:
                pass
            clicked = try_click_provider_on_page(page, "google")
            log_fn(f"[AnyCap] 点击 Google 按钮 attempt={attempt} clicked={clicked} url={(page.url or '')[:80]}")
            if not clicked:
                time.sleep(2)
                continue
            # 等待跳转到 Google / Auth0 callback，确认点击生效
            for _ in range(10):
                time.sleep(1)
                cur = page.url or ""
                if "accounts.google.com" in cur or "auth.converge.ai/login/callback" in cur:
                    break
            cur = page.url or ""
            if "accounts.google.com" in cur:
                clicked_google = True
                break
            log_fn(f"[AnyCap] 点击 Google 后未跳转 accounts.google.com，重点: url={cur[:80]}")
            time.sleep(2)
        if not clicked_google:
            pages_urls = [(p.url or "")[:80] for p in browser.pages() if not p.is_closed()]
            raise RuntimeError(f"AnyCap 点击 Continue with Google 未跳转到 Google 登录页: pages={pages_urls}")
        oauth_result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=lambda b: any("anycap.ai" in (p.url or "") and "auth" not in (p.url or "") for p in b.pages() if not p.is_closed()),
        )
        log_fn(
            f"[AnyCap] drive_google_oauth 结果: email_submitted={oauth_result.email_submitted} "
            f"password_submitted={oauth_result.password_submitted} blocked_on_password={oauth_result.blocked_on_password} "
            f"last_url={(oauth_result.last_url or '')[:100]}"
        )
        if oauth_result.blocked_on_password and not any("anycap.ai" in (p.url or "") for p in browser.pages() if not p.is_closed()):
            # Google 登录被拒绝/验证码拦截，打印现场便于定位
            for p in browser.pages():
                if p.is_closed():
                    continue
                try:
                    body = str(p.evaluate("() => document.body ? document.body.innerText : ''") or "")[:300]
                except Exception as exc:
                    body = f"<eval failed: {exc!r}>"
                log_fn(f"[AnyCap] blocked 现场: url={(p.url or '')[:100]} body={body!r}")
            raise RuntimeError(
                f"AnyCap Google OAuth 登录被 Google 拒绝/拦截: last_url={(oauth_result.last_url or '')[:100]} "
                f"email={email_hint} (workspace 域策略或账号风控，详见 blocked 现场)"
            )
        if chrome_cdp_url or chrome_user_data_dir:
            browser.auto_select_google_account(timeout=8)
        _click_login_prompts(browser, timeout=min(timeout, 90), log_fn=log_fn)

        deadline = time.time() + timeout
        cookies: dict[str, str] = {}
        access_result: dict[str, Any] = {}
        profile_result: dict[str, Any] = {}
        while time.time() < deadline:
            cookies = browser.cookie_dict(domain_substrings=("anycap.ai",))
            profile_result = _get_profile_http(cookies, proxy=proxy)
            access_result = _get_access_token_http(cookies, proxy=proxy)
            if access_result.get("token"):
                break
            for p in browser.pages():
                if p.is_closed():
                    continue
                if "anycap.ai" in (p.url or "") and "/dashboard" not in (p.url or ""):
                    try:
                        p.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    break
            time.sleep(2)
        else:
            raise RuntimeError("AnyCap OAuth 登录超时，未拿到 /auth/access-token")

    access_token = str(access_result.get("token") or "").strip()
    key_result = _create_api_key_http(access_token, name=api_key_name, proxy=proxy)
    if not key_result.get("ok"):
        raise RuntimeError(f"AnyCap 协议创建 API Key 失败: {key_result}")
    api_key = str(key_result.get("api_key") or "").strip()
    api_verification = _verify_api_key_http(api_key, proxy=proxy)
    actual_email = _extract_logged_email(profile_result.get("data"), email_hint)

    return {
        "email": finalize_oauth_email(actual_email, email_hint, "AnyCap"),
        "api_key": api_key,
        "api_key_info": _extract_data(key_result.get("data")) or key_result.get("data") or {},
        "api_verification": api_verification,
        "key_create_result": key_result,
        "access_token": access_token,
        "profile": profile_result.get("data") or {},
        "cookies": cookies,
        "cookie_header": _cookie_header(cookies),
        "api_base": API_BASE,
        "native_api_base": API_BASE,
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
    }


# --- 系统邮箱注册：AnyCap 与 Enter 共用 auth.converge.ai Auth0，但 audience/client/redirect 不同 ---
AUTH0_DOMAIN = "auth.converge.ai"
AUTH0_CLIENT_ID = "4UbxENpNWnCX1JR6CNBHcE9o6lEQ9Wci"
AUTH0_AUDIENCE = "https://api.anycap.ai"
AUTH0_REDIRECT_URI = "https://anycap.ai/api/auth/callback"
# 复用固定 PKCE；Auth0 仅要求 verifier 与 challenge 匹配。
AUTH0_CODE_VERIFIER = "m8Tg8P7x9P4g2QmW0K4bF6vE1LxN3sR5uY7cD9nH2jK6pQ1a"
AUTH0_CODE_CHALLENGE = "wl4xqt5G44TNv8KzmVRFFFXlrz0MfMIA1hVyffSZHuk"
AUTH0_TOKEN_URL = f"https://{AUTH0_DOMAIN}/oauth/token"


def _build_auth0_signup_url(state: str = "") -> str:
    import random
    import urllib.parse

    params = {
        "client_id": AUTH0_CLIENT_ID,
        "redirect_uri": AUTH0_REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "scope": "openid profile email offline_access",
        "audience": AUTH0_AUDIENCE,
        "state": state or f"signup-{random.randint(10000, 99999)}",
        "code_challenge": AUTH0_CODE_CHALLENGE,
        "code_challenge_method": "S256",
        "screen_hint": "signup",
    }
    return f"https://{AUTH0_DOMAIN}/authorize?{urllib.parse.urlencode(params)}"


def _parse_auth_code_from_url(url: str) -> str:
    import urllib.parse

    parsed = urllib.parse.urlparse(str(url or ""))
    return (urllib.parse.parse_qs(parsed.query).get("code") or [""])[0]


def _exchange_auth_code_for_tokens(auth_code: str, *, proxy: str | None = None) -> dict[str, Any]:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    payload = {
        "grant_type": "authorization_code",
        "client_id": AUTH0_CLIENT_ID,
        "code_verifier": AUTH0_CODE_VERIFIER,
        "code": auth_code,
        "redirect_uri": AUTH0_REDIRECT_URI,
    }
    response = requests.post(AUTH0_TOKEN_URL, json=payload, headers={"Content-Type": "application/json"}, proxies=proxies, timeout=30)
    try:
        data = response.json()
    except Exception:
        data = {"raw": response.text[:2000]}
    if not response.ok:
        raise RuntimeError(f"AnyCap Auth0 token exchange failed: status={response.status_code} body={data}")
    return data if isinstance(data, dict) else {}


class AnyCapMailboxRegistrar:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        otp_callback=None,
        timeout: int = 180,
        chrome_path: str = "",
        cdp_url: str = "",
        log_fn=print,
    ) -> None:
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.timeout = timeout
        self.chrome_path = chrome_path
        self.cdp_url = cdp_url
        self.log = log_fn or (lambda _msg: None)

    def _l(self, msg: str) -> None:
        self.log(f"[AnyCap] {msg}")

    def _body_text(self, page: Any) -> str:
        try:
            return str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
        except Exception:
            return ""

    def _normalized_body_text(self, page: Any) -> str:
        return " ".join(self._body_text(page).lower().split())

    def _detect_signup_block_reason(self, page: Any = None, error: Any = None) -> str:
        texts = []
        if page is not None:
            texts.append(self._normalized_body_text(page))
        if error is not None:
            texts.append(" ".join(str(error or "").lower().split()))
        text = " ".join(item for item in texts if item)
        if not text:
            return ""
        if "too many signup attempts" in text or "please try again later" in text:
            return "anycap_signup_attempts_limited"
        if (
            "email domain is not allowed" in text
            or "domain is not allowed" in text
            or "not allowed to sign up" in text
        ):
            return "anycap_email_domain_not_allowed"
        return ""

    def _blacklist_domain(self, email: str, reason: str) -> str:
        domain = mailbox_domain(email)
        add_mailbox_domain_blacklist(email, platform="anycap", reason=reason or "anycap_signup_blocked")
        if domain:
            self._l(f"email domain blacklisted for anycap: {domain}")
        return domain

    def _raise_if_signup_blocked(self, email: str, page: Any = None, error: Any = None) -> None:
        reason = self._detect_signup_block_reason(page=page, error=error)
        if not reason:
            return
        domain = self._blacklist_domain(email, reason)
        if reason == "anycap_signup_attempts_limited":
            raise RuntimeError(f"AnyCap 邮箱域名/注册频率受限，已拉黑域名: {domain or email}")
        raise RuntimeError(f"AnyCap 邮箱域名不允许注册，已拉黑域名: {domain or email}")

    def run(self, *, email: str, password: str) -> dict[str, Any]:
        # 复用 Enter 已验证的 Auth0 Universal Login CDP 驱动：邮箱、密码、OTP、Turnstile 点击。
        from platforms.enter.browser_register import EnterBrowserRegistrar

        registrar = EnterBrowserRegistrar(
            headless=False,
            proxy=self.proxy,
            otp_callback=self.otp_callback,
            timeout=self.timeout,
            chrome_path=self.chrome_path,
            cdp_url=self.cdp_url,
            log_fn=self.log,
        )
        launch_meta = registrar._prepare_chrome()
        browser = None
        page = None
        auth_code = ""
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                self._l("打开 Auth0 邮箱注册页")
                page.goto(_build_auth0_signup_url(), wait_until="domcontentloaded", timeout=self.timeout * 1000)
                email_selector = "input[name='email'], input[type='email'], input#username"
                page.wait_for_selector(email_selector, timeout=30_000)
                page.locator(email_selector).first.fill(email)
                page.wait_for_timeout(800)
                self._raise_if_signup_blocked(email, page=page)
                try:
                    self._l("点击 Turnstile")
                    registrar._click_turnstile_until_token(page)
                except Exception as exc:
                    self._l(f"Turnstile 点击异常，继续尝试提交: {exc}")
                    self._raise_if_signup_blocked(email, page=page, error=exc)
                registrar._click_submit_no_wait(page)
                page.wait_for_timeout(1200)
                self._raise_if_signup_blocked(email, page=page)
                try:
                    auth_code = registrar._drive_post_identifier_steps(page, password) or ""
                except Exception as exc:
                    self._raise_if_signup_blocked(email, page=page, error=exc)
                    raise
                self._raise_if_signup_blocked(email, page=page)
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            registrar._teardown_chrome(launch_meta)

        if not auth_code:
            raise RuntimeError("AnyCap Auth0 邮箱注册未获得 authorization code")
        self._l("Auth0 code 获取成功，交换 token")
        tokens = _exchange_auth_code_for_tokens(auth_code, proxy=self.proxy)
        access_token = str(tokens.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"AnyCap Auth0 token 响应缺少 access_token: {tokens}")
        key_result = _create_api_key_http(access_token, name=f"auto-register-{int(time.time())}", proxy=self.proxy)
        if not key_result.get("ok"):
            raise RuntimeError(f"AnyCap 创建 API Key 失败: {key_result}")
        api_key = str(key_result.get("api_key") or "").strip()
        return {
            "email": email,
            "password": password,
            "api_key": api_key,
            "api_key_info": _extract_data(key_result.get("data")) or key_result.get("data") or {},
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
            "dashboard_url": DASHBOARD_URL,
        }
