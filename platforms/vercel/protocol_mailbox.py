"""Vercel 浏览器驱动注册 Worker（patchright + resin 住宅代理 + outlook 收信）。

从 scripts/test_vercel_register.py 移植验证过的完整链路，复用 platforms.vercel.core 的页面 helpers。

流程：
  1. _build_outlook_mailbox 取号（succ 降序，msvcrt 锁防并发）+ before_ids 基线。
  2. patchright（CDP-patched Chromium，过 Kasada）+ resin 代理打开 vercel.com/signup。
  3. Continue with Email → 填邮箱提交 → 观察分叉（OTP / further / recovery）。
  4a. OTP 路径：wait_email_otp（keyword="Vercel"+before_ids，避免 #000000 假码）→ 填入。
      - OTP 后进 onboarding/dashboard = 路径A 真注册成功（registered=True）。
      - OTP 后 further/verification = 路径C 走填表流。
  4b. different-method 拦截 = 路径B 走填表流。
  5. 填表流：/accountrecovery 填 name + sign-up method Google choicebox +
     verification 场景额外勾两个 "I don't know / Not Applicable" choicebox（phone + Git Provider）。
  6. evaluate btn.click() Submit Appeal → POST /api/appeals（Kasada 包装带 x-kpsdk/x-is-human）→ 201。
  7. 落地 cookie（供 4-8h 后登录绑卡）。
  8. 等申诉确认邮件（路径B abuse / 路径C Hobby Case Opened）。
  9. 返回 result dict（registered 仅路径A，appeal_submitted 路径B/C）。
"""
from __future__ import annotations

# BLAS 单线程（Windows OOM 防护），必须在 playwright import 前设。
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from platforms.vercel.core import (
    SIGNUP_URL, LOGIN_URL, AI_GATEWAY_BASE, NATIVE_API_BASE,
    log, page_state, fill_email, enter_otp, random_name,
)

# 模块级：监听 /api/appeals 请求的 Kasada 人机分 v 值和响应状态
_APPEALS_META: dict[str, Any] = {"v": None, "status": None, "request_headers": None}

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

_OUTLOOK_LOCK_PATH = ROOT / "scripts" / "_vercel_outlook.lock"


def _build_outlook_mailbox(proxy: str | None, preferred_email: str = "", log_fn=print):
    """从 mailbox_inventory 取 unused 无别名 outlook_token 号（succ 降序，msvcrt 锁防并发）。"""
    import json as _json
    import msvcrt
    from sqlmodel import Session, select
    from core.db import engine, MailboxInventoryModel
    from core.base_mailbox import create_mailbox

    def _succ(r):
        md = _json.loads(r.metadata_json or "{}")
        return int(md.get("successful_registrations") or 0)

    lockf = open(_OUTLOOK_LOCK_PATH, "w")
    succ_val = 0
    try:
        msvcrt.locking(lockf.fileno(), msvcrt.LK_LOCK, 1)
        with Session(engine) as s:
            rows = s.exec(
                select(MailboxInventoryModel).where(
                    MailboxInventoryModel.provider_key == "outlook_token",
                    MailboxInventoryModel.status == "unused",
                    MailboxInventoryModel.purchase_token != "",
                )
            ).all()
            no_alias = [r for r in rows if "+" not in (r.email or "")]
            if not no_alias:
                raise RuntimeError("mailbox_inventory 无不带别名的 outlook_token unused 号")
            if preferred_email:
                no_alias = [r for r in no_alias if r.email.lower() == preferred_email.lower()]
                if not no_alias:
                    raise RuntimeError(f"指定的 outlook 号 {preferred_email} 不在 unused 无别名列表")
            no_alias.sort(key=_succ, reverse=True)
            r = no_alias[0]
            acc = _json.loads(r.metadata_json or "{}")
            acc["id"] = r.id
            acc["email"] = r.email
            acc["purchase_token"] = r.purchase_token
            succ_val = _succ(r)
    finally:
        try:
            msvcrt.locking(lockf.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        lockf.close()
    extra = {
        "outlook_email": acc.get("email", ""),
        "outlook_password": acc.get("password", ""),
        "outlook_client_id": acc.get("client_id", ""),
        "outlook_refresh_token": acc.get("purchase_token") or acc.get("refresh_token", ""),
        "mail_provider": "outlook_token",
    }
    log_fn(f"[vercel] outlook DB 取号(无别名): {acc.get('email')} id={acc.get('id')} succ={succ_val} rt_len={len(str(extra['outlook_refresh_token'] or ''))}")
    return create_mailbox(provider="outlook_token", extra=extra, proxy=proxy), acc.get("email", "")


def _resolve_resin_proxy(account: str = "") -> str | None:
    """从 config_store 解析 resin 住宅代理 URL（account 后缀换 session 换 IP）。"""
    from core.config_store import config_store
    from core.resin_proxy import resolve_resin_proxy_config
    token = config_store.get("resin_token", "") or config_store.get("resin_password", "")
    if not token or not config_store.get("resin_host", ""):
        return None
    resolved = resolve_resin_proxy_config(
        {
            "resin_enabled": "true",
            "resin_scheme": config_store.get("resin_scheme", ""),
            "resin_host": config_store.get("resin_host", ""),
            "resin_port": config_store.get("resin_port", ""),
            "resin_token": token,
            "resin_default_platform": config_store.get("resin_default_platform", "Default"),
            "resin_platform_map": config_store.get("resin_platform_map", ""),
        },
        task_platform="vercel",
        account=account,
        require_enabled=True,
    )
    return str(resolved.get("proxy_url") or "").strip() or None


def _click_continue_with_email_entry(page, log_fn=print) -> bool:
    """点 signup 首页的 'Continue with Email' 入口链接展开邮箱表单。"""
    for sel in ("[role='link']:has-text('Continue with Email')",
                "[role='link']:has-text('Continue with email')",
                "span:has-text('Continue with Email')",
                "a:has-text('Continue with Email')"):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=8000)
                log_fn(f"[vercel] 点击 Continue with Email 入口 (sel={sel})")
                return True
        except Exception:
            continue
    return False


