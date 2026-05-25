from __future__ import annotations

from fastapi import APIRouter
from core.config_store import config_store
from core.subscription_proxy import subscription_proxy_manager

router = APIRouter(prefix="/subscription-proxy", tags=["subscription-proxy"])


def _build_subscription_config() -> dict:
    truthy = lambda v: str(v or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}
    int_or = lambda k, d: max(int(config_store.get(k, d) or d), 1)
    return {"proxy_subscription": {
        "enabled": truthy(config_store.get("subscription_proxy_enabled", "false")),
        "url": str(config_store.get("subscription_proxy_url", "") or "").strip(),
        "kernel_path": str(config_store.get("subscription_proxy_kernel_path", "auto") or "auto").strip(),
        "listen": str(config_store.get("subscription_proxy_listen", "http://127.0.0.1:18080") or "http://127.0.0.1:18080").strip(),
        "strategy": str(config_store.get("subscription_proxy_strategy", "urltest") or "urltest").strip(),
        "check": str(config_store.get("subscription_proxy_check", "https://www.gstatic.com/generate_204") or "https://www.gstatic.com/generate_204").strip(),
        "check_interval": int_or("subscription_proxy_check_interval", 30),
        "refresh_interval_min": int_or("subscription_proxy_refresh_interval_min", 30),
        "max_nodes": int_or("subscription_proxy_max_nodes", 50),
        "fetch_via_proxy": truthy(config_store.get("subscription_proxy_fetch_via_proxy", "true")),
        "manual_node_tag": str(config_store.get("subscription_proxy_manual_node_tag", "") or "").strip(),
        "whitelist_tags": str(config_store.get("subscription_proxy_whitelist_tags", "") or "").strip(),
        "blacklist_tags": str(config_store.get("subscription_proxy_blacklist_tags", "") or "").strip(),
    }}


@router.get("/status")
def get_status():
    config = _build_subscription_config()
    return subscription_proxy_manager.status(config)


@router.post("/refresh")
def refresh():
    config = _build_subscription_config()
    try:
        listen = subscription_proxy_manager.refresh(config)
        return {"ok": True, "listen": listen}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/rotate")
def rotate():
    config = _build_subscription_config()
    try:
        listen = subscription_proxy_manager.rotate_proxy(config)
        return {"ok": True, "listen": listen}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
