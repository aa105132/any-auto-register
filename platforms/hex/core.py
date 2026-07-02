"""Hex 平台核心：magic-link 注册 + GraphQL APQ thread LLM（2api 包装）。

hex 是数据科学平台（Remix + Apollo Client），认证 app.hex.tech 自有 magic-link。
注册：POST app.hex.tech/auth/magic/signup{email,name} → magic link 邮件 → /auth/magic/callback → session。
无密码无 captcha（185 bundle 确认）。login 同模式 POST /auth-all/magic{email,orgId,embedded}。

2api：GraphQL app.hex.tech/graphql Apollo APQ（persistedQuery sha256）
  - createAskProjectAndThread sha=01cee6254e832c...
  - AgentChatMessage sha=4a7b27f4a1cd339547160421e71fd29e5b28e8d6c46caa3ca4fd78825f063ec0
  - 流式 subscription app.hex.tech/subscriptions (AgentChatUpdatedThreadById)
  - 模型 claude-fable-5/haiku-4.5/opus-4.6/4.7/4.8/sonnet-4.5/4.6
headers: Cookie(session)+x-csrf-safe+x-hex-user-bearer-token+x-org-id+x-agent-type
致命：hex agent 是数据 agent（写 SQL 查 warehouse 返回 chart/table），非通用文本 LLM；新号无 warehouse 无法跑 thread。
Public API（hxtp_/hxtw_ Bearer）不暴露 chat/threads。
"""
from __future__ import annotations

import time
from typing import Any

import requests

SITE_URL = "https://hex.tech"
APP_URL = "https://app.hex.tech"
AUTH_MAGIC_SIGNUP = f"{APP_URL}/auth/magic/signup"
AUTH_MAGIC_CALLBACK = f"{APP_URL}/auth/magic/callback"
AUTH_ALL_MAGIC = f"{APP_URL}/auth-all/magic"
GRAPHQL_API = f"{APP_URL}/graphql"
SUBSCRIPTIONS_API = f"{APP_URL}/subscriptions"
MCP_API = f"{APP_URL}/mcp"

# Apollo APQ persisted query hashes（workflow 抓的 operationName→sha256）
APQ_HASHES = {
    "createAskProjectAndThread": "01cee6254e832c",
    "AgentChatMessage": "4a7b27f4a1cd339547160421e71fd29e5b28e8d6c46caa3ca4fd78825f063ec0",
    "SemanticAuthoringAgentChatMessage": "18108369b8035b8cd24fd788f650452c4efb9e959f24576e4771b13fd307033c",
    "AgentChatThreadById": "d96f63cc872a5c927dcc7d7cb25d0802b7e7b656f25d8a31d9c3f76cd53fd4f9",
    "GetSelectedAgentChatThread": "09af13bf89d200b7a821ab6ea49c2890078f4ba9f541c120e48b4cd259b1217e",
    "AgentChatMessagesByThreadId": "6e264b435a7320260d596a05cdd7e7fca1ed2db80c3b06a88fa178688a96bde2",
    "GetAgentChatThreadsList": "62ff68fa5b253e35cfddc1019c2187356c655bda6412d98b82516e0f041ee125",
    "GetAgentPickerModelsForOrg": "747de32b9d4e102768d9e3602d8463e4a85c3291b602b2925805ec88e564c907",
    "CancelQueuedPrompt": "3d097cd3814dd04d2a579f0fd0e18ed147933c53f6ca13159bd2289190b68b61",
    "DeleteAgentChatThread": "e02d42b86bb86d1efd2de5d511a4b151b9c6eade0fd285a3835987fa5da68059",
}

HEX_MODELS = [
    "claude-fable-5", "claude-haiku-4-5-20251001",
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-sonnet-4-5-20250929", "claude-sonnet-4-6",
]
DEFAULT_MODEL = "claude-sonnet-4-6"
FREE_MODELS = list(HEX_MODELS)

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [hex] {msg}", flush=True)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] [hex] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


class HexClient:
    """Hex HTTP 客户端：magic-link 注册 + session + GraphQL thread LLM（待补）。

    proxy 走任务代理。magic link 由 mailbox 收（wait_for_link）。
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

    def magic_signup(self, *, email: str, name: str = "Auto Register") -> dict[str, Any]:
        """注册：POST /auth/magic/signup{email,name} → {code,error,redirectTo,success}。

        发 magic link 邮件，用户点链接 → /auth/magic/callback → session cookie。
        无密码无 captcha。
        """
        body = {"email": str(email or "").strip().lower(), "name": name}
        resp = self.session.post(AUTH_MAGIC_SIGNUP, json=body, timeout=30)
        data = _json_or_text(resp)
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200 and bool(data.get("success", True))
        return data

    def complete_magic_callback(self, callback_url: str) -> dict[str, Any]:
        """GET magic link 回调 URL → set session cookie。

        callback_url 是邮件里的链接，含 token。访问后 session 写入 self.session.cookies。
        """
        resp = self.session.get(callback_url, timeout=30, allow_redirects=True)
        return {
            "ok": bool(resp.ok),
            "status": resp.status_code,
            "final_url": str(resp.url),
            "cookies": {c.name: c.value for c in self.session.cookies},
            "text": resp.text[:500],
        }

    # ===== GraphQL thread LLM（2api 核心，待登录抓变量 shape）=====

    def graphql_apq(self, *, operation_name: str, variables: dict[str, Any], session_cookies: dict[str, str] | None = None) -> dict[str, Any]:
        """GraphQL APQ 请求：发 operationName + sha256，server 返回结果。

        variables shape 需 Playwright 登录抓真实请求确认（hashes 稳定 server 缓存）。
        session_cookies 从 magic-link callback 拿。
        """
        sha = APQ_HASHES.get(operation_name, "")
        if not sha:
            raise ValueError(f"未知 APQ operation: {operation_name}")
        body = {
            "operationName": operation_name,
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": sha}},
        }
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if session_cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session_cookies.items())
        resp = self.session.post(GRAPHQL_API, json=body, headers=headers, timeout=60)
        return _json_or_text(resp)

    def chat(
        self,
        *,
        session_cookies: dict[str, str],
        org_id: str,
        prompt: str,
        model: str = DEFAULT_MODEL,
        stream: bool = False,
    ) -> dict[str, Any]:
        """thread LLM：createAskProjectAndThread + AgentChatMessage + subscription。待抓变量。

        TODO：精确 variables shape（agentChatThreadId/threadId/promptText/orgId）+ x-csrf-safe/x-hex-user-bearer-token
        来源 + subscription SSE 桥接。需 Playwright 登录抓包。
        """
        raise NotImplementedError(
            "hex chat 待抓 GraphQL variables shape + session headers"
            "（需 magic-link 登录后 Playwright 抓 AgentChatMessage 请求）"
        )


def account_preview(token: str) -> str:
    raw = str(token or "")
    if len(raw) <= 16:
        return raw
    return f"{raw[:8]}...{raw[-6:]}"
