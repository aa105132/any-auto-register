from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any
from urllib.parse import urlparse

from core.oauth_browser import OAUTH_PROVIDER_LABELS, OAuthBrowser, finalize_oauth_email

BASE_URL = "https://gettoken.dev"
PORTAL_ORIGIN = "https://pay.imgto.link"
API_KEY_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|gt-[A-Za-z0-9_-]{16,}|[A-Za-z0-9_-]{32,})")

# Google ?????????/?? Workspace ????ToS ? OAuth consent?
# ???????? Unicode ??????? ??????????????
GOOGLE_TOS_LABELS = (
    "我同意", "我明白", "我理解", "我了解", "接受", "同意", "继续", "下一步",
    "I understand", "I agree", "I accept", "Accept", "Agree", "Continue", "Next",
)
GOOGLE_CONSENT_LABELS = (
    "继续", "允许", "下一步", "Continue", "Allow", "Next",
)
GOOGLE_TOS_TEXT_HINTS = (
    "欢迎使用您的新帐号", "欢迎使用您的新账号", "欢迎使用", "Google Workspace",
    "隐私权和条款", "服务条款", "Terms of Service", "Privacy and Terms",
    "Welcome to your new account", "Google Terms",
)
GOOGLE_CONSENT_TEXT_HINTS = (
    "使用 Google 账号登录", "使用 Google 帐号登录", "使用 Google 账户登录",
    "登录 gettoken", "继续使用", "允许", "选择您要允许", "Sign in with Google",
    "Continue to", "wants access", "Allow", "GetToken",
)

def _solve_image_with_yescaptcha(image_bytes: bytes, *, log_fn) -> str:
    try:
        from core.config_store import config_store
        import requests, time
        client_key = str(config_store.get('yescaptcha_key', '') or '').strip()
        api_base = str(config_store.get('yescaptcha_api_url', 'https://api.yescaptcha.com') or 'https://api.yescaptcha.com').rstrip('/')
        if not client_key:
            return ''
        body = base64.b64encode(image_bytes).decode()
        payload = {
            'clientKey': client_key,
            'task': {
                'type': 'ImageToTextTask',
                'body': body,
                'phrase': False,
                'case': False,
                'numeric': 0,
                'math': False,
                'minLength': 4,
                'maxLength': 12,
                'comment': 'google identifier captcha',
            },
        }
        r = requests.post(f'{api_base}/createTask', json=payload, timeout=60, verify=False)
        data = r.json()
        task_id = data.get('taskId')
        if not task_id:
            log_fn(f"[GetToken] YesCaptcha createTask failed: {data}")
            return ''
        for _ in range(24):
            time.sleep(3)
            rr = requests.post(f'{api_base}/getTaskResult', json={'clientKey': client_key, 'taskId': task_id}, timeout=60, verify=False)
            d = rr.json()
            if d.get('status') == 'ready':
                answer = str((d.get('solution') or {}).get('text') or '').strip()
                if answer:
                    log_fn(f"[GetToken] YesCaptcha image solved: {answer}")
                    return answer
                return ''
            if d.get('errorId', 0) != 0:
                log_fn(f"[GetToken] YesCaptcha error: {d}")
                return ''
    except Exception as exc:
        log_fn(f"[GetToken] YesCaptcha image solve failed: {exc}")
    return ''


def _handle_google_identifier_challenge(page, *, log_fn) -> bool:
    try:
        challenge = page.locator('input#ca:visible, input[name="ca"]:visible').first
        image = page.locator('#captchaimg').first
        if challenge.count() == 0 or image.count() == 0:
            return False
        image_bytes = image.screenshot(timeout=15000)
        answer = _solve_image_with_yescaptcha(image_bytes, log_fn=log_fn)
        if not answer:
            return False
        challenge.fill(answer, timeout=3000)
        _submit_google_step(page, log_fn=log_fn, step_name='challenge')
        return True
    except Exception as exc:
        log_fn(f"[GetToken] identifier challenge handling failed: {exc}")
        return False


def _submit_google_step(page, *, log_fn, step_name: str) -> None:
    labels = ["\u4e0b\u4e00\u6b65", "Next", "\u7ee7\u7eed", "Continue"]
    try:
        active = page.locator(':focus').first
        if active.count() > 0:
            active.press('Enter', timeout=1500)
        else:
            page.keyboard.press('Enter')
    except Exception:
        pass
    try:
        page.wait_for_timeout(500)
    except Exception:
        pass
    for label in labels:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)).first
            if btn.count() > 0:
                btn.click(timeout=2000, force=True)
                log_fn(f"[GetToken] submitted Google {step_name} via role button: {label}")
                return
        except Exception:
            pass
    try:
        clicked = page.evaluate(
            """
            ({labels}) => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const textOf = (el) => (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim();
              const nodes = [...document.querySelectorAll('button,[role="button"],div[role="button"],span[role="button"],input[type="submit"],input[type="button"],div,span')]
                .filter(visible)
                .map(el => ({el, text: textOf(el)}))
                .filter(x => x.text && x.text.length <= 40);
              for (const label of labels) {
                const hit = nodes.find(x => x.text === label) || nodes.find(x => x.text.includes(label));
                if (!hit) continue;
                hit.el.scrollIntoView({block:'center', inline:'center'});
                const r = hit.el.getBoundingClientRect();
                const x = r.left + r.width / 2;
                const y = r.top + r.height / 2;
                const t = document.elementFromPoint(x, y) || hit.el;
                for (const type of ['mouseover','mousedown','mouseup','click']) {
                  t.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                }
                return hit.text;
              }
              return '';
            }
            """,
            {"labels": labels},
        )
        if clicked:
            log_fn(f"[GetToken] submitted Google {step_name} via DOM text: {clicked}")
            return
    except Exception:
        pass



