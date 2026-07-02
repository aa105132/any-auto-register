"""Vercel 客户端 + 共享页面交互逻辑。

VercelClient：HTTP 客户端，verify_api_key（curl ai-gateway.vercel.sh/v1/models）+
绑卡/拿 key 占位（审核通过后 4-8h 手动触发，现场 dump dashboard 元素）。

页面交互 helpers（page_state/fill_email/enter_otp 等）从 scripts/test_vercel_register.py
移植，供 protocol_mailbox.py Worker 复用，避免重复维护。
"""
from __future__ import annotations

# Windows + numpy(scipy-openblas) 在 camoufox/patchright 启动时多线程分配线程栈会 OOM 崩溃。
# 必须在 import numpy/playwright 之前限制 BLAS 单线程。
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import json
import re
import time
from typing import Any

import requests

SIGNUP_URL = "https://vercel.com/signup"
LOGIN_URL = "https://vercel.com/login"
AI_GATEWAY_BASE = "https://ai-gateway.vercel.sh/v1"
NATIVE_API_BASE = "https://api.vercel.com"
# Vercel Stripe publishable key（绑卡纯协议用，从浏览器抓包确认）。
STRIPE_PUBLISHABLE_KEY = "pk_live_alyEi3lN0kSwbdevK0nrGwTw"
STRIPE_API_BASE = "https://api.stripe.com/v1"
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [vercel] {msg}", flush=True)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] [vercel] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


