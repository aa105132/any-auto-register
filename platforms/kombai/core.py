"""Kombai 平台核心：footprint 算法 + 注册/OAuth/订阅 client + WebSocket 聊天骨架。

kombai 是 IDE 扩展产品（VS Code/Cursor/Windsurf），认证后端 PropelAuth（auth.kombai.com）。
注册：app.kombai.com/auth/signup → POST auth.kombai.com/api/fe/v2/signup {email,password}
     → ConfirmEmailRequired 邮件确认链接 → 登录。
IDE 扩展 OAuth：agent.kombai.com/vscode-connect?type=new&code={base64(随机16字符)}
     → 注册/登录 → GET api.assistant.app.kombai.com/auth/api-key?code={code}&appMode=Assistant
     → {token, referralCode}，token 作为 x-api-key 用于所有请求 + WebSocket。

footprint（getFootprintV2）：AES-256-CBC，双重发送 x-client-context header + clientContext body。
服务端 footprintBlocked=true → account_blocked；FORBIDDEN_WAF=403 疑 Cloudflare WAF。

聊天走 WebSocket wss://ws.assistant.app.kombai.com 自定义协议（CHATv2/AGENTv3/TOOL_V1），
2api 需 WS→OpenAI REST 转换（消息格式细节需抓 WS 流量补全，见 ws_chat 骨架）。
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any

import requests

# ===== footprint 常量（主人逆向 + workflow 确认）=====
AES_KEY = b"nSL5yfPWKESQm2g9sII2MvX3dzcIH2rz"  # 32 字节
SECRET_STORAGE_KEY = "editor.workspace.sessionPersistenceId"
RUNTIME_DIR = Path.home() / ".config" / ".node_repl_history_meta"
RUNTIME_STATE_FILE = RUNTIME_DIR / ".runtime_state"
LOCAL_SESSION_FILE = RUNTIME_DIR / ".session_persistence_id"

FIXED_CONTEXT_FIELDS = {
    "s": "",
    "h": "desktop",
    "pe": "",
    "pt": "",
    "vp": "",
    "vh": "",
    "vr": "",
    "va": "Visual Studio Code",
}

# code 生成字符集（去掉 l/o/0/1 避混淆，与扩展端 ko() 一致）
CODE_CHARSET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"

# ===== 端点 ======
SITE_URL = "https://kombai.com"
APP_URL = "https://app.kombai.com"
AUTH_BASE = "https://auth.kombai.com"  # PropelAuth
AGENT_BASE = "https://agent.kombai.com"  # vscode-connect SPA
API_BASE = "https://api.assistant.app.kombai.com"
WS_BASE = "wss://ws.assistant.app.kombai.com"
VSCODE_REDIRECT_URI = "vscode://kombai.kombai/auth-callback"
EXTENSION_VERSION = "2.0.36"

SIGNUP_URL = f"{APP_URL}/auth/signup"
VSCODE_CONNECT_URL = f"{AGENT_BASE}/vscode-connect"
AUTH_SIGNUP_API = f"{AUTH_BASE}/api/fe/v2/signup"
AUTH_CONFIG_API = f"{AUTH_BASE}/api/fe/v2/auth_configuration"
AUTH_LOGIN_STATE_API = f"{AUTH_BASE}/api/fe/v2/login_state"
API_KEY_EXCHANGE_API = f"{API_BASE}/auth/api-key"
SUBSCRIPTION_STATUS_API = f"{API_BASE}/subscription/status-v2"
SUBSCRIPTION_PRICES_API = f"{API_BASE}/subscription/prices-v2"
SUBSCRIPTION_CREDITLOGS_API = f"{API_BASE}/subscription/creditlogs"
THREADS_API = f"{API_BASE}/threads"
THREAD_CREATE_API = f"{API_BASE}/thread"
REFERRAL_API = f"{API_BASE}/referral"
ORG_API = f"{API_BASE}/org"

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"

# ===== 模型库（credit 倍率）=====
KOMBAI_ROUTERS = ["Kombai-Auto", "Kombai-Ultra", "Kombai-High", "Kombai-Medium", "Kombai-Mini"]
KOMBAI_MODELS = [
    # OpenAI
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.5",
    # Anthropic
    "claude-opus-4.8", "claude-opus-4.7", "claude-opus-4.6", "claude-sonnet-4.6", "claude-haiku-4.5",
    # Google
    "gemini-3.5-flash", "gemini-3.1-pro", "gemini-3-flash", "gemini-3.1-flash-lite",
    # xAI
    "grok-build-0.1",
    # Moonshot
    "kimi-k2.5", "kimi-k2.6",
    # Alibaba
    "qwen-3.6-27b",
]
DEFAULT_MODEL = "Kombai-Auto"
FREE_MODELS = list(KOMBAI_ROUTERS)  # 路由器按倍率消耗 credits，无纯免费模型


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [kombai] {msg}", flush=True)
    except UnicodeEncodeError:
        import sys
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] [kombai] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


# ===== footprint 持久化 + AES 加密 =====

def _read_trimmed(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return value or None


def _get_or_create_file_value(path: Path) -> str:
    """读持久化 id，不存在则生成随机 64 hex 并落盘（与扩展端一致）。"""
    existing = _read_trimmed(path)
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    created = secrets.token_hex(32)
    path.write_text(created, encoding="utf-8")
    return created


def get_runtime_state_id() -> str:
    return _get_or_create_file_value(RUNTIME_STATE_FILE)


def get_local_session_persistence_id() -> str:
    return _get_or_create_file_value(LOCAL_SESSION_FILE)


def _json_stringify(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _aes_cbc_encrypt(plaintext: bytes, iv: bytes) -> bytes:
    """AES-256-CBC 加密。优先 cryptography 库，兜底 openssl subprocess。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        pad = PKCS7(128).padder()
        padded = pad.update(plaintext) + pad.finalize()
        cipher = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv))
        enc = cipher.encryptor()
        return enc.update(padded) + enc.finalize()
    except ImportError:
        # 兜底 openssl subprocess（与主人原算法一致）
        import subprocess
        proc = subprocess.run(
            ["openssl", "enc", "-aes-256-cbc", "-K", AES_KEY.hex(), "-iv", iv.hex()],
            input=plaintext, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        return proc.stdout


def encrypt_payload(payload: dict[str, Any], iv: bytes | None = None) -> str:
    """加密 footprint payload，返回 iv_hex:ciphertext_hex。"""
    iv = iv or secrets.token_bytes(16)
    ciphertext = _aes_cbc_encrypt(_json_stringify(payload), iv)
    return f"{iv.hex()}:{ciphertext.hex()}"


def get_fallback_footprint(now: int | None = None) -> str:
    return encrypt_payload({"m": "", "i": "", "t": now or int(time.time() * 1000), "e": "x"})


def get_footprint_v2(
    *,
    machine_id: str = "",
    session_persistence_id: str = "",
    runtime_state_id: str = "",
    now: int | None = None,
    allow_local_session: bool = True,
    context_overrides: dict[str, str] | None = None,
) -> str:
    """生成 footprint v2（iv_hex:ciphertext_hex）。

    session_persistence_id 为空时从本地持久化读（allow_local_session=True），
    都没有则返回 fallback footprint（空 session）。
    """
    session_id = session_persistence_id or (get_local_session_persistence_id() if allow_local_session else "")
    if not session_id:
        return get_fallback_footprint(now)
    device_id = runtime_state_id or (get_runtime_state_id() if allow_local_session else "")
    fields = {**FIXED_CONTEXT_FIELDS, **(context_overrides or {})}
    payload = {
        "m": machine_id,
        "i": session_id,
        "t": now or int(time.time() * 1000),
        "v": fields.pop("v", machine_id),
        **fields,
        "d": device_id,
    }
    return encrypt_payload(payload)


def get_client_context(**kwargs) -> str:
    """生成 x-client-context header 值（= get_footprint_v2）。"""
    return get_footprint_v2(**kwargs)


def generate_auth_code(byte_count: int = 12) -> str:
    """生成 vscode-connect 的 code 参数：随机字符 base64 编码（与扩展端 ko() 一致）。"""
    raw = "".join(secrets.choice(CODE_CHARSET) for _ in range(byte_count))
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def build_vscode_connect_url(code: str, *, type_: str = "new") -> str:
    """构造 vscode-connect 注册/登录 URL。type=new 注册 / type=agent 登录。"""
    from urllib.parse import urlencode
    return f"{VSCODE_CONNECT_URL}?" + urlencode({
        "redirectUri": VSCODE_REDIRECT_URI,
        "code": code,
        "from": "vscode",
        "type": type_,
    })


# ===== KombaiClient =====

class KombaiClient:
    """kombai HTTP 客户端：web 注册 + OAuth code 换 token + 订阅/线程查询 + WS 聊天骨架。

    proxy 走任务代理（trust_env=False 避免继承系统代理）。
    footprint 在 IDE 扩展调推理 API 时双重发送（x-client-context + clientContext body），
    web 注册请求不需要 footprint（已 Playwright 抓包确认）。
    """

    def __init__(self, *, proxy: str | None = None, log_fn=print):
        self.proxy = proxy
        self.log = log_fn or log
        self.session = requests.Session()
        self.session.trust_env = False
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": CHROME_UA,
            "Origin": APP_URL,
            "Referer": f"{APP_URL}/",
        })

    def _proxies(self):
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    # ===== web 注册（PropelAuth）=====

    def web_signup(self, email: str, password: str) -> dict[str, Any]:
        """web 注册：POST auth.kombai.com/api/fe/v2/signup {email,password}。

        返回 {user_id, login_state}。login_state=ConfirmEmailRequired → 需收确认邮件点链接。
        已 Playwright 抓包确认：无 captcha、无 footprint、无 OTP，纯 email+password。
        """
        resp = self.session.post(
            AUTH_SIGNUP_API,
            json={"email": email, "password": password},
            timeout=30,
        )
        data = resp.json() if resp.status_code in (200, 400, 401, 403) else {"raw": resp.text[:500]}
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200
        return data

    def get_login_state(self) -> dict[str, Any]:
        """GET auth.kombai.com/api/fe/v2/login_state 查登录状态。"""
        resp = self.session.get(AUTH_LOGIN_STATE_API, timeout=20)
        return resp.json() if resp.ok else {"raw": resp.text[:300], "status": resp.status_code}

    def get_auth_configuration(self) -> dict[str, Any]:
        """GET auth_configuration 看 PropelAuth 配置（google/otp/sso 开关）。"""
        resp = self.session.get(AUTH_CONFIG_API, timeout=20)
        return resp.json() if resp.ok else {}

    # ===== OAuth code 换 token（IDE 扩展流程）=====

    def _ide_headers(self, *, with_footprint: bool = True) -> dict[str, str]:
        """IDE 扩展调推理 API 的 headers：x-api-key/x-type/x-editor/x-extension-version + x-client-context footprint。"""
        headers = {
            "x-type": "agent",
            "x-editor": "vscode",
            "x-extension-version": EXTENSION_VERSION,
            "x-session-id": secrets.token_hex(16),
            "User-Agent": CHROME_UA,
            "Accept": "application/json, text/plain, */*",
        }
        if with_footprint:
            headers["x-client-context"] = get_client_context()
        return headers

    def exchange_code(self, code: str, *, app_mode: str = "Assistant",
                      access_token: str = "") -> dict[str, Any]:
        """OAuth code 换 token：POST /auth/api-key?code&appMode + Bearer 绑定 → GET ?code&appMode + Bearer 取 apiKeyToken。

        实测正确流程（见 scripts/_wf_kombai_e2e.py）：
          1. POST /auth/api-key?code={code}&appMode=Assistant + Authorization: Bearer <access_token>（无 body）→ 200 {} 绑定 code 到账号。
          2. GET  /auth/api-key?code={code}&appMode=Assistant + Authorization: Bearer <access_token> → 200 {apiKeyToken, userId, email}。
        access_token 是 PropelAuth refresh_token 响应里的 access_token（短时 JWT，浏览器 signup/confirm 会话内捕获，必须新鲜时换）。
        单发 GET 无 Bearer 会被 API Gateway SigV4 解析器拒（403 Missing Authentication Token）——旧实现就是错的。

        返回 {token/apiKeyToken, userId, email, status, ok}。token 即 x-api-key 用于后续所有请求 + WS。
        access_token 为空时退回旧 GET-only 兜底（多半 403，仅作诊断）。
        """
        if access_token:
            # 与实测可行的 SPA 同路径请求形状一致：Bearer + UA + Origin(agent.kombai.com) + Accept
            bearer_headers = {
                "Authorization": f"Bearer {access_token}",
                "User-Agent": CHROME_UA,
                "Origin": AGENT_BASE,
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
            }
            # 1) POST 绑定 code（query code/appMode + Bearer，无 body，同 SPA getAuthToken 形状）
            try:
                self.session.post(
                    API_KEY_EXCHANGE_API,
                    params={"code": code, "appMode": app_mode},
                    headers=bearer_headers, timeout=30,
                )
            except Exception:
                pass
            # 2) 紧接 GET 取 apiKeyToken（同连接同 session 才过 SigV4）
            resp = self.session.get(
                API_KEY_EXCHANGE_API,
                params={"code": code, "appMode": app_mode},
                headers=bearer_headers, timeout=30,
            )
            data = resp.json() if resp.status_code in (200, 400, 401, 403) else {"raw": resp.text[:500]}
            data["status"] = resp.status_code
            data["ok"] = resp.status_code == 200
            # 实测响应字段名是 apiKeyToken（非 token）
            if data.get("ok") and not data.get("token"):
                data["token"] = str(data.get("apiKeyToken") or data.get("token") or "")
            return data
        # 旧 GET-only 兜底（无 Bearer，实测 403 Missing Authentication Token，保留诊断用）
        resp = self.session.get(
            API_KEY_EXCHANGE_API,
            params={"code": code, "appMode": app_mode},
            headers=self._ide_headers(with_footprint=True), timeout=30,
        )
        data = resp.json() if resp.status_code in (200, 400, 401, 403) else {"raw": resp.text[:500]}
        data["status"] = resp.status_code
        data["ok"] = resp.status_code == 200
        if data.get("ok") and not data.get("token"):
            data["token"] = str(data.get("apiKeyToken") or data.get("token") or "")
        return data

    def verify_token(self, token: str) -> bool:
        """验证 token：GET /auth/api-key?apiKey={token}。"""
        resp = self.session.get(
            API_KEY_EXCHANGE_API,
            params={"apiKey": token},
            headers=self._ide_headers(with_footprint=True),
            timeout=20,
        )
        return resp.status_code == 200

    # ===== 订阅 / 线程 / 组织 =====

    def _authed_headers(self, token: str, *, with_footprint: bool = True) -> dict[str, str]:
        headers = self._ide_headers(with_footprint=with_footprint)
        headers["x-api-key"] = token
        return headers

    def get_subscription_status(self, token: str) -> dict[str, Any]:
        resp = self.session.get(
            SUBSCRIPTION_STATUS_API,
            headers=self._authed_headers(token), timeout=20,
        )
        return resp.json() if resp.ok else {"status": resp.status_code, "raw": resp.text[:300]}

    def get_creditlogs(self, token: str) -> dict[str, Any]:
        resp = self.session.get(
            SUBSCRIPTION_CREDITLOGS_API,
            headers=self._authed_headers(token), timeout=20,
        )
        return resp.json() if resp.ok else {"status": resp.status_code, "raw": resp.text[:300]}

    def list_threads(self, token: str) -> dict[str, Any]:
        resp = self.session.get(THREADS_API, headers=self._authed_headers(token), timeout=20)
        return resp.json() if resp.ok else {"status": resp.status_code, "raw": resp.text[:300]}

    def get_org(self, token: str) -> dict[str, Any]:
        resp = self.session.get(ORG_API, headers=self._authed_headers(token), timeout=20)
        return resp.json() if resp.ok else {"status": resp.status_code, "raw": resp.text[:300]}

    # ===== WebSocket 聊天（2api 核心，骨架待补 WS 协议消息格式）=====

    @staticmethod
    def map_model_size(model: str) -> str:
        """OpenAI 模型名 → kombai modelSize（workflow 逆向确认的枚举）。"""
        m = str(model or "").lower().strip()
        if not m or m in ("auto", "kombai-auto"):
            return "auto"
        if "opus-4-8" in m or "opus-4.8" in m:
            return "claude-opus-4-8"
        if "opus-4-7" in m:
            return "claude-opus-4-7"
        if "opus-4-6" in m or "opus-4.6" in m:
            return "claude-opus-4-6"
        if "sonnet-4-6" in m or "sonnet-4.6" in m:
            return "claude-sonnet-4-6"
        if "haiku-4-5" in m or "haiku-4.5" in m:
            return "claude-haiku-4.5"
        if "gpt-5.5" in m or "gpt5.5" in m:
            return "gpt-5.5"
        if "gpt-5.4-mini" in m or "gpt5.4-mini" in m:
            return "gpt-5.4-mini"
        if "gpt-5.4-nano" in m or "gpt5.4-nano" in m:
            return "gpt-5.4-nano"
        if "gpt-5.4" in m or "gpt5.4" in m or "gpt-4o" in m:
            return "gpt-5.4"
        if "gemini-3.5-flash" in m or "gemini-3-flash" in m or "gemini-3.1" in m:
            return "google/vertex/gemini-3-flash-preview"
        if "kimi-k2.7" in m:
            return "moonshotai/kimi-k2.7-code"
        if "kimi-k2.5" in m or "kimi-k2.6" in m:
            return "moonshotai/kimi-k2.7-code"
        if "qwen-3.6" in m or "qwen3.6" in m:
            return "qwen/qwen3.6-27b"
        if "mimo" in m:
            return "xiaomi/mimo-v2.5"
        if "claude" in m:
            return "claude-sonnet-4-6"
        if "kombai-ultra" in m or m == "opus":
            return "opus"
        if "kombai-high" in m or m == "best":
            return "best"
        if "kombai-medium" in m or m == "balanced":
            return "balanced"
        if "kombai-mini" in m or m == "lite":
            return "lite"
        return "auto"

    def ws_chat(
        self,
        token: str,
        *,
        messages: list[dict[str, str]],
        model: str = DEFAULT_MODEL,
        stream: bool = False,
        timeout: float = 120.0,
        thread_id: str = "",
        thinking_effort: str = "medium",
    ) -> dict[str, Any]:
        """WebSocket 聊天：连 wss://ws.assistant.app.kombai.com 发 CHATv2 收响应帧拼接文本。

        workflow 逆向 extension.js 确认的协议（见 memory kombai_ws_protocol）：
          - WS URL: wss://ws.assistant.app.kombai.com?sessionId={uuid}，握手 headers x-api-key/x-type/x-editor/x-extension-version
          - 发信封 {requestId, action:chatv2, payload:{...CHATv2 data}, sessionId, threadId, messageType:chat, clientContext:footprint}
          - 收帧 streamStart/streamMessage(response.text+index)/streamEnd(pending=false)/agentResult(content[])/error
          - 拼 response.text 按 index → 过滤 <tool-use/> XML → 返回 {text, stop_reason, is_error, thread_id, frames}

        messages 取最后 user 作 prompt，system 拼前。modelSize 即模型选择器（无独立 model 字段）。
        """
        import websocket  # websocket-client

        session_id = str(uuid.uuid4())
        ws_url = f"{WS_BASE}?sessionId={session_id}"
        header_list = [
            f"x-api-key: {token}",
            "x-type: agent",
            "x-editor: vscode",
            f"x-extension-version: {EXTENSION_VERSION}",
        ]

        # 取 system + 最后 user 拼 prompt（kombai 无 system role，拼到 prompt 前文本）
        sys_parts: list[str] = []
        prompt = ""
        for msg in messages or []:
            role = str(msg.get("role") or "").lower()
            content = str(msg.get("content") or "")
            if role == "system":
                if content:
                    sys_parts.append(content)
            elif role == "user":
                prompt = content  # 取最后一条 user
        if sys_parts:
            prompt = "\n\n".join(sys_parts) + "\n\n" + prompt
        if not prompt:
            prompt = "Hello"

        request_id = str(uuid.uuid4())
        thread_id = thread_id or f"thread-{uuid.uuid4()}"
        payload = {
            "fileContents": {}, "imageAttachments": {}, "indexedComponentIds": [],
            "file": {}, "folder": {}, "component": {}, "figma": {},
            "terminals": [], "excalidraw": {}, "userEditedFiles": {},
            "openTabs": [], "skills": [], "connectedMcps": [], "toolResults": [],
            "modelSize": self.map_model_size(model),
            "thinkingEffort": thinking_effort or "medium",
            "messageType": "chat",
            "planningMode": "plan_n_chat",
            "writeToDisk": False,
            "browserInfo": [], "browserMode": "off",
            "figmaToken": "", "tokenType": "Public",
            "planTechStack": False,
            "threadId": thread_id,
            "prompt": prompt,
            "editorState": {
                "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": prompt}]}],
            },
            "messageInitiator": "user",
            "subAction": "SelfServePl",
        }
        envelope = {
            "requestId": request_id,
            "action": "chatv2",
            "payload": payload,
            "sessionId": session_id,
            "threadId": thread_id,
            "messageType": "chat",
            "clientContext": get_footprint_v2(),
        }

        try:
            ws = websocket.create_connection(ws_url, header=header_list, timeout=timeout)
        except Exception as exc:
            return {"text": "", "stop_reason": "ws_connect_failed", "is_error": True,
                    "thread_id": thread_id, "frames": [], "error": str(exc)[:200]}

        frames: list[dict[str, Any]] = []
        chunks: dict[int, str] = {}
        final_text = ""
        stop_reason = ""
        is_error = False
        try:
            ws.send(json.dumps(envelope, ensure_ascii=False))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except Exception:
                    break
                if not raw:
                    continue
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(frame, dict):
                    continue
                frames.append(frame)
                action = str(frame.get("action") or "")
                if action == "streamMessage":
                    resp = frame.get("response") or {}
                    chunk = str(resp.get("text") or "")
                    idx = int(resp.get("index") if resp.get("index") is not None else -1)
                    if chunk:
                        chunks[idx] = chunks.get(idx, "") + chunk
                elif action == "streamEnd":
                    stop_reason = str(frame.get("stopReason") or "end_turn")
                    if frame.get("pending") is False:
                        break
                elif action == "agentResult":
                    result = frame.get("result") or {}
                    if "s3Link" in result:
                        # 大结果落 S3，此处不 fetch（2api 小响应一般不走）
                        pass
                    else:
                        content = result.get("content") or []
                        if isinstance(content, list):
                            final_text = "".join(
                                str(b.get("text") or "") for b in content if isinstance(b, dict)
                            )
                        stop_reason = str(result.get("stopReason") or stop_reason or "end_turn")
                        is_error = bool(result.get("isError"))
                    if frame.get("pending") is False:
                        break
                elif action == "error":
                    is_error = True
                    stop_reason = str(frame.get("error") or "error")
                    frames.append({"_error_message": str(frame.get("message") or "")[:300]})
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass

        if not final_text and chunks:
            final_text = "".join(chunks[k] for k in sorted(chunks.keys()))
        # 过滤流式文本中的 <tool-use .../> XML 标签
        final_text = re.sub(r"<tool-use[^>]*/?>", "", final_text).strip()
        return {
            "text": final_text,
            "stop_reason": stop_reason or "end_turn",
            "is_error": is_error,
            "thread_id": thread_id,
            "frames": frames,
        }


def account_preview(token: str) -> str:
    raw = str(token or "")
    if len(raw) <= 16:
        return raw
    return f"{raw[:8]}...{raw[-6:]}"