def _url_host(url: str) -> str:
    try:
        return urlparse(url or "").hostname or ""
    except Exception:
        return ""


def _is_gettoken_url(url: str) -> bool:
    return _url_host(url).endswith("gettoken.dev")


def _is_portal_url(url: str) -> bool:
    return _url_host(url).endswith("pay.imgto.link")


def _redact(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return value[:3] + "..."
    return value[:8] + "..." + value[-4:]


def _extract_api_key_from_text(text: str) -> str:
    for match in API_KEY_RE.finditer(text or ""):
        token = match.group(0)
        lowered = token.lower()
        if any(skip in lowered for skip in ("chunk", "static", "webpack", "turbopack")):
            continue
        return token
    return ""


def _collect_json_api_key(payload: Any) -> tuple[str, dict]:
    if isinstance(payload, dict):
        direct = payload.get("apiKey") or payload.get("api_key") or payload.get("key") or payload.get("token")
        if isinstance(direct, str) and direct.strip():
            return direct.strip(), payload
        for key in ("apiKeys", "api_keys", "items", "list", "data", "result"):
            found, info = _collect_json_api_key(payload.get(key))
            if found:
                return found, info
        for value in payload.values():
            found, info = _collect_json_api_key(value)
            if found:
                return found, info
    if isinstance(payload, list):
        for item in payload:
            found, info = _collect_json_api_key(item)
            if found:
                return found, info
    return "", {}




def _same_email(actual: str, expected: str) -> bool:
    actual_norm = (actual or "").strip().lower()
    expected_norm = (expected or "").strip().lower()
    return bool(actual_norm and expected_norm and actual_norm == expected_norm)


def _clear_gettoken_session(browser: OAuthBrowser, *, log_fn) -> None:
    """??? gettoken.dev ????????????????????"""
    for page in list(browser.pages()):
        try:
            if _is_gettoken_url(page.url or ""):
                page.evaluate("""
                () => {
                  try { localStorage.clear(); } catch (_) {}
                  try { sessionStorage.clear(); } catch (_) {}
                }
                """)
        except Exception:
            pass
    try:
        browser.context.clear_cookies(domain="gettoken.dev")
    except TypeError:
        try:
            browser.context.clear_cookies()
        except Exception:
            pass
    except Exception:
        pass
    log_fn("[GetToken] ??? gettoken.dev ? session?????? Google ?? OAuth")


def _click_text_like(page, hints: list[str]) -> bool:
    try:
        return bool(page.evaluate(
            """
            (hints) => {
              const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]'));
              for (const node of nodes) {
                const text = [node.innerText, node.textContent, node.value, node.getAttribute('aria-label'), node.getAttribute('title'), node.getAttribute('href')].filter(Boolean).join(' ').toLowerCase();
                if (hints.some(h => text.includes(String(h).toLowerCase()))) {
                  node.click();
                  return true;
                }
              }
              return false;
            }
            """,
            hints,
        ))
    except Exception:
        return False


def _click_oauth_in_iframes(browser: OAuthBrowser, provider: str, *, timeout: int, log_fn) -> bool:
    """在页面所有 iframe 内搜索 OAuth provider 按钮并点击（Playwright Locator API）。"""
    from core.oauth_browser import oauth_provider_label

    provider_label = oauth_provider_label(provider)
    label_lower = provider_label.lower()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for page in browser.pages():
            try:
                for iframe_el in page.locator("iframe").all():
                    try:
                        content = iframe_el.content_frame()
                        if not content:
                            continue
                        btn = content.get_by_role("button", name=re.compile(provider_label, re.IGNORECASE))
                        if btn.count() > 0:
                            btn.first.click()
                            log_fn(f"[GetToken] iframe 内点击了 {provider_label} 按钮")
                            return True
                    except Exception:
                        continue
            except Exception:
                pass
        time.sleep(1)
    return False



def _click_google_prompt_button(page, labels: list[str], *, log_fn, label_name: str) -> str:
    """Click a visible Google prompt button. Prefer JS Unicode path to avoid Windows selector mojibake."""
    try:
        result = page.evaluate(
            """
            ({labels}) => {
              const denyRe = /^(Cancel|Back|No|Not now|\u53d6\u6d88|\u8fd4\u56de|\u62d2\u7edd|\u6682\u4e0d)$/i;
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const textOf = (el) => (el.value || el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
              const clickEl = (el) => {
                el.scrollIntoView({block:'center', inline:'center'});
                const r = el.getBoundingClientRect();
                const x = Math.max(2, Math.min(innerWidth - 2, r.left + r.width / 2));
                const y = Math.max(2, Math.min(innerHeight - 2, r.top + r.height / 2));
                const target = document.elementFromPoint(x, y) || el;
                for (const type of ['mouseover','mousedown','mouseup','click']) {
                  target.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                }
              };
              // Workspace welcome/TOS is a long page; the submit button only becomes easy to hit near bottom.
              window.scrollTo(0, document.body.scrollHeight);
              const buttons = [...document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]')]
                .filter(visible)
                .map(el => ({el, r: el.getBoundingClientRect(), text: textOf(el)}))
                .filter(x => x.r.width > 40 && x.r.height > 16 && x.r.width < innerWidth * 0.95 && x.r.height < 180 && !denyRe.test(x.text));
              for (const label of labels) {
                const exact = buttons.find(x => x.text === label);
                if (exact) { clickEl(exact.el); return exact.text || label; }
                const partial = buttons.find(x => x.text.includes(label));
                if (partial) { clickEl(partial.el); return partial.text || label; }
              }
              const skipBottomWords = ['privacy', 'terms', '\u9690\u79c1', '\u6761\u6b3e'];
              const bottomRight = buttons
                .filter(x => x.r.top > innerHeight * 0.35 && !skipBottomWords.some(w => x.text.toLowerCase().includes(w)))
                .sort((a,b) => (b.r.left - a.r.left) || (b.r.top - a.r.top))[0];
              if (bottomRight) { clickEl(bottomRight.el); return bottomRight.text || 'bottom-right'; }
              return '';
            }
            """,
            {"labels": labels},
        )
        if result:
            return str(result)
    except Exception as exc:
        # Clicking often triggers navigation and destroys the execution context; treat that as likely progress.
        msg = str(exc)
        if "Execution context was destroyed" in msg or "Target closed" in msg or "Navigation" in msg:
            return "navigation-after-click"
        log_fn(f"[GetToken] Google {label_name} JS click failed: {exc}")

    # Fallback for English labels only; avoids Chinese selector encoding issues on Windows consoles.
    for label in labels:
        if not label.isascii():
            continue
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)).first
            if btn.count() > 0:
                btn.click(timeout=5000, force=True)
                return label
        except Exception:
            pass
    return ""


