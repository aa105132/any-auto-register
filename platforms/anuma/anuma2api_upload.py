"""Anuma 账号导入 anuma2api。"""

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
    return f"{target.rstrip('/')}/api/accounts"


def build_anuma2api_payload(account: Any) -> dict[str, Any]:
    extra = dict(getattr(account, "extra", {}) or {})
    email = str(getattr(account, "email", "") or "").strip()
    privy_token = str(extra.get("privy_token") or getattr(account, "token", "") or "").strip()
    refresh_token = str(extra.get("privy_refresh_token") or "").strip()
    caid = str(extra.get("privy_caid") or "").strip()

    if not privy_token:
        return {}

    payload: dict[str, Any] = {"email": email, "privy_token": privy_token}
    if refresh_token:
        payload["refresh_token"] = refresh_token
    if caid:
        payload["caid"] = caid

    return payload


def upload_to_anuma2api(
    account: Any,
    api_url: str | None = None,
    timeout: int = 30,
) -> Tuple[bool, str]:
    base_url = str(api_url or _get_config_value("anuma2api_url") or "").strip()
    if not base_url:
        return False, "Anuma2API URL 未配置"

    payload = build_anuma2api_payload(account)
    if not payload:
        return False, "缺少 privy_token"

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
    try:
        detail = response.json()
        msg = detail.get("detail", detail.get("message", ""))
    except Exception:
        msg = ""
    return False, msg or f"HTTP {response.status_code}"
