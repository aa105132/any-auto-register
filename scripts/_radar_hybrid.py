"""混合模式：纯 HTTP 跑到 OTP 303 拿到 radar-challenge/send URL，
然后用 Playwright 浏览器打开 send URL，观察浏览器如何触发发码。
"""
from __future__ import annotations
import base64, json, random, re, string, sys, time, hashlib
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))

PROXY = "http://127.0.0.1:7897"
REDIRECT = "https://www.vellum.ai/accounts/workos/login/callback/"
PASSWORD = "VlMixcap1234!7"
ACTION_BOT_DETECT = "a67eb6646e43eddcbd0d038cbee664aac59f5a53"

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

def main():
    from curl_cffi import requests as creq
    from core.base_mailbox import create_mailbox
    import sqlite3, json as _json

    ALIAS = "FerraNate61+6ymy@outlook.com"
    PARENT = "FerraNate61@outlook.com"
    with sqlite3.connect(str(ROOT/"account_manager.db")) as _c:
        _c.row_factory = sqlite3.Row
        r = _c.execute("SELECT email,purchase_token,metadata_json FROM mailbox_inventory WHERE email=?", (ALIAS,)).fetchone()
    md = _json.loads(r["metadata_json"] or "{}")
    log(f"邮箱: {ALIAS}")
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

    s = creq.Session(impersonate="chrome131"); s.proxies = {"http":PROXY,"https":PROXY}
    s.get("https://www.vellum.ai/account/signup", timeout=15, allow_redirects=True)
    s.get("https://www.vellum.ai/_allauth/browser/v1/auth/session", timeout=15, headers={"Accept":"application/json"})
    csrf = ""
    for c in s.cookies.jar:
        if "csrftoken" in c.name.lower(): csrf = c.value; break
    r3 = s.post("https://www.vellum.ai/_allauth/browser/v1/auth/provider/redirect", data={"provider":"workos","callback_url":"https://www.vellum.ai/account/provider/callback?authIntent=signup","process":"login","intent":"signup","csrfmiddlewaretoken":csrf}, headers={"X-CSRFToken":csrf,"Referer":"https://www.vellum.ai/account/signup"}, timeout=15, allow_redirects=False)
    wu = r3.headers.get("Location","")
    r4 = s.get(wu, timeout=15, allow_redirects=True)
    fu = str(r4.url); a = aid(r4.text); h = hidden(r4.text); asid = h.get("authorization_session_id",""); st = h.get("state","")
    log(f"sign-up 页: action={a[:16]}")

    # bot detection
    bot_hash = hashlib.sha256(f"{ALIAS}{int(time.time())}{random.random()}".encode()).hexdigest()
    r_bot = post_action_text(s, fu, ACTION_BOT_DETECT, [bot_hash])
    log(f"bot detection: {r_bot.status_code}")

    # page1
    r5 = post_action(s, fu, a, {"first_name":"Aaron","last_name":"Turner","email":ALIAS,"intent":"sign-up","redirect_uri":REDIRECT,"authorization_session_id":asid,"state":st})
    log(f"page1: {r5.status_code}")
    if r5.status_code != 303:
        log(f"fail: {r5.text[:200]}"); return
    p = params(r5.text); pa = p.get("authorization_session_id",asid); ps = p.get("state",st)

    # password
    purl = f"https://login.platform.vellum.ai/sign-up/password?state={ps}&redirect_uri=https%3A%2F%2Fwww.vellum.ai%2Faccounts%2Fworkos%2Flogin%2Fcallback%2F&authorization_session_id={pa}"
    r6 = s.get(purl, timeout=15, allow_redirects=True)
    pa2 = aid(r6.text) or a; ph = hidden(r6.text); pa = ph.get("authorization_session_id",pa); ps = ph.get("state",ps)
    r7 = post_action(s, purl, pa2, {"first_name":"Aaron","last_name":"Turner","email":ALIAS,"password":PASSWORD,"intent":"sign-up","redirect_uri":REDIRECT,"authorization_session_id":pa,"state":ps})
    log(f"password: {r7.status_code}")
    if r7.status_code != 303:
        log(f"password fail: {r7.text[:200]}"); return
    log("★ password OK")
    p2 = params(r7.text); oa = p2.get("authorization_session_id",pa); os = p2.get("state",ps)

    # email-verification
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
    log(f"OTP body[:600]: {r9.text[:600]}")

    if r9.status_code != 303:
        log(f"OTP fail: {r9.text[:200]}"); return

    # 解析 RSC 找 radar-challenge/send URL（RSC body 里引号是 \" 转义的）
    rsc_user_id = ""; rsc_state = ""; rsc_redirect = REDIRECT; rsc_auth = ""
    # 先 unescape \" -> " 再 regex
    body_clean = r9.text.replace('\\"', '"').replace('\\\\', '\\')
    rsc_match = re.search(r'"user_id":"([^"]+)".*?"state":"([^"]+)".*?"redirect_uri":"([^"]+)".*?"authorization_session_id":"([^"]+)"', body_clean)
    if rsc_match:
        rsc_user_id = rsc_match.group(1)
        rsc_state = rsc_match.group(2)
        rsc_redirect = rsc_match.group(3).replace('\\/', '/')
        rsc_auth = rsc_match.group(4)
        log(f"  RSC 解析: user_id={rsc_user_id} state={rsc_state} auth={rsc_auth}")
    else:
        log(f"  RSC regex 未匹配，尝试 params()")
        p3 = params(r9.text); rsc_auth = p3.get("authorization_session_id",oa); rsc_state = p3.get("state",os)
    from urllib.parse import quote
    send_url = f"https://login.platform.vellum.ai/radar-challenge/send?user_id={rsc_user_id}&state={rsc_state}&redirect_uri={quote(rsc_redirect, safe='')}&authorization_session_id={rsc_auth}"
    log(f"★ radar-challenge/send URL:")
    log(f"  {send_url}")

    # 保存 cookies 和 send_url 给浏览器用
    cookies_list = []
    for c in s.cookies.jar:
        cookies_list.append({"name":c.name,"value":c.value,"domain":c.domain,"path":c.path or "/","secure":bool(c.secure),"httpOnly":bool(getattr(c,'_rest',{}).get('HttpOnly',False))})
    state = {"send_url":send_url, "cookies":cookies_list, "alias":ALIAS}
    (ROOT/"scripts"/"_radar_state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    log(f"已保存 state 到 _radar_state.json")

    # 现在用纯 HTTP GET send_url，看响应
    log("纯 HTTP GET send_url...")
    r10 = s.get(send_url, timeout=20, allow_redirects=True)
    log(f"send GET: {r10.status_code} len={len(r10.text)}")
    (ROOT/"scripts"/"_radar_send_http.html").write_text(r10.text, encoding="utf-8")
    # 找 send 页的 action
    send_aid = aid(r10.text)
    log(f"send 页 action: {send_aid[:25]}")
    # 搜 send 页面里的按钮文字
    for kw in ["Resend","Send code","重新发送","发送验证码","didn't get","没有收到"]:
        if kw.lower() in r10.text.lower():
            log(f"  send 页含关键词: {kw}")

    # 关键：尝试 POST send 页的 action（可能是 resend/send code 按钮）
    if send_aid:
        log(f"尝试 POST send 页 action {send_aid[:16]}...")
        rh = all_hidden(r10.text)
        send_fields = {"redirect_uri":rh.get("redirect_uri",REDIRECT),"authorization_session_id":rh.get("authorization_session_id",rsc_auth if rsc_match else oa),"state":rh.get("state",rsc_state if rsc_match else os)}
        # 可能需要的字段
        for k in ("email","user_id","intent"):
            if rh.get(k): send_fields[k] = rh[k]
        r_send_post = post_action(s, send_url, send_aid, send_fields)
        log(f"send POST: {r_send_post.status_code}")
        log(f"send POST body[:300]: {r_send_post.text[:300]}")
        (ROOT/"scripts"/"_radar_send_post_resp.txt").write_text(r_send_post.text[:2000], encoding="utf-8")

    # 等码
    before_ids2 = mailbox.get_current_ids(account)
    log("等 radar 验证码（120s）...")
    try:
        code2 = mailbox.wait_for_code(account, code_pattern=r"(?<!\d)(\d{6})(?!\d)", timeout=120, before_ids=before_ids2)
    except Exception:
        code2 = ""
    log(f"radar 验证码: {code2}")
    if code2:
        log(f"★ 成功收到 radar 验证码！POST send action 触发了发码")

if __name__ == "__main__":
    main()
