"""Vercel 平台插件。

注册链路（浏览器驱动，patchright + resin 住宅代理）：
- 打开 https://vercel.com/signup → Continue with Email → 填邮箱提交。
- 邮箱提交后非确定性分三条路径：
  A. OTP 输入框 → 读 6 位 OTP（keyword="Vercel" + before_ids 基线，避免 HTML #000000 假码）
     → 填入 → 直接进 onboarding/dashboard = **真注册成功（registered=True）**。
  B. 直接被拦 different-method → /accountrecovery 填表 → POST /api/appeals 201 →
     收 abuse@vercel.com "A note about your sign-up attempt"（人工审核工单，要回复）。
  C. OTP 验证后被拦 verification → /accountrecovery 填 verification 表（两个 "I don't know /
     Not Applicable" choicebox：phone + Git Provider，必须全勾）→ POST /api/appeals 201 →
     收 no-reply@vercel.com "Vercel - Hobby Case Opened"（人工 Case 等工程师）。
- 路径 B/C 都是人工审核工单（appeal_submitted=True, registered=False），需 4-8h 人工审核
  通过才真正建成 Hobby 账号。绑卡 + 拿 API key（ai-gateway.vercel.sh/v1）留审核通过后做。

收信：默认复用 Outlook Token IMAP（已扫 INBOX/Junk），mailbox_inventory DB 取 unused 无别名
号按 successful_registrations **降序**取（succ 高=收信链路已验证）。

Kasada BotID 人机验证：patchright（CDP-patched Chromium）产出有效 Kasada token 能过 appeals
（实测 v=0.026/0.13/0.44 均拿 201）；camoufox/chrome token 无效拿 400。故浏览器后端固定 patchright。
"""
from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register

from contextlib import contextmanager


@contextmanager
def _bindkey_global_lock(log_fn=None):
    """全局锁：序列化所有 bind_card_create_key 子进程调用。

    多个 runner / 多个 agent session 会并发调 execute_action，但它们共享一个
    _vercel_full_bindkey_result.json（subprocess 写、plugin 读），并发会串号（A 的结果被
    B 读到）。email-guard 能挡住串号但会造成假失败 + 重复跑同号。本锁用 msvcrt.locking 对
    scripts/_vercel_bindkey.lock 加 1 字节排他锁，进程退出自动释放；带 10min 超时兜底防死锁。
    """
    import msvcrt
    import time as _time
    from pathlib import Path as _P
    lockpath = _P(__file__).resolve().parents[2] / "scripts" / "_vercel_bindkey.lock"
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    f = open(lockpath, "a+b")
    acquired = False
    deadline = _time.time() + 600
    while True:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
            break
        except OSError:
            if _time.time() > deadline:
                if log_fn:
                    log_fn("[vercel] bindkey 全局锁等待 10min 超时，强制继续（偶发并发）")
                break
            _time.sleep(5)
    try:
        yield
    finally:
        if acquired:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        f.close()



