from __future__ import annotations

from typing import Any
from urllib.parse import quote


def _is_truthy(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


def _normalize_port(value: Any, default: int = 33335) -> int:
    raw = str(value or "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return default
    return port if port > 0 else default


def _build_proxy_url(host: str, port: int, username: str, password: str) -> str:
    auth = ""
    if username:
        auth = quote(username, safe="")
        if password:
            auth += f":{quote(password, safe='')}"
        auth += "@"
    return f"http://{auth}{host}:{port}"


def resolve_brightdata_proxy_config(
    values: dict[str, Any] | None,
    *,
    slot: int = 0,
    require_enabled: bool = False,
) -> dict[str, Any]:
    config = dict(values or {})
    enabled = _is_truthy(config.get("brightdata_enabled"))

    if require_enabled and not enabled:
        return {"enabled": False, "source": "disabled", "proxy_url": None}

    host = str(config.get("brightdata_host") or "brd.superproxy.io").strip()
    port = _normalize_port(config.get("brightdata_port"), default=22225)
    base_username = str(config.get("brightdata_username") or "").strip()
    password = str(config.get("brightdata_password") or "").strip()

    if not base_username:
        return {"enabled": enabled, "source": "none", "proxy_url": None}

    if slot > 0:
        username = f"{base_username}-session-slot{slot}"
        mode = "sticky"
    else:
        username = base_username
        mode = "rotating"

    return {
        "enabled": enabled,
        "source": "brightdata",
        "proxy_url": _build_proxy_url(host, port, username, password),
        "host": host,
        "port": port,
        "mode": mode,
    }
