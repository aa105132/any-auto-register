"""Clash verge 节点切换工具（避 Vercel 限流，每号切不同 IP）。

API: 127.0.0.1:9097 secret=set-your-secret，代理组「🔰 选择节点」。
"""
from __future__ import annotations

import sys, urllib.parse, json, urllib.request

API = "http://127.0.0.1:9097"
SECRET = "set-your-secret"
GROUP = "🔰 选择节点"

# Vercel 可用节点（非中国内地 IP，避开 x0.01 下载专用）
USABLE_NODES = [
    "🇭🇰 香港W01", "🇭🇰 香港W02", "🇭🇰 香港W03", "🇭🇰 香港W04", "🇭🇰 香港W05",
    "🇭🇰 香港W06 | x0.8", "🇭🇰 香港W07 | x0.8", "🇭🇰 香港W08 | x0.8",
    "🇭🇰 香港W09", "🇭🇰 香港W10", "🇭🇰 香港W11",
    "🇯🇵 日本W01 | IEPL", "🇯🇵 日本W02 | IEPL", "🇯🇵 日本W03 | IEPL",
    "🇯🇵 日本W04 | IEPL", "🇯🇵 日本W07 | IEPL", "🇯🇵 日本W08 | IEPL",
    "🇯🇵 日本W09 | IEPL", "🇯🇵 日本W10 | IEPL", "🇯🇵 日本W11 | IEPL",
    "🇸🇬 新加坡W01 | IEPL | x2", "🇸🇬 新加坡W02 | IEPL | x2", "🇸🇬 新加坡W03 | IEPL | x2",
    "🇨🇳 台湾W01 | IEPL | x2",
    "🇺🇲 美国W01 | IEPL | x1.5", "🇺🇲 美国W02 | IEPL | x1.5",
    "🇬🇧 英国W01", "🇰🇷 韩国W01", "🇩🇪 德国W01", "🇨🇦 加拿大W01", "🇦🇺 澳大利亚W01",
    "🇫🇷 法国W01",
]


def _req(path: str, method: str = "GET", data: bytes | None = None) -> dict:
    url = f"{API}{path}"
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Authorization": f"Bearer {SECRET}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or "{}")


def list_nodes() -> list[str]:
    g = urllib.parse.quote(GROUP)
    d = _req(f"/proxies/{g}")
    return d.get("all") or []


def current_node() -> str:
    g = urllib.parse.quote(GROUP)
    return _req(f"/proxies/{g}").get("now", "")


def switch(node: str) -> bool:
    """切到指定节点。"""
    g = urllib.parse.quote(GROUP)
    try:
        _req(f"/proxies/{g}", method="PUT", data=json.dumps({"name": node}).encode())
        return True
    except Exception as exc:
        print(f"[clash] 切节点 {node!r} 失败: {exc!r}", file=sys.stderr)
        return False


def switch_by_index(idx: int) -> str:
    """按索引轮转切节点（idx % len），返回切到的节点名。"""
    nodes = USABLE_NODES
    node = nodes[idx % len(nodes)]
    # 节点名带后缀（| IEPL 等）要精确匹配 list_nodes 里的名字
    real = list_nodes()
    # 找真实节点名（前缀匹配）
    match = next((n for n in real if n == node or n.startswith(node.split(" |")[0])), None)
    if match:
        switch(match)
        return match
    # 兜底用 node 本身
    switch(node)
    return node


def get_exit_ip() -> str:
    """查当前出口 IP。"""
    import urllib.request as u
    proxy = urllib.request.ProxyHandler({"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
    opener = u.build_opener(proxy)
    try:
        with opener.open("https://api.ipify.org", timeout=15) as r:
            return r.read().decode()
    except Exception:
        return ""


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        print(f"当前节点: {current_node().encode('ascii','replace').decode()}")
        print(f"出口 IP: {get_exit_ip()}")
        print(f"可用节点: {len(USABLE_NODES)}")
    elif cmd == "switch":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        n = switch_by_index(idx)
        ip = get_exit_ip()
        print(f"切到: {n.encode('ascii','replace').decode()} | IP: {ip}")
    elif cmd == "list":
        for n in list_nodes():
            print(n)
