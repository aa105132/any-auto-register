"""Anuma 浏览器注册流程（Camoufox）。"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

ANUMA_URL = "https://chat.anuma.ai/zh-CN"
DEBUG_DIR = Path(tempfile.gettempdir()) / "any-auto-register-anuma"


def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _find_visible(page, selectors: list[str]):
    for selector in selectors:
        try:
            for element in page.query_selector_all(selector):
                try:
                    if element.is_visible():
                        return element, selector
                except Exception:
                    continue
        except Exception:
            continue
    return None, ""


def _wait_visible(page, selectors: list[str], timeout: int = 30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        element, selector = _find_visible(page, selectors)
        if element:
            return element, selector
        time.sleep(0.2)
    return None, ""


def _dump_debug(page, prefix: str) -> None:
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"{prefix}.png"), full_page=True)
        (DEBUG_DIR / f"{prefix}.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def _read_storage(page, storage_name: str) -> dict[str, str]:
    try:
        result = page.evaluate(
            """(target) => {
                const storage = window[target];
                const output = {};
                if (!storage) return output;
                for (const key of Object.keys(storage)) {
                    output[key] = storage.getItem(key);
                }
                return output;
            }""",
            storage_name,
        )
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _cookies_to_map(page) -> dict[str, str]:
    try:
        return {
            str(item.get("name", "") or ""): str(item.get("value", "") or "")
            for item in page.context.cookies()
            if item.get("name")
        }
    except Exception:
        return {}


def _decode_storage_value(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        decoded = json.loads(raw)
        return str(decoded or "")
    except Exception:
        return raw


def _dismiss_cookie_banner(page, log_fn: Callable[[str], None]) -> None:
    selectors = [
        'button:has-text("全部接受")',
        'button:has-text("Accept all")',
        'button:has-text("接受")',
    ]
    element, selector = _find_visible(page, selectors)
    if not element:
        return
    try:
        element.click()
        time.sleep(0.5)
        log_fn(f"已处理 Cookie 弹窗: {selector}")
    except Exception:
        pass


def _check_terms(page, log_fn: Callable[[str], None]) -> bool:
    selectors = [
        'label[for="terms-checkbox"]',
        'label:has-text("我同意")',
        'label:has-text("I agree")',
    ]

    try:
        checked = page.evaluate(
            """() => {
                const input = document.querySelector('#terms-checkbox');
                return !!(input && input.checked);
            }"""
        )
        if checked:
            return True
    except Exception:
        pass

    for selector in selectors:
        element, _ = _find_visible(page, [selector])
        if not element:
            continue
        try:
            element.click(force=True)
            time.sleep(0.3)
            checked = page.evaluate(
                """() => {
                    const input = document.querySelector('#terms-checkbox');
                    return !!(input && input.checked);
                }"""
            )
            if checked:
                log_fn(f"已勾选条款: {selector}")
                return True
        except Exception:
            continue
    return False


def _wait_for_auth_state(page, timeout: int = 90) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    deadline = time.time() + timeout
    cookies = {}
    local_storage = {}
    session_storage = {}
    while time.time() < deadline:
        cookies = _cookies_to_map(page)
        local_storage = _read_storage(page, "localStorage")
        session_storage = _read_storage(page, "sessionStorage")
        if cookies.get("privy-token") or local_storage.get("privy:token"):
            return cookies, local_storage, session_storage
        time.sleep(1)
    return cookies, local_storage, session_storage


class AnumaBrowserRegister:
    def __init__(
        self,
        *,
        headless: bool,
        proxy: Optional[str] = None,
        otp_callback: Optional[Callable[[], str]] = None,
        log_fn: Callable[[str], None] = print,
    ):
        self.headless = headless
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.log = log_fn

    def run(self, email: str, password: str) -> dict:
        if not self.otp_callback:
            raise RuntimeError("Anuma 注册需要邮箱验证码，但未提供 otp_callback")

        launch_options = {"headless": self.headless}
        proxy = _build_proxy_config(self.proxy)
        if proxy:
            launch_options["proxy"] = proxy

        with Camoufox(**launch_options) as browser:
            page = browser.new_page()
            self.log("打开 Anuma 注册页")
            page.goto(ANUMA_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)

            _dismiss_cookie_banner(page, self.log)

            email_selectors = [
                'input[autocomplete="email"]',
                'input[type="email"]',
                'input[name="email"]',
                'input[placeholder*="邮箱"]',
                'input[placeholder*="email" i]',
            ]
            email_input, email_selector = _wait_visible(page, email_selectors, timeout=30)
            if not email_input:
                _dump_debug(page, "anuma_email_missing")
                raise RuntimeError(f"未找到 Anuma 邮箱输入框: {page.url}")
            self.log(f"填写 Anuma 邮箱: {email_selector}")
            email_input.click()
            email_input.fill(email)

            if not _check_terms(page, self.log):
                _dump_debug(page, "anuma_terms_missing")
                raise RuntimeError("Anuma 注册未能勾选服务条款")

            submit_selectors = [
                'button[type="submit"]',
                'button:has-text("继续")',
                'button:has-text("Continue")',
            ]
            submit_button, submit_selector = _wait_visible(page, submit_selectors, timeout=20)
            if not submit_button:
                _dump_debug(page, "anuma_submit_missing")
                raise RuntimeError(f"未找到 Anuma 提交按钮: {page.url}")
            self.log(f"提交注册表单: {submit_selector}")
            submit_button.click()
            time.sleep(3)

            otp_selectors = [
                'input[autocomplete="one-time-code"]',
                'input[aria-label="Digit 1 of 6"]',
                'input[inputmode="numeric"]',
            ]
            otp_input, otp_selector = _wait_visible(page, otp_selectors, timeout=90)
            if not otp_input:
                _dump_debug(page, "anuma_otp_missing")
                raise RuntimeError(f"未进入 Anuma 验证码页面: {page.url}")
            self.log(f"等待并填写 Anuma OTP: {otp_selector}")

            code = str(self.otp_callback() or "").strip()
            if not code:
                raise RuntimeError("未获取到 Anuma 验证码")

            try:
                otp_input.click(force=True)
            except Exception:
                otp_input.click()
            try:
                otp_input.fill("")
            except Exception:
                pass
            page.keyboard.type(code, delay=80)
            time.sleep(2)

            # 某些环境会在输入完成后自动提交；若按钮仍可见则补点一次。
            verify_button, verify_selector = _find_visible(
                page,
                [
                    'button[type="submit"]',
                    'button:has-text("验证")',
                    'button:has-text("继续")',
                    'button:has-text("Confirm")',
                    'button:has-text("Continue")',
                ],
            )
            if verify_button:
                try:
                    verify_button.click()
                    self.log(f"补点验证码确认按钮: {verify_selector}")
                except Exception:
                    pass

            cookies, local_storage, session_storage = _wait_for_auth_state(page, timeout=120)
            privy_token = str(cookies.get("privy-token", "") or local_storage.get("privy:token", "") or "")
            if not privy_token:
                _dump_debug(page, "anuma_auth_missing")
                raise RuntimeError(f"Anuma 注册后未拿到 privy token: {page.url}")

            self.log(f"Anuma 注册成功: {email}")
            return {
                "email": email,
                "password": password or "",
                "url": page.url,
                "title": page.title(),
                "cookies": page.context.cookies(),
                "local_storage": local_storage,
                "session_storage": session_storage,
                "privy_session": str(cookies.get("privy-session", "") or ""),
                "privy_token": str(cookies.get("privy-token", "") or local_storage.get("privy:token", "") or ""),
                "privy_id_token": str(cookies.get("privy-id-token", "") or local_storage.get("privy:id_token", "") or ""),
                "privy_refresh_token": _decode_storage_value(local_storage.get("privy:refresh_token")),
                "privy_caid": _decode_storage_value(local_storage.get("privy:caid")),
            }
