from __future__ import annotations

from urllib.parse import urlsplit


def _format_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def parse_playwright_proxy(proxy: str) -> dict[str, str]:
    """
    解析 Playwright 代理配置。

    支持：
    - http://user:pass@host:port
    - http:host:port:user:pass
    - host:port:user:pass（默认按 http 处理）
    - http://host:port
    """
    value = proxy.strip()
    if not value:
        raise ValueError("Invalid proxy format: empty")

    if "://" in value:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname or parsed.port is None:
            raise ValueError(f"Invalid proxy format: {proxy}")

        result: dict[str, str] = {
            "server": f"{parsed.scheme}://{_format_host(parsed.hostname)}:{parsed.port}",
        }
        if parsed.username:
            result["username"] = parsed.username
        if parsed.password:
            result["password"] = parsed.password
        return result

    parts = value.split(":")
    if len(parts) == 5:
        scheme, host, port, username, password = parts
    elif len(parts) == 4:
        scheme = "socks5"
        host, port, username, password = parts
    else:
        raise ValueError(f"Invalid proxy format: {proxy}")

    if not scheme or not host or not port:
        raise ValueError(f"Invalid proxy format: {proxy}")

    return {
        "server": f"{scheme}://{_format_host(host)}:{port}",
        "username": username,
        "password": password,
    }
