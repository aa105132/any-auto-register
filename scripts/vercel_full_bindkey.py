"""Vercel 完整绑卡+建key+送额度脚本（登录→Stripe绑卡→纯协议建key→调chat送$5）。

给审核通过但未绑卡的号跑。流程：
1. playwright 本地7897登录 vercel.com/login（邮箱OTP keyword=Vercel+before_ids）→ 拿 vcp_ token
2. 导航 AI Gateway → Add a Card → Stripe iframe 填卡号/有效期/CVC + 地址组 country=US/state select → Continue 绑卡
3. 纯协议 POST /api/api-keys 建 vck_ key
4. 调 ai-gateway chat/completions 送 $5 免费额度
5. 查余额验证

用法：python scripts/vercel_full_bindkey.py <email>
"""
from __future__ import annotations

import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from platforms.vercel.protocol_mailbox import _build_outlook_mailbox, _wait_email_otp, _click_continue_with_email_entry
from platforms.vercel.core import page_state, fill_email, enter_otp, LOGIN_URL, VercelClient
from core.base_mailbox import MailboxAccount, create_mailbox
from core.credit_card_pool import CreditCardPool

EMAIL = sys.argv[1] if len(sys.argv) > 1 else "MillwoodAndrepont446@outlook.com"
PROXY_LOCAL = "http://127.0.0.1:7897"
# 结果文件：优先用 VERCEL_BINDKEY_OUT 环境变量（per-driver/per-email 隔离，避免多驱动并发写冲突）；
# 不设则用默认共享文件（向后兼容老驱动）。
_OUT_ENV = os.environ.get("VERCEL_BINDKEY_OUT", "").strip()
OUT = Path(_OUT_ENV) if _OUT_ENV else (ROOT / "scripts" / "_vercel_full_bindkey_result.json")


def _lookup_mail_provider(email: str) -> tuple[str, str]:
    """从 db 查 account 的 mail_provider + mail_domain（legacy_extra）。返回 (provider, domain)。"""
    from sqlmodel import Session, select
    from core.db import engine, AccountModel, AccountOverviewModel
    with Session(engine) as s:
        rows = s.exec(select(AccountModel, AccountOverviewModel).join(
            AccountOverviewModel, AccountOverviewModel.account_id == AccountModel.id
        ).where(AccountModel.platform == "vercel", AccountModel.email == email)).all()
        for a, o in rows:
            le = o.get_summary().get("legacy_extra") or {}
            if isinstance(le, dict):
                return str(le.get("mail_provider") or ""), str(le.get("mail_domain") or "")
    return "", ""


def _build_mailbox_for_email(proxy: str, email: str, log_fn=print):
    """按 account 的 mail_provider 分发建 mailbox（outlook/cfworker/yyds_mail）。返回 (mailbox, email)。"""
    from core.config_store import config_store
    provider, domain = _lookup_mail_provider(email)
    log_fn(f"[full] mail_provider={provider or '(未知)'} domain={domain or '(未知)'}")
    if provider == "outlook" or email.lower().endswith("@outlook.com"):
        return _build_outlook_mailbox(proxy=proxy, preferred_email=email, log_fn=log_fn)
    if provider == "cfworker":
        extra = {
            "cfworker_api_url": config_store.get("cfworker_api_url", ""),
            "cfworker_admin_token": config_store.get("cfworker_admin_token", ""),
            "cfworker_domain": domain or config_store.get("cfworker_domain", ""),
            "cfworker_fingerprint": config_store.get("cfworker_fingerprint", ""),
            "cfworker_auth_mode": "admin_token",
            "mail_provider": "cfworker",
        }
        mb = create_mailbox(provider="cfworker", extra=extra, proxy=proxy)
        log_fn(f"[full] cfworker mailbox: api={extra['cfworker_api_url']} domain={extra['cfworker_domain']}")
        return mb, email
    if provider == "yyds_mail":
        extra = {
            "yyds_mail_api_base_url": config_store.get("yyds_mail_api_base_url", "") or "https://maliapi.215.im",
            "yyds_mail_api_key": config_store.get("yyds_mail_api_key", ""),
            "yyds_mail_prefix": "",
            "yyds_mail_domain": domain,
            "mail_provider": "yyds_mail",
        }
        mb = create_mailbox(provider="yyds_mail", extra=extra, proxy=proxy)
        # 老号临时邮箱可能已过期，ensure_inbox 重建（409=仍存活，API Key 即可查；201=重建成功）
        try:
            tok = mb.ensure_inbox(email)
            log_fn(f"[full] yyds_mail ensure_inbox: domain={domain} recreated={'是' if tok else '否(仍存活)'}")
        except Exception as exc:
            log_fn(f"[full] yyds_mail ensure_inbox 异常: {exc!r}")
        return mb, email
    log_fn(f"[full] 未知 mail_provider={provider!r}，尝试 outlook 兜底")
    return _build_outlook_mailbox(proxy=proxy, preferred_email=email, log_fn=log_fn), email


