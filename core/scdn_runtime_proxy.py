from __future__ import annotations

import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit
import ipaddress
import json

import requests

from core.proxy_utils import normalize_proxy_url


SCDN_PROXY_API_URL = "https://proxy.scdn.io/api/get_proxy.php"


def _normalize_protocol(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"http", "https", "socks4", "socks5"}:
        return raw
    return "http"


def _normalize_positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _resolve_validate_url(protocol: str, validate_url: str, logger: Any | None = None) -> str:
    normalized_url = str(validate_url or "").strip()
    if protocol not in {"socks4", "socks5"}:
        return normalized_url
    parsed = urlsplit(normalized_url)
    if parsed.scheme.lower() != "https":
        return normalized_url
    resolved = urlunsplit(("http", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    if logger and hasattr(logger, "log"):
        logger.log(
            f"SCDN {protocol.upper()} 检测 URL 已自动降级为 HTTP: {resolved}",
            level="info",
        )
    return resolved


def _extract_origin_ip(raw_text: str) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except Exception:
        payload = None
    if isinstance(payload, dict):
        for key in ("ip", "origin"):
            value = payload.get(key)
            if value in (None, ""):
                continue
            candidate = _extract_origin_ip(str(value))
            if candidate:
                return candidate
    for chunk in text.replace(",", " ").split():
        candidate = chunk.strip(" ,\"'[]{}()")
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return candidate
    return ""


class ScdnRuntimeProxySource:
    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        time_fn: Any | None = None,
    ) -> None:
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False
        self._time_fn = time_fn or time.time
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()

    def acquire_proxy(
        self,
        *,
        protocol: str,
        country_code: str,
        count: int,
        validate_url: str,
        validate_timeout_sec: int,
        cache_ttl_sec: int,
        cache_size: int,
        logger: Any | None = None,
    ) -> str | None:
        normalized_protocol = _normalize_protocol(protocol)
        normalized_country = str(country_code or "").strip().upper()
        normalized_count = _normalize_positive_int(count, 10)
        normalized_timeout = _normalize_positive_int(validate_timeout_sec, 8)
        normalized_cache_ttl = _normalize_positive_int(cache_ttl_sec, 120)
        normalized_cache_size = _normalize_positive_int(cache_size, 20)
        resolved_validate_url = _resolve_validate_url(normalized_protocol, validate_url, logger=logger)
        cache_key = (normalized_protocol, normalized_country)

        cached_proxy = self._pop_cached_proxy(cache_key)
        if cached_proxy:
            return cached_proxy

        fetched = self._fetch_candidates(
            protocol=normalized_protocol,
            country_code=normalized_country,
            count=normalized_count,
            logger=logger,
        )
        if not fetched:
            return None

        valid_proxies: list[str] = []
        for candidate in fetched:
            proxy_url = self._build_proxy_url(normalized_protocol, candidate)
            if not proxy_url:
                continue
            if self._validate_proxy(
                proxy_url,
                validate_url=resolved_validate_url,
                timeout_sec=normalized_timeout,
                logger=logger,
            ):
                valid_proxies.append(proxy_url)
            if len(valid_proxies) >= normalized_cache_size:
                break

        if not valid_proxies:
            return None

        first_proxy = valid_proxies[0]
        rest = valid_proxies[1:normalized_cache_size]
        if rest:
            self._store_cached_proxies(cache_key, rest, ttl_sec=normalized_cache_ttl)
        return first_proxy

    def _pop_cached_proxy(self, cache_key: tuple[str, str]) -> str | None:
        now = float(self._time_fn())
        with self._lock:
            entry = self._cache.get(cache_key)
            if not entry:
                return None
            if float(entry.get("expires_at", 0) or 0) <= now:
                self._cache.pop(cache_key, None)
                return None
            proxies = entry.get("proxies") or []
            if not proxies:
                self._cache.pop(cache_key, None)
                return None
            proxy_url = proxies.pop(0)
            if proxies:
                entry["proxies"] = proxies
            else:
                self._cache.pop(cache_key, None)
            return str(proxy_url or "").strip() or None

    def _store_cached_proxies(self, cache_key: tuple[str, str], proxies: list[str], *, ttl_sec: int) -> None:
        expires_at = float(self._time_fn()) + max(ttl_sec, 1)
        with self._lock:
            self._cache[cache_key] = {
                "expires_at": expires_at,
                "proxies": list(proxies),
            }

    def _fetch_candidates(
        self,
        *,
        protocol: str,
        country_code: str,
        count: int,
        logger: Any | None = None,
    ) -> list[str]:
        params: dict[str, Any] = {
            "protocol": protocol,
            "count": count,
        }
        if country_code:
            params["country_code"] = country_code
        try:
            response = self._session.get(
                SCDN_PROXY_API_URL,
                params=params,
                timeout=max(3, min(count, 30)),
            )
        except Exception as exc:
            if logger and hasattr(logger, "log"):
                logger.log(f"SCDN 运行时代理拉取失败: {exc}", level="warning")
            return []

        status_code = int(getattr(response, "status_code", 0) or 0)
        response_text = str(getattr(response, "text", "") or "")
        response_preview = response_text[:240]
        if status_code >= 400:
            if logger and hasattr(logger, "log"):
                logger.log(
                    f"SCDN 运行时代理拉取失败: status={status_code}; body_preview={response_preview}",
                    level="warning",
                )
            return []

        try:
            payload = response.json()
        except Exception:
            if logger and hasattr(logger, "log"):
                logger.log(
                    f"SCDN 运行时代理返回非 JSON 响应: status={status_code}; body_preview={response_preview}",
                    level="warning",
                )
            return []

        data = payload.get("data") if isinstance(payload, dict) else None
        proxies = data.get("proxies") if isinstance(data, dict) else None
        if not isinstance(proxies, list):
            if logger and hasattr(logger, "log"):
                logger.log(
                    f"SCDN 运行时代理响应缺少 proxies 字段: payload_preview={str(payload)[:240]}",
                    level="warning",
                )
            return []
        return [str(item or "").strip() for item in proxies if str(item or "").strip()]

    def _build_proxy_url(self, protocol: str, candidate: str) -> str | None:
        raw = str(candidate or "").strip()
        if not raw:
            return None
        if "://" not in raw:
            raw = f"{protocol}://{raw}"
        return normalize_proxy_url(raw, default_scheme=protocol)

    def _validate_proxy(
        self,
        proxy_url: str,
        *,
        validate_url: str,
        timeout_sec: int,
        logger: Any | None = None,
    ) -> bool:
        try:
            response = self._session.get(
                validate_url,
                timeout=timeout_sec,
                proxies={
                    "http": proxy_url,
                    "https": proxy_url,
                },
            )
        except Exception as exc:
            if logger and hasattr(logger, "log"):
                logger.log(
                    f"SCDN 代理验证失败: proxy={proxy_url}; validate_url={validate_url}; "
                    f"error_type={exc.__class__.__name__}; error={str(exc)[:240]}",
                    level="warning",
                )
            return False
        status_code = int(getattr(response, "status_code", 0) or 0)
        body_preview = str(getattr(response, "text", "") or "")[:240]
        origin_ip = _extract_origin_ip(getattr(response, "text", "") or "")
        if origin_ip:
            if logger and hasattr(logger, "log") and not (200 <= status_code < 400):
                logger.log(
                    f"SCDN 代理验证接受非 2xx 响应: proxy={proxy_url}; validate_url={validate_url}; "
                    f"status={status_code}; origin={origin_ip}",
                    level="info",
                )
            return True
        if not (200 <= status_code < 400):
            if logger and hasattr(logger, "log"):
                logger.log(
                    f"SCDN 代理验证失败: proxy={proxy_url}; validate_url={validate_url}; "
                    f"status={status_code}; body_preview={body_preview}",
                    level="warning",
                )
            return False
        return 200 <= status_code < 400
