"""AIHubMix 协议邮箱注册 worker（全协议，无浏览器）。

链路：
1. Clerk Frontend API 协议注册（sign_up → prepare_verification → 邮箱 OTP →
   attempt_verification → 创建 session token），注册阶段带 Turnstile captcha 重试。
2. 拿 key（全协议）：
   a. 协议 GET console.aihubmix.com 首页（带 Clerk session cookie）预热会话，
      提取 deployment_id。
   b. 协议 GET /token 页面 HTML，定位该页专属 JS chunk，下载后从中提取
      Next.js Server Action 的 createApiKey action ID（部署更新会变，必须动态提取）。
   c. 协议 POST /token（Next-Action header + Clerk cookie + `["name"]` body），
      响应 RSC 流里含 fullKey=sk-...，直接取出明文 key。

实测 Clerk 注册协议链路已摸通（见 aihubmix 注册流程调研）；aihubmix console 的
Next.js 部署结构未知，用通用正则动态提取 action ID + 浏览器 DOM 兜底（browser_register）。

已知限制：
- 纯 HTTP 协议路径的 Turnstile token：Clerk 的 captcha token 需绑定 client session，
  裸 API 拿的远程 token 可能被 `captcha_invalid` 拒绝。CDP `clerk_mode` solver 或
  真实浏览器路径更可靠。
- console key 创建路由：aihubmix console 的 Next.js Server Action ID / router state
  tree / baseline chunks 未知（无实账号抓包），用通用正则动态提取 + 浏览器 DOM 兜底。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

from platforms.aihubmix.core import (
    API_KEY_PATTERN,
    CONSOLE_URL,
    DEFAULT_USER_AGENT,
    DASHBOARD_URL,
    KEYS_DASHBOARD_URL,
    MODELS_URL,
    SIGN_UP_URL,
    SITE_URL,
    TURNSTILE_SITEKEY,
    TURNSTILE_SITEKEY_INVISIBLE,
    AIHubMixClient,
    _extract_api_key,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Next.js Server Action ID（40+ 位 hex）与 action 名的映射正则。
# chunk 里形如：createServerReference)("4085...8a77",o.callServer,void 0,o.findSourceMapURL,"createApiKey")
# （前面可能有 (0,o. 包裹，createServerReference 后紧跟 ) 再 ("ID"。）
# 这是 Next.js App Router 把 Server Action ID 编译进路由 chunk 的通用模式，
# 不绑定 aihubmix 专属 baseline chunk（aihubmix console 部署结构未知，动态提取更稳）。
SERVER_ACTION_RE = re.compile(
    r'createServerReference\)\("([0-9a-f]{40,50})"[^)]*?o\.findSourceMapURL,"([A-Za-z]+)"\)'
)

# 默认 createApiKey action ID（未知，空字符串强制动态提取；提取失败时 fetch 会抛错，
# 由 browser_register._fetch_key_via_browser 兜底）。
DEFAULT_CREATE_API_KEY_ACTION_ID = ""

# 默认 deployment ID（未知，空字符串强制动态提取）。
DEFAULT_DEPLOYMENT_ID = ""

# /token 页面的 Next-Router-State-Tree（URL 编码），Server Action 请求需要。
# aihubmix console 路由结构未知，这里给一个通用占位；fetch 会先尝试从 HTML 动态提取，
# 提取失败时用此值。空字符串表示不发送该 header（部分 Next.js 部署不强制要求）。
KEYS_ROUTER_STATE_TREE = ""


def _solver_accepts_clerk_mode(captcha_solver) -> bool:
    """CdpTurnstileSolver.solve_turnstile 支持 clerk_mode 强制走 Clerk widget 注入路径。

    远程打码（yescaptcha/2captcha）只认 url+sitekey，不接受该参数；用签名探测避免
    TypeError。AIHubMix 注册页是 Clerk 托管（Turnstile widget 由 Clerk 组件按需
    渲染），CDP 普通路径点不到 widget，必须走 clerk 路径（主动注入 turnstile script）。
    """
    import inspect

    try:
        params = inspect.signature(captcha_solver.solve_turnstile).parameters
    except (ValueError, TypeError):
        return False
    return "clerk_mode" in params


def _solve_turnstile(captcha_solver, *, log_fn: Callable[[str], None]) -> str:
    if captcha_solver is None:
        raise RuntimeError("AIHubMix 协议注册缺少验证码解决器")
    log_fn("AIHubMix Turnstile 打码中…")
    clerk_mode = _solver_accepts_clerk_mode(captcha_solver)
    if clerk_mode:
        log_fn("AIHubMix Turnstile: 走 CDP Clerk widget 注入路径")
    token = ""
    last_error: Exception | None = None
    for sitekey in (TURNSTILE_SITEKEY, TURNSTILE_SITEKEY_INVISIBLE):
        try:
            if clerk_mode:
                token = str(captcha_solver.solve_turnstile(SIGN_UP_URL, sitekey, clerk_mode=True) or "").strip()
            else:
                token = str(captcha_solver.solve_turnstile(SIGN_UP_URL, sitekey) or "").strip()
        except Exception as exc:
            last_error = exc
            token = ""
        if token:
            return token
    raise RuntimeError(
        f"AIHubMix Turnstile 打码返回空 token: {last_error or 'solver returned empty'}"
    )


def _extract_deployment_id(html: str) -> str:
    """从 HTML 提取 Next.js deployment ID（dpl_ 前缀）。"""
    match = re.search(r'(dpl_[A-Za-z0-9]+)', html or "")
    return match.group(1) if match else DEFAULT_DEPLOYMENT_ID


def _extract_keys_page_chunks(html: str) -> list[str]:
    """从 /token 页面 HTML 里提取所有 Next.js chunk 名（去重）。

    aihubmix console 部署结构未知，不像 embercloud 有已知 baseline chunk 集合可减。
    这里直接返回所有 chunk，由 _resolve_create_api_key_action_id 逐个下载用正则匹配
    createApiKey action 名。chunk 数量通常 10-30 个，每个下载后正则扫描很快。
    """
    chunks = []
    for raw in re.findall(r'/_next/static/chunks/([A-Za-z0-9_-]+)\.js', html or ""):
        if raw not in chunks:
            chunks.append(raw)
    return chunks


def _extract_create_api_key_action_id(chunk_text: str) -> str:
    """从 JS chunk 文本里提取 createApiKey 的 Server Action ID。"""
    for match in SERVER_ACTION_RE.finditer(chunk_text or ""):
        action_name = match.group(2)
        if action_name == "createApiKey":
            return match.group(1)
    return ""


def _extract_router_state_tree(html: str) -> str:
    """从 keys 页 HTML 动态提取 Next-Router-State-Tree（URL 编码）。

    Next.js App Router 把 router state 编进 RSC payload 或 __NEXT_DATA__ seed。
    aihubmix console 部署结构未知，这里尝试两种常见模式：
    1. <script>self.__next_f.push([1,"..."]) 里含 "children":["token"...] 的 URL 编码树
    2. __NEXT_DATA__ script JSON 里的 router.tree

    提取失败返回空字符串（fetch 不发送该 header，部分部署不强制要求）。
    """
    if not html:
        return ""
    # 模式 1：__next_f.push 里含 "token" 路由的编码树片段
    for m in re.finditer(r'self\.__next_f\.push\(\[1,"([^"]+)"\]', html):
        blob = m.group(1)
        # 解 URL 转义（\x 系列已被 string 转义，这里只处理 % 编码）
        if "token" in blob and "children" in blob:
            # blob 已是 URL 编码形式（%5B%22...），直接用
            if "%" in blob:
                return blob
    # 模式 2：__NEXT_DATA__ JSON 里的 router.tree（未 URL 编码，需手动编码）
    nd = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>([^<]+)</script>', html)
    if nd:
        try:
            data = json.loads(nd.group(1))
            tree = (data.get("props") or {}).get("router") or data.get("router") or {}
            if isinstance(tree, dict) and tree:
                from urllib.parse import quote
                return quote(json.dumps(tree), safe="")
        except Exception:
            pass
    return ""


class AIHubMixProtocolRegister:
    """AIHubMix 协议邮箱注册 worker（全协议）。"""

    def __init__(
        self,
        *,
        client: AIHubMixClient | None = None,
        proxy: str | None = None,
        api_key_name: str = "auto-register",
        log_fn: Callable[[str], None] = print,
        **_kwargs,
    ) -> None:
        self.client = client or AIHubMixClient(proxy=proxy, log_fn=log_fn)
        self.api_key_name = api_key_name
        self.log = log_fn

    def _l(self, msg: str) -> None:
        self.log(f"[AIHubMix] {msg}")

    def _create_sign_up_with_captcha(
        self,
        *,
        email: str,
        password: str,
        captcha_solver,
    ) -> dict[str, Any]:
        try:
            return self.client.create_sign_up(email=email, password=password, captcha_token=None)
        except RuntimeError as exc:
            if not self.client._needs_captcha_retry(None, exc):
                raise
            if captcha_solver is None:
                raise
            token = _solve_turnstile(captcha_solver, log_fn=self.log)
            return self.client.create_sign_up(
                email=email,
                password=password,
                captcha_token=token,
            )

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Optional[Callable[[], str]] = None,
        captcha_solver=None,
    ) -> dict[str, Any]:
        self.client.init_clerk_client()
        sign_up = self._create_sign_up_with_captcha(
            email=email,
            password=password,
            captcha_solver=captcha_solver,
        )
        sign_up_id = self.client.extract_sign_up_id(sign_up)
        if not sign_up_id:
            raise RuntimeError(f"AIHubMix 注册响应缺少 sign_up_id: {json.dumps(sign_up, ensure_ascii=False)[:400]}")

        self.client.prepare_email_verification(sign_up_id)
        otp = str(otp_callback() if otp_callback else "").strip()
        if not otp:
            raise RuntimeError("未收到 AIHubMix 邮箱验证码")

        verification = self.client.attempt_email_verification(sign_up_id, code=otp)
        session_id = self.client.extract_verification_session_id(verification)
        user_id = self.client.extract_verification_user_id(verification)
        if not session_id:
            raise RuntimeError("AIHubMix 邮箱验证完成但缺少 session_id")

        session_token_payload = self.client.create_session_token(session_id)
        access_token = ""
        for key in ("jwt", "token", "session_token"):
            access_token = str(session_token_payload.get(key) or "").strip()
            if access_token:
                break
        if not access_token:
            raise RuntimeError("AIHubMix 会话令牌接口未返回 jwt")

        auth_state = self.client.collect_auth_state(
            access_token=access_token,
            default_session_id=session_id,
            default_user_id=user_id,
        )

        # 拿 key：全协议。console 会话 + 预热 + 动态提取 action ID + Server Action 创建。
        key_worker = _KeyFetchWorker(proxy=self.client._proxy_candidates[self.client._active_proxy_index] if self.client._proxy_candidates else None, log_fn=self.log)
        key_create = key_worker.fetch(auth_state, name=self.api_key_name)
        api_key = str(key_create.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("AIHubMix 协议创建 key 未返回 sk-")

        verification_info = self.client.verify_api_key(api_key)
        try:
            models = self.client.list_models_raw(api_key)
        except Exception:
            models = {}

        return {
            "email": email,
            "password": password,
            "user_id": auth_state.get("user_id") or user_id,
            "session_id": auth_state.get("session_id") or session_id,
            "access_token": auth_state.get("access_token"),
            "refresh_token": auth_state.get("refresh_token"),
            "refresh_token_source": auth_state.get("refresh_token_source"),
            "session_token": auth_state.get("session_token"),
            "client_id": auth_state.get("client_id"),
            "client_cookie": auth_state.get("client_cookie"),
            "session_cookie": auth_state.get("session_cookie"),
            "api_key": api_key,
            "api_key_name": self.api_key_name,
            "api_key_source": "protocol",
            "key_create_result": key_create,
            "api_verification": {"ok": verification_info},
            "models": models if isinstance(models, dict) else {},
            "site_url": SITE_URL,
            "dashboard_url": DASHBOARD_URL,
            "api_base": "https://aihubmix.com/v1",
            "checked_at": _utcnow_iso(),
        }


class _KeyFetchWorker:
    """协议拿 key 子流程：console 预热 + 动态提取 action ID + Server Action 创建。

    aihubmix console 的 Next.js 部署结构未知（无实账号抓包），用通用正则动态提取
    action ID + router state tree。提取失败时 fetch 抛错，由 browser_register 的
    _fetch_key_via_browser 浏览器 DOM 兜底。
    """

    def __init__(self, *, proxy: str | None = None, log_fn: Callable[[str], None] | None = None):
        self._proxy = proxy
        self._log = log_fn or (lambda m: None)

    def _l(self, msg: str) -> None:
        self._log(f"[aihubmix:key] {msg}")

    def _dashboard_session(self, auth_state: dict[str, str]) -> requests.Session:
        sess = requests.Session()
        sess.trust_env = False
        for name, value in (
            ("__client", auth_state.get("client_cookie") or ""),
            ("__session", auth_state.get("session_cookie") or auth_state.get("access_token") or ""),
            ("__client_uat", "1"),
        ):
            value = str(value or "").strip()
            if value:
                sess.cookies.set(name, value, domain=".aihubmix.com")
                sess.cookies.set(name, value, domain="console.aihubmix.com")
        if self._proxy:
            sess.proxies.update({"http": self._proxy, "https": self._proxy})
        return sess

    def fetch(self, auth_state: dict[str, str], *, name: str) -> dict[str, Any]:
        sess = self._dashboard_session(auth_state)

        # 1. 预热 console 首页（提取 deployment_id）
        self._l("预热 console 首页")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        try:
            resp = sess.get(DASHBOARD_URL, headers=headers, timeout=30, allow_redirects=True)
        except Exception as exc:
            raise RuntimeError(f"AIHubMix console 首页预热失败: {exc}") from exc
        if "/sign-in" in (resp.url or "") or "/sign-up" in (resp.url or ""):
            raise RuntimeError("AIHubMix Clerk 会话未被 console 接受（重定向回登录）")
        if not resp.ok:
            raise RuntimeError(f"AIHubMix console 首页预热异常 status={resp.status_code}")
        deployment_id = _extract_deployment_id(resp.text or "")
        self._l(f"deployment_id={deployment_id or '(未提取到)'}")

        # 2. 动态提取 createApiKey action ID + router state tree
        try:
            resp_keys = sess.get(KEYS_DASHBOARD_URL, headers=headers, timeout=30, allow_redirects=True)
        except Exception as exc:
            raise RuntimeError(f"AIHubMix /token 页抓取失败: {exc}") from exc
        if "/sign-in" in (resp_keys.url or "") or "/sign-up" in (resp_keys.url or ""):
            raise RuntimeError("AIHubMix Clerk 会话未被 console 接受")
        keys_html = resp_keys.text or ""
        router_state_tree = _extract_router_state_tree(keys_html) or KEYS_ROUTER_STATE_TREE
        chunk_names = _extract_keys_page_chunks(keys_html)
        self._l(f"/token 页 chunk: {chunk_names or '(无)'}")
        action_id = DEFAULT_CREATE_API_KEY_ACTION_ID
        for chunk_name in chunk_names:
            chunk_url = f"https://console.aihubmix.com/_next/static/chunks/{chunk_name}.js"
            if deployment_id:
                chunk_url += f"?dpl={deployment_id}"
            try:
                chunk_resp = sess.get(chunk_url, headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=30)
            except Exception:
                continue
            if not chunk_resp.ok:
                continue
            aid = _extract_create_api_key_action_id(chunk_resp.text or "")
            if aid:
                action_id = aid
                self._l(f"从 chunk {chunk_name} 提取 createApiKey action ID: {aid}")
                break
        if not action_id:
            raise RuntimeError(
                "AIHubMix 未能动态提取 createApiKey action ID（aihubmix console 部署结构未知，"
                "需用 browser_register._fetch_key_via_browser 浏览器 DOM 兜底）"
            )

        # 3. POST Server Action 创建 key
        sa_headers = {
            "Next-Action": action_id,
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "text/x-component",
            "Referer": KEYS_DASHBOARD_URL,
            "x-deployment-id": deployment_id,
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if router_state_tree:
            sa_headers["Next-Router-State-Tree"] = router_state_tree
        body = json.dumps([name])
        try:
            r = sess.post(KEYS_DASHBOARD_URL, headers=sa_headers, data=body, timeout=30)
        except Exception as exc:
            raise RuntimeError(f"AIHubMix 协议创建 key 请求失败: {exc}") from exc
        text = r.text or ""
        if not r.ok:
            raise RuntimeError(f"AIHubMix 创建 key 失败 status={r.status_code}: {text[:400]}")
        # RSC 流：先找 fullKey 字段（明文 key），再回退通用 sk- 正则。
        match = re.search(r'"fullKey"\s*:\s*"(sk-[A-Za-z0-9_-]+)"', text)
        api_key = match.group(1) if match else _extract_api_key(text)
        if not api_key:
            raise RuntimeError(f"AIHubMix 协议创建 key 响应未含 sk_ 明文: {text[:400]}")
        # 顺便抓 key 元信息（id/name/maskedKey/createdAt）。
        key_info: dict[str, Any] = {}
        info_match = re.search(r'"key"\s*:\s*(\{[^}]*\})', text)
        if info_match:
            try:
                key_info = json.loads(info_match.group(1))
            except Exception:
                key_info = {"raw": info_match.group(1)}
        self._l(f"协议创建 key 成功: {api_key[:8]}...{api_key[-4:]}")
        return {
            "ok": True,
            "api_key": api_key,
            "status": r.status_code,
            "key_info": key_info,
            "action_id": action_id,
            "deployment_id": deployment_id,
        }
