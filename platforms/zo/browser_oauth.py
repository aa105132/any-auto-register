"""Zo Computer Google OAuth + 后续 HTTP/CDP 混合链路。"""
from __future__ import annotations

import json
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.google_oauth import drive_google_oauth
from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page
from platforms.zo.core import (
    AUTH_BASE,
    CLIENT_ID,
    DEFAULT_COUPON_CODE,
    SITE_URL,
    ZoClient,
    find_api_key,
    mask_card_info,
    resolve_card_info,
)


@contextmanager
def isolated_oauth_browser_options(
    *,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    allow_shared_cdp: bool = False,
):
    """为 Zo 并发 OAuth 分配隔离 profile，避免多个任务撞同一个 CDP 端口。"""
    if chrome_user_data_dir:
        yield {"chrome_user_data_dir": chrome_user_data_dir, "chrome_cdp_url": chrome_cdp_url if allow_shared_cdp else ""}
        return
    if chrome_cdp_url and allow_shared_cdp:
        yield {"chrome_user_data_dir": "", "chrome_cdp_url": chrome_cdp_url}
        return
    profile_root = Path(tempfile.mkdtemp(prefix="zo_oauth_"))
    try:
        yield {"chrome_user_data_dir": str(profile_root), "chrome_cdp_url": ""}
    finally:
        try:
            import shutil

            shutil.rmtree(profile_root, ignore_errors=True)
        except Exception:
            pass


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    try:
        return browser.cookie_dict(domain_substrings=("zo.computer",))
    except Exception:
        return {}


def _has_auth_cookie(cookies: dict[str, str]) -> bool:
    return bool(cookies.get("access_token") or cookies.get("refresh_token"))


def _has_oauth_callback_url(browser: OAuthBrowser) -> bool:
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        if "auth.zo.computer" in url and ("/callback" in url or "code=" in url):
            return True
        if "zo.computer" in url and "code=" in url and "state=" in url:
            return True
    return False


def _extract_oauth_callback(browser: OAuthBrowser) -> dict[str, str]:
    """从浏览器回跳 URL 提取 Zo OAuth code/state。"""
    for page in browser.pages():
        if page.is_closed():
            continue
        raw_url = str(page.url or "")
        if "zo.computer" not in raw_url or "code=" not in raw_url:
            continue
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query)
        code = str((query.get("code") or [""])[0] or "").strip()
        if not code:
            continue
        redirect_uri = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme and parsed.netloc else SITE_URL
        if redirect_uri.endswith("/") and not SITE_URL.endswith("/"):
            redirect_uri = redirect_uri.rstrip("/")
        return {
            "code": code,
            "state": str((query.get("state") or [""])[0] or ""),
            "url": raw_url,
            "redirect_uri": redirect_uri or SITE_URL,
        }
    return {}


def _exchange_callback_code(client: ZoClient, browser: OAuthBrowser, *, log_fn=print) -> dict[str, Any]:
    callback = _extract_oauth_callback(browser)
    if not callback.get("code"):
        return {}
    log_fn(f"[Zo] 捕获 OAuth code 回跳，开始协议换 token: {callback.get('redirect_uri')}")
    try:
        result = client.exchange_code(code=callback["code"], redirect_uri=callback.get("redirect_uri") or SITE_URL)
    except Exception as first_exc:
        # Zo authorize 里使用的是 SITE_URL；浏览器地址可能带 /。两种 redirect_uri 都试一次。
        alt_redirect = SITE_URL if callback.get("redirect_uri") != SITE_URL else f"{SITE_URL}/"
        log_fn(f"[Zo] code exchange 首次失败，尝试 alternate redirect_uri: {first_exc}")
        result = client.exchange_code(code=callback["code"], redirect_uri=alt_redirect)
    result["callback"] = {"state": callback.get("state"), "url": callback.get("url"), "redirect_uri": callback.get("redirect_uri")}
    return result