def _handle_google_tos(browser: OAuthBrowser, *, timeout: int, log_fn) -> bool:
    """Handle Google ToS/speedbump pages. This does not assume consent is finished."""
    labels = list(GOOGLE_TOS_LABELS)
    deadline = time.time() + min(timeout, 90)
    while time.time() < deadline:
        for page in browser.pages():
            url = page.url or ""
            if "accounts.google.com" not in url or ("gaplustos" not in url and "speedbump" not in url):
                continue
            clicked = _click_google_prompt_button(page, labels, log_fn=log_fn, label_name="ToS")
            if clicked:
                log_fn(f"[GetToken] clicked Google ToS: {clicked}")
                time.sleep(3)
                return True
        time.sleep(1)
    return False


def _handle_oauth_consent(browser: OAuthBrowser, *, timeout: int, log_fn) -> bool:
    """Handle real Google OAuth consent/continue pages. Do not consume Workspace ToS."""
    labels = list(GOOGLE_CONSENT_LABELS)
    deadline = time.time() + min(timeout, 90)
    while time.time() < deadline:
        for page in browser.pages():
            url = page.url or ""
            if "accounts.google.com" not in url:
                continue
            if "gaplustos" in url or "speedbump" in url:
                continue
            try:
                body_text = page.locator("body").inner_text(timeout=1000)
            except Exception:
                body_text = ""
            body_norm = body_text or ""
            if not any(hint in body_norm for hint in GOOGLE_CONSENT_TEXT_HINTS + GOOGLE_CONSENT_LABELS):
                continue
            clicked = _click_google_prompt_button(page, labels, log_fn=log_fn, label_name="consent")
            if clicked:
                log_fn(f"[GetToken] clicked Google consent: {clicked}")
                time.sleep(4)
                return True
        time.sleep(1)
    return False


