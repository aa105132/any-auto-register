from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit


def _format_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _parse_proxy_parts(proxy: str, *, default_scheme: str = "http") -> dict[str, str]:
    value = str(proxy or "").strip()
    if not value:
        raise ValueError("Invalid proxy format: empty")

    if "://" in value:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname or parsed.port is None:
            raise ValueError(f"Invalid proxy format: {proxy}")
        result = {
            "scheme": parsed.scheme,
            "host": parsed.hostname,
            "port": str(parsed.port),
        }
        if parsed.username:
            result["username"] = unquote(parsed.username)
        if parsed.password:
            result["password"] = unquote(parsed.password)
        return result

    parts = value.split(":")
    if len(parts) == 5:
        scheme, host, port, username, password = parts
        return {"scheme": scheme, "host": host, "port": port, "username": username, "password": password}
    if len(parts) == 4:
        host, port, username, password = parts
        return {"scheme": default_scheme, "host": host, "port": port, "username": username, "password": password}
    if len(parts) == 3:
        scheme, host, port = parts
        return {"scheme": scheme, "host": host, "port": port}
    if len(parts) == 2:
        host, port = parts
        return {"scheme": "http", "host": host, "port": port}
    raise ValueError(f"Invalid proxy format: {proxy}")


def normalize_proxy_url(proxy: str | None, *, default_scheme: str = "http") -> str | None:
    if not proxy:
        return None

    parts = _parse_proxy_parts(proxy, default_scheme=default_scheme)
    auth = ""
    username = parts.get("username", "")
    password = parts.get("password", "")
    if username:
        auth = quote(username, safe="")
        if password:
            auth += f":{quote(password, safe='')}"
        auth += "@"
    return f"{parts['scheme']}://{auth}{_format_host(parts['host'])}:{parts['port']}"


def build_playwright_proxy_settings(proxy: str | None, *, default_scheme: str = "http") -> dict[str, str] | None:
    if not proxy:
        return None

    parts = _parse_proxy_parts(proxy, default_scheme=default_scheme)
    scheme = parts["scheme"]
    # requests 支持 socks5h:// 表示远端 DNS；Playwright/Chromium 只接受 socks5://。
    if scheme.lower() == "socks5h":
        scheme = "socks5"
    result = {"server": f"{scheme}://{_format_host(parts['host'])}:{parts['port']}"}
    if parts.get("username"):
        result["username"] = parts["username"]
    if parts.get("password"):
        result["password"] = parts["password"]
    return result