def _is_google_account_chooser_page(page) -> bool:
    """只在真正的 Google account chooser 上执行账号行点击。

    Consent/Continue 页面可能仍然在 accounts.google.com 域名下，也可能保留
    accountchooser 相关 continue 参数；如果 Zo 兜底继续点账号行，会把流程带回
    选择账号页或原地循环。
    """
    if page.is_closed() or "accounts.google.com" not in str(page.url or ""):
        return False
    url = str(page.url or "").lower()
    if "consent" in url or "oauth/consent" in url or "gaplustos" in url or "speedbump" in url:
        return False
    try:
        body = str(page.locator("body").inner_text(timeout=800) or "")
    except Exception:
        body = ""
    lower = body.lower()
    consent = "you're signing back in" in lower or "you’re signing back in" in lower or "google will allow" in lower or "wants to access" in lower
    if consent:
        return False
    if "accountchooser" in url:
        return True
    if not lower:
        return False
    chooser = "choose an account" in lower or "选择账号" in body or "请选择账号" in body
    return chooser


def _click_google_account_for_zo(browser: OAuthBrowser, *, email_hint: str = "", log_fn=print) -> bool:
    target = str(email_hint or "").strip().lower()
    clicked_any = False
    for page in browser.pages():
        if not _is_google_account_chooser_page(page):
            continue
        try:
            import re

            if target:
                for selector in ("[data-identifier]", "[data-email]", "div[role='link']", "li"):
                    try:
                        locator = page.locator(selector).filter(has_text=re.compile(re.escape(target), re.IGNORECASE)).first
                        locator.wait_for(state="visible", timeout=1000)
                        try:
                            locator.scroll_into_view_if_needed(timeout=800)
                        except Exception:
                            pass
                        box = None
                        try:
                            box = locator.bounding_box(timeout=800)
                        except Exception:
                            box = None
                        if box:
                            page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        else:
                            locator.click(timeout=1500, force=True)
                        clicked_any = True
                        log_fn(f"[Zo] 精确点击 Google 账号行: {target}")
                        time.sleep(2)
                        return True
                    except Exception:
                        continue
            clicked = page.evaluate(
                """
                ({target}) => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                  };
                  const textOf = (el) => (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
                  const nodes = [...document.querySelectorAll('[data-email], [data-identifier], div[role=link], div[role=button], li, button, a')]
                    .filter(el => visible(el) && !['HTML','BODY','MAIN'].includes(el.tagName));
                  const score = (el) => {
                    const r = el.getBoundingClientRect();
                    const role = String(el.getAttribute('role') || '').toLowerCase();
                    let out = 0;
                    if (el.getAttribute('data-email') || el.getAttribute('data-identifier')) out += 100;
                    if (role === 'link' || role === 'button') out += 30;
                    if (el.tagName === 'LI') out += 10;
                    if (getComputedStyle(el).cursor === 'pointer') out += 10;
                    out -= Math.min(50, (r.width * r.height) / 10000);
                    return out;
                  };
                  const candidates = target
                    ? nodes.filter(el => String(el.getAttribute('data-email') || el.getAttribute('data-identifier') || textOf(el)).toLowerCase().includes(target))
                    : nodes.filter(el => /@/.test(textOf(el)) || el.getAttribute('data-email') || el.getAttribute('data-identifier'));
                  const pick = candidates.sort((a, b) => score(b) - score(a))[0];
                  if (!pick) return '';
                  pick.scrollIntoView({block: 'center', inline: 'center'});
                  const box = pick.getBoundingClientRect();
                  const x = Math.max(2, Math.min(innerWidth - 2, box.left + box.width / 2));
                  const y = Math.max(2, Math.min(innerHeight - 2, box.top + box.height / 2));
                  const hit = document.elementFromPoint(x, y) || pick;
                  for (const type of ['pointerover','mouseover','pointerdown','mousedown','pointerup','mouseup','click']) {
                    hit.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                  }
                  return textOf(pick) || pick.getAttribute('data-email') || pick.getAttribute('data-identifier') || 'account';
                }
                """,
                {"target": target},
            )
            if clicked:
                clicked_any = True
                log_fn(f"[Zo] 点击 Google 账号选择器: {clicked}")
                time.sleep(2)
        except Exception:
            continue
    return clicked_any


