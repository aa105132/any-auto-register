"""Vellum 已登录会话能力：提取容器凭据(终端 REST) + 查询余额(billing summary)。

设计依据（开源 github.com/vellum-ai/vellum-assistant，apps/web）：
- 网页 Debug 终端 = 平台 REST：POST/GET(SSE)/POST(input)/DELETE  /v1/assistants/{aid}/terminal/sessions/...
- 余额：GET /v1/organizations/billing/summary/ → effective_balance_usd 等。
- 认证(托管/平台模式)：Django session cookie + X-CSRFToken(__Secure-csrftoken) + Vellum-Organization-Id 头（必带）。
登录走 WorkOS(浏览器)：Continue with Email→邮箱→密码→radar 邮箱码(cfworker 秒收，登录无手机步)。出口需 resin 干净 IP。

对外：
- run_session(email, password, provision_key=True) -> dict  独立完整流程(登录+签发 key/查余额)，用于平台动作。
- extract_on_page(page, provision_key=True) -> dict  在已登录页面内评估(注册闭环复用，免重登)。
"""
from __future__ import annotations

import json
import random
import re
import string
import time
from typing import Any, Callable

from core.config_store import config_store
from core.resin_proxy import resolve_resin_proxy_config
from core.proxy_utils import build_playwright_proxy_settings
from core.base_mailbox import create_mailbox, MailboxAccount

APP_URL = "https://www.vellum.ai/assistant"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
MAIL_DOMAIN = "pangxie888.com"
# 与 web 端 onboarding-cleanup.ts 的 CONSENT_VERSION 对齐；PATCH /v1/user/me/ 记录同意，使 hatch 自然。
CONSENT_VERSION = "2026-06-08"

DONE = "__VELLUM_DONE_8842__"
# bash 拼接：输出=DONE，但命令源文本含 "" 分隔，PTY 回显不含完整 DONE → 避免读到回显误判结束。
_DONE_ECHO = '"__VELLUM_DONE""_8842__"'
EXTRACT_CMD = (
    "cd /app/assistant && jq -r '.credentials[] | \"\\(.service)\\t\\(.field)\"' "
    "/workspace/data/credentials/metadata.json | while IFS=$'\\t' read -r s f; do "
    "echo \"--- $s/$f ---\"; "
    "bun -e \"import { getSecureKeyAsync } from './src/security/secure-keys.ts'; "
    "import { credentialKey } from './src/security/credential-key.ts'; "
    "console.log(await getSecureKeyAsync(credentialKey('${s}', '${f}')));\"; "
    "done; echo " + _DONE_ECHO
)

