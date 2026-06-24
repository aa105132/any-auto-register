"""Vellum 纯 HTTP 协议注册 worker（无浏览器，curl_cffi 实现）。

已验证的纯 HTTP 协议链路（2026-06-20 逆向成功）：
1. GET vellum.ai/account/signup → 200（SPA HTML）
2. GET allauth session → 401 + __Secure-csrftoken cookie
3. POST allauth provider redirect → 302 → WorkOS authorize URL
4. GET WorkOS authorize → bootstrap → sign-up 页面（约40% IP 不被 CF 拦截）
5. 解析 sign-up HTML 提取 Server Action ID + hidden fields
6. POST Server Action (multipart/form-data) → page1: first_name/last_name/email
   - 字段名带 1_ 前缀: 1_first_name, 1_last_name, 1_email, 1_intent, etc.
   - 1_signals: base64({puppeteerDetected:false, submittedAtMs:timestamp})
   - 0: '["$K1"]' (RSC form state)
   - intent: 'sign-up'（带连字符）
   - Next-Action header: action_id
7. GET /sign-up/password → 提取新 action ID + hidden fields → POST password（含所有字段）
8. GET /email-verification → 提取 action ID → 等待邮箱验证码 → POST OTP
9. GET /phone → POST phone number → 等待 SMS → POST SMS code
10. 跟随 redirect 回 vellum.ai → REST ensure-registration 签发 api_key

关键发现：
- WorkOS BotCheck 的 1_signals 只有 puppeteer 检测 + 时间戳，可重放
- Cloudflare __cf_bm cookie 是 IP 绑定的，必须在同一 session（同 IP）完成所有请求
- resin 单 IP 只能可靠发 2-3 个请求，需选择稳定 IP + 同 session 重试
- email-verification URL 是 /email-verification（不带 /sign-up/ 前缀）
"""
from __future__ import annotations

import json
import re
import time
import random
import string
import base64
from typing import Any, Callable

from curl_cffi import requests as creq
from core.config_store import config_store
from core.resin_proxy import resolve_resin_proxy_config
from core.registry import load_all

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/149.0.0.0 Safari/537.36"
REDIRECT_URI = "https://www.vellum.ai/accounts/workos/login/callback/"
SITE_URL = "https://www.vellum.ai/"

_slot = [random.randint(1000, 9999)]


def _get_proxy():
    load_all()
    _slot[0] += 1
    r = resolve_resin_proxy_config({
        "resin_enabled": "true",
        "resin_scheme": config_store.get("resin_scheme", ""),
        "resin_host": config_store.get("resin_host", ""),
        "resin_port": config_store.get("resin_port", ""),
        "resin_token": config_store.get("resin_token", ""),
        "resin_default_platform": config_store.get("resin_default_platform", "Default"),
        "resin_platform_map": config_store.get("resin_platform_map", ""),
    }, task_platform="vellum", account=f"pr{int(time.time())%100000}s{_slot[0]}", require_enabled=True)
    return r.get("proxy_url")