def _drain_google_post_login_prompts(browser: OAuthBrowser, *, timeout: int, log_fn) -> bool:
    """After password, Google may show ToS and then consent. Drain both in sequence."""
    deadline = time.time() + max(180, int(timeout or 180))
    clicked_any = False
    last_urls: set[str] = set()
    while time.time() < deadline:
        account_pages = [p for p in browser.pages() if (not p.is_closed()) and "accounts.google.com" in (p.url or "")]
        if not account_pages:
            return clicked_any
        progressed = False
        for page in account_pages:
            url = page.url or ""
            if url not in last_urls:
                last_urls.add(url)
                log_fn(f"[GetToken] Google prompt page: {url[:140]}")
            if "gaplustos" in url or "speedbump" in url:
                progressed = _handle_google_tos(browser, timeout=8, log_fn=log_fn) or progressed
            # ToS ??????? accounts.google.com ? consent/approval ?????????? consent?
            progressed = _handle_oauth_consent(browser, timeout=8, log_fn=log_fn) or progressed
        if progressed:
            clicked_any = True
            time.sleep(2)
            continue
        time.sleep(1)
    return clicked_any


def _click_google_account_or_other(browser: OAuthBrowser, *, email: str, log_fn) -> bool:
    """Google ??????TreeWalker ????????? selector/????????"""
    labels = [
        (email or "").strip(),
        "\u4f7f\u7528\u5176\u4ed6\u8d26\u53f7",  # ??????
        "\u4f7f\u7528\u5176\u5b83\u8d26\u53f7",  # ??????
        "\u4f7f\u7528\u5176\u4ed6\u5e10\u53f7",  # ??????
        "\u4f7f\u7528\u5176\u5b83\u5e10\u53f7",  # ??????
        "Use another account", "Use other account", "Another account", "Different account",
    ]
    for page in browser.pages():
        url = page.url or ""
        if "accounts.google.com" not in url:
            continue
        try:
            result = page.evaluate(
                """
                async ({labels}) => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
                  };
                  const clickText = (label) => {
                    if (!label) return null;
                    const labelLower = String(label).toLowerCase();
                    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let node;
                    while (node = walker.nextNode()) {
                      const text = node.nodeValue || '';
                      if (!text.toLowerCase().includes(labelLower)) continue;
                      let el = node.parentElement;
                      let best = el;
                      for (let i = 0; i < 10 && el; i++, el = el.parentElement) {
                        const r = el.getBoundingClientRect();
                        const allText = (el.innerText || el.textContent || '').toLowerCase();
                        if (!allText.includes(labelLower)) continue;
                        if (visible(el) && r.width > 80 && r.height > 20 && r.width <= window.innerWidth + 5 && r.height < window.innerHeight * 0.5) {
                          best = el;
                          if (r.width > 180 && r.height >= 40) break;
                        }
                      }
                      if (best) {
                        best.scrollIntoView({block:'center', inline:'center'});
                        const rr = best.getBoundingClientRect();
                        const x = Math.max(2, Math.min(window.innerWidth - 2, rr.left + rr.width / 2));
                        const y = Math.max(2, Math.min(window.innerHeight - 2, rr.top + rr.height / 2));
                        const target = document.elementFromPoint(x, y) || best;
                        target.dispatchEvent(new MouseEvent('mouseover', {bubbles:true, clientX:x, clientY:y}));
                        target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:x, clientY:y}));
                        target.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:x, clientY:y}));
                        target.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:x, clientY:y}));
                        return label;
                      }
                    }
                    return null;
                  };
                  for (const label of labels) {
                    const clicked = clickText(label);
                    if (clicked) return clicked;
                  }
                  return '';
                }
                """,
                {"labels": labels},
            )
            if result:
                log_fn(f"[GetToken] Google ??????: {result}")
                time.sleep(3)
                return True
        except Exception:
            pass
    return False


# Backward-compatible name for old callers.
def _click_use_another_account(browser: OAuthBrowser, log_fn) -> bool:
    return _click_google_account_or_other(browser, email="", log_fn=log_fn)