def _wait_email_otp(mailbox, account, timeout: int = 180, before_ids: set | None = None, log_fn=print) -> str:
    """等 outlook 收到 Vercel 6 位注册 OTP。

    必须传 keyword='Vercel'：wait_for_code 用 combined=subject+body_text+body_html 做 6 位正则，
    HTML 邮件里 color:#000000 的 #000000 会被误匹配成假码 "000000"（实测踩中）。加 keyword
    只搜 Vercel 邮件，OTP 主题 <code> is your Vercel sign up code 在 combined 最前先命中真码。
    before_ids=注册前基线，避免读到旧 OTP。
    """
    log_fn(f"[vercel] 等待注册邮箱 OTP (timeout={timeout}s)...")
    try:
        code = mailbox.wait_for_code(
            account, keyword="Vercel", timeout=timeout,
            before_ids=before_ids,
            code_pattern=r"(?<!\d)(\d{6})(?!\d)",
        )
        return code or ""
    except Exception as exc:
        log_fn(f"[vercel] 收 OTP 异常: {exc!r}")
        return ""


def _wait_hobby_case_email(mailbox, account, timeout: int = 180, log_fn=print) -> dict:
    """等 outlook 收到 Vercel 申诉确认邮件。

    路径B（different-method）：abuse@vercel.com 'A note about your sign-up attempt'（要回复）。
    路径C（verification）：no-reply@vercel.com 'Vercel - Hobby Case Opened'（Case 号等工程师）。
    都是人工审核工单。邮件无 6 位数字，用 code_pattern='.' 确保 keyword 命中即返回。
    """
    log_fn(f"[vercel] 等待申诉确认邮件 (timeout={timeout}s)...")
    kws = ("note about your sign-up", "sign-up attempt", "abuse@vercel.com",
           "additional information about your sign-up",
           "Hobby Case Opened", "Case Opened", "We've received your appeal")
    deadline = time.time() + timeout
    while time.time() < deadline:
        for kw in kws:
            try:
                code = mailbox.wait_for_code(account, keyword=kw, timeout=10, code_pattern=r".")
                if code:
                    return {"ok": True, "keyword": kw, "matched": code, "email": account.email}
            except TimeoutError:
                pass
            except Exception as exc:
                log_fn(f"[vercel] 收信查询异常({kw}): {exc!r}")
        time.sleep(3)
    return {"ok": False, "reason": "timeout"}


