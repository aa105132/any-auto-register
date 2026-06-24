"""CatAPI 远端自定义渠道补号接口客户端。

对接 CatAPI 的外部补号接口：
- GET  /api/external/channels/{slug}/keys 查询现有 key
- POST /api/external/channels/{slug}/keys 推送新 key

鉴权使用 Header 方式（X-Admin-Username / X-Admin-Password），避免密码进入 body
被业务日志记录。本模块保持纯函数风格，不读 config_store、不写日志，便于单测和复用。
"""
from __future__ import annotations

from typing import Any

import requests


class CatAPIError(RuntimeError):
    """CatAPI 调用失败（HTTP 非 2xx 或响应结构异常）。"""


def _normalize_base_url(base_url: str) -> str:
    text = str(base_url or "").strip().rstrip("/")
    if not text:
        raise CatAPIError("CatAPI 服务地址未配置")
    if not text.startswith(("http://", "https://")):
        raise CatAPIError("CatAPI 服务地址必须以 http:// 或 https:// 开头")
    return text


def _auth_headers(admin_username: str, admin_password: str, *, json_body: bool = False) -> dict[str, str]:
    username = str(admin_username or "").strip()
    password = str(admin_password or "").strip()
    if not username or not password:
        raise CatAPIError("CatAPI 管理员账号或密码未配置")
    headers = {
        "X-Admin-Username": username,
        "X-Admin-Password": password,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json"
    return headers


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
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    text = str(getattr(response, "text", "") or "").strip()
    return text[:200] if text else f"HTTP {response.status_code}"


def list_channel_keys(
    base_url: str,
    slug: str,
    *,
    admin_username: str,
    admin_password: str,
    timeout: float = 20.0,
) -> list[str]:
    """查询指定渠道的现有 api_key 列表。

    返回所有 key 的完整 api_key 字段，供调用方做去重判断。
    失败时抛 CatAPIError。
    """
    resolved_slug = str(slug or "").strip()
    if not resolved_slug:
        raise CatAPIError("CatAPI 渠道 slug 为空")
    url = f"{_normalize_base_url(base_url)}/api/external/channels/{resolved_slug}/keys"
    try:
        response = requests.get(
            url,
            headers=_auth_headers(admin_username, admin_password),
            timeout=max(1.0, float(timeout or 20.0)),
        )
    except requests.RequestException as exc:
        raise CatAPIError(f"查询 CatAPI 渠道 key 异常: {exc}") from exc
    if not (200 <= response.status_code < 300):
        raise CatAPIError(f"查询 CatAPI 渠道 key 失败: {_extract_error_message(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CatAPIError(f"CatAPI 返回非 JSON: {str(getattr(response, 'text', '') or '')[:200]}") from exc
    if not isinstance(data, dict) or not data.get("success"):
        message = str(data.get("detail") or data.get("message") or "CatAPI 返回 success=false") if isinstance(data, dict) else "CatAPI 返回结构异常"
        raise CatAPIError(message)
    keys = data.get("keys") or []
    if not isinstance(keys, list):
        return []
    result: list[str] = []
    for item in keys:
        if isinstance(item, dict):
            value = str(item.get("api_key") or "").strip()
            if value:
                result.append(value)
    return result


def push_channel_keys(
    base_url: str,
    slug: str,
    api_keys: list[str],
    *,
    admin_username: str,
    admin_password: str,
    name_prefix: str = "external",
    timeout: float = 20.0,
) -> dict[str, Any]:
    """推送 api_key 到指定渠道。

    返回 CatAPI 的响应字段：received / added / skipped / before_total / after_total。
    失败时抛 CatAPIError。
    """
    resolved_slug = str(slug or "").strip()
    if not resolved_slug:
        raise CatAPIError("CatAPI 渠道 slug 为空")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in list(api_keys or []):
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    if not normalized:
        raise CatAPIError("没有可推送的 api_key")
    url = f"{_normalize_base_url(base_url)}/api/external/channels/{resolved_slug}/keys"
    body = {"api_keys": normalized, "name_prefix": str(name_prefix or "external").strip() or "external"}
    try:
        response = requests.post(
            url,
            headers=_auth_headers(admin_username, admin_password, json_body=True),
            json=body,
            timeout=max(1.0, float(timeout or 20.0)),
        )
    except requests.RequestException as exc:
        raise CatAPIError(f"推送 CatAPI 渠道 key 异常: {exc}") from exc
    if not (200 <= response.status_code < 300):
        raise CatAPIError(f"推送 CatAPI 渠道 key 失败: {_extract_error_message(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise CatAPIError(f"CatAPI 返回非 JSON: {str(getattr(response, 'text', '') or '')[:200]}") from exc
    if not isinstance(data, dict) or not data.get("success"):
        message = str(data.get("detail") or data.get("message") or "CatAPI 返回 success=false") if isinstance(data, dict) else "CatAPI 返回结构异常"
        raise CatAPIError(message)
    result: dict[str, Any] = {
        "received": int(data.get("received") or 0),
        "added": int(data.get("added") or 0),
        "skipped": int(data.get("skipped") or 0),
        "before_total": int(data.get("before_total") or 0),
        "after_total": int(data.get("after_total") or 0),
    }
    if isinstance(data.get("channel"), dict):
        result["channel"] = data.get("channel")
    return result
