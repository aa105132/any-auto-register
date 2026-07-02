"""Runbear 平台核心：注册端点 + Turnstile + 确认邮件 + agent/chat LLM（2api 包装）。

runbear 是 plugbear.io 旗下 AI agent 平台，认证后端 PropelAuth（auth.runbear.io）。
注册：POST auth.runbear.io/api/fe/v2/signup{email,password,turnstile_token,first_name,last_name,
     properties{referral_source,tos}} → 200 → /en/login/confirm_email 等确认邮件链接。
Turnstile sitekey: 0x4AAAAAADrn0IM-tpRSsa_-（从 __NEXT_DATA__.pageProps.pageConfig 拿）。
密码要求：min_length=8，无大小写/数字/特殊要求，no_common_passwords。

2api（已抓包确认，scripts/_wf_runbear_chat_capture_result.json）：
  - 个人 agent：POST api.runbear.io/internal/trpc/inbox.personalAgent.ensure?batch=1 body {"0":{}}
    → 200 返回 {id, name, systemInstruction, ...}（Mastra agent，模型 gemini-3-flash-preview）。
  - chat 端点：POST api.runbear.io/internal/http/assistants/<appId>/chat
    headers Authorization: Bearer <__pa_at>, Content-Type: application/json
    body {threadId, userId, orgId, id, messages:[{id,role,parts:[{type:"text",text}]}],
          trigger:"submit-message", messageId}
    响应：text/event-stream，Vercel AI SDK data stream 协议
      data: {"type":"start","messageId":"..."}
      data: {"type":"start-step"}
      data: {"type":"text-delta","textDelta":"Hi"}   ← 拼 LLM 文本
      data: {"type":"reasoning-delta","reasoningDelta":"..."}  ← 推理（可选）
      data: {"type":"finish-step"}
      data: {"type":"finish","finishReason":"stop"}
      data: [DONE]
  - 会话持久化（playground UI 用，2api 不需要）：playground.session.upsert/appendMessages/rename。
  - __pa_at 获取链：协议 POST /api/fe/v1/login{email,pwd,turnstile_token}+x-csrf-token:'-.'
    → refresh_token cookie → patchright goto /en/post_login JS 换 __pa_at cookie（.runbear.io 域）。
"""
from __future__ import annotations

import base64
import json
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse

import requests

SITE_URL = "https://runbear.io"
APP_URL = "https://app.runbear.io"
AUTH_BASE = "https://auth.runbear.io"  # PropelAuth
API_BASE = "https://api.runbear.io"  # tRPC + chat 端点后端
TURNSTILE_SITEKEY = "0x4AAAAAADrn0IM-tpRSsa_-"

AUTH_CONFIG_API = f"{AUTH_BASE}/api/fe/v2/auth_configuration"
SIGNUP_API = f"{AUTH_BASE}/api/fe/v2/signup"
LOGIN_STATE_API = f"{AUTH_BASE}/api/fe/v2/login_state"
LOGIN_API = f"{AUTH_BASE}/api/fe/v1/login"  # 纯协议登录（拿 refresh_token cookie）

# tRPC 端点（batch=1，body {"0":{...}}）
TRPC_PERSONAL_AGENT_ENSURE = f"{API_BASE}/internal/trpc/inbox.personalAgent.ensure?batch=1"
TRPC_PERSONAL_AGENT_RETRIEVE = f"{API_BASE}/internal/trpc/inbox.personalAgent.retrieve?batch=1"
TRPC_CREDITS_BALANCE = f"{API_BASE}/internal/trpc/usage.credits.balance?batch=1"
TRPC_SESSION_UPSERT = f"{API_BASE}/internal/trpc/playground.session.upsert?batch=1"

# chat 端点（Vercel AI SDK DefaultChatTransport，非 tRPC）
def chat_url(app_id: str) -> str:
    return f"{API_BASE}/internal/http/assistants/{app_id}/chat"


# referral_source 枚举（user_property_settings.fields.referral_source.metadata.enum_values）
REFERRAL_SOURCES = [
    "Search engine", "YouTube", "Blog", "Google search ads", "Other ads",
    "Recommendation", "Newsletter", "LinkedIn", "X (Twitter)", "Reddit",
    "Other social media", "Not in the list",
]
DEFAULT_REFERRAL_SOURCE = "Search engine"

