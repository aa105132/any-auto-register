"""MixRoute (console.mixroute.ai) 注册/登录协议客户端。

MixRoute 是 new-api 架构的 AI API 聚合网关：控制台为 Vite + React SPA
（console.mixroute.ai），后端走 new-api 的 `/api/*` JSON 协议，推理走
`https://api.mixroute.ai/v1`（OpenAI 兼容）。注册/登录/拿 key 全程是 JSON
协议，无需渲染 SPA：

- 发送邮箱验证码：GET `/api/verification?email=...&turnstile=...`
- 注册：POST `/api/user/register?turnstile=...`，body 形如
  `{username,password,password2,email,verification_code,aff_code,turnstile}`，
  响应 `{success,message,data}`；`data` 即新登录会话（含 `token`/`user`）。
- 登录：POST `/api/user/login?turnstile=...`，body `{username,password}`。
- 拿 API Key：POST `/api/token/`，body
  `{name,remain_quota,expired_time,unlimited_quota,model_limits_enabled,model_limits}`，
  响应 `data` 含明文 key（new-api 习惯 `key` 字段，部分部署前缀 `sk-`）。
- 会话令牌保存在响应 `data.token`，前端写入 `localStorage['token']` 后，后续
  请求带 `Authorization: Bearer <token>` 与 `New-API-User: <user id>` 头。

Turnstile 由 `/api/status` 的 `turnstile_site_key`/`turnstile_check` 动态下发，
注册与发送验证码两个端点都需要 `turnstile` 查询参数。Google 登录走 OIDC：
`/api/oauth/state` 取 state → `accounts.google.com/o/oauth2/v2/auth` → 回调
`/api/oauth/oidc?code=...&state=...`（new-api 把 Google 当作 `oidc` provider）。
"""
from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import quote, urlencode

import requests
from requests import Session

SITE_URL = "https://mixroute.ai/"
CONSOLE_URL = "https://console.mixroute.ai"
LOGIN_URL = f"{CONSOLE_URL}/login"
REGISTER_URL = f"{CONSOLE_URL}/register"
TOKEN_URL = f"{CONSOLE_URL}/token"
DASHBOARD_URL = f"{CONSOLE_URL}/dashboard"
API_BASE = "https://api.mixroute.ai/v1"
MODELS_URL = f"{API_BASE}/models"

# /api/status 实地返回：turnstile_check=true，sitekey 如下。
# 与 cometapi/hpcai 一样由 /api/status 动态读取，这里给默认值兜底（部署变更会换）。
TURNSTILE_SITEKEY = "0x4AAAAAADTp-Awo9ZYRRxC1"
# Google OIDC（/api/status: oidc_enabled=true, oidc_client_id, oidc_authorization_endpoint）。
GOOGLE_OIDC_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OIDC_REDIRECT_PATH = "/oauth/oidc"  # new-api 把 Google 作为 oidc provider 回调
GITHUB_CLIENT_ID = "Ov23liNZCXDftcnpUEBP"  # /api/status.github_client_id（仅参考，注册机不用）

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/147.0.0.0 Safari/537.36"
)

# MixRoute key 由 new-api 生成：默认无前缀的随机串，部分部署前缀 sk-。
# _find_api_key 先匹配 sk- 形式，再回退 32+ 位随机串，最后按 new-api 习惯补 sk-。
API_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{20,}|[A-Za-z0-9_-]{32,}")


def _json_or_text(response: requests.Response, *, limit: int = 2000) -> Any:
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:limit]}


def _build_session(proxy: str | None = None) -> Session:
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json",
        "Origin": CONSOLE_URL,
        "Referer": REGISTER_URL,
        "Cache-Control": "no-store",
    })
    return session


