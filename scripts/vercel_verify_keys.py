"""Vercel vck_ key 批量验证：对所有绑卡成功的号调 ai-gateway 确认 key 可用 + 模型回复。

用法：
  python scripts/vercel_verify_keys.py [--concurrency 15] [--proxy http://127.0.0.1:7897]

背景：inline绑卡后 vck_ key 有 5-10min 同步延迟，绑卡块当场 trigger 会 403。
本脚本在 500 个跑完后 cron 补验证：调 ai-gateway chat/completions(max_tokens=20)，
成功 → 写 _vercel_verified_keys.txt + 更新 db summary trigger_verified=True；
失败 → 保留 trigger_pending，下次 cron 补。主人要求"调用 key 成功 模型成功回复的写入 db"。
"""
from __future__ import annotations
import argparse, json, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests
from sqlmodel import Session, select

from core.db import engine, AccountModel, AccountCredentialModel, AccountOverviewModel
from platforms.vercel.core import VercelClient

AI_GATEWAY = "https://ai-gateway.vercel.sh/v1/chat/completions"


def load_vck_accounts() -> list[tuple[int, str, str]]:
    """返回 [(account_id, email, vck_key)] 所有 vercel 号有 vck_ key。"""
    s = Session(engine)
    accs = s.exec(select(AccountModel).where(AccountModel.platform == "vercel")).all()
    out = []
    for a in accs:
        c = s.exec(select(AccountCredentialModel).where(
            AccountCredentialModel.account_id == a.id,
            AccountCredentialModel.key == "api_key",
        )).first()
        vck = str(c.value or "") if c else ""
        if vck.startswith("vck_"):
            out.append((a.id, a.email, vck))
    return out