def _fill_recovery_form(page, name: str, contact_email: str, problem_type: str, log_fn=print) -> bool:
    """填 /accountrecovery 申诉表单并提交。

    两种场景：
    1. different-method：userType/email/problemType 都 disabled 已预选，只填 name + 勾 Google choicebox。
    2. verification：URL problemType=verification，userType/problemType select disabled 预填，
       字段是 Phone Number + Git Provider，两个 "I don't know / Not Applicable" choicebox 都要勾。
    """
    log_fn(f"[vercel] 填 accountrecovery 表单: name={name}")
    # 1. name
    name_filled = False
    name_sels = ("input[aria-label='Name']", "input[id^='name-']", "input[placeholder='Full name']",
                 "input[name='name']", "input#name", "input[autocomplete='name']")
    try:
        page.wait_for_selector(", ".join(name_sels), timeout=20000)
    except Exception:
        pass
    for sel in name_sels:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click()
                # React 受控 input：loc.fill 只设 DOM value，React state 不同步，后续 choicebox/select
                # 操作触发重渲染会被清空。直接 keyboard.type 触发 React onChange 同步 state 最稳。
                try:
                    loc.press("Control+a"); loc.press("Delete")
                except Exception:
                    pass
                page.keyboard.type(name, delay=60)
                page.wait_for_timeout(300)
                val = loc.input_value(timeout=3000)
                if val != name:
                    # keyboard.type 没生效，兜底 fill
                    loc.fill(name, timeout=8000)
                    page.wait_for_timeout(300)
                    val = loc.input_value(timeout=3000)
                if val == name:
                    name_filled = True
                    log_fn(f"[vercel] 已填 name (sel={sel})")
                    break
        except Exception:
            continue
    if not name_filled:
        log_fn("[vercel] 未找到 name 输入框")
        return False
    page.wait_for_timeout(800)
    # 2. sign-up method Google choicebox（different-method 场景显示）
    method_clicked = False
    for _m in range(5):
        try:
            loc = page.locator("div.choicebox").filter(has_text="Google").first
            if loc.count() > 0:
                loc.click(timeout=6000)
                page.wait_for_timeout(500)
                checked = page.eval_on_selector("input[type='checkbox'][name^='choicebox-name']", "els=>els.map(e=>e.checked)")
                if any(checked):
                    method_clicked = True
                    log_fn(f"[vercel] 勾选 sign-up method: Google (checked={checked})")
                    break
        except Exception:
            pass
        page.wait_for_timeout(1000)
    if not method_clicked:
        try:
            page.evaluate("""()=>{
              const t=Array.from(document.querySelectorAll('div')).find(d=>(d.innerText||'').trim()==='Google' && (d.className||'').includes('choicebox'));
              if(t){t.click(); return true;} return false;
            }""")
            log_fn("[vercel] 兜底 evaluate 点 Google choicebox")
        except Exception:
            log_fn("[vercel] 未找到 Google choicebox（可能 further-verification 场景）")
    page.wait_for_timeout(800)
    # 2b. verification 场景：userType/problemType select 预填 disabled，用 React setter force-set。
    if "verification" in problem_type:
        log_fn("[vercel] verification 场景：选 userType + problemType + I don't know choicebox")
        ut_opts: dict = {}
        pt_opts: dict = {}
        try:
            sel_state = page.evaluate("""()=>{
              const dump=s=>{if(!s) return null;
                return {disabled:s.disabled, value:s.value, selectedIndex:s.selectedIndex,
                  options:Array.from(s.options).map(o=>({v:o.value, t:(o.text||'').trim()}))};};
              return {ut:dump(document.getElementById('userType')), pt:dump(document.getElementById('problemType'))};
            }""")
            log_fn(f"[vercel] select 状态+options: {json.dumps(sel_state, ensure_ascii=False)[:700]}")
            for o in (sel_state.get("ut") or {}).get("options") or []:
                ut_opts[o["t"]] = o["v"]
            for o in (sel_state.get("pt") or {}).get("options") or []:
                pt_opts[o["t"]] = o["v"]
        except Exception as exc:
            log_fn(f"[vercel] select dump 失败: {exc!r}")

        def _pick(opts: dict, substr: str):
            for t, v in opts.items():
                if substr in t:
                    return t, v
            return None, None

        def _select_by_text(sel_id: str, opts: dict, substr: str) -> bool:
            t, v = _pick(opts, substr)
            if not v:
                log_fn(f"[vercel] {sel_id} 未找到含 {substr!r} 的 option")
                return False
            try:
                page.select_option(f"#{sel_id}", value=v)
                page.wait_for_timeout(500)
                cur = page.eval_on_selector(f"#{sel_id}", "e=>e.value")
                if cur == v:
                    return True
            except Exception as exc:
                log_fn(f"[vercel] select_option({sel_id}) 失败: {str(exc)[:80]}")
            # 兜底：React 原生 setter + dispatch change/input
            try:
                r = page.evaluate("""(args)=>{
                  const [sid, val] = args;
                  const s=document.getElementById(sid);
                  const setter=Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype,'value').set;
                  setter.call(s, val);
                  s.dispatchEvent(new Event('change',{bubbles:true}));
                  s.dispatchEvent(new Event('input',{bubbles:true}));
                  return s.value;
                }""", [sel_id, v])
                log_fn(f"[vercel] setter 兜底({sel_id}) value={r[:50]!r}")
                return r == v
            except Exception as exc:
                log_fn(f"[vercel] setter 兜底({sel_id}) 失败: {exc!r}")
                return False

        _select_by_text("userType", ut_opts, "I am trying to create a new Vercel account")
        page.wait_for_timeout(700)
        _select_by_text("problemType", pt_opts, "I was told my account requires further verification")
        page.wait_for_timeout(900)
        # verification 表单有多个 "I don't know / Not Applicable" choicebox（实测 phone + Git Provider
        # 两个都必填）。勾所有 label 同时含 "I don't know"+"Not Applicable" 的未勾 checkbox，循环到全勾。
        try:
            for _round in range(3):
                unchecked = page.evaluate("""()=>{
                  const all=Array.from(document.querySelectorAll('input[type=checkbox]'));
                  return all.filter(c=>{const lbl=c.closest('label')||c.closest('[class*=choicebox]');
                    const t=(lbl&&(lbl.innerText||''))||'';
                    return t.includes("I don't know") && t.includes("Not Applicable") && !c.checked;})
                    .map(c=>c.id);
                }""")
                if not unchecked:
                    break
                for cb_id in unchecked:
                    if cb_id:
                        page.evaluate("""(id)=>{
                          const c=document.getElementById(id);
                          if(!c) return false;
                          c.click();
                          c.dispatchEvent(new Event('change',{bubbles:true}));
                          c.dispatchEvent(new Event('input',{bubbles:true}));
                          return c.checked;
                        }""", cb_id)
                page.wait_for_timeout(600)
            log_fn("[vercel] 勾所有 I don't know choicebox 完成 (含 phone + Git Provider)")
        except Exception as exc:
            log_fn(f"[vercel] I don't know choicebox 异常: {exc!r}")
        page.wait_for_timeout(800)
        try:
            ck = page.evaluate("""()=>{
              const all=Array.from(document.querySelectorAll('input[type=checkbox]'));
              return all.filter(c=>{const lbl=c.closest('label')||c.closest('[class*=choicebox]');
                const t=(lbl&&(lbl.innerText||''))||'';
                return t.includes("I don't know") && t.includes("Not Applicable");})
                .map(c=>({id:c.id, checked:c.checked}));
            }""")
            log_fn(f"[vercel] I don't know 勾选验证: {ck}")
        except Exception as exc:
            log_fn(f"[vercel] choicebox 验证异常: {exc!r}")
        page.wait_for_timeout(800)
    # 2c. 补验 name（React 受控 input 偶发被 choicebox/select 操作触发重渲染清空，实测 Tatiana/Mattiello
    # name 被清空 → button disabled）。用 page.evaluate 读 value（eval_on_selector 不接 timeout 参数），
    # 空了用 keyboard.type 重填（触发 React onChange 同步 state，fill 只设 DOM value 会被 React 清空）。
    try:
        cur_name = page.evaluate("()=>{const e=document.querySelector(\"input[aria-label='Name']\");return e?e.value:'';}")
        if cur_name != name:
            log_fn(f"[vercel] name 被清空(val={cur_name!r})，keyboard.type 重新填入 {name!r}")
            loc = page.locator("input[aria-label='Name']").first
            if loc.count() > 0:
                loc.click()
                try:
                    loc.press("Control+a"); loc.press("Delete")
                except Exception:
                    pass
                page.keyboard.type(name, delay=60)
                page.wait_for_timeout(400)
    except Exception as exc:
        log_fn(f"[vercel] name 补验异常: {exc!r}")
    page.wait_for_timeout(1200)
    # 3. 提交申诉：主路径用 Playwright locator.click() 真实鼠标事件触发 React 合成 onClick（走 Kasada 包装 fetch，
    # 自动带 x-kpsdk/x-is-human）。旧 evaluate b.click() 是 DOM click，偶发不触发 React onClick → Kasada fetch 不发出。
    # 兜底：button disabled 时点 choicebox label 触发 React onChange 同步 state，再重试。
    clicked = {"ok": False}
    try:
        btn = page.locator("button:has-text('Submit Appeal')").first
        if btn.count() > 0:
            en = page.evaluate("""()=>{const b=Array.from(document.querySelectorAll('button')).find(b=>b.innerText.includes('Submit Appeal'));return !!(b&&!b.disabled&&b.getAttribute('aria-disabled')!=='true');}""")
            if en:
                btn.click(timeout=8000)
                clicked = {"ok": True, "method": "locator.click"}
                log_fn("[vercel] locator.click() Submit Appeal 真实鼠标点击成功")
            else:
                log_fn("[vercel] Submit Appeal button disabled，先重勾 choicebox")
                clicked = {"ok": False, "disabled": True}
        else:
            clicked = {"ok": False, "not_found": True}
    except Exception as exc:
        log_fn(f"[vercel] locator.click Submit Appeal 异常: {exc!r}")
    if not clicked.get("ok"):
        # 重勾：点 choicebox 的 label/容器（而非 checkbox 本身），触发 React onChange 同步 state
        try:
            page.evaluate("""()=>{
              const all=Array.from(document.querySelectorAll('input[type=checkbox]'));
              const cbs=all.filter(c=>{const lbl=c.closest('label')||c.closest('[class*=choicebox]');
                const t=(lbl&&(lbl.innerText||''))||'';
                return t.includes("I don't know") && t.includes("Not Applicable") && !c.checked;});
              for(const c of cbs){
                const lbl=c.closest('label')||c.closest('[class*=choicebox]')||c;
                lbl.click();
              }
              return cbs.length;
            }""")
            page.wait_for_timeout(1500)
            log_fn("[vercel] 重勾 choicebox 容器（点 label 触发 React onChange）")
        except Exception as exc:
            log_fn(f"[vercel] 重勾 choicebox 异常: {exc!r}")
        # 再试 locator.click（enabled 后）
        try:
            btn = page.locator("button:has-text('Submit Appeal')").first
            if btn.count() > 0:
                en = page.evaluate("""()=>{const b=Array.from(document.querySelectorAll('button')).find(b=>b.innerText.includes('Submit Appeal'));return !!(b&&!b.disabled);}""")
                if en:
                    btn.click(timeout=8000)
                    clicked = {"ok": True, "method": "locator.click-after-recheck"}
                    log_fn("[vercel] 重勾后 locator.click Submit Appeal 成功")
        except Exception as exc:
            log_fn(f"[vercel] 重勾后 click 异常: {exc!r}")
        # 兜底 mouse.click 真实坐标
        if not clicked.get("ok"):
            try:
                btn = page.locator("button:has-text('Submit Appeal')").first
                if btn.count() > 0:
                    box = btn.bounding_box()
                    if box:
                        page.mouse.click(box["x"]+box["width"]/2, box["y"]+box["height"]/2)
                        clicked = {"ok": True, "method": "mouse.click"}
                        log_fn("[vercel] mouse.click Submit Appeal 兜底")
            except Exception as exc:
                log_fn(f"[vercel] mouse.click 异常: {exc!r}")
        # 兜底2 evaluate b.click（旧路径）
        if not clicked.get("ok"):
            try:
                c2 = page.evaluate("""()=>{
                  const b=Array.from(document.querySelectorAll('button')).find(b=>b.innerText.includes('Submit Appeal'));
                  if(!b||b.disabled) return {ok:false,disabled:b?.disabled};
                  b.click(); return {ok:true};
                }""")
                clicked = c2
                log_fn(f"[vercel] 兜底 evaluate btn.click() Submit Appeal: {clicked}")
            except Exception as exc:
                log_fn(f"[vercel] evaluate btn.click 异常: {exc!r}")
    # 等 /api/appeals 响应（延长到 25s，Kasada fetch 响应偶发慢）
    for _ in range(25):
        page.wait_for_timeout(1000)
        if _APPEALS_META.get("status") is not None:
            break
    appeals_status = _APPEALS_META.get("status")
    v_value = _APPEALS_META.get("v")
    body_sent = bool(_APPEALS_META.get("body"))
    log_fn(f"[vercel] 申诉提交结果: appeals_status={appeals_status} kasada_v={v_value} body_sent={body_sent}")
    if appeals_status in (200, 201):
        log_fn("[vercel] 申诉提交成功（响应 201）")
        return True
    if appeals_status == 400:
        log_fn(f"[vercel] 申诉被 appeals API 拒（Kasada 人机分 v={v_value} 低）")
        return False
    # 响应未捕到但 body 已发出 + 点击 ok → 工单大概率已开（响应监听竞态）
    if body_sent and clicked.get("ok"):
        log_fn("[vercel] body 已发出但响应未捕到，仍判 submitted（工单可能已开）")
        return True
    return False


