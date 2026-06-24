"""用 Playwright sync API 跑完整 vellum 注册，重点抓 radar-challenge 阶段的所有请求。
用 CarvajalSloman80+fzo8（parent 已验证能收码）。
"""
from __future__ import annotations
import json, sys, time, re, os
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

PROXY = "http://127.0.0.1:7897"
REDIRECT = "https://www.vellum.ai/accounts/workos/login/callback/"
PASSWORD = "VlMixcap1234!7"
ALIAS = "CarvajalSloman80+fzo8@outlook.com"
PARENT = "CarvajalSloman80@outlook.com"

def main():
    from core.base_mailbox import create_mailbox
    import sqlite3, json as _json
    with sqlite3.connect(str(ROOT/"account_manager.db")) as _c:
        _c.row_factory = sqlite3.Row
        r = _c.execute("SELECT email,purchase_token,metadata_json FROM mailbox_inventory WHERE email=?", (ALIAS,)).fetchone()
    md = _json.loads(r["metadata_json"] or "{}")
    extra = {
        "mail_provider":"outlook_token","outlook_email":PARENT,
        "outlook_password":str(md.get("password") or ""),
        "outlook_client_id":str(md.get("client_id") or ""),
        "outlook_refresh_token":r["purchase_token"],
        "outlook_registration_email":ALIAS,"outlook_alias_parent_email":PARENT,
    }
    mailbox = create_mailbox("outlook_token", extra=extra, proxy=PROXY)
    account = mailbox.get_email()
    before_ids = mailbox.get_current_ids(account)
    print(f"before_ids: {before_ids}")

    captured = []
    out_path = ROOT/"scripts"/"_radar_pw_capture.json"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": PROXY})
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900},
        )
        page = ctx.new_page()
        def on_request(req):
            if req.method == "POST" and "login.platform.vellum.ai" in req.url:
                try: pd = req.post_data
                except Exception: pd = None
                captured.append({
                    "url": req.url, "method": req.method,
                    "headers": {k:v for k,v in (req.headers or {}).items() if k.lower() in ("next-action","content-type")},
                    "post_data": pd[:3000] if pd else None,
                    "ts": time.time()
                })
                try:
                    json.dump(captured, open(out_path,"w",encoding="utf-8"), ensure_ascii=False, indent=1)
                except Exception: pass
                print(f"  [POST] {req.url[:80]} action={req.headers.get('next-action','')[:20]}")
        page.on("request", on_request)

        print("1. 打开 vellum signup")
        page.goto("https://www.vellum.ai/account/signup", wait_until="networkidle", timeout=30000)
        time.sleep(1)
        page.get_by_role("button", name="Continue with Email").click()
        time.sleep(3)
        print(f"   URL: {page.url}")

        print("2. 填 sign-up 表单")
        page.get_by_placeholder("您").first.fill("Aaron")  # 名
        page.locator('input[name="last_name"], input[placeholder*="姓"]').fill("Turner")
        page.locator('input[type="email"], input[placeholder*="邮"]').fill(ALIAS)
        time.sleep(0.5)
        page.get_by_role("button", name="继续").click()
        time.sleep(3)
        print(f"   URL: {page.url}")

        print("3. 填密码")
        page.locator('input[type="password"]').fill(PASSWORD)
        time.sleep(0.5)
        page.get_by_role("button", name="继续").click()
        time.sleep(3)
        print(f"   URL: {page.url}")

        print("4. 等邮箱验证码...")
        code = mailbox.wait_for_code(account, code_pattern=r"(?<!\d)(\d{6})(?!\d)", timeout=180, before_ids=before_ids)
        if not code:
            print("未收到验证码")
            browser.close()
            return
        print(f"   验证码: {code}")

        print("5. 填 6 位验证码")
        inputs = page.locator('input[maxlength="1"]').all()
        if len(inputs) >= 6:
            for i, ch in enumerate(code):
                inputs[i].fill(ch)
        else:
            page.locator('input[name="code"], input[placeholder*="码"]').fill(code)
        time.sleep(2)
        print(f"   URL: {page.url}")
        # 检查是否自动提交了
        if "email-verification" in page.url:
            try:
                page.get_by_role("button", name="继续").click(timeout=3000)
            except Exception:
                pass
        time.sleep(3)
        print(f"   提交后 URL: {page.url}")

        # 现在应该在 radar-challenge/send 或直接落地
        print(f"6. 当前页面: {page.url}")
        page.screenshot(path=str(ROOT/"scripts"/"_pw_after_otp.png"))

        # 如果在 radar-challenge，等 radar 验证码
        if "radar-challenge" in page.url:
            print("   在 radar-challenge！等 radar 验证码（120s）...")
            before_ids2 = mailbox.get_current_ids(account)
            code2 = mailbox.wait_for_code(account, code_pattern=r"(?<!\d)(\d{6})(?!\d)", timeout=120, before_ids=before_ids2)
            print(f"   radar 验证码: {code2}")
            if code2:
                print("7. 填 radar 验证码")
                inputs2 = page.locator('input[maxlength="1"]').all()
                if len(inputs2) >= 6:
                    for i, ch in enumerate(code2):
                        inputs2[i].fill(ch)
                time.sleep(3)
                print(f"   URL: {page.url}")
                if "radar" in page.url:
                    try:
                        page.get_by_role("button", name="继续").click(timeout=3000)
                    except Exception: pass
                time.sleep(5)
                print(f"   最终 URL: {page.url}")
        else:
            print("   不在 radar-challenge，可能直接落地")

        page.screenshot(path=str(ROOT/"scripts"/"_pw_final.png"))
        time.sleep(2)
        print(f"最终 URL: {page.url}")
        # 抓 cookies
        cookies = ctx.cookies()
        print(f"cookies: {len(cookies)}")
        vellum_ck = [c for c in cookies if "vellum.ai" in c.get("domain","")]
        print(f"vellum.ai cookies: {len(vellum_ck)}")
        for c in vellum_ck[:5]:
            print(f"  {c['name']}={c['value'][:25]}... domain={c['domain']}")

        # 尝试 ensure-registration
        if "vellum.ai" in page.url and ("assistant" in page.url or "account" in page.url):
            print("8. 尝试 ensure-registration...")
            csrf = ""
            for c in vellum_ck:
                if "csrftoken" in c["name"].lower(): csrf = c["value"]; break
            # 用 page.evaluate 调 API
            try:
                result = page.evaluate("""async (csrf) => {
                    const r = await fetch('/v1/organizations/', {headers: {'Accept':'application/json'}});
                    const orgs = await r.json();
                    return {status: r.status, orgs: orgs};
                }""", csrf)
                print(f"   organizations: {result['status']}")
                orgs = result.get("orgs", [])
                ol = orgs if isinstance(orgs, list) else (orgs.get("results") or orgs.get("items") or [])
                if ol:
                    org_id = ol[0].get("id") or ol[0].get("organization_id") or ""
                    print(f"   org_id: {org_id}")
                    import uuid
                    cid = str(uuid.uuid4()); rid = str(uuid.uuid4())
                    er = page.evaluate("""async ({csrf,org_id,cid,rid}) => {
                        const r = await fetch('/v1/assistants/self-hosted-local/ensure-registration/', {
                            method:'POST',
                            headers:{'Accept':'application/json','Content-Type':'application/json','X-CSRFToken':csrf,'Vellum-Organization-Id':org_id},
                            body: JSON.stringify({client_installation_id:cid, runtime_assistant_id:rid, client_platform:'web'})
                        });
                        const data = await r.json().catch(()=>({}));
                        return {status:r.status, data:data};
                    }""", {"csrf":csrf,"org_id":org_id,"cid":cid,"rid":rid})
                    print(f"   ensure-registration: {er['status']}")
                    print(f"   data: {json.dumps(er.get('data',{}))[:200]}")
                    api_key = er.get("data",{}).get("assistant_api_key") or (er.get("data",{}).get("provisioning") or {}).get("assistant_api_key") or ""
                    if api_key:
                        print(f"   ★★★★★ api_key: {api_key}")
            except Exception as e:
                print(f"   ensure 异常: {e}")

        browser.close()

    print(f"\n共抓到 {len(captured)} 个 POST:")
    for i, c in enumerate(captured):
        print(f"  [{i}] action={c['headers'].get('next-action','')[:20]} ct={c['headers'].get('content-type','')[:30]}")
        print(f"      url={c['url'][:100]}")
        if c.get("post_data"):
            print(f"      body[:150]={c['post_data'][:150]}")
    print(f"\n保存到 {out_path}")

if __name__ == "__main__":
    main()
