"""Turnstile solver 子进程入口：独立进程跑 solve_turnstile，避免与主线程
已持有的 Playwright sync session 冲突（'Playwright Sync API inside asyncio loop'）。

主进程在 with sync_playwright() 块内调 CdpTurnstileSolver.solve_turnstile 时，
solver 内部再开 sync_playwright 会撞 asyncio loop。用 subprocess 隔离即可。

调用：python -m services.turnstile_solver.cli_solve --provider cdp_turnstile \
        --url <page_url> --sitekey <sitekey> [--proxy <url>] [--chrome-path <p>] [--cdp-url <u>]
输出 JSON 到 stdout：{"ok": true, "token": "..."} 或 {"ok": false, "error": "..."}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True, help="captcha provider key (cdp_turnstile/yescaptcha_api/twocaptcha_api/local_solver)")
    ap.add_argument("--url", required=True, help="page URL")
    ap.add_argument("--sitekey", required=True, help="Turnstile sitekey")
    ap.add_argument("--proxy", default="", help="proxy URL")
    ap.add_argument("--chrome-path", default="", help="Chrome path (cdp_turnstile)")
    ap.add_argument("--cdp-url", default="", help="CDP url (cdp_turnstile，复用外部 Chrome)")
    ap.add_argument("--user-agent", default="", help="user agent (远程打码)")
    ap.add_argument("--yescaptcha-key", default="", help="YesCaptcha client key")
    ap.add_argument("--twocaptcha-key", default="", help="2Captcha api key")
    ap.add_argument("--solver-url", default="http://localhost:8889", help="local_solver url")
    args = ap.parse_args()

    extra: dict = {
        "chrome_path": args.chrome_path,
        "chrome_cdp_url": args.cdp_url,
        "yescaptcha_key": args.yescaptcha_key,
        "twocaptcha_key": args.twocaptcha_key,
        "solver_url": args.solver_url,
    }

    try:
        from core.base_captcha import create_captcha_solver
        solver = create_captcha_solver(args.provider, extra)
        import inspect
        params = inspect.signature(solver.solve_turnstile).parameters
        kwargs: dict = {}
        if "proxy" in params and args.proxy:
            kwargs["proxy"] = args.proxy
        if "user_agent" in params and args.user_agent:
            kwargs["user_agent"] = args.user_agent
        token = str(solver.solve_turnstile(args.url, args.sitekey, **kwargs) or "").strip()
        if token:
            print(json.dumps({"ok": True, "token": token}, ensure_ascii=False))
            return 0
        print(json.dumps({"ok": False, "error": "solver returned empty token"}, ensure_ascii=False))
        return 1
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
