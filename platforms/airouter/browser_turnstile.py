"""AI-ROUTER Turnstile CDP 采集器。

只用于通过 Cloudflare Turnstile 挑战；注册、发码、创建 API Key 均由协议接口完成。
"""
from __future__ import annotations

import time
from typing import Any

from platforms.enter.browser_register import EnterBrowserRegistrar, PLAYWRIGHT_AVAILABLE, sync_playwright

REGISTER_URL = "https://ai-router.dev/register"


class AiRouterTurnstileHarvester:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: int = 180,
        chrome_path: str = "",
        cdp_url: str = "",
        log_fn=print,
    ) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.chrome_path = chrome_path
        self.cdp_url = cdp_url
        self.log = log_fn or (lambda _msg: None)

    def _l(self, msg: str) -> None:
        self.log(f"[AI-ROUTER:turnstile] {msg}")

    def harvest(self, *, email: str = "", password: str = "") -> str:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("AI-ROUTER Turnstile 采集需要 Playwright")

        # 复用 Enter 已验证的真实 Chrome/CDP 启动与清理逻辑。
        registrar = EnterBrowserRegistrar(
            headless=False,
            proxy=self.proxy,
            timeout=self.timeout,
            chrome_path=self.chrome_path,
            cdp_url=self.cdp_url,
            log_fn=self.log,
        )
        launch_meta = registrar._prepare_chrome()
        browser = None
        page = None
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                page.goto(REGISTER_URL, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                self._prefill(page, email=email, password=password)
                token = self._click_until_token(page)
                if not token:
                    raise RuntimeError("AI-ROUTER Turnstile 未获得 token")
                return token
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

    def _prefill(self, page: Any, *, email: str, password: str) -> None:
        try:
            if email:
                loc = page.locator("input[type='email'], input#email, input[name='email']").first
                if loc.count() > 0 and loc.is_visible(timeout=10_000):
                    loc.fill(email)
            if password:
                loc = page.locator("input[type='password'], input#password, input[name='password']").first
                if loc.count() > 0 and loc.is_visible(timeout=5_000):
                    loc.fill(password)
        except Exception as exc:
            self._l(f"预填表单失败，继续采集 Turnstile: {exc}")

    def _read_token(self, page: Any) -> str:
        try:
            return str(page.evaluate("""
                () => {
                  const selectors = [
                    "input[name='cf-turnstile-response']",
                    "textarea[name='cf-turnstile-response']",
                    "input[name='turnstile']",
                    "textarea[name='turnstile']"
                  ];
                  for (const selector of selectors) {
                    const el = document.querySelector(selector);
                    if (el && el.value) return el.value;
                  }
                  return '';
                }
            """) or "").strip()
        except Exception:
            return ""

    def _click_until_token(self, page: Any) -> str:
        deadline = time.time() + max(45, self.timeout)
        attempts = 0
        while time.time() < deadline:
            token = self._read_token(page)
            if token:
                self._l(f"Turnstile token obtained length={len(token)}")
                return token

            attempts += 1
            clicked = self._click_turnstile_widget(page)
            if clicked:
                page.wait_for_timeout(3500)
            else:
                page.wait_for_timeout(1000)
            if attempts % 5 == 0:
                self._l("等待 Turnstile token...")
        return self._read_token(page)

    def _click_turnstile_widget(self, page: Any) -> bool:
        # 优先点击 Turnstile iframe 中心；失败再点击容器。
        try:
            iframe = page.locator("iframe[src*='challenges.cloudflare.com']").first
            if iframe.count() > 0:
                box = iframe.bounding_box(timeout=1000)
                if box:
                    x = box["x"] + min(28, max(18, box["width"] * 0.12))
                    y = box["y"] + min(32, max(20, box["height"] * 0.50))
                    page.mouse.move(x - 16, y - 8, steps=10)
                    page.wait_for_timeout(100)
                    page.mouse.click(x, y, delay=120)
                    return True
        except Exception:
            pass

        for selector in [".turnstile-container", ".cf-turnstile", "[class*='turnstile']"]:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible(timeout=500):
                    box = loc.bounding_box(timeout=1000)
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, delay=120)
                        return True
            except Exception:
                continue
        return False
