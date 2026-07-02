"""ClickUp 平台核心：reCAPTCHA v3 注册 + session+JWT + Brain AI SSE（2api 包装）。

clickup 是项目管理平台（Angular SPA），自有 session 认证。
注册：POST {shardBase}/user/v1/user{username,email,password,recaptcha,recaptchaV3,...} → 自动调 login。
登录：POST {shardBase}/auth/v1/login{email,password,recaptcha} withCredentials → session cookie。
workspace JWT：POST {apiUrlWorkspaceV3Service}/workspace-auth/generate-token/{workspaceId} → cu_jwt。
reCAPTCHA v3 action=signup sitekey 6Lf6D0YoAAAAAEgVBxwLwC_gxFaDBPyYZX19ocU1（v2 fallback 6Lfj8kUo...）。

2api：SSE content-assistant POST {apiUrlAiService}/ai/v1/... 或 {apiUrlChatService}/chat/v1/...
  - type=refinement payload{content,chatId,messageId}（最接近 OpenAI chat，workspace/task scoped）
  - GraphQL aiRequest(type:CREATE_SUBTASKS) task-scoped
  - 流式 SSE {data:{content,status:header|delta,aiResultID,responseType}}
headers: Authorization Bearer cu_jwt + Cookie(session)
公开 API（pk_/OAuth）无 Brain 端点。AI credits 门控（Brain MAX 付费）。SSE URL 动态构造需 Playwright 抓。
"""
from __future__ import annotations

import time
from typing import Any

import requests

SITE_URL = "https://clickup.com"
APP_URL = "https://app.clickup.com"
AUTH_HOST = "https://app-auth.clickup.com"  # env appAuthUrl
# shard base 运行时注入，prod 用 app.clickup.com
SHARD_BASE = APP_URL
USER_CREATE_API = f"{SHARD_BASE}/user/v1/user"
LOGIN_API = f"{SHARD_BASE}/auth/v1/login"
WORKSPACE_TOKEN_API = f"{SHARD_BASE}/workspace-auth/generate-token"  # /{workspaceId}

RECAPTCHA_V3_SITEKEY = "6Lf6D0YoAAAAAEgVBxwLwC_gxFaDBPyYZX19ocU1"
RECAPTCHA_V2_SITEKEY = "6Lfj8kUoAAAAAPQRzgovYfo4AhDwOpOOqc_4SvRk"

# AI 端点（运行时 apiUrlAiService/apiUrlChatService，base shard）
AI_SERVICE_BASE = f"{SHARD_BASE}/ai/v1"
CHAT_SERVICE_BASE = f"{SHARD_BASE}/chat/v1"
GRAPHQL_API = f"{APP_URL}/graphql"
GRAPHQL_WS = "wss://ws.clickup.com/ws"

CLICKUP_MODELS = [
    "brain",  # 内部 ClickUp Brain，无 user-facing model id
]
DEFAULT_MODEL = "brain"
FREE_MODELS = ["brain"]

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [clickup] {msg}", flush=True)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] [clickup] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


class ClickUpClient:
    """ClickUp HTTP 客户端：reCAPTCHA v3 注册 + session + workspace JWT + Brain SSE（待补）。

    proxy 走任务代理。reCAPTCHA v3 token 需 solver 解（action=signup）。
    """

    def __init__(self, *, proxy: str | None = None, log_fn=print) -> None:
        self.proxy = proxy
        self.log = log_fn or log
        self.session = requests.Session()
        self.session.trust_env = False
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": APP_URL,
                "Referer": f"{APP_URL}/signup",
                "User-Agent": CHROME_UA,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def create_user(
        self,
        *,
        username: str,
        email: str,
        password: str,
        recaptcha_v3: str = "",
        recaptcha_v2: str = "",
    ) -> dict[str, Any]:
        """注册：POST /user/v1/user{username,email,password,recaptcha,recaptchaV3,...}。

        成功后自动调 login。withCredentials → session cookie。
        reCAPTCHA v3 action=signup token 必填（v2 fallback 可选）。
        """
        body = {
            "username": username,
            "email": str(email or "").strip().lower(),
            "password": password,
            "eu_marketing": None,
            "check_for_compromised_password": True,
            "dashboard": 6,
            "global_font_support": True,
            "utms": {},
            "recaptcha": recaptcha_v2 or "",
            "recaptchaV3": recaptcha_v3 or "",
            "token": "",
            "sso_provider": "",
            "sso_token": "",
        }
        resp = self.session.post(USER_CREATE_API, json=body, timeout=30)
        data = _json_or_text(resp)
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200
        data["cookies"] = {c.name: c.value for c in self.session.cookies}
        return data

    def login(self, *, email: str, password: str, recaptcha_v3: str = "") -> dict[str, Any]:
        """登录：POST /auth/v1/login{email,password,recaptcha} withCredentials → session cookie。

        处理 2FA（twofa/twofa_totp/text_enabled）。
        """
        body = {"email": str(email or "").strip().lower(), "password": password, "recaptcha": recaptcha_v3 or ""}
        resp = self.session.post(LOGIN_API, json=body, timeout=30)
        data = _json_or_text(resp)
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200
        data["cookies"] = {c.name: c.value for c in self.session.cookies}
        return data

    def generate_workspace_token(self, *, workspace_id: str) -> dict[str, Any]:
        """POST /workspace-auth/generate-token/{workspaceId} → cu_jwt（per-workspace JWT）。"""
        resp = self.session.post(f"{WORKSPACE_TOKEN_API}/{workspace_id}", json={}, timeout=30)
        data = _json_or_text(resp)
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200
        return data

    # ===== Brain AI SSE（2api 核心，待 Playwright 抓动态 URL）=====

    def chat(
        self,
        *,
        cu_jwt: str,
        chat_id: str,
        content: str,
        message_id: str = "",
        stream: bool = False,
    ) -> dict[str, Any]:
        """Brain SSE chat：POST {apiUrlAiService}/ai/v1/... type=refinement。待抓动态 URL。

        TODO：精确 SSE POST url（运行时构造）+ 完整 body schema + SSE 帧解析。
        需 Playwright 登录抓 fetchEventSource 调用。
        """
        raise NotImplementedError(
            "clickup chat 待抓 Brain SSE content-assistant 动态 URL + body"
            "（需登录后 Playwright 抓 fetchEventSource 调用）"
        )


def account_preview(token: str) -> str:
    raw = str(token or "")
    if len(raw) <= 16:
        return raw
    return f"{raw[:8]}...{raw[-6:]}"
