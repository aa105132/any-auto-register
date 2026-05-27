"""YepAPI Google OAuth 自动化。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from core.google_oauth import drive_google_oauth, google_oauth_snapshot
from core.oauth_browser import OAuthBrowser, finalize_oauth_email, try_click_provider_on_page

SITE_URL = "https://www.yepapi.com/"
LOGIN_URL = "https://www.yepapi.com/login"
DASHBOARD_URL = "https://www.yepapi.com/dashboard"
AUTH_BASE = "https://www.yepapi.com/api/auth"
API_BASE = "https://api.yepapi.com"
VERIFY_PATH = "/v1/ai/models"


def _cookie_map(browser: OAuthBrowser) -> dict[str, str]:
    try:
        return browser.cookie_dict(domain_substrings=("yepapi.com", "www.yepapi.com"))
    except Exception:
        return {}


def _clear_yepapi_oauth_transaction(browser: OAuthBrowser) -> None:
    """清理 Better Auth/OAuth 临时 transaction cookie，避免复用旧 state。"""
    if not browser.context:
        return
    expired: list[dict[str, Any]] = []
    for cookie in browser.cookies():
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        low = name.lower()
        if "yepapi.com" not in domain:
            continue
        if (
            "state" in low
            or "verifier" in low
            or "callback" in low
            or "oauth" in low
            or "better-auth" in low and "session" not in low
        ):
            expired.append({
                "name": name,
                "value": "",
                "domain": domain,
                "path": str(cookie.get("path") or "/"),
                "expires": 0,
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": bool(cookie.get("secure", True)),
                "sameSite": cookie.get("sameSite") or "Lax",
            })
    if expired:
        try:
            browser.context.add_cookies(expired)
        except Exception:
            pass


def _has_restart_process_error(browser: OAuthBrowser) -> bool:
    for page in browser.pages():
        if page.is_closed():
            continue
        if "please_restart_the_process" in str(page.url or ""):
            return True
    return False


def _yepapi_oauth_error(browser: OAuthBrowser) -> str:
    """读取 YepAPI OAuth 回调落地错误。"""
    for page in browser.pages():
        if page.is_closed():
            continue
        urls = [str(page.url or "")]
        try:
            urls.extend(str(getattr(frame, "url", "") or "") for frame in list(getattr(page, "frames", []) or []))
        except Exception:
            pass
        for url in urls:
            if "yepapi.com" not in url or "error=" not in url:
                continue
            parsed = urlparse(url)
            query = parsed.query or ""
            for part in query.split("&"):
                if part.startswith("error="):
                    return part.split("=", 1)[1] or url
            return url
    return ""


def _cookie_session(cookies: dict[str, str], *, proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="www.yepapi.com")
        session.cookies.set(name, value, domain=".yepapi.com")
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": SITE_URL.rstrip("/"),
        "Referer": DASHBOARD_URL,
    })
    return session


def _get_session_http(cookies: dict[str, str], *, proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies, proxy=proxy)
    try:
        response = session.get(f"{AUTH_BASE}/get-session", timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {"raw": response.text[:2000]}
        return {"ok": response.ok and bool(data), "status": response.status_code, "data": data}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "data": {}}


def _open_oauth_url_nonblocking(page, url: str, *, log_fn=print) -> bool:
    """用可控新标签非阻塞打开 OAuth URL。

    Playwright 的 page.goto(accounts.google.com/...) 在 Google/CF 链路上经常
    等待到超时；这里不等待跨站导航完成，只负责把 URL 交给真实浏览器。
    后续由 _wait_google_page / drive_google_oauth 接管新标签。
    """
    if not url:
        return False
    attempts: list[str] = []
    # 最稳定路径：创建 about:blank 标签后用 location.assign，避免 popup 拦截。
    try:
        new_page = page.context.new_page()
        new_page.set_default_navigation_timeout(15000)
        try:
            new_page.goto("about:blank", wait_until="commit", timeout=5000)
        except Exception:
            pass
        try:
            new_page.evaluate("u => { window.location.assign(u); }", url)
        except Exception as exc:
            # 导航导致 execution context 销毁也视作已发起。
            if "Execution context was destroyed" not in str(exc) and "Navigation" not in str(exc):
                raise
        # 不等完整 load，只等 commit；如果 evaluate 被浏览器吞掉，用短 goto 兜底。
        time.sleep(1)
        if "accounts.google.com" not in str(new_page.url or ""):
            try:
                new_page.goto(url, wait_until="commit", timeout=12000)
            except Exception as exc:
                if "Timeout" not in str(exc) and "Navigation" not in str(exc):
                    raise
        log_fn(f"[YepAPI] 已用新标签发起 Google OAuth: {new_page.url}")
        return True
    except Exception as exc:
        attempts.append(f"new_page={exc!r}")
    # 兜底：主页面 window.open。
    try:
        page.evaluate("u => { window.open(u, '_blank'); }", url)
        log_fn("[YepAPI] 已用 window.open 发起 Google OAuth")
        return True
    except Exception as exc:
        attempts.append(f"window_open={exc!r}")
    # 最后兜底：当前页 location.assign，仍不使用 goto 等待。
    try:
        page.evaluate("u => { window.location.assign(u); }", url)
        log_fn("[YepAPI] 已用当前标签发起 Google OAuth")
        return True
    except Exception as exc:
        attempts.append(f"same_page={exc!r}")
    log_fn(f"[YepAPI] OAuth URL 打开失败: {'; '.join(attempts)}")
    return False


def _start_google_oauth_protocol(page, *, callback_url: str = DASHBOARD_URL, log_fn=print) -> bool:
    """优先用 Better Auth 协议发起 Google OAuth。"""
    try:
        temp_browser = type("_B", (), {"context": page.context, "cookies": lambda self: list(page.context.cookies())})()
        _clear_yepapi_oauth_transaction(temp_browser)
    except Exception:
        pass
    payloads = [
        {"provider": "google", "callbackURL": callback_url},
    ]
    for payload in payloads:
        try:
            log_fn(f"[YepAPI] 尝试 Better Auth OAuth payload={payload}")
            result = page.evaluate(
                """
                async ({payload}) => {
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort('timeout'), 15000);
                  try {
                    const response = await fetch('/api/auth/sign-in/social', {
                      method: 'POST',
                      headers: {'content-type': 'application/json', 'accept': 'application/json'},
                      credentials: 'include',
                      body: JSON.stringify(payload),
                      redirect: 'manual',
                      signal: controller.signal
                    });
                    const text = await response.text();
                    let data = null;
                    try { data = JSON.parse(text); } catch (e) { data = {raw: text.slice(0, 2000)}; }
                    return {ok: response.ok, status: response.status, headers: Object.fromEntries(response.headers.entries()), data};
                  } catch (e) {
                    return {ok: false, status: 0, error: String(e && (e.message || e))};
                  } finally { clearTimeout(timer); }
                }
                """,
                {"payload": payload},
            ) or {}
            data = result.get("data") if isinstance(result, dict) else {}
            url = ""
            if isinstance(data, dict):
                url = str(data.get("url") or data.get("redirect") or data.get("location") or "")
            if not url and isinstance(result, dict):
                headers = result.get("headers") or {}
                if isinstance(headers, dict):
                    url = str(headers.get("location") or "")
            if url:
                log_fn(f"[YepAPI] Better Auth Google OAuth URL: {url[:160]}")
                return _open_oauth_url_nonblocking(page, url, log_fn=log_fn)
            log_fn(f"[YepAPI] Better Auth OAuth 发起未返回 URL: {result}")
        except Exception as exc:
            log_fn(f"[YepAPI] Better Auth OAuth 发起异常: {exc!r}")
    return False




def _click_cloudflare_challenge(page, *, log_fn=print) -> bool:
    """主动点击 Cloudflare challenge。

    YepAPI 的 challenge 页面有时没有 Turnstile iframe/checkbox，实测在真实
    Chrome 中点击主页面可见大容器后会继续验证。因此优先模拟这个动作。
    """
    # 1) 优先复刻实测成功路径：点击主页面最大可见 challenge 容器中心。
    try:
        box = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 80 && r.height > 80 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const nodes = [...document.querySelectorAll('[role=main], .main-content, main, body > div, div')].filter(visible);
              nodes.sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height) - (a.getBoundingClientRect().width * a.getBoundingClientRect().height));
              const el = nodes[0] || document.body;
              const r = el.getBoundingClientRect();
              // 实测成功坐标在视口下半部（约 y=600），不是提示区中部。
              const x = Math.max(120, Math.min(innerWidth - 120, innerWidth / 2));
              const y = Math.max(260, Math.min(innerHeight - 40, 610));
              return {x, y, w: r.width, h: r.height};
            }
            """
        ) or {}
        x = float(box.get("x") or 320)
        y = float(box.get("y") or 360)
        page.mouse.move(x, y)
        page.mouse.down()
        time.sleep(0.12)
        page.mouse.up()
        log_fn(f"[YepAPI] 已点击 Cloudflare 主页面验证区域: x={int(x)} y={int(y)}")
        time.sleep(8)
        return True
    except Exception:
        pass

    # 2) 如果存在 Turnstile iframe/checkbox，再点具体控件。
    for frame in list(getattr(page, "frames", []) or []):
        frame_url = str(getattr(frame, "url", "") or "")
        if "cloudflare" not in frame_url and "turnstile" not in frame_url:
            continue
        for selector in ['input[type="checkbox"]', '[role="checkbox"]', '.ctp-checkbox-label', 'label', 'button']:
            try:
                loc = frame.locator(selector).first
                loc.wait_for(state="visible", timeout=1000)
                loc.click(timeout=2000, force=True)
                log_fn(f"[YepAPI] 已点击 Cloudflare 验证元素: {selector}")
                time.sleep(8)
                return True
            except Exception:
                pass
    return False