class VellumPureProtocolRegister:
    """纯 HTTP 协议注册（无浏览器），通过 resin 疯狂轮换 IP。"""

    def __init__(
        self,
        *,
        otp_callback: Callable[[], str] | None = None,
        phone_callback: Callable[[], str] | None = None,
        invite_code: str = "H5QJRV",
        country_code: str = "+86",
        max_attempts: int = 100,
        phone_wait_attempts: int = 20,
        log_fn: Callable[[str], None] = print,
        proxy: str | None = None,
    ) -> None:
        self.otp_callback = otp_callback
        self.phone_callback = phone_callback
        self.invite_code = invite_code
        self.country_code = country_code
        self.max_attempts = max_attempts
        self.phone_wait_attempts = phone_wait_attempts
        self.log = log_fn
        # 外部注入固定代理（如 Clash 本地节点）：非空时不再轮换 resin，
        # 全程复用同一出口 IP。resin 机房 IP 全被 WorkOS Radar policy_denied，
        # 住宅/干净节点（Clash）才有机会过 password 步。
        self.fixed_proxy = str(proxy or "").strip() or None

    def _l(self, msg: str) -> None:
        self.log(f"[vellum-proto] {msg}")

    def _new_session(self) -> creq.Session:
        proxy = self.fixed_proxy or _get_proxy()
        s = creq.Session(impersonate="chrome131")
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s

    @staticmethod
    def _extract_action_id(html: str) -> str:
        m = re.search(r'([a-f0-9]{40}).{0,20}bound', html)
        return m.group(1) if m else ""

    @staticmethod
    def _extract_hidden_fields(html: str) -> dict:
        fields = {}
        for m in re.finditer(r'name="([^"]+)"[^>]*value="([^"]*)"', html):
            name, val = m.group(1), m.group(2)
            if name in ("authorization_session_id", "state", "redirect_uri", "intent"):
                fields[name] = val
        return fields

    @staticmethod
    def _extract_page_params(rsc_text: str) -> dict:
        """从 RSC __PAGE__ 响应提取路由参数（authorization_session_id, state, redirect_uri）。"""
        m = re.search(r'__PAGE__\?(\{[^}]+\})', rsc_text)
        if m:
            try:
                return json.loads(m.group(1).replace('\\"', '"'))
            except Exception:
                pass
        return {}

    def _post_server_action(self, s: creq.Session, url: str, action_id: str, fields: dict) -> creq.Response:
        """POST Next.js Server Action: multipart/form-data + Next-Action header + 1_ prefix fields + signals + 0.

        signals 是 WorkOS BotCheck 的环境指纹：真实浏览器会采集 createdAtMs/timezone/
        language/hardwareConcurrency/webdriver/userAgent/appVersion/platform/screen + canvasHash/
        audioHash/webGL/minimalSurface/worker 等深层指纹。简陋的 {puppeteerDetected:false} 在
        IP 信誉好时也能过，但完整指纹更稳。这里构造与 Camoufox 抓到的真实 signals 一致的
        完整 Chrome 环境（从浏览器抓包逆向，字段顺序和值都匹配真实 Firefox/Chrome）。
        """
        ua = UA  # 模块级 UA 常量，Chrome/149，与 curl_cffi impersonate 一致
        now_ms = int(time.time() * 1000)
        signals_json = json.dumps({
            "createdAtMs": now_ms,
            "timezone": "Asia/Hong_Kong",
            "language": "zh-HK",
            "hardwareConcurrency": 32,
            "webdriver": False,
            "userAgent": ua,
            "appVersion": ua.replace("Mozilla/", ""),
            "platform": "Win32",
            "screen": {
                "width": 1920,
                "height": 1080,
                "availWidth": 1920,
                "availHeight": 1032,
                "windowOuterWidth": 1936,
                "windowOuterHeight": 1048,
                "colorDepth": 24,
                "pixelDepth": 24,
            },
            "rangeErrorLength": 0,
            "evalStringLength": 37,
            "playwrightDetected": False,
            "phantomDetected": False,
            "nightmareDetected": False,
            "seleniumDetected": False,
            "puppeteerDetected": False,
            "maxTouchPoints": 10,
            "permissionsState": "prompt",
            "notificationPermission": "default",
            "devicePixelRatio": 1.5,
            "pluginsLength": 5,
            "mimeTypesCount": 2,
            "documentHidden": False,
            "documentVisibilityState": "visible",
            "mediaPreferences": {
                "colorScheme": "dark",
                "reducedMotion": False,
                "reducedTransparency": False,
                "contrast": "no-preference",
                "colorGamut": "srgb",
                "hdr": False,
                "forcedColors": False,
                "invertedColors": False,
            },
            "webGLVendor": "Google Inc. (Microsoft)",
            "webGLRenderer": "ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0), or similar",
            "minimalSurface": {
                "windowFeaturesHash": "dG_wL_cSDPoICmbxzRNj0ZHJGVOkwFOGGiTi7L4lT90",
                "windowFeaturesCount": 892,
                "cssKeysHash": "tlZTH-moGx_55o6-xSuKEYL9dZk-ybn2IVFsoC2fsdM",
                "cssKeysCount": 1311,
                "voicesHash": "KKEkutbdwufQNTjHAztVs7y3X1DcV3Agc4w6ViRJffU",
                "voicesLocalCount": 5,
                "voicesRemoteCount": 0,
                "voicesLanguagesCount": 2,
                "mediaMimeHash": "djbIVlBnq0wyvT4m85wSUyeqLiEeqaQFpX8fyodcE14",
                "mediaMimeCount": 8,
                "fontsHash": "WRfQIPgS_OOrqPfpU1IraGr6zlsI-kSh9N2bv2dGWuY",
                "fontsCount": 16,
            },
            "worker": {
                "ok": True,
                "hardwareConcurrency": 32,
                "platform": "Win32",
                "userAgent": ua,
                "language": "zh-HK",
                "webGLRenderer": "ANGLE (Microsoft, Microsoft Basic Render Driver Direct3D11 vs_5_0 ps_5_0), or similar",
                "webGLVendor": "Google Inc. (Microsoft)",
            },
            "canvasHash": "-XZ0cceJ5yyePzlJkTzj50rgYfXUyO3mb2K2yeeg8bM",
            "audioHash": "_O1t9EFQ5fmXLd1XJ0ht152FhOgOTlGYNjkSXTt24cI",
            "mathHash": "5XmgTCq9e_ehyn6Jz-CpeljHt18hMoosXNMSaDtEebU",
            "intlHash": "AL83QbFqWI1usaZZ4gdn5yzlhcXP1ABsV8cnvEfQRa0",
            "webGLParamsHash": "wlcy5gfVP5anM115Dcj3wo59lOA9qSc33BVgQTgJEcM",
            "puppeteerDocumentNotAvailable": False,
            "submittedAtMs": now_ms,
        })
        signals_b64 = base64.b64encode(signals_json.encode()).decode()
        boundary = "----WebKitFormBoundary" + "".join(random.choices(string.ascii_letters + string.digits, k=16))
        all_fields = {"1_signals": signals_b64}
        for k, v in fields.items():
            all_fields[f"1_{k}"] = v
        all_fields["0"] = '["$K1"]'
        parts = []
        for name, value in all_fields.items():
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
        parts.append(f"--{boundary}--\r\n")
        return s.post(url, data="".join(parts).encode("utf-8"), headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Next-Action": action_id, "Accept": "text/x-component",
            "Referer": url, "Origin": "https://login.platform.vellum.ai",
        }, timeout=20, allow_redirects=False)

    def run(self, *, email: str, password: str) -> dict:
        """完整纯 HTTP 协议注册流程。"""
        first_name = random.choice(["Aaron", "Brian", "Chloe", "Diane", "Ethan"])
        last_name = random.choice(["Mitchell", "Parker", "Reed", "Sawyer", "Turner"])
        self._l(f"开始纯协议注册: {email} / {first_name} {last_name}")

        for attempt in range(self.max_attempts):
            self._l(f"尝试 {attempt + 1}/{self.max_attempts}")
            s = self._new_session()
            try:
                # Step 1-2: GET vellum signup + allauth session (CSRF cookie)
                r1 = s.get("https://www.vellum.ai/account/signup", timeout=8, allow_redirects=True)
                if r1.status_code != 200:
                    continue
                r2 = s.get("https://www.vellum.ai/_allauth/browser/v1/auth/session",
                           timeout=8, headers={"Accept": "application/json"})
                if r2.status_code != 401:
                    continue
                csrf = ""
                for c in (s.cookies.jar if hasattr(s.cookies, "jar") else s.cookies):
                    if "csrftoken" in c.name.lower():
                        csrf = c.value
                        break
                if not csrf:
                    continue

                # Step 3: POST allauth provider redirect → WorkOS authorize URL
                r3 = s.post("https://www.vellum.ai/_allauth/browser/v1/auth/provider/redirect",
                    data={"provider": "workos", "callback_url": "https://www.vellum.ai/account/provider/callback?authIntent=signup",
                          "process": "login", "intent": "signup", "csrfmiddlewaretoken": csrf},
                    headers={"X-CSRFToken": csrf, "Referer": "https://www.vellum.ai/account/signup"},
                    timeout=8, allow_redirects=False)
                workos_url = r3.headers.get("Location", "") or r3.headers.get("location", "")
                if not workos_url:
                    continue

                # Step 4: Follow WorkOS authorize → bootstrap → sign-up page
                r4 = s.get(workos_url, timeout=12, allow_redirects=True)
                final_url = str(r4.url)
                if r4.status_code != 200 or "just a moment" in r4.text.lower():
                    if "just a moment" in r4.text.lower():
                        self._l("  CF challenge, 换 IP")
                    continue
                if "sign-up" not in final_url:
                    continue

                # Step 5: Extract action ID + hidden fields from sign-up page
                action_id = self._extract_action_id(r4.text)
                hidden = self._extract_hidden_fields(r4.text)
                auth_sid = hidden.get("authorization_session_id", "")
                state = hidden.get("state", "")
                if not action_id or not auth_sid:
                    continue
                self._l(f"  到达 sign-up! action={action_id[:12]}...")

                # Step 6: POST page1 (first_name, last_name, email)
                r5 = self._post_server_action(s, final_url, action_id, {
                    "first_name": first_name, "last_name": last_name, "email": email,
                    "intent": "sign-up", "redirect_uri": REDIRECT_URI,
                    "authorization_session_id": auth_sid, "state": state,
                })
                if r5.status_code != 303:
                    continue
                self._l("  page1 OK → password")

                # Step 7: GET password page + POST password
                params = self._extract_page_params(r5.text)
                pwd_auth = params.get("authorization_session_id", auth_sid)
                pwd_state = params.get("state", state)
                pwd_url = f"https://login.platform.vellum.ai/sign-up/password?state={pwd_state}&redirect_uri=https%3A%2F%2Fwww.vellum.ai%2Faccounts%2Fworkos%2Flogin%2Fcallback%2F&authorization_session_id={pwd_auth}"
                r6 = s.get(pwd_url, timeout=15, allow_redirects=True)
                if r6.status_code != 200 or "just a moment" in r6.text.lower():
                    self._l("  password page GET failed")
                    continue
                pwd_action = self._extract_action_id(r6.text) or action_id
                # 从 password page HTML 提取 hidden fields（更可靠）
                pwd_hidden = self._extract_hidden_fields(r6.text)
                pwd_auth = pwd_hidden.get("authorization_session_id", pwd_auth)
                pwd_state = pwd_hidden.get("state", pwd_state)
                r7 = self._post_server_action(s, pwd_url, pwd_action, {
                    "first_name": first_name, "last_name": last_name, "email": email,
                    "password": password, "intent": "sign-up",
                    "redirect_uri": REDIRECT_URI, "authorization_session_id": pwd_auth, "state": pwd_state,
                })
                if r7.status_code != 303 or "verification" not in r7.text.lower():
                    self._l(f"  password failed: status={r7.status_code}")
                    # 打印 body 用于诊断：policy_denied=IP 问题，其它=payload 问题
                    try:
                        self._l(f"  pwd body[:300]: {r7.text[:300]}")
                    except Exception:
                        pass
                    continue
                self._l("  password OK → email verification")

                # Step 8: GET email-verification page + POST OTP
                params2 = self._extract_page_params(r7.text)
                otp_auth = params2.get("authorization_session_id", pwd_auth)
                otp_state = params2.get("state", pwd_state)
                # URL: /email-verification (NOT /sign-up/email-verification)
                otp_url = f"https://login.platform.vellum.ai/email-verification?state={otp_state}&redirect_uri=https%3A%2F%2Fwww.vellum.ai%2Faccounts%2Fworkos%2Flogin%2Fcallback%2F&authorization_session_id={otp_auth}"
                # 同 session 重试 GET（CF __cf_bm cookie 是 IP 绑定的，不能换 session）
                r8 = None
                for otp_retry in range(5):
                    try:
                        r8 = s.get(otp_url, timeout=15, allow_redirects=True)
                        if r8.status_code == 200 and "just a moment" not in r8.text.lower():
                            break
                    except Exception:
                        pass
                    time.sleep(1)
                if not r8 or r8.status_code != 200:
                    self._l("  email verification page GET failed")
                    continue
                # 提取所有 hidden fields（包括 pending_authentication_token）
                otp_hidden = {}
                for m4 in re.finditer(r'name="([^"]+)"[^>]*value="([^"]*)"', r8.text):
                    if m4.group(1) not in ("viewport", "next-size-adjust", "signals"):
                        otp_hidden[m4.group(1)] = m4.group(2)
                otp_auth = otp_hidden.get("authorization_session_id", otp_auth)
                otp_state = otp_hidden.get("state", otp_state)
                otp_action = self._extract_action_id(r8.text) or pwd_action
                self._l(f"  email verify page OK, action={otp_action[:12]}...")

                # 等待邮箱验证码（用空 before_ids — 新邮箱在注册前没有邮件）
                if not self.otp_callback:
                    raise RuntimeError("Vellum 邮箱验证需要 otp_callback")
                code = (self.otp_callback() or "").strip()
                if not code:
                    raise RuntimeError("Vellum 未收到邮箱验证码")
                self._l(f"  邮箱验证码: {code}")
                # POST OTP — 包含 pending_authentication_token 字段
                otp_fields = {
                    "code": code, "first_name": first_name, "last_name": last_name, "email": email,
                    "intent": "sign-up", "redirect_uri": REDIRECT_URI,
                    "authorization_session_id": otp_auth, "state": otp_state,
                }
                pending_token = otp_hidden.get("pending_authentication_token", "")
                if pending_token:
                    otp_fields["pending_authentication_token"] = pending_token
                r9 = self._post_server_action(s, otp_url, otp_action, otp_fields)
                has_phone = "phone" in r9.text.lower() or "radar" in r9.text.lower()
                has_dashboard = "dashboard" in r9.text.lower() or "onboarding" in r9.text.lower()
                # 303 = 成功重定向。invalid_params/policy_denied 才是错误
                has_err = r9.status_code not in (200, 303) or "invalid_params" in r9.text or "policy_denied" in r9.text
                self._l(f"  OTP POST: status={r9.status_code} phone={has_phone} dashboard={has_dashboard} err={has_err}")
                if has_err and not has_phone:
                    self._l(f"  OTP error: {r9.text[:200]}")
                    continue

                # Step 9: Phone verification
                if has_phone:
                    self._l("  手机验证步骤...")
                    if not self.phone_callback:
                        raise RuntimeError("Vellum 手机验证需要 phone_callback")
                    phone = ""
                    for i in range(self.phone_wait_attempts):
                        phone = (self.phone_callback() or "").strip()
                        if phone:
                            break
                        time.sleep(15)
                    if not phone:
                        raise RuntimeError("Vellum 未取到手机号")
                    raw = re.sub(r"\D", "", phone)
                    if self.country_code == "+86":
                        # 豪猪返回 11 位中国号码（1开头），若带 86 前缀则去掉
                        if raw.startswith("86") and len(raw) == 13:
                            raw = raw[2:]
                        national = raw
                    elif self.country_code == "+1":
                        national = raw[1:] if raw.startswith("1") and len(raw) == 11 else raw
                    else:
                        national = raw
                    self._l(f"  phone {phone} -> {self.country_code} {national}")
                    params3 = self._extract_page_params(r9.text)
                    phone_auth = params3.get("authorization_session_id", otp_auth)
                    phone_st = params3.get("state", otp_state)
                    # phone 页真实路径是 /radar-challenge（WorkOS Radar 手机验证），不是 /phone。
                    # browser_register.py 的 phone_step 判定用 "radar-challenge" in url，
                    # 且实测 /phone?state=... 返回 404（Next.js __next_error__）。
                    phone_url = f"https://login.platform.vellum.ai/radar-challenge?state={phone_st}&redirect_uri=https%3A%2F%2Fwww.vellum.ai%2Faccounts%2Fworkos%2Flogin%2Fcallback%2F&authorization_session_id={phone_auth}"
                    # phone page GET 可能因 CF/网络瞬时失败：同 session 重试，不能 continue 换 session——
                    # 邮箱此时已在 WorkOS 注册（OTP 已提交 303），换 session 重试会 email_not_available。
                    r10 = None
                    for phone_get_try in range(4):
                        try:
                            r10 = s.get(phone_url, timeout=20, allow_redirects=True)
                            if r10.status_code == 200 and "just a moment" not in r10.text.lower():
                                break
                            self._l(f"  phone page GET 重试 {phone_get_try+1}: status={r10.status_code} body[:80]={r10.text[:80]}")
                        except Exception as exc:
                            self._l(f"  phone page GET 异常 {phone_get_try+1}: {type(exc).__name__}: {str(exc)[:80]}")
                        time.sleep(2)
                    if not r10 or r10.status_code != 200:
                        self._l(f"  phone page GET 最终失败: url={phone_url[:80]}")
                        # 邮箱已注册，不能换 session 重试；直接抛错让外层标注邮箱已用
                        raise RuntimeError(f"vellum_phone_page_failed: 邮箱已注册但手机页加载失败 email={email}")
                    phone_action = self._extract_action_id(r10.text) or otp_action
                    phone_hidden = self._extract_hidden_fields(r10.text)
                    phone_auth = phone_hidden.get("authorization_session_id", phone_auth)
                    phone_st = phone_hidden.get("state", phone_st)
                    r11 = self._post_server_action(s, phone_url, phone_action, {
                        "country_code": self.country_code, "local_number": national,
                        "first_name": first_name, "last_name": last_name, "email": email,
                        "intent": "sign-up", "redirect_uri": REDIRECT_URI,
                        "authorization_session_id": phone_auth, "state": phone_st,
                    })
                    self._l(f"  phone POST: status={r11.status_code}")
                    # 诊断：打印 phone POST body，判断 200 是真发短信还是错误（如 invalid phone）
                    try:
                        self._l(f"  phone POST body[:300]: {r11.text[:300]}")
                    except Exception:
                        pass
                    # 提取 radar-challenge 页的真实 input 字段名，校验 country_code/local_number 是否正确
                    try:
                        _inputs = re.findall(r'<input[^>]*name="([^"]*)"[^>]*>', r10.text)
                        self._l(f"  radar-challenge input names: {_inputs}")
                    except Exception:
                        pass
                    sms = (self.phone_callback() or "").strip()
                    if not sms:
                        raise RuntimeError("Vellum 未收到短信验证码")
                    self._l(f"  短信验证码: {sms}")
                    r12 = self._post_server_action(s, phone_url, phone_action, {
                        "code": sms, "first_name": first_name, "last_name": last_name, "email": email,
                        "intent": "sign-up", "redirect_uri": REDIRECT_URI,
                        "authorization_session_id": phone_auth, "state": phone_st,
                    })
                    has_dash = "dashboard" in r12.text.lower() or "onboarding" in r12.text.lower()
                    self._l(f"  SMS POST: status={r12.status_code} dashboard={has_dash}")

                # Step 10: REST ensure-registration
                current_result = r12 if "r12" in dir() else (r11 if "r11" in dir() else r9)
                if has_dashboard or "vellum.ai" in current_result.text.lower():
                    self._l("  注册成功! 走 REST 闭环签发 api_key...")
                    # 跟随 redirect 回 vellum.ai
                    params_final = self._extract_page_params(current_result.text)
                    redirect_url = params_final.get("redirect_uri", REDIRECT_URI)
                    if "vellum.ai" in redirect_url:
                        r_final = s.get(redirect_url, timeout=15, allow_redirects=True)
                        final_url = str(r_final.url)
                    # REST ensure-registration
                    if "vellum.ai" in final_url:
                        r_org = s.get("https://www.vellum.ai/v1/organizations/",
                            headers={"Accept": "application/json"}, timeout=15)
                        org_data = r_org.json() if r_org.status_code == 200 else {}
                        org_list = org_data.get("results", org_data.get("items", [])) if isinstance(org_data, dict) else []
                        org_id = org_list[0].get("id", "") if org_list else ""
                        if org_id:
                            import uuid as _uuid
                            cid = str(_uuid.uuid4())
                            rid = str(_uuid.uuid4())
                            csrf_val = ""
                            for c in (s.cookies.jar if hasattr(s.cookies, "jar") else s.cookies):
                                if "csrftoken" in c.name.lower():
                                    csrf_val = c.value
                            r_prov = s.post("https://www.vellum.ai/v1/assistants/self-hosted-local/ensure-registration/",
                                json={"client_installation_id": cid, "runtime_assistant_id": rid, "client_platform": "web"},
                                headers={"Content-Type": "application/json", "Accept": "application/json",
                                         "X-CSRFToken": csrf_val, "Vellum-Organization-Id": org_id},
                                timeout=30)
                            prov = r_prov.json() if r_prov.status_code == 200 else {}
                            api_key = prov.get("assistant_api_key", "")
                            if api_key:
                                self._l("  API Key 签发成功!")
                                return {
                                    "email": email, "password": password, "api_key": api_key,
                                    "ai_api_token": api_key, "assistant_api_key": api_key,
                                    "webhook_secret": prov.get("webhook_secret", ""),
                                    "platform_organization_id": org_id,
                                    "client_installation_id": cid, "runtime_assistant_id": rid,
                                    "phone_verified": True, "landed_url": final_url,
                                }
                    return {"email": email, "password": password, "api_key": "", "phone_verified": True, "landed_url": final_url}

            except Exception as e:
                self._l(f"  异常: {type(e).__name__}: {str(e)[:80]}")
            time.sleep(0.02)

        raise RuntimeError(f"Vellum 纯协议注册未成功：用尽 {self.max_attempts} 次尝试")
