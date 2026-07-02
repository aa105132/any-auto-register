"""扫 db 所有 vercel pending 号，查邮箱是否收到 Registration Approved，收到则绑卡拿key标记 registered。

循环模式：--loop --hours 8 每 1h 查一次（主人睡 8h 期间自动跑）。
单次模式：默认查一次（先查 3+5 号熟路子）。
只查 outlook 号（有 refresh token 能查邮件）；cfworker 号待主人醒处理。
"""
from __future__ import annotations

import os, sys, time, json, subprocess, email as emaillib
from pathlib import Path
from email import policy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROXY = "http://127.0.0.1:7897"


def log(m): print(f"[{time.strftime('%H:%M:%S')}] [approval] {m}", flush=True)


def get_pending_outlook_accounts() -> list[dict]:
    """取 db 所有 vercel pending 且 outlook 邮箱的号（有 refresh token 能查邮件）。"""
    from sqlmodel import Session, select
    from core.db import engine, AccountModel, AccountOverviewModel, MailboxInventoryModel
    out = []
    with Session(engine) as s:
        rows = s.exec(
            select(AccountModel, AccountOverviewModel).join(AccountOverviewModel, AccountOverviewModel.account_id == AccountModel.id)
            .where(AccountModel.platform == "vercel", AccountOverviewModel.lifecycle_status == "pending")
        ).all()
        for acc, ov in rows:
            em = acc.email or ""
            if "@outlook.com" not in em.lower():
                continue  # 只查 outlook 号（cfworker 号没 refresh token 查不了）
            # 取 outlook refresh token
            mb = s.exec(select(MailboxInventoryModel).where(MailboxInventoryModel.email == em)).first()
            if not mb or not mb.purchase_token:
                continue
            out.append({"account_id": acc.id, "email": em, "token": mb.purchase_token})
    return out


def check_approved(email: str, token: str) -> bool:
    """查 outlook 邮箱是否收到 Vercel Registration Approved 邮件。"""
    from scripts.test_vercel_register import _build_outlook_mailbox
    from core.base_mailbox import MailboxAccount
    try:
        mailbox, em = _build_outlook_mailbox(proxy=PROXY, preferred_email=email)
        account = MailboxAccount(email=em, account_id=em)
        access = mailbox._refresh_access_token(account)  # noqa
        conn = mailbox._open_imap_connection(access)  # noqa
        conn.select("INBOX", readonly=True)
        # 搜 Vercel 邮件
        typ, data = conn.uid("search", None, '(FROM "vercel" SUBJECT "Approved")')
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        if not uids:
            # 兜底搜全部再过滤
            typ, data = conn.uid("search", None, "ALL")
            all_uids = data[0].split() if typ == "OK" and data and data[0] else []
            for ub in all_uids[::-1][:15]:
                t, md = conn.uid("fetch", ub, "(BODY.PEEK[])")
                raw = b""
                for it in (md or []):
                    if isinstance(it, tuple) and len(it) == 2:
                        raw = it[1]; break
                msg = emaillib.message_from_bytes(raw, policy=policy.default)
                subj = (msg.get("Subject") or "").lower()
                frm = (msg.get("From") or "").lower()
                if "approved" in subj and "vercel" in frm and "registration" in subj:
                    return True
            return False
        return len(uids) > 0
    except Exception as exc:
        log(f"查 {email} 异常: {exc!r}")
        return False


def bindcard_create_key(email: str) -> bool:
    """subprocess 调 vercel_full_bindkey.py 绑卡拿key。"""
    script = ROOT / "scripts" / "vercel_full_bindkey.py"
    try:
        r = subprocess.run([sys.executable, str(script), email], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=480, encoding="utf-8", errors="replace")
        out = ROOT / "scripts" / "_vercel_full_bindkey_result.json"
        data = json.load(open(out, encoding="utf-8"))
        vck = str(data.get("api_key_full") or "").strip()
        if vck:
            log(f"✅ {email} 绑卡拿key成功 vck_={vck[:20]}...")
            return True
        log(f"❌ {email} 绑卡拿key失败: {data.get('error','')}")
        return False
    except Exception as exc:
        log(f"❌ {email} 绑卡异常: {exc!r}")
        return False


def run_once() -> dict:
    """单次扫所有 pending outlook 号，Approved 的绑卡拿key。"""
    accs = get_pending_outlook_accounts()
    log(f"扫到 {len(accs)} 个 outlook pending 号")
    approved = []
    for a in accs:
        if check_approved(a["email"], a["token"]):
            log(f"🎉 {a['email']} 收到 Registration Approved！开始绑卡拿key")
            if bindcard_create_key(a["email"]):
                approved.append(a["email"])
        else:
            log(f"⏳ {a['email']} 还未通过审核")
    return {"scanned": len(accs), "approved_and_bound": len(approved), "emails": approved}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="循环模式")
    ap.add_argument("--hours", type=float, default=8, help="循环时长（小时）")
    ap.add_argument("--interval", type=int, default=3600, help="循环间隔（秒）")
    args = ap.parse_args()
    if not args.loop:
        r = run_once()
        log(f"单次完成: {json.dumps(r, ensure_ascii=False)}")
        return 0
    deadline = time.time() + args.hours * 3600
    round_n = 0
    while time.time() < deadline:
        round_n += 1
        log(f"\n=== 循环第 {round_n} 轮 ===")
        try:
            run_once()
        except Exception as exc:
            log(f"循环异常: {exc!r}")
        if time.time() >= deadline:
            break
        time.sleep(args.interval)
    log("循环结束")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
