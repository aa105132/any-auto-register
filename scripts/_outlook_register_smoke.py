"""Outlook 注册端到端冒烟脚本。

用法：
  python scripts/_outlook_register_smoke.py [--headed] [--camoufox] [--proxy URL] [--suffix @outlook.com]

以 headed 模式启动浏览器，跑一次完整 Outlook 注册流程，把每步日志打到 stdout，
最后把结果 JSON 写到 scripts/_outlook_smoke_result.json 便于检查。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 让 platforms.* 能 import
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true", help="有头模式（默认 true）")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--camoufox", action="store_true", help="用 Camoufox 而非 patchright")
    parser.add_argument("--proxy", default="http://127.0.0.1:7897", help="代理 URL")
    parser.add_argument("--no-proxy", action="store_true", help="不走代理")
    parser.add_argument("--suffix", default="@outlook.com", help="邮箱后缀")
    parser.add_argument("--no-protocol-proof", action="store_true", help="关闭协议级 proof 合成，纯短按")
    parser.add_argument("--bot-wait", type=int, default=11, help="bot_protection_wait 秒数")
    parser.add_argument("--captcha-retries", type=int, default=3, help="验证码重试次数")
    parser.add_argument("--register-timeout", type=int, default=300, help="注册总超时秒数")
    parser.add_argument("--oauth-timeout", type=int, default=120, help="OAuth 拿 token 超时秒数")
    args = parser.parse_args()

    headless = args.headless and not args.headed
    proxy = None if args.no_proxy else args.proxy

    from platforms.outlook.browser_register import OutlookBrowserRegister

    logs: list[str] = []

    def log_fn(msg: str):
        print(msg, flush=True)
        logs.append(msg)

    worker = OutlookBrowserRegister(
        headless=headless,
        proxy=proxy,
        email_suffix=args.suffix,
        bot_protection_wait=args.bot_wait,
        max_captcha_retries=args.captcha_retries,
        use_camoufox=args.camoufox,
        use_protocol_proof=not args.no_protocol_proof,
        register_timeout=args.register_timeout,
        oauth_timeout=args.oauth_timeout,
        extra={},
        log_fn=log_fn,
    )
    print(f"[smoke] 启动注册 headless={headless} proxy={proxy} suffix={args.suffix} camoufox={args.camoufox} protocol_proof={not args.no_protocol_proof}", flush=True)
    start = time.time()
    try:
        result = worker.run()
    except Exception as exc:
        result = {"ok": False, "error": f"exception:{type(exc).__name__}", "exception": str(exc)[:500]}
        print(f"[smoke] 注册异常: {repr(exc)[:200]}", flush=True)
    elapsed = time.time() - start
    result["elapsed_seconds"] = round(elapsed, 1)
    result["logs_tail"] = logs[-30:]
    print(f"[smoke] 注册结束 ok={result.get('ok')} elapsed={elapsed:.1f}s error={result.get('error', '')}", flush=True)

    out = Path(__file__).resolve().parent / "_outlook_smoke_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[smoke] 结果已写入 {out}", flush=True)

    if result.get("ok") and result.get("refresh_token"):
        print(f"[smoke] ✅ 注册成功 email={result.get('email')} refresh_token={'yes' if result.get('refresh_token') else 'no'}", flush=True)
        sys.exit(0)
    else:
        print(f"[smoke] ❌ 注册失败 error={result.get('error')}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