# 在已登录的 www.vellum.ai 页面内执行：发现 org/assistant、查 billing、（可选）跑终端命令读凭据。
JS_SESSION = r"""
async (params) => {
  const { command, done, terminal } = params;
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  const csrf = (document.cookie.split('; ').findLast(r => r.startsWith('__Secure-csrftoken=')) || '').split('=').slice(1).join('=');
  const listOf = (j) => Array.isArray(j) ? j : (j && (j.results || j.items || j.data)) || [];
  const getJson = async (u, hdrs) => { try { const r = await fetch(u, {credentials:'include', headers: Object.assign({'Accept':'application/json'}, hdrs||{})}); return {status:r.status, body: await r.json().catch(()=>null)}; } catch(e){ return {status:-1, err:String(e)}; } };

  const orgs = await getJson('/v1/organizations/');
  let orgId = ''; const ol = listOf(orgs.body); if (ol[0]) orgId = ol[0].id || ol[0].organization_id || '';
  if (!orgId) return {step:'no_org', orgs};
  const H = (extra) => Object.assign({'Accept':'application/json','Content-Type':'application/json','X-CSRFToken':csrf,'Vellum-Organization-Id':orgId}, extra||{});

  // billing summary (balance). GET first; POST bootstraps a BillingAccount w/ initial credit.
  let billing = await getJson('/v1/organizations/billing/summary/', {'Vellum-Organization-Id': orgId});
  if (!(billing.body && billing.body.effective_balance_usd != null)) {
    try { const br = await fetch('/v1/organizations/billing/summary/', {method:'POST', credentials:'include', headers:H(), body:'{}'}); billing = {status:br.status, body: await br.json().catch(()=>null), bootstrapped:true}; } catch(e){ billing = {err:String(e)}; }
  }

  if (!terminal) return {step:'ok', orgId, billing, output:''};

  let asts = await getJson('/v1/assistants/', {'Vellum-Organization-Id': orgId});
  let al = listOf(asts.body);
  let aid = al[0] && (al[0].id || al[0].assistant_id);
  if (!aid) {
    try {
      const hr = await fetch('/v1/assistants/hatch/', {method:'POST', credentials:'include', headers:H(), body:'{}'});
      const hj = await hr.json().catch(()=>({}));
      aid = hj.id || hj.assistant_id || (hj.data && hj.data.id);
      if (!aid) return {step:'hatch', status:hr.status, body:hj, orgId, billing};
    } catch(e) { return {step:'hatch_err', err:String(e), orgId, billing}; }
  }

  let cs; try { cs = await fetch(`/v1/assistants/${aid}/terminal/sessions/`, {method:'POST', credentials:'include', headers:H(), body:'{}'}); } catch(e){ return {step:'create_session_fetch', err:String(e), aid, orgId, billing}; }
  const csj = await cs.json().catch(()=>({}));
  const sid = csj.session_id || csj.id;
  if (!sid) return {step:'create_session', status:cs.status, body:csj, aid, orgId, billing};

  const out = []; const rawSample = [];
  const ac = new AbortController();
  let sseErr = '';
  const readP = (async () => {
    let resp; try { resp = await fetch(`/v1/assistants/${aid}/terminal/sessions/${sid}/events/`, {credentials:'include', headers: Object.assign({'Accept':'text/event-stream, application/json','X-CSRFToken':csrf,'Vellum-Organization-Id':orgId}), signal:ac.signal}); } catch(e){ sseErr='sse_fetch:'+e; return; }
    if (!resp.ok || !resp.body) { sseErr='sse_status:'+resp.status; return; }
    const reader = resp.body.getReader(); const dec = new TextDecoder(); let buf='';
    while (true) {
      let chunk; try { chunk = await reader.read(); } catch(e){ break; }
      if (chunk.done) break;
      buf += dec.decode(chunk.value, {stream:true});
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const ev = buf.slice(0, idx); buf = buf.slice(idx+2);
        for (const line of ev.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const p = line.slice(5).trim(); if (!p) continue;
          if (rawSample.length < 3) rawSample.push(p.slice(0,160));
          try { let o = JSON.parse(p); o = (o && o.message) ? o.message : o; if (typeof o.data === 'string') { try { out.push(decodeURIComponent(escape(atob(o.data)))); } catch(e){ out.push(atob(o.data)); } } } catch(e) {}
        }
      }
      if (out.join('').includes(done)) { ac.abort(); break; }
    }
  })();

  await sleep(900);
  let inResp = await fetch(`/v1/assistants/${aid}/terminal/sessions/${sid}/input/`, {method:'POST', credentials:'include', headers:H(), body: JSON.stringify({data: command + '\n'})});
  const inStatus = inResp.status;

  const t0 = Date.now();
  while (Date.now() - t0 < 45000) { if (out.join('').includes(done)) break; await sleep(400); }
  ac.abort();
  try { await fetch(`/v1/assistants/${aid}/terminal/sessions/${sid}/`, {method:'DELETE', credentials:'include', headers:H()}); } catch(e){}

  return {step:'ok', aid, orgId, sid, inStatus, sseErr, rawSample, billing, output: out.join('')};
}
"""