# 模型库：个人 agent 实测 Mastra + gemini-3-flash-preview（assistants.getCreationDefaults）。
# 平台亦支持 gpt5.5/5.4、Claude4.6（team agent），2api 暴露个人 agent 实际可用模型。
RUNBEAR_MODELS = [
    "gemini-3-flash-preview",
    "gpt-5.5", "gpt-5.4",
    "claude-sonnet-4.6",
]
DEFAULT_MODEL = "gemini-3-flash-preview"
FREE_MODELS = list(RUNBEAR_MODELS)

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [runbear] {msg}", flush=True)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] [runbear] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


def _safe_response(response: requests.Response) -> dict[str, Any]:
    return {
        "ok": bool(response.ok),
        "status": int(response.status_code),
        "content_type": response.headers.get("content-type", ""),
        "data": _json_or_text(response),
        "text": response.text[:2000],
    }


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    """解 PropelAuth __pa_at JWT payload（拿 user_id / org_id，chat body 需要）。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as exc:
        return {"decode_error": repr(exc)}


def _trpc_batch0(data: Any) -> dict[str, Any]:
    """从 tRPC batch 响应取第 0 个 result.data。"""
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            if "result" in first and isinstance(first["result"], dict):
                return first["result"].get("data") or {}
            if "error" in first:
                return {"error": first["error"]}
            return first
    return data if isinstance(data, dict) else {}


def _parse_sse_stream(response: requests.Response) -> dict[str, Any]:
    """解析 Vercel AI SDK data stream SSE（text/event-stream）→ {text, reasoning, stop_reason, events, is_error}。

    协议事件：
      start {messageId} → start-step → text-delta {textDelta} → reasoning-delta {reasoningDelta}
      → finish-step → finish {finishReason} → [DONE]
    """
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    stop_reason = "end_turn"
    events: list[dict[str, Any]] = []
    is_error = False
    raw_bytes = b""
    try:
        for chunk in response.iter_content(chunk_size=None):
            if not chunk:
                continue
            raw_bytes += chunk
    except Exception:
        pass
    body = raw_bytes.decode("utf-8", "replace")
    # 非 SSE（JSON 错误体）
    if not body.lstrip().startswith("data:") and "text/event-stream" not in response.headers.get("content-type", ""):
        try:
            j = json.loads(body)
            return {"text": "", "reasoning": "", "stop_reason": "error", "events": [], "is_error": True,
                    "error": str(j.get("message") or j.get("error") or body[:300]), "raw": body[:600]}
        except Exception:
            return {"text": "", "reasoning": "", "stop_reason": "error", "events": [], "is_error": True,
                    "error": body[:300], "raw": body[:600]}
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            events.append({"type": "done"})
            break
        try:
            evt = json.loads(payload)
        except Exception:
            continue
        if not isinstance(evt, dict):
            continue
        etype = evt.get("type", "")
        events.append(evt)
        if etype == "text-delta":
            text_parts.append(str(evt.get("textDelta") or evt.get("text") or ""))
        elif etype == "reasoning-delta":
            reasoning_parts.append(str(evt.get("reasoningDelta") or evt.get("reasoning") or ""))
        elif etype == "error":
            is_error = True
            stop_reason = "error"
            text_parts.append(str(evt.get("message") or evt.get("error") or ""))
        elif etype == "finish":
            fr = str(evt.get("finishReason") or "stop")
            stop_reason = {"stop": "end_turn", "length": "max_tokens", "tool-calls": "tool_calls"}.get(fr, fr)
        elif etype == "finish-step":
            # 某些实现把 text 放在 finish-step 的 part 里
            for part in (evt.get("part") or []):
                if isinstance(part, dict) and part.get("type") == "text":
                    text_parts.append(str(part.get("text") or ""))
    text = "".join(text_parts).strip()
    return {"text": text, "reasoning": "".join(reasoning_parts).strip(), "stop_reason": stop_reason,
            "events": events, "is_error": is_error, "raw": body[:800]}


class RunbearClient:
    """runbear HTTP 客户端：注册（PropelAuth + Turnstile）+ 确认 + 登录态 + agent/chat（待补）。

    proxy 走任务代理（trust_env=False 避免继承系统代理）。
    Turnstile token 需浏览器或 solver 解，纯协议不内置（由 worker 传入）。
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
                "Origin": AUTH_BASE,
                "Referer": f"{AUTH_BASE}/en/signup",
                "User-Agent": CHROME_UA,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    def get_auth_configuration(self) -> dict[str, Any]:
        """GET auth_configuration 看 PropelAuth 配置。"""
        resp = self.session.get(AUTH_CONFIG_API, timeout=20)
        return resp.json() if resp.ok else {"status": resp.status_code, "raw": resp.text[:300]}

    def signup(
        self,
        *,
        email: str,
        password: str,
        turnstile_token: str,
        first_name: str = "Auto",
        last_name: str = "Register",
        referral_source: str = DEFAULT_REFERRAL_SOURCE,
        tos: bool = True,
    ) -> dict[str, Any]:
        """注册：POST /api/fe/v2/signup。

        body={email,password,turnstile_token,first_name,last_name,properties{referral_source,tos}}
        200 {login_state,user_id} → 跳 /en/login/confirm_email；400 缺 referral_source/turnstile。
        注：signup 也需 x-csrf-token:'-.-' header（同 login），Turnstile 需用 proxy 解（IP 匹配）。
        """
        body = {
            "email": email,
            "password": password,
            "turnstile_token": turnstile_token,
            "first_name": first_name,
            "last_name": last_name,
            "properties": {
                "referral_source": referral_source or DEFAULT_REFERRAL_SOURCE,
                "tos": bool(tos),
            },
        }
        # signup 也需 x-csrf-token:'-.-'（同 login），否则 400 Invalid request
        signup_headers = {"x-csrf-token": "-.-", "Referer": f"{AUTH_BASE}/en/signup"}
        resp = self.session.post(SIGNUP_API, json=body, timeout=30, headers=signup_headers)
        data = _json_or_text(resp)
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200
        return data

    def get_login_state(self) -> dict[str, Any]:
        resp = self.session.get(LOGIN_STATE_API, timeout=20)
        return resp.json() if resp.ok else {"status": resp.status_code, "raw": resp.text[:300]}

    # ===== 确认邮件链接处理 =====

    def confirm_email(self, confirm_url: str) -> dict[str, Any]:
        """GET 确认邮件链接（通常带 token 参数）完成邮箱确认。

        确认后 PropelAuth 写 cookie/跳转，session 持有登录态。
        """
        resp = self.session.get(confirm_url, timeout=30, allow_redirects=True)
        return {
            "ok": bool(resp.ok),
            "status": resp.status_code,
            "final_url": str(resp.url),
            "text": resp.text[:500],
        }

    # ===== agent / chat LLM（2api 核心，已抓包确认）=====

    def _api_headers(self, access_token: str) -> dict[str, str]:
        """api.runbear.io 请求头（Bearer __pa_at）。"""
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": CHROME_UA,
            "Origin": APP_URL,
            "Referer": f"{APP_URL}/personal/playground",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def create_agent(
        self,
        *,
        access_token: str,
        name: str = "auto-agent",
        model: str = DEFAULT_MODEL,
    ) -> dict[str, Any]:
        """确保个人 agent 存在并返回其 UUID。

        实测：POST api.runbear.io/internal/trpc/inbox.personalAgent.ensure?batch=1 body {"0":{}}
        → 200 {id, orgId, userId, name, systemInstruction, ...}（Mastra agent）。
        个人 agent 每用户一个，ensure 幂等。返回 {assistant_uuid, name, model, raw}。
        """
        resp = self.session.post(
            TRPC_PERSONAL_AGENT_ENSURE,
            json={"0": {}},
            headers=self._api_headers(access_token),
            timeout=30,
        )
        if not resp.ok:
            return {"ok": False, "status": resp.status_code, "error": resp.text[:300]}
        data = _trpc_batch0(resp.json())
        if "error" in data:
            return {"ok": False, "status": resp.status_code, "error": data["error"]}
        agent_id = str(data.get("id") or "")
        return {
            "ok": True,
            "assistant_uuid": agent_id,
            "agent_id": agent_id,
            "name": str(data.get("name") or ""),
            "model": model or DEFAULT_MODEL,
            "user_id": str(data.get("userId") or ""),
            "org_id": str(data.get("orgId") or ""),
            "raw": {k: data[k] for k in ("id", "name", "userId", "orgId") if k in data},
        }

    def get_personal_agent(self, *, access_token: str) -> dict[str, Any]:
        """GET inbox.personalAgent.retrieve 取个人 agent（query，GET）。"""
        resp = self.session.get(
            TRPC_PERSONAL_AGENT_RETRIEVE + "&input=%7B%7D",
            headers=self._api_headers(access_token),
            timeout=20,
        )
        if not resp.ok:
            return {"ok": False, "status": resp.status_code, "error": resp.text[:300]}
        data = _trpc_batch0(resp.json())
        return {"ok": True, "assistant_uuid": str(data.get("id") or ""),
                "name": str(data.get("name") or ""),
                "user_id": str(data.get("userId") or ""),
                "org_id": str(data.get("orgId") or "")}

    def get_credits(self, *, access_token: str) -> dict[str, Any]:
        """GET usage.credits.balance 查额度。"""
        resp = self.session.get(
            TRPC_CREDITS_BALANCE + "&input=%7B%7D",
            headers=self._api_headers(access_token),
            timeout=20,
        )
        if not resp.ok:
            return {"ok": False, "status": resp.status_code}
        data = _trpc_batch0(resp.json())
        return {"ok": True, "balance": data.get("remainingIncludedCredits"),
                "chargeable": data.get("chargeableCreditsThisMonth")}

    def chat(
        self,
        *,
        access_token: str,
        assistant_uuid: str,
        messages: list[dict[str, str]],
        model: str = DEFAULT_MODEL,
        stream: bool = False,
    ) -> dict[str, Any]:
        """agent chat LLM 端点 → 包装 OpenAI。

        实测：POST api.runbear.io/internal/http/assistants/<appId>/chat
        headers Authorization: Bearer __pa_at
        body {threadId, userId, orgId, id, messages:[{id,role,parts:[{type:"text",text}]}],
              trigger:"submit-message", messageId}
        响应：text/event-stream，Vercel AI SDK data stream（data: {type:text-delta,textDelta}）。
        收完聚合返回 {text, stop_reason}。
        """
        if not assistant_uuid:
            # 兜底：自动 ensure 个人 agent
            ag = self.create_agent(access_token=access_token, model=model)
            assistant_uuid = ag.get("assistant_uuid") or ""
            if not assistant_uuid:
                return {"text": "", "stop_reason": "error", "is_error": True,
                        "error": f"无 assistant_uuid 且 ensure 失败: {ag.get('error')}"}
        # 从 JWT 取 userId/orgId（chat body 需要）
        jwt = _decode_jwt_payload(access_token)
        user_id = str(jwt.get("user_id") or "")
        org_info = jwt.get("org_member_info") or {}
        org_id = str(org_info.get("org_id") or "")
        if not user_id or not org_id:
            # 兜底：从 personalAgent.retrieve 取
            ag = self.get_personal_agent(access_token=access_token)
            user_id = user_id or ag.get("user_id") or ""
            org_id = org_id or ag.get("org_id") or ""

        thread_id = str(uuid.uuid4())
        msg_id = str(uuid.uuid4())[:13]
        asst_msg_id = str(uuid.uuid4())[:13]
        # OpenAI messages → runbear UI message parts 格式
        ui_messages = []
        for m in messages or []:
            role = str(m.get("role") or "user")
            content = str(m.get("content") or "")
            if not content and isinstance(m.get("parts"), list):
                # 已是 parts 格式则透传
                ui_messages.append({"id": str(m.get("id") or str(uuid.uuid4())[:13]), "role": role,
                                    "parts": m["parts"]})
                continue
            if not content:
                continue
            ui_messages.append({"id": str(m.get("id") or str(uuid.uuid4())[:13]), "role": role,
                                "parts": [{"type": "text", "text": content}]})
        if not ui_messages:
            return {"text": "", "stop_reason": "error", "is_error": True, "error": "无有效消息"}

        body = {
            "threadId": thread_id,
            "userId": user_id,
            "orgId": org_id,
            "id": thread_id,
            "messages": ui_messages,
            "trigger": "submit-message",
            "messageId": asst_msg_id,
        }
        resp = self.session.post(
            chat_url(assistant_uuid),
            json=body,
            headers=self._api_headers(access_token),
            timeout=120,
            stream=True,
        )
        if not resp.ok:
            return {"text": "", "stop_reason": "error", "is_error": True,
                    "status": resp.status_code, "error": resp.text[:300]}
        parsed = _parse_sse_stream(resp)
        return {
            "text": parsed["text"],
            "reasoning": parsed.get("reasoning", ""),
            "stop_reason": parsed["stop_reason"],
            "is_error": parsed["is_error"],
            "events": parsed.get("events", []),
            "error": parsed.get("error") or ("" if parsed["text"] else "empty response"),
            "raw": parsed.get("raw", ""),
        }


def account_preview(token: str) -> str:
    raw = str(token or "")
    if len(raw) <= 16:
        return raw
    return f"{raw[:8]}...{raw[-6:]}"