@register
class VercelPlatform(BasePlatform):
    name = "vercel"
    display_name = "Vercel"
    version = "1.0.0"
    # 浏览器驱动填表流（patchright 过 Kasada），协议路径只是 appeals REST 提交。
    supported_executors = ["protocol", "headless", "headed", "cdp_protocol"]
    supported_identity_modes = ["mailbox"]
    # Vercel signup 邮箱 OTP + 申诉确认邮件都进 outlook INBOX（DKIM 好，不进 Junk），
    # 默认复用 Outlook Token IMAP。DB 取号按 succ 降序。
    default_mail_provider = "outlook_token"
    # Vercel signup 无 captcha widget（Kasada 是隐形 JS 人机分，由 patchright 指纹过），
    # 不需要 Turnstile solver。
    protocol_captcha_order = ()

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # Vercel signup 是邮箱 OTP 无密码；后续登录也走邮箱 OTP。不生成密码。
        return password or ""

    def _map_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        """把 Worker 返回的 dict 映射成 RegistrationResult。

        registered=True 仅路径 A（OTP→直接进 onboarding）。
        appeal_submitted=True 路径 B/C（人工审核工单，registered=False/PENDING）。
        """
        registered = bool(result.get("registered"))
        appeal_submitted = bool(result.get("appeal_submitted"))
        api_key = str(result.get("api_key") or "").strip()
        # 状态：真注册成功→REGISTERED；工单已开待审核→PENDING；否则 INVALID
        if registered or api_key:
            status = AccountStatus.REGISTERED
        elif appeal_submitted:
            status = AccountStatus.PENDING
        else:
            status = AccountStatus.INVALID
        return RegistrationResult(
            email=str(result.get("email") or "").strip(),
            password=password or str(result.get("password") or ""),
            user_id=str(result.get("user_id") or ""),
            token=api_key,
            status=status,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "registered": registered,
                "appeal_submitted": appeal_submitted,
                "card_bound": result.get("card_bound"),
                "trigger_verified": result.get("trigger_verified"),
                "trigger_pending": result.get("trigger_pending"),
                "status_detail": str(result.get("status") or ""),
                "name_used": str(result.get("name_used") or ""),
                "problem_type": str(result.get("problem_type") or ""),
                "appeals_status": result.get("appeals_status"),
                "kasada_v": result.get("kasada_v"),
                "hobby_case_email": dict(result.get("hobby_case_email") or {}),
                "cookies": dict(result.get("cookies") or {}),
                "cookie_count": int(result.get("cookie_count") or 0),
                "stages": list(result.get("stages") or []),
                "api_base": str(result.get("api_base") or "https://ai-gateway.vercel.sh/v1"),
                "native_api_base": str(result.get("native_api_base") or "https://api.vercel.com"),
                "auth_header": "Authorization",
                "auth_scheme": "Bearer v1_...",
                "site_url": "https://vercel.com",
                "dashboard_url": "https://vercel.com/dashboard",
                "checked_at": str(result.get("checked_at") or ""),
            },
        )

    def build_protocol_mailbox_adapter(self):
        """浏览器驱动注册（patchright + resin）wiring。

        Worker 复用 scripts/test_vercel_register.py 验证过的完整链路：
        signup→OTP→(onboarding|verification/different-method appeal)→收确认邮件。
        OTP 由框架从 mailbox 构建 otp_callback，Worker 检测到 OTP 输入框时调用。

        浏览器后端可选：默认 patchright(CDP-Chromium，过 Kasada)；ctx.extra 传
        vercel_use_ruyipage=true 切 ruyiPage(Firefox+BiDi，无 CDP 暴露面试水绕 Kasada)，
        vercel_ruyipage_browser_path 指定制 Firefox 内核路径（留空自动探测）。
        """
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.vercel.protocol_mailbox",
                fromlist=["VercelProtocolMailboxWorker"],
            ).VercelProtocolMailboxWorker(
                proxy=ctx.proxy,
                timeout=resolve_timeout(ctx.extra or {}, ("vercel_timeout", "mail_otp_timeout", "browser_oauth_timeout"), 300),
                log_fn=ctx.log,
                otp_callback=artifacts.otp_callback,
                # Vercel 无 captcha widget，不传 captcha_solver（保留参数兼容框架）。
                captcha_solver=None,
                # 浏览器后端：默认 patchright（过 Kasada）；ctx.extra 开 ruyipage 试水时关 patchright。
                use_patchright=not bool((ctx.extra or {}).get("vercel_use_ruyipage")),
                use_ruyipage=bool((ctx.extra or {}).get("vercel_use_ruyipage")),
                ruyipage_browser_path=str((ctx.extra or {}).get("vercel_ruyipage_browser_path") or ""),
                # 指定 outlook 号（可选，复用 DB 取号时留空）。
                outlook_email=str((ctx.extra or {}).get("vercel_outlook_email") or ""),
                # 注册时是否当场绑卡建key：默认 True（路径 A registered_directly 顺带拿 vck_）。
                # 主人注册页勾掉 vercel_inline_bindkey 则只提工单，审核通过后用第二步补绑卡。
                inline_bindkey=bool((ctx.extra or {}).get("vercel_inline_bindkey", True)),
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
                # 框架已按 web 端所选 mail_provider 建好 mailbox 挂在 ctx.platform.mailbox +
                # ctx.identity.mailbox_account（outlook/cfworker/yyds_mail 统一处理）。
                # 传给 Worker 避免它自己 _build_outlook_mailbox 把 yyds 邮箱当 outlook 取号崩。
                mailbox=getattr(ctx.platform, "mailbox", None),
                mailbox_account=getattr(ctx.identity, "mailbox_account", None),
            ),
            otp_spec=OtpSpec(
                keyword="Vercel",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for Vercel signup OTP (keyword='Vercel' to avoid HTML #000000 fake code)...",
                success_label="Vercel OTP",
            ),
            # Vercel Kasada 是隐形人机分不是 captcha widget，不需要 solver。
            use_captcha=False,
            preflight=None,
        )

    def check_valid(self, account: Account) -> bool:
        """校验账号有效性：有 api_key 则 curl ai-gateway.vercel.sh/v1/models 验证。

        工单号（appeal_submitted、无 key）视为 PENDING 不算 valid，返回 False。
        """
        from platforms.vercel.core import VercelClient

        client = VercelClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        api_key = str((account.extra or {}).get("api_key") or account.token or "")
        if not api_key:
            return False
        try:
            return client.verify_api_key(api_key)
        except Exception:
            return False

    def get_platform_actions(self) -> list:
        # 两步流：第一步注册填表（appeals 201→PENDING）；第二步绑卡+拿key（主人 4-8h 审核通过后手动触发）。
        # 第二步复用 scripts/vercel_full_bindkey.py 已验证逻辑：浏览器登录拿 vcp_ + 纯协议绑卡建key送额度。
        return [
            {"id": "bind_card_create_key", "label": "绑卡+拿API Key（第二步）", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        """第二步：审核通过后手动触发——登录拿 vcp_ + 纯协议绑卡 + 建 vck_ key + 送 $5 额度。

        绑卡/建key/送额度全纯协议（VercelClient.bind_card_protocol/create_ai_gateway_key/trigger_free_credit），
        只有登录拿 vcp_ token 要浏览器（Kasada soft-fail 纯协议不发 OTP）。复用 vercel_full_bindkey.py。
        成功后 save_account 标记 REGISTERED + 存 vck_ token（第二步完成）。
        """
        if action_id != "bind_card_create_key":
            raise NotImplementedError(f"Vercel 不支持操作: {action_id}")
        import json as _json
        import subprocess
        import sys
        from pathlib import Path
        from core.db import save_account
        email = str(account.email or "").strip()
        if not email:
            return {"ok": False, "error": "缺少邮箱，无法绑卡拿key"}
        root = Path(__file__).resolve().parents[2]
        script = root / "scripts" / "vercel_full_bindkey.py"
        out = root / "scripts" / "_vercel_full_bindkey_result.json"
        self.log(f"[vercel] 第二步绑卡+拿key: {email}（subprocess 调 vercel_full_bindkey.py）")
        with _bindkey_global_lock(self.log):
            try:
                if out.exists():
                    out.unlink()
            except Exception:
                pass
            try:
                proc = subprocess.run([sys.executable, str(script), email], cwd=str(root),
                               capture_output=True, text=True, timeout=420, encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "绑卡拿key超时（登录OTP可能未收到，等审核通过后再试）"}
            try:
                data = _json.load(open(out, encoding="utf-8"))
            except Exception:
                data = {}
            if data.get("email") and str(data.get("email")).lower() != email.lower():
                self.log(f"[vercel] result.json email 不匹配({data.get('email')}!={email})，subprocess 可能崩溃")
                err_tail = (proc.stderr or "")[-300:] if proc else ""
                return {"ok": False, "error": f"绑卡脚本崩溃未产出结果: {(data.get('error') or err_tail or '无stderr')[:120]}"}
            vck = str(data.get("api_key_full") or "").strip()
            if not vck:
                return {"ok": False, "error": data.get("error") or "绑卡拿key失败（未拿到 vck_ key，可能登录OTP未到/卡被拒）"}
            team_id = str(data.get("team_id") or "")
            bal = data.get("balance_after") or {}
            # 第二步完成：更新 db，status=REGISTERED + vck_ token
            extra = dict(account.extra or {})
            extra.update({
                "api_key": vck, "ai_api_token": vck, "appeal_stage": "key_claimed",
                "approved": True, "team_id": team_id, "card_bound": True,
                "balance_usd": bal.get("cumulativeBalance"),
                "has_verified_payment_method": True, "has_ever_paid": False,
            })
            save_account(Account(
                platform="vercel", email=email, password="", user_id=team_id,
                token=vck, status=AccountStatus.REGISTERED, extra=extra,
            ))
            self.log(f"[vercel] 第二步完成: {email} vck_={vck[:20]}... team={team_id}")
            return {"ok": True, "data": {
                "credential_updates": {"api_key": vck, "ai_api_token": vck},
                "account_overview": {
                    "appeal_stage": "key_claimed", "team_id": team_id, "card_bound": True,
                    "balance_usd": str(bal.get("cumulativeBalance") or ""),
                },
            }}
