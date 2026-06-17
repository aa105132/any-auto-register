"""Swarms 可视/无头浏览器邮箱注册 worker。"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import requests

from core.proxy_utils import build_playwright_proxy_settings

SIGNUP_URL = "https://swarms.world/signin/signup"
ACCOUNT_URL = "https://swarms.world/platform/account?tab=billing"
API_KEYS_URL = "https://swarms.world/platform/api-keys"


def _clean_verify_link(url: str) -> str:
    cleaned = str(url or "").strip().strip("<>\"'")
    cleaned = re.sub(r"(?i)(/auth/callback)\](\?)", r"\1\2", cleaned)
    return cleaned.rstrip(").,;'\"]}>")


def _unwrap_trpc(data: Any) -> Any:
    if isinstance(data, list) and len(data) == 1:
        return _unwrap_trpc(data[0])
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict):
            data_part = result.get("data", result)
            if isinstance(data_part, dict) and "json" in data_part:
                return data_part.get("json")
            return _unwrap_trpc(data_part)
    return data


class SwarmsBrowserMailboxWorker:
    def __init__(
        self,
        *,
        headless: bool = False,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.headless = bool(headless)
        self.proxy = proxy
        self.log = log_fn

    def _session_from_cookies(self, cookies: list[dict]) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://swarms.world",
            "Referer": API_KEYS_URL,
        })
        if self.proxy:
            session.proxies = {"http": self.proxy, "https": self.proxy}
        for cookie in cookies:
            name = str(cookie.get("name") or "")
            value = str(cookie.get("value") or "")
            if not name:
                continue
            session.cookies.set(
                name,
                value,
                domain=str(cookie.get("domain") or ".swarms.world"),
                path=str(cookie.get("path") or "/"),
            )
        return session

    @staticmethod
    def _auth_cookie_tokens(cookies: list[dict]) -> tuple[str, str, str]:
        raw = ""
        for cookie in cookies:
            if cookie.get("name") == "sb-db-auth-token":
                raw = str(cookie.get("value") or "")
                break
        access_token = ""
        refresh_token = ""
        user_id = ""
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                access_token = str(payload.get("access_token") or "")
                refresh_token = str(payload.get("refresh_token") or "")
                user = payload.get("user") or {}
                if isinstance(user, dict):
                    user_id = str(user.get("id") or "")
            elif isinstance(payload, list):
                access_token = str(payload[0] if len(payload) > 0 else "")
                refresh_token = str(payload[1] if len(payload) > 1 else "")
        except Exception:
            pass
        return access_token, refresh_token, user_id

    @staticmethod
    def _cookie_map(cookies: list[dict]) -> dict[str, str]:
        return {str(item.get("name") or ""): str(item.get("value") or "") for item in cookies if item.get("name")}

    def _trpc_get(self, session: requests.Session, path: str) -> Any:
        resp = session.get(f"https://swarms.world/api/trpc/{path}", timeout=30, proxies=session.proxies or None)
        if resp.status_code >= 400:
            raise RuntimeError(f"{path} HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"{path} error: {json.dumps(data, ensure_ascii=False)[:300]}")
        return _unwrap_trpc(data)

    def _trpc_post(self, session: requests.Session, path: str, payload: dict) -> Any:
        resp = session.post(f"https://swarms.world/api/trpc/{path}", json={"json": payload}, timeout=30, proxies=session.proxies or None)
        if resp.status_code >= 400:
            raise RuntimeError(f"{path} HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"{path} error: {json.dumps(data, ensure_ascii=False)[:300]}")
        return _unwrap_trpc(data)

    def _browser_trpc(self, page: Any, path: str, payload: dict | None = None, *, method: str = "GET") -> Any:
        method_name = str(method or "GET").upper()
        last_error = ""
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            result = page.evaluate(
                """async ({ path, payload, method }) => {
                    const response = await fetch(`/api/trpc/${path}`, {
                        method,
                        credentials: 'include',
                        headers: {
                            'Accept': 'application/json',
                            'Content-Type': 'application/json'
                        },
                        body: method === 'GET' ? undefined : JSON.stringify({ json: payload || {} })
                    });
                    return {
                        status: response.status,
                        text: await response.text()
                    };
                }""",
                {"path": path, "payload": payload or {}, "method": method_name},
            )
            status = int((result or {}).get("status") or 0)
            text = str((result or {}).get("text") or "")
            if self._looks_like_vercel_checkpoint(text):
                last_error = f"{path} HTTP {status}: {text[:300]}"
                if attempt < max_attempts:
                    self._resolve_trpc_checkpoint(page, path, method=method_name, attempt=attempt)
                    continue
                raise RuntimeError(last_error)
            if status >= 400:
                raise RuntimeError(f"{path} HTTP {status}: {text[:300]}")
            try:
                data = json.loads(text)
            except Exception:
                return {"raw": text}
            if isinstance(data, dict) and data.get("error"):
                error_text = json.dumps(data, ensure_ascii=False)
                if self._looks_like_vercel_checkpoint(error_text):
                    last_error = f"{path} error: {error_text[:300]}"
                    if attempt < max_attempts:
                        self._resolve_trpc_checkpoint(page, path, method=method_name, attempt=attempt)
                        continue
                raise RuntimeError(f"{path} error: {error_text[:300]}")
            return _unwrap_trpc(data)
        raise RuntimeError(last_error or f"{path} 调用失败")

    def _goto(self, page: Any, url: str, *, label: str, wait_until: str = "domcontentloaded", timeout: int = 60000) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                return page.goto(url, wait_until=wait_until, timeout=timeout)
            except Exception as exc:
                last_exc = exc
                message = str(exc)
                retriable = (
                    "NS_BINDING_ABORTED" in message
                    or "ERR_ABORTED" in message
                    or "frame was detached" in message
                    or "Timeout" in message
                )
                if attempt < 3 and retriable:
                    self.log(f"{label}导航中断，第 {attempt}/3 次重试: {message[:160]}")
                    try:
                        page.wait_for_timeout(1500)
                    except Exception:
                        pass
                    continue
                current_url = str(getattr(page, "url", "") or "")
                raise RuntimeError(f"{label}打开失败: {exc}; current_url={current_url}") from exc
        current_url = str(getattr(page, "url", "") or "")
        raise RuntimeError(f"{label}打开失败: {last_exc}; current_url={current_url}")

    @staticmethod
    def _looks_like_vercel_checkpoint(text: str) -> bool:
        body = str(text or "")
        return (
            "Vercel Security Checkpoint" in body
            or "Security Checkpoint" in body
            or "无法验证您的浏览器" in body
            or "正在验证您的浏览器" in body
            or "We're verifying your browser" in body
        )

    def _body_preview(self, page: Any, *, limit: int = 300) -> str:
        try:
            return str(page.locator("body").inner_text(timeout=5000) or "").strip()[:limit]
        except Exception:
            return ""

    def _checkpoint_probe_text(self, page: Any) -> str:
        parts: list[str] = []
        try:
            parts.append(str(page.title() or ""))
        except Exception:
            pass
        parts.append(self._body_preview(page, limit=800))
        return "\n".join(item for item in parts if item)

    def _wait_until_not_vercel_checkpoint(self, page: Any, *, label: str, timeout_ms: int = 45000) -> bool:
        waited_ms = 0
        logged = False
        while waited_ms <= timeout_ms:
            text = self._checkpoint_probe_text(page)
            if not self._looks_like_vercel_checkpoint(text):
                return True
            if not logged:
                self.log(f"{label} 命中 Vercel Security Checkpoint，等待浏览器自动通过...")
                logged = True
            page.wait_for_timeout(2500)
            waited_ms += 2500
        return False

    def _resolve_trpc_checkpoint(self, page: Any, path: str, *, method: str, attempt: int) -> None:
        # fetch 拿到 checkpoint HTML 时不会渲染挑战页；必须用浏览器顶层导航跑完挑战。
        probe_path = path if method == "GET" else "main.getUser"
        self.log(f"{path} 第 {attempt} 次命中 Vercel checkpoint，顶层打开 tRPC 预热后重试...")
        self._goto(
            page,
            f"https://swarms.world/api/trpc/{probe_path}",
            label=f"Swarms tRPC checkpoint 预热({probe_path})",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        self._wait_until_not_vercel_checkpoint(page, label=f"Swarms tRPC checkpoint 预热({probe_path})", timeout_ms=60000)
        page.wait_for_timeout(1500)
        self._goto(page, API_KEYS_URL, label="Swarms API Key 页")
        self._wait_until_not_vercel_checkpoint(page, label="Swarms API Key 页", timeout_ms=45000)
        page.wait_for_timeout(1500)

    def _retry_call(self, label: str, fn: Callable[[], Any], *, attempts: int = 5, delay_ms: int = 2500) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return fn()
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self.log(f"{label} 第 {attempt}/{attempts} 次失败，继续重试: {str(exc)[:180]}")
                try:
                    import time

                    time.sleep(max(delay_ms, 0) / 1000)
                except Exception:
                    pass
        if last_error:
            raise last_error
        raise RuntimeError(f"{label} 失败")

    def _click_submit(self, submit: Any) -> None:
        try:
            submit.first.click(timeout=12000)
            return
        except Exception as exc:
            self.log(f"Swarms 注册提交按钮普通点击失败，尝试 force click: {str(exc)[:160]}")
        try:
            submit.first.click(timeout=8000, force=True)
            return
        except Exception as exc:
            self.log(f"Swarms 注册提交按钮 force click 失败，尝试 DOM click: {str(exc)[:160]}")
        handle = submit.first.element_handle(timeout=8000)
        if handle is None:
            raise RuntimeError("Swarms 注册页提交按钮无法获取 element handle")
        handle.evaluate("(node) => node.click()")

    def _hold_headed_failure(self, page: Any, reason: str) -> None:
        if self.headless:
            return
        self.log(f"Swarms 可视浏览器注册页不可用，保留窗口 10 秒便于查看: {reason[:160]}")
        try:
            page.wait_for_timeout(10000)
        except Exception:
            pass

    def _run_protocol_fallback(
        self,
        *,
        email: str,
        password: str,
        verification_link_callback: Callable[[], str],
        reason: str,
    ) -> dict:
        self.log(f"Swarms 浏览器注册页不可用，回退协议注册: {reason[:180]}")
        from platforms.swarms.protocol_mailbox import SwarmsProtocolMailboxWorker

        worker = SwarmsProtocolMailboxWorker(proxy=self.proxy, log_fn=self.log)
        result = worker.run(
            email=email,
            password=password,
            verification_link_callback=verification_link_callback,
        )
        result["browser_fallback_reason"] = reason
        result["browser_registered"] = False
        return result

    def _probe_browser_proxy(self, page: Any) -> str:
        if not self.proxy:
            return ""
        try:
            page.goto("https://api.ipify.org", wait_until="domcontentloaded", timeout=20000)
            text = str(page.locator("body").inner_text(timeout=5000) or "").strip()
        except Exception as exc:
            raise RuntimeError(
                f"Swarms 浏览器代理预检失败：代理不可用或 Playwright 代理认证失败: {exc}"
            ) from exc
        if not text:
            raise RuntimeError("Swarms 浏览器代理预检失败：代理出口 IP 响应为空")
        self.log(f"Swarms 浏览器代理出口 IP: {text[:120]}")
        return text

    def run(
        self,
        *,
        email: str,
        password: str,
        verification_link_callback: Callable[[], str] | None = None,
    ) -> dict:
        if not verification_link_callback:
            raise RuntimeError("Swarms 浏览器注册需要 verification_link_callback")

        self.log("打开 Swarms 注册页（浏览器模式）...")
        launch_kwargs: dict[str, Any] = {
            "headless": self.headless,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--window-size=1365,900",
            ],
        }
        if self.proxy:
            try:
                proxy_settings = build_playwright_proxy_settings(self.proxy)
            except ValueError as exc:
                raise RuntimeError(f"Swarms 浏览器代理格式错误: {exc}") from exc
            if proxy_settings:
                launch_kwargs["proxy"] = proxy_settings

        browser_kind = "chromium"

        def _open_browser():
            nonlocal browser_kind
            try:
                from camoufox.sync_api import Camoufox

                browser_kind = "camoufox"
                camoufox_kwargs: dict[str, Any] = {
                    "headless": self.headless,
                    "humanize": True,
                }
                if "proxy" in launch_kwargs:
                    camoufox_kwargs["proxy"] = launch_kwargs["proxy"]
                self.log("使用 Camoufox 打开 Swarms 注册页...")
                return Camoufox(**camoufox_kwargs)
            except Exception as camoufox_exc:
                self.log(f"Camoufox 不可用，回退 Chromium: {camoufox_exc}")
                try:
                    from playwright.sync_api import sync_playwright
                except Exception as exc:
                    raise RuntimeError("未安装 playwright，无法执行 Swarms 可视浏览器注册") from exc

                class _ChromiumBrowserContext:
                    def __enter__(self):
                        self._playwright_context = sync_playwright()
                        self._playwright = self._playwright_context.__enter__()
                        self._browser = self._playwright.chromium.launch(**launch_kwargs)
                        return self._browser

                    def __exit__(self, *_args):
                        try:
                            self._browser.close()
                        finally:
                            self._playwright_context.__exit__(*_args)
                        return False

                return _ChromiumBrowserContext()

        with _open_browser() as browser:
            if browser_kind == "camoufox":
                context = browser.new_context(
                    viewport={"width": 1365, "height": 900},
                    locale="en-US",
                    timezone_id="Asia/Shanghai",
                )
            else:
                context = browser.new_context(
                    viewport={"width": 1365, "height": 900},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                    locale="en-US",
                    timezone_id="Asia/Shanghai",
                )
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = None
            protocol_fallback_reason = ""
            browser_user: Any = None
            browser_credit: Any = None
            browser_api_key_info: Any = None
            browser_api_key = ""
            phase = "proxy_probe"
            try:
                if self.proxy:
                    probe_page = context.new_page()
                    try:
                        self._probe_browser_proxy(probe_page)
                    finally:
                        try:
                            probe_page.close()
                        except Exception:
                            pass
                phase = "open_signup"
                page = context.new_page()
                self._goto(page, SIGNUP_URL, label="Swarms 注册页")
                phase = "find_inputs"
                email_input = page.locator("input[name='email'], input[type='email']")
                password_input = page.locator("input[name='password'], input[type='password']")
                deadline_ms = 35000 if browser_kind == "camoufox" else 12000
                waited_ms = 0
                while (email_input.count() < 1 or password_input.count() < 1) and waited_ms < deadline_ms:
                    page.wait_for_timeout(1000)
                    waited_ms += 1000
                if email_input.count() < 1 or password_input.count() < 1:
                    body = self._body_preview(page)
                    if self._looks_like_vercel_checkpoint(body):
                        raise RuntimeError("Swarms 注册页是 Vercel Security Checkpoint，不存在邮箱/密码输入框")
                    raise RuntimeError(f"Swarms 注册页未找到邮箱/密码输入框；页面预览: {body[:180]}")
                email_input.first.fill(email, timeout=12000)
                password_input.first.fill(password, timeout=12000)
                submit = page.locator("form button[type='submit'], button[type='submit']")
                if submit.count() < 1:
                    raise RuntimeError("Swarms 注册页未找到提交按钮")
                phase = "submit_signup"
                self._click_submit(submit)
                page.wait_for_timeout(6000)
                if "/signin/signup" in page.url:
                    body = ""
                    try:
                        body = page.locator("body").inner_text(timeout=5000)
                    except Exception:
                        pass
                    if "Sign-up limit reached" in body:
                        raise RuntimeError("Swarms 注册限制: Please wait 24 hours before creating another account")
                self.log("注册提交成功，等待邮箱确认链接...")
                verify_link = _clean_verify_link(verification_link_callback())
                self._goto(page, verify_link, label="Swarms 邮箱确认链接")
                page.wait_for_timeout(8000)
                self._wait_until_not_vercel_checkpoint(page, label="Swarms 邮箱确认后页面", timeout_ms=45000)
                cookies = context.cookies("https://swarms.world")
                if not any(item.get("name") == "sb-db-auth-token" for item in cookies):
                    raise RuntimeError("邮箱验证后未获得 Swarms 登录 Cookie")
                try:
                    self._goto(page, ACCOUNT_URL, label="Swarms 账户页")
                    page.wait_for_timeout(4000)
                    self._wait_until_not_vercel_checkpoint(page, label="Swarms 账户页", timeout_ms=45000)
                except RuntimeError as exc:
                    self.log(f"Swarms 账户页打开失败，保留 Cookie 并继续协议会话兜底: {str(exc)[:180]}")
                try:
                    self._goto(page, API_KEYS_URL, label="Swarms API Key 页")
                    page.wait_for_timeout(3000)
                    self._wait_until_not_vercel_checkpoint(page, label="Swarms API Key 页", timeout_ms=45000)
                except RuntimeError as exc:
                    self.log(f"Swarms API Key 页打开失败，保留 Cookie 并继续协议会话兜底: {str(exc)[:180]}")
                cookies = context.cookies("https://swarms.world")
                try:
                    try:
                        browser_user = self._retry_call(
                            "浏览器 main.getUser",
                            lambda: self._browser_trpc(page, "main.getUser", method="GET"),
                            attempts=2,
                            delay_ms=200,
                        )
                    except Exception as exc:
                        self.log(f"浏览器 main.getUser 失败，稍后回退协议会话: {str(exc)[:180]}")
                    try:
                        browser_credit = self._retry_call(
                            "浏览器 panel.getUserCredit",
                            lambda: self._browser_trpc(page, "panel.getUserCredit", method="GET"),
                            attempts=2,
                            delay_ms=200,
                        )
                        credit_value = SwarmsBrowserMailboxWorker._credit_amount(browser_credit)
                        self.log(f"浏览器注册额度: ${credit_value:g}")
                        if credit_value < 0.01:
                            self.log(f"Swarms 额度显示为 ${credit_value:g}，仍尝试创建 API Key 以适配站点当前策略")
                    except Exception as exc:
                        self.log(f"浏览器 panel.getUserCredit 失败，稍后回退协议会话: {str(exc)[:180]}")
                    try:
                        browser_api_key_info = self._retry_call(
                            "浏览器 apiKey.addApiKey",
                            lambda: self._browser_trpc(
                                page,
                                "apiKey.addApiKey",
                                {"name": "auto-register"},
                                method="POST",
                            ),
                            attempts=3,
                            delay_ms=300,
                        )
                    except Exception as exc:
                        self.log(f"浏览器创建 API Key 失败，回退协议会话继续创建: {str(exc)[:180]}")
                        browser_api_key_info = None
                    if isinstance(browser_api_key_info, dict):
                        browser_api_key = str(browser_api_key_info.get("key") or browser_api_key_info.get("apiKey") or "")
                    if not browser_api_key:
                        self.log("浏览器态未拿到 API Key，保留登录 Cookie 并进入协议会话创建")
                except Exception as exc:
                    self.log(
                        f"浏览器内 tRPC 调用未完成，回退 requests/协议会话继续查额度和创建 API Key: {str(exc)[:180]}"
                    )
            except RuntimeError as exc:
                if phase in {"open_signup", "find_inputs"}:
                    protocol_fallback_reason = str(exc)
                    if page is not None:
                        self._hold_headed_failure(page, protocol_fallback_reason)
                else:
                    raise
            if protocol_fallback_reason:
                return self._run_protocol_fallback(
                    email=email,
                    password=password,
                    verification_link_callback=verification_link_callback,
                    reason=protocol_fallback_reason,
                )

        if browser_api_key:
            user = browser_user
            credit_value = SwarmsBrowserMailboxWorker._credit_amount(browser_credit)
            api_key_info = browser_api_key_info if isinstance(browser_api_key_info, dict) else {}
            api_key = browser_api_key
        else:
            session = self._session_from_cookies(cookies)
            try:
                user = browser_user if isinstance(browser_user, dict) else self._retry_call(
                    "协议 main.getUser",
                    lambda: self._trpc_get(session, "main.getUser"),
                    attempts=5,
                    delay_ms=2500,
                )
            except Exception as exc:
                user = {}
                self.log(f"协议 main.getUser 失败，使用 Cookie 用户信息继续: {str(exc)[:180]}")
            credit = browser_credit
            if credit is None:
                credit = self._retry_call(
                    "协议 panel.getUserCredit",
                    lambda: self._trpc_get(session, "panel.getUserCredit"),
                    attempts=5,
                    delay_ms=2500,
                )
            credit_value = SwarmsBrowserMailboxWorker._credit_amount(credit)
            self.log(f"浏览器注册额度: ${credit_value:g}")
            if credit_value < 0.01:
                raise RuntimeError(f"Swarms 额度未到账，跳过创建 API Key: ${credit_value:g}")
            api_key_info = self._retry_call(
                "协议 apiKey.addApiKey",
                lambda: self._trpc_post(session, "apiKey.addApiKey", {"name": "auto-register"}),
                attempts=6,
                delay_ms=3000,
            )
            api_key = str(api_key_info.get("key") or api_key_info.get("apiKey") or "") if isinstance(api_key_info, dict) else ""
            if not api_key:
                raise RuntimeError("浏览器注册后未能创建 Swarms API Key")
        access_token, refresh_token, cookie_user_id = self._auth_cookie_tokens(cookies)
        user = user if isinstance(user, dict) else {}
        user_id = str(user.get("id") or cookie_user_id or "")
        cookie_map = self._cookie_map(cookies)
        session_cookie = "; ".join(f"{k}={v}" for k, v in cookie_map.items() if v)
        return {
            "email": email,
            "password": password,
            "user_id": user_id,
            "user_name": str(user.get("full_name") or ""),
            "username": str(user.get("username") or ""),
            "profile": user,
            "credit_info": {"data": credit_value},
            "api_key": api_key,
            "api_key_info": api_key_info if isinstance(api_key_info, dict) else {},
            "access_token": access_token,
            "refresh_token": refresh_token,
            "user_info": user,
            "cookies": cookie_map,
            "session_cookie": session_cookie,
            "browser_registered": True,
        }

    @staticmethod
    def _credit_amount(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for key in ("credit", "credits", "balance", "amount", "data"):
                if key not in value:
                    continue
                amount = SwarmsBrowserMailboxWorker._credit_amount(value.get(key))
                if amount:
                    return amount
        try:
            return float(str(value).strip())
        except Exception:
            return 0.0