def _call_ai_gateway(vck: str, proxy: str, timeout: int = 30) -> tuple[bool, str]:
    """调 ai-gateway chat，返回 (ok, content_or_error)。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.post(AI_GATEWAY,
            headers={"Authorization": f"Bearer {vck}", "Content-Type": "application/json"},
            json={"model": "openai/gpt-4.1-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20},
            proxies=proxies, timeout=timeout)
        if r.status_code == 200:
            d = r.json() or {}
            content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            return True, content
        return False, f"status={r.status_code} body={r.text[:120]}"
    except Exception as exc:
        return False, f"exc={exc!r}"[:120]


def _rebuild_key_from_cookies(account_id: int, proxy: str) -> str:
    """vck_ 403(旧key被缓存无卡)时，取 cookies vcp_ 重建 key 绕过缓存。返回新 vck_ 或空。"""
    import ast
    from urllib.parse import unquote
    with Session(engine) as s:
        c = s.exec(select(AccountCredentialModel).where(
            AccountCredentialModel.account_id == account_id,
            AccountCredentialModel.key == "cookies",
        )).first()
        if not c: return ""
        try: cd = ast.literal_eval(c.value or "{}")
        except Exception: return {}
        auth = cd.get("authorization", "")
        vcp = unquote(auth).replace("Bearer ", "") if auth else ""
        if not vcp or len(vcp) < 50: return ""
    cl = VercelClient(proxy=proxy, log_fn=lambda m: None)
    team = cl.get_team_id(vcp)
    if not team: return ""
    return cl.create_ai_gateway_key(vcp, team, name="auto-register-v2") or ""


def _check_balance_has_card(account_id: int, proxy: str) -> bool | None:
    """查 db cookies vcp_ → balance.hasVerifiedPaymentMethod。返回 True/False/None(查不到)。"""
    import ast
    from urllib.parse import unquote
    with Session(engine) as s:
        c = s.exec(select(AccountCredentialModel).where(
            AccountCredentialModel.account_id == account_id,
            AccountCredentialModel.key == "cookies",
        )).first()
        if not c: return None
        try: cd = ast.literal_eval(c.value or "{}")
        except Exception: return None
        auth = cd.get("authorization", "")
        vcp = unquote(auth).replace("Bearer ", "") if auth else ""
        if not vcp or len(vcp) < 50: return None
    cl = VercelClient(proxy=proxy, log_fn=lambda m: None)
    team = cl.get_team_id(vcp)
    if not team: return None
    bal = cl.get_credits_balance(vcp, team)
    return bool(bal.get("hasVerifiedPaymentMethod"))


def verify_one(account_id: int, email: str, vck: str, proxy: str, timeout: int = 30) -> dict:
    """验证 vck_：先调旧 key；403 时先查 balance，hasCard=True 才重建 key（绕缓存），
    hasCard=False 直接判 invalid（绑卡真失败，重建无意义，旧逻辑产出的无效 vck_）。"""
    ok, info = _call_ai_gateway(vck, proxy, timeout)
    if ok:
        return {"ok": True, "account_id": account_id, "email": email, "reply": info, "vck": vck, "rebuilt": False}
    # 403：先查 balance 确认是否真绑卡
    has_card = _check_balance_has_card(account_id, proxy)
    if has_card is False:
        return {"ok": False, "account_id": account_id, "email": email, "status": "no_card",
                "body": "绑卡未生效(hasVerifiedPaymentMethod=False)，旧无效 vck_", "vck": vck}
    if has_card is None:
        return {"ok": False, "account_id": account_id, "email": email, "status": "no_vcp", "body": info, "vck": vck}
    # hasCard=True：旧 key 被缓存无卡，重建 key 绕缓存
    new_key = _rebuild_key_from_cookies(account_id, proxy)
    if new_key:
        ok2, info2 = _call_ai_gateway(new_key, proxy, timeout)
        if ok2:
            return {"ok": True, "account_id": account_id, "email": email, "reply": info2, "vck": new_key, "rebuilt": True}
        return {"ok": False, "account_id": account_id, "email": email, "status": "rebuild_fail", "body": info2, "vck": new_key}
    return {"ok": False, "account_id": account_id, "email": email, "status": "rebuild_no_key", "body": info, "vck": vck}


def mark_verified(account_id: int, new_vck: str = ""):
    """更新 db：summary trigger_verified=True；若重建 key 则同步更新 api_key/ai_api_token/legacy_token。"""
    with Session(engine) as s:
        ov = s.exec(select(AccountOverviewModel).where(AccountOverviewModel.account_id == account_id)).first()
        if ov:
            sm = ov.get_summary()
            sm["trigger_verified"] = True
            sm["trigger_pending"] = False
            ov.set_summary(sm)
            s.add(ov)
        if new_vck:
            for keyname in ("api_key", "ai_api_token", "legacy_token"):
                c = s.exec(select(AccountCredentialModel).where(
                    AccountCredentialModel.account_id == account_id,
                    AccountCredentialModel.key == keyname,
                )).first()
                if c:
                    c.value = new_vck
                    s.add(c)
        s.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=15)
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--limit", type=int, default=0, help="只验证最近 N 个(0=全部)")
    ap.add_argument("--skip-verified", action="store_true", help="跳过 db 已 trigger_verified 的号")
    args = ap.parse_args()

    accs = load_vck_accounts()
    if args.limit:
        accs = accs[-args.limit:]
    # 跳过已 verified（多轮补验证只攻未 verified 的）
    if args.skip_verified:
        with Session(engine) as s:
            verified_ids = set()
            ovs = s.exec(select(AccountOverviewModel).where(
                AccountOverviewModel.account_id.in_([a[0] for a in accs]))).all()
            for ov in ovs:
                if ov.get_summary().get("trigger_verified"):
                    verified_ids.add(ov.account_id)
        accs = [a for a in accs if a[0] not in verified_ids]
    print(f"[verify] 待验证 vck_ 号: {len(accs)}，并发 {args.concurrency}", flush=True)

    verified, failed = [], []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futs = {pool.submit(verify_one, aid, em, vck, args.proxy): (aid, em) for aid, em, vck in accs}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r.get("ok"):
                verified.append(r)
                mark_verified(r["account_id"], r["vck"] if r.get("rebuilt") else "")
            else:
                failed.append(r)
            if i % 50 == 0 or i == len(accs):
                print(f"  进度 {i}/{len(accs)} verified={len(verified)} failed={len(failed)} elapsed={int(time.time()-t0)}s", flush=True)

    # 导出累计 verified vck_ keys（db 所有 trigger_verified 的号，含本轮+历史）
    verified_keys_all = []
    with Session(engine) as s:
        all_accs = s.exec(select(AccountModel).where(AccountModel.platform == "vercel")).all()
        for a in all_accs:
            ov = s.exec(select(AccountOverviewModel).where(AccountOverviewModel.account_id == a.id)).first()
            if ov and ov.get_summary().get("trigger_verified"):
                c = s.exec(select(AccountCredentialModel).where(
                    AccountCredentialModel.account_id == a.id,
                    AccountCredentialModel.key == "api_key",
                )).first()
                vck = str(c.value or "") if c else ""
                if vck.startswith("vck_"):
                    verified_keys_all.append(vck)
    out = ROOT / "scripts" / "_vercel_verified_keys.txt"
    out.write_text("\n".join(verified_keys_all), encoding="utf-8")
    final_out = ROOT / "scripts" / "_vercel_final_500_keys.txt"
    final_out.write_text("\n".join(verified_keys_all), encoding="utf-8")
    print(f"\n[verify] 完成: 本轮 verified={len(verified)} failed={len(failed)} 耗时={int(time.time()-t0)}s", flush=True)
    print(f"[verify] 累计 verified keys: {len(verified_keys_all)} 导出: {out} + {final_out}", flush=True)
    if failed[:5]:
        print("[verify] 失败样例:", flush=True)
        for r in failed[:5]:
            print(f"  {r['email']} status={r.get('status')} err={(r.get('body') or r.get('error') or '')[:80]}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
