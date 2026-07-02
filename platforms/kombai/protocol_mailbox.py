"""Kombai 浏览器驱动注册 Worker（patchright + 住宅代理 + 邮件确认链接 + OAuth code 换 token）。

流程（vscode-connect OAuth，对标 VS Code 扩展真实流程，已实测闭环）：
  1. 生成 OAuth code（base64 随机16字符，与扩展 ko() 一致）。
  2. patchright 单 context 打开 agent.kombai.com/vscode-connect?type=new&code={code} → 跳 auth.agent.kombai.com/en/signup。
     单 context 贯穿 signup→confirm→SPA 绑定（PropelAuth 需 signup 会话匹配才真正 confirm；fresh context 只显示不激活）。
  3. 填 email+password → click "Sign up with email" → POST /api/fe/v2/signup → 跳 /en/login/confirm_email。
  4. _decode_mail_detail_text(email 模块 get_payload(decode=True) 去 QP 软换行 + =3D→=) + _extract_verification_link
     收【完整】确认链接（wait_for_link 被 QP 软换行截断长 base64 token 成 len=76 → /error?code=InvalidToken，是根因）。
  5. 同会话 patchright navigate 确认链接 → 邮箱确认激活会话。
  6. 会话激活后 goto vscode-connect SPA（带 code）让 SPA 调 /auth/api-key 绑定 code；监听器捕获 IDE token
     + PropelAuth access_token（refresh_token 响应，短时 JWT）。
  7. 换 IDE token：优先 SPA 监听器捕获的 apiKeyToken；次选 POST /auth/api-key?code&appMode + Bearer 绑定
     → GET ?code&appMode + Bearer 取 apiKeyToken（access_token 新鲜时）；兜底 GET-only（预期 403）。
     单发 GET 无 Bearer 被 API Gateway SigV4 拒（403 Missing Authentication Token）。
  8. verify_token + get_subscription_status 查 credits。
  9. 返回 result dict（email/token/referral_code/subscription/email_confirmed）。
"""
from __future__ import annotations

# BLAS 单线程（Windows OOM 防护），必须在 playwright import 前设。
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from platforms.kombai.core import (
    API_BASE,
    KombaiClient,
    build_vscode_connect_url,
    generate_auth_code,
    log,
)
from core.base_mailbox import _extract_verification_link

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))


def _wait_confirm_link(mailbox, account, *, keyword: str = "Kombai", timeout: int = 240,
                       before_ids: set | None = None, log_fn=print) -> str:
    """等 kombai 确认邮件【完整】链接。

    必须用 _decode_mail_detail_text（email 模块 get_payload(decode=True) 去 QP 软换行 + =3D→=）
    再 _extract_verification_link，而非 wait_for_link。wait_for_link 把 raw 与 decoded 拼一起，
    正则优先命中 raw 里被 quoted-printable 软换行截断成 len=76 的残链 → /error?code=InvalidToken
    （长 base64 token 被截断是 InvalidToken 根因）。decoded 文本里软换行已去除，得完整 len≈252 链接。
    """
    import time as _t
    log_fn(f"[kombai] 等待确认邮件链接 (keyword={keyword!r} timeout={timeout}s, QP-decoded)...")
    seen = set(before_ids or [])
    start = _t.time()
    while _t.time() - start < timeout:
        try:
            mails = mailbox._get_mails(account)
            for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                mid = str(mail.get("id", ""))
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                raw = mailbox._get_mail_detail_raw(account, mail)
                # 关键：只用 decoded（QP 软换行已去 + =3D→=），不拼 raw，避免残链误匹配
                decoded = mailbox._decode_mail_detail_text(raw)
                link = _extract_verification_link(decoded, keyword)
                if link and ("confirm_email" in link or "auth" in link):
                    log_fn(f"[kombai] 收到完整确认链接 len={len(link)}: {link[:80]}...{link[-32:]}")
                    return link
        except Exception as exc:
            log_fn(f"[kombai] 收确认链接异常: {exc!r}")
        _t.sleep(3)
    log_fn("[kombai] 等待确认邮件链接超时")
    return ""


