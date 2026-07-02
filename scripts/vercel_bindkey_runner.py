"""Vercel pending 工单号 绑卡拿key 自动巡检（多轮，约 8h）。

每轮：查 pending 的 outlook 号 → 逐个 execute_action('bind_card_create_key')
→ 成功自动 save_account 标 registered + 存 vck_。execute_action 内部 subprocess.run
调 scripts/vercel_full_bindkey.py（每号独立子进程，7min 超时，浏览器在子进程内开关）。
轮间等 1h 再扫（新通过审核的号）。每轮结束写 scripts/_vercel_bindkey_progress.json。

逐个跑不并发（避限流）。每号间 5s 间隔。每号后清理残留 chrome（仅杀带 7897 代理端口的
playwright chromium，不动用户其他浏览器）。遇套接字队列满自动停残留再继续。

用法：python scripts/vercel_bindkey_runner.py
日志：scripts/_vercel_bindkey_runner.log（UTF-8，append）
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROGRESS = ROOT / "scripts" / "_vercel_bindkey_progress.json"
LOGFILE = ROOT / "scripts" / "_vercel_bindkey_runner.log"

MAX_TOTAL_SECONDS = 8 * 3600  # 约 8h 总预算
ROUND_WAIT_SECONDS = 3600     # 轮间等 1h
BETWEEN_ACCOUNT_SECONDS = 5   # 每号间隔
MAX_ROUNDS = 10               # 安全上限


def flog(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def cleanup_stray_chrome() -> None:
    """杀掉带 7897 代理端口的 playwright chromium 残留（不动用户其他 chrome）。"""
    try:
        import subprocess
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -match '127.0.0.1:7897' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, timeout=20)
    except Exception as e:
        flog(f"[cleanup] chrome 清理跳过: {str(e)[:80]}")


def query_pending_outlook(bound_emails: set) -> list[str]:
    """查 vercel pending 且 @outlook.com 的邮箱，排除本轮及历史已绑的。"""
    from sqlmodel import Session, select
    from core.db import engine, AccountModel, AccountOverviewModel
    with Session(engine) as s:
        rows = s.exec(
            select(AccountModel, AccountOverviewModel)
            .join(AccountOverviewModel, AccountOverviewModel.account_id == AccountModel.id)
            .where(AccountModel.platform == 'vercel',
                   AccountOverviewModel.lifecycle_status == 'pending')
        ).all()
    out = []
    for a, o in rows:
        em = (a.email or "").strip()
        if "@outlook.com" in em.lower() and em not in bound_emails:
            out.append(em)
    return out


def query_total_registered() -> int:
    from sqlmodel import Session, select, func
    from core.db import engine, AccountModel, AccountOverviewModel
    with Session(engine) as s:
        cnt = s.exec(
            select(func.count())
            .select(AccountModel)
            .join(AccountOverviewModel, AccountOverviewModel.account_id == AccountModel.id)
            .where(AccountModel.platform == 'vercel',
                   AccountOverviewModel.lifecycle_status == 'registered')
        ).first()
    return int(cnt or 0)


def run_one(email: str) -> dict:
    """对单号跑 execute_action('bind_card_create_key')，返回结果 dict。"""
    from platforms.vercel.plugin import VercelPlatform
    from core.base_platform import Account, AccountStatus, RegisterConfig
    p = VercelPlatform(config=RegisterConfig())
    p.log = lambda m: flog(f"[bind] {m}")
    acct = Account(platform='vercel', email=email, password='', user_id='',
                   token='', status=AccountStatus.PENDING, extra={})
    t0 = time.time()
    try:
        r = p.execute_action('bind_card_create_key', acct, {})
        ok = bool(r.get('ok'))
        vck = ''
        if ok:
            vck = str(r.get('data', {}).get('credential_updates', {}).get('api_key', '') or '')
        err = '' if ok else str(r.get('error', '') or '')
        return {"email": email, "ok": ok, "vck": vck, "error": err,
                "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        return {"email": email, "ok": False, "vck": "", "error": f"EXC {repr(e)[:160]}",
                "elapsed": round(time.time() - t0, 1)}


def write_progress(round_no: int, scanned: int, bound_this_round: int,
                   total_registered: int, emails_bound: list, still_pending: int,
                   round_results: list) -> None:
    data = {
        "round": round_no,
        "scanned": scanned,
        "bound_this_round": bound_this_round,
        "total_registered": total_registered,
        "emails_bound": emails_bound,
        "still_pending": still_pending,
        "updated_at": datetime.now().isoformat(),
        "round_results": round_results,
    }
    try:
        PROGRESS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        flog(f"[progress] 写入失败: {e}")


def main() -> int:
    flog("=" * 60)
    flog("Vercel pending 绑卡拿key 巡检启动")
    start = time.time()
    bound_emails: set = set()
    round_no = 0
    while round_no < MAX_ROUNDS:
        elapsed = time.time() - start
        if elapsed >= MAX_TOTAL_SECONDS:
            flog(f"总时长 {elapsed/3600:.1f}h 达上限，退出")
            break
        round_no += 1
        flog(f"--- 第 {round_no} 轮开始 (已运行 {elapsed/3600:.1f}h) ---")
        emails = query_pending_outlook(bound_emails)
        flog(f"待测 pending outlook: {len(emails)} 号")
        if not emails:
            flog("无待测号，退出")
            break
        scanned = 0
        bound_this_round = 0
        emails_bound = []
        round_results = []
        for em in emails:
            if time.time() - start >= MAX_TOTAL_SECONDS:
                flog(f"总时长达上限，本轮中断于 {em}")
                break
            flog(f"[{scanned+1}/{len(emails)}] 测 {em}")
            res = run_one(em)
            scanned += 1
            round_results.append(res)
            if res["ok"]:
                bound_this_round += 1
                emails_bound.append(em)
                bound_emails.add(em)
                flog(f"  -> OK vck={res['vck'][:20]} ({res['elapsed']}s)")
            else:
                flog(f"  -> 未通过: {res['error'][:90]} ({res['elapsed']}s)")
            # 每号后清理残留 chrome（防套接字堆积）
            cleanup_stray_chrome()
            time.sleep(BETWEEN_ACCOUNT_SECONDS)
        total_registered = query_total_registered()
        still = len(query_pending_outlook(bound_emails))
        write_progress(round_no, scanned, bound_this_round, total_registered,
                       emails_bound, still, round_results)
        flog(f"第 {round_no} 轮结束: 扫 {scanned} / 绑 {bound_this_round} / "
             f"累计 registered {total_registered} / 仍 pending {still}")
        # 轮间等待（若剩余时间不够一轮+等待就退出）
        remaining = MAX_TOTAL_SECONDS - (time.time() - start)
        if remaining < ROUND_WAIT_SECONDS + 600:
            flog(f"剩余 {remaining/60:.0f}min 不足下一轮，退出")
            break
        flog(f"轮间等 {ROUND_WAIT_SECONDS/60:.0f}min 后开始下一轮...")
        time.sleep(ROUND_WAIT_SECONDS)
    total_registered = query_total_registered()
    still = len(query_pending_outlook(bound_emails))
    flog(f"巡检结束: 累计 registered {total_registered} / 仍 pending {still} / "
         f"本轮次共绑 {len(bound_emails)} 号")
    flog("已绑号: " + ", ".join(sorted(bound_emails)) if bound_emails else "无新绑")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        flog("手动中断")
        raise
    except Exception:
        flog("runner 崩溃: " + traceback.format_exc())
        raise SystemExit(1)