# 同会话纯协议签发 assistant_api_key（托管模式正路）。
# 依据开源 cli/src/lib/platform-client.ts：
#   POST /v1/assistants/self-hosted-local/ensure-registration/  body{client_installation_id, runtime_assistant_id, client_platform}
#     → 首次返回 {assistant{id,name}, registration, assistant_api_key, webhook_secret}；runtime_assistant_id 必须 != 平台 assistant id。
#   POST /v1/assistants/self-hosted-local/reprovision-api-key/  同 body → {provisioning:{assistant_api_key}}（轮换，撤销旧 key）。
# 每用户仅允许一个本地 assistant（single_local_assistant_limit）：首次 ensure，之后用同组 id reprovision。
# 同时拿 billing 余额、平台 assistant_id、user_id（GET /v1/user/me/）。认证=同源 cookie + X-CSRFToken + Vellum-Organization-Id。
JS_PROVISION = r"""
async (params) => {
  const { provisionKey, clientInstallationId, runtimeAssistantId, reprovision } = params;
  const uuid = () => (self.crypto && crypto.randomUUID && crypto.randomUUID()) || ('rt-' + Date.now() + '-' + Math.random().toString(16).slice(2));
  const csrf = (document.cookie.split('; ').findLast(r => r.startsWith('__Secure-csrftoken=')) || '').split('=').slice(1).join('=');
  const listOf = (j) => Array.isArray(j) ? j : (j && (j.results || j.items || j.data)) || [];
  const gj = async (u, h) => { try { const r = await fetch(u, {credentials:'include', headers: Object.assign({'Accept':'application/json'}, h||{})}); let b=null; try{b=await r.json();}catch(e){b=null;} return {status:r.status, body:b}; } catch(e){ return {status:-1, err:String(e)}; } };

  const orgs = await gj('/v1/organizations/');
  let orgId=''; const ol=listOf(orgs.body); if(ol[0]) orgId=ol[0].id||ol[0].organization_id||'';
  if(!orgId) return {step:'no_org', orgs};
  const H=(e)=>Object.assign({'Accept':'application/json','Content-Type':'application/json','X-CSRFToken':csrf,'Vellum-Organization-Id':orgId}, e||{});

  // billing summary (GET; POST bootstraps initial credit if missing)
  let billing = await gj('/v1/organizations/billing/summary/', {'Vellum-Organization-Id':orgId});
  if (!(billing.body && billing.body.effective_balance_usd != null)) {
    try { const br=await fetch('/v1/organizations/billing/summary/', {method:'POST', credentials:'include', headers:H(), body:'{}'}); billing={status:br.status, body: await br.json().catch(()=>null), bootstrapped:true}; } catch(e){ billing={err:String(e)}; }
  }

  // platform assistant id + user id
  const asts = await gj('/v1/assistants/', {'Vellum-Organization-Id':orgId});
  const al = listOf(asts.body); const platformAssistantId = (al[0] && (al[0].id||al[0].assistant_id))||'';
  const me = await gj('/v1/user/me/', {'Vellum-Organization-Id':orgId});
  const platformUserId = (me.body && (me.body.id||me.body.user_id||(me.body.user&&me.body.user.id)))||'';

  // 本号自己的邀请码（后端懒创建）：GET /v1/referral-codes/me/ → {code, referral_url}
  let ownInviteCode='', referralUrl='';
  try { const rc = await gj('/v1/referral-codes/me/', {'Vellum-Organization-Id':orgId}); if (rc.body) { ownInviteCode = rc.body.code || ''; referralUrl = rc.body.referral_url || ''; } } catch(e){}

  let apiKey='', webhookSecret='', localAssistantId='', prov=null;
  let cid = clientInstallationId || '', rid = runtimeAssistantId || '';
  if (provisionKey) {
    if (!cid) cid = uuid();
    if (!rid) rid = uuid();   // 必须不同于平台 assistant id
    const url = reprovision ? '/v1/assistants/self-hosted-local/reprovision-api-key/'
                            : '/v1/assistants/self-hosted-local/ensure-registration/';
    try {
      const r = await fetch(url, {method:'POST', credentials:'include', headers:H(), body: JSON.stringify({client_installation_id:cid, runtime_assistant_id:rid, client_platform:'web'})});
      const b = await r.json().catch(()=>null);
      prov = {status:r.status, body:b};
      if (b) {
        apiKey = b.assistant_api_key || (b.provisioning && b.provisioning.assistant_api_key) || '';
        webhookSecret = b.webhook_secret || '';
        localAssistantId = (b.assistant && b.assistant.id) || '';
      }
    } catch(e){ prov={err:String(e)}; }
  }

  return {step:'ok', orgId, billing, platformAssistantId, platformUserId,
          ownInviteCode, referralUrl,
          clientInstallationId:cid, runtimeAssistantId:rid, localAssistantId,
          apiKey, webhookSecret, prov};
}
"""


