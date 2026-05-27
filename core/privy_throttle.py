"""Privy 调用限流器。

ATXP / Anuma / 其他基于 Privy 的平台共享同一台 auth.privy.io，并发跑注册时
会撞同一个 IP 上的 OTP send 限流（429）。这里在进程内做：

1. 全局最小间隔：两次 /passwordless/init 至少相隔 ``min_gap_seconds`` 秒。
2. 429 退避重试：调用方传入 send_callable，由本模块负责重试。

调用方只关心 acquire_send_slot() 和 execute_with_429_retry()。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import requests


class _PrivyThrottle:
    def __init__(self, min_gap_seconds: float = 2.5) -> None:
        self.min_gap_seconds = min_gap_seconds
        self._lock = threading.Lock()
        self._last_send_ts: float = 0.0

    def acquire_send_slot(self) -> float:
        """阻塞直到距离上次 send 至少 min_gap_seconds 秒。返回等待秒数。"""
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._last_send_ts + self.min_gap_seconds - now)
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._last_send_ts = now
            return wait


_GLOBAL = _PrivyThrottle()


def acquire_send_slot() -> float:
    return _GLOBAL.acquire_send_slot()


_HARD_BACKOFF_CAP = 60.0


def execute_with_429_retry(
    fn: Callable[[], "requests.Response"],
    *,
    log_fn: Callable[[str], None] | None = None,
    label: str = "privy",
    max_retries: int = 4,
    base_backoff: float = 3.0,
    backoff_cap: float = 30.0,
) -> "requests.Response":
    """执行 fn，遇 429 / Retry-After 自适应退避后重试。

    最多 max_retries 次重试。退避取指数退避和远端 Retry-After 的较小值，
    硬上限 _HARD_BACKOFF_CAP (60s)，防止远端返回超大 Retry-After 导致长等待。
    """
    attempt = 0
    while True:
        if attempt > 0:
            slept = acquire_send_slot()
            if log_fn and slept > 0:
                log_fn(f"{label}: throttle wait {slept:.2f}s")
        response = fn()
        status = int(getattr(response, "status_code", 200) or 200)
        if status != 429:
            return response
        if attempt >= max_retries:
            return response
        retry_after_raw = ""
        try:
            retry_after_raw = response.headers.get("Retry-After", "") or ""
        except Exception:
            retry_after_raw = ""
        try:
            retry_after = float(retry_after_raw) if retry_after_raw else 0.0
        except (TypeError, ValueError):
            retry_after = 0.0
        exponential = min(base_backoff * (2 ** attempt), backoff_cap)
        if retry_after > 0:
            backoff = min(retry_after, exponential, _HARD_BACKOFF_CAP)
        else:
            backoff = min(exponential, _HARD_BACKOFF_CAP)
        if log_fn:
            log_fn(
                f"{label}: 429 received, backoff {backoff:.1f}s "
                f"(attempt {attempt + 1}/{max_retries})"
            )
        time.sleep(backoff)
        attempt += 1
