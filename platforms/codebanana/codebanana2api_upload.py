"""CodeBanana 账号导入 codebanana2api。"""

from __future__ import annotations

from typing import Any, Tuple

import requests


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "")
    except Exception:
        return ""


def _resolve_import_url(api_url: str) -> str:
    target = str(api_url or "").strip()
    if not target:
        return ""
    if target.endswith("/api/admin/accounts") or target.endswith("/api/admin/accounts/import"):
        return target
    return f"{target.rstrip('/')}/api/admin/accounts"


def _extract_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            message = error_payload.get("message")
            if message:
                return str(message)
        message = payload.get("message")
        if message:
            return str(message)
    text = str(getattr(response, "text", "") or "").strip()
    return text[:200] if text else f"HTTP {response.status_code}"


def build_codebanana2api_payload(account: Any) -> dict[str, Any]:
    extra = dict(getattr(account, "extra", {}) or {})
    cookie = extra.get("cookies")
    if cookie in (None, ""):
        cookie = extra.get("cookie")
    session_token = str(extra.get("session_token") or getattr(account, "token", "") or "").strip()
    jwt_token = str(extra.get("jwtToken") or extra.get("jwt_token") or "").strip()
    name = (
        str(getattr(account, "email", "") or "").strip()
        or str(extra.get("username") or "").strip()
        or str(getattr(account, "user_id", "") or "").strip()
        or "codebanana-account"
    )

    payload: dict[str, Any] = {
        "name": name,
        "prefer_cookie_auth": True,
        "enabled": True,
        "notes": "any-auto-register 自动导入",
    }
    if cookie not in (None, "", {}, []):
        payload["cookie"] = cookie
    if session_token:
        payload["session_token"] = session_token
    if jwt_token:
        payload["jwt_token"] = jwt_token

    for key in ("chat_id", "agent_id", "workspace", "prefer_cookie_auth", "enabled", "notes"):
        value = extra.get(key)
        if value not in (None, "", [], {}):
            payload[key] = value

    return payload


def upload_to_codebanana2api(
    account: Any,
    api_url: str | None = None,
    timeout: int = 30,
) -> Tuple[bool, str]:
    base_url = str(api_url or _get_config_value("codebanana2api_url") or "").strip()
    if not base_url:
        return False, "CodeBanana2API URL 未配置"

    payload = build_codebanana2api_payload(account)
    if not any(key in payload for key in ("cookie", "session_token", "jwt_token")):
        return False, "缺少可导入认证信息（cookie / session_token / jwt_token）"

    try:
        response = requests.post(
            url=_resolve_import_url(base_url),
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, f"导入异常: {exc}"

    if 200 <= response.status_code < 300:
        return True, "导入成功"
    return False, _extract_error_message(response)
