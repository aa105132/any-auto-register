"""用 Playwright 实地跑 radar-challenge/send，确认 WorkOS 是否真的发短信。
先纯 HTTP 跑到 send URL，然后 Playwright 打开 send 页面，填手机号，点按钮，观察。
"""
from __future__ import annotations
import base64, json, random, re, string, sys, time, hashlib
from pathlib import Path
from urllib.parse import quote
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

PROXY = "http://127.0.0.1:7897"
REDIRECT = "https://www.vellum.ai/accounts/workos/login/callback/"
PASSWORD = "VlMixcap1234!7"
ACTION_BOT_DETECT = "a67eb6646e43eddcbd0d038cbee664aac59f5a53"
ACTION_RADAR_SEND = "a26cb1c7b9ef800ba3a8e9fc9b3153716b5465d4"
HAOZHU_SID = "108717"

def log(m): print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)

def post_action(s, url, aid_v, fields):
    sig_json = json.dumps({"puppeteerDetected": False, "puppeteerDocumentNotAvailable": False, "submittedAtMs": int(time.time()*1000)})
    sig = base64.b64encode(sig_json.encode()).decode()
    b = "----WebKitFormBoundary" + "".join(random.choices(string.ascii_letters+string.digits, k=16))
    af = {"1_signals": sig}
    for k,v in fields.items(): af[f"1_{k}"] = v
    af["0"] = '["$K1"]'
    p = "".join(f"--{b}\r\nContent-Disposition: form-data; name=\"{n}\"\r\n\r\n{v}\r\n" for n,v in af.items()) + f"--{b}--\r\n"
    return s.post(url, data=p.encode(), headers={"Content-Type":f"multipart/form-data; boundary={b}","Next-Action":aid_v,"Accept":"text/x-component","Referer":url,"Origin":"https://login.platform.vellum.ai"}, timeout=20, allow_redirects=False)

def post_action_text(s, url, aid_v, args):
    body = json.dumps(args)
    return s.post(url, data=body.encode(), headers={"Content-Type":"text/plain;charset=UTF-8","Next-Action":aid_v,"Accept":"text/x-component","Referer":url,"Origin":"https://login.platform.vellum.ai"}, timeout=20, allow_redirects=False)

def aid(h):
    m = re.search(r'([a-f0-9]{40}).{0,20}bound', h); return m.group(1) if m else ""
def hidden(h):
    f = {}
    for m in re.finditer(r'name="([^"]+)"[^>]*value="([^"]*)"', h):
        if m.group(1) in ("authorization_session_id","state","redirect_uri","intent","pending_authentication_token"): f[m.group(1)] = m.group(2)
    return f
def all_hidden(h):
    f = {}
    for m in re.finditer(r'name="([^"]+)"[^>]*value="([^"]*)"', h):
        f[m.group(1)] = m.group(2)
    return f
def params(t):
    m = re.search(r'__PAGE__\?(\{[^}]+\})', t)
    if m:
        try: return json.loads(m.group(1).replace('\\"','"'))
        except Exception: pass
    return {}

def haozhu_login():
    import requests, sqlite3, json
    with sqlite3.connect(str(ROOT/"account_manager.db")) as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT config_json, auth_json FROM provider_settings WHERE provider_type='phone' AND provider_key='haozhu'").fetchone()
    cfg = json.loads(row["config_json"] or "{}")
    auth = json.loads(row["auth_json"] or "{}")
    api = cfg.get("haozhu_api_base_url", "https://api.haozhuma.com")
    r = requests.get(f"{api}/sms/", params={"api":"login","user":auth.get("haozhu_username",""),"pass":auth.get("haozhu_password","")}, proxies={"http":PROXY,"https":PROXY}, timeout=30)
    return r.json().get("token","")

def get_haozhu_phone(token):
    import requests
    r = requests.get("https://api.haozhuma.com/sms/", params={"api":"getPhone","token":token,"sid":HAOZHU_SID}, proxies={"http":PROXY,"https":PROXY}, timeout=30)
    d = r.json()
    return d.get("phone",""), token