class VercelClient:
    """Vercel HTTP 客户端。

    verify_api_key：用 api key 调 ai-gateway.vercel.sh/v1/models，200/401 区分有效。
    绑卡 + 创建 key 留审核通过后做（需登录态 cookie，现场 dump dashboard 元素）。
    """

    def __init__(self, proxy: str | None = None, log_fn=print):
        self.proxy = proxy
        self.log = log_fn or log

    def verify_api_key(self, api_key: str) -> bool:
        """验证 api key：GET ai-gateway.vercel.sh/v1/models 带 Authorization: Bearer。

        返回 True=有效，False=无效/无权限。Vercel AI Gateway key 前缀 v1_。
        """
        if not api_key:
            return False
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            resp = requests.get(
                f"{AI_GATEWAY_BASE}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                proxies=proxies,
                timeout=20,
            )
            if resp.status_code == 200:
                return True
            # 401/403=key 无效或无权限；429=限流但 key 存在（视作有效）
            if resp.status_code == 429:
                return True
            return False
        except Exception as exc:
            self.log(f"[vercel] verify_api_key 异常: {exc!r}")
            return False

    # ===== 纯协议：建 key + 触发免费额度（实测跑通）=====

    def _api_headers(self, vcp_token: str) -> dict:
        """vercel.com/api/* 用 vcp_ user token。"""
        return {"Authorization": f"Bearer {vcp_token}", "Content-Type": "application/json"}

    def list_ai_gateway_keys(self, vcp_token: str, team_id: str) -> list[dict]:
        """列 team 的 AI Gateway key（只返回 partialKey，明文只在创建时给）。"""
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            resp = requests.get(
                f"{NATIVE_API_BASE.replace('https://api.vercel.com', 'https://vercel.com')}/api/api-keys",
                params={"teamId": team_id, "purpose": "ai-gateway", "limit": 20},
                headers=self._api_headers(vcp_token), proxies=proxies, timeout=20,
            )
            if resp.status_code == 200:
                return (resp.json() or {}).get("apiKeys") or []
        except Exception as exc:
            self.log(f"[vercel] list_keys 异常: {exc!r}")
        return []

    def create_ai_gateway_key(self, vcp_token: str, team_id: str, name: str = "auto-register") -> str:
        """纯协议建 AI Gateway key，返回 vck_ 明文 key（只返回一次）。

        实测：POST https://vercel.com/api/api-keys?teamId={team_id}
        body {"purpose":"ai-gateway","name":"..."} → 200 {"apiKeyString":"vck_...","apiKey":{...}}
        需 vcp_ user token（登录 cookie 的 authorization）。
        """
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            resp = requests.post(
                f"https://vercel.com/api/api-keys",
                params={"teamId": team_id},
                headers=self._api_headers(vcp_token),
                json={"purpose": "ai-gateway", "name": name},
                proxies=proxies, timeout=25,
            )
            if resp.status_code == 200:
                data = resp.json() or {}
                key = str(data.get("apiKeyString") or "").strip()
                if key:
                    self.log(f"[vercel] 建 key 成功 name={name} partialKey={(data.get('apiKey') or {}).get('partialKey','')}")
                return key
            self.log(f"[vercel] 建 key 失败 status={resp.status_code} body={resp.text[:200]!r}")
        except Exception as exc:
            self.log(f"[vercel] 建 key 异常: {exc!r}")
        return ""

    def trigger_free_credit(self, vck_key: str, model: str = "openai/gpt-4.1-mini") -> dict:
        """建 key 后调一次 ai-gateway chat/completions 验证 key 可用（绑卡后 Vercel 自动送 $5 额度）。

        实测：绑卡 succeeded 后 balance.cumulativeBalance 直接变 5（Vercel 绑卡自动送，不用调 chat 触发）。
        本调用目的是验证 vck_ key 能调通 ai-gateway + 模型回复（主人要求"调用 key 成功 模型成功回复"）。
        关键坑：①max_tokens 必须 >=16（ai-gateway 拒绝 <16）；②绑卡后 key 同步有延迟，立即调可能 403
        "requires credit card"，需等 ~60-90s 重试才通（卡已绑上 balance.hasVerifiedPaymentMethod=true）。
        返回 {ok, response}。
        """
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        import time as _time
        # 403 key 同步延迟：每 5s 探测一次，最多 6 次（总 30s）。
        # key 同步通常 5-30s 内完成，短间隔多次比单次长等待更早抓住同步完成的窗口。
        max_attempts = 6
        for attempt in range(max_attempts):
            try:
                resp = requests.post(
                    f"{AI_GATEWAY_BASE}/chat/completions",
                    headers={"Authorization": f"Bearer {vck_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20},
                    proxies=proxies, timeout=40,
                )
                if resp.status_code == 200:
                    data = resp.json() or {}
                    choice = (data.get("choices") or [{}])[0]
                    content = (choice.get("message") or {}).get("content", "")
                    self.log(f"[vercel] ai-gateway 调用成功 model={model} resp={content[:40]!r} (attempt={attempt+1})")
                    return {"ok": True, "response": content, "raw": data}
                body = resp.text[:200]
                # 403 requires credit card = key 同步延迟（卡已绑上 balance.hasVerifiedPaymentMethod=true）。
                # 每 5s 重试，最多 6 次（30s）；仍 403 则标 trigger_pending 由 cron 补验证。
                if resp.status_code == 403 and "credit card" in body and attempt < max_attempts - 1:
                    self.log(f"[vercel] ai-gateway 403(key 同步延迟 attempt={attempt+1}/{max_attempts})，等 5s 重试")
                    _time.sleep(5); continue
                self.log(f"[vercel] ai-gateway 调用失败 status={resp.status_code} body={body!r}")
                return {"ok": False, "status": resp.status_code, "body": resp.text[:300]}
            except Exception as exc:
                self.log(f"[vercel] ai-gateway 调用异常: {exc!r}")
                if attempt < max_attempts - 1:
                    _time.sleep(5); continue
                return {"ok": False, "error": repr(exc)}
        return {"ok": False, "error": "key 同步延迟 trigger_pending(卡已绑上，cron 补验证)"}

    def get_credits_balance(self, vcp_token: str, team_id: str) -> dict:
        """查 AI Gateway 余额（cumulativeBalance 应 ~5.0 after trigger）。"""
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            resp = requests.get(
                "https://vercel.com/api/ai/ai-credits-balance",
                params={"teamId": team_id},
                headers=self._api_headers(vcp_token), proxies=proxies, timeout=20,
            )
            if resp.status_code == 200:
                return resp.json() or {}
        except Exception as exc:
            self.log(f"[vercel] 查余额异常: {exc!r}")
        return {}

    def get_team_id(self, vcp_token: str) -> str:
        """从 /api/teams 拿默认 team_id（个人 Hobby 号的 team）。"""
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            resp = requests.get(
                "https://vercel.com/api/teams",
                params={"flags": "true", "permissions": "true", "entitlements": "true"},
                headers=self._api_headers(vcp_token), proxies=proxies, timeout=20,
            )
            if resp.status_code == 200:
                teams = (resp.json() or {}).get("teams") or []
                if teams:
                    return str(teams[0].get("id") or "")
        except Exception as exc:
            self.log(f"[vercel] 拿 team_id 异常: {exc!r}")
        return ""

    def bind_card_protocol(self, vcp_token: str, team_id: str, card: dict) -> dict:
        """纯协议绑卡（无浏览器，实测链路抓包还原）。

        4 步：
        1. POST vercel.com/api/stripe/sources/setup?teamId={team_id}
           body {assign:true,automaticPaymentMethods:true} → {client_secret:"seti_xxx_secret_xxx"}
        2. POST api.stripe.com/v1/setup_intents/{seti}/confirm form：
           pk_live + 卡号 + billing_details + 随机 guid/muid/sid（无 hCaptcha，参考 zo/core.py）→ succeeded + pm_xxx
        3. POST vercel.com/api/stripe/sources/payment-method?teamId={team_id}
           body {paymentMethod:pm_xxx,makeDefault:true,emitAddedUserEvent:true}
        4. POST vercel.com/api/v0/billing-details?teamId={team_id}
           body {address:{line1,city,state,zipCode,country:"us"}}

        card: {number, exp_month, exp_year, cvv, name, address, city, state, postal_code, country}
        返回 {ok, payment_method, setup_intent, card_bound, error}。
        3DS 卡（confirm 返回 requires_action）需浏览器兜底，返回 ok=False。
        """
        import uuid
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        number = str(card.get("number", "")).replace(" ", "")
        exp_month = str(card.get("exp_month", "")).zfill(2)
        exp_year = str(card.get("exp_year", ""))
        cvv = str(card.get("cvv", ""))
        name = card.get("name", "Zo User")
        address = card.get("address", "")
        city = card.get("city", "")
        state = card.get("state", "")
        postal = card.get("postal_code", "")
        country = (card.get("country", "US") or "US").upper()

        # Step 1: 创建 SetupIntent（Vercel 后端用 sk_ 创建，返回 client_secret）
        try:
            r1 = requests.post(
                "https://vercel.com/api/stripe/sources/setup",
                params={"teamId": team_id},
                headers=self._api_headers(vcp_token),
                json={"assign": True, "automaticPaymentMethods": True},
                proxies=proxies, timeout=25,
            )
            client_secret = (r1.json() or {}).get("client_secret", "") if r1.status_code == 200 else ""
            if not client_secret:
                return {"ok": False, "error": f"setup 失败 status={r1.status_code} body={r1.text[:200]}", "step": "setup"}
            self.log(f"[vercel] 纯协议 setup_intent: {client_secret[:25]}...")
        except Exception as exc:
            return {"ok": False, "error": repr(exc), "step": "setup"}

        # Step 2: Stripe confirm（pk_live + 卡号 + 随机 guid/muid/sid，无 hCaptcha）
        seti_id = client_secret.split("_secret_", 1)[0]
        form = {
            "client_secret": client_secret,
            "payment_method_data[type]": "card",
            "payment_method_data[card][number]": number,
            "payment_method_data[card][cvc]": cvv,
            "payment_method_data[card][exp_month]": exp_month,
            "payment_method_data[card][exp_year]": exp_year[-2:],
            "payment_method_data[billing_details][name]": name,
            "payment_method_data[billing_details][address][country]": country,
            "payment_method_data[billing_details][address][line1]": address,
            "payment_method_data[billing_details][address][city]": city,
            "payment_method_data[billing_details][address][postal_code]": postal,
            "payment_method_data[billing_details][address][state]": state,
            "payment_method_data[guid]": str(uuid.uuid4()),
            "payment_method_data[muid]": str(uuid.uuid4()),
            "payment_method_data[sid]": str(uuid.uuid4()),
            "payment_method_data[payment_user_agent]": "stripe.js/39914d4bef; stripe-js-v3/39914d4bef; payment-element; deferred-intent; autopm",
            "payment_method_data[referrer]": "https://vercel.com",
            "expected_payment_method_type": "card",
            "use_stripe_sdk": "true",
            "key": STRIPE_PUBLISHABLE_KEY,
        }
        try:
            r2 = requests.post(
                f"{STRIPE_API_BASE}/setup_intents/{seti_id}/confirm",
                data=form,
                headers={
                    "Authorization": f"Bearer {STRIPE_PUBLISHABLE_KEY}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://vercel.com", "Referer": "https://vercel.com/",
                    "User-Agent": CHROME_UA, "Accept": "application/json",
                },
                proxies=proxies, timeout=30,
            )
            d2 = r2.json() if r2.status_code in (200, 400, 402) else {}
            status = str(d2.get("status", ""))
            pm = str(d2.get("payment_method", ""))
            self.log(f"[vercel] 纯协议 stripe confirm: status={status} pm={pm[:15]} http={r2.status_code}")
            if status == "requires_action":
                return {"ok": False, "error": "3DS requires_action 需浏览器兜底", "step": "confirm", "requires_browser": True, "data": d2}
            if status != "succeeded" or not pm:
                return {"ok": False, "error": f"confirm 未成功 status={status} body={str(d2)[:300]}", "step": "confirm", "data": d2}
        except Exception as exc:
            return {"ok": False, "error": repr(exc), "step": "confirm"}

        # Step 3: 关联 payment_method 到 team
        try:
            r3 = requests.post(
                "https://vercel.com/api/stripe/sources/payment-method",
                params={"teamId": team_id},
                headers=self._api_headers(vcp_token),
                json={"paymentMethod": pm, "makeDefault": True, "emitAddedUserEvent": True},
                proxies=proxies, timeout=25,
            )
            self.log(f"[vercel] 纯协议 attach payment-method: status={r3.status_code}")
            if r3.status_code != 200:
                return {"ok": False, "error": f"attach 失败 status={r3.status_code} body={r3.text[:200]}", "step": "attach"}
        except Exception as exc:
            return {"ok": False, "error": repr(exc), "step": "attach"}

        # Step 4: billing-details
        try:
            r4 = requests.post(
                "https://vercel.com/api/v0/billing-details",
                params={"teamId": team_id},
                headers=self._api_headers(vcp_token),
                json={"address": {"line1": address, "city": city, "state": state, "zipCode": postal, "country": "us"}},
                proxies=proxies, timeout=25,
            )
            self.log(f"[vercel] 纯协议 billing-details: status={r4.status_code}")
        except Exception as exc:
            self.log(f"[vercel] billing-details 异常: {exc!r}")

        return {"ok": True, "payment_method": pm, "setup_intent": seti_id, "card_bound": True}

    def bind_card_and_create_key(self, cookies: dict, card: dict) -> dict:
        """绑卡 + 创建 API key（占位，审核通过后实现）。

        参数：
          cookies: 登录态 cookie dict（注册时落地，4-8h 后登录刷新）。
          card: {number, month, year, cvv} unionpay 卡。
        返回：{api_key, card_bound, ...}。

        TODO（审核通过后现场 dump 实现）：
          1. 用 cookie 登录 dashboard.vercel.com → /~/ai-gateway 或 billing 页。
          2. dump 绑卡表单元素（卡号/月年/cvv input + submit button）。
          3. 填卡提交 → 等绑卡成功。
          4. 进 AI Gateway → Create API key → 抓 v1_ 开头 key。
          5. 用 key 调 AI_GATEWAY_BASE/models 验证。
        """
        raise NotImplementedError("绑卡+拿key 待人工审核通过后现场实现（见 cron 任务）")


# ===== 以下 helpers 从 scripts/test_vercel_register.py 移植，供 Worker 复用 =====
# 保持与脚本同步，避免重复维护。4 个已修 bug（OTP keyword / Hobby keyword /
# phone choicebox locator / 两个 I-don't-know choicebox）必须保留。


def page_state(page) -> dict:
    """读页面状态：url/text/各类输入框/拦截/dashboard/onboarding。"""
    try:
        txt = page.inner_text("body", timeout=3000)
    except Exception:
        txt = ""
    low = txt.lower()
    url = (page.url or "").lower()
    return {
        "url": page.url or "",
        "text": txt,
        "has_email_input": _has_email_input(page),
        "has_otp_input": _has_otp_input(page),
        "further_verification": ("further verification" in low) or ("complete this form" in low)
            or ("try a different sign up method" in low) or ("try again or try a different" in low)
            or ("please try again" in low) or ("for further assistance" in low),
        "account_recovery": "/accountrecovery" in url,
        "dashboard": ("/dashboard" in url) or ("/notifications" in url) or ("/~/" in url)
            or ("/onboarding" in url) or ("/home" in url),
        "onboarding": ("/onboarding" in url) or ("tell us about" in low) or ("what's your name" in low)
            or (("create your" in low) and ("team" in low)) or (("hobby" in low) and (("pro" in low) or ("team" in low)))
            or ("let's get you started" in low) or ("welcome to vercel" in low),
        "otp_sent": ("check your email" in low) or ("enter the code" in low) or ("we sent" in low) or ("verification code" in low),
        "try_different_method": ("try a different method" in low) or ("different method" in low),
        "kicked_to_login": url.rstrip("/").endswith("vercel.com/login") or "/login" in url,
    }


def _has_email_input(page) -> bool:
    try:
        return page.locator("input[type='email'], input[name='email'], input#email, input[autocomplete='email']").count() > 0
    except Exception:
        return False


def _has_otp_input(page) -> bool:
    try:
        return page.locator(
            "input[autocomplete='one-time-code'], input[inputmode='numeric'], input[name='code'], input#code"
        ).count() > 0
    except Exception:
        return False


def fill_email(page, email: str, log_fn=print) -> bool:
    """填邮箱并点 Continue with Email 提交。"""
    filled = False
    for sel in ("input[data-testid='login/email-input']", "input[type='email']", "input[name='email']", "input#email", "input[autocomplete='email']"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                try:
                    loc.press("Control+a"); loc.press("Delete")
                except Exception:
                    pass
                loc.fill(email, timeout=8000)
                log_fn(f"[vercel] 已填邮箱 {email} (sel={sel})")
                filled = True
                break
        except Exception:
            continue
    if not filled:
        log_fn("[vercel] 未找到邮箱输入框")
        return False
    page.wait_for_timeout(700)
    clicked = False
    for sel in ("form[data-testid='login/email-form'] button[type='submit']",
                "button[type='submit']:has-text('Continue with Email')",
                "form button[type='submit']", "button:has-text('Continue with Email')",
                "button:has-text('Continue')", "form button"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                clicked = True
                log_fn(f"[vercel] 点击提交按钮 (sel={sel})")
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


def enter_otp(page, code: str) -> None:
    """填 6 位 OTP。Vercel OTP 多为单字符分框或单框。"""
    target = None
    for s in ("input[autocomplete='one-time-code']", "input[inputmode='numeric']",
              "input[name='code']", "input#code", "input[type='tel']", "input[type='text']"):
        try:
            loc = page.locator(s)
            if loc.count() > 0:
                target = loc
                break
        except Exception:
            continue
    if target is None:
        page.keyboard.type(code, delay=80)
    else:
        cnt = target.count()
        if cnt >= len(code):
            for i, ch in enumerate(code):
                try:
                    target.nth(i).fill(ch, timeout=4000)
                except Exception:
                    pass
        else:
            try:
                target.first.click(); target.first.fill("")
            except Exception:
                pass
            page.keyboard.type(code, delay=80)
    page.wait_for_timeout(800)
    for sel in ("button[type='submit']", "button:has-text('Verify')", "button:has-text('Continue')", "form button"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=6000)
                break
        except Exception:
            continue


def random_name() -> str:
    import random
    first = random.choice(["Aaron", "Brian", "Chloe", "Diane", "Ethan", "Grace", "Helen", "Ivan"])
    last = random.choice(["Mitchell", "Parker", "Reed", "Sawyer", "Turner", "Walsh", "Bennett", "Carter"])
    return f"{first} {last}"