def _fill_google_credentials(browser: OAuthBrowser, *, email: str, password: str, timeout: int, log_fn) -> None:
    if not email or not password:
        return
    deadline = time.time() + min(timeout, 240)
    email_done = False
    password_done = False
    logged_urls = set()
    last_google_url = ""
    last_body_preview = ""
    while time.time() < deadline and not password_done:
        for page in browser.pages():
            url = page.url or ""
            if url not in logged_urls:
                logged_urls.add(url)
                log_fn(f"[GetToken] browsr page: {url[:120]}")
            if "accounts.google.com" in url:
                last_google_url = url
            if "accounts.google.com" not in url and "gaplustos" not in url and "speedbump" not in url:
                continue
            try:
                try:
                    body_preview = page.locator("body").inner_text(timeout=1000)
                except Exception:
                    body_preview = ""
                if body_preview:
                    last_body_preview = body_preview[:800]

                # 1) Workspace ToS / speedbump has priority over everything else.
                if "gaplustos" in url or "speedbump" in url:
                    _handle_google_tos(browser, timeout=30, log_fn=log_fn)
                    continue

                # 2) Identifier captcha is still an input page, not OAuth consent.
                if _handle_google_identifier_challenge(page, log_fn=log_fn):
                    time.sleep(3)
                    continue

                # 3) Password page.
                pwd_locator = page.locator('input[type="password"]:visible, input[name="Passwd"]:visible').first
                if pwd_locator.count() > 0:
                    pwd_locator.fill(password, timeout=3000)
                    _submit_google_step(page, log_fn=log_fn, step_name="password")
                    password_done = True
                    log_fn("[GetToken] ??? Google ??")
                    time.sleep(5)
                    break

                # 4) Email / account chooser page. It may contain ?continue to ... / Next?,
                # so it must be handled before any generic consent click.
                email_locator = page.locator('input[type="email"]:visible, input[name="identifier"]:visible, input#identifierId:visible').first
                if email_locator.count() > 0:
                    if not email_done:
                        email_locator.fill(email, timeout=3000)
                        _submit_google_step(page, log_fn=log_fn, step_name="email")
                        email_done = True
                        log_fn("[GetToken] ??? Google ??")
                    else:
                        _submit_google_step(page, log_fn=log_fn, step_name="email-retry")
                    time.sleep(3)
                    continue

                if not email_done and ("accountchooser" in url or "signin/oauth" in url):
                    if _click_google_account_or_other(browser, email=email, log_fn=log_fn):
                        time.sleep(3)
                        continue

                # 5) Only after no credential input exists do we treat the page as OAuth consent.
                if any(hint in body_preview for hint in GOOGLE_CONSENT_TEXT_HINTS + GOOGLE_CONSENT_LABELS):
                    if _handle_oauth_consent(browser, timeout=20, log_fn=log_fn):
                        time.sleep(3)
                        continue
            except Exception:
                pass
        time.sleep(1)
    if not password_done:
        raise RuntimeError(f"GetToken Google password step not completed; last_url={last_google_url}; body={last_body_preview[:300]}")