def _extract_logged_email(page, settings: dict[str, Any] | None = None) -> str:
    data = dict(settings or {})
    for key in ("email", "user_email"):
        if data.get(key):
            return str(data.get(key) or "").strip()
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    if user.get("email"):
        return str(user.get("email") or "").strip()
    try:
        return str(page.evaluate("() => document.body.innerText.match(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}/i)?.[0] || ''") or "").strip()
    except Exception:
        return ""


def _start_google_oauth_protocol(page, *, redirect_uri: str = SITE_URL, log_fn=print) -> bool:
    """用 Zo OpenAuth 协议发起 Google OAuth，失败再页面点击兜底。"""
    try:
        import secrets
        from urllib.parse import urlencode

        query = {
            "client_id": CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": f"auto-{secrets.token_hex(8)}",
            "provider": "google",
        }
        url = f"{AUTH_BASE}/authorize?{urlencode(query)}"
        log_fn(f"[Zo] OpenAuth Google OAuth URL: {url[:180]}")
        page.goto(url, wait_until="commit", timeout=90000)
        return True
    except Exception as exc:
        log_fn(f"[Zo] OpenAuth Google OAuth 发起异常: {exc!r}")
        return False


def _import_browser_auth(client: ZoClient, browser: OAuthBrowser) -> dict[str, str]:
    cookies = _cookie_map(browser)
    client.import_cookies(cookies)
    return cookies


def _sync_client_auth_to_browser(client: ZoClient, browser: OAuthBrowser, *, log_fn=print) -> dict[str, str]:
    """把协议换到的 Zo token 写回浏览器，保证 CDP 兜底 fetch 仍是登录态。"""
    cookies = dict(client.cookies)
    wanted = []
    for name in ("access_token", "refresh_token"):
        value = str(cookies.get(name) or "").strip()
        if not value:
            continue
        wanted.append({
            "name": name,
            "value": value,
            "domain": ".zo.computer",
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "Lax",
        })
    if wanted:
        try:
            browser.context.add_cookies(wanted)
        except Exception as exc:
            log_fn(f"[Zo] 同步 token 到浏览器 cookie 失败: {exc!r}")
    return _cookie_map(browser)


def _redeem_coupon_http(cookies: dict[str, str], *, code: str, proxy: str | None = None, log_fn=print) -> dict[str, Any]:
    client = ZoClient(proxy=proxy, log_fn=log_fn)
    client.import_cookies(cookies)
    return client.redeem_coupon(code=code)


def _bind_card_in_browser(page, *, card: dict[str, Any], log_fn=print) -> dict[str, Any]:
    """浏览器同源 fetch 绑卡兜底；不把完整卡号/CVV 写入返回值。"""
    masked = mask_card_info(card)
    try:
        result = page.evaluate(
            r"""
            async ({card}) => {
              const out = {ok: false, attempts: []};
              const headers = {'Content-Type': 'application/json'};
              if (location.hostname.endsWith('.zo.computer') && !['www','api','auth','signup'].includes(location.hostname.split('.')[0])) {
                headers['X-Zo-Workspace-Origin'] = location.origin;
              }
              const payload = {
                payment_method: {
                  type: 'card',
                  card: {
                    number: card.number,
                    exp_month: card.exp_month,
                    exp_year: card.exp_year,
                    cvc: card.cvv,
                  },
                  billing_details: {
                    name: card.name || 'Zo User',
                    address: {
                      country: card.country,
                      line1: card.address,
                      city: card.city,
                      postal_code: card.postal_code,
                      state: card.state,
                    },
                  },
                },
              };
              const endpoints = [
                'https://api.zo.computer/billing/payment-methods',
                'https://api.zo.computer/billing/payment',
                'https://api.zo.computer/payment-methods',
              ];
              const readJson = async (response) => {
                const text = await response.text();
                try { return JSON.parse(text); } catch (e) { return {raw: text.slice(0, 1200)}; }
              };
              for (const endpoint of endpoints) {
                try {
                  const response = await fetch(endpoint, {
                    method: 'POST', credentials: 'include', headers, body: JSON.stringify(payload),
                  });
                  const data = await readJson(response);
                  out.attempts.push({endpoint, status: response.status, ok: response.ok, data});
                  if (response.ok) return {ok: true, endpoint, status: response.status, data, attempts: out.attempts};
                } catch (e) {
                  out.attempts.push({endpoint, ok: false, error: String(e)});
                }
              }
              return out;
            }
            """,
            {"card": card},
        ) or {}
        return {"ok": bool(result.get("ok")), "card": masked, "raw": result, "attempts": result.get("attempts") or []}
    except Exception as exc:
        log_fn(f"[Zo] browser bind card failed: {exc!r}")
        return {"ok": False, "error": repr(exc), "card": masked}


