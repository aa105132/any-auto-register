"""BlendSpace 账号导入本地 blendspace2api / OpenAI 兼容代理。"""
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
    if target.endswith("/admin/accounts/import"):
        return target
    return f"{target.rstrip('/')}/admin/accounts/import"


def _extract_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("detail", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("message") or value.get("detail")
                if nested:
                    return str(nested)
    text = str(getattr(response, "text", "") or "").strip()
    return text[:200] if text else f"HTTP {response.status_code}"


def _normalize_session_id(value: Any) -> str:
    raw = str(value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        raw = raw[1:-1].strip()
    return raw


def build_blendspace2api_payload(account: Any) -> dict[str, Any]:
    extra = dict(getattr(account, "extra", {}) or {})
    session_id = _normalize_session_id(
        extra.get("session_id")
        or extra.get("sessionId")
        or extra.get("wasp_session_id")
        or getattr(account, "token", "")
    )
    label = str(extra.get("label") or getattr(account, "email", "") or "blendspace-account").strip()
    item: dict[str, Any] = {"sessionId": session_id}
    if label:
        item["label"] = label
    return {"accounts": [item]}


def upload_to_blendspace2api(
    account: Any,
    api_url: str | None = None,
    admin_api_key: str | None = None,
    timeout: int = 30,
) -> Tuple[bool, str]:
    base_url = str(api_url or _get_config_value("blendspace2api_url") or "").strip()
    if not base_url:
        return False, "BlendSpace2API URL 未配置"

    key = str(admin_api_key or _get_config_value("blendspace2api_admin_api_key") or "").strip()
    if not key:
        return False, "BlendSpace2API ADMIN_API_KEY 未配置"

    payload = build_blendspace2api_payload(account)
    session_id = str(payload.get("accounts", [{}])[0].get("sessionId") or "").strip()
    if not session_id:
        return False, "缺少可导入的 wasp:sessionId"

    try:
        response = requests.post(
            url=_resolve_import_url(base_url),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return False, f"导入异常: {exc}"

    if 200 <= response.status_code < 300:
        return True, "导入成功"
    return False, _extract_error_message(response)
