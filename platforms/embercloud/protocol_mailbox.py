"""EmberCloud 协议邮箱注册 worker（全协议，无浏览器）。

链路：
1. Clerk Frontend API 协议注册（sign_up → prepare_verification → 邮箱 OTP →
   attempt_verification → 创建 session token），注册阶段带 Turnstile captcha 重试。
2. 拿 key（全协议）：
   a. 协议 GET /dashboard 首页（带 Clerk session cookie）触发新用户 $1 credit 入账
      —— 实测不预热会导致 chat 接口 402 Insufficient balance；credit 由 dashboard
      首页服务端渲染时后端自动入账，纯 GET 带 cookie 即可，无需执行前端 JS。
   b. 协议 GET /dashboard/keys 页面 HTML，定位该页专属 JS chunk，下载后从中提取
      Next.js Server Action 的 createApiKey action ID（部署更新会变，必须动态提取）。
   c. 协议 POST /dashboard/keys（Next-Action header + Clerk cookie + `["name"]` body），
      响应 RSC 流里含 fullKey=ek_live_...，直接取出明文 key。

实测全链路纯协议跑通：注册→收码→credit 入账→创建 key→chat 200 pong。
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import requests

from platforms.embercloud.core import (
    API_KEY_PATTERN,
    API_V1_BASE,
    DASHBOARD_URL,
    DEFAULT_USER_AGENT,
    KEYS_DASHBOARD_URL,
    MODELS_URL,
    SIGN_IN_URL,
    SITE_URL,
    TURNSTILE_SITEKEY,
    TURNSTILE_SITEKEY_INVISIBLE,
    EmberCloudClient,
    _extract_api_key,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# Next.js Server Action ID（42 位 hex）与 action 名的映射正则。
# chunk 里形如：createServerReference)("4085...8a77",o.callServer,void 0,o.findSourceMapURL,"createApiKey")
# （前面可能有 (0,o. 包裹，createServerReference 后紧跟 ) 再 ("ID"。）
SERVER_ACTION_RE = re.compile(
    r'createServerReference\)\("([0-9a-f]{40,50})"[^)]*?o\.findSourceMapURL,"([A-Za-z]+)"\)'
)

# 默认 createApiKey action ID（实地抓包），部署更新后由动态提取覆盖。
DEFAULT_CREATE_API_KEY_ACTION_ID = "4085a688fe9ba267a0255d3c9f7e0ced3173698a77"
# 默认 deployment ID，用于 x-deployment-id header；优先从 HTML 动态提取。
DEFAULT_DEPLOYMENT_ID = "dpl_CAtMvMugA2EUK6rRaektDyn1QnCe"

# /dashboard/keys 页面的 Next-Router-State-Tree（URL 编码），Server Action 请求需要。
KEYS_ROUTER_STATE_TREE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22dashboard%22%2C%7B%22children%22%3A%5B%22keys%22"
    "%2C%7B%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D"
    "%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)

# 未登录访问 /dashboard/keys 返回 404 页加载的共享 chunk 集合（用于减法定位 keys 页专属 chunk）。
_BASELINE_CHUNK_NAMES = frozenset(
    {
        "0d61b6be3eaf0a6f",
        "111069cce7be9740",
        "18194d95b6280d3d",
        "3437b8eaa45632a6",
        "81dd6720e45e92cc",
        "9e04cb3f6038f91c",
        "a6dad97d9634a72d",
        "b40fcc64a5336f02",
        "cb24ba999f017f08",
        "e5f1067d2607aa84",
        "ffeecbefd5804ad1",
        "turbopack-15066a061a8e6115",
    }
)


def _solver_accepts_clerk_mode(captcha_solver) -> bool:
    """CdpTurnstileSolver.solve_turnstile 支持 clerk_mode 强制走 Clerk widget 注入路径。

    远程打码（yescaptcha/2captcha）只认 url+sitekey，不接受该参数；用签名探测避免
    TypeError。EmberCloud 注册页是 Clerk 托管（Turnstile widget 由 Clerk 组件按需
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
        raise RuntimeError("EmberCloud 协议注册缺少验证码解决器")
    log_fn("EmberCloud Turnstile 打码中…")
    clerk_mode = _solver_accepts_clerk_mode(captcha_solver)
    if clerk_mode:
        log_fn("EmberCloud Turnstile: 走 CDP Clerk widget 注入路径")
    token = ""
    last_error: Exception | None = None
    for sitekey in (TURNSTILE_SITEKEY, TURNSTILE_SITEKEY_INVISIBLE):
        try:
            if clerk_mode:
                token = str(captcha_solver.solve_turnstile(SIGN_IN_URL, sitekey, clerk_mode=True) or "").strip()
            else:
                token = str(captcha_solver.solve_turnstile(SIGN_IN_URL, sitekey) or "").strip()
        except Exception as exc:
            last_error = exc
            token = ""
        if token:
            return token
    raise RuntimeError(
        f"EmberCloud Turnstile 打码返回空 token: {last_error or 'solver returned empty'}"
    )


