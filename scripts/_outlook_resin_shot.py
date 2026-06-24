"""用 resin IP 打开 Outlook 注册页，截图 + dump，判断页面是空白/拦截/正常。"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_store import config_store
from core.resin_proxy import resolve_resin_proxy_config
from core.proxy_utils import build_playwright_proxy_settings
from patchright.sync_api import sync_playwright

OUT = Path("D:/Desktop/cat/any-auto-register/scripts/_outlook_resin_page.png")

cfg = {
    "resin_enabled": "true",
    "resin_scheme": config_store.get("resin_scheme", ""),
    "resin_host": config_store.get("resin_host", ""),
    "resin_port": config_store.get("resin_port", ""),
    "resin_token": config_store.get("resin_token", ""),
    "resin_default_platform": config_store.get("resin_default_platform", "Default"),
    "resin_platform_map": config_store.get("resin_platform_map", ""),
}
resolved = resolve_resin_proxy_config(cfg, task_platform="outlook", account="shot1", require_enabled=True)
proxy_url = str(resolved.get("proxy_url") or "")
import requests
try:
    ip = requests.get("https://api.ipify.org", proxies={"http": proxy_url, "https": proxy_url}, timeout=12).text.strip()
except Exception as e:
    ip = f"FAIL {e}"
print(f"proxy: {proxy_url[:50]}")
print(f"ip: {ip}")

proxy_cfg = build_playwright_proxy_settings(proxy_url)
pw = sync_playwright().start()
browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--lang=zh-CN"], proxy=proxy_cfg)
ctx = browser.new_context(viewport={"width": 1366, "height": 800}, locale="zh-CN")
page = ctx.new_page()
ctx.set_default_timeout(45000)
try:
    page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=45000, wait_until="networkidle")
    time.sleep(8)
    print(f"url: {page.url}")
    body = page.inner_text("body", timeout=5000)[:800].replace("\n", " | ")
    print(f"body: {body}")
    page.screenshot(path=str(OUT), full_page=True)
    print(f"screenshot: {OUT}")
    html_len = len(page.content())
    print(f"html length: {html_len}")
    # frames
    for f in page.frames:
        if f.url and f.url != page.url:
            print(f"  frame: {f.url[:100]}")
finally:
    ctx.close(); browser.close(); pw.stop()
