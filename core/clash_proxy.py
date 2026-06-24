"""Clash/mihomo 本地代理 + API 节点轮换工具。

Clash Verge (mihomo) 在本地 mixed-port（默认 7897）提供代理入口，
通过 external-controller（默认 127.0.0.1:9097）的 REST API 可切换 selector 节点，
实现「每次注册尝试用不同出口 IP」的住宅 IP 轮换。

相比 resin 机房 IP，Clash 节点通常是住宅/干净 IP，能过 WorkOS Radar 的 policy_denied 门。
"""
from __future__ import annotations

import time
from typing import Optional

# 默认配置：Clash Verge Rev 标准端口。实际值可被 config_store 覆盖。
DEFAULT_CLASH_API = "http://127.0.0.1:9097"
DEFAULT_CLASH_SECRET = "set-your-secret"
DEFAULT_CLASH_PROXY = "http://127.0.0.1:7897"
DEFAULT_CLASH_SELECTOR = "🔰 选择节点"

# 轮换池：优先住宅型节点，跳过免费节点（信誉差易被 WorkOS Radar policy_denied）和 DIRECT。
# 这些节点名是 Clash Verge 订阅里的典型命名，实际可用节点由 /proxies API 返回。
DEFAULT_ROTATE_NODES = [
    "🇭🇰 香港W01", "🇭🇰 香港W02 | IEPL", "🇭🇰 香港W03 | IEPL", "🇭🇰 香港W04 | IEPL",
    "🇭🇰 香港W06 | x0.8", "🇭🇰 香港W07 | x0.8", "🇭🇰 香港W08 | x0.8", "🇭🇰 香港W09 | IEPL",
    "🇭🇰 香港W10 | IEPL", "🇭🇰 香港W11 | IEPL",
    "🇯🇵 日本W01 | IEPL", "🇯🇵 日本W02 | IEPL", "🇯🇵 日本W03 | IEPL", "🇯🇵 日本W04 | IEPL",
    "🇯🇵 日本W07 | IEPL", "🇯🇵 日本W08 | IEPL", "🇯🇵 日本W09 | IEPL", "🇯🇵 日本W10 | IEPL", "🇯🇵 日本W11 | IEPL",
    "🇸🇬 新加坡W01 | IEPL | x2", "🇸🇬 新加坡W02 | IEPL | x2", "🇸🇬 新加坡W03 | IEPL | x2",
    "🇨🇳 台湾W01 | IEPL | x2",
    "🇺🇲 美国W01 | IEPL | x1.5", "🇺🇲 美国W02 | IEPL | x1.5",
    "🇬🇧 英国W01", "🇰🇷 韩国W01", "🇩🇪 德国W01", "🇨🇦 加拿大W01", "🇦🇺 澳大利亚W01", "🇫🇷 法国W01",
]

# 模块级轮换指针，跨调用累加，保证每次 rotate 拿到下一个节点
_rotate_idx = [0]


def _clash_config():
    """从 config_store 读取 Clash 配置，回退到默认值。"""
    try:
        from core.config_store import config_store
        api = str(config_store.get("clash_api", "") or "").strip() or DEFAULT_CLASH_API
        secret = str(config_store.get("clash_secret", "") or "").strip()
        if not secret:
            secret = DEFAULT_CLASH_SECRET
        proxy = str(config_store.get("clash_proxy", "") or "").strip() or DEFAULT_CLASH_PROXY
        selector = str(config_store.get("clash_selector", "") or "").strip() or DEFAULT_CLASH_SELECTOR
    except Exception:
        api, secret, proxy, selector = DEFAULT_CLASH_API, DEFAULT_CLASH_SECRET, DEFAULT_CLASH_PROXY, DEFAULT_CLASH_SELECTOR
    return api, secret, proxy, selector


def clash_switch_node(node: str, *, api: str = "", secret: str = "", selector: str = "") -> bool:
    """通过 Clash API 切 selector 到指定节点。返回是否成功。"""
    try:
        import requests as _rq
        from urllib.parse import quote as _q
        if not api:
            api, secret, _, selector = _clash_config()
        H = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
        url = f"{api}/proxies/{_q(selector)}"
        r = _rq.put(url, headers=H, json={"name": node}, timeout=5)
        return r.status_code in (204, 200)
    except Exception:
        return False


def clash_current_node(*, api: str = "", secret: str = "", selector: str = "") -> str:
    """读 Clash 当前选中的节点名。API 不可达返回空串。"""
    try:
        import requests as _rq
        from urllib.parse import quote as _q
        if not api:
            api, secret, _, selector = _clash_config()
        H = {"Authorization": f"Bearer {secret}"}
        r = _rq.get(f"{api}/proxies/{_q(selector)}", headers=H, timeout=5)
        if r.status_code == 200:
            return str(r.json().get("now") or "")
    except Exception:
        pass
    return ""


def clash_available() -> bool:
    """Clash API 是否可达（selector 能读到当前节点）。"""
    return bool(clash_current_node())


def probe_exit_ip(*, proxy: str = "", timeout: int = 12) -> str:
    """通过本地 Clash 代理探测当前出口 IP（用 curl_cffi 模拟 Chrome 指纹）。"""
    try:
        from curl_cffi import requests as creq
        if not proxy:
            _, _, proxy, _ = _clash_config()
        s = creq.Session(impersonate="chrome131")
        s.proxies = {"http": proxy, "https": proxy}
        pr = s.get("http://ip-api.com/json/", timeout=timeout)
        d = pr.json()
        return f"{d.get('query','?')} {d.get('country','?')} {d.get('city','?')}"
    except Exception as e:
        return f"ERR {type(e).__name__}"


def rotate_node(*, nodes: list[str] | None = None, settle: float = 1.2) -> str:
    """轮换到下一个 Clash 节点并返回 '节点名 | 出口IP' 描述。失败返回当前节点名。

    跨调用累加指针，保证每次调用拿到池里下一个节点（不重复当前节点）。
    """
    pool = nodes or DEFAULT_ROTATE_NODES
    if not pool:
        return probe_exit_ip()
    cur = clash_current_node()
    n = len(pool)
    for _ in range(n):
        node = pool[_rotate_idx[0] % n]
        _rotate_idx[0] += 1
        if node == cur:
            continue
        if clash_switch_node(node):
            time.sleep(settle)  # 等 mihomo 切链路 + 建连
            ip = probe_exit_ip()
            return f"{node} | {ip}"
    return cur or "clash_unavailable"


def resolve_clash_proxy() -> Optional[str]:
    """返回 Clash 本地代理 URL（若 API 可达），否则 None（调用方回退到 resin）。"""
    if clash_available():
        _, _, proxy, _ = _clash_config()
        return proxy
    return None
