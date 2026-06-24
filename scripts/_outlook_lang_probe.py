"""探针：用 resin 英文 IP 打开注册页，dump 英文版的选择器文本。"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_store import config_store
from core.resin_proxy import resolve_resin_proxy_config
from core.proxy_utils import build_playwright_proxy_settings
from patchright.sync_api import sync_playwright

cfg = {
    "resin_enabled": "true",
    "resin_scheme": config_store.get("resin_scheme", ""),
    "resin_host": config_store.get("resin_host", ""),
    "resin_port": config_store.get("resin_port", ""),
    "resin_token": config_store.get("resin_token", ""),
    "resin_default_platform": config_store.get("resin_default_platform", "Default"),
    "resin_platform_map": config_store.get("resin_platform_map", ""),
}
resolved = resolve_resin_proxy_config(cfg, task_platform="outlook", account="langprobe1", require_enabled=True)
proxy_url = str(resolved.get("proxy_url") or "")
print(f"proxy: {proxy_url[:40]}...")

import requests
ip = requests.get("https://api.ipify.org", proxies={"http": proxy_url, "https": proxy_url}, timeout=12).text.strip()
print(f"ip: {ip}")

proxy_cfg = build_playwright_proxy_settings(proxy_url)
pw = sync_playwright().start()
browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--lang=en-US"], proxy=proxy_cfg)
# 故意用 en-US locale 看 Outlook 给英文还是跟随 IP
ctx = browser.new_context(viewport={"width": 1366, "height": 800}, locale="en-US")
page = ctx.new_page()
ctx.set_default_timeout(20000)
try:
    page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=30000, wait_until="domcontentloaded")
    time.sleep(3)
    # dump body 文本 + 关键元素的 aria-label/text
    body = page.inner_text("body", timeout=3000)[:500].replace("\n", " | ")
    print(f"body: {body}")
    info = page.evaluate("""
        () => {
          const out = [];
          for (const el of document.querySelectorAll('input, button, [data-testid]')) {
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            out.push({tag: el.tagName, type: el.getAttribute('type')||'', name: el.getAttribute('name')||'', id: el.id||'', aria: el.getAttribute('aria-label')||'', testid: el.getAttribute('data-testid')||'', text: (el.innerText||el.value||'').slice(0,50)});
          }
          return out.slice(0,20);
        }
    """)
    for el in info: print("  ", el)
    time.sleep(2)
    # 试找"同意"按钮的英文文案
    for txt in ["Agree and continue", "Agree", "Continue", "I agree", "Accept", "同意并继续"]:
        c = page.get_by_text(txt).count()
        if c: print(f"  text '{txt}': count={c}")
finally:
    ctx.close(); browser.close(); pw.stop()
