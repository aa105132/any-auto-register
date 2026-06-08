"""Enter platform - protocol mailbox registration worker.

Combines CDP browser for Auth0 signup (Turnstile + OTP) with pure HTTP for
post-registration enrichment: workspace fetch, project create, entercloud bind,
AI capability token extraction, and optional enter2api remote push.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid
from typing import Any, Callable

import requests

from application.mailbox_inventory_support import add_mailbox_domain_blacklist

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ModuleNotFoundError:
    sync_playwright = None
    PLAYWRIGHT_AVAILABLE = False

from platforms.enter.core import (
    AUTH0_DOMAIN,
    CLIENT_ID,
    CODE_VERIFIER,
    REDIRECT_URI,
    EnterClient,
    extract_ai_api_token,
    is_success_response,
    _parse_auth_code_from_url,
    _utcnow_iso,
)


class EnterProtocolMailboxWorker:

    def __init__(
        self,
        proxy: str | None = None,
        referrer_code: str = "",
        workspace_id: str = "10000010136",
        project_name_prefix: str = "enter-project",
        project_prompt: str = "Create a minimal hello world web app.",
        enable_entercloud: bool = True,
        enable_ai_capability: bool = True,
        enter2api_base_url: str = "",
        enter2api_enabled: bool = False,
        chrome_path: str = "",
        cdp_url: str = "",
        timeout: int = 120,
        log_fn: Any = None,
    ):
        self._proxy = proxy
        self._referrer_code = referrer_code
        self._workspace_id = workspace_id
        self._project_name_prefix = project_name_prefix
        self._project_prompt = project_prompt
        self._enable_entercloud = enable_entercloud
        self._enable_ai_capability = enable_ai_capability
        self._enter2api_base_url = enter2api_base_url.rstrip("/") if enter2api_base_url else ""
        self._enter2api_enabled = enter2api_enabled and bool(self._enter2api_base_url)
        self._chrome_path = chrome_path
        self._cdp_url = cdp_url
        self._timeout = timeout
        self._session = requests.Session()
        self._log = log_fn or (lambda msg: None)
        self._referral_pool_file = "D:/Desktop/cat/any-auto-register/_tmp/enter_referral_codes.json"

    def _l(self, msg: str) -> None:
        self._log(f"[enter] {msg}")

    def run(
        self,
        email: str,
        password: str,
        otp_callback: Callable[[], str],
        captcha_solver: Any = None,
    ) -> dict[str, Any]:

        self._l(f"starting Enter registration for {email}")

        try:
            result = self._run_auth0_protocol_flow(
                email=email,
                password=password,
                otp_callback=otp_callback,
                captcha_solver=captcha_solver,
            )
        except Exception as exc:
            if "enter_email_domain_not_allowed" in str(exc) or "domain is not allowed" in str(exc).lower():
                add_mailbox_domain_blacklist(email, platform="enter", reason="enter_email_domain_not_allowed")
                self._l(f"email domain blacklisted for enter: {email.rsplit('@', 1)[-1].lower()}")
                raise RuntimeError(f"Enter 邮箱域名不允许注册，已拉黑域名: {email.rsplit('@', 1)[-1].lower()}") from exc
            raise

        access_token = result.get("access_token", "")
        if not access_token:
            raise RuntimeError("Registration failed: no access_token obtained")

        self._enrich_result(result, access_token)

        # Persist referral_code_self for future accounts to claim
        ref_code = result.get("referral_code_self", "")
        if ref_code:
            self._save_referral_code(ref_code)

        if self._enter2api_enabled:
            self._push_to_remote(result)

        result["metadata"] = {
            "registered_at": _utcnow_iso(),
            "platform": "enter",
            "client_id": CLIENT_ID,
            "workspace_id": result.get("workspace_id", ""),
        }

        return result

    def _auth_headers(self, referer: str = "") -> dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": f"https://{AUTH0_DOMAIN}",
        }
        if referer:
            headers["Referer"] = referer
        return headers

    def _extract_forms(self, html: str) -> list[dict[str, Any]]:
        forms: list[dict[str, Any]] = []
        for form_match in re.finditer(r"<form\b(?P<attrs>[^>]*)>(?P<body>.*?)</form>", html or "", re.I | re.S):
            attrs = self._parse_attrs(form_match.group("attrs"))
            body = form_match.group("body")
            inputs = []
            for input_match in re.finditer(r"<input\b(?P<attrs>[^>]*)>", body, re.I | re.S):
                ia = self._parse_attrs(input_match.group("attrs"))
                inputs.append({"name": ia.get("name", ""), "type": ia.get("type", ""), "value": ia.get("value", ""), "id": ia.get("id", "")})
            buttons = []
            for button_match in re.finditer(r"<button\b(?P<attrs>[^>]*)>(?P<text>.*?)</button>", body, re.I | re.S):
                ba = self._parse_attrs(button_match.group("attrs"))
                text = re.sub(r"<[^>]+>", " ", button_match.group("text"))
                buttons.append({"name": ba.get("name", ""), "value": ba.get("value", ""), "aria_hidden": ba.get("aria-hidden", ""), "text": re.sub(r"\s+", " ", text).strip()})
            forms.append({"action": attrs.get("action", ""), "method": attrs.get("method", "post"), "inputs": inputs, "buttons": buttons})
        return forms

    def _parse_attrs(self, raw: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for m in re.finditer(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(['\"])(.*?)\2", raw or "", re.S):
            attrs[m.group(1)] = m.group(3).replace("&quot;", '"').replace("&amp;", "&")
        return attrs

    def _form_payload(self, forms: list[dict[str, Any]], *, updates: dict[str, str]) -> tuple[str, dict[str, str]]:
        if not forms:
            raise RuntimeError("Auth0 form not found")
        form = forms[0]
        payload: dict[str, str] = {}
        for item in form.get("inputs") or []:
            name = str(item.get("name") or "")
            if name:
                payload[name] = str(item.get("value") or "")
        payload.update({k: v for k, v in updates.items() if v is not None})
        if "action" not in payload:
            for btn in form.get("buttons") or []:
                if btn.get("name") == "action" and btn.get("aria_hidden") != "true":
                    payload["action"] = str(btn.get("value") or "default")
                    break
            payload.setdefault("action", "default")
        return str(form.get("action") or ""), payload

    def _post_auth_form(self, url: str, action: str, payload: dict[str, str]) -> requests.Response:
        target = urllib.parse.urljoin(url, action or urllib.parse.urlparse(url).path)
        headers = self._auth_headers(referer=url)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return self._session.post(target, data=payload, headers=headers, timeout=self._timeout, allow_redirects=True, proxies={"http": self._proxy, "https": self._proxy} if self._proxy else None)

    def _sync_cdp_session(self, solved: Any) -> str:
        token = ""
        if isinstance(solved, dict):
            token = str(solved.get("token") or solved.get("turnstile_token") or "").strip()
            ua = str(solved.get("user_agent") or solved.get("userAgent") or "").strip()
            if ua:
                self._session.headers.update({"User-Agent": ua})
            cookies = solved.get("cookies") or {}
            if isinstance(cookies, dict):
                for name, value in cookies.items():
                    if name and value is not None:
                        self._session.cookies.set(str(name), str(value), domain=AUTH0_DOMAIN)
                        self._session.cookies.set(str(name), str(value), domain=".converge.ai")
        else:
            token = str(solved or "").strip()
        return token

    def _session_cookies_for_browser(self) -> list[dict[str, Any]]:
        cookies = []
        for cookie in self._session.cookies:
            domain = str(cookie.domain or AUTH0_DOMAIN)
            if domain.startswith("."):
                domain = domain[1:]
            cookies.append({
                "name": str(cookie.name),
                "value": str(cookie.value),
                "domain": domain,
                "path": str(cookie.path or "/"),
                "httpOnly": bool(getattr(cookie, "_rest", {}).get("HttpOnly")),
                "secure": True,
                "sameSite": "Lax",
            })
        return cookies

    def _solve_turnstile_cdp_same_state(
        self,
        page_url: str,
        sitekey: str,
        *,
        fill_selector: str = "",
        fill_value: str = "",
        label: str = "Auth0",
    ) -> str:
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Enter cdp_protocol requires Playwright for same-state Turnstile")
        if not sitekey:
            raise RuntimeError("Enter Auth0 Turnstile sitekey not found")
        from platforms.enter.browser_register import EnterBrowserRegistrar

        registrar = EnterBrowserRegistrar(
            headless=False,
            proxy=self._proxy,
            chrome_path=self._chrome_path,
            cdp_url=self._cdp_url,
            timeout=self._timeout,
            log_fn=self._log,
        )
        launch_meta = registrar._prepare_chrome()
        browser = None
        page = None
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                cookies = self._session_cookies_for_browser()
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()
                self._l(f"CDP opening fresh {label} page for Turnstile only...")
                page.goto(page_url, wait_until="domcontentloaded", timeout=self._timeout * 1000)
                if fill_selector and fill_value:
                    page.wait_for_selector(fill_selector, timeout=30_000)
                    page.locator(fill_selector).first.fill(fill_value)
                    page.wait_for_timeout(800)
                token = registrar._click_turnstile_until_token(page)
                if not token:
                    raise RuntimeError("Enter Turnstile token is empty")
                for c in context.cookies(page_url):
                    name = str(c.get("name") or "")
                    value = str(c.get("value") or "")
                    domain = str(c.get("domain") or AUTH0_DOMAIN).lstrip(".")
                    if name and value:
                        self._session.cookies.set(name, value, domain=domain)
                        self._session.cookies.set(name, value, domain=AUTH0_DOMAIN)
                self._l(f"Turnstile token obtained via same-state CDP (length={len(token)})")
                return token
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            registrar._teardown_chrome(launch_meta)

    def _extract_turnstile_sitekey(self, html: str) -> str:
        match = re.search(r"data-captcha-sitekey=['\"]([^'\"]+)['\"]", html or "", re.I)
        return match.group(1).strip() if match else ""

    def _run_auth0_protocol_flow(self, *, email: str, password: str, otp_callback: Callable[[], str], captcha_solver: Any = None) -> dict[str, Any]:
        """Run the Auth0 signup path in one CDP browser session, then switch to HTTP.

        Auth0's new Universal Login binds /u/signup/password to the browser-side
        transaction state. Opening that password URL from a separate CDP/HTTP hop
        can produce the converge-ai "Oops" page. Therefore CDP owns the complete
        registration UI path up to authorization code. Token exchange and all
        Enter API enrichment remain pure HTTP below.
        """
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Enter cdp_protocol requires Playwright")
        from platforms.enter.browser_register import EnterBrowserRegistrar

        registrar = EnterBrowserRegistrar(
            captcha=captcha_solver,
            headless=False,
            proxy=self._proxy,
            otp_callback=otp_callback,
            referrer_code=self._referrer_code,
            workspace_id=self._workspace_id,
            timeout=self._timeout,
            chrome_path=self._chrome_path,
            cdp_url=self._cdp_url,
            log_fn=self._log,
        )
        launch_meta = registrar._prepare_chrome()
        browser = None
        page = None
        auth_code = ""
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(launch_meta["cdp_url"], timeout=30_000)
                if not browser.contexts:
                    raise RuntimeError("CDP connected but no browser context found")
                context = browser.contexts[0]
                page = context.new_page()
                auth_code = registrar._run_auth_flow(page, email, password) or ""
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            registrar._teardown_chrome(launch_meta)

        if not auth_code:
            raise RuntimeError("Failed to obtain authorization code from CDP registration flow")
        self._l("got auth_code by CDP, exchanging for tokens by HTTP...")
        client = EnterClient(proxy=self._proxy, session=self._session, timeout=self._timeout, log_fn=self._log)
        tokens = client.exchange_code_for_tokens(auth_code)
        access_token = tokens.get("access_token", "")
        if not access_token:
            raise RuntimeError(f"Token exchange failed: {tokens}")
        return {
            "email": email,
            "password": password,
            "access_token": access_token,
            "refresh_token": tokens.get("refresh_token", ""),
            "id_token": tokens.get("id_token", ""),
            "expires_in": tokens.get("expires_in", 0),
            "token_type": tokens.get("token_type", ""),
        }

    def _enrich_result(self, result: dict[str, Any], access_token: str) -> None:
        client = EnterClient(proxy=self._proxy, session=self._session, timeout=self._timeout, log_fn=self._log)
        ws_id = result.get("workspace_id", self._workspace_id)

        if not result.get("workspace_id") or not result.get("balance"):
            ws_info = client.get_workspaces(access_token)
            if isinstance(ws_info, dict):
                ws_list = (ws_info.get("data") or {}).get("workspaces") or []
                if ws_list:
                    ws = ws_list[0]
                    result["workspace_id"] = ws.get("id", ws_id)
                    result["plan_type"] = ws.get("plan_type", "")
                    credits = ws.get("credits_balance", {})
                    result["balance"] = credits.get("total", 0)
                    breakdown = credits.get("breakdown") or {}
                    result["balance_bonus"] = breakdown.get("bonus", 0)
                    result["balance_daily"] = breakdown.get("daily", 0)
                    result["balance_monthly"] = breakdown.get("monthly", 0)
                    result["balance_purchase"] = breakdown.get("purchase", 0)
                    ent = ws.get("entitlement") or {}
                    result["entitlement_daily_credits"] = ent.get("daily_credits", 0)
                    result["entitlement_monthly_build"] = ent.get("monthly_build_credits", 0)
                    result["entitlement_monthly_ai"] = ent.get("monthly_ai_credits", 0)
                    result["entitlement_plan_name"] = ent.get("name", "Free")
                    result["subscription_status"] = ws.get("subscription_status", "")
                    result["enter_ai_credits_status"] = ws.get("enter_ai_credits_status", "")
            ws_id = result.get("workspace_id", ws_id)

        if self._referrer_code and not result.get("referral_claimed"):
            try:
                ref = client.claim_referral(access_token, self._referrer_code)
                result["referral_claimed"] = isinstance(ref, dict) and ref.get("code") == 0
                if result["referral_claimed"]:
                    self._l("referral claimed (+100 bonus)")
                    ws_after = client.get_workspaces(access_token)
                    if isinstance(ws_after, dict):
                        ws_a = ((ws_after.get("data") or {}).get("workspaces") or [None])[0]
                        if ws_a:
                            cb = ws_a.get("credits_balance", {})
                            result["balance"] = cb.get("total", result.get("balance", 0))
                            result["balance_bonus"] = (cb.get("breakdown") or {}).get("bonus", result.get("balance_bonus", 0))
            except Exception as exc:
                self._l(f"referral claim failed (non-fatal): {exc}")

        project_name = f"{self._project_name_prefix}-{uuid.uuid4().hex[:6]}"
        try:
            proj = client.get_or_create_project(access_token, ws_id, project_name, self._project_prompt)
            if isinstance(proj, dict):
                pdata = (proj.get("data") or {}).get("project") or {}
                result["project_id"] = pdata.get("project_id", "")
                result["project_name"] = project_name
                result["preview_url"] = pdata.get("preview_url", "")
                result["thread_id"] = pdata.get("thread_id", "")
            self._l(f"project: {result.get('project_id', 'FAILED')}")
        except Exception as exc:
            self._l(f"project create failed (non-fatal): {exc}")

        project_id = result.get("project_id", "")
        if project_id and self._enable_entercloud:
            self._bind_entercloud(client, access_token, project_id, result)

        if project_id and self._enable_ai_capability:
            self._fetch_ai_token(client, access_token, ws_id, project_id, result)

        user_info = client.get_user_info(access_token)
        if isinstance(user_info, dict):
            udata = (user_info.get("data") or {}).get("user") or {}
            result["user_id"] = udata.get("user_id", "")
            result["referral_code_self"] = udata.get("referral_code", "")

    def _bind_entercloud(self, client: EnterClient, access_token: str, project_id: str, result: dict[str, Any]) -> None:
        try:
            client.enable_entercloud(access_token, project_id)
            for _ in range(10):
                ec = client.get_entercloud_status(access_token, project_id)
                if isinstance(ec, dict) and (ec.get("data") or {}).get("enabled"):
                    ec_data = ec["data"]
                    binding = ec_data.get("binding") or {}
                    instance = ec_data.get("instance") or {}
                    result["entercloud_enabled"] = True
                    result["entercloud_setup_completed"] = binding.get("setup_completed", False)
                    result["entercloud_provider"] = instance.get("provider", "")
                    result["entercloud_cloud_ref"] = instance.get("cloud_ref", "")
                    result["entercloud_api_url"] = instance.get("api_url", "")
                    result["entercloud_anon_key"] = instance.get("anon_key", "")
                    break
                time.sleep(3.0)
            self._l(f"entercloud enabled={result.get('entercloud_enabled', False)}")
        except Exception as exc:
            self._l(f"entercloud failed (non-fatal): {exc}")

    def _fetch_ai_token(self, client: EnterClient, access_token: str, ws_id: str, project_id: str, result: dict[str, Any]) -> None:
        try:
            client.connect_ai_capability(access_token, project_id)
            for _ in range(10):
                stats = client.get_ai_capability_stats(access_token, ws_id, project_id)
                if isinstance(stats, dict) and is_success_response(stats):
                    ai_data = stats.get("data") or {}
                    result["ai_api_token"] = extract_ai_api_token(ai_data) or extract_ai_api_token(stats)
                    result["ai_connection_state"] = (
                        ai_data.get("aiConnectionState")
                        or ai_data.get("ai_connection_state")
                        or ai_data.get("connectionState")
                        or ""
                    )
                    break
                time.sleep(3.0)
            self._l(f"ai_token={'ok' if result.get('ai_api_token') else 'FAILED'}")
        except Exception as exc:
            self._l(f"ai capability failed (non-fatal): {exc}")

    def _push_to_remote(self, result: dict[str, Any]) -> None:
        if not self._enter2api_base_url:
            return
        payload = {
            "mode": "append",
            "raw": {
                "accounts": [{
                    "email": result.get("email", ""),
                    "access_token": result.get("access_token", ""),
                    "refresh_token": result.get("refresh_token", ""),
                    "workspace_id": result.get("workspace_id", ""),
                    "project_id": result.get("project_id", ""),
                    "default_project_name": result.get("project_name", ""),
                    "ai_api_token": result.get("ai_api_token", ""),
                    "ai_connection_state": result.get("ai_connection_state", ""),
                    "entercloud_enabled": result.get("entercloud_enabled", False),
                    "entercloud_setup_completed": result.get("entercloud_setup_completed", False),
                    "entercloud_provider": result.get("entercloud_provider", ""),
                    "entercloud_cloud_ref": result.get("entercloud_cloud_ref", ""),
                    "entercloud_api_url": result.get("entercloud_api_url", ""),
                    "entercloud_anon_key": result.get("entercloud_anon_key", ""),
                }]
            },
        }
        url = f"{self._enter2api_base_url}/api/ui/accounts/import"
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            rj = r.json() if r.text else {}
            added = (rj.get("result") or {}).get("added", "?")
            self._l(f"pushed to enter2api: added={added}")
        except Exception as exc:
            self._l(f"enter2api push failed (non-fatal): {exc}")

    def _save_referral_code(self, code: str) -> None:
        import os
        try:
            data = {}
            if os.path.exists(self._referral_pool_file):
                with open(self._referral_pool_file, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            if not isinstance(data, dict):
                data = {}
            codes = data.get("codes", [])
            if not isinstance(codes, list):
                codes = []
            if code not in codes:
                codes.append(code)
            data["codes"] = codes
            data["updated_at"] = _utcnow_iso()
            with open(self._referral_pool_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._l(f"saved referral code to pool: {code} (pool size={len(codes)})")
        except Exception as exc:
            self._l(f"failed to save referral code: {exc}")

    def _complete_quests(self, result: dict[str, Any], access_token: str) -> None:
        """Run post-registration quests via API for extra credits.

        Quests that can be completed via API:
        - remix (50 credits): POST /projects/{pid}/remix
        - switch_model (50): already done via browser during registration

        Quests requiring browser UI (skipped):
        - select_to_edit, component, ai_all, plan, publish_ai, join_forum, join_discord
        """
        client = EnterClient(proxy=self._proxy, session=self._session, timeout=self._timeout, log_fn=self._log)
        ws_id = str(result.get("workspace_id", self._workspace_id))
        pid = str(result.get("project_id", ""))

        if not pid:
            self._l("no project_id, skipping quests")
            return

        # Quest 1: Remix — create a remixed project copy
        try:
            remix = client.remix_project(access_token, ws_id, pid)
            if isinstance(remix, dict):
                rcode = remix.get("code", -1)
                rdata = remix.get("data") or {}
                remix_pid = ""
                if isinstance(rdata, dict):
                    remix_pid = rdata.get("project_id", "")
                if rcode == 0 and remix_pid:
                    self._l(f"remix quest ok -> {remix_pid}")
                    result["remixed_project_id"] = remix_pid
                else:
                    self._l(f"remix quest skipped (code={rcode})")
        except Exception as exc:
            self._l(f"remix quest failed (non-fatal): {exc}")

        # Log completed quests summary from classroom API
        try:
            quests = client.get_classroom_quests(access_token)
            if isinstance(quests, dict) and quests.get("code") == 0:
                qdata = quests.get("data", {}).get("quests", {})
                claimed = quests.get("data", {}).get("total_claimed_credits", 0)
                completed = []
                for category, items in qdata.items():
                    if isinstance(items, list):
                        for q in items:
                            if isinstance(q, dict) and q.get("status") == "completed":
                                completed.append(f"{q.get('quest_id')}(+{q.get('reward_amount', 0)})")
                self._l(f"quests: total_claimed={claimed}, completed={completed}")
                result["quest_credits_claimed"] = claimed
                result["quests_completed"] = completed
        except Exception as exc:
            self._l(f"quests status check failed (non-fatal): {exc}")