def main():
    from curl_cffi import requests as creq
    from core.base_mailbox import create_mailbox
    from playwright.sync_api import sync_playwright
    import sqlite3, json as _json

    log("豪猪登录...")
    haozhu_token = haozhu_login()

    # 选候选
    with sqlite3.connect(str(ROOT/"account_manager.db")) as _c:
        _c.row_factory = sqlite3.Row
        rows = _c.execute("SELECT email,purchase_token,metadata_json FROM mailbox_inventory WHERE provider_key='outlook_token' AND status='unused' AND email LIKE '%+%' ORDER BY id ASC").fetchall()
    tested = {'sainzdewall392','nowlandbillet33','caviggiamendias04','bellafiorerevoir78','romerhaushalter512',
              'detwilerkenebrew9176','carvajalsloman80','ferranate61','darrahelga8893','gauanimoney357',
              'densfordlagoa08','gabrenassharlow360','tiftemenaha9695','strelowlogalbo11','jerichorhein87',
              'mattiellomaggart494','tatianateitenberg500','perlsteinlepage0293','lachariteroderiquez91',
              'hoffelmeyervalladores9533','contosjustiniano7200','buesgensvalakas1972','olaldebuchholz504',
              'biniongrossack023','guaglianoglasson3254','traffanstedtbordenet706','showdenlouise4877',
              'bodenheimernealis014','miccodrevs9115','strogenpollnow5293','macombbullion704','klarbergbonda359',
              'marlyflower052','pottcossette047','marzinskedonnette353'}
    cand = None
    for row in rows:
        mdd = _json.loads(row["metadata_json"] or "{}")
        if not mdd.get("alias_parent_email"): continue
        if any(t in row["email"].lower() for t in tested): continue
        cand = (dict(row), mdd); break
    if not cand:
        log("无候选"); return
    r, md = cand
    ALIAS = r["email"]; PARENT = md.get("alias_parent_email")
    log(f"用 {ALIAS}")

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

    # 纯 HTTP 跑到 send URL
    s = creq.Session(impersonate="chrome131"); s.proxies = {"http":PROXY,"https":PROXY}
    s.get("https://www.vellum.ai/account/signup", timeout=15, allow_redirects=True)
    s.get("https://www.vellum.ai/_allauth/browser/v1/auth/session", timeout=15, headers={"Accept":"application/json"})
    csrf = ""
    for c in s.cookies.jar:
        if "csrftoken" in c.name.lower(): csrf = c.value; break
    r3 = s.post("https://www.vellum.ai/_allauth/browser/v1/auth/provider/redirect", data={"provider":"workos","callback_url":"https://www.vellum.ai/account/provider/callback?authIntent=signup","process":"login","intent":"signup","csrfmiddlewaretoken":csrf}, headers={"X-CSRFToken":csrf,"Referer":"https://www.vellum.ai/account/signup"}, timeout=15, allow_redirects=False)
    r4 = s.get(r3.headers.get("Location",""), timeout=15, allow_redirects=True)
    fu = str(r4.url); a = aid(r4.text); h = hidden(r4.text); asid = h.get("authorization_session_id",""); st = h.get("state","")
    post_action_text(s, fu, ACTION_BOT_DETECT, [hashlib.sha256(f"{ALIAS}{time.time()}{random.random()}".encode()).hexdigest()])

    r5 = post_action(s, fu, a, {"first_name":"Aaron","last_name":"Turner","email":ALIAS,"intent":"sign-up","redirect_uri":REDIRECT,"authorization_session_id":asid,"state":st})
    log(f"page1: {r5.status_code}")
    if r5.status_code != 303: log(r5.text[:200]); return
    p = params(r5.text); pa = p.get("authorization_session_id",asid); ps = p.get("state",st)

    purl = f"https://login.platform.vellum.ai/sign-up/password?state={ps}&redirect_uri=https%3A%2F%2Fwww.vellum.ai%2Faccounts%2Fworkos%2Flogin%2Fcallback%2F&authorization_session_id={pa}"
    r6 = s.get(purl, timeout=15, allow_redirects=True)
    pa2 = aid(r6.text) or a; ph = hidden(r6.text); pa = ph.get("authorization_session_id",pa); ps = ph.get("state",ps)
    r7 = post_action(s, purl, pa2, {"first_name":"Aaron","last_name":"Turner","email":ALIAS,"password":PASSWORD,"intent":"sign-up","redirect_uri":REDIRECT,"authorization_session_id":pa,"state":ps})
    log(f"password: {r7.status_code}")
    if r7.status_code != 303: log(r7.text[:200]); return
    p2 = params(r7.text); oa = p2.get("authorization_session_id",pa); os = p2.get("state",ps)

    ourl = f"https://login.platform.vellum.ai/email-verification?state={os}&redirect_uri=https%3A%2F%2Fwww.vellum.ai%2Faccounts%2Fworkos%2Flogin%2Fcallback%2F&authorization_session_id={oa}"
    r8 = s.get(ourl, timeout=15, allow_redirects=True)
    oh = all_hidden(r8.text)
    oa = oh.get("authorization_session_id",oa); os = oh.get("state",os); oact = aid(r8.text) or pa2; pt = oh.get("pending_authentication_token","")

    log("等邮箱验证码...")
    code = mailbox.wait_for_code(account, code_pattern=r"(?<!\d)(\d{6})(?!\d)", timeout=180, before_ids=before_ids)
    if not code: log("未收到验证码"); return
    log(f"验证码 {code}")
    ofields = {"code":code,"first_name":"Aaron","last_name":"Turner","email":ALIAS,"intent":"sign-up","redirect_uri":REDIRECT,"authorization_session_id":oa,"state":os}
    if pt: ofields["pending_authentication_token"] = pt
    r9 = post_action(s, ourl, oact, ofields)
    log(f"OTP: {r9.status_code}")
    if r9.status_code != 303: log(r9.text[:200]); return

    body_clean = r9.text.replace('\\"', '"').replace('\\\\', '\\')
    rsc_match = re.search(r'"user_id":"([^"]+)".*?"state":"([^"]+)".*?"redirect_uri":"([^"]+)".*?"authorization_session_id":"([^"]+)"', body_clean)
    rsc_user_id = rsc_match.group(1) if rsc_match else ""
    rsc_state = rsc_match.group(2) if rsc_match else os
    rsc_redirect = rsc_match.group(3).replace('\\/', '/') if rsc_match else REDIRECT
    rsc_auth = rsc_match.group(4) if rsc_match else oa

    send_url = f"https://login.platform.vellum.ai/radar-challenge/send?user_id={rsc_user_id}&state={rsc_state}&redirect_uri={quote(rsc_redirect, safe='')}&authorization_session_id={rsc_auth}"
    log(f"send URL: {send_url[:100]}")

    # 取豪猪号
    phone, haozhu_token = get_haozhu_phone(haozhu_token)
    log(f"豪猪号: {phone}")
    raw = re.sub(r"\D", "", phone)
    local_number = raw
    phone_number = "+86" + raw

    # 用 Playwright 打开 send 页面，填手机号，点按钮
    log("用 Playwright 打开 send 页面...")
    # 导出 cookies 给 Playwright
    cookies = []
    for c in s.cookies.jar:
        cookies.append({
            "name": c.name, "value": c.value,
            "domain": c.domain or ".platform.vellum.ai",
            "path": c.path or "/",
            "secure": bool(c.secure),
            "httpOnly": bool(getattr(c, '_rest', {}).get('HttpOnly', False)),
        })

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": PROXY})
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
            viewport={"width":1280,"height":900},
        )
        ctx.add_cookies(cookies)
        page = ctx.new_page()

        # 监听网络请求
        requests_log = []
        def on_req(req):
            if req.method == "POST" and "login.platform.vellum.ai" in req.url:
                requests_log.append({"url": req.url[:100], "action": req.headers.get("next-action","")[:25]})
                log(f"  [POST] action={req.headers.get('next-action','')[:20]} url={req.url[:80]}")
        page.on("request", on_req)

        page.goto(send_url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        log(f"send 页面 URL: {page.url}")
        log(f"send 页面 title: {page.title()}")

        # 截图
        page.screenshot(path=str(ROOT/"scripts"/"_pw_send_page.png"))

        # 填手机号
        log("填手机号...")
        try:
            # country_code 输入框
            cc_input = page.locator("input[name='country_code']").first
            cc_input.click()
            cc_input.fill("")
            cc_input.type("+86", delay=50)
            time.sleep(0.5)
            # local_number 输入框
            ln_input = page.locator("input[name='local_number']").first
            ln_input.click()
            ln_input.type(local_number, delay=50)
            time.sleep(1)
            log(f"cc={cc_input.input_value()} local={ln_input.input_value()}")
        except Exception as e:
            log(f"填手机号失败: {e}")
            # 试 tel input
            try:
                page.locator("input[type='tel']").first.fill(local_number)
            except Exception as e2:
                log(f"tel 也失败: {e2}")

        page.screenshot(path=str(ROOT/"scripts"/"_pw_send_filled.png"))

        # 点 Send verification code 按钮
        log("点 Send verification code...")
        try:
            page.get_by_role("button", name=re.compile("Send verification code|Send|Continue", re.I)).first.click(timeout=5000)
        except Exception as e:
            log(f"点按钮失败: {e}")
            try:
                page.locator("button[type=submit]").first.click(timeout=5000)
            except Exception as e2:
                log(f"submit 也失败: {e2}")

        time.sleep(5)
        log(f"点按钮后 URL: {page.url}")
        page.screenshot(path=str(ROOT/"scripts"/"_pw_send_after.png"))

        # 等豪猪短信码
        log("等豪猪短信码（300s）...")
        import requests as _req
        api = "https://api.haozhuma.com"
        deadline = time.time() + 300
        got_code = ""
        while time.time() < deadline:
            r = _req.get(f"{api}/sms/", params={"api":"getMessage","token":haozhu_token,"sid":HAOZHU_SID,"phone":phone}, proxies={"http":PROXY,"https":PROXY}, timeout=30)
            d = r.json()
            if str(d.get("code","")) in ("0","200"):
                yzm = str(d.get("yzm") or "").strip()
                sms = str(d.get("sms") or "")
                log(f"★ 收到! yzm={yzm} sms={sms[:100]}")
                got_code = yzm or (re.search(r'(?<!\d)(\d{6})(?!\d)', sms) or [None,""])[1] if sms else ""
                if got_code: break
            time.sleep(5)

        if got_code:
            log(f"★ 短信码: {got_code}")
            # 填验证码
            try:
                inputs = page.locator('input[maxlength="1"]').all()
                if len(inputs) >= 6:
                    for i, ch in enumerate(got_code):
                        inputs[i].fill(ch)
                time.sleep(3)
                log(f"填码后 URL: {page.url}")
                page.screenshot(path=str(ROOT/"scripts"/"_pw_verify_filled.png"))
            except Exception as e:
                log(f"填验证码失败: {e}")
        else:
            log("未收到短信码")
            # 看页面是否有错误
            try:
                log(f"页面内容: {page.content()[:500]}")
            except: pass

        time.sleep(3)
        browser.close()

    # 释放豪猪号
    import requests as _req
    _req.get("https://api.haozhuma.com/sms/", params={"api":"cancelAllRecv","token":haozhu_token,"phone":phone,"sid":HAOZHU_SID}, proxies={"http":PROXY,"https":PROXY}, timeout=15)
    log("已释放豪猪号")

if __name__ == "__main__":
    main()