def parse_credentials(raw_output: str) -> dict:
    """从 PTY 输出解析 '--- service/field ---\\n<value>' 块。"""
    creds: dict[str, str] = {}
    cur = None
    for ln in str(raw_output or "").replace("\r", "").split("\n"):
        s = ln.strip()
        m = re.match(r"^---\s*(\S+)\s*---$", s)
        if m:
            cur = m.group(1)
            continue
        if cur:
            if s and s != DONE:
                creds[cur] = s
            cur = None
    return creds


def _balance_from_billing(billing: Any) -> dict:
    body = billing.get("body") if isinstance(billing, dict) else None
    if not isinstance(body, dict):
        return {}
    out = {}
    for k in ("effective_balance_usd", "settled_balance_usd", "pending_compute_usd", "maximum_balance_usd"):
        if body.get(k) is not None:
            out[k] = str(body.get(k))
    return out


def _shape(res: dict) -> dict:
    """把 JS 返回整理成统一结构。"""
    res = res or {}
    creds = parse_credentials(res.get("output") or "")
    billing = res.get("billing")
    bal = _balance_from_billing(billing)
    return {
        "ok": res.get("step") == "ok",
        "credentials": creds,                    # {"vellum/assistant_api_key": "...", ...}
        "assistant_api_key": creds.get("vellum/assistant_api_key", ""),
        "platform_assistant_id": creds.get("vellum/platform_assistant_id", ""),
        "webhook_secret": creds.get("vellum/webhook_secret", ""),
        "platform_user_id": creds.get("vellum/platform_user_id", ""),
        "platform_organization_id": creds.get("vellum/platform_organization_id", "") or res.get("orgId", ""),
        "balance_usd": bal.get("effective_balance_usd") or bal.get("settled_balance_usd", ""),
        "balance": bal,
        "step": res.get("step"),
        "raw_output": res.get("output") or "",
    }


def _shape_provision(res: dict) -> dict:
    """整理 JS_PROVISION 返回（纯 REST：billing + 平台 id + ensure/reprovision 签发的 key）。"""
    res = res or {}
    bal = _balance_from_billing(res.get("billing"))
    prov = res.get("prov") or {}
    prov_body = prov.get("body") if isinstance(prov, dict) else None
    prov_code = (prov_body or {}).get("code") if isinstance(prov_body, dict) else None
    return {
        "ok": res.get("step") == "ok",
        "credentials": {},
        "assistant_api_key": res.get("apiKey", ""),
        "platform_assistant_id": res.get("platformAssistantId", ""),
        "webhook_secret": res.get("webhookSecret", ""),
        "platform_user_id": res.get("platformUserId", ""),
        "platform_organization_id": res.get("orgId", ""),
        "balance_usd": bal.get("effective_balance_usd") or bal.get("settled_balance_usd", ""),
        "balance": bal,
        "client_installation_id": res.get("clientInstallationId", ""),
        "runtime_assistant_id": res.get("runtimeAssistantId", ""),
        "local_assistant_id": res.get("localAssistantId", ""),
        "own_invite_code": res.get("ownInviteCode", ""),
        "referral_url": res.get("referralUrl", ""),
        "provision_status": (prov.get("status") if isinstance(prov, dict) else None),
        "provision_code": prov_code,
        "step": res.get("step"),
    }


def extract_on_page(page, *, provision_key: bool = False, client_installation_id: str = "",
                    runtime_assistant_id: str = "", reprovision: bool = False,
                    log: Callable[[str], None] = print) -> dict:
    """在已登录的 www.vellum.ai 页面内评估（纯 REST），返回统一结构。

    provision_key=False：仅查 billing 余额 + 平台 id（query_balance 用）。
    provision_key=True ：另调 ensure-registration 签发 assistant_api_key；
      传 client_installation_id+runtime_assistant_id 且 reprovision=True 则走 reprovision-api-key 轮换（已有本地 assistant 的号）。
    """
    res = page.evaluate(JS_PROVISION, {
        "provisionKey": bool(provision_key),
        "clientInstallationId": client_installation_id or "",
        "runtimeAssistantId": runtime_assistant_id or "",
        "reprovision": bool(reprovision),
    })
    shaped = _shape_provision(res)
    if provision_key and not shaped["assistant_api_key"]:
        log(f"[vellum.session] provision 未拿到 key: status={shaped['provision_status']} code={shaped['provision_code']}")
    log(f"[vellum.session] step={shaped['step']} api_key={'yes' if shaped['assistant_api_key'] else 'no'} balance={shaped['balance_usd'] or '-'}")
    return shaped


