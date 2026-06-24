"""用 resin IP 打开 Outlook 注册页，dump body + 所有按钮文本，找真正的同意按钮。"""
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
resolved = resolve_resin_proxy_config(cfg, task_platform="outlook", account="rdbg2", require_enabled=True)
proxy_url = str(resolved.get("proxy_url") or "")
import requests
ip = requests.get("https://api.ipify.org", proxies={"http": proxy_url, "https": proxy_url}, timeout=12).text.strip()
print(f"ip: {ip}")

proxy_cfg = build_playwright_proxy_settings(proxy_url)
pw = sync_playwright().start()
browser = pw.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--lang=zh-CN"], proxy=proxy_cfg)
ctx = browser.new_context(viewport={"width": 1366, "height": 800}, locale="zh-CN")
page = ctx.new_page()
ctx.set_default_timeout(30000)
try:
    page.goto("https://outlook.live.com/mail/0/?prompt=create_account", timeout=45000, wait_until="networkidle")
    time.sleep(8)
    print(f"url: {page.url}")
    body = page.inner_text("body", timeout=5000)[:800].replace("\n", " | ")
    print(f"body: {body}")
    # 截图
    from pathlib import Path
    Path("scripts/_outlook_resin_debug.png").resolve()
    page.screenshot(path="D:/Desktop/cat/any-auto-register/scripts/_outlook_resin_debug.png", full_page=True)
    print("screenshot saved")
    # 所有可见按钮的文本
    btns = page.evaluate("""
        () => {
          const out = [];
          for (const el of document.querySelectorAll('button, a, [role="button"], input[type="submit"], input')) {
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) continue;
            out.push({tag: el.tagName, type: el.getAttribute('type')||'', id: el.id||'', testid: el.getAttribute('data-testid')||'', aria: el.getAttribute('aria-label')||'', text: (el.innerText||el.value||'').slice(0,60)});
          }
          return out.slice(0,30);
        }
    """)
    print(f"buttons ({len(btns)}):")
    for b in btns: print("  ", b)
    # 所有 iframe
    print("frames:")
    for f in page.frames:
        if f.url and f.url != page.url: print(f"  {f.url[:100]}")
    time.sleep(3)
finally:
    ctx.close(); browser.close(); pw.stop()