def log(msg: str) -> None:
    try:
        print(f"[{time.strftime('%H:%M:%S')}] [full] {msg}", flush=True)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(f"[{time.strftime('%H:%M:%S')}] [full] {msg}\n".encode("utf-8", "replace"))
        sys.stdout.buffer.flush()


def pick_card() -> dict:
    """优先银联 6227 卡绑 vercel（不触发 3DS），轮转每次换一张。

    实测：mastercard/visa 美国卡 confirm 返回 requires_action(3DS) 纯协议过不了；
    银联 6227 卡不触发 3DS 能直接 succeeded。卡池 511 张银联 + 1 张 mastercard，
    优先轮转银联，mastercard 仅兜底。
    """
    p = CreditCardPool()
    union_cards = []
    other_cards = []
    for c in p.list_all():
        d = c.dict() if hasattr(c, "dict") else dict(c)
        num = str(d.get("number", ""))
        if not num or str(d.get("status", "valid")).lower() == "invalid":
            continue
        if num.startswith("6227"):
            union_cards.append(d)
        else:
            other_cards.append(d)
    all_cards = union_cards if union_cards else other_cards
    if not all_cards:
        return {}
    # 轮转：每次调用取下一张（按全局 idx）
    idx_file = ROOT / "scripts" / "_vercel_card_idx.txt"
    try:
        idx = int(idx_file.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        idx = 0
    card = all_cards[idx % len(all_cards)]
    try:
        idx_file.write_text(str(idx + 1), encoding="utf-8")
    except Exception:
        pass
    return card


def main() -> int:
    card = pick_card()
    number = str(card.get("number", ""))
    exp_month = str(card.get("exp_month", "")).zfill(2)
    exp_year = str(card.get("exp_year", ""))
    cvv = str(card.get("cvv", ""))
    name = card.get("name", "Zo User")
    address = card.get("address", "")
    city = card.get("city", "")
    state = card.get("state", "Oregon")
    postal = card.get("postal_code", "")
    # state name → 代码（Stripe administrativeArea select 要 OR/CA 等）
    STATE_CODE = {"Oregon": "OR", "California": "CA", "Washington": "WA", "New York": "NY", "Texas": "TX", "Florida": "FL"}
    state_code = STATE_CODE.get(state, state[:2].upper() if state else "OR")
    log(f"账号: {EMAIL} | 卡: {number[:6]}****{number[-4:]} {exp_month}/{exp_year} cvv={cvv} addr={address},{city},{state_code} {postal}")

    mailbox, em = _build_mailbox_for_email(proxy=PROXY_LOCAL, email=EMAIL, log_fn=log)
    acc = MailboxAccount(email=em, account_id=em)
    before_ids = mailbox.get_current_ids(acc)
    log(f"登录前基线: {len(before_ids)} 封")

    # 推断 team slug（登录后 url 用）
    team_slug = em.split("@")[0].lower() + "s-projects"

    from playwright.sync_api import sync_playwright
    from contextlib import contextmanager

    @contextmanager
    def _ctx():
        pw = sync_playwright().start()
        p = urlparse(PROXY_LOCAL)
        br = pw.chromium.launch(headless=False, proxy={"server": f"{p.scheme}://{p.hostname}:{p.port}"},
            args=["--disable-dev-shm-usage", "--disable-default-apps", "--disable-extensions",
                  "--no-first-run", "--no-default-browser-check", "--no-sandbox", "--mute-audio"])
        try: yield br, pw
        finally:
            try: br.close()
            except Exception: pass
            try: pw.stop()
            except Exception: pass

    result = {"email": em, "card": f"{number[:6]}****{number[-4:]}", "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with _ctx() as (br, pw):
        ctx = br.new_context(viewport={"width": 1366, "height": 800}, locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
        page = ctx.new_page()

        # 1. 登录
        for _g in range(4):
            try:
                page.goto(LOGIN_URL, wait_until="commit", timeout=60000); break
            except Exception as exc:
                log(f"login goto 重试 {_g+1}: {str(exc)[:80]}"); page.wait_for_timeout(2000)
        page.wait_for_timeout(3500)
        for _e in range(12):
            if _click_continue_with_email_entry(page, log_fn=log):
                page.wait_for_timeout(2000)
                if page_state(page)["has_email_input"]: break
            page.wait_for_timeout(3000)
        fill_email(page, em, log_fn=log)
        page.wait_for_timeout(5000)
        for _ in range(8):
            st = page_state(page)
            if st["has_otp_input"] or st["otp_sent"]: break
            page.wait_for_timeout(2500)
        otp = _wait_email_otp(mailbox, acc, timeout=180, before_ids=before_ids, log_fn=log)
        if not otp:
            result["error"] = "未收到登录 OTP"; OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); return 1
        log(f"收到 OTP: {otp}")
        enter_otp(page, otp)
        page.wait_for_timeout(8000)
        log(f"登录后 url={page.url[:80]}")

        # 登录后若在 onboarding/team-selection 页要选 Hobby plan（不然进 Pro 计费 / 卡 onboarding 拿不到 vcp_）
        cur_url_low = (page.url or "").lower()
        body_low = ""
        try: body_low = page.inner_text("body", timeout=3000).lower()
        except Exception: pass
        # url 优先：若已进 team 仪表盘（vercel.com/<slug>/... 且非 onboarding/login/new），
        # 说明已建 team，强制跳过 onboarding 分支——避免 body 含 "hobby" 关键词误判进
        # onboarding 分支点错元素导致 page 关闭崩溃。
        _after_host = cur_url_low.split("vercel.com/", 1)[1] if "vercel.com/" in cur_url_low else ""
        _first_seg = _after_host.split("/")[0] if _after_host else ""
        has_team_slug = bool(_first_seg) and _first_seg not in ("login", "signup", "new", "onboarding", "join", "accountrecovery", "")
        onboarding_detected = (not has_team_slug) and ("/onboarding" in cur_url_low or "tell us about" in body_low or "hobby" in body_low or "choose your plan" in body_low or "what's your name" in body_low)
        result["post_login_url"] = page.url
        result["onboarding_detected"] = onboarding_detected
        if onboarding_detected:
            log("[vercel] 检测到 onboarding/plan 选择页，选 Hobby")
            try:
                import random as _r, string as _st
                # onboarding 页 DOM 实测：选项是 div.selector-option-module__*__suffix，innerText 严格 "Hobby"/"Pro"
                # 默认选 Pro（"Continuing will start a Pro plan trial"），必须主动点 Hobby 选项卡
                hobby_clicked = False
                # 主路径：Playwright locator 点含 Hobby 文字的选项卡（用精确 selector 避免父容器）
                # selector-option-module 是动态 class，用 [class*='selector-option'] 匹配
                for sel in ("div[class*='selector-option']:has-text('Hobby')",
                            "div:has-text('Hobby'):not(:has(div))",
                            "label:has-text('Hobby')", "button:has-text('Hobby')"):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            loc.click(timeout=6000); hobby_clicked = True
                            log(f"[vercel] 点 Hobby (sel={sel})"); page.wait_for_timeout(2000); break
                    except Exception: continue
                if not hobby_clicked:
                    # 兜底：evaluate 精确点 innerText=='Hobby' 的最小元素
                    try:
                        clicked = page.evaluate("""()=>{
                          const els=Array.from(document.querySelectorAll('div,label,button,span'));
                          let best=null,bestLen=999;
                          for(const e of els){const t=(e.innerText||'').trim();
                            if(t==='Hobby'&&t.length<bestLen){best=e;bestLen=t.length;}}
                          if(best){best.click();return 'ok';} return null;
                        }""")
                        if clicked: hobby_clicked=True; log("[vercel] 兜底 evaluate 点 Hobby")
                        page.wait_for_timeout(2000)
                    except Exception: pass
                # 填 Team Name（随机，避免 my-projects 被占）
                team_name = "Team" + "".join(_r.choices(_st.ascii_lowercase + _st.digits, k=8))
                for sel in ("input[aria-label='Team Name']", "input[placeholder='ACME']", "input[name='teamName']"):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0:
                            loc.click(timeout=4000)
                            try: loc.press("Control+a"); loc.press("Delete")
                            except: pass
                            page.keyboard.type(team_name, delay=50); page.wait_for_timeout(800)
                            log(f"[vercel] 填 Team Name: {team_name}"); break
                    except Exception: continue
                page.wait_for_timeout(1500)
                # 点 Continue（Hobby 选了 + Team Name 填了才 enable）
                for sel in ("button:has-text('Continue')", "button[type='submit']"):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() > 0 and not loc.is_disabled(timeout=3000):
                            loc.click(timeout=6000); log(f"[vercel] 点 Continue (sel={sel})"); page.wait_for_timeout(6000); break
                    except Exception: continue
                page.wait_for_timeout(3000)
                log(f"[vercel] 选 Hobby 后 url={page.url[:80]}")
            except Exception as exc:
                log(f"[vercel] 选 Hobby 异常: {exc!r}")

        # 拿 vcp_ token（存完整，不截断）
        cookies = {c["name"]: c["value"] for c in ctx.cookies() if "vercel.com" in (c.get("domain") or "")}
        vcp_token = unquote(cookies.get("authorization", "")).replace("Bearer ", "")
        result["vcp_token"] = vcp_token  # 完整存
        if not vcp_token:
            result["error"] = "未拿到 vcp_ token"; OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); return 1
        log(f"vcp_ token: {vcp_token[:25]}...")

        # 从登录后 url 拿 team slug
        cur_url = page.url
        if "/" in cur_url and "vercel.com/" in cur_url:
            slug_part = cur_url.split("vercel.com/", 1)[1].split("/")[0]
            if slug_part and slug_part != "login":
                team_slug = slug_part
        log(f"team_slug={team_slug}")

    # 2. 纯协议绑卡（bind_card_protocol，无浏览器 Stripe 填卡，避免卡被拒超时）
    client = VercelClient(proxy=PROXY_LOCAL, log_fn=log)
    team_id = client.get_team_id(vcp_token)
    result["team_id"] = team_id
    if not team_id:
        result["error"] = "未拿到 team_id"; OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); return 1
    # 先查是否已绑卡（余额查询能拿 hasVerifiedPaymentMethod）
    bal0 = client.get_credits_balance(vcp_token, team_id)
    if bal0.get("hasVerifiedPaymentMethod"):
        log("已绑卡，跳过绑卡")
        result["card_bound"] = True
    else:
        # 纯协议绑卡，3DS/requires_action 时换卡重试（最多 5 张），失败卡标 invalid 不再用
        from core.credit_card_pool import CreditCardPool as _CCP
        pool = _CCP()
        bind_ok = False
        tried_cards = []
        for _try in range(5):
            card = pick_card()
            if not card or not card.get("number"):
                result["error"] = "无可用卡（卡池耗尽或全被本会话用过）"; break
            number = str(card.get("number", ""))
            tried_cards.append(number)
            log(f"纯协议绑卡尝试 {_try+1}: {number[:6]}****{number[-4:]}")
            bind = client.bind_card_protocol(vcp_token, team_id, card)
            if bind.get("ok"):
                result["bind"] = bind; result["card_bound"] = True; bind_ok = True; break
            # 3DS / requires_action / 卡被拒 → 标 invalid 换卡重试
            err = str(bind.get("error", ""))
            is_3ds = bind.get("requires_browser") or "requires_action" in err or "3DS" in err or "authentication" in err.lower()
            log(f"绑卡失败 (try={_try+1}): {err[:80]} is_3ds={is_3ds}")
            try:
                pool.mark_invalid(card.get("_pool_id") or "", platform="vercel", reason=err[:60])
            except Exception:
                pass
            if not is_3ds:
                # 非 3DS 错误（如 attach 失败、team 问题）不换卡，直接败
                result["bind"] = bind; result["error"] = f"纯协议绑卡失败: {err}"; break
            time.sleep(2)
        if not bind_ok and not result.get("error"):
            result["error"] = f"5 张卡全 3DS/被拒，纯协议绑卡失败（tried={tried_cards}）"
        if not bind_ok:
            OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return 1

    # 3. 纯协议建 key + 送额度
    key = client.create_ai_gateway_key(vcp_token, team_id, name="auto-register")
    result["api_key"] = key[:20] + "..." if key else ""
    if not key:
        result["error"] = "建 key 失败"; OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"); return 1
    trig = client.trigger_free_credit(key)
    result["trigger_ok"] = trig.get("ok")
    bal = client.get_credits_balance(vcp_token, team_id)
    result["balance_after"] = bal
    result["api_key_full"] = key
    log(f"完整 key: {key} | 余额: {bal}")

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结果已写入 {OUT}")
    return 0 if trig.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