# ---------------- 独立流程：取 resin + 登录 + 评估 ----------------
_RUN = "".join(random.choices(string.ascii_lowercase, k=3)) + str(random.randint(10, 99))
_SLOT = [0]


def _resin_proxy(account: str) -> str | None:
    if str(config_store.get("resin_enabled", "false")).strip().lower() not in {"1", "true", "yes", "on", "enabled"}:
        return None
    token = config_store.get("resin_token", "") or config_store.get("resin_password", "")
    return resolve_resin_proxy_config({
        "resin_enabled": "true",
        "resin_scheme": config_store.get("resin_scheme", ""),
        "resin_host": config_store.get("resin_host", ""),
        "resin_port": config_store.get("resin_port", ""),
        "resin_token": token,
        "resin_default_platform": config_store.get("resin_default_platform", "Default"),
    }, task_platform="vellum", account=account, require_enabled=True).get("proxy_url")


def _probe_ip(purl: str) -> str:
    import requests
    s = requests.Session(); s.trust_env = False
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = s.get(url, proxies={"http": purl, "https": purl}, timeout=12)
            t = (r.text or "").strip()
            if t and "." in t and len(t) < 64:
                return t
        except Exception:
            pass
    return ""


def _acquire_resin(log, rounds=3, cooldown=10, slots=18):
    for _ in range(rounds):
        time.sleep(cooldown)
        for _i in range(slots):
            n = _SLOT[0]; _SLOT[0] += 1
            purl = _resin_proxy(f"{_RUN}s{n}")
            if not purl:
                return None, ""   # resin 未启用：直连
            ip = _probe_ip(purl)
            if ip:
                log(f"[vellum.session] resin ip={ip}")
                return purl, ip
            time.sleep(1.0)
    raise RuntimeError("resin unreachable")


def _fill(page, sels, val):
    for s in sels:
        try:
            loc = page.locator(s).first
            if loc.count() > 0:
                loc.fill(val, timeout=8000); return True
        except Exception:
            continue
    return False


def _click(page, sels):
    for s in sels:
        try:
            loc = page.locator(s).first
            if loc.count() > 0:
                loc.click(timeout=8000); return True
        except Exception:
            continue
    return False


def _enter_otp(page, code):
    target = None
    for s in ("input[autocomplete='one-time-code']", "input[inputmode='numeric']", "input[name='code']",
              "input#code", "input[type='tel']", "input[type='text']", "input"):
        try:
            loc = page.locator(s)
            if loc.count() > 0:
                target = loc; break
        except Exception:
            continue
    if target is None:
        page.keyboard.type(code, delay=90)
    else:
        if target.count() >= len(code):
            for i, ch in enumerate(code):
                try: target.nth(i).fill(ch)
                except Exception: pass
        else:
            try: target.first.click(); target.first.fill("")
            except Exception: pass
            page.keyboard.type(code, delay=90)
    page.wait_for_timeout(800)
    _click(page, ["button[type=submit]", "button:has-text('Continue')", "button:has-text('Verify')", "form button"])


def _txt(page):
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""


def _flags(page):
    low = _txt(page).lower(); url = (page.url or "").lower()
    cf = ("just a moment" in low) or ("__cf_chl" in url) or ("performing security verification" in low) or ("verify you are not a bot" in low)
    fb = ("forbidden" in low) or ("does not have permission" in low) or ("error 1020" in low)
    return cf, fb


