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

    def _goto(self, page: Any, url: str, *, label: str, wait_until: str = "domcontentloaded", timeout: int = 60000) -> Any:
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout)
        except Exception as exc:
            current_url = str(getattr(page, "url", "") or "")
            raise RuntimeError(f"{label}打开失败: {exc}; current_url={current_url}") from exc

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
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise RuntimeError("未安装 playwright，无法执行 Swarms 可视浏览器注册") from exc

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

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                viewport={"width": 1365, "height": 900},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
                locale="en-US",
                timezone_id="Asia/Shanghai",
            )
            context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            try:
                self._probe_browser_proxy(page)
                self._goto(page, SIGNUP_URL, label="Swarms 注册页")
                email_input = page.locator("input[name='email'], input[type='email']")
                password_input = page.locator("input[name='password'], input[type='password']")
                if email_input.count() < 1 or password_input.count() < 1:
                    raise RuntimeError("Swarms 注册页未找到邮箱/密码输入框")
                email_input.first.fill(email, timeout=12000)
                password_input.first.fill(password, timeout=12000)
                submit = page.locator("form button[type='submit'], button[type='submit']")
                if submit.count() < 1:
                    raise RuntimeError("Swarms 注册页未找到提交按钮")
                submit.first.click(timeout=12000)
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
                cookies = context.cookies("https://swarms.world")
                if not any(item.get("name") == "sb-db-auth-token" for item in cookies):
                    raise RuntimeError("邮箱验证后未获得 Swarms 登录 Cookie")
                self._goto(page, ACCOUNT_URL, label="Swarms 账户页")
                page.wait_for_timeout(4000)
                self._goto(page, API_KEYS_URL, label="Swarms API Key 页")
                page.wait_for_timeout(3000)
                cookies = context.cookies("https://swarms.world")
            finally:
                browser.close()

        session = self._session_from_cookies(cookies)
        user = self._trpc_get(session, "main.getUser")
        credit = self._trpc_get(session, "panel.getUserCredit")
        try:
            credit_value = float(credit)
        except Exception:
            credit_value = 0.0
        self.log(f"浏览器注册额度: ${credit_value:g}")
        if credit_value < 0.01:
            raise RuntimeError(f"Swarms 额度未到账，跳过创建 API Key: ${credit_value:g}")
        api_key_info = self._trpc_post(session, "apiKey.addApiKey", {"name": "auto-register"})
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