def _wait_out_cloudflare(page, browser: OAuthBrowser, *, timeout: int = 120, log_fn=print) -> bool:
    deadline = time.time() + max(10, timeout)
    last_body = ""
    click_attempts = 0
    refreshed = False
    while time.time() < deadline:
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:
            body = ""
        last_body = body
        cookies = _cookie_map(browser)
        if cookies.get("cf_clearance") and not _is_cloudflare_challenge_text(body):
            return True
        if not _is_cloudflare_challenge_text(body):
            return True
        if click_attempts < 4:
            click_attempts += 1
            if _click_cloudflare_challenge(page, log_fn=log_fn):
                time.sleep(10)
                continue
        if not refreshed and time.time() + 45 < deadline:
            refreshed = True
            time.sleep(6)
            try:
                page.reload(wait_until="domcontentloaded", timeout=90000)
            except Exception:
                pass
        time.sleep(3)
    log_fn(f"[YepAPI] Cloudflare 等待/点击超时，body={last_body[:300]}")
    return False




def _dump_yepapi_login_diagnostics(page, *, log_fn=print) -> str:
    """保存 YepAPI 登录页关键 DOM 摘要，便于定位 OAuth 入口识别失败。"""
    try:
        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        base = output_dir / f"yepapi_login_diagnostics_{stamp}"
        data: dict[str, Any] = {
            "url": str(page.url or ""),
            "title": "",
            "body_preview": "",
            "buttons": [],
        }
        try:
            data["title"] = str(page.title() or "")
        except Exception:
            pass
        try:
            data["body_preview"] = str(page.inner_text("body", timeout=3000) or "")[:8000]
        except Exception:
            pass
        try:
            data["buttons"] = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
                  };
                  const textOf = (el) => {
                    const attrs = ['aria-label','title','name','value','data-provider','data-testid','href','type','id','class','alt'];
                    const parts = [el.innerText || '', el.textContent || '', el.value || ''];
                    for (const attr of attrs) parts.push(el.getAttribute(attr) || '');
                    try { parts.push(Object.values(el.dataset || {}).join(' ')); } catch (e) {}
                    try {
                      for (const img of el.querySelectorAll('img,svg,use')) {
                        parts.push(img.getAttribute('alt') || '');
                        parts.push(img.getAttribute('aria-label') || '');
                        parts.push(img.getAttribute('title') || '');
                        parts.push(img.getAttribute('href') || '');
                        parts.push(img.getAttribute('xlink:href') || '');
                        parts.push(img.outerHTML || '');
                      }
                    } catch (e) {}
                    return parts.join(' ').replace(/\\s+/g, ' ').trim();
                  };
                  return [...document.querySelectorAll('button,a,[role="button"],input[type="submit"],input[type="button"],div[onclick],span[onclick],[tabindex]')]
                    .filter(visible)
                    .slice(0, 120)
                    .map((el) => {
                      const r = el.getBoundingClientRect();
                      return {
                        tag: el.tagName,
                        role: el.getAttribute('role') || '',
                        type: el.getAttribute('type') || '',
                        text: textOf(el).slice(0, 600),
                        id: el.id || '',
                        className: String(el.className || '').slice(0, 300),
                        rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
                      };
                    });
                }
                """
            ) or []
        except Exception as exc:
            data["buttons_error"] = repr(exc)
        json_path = base.with_suffix(".json")
        html_path = base.with_suffix(".html")
        json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            html_path.write_text(str(page.content() or ""), encoding="utf-8")
        except Exception:
            pass
        log_fn(f"[YepAPI] 登录页诊断已保存: {json_path}")
        return str(json_path)
    except Exception as exc:
        log_fn(f"[YepAPI] 登录页诊断保存失败: {exc!r}")
        return ""


def _click_yepapi_google_entry(page, *, log_fn=print) -> bool:
    """在 YepAPI 登录页深度识别并点击 Google OAuth 入口。"""
    try:
        result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
              };
              const attrs = ['aria-label','title','name','value','data-provider','data-testid','data-auth','href','type','id','class','alt'];
              const textOf = (el) => {
                const parts = [el.innerText || '', el.textContent || '', el.value || ''];
                for (const attr of attrs) parts.push(el.getAttribute(attr) || '');
                try { parts.push(Object.values(el.dataset || {}).join(' ')); } catch (e) {}
                try {
                  for (const child of el.querySelectorAll('*')) {
                    parts.push(child.getAttribute('alt') || '');
                    parts.push(child.getAttribute('aria-label') || '');
                    parts.push(child.getAttribute('title') || '');
                    parts.push(child.getAttribute('href') || '');
                    parts.push(child.getAttribute('xlink:href') || '');
                    if ((child.tagName || '').toLowerCase() === 'svg') parts.push(child.outerHTML || '');
                  }
                } catch (e) {}
                return parts.join(' ').replace(/\\s+/g, ' ').trim();
              };
              const clickableAncestor = (el) => {
                let cur = el;
                for (let i = 0; cur && i < 6; i += 1, cur = cur.parentElement) {
                  const tag = (cur.tagName || '').toLowerCase();
                  const role = (cur.getAttribute('role') || '').toLowerCase();
                  const style = getComputedStyle(cur);
                  if (tag === 'button' || tag === 'a' || role === 'button' || cur.onclick || cur.tabIndex >= 0 || style.cursor === 'pointer') {
                    return cur;
                  }
                }
                return el;
              };
              const scoreFor = (text) => {
                const low = text.toLowerCase();
                let score = 0;
                if (low.includes('continue with google')) score += 20;
                if (low.includes('sign in with google')) score += 20;
                if (low.includes('login with google')) score += 18;
                if (low.includes('google')) score += 12;
                if (low.includes('g-logo') || low.includes('google-logo') || low.includes('googleoauth')) score += 10;
                if (low.includes('oauth') && low.includes('google')) score += 8;
                if (low.includes('github') || low.includes('apple') || low.includes('microsoft')) score -= 10;
                return score;
              };
              let best = null;
              for (const el of [...document.querySelectorAll('*')]) {
                if (!visible(el)) continue;
                const text = textOf(el);
                const score = scoreFor(text);
                if (score <= 0) continue;
                const target = clickableAncestor(el);
                if (!target || !visible(target)) continue;
                const r = target.getBoundingClientRect();
                const area = r.width * r.height;
                const item = {el, target, score, area, text, tag: target.tagName, x: r.x + r.width / 2, y: r.y + r.height / 2};
                if (!best || item.score > best.score || (item.score === best.score && item.area > best.area)) best = item;
              }
              if (!best) return {clicked: false, reason: 'not_found'};
              best.target.scrollIntoView({block: 'center', inline: 'center'});
              const r = best.target.getBoundingClientRect();
              return {
                clicked: true,
                text: best.text.slice(0, 240),
                tag: best.tag,
                score: best.score,
                x: r.x + r.width / 2,
                y: r.y + r.height / 2
              };
            }
            """
        ) or {}
        if isinstance(result, dict) and result.get("clicked"):
            x = float(result.get("x") or 0)
            y = float(result.get("y") or 0)
            if x > 0 and y > 0:
                page.mouse.move(x, y)
                page.mouse.click(x, y, delay=80)
                log_fn(f"[YepAPI] 深度识别并真实鼠标点击 Google 登录入口: {result}")
                return True
            log_fn(f"[YepAPI] 深度识别到 Google 入口但坐标无效: {result}")
        log_fn(f"[YepAPI] 深度识别未找到 Google 登录入口: {result}")
    except Exception as exc:
        log_fn(f"[YepAPI] 深度点击 Google 登录入口异常: {exc!r}")
    return False

