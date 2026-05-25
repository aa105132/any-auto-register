from __future__ import annotations

from typing import Any
from urllib.parse import quote


def _is_truthy(value: Any) -> bool:
    raw = str(value or "").strip().lower()
    return raw in {"1", "true", "yes", "on", "enabled"}


def _normalize_port(value: Any, default: int = 0) -> int:
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


def resolve_decodo_proxy_config(
    values: dict[str, Any] | None,
    *,
    slot: int = 0,
    require_enabled: bool = False,
) -> dict[str, Any]:
    config = dict(values or {})
    enabled = _is_truthy(config.get("decodo_enabled"))

    if require_enabled and not enabled:
        return {"enabled": False, "source": "disabled", "proxy_url": None}

    host = str(config.get("decodo_host") or "dc.decodo.com").strip()
    username = str(config.get("decodo_username") or "").strip()
    password = str(config.get("decodo_password") or "").strip()
    explicit_port = _normalize_port(config.get("decodo_port"), default=0)
    port_base = _normalize_port(config.get("decodo_port_base"), default=10001)

    if not username:
        return {"enabled": enabled, "source": "none", "proxy_url": None}

    if explicit_port > 0:
        port = explicit_port
        mode = "rotating" if explicit_port == 10000 else "static"
    elif slot > 0:
        port = port_base + slot - 1
        mode = "sticky"
    else:
        port = 10000
        mode = "rotating"

    return {
        "enabled": enabled,
        "source": "decodo",
        "proxy_url": _build_proxy_url(host, port, username, password),
        "host": host,
        "port": port,
        "mode": mode,
    }