def _request_json(
    session: Session,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    token: str | None = None,
    query: dict[str, Any] | None = None,
    referer: str | None = None,
    timeout: int = 45,
) -> dict[str, Any]:
    """发 new-api `/api/*` 请求，返回统一的 {ok,status,data,text,url} 包装。"""
    url = f"{CONSOLE_URL}{path}" if path.startswith("/") else path
    if query:
        clean = {k: v for k, v in query.items() if v not in (None, "")}
        if clean:
            url = f"{url}?{urlencode(clean, doseq=True)}"
    headers: dict[str, str] = {}
    if referer:
        headers["Referer"] = referer
    auth = str(token or "").strip()
    if auth:
        headers["Authorization"] = auth if auth.lower().startswith("bearer ") else f"Bearer {auth}"
    response = session.request(
        method.upper(),
        url,
        json=body if method.upper() not in {"GET"} else None,
        headers=headers,
        timeout=timeout,
    )
    data = _json_or_text(response)
    return {
        "ok": response.ok,
        "status": response.status_code,
        "data": data,
        "text": response.text[:2000],
        "url": response.url,
    }


def _response_success(result: dict[str, Any]) -> bool:
    """new-api 统一响应 {success,message,data}；success=true 即成功。"""
    data = result.get("data")
    if isinstance(data, dict) and "success" in data:
        return bool(data.get("success"))
    return bool(result.get("ok"))


def _response_data(result: dict[str, Any]) -> Any:
    data = result.get("data")
    if isinstance(data, dict) and "data" in data:
        return data.get("data")
    return data