def _extract_deployment_id(html: str) -> str:
    match = re.search(r'(dpl_[A-Za-z0-9]+)', html or "")
    return match.group(1) if match else DEFAULT_DEPLOYMENT_ID


def _extract_keys_page_chunks(html: str) -> list[str]:
    """从 /dashboard/keys 页面 HTML 里挑出该页专属 chunk（去掉 404 基线共享 chunk）。"""
    chunks = []
    for raw in re.findall(r'/_next/static/chunks/([A-Za-z0-9_-]+)\.js', html or ""):
        if raw in _BASELINE_CHUNK_NAMES:
            continue
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


class EmberCloudProtocolMailboxWorker:
    def __init__(
        self,
        *,
        client: EmberCloudClient | None = None,
        proxy: str | None = None,
        api_key_name: str = "auto-register",
        log_fn: Callable[[str], None] = print,
        **_kwargs,
    ) -> None:
        self.client = client or EmberCloudClient(proxy=proxy, log_fn=log_fn)
        self.api_key_name = api_key_name
        self.log = log_fn

    def _l(self, msg: str) -> None:
        self.log(f"[EmberCloud] {msg}")

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

    # --- dashboard 协议会话：基于 Clerk session cookie 的 requests.Session ---

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
                sess.cookies.set(name, value, domain=".embercloud.ai")
        if self.client._proxy_candidates:
            proxy = self.client._proxy_candidates[self.client._active_proxy_index]
            sess.proxies.update({"http": proxy, "https": proxy})
        return sess

    def _warmup_dashboard_credit(self, sess: requests.Session) -> str:
        """协议 GET dashboard 首页触发新用户 $1 credit 入账，并提取 deployment_id。

        实测：credit 由 dashboard 首页服务端渲染时后端自动入账，纯 GET 带 Clerk
        session cookie 即可；不预热会导致创建出的 key 调 chat 接口 402。
        """
        self._l("预热 dashboard 首页（触发新用户 credit 入账）")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        try:
            response = sess.get(DASHBOARD_URL, headers=headers, timeout=30, allow_redirects=True)
        except Exception as exc:
            raise RuntimeError(f"EmberCloud dashboard 首页预热失败: {exc}") from exc
        if "/sign-in" in (response.url or ""):
            raise RuntimeError("EmberCloud Clerk 会话未被 dashboard 接受（重定向回登录）")
        if not response.ok:
            raise RuntimeError(f"EmberCloud dashboard 首页预热异常 status={response.status_code}")
        deployment_id = _extract_deployment_id(response.text or "")
        self._l(f"deployment_id={deployment_id}")
        return deployment_id

    def _resolve_create_api_key_action_id(
        self,
        sess: requests.Session,
        deployment_id: str,
    ) -> str:
        """从 /dashboard/keys 页面专属 chunk 动态提取 createApiKey 的 Server Action ID。

        Next.js App Router 把 Server Action ID 编译进路由 chunk；部署更新后 ID 会变，
        故从登录后的 keys 页 HTML 定位专属 chunk，下载后用 createServerReference 正则
        按 action 名匹配。提取失败时回退到实地抓包得到的默认 ID。
        """
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        try:
            response = sess.get(KEYS_DASHBOARD_URL, headers=headers, timeout=30, allow_redirects=True)
        except Exception as exc:
            self._l(f"keys 页面抓取失败，回退默认 action ID: {exc}")
            return DEFAULT_CREATE_API_KEY_ACTION_ID
        if "/sign-in" in (response.url or ""):
            raise RuntimeError("EmberCloud Clerk 会话未被 dashboard 接受（重定向回登录）")

        chunk_names = _extract_keys_page_chunks(response.text or "")
        self._l(f"keys 页专属 chunk: {chunk_names or '(无)'}")
        for chunk_name in chunk_names:
            chunk_url = (
                f"https://www.embercloud.ai/_next/static/chunks/{chunk_name}.js"
                f"?dpl={deployment_id}"
            )
            try:
                chunk_resp = sess.get(chunk_url, headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=30)
            except Exception:
                continue
            if not chunk_resp.ok:
                continue
            action_id = _extract_create_api_key_action_id(chunk_resp.text or "")
            if action_id:
                self._l(f"从 chunk {chunk_name} 提取 createApiKey action ID: {action_id}")
                return action_id
        self._l(f"未能从 chunk 动态提取 createApiKey action ID，回退默认: {DEFAULT_CREATE_API_KEY_ACTION_ID}")
        return DEFAULT_CREATE_API_KEY_ACTION_ID

    def _protocol_create_api_key(
        self,
        sess: requests.Session,
        *,
        name: str,
        action_id: str,
        deployment_id: str,
    ) -> dict[str, Any]:
        """协议创建 key：POST /dashboard/keys 调 Next.js createApiKey Server Action。

        鉴权靠 Clerk session cookie（sess 已注入），不需要 Bearer；body 是 JSON 数组
        `["<name>"]`（Server Action 参数序列化）；响应是 RSC 流式，每行 `n:{...}`，
        第二行含 {"success":true,"fullKey":"ek_live_...","key":{...}}。
        """
        headers = {
            "Next-Action": action_id,
            "Content-Type": "text/plain;charset=UTF-8",
            "Accept": "text/x-component",
            "Referer": KEYS_DASHBOARD_URL,
            "Next-Router-State-Tree": KEYS_ROUTER_STATE_TREE,
            "x-deployment-id": deployment_id,
            "User-Agent": DEFAULT_USER_AGENT,
        }
        body = json.dumps([name])
        try:
            response = sess.post(KEYS_DASHBOARD_URL, headers=headers, data=body, timeout=30)
        except Exception as exc:
            raise RuntimeError(f"EmberCloud 协议创建 key 请求失败: {exc}") from exc

        text = response.text or ""
        if not response.ok:
            raise RuntimeError(
                f"EmberCloud 协议创建 key 失败 status={response.status_code}: {text[:400]}"
            )
        # RSC 流：先找 fullKey 字段（明文 key），再回退通用 ek_live_ 正则。
        match = re.search(r'"fullKey"\s*:\s*"(ek_live_[A-Za-z0-9_-]+)"', text)
        api_key = match.group(1) if match else _extract_api_key(text)
        if not api_key:
            raise RuntimeError(f"EmberCloud 协议创建 key 响应未含 ek_live_ 明文: {text[:400]}")
        # 顺便抓 key 元信息（id/name/maskedKey/createdAt）。
        key_info: dict[str, Any] = {}
        info_match = re.search(r'"key"\s*:\s*(\{[^}]*\})', text)
        if info_match:
            try:
                key_info = json.loads(info_match.group(1))
            except Exception:
                key_info = {"raw": info_match.group(1)}
        return {
            "ok": True,
            "api_key": api_key,
            "status": response.status_code,
            "key_info": key_info,
            "action_id": action_id,
            "deployment_id": deployment_id,
        }

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
            raise RuntimeError(f"EmberCloud 注册响应缺少 sign_up_id: {json.dumps(sign_up, ensure_ascii=False)[:400]}")

        self.client.prepare_email_verification(sign_up_id)
        otp = str(otp_callback() if otp_callback else "").strip()
        if not otp:
            raise RuntimeError("未收到 EmberCloud 邮箱验证码")

        verification = self.client.attempt_email_verification(sign_up_id, code=otp)
        session_id = self.client.extract_verification_session_id(verification)
        user_id = self.client.extract_verification_user_id(verification)
        if not session_id:
            raise RuntimeError("EmberCloud 邮箱验证完成但缺少 session_id")

        session_token_payload = self.client.create_session_token(session_id)
        access_token = ""
        for key in ("jwt", "token", "session_token"):
            access_token = str(session_token_payload.get(key) or "").strip()
            if access_token:
                break
        if not access_token:
            raise RuntimeError("EmberCloud 会话令牌接口未返回 jwt")

        auth_state = self.client.collect_auth_state(
            access_token=access_token,
            default_session_id=session_id,
            default_user_id=user_id,
        )

        # 拿 key：全协议。dashboard 会话 + 预热 credit + 动态提取 action ID + Server Action 创建。
        sess = self._dashboard_session(auth_state)
        deployment_id = self._warmup_dashboard_credit(sess)
        action_id = self._resolve_create_api_key_action_id(sess, deployment_id)
        key_create = self._protocol_create_api_key(
            sess,
            name=self.api_key_name,
            action_id=action_id,
            deployment_id=deployment_id,
        )
        api_key = str(key_create.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("EmberCloud 协议创建 key 未返回 ek_live_")

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
            "api_base": "https://api.embercloud.ai",
            "native_api_base": "https://api.embercloud.ai",
            "checked_at": _utcnow_iso(),
        }