def _fill_signup_form(page, email: str, password: str, log_fn=print) -> bool:
    """填 auth.agent.kombai.com/en/signup 表单（email+password）并点 Sign up with email。"""
    # email
    filled = False
    for sel in ("input[type='email']", "input[name='email']", "input#email", "input[autocomplete='email']"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                try:
                    loc.press("Control+a"); loc.press("Delete")
                except Exception:
                    pass
                loc.fill(email, timeout=8000)
                log_fn(f"[kombai] 已填邮箱 {email} (sel={sel})")
                filled = True
                break
        except Exception:
            continue
    if not filled:
        log_fn("[kombai] 未找到邮箱输入框")
        return False
    page.wait_for_timeout(500)
    # password
    for sel in ("input[type='password']", "input[name='password']", "input#password", "input[autocomplete='new-password']"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                try:
                    loc.press("Control+a"); loc.press("Delete")
                except Exception:
                    pass
                loc.fill(password, timeout=8000)
                log_fn(f"[kombai] 已填密码 (sel={sel})")
                break
        except Exception:
            continue
    page.wait_for_timeout(700)
    # click "Sign up with email"
    clicked = False
    for sel in ("button:has-text('Sign up with email')",
                "button[type='submit']:has-text('Sign up')",
                "button:has-text('Sign up')",
                "form button[type='submit']"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                clicked = True
                log_fn(f"[kombai] 点击 Sign up with email (sel={sel})")
                break
        except Exception:
            continue
    if not clicked:
        try:
            page.keyboard.press("Enter")
            clicked = True
        except Exception:
            pass
    return clicked


def _page_state(page) -> dict:
    """读页面状态：url/确认页/登录页/错误。"""
    try:
        txt = page.inner_text("body", timeout=3000)
    except Exception:
        txt = ""
    low = txt.lower()
    url = (page.url or "").lower()
    return {
        "url": page.url or "",
        "text": txt,
        "confirm_email": ("confirm your email" in low) or ("/confirm_email" in url) or ("check your email" in low),
        "login": "/login" in url and "confirm" not in url,
        "dashboard": ("/dashboard" in url) or ("/overview" in url) or ("/agents" in url),
        "vscode_connect": "/vscode-connect" in url,
        "challenge": "/challenge" in url,  # PropelAuth 人机挑战页（Turnstile widget）
        "error": ("error" in low) or ("invalid" in low) or ("already" in low) or ("failed" in low),
    }


class KombaiProtocolMailboxWorker:
    """Kombai 浏览器驱动注册 Worker（patchright + 邮件确认链接 + OAuth code 换 token）。

    构造参数兼容框架 ProtocolMailboxAdapter（link_callback 由框架注入，但 kombai 用 wait_for_link
    直接收链接，link_callback 仅作兜底）。run(email, password, mailbox, mailbox_account) 执行完整链路。
    """

    def __init__(self, *, proxy: str | None = None, timeout: int = 300,
                 log_fn=print, otp_callback: Callable[[], str] | None = None,
                 captcha_solver=None, **_kwargs):
        self.proxy = proxy
        self.timeout = timeout
        self.otp_callback = otp_callback  # 兼容框架，kombai 用 wait_for_link 不用此
        self.captcha_solver = captcha_solver
        raw_log = log_fn or log
        self.log = lambda msg: self._safe_log(raw_log, msg)

    @staticmethod
    def _safe_log(raw_log, msg: str) -> None:
        try:
            raw_log(msg)
        except UnicodeEncodeError:
            try:
                import sys
                sys.stdout.buffer.write((str(msg) + "\n").encode("utf-8", "replace"))
                sys.stdout.buffer.flush()
            except Exception:
                pass

    def run(self, *, email: str = "", password: str = "",
            link_callback: Callable[[], str] | None = None,
            mailbox=None, mailbox_account=None) -> dict[str, Any]:
        """执行 Kombai 注册。email/password 由框架注入；mailbox/mailbox_account 框架按 mail_provider 建好传入。"""
        from core.base_mailbox import MailboxAccount

        if not email:
            raise RuntimeError("Kombai 注册缺少邮箱")
        if not password:
            raise RuntimeError("Kombai 注册缺少密码")
        if mailbox is None or mailbox_account is None:
            raise RuntimeError("Kombai 注册需 mailbox + mailbox_account（框架按 mail_provider 注入）")

        account = mailbox_account
        before_ids = mailbox.get_current_ids(account) if mailbox is not None else set()
        self.log(f"[kombai] 使用邮箱: {email} | 注册前邮件基线: {len(before_ids)} 封")

        # 1. 生成 OAuth code
        auth_code = generate_auth_code()
        connect_url = build_vscode_connect_url(auth_code, type_="new")
        self.log(f"[kombai] 生成 OAuth code={auth_code[:16]}... 连接 URL={connect_url[:80]}")

        result: dict[str, Any] = {
            "email": email,
            "password": password,
            "auth_code": auth_code,
            "proxy": self.proxy,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stages": [],
            "api_base": API_BASE,
        }

        # 2. patchright 打开 vscode-connect
        from contextlib import contextmanager

        @contextmanager
        def _patchright_ctx():
            from patchright.sync_api import sync_playwright
            pw = sync_playwright().start()
            launch_args = [
                "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                "--disable-default-apps", "--disable-extensions", "--disable-sync",
                "--disable-translate", "--disable-hang-monitor", "--disable-domain-reliability",
                "--no-first-run", "--no-default-browser-check", "--no-sandbox",
                "--metrics-recording-only", "--mute-audio",
            ]
            launch_opts = {"headless": False, "args": launch_args}
            if self.proxy:
                p = urlparse(self.proxy)
                pcfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
                if p.username: pcfg["username"] = p.username
                if p.password: pcfg["password"] = p.password
                launch_opts["proxy"] = pcfg
            browser = pw.chromium.launch(**launch_opts)
            try:
                yield browser, pw
            finally:
                try: browser.close()
                except Exception: pass
                try: pw.stop()
                except Exception: pass

        with _patchright_ctx() as (browser, pw):
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = ctx.new_page()

            # 监听网络：捕获 PropelAuth access_token（refresh_token 响应，短时 JWT）+ IDE token（/auth/api-key 200）
            # 实测：exchange IDE token 需 Bearer PropelAuth access_token（POST 绑定→GET 取 apiKeyToken），
            # access_token 短时新鲜，必须在浏览器会话内捕获并立即换。SPA 确认后 goto connect_url 会自动调 /auth/api-key 绑定。
            import json as _json
            captured = {"propel_token": "", "ide_token": "", "apikey_status": None, "apikey_resp": ""}

            def _on_refresh(resp):
                try:
                    if "auth.agent.kombai.com/api/v1/refresh_token" in resp.url and resp.status == 200:
                        j = _json.loads(resp.text())
                        at = str(j.get("access_token") or j.get("accessToken") or j.get("token") or "")
                        if at and not captured["propel_token"]:
                            captured["propel_token"] = at
                            self.log(f"[kombai] 捕获 PropelAuth access_token len={len(at)} head={at[:20]}...")
                except Exception:
                    pass

            def _on_apikey(resp):
                try:
                    if "api.assistant.app.kombai.com/auth/api-key" in resp.url:
                        captured["apikey_status"] = resp.status
                        try:
                            body = resp.text()[:400]
                        except Exception:
                            body = ""
                        captured["apikey_resp"] = body
                        if resp.status == 200 and not captured["ide_token"]:
                            j = _json.loads(body)
                            t = str(j.get("apiKeyToken") or j.get("token") or "")
                            if t:
                                captured["ide_token"] = t
                                self.log(f"[kombai] 捕获 IDE token(apiKeyToken)={t[:24]}...{t[-8:]}")
                except Exception:
                    pass

            page.on("response", _on_refresh)
            page.on("response", _on_apikey)

            opened = False
            for _g in range(3):
                try:
                    page.goto(connect_url, wait_until="commit", timeout=60000)
                    opened = True
                    break
                except Exception as exc:
                    self.log(f"[kombai] goto vscode-connect 失败重试 {_g+1}: {str(exc)[:120]}")
                    page.wait_for_timeout(2000)
            if not opened:
                raise RuntimeError(f"打开 vscode-connect 多次失败: {connect_url[:80]}")
            page.wait_for_timeout(4000)
            st = _page_state(page)
            self.log(f"[kombai] vscode-connect 跳转后 url={st['url'][:80]} confirm={st['confirm_email']} login={st['login']}")

            # 3. 填注册表单（跳转后应在 auth.agent.kombai.com/en/signup）
            # 若跳到 login 页（账号已存在），切回 signup
            if st["login"] and not st["confirm_email"]:
                try:
                    page.goto(connect_url.replace("type=new", "type=new"), wait_until="commit", timeout=30000)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass
            if not _fill_signup_form(page, email, password, log_fn=self.log):
                result["stages"].append({"stage": "fill_signup", "ok": False, "state": st})
                raise RuntimeError("填注册表单失败")
            result["stages"].append({"stage": "fill_signup", "ok": True})

            # 4. 等跳 confirm_email 页（可能先经 /challenge 人机挑战页，Turnstile widget 自动解）
            page.wait_for_timeout(5000)
            st = _page_state(page)
            self.log(f"[kombai] 注册提交后 url={st['url'][:80]} confirm={st['confirm_email']} challenge={st['challenge']} error={st['error']}")
            if not st["confirm_email"]:
                # /challenge 页等 Turnstile 自动解（最多 60s），解完跳 confirm_email/dashboard
                for _ in range(24):
                    if st["confirm_email"] or st["dashboard"]:
                        break
                    page.wait_for_timeout(2500)
                    st = _page_state(page)
            if st["error"] and not st["confirm_email"] and not st["challenge"]:
                result["stages"].append({"stage": "signup_submit", "ok": False, "state": st})
                raise RuntimeError(f"注册被拒: {st['text'][:200]}")
            if not st["confirm_email"] and not st["dashboard"]:
                result["stages"].append({"stage": "signup_submit", "ok": False, "state": st})
                raise RuntimeError(f"注册后未进 confirm_email/dashboard: url={st['url'][:80]}")
            result["stages"].append({"stage": "signup_submit", "ok": True})

            # 5. 收确认邮件链接
            if st["confirm_email"]:
                link = _wait_confirm_link(
                    mailbox, account, keyword="Kombai",
                    timeout=min(self.timeout, 240), before_ids=before_ids, log_fn=self.log,
                )
                if not link:
                    result["stages"].append({"stage": "wait_confirm_link", "ok": False})
                    raise RuntimeError("未收到 Kombai 确认邮件链接")
                result["stages"].append({"stage": "wait_confirm_link", "ok": True, "link": link[:80]})

                # 6. navigate 确认链接
                try:
                    page.goto(link, wait_until="commit", timeout=60000)
                    page.wait_for_timeout(5000)
                except Exception as exc:
                    self.log(f"[kombai] navigate 确认链接异常（可能跳 vscode:// 被拦）: {str(exc)[:120]}")
                st2 = _page_state(page)
                self.log(f"[kombai] 确认链接后 url={st2['url'][:80]} dashboard={st2['dashboard']} vscode_connect={st2['vscode_connect']}")
                result["email_confirmed"] = True
                result["stages"].append({"stage": "confirm_email", "ok": True, "state": st2})

                # 6b. 会话激活后 goto vscode-connect SPA（带 code）让 SPA 调 /auth/api-key 绑定 code → 捕获 IDE token
                # 实测：确认链接必须在 signup 同会话点才真正 confirm+激活会话；激活后 SPA 同源调 /auth/api-key
                # 带 PropelAuth session 绑定 code 拿 apiKeyToken。监听器已在 page 上注册捕获。
                try:
                    page.goto(connect_url, wait_until="commit", timeout=60000)
                except Exception as exc:
                    self.log(f"[kombai] goto SPA 绑定异常: {str(exc)[:120]}")
                for _ in range(12):
                    page.wait_for_timeout(2500)
                    if captured["ide_token"] or captured["apikey_status"]:
                        break
                self.log(f"[kombai] SPA 绑定后 captured ide_token={'YES' if captured['ide_token'] else 'NO'} apikey_status={captured['apikey_status']} propel={'YES' if captured['propel_token'] else 'NO'}")
                result["stages"].append({"stage": "spa_bind", "ok": bool(captured["ide_token"] or captured["propel_token"]),
                                         "apikey_status": captured["apikey_status"]})
            else:
                # 已直接进 dashboard（账号已确认/已存在登录态）
                result["email_confirmed"] = True
                result["stages"].append({"stage": "confirm_email", "ok": True, "skipped": True})

        # 7. OAuth code 换 token（实测：需 PropelAuth access_token Bearer，POST 绑定→GET 取 apiKeyToken）
        client = KombaiClient(proxy=self.proxy, log_fn=self.log)
        token = ""
        # 优先：监听器已在 SPA 绑定时直接捕获到 IDE token（apiKeyToken）
        if captured["ide_token"]:
            token = captured["ide_token"].strip()
            self.log(f"[kombai] 用 SPA 捕获的 IDE token={token[:16]}...{token[-8:]}")
            result["auth_result"] = {"ok": True, "status": 200, "token": token, "source": "spa_captured"}
        # 次选：用捕获的 PropelAuth access_token POST+GET+Bearer 换 apiKeyToken（token 仍新鲜，浏览器刚关）
        elif captured["propel_token"]:
            self.log(f"[kombai] 用 PropelAuth access_token 换 IDE token: code={auth_code[:16]}... (POST 绑定→GET 取 apiKeyToken)")
            exchange_result = client.exchange_code(auth_code, app_mode="Assistant", access_token=captured["propel_token"])
            result["auth_result"] = exchange_result
            token = str(exchange_result.get("token") or exchange_result.get("apiKeyToken") or "").strip()
            if not token:
                err = exchange_result.get("raw") or exchange_result.get("error") or f"status={exchange_result.get('status')}"
                self.log(f"[kombai] POST+GET+Bearer 未拿到 token: {str(err)[:200]}")
        # 兜底：旧 GET-only（无 Bearer，实测 403 Missing Authentication Token，仅诊断）
        if not token:
            self.log("[kombai] 兜底：exchange_code GET-only（无 Bearer，预期 403）")
            exchange_result = client.exchange_code(auth_code, app_mode="Assistant")
            result["auth_result_fallback"] = exchange_result
            token = str(exchange_result.get("token") or "").strip()
        if not token:
            err = (result.get("auth_result") or {}).get("raw") or (result.get("auth_result") or {}).get("error") or "all exchange paths failed"
            result["stages"].append({"stage": "exchange_code", "ok": False, "error": str(err)[:200]})
            self.log(f"[kombai] exchange_code 全部失败: {str(err)[:200]}")
            result["status"] = "exchange_failed"
            result["registered"] = False
            result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            return result
        result["token"] = token
        result["api_key"] = token
        result["referral_code"] = str(exchange_result.get("referralCode") or "")
        result["stages"].append({"stage": "exchange_code", "ok": True})

        # 8. 验证 token + 查订阅 credits
        try:
            verified = client.verify_token(token)
            result["token_verified"] = verified
        except Exception as exc:
            self.log(f"[kombai] verify_token 异常: {exc!r}")
            result["token_verified"] = False
        try:
            subscription = client.get_subscription_status(token)
            result["subscription"] = subscription
            # credits 从 subscription 提取（字段名实测确认）
            result["credit_info"] = {
                "credits": subscription.get("credits") or subscription.get("creditBalance") or subscription.get("remainingCredits"),
                "plan": subscription.get("plan") or subscription.get("tier"),
            }
        except Exception as exc:
            self.log(f"[kombai] 查订阅异常（非阻塞）: {exc!r}")

        result["status"] = "registered"
        result["registered"] = True
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.log(f"[kombai] [OK] 注册成功 {email} token={token[:16]}... referral={result['referral_code']}")
        return result