def _close_stale_yepapi_error_pages(browser: OAuthBrowser) -> None:
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        if "www.yepapi.com" in url and "error=" in url:
            try:
                page.close()
            except Exception:
                pass


def _click_google_login_browser_only(page, *, log_fn=print) -> bool:
    """纯浏览器点击 YepAPI Google 登录入口，并捕获 popup/导航。"""
    def click_once() -> bool:
        if _click_yepapi_google_entry(page, log_fn=log_fn):
            return True
        if try_click_provider_on_page(page, "google"):
            log_fn("[YepAPI] 通用识别点击 Google 登录按钮")
            return True
        return False

    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        with page.expect_popup(timeout=8000) as popup_info:
            if not click_once():
                return False
        popup = popup_info.value
        log_fn(f"[YepAPI] 纯浏览器点击打开 popup: {popup.url}")
        return True
    except Exception:
        try:
            if "accounts.google.com" in str(page.url or ""):
                log_fn(f"[YepAPI] 纯浏览器点击后当前页进入 Google: {page.url}")
                return True
        except Exception:
            pass
    if click_once():
        log_fn("[YepAPI] 纯浏览器点击 Google 登录按钮")
        time.sleep(5)
        return True
    return False


def _wait_google_page(browser: OAuthBrowser, *, timeout: int = 20, log_fn=print) -> bool:
    deadline = time.time() + max(1, timeout)
    last_urls = ""
    while time.time() < deadline:
        urls: list[str] = []
        for item in browser.pages():
            if item.is_closed():
                continue
            current = str(item.url or "")
            urls.append(current)
            if "accounts.google.com" in current:
                log_fn(f"[YepAPI] 已进入 Google OAuth 页面: {current}")
                return True
        joined = " | ".join(urls)[:500]
        if joined and joined != last_urls:
            last_urls = joined
            log_fn(f"[YepAPI] 等待 Google OAuth 页面，当前标签: {joined}")
        time.sleep(0.5)
    log_fn(f"[YepAPI] 等待 Google OAuth 页面超时，最后标签: {last_urls}")
    return False

