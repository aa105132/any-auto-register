"""Outlook/Hotmail 注册后 OAuth2 令牌获取（PKCE Authorization Code Flow）。

注册成功后浏览器已登录 Outlook，复用同一 page 直接跳到 Microsoft OAuth2 授权端点，
无需再输密码：authorize URL → 同意页（若出现）→ redirect 捕获 code → POST /token
换 refresh_token/access_token/expires_in。

scope 含 IMAP.AccessAsUser.All + offline_access，refresh_token 可长期刷新 access_token
走 IMAP XOAUTH2 收件（复用 core.base_mailbox.OutlookTokenMailbox）。

参考公开实现 get_token.py 的 PKCE 与 redirect 捕获逻辑，但用 requests 走代理换 token，
并把 client_id/redirect_url/scopes/租户全部参数化，支持内置公开 client_id 与用户自填。
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, quote

from platforms.outlook.constants import (
    DEFAULT_CLIENT_ID,
    DEFAULT_REDIRECT_URL,
    DEFAULT_SCOPES,
    EXTRA_CLIENT_ID,
    EXTRA_OAUTH_TIMEOUT,
    EXTRA_REDIRECT_URL,
    EXTRA_SCOPES,
    EXTRA_USE_CONSUMERS_TENANT,
    OUTLOOK_OAUTH_AUTHORIZE_URL,
    OUTLOOK_OAUTH_AUTHORIZE_URL_CONSUMERS,
    OUTLOOK_OAUTH_TOKEN_URL,
    OUTLOOK_OAUTH_TOKEN_URL_CONSUMERS,
    SEL_OAUTH_CONSENT_BUTTON,
    SEL_OAUTH_LOGINFMT,
    SEL_OAUTH_SIGNIN_BUTTON,
)


@dataclass
class OutlookTokens:
    refresh_token: str = ""
    access_token: str = ""
    expires_at: str = ""  # ISO8601 / unix 字符串，由调用方决定格式
    client_id: str = ""
    scope: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "client_id": self.client_id,
            "scope": self.scope,
        }


def generate_code_verifier(length: int = 128) -> str:
    """PKCE code_verifier：128 字符，字符集 A-Z a-z 0-9 - . _ ~。"""
    alphabet = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(alphabet) for _ in range(max(43, int(length))))


def generate_code_challenge(code_verifier: str) -> str:
    """PKCE code_challenge：S256 = base64url(sha256(verifier)) 去 padding。"""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _resolve_oauth_config(extra: dict | None) -> tuple[str, str, tuple[str, ...], str, str]:
    """从任务 extra 解析 OAuth 配置，回退到内置公开 client_id。

    返回 (client_id, redirect_url, scopes, authorize_url, token_url)。
    """
    extra = extra or {}
    client_id = str(extra.get(EXTRA_CLIENT_ID) or "").strip() or DEFAULT_CLIENT_ID
    redirect_url = str(extra.get(EXTRA_REDIRECT_URL) or "").strip() or DEFAULT_REDIRECT_URL
    raw_scopes = extra.get(EXTRA_SCOPES)
    if isinstance(raw_scopes, (list, tuple)) and raw_scopes:
        scopes = tuple(str(s).strip() for s in raw_scopes if str(s).strip())
    elif isinstance(raw_scopes, str) and raw_scopes.strip():
        scopes = tuple(s.strip() for s in raw_scopes.split() if s.strip())
    else:
        scopes = tuple(DEFAULT_SCOPES)
    use_consumers = str(extra.get(EXTRA_USE_CONSUMERS_TENANT, "")).strip().lower() in {"1", "true", "yes", "on"}
    if use_consumers:
        return client_id, redirect_url, scopes, OUTLOOK_OAUTH_AUTHORIZE_URL_CONSUMERS, OUTLOOK_OAUTH_TOKEN_URL_CONSUMERS
    return client_id, redirect_url, scopes, OUTLOOK_OAUTH_AUTHORIZE_URL, OUTLOOK_OAUTH_TOKEN_URL


def _build_authorize_url(client_id: str, redirect_url: str, scopes: tuple[str, ...],
                         code_challenge: str) -> str:
    scope_str = " ".join(scopes)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_url,
        "scope": scope_str,
        "response_mode": "query",
        # 注册后 page 已登录当前会话，用 consent 直接用当前会话 + 显示同意页（若需）。
        # 不用 login（强制重新登录，会清会话）、不用 select_account（多账号弹选择页）。
        "prompt": "consent",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
    return f"{OUTLOOK_OAUTH_AUTHORIZE_URL}?{query}"


def _handle_oauth_form(page, email: str, password: str, log_fn: Callable[[str], None]) -> None:
    """注册后通常已登录，但 authorize 带 prompt=login 会再出登录页。

    可能出现的页面（按顺序）：
      1. 邮箱页：填邮箱并提交
      2. 密码页：填密码并提交
      3. 同意页：点同意按钮
      4. 直接跳 redirect（已登录且无需同意）
    任一步失败都不抛错——已登录会话可能直接跳转 redirect，无需表单交互。

    注意：authorize 跳转可能有多次重定向，执行上下文会被销毁。这里先等页面稳定。
    """
    # 等页面稳定（多次重定向后 domcontentloaded）
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    # 1) 邮箱页：填邮箱（prompt=login 会强制重新登录）
    try:
        loc = page.locator(SEL_OAUTH_LOGINFMT)
        if loc.count() > 0:
            loc.first.fill(email, timeout=20000)
            page.locator(SEL_OAUTH_SIGNIN_BUTTON).first.click(timeout=7000)
            log_fn(f"[outlook-oauth] 已填邮箱并提交: {email}")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
    except Exception as exc:
        log_fn(f"[outlook-oauth] 填邮箱步骤跳过/失败（可能已登录）: {repr(exc)[:120]}")

    # 2) 密码页：填密码
    try:
        from platforms.outlook.constants import SEL_PASSWORD_INPUT
        pwd_loc = page.locator(SEL_PASSWORD_INPUT).first
        pwd_loc.wait_for(state="visible", timeout=10000)
        pwd_loc.fill(password, timeout=10000)
        page.locator(SEL_OAUTH_SIGNIN_BUTTON).first.click(timeout=7000)
        log_fn("[outlook-oauth] 已填密码并提交")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
    except Exception as exc:
        log_fn(f"[outlook-oauth] 填密码步骤跳过/失败（可能无密码页）: {repr(exc)[:120]}")

    # 3) "保持登录" 页（KMSI）：点"是"
    try:
        # KMSI 页的按钮 id 也是 idSIButton9
        kmsi = page.locator(SEL_OAUTH_SIGNIN_BUTTON)
        if kmsi.count() > 0:
            kmsi.first.click(timeout=5000)
            log_fn("[outlook-oauth] 已点击保持登录")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
    except Exception:
        pass

    # 4) 同意页
    try:
        consent = page.locator(SEL_OAUTH_CONSENT_BUTTON)
        consent.wait_for(state="visible", timeout=20000)
        consent.click(timeout=10000)
        log_fn("[outlook-oauth] 已点击同意按钮")
    except Exception:
        # 多数注册后场景不会出现同意页，跳过即可
        pass

    # 5) 兜底：检查是否还有"接受/同意/继续/是"按钮（不同 consent 页 selector 不同）
    for sel_text in ("接受", "同意", "继续", "是", "Accept", "Agree", "Continue", "Yes", "I agree"):
        try:
            btn = page.get_by_role("button", name=sel_text).first
            if btn.count() > 0:
                btn.click(timeout=5000)
                log_fn(f"[outlook-oauth] 已点击兜底同意按钮: {sel_text}")
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                break
        except Exception:
            continue

    # 6) 检查当前 URL 状态（debug 用）
    try:
        cur_url = page.url or ""
        log_fn(f"[outlook-oauth] 表单处理完，当前 URL: {cur_url[:120]}")
    except Exception:
        pass


def _exchange_code_for_tokens(
    *,
    token_url: str,
    client_id: str,
    code: str,
    redirect_url: str,
    code_verifier: str,
    scopes: tuple[str, ...],
    proxy: str | None,
) -> dict[str, Any]:
    """POST /token 换 refresh_token/access_token/expires_in。"""
    import requests
    proxies = {"http": proxy, "https": proxy} if proxy else None
    scope_str = " ".join(scopes)
    resp = requests.post(
        token_url,
        data={
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_url,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
            "scope": scope_str,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxies=proxies,
        timeout=20,
    )
    if resp.status_code != 200:
        # 打印响应体便于调试（400 时 Microsoft 会返回 error/error_description）
        try:
            err_body = resp.text[:500]
        except Exception:
            err_body = ""
        raise RuntimeError(
            f"Outlook OAuth 换 token HTTP {resp.status_code}: {err_body}"
        )
    data = resp.json()
    if "refresh_token" not in data:
        raise RuntimeError(f"Outlook OAuth 换 token 未返回 refresh_token: {str(data)[:200]}")
    return data


def get_outlook_tokens(
    page,
    email: str,
    *,
    password: str = "",
    extra: dict | None = None,
    proxy: str | None = None,
    timeout: int = 90,
    log_fn: Callable[[str], None] = print,
) -> OutlookTokens:
    """在已注册并登录的 page 上，跳到 Microsoft OAuth2 授权端点拿 refresh_token。

    流程：
      1. 生成 PKCE code_verifier/code_challenge
      2. page.goto(authorize_url) — 已登录会话直接跳转 redirect，或经登录/同意页
      3. 监听 redirect_url 带 code= 的请求，捕获 auth code
      4. POST /token 换 refresh_token/access_token/expires_in

    timeout 秒内未捕获 code 视为失败。
    password 用于 prompt=login 模式下的密码页填写。
    """
    client_id, redirect_url, scopes, authorize_url, token_url = _resolve_oauth_config(extra)
    scope_str = " ".join(scopes)

    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    auth_url = _build_authorize_url(client_id, redirect_url, scopes, code_challenge)
    log_fn(f"[outlook-oauth] 跳转授权端点 client_id={client_id[:8]}... scope={scope_str[:60]}")

    captured_url: str | None = None

    def _on_request(request):
        nonlocal captured_url
        try:
            req_url = request.url or ""
        except Exception:
            req_url = ""
        if redirect_url in req_url and "code=" in req_url:
            captured_url = req_url

    page.on("request", _on_request)
    try:
        try:
            page.goto(auth_url, timeout=30000, wait_until="domcontentloaded")
        except Exception as exc:
            log_fn(f"[outlook-oauth] goto authorize 异常（继续等 redirect）: {repr(exc)[:120]}")

        _handle_oauth_form(page, email, password, log_fn)

        deadline = time.time() + max(15, int(timeout))
        while time.time() < deadline:
            if captured_url:
                break
            # 已登录会话可能在 goto 后立即跳 redirect，但 page.on("request") 在
            # 某些 chromium 上对顶层导航的捕获有延迟；这里额外直接读 page.url 兜底。
            try:
                cur = page.url or ""
            except Exception:
                cur = ""
            if redirect_url in cur and "code=" in cur:
                captured_url = cur
                break
            page.wait_for_timeout(100)
    finally:
        try:
            page.remove_listener("request", _on_request)
        except Exception:
            pass

    if not captured_url or "code=" not in captured_url:
        raise RuntimeError(
            f"Outlook OAuth 未捕获到 auth code (redirect_url={redirect_url}, "
            f"timeout={timeout}s, last_url={(page.url or '')[:120]})"
        )

    # 提取 code
    query_idx = captured_url.find("?")
    if query_idx < 0:
        raise RuntimeError(f"Outlook OAuth redirect 缺少 query: {captured_url[:160]}")
    qs = parse_qs(captured_url[query_idx + 1:])
    code_values = qs.get("code") or []
    if not code_values:
        raise RuntimeError(f"Outlook OAuth redirect 缺少 code 参数: {captured_url[:160]}")
    auth_code = code_values[0]
    log_fn("[outlook-oauth] 已捕获 auth code，换 token...")

    data = _exchange_code_for_tokens(
        token_url=token_url,
        client_id=client_id,
        code=auth_code,
        redirect_url=redirect_url,
        code_verifier=code_verifier,
        scopes=scopes,
        proxy=proxy,
    )
    refresh_token = str(data.get("refresh_token") or "")
    access_token = str(data.get("access_token") or "")
    expires_in = int(data.get("expires_in") or 0)
    expires_at = str(int(time.time()) + expires_in) if expires_in else ""
    log_fn(
        f"[outlook-oauth] 换 token 成功: refresh_token={'yes' if refresh_token else 'no'} "
        f"access_token={'yes' if access_token else 'no'} expires_in={expires_in}"
    )
    return OutlookTokens(
        refresh_token=refresh_token,
        access_token=access_token,
        expires_at=expires_at,
        client_id=client_id,
        scope=scope_str,
    )
