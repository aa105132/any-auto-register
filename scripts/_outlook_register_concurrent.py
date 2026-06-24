"""Outlook 注册并发批量跑 — Clash 节点轮换 + resin 兜底，并发 3 × N 次。

每个 worker 用唯一 resin account（ol<slot>）拿独立 IP，使劲换 IP 直到跑通。
成功一个就停；全部失败则汇总各节点/IP/错误。

用法：
  python scripts/_outlook_register_concurrent.py [--concurrency 3] [--total 50] [--proxy-source clash|resin|both]
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SUFFIX = "@outlook.com"
BOT_WAIT = 11
CAPTCHA_RETRIES = 3
REGISTER_TIMEOUT = 300
OAUTH_TIMEOUT = 120

# Clash 节点轮换池：住宅型 IEPL 节点（这些节点之前验证过能加载 Outlook 注册页）
CLASH_NODES = [
    "🇯🇵 日本W01 | IEPL",
    "🇯🇵 日本W02 | IEPL",
    "🇯🇵 日本W03 | IEPL",
    "🇯🇵 日本W07 | IEPL",
    "🇯🇵 日本W08 | IEPL",
    "🇯🇵 日本W09 | IEPL",
    "🇯🇵 日本W10 | IEPL",
    "🇯🇵 日本W11 | IEPL",
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
CLASH_API = "http://127.0.0.1:9097"
CLASH_SECRET = "set-your-secret"
CLASH_SELECTOR = "🔰 选择节点"
CLASH_PROXY = "http://127.0.0.1:7897"

_slot_counter = [0]
_slot_lock = threading.Lock()
_results_lock = threading.Lock()
_results: list[dict] = []
_success_result: dict | None = None
_stop_flag = threading.Event()
_print_lock = threading.Lock()
_clash_lock = threading.Lock()  # Clash 节点切换串行化（API 非线程安全）


def next_slot() -> int:
    with _slot_lock:
        _slot_counter[0] += 1
        return _slot_counter[0]


_LOG_PATH = Path(__file__).resolve().parent / "_outlook_concurrent.log"
_RESULT_PATH = Path(__file__).resolve().parent / "_outlook_concurrent_result.json"


def safe_print(msg: str):
    with _print_lock:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}\n"
        print(line, end="", flush=True)
        try:
            with _LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def write_results_incremental():
    """增量写结果文件，便于中途查看进度。"""
    try:
        with _results_lock:
            payload = {
                "success": _success_result,
                "attempts_count": len(_results),
                "all_results": list(_results),
            }
        _RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def switch_clash_node(node: str) -> bool:
    try:
        import requests
        H = {"Authorization": f"Bearer {CLASH_SECRET}", "Content-Type": "application/json"}
        with _clash_lock:
            r = requests.put(f"{CLASH_API}/proxies/{requests.utils.quote(CLASH_SELECTOR)}", headers=H, json={"name": node}, timeout=8)
            if r.status_code in (204, 200):
                time.sleep(1.5)
                return True
    except Exception as e:
        safe_print(f"switch node {node} fail: {e}")
    return False


def resolve_resin_for_outlook(slot: int) -> str | None:
    from core.config_store import config_store
    from core.resin_proxy import resolve_resin_proxy_config
    cfg = {
        "resin_enabled": "true",
        "resin_scheme": config_store.get("resin_scheme", ""),
        "resin_host": config_store.get("resin_host", ""),
        "resin_port": config_store.get("resin_port", ""),
        "resin_token": config_store.get("resin_token", ""),
        "resin_default_platform": config_store.get("resin_default_platform", "Default"),
        "resin_platform_map": config_store.get("resin_platform_map", ""),
    }
    resolved = resolve_resin_proxy_config(cfg, task_platform="outlook", account=f"ol{slot}", require_enabled=True)
    return str(resolved.get("proxy_url") or "").strip() or None


def resolve_proxy_for_slot(slot: int, proxy_source: str) -> tuple[str | None, str]:
    """返回 (proxy_url, ip_or_node_label)。"""
    if proxy_source == "clash":
        node = CLASH_NODES[slot % len(CLASH_NODES)]
        if switch_clash_node(node):
            return CLASH_PROXY, node
        return None, node
    if proxy_source == "resin":
        return resolve_resin_for_outlook(slot), f"resin-ol{slot}"
    # both：偶数 slot 用 clash 节点，奇数用 resin
    if slot % 2 == 0:
        node = CLASH_NODES[(slot // 2) % len(CLASH_NODES)]
        if switch_clash_node(node):
            return CLASH_PROXY, node
        return None, node
    return resolve_resin_for_outlook(slot), f"resin-ol{slot}"


def probe_ip(proxy_url: str) -> str:
    try:
        import requests
        r = requests.get("https://api.ipify.org", proxies={"http": proxy_url, "https": proxy_url}, timeout=12)
        return r.text.strip()
    except Exception:
        return ""


def worker_run(worker_id: int, total: int, proxy_source: str, single_attempt: bool = False):
    """跑注册 worker。single_attempt=True 时只跑 1 次就返回（子进程模式，避免 camoufox asyncio 泄漏）。"""
    global _success_result
    from platforms.outlook.browser_register import OutlookBrowserRegister
    while not _stop_flag.is_set():
        with _results_lock:
            if _success_result is not None or len(_results) >= total:
                return
        # single_attempt 模式下用 worker_id 作为 slot（子进程间不共享 _slot_counter）
        slot = worker_id if single_attempt else next_slot()
        proxy, node_label = resolve_proxy_for_slot(slot, proxy_source)
        if not proxy:
            safe_print(f"[w{worker_id}] slot {slot} {node_label}: 无代理，跳过")
            with _results_lock:
                _results.append({"ok": False, "error": "no_proxy", "slot": slot, "worker": worker_id, "node": node_label})
            continue
        ip = probe_ip(proxy) if proxy_source != "clash" else node_label
        safe_print(f"[w{worker_id}] slot {slot} {node_label} ip={ip} 开始注册")

        logs: list[str] = []
        def log_fn(msg: str, _w=worker_id, _s=slot, _logs=logs):
            safe_print(f"[w{_w}|s{_s}] {msg}")
            _logs.append(msg)

        worker = OutlookBrowserRegister(
            headless=False,  # camoufox headed：PX headless 下不渲染验证码按钮
            proxy=proxy,
            email_suffix=SUFFIX,
            bot_protection_wait=BOT_WAIT,
            max_captcha_retries=CAPTCHA_RETRIES,
            use_camoufox=True,  # resin IP 下 Chromium TLS 指纹被 Microsoft RST，必须用 camoufox (Firefox)
            use_protocol_proof=True,
            register_timeout=REGISTER_TIMEOUT,
            oauth_timeout=OAUTH_TIMEOUT,
            extra={},
            log_fn=log_fn,
        )
        start = time.time()
        try:
            result = worker.run()
        except Exception as exc:
            result = {"ok": False, "error": f"exception:{type(exc).__name__}", "exception": str(exc)[:300]}
        result["elapsed"] = round(time.time() - start, 1)
        result["slot"] = slot
        result["ip"] = ip
        result["node"] = node_label
        result["worker"] = worker_id
        result["logs_tail"] = logs[-15:]

        with _results_lock:
            _results.append(result)
            if result.get("ok") and result.get("refresh_token"):
                if _success_result is None:
                    _success_result = result
                safe_print(f"\n[w{worker_id}] ✅✅✅ slot {slot} {node_label} ip={ip} 注册成功 email={result.get('email')}")
                write_results_incremental()
                _stop_flag.set()
                return
        write_results_incremental()
        safe_print(f"[w{worker_id}] slot {slot} {node_label} ip={ip} 失败 error={result.get('error')} elapsed={result.get('elapsed')}s")
        # 单次模式：跑完 1 次就退出（子进程，避免 camoufox asyncio 泄漏）
        if single_attempt:
            return
        # account_blocked / ratelimit / captcha_failed 都继续换 IP 重试
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--total", type=int, default=50)
    parser.add_argument("--proxy-source", default="clash", choices=["clash", "resin", "both"], help="代理来源：clash 节点轮换 / resin / 两者交替")
    parser.add_argument("--worker-id", type=int, default=0, help="内部用：子进程模式的 worker_id（0=父进程模式）")
    args = parser.parse_args()

    # 子进程模式：只跑 1 次注册就退出（被父进程循环起的，避免 camoufox asyncio 泄漏）
    if args.worker_id > 0:
        worker_run(args.worker_id, args.total, args.proxy_source, single_attempt=True)
        write_results_incremental()
        return

    if args.concurrency <= 1:
        safe_print(f"[concurrent] 单进程模式 concurrency=1 total={args.total} proxy_source={args.proxy_source}")
        worker_run(1, args.total, args.proxy_source)
        write_results_incremental()
        success = _success_result
        _print_summary(success)
        sys.exit(0 if success else 1)

    # 多进程模式：父进程循环起子进程，每个子进程只跑 1 次注册就退出。
    # camoufox 用 asyncio，同一进程里第二次创建会冲突，所以每子进程跑 1 次就退。
    import subprocess
    safe_print(f"[concurrent] 循环子进程模式 concurrency={args.concurrency} total={args.total} proxy_source={args.proxy_source}")
    script = str(Path(__file__).resolve())
    py = sys.executable

    # 检查是否已跑完（通过日志文件统计尝试数）
    def check_done():
        try:
            log_p = Path(__file__).resolve().parent / "_outlook_concurrent.log"
            if log_p.exists():
                log_lines = log_p.read_text(encoding='utf-8', errors='replace').splitlines()
                attempts = sum(1 for l in log_lines if '失败 error=' in l or ('注册成功' in l and '✅' in l))
                if attempts >= args.total:
                    return True
        except Exception:
            pass
        return False

    completed = 0
    worker_counter = 0
    while not check_done():
        # 起一批子进程（concurrency 个），每个跑 1 次
        batch = []
        for w in range(args.concurrency):
            worker_counter += 1
            p = subprocess.Popen(
                [py, script, "--concurrency", "1", "--total", str(args.total),
                 "--proxy-source", args.proxy_source, "--worker-id", str(worker_counter)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            batch.append(p)
        for p in batch:
            p.wait()
        completed += len(batch)
        safe_print(f"[concurrent] 已完成 {completed} 次尝试")

    # 汇总
    write_results_incremental()
    success = _success_result
    _print_summary(success)
    sys.exit(0 if success else 1)


def _print_summary(success):
    safe_print(f"\n[concurrent] 结束: 共 {len(_results)} 次尝试")
    if success:
        safe_print(f"[conlook] ✅ 成功 email={success.get('email')} refresh_token={'yes' if success.get('refresh_token') else 'no'}")
        succ_out = Path(__file__).resolve().parent / "_outlook_smoke_result.json"
        succ_out.write_text(json.dumps(success, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_print(f"[concurrent] 成功结果已写入 {succ_out}")
    else:
        safe_print(f"[concurrent] ❌ 全部失败")
        from collections import Counter
        err_counter = Counter(r.get("error") for r in _results)
        safe_print(f"[concurrent] 错误分布: {dict(err_counter)}")
    safe_print(f"[concurrent] 全量结果已写入 {_RESULT_PATH}")


if __name__ == "__main__":
    main()
