# -*- coding: utf-8 -*-
"""Vercel Kasada 通过率对比：patchright (CDP-Chromium) vs ruyiPage (Firefox+BiDi)。

跑两版各 N 次（subprocess 调独立 CLI），每次从结果 JSON 读 kasada_v + appeals_status，
汇总对比：
  - x-is-human v 值分布（mean / min / max / 列表）
  - /api/appeals 响应 201 / 400 / 其他 比例（Kasada 通过率核心指标）
  - 路径分布（registered_directly / appeal_submitted / unknown / error）

核心假设验证：ruyiPage 无 CDP 暴露面，Kasada 针对 CDP 的检测对它无效，
预期 ruyiPage 版 v 值更高、400 更少、201 更多。

前置：两版都已配好环境（patchright 已装；ruyiPage + 定制 Firefox 内核已装；
resin 住宅代理 + outlook_token 邮箱池已配）。

用法：
    python scripts/vercel_kasada_compare.py --each 3 --resin --outlook
    python scripts/vercel_kasada_compare.py --each 5 --resin --outlook --headless
    python scripts/vercel_kasada_compare.py --patchright-only --each 5 --resin --outlook
    python scripts/vercel_kasada_compare.py --ruyipage-only --each 5 --resin --outlook --browser-path "E:/firefoxbrowser/firefox.exe"

结果落 scripts/_vercel_kasada_compare.json + 控制台打印对比表。
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    for _sn in ("stdout", "stderr"):
        _st = getattr(sys, _sn, None)
        if _st and hasattr(_st, "reconfigure"):
            try:
                _st.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

OUT = ROOT / "scripts" / "_vercel_kasada_compare.json"
PATCHRIGHT_RESULT = ROOT / "scripts" / "_vercel_register_result.json"
RUYIPAGE_RESULT = ROOT / "scripts" / "_vercel_ruyipage_result.json"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _run_one(backend: str, *, resin: bool, outlook: bool, headless: bool,
             timeout: int, browser_path: str, resin_account: str) -> dict:
    """subprocess 跑一次指定后端，返回结果 dict（读其结果 JSON）。"""
    if backend == "patchright":
        cmd = [sys.executable, str(ROOT / "scripts" / "test_vercel_register.py"),
               "--patchright", f"--timeout={timeout}"]
        result_file = PATCHRIGHT_RESULT
    else:
        cmd = [sys.executable, str(ROOT / "scripts" / "test_vercel_register_ruyipage.py"),
               f"--timeout={timeout}"]
        if browser_path:
            cmd.append(f"--browser-path={browser_path}")
        result_file = RUYIPAGE_RESULT
    if resin:
        cmd.append("--resin")
        if resin_account:
            cmd.append(f"--resin-account={resin_account}")
    if outlook:
        cmd.append("--outlook")
    if headless:
        cmd.append("--headless")

    log(f"[{backend}] 跑: {' '.join(cmd[1:])}")
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 120,
                              encoding="utf-8", errors="replace")
        ok = proc.returncode in (0, 1)  # 1=未注册但脚本正常跑完
        stderr_tail = (proc.stderr or "")[-800:]
        log(f"[{backend}] exit={proc.returncode} 用时={int(time.time()-t0)}s ok={ok}")
        if not ok:
            return {"backend": backend, "ok": False, "error": f"exit={proc.returncode}",
                    "stderr": stderr_tail}
    except subprocess.TimeoutExpired:
        return {"backend": backend, "ok": False, "error": "timeout"}
    except Exception as exc:
        return {"backend": backend, "ok": False, "error": repr(exc)}

    try:
        d = json.loads(result_file.read_text(encoding="utf-8"))
        d["backend"] = backend
        d["ok"] = True
        return d
    except Exception as exc:
        return {"backend": backend, "ok": False, "error": f"read result failed: {exc!r}"}


def _summarize(runs: list[dict]) -> dict:
    """汇总单后端 N 次结果。"""
    valid = [r for r in runs if r.get("ok")]
    vs = [float(r["kasada_v"]) for r in valid if r.get("kasada_v") is not None]
    statuses = [r.get("appeals_status") for r in valid]
    paths = []
    for r in valid:
        if r.get("registered_directly") or r.get("registered"):
            paths.append("registered_directly")
        elif r.get("appeal_submitted"):
            paths.append("appeal_submitted")
        elif r.get("recovery_submitted") is False:
            paths.append("recovery_rejected")
        else:
            paths.append(r.get("status") or "unknown")
    return {
        "total": len(runs),
        "succeeded": len(valid),
        "v_values": vs,
        "v_mean": round(statistics.mean(vs), 4) if vs else None,
        "v_min": min(vs) if vs else None,
        "v_max": max(vs) if vs else None,
        "appeals_201": sum(1 for s in statuses if s in (200, 201)),
        "appeals_400": sum(1 for s in statuses if s == 400),
        "appeals_other": sum(1 for s in statuses if s not in (None, 200, 201, 400)),
        "appeals_none": sum(1 for s in statuses if s is None),
        "paths": paths,
    }


def _print_table(patchright: dict, ruyipage: dict) -> None:
    """控制台打印对比表。"""
    print("\n" + "=" * 72)
    print("Vercel Kasada 通过率对比：patchright vs ruyiPage")
    print("=" * 72)
    rows = [
        ("成功跑完", f"{patchright['succeeded']}/{patchright['total']}",
         f"{ruyipage['succeeded']}/{ruyipage['total']}"),
        ("x-is-human v mean", str(patchright['v_mean']), str(ruyipage['v_mean'])),
        ("x-is-human v min", str(patchright['v_min']), str(ruyipage['v_min'])),
        ("x-is-human v max", str(patchright['v_max']), str(ruyipage['v_max'])),
        ("v 值列表", str(patchright['v_values']), str(ruyipage['v_values'])),
        ("appeals 201（过）", str(patchright['appeals_201']), str(ruyipage['appeals_201'])),
        ("appeals 400（Kasada 拒）", str(patchright['appeals_400']), str(ruyipage['appeals_400'])),
        ("appeals other", str(patchright['appeals_other']), str(ruyipage['appeals_other'])),
        ("appeals 未触发", str(patchright['appeals_none']), str(ruyipage['appeals_none'])),
        ("路径分布", str(patchright['paths']), str(ruyipage['paths'])),
    ]
    print(f"\n{'指标':<24} {'patchright':<22} {'ruyiPage':<22}")
    print("-" * 72)
    for name, pv, rv in rows:
        print(f"{name:<24} {str(pv):<22} {str(rv):<22}")
    print("=" * 72)
    # 结论提示
    pr_rate = patchright['appeals_201'] / patchright['succeeded'] if patchright['succeeded'] else 0
    rp_rate = ruyipage['appeals_201'] / ruyipage['succeeded'] if ruyipage['succeeded'] else 0
    if pr_rate or rp_rate:
        print(f"\nKasada 通过率：patchright={pr_rate:.0%}  ruyiPage={rp_rate:.0%}")
        if rp_rate > pr_rate:
            print("→ ruyiPage 通过率更高，假设成立（无 CDP 暴露面绕过 Kasada）")
        elif rp_rate < pr_rate:
            print("→ ruyiPage 通过率更低，假设不成立（需查 v 值/指纹/IP）")
        else:
            print("→ 两版持平")


def main() -> int:
    ap = argparse.ArgumentParser(description="Vercel Kasada 通过率对比 patchright vs ruyiPage")
    ap.add_argument("--each", type=int, default=3, help="每个后端跑几次（默认 3）")
    ap.add_argument("--resin", action="store_true", help="用 resin 住宅代理")
    ap.add_argument("--outlook", action="store_true", help="用 outlook 邮箱池")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--browser-path", default="", help="ruyiPage 定制 Firefox 内核路径")
    ap.add_argument("--patchright-only", action="store_true")
    ap.add_argument("--ruyipage-only", action="store_true")
    args = ap.parse_args()

    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "each": args.each}
    pr_runs, rp_runs = [], []

    def _account_seq(prefix: str) -> str:
        return prefix + "".join(__import__("random").choices(
            __import__("string").ascii_lowercase + __import__("string").digits, k=6))

    if not args.ruyipage_only:
        log(f"==== patchright 跑 {args.each} 次 ====")
        for i in range(args.each):
            r = _run_one("patchright", resin=args.resin, outlook=args.outlook,
                         headless=args.headless, timeout=args.timeout,
                         browser_path="", resin_account=_account_seq("pr"))
            pr_runs.append(r)
    if not args.patchright_only:
        log(f"==== ruyiPage 跑 {args.each} 次 ====")
        for i in range(args.each):
            r = _run_one("ruyipage", resin=args.resin, outlook=args.outlook,
                         headless=args.headless, timeout=args.timeout,
                         browser_path=args.browser_path, resin_account=_account_seq("rp"))
            rp_runs.append(r)

    pr_sum = _summarize(pr_runs) if pr_runs else {"total": 0, "succeeded": 0}
    rp_sum = _summarize(rp_runs) if rp_runs else {"total": 0, "succeeded": 0}
    report["patchright"] = pr_sum
    report["patchright_runs"] = pr_runs
    report["ruyipage"] = rp_sum
    report["ruyipage_runs"] = rp_runs
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    if pr_runs and rp_runs:
        _print_table(pr_sum, rp_sum)
    elif pr_runs:
        log(f"patchright 汇总: {json.dumps(pr_sum, ensure_ascii=False)}")
    elif rp_runs:
        log(f"ruyiPage 汇总: {json.dumps(rp_sum, ensure_ascii=False)}")

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"对比报告已写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
