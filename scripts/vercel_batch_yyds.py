"""yyds mail 批量注册 Vercel（路径A直接注册成功，不用 appeals 工单等审核）。

yyds 域名（api.qwen3-30b-a3b.xyz 等）没被 Vercel 风控，OTP 通过直接进 onboarding=registered。
5 并发 + 撞限流自动切 clash 节点。成功自动 save_account registered。
补到 1000 个 vercel 号。
"""
from __future__ import annotations

import os, sys, json, time, subprocess, threading, re
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

OUT = Path(os.environ.get("VERCEL_YYDS_OUT") or (ROOT / "scripts" / "_vercel_batch_yyds_result.json"))
CONCURRENCY = 5
_LIMIT_THRESHOLD = 2

_lock = threading.Lock()
_limit_streak = {"count": 0, "node_idx": 0}


def log(m): print(f"[{time.strftime('%H:%M:%S')}] [yydsbatch] {m}", flush=True)


def _check_and_switch_node(limited: bool) -> None:
    global _limit_streak
    with _lock:
        if limited:
            _limit_streak["count"] += 1
            if _limit_streak["count"] >= _LIMIT_THRESHOLD:
                _limit_streak["node_idx"] = (_limit_streak["node_idx"] + 1) % 31
                _limit_streak["count"] = 0
                idx = _limit_streak["node_idx"]
            else:
                return
        else:
            _limit_streak["count"] = 0
            return
    try:
        sys.path.insert(0, str(ROOT))
        from scripts.clash_switch import switch_by_index, get_exit_ip
        node = switch_by_index(idx)
        ip = get_exit_ip()
        log(f"🔄 撞限流切节点 idx={idx} -> IP={ip}")
        time.sleep(5)
    except Exception as exc:
        log(f"切节点异常: {exc!r}")


def run_one(idx, total, stats, results):
    tag = os.environ.get("VERCEL_YYDS_TAG") or "yyds"
    log_file = ROOT / "scripts" / f"_vercel_batch_log_{tag}{idx}.log"
    result_file = ROOT / "scripts" / f"_vercel_batch_result_{tag}{idx}.json"
    # 用好域名轮转（scripts/_vercel_good_domains.json，能直接进 onboarding 的域名）
    domain_arg = ""
    try:
        import json as _j
        gd = _j.load(open(ROOT / "scripts" / "_vercel_good_domains.json", encoding="utf-8"))
        if gd: domain_arg = gd[idx % len(gd)]
    except Exception:
        pass
    cmd = [sys.executable, str(ROOT / "scripts" / "test_vercel_register.py"),
           "--yyds", "--patchright", "--proxy", "http://127.0.0.1:7897", "--timeout", "240"]
    if domain_arg:
        cmd += ["--domain", domain_arg]
    env = dict(os.environ)
    env["VERCEL_RESULT_OUT"] = str(result_file)
    entry = {"status": "error", "registered": False, "registered_directly": False,
             "appeals_status": None, "appeal_submitted": False, "log": log_file.name}
    try:
        with open(log_file, "w", encoding="utf-8", errors="replace") as lf:
            subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, text=True,
                           timeout=420, encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        entry["status"] = "timeout"
    try:
        data = json.load(open(result_file, encoding="utf-8"))
        entry["email"] = data.get("email", "")
        entry["status"] = data.get("status", "error")
        entry["registered"] = data.get("registered", False)
        entry["registered_directly"] = data.get("registered_directly", False)
        entry["appeals_status"] = data.get("appeals_status")
        entry["appeal_submitted"] = data.get("appeal_submitted", False)
    except Exception:
        data = {}
    # log 兜底
    if not entry.get("email"):
        try:
            logtxt = log_file.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"使用 yyds 邮箱: (\S+)", logtxt)
            if m: entry["email"] = m.group(1)
            if "registered_directly" in logtxt or "dashboard=True" in logtxt:
                entry["registered"] = True; entry["registered_directly"] = True
                if entry["status"] in ("error","unknown","timeout"): entry["status"]="registered_directly"
        except Exception:
            pass
    # 自动登记 db（registered 直接存，pending 工单也存）
    email = entry.get("email", "")
    if email:
        try:
            from core.base_platform import Account, AccountStatus
            from core.db import save_account
            st = AccountStatus.REGISTERED if entry["registered"] else AccountStatus.PENDING
            extra = {"mail_provider": "yyds_mail", "mail_domain": email.split("@")[1] if "@" in email else "",
                     "registered": entry["registered"], "registered_directly": entry["registered_directly"],
                     "appeal_submitted": entry["appeal_submitted"], "appeal_stage": "direct" if entry["registered"] else "submitted",
                     "api_key": "", "ai_api_token": "",
                     "api_base": "https://ai-gateway.vercel.sh/v1", "native_api_base": "https://api.vercel.com",
                     "auth_header": "Authorization", "auth_scheme": "Bearer vck_...",
                     "site_url": "https://vercel.com", "dashboard_url": "https://vercel.com/dashboard"}
            save_account(Account(platform="vercel", email=email, password="", user_id="", token="", status=st, extra=extra))
        except Exception as exc:
            log(f"登记异常 {email}: {exc!r}")
    with _lock:
        results.append(entry)
        stats["total"] += 1
        if entry["registered"]: stats["registered_directly"] += 1
        if entry["appeal_submitted"]: stats["appeal_submitted"] += 1
        if entry["appeals_status"] in (200, 201): stats["appeal_201"] += 1
        if entry["status"] in ("error", "unknown", "timeout") and not entry["registered"] and not entry["appeal_submitted"]:
            stats["error"] += 1
        OUT.write_text(json.dumps({"results": results, "stats": stats}, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[{idx}/{total}] {email or '?'}: status={entry['status']} registered={entry['registered']}")
    # 撞限流切节点（限流=没注册没工单）
    _check_and_switch_node(not entry["registered"] and not entry["appeal_submitted"])
    return entry


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=922)
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = ap.parse_args()
    log(f"跑 {args.count} 个 yyds 号，{args.concurrency} 并发")
    stats = {"total": 0, "registered_directly": 0, "appeal_submitted": 0, "appeal_201": 0, "error": 0}
    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {ex.submit(run_one, i, args.count, stats, results): i for i in range(1, args.count + 1)}
        for fut in as_completed(futs):
            try: fut.result()
            except Exception as exc: log(f"任务异常: {exc!r}")
    log(f"\n=== 最终统计 ===\n{json.dumps(stats, ensure_ascii=False, indent=2)}")
    OUT.write_text(json.dumps({"results": results, "stats": stats}, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结果已写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
