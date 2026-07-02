"""AnyCap CDP/OAuth 注册与 API Key 创建。"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from application.mailbox_inventory_support import add_mailbox_domain_blacklist, mailbox_domain
from core.google_oauth import drive_google_oauth
from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page

ROOT_DIR = Path(__file__).resolve().parents[2]

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
    # POST 重试：resin 代理偶发 ConnectionReset/ConnectionAborted，重试 3 次提高成功率
    response = None
    last_exc = None
    for _attempt in range(3):
        try:
            response = requests.post(API_KEYS_URL, headers=headers, json=payload, proxies=proxies, timeout=30)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            time.sleep(2)
    if response is None:
        return {"ok": False, "error": f"POST /v1/api-keys 重试 3 次仍失败: {last_exc!r}", "data": {}}
    try:
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


def _detect_callback_block(browser: OAuthBrowser, email: str, *, log_fn=print) -> str:
    """扫 anycap 回调页 URL/body，检测 Auth0 access_denied 类拦截，命中则拉黑邮箱域名。

    AnyCap Google OAuth 成功后回调 anycap.ai/api/auth/callback，Auth0 可能以
    error=access_denied&error_description=email_domain_not_allowed 拒绝（域名白名单）。
    返回人类可读错误串（调用方应 raise），无拦截返回空串。
    """
    import urllib.parse

    target_email = (email or "").strip()
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        if "anycap.ai" not in url:
            continue
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        error = (query.get("error") or [""])[0].strip()
        error_desc = (query.get("error_description") or [""])[0].strip()
        if not error:
            continue
        reason = "anycap_oauth_access_denied"
        if error_desc == "email_domain_not_allowed" or "domain" in error_desc.lower():
            reason = "anycap_email_domain_not_allowed"
            domain = mailbox_domain(target_email) if target_email else ""
            if domain:
                add_mailbox_domain_blacklist(target_email, platform="anycap", reason=reason)
                log_fn(f"[AnyCap] OAuth 回调域名被拒，已拉黑域名: {domain} ({error_desc})")
        log_fn(f"[AnyCap] OAuth 回调被拒: error={error} desc={error_desc} url={url[:120]}")
        return (
            f"AnyCap Google OAuth 回调被平台拒绝: error={error} "
            f"error_description={error_desc or '(empty)'} email={target_email} "
            f"({reason}；该邮箱域名/账号不在 AnyCap 允许范围，请换号)"
        )
    return ""


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
            # AnyCap 回调 URL 是 anycap.ai/api/auth/callback（含 "auth"），旧条件
            # "auth" not in url 永远不满足，导致 driver 空转满 timeout。改成到达
            # anycap.ai 任何页就返回，让上层 _click_login_prompts + access-token 循环
            # 接力处理 callback→dashboard 跳转与取 token。
            stop_when=lambda b: any("anycap.ai" in (p.url or "") for p in b.pages() if not p.is_closed()),
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

        # AnyCap Auth0 对部分邮箱域名（如 cttnoot.us 等 Workspace 域）配置了白名单，
        # Google OAuth 全程成功后回调 anycap.ai/api/auth/callback 会被以
        # error=access_denied&error_description=email_domain_not_allowed 拒绝，
        # 不建会话、access-token 401 missing_session。旧代码在此情形下空转到
        # timeout 才报"登录超时"，错误信息误导。这里在进 access-token 循环前扫
        # 所有页面 URL，命中域名拒绝立即拉黑域名并抛准确错误，早失败、早换号。
        callback_error = _detect_callback_block(browser, email_hint, log_fn=log_fn)
        if callback_error:
            raise RuntimeError(callback_error)

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


class AnyCapSignupRateLimited(RuntimeError):
    """AnyCap IP 维度注册频率风控（Too many signup attempts）。

    这是出口 IP 短时间内注册次数过多触发的，换 IP 即可继续，邮箱本身没问题，
    绝不能拉黑邮箱或域名。上层捕获后应换 resin session/IP 重试同一邮箱。
    """


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


def _exchange_auth_code_for_tokens(auth_code: str, *, proxy: str | None = None) -> dict[str, Any]:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    payload = {
        "grant_type": "authorization_code",
        "client_id": AUTH0_CLIENT_ID,
        "code_verifier": AUTH0_CODE_VERIFIER,
        "code": auth_code,
        "redirect_uri": AUTH0_REDIRECT_URI,
    }
    # POST 重试：resin 代理偶发 ConnectionReset/ConnectionAborted，重试 3 次提高成功率
    response = None
    last_exc = None
    for _attempt in range(3):
        try:
            response = requests.post(AUTH0_TOKEN_URL, json=payload, headers={"Content-Type": "application/json"}, proxies=proxies, timeout=30)
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            time.sleep(2)
    if response is None:
        raise RuntimeError(f"AnyCap Auth0 token exchange 重试 3 次仍失败: {last_exc!r}")
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
        inventory_id: int = 0,
        captcha_solver: Any = None,
    ) -> None:
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.timeout = timeout
        self.chrome_path = chrome_path
        self.cdp_url = cdp_url
        self.log = log_fn or (lambda _msg: None)
        # mailbox_inventory 项 id：anycap_signup_attempts_limited 是单邮箱注册频率
        # 风控（"Too many signup attempts"），只拉黑当前这一个 outlook 邮箱，
        # 绝不能拉黑整个 outlook.com 域名（会害所有 outlook 号都领不出）。
        self.inventory_id = int(inventory_id or 0)
        # 协议打码解决器（cdp_turnstile / yescaptcha / 2captcha / local_solver）。
        # 不为 None 时优先调用 solve_turnstile(url, sitekey) 拿 token 并注入表单，
        # 不再依赖浏览器内模拟点击 Turnstile 复选框（点击对 Auth0 Universal Login
        # 经常点不到/拿不到 token）。
        self.captcha_solver = captcha_solver

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
        # 邮箱已在 AnyCap/Auth0 注册过（密码页 "This email is already registered. Please log in instead"）。
        # 这种号 accounts 表通常已有 anycap 记录，但 inventory.used_platforms 漏记 anycap（旧回写 bug），
        # 导致被当 unused 重领。命中后拉黑单邮箱 + 补记 used_platforms=anycap，避免再被领出浪费 90s 超时。
        if (
            "already registered" in text
            or "email is already registered" in text
            or "please log in instead" in text
            or "already signed up" in text
        ):
            return "anycap_email_already_registered"
        if (
            "email domain is not allowed" in text
            or "domain is not allowed" in text
            or "not allowed to sign up" in text
        ):
            return "anycap_email_domain_not_allowed"
        # Turnstile captcha 被拒：打码 token 跨 session/state 不被 Auth0 接受、token 失效等。
        # 用 Auth0 实际报错措辞（security check / verify you are human / invalid captcha），
        # 不用裸 "captcha"（页面正常 Turnstile 区也会含该词，会误判）。
        if (
            "security check" in text
            or "complete the captcha" in text
            or "complete the security check" in text
            or "verify you are human" in text
            or "verify you're not a robot" in text
            or "not a robot" in text
            or "invalid captcha" in text
        ):
            return "anycap_captcha_rejected"
        return ""

    def _blacklist_single_mailbox(self, email: str, reason: str) -> None:
        """只拉黑当前这一个 outlook 邮箱的 inventory 项，不动域名。

        "Too many signup attempts" 是单邮箱注册频率风控（同一邮箱短时间内反复
        注册触发），换一个 outlook 号就没事，所以绝不能拉黑 outlook.com 域名。
        """
        if not self.inventory_id:
            return
        try:
            from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository
            MailboxInventoryRepository().update_item(
                self.inventory_id,
                status="blacklisted",
                last_error=str(reason or "anycap_signup_attempts_limited"),
                note="AnyCap 注册频率风控(too many signup attempts)，单邮箱拉黑",
            )
            self._l(f"已拉黑单个邮箱 inventory_id={self.inventory_id} ({email})，未拉黑域名")
        except Exception as exc:
            self._l(f"拉黑单邮箱失败（不影响报错）: {exc}")

    def _mark_inventory_platform_used(self, email: str, *, platform: str = "anycap") -> None:
        """补记 inventory.used_platforms 含 platform，让领号链路跳过该号。

        already_registered 命中时调用：这些号 accounts 表已注册过 anycap，但 inventory
        metadata.used_platforms 漏记 anycap（旧回写 bug），导致被当 unused 重领。补记后
        inventory_platform_already_used 会跳过它，不再浪费 90s 超时。
        """
        if not self.inventory_id:
            return
        try:
            from core.db import engine, MailboxInventoryModel
            from sqlmodel import Session
            with Session(engine) as session:
                item = session.get(MailboxInventoryModel, self.inventory_id)
                if not item:
                    return
                metadata = item.get_metadata()
                used = [str(p or "").strip() for p in list(metadata.get("used_platforms") or []) if str(p or "").strip()]
                if platform in used:
                    return
                used.append(platform)
                metadata["used_platforms"] = used
                item.set_metadata(metadata)
                item.updated_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
                session.add(item)
                session.commit()
            self._l(f"已补记 inventory used_platforms+={platform} (id={self.inventory_id})")
        except Exception as exc:
            self._l(f"补记 used_platforms 失败（不影响报错）: {exc}")

    def _drive_post_identifier_with_already_registered_guard(self, registrar, page, password, email):
        """包装 Enter._drive_post_identifier_steps，在密码页检测 already registered 早 raise。

        Enter._drive_post_identifier_steps 在密码页填密码提交后会循环等 auth_code 到 90s
        超时。若该邮箱已在 AnyCap 注册过，Auth0 密码页提示 'already registered'，密码提交不
        推进，白白等满 90s。Playwright sync API 绑主线程 greenlet，不能在子线程跑 page 操作，
        故不能并发轮询。这里 monkey-patch registrar._drive_post_identifier_steps，在密码页
        password_entered 后的循环里加 already registered body 检测，命中立即 raise 换号。
        """
        orig_drive = registrar._drive_post_identifier_steps
        registrar_self = registrar
        anycap_self = self

        def guarded_drive(page_, password_):
            from platforms.enter.browser_register import _parse_auth_code_from_url
            password_selector = "input[name='password'], input[type='password'], input#password"
            otp_selectors = [
                "input[name='code']", "input[name='verification_code']", "input[name='otp']",
                "input[inputmode='numeric']", "input[autocomplete='one-time-code']",
                "input[placeholder*='code' i]", "input[aria-label*='code' i]",
            ]
            password_entered = False
            otp_submitted_count = 0
            # 缩短 deadline 到 60s（卡死时早退出，避免 batch 200s 超时杀不干净 Chrome 堆积）
            deadline = time.time() + 60
            last_url = ""
            wait_count = 0
            while time.time() < deadline:
                auth_code = _parse_auth_code_from_url(page_.url)
                if auth_code:
                    return auth_code
                # already registered 早检测（密码页提交后 Auth0 显示该提示，不推进）
                try:
                    body = anycap_self._normalized_body_text(page_)
                    if body and (
                        "already registered" in body
                        or "please log in instead" in body
                        or "already signed up" in body
                    ):
                        anycap_self._l(f"检测到密码页 already registered，早失败换号: {email}")
                        anycap_self._raise_if_signup_blocked(email, page=page_)
                except RuntimeError:
                    raise
                except Exception:
                    pass
                if registrar_self._has_forbidden_email_domain_error(page_):
                    raise RuntimeError("email domain is not allowed: enter_email_domain_not_allowed")

                password_input = registrar_self._first_visible_locator(page_, [password_selector])
                if password_input and not password_entered:
                    registrar_self._l("password step found, entering password...")
                    password_input.fill(password_)
                    page_.wait_for_timeout(800)
                    registrar_self._click_submit_no_wait(page_)
                    password_entered = True
                    page_.wait_for_timeout(1500)
                    continue

                otp_input = registrar_self._first_visible_locator(page_, otp_selectors)
                if otp_input:
                    if not registrar_self._otp_callback:
                        registrar_self._l("OTP input found but no OTP callback configured")
                        return None
                    otp_submitted_count += 1
                    registrar_self._l(f"OTP input found (step {otp_submitted_count}), getting code from mailbox...")
                    otp = registrar_self._otp_callback()
                    if not otp:
                        registrar_self._l("OTP callback returned empty code")
                        return None
                    registrar_self._l("entering OTP...")
                    otp_input.fill(otp)
                    page_.wait_for_timeout(800)
                    registrar_self._click_submit_no_wait(page_)
                    page_.wait_for_timeout(2000)
                    continue

                # 密码提交后等 auth_code：每 ~5s 打一次进度日志，便于定位卡点
                wait_count += 1
                cur_url = str(page_.url or "")[:90]
                if wait_count % 3 == 0 and cur_url != last_url:
                    anycap_self._l(f"等待 auth_code: url={cur_url} (剩余 {int(deadline - time.time())}s)")
                    last_url = cur_url
                auth_code = registrar_self._wait_for_auth_code(page_, timeout=2)
                if auth_code:
                    return auth_code
                try:
                    page_.wait_for_timeout(1000)
                except Exception:
                    pass
            anycap_self._l(f"auth flow timed out at url={str(page_.url or '')[:100]}")
            return None

        try:
            return guarded_drive(page, password)
        finally:
            # 不还原 registrar._drive_post_identifier_steps（实例方法，实例用完即弃）
            pass

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
        if reason == "anycap_signup_attempts_limited":
            # IP 维度频率风控：换 IP 即可继续，不拉黑邮箱、不拉黑域名。
            # raise 专属异常让上层换 resin session/IP 重试同一邮箱。
            self._l(f"AnyCap IP 维度注册频率受限(too many signup attempts)，不拉黑邮箱，需换 IP 重试: {email}")
            raise AnyCapSignupRateLimited(f"AnyCap IP 维度注册频率受限(too many signup attempts)，请换 IP 重试: {email}")
        if reason == "anycap_email_already_registered":
            # 邮箱已在 AnyCap 注册过（accounts 表有记录但 inventory.used_platforms 漏记 anycap）。
            # 拉黑单邮箱 + 补记 used_platforms=anycap，让领号链路 inventory_platform_already_used
            # 跳过它，避免再被当 unused 重领浪费 90s。不拉黑 outlook.com 域名。
            self._l(f"AnyCap 邮箱已注册过 anycap（already registered），拉黑单邮箱+补记 used_platforms: {email}")
            self._mark_inventory_platform_used(email, platform="anycap")
            self._blacklist_single_mailbox(email, reason)
            raise RuntimeError(f"AnyCap 邮箱已注册过，已拉黑单邮箱+补记 used_platforms: {email}")
        if reason == "anycap_captcha_rejected":
            # 打码 token 被 Auth0 拒绝（跨 session/state 不被接受或 token 失效），
            # 不拉黑邮箱/域名，raise 让上层换 IP/session 或改浏览器点击重试。
            self._l(f"AnyCap Turnstile captcha 被拒（打码 token 不被接受），不拉黑邮箱，需换 IP/session 重试: {email}")
            raise RuntimeError(f"AnyCap Turnstile captcha 被拒，请换 IP/session 或改用浏览器点击重试: {email}")
        # email_domain_not_allowed：域级白名单拦截，拉黑域名合理
        domain = self._blacklist_domain(email, reason)
        raise RuntimeError(f"AnyCap 邮箱域名不允许注册，已拉黑域名: {domain or email}")

    # --- 协议打码：用打码服务 solve_turnstile(url, sitekey) 拿 token 注入 Auth0 表单 ---

    _SOLVER_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/143.0.0.0 Safari/537.36"
    )

    def _extract_turnstile_sitekey(self, page: Any) -> str:
        """从 Auth0 Universal Login 页面提取 Turnstile sitekey。

        优先锚定 Auth0 captcha 容器 #ulp-auth0-v2-captcha（与 AnyCap 复用的
        EnterBrowserRegistrar._click_turnstile_until_token 同源），再回退通用
        [data-captcha-sitekey]/[data-sitekey]/.cf-turnstile，最后 page.content() 正则。
        通用 group 会按文档序命中第一个 .cf-turnstile（可能不带 sitekey），故 Auth0
        容器优先，避免取到无关 widget。
        """
        sitekey = ""
        try:
            sitekey = page.evaluate(
                """() => {
                    const anchors = [
                        '#ulp-auth0-v2-captcha [data-captcha-sitekey]',
                        '#ulp-auth0-v2-captcha [data-sitekey]',
                        '#ulp-auth0-v2-captcha',
                        '[data-captcha-sitekey]',
                        '[data-sitekey]',
                        '.cf-turnstile',
                    ];
                    for (const sel of anchors) {
                        const node = document.querySelector(sel);
                        if (!node) continue;
                        const sk = node.getAttribute('data-captcha-sitekey')
                            || node.getAttribute('data-sitekey') || '';
                        if (sk) return sk;
                    }
                    return '';
                }"""
            ) or ""
        except Exception:
            sitekey = ""
        sitekey = str(sitekey or "").strip()
        if sitekey:
            return sitekey
        try:
            html = page.content() or ""
        except Exception:
            html = ""
        import re
        match = re.search(r"data-captcha-sitekey=['\"]([^'\"]+)['\"]", html, re.I)
        return match.group(1).strip() if match else ""

    def _call_solver(self, signup_url: str, sitekey: str) -> str:
        """调用打码服务 solve_turnstile(url, sitekey)，按签名透传 proxy/user_agent。

        本地浏览器类 solver（CdpTurnstileSolver/local_solver）内部会再开 sync_playwright，
        与 run() 主线程已持有的 sync_playwright session 冲突（'Playwright Sync API inside
        asyncio loop'）。对这类 solver 用 subprocess 进程隔离跑（services.turnstile_solver.cli_solve），
        主线程 sync session 不受影响。远程纯 HTTP 打码（YesCaptcha/2Captcha）直接调不冲突。
        """
        import inspect

        solver = self.captcha_solver
        if solver is None:
            return ""
        solver_name = type(solver).__name__
        # 本地浏览器类 solver 用进程隔离；远程纯 HTTP solver 直接调
        needs_isolation = solver_name in {"CdpTurnstileSolver", "LocalSolverCaptcha", "PatchrightHarvester"}
        if needs_isolation:
            return self._call_solver_subprocess(signup_url, sitekey)

        try:
            params = inspect.signature(solver.solve_turnstile).parameters
        except (ValueError, TypeError):
            params = {}
        kwargs: dict[str, Any] = {}
        if "proxy" in params and self.proxy:
            kwargs["proxy"] = self.proxy
        if "user_agent" in params:
            kwargs["user_agent"] = self._SOLVER_USER_AGENT
        token = str(solver.solve_turnstile(signup_url, sitekey, **kwargs) or "").strip()
        return token

    def _solver_provider_key(self) -> str:
        """从 captcha_solver 实例反推 provider key（cli_solve 入参）。"""
        name = type(self.captcha_solver).__name__
        return {
            "CdpTurnstileSolver": "cdp_turnstile",
            "LocalSolverCaptcha": "local_solver",
            "PatchrightHarvester": "patchright_harvester",
            "YesCaptcha": "yescaptcha_api",
            "TwoCaptcha": "twocaptcha_api",
        }.get(name, "cdp_turnstile")

    def _call_solver_subprocess(self, signup_url: str, sitekey: str) -> str:
        """进程隔离跑 solver（避免与主 sync_playwright session 冲突），单次不重试。

        CdpTurnstileSolver headless Chrome 解 Auth0 Turnstile 不稳定（约 1/3 成功，
        常报 'Page.close: Connection closed' / 'failed to obtain token'）。重试会堆积
        Chrome 进程 + 拖长总时长（3 次 ~360s 超过 batch 280s 超时）。单次试 60s，失败
        立即回退浏览器点击（run() 兜底，~10s 稳定），总流程可控在 ~120s。
        """
        import json as _json
        import subprocess
        import sys

        provider = self._solver_provider_key()
        chrome_path = str(getattr(self.captcha_solver, "chrome_path", "") or "")
        cdp_url = str(getattr(self.captcha_solver, "cdp_url", "") or "")
        cmd = [
            sys.executable, "-m", "services.turnstile_solver.cli_solve",
            "--provider", provider,
            "--url", signup_url,
            "--sitekey", sitekey,
            "--chrome-path", chrome_path,
            "--cdp-url", cdp_url,
        ]
        if self.proxy:
            cmd += ["--proxy", self.proxy]
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT_DIR), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60)
        except subprocess.TimeoutExpired:
            self._l("协议打码：solver 子进程超时 60s，回退浏览器点击")
            return ""
        except Exception as exc:
            self._l(f"协议打码：solver 子进程异常: {exc!r}")
            return ""
        try:
            data = _json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            data = {"ok": False, "error": proc.stdout[-200:] or proc.stderr[-200:]}
        if data.get("ok"):
            return str(data.get("token") or "").strip()
        self._l(f"协议打码：solver 失败: {str(data.get('error'))[:120]}，回退浏览器点击")
        return ""

    def _inject_turnstile_token(self, page: Any, token: str) -> bool:
        """把打码服务返回的 token 注入 Auth0 表单并触发 Turnstile 回调，让 Continue 按钮放行。

        镜像 platforms/cursor/browser_register._inject_turnstile 的防御式注入（Cursor/Venice
        已验证可让 Clerk/WorkOS 的 Turnstile 受控按钮放行）：
        1) override window.turnstile（getResponse 返回 token、isExpired 返回 false），覆盖
           explicit 渲染模式下 Auth0 前端读 token 的入口；
        2) 触发所有已注册 Turnstile callback（_turnstileTokenCallback/turnstileCallback/
           onTurnstileSuccess/cfTurnstileCallback）——Auth0 Universal Login 的 Continue 按钮
           靠这些回调启用，光设 input.value 不触发回调时按钮仍 disabled；
        3) 建/填 captcha + cf-turnstile-response 隐藏域（不存在则创建）+ dispatch input/change；
        4) postMessage Cloudflare iframe 兜底。
        Auth0 提交字段优先 captcha（与 Enter._read_turnstile_token 读序一致）。
        """
        safe = str(token or "").replace("\\", "\\\\").replace("'", "\\'")
        script = f"""(function() {{
            const token = '{safe}';
            if (window.turnstile) {{
                const orig = window.turnstile;
                window.turnstile = new Proxy(orig, {{
                    get(target, prop) {{
                        if (prop === 'getResponse') return () => token;
                        if (prop === 'isExpired') return () => false;
                        return Reflect.get(target, prop);
                    }}
                }});
            }}
            const fns = [window._turnstileTokenCallback, window.turnstileCallback, window.onTurnstileSuccess, window.cfTurnstileCallback];
            fns.forEach(fn => {{ if (typeof fn === 'function') {{ try {{ fn(token); }} catch(e) {{}} }} }});
            const names = ['captcha', 'cf-turnstile-response'];
            const form = document.querySelector('form') || document.body;
            names.forEach(name => {{
                let f = document.querySelector("input[name='" + name + "'], textarea[name='" + name + "']");
                if (!f) {{ f = document.createElement('input'); f.type = 'hidden'; f.name = name; form.appendChild(f); }}
                f.value = token;
                f.dispatchEvent(new Event('input', {{bubbles: true}}));
                f.dispatchEvent(new Event('change', {{bubbles: true}}));
            }});
            try {{
                document.querySelectorAll('iframe').forEach(iframe => {{
                    if (iframe.src && iframe.src.includes('cloudflare.com')) {{
                        iframe.contentWindow.postMessage(JSON.stringify({{source: 'cloudflare-challenge', token: token}}), '*');
                    }}
                }});
            }} catch(e) {{}}
            return true;
        }})();"""
        try:
            return bool(page.evaluate(script))
        except Exception:
            return False

    def _solve_turnstile_via_solver(self, page: Any) -> str:
        """协议打码主路径：提 sitekey → 调打码服务 → 注入 token。返回 token（空串=失败）。"""
        # 始终用新建的 Auth0 signup URL 调打码服务（不带当前事务 state）：Turnstile widget
        # 在 signup 首页，solver 加载该页才能渲染 widget 解题。用 page.url（可能已跳到
        # email-identifier/password 等后续页，无 Turnstile widget）会让 solver 拿不到 token。
        # Turnstile token 是 sitekey 级有效（不绑 state），Auth0 接受跨事务 token。
        page_url = _build_auth0_signup_url()
        sitekey = self._extract_turnstile_sitekey(page)
        if not sitekey:
            self._l("协议打码：未提取到 Turnstile sitekey，回退浏览器点击")
            return ""
        self._l(f"协议打码：sitekey={sitekey[:12]}… 调用 {type(self.captcha_solver).__name__}.solve_turnstile")
        token = self._call_solver(page_url, sitekey)
        if not token:
            self._l("协议打码：打码服务返回空 token，回退浏览器点击")
            return ""
        self._inject_turnstile_token(page, token)
        self._l(f"协议打码：已注入 Turnstile token (len={len(token)})")
        return token

    def _submit_button_enabled(self, page: Any) -> bool:
        """检查 Auth0 Continue/submit 按钮是否可点（非 disabled）。"""
        try:
            return bool(page.evaluate(
                """() => {
                    const btn = document.querySelector(
                        "button[type='submit']:not([aria-hidden='true'])"
                    );
                    if (!btn) return true;
                    return !btn.disabled;
                }"""
            ))
        except Exception:
            return True

    def _clear_turnstile_field(self, page: Any) -> None:
        """清空注入的 captcha token，让回退浏览器点击能触发真实 Turnstile 求解。"""
        try:
            page.evaluate(
                """() => {
                    const names = ['captcha', 'cf-turnstile-response'];
                    names.forEach(name => {
                        const el = document.querySelector("input[name='" + name + "'], textarea[name='" + name + "']");
                        if (el) { el.value = ''; }
                    });
                }"""
            )
        except Exception:
            pass

    def _pw_proxy(self) -> dict | None:
        """把 resin 代理 URL 解析成 Playwright proxy 格式（server/username/password）。

        EnterBrowserRegistrar._prepare_chrome 启动 Chrome 时不传 --proxy-server（Chrome 命令行
        代理不支持 username/password 认证，resin 代理要认证会 407）。故有 resin proxy 时改用
        Playwright launch(proxy=) 启动 Chrome（内置支持认证），让浏览器走 resin 出口 IP，
        Auth0 看到的是 resin IP（可换 IP 避风控），而非本机 IP。
        """
        if not self.proxy:
            return None
        from urllib.parse import urlsplit
        pp = urlsplit(self.proxy)
        pw_proxy: dict[str, Any] = {"server": f"{pp.scheme}://{pp.hostname}:{pp.port}"}
        if pp.username:
            pw_proxy["username"] = pp.username
        if pp.password:
            pw_proxy["password"] = pp.password
        return pw_proxy

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
        # 有 resin proxy 时用 Playwright launch(proxy=) 启动 Chrome（支持认证，走 resin IP）；
        # 无 proxy 时用 EnterBrowserRegistrar._prepare_chrome（subprocess Chrome，本机 IP）。
        pw_proxy = self._pw_proxy()
        launch_meta = None if pw_proxy else registrar._prepare_chrome()
        if pw_proxy:
            self._l(f"浏览器走 resin 代理（Playwright launch 带认证）: {pw_proxy['server']}")
        browser = None
        page = None
        auth_code = ""
        captured_code = {"code": ""}
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as pw:
                if pw_proxy:
                    # Playwright launch 带 proxy 认证，Chrome 走 resin 出口 IP
                    launch_args = ["--no-first-run", "--no-default-browser-check", "--disable-blink-features=AutomationControlled"]
                    browser = pw.chromium.launch(headless=False, args=launch_args, proxy=pw_proxy)
                    context = browser.new_context(user_agent=self._SOLVER_USER_AGENT)
                    page = context.new_page()
                else:
                    browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.new_page()
                # 监听 /authorize/resume 的 302 Location 捕获 auth_code：Chrome 走 resin 加载
                # anycap.ai callback 可能失败（chrome-error），page.url 丢 code。直接从 302
                # Location 提取 code= 参数，不依赖 Chrome 加载 redirect 目标成功。
                def _capture_code(resp):
                    try:
                        loc = resp.headers.get("location", "") or ""
                        if "/api/auth/callback" in loc and "code=" in loc:
                            import urllib.parse as _up
                            m = _up.parse_qs(_up.urlparse(loc).query).get("code") or [""]
                            if m[0]:
                                captured_code["code"] = m[0]
                    except Exception:
                        pass
                page.on("response", _capture_code)
                self._l("打开 Auth0 邮箱注册页")
                # goto 重试：resin 代理偶发 ERR_CONNECTION_RESET / 连接断开，重试 3 次
                signup_url = _build_auth0_signup_url()
                goto_ok = False
                for goto_attempt in range(3):
                    try:
                        page.goto(signup_url, wait_until="domcontentloaded", timeout=60000)
                        goto_ok = True
                        break
                    except Exception as exc:
                        self._l(f"goto 异常 (attempt {goto_attempt+1}/3): {str(exc)[:80]}")
                        if goto_attempt < 2:
                            import time as _t
                            _t.sleep(3)
                if not goto_ok:
                    raise RuntimeError("AnyCap 打开 Auth0 signup 页失败（resin 代理连接不稳，重试 3 次仍失败）")
                email_selector = "input[name='email'], input[type='email'], input#username"
                page.wait_for_selector(email_selector, timeout=30_000)
                page.locator(email_selector).first.fill(email)
                page.wait_for_timeout(800)
                self._raise_if_signup_blocked(email, page=page)
                # Turnstile：优先协议打码（solve_turnstile 拿 token 注入表单 + 触发回调），
                # 未配置打码服务或打码失败时回退浏览器内点击复选框兜底。
                turnstile_token = ""
                try:
                    if self.captcha_solver is not None:
                        turnstile_token = self._solve_turnstile_via_solver(page)
                        # 注入 token 后若 Continue 按钮仍 disabled（Auth0 Universal Login 靠
                        # Turnstile 回调启用按钮，注入未触发回调时按钮仍 disabled），清空注入值
                        # 回退浏览器点击触发真实回调，避免 submit 点不动→90s 超时。
                        if turnstile_token and not self._submit_button_enabled(page):
                            self._l("协议打码：注入 token 后 Continue 仍 disabled，清空注入值回退浏览器点击")
                            self._clear_turnstile_field(page)
                            turnstile_token = registrar._click_turnstile_until_token(page) or ""
                    else:
                        self._l("未配置打码服务，浏览器点击 Turnstile")
                        turnstile_token = registrar._click_turnstile_until_token(page) or ""
                except Exception as exc:
                    self._l(f"Turnstile 解题异常: {exc}")
                    self._raise_if_signup_blocked(email, page=page, error=exc)
                if not turnstile_token and self.captcha_solver is not None:
                    try:
                        self._l("协议打码未拿到 token，回退浏览器点击 Turnstile")
                        turnstile_token = registrar._click_turnstile_until_token(page) or ""
                    except Exception as exc:
                        self._l(f"回退点击 Turnstile 异常: {exc}")
                        self._raise_if_signup_blocked(email, page=page, error=exc)
                if not turnstile_token:
                    self._l("Turnstile token 为空，继续尝试提交（Auth0 可能未要求 captcha）")
                registrar._click_submit_no_wait(page)
                page.wait_for_timeout(1200)
                self._raise_if_signup_blocked(email, page=page)
                try:
                    auth_code = self._drive_post_identifier_with_already_registered_guard(
                        registrar, page, password, email
                    ) or ""
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
            # Playwright launch 模式（pw_proxy）无需 _teardown_chrome（launch_meta=None）；
            # subprocess Chrome 模式才需杀进程 + 删 profile
            if launch_meta is not None:
                registrar._teardown_chrome(launch_meta)

        if not auth_code and captured_code["code"]:
            auth_code = captured_code["code"]
            self._l(f"从 302 Location 捕获 auth_code (len={len(auth_code)})")
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