def _response_message(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("message", "msg", "error", "detail", "reason"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
    return str(result.get("text") or "")


def _require_success(label: str, result: dict[str, Any]) -> Any:
    if not _response_success(result):
        raise RuntimeError(f"MixRoute {label} 失败: HTTP {result.get('status')} {_response_message(result)}")
    return _response_data(result)


def _session_cookie_dict(session: Session) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for cookie in session.cookies:
        domain = str(cookie.domain or "")
        if not domain or "mixroute.ai" in domain:
            cookies[str(cookie.name)] = str(cookie.value)
    return cookies


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k)


def _find_api_key(data: Any) -> str:
    """在 new-api 创建 key 响应里找出明文 key。

    new-api 的 `/api/token/` 创建响应形如 `{success,data:{key:"..."}}` 或
    `{success,data:{id,key,name,...}}`，明文 key 在 `key` 字段。部分部署会返回
    `sk-` 前缀，部分只返回裸串。专用密钥字段（key/fullKey/api_key 等）的值本身
    就是 key，直接信任；其余字段走正则兜底，避免把 id/name(UUID) 误当 key。
    """
    if isinstance(data, str):
        match = API_KEY_PATTERN.search(data)
        return match.group(0) if match else ""
    if isinstance(data, dict):
        # 优先取真正密钥字段：这些字段的值就是 key，直接返回（不卡长度）。
        for key in ("key", "full_key", "fullKey", "api_key", "apiKey", "token", "value", "secret"):
            value = data.get(key)
            if isinstance(value, str) and value.strip() and "****" not in value:
                return value.strip()
        # 其余字段走正则兜底（排除 id/name 等元信息字段，它们通常是 UUID/短名）。
        for key in ("data", "payload", "result", "item"):
            value = data.get(key)
            if isinstance(value, (dict, list)):
                found = _find_api_key(value)
                if found:
                    return found
        for key, value in data.items():
            if key in {"id", "uuid", "userId", "createdAt", "lastFour", "name", "status", "remain_quota", "expired_time"}:
                continue
            found = _find_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_api_key(item)
            if found:
                return found
    return ""


def _normalize_api_key(raw: str) -> str:
    """new-api 部分部署的 key 不带 sk- 前缀，统一补齐以便 OpenAI 兼容客户端识别。"""
    key = str(raw or "").strip()
    if not key:
        return ""
    if key.startswith("sk-"):
        return key
    return f"sk-{key}"


def _extract_token(data: Any) -> str:
    """从登录/注册响应里取会话 token（new-api 习惯放 data.token 或 data.access_token）。"""
    if isinstance(data, dict):
        for key in ("token", "access_token", "accessToken", "session_token"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = data.get("data") if isinstance(data.get("data"), dict) else None
        if nested:
            return _extract_token(nested)
    return ""


def _extract_user(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        if isinstance(data.get("user"), dict):
            return dict(data.get("user") or {})
        if any(k in data for k in ("id", "email", "username", "quota", "used_quota")):
            return dict(data)
        nested = data.get("data") if isinstance(data.get("data"), dict) else None
        if nested:
            user = _extract_user(nested)
            if user:
                return user
    return {}


def apply_session_auth(session: Session, token: str, user_id: str = "") -> None:
    """把 new-api 会话 token 注入 session（Authorization + New-API-User 头）。"""
    token = str(token or "").strip()
    if token:
        session.headers.update({
            "Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}",
        })
    if user_id:
        session.headers.update({"New-API-User": str(user_id)})


def import_cookies(session: Session, cookies: dict[str, str]) -> None:
    for name, value in (cookies or {}).items():
        if not name or value in (None, ""):
            continue
        for domain in ("console.mixroute.ai", ".mixroute.ai", "mixroute.ai"):
            session.cookies.set(str(name), str(value), domain=domain)


# --- 协议端点封装 -----------------------------------------------------------


def get_status_http(session: Session) -> dict[str, Any]:
    """读 /api/status：拿 turnstile_site_key、oidc_*、github_client_id 等运行时配置。"""
    return _request_json(session, "/api/status", method="GET", referer=LOGIN_URL)


def send_verification_http(
    session: Session,
    email: str,
    *,
    turnstile: str = "",
) -> dict[str, Any]:
    """GET /api/verification?email=...&turnstile=...：发送邮箱验证码。"""
    return _request_json(
        session,
        f"/api/verification?email={quote(email)}&turnstile={quote(turnstile or '')}",
        method="GET",
        referer=REGISTER_URL,
    )


def register_http(
    session: Session,
    *,
    username: str,
    password: str,
    email: str,
    verification_code: str,
    aff_code: str = "",
    turnstile: str = "",
) -> dict[str, Any]:
    """POST /api/user/register?turnstile=...：注册并直接签发会话 token。"""
    body: dict[str, Any] = {
        "username": username,
        "password": password,
        "password2": password,
        "email": email,
        "verification_code": verification_code,
        "aff_code": aff_code,
        "turnstile": turnstile,
    }
    return _request_json(
        session,
        f"/api/user/register?turnstile={quote(turnstile or '')}",
        method="POST",
        body=body,
        referer=REGISTER_URL,
    )


def login_http(
    session: Session,
    *,
    username: str,
    password: str,
    turnstile: str = "",
) -> dict[str, Any]:
    """POST /api/user/login?turnstile=...：用户名+密码登录。"""
    return _request_json(
        session,
        f"/api/user/login?turnstile={quote(turnstile or '')}",
        method="POST",
        body={"username": username, "password": password},
        referer=LOGIN_URL,
    )


def get_user_self_http(session: Session, token: str) -> dict[str, Any]:
    return _request_json(session, "/api/user/self", method="GET", token=token, referer=DASHBOARD_URL)


def create_api_key_http(
    session: Session,
    *,
    token: str,
    key_name: str = "auto-register",
    log_fn: Callable[[str], None] = print,
) -> dict[str, Any]:
    """POST /api/token/：创建 API Key。

    new-api 创建 token 的标准 payload 与 MixRoute 前端 `s$({name:'default',
    remain_quota:0, expired_time:-1, unlimited_quota:true, model_limits_enabled:false,
    model_limits:''})` 一致。

    new-api 安全策略：创建端点 `POST /api/token/` 只返回 `{success:true}`（不含明文 key），
    列表端点返回的 key 是掩码（`taZ0**********vOz3`）。明文 key 需通过
    `POST /api/token/{id}/key` 按需获取（前端 c$ 函数：`sk-${data.key}`）。
    故流程：创建 → 列表拿 id → POST /api/token/{id}/key 拿明文 → 补 sk- 前缀。
    """
    name = str(key_name or "auto-register").strip() or "auto-register"
    payload = {
        "name": name,
        "remain_quota": 0,
        "expired_time": -1,
        "unlimited_quota": True,
        "model_limits_enabled": False,
        "model_limits": "",
    }
    create_result = _request_json(
        session,
        "/api/token/",
        method="POST",
        body=payload,
        token=token,
        referer=TOKEN_URL,
    )
    create_data = _require_success("创建 API Key", create_result)
    # 创建响应可能直接含明文 key（部分 new-api 部署），优先取。
    api_key = _find_api_key(create_data)
    key_id = ""
    if isinstance(create_data, dict):
        key_id = str(create_data.get("id") or "")
    if api_key:
        api_key = _normalize_api_key(api_key)
        log_fn(f"[mixroute] API Key 已创建: {name}")
        return {"ok": True, "api_key": api_key, "api_key_info": create_data, "result": create_result}

    # 创建响应无明文 key：列表拿 id，再 POST /api/token/{id}/key 取明文。
    list_result = _request_json(session, "/api/token/?p=1&size=20", method="GET", token=token, referer=TOKEN_URL)
    list_data = _require_success("读取 API Key 列表", list_result)
    items = list_data if isinstance(list_data, list) else (
        (list_data.get("items") if isinstance(list_data, dict) else None) or []
    )
    # 优先用刚创建的 key id；否则取列表第一条（最新创建）。
    target_id = key_id
    if not target_id and isinstance(items, list) and items:
        # 取 name 匹配的或最新的
        for item in items:
            if str(item.get("name") or "") == name:
                target_id = str(item.get("id") or "")
                break
        if not target_id:
            target_id = str(items[0].get("id") or "")
    if not target_id:
        raise RuntimeError(f"MixRoute 创建 API Key 后未取到 key id: {list_data}")

    # POST /api/token/{id}/key — 按需获取明文 key（new-api 安全策略）。
    fetch_result = _request_json(
        session,
        f"/api/token/{target_id}/key",
        method="POST",
        token=token,
        referer=TOKEN_URL,
    )
    fetch_data = _require_success("获取 API Key 明文", fetch_result)
    raw_key = ""
    if isinstance(fetch_data, dict):
        raw_key = str(fetch_data.get("key") or "").strip()
    if not raw_key:
        raise RuntimeError(f"MixRoute /api/token/{target_id}/key 未返回明文 key: {fetch_data}")
    api_key = _normalize_api_key(raw_key)
    data = {"create": create_data, "list": list_data, "plaintext": fetch_data, "key_id": target_id}
    log_fn(f"[mixroute] API Key 已创建: {name}")
    return {"ok": True, "api_key": api_key, "api_key_info": data, "result": create_result}


def verify_api_key_http(api_key: str, *, proxy: str | None = None) -> dict[str, Any]:
    """用 OpenAI 兼容 /v1/models 验证 key 是否可用。"""
    if not api_key:
        return {"ok": False, "reason": "missing_api_key", "url": MODELS_URL}
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    try:
        response = session.get(
            MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=30,
        )
        body = _json_or_text(response)
        ok = bool(response.ok and isinstance(body, dict) and isinstance(body.get("data"), list))
        return {"ok": ok, "status": response.status_code, "url": MODELS_URL, "body": body}
    except Exception as exc:
        return {"ok": False, "error": repr(exc), "url": MODELS_URL}


def get_oauth_state_http(session: Session) -> str:
    """GET /api/oauth/state：取 OAuth state（带 aff 推广码，可选）。"""
    result = _request_json(session, "/api/oauth/state", method="GET", referer=LOGIN_URL)
    data = _require_success("获取 OAuth state", result)
    return str(data or "").strip()


def build_google_oidc_url(*, client_id: str, state: str, redirect_uri: str) -> str:
    """构造 Google OIDC 授权 URL（与 MixRoute 前端 Nm() 一致）。"""
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
    }
    return f"{GOOGLE_OIDC_AUTH_ENDPOINT}?{urlencode(params)}"


def exchange_oauth_code_http(
    session: Session,
    *,
    provider: str,
    code: str,
    state: str,
    redirect_uri: str = "",
) -> dict[str, Any]:
    """GET /api/oauth/${provider}?code=...&state=...[&redirect_uri=...]：OAuth 回调换会话。

    MixRoute 的 Google 登录走 new-api 的 oidc provider（回调路径 /oauth/oidc），
    GitHub 走 github provider。成功响应 data 含 token/user。
    """
    path = f"/api/oauth/{provider}?code={quote(code)}&state={quote(state)}"
    if redirect_uri:
        path += f"&redirect_uri={quote(redirect_uri)}"
    return _request_json(session, path, method="GET", referer=LOGIN_URL)
