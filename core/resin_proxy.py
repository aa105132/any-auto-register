from __future__ import annotations

from typing import Any
from urllib.parse import quote

from core.proxy_utils import normalize_proxy_url


def _is_truthy(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


def parse_resin_platform_map(raw: Any) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        normalized = line.strip()
        if not normalized or normalized.startswith("#"):
            continue
        if "=" in normalized:
            left, right = normalized.split("=", 1)
        elif ":" in normalized:
            left, right = normalized.split(":", 1)
        else:
            continue
        task_platform = left.strip().lower()
        resin_platform = right.strip()
        if task_platform and resin_platform:
            mapping[task_platform] = resin_platform
    return mapping


def resolve_resin_platform(task_platform: str = "", default_platform: Any = "Default", platform_map: Any = "") -> str:
    mapping = parse_resin_platform_map(platform_map)
    normalized_task_platform = str(task_platform or "").strip().lower()
    if normalized_task_platform and normalized_task_platform in mapping:
        return mapping[normalized_task_platform]
    return str(default_platform or "").strip()


def _normalize_scheme(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"http", "https", "socks5"}:
        return raw
    return "http"


def _normalize_port(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "2260"
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return "2260"
    if port <= 0:
        return "2260"
    return str(port)


def _build_proxy_url(scheme: str, host: str, port: str, username: str, token: str) -> str:
    auth = ""
    if username:
        auth = quote(username, safe="")
        if token:
            auth += f":{quote(token, safe='')}"
        auth += "@"
    elif token:
        auth = f":{quote(token, safe='')}@"
    return f"{scheme}://{auth}{host}:{port}"


def resolve_resin_proxy_config(
    values: dict[str, Any] | None,
    *,
    task_platform: str = "",
    account: str = "",
    require_enabled: bool = False,
) -> dict[str, Any]:
    config = dict(values or {})
    enabled = _is_truthy(config.get("resin_enabled"))
    if require_enabled and not enabled:
        return {
            "enabled": enabled,
            "source": "disabled",
            "proxy_url": None,
            "resolved_platform": "",
        }

    scheme = _normalize_scheme(config.get("resin_scheme"))
    host = str(config.get("resin_host") or "").strip()
    port = _normalize_port(config.get("resin_port"))
    token = str(config.get("resin_token") or "").strip()
    resolved_platform = resolve_resin_platform(
        task_platform=task_platform,
        default_platform=config.get("resin_default_platform", "Default"),
        platform_map=config.get("resin_platform_map", ""),
    )

    account_id = str(account or "").strip()
    username = f"{resolved_platform}.{account_id}" if account_id else resolved_platform

    if host:
        return {
            "enabled": enabled,
            "source": "structured",
            "proxy_url": _build_proxy_url(scheme, host, port, username, token),
            "resolved_platform": resolved_platform,
            "scheme": scheme,
            "host": host,
            "port": port,
        }

    legacy_url = str(config.get("resin_proxy_url") or "").strip()
    if legacy_url:
        return {
            "enabled": enabled,
            "source": "legacy_url",
            "proxy_url": normalize_proxy_url(legacy_url, default_scheme="http"),
            "resolved_platform": resolved_platform,
            "scheme": scheme,
            "host": host,
            "port": port,
        }

    return {
        "enabled": enabled,
        "source": "none",
        "proxy_url": None,
        "resolved_platform": resolved_platform,
        "scheme": scheme,
        "host": host,
        "port": port,
    }
