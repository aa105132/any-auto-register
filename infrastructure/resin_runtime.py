from __future__ import annotations

import time
from typing import Any

import requests

from core.resin_proxy import resolve_resin_proxy_config


class ResinRuntime:
    def probe(self, data: dict[str, Any] | None, task_platform: str = "") -> dict[str, Any]:
        resolved = resolve_resin_proxy_config(data, task_platform=task_platform, require_enabled=False)
        proxy_url = str(resolved.get("proxy_url") or "").strip()
        if not proxy_url:
            return {
                "ok": False,
                "error": "未检测到可用的 Resin 代理配置",
                "proxy_url": None,
                "resolved_platform": resolved.get("resolved_platform", ""),
                "source": resolved.get("source", "none"),
            }

        start = time.perf_counter()
        try:
            response = requests.get(
                "https://httpbin.org/ip",
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=8,
            )
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            payload: dict[str, Any] = {}
            try:
                payload = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            except Exception:
                payload = {}
            origin_ip = payload.get("origin") or payload.get("ip") or ""
            return {
                "ok": response.ok,
                "status_code": int(response.status_code),
                "latency_ms": elapsed_ms,
                "probe_url": "https://httpbin.org/ip",
                "proxy_url": proxy_url,
                "resolved_platform": resolved.get("resolved_platform", ""),
                "source": resolved.get("source", "none"),
                "origin_ip": str(origin_ip or ""),
            }
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return {
                "ok": False,
                "error": str(exc),
                "latency_ms": elapsed_ms,
                "probe_url": "https://httpbin.org/ip",
                "proxy_url": proxy_url,
                "resolved_platform": resolved.get("resolved_platform", ""),
                "source": resolved.get("source", "none"),
            }