def _write_oauth_timeout_snapshot(browser: OAuthBrowser, *, email_hint: str = "", log_fn=print) -> dict[str, Any]:
    """保存 OAuth 超时现场，定位 token 在 cookie/localStorage/URL code 中的位置。"""
    snapshot: dict[str, Any] = {"email_hint": email_hint, "pages": [], "cookies": []}
    try:
        snapshot["cookies"] = [
            {"name": c.get("name"), "domain": c.get("domain"), "path": c.get("path"), "expires": c.get("expires")}
            for c in browser.cookies()
            if "zo.computer" in str(c.get("domain") or "") or "auth.zo.computer" in str(c.get("domain") or "")
        ]
    except Exception as exc:
        snapshot["cookies_error"] = repr(exc)
    for page in browser.pages():
        if page.is_closed():
            continue
        item: dict[str, Any] = {"url": str(page.url or "")}
        try:
            item["body"] = str(page.locator("body").inner_text(timeout=1200) or "")[:2000]
        except Exception as exc:
            item["body_error"] = repr(exc)
        try:
            item["storage"] = page.evaluate(
                """
                () => ({
                  localStorage: Object.fromEntries(Object.keys(localStorage || {}).map(k => [k, String(localStorage.getItem(k) || '').slice(0, 200)])),
                  sessionStorage: Object.fromEntries(Object.keys(sessionStorage || {}).map(k => [k, String(sessionStorage.getItem(k) || '').slice(0, 200)])),
                  documentCookieNames: String(document.cookie || '').split(';').map(x => x.trim().split('=')[0]).filter(Boolean),
                })
                """
            )
        except Exception as exc:
            item["storage_error"] = repr(exc)
        snapshot["pages"].append(item)
    try:
        out_path = Path(__file__).resolve().parents[2] / "output" / "zo_oauth_timeout_snapshot.json"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        log_fn(f"[Zo] OAuth 超时快照已保存: {out_path}")
        snapshot["path"] = str(out_path)
    except Exception as exc:
        log_fn(f"[Zo] OAuth 超时快照保存失败: {exc!r}")
    return snapshot


