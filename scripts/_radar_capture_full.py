"""用 Camoufox 实地跑完整注册，重点抓 radar-challenge/send → verify 阶段的所有 POST。
目的：找出真正触发"发码"的 action 和参数。
"""
from __future__ import annotations
import json, os, sys, time, base64, re, random, string, hashlib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from camoufox.sync_api import Camoufox
from curl_cffi import requests as creq

PROXY = "http://127.0.0.1:7897"
REDIRECT = "https://www.vellum.ai/accounts/workos/login/callback/"
PASSWORD = "VlMixcap1234!7"

def main():
    # 选一个未测的 outlook 别名
    import sqlite3, json as _json
    with sqlite3.connect(str(ROOT/"account_manager.db")) as _c:
        _c.row_factory = sqlite3.Row
        rows = _c.execute("SELECT id,email,purchase_token,metadata_json FROM mailbox_inventory WHERE provider_key='outlook_token' AND status='unused' ORDER BY id ASC").fetchall()
    tested = {'sainzdewall392','nowlandbillet33','caviggiamendias04','bellafiorerevoir78','romerhaushalter512',
              'detwilerkenebrew9176','carvajalsloman80','ferranate61','darrahelga8893','gauanimoney357',
              'densfordlagoa08','gabrenassharlow360','tiftemenaha9695','strelowlogalbo11','jerichorhein87',
              'mattiellomaggart494','tatianateitenberg500','perlsteinlepage0293','lachariteroderiquez91',
              'hoffelmeyervalladores9533','contosjustiniano7200'}
    cand = None
    for r in rows:
        md = _json.loads(r["metadata_json"] or "{}")
        if not (r["purchase_token"] and md.get("client_id") and md.get("alias_parent_email")): continue
        if any(t in r["email"].lower() for t in tested): continue
        used = md.get("used_platforms", []) or []
        if "vellum" in used: continue
        cand = (dict(r), md); break
    if not cand:
        print("无候选"); return
    r, md = cand
    alias = r["email"]
    pe = md.get("alias_parent_email")
    print(f"用 {alias} (parent={pe})")

    from core.base_mailbox import create_mailbox
    extra = {
        "mail_provider":"outlook_token","outlook_email":pe,
        "outlook_password":str(md.get("password") or ""),
        "outlook_client_id":str(md.get("client_id") or ""),
        "outlook_refresh_token":r["purchase_token"],
        "outlook_registration_email":alias,"outlook_alias_parent_email":pe,
    }
    mailbox = create_mailbox("outlook_token", extra=extra, proxy=PROXY)
    account = mailbox.get_email()
    before_ids = mailbox.get_current_ids(account)

    captured = []
    out_path = ROOT/"scripts"/"_radar_full_capture.json"

    with Camoufox(headless=False, geoip=True, proxy={"server": PROXY}, i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        def on_request(req):
            if req.method == "POST" and "login.platform.vellum.ai" in req.url:
                try:
                    pd = req.post_data
                except Exception:
                    pd = None
                captured.append({
                    "url": req.url, "method": req.method,
                    "headers": {k:v for k,v in (req.headers or {}).items() if k.lower() in ("next-action","content-type","referer","origin")},
                    "post_data": pd[:2000] if pd else None,
                    "ts": time.time()
                })
                try:
                    json.dump(captured, open(out_path,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
                except Exception: pass
        page.on("request", on_request)

        print("1. 打开 signup")
        page.goto("https://www.vellum.ai/account/signup", wait_until="networkidle", timeout=30000)
        time.sleep(1)
        # 点 sign up with workos
        try:
            page.get_by_role("link", name=re.compile("Sign up", re.I)).first.click(timeout=5000)
        except Exception:
            try:
                page.locator("a[href*='workos'], button:has-text('Sign up')").first.click(timeout=5000)
            except Exception as e:
                print(f"点 sign up 失败: {e}")
        time.sleep(3)
        print(f"当前 URL: {page.url}")

        # sign-up 表单
        print("2. 填 sign-up 表单")
        try:
            page.fill('input[name="first_name"], input[placeholder*="First" i]', "Aaron")
            page.fill('input[name="last_name"], input[placeholder*="Last" i]', "Turner")
            page.fill('input[name="email"], input[type="email"]', alias)
            time.sleep(0.5)
            page.get_by_role("button", name=re.compile("Continue|Next|Sign up", re.I)).first.click(timeout=5000)
        except Exception as e:
            print(f"填表单失败: {e}")
        time.sleep(3)
        print(f"当前 URL: {page.url}")

        # password
        print("3. 填密码")
        try:
            page.fill('input[type="password"]', PASSWORD)
            time.sleep(0.5)
            page.get_by_role("button", name=re.compile("Continue|Sign up|Next", re.I)).first.click(timeout=5000)
        except Exception as e:
            print(f"填密码失败: {e}")
        time.sleep(3)
        print(f"当前 URL: {page.url}")

        # email-verification: 等码
        print("4. 等邮箱验证码...")
        code = mailbox.wait_for_code(account, code_pattern=r"(?<!\d)(\d{6})(?!\d)", timeout=180, before_ids=before_ids)
        if not code:
            print("未收到验证码"); return
        print(f"验证码 {code}")
        # 填 6 位
        try:
            # 6 个单字符输入框
            inputs = page.locator('input[maxlength="1"]').all()
            if len(inputs) >= 6:
                for i, ch in enumerate(code):
                    inputs[i].fill(ch)
            else:
                # 一个输入框
                page.fill('input[name="code"], input[placeholder*="code" i]', code)
            time.sleep(0.5)
            # 自动提交或点按钮
            try:
                page.get_by_role("button", name=re.compile("Continue|Verify|Submit", re.I)).first.click(timeout=3000)
            except Exception:
                pass  # 可能自动提交
        except Exception as e:
            print(f"填验证码失败: {e}")
        time.sleep(3)
        print(f"当前 URL: {page.url}")

        # 到 radar-challenge/send 了
        print("5. 现在在 radar-challenge/send，等自动发码并观察 POST...")
        # 等新的验证码（radar 的）
        before_ids2 = mailbox.get_current_ids(account)
        print("等 radar 验证码（最多 120s，观察浏览器是否自动发码）...")
        code2 = mailbox.wait_for_code(account, code_pattern=r"(?<!\d)(\d{6})(?!\d)", timeout=120, before_ids=before_ids2)
        print(f"radar 验证码: {code2}")
        time.sleep(2)
        print(f"当前 URL: {page.url}")
        # 截图
        page.screenshot(path=str(ROOT/"scripts"/"_radar_browser_state.png"))
        # 抓 HTML
        try:
            html = page.content()
            (ROOT/"scripts"/"_radar_browser_live.html").write_text(html, encoding="utf-8")
        except Exception: pass

        if code2:
            print("6. 填 radar 验证码")
            try:
                inputs = page.locator('input[maxlength="1"]').all()
                if len(inputs) >= 6:
                    for i, ch in enumerate(code2):
                        inputs[i].fill(ch)
                time.sleep(1)
                try:
                    page.get_by_role("button", name=re.compile("Continue|Verify|Submit", re.I)).first.click(timeout=3000)
                except Exception:
                    pass
            except Exception as e:
                print(f"填 radar 码失败: {e}")
            time.sleep(5)
            print(f"最终 URL: {page.url}")
            page.screenshot(path=str(ROOT/"scripts"/"_radar_browser_final.png"))

    print(f"\n共抓到 {len(captured)} 个 POST:")
    for i, c in enumerate(captured):
        print(f"  [{i}] {c['url'][:90]}")
        print(f"      action={c['headers'].get('next-action','')[:20]} ct={c['headers'].get('content-type','')[:30]}")
        if c.get('post_data'):
            print(f"      body[:120]={c['post_data'][:120]}")
    print(f"\n保存到 {out_path}")

if __name__ == "__main__":
    main()