def _click_google_login(page, *, log_fn=print, protocol_first: bool = True) -> bool:
    if protocol_first and _start_google_oauth_protocol(page, log_fn=log_fn):
        return True
    if try_click_provider_on_page(page, "google"):
        log_fn("[YepAPI] 点击 Google 登录按钮")
        return True
    try:
        clicked = page.evaluate(
            """
            () => {
              const words = ['Google', 'Continue with Google', 'Sign in with Google', '使用 Google'];
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const nodes = [...document.querySelectorAll('button,a,[role=button]')].filter(visible);
              const node = nodes.find(n => words.some(w => ((n.innerText||n.textContent||n.value||n.getAttribute('aria-label')||'').includes(w))));
              if (!node) return '';
              node.click();
              return (node.innerText||node.textContent||node.value||node.getAttribute('aria-label')||'clicked').trim();
            }
            """
        )
        if clicked:
            log_fn(f"[YepAPI] 点击 Google 登录入口: {clicked}")
            return True
    except Exception:
        pass
    return False


def _is_cloudflare_challenge_text(text: str) -> bool:
    low = str(text or "").lower()
    return "cloudflare" in low and ("安全验证" in text or "just a moment" in low or "checking your browser" in low)




def _google_rejected_reason(browser: OAuthBrowser) -> str:
    for item in browser.pages():
        if item.is_closed():
            continue
        url = str(item.url or "")
        if "accounts.google.com" not in url:
            continue
        try:
            body = item.inner_text("body", timeout=1200)
        except Exception:
            body = ""
        if "signin/rejected" in url or "无法登录" in body or "contact your domain administrator" in body.lower():
            return body[:1000] or url
    return ""

