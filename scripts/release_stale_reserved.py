"""清理 Google 账号池中陈旧的 reserved_platforms 锁。

背景：注册失败时若 release 没生效（自动取号 + register 中途抛异常 / guard 不对称），
reserved_platforms 会永久残留，导致该号被后续同平台任务跳过、看似"无可用号"实则 valid 一堆。
本脚本扫描池中 reserved_platforms 含某平台、但 registered_platforms 不含该平台的"陈旧锁"并释放。

安全：默认 dry-run 只打印；--apply 才真清。--platform 过滤只清指定平台，不传则清所有陈旧锁。
警告：执行前请确认没有对应平台任务在跑，否则会撞号（两个任务拿同一 Google 账号）。

用法：
    python scripts/release_stale_reserved.py                 # dry-run，列出所有陈旧锁
    python scripts/release_stale_reserved.py --platform vellum   # dry-run，只看 vellum
    python scripts/release_stale_reserved.py --platform vellum --apply  # 真清 vellum 陈旧锁
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from core.google_account_pool import GoogleAccountPool


def _norm(platform: str) -> str:
    return (platform or "").strip().lower()


def find_stale(pool_path: Path, platform_filter: str) -> list[tuple[str, list[str]]]:
    """返回 [(email, [陈旧平台...]), ...]。陈旧 = reserved 含该平台但 registered 不含。

    只读扫描，不修改。复用 core.GoogleAccountPool 的判定逻辑（单一真理源），
    通过临时指向传入池路径来读，避免与 core 实现分叉。
    """
    import json

    data = json.load(open(pool_path, "r", encoding="utf-8"))
    target = _norm(platform_filter)
    stale: list[tuple[str, list[str]]] = []
    for item in data.get("accounts", []):
        if str(item.get("status") or "valid").strip().lower() == "invalid":
            continue
        reserved = {_norm(p) for p in (item.get("reserved_platforms") or [])}
        registered = {_norm(p) for p in (item.get("registered_platforms") or [])}
        stale_platforms = sorted(p for p in (reserved - registered) if (not target or p == target))
        if stale_platforms:
            stale.append((str(item.get("email") or ""), stale_platforms))
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 Google 账号池陈旧 reserved_platforms 锁")
    parser.add_argument("--pool", default=str(ROOT / "output" / "google_accounts_pool.json"), help="池 JSON 路径")
    parser.add_argument("--platform", default="", help="只清理指定平台（默认全部陈旧锁）")
    parser.add_argument("--apply", action="store_true", help="实际执行清理；不传则 dry-run")
    args = parser.parse_args()

    pool_path = Path(args.pool)
    if not pool_path.exists():
        print(f"[ERROR] 池文件不存在: {pool_path}")
        return 2

    stale = find_stale(pool_path, args.platform)
    if not stale:
        print("[OK] 未发现陈旧 reserved 锁，池是干净的。")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    label = f"平台 '{args.platform}'" if args.platform else "全部平台"
    print(f"[{mode}] 发现 {len(stale)} 个号带陈旧 reserved 锁（{label}）")
    for email, platforms in stale:
        print(f"  {email} -> {platforms}")

    if not args.apply:
        print(f"\n[DRY-RUN] 未实际清理。确认无误后加 --apply 执行：")
        cmd = f"python scripts/release_stale_reserved.py --apply"
        if args.platform:
            cmd += f" --platform {args.platform}"
        print(f"  {cmd}")
        print("[警告] 执行前请确认没有对应平台任务在跑，否则会撞号。")
        return 0

    # 真清：复用 core.release_stale 锁内批量清理，单一真理源。
    pool = GoogleAccountPool()
    result = pool.release_stale(platform_filter=args.platform)
    released = sum(len(platforms) for _, platforms in result)
    for email, platforms in result:
        for p in platforms:
            print(f"  [released] {email} <- {p}")
    print(f"\n[APPLY] 已释放 {released} 个陈旧锁。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