def run_session(email: str, password: str, *, provision_key: bool = False,
                client_installation_id: str = "", runtime_assistant_id: str = "",
                reprovision: bool = False, log: Callable[[str], None] = print,
                max_attempts: int = 14) -> dict:
    """完整流程：resin 干净 IP → 登录(含 radar 邮箱码) → 页面内评估。返回 _shape_provision 结构(含 error)。

    provision_key/ids/reprovision 透传给 extract_on_page：True 时调 ensure-registration/reprovision 签发 assistant_api_key。
    """
    from playwright.sync_api import sync_playwright

    mb = create_mailbox("cfworker", {"cfworker_domain": MAIL_DOMAIN})
    macc = MailboxAccount(email=email, account_id=email)
    last_err = ""
    with sync_playwright() as p:
        for attempt in range(max_attempts):
            proxy_url, ip = _acquire_resin(log)
            launch = {"headless": True, "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]}
            if proxy_url:
                launch["proxy"] = build_playwright_proxy_settings(proxy_url)
            browser = p.chromium.launch(**launch)
            try:
                ctx = browser.new_context(viewport={"width": 1440, "height": 900}, user_agent=UA, locale="en-US")
                ctx.set_default_timeout(45000)
                page = ctx.new_page()
                try:
                    page.goto(APP_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    last_err = "goto failed"; browser.close(); continue
                page.wait_for_timeout(3000)
                cur = page.url or ""
                if cur.startswith("chrome-error") or cur in ("", "about:blank"):
                    last_err = "dead url"; browser.close(); continue
                cf, fb = _flags(page)
                if cf or fb:
                    last_err = "cf" if cf else "403"; browser.close(); continue
                try: before_ids = mb.get_current_ids(macc)
                except Exception: before_ids = set()
                if "/account/login" in cur or "/account/signup" in cur:
                    _click(page, ["button:has-text('Continue with Email')", "a:has-text('Continue with Email')", "button:has-text('Email')"])
                    page.wait_for_timeout(3500)
                try: page.wait_for_selector("input[type=email], input[name=email], input#email", timeout=20000)
                except Exception: pass
                if page.locator("input[type=email], input[name=email], input#email").count() > 0:
                    _fill(page, ["input[type=email]", "input[name=email]", "input#email"], email)
                    _click(page, ["button[type=submit]", "button:has-text('Continue')", "form button"])
                    try: page.wait_for_selector("input[type=password]", timeout=20000)
                    except Exception: pass
                    page.wait_for_timeout(1500)
                    _fill(page, ["input[type=password]", "input#password"], password)
                    _click(page, ["button[type=submit]", "button:has-text('Continue')", "button:has-text('Sign in')", "form button"])
                    page.wait_for_timeout(8000)
                cf, fb = _flags(page)
                if cf or fb:
                    last_err = "cf/403 after login"; browser.close(); continue
                head = " ".join(_txt(page).split())[:300].lower()
                if "radar-challenge" in (page.url or "").lower() or "code challenge" in head or "enter the code" in head:
                    try:
                        code = mb.wait_for_code(macc, keyword="", timeout=150, before_ids=before_ids)
                        log(f"[vellum.session] login email code {code}")
                        _enter_otp(page, code)
                        page.wait_for_timeout(7000)
                    except Exception as e:
                        last_err = f"login code: {repr(e)[:80]}"; browser.close(); continue
                for _ in range(10):
                    page.wait_for_timeout(2000)
                    if "/assistant" in (page.url or "").lower():
                        break
                cf, fb = _flags(page)
                cur = page.url or ""
                if cf or fb or "vellum.ai" not in cur or cur.startswith("chrome-error") or "login.platform.vellum.ai" in cur:
                    last_err = f"not on app ({cur[:50]})"; browser.close(); continue
                shaped = extract_on_page(page, provision_key=provision_key,
                                         client_installation_id=client_installation_id,
                                         runtime_assistant_id=runtime_assistant_id,
                                         reprovision=reprovision, log=log)
                browser.close()
                if shaped["ok"] and (shaped["balance_usd"] or shaped["assistant_api_key"] or not provision_key):
                    return shaped
                last_err = f"extract step={shaped.get('step')}"
                continue
            except Exception as e:
                last_err = f"{type(e).__name__}: {repr(e)[:120]}"
                try: browser.close()
                except Exception: pass
                continue
    return {"ok": False, "error": last_err or "exhausted attempts", "credentials": {}, "assistant_api_key": "", "balance_usd": "", "balance": {}}