def _looks_like_yepapi_shell_text(text: str) -> bool:
    body = str(text or "")
    return (
        "Unified API gateway" in body
        or "Command Palette" in body and "APIs" in body and "Get Started" in body
        or "Sign in to your account" in body and "Continue with Google" in body
    )


def _login_done(browser: OAuthBrowser) -> bool:
    # 这里运行在 Google driver 的高频循环里，禁止做长 HTTP 请求；
    # 真正 Better Auth session 校验在 driver 退出后集中轮询。
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        path = parsed.path or "/"
        if host.endswith("yepapi.com") and path.startswith("/dashboard"):
            return True
        if host in {"www.yepapi.com", "yepapi.com"} and path not in {"/login", "/api/auth/callback/google"} and "__cf_chl" not in url:
            return True
        # stop_when 必须轻量，禁止在 Google 导航中读 DOM；目标站 shell 由
        # drive_google_oauth 主循环在已有 body 快照后处理。
    return False


def _extract_email(session_result: dict[str, Any], fallback: str) -> str:
    data = session_result.get("data") if isinstance(session_result, dict) else {}
    if isinstance(data, dict):
        user = data.get("user") if isinstance(data.get("user"), dict) else data.get("data") if isinstance(data.get("data"), dict) else data
        if isinstance(user, dict):
            for key in ("email", "user_email"):
                if user.get(key):
                    return str(user.get(key)).strip()
    return fallback