def _create_access_token_in_browser(page, *, name: str, log_fn=print) -> dict[str, Any]:
    """浏览器同源 fetch 创建 Access Token 兜底。"""
    try:
        result = page.evaluate(
            r"""
            async ({name}) => {
              const out = {ok: false, attempts: []};
              const headers = {'Content-Type': 'application/json'};
              if (location.hostname.endsWith('.zo.computer') && !['www','api','auth','signup'].includes(location.hostname.split('.')[0])) {
                headers['X-Zo-Workspace-Origin'] = location.origin;
              }
              const readJson = async (response) => {
                const text = await response.text();
                try { return JSON.parse(text); } catch (e) { return {raw: text.slice(0, 1600)}; }
              };
              const endpoints = [
                'https://api.zo.computer/api-keys/',
                'https://api.zo.computer/access-tokens',
                'https://api.zo.computer/tokens',
                'https://api.zo.computer/settings/access-tokens',
                'https://api.zo.computer/user-services/',
              ];
              for (const endpoint of endpoints) {
                try {
                  const listed = await fetch(endpoint, {credentials: 'include'});
                  const listedData = await readJson(listed);
                  out.attempts.push({endpoint, method: 'GET', status: listed.status, ok: listed.ok, data: listedData});
                  for (const payload of [{name}, {label: name}, {description: name}, {}]) {
                    const response = await fetch(endpoint, {
                      method: 'POST', credentials: 'include', headers, body: JSON.stringify(payload),
                    });
                    const data = await readJson(response);
                    out.attempts.push({endpoint, method: 'POST', status: response.status, ok: response.ok, payload, data});
                    if (response.ok) return {ok: true, endpoint, status: response.status, data, attempts: out.attempts};
                    if ([401,403,404,405].includes(response.status)) break;
                  }
                } catch (e) {
                  out.attempts.push({endpoint, ok: false, error: String(e)});
                }
              }
              return out;
            }
            """,
            {"name": name},
        ) or {}
        api_key = find_api_key(result)
        return {"ok": bool(api_key), "api_key": api_key, "api_key_info": result.get("data") or result, "raw": result, "attempts": result.get("attempts") or []}
    except Exception as exc:
        log_fn(f"[Zo] browser create access token failed: {exc!r}")
        return {"ok": False, "error": repr(exc)}


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    timeout: int = 300,
    log_fn=print,
    headless: bool = False,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    google_password: str = "",
    allow_shared_cdp: bool = False,
    extra: dict | None = None,
) -> dict:
    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("Zo 当前只支持 Google OAuth 自动化")
    extra = dict(extra or {})

    with isolated_oauth_browser_options(
        chrome_user_data_dir=chrome_user_data_dir,
        chrome_cdp_url=chrome_cdp_url,
        allow_shared_cdp=allow_shared_cdp,
    ) as browser_options, OAuthBrowser(
        proxy=proxy,
        headless=headless,
        chrome_user_data_dir=browser_options["chrome_user_data_dir"],
        chrome_cdp_url=browser_options["chrome_cdp_url"],
        log_fn=log_fn,
    ) as browser:
        page = browser.new_page()
        try:
            page.goto(SITE_URL, wait_until="domcontentloaded", timeout=90000)
            time.sleep(1)
        except Exception as exc:
            log_fn(f"[Zo] Zo 首页打开超时，直接进入 OpenAuth: {exc!r}")
        if not _start_google_oauth_protocol(page, log_fn=log_fn):
            try_click_provider_on_page(page, "google")

        google_result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 180),
            log_fn=log_fn,
            stop_when=lambda b: _has_auth_cookie(_cookie_map(b)) or _has_oauth_callback_url(b),
        )
        if getattr(google_result, "blocked_on_password", False):
            raise RuntimeError(f"Zo Google OAuth 未完成: {google_result.last_url} :: {google_result.last_body[:300]}")
        account_deadline = time.time() + 45
        while time.time() < account_deadline and not (_has_auth_cookie(_cookie_map(browser)) or _has_oauth_callback_url(browser)):
            if not _click_google_account_for_zo(browser, email_hint=email_hint, log_fn=log_fn):
                time.sleep(1)

        client = ZoClient(proxy=proxy, log_fn=log_fn)
        code_exchange_result: dict[str, Any] = {}
        deadline = time.time() + max(30, min(timeout, 180))
        dashboard_page = page
        cookies: dict[str, str] = {}
        while time.time() < deadline:
            for item in browser.pages():
                if item.is_closed():
                    continue
                if "zo.computer" in (item.url or "") and "auth.zo.computer" not in (item.url or ""):
                    dashboard_page = item
            cookies = _cookie_map(browser)
            if _has_auth_cookie(cookies):
                client.import_cookies(cookies)
                break
            if not code_exchange_result and _has_oauth_callback_url(browser):
                code_exchange_result = _exchange_callback_code(client, browser, log_fn=log_fn)
                if code_exchange_result.get("ok"):
                    cookies = dict(client.cookies)
                    break
            time.sleep(1)
        else:
            snapshot = _write_oauth_timeout_snapshot(browser, email_hint=email_hint, log_fn=log_fn)
            raise RuntimeError(f"Zo OAuth 登录超时，未拿到 access_token/refresh_token cookie/code token; snapshot={snapshot.get('path', '')}")

        if not code_exchange_result:
            cookies = _import_browser_auth(client, browser)
        else:
            log_fn("[Zo] OAuth code exchange 成功，继续 HTTP 后登录步骤")
            cookies = _sync_client_auth_to_browser(client, browser, log_fn=log_fn)

        coupon_code = str(extra.get("zo_coupon_code") or DEFAULT_COUPON_CODE).strip() or DEFAULT_COUPON_CODE
        workspace_result = client.ensure_workspace(
            handle=str(extra.get("zo_workspace_handle") or ""),
            promo_code=coupon_code,
            signup_code=str(extra.get("zo_signup_code") or ""),
        )
        if workspace_result.get("source") == "created":
            log_fn(f"[Zo] workspace 创建完成: {workspace_result.get('workspace', {}).get('origin', '')}")
        else:
            log_fn(f"[Zo] 复用已有 workspace: {workspace_result.get('workspace', {}).get('origin', '')}")

        try:
            dashboard_page.goto(str(client.workspace_origin or SITE_URL), wait_until="domcontentloaded", timeout=90000)
            time.sleep(2)
        except Exception:
            pass

        skip_onboarding_result = client.skip_onboarding()
        skip_phone_result = client.skip_phone()

        coupon_result = client.redeem_coupon(code=coupon_code)
        credit_result = client.check_credits(min_amount=float(extra.get("zo_min_credit", 100.0) or 100.0))

        card = resolve_card_info(extra)
        try:
            card_binding_result = client.bind_card(card=card, require_confirmed=True)
        except Exception as http_exc:
            log_fn(f"[Zo] HTTP 绑卡失败，改用浏览器同源 fetch: {http_exc}")
            card_binding_result = _bind_card_in_browser(dashboard_page, card=card, log_fn=log_fn)
            if not card_binding_result.get("ok"):
                raise RuntimeError(f"Zo 绑卡失败: {card_binding_result}") from http_exc

        token_name = str(extra.get("zo_access_token_name") or "auto-register").strip() or "auto-register"
        try:
            key_create_result = client.create_access_token(name=token_name)
        except Exception as http_exc:
            log_fn(f"[Zo] HTTP 创建 Access Token 失败，改用浏览器同源 fetch: {http_exc}")
            key_create_result = _create_access_token_in_browser(dashboard_page, name=token_name, log_fn=log_fn)
            if not key_create_result.get("api_key"):
                raise RuntimeError(f"Zo 创建 Access Token 失败: {key_create_result}") from http_exc

        api_key = str(key_create_result.get("api_key") or "").strip()
        api_verification = client.verify_api_key(api_key)
        settings = client.get_settings()
        actual_email = _extract_logged_email(dashboard_page, settings.get("data") if isinstance(settings, dict) else {})

        pool_card_id = str(card.get("_pool_id") or "").strip()
        if pool_card_id and card_binding_result.get("ok"):
            try:
                from core.credit_card_pool import CreditCardPool
                CreditCardPool(str(card.get("_pool_path") or "")).mark_used(pool_card_id, platform="zo", account_email=actual_email or email_hint)
            except Exception as mark_exc:
                log_fn(f"[Zo] 信用卡池使用记录回写失败: {mark_exc!r}")

    return {
        "email": finalize_oauth_email(actual_email, email_hint, "Zo"),
        "api_key": api_key,
        "api_key_info": key_create_result.get("api_key_info") or {},
        "api_verification": api_verification,
        "key_create_result": key_create_result,
        "onboarding_result": skip_onboarding_result,
        "phone_result": skip_phone_result,
        "workspace_result": workspace_result,
        "coupon_result": coupon_result,
        "credit_result": credit_result,
        "card_binding_result": card_binding_result,
        "settings": settings.get("data") or {},
        "account_info": settings.get("data") or {},
        "oauth_code_exchange": code_exchange_result,
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
    }