def _wait_recovery_submitted(page, timeout: int = 30, log_fn=print) -> bool:
    """提交后等待确认页/文案。返回 False 表示服务端拒或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = page_state(page)
        txt = st["text"].lower()
        if "issue processing your appeal" in txt or "email registration@vercel.com" in txt:
            log_fn("[vercel] 申诉被服务端拒：We had an issue processing your appeal")
            return False
        if ("case opened" in txt) or ("we've received" in txt) or ("received your request" in txt) \
           or ("get back to you" in txt) or ("we will review" in txt) or ("thank you for submitting" in txt):
            log_fn(f"[vercel] 申诉表单已提交成功，确认文案出现 url={st['url'][:80]}")
            return True
        if not st["account_recovery"] and "/accountrecovery" not in st["url"].lower():
            log_fn(f"[vercel] 已离开 /accountrecovery url={st['url'][:80]}，视作已提交")
            return True
        time.sleep(2)
    log_fn("[vercel] 等待申诉提交确认超时")
    return False


class VercelProtocolMailboxWorker:
    """Vercel 浏览器驱动注册 Worker（patchright + resin + outlook 收信）。

    构造参数兼容框架 ProtocolMailboxAdapter（otp_callback/captcha_solver 由框架注入）。
    run(email, password, otp_callback) 执行完整注册链路，返回 result dict。
    """

    def __init__(self, *, proxy: str | None = None, timeout: int = 300,
                 log_fn=print, otp_callback: Callable[[], str] | None = None,
                 captcha_solver=None, use_patchright: bool = True,
                 use_ruyipage: bool = False, ruyipage_browser_path: str = "",
                 outlook_email: str = "", inline_bindkey: bool = True, **_kwargs):
        self.proxy = proxy
        self.timeout = timeout
        self.otp_callback = otp_callback
        self.captcha_solver = captcha_solver
        self.use_patchright = use_patchright
        # use_ruyipage=True 时走 ruyiPage(Firefox+BiDi) 驱动替代 patchright(CDP-Chromium)，
        # 委托 scripts/test_vercel_register_ruyipage.py 的 run()（已写好的骨架），
        # 假设 ruyiPage 无 CDP 暴露面绕过 Kasada。browser_path 指定制 Firefox 指纹内核。
        self.use_ruyipage = use_ruyipage
        self.ruyipage_browser_path = ruyipage_browser_path
        self.outlook_email = outlook_email
        # inline_bindkey=True（默认）：路径 A registered_directly 时当场绑卡+建 vck_ key+送 $5。
        # =False：只标 registered_directly，不绑卡（留待审核通过后用第二步 bind_card_create_key 补绑卡）。
        self.inline_bindkey = inline_bindkey
        # 包一层 GBK 兜底：Windows 控制台 print 遇 emoji/特殊字符会 UnicodeEncodeError 崩。
        raw_log = log_fn or log
        self.log = lambda msg: self._safe_log(raw_log, msg)

    @staticmethod
    def _safe_log(raw_log, msg: str) -> None:
        try:
            raw_log(msg)
        except UnicodeEncodeError:
            try:
                import sys
                sys.stdout.buffer.write((str(msg) + "\n").encode("utf-8", "replace"))
                sys.stdout.buffer.flush()
            except Exception:
                pass

    def run(self, *, email: str = "", password: str = "", otp_callback: Callable[[], str] | None = None,
            mailbox=None, mailbox_account=None) -> dict:
        """执行 Vercel 注册。email 由框架从 identity 注入；otp_callback 框架注入或用本地收信。

        mailbox/mailbox_account 由框架按 web 端所选 mail_provider 建好传入
        （outlook/cfworker/yyds_mail 统一），避免 Worker 自己 _build_outlook_mailbox
        把 yyds 邮箱当 outlook 取号导致 NameError。
        """
        if otp_callback is not None:
            self.otp_callback = otp_callback
        global _APPEALS_META
        _APPEALS_META = {"v": None, "status": None, "request_headers": None}

        # use_ruyipage=True：走 ruyiPage(Firefox+BiDi) 驱动，委托骨架 run()。
        # 假设 ruyiPage 无 CDP 暴露面绕过 Kasada（对标 patchright CDP-Chromium）。
        # 骨架复用本 Worker 同源的邮箱/OTP/收信逻辑，只换浏览器+网络监听层。
        if self.use_ruyipage:
            return self._run_ruyipage(email=email, password=password)

        # 1. mailbox + before_ids 基线
        # 优先用框架传入的 mailbox/mailbox_account（按 web 端所选 mail_provider 建好）。
        # 兜底：旧调用方没传 mailbox 时，按 email 走 outlook 取号（保持向后兼容）。
        from core.base_mailbox import MailboxAccount
        account_email = email or self.outlook_email
        if mailbox is not None and mailbox_account is not None:
            account = mailbox_account
            account_email = account_email or getattr(account, "email", "") or ""
        else:
            if not account_email:
                mailbox, account_email = _build_outlook_mailbox(proxy=self.proxy, preferred_email="", log_fn=self.log)
                email = account_email
            else:
                mailbox, account_email = _build_outlook_mailbox(proxy=self.proxy, preferred_email=account_email, log_fn=self.log)
            account = MailboxAccount(email=account_email, account_id=account_email)
        before_ids = mailbox.get_current_ids(account) if mailbox is not None else set()
        self.log(f"[vercel] 使用邮箱: {account_email} | 注册前邮件 ID 基线: {len(before_ids)} 封")

        result: dict[str, Any] = {
            "email": account_email,
            "proxy": self.proxy,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stages": [],
            "api_base": AI_GATEWAY_BASE,
            "native_api_base": NATIVE_API_BASE,
        }

        # 2. patchright + resin 打开 signup
        from contextlib import contextmanager
        from urllib.parse import urlparse

        @contextmanager
        def _patchright_ctx():
            from patchright.sync_api import sync_playwright
            pw = sync_playwright().start()
            launch_args = [
                "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage",
                "--disable-default-apps", "--disable-extensions", "--disable-sync",
                "--disable-translate", "--disable-hang-monitor", "--disable-domain-reliability",
                "--no-first-run", "--no-default-browser-check", "--no-sandbox",
                "--metrics-recording-only", "--mute-audio",
            ]
            launch_opts = {"headless": False, "args": launch_args}
            if self.proxy:
                # 必须用 urlparse 拆成 server+username+password 分字段，整个 URL 塞 server 会
                # ERR_INVALID_AUTH_CREDENTIALS（patchright 把 user:pass@ 当 auth 解析失败）。
                p = urlparse(self.proxy)
                pcfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
                if p.username: pcfg["username"] = p.username
                if p.password: pcfg["password"] = p.password
                launch_opts["proxy"] = pcfg
            browser = pw.chromium.launch(**launch_opts)
            try:
                yield browser, pw
            finally:
                try: browser.close()
                except Exception: pass
                try: pw.stop()
                except Exception: pass

        with _patchright_ctx() as (browser, pw):
            ctx = browser.new_context(
                viewport={"width": 1366, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = ctx.new_page()

            def _dump_req(req):
                try:
                    if "/api/appeals" in (req.url or ""):
                        _APPEALS_META["request_headers"] = dict(req.headers)
                        try: _APPEALS_META["body"] = req.post_data
                        except Exception: pass
                except Exception:
                    pass

            def _dump_resp(resp):
                try:
                    if "/api/appeals" in (resp.url or ""):
                        ih = resp.request.headers.get("x-is-human") or ""
                        if ih:
                            try:
                                m = re.search(r'"v":\s*([0-9.]+)', ih)
                                if m: _APPEALS_META["v"] = float(m.group(1))
                            except Exception:
                                pass
                        _APPEALS_META["status"] = resp.status
                        try: _APPEALS_META["resp_text"] = resp.text()
                        except Exception: pass
                        self.log(f"[vercel] /api/appeals x-is-human v={_APPEALS_META.get('v')} 响应 status={resp.status}")
                except Exception:
                    pass

            page.on("request", _dump_req)
            page.on("response", _dump_resp)

            opened = False
            for _g in range(3):
                try:
                    page.goto(SIGNUP_URL, wait_until="commit", timeout=60000)
                    opened = True
                    break
                except Exception as exc:
                    self.log(f"[vercel] goto 失败重试 {_g+1}: {str(exc)[:120]}")
                    page.wait_for_timeout(2000)
            if not opened:
                raise RuntimeError(f"打开 {SIGNUP_URL} 多次失败（NS_ERROR_ABORT）")
            page.wait_for_timeout(3500)
            st = page_state(page)
            self.log(f"[vercel] signup 首页 url={st['url'][:80]} email_input={st['has_email_input']}")

            # 3. 点 Continue with Email 入口
            opened_form = False
            for _e in range(12):
                if _click_continue_with_email_entry(page, log_fn=self.log):
                    page.wait_for_timeout(2000)
                    st = page_state(page)
                    if st["has_email_input"]:
                        opened_form = True
                        break
                page.wait_for_timeout(3000)
            if not opened_form:
                result["stages"].append({"stage": "open_email_form", "ok": False, "state": st})
                raise RuntimeError("无法展开邮箱注册表单")
            result["stages"].append({"stage": "open_email_form", "ok": True})

            # 4. 填邮箱提交
            if not fill_email(page, account_email, log_fn=self.log):
                raise RuntimeError("填邮箱提交失败")
            result["stages"].append({"stage": "submit_email", "ok": True})

            # 5. 观察分叉：OTP / further / recovery
            page.wait_for_timeout(5000)
            st = page_state(page)
            self.log(f"[vercel] 邮箱提交后 url={st['url'][:80]} otp={st['has_otp_input']} "
                     f"otp_sent={st['otp_sent']} further={st['further_verification']} "
                     f"recovery={st['account_recovery']} diff_method={st['try_different_method']}")
            for _ in range(8):
                if st["has_otp_input"] or st["otp_sent"] or st["further_verification"] or st["account_recovery"]:
                    break
                page.wait_for_timeout(2500)
                st = page_state(page)
                self.log(f"[vercel] 再观察 url={st['url'][:80]} otp={st['has_otp_input']} further={st['further_verification']} recovery={st['account_recovery']}")

            recovery_triggered = st["further_verification"] or st["account_recovery"]
            otp_stage = st["has_otp_input"] or st["otp_sent"]

            # 邮箱提交后无任何响应（无 OTP/further/recovery）= Kasada 静默拦，主动进填表流(verification)
            if not otp_stage and not recovery_triggered:
                self.log("[vercel] 邮箱提交后无响应(静默拦)，主动进填表流(verification)")
                recovery_triggered = True
                result["recovery_scene"] = "verification"

            if otp_stage and not recovery_triggered:
                # 6a. 收 OTP 填入
                otp = self._get_otp(mailbox, account, before_ids)
                if not otp:
                    page.wait_for_timeout(3000)
                    st2 = page_state(page)
                    if st2["further_verification"] or st2["account_recovery"]:
                        recovery_triggered = True
                        st = st2
                    else:
                        raise RuntimeError("未收到 Vercel 注册 OTP，且未进入填表流")
                else:
                    self.log(f"[vercel] 收到 OTP: {otp}")
                    enter_otp(page, otp)
                    result["stages"].append({"stage": "submit_otp", "ok": True, "otp": otp})
                    page.wait_for_timeout(6000)
                    st = page_state(page)
                    self.log(f"[vercel] OTP 提交后 url={st['url'][:80]} further={st['further_verification']} recovery={st['account_recovery']} dashboard={st['dashboard']}")
                    recovery_triggered = st["further_verification"] or st["account_recovery"]
                    for _ in range(10):
                        if recovery_triggered or st["dashboard"]:
                            break
                        page.wait_for_timeout(2500)
                        st = page_state(page)
                        recovery_triggered = st["further_verification"] or st["account_recovery"]
                    if st["dashboard"] or st["onboarding"]:
                        # 路径A：直接注册通过
                        result["stages"].append({"stage": "registered_directly", "ok": True})
                        result["registered_directly"] = True
                        result["registered"] = True
                        recovery_triggered = False
                        # 顺带绑卡建key：浏览器已登录 onboarding，直接拿 vcp_ + 纯协议绑卡建key送额度
                        # （省绑卡 agent 重新登录一次 OTP，registered_directly 号当场拿 vck_ key）
                        if not self.inline_bindkey:
                            self.log("[vercel] 跳过顺带绑卡（inline_bindkey 开关关闭，留待第二步补绑卡）")
                        else:
                            try:
                                from urllib.parse import unquote as _unq
                                from platforms.vercel.core import VercelClient
                                import random as _r, string as _st
                                # onboarding 页选 Hobby plan（实测 DOM: div.selector-option-module__*__suffix，innerText=="Hobby"）
                                hobby_clicked = False
                                for sel in ("div[class*='selector-option']:has-text('Hobby')",
                                            "div:has-text('Hobby'):not(:has(div))",
                                            "label:has-text('Hobby')", "button:has-text('Hobby')"):
                                    try:
                                        loc = page.locator(sel).first
                                        if loc.count() > 0:
                                            loc.click(timeout=6000); hobby_clicked = True
                                            self.log(f"[vercel] 顺带选 Hobby (sel={sel})"); page.wait_for_timeout(2000); break
                                    except Exception: continue
                                if not hobby_clicked:
                                    try:
                                        page.evaluate("""()=>{const els=Array.from(document.querySelectorAll('div,label,button,span'));
                                          let best=null,bestLen=999;
                                          for(const e of els){const t=(e.innerText||'').trim();
                                            if(t==='Hobby'&&t.length<bestLen){best=e;bestLen=t.length;}}
                                          if(best){best.click();return 'ok';} return null;}""")
                                        self.log("[vercel] 兜底 evaluate 点 Hobby"); page.wait_for_timeout(2000)
                                    except Exception: pass
                                team_name = "Team" + "".join(_r.choices(_st.ascii_lowercase + _st.digits, k=8))
                                for sel in ("input[aria-label='Team Name']", "input[placeholder='ACME']", "input[name='teamName']"):
                                    try:
                                        loc = page.locator(sel).first
                                        if loc.count() > 0:
                                            loc.click(timeout=4000)
                                            try: loc.press("Control+a"); loc.press("Delete")
                                            except: pass
                                            page.keyboard.type(team_name, delay=50); page.wait_for_timeout(800)
                                            self.log(f"[vercel] 填 Team Name: {team_name}"); break
                                    except Exception: continue
                                page.wait_for_timeout(1500)
                                for sel in ("button:has-text('Continue')", "button[type='submit']"):
                                    try:
                                        loc = page.locator(sel).first
                                        if loc.count() > 0 and not loc.is_disabled(timeout=3000):
                                            loc.click(timeout=6000); self.log(f"[vercel] 顺带点 Continue"); page.wait_for_timeout(6000); break
                                    except Exception: continue
                                page.wait_for_timeout(3000)
                                # 拿 authorization cookie（vcp_ token）——用 patchright context 直接拿
                                _cookies = {}
                                try:
                                    for _c in ctx.cookies():
                                        if "vercel.com" in (_c.get("domain") or ""):
                                            _cookies[_c["name"]] = _c["value"]
                                except Exception: pass
                                _vcp = _unq(_cookies.get("authorization", "") or "").replace("Bearer ", "")
                                self.log(f"[vercel] 顺带绑卡建key vcp_={'有' if _vcp else '空'} url={page.url[:60]}")
                                if _vcp:
                                    _client = VercelClient(proxy="http://127.0.0.1:7897", log_fn=self.log)
                                    _team = _client.get_team_id(_vcp)
                                    if _team:
                                        result["team_id"] = _team
                                        _bal0 = _client.get_credits_balance(_vcp, _team)
                                        _card_bound = bool(_bal0.get("hasVerifiedPaymentMethod"))
                                        if not _card_bound:
                                            try:
                                                from scripts.vercel_full_bindkey import pick_card as _pick
                                                for _try in range(5):
                                                    _card = _pick()
                                                    if not _card or not _card.get("number"): break
                                                    _bind = _client.bind_card_protocol(_vcp, _team, _card)
                                                    _cnum = str(_card.get("number", ""))
                                                    self.log(f"[vercel] 顺带绑卡 try{_try+1} card={_cnum[:6]}**{_cnum[-4:]} ok={_bind.get('ok')} step={_bind.get('step')} err={(_bind.get('error') or '')[:90]}")
                                                    if _bind.get("ok"):
                                                        _card_bound = True
                                                        self.log("[vercel] 顺带绑卡成功"); break
                                                    # 3DS(requires_browser)/卡拒/setup失败→换下张银联卡继续试(不同卡可能不触发3DS)
                                            except Exception as exc: self.log(f"[vercel] 顺带绑卡异常: {exc!r}")
                                        # 绑卡成功(或已有支付方式)才建key+送额度；失败则跳过建key避免产出"有vck_但调不通"的无效号
                                        if _card_bound:
                                            _key = _client.create_ai_gateway_key(_vcp, _team, name="auto-register")
                                            if _key:
                                                result["api_key_full"] = _key
                                                result["api_key"] = _key
                                                result["card_bound"] = True
                                                _trig = _client.trigger_free_credit(_key)
                                                if _trig.get("ok"):
                                                    result["trigger_verified"] = True
                                                    self.log(f"[vercel] 顺带绑卡+建key+ai-gateway验证全成功 vck_={_key[:18]}... resp={(_trig.get('response') or '')[:30]!r}")
                                                else:
                                                    # 旧key被ai-gateway缓存为无卡(建key时卡状态未同步)，重建key绕过缓存(卡现已绑上)
                                                    self.log(f"[vercel] 旧key trigger 403(缓存无卡)，重建key绕过缓存")
                                                    _key2 = _client.create_ai_gateway_key(_vcp, _team, name="auto-register-v2")
                                                    if _key2:
                                                        _trig2 = _client.trigger_free_credit(_key2)
                                                        if _trig2.get("ok"):
                                                            result["api_key_full"] = _key2
                                                            result["api_key"] = _key2
                                                            result["trigger_verified"] = True
                                                            self.log(f"[vercel] 重建key+ai-gateway验证成功 vck_={_key2[:18]}... resp={(_trig2.get('response') or '')[:30]!r}")
                                                        else:
                                                            result["trigger_pending"] = True
                                                            self.log(f"[vercel] 重建key仍403 trigger待cron补 vck_={_key2[:18]}...")
                                                    else:
                                                        result["trigger_pending"] = True
                                                        self.log(f"[vercel] 重建key失败 trigger待cron补 vck_={_key[:18]}...")
                                            else:
                                                self.log("[vercel] 绑卡成功但建key失败")
                                        else:
                                            self.log("[vercel] 绑卡失败(5张卡全拒/3DS)，跳过建key避免无效vck_，留待第二步补绑卡")
                                            result["card_bound"] = False
                                    else:
                                        self.log("[vercel] 顺带绑卡：未拿到 team_id 跳过")
                                else:
                                    self.log("[vercel] registered_directly 但没拿到 vcp_，跳过顺带绑卡")
                            except Exception as exc:
                                self.log(f"[vercel] 顺带绑卡建key异常: {exc!r}")
                    elif not recovery_triggered:
                        # OTP 提交后无 further/dashboard = 静默拦(unknown error/退回首页)，主动进填表(verification)
                        try:
                            body_txt = page.inner_text("body", timeout=3000)
                        except Exception:
                            body_txt = ""
                        bl = body_txt.lower()
                        back_to_signup = ("continue with email" in bl and "other sign up options" in bl)
                        if ("unknown error" in bl) or ("use a different email" in bl) or ("an error occurred" in bl) or back_to_signup:
                            self.log(f"[vercel] OTP 提交被静默拦(back_to_signup={back_to_signup})，主动进填表(verification)")
                            recovery_triggered = True
                            result["recovery_scene"] = "verification"

            # 7. 填表流（路径B different-method / 路径C verification）
            if recovery_triggered:
                self.log("[vercel] 触发填表流（被拦进一步验证/换方法）")
                if not st["account_recovery"]:
                    clicked_link = False
                    for sel in ("a:has-text('this form')", "a:has-text('complete this form')",
                                "a:has-text('further assistance')"):
                        try:
                            loc = page.locator(sel).first
                            if loc.count() > 0:
                                loc.click(timeout=8000)
                                clicked_link = True
                                self.log(f"[vercel] 点击 this form 链接 (sel={sel})")
                                break
                        except Exception:
                            continue
                    if not clicked_link:
                        # 兜底导航：按 recovery_scene 选 problemType（OTP 静默拦走 verification=4号成功路径）
                        scene = result.get("recovery_scene") or "verification"
                        ptype = "verification" if scene == "verification" else "different-method"
                        target = f"https://vercel.com/accountrecovery?userType=new&problemType={ptype}&email=" + account_email
                        self.log(f"[vercel] 未找到 this form，直接导航 {target[:90]}")
                        for _g in range(3):
                            try:
                                page.goto(target, wait_until="commit", timeout=60000)
                                break
                            except Exception as exc:
                                self.log(f"[vercel] accountrecovery goto 失败重试 {_g+1}: {str(exc)[:100]}")
                                page.wait_for_timeout(2000)
                    page.wait_for_timeout(4000)
                    st = page_state(page)

                recovery_url = st.get("url") or ""
                problem_type = "verification" if "problemType=verification" in recovery_url else (
                    "different-method" if "problemType=different-method" in recovery_url else "")
                result["problem_type"] = problem_type
                self.log(f"[vercel] recovery 场景 problem_type={problem_type!r}")
                name = random_name()
                if not _fill_recovery_form(page, name, contact_email=account_email, problem_type=problem_type, log_fn=self.log):
                    result["stages"].append({"stage": "recovery_submit", "ok": False, "state": st})
                else:
                    submitted_ok = _wait_recovery_submitted(page, timeout=30, log_fn=self.log)
                    result["stages"].append({"stage": "recovery_submit", "ok": submitted_ok, "name": name})
                    result["recovery_submitted"] = bool(submitted_ok)
                    result["name_used"] = name
                    result["appeals_status"] = _APPEALS_META.get("status")
                    result["kasada_v"] = _APPEALS_META.get("v")
                    if not submitted_ok:
                        result["status"] = "recovery_rejected_by_server"
                        result["registered"] = False
                        result["appeal_submitted"] = False

            # 落地 cookie 供后续 4-8h 登录绑卡
            try:
                from core.oauth_browser import OAuthBrowser
                cookies = {}
                # patchright 直接从 context 拿 cookie
                for c in ctx.cookies():
                    if "vercel.com" in (c.get("domain") or ""):
                        cookies[c["name"]] = c["value"]
                result["cookies"] = cookies
                result["cookie_count"] = len(cookies)
            except Exception:
                result["cookies"] = {}
                result["cookie_count"] = 0

        # 8. 等申诉确认邮件（填表流触发后才等）
        if result.get("recovery_submitted"):
            self.log("[vercel] 申诉已提交，开始等申诉确认邮件...")
            mail_result = _wait_hobby_case_email(mailbox, account, timeout=min(self.timeout, 240), log_fn=self.log)
            result["hobby_case_email"] = mail_result
            result["appeal_submitted"] = True
            if mail_result.get("ok"):
                self.log(f"[vercel] [OK] 收到申诉确认邮件，人工工单已开（等待 4-8h 审核通过）keyword={mail_result.get('keyword')!r}")
                result["status"] = "appeal_submitted_awaiting_human_review"
                result["registered"] = False  # 人工工单非自动注册成功
            else:
                self.log(f"[vercel] [WARN] 申诉已提交但未在限定时间内确认到确认邮件。请人工查 {account_email}")
                result["status"] = "appeal_submitted_email_unconfirmed"
                result["registered"] = False
        elif result.get("registered_directly"):
            result["status"] = "registered_directly"
            result["registered"] = True
        else:
            result["status"] = "unknown"
            result["registered"] = False

        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return result

    def _run_ruyipage(self, *, email: str = "", password: str = "") -> dict:
        """use_ruyipage=True 时的注册执行：委托骨架 run()，再补齐 Worker 特有字段。

        骨架 scripts/test_vercel_register_ruyipage.py 的 run() 复用本模块同源的
        邮箱/OTP/收信逻辑（importlib 加载 patchright 版绑定浏览器无关函数），只替换
        浏览器驱动(Firefox+BiDi) + 网络监听(intercept 抓 x-is-human v)。返回 dict
        含 email/stages/registered/appeal_submitted/kasada_v/appeals_status 等。
        本方法补齐 Worker 返回契约里的 api_base/native_api_base/problem_type/cookies。
        """
        import importlib.util
        skeleton = ROOT / "scripts" / "test_vercel_register_ruyipage.py"
        spec = importlib.util.spec_from_file_location("_vercel_ruyipage_skeleton", skeleton)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.log(f"[vercel] use_ruyipage=True 委托 ruyiPage 骨架 (browser_path={self.ruyipage_browser_path or '自动探测'})")
        try:
            r = mod.run(
                proxy=self.proxy,
                headless=False,  # ruyiPage 反检测建议有头，Kasada 人机分更稳
                timeout=self.timeout,
                browser_path=self.ruyipage_browser_path,
                use_outlook=True,
                outlook_email=email or self.outlook_email,
                use_smart_fp=True,
            )
        except Exception as exc:
            self.log(f"[vercel] ruyiPage 骨架异常: {exc!r}")
            return {
                "email": email or self.outlook_email, "proxy": self.proxy,
                "registered": False, "appeal_submitted": False,
                "status": "ruyipage_error", "error": repr(exc),
                "stages": [], "kasada_v": None, "appeals_status": None,
            }
        # 补齐 Worker 返回契约字段（骨架没落的）
        r.setdefault("api_base", AI_GATEWAY_BASE)
        r.setdefault("native_api_base", NATIVE_API_BASE)
        # problem_type 从 recovery_url 推断（骨架没显式落，但 recovery_submitted 时有 url 信息）
        if "problem_type" not in r:
            r["problem_type"] = ""
        # cookies：骨架没落（ruyiPage 取 cookie API 与 Playwright 不同），留空不阻塞后续
        r.setdefault("cookies", {})
        r.setdefault("cookie_count", 0)
        # 同步模块级 _APPEALS_META 让外部取 v 一致
        _APPEALS_META["v"] = r.get("kasada_v")
        _APPEALS_META["status"] = r.get("appeals_status")
        return r

    def _get_otp(self, mailbox, account, before_ids) -> str:
        """收 OTP：优先框架注入的 otp_callback，否则本地 _wait_email_otp。"""
        if self.otp_callback is not None:
            try:
                code = self.otp_callback()
                if code:
                    return str(code).strip()
            except Exception as exc:
                self.log(f"[vercel] otp_callback 异常: {exc!r}")
        return _wait_email_otp(mailbox, account, timeout=min(self.timeout, 180), before_ids=before_ids, log_fn=self.log)
