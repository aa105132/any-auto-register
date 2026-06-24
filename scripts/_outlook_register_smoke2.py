"""Outlook 注册端到端冒烟脚本 v2 — 带 Clash 节点轮换 + Camoufox 选项。

account_blocked 是微软在验证码前就判定机器人并直接拦截，核心原因是 IP 质量/浏览器指纹。
本脚本轮换 Clash 住宅节点（日本/新加坡/美国/台湾 IEPL）逐个尝试，用 Camoufox 反检测 Firefox
最大化过 fingerprint 门。

用法：
  python scripts/_outlook_register_smoke2.py [--camoufox] [--proxy URL] [--suffix @outlook.com]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Clash 节点轮换池：优先住宅型 IEPL 节点，跳过免费节点和 DIRECT
ROTATE_NODES = [
    "🇯🇵 日本W01 | IEPL",
    "🇯🇵 日本W02 | IEPL",
    "🇯🇵 日本W03 | IEPL",
    "🇯🇵 日本W07 | IEPL",
    "🇸🇬 新加坡W01 | IEPL | x2",
    "🇸🇬 新加坡W02 | IEPL | x2",
    "🇨🇳 台湾W01 | IEPL | x2",
    "🇺🇲 美国W01 | IEPL | x1.5",
    "🇺🇲 美国W02 | IEPL | x1.5",
    "🇰🇷 韩国W01",
    "🇭🇰 香港W01",
    "🇭🇰 香港W02 | IEPL",
    "🇭🇰 香港W03 | IEPL",
    "🇬🇧 英国W01",
    "🇩🇪 德国W01",
    "🇨🇦 加拿大W01",
    "🇦🇺 澳大利亚W01",
    "🇫🇷 法国W01",
]


def switch_clash_node(node: str) -> bool:
    try:
        import requests
        api = "http://127.0.0.1:9097"
        H = {"Authorization": "Bearer set-your-secret", "Content-Type": "application/json"}
        sel = "🔰 选择节点"
        r = requests.put(f"{api}/proxies/{requests.utils.quote(sel)}", headers=H, json={"name": node}, timeout=5)
        if r.status_code in (204, 200):
            time.sleep(1.5)
            return True
    except Exception as e:
        print(f"[smoke2] switch node {node} fail: {e}", flush=True)
    return False


def current_ip(proxy: str) -> str:
    try:
        import requests
        r = requests.get("https://api.ipify.org", proxies={"http": proxy, "https": proxy}, timeout=10)
        return r.text.strip()
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camoufox", action="store_true", default=True, help="用 Camoufox（默认开）")
    parser.add_argument("--no-camoufox", action="store_true", help="用 patchright Chromium")
    parser.add_argument("--proxy", default="http://127.0.0.1:7897", help="代理 URL")
    parser.add_argument("--suffix", default="@outlook.com", help="邮箱后缀")
    parser.add_argument("--bot-wait", type=int, default=11, help="bot_protection_wait 秒数")
    parser.add_argument("--captcha-retries", type=int, default=3, help="验证码重试次数")
    parser.add_argument("--max-node-attempts", type=int, default=6, help="最多换几个节点")
    parser.add_argument("--register-timeout", type=int, default=300, help="单次注册超时秒数")
    parser.add_argument("--oauth-timeout", type=int, default=120, help="OAuth 拿 token 超时秒数")
    args = parser.parse_args()

    use_camoufox = args.camoufox and not args.no_camoufox

    from platforms.outlook.browser_register import OutlookBrowserRegister

    logs: list[str] = []

    def log_fn(msg: str):
        print(msg, flush=True)
        logs.append(msg)

    print(f"[smoke2] 启动: camoufox={use_camoufox} proxy={args.proxy} suffix={args.suffix} max_nodes={args.max_node_attempts}", flush=True)

    results: list[dict] = []
    for node_idx in range(max(1, args.max_node_attempts)):
        node = ROTATE_NODES[node_idx % len(ROTATE_NODES)]
        print(f"\n[smoke2] === 节点尝试 {node_idx + 1}/{args.max_node_attempts}: {node} ===", flush=True)
        if not switch_clash_node(node):
            print(f"[smoke2] 切节点失败，跳过", flush=True)
            continue
        ip = current_ip(args.proxy)
        print(f"[smoke2] 当前 IP: {ip}", flush=True)

        worker = OutlookBrowserRegister(
            headless=False,
            proxy=args.proxy,
            email_suffix=args.suffix,
            bot_protection_wait=args.bot_wait,
            max_captcha_retries=args.captcha_retries,
            use_camoufox=use_camoufox,
            use_protocol_proof=True,
            register_timeout=args.register_timeout,
            oauth_timeout=args.oauth_timeout,
            extra={},
            log_fn=log_fn,
        )
        start = time.time()
        try:
            result = worker.run()
        except Exception as exc:
            result = {"ok": False, "error": f"exception:{type(exc).__name__}", "exception": str(exc)[:500]}
            print(f"[smoke2] 注册异常: {repr(exc)[:200]}", flush=True)
        elapsed = time.time() - start
        result["elapsed_seconds"] = round(elapsed, 1)
        result["node"] = node
        result["ip"] = ip
        results.append(result)
        print(f"[smoke2] 节点 {node} 结果: ok={result.get('ok')} error={result.get('error', '')} elapsed={elapsed:.1f}s", flush=True)

        if result.get("ok") and result.get("refresh_token"):
            print(f"\n[smoke2] ✅✅✅ 注册成功 email={result.get('email')} refresh_token={'yes' if result.get('refresh_token') else 'no'}", flush=True)
            out = Path(__file__).resolve().parent / "_outlook_smoke_result.json"
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[smoke2] 结果已写入 {out}", flush=True)
            sys.exit(0)

        # account_blocked / ratelimit / wrong_captcha_type 都换节点重试
        # fill_*_failed 可能是瞬时网络问题，也换节点
        # 若是 oauth_failed 但注册已成功（邮箱已建），后面单独处理
        if result.get("error") == "oauth_failed":
            # 注册成功但 OAuth 拿 token 失败 — 邮箱已建，记录下来
            print(f"[smoke2] 注册成功但 OAuth 失败，邮箱 {result.get('email')} 已创建，继续换节点尝试新号", flush=True)

        time.sleep(3)

    # 全部节点都失败
    out = Path(__file__).resolve().parent / "_outlook_smoke_result.json"
    out.write_text(json.dumps({"ok": False, "results": results, "logs_tail": logs[-50:]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[smoke2] ❌ 所有节点尝试失败（{len(results)} 个）", flush=True)
    for r in results:
        print(f"  node={r.get('node')} ip={r.get('ip')} error={r.get('error')}", flush=True)
    print(f"[smoke2] 结果已写入 {out}", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    main()