def _portal_success_result(browser: OAuthBrowser, *, timeout: int, log_fn) -> dict:
    """? Portal login attempt ?????????? loginToken ? result?"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for page in browser.pages():
            if page.is_closed() or not _is_portal_url(str(page.url or "")):
                continue
            try:
                result = page.evaluate(
                    """
                    async () => {
                      const session = Object.fromEntries(Array.from({length: sessionStorage.length}, (_, i) => [sessionStorage.key(i), sessionStorage.getItem(sessionStorage.key(i))]));
                      const ids = Object.keys(session)
                        .filter((key) => key.startsWith('yd-login-attempt:') && String(session[key] || '').includes('GOOGLE'))
                        .map((key) => key.split(':')[1])
                        .reverse();
                      for (const id of ids) {
                        const r = await fetch(`/api/v1/login/attempts/${id}`, {credentials: 'include', cache: 'no-store'});
                        const j = await r.json().catch(() => null);
                        const data = j && j.data;
                        if (data && data.status === 'SUCCESS' && data.result && data.result.loginToken) {
                          return data.result;
                        }
                        if (data && ['ERROR', 'CANCELLED', 'EXPIRED'].includes(data.status)) {
                          return {error: data.errorMessage || data.status, attemptId: id};
                        }
                      }
                      return null;
                    }
                    """
                )
                if isinstance(result, dict) and result.get("loginToken"):
                    log_fn(f"[GetToken] Portal OAuth ??: {result.get('email') or result.get('userId')}")
                    return result
                if isinstance(result, dict) and result.get("error"):
                    raise RuntimeError(f"GetToken Portal attempt failed: {result.get('error')}")
            except RuntimeError:
                raise
            except Exception:
                pass
        time.sleep(1)
    return {}



def _wait_portal_result_through_google_prompts(browser: OAuthBrowser, *, timeout: int, log_fn) -> dict:
    """After password there are only three relevant states: ToS, consent, or Portal success.

    Google Workspace welcome / gaplustos may require two confirmations:
    Chinese page -> click ???, then English page -> click I understand,
    then OAuth consent -> click Continue/Allow, then Portal exposes loginToken.
    """
    deadline = time.time() + max(1, int(timeout or 300))
    seen_urls: set[str] = set()
    tos_clicks = 0
    consent_clicks = 0
    while time.time() < deadline:
        # Portal success is decisive; poll it every loop without blocking for long.
        try:
            portal_result = _portal_success_result(browser, timeout=1, log_fn=log_fn)
            if isinstance(portal_result, dict) and portal_result.get("loginToken"):
                return portal_result
        except Exception:
            raise

        progressed = False
        for page in list(browser.pages()):
            if page.is_closed():
                continue
            url = page.url or ""
            if url and url not in seen_urls:
                seen_urls.add(url)
                log_fn(f"[GetToken] OAuth state page: {url[:160]}")
            if "accounts.google.com" not in url:
                continue

            if "gaplustos" in url or "speedbump" in url:
                # Do not mark ToS as one-shot. This page frequently requires two clicks.
                if _handle_google_tos(browser, timeout=10, log_fn=log_fn):
                    tos_clicks += 1
                    log_fn(f"[GetToken] ToS acknowledged count={tos_clicks}")
                    progressed = True
                    break

            # After ToS, or sometimes directly after password, Google shows OAuth consent.
            if _handle_oauth_consent(browser, timeout=6, log_fn=log_fn):
                consent_clicks += 1
                log_fn(f"[GetToken] consent continued count={consent_clicks}")
                progressed = True
                break

        time.sleep(2 if progressed else 1)

    return {}


def _portal_login_gettoken(browser: OAuthBrowser, login_token: str, *, log_fn) -> dict:
    """? gettoken.dev ?? Portal loginToken ???? session?"""
    page = next((p for p in browser.pages() if _is_gettoken_url(str(p.url or ""))), None)
    if page is None:
        page = browser.new_page()
        page.goto(f"{BASE_URL}/console/api-keys", wait_until="domcontentloaded", timeout=30000)
    result = page.evaluate(
        """
        async (loginToken) => {
          const r = await fetch('/api/auth/portal-login', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'content-type': 'application/json'},
            body: JSON.stringify({loginToken, referralCode: null, referralHost: 'gettoken.dev', referralSlug: null}),
          });
          const text = await r.text();
          let body = null;
          try { body = JSON.parse(text); } catch (_) { body = {raw: text}; }
          return {status: r.status, body};
        }
        """,
        login_token,
    )
    body = result.get("body") if isinstance(result, dict) else {}
    if not result or int(result.get("status") or 0) >= 400 or not body.get("success"):
        raise RuntimeError(f"GetToken portal-login failed: {result}")
    user = ((body.get("data") or {}) if isinstance(body, dict) else {}).get("user") or {}
    log_fn(f"[GetToken] portal-login ??: {user.get('email') or user.get('id')}")
    return user


def _wait_logged_in(browser: OAuthBrowser, *, timeout: int, log_fn) -> dict:
    deadline = time.time() + timeout
    logged_urls = set()
    refreshed = False
    while time.time() < deadline:
        for page in browser.pages():
            url = page.url or ""
            if url and url not in logged_urls:
                logged_urls.add(url)
                log_fn(f"[GetToken] wait page: {url[:120]}")
            # 处理 OAuth 后的 ToS/consent 页面
            if "gaplustos" in url or "speedbump" in url:
                _handle_google_tos(browser, timeout=15, log_fn=log_fn)
                time.sleep(2)
                continue
            if "consent" in url or "oauth" in url:
                _handle_oauth_consent(browser, timeout=15, log_fn=log_fn)
                time.sleep(2)
                continue
            if not _is_gettoken_url(url):
                continue
            if not refreshed:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=15000)
                    refreshed = True
                    time.sleep(2)
                except Exception:
                    pass
            try:
                data = page.evaluate(
                    """
                    async () => {
                      const r = await fetch('/api/user/me', {method:'GET', credentials:'same-origin', cache:'no-store'});
                      return {status:r.status, body: await r.json().catch(() => null)};
                    }
                    """
                )
                body = data.get("body") if isinstance(data, dict) else None
                user = ((body or {}).get("data") or {}).get("user") if isinstance(body, dict) else None
                if user:
                    log_fn(f"[GetToken] 已登录: {user.get('email') or user.get('id')}")
                    return user
            except Exception:
                pass
        time.sleep(2)
    return {}


def _extract_or_create_api_key(browser: OAuthBrowser, *, timeout: int, create_api_key: bool, log_fn) -> tuple[str, dict]:
    """??????/?? GetToken API Key?UI ??????????"""
    request_trace: list[dict] = []
    page = next((p for p in browser.pages() if _is_gettoken_url(p.url or "")), None) or browser.active_page()
    try:
        if not _is_gettoken_url(page.url or ""):
            page.goto(f"{BASE_URL}/console/api-keys", wait_until="domcontentloaded", timeout=45000)
        else:
            page.goto(f"{BASE_URL}/console/api-keys", wait_until="domcontentloaded", timeout=45000)
        time.sleep(2)
    except Exception:
        pass

    def api_fetch(path: str, *, method: str = "GET", body: dict | None = None) -> dict:
        result = page.evaluate(
            """
            async ({path, method, body}) => {
              const opts = {method, credentials: 'include', headers: {'Content-Type': 'application/json'}};
              if (body !== null && body !== undefined) opts.body = JSON.stringify(body);
              const res = await fetch(path, opts);
              const text = await res.text();
              let data = null;
              try { data = JSON.parse(text); } catch (_) { data = {raw: text}; }
              return {ok: res.ok, status: res.status, data};
            }
            """,
            {"path": path, "method": method, "body": body},
        )
        request_trace.append({"method": method, "url": path, "status": result.get("status") if isinstance(result, dict) else 0})
        return result if isinstance(result, dict) else {"ok": False, "status": 0, "data": result}

    # 1. ????? keys??? apiKey ??????? id?? reveal?
    for list_path in ("/api/workspace/api-keys", "/api/workspace/api-keys?page=1&pageSize=20"):
        try:
            listed = api_fetch(list_path)
            data = listed.get("data") or {}
            key, info = _collect_json_api_key(data)
            if key:
                return key, {"source": "protocol_list", "request_trace": request_trace, **(info if isinstance(info, dict) else {})}
            # ? apiKeyId/id ? reveal?
            stack = [data]
            seen_ids = []
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    kid = cur.get("apiKeyId") or cur.get("id")
                    if kid and str(kid) not in seen_ids:
                        seen_ids.append(str(kid))
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)
            for kid in seen_ids:
                rev = api_fetch(f"/api/workspace/api-keys/{kid}/reveal", method="POST")
                key, info = _collect_json_api_key(rev.get("data"))
                if key:
                    return key, {"source": "protocol_reveal", "apiKeyId": kid, "request_trace": request_trace, **(info if isinstance(info, dict) else {})}
        except Exception:
            pass

    # 2. ???? key ???????
    if create_api_key:
        body = {"name": f"auto-register-{int(time.time())}"}
        created = api_fetch("/api/workspace/api-keys", method="POST", body=body)
        if created.get("ok"):
            data = created.get("data") or {}
            key, info = _collect_json_api_key(data)
            if key:
                return key, {"source": "protocol_create", "request_trace": request_trace, **(info if isinstance(info, dict) else {})}
            item = data.get("item") if isinstance(data, dict) else None
            kid = (item or {}).get("apiKeyId") if isinstance(item, dict) else None
            if kid:
                rev = api_fetch(f"/api/workspace/api-keys/{kid}/reveal", method="POST")
                key, info = _collect_json_api_key(rev.get("data"))
                if key:
                    return key, {"source": "protocol_create_reveal", "apiKeyId": kid, "request_trace": request_trace, **(info if isinstance(info, dict) else {})}
        else:
            log_fn(f"[GetToken] ???? API Key ??: {created}")

    # 3. ????????????
    try:
        text = page.inner_text("body", timeout=5000)
    except Exception:
        text = ""
    key = _extract_api_key_from_text(text)
    if key:
        return key, {"source": "page_text_fallback", "request_trace": request_trace}

    return "", {"source": "not_found", "request_trace": request_trace}


PORTAL_APP_ID = "appw084AkI0Jtflej7t"
PORTAL_BASE = "https://pay.imgto.link"
PORTAL_AUTH_URL = f"{PORTAL_BASE}/en/auth/connect/{PORTAL_APP_ID}?origin={BASE_URL}"


def register_with_browser_oauth(
    *,
    proxy: str | None = None,
    oauth_provider: str = "google",
    email_hint: str = "",
    google_password: str = "",
    timeout: int = 300,
    log_fn=print,
    chrome_user_data_dir: str = "",
    chrome_cdp_url: str = "",
    create_api_key: bool = True,
    crash_recovery: bool = True,
) -> dict:
    if not email_hint or not google_password:
        raise RuntimeError("GetToken OAuth 需要提供 Google 邮箱和密码")

    def _click_portal_google_button(context, portal_page, log_fn) -> bool:
        """在 Portal 页面上点击 Google 按钮，并捕获打开的新 Google OAuth 页面。"""
        url = portal_page.url or ""
        if not _is_portal_url(url):
            return False
        try:
            btn = portal_page.get_by_role("button", name=re.compile(r"Google|google", re.IGNORECASE))
            if btn.count() > 0:
                # 监听新页面（Google 按钮在 iframe 中通过 window.open 打开）
                with context.expect_page(timeout=10000) as page_info:
                    btn.first.click()
                new_page = page_info.value
                log_fn(f"[GetToken] Portal Google 按钮点击，新页面: {(new_page.url or '')[:120]}")
                time.sleep(3)
                return True
            # fallback: 搜 Google 链接
            for el in portal_page.locator("a, div[role='button'], span[role='button']").all():
                try:
                    text = (el.inner_text() or "").lower()
                    if "google" in text:
                        with context.expect_page(timeout=10000) as page_info:
                            el.click()
                        new_page = page_info.value
                        log_fn(f"[GetToken] Portal Google 元素点击: {text[:80]}")
                        time.sleep(3)
                        return True
                except Exception:
                    continue
        except Exception as e:
            log_fn(f"[GetToken] Portal 点击失败: {e}")
            return False
        return False

    def _do_oauth(browser: OAuthBrowser) -> dict:
        # 1. 先开一个 gettoken 页面（用于最终接收 callback session）
        log_fn("[GetToken] 打开 gettoken 控制台")
        browser.goto(f"{BASE_URL}/console/api-keys", wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        user = _wait_logged_in(browser, timeout=3, log_fn=log_fn)
        if user:
            current_email = str(user.get("email") or "").strip()
            if _same_email(current_email, email_hint):
                return user
            log_fn(f"[GetToken] ?? gettoken session ?? {current_email} ??? {email_hint} ????????? OAuth")
            _clear_gettoken_session(browser, log_fn=log_fn)

        # 2. ????? Portal auth URL????? Portal/Google ?????????? OAuth attempt ???? accountchooser?
        for old_page in list(browser.pages()):
            try:
                old_url = old_page.url or ""
                if _is_portal_url(old_url) or "accounts.google.com" in old_url:
                    old_page.close()
            except Exception:
                pass
        log_fn(f"[GetToken] ?? Portal: {PORTAL_AUTH_URL}")
        portal_page = browser.new_page()
        portal_page.goto(PORTAL_AUTH_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(3)

        # 3. 在 Portal 页面点击 Google 按钮（自动捕获新页面）
        if not _click_portal_google_button(browser.context, portal_page, log_fn):
            raise RuntimeError("GetToken Portal 页面未找到 Google 登录按钮")

        # 4. 填 Google 邮箱密码
        _fill_google_credentials(browser, email=email_hint, password=google_password, timeout=timeout, log_fn=log_fn)

        # 5. After password, deterministically handle: ToS -> consent -> Portal loginToken.
        log_fn("[GetToken] waiting OAuth state: ToS / consent / Portal loginToken")
        portal_result = _wait_portal_result_through_google_prompts(browser, timeout=timeout, log_fn=log_fn)
        if not portal_result.get("loginToken"):
            raise RuntimeError("GetToken Portal ??? loginToken")

        # 6. ? loginToken ? gettoken ??????? session
        user = _portal_login_gettoken(browser, str(portal_result["loginToken"]), log_fn=log_fn)
        for page in browser.pages():
            if _is_gettoken_url(page.url or ""):
                try:
                    page.goto(f"{BASE_URL}/console/api-keys", wait_until="networkidle", timeout=45000)
                except Exception:
                    pass
                break
        if not user:
            user = _wait_logged_in(browser, timeout=timeout, log_fn=log_fn)
        if not user:
            body = ""
            try:
                body = browser.active_page().inner_text("body", timeout=5000)
            except Exception:
                pass
            if "新用户暂时关闭注册" in body or "Registration is temporarily closed" in body:
                raise RuntimeError("GetToken 新用户暂时关闭注册")
            raise RuntimeError("GetToken OAuth 登录未完成")
        return user

    def _extract_key(browser: OAuthBrowser, user: dict) -> dict:
        request_trace: list[dict] = []
        for page in browser.pages():
            try:
                page.on("request", lambda req: request_trace.append({"method": req.method, "url": req.url.replace(BASE_URL, "")}) if _is_gettoken_url(req.url) and "/api/" in req.url else None)
            except Exception:
                pass
        api_key, api_key_info = _extract_or_create_api_key(browser, timeout=60, create_api_key=bool(create_api_key), log_fn=log_fn)
        if not api_key:
            raise RuntimeError("GetToken 未找到或创建 API Key")
        email = finalize_oauth_email(str(user.get("email") or ""), email_hint, "GetToken")
        cookie_map = browser.cookie_dict(domain_substrings=("gettoken.dev",))
        return {
            "email": email,
            "user_id": str(user.get("id") or ""),
            "api_key": api_key,
            "api_key_info": {**api_key_info, "key_preview": _redact(api_key)},
            "account_info": user,
            "cookies": cookie_map,
            "session_cookie": browser.cookie_header(domain_substrings=("gettoken.dev",)),
            "request_trace": request_trace + list(api_key_info.get("request_trace") or []),
            "registration_note": "browser_oauth",
        }

    def _make_browser():
        # Google OAuth ???????????????????? CDP / Profile?
        # ???? OAuth ??? Playwright Chromium??? Google ?????????
        return OAuthBrowser(
            proxy=None,
            headless=False,
            chrome_user_data_dir=chrome_user_data_dir,
            chrome_cdp_url=chrome_cdp_url,
            log_fn=log_fn,
        )

    with _make_browser() as browser:
        try:
            user = _do_oauth(browser)
            return _extract_key(browser, user)
        except Exception as first_err:
            if not crash_recovery:
                raise
            log_fn(f"[GetToken] 尝试崩溃恢复: {first_err}")
            try:
                with _make_browser() as browser2:
                    browser2.goto(f"{BASE_URL}/console/api-keys", wait_until="domcontentloaded", timeout=45000)
                    time.sleep(3)
                    user = _wait_logged_in(browser2, timeout=10, log_fn=log_fn)
                    if user:
                        log_fn(f"[GetToken] cookies 恢复成功: {user.get('email')}")
                        return _extract_key(browser2, user)
                    user = _do_oauth(browser2)
                    return _extract_key(browser2, user)
            except Exception as second_err:
                log_fn(f"[GetToken] 崩溃恢复失败: {second_err}")
                raise first_err