def _find_api_key(data: Any) -> str:
    if isinstance(data, str):
        match = re.search(r"yep_[a-zA-Z0-9_\-]{12,}|yep_sk_[a-zA-Z0-9_\-]{8,}|sk-[a-zA-Z0-9_\-]{12,}", data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        for key in ("api_key", "apiKey", "key", "token", "secret", "value"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                found = _find_api_key(value)
                return found or value.strip()
        for value in data.values():
            found = _find_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_api_key(item)
            if found:
                return found
    return ""


def _try_key_endpoints(cookies: dict[str, str], *, proxy: str | None = None) -> dict[str, Any]:
    session = _cookie_session(cookies, proxy=proxy)
    candidates = [
        ("POST", "/api/keys", {"name": f"auto-register-{int(time.time())}"}),
        ("POST", "/api/api-keys", {"name": f"auto-register-{int(time.time())}"}),
        ("POST", "/api/user/api-keys", {"name": f"auto-register-{int(time.time())}"}),
        ("POST", "/api/dashboard/api-keys", {"name": f"auto-register-{int(time.time())}"}),
        ("POST", "/api/keys/create", {"name": f"auto-register-{int(time.time())}"}),
        ("GET", "/api/keys", None),
        ("GET", "/api/api-keys", None),
        ("GET", "/api/user/api-keys", None),
    ]
    attempts: list[dict[str, Any]] = []
    for method, path, payload in candidates:
        url = f"{SITE_URL.rstrip('/')}{path}"
        try:
            if method == "POST":
                response = session.post(url, json=payload, timeout=30)
            else:
                response = session.get(url, timeout=30)
            try:
                body = response.json()
            except Exception:
                body = {"raw": response.text[:2000]}
            api_key = _find_api_key(body)
            item = {"method": method, "path": path, "ok": response.ok, "status": response.status_code, "body": body, "api_key": api_key}
            attempts.append(item)
            if response.ok and api_key:
                return {"ok": True, "attempts": attempts, "api_key": api_key, "result": item}
        except Exception as exc:
            attempts.append({"method": method, "path": path, "ok": False, "error": repr(exc)})
    return {"ok": False, "attempts": attempts, "api_key": ""}


def _verify_api_key_http(api_key: str, *, proxy: str | None = None) -> dict[str, Any]:
    if not api_key:
        return {"ok": False, "reason": "missing_api_key"}
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get(
            f"{API_BASE}{VERIFY_PATH}",
            headers={"x-api-key": api_key},
            proxies=proxies,
            timeout=30,
        )
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:2000]}
        return {"ok": response.ok, "status": response.status_code, "path": VERIFY_PATH, "body": body}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "path": VERIFY_PATH}


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
    protocol_oauth: bool = True,
) -> dict:
    if (oauth_provider or "google").lower() != "google":
        raise RuntimeError("YepAPI 当前只支持 Google OAuth 自动化")

    with OAuthBrowser(proxy=proxy, headless=headless, chrome_user_data_dir=chrome_user_data_dir, chrome_cdp_url=chrome_cdp_url, log_fn=log_fn) as browser:
        page = browser.new_page()
        events: list[dict[str, Any]] = []
        api_response_bodies: list[dict[str, Any]] = []

        def on_request(req):
            url = str(req.url or "")
            if "yepapi.com" in url and ("/api/" in url or "/auth/" in url):
                events.append({"type": "request", "method": req.method, "url": url, "post": (req.post_data or "")[:1000]})

        def on_response(resp):
            try:
                url = str(resp.url or "")
                if "yepapi.com" not in url or ("/api/" not in url and "/auth/" not in url):
                    return
                try:
                    headers = dict(resp.headers or {})
                except Exception:
                    headers = {}
                ctype = str(headers.get("content-type") or "")
                body: Any = {}
                text_preview = ""
                if "json" in ctype.lower():
                    try:
                        body = resp.json()
                    except BaseException:
                        body = {}
                else:
                    try:
                        text_preview = (resp.text() or "")[:1200]
                        body = {"raw": text_preview}
                    except BaseException:
                        body = {}
                item = {"type": "response", "url": url, "status": resp.status, "headers": headers, "body": body}
                api_response_bodies.append(item)
                found = _find_api_key(body)
                events.append({
                    "type": "response",
                    "url": url,
                    "status": resp.status,
                    "content_type": ctype,
                    "location": headers.get("location") or headers.get("Location") or "",
                    "set_cookie": headers.get("set-cookie") or headers.get("Set-Cookie") or "",
                    "api_key_found": bool(found),
                    "body_preview": text_preview or (json.dumps(body, ensure_ascii=False)[:500] if body else ""),
                })
            except BaseException:
                # 页面/上下文关闭时 Playwright response body 读取会抛 CancelledError/TargetClosedError；
                # 这是监听器清理竞态，不应污染主 OAuth 诊断。
                return

        try:
            page.on("request", on_request)
            page.on("response", on_response)
            # OAuth 回调发生在新标签页，必须挂 context 级监听，否则会漏掉
            # /api/auth/callback/google 和 Better Auth 错误响应。
            browser.context.on("request", on_request)
            browser.context.on("response", on_response)
        except Exception:
            pass
        # YepAPI 的 auth 端点有独立 Cloudflare challenge。实测最稳定路径是：
        # 先进入可见 /login 页面并点过 CF，再点击页面上的 Continue with Google；
        # 不要先从首页协议 POST /api/auth/sign-in/social，否则会制造额外 challenge 状态。
        log_fn("[YepAPI] 打开登录页并处理 Cloudflare")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
        time.sleep(6)
        log_fn(f"[YepAPI] 登录页 URL: {page.url}")
        if not _wait_out_cloudflare(page, browser, timeout=min(timeout, 150), log_fn=log_fn):
            raise RuntimeError("YepAPI 登录页仍被 Cloudflare challenge，未执行 OAuth，当前环境无法发起 OAuth")
        _close_stale_yepapi_error_pages(browser)
        opened_google = False
        for oauth_attempt in range(2):
            if oauth_attempt:
                log_fn("[YepAPI] 检测到 OAuth transaction 失配，清理后重新发起")
                _clear_yepapi_oauth_transaction(browser)
                try:
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(2)
                except Exception:
                    pass
            if protocol_oauth:
                clicked_login = _click_google_login(page, log_fn=log_fn, protocol_first=True)
            else:
                clicked_login = _click_google_login_browser_only(page, log_fn=log_fn)
            if not clicked_login:
                diag_path = _dump_yepapi_login_diagnostics(page, log_fn=log_fn)
                raise RuntimeError(f"YepAPI 未找到 Google OAuth 登录入口; diagnostics={diag_path}")
            if _wait_google_page(browser, timeout=35, log_fn=log_fn):
                opened_google = True
                break
            if _has_restart_process_error(browser):
                continue
            # 协议发起可能被浏览器阻止时，再点一次页面按钮兜底。
            if try_click_provider_on_page(page, "google") and _wait_google_page(browser, timeout=25, log_fn=log_fn):
                opened_google = True
                break
        if not opened_google:
            raise RuntimeError("YepAPI 已发起 OAuth，但未打开 Google 授权页")
        log_fn("[YepAPI] 开始驱动 Google OAuth 表单/consent")
        google_result = drive_google_oauth(
            browser,
            email=email_hint,
            password=google_password,
            timeout=min(timeout, 220),
            log_fn=log_fn,
            stop_when=_login_done,
        )
        log_fn(f"[YepAPI] Google OAuth driver 返回: url={google_result.last_url} email={google_result.email_submitted} password={google_result.password_submitted} prompt={google_result.clicked_prompt}")
        deadline = time.time() + max(30, timeout)
        cookies = _cookie_map(browser)
        log_fn("[YepAPI] 开始轮询 Better Auth session")
        session_result = _get_session_http(cookies, proxy=proxy) if cookies else {"ok": False}
        last_session_log = 0.0
        while time.time() < deadline and not session_result.get("ok"):
            platform_error = _yepapi_oauth_error(browser)
            if platform_error:
                raise RuntimeError(f"YepAPI OAuth 回调失败: error={platform_error}; captured={events[-30:]}")
            rejected = _google_rejected_reason(browser)
            if rejected:
                raise RuntimeError(f"YepAPI Google OAuth 被 Google/域策略拒绝: {rejected}")
            cookies = _cookie_map(browser)
            if time.time() - last_session_log > 10:
                last_session_log = time.time()
                log_fn(f"[YepAPI] session 轮询: cookies={list(cookies.keys())[:12]} status={session_result.get('status')} data={str(session_result.get('data'))[:180]}")
            if cookies:
                session_result = _get_session_http(cookies, proxy=proxy)
                if session_result.get("ok"):
                    break
            time.sleep(1)
        if not session_result.get("ok"):
            snapshot = google_oauth_snapshot(browser)
            raise RuntimeError(f"YepAPI OAuth 后未拿到 Better Auth session: session={session_result}, snapshot={snapshot[:2]}")
        actual_email = finalize_oauth_email(_extract_email(session_result, email_hint), email_hint, "YepAPI")

        # 登录完成后先进入 dashboard/API-key 区域，让前端真实接口暴露出来；
        # 能从运行时响应拿到 key 就优先用真实响应，否则再回落到候选协议 endpoint。
        try:
            for p in browser.pages():
                if not p.is_closed() and "yepapi.com" in str(p.url or ""):
                    p.on("request", on_request)
                    p.on("response", on_response)
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
        except Exception as exc:
            log_fn(f"[YepAPI] dashboard 响应采集失败，继续协议候选接口: {exc!r}")

        captured_key = ""
        captured_item: dict[str, Any] = {}
        for item in reversed(api_response_bodies):
            captured_key = _find_api_key(item.get("body"))
            if captured_key:
                captured_item = item
                break

        if captured_key:
            key_result = {"ok": True, "source": "captured_dashboard_response", "api_key": captured_key, "result": captured_item}
        else:
            key_result = _try_key_endpoints(cookies, proxy=proxy)
        api_key = str(key_result.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError(f"YepAPI 已登录但未能通过真实响应或候选协议接口创建/获取 API Key: {key_result}; captured={events[-30:]}; responses={api_response_bodies[-10:]}")
        api_verification = _verify_api_key_http(api_key, proxy=proxy)
        if not api_verification.get("ok"):
            raise RuntimeError(f"YepAPI 已拿到 API Key 但验证失败: verify={api_verification}; key_result={key_result}")

    return {
        "email": actual_email,
        "api_key": api_key,
        "api_key_info": key_result.get("result") or {},
        "key_create_result": key_result,
        "api_verification": api_verification,
        "session": session_result.get("data") or {},
        "cookies": cookies,
        "cookie_header": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "captured_requests": events[-50:],
        "site_url": SITE_URL,
        "dashboard_url": DASHBOARD_URL,
        "api_base": API_BASE,
        "auth_header": "x-api-key",
    }
