from __future__ import annotations

import importlib
import uuid

from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from platforms.atxp.core import AtxpClient
from platforms.atxp.protocol_mailbox import AtxpProtocolMailboxWorker


def _identity_register(cls):
    return cls


def _load_register(import_module=importlib.import_module):
    try:
        return import_module("core.registry").register
    except ModuleNotFoundError as exc:  # pragma: no cover - 测试环境缺少可选依赖时降级
        if getattr(exc, "name", "") != "sqlmodel":
            raise
        return _identity_register


register = _load_register()


@register
class AtxpPlatform(BasePlatform):
    name = "atxp"
    display_name = "ATXP"
    version = "1.0.0"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]

    def __init__(self, config: RegisterConfig = None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or ""

    def _map_atxp_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        gateway_health = result.get("gateway_health") or {}
        account_overview = {
            "gateway_health": gateway_health,
            "gateway_health_alive": bool(gateway_health.get("success")),
            "gateway_health_model": gateway_health.get("model", ""),
            "gateway_health_checked_at": gateway_health.get("checked_at", ""),
            "gateway_error": result.get("gateway_error", "") or gateway_health.get("error", ""),
            "clowdbot_status": result.get("clowdbot_status", "pending"),
            "create_clowdbot_completed": bool(result.get("create_clowdbot_completed")),
            "claim_email_completed": bool(result.get("claim_email_completed")),
            "reward_progress": result.get("reward_progress"),
            "task_error": result.get("task_error", ""),
            "balance": result.get("balance"),
            "balance_error": result.get("balance_error", ""),
            "balance_warning": result.get("balance_warning", ""),
            "atxp_me": result.get("me") or {},
            "wallet_info": result.get("wallet_info") or {},
            "clowdbot_result": result.get("clowdbot_result") or {},
        }
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=result.get("account_id", ""),
            token=result.get("connection_string", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "privy_token": result.get("privy_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "account_id": result.get("account_id", ""),
                "connection_token": result.get("connection_token", ""),
                "connection_string": result.get("connection_string", ""),
                "wallet_address": result.get("wallet_address", ""),
                "gateway_health": gateway_health,
                "gateway_error": result.get("gateway_error", "") or gateway_health.get("error", ""),
                "clowdbot_status": result.get("clowdbot_status", "pending"),
                "reward_progress": result.get("reward_progress"),
                "task_error": result.get("task_error", ""),
                "balance": result.get("balance"),
                "balance_error": result.get("balance_error", ""),
                "balance_warning": result.get("balance_warning", ""),
                "atxp_me": result.get("me") or {},
                "wallet_info": result.get("wallet_info") or {},
                "clowdbot_result": result.get("clowdbot_result") or {},
                "clowdbot_instance_id": result.get("clowdbot_instance_id", ""),
                "claimed_agent_email": result.get("claimed_agent_email", ""),
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_atxp_result(
                result,
                password=ctx.password or "",
            ),
            worker_builder=lambda ctx, artifacts: AtxpProtocolMailboxWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
                enable_clowdbot=bool(ctx.extra.get("enable_clowdbot")),
            ),
            otp_spec=OtpSpec(
                keyword="ATXP",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="等待 ATXP 验证码...",
                success_label="ATXP 验证码",
            ),
        )

    def get_platform_actions(self) -> list:
        return [
            {
                "id": "reauth_privy",
                "label": "重新认证 Privy (邮箱OTP)",
                "params": [],
            },
            {
                "id": "retry_clowdbot_tasks",
                "label": "一键领取 Clowdbot 奖励",
                "params": [],
            },
            {
                "id": "check_balance",
                "label": "查询余额",
                "params": [],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        self.log(f"[ATXP] execute_action called: action_id={action_id!r}")
        if action_id == "reauth_privy":
            return self._action_reauth_privy(account)
        if action_id == "check_balance":
            return self._action_check_balance(account)
        if action_id == "retry_clowdbot_tasks":
            return self._action_retry_clowdbot(account)
        raise NotImplementedError(f"ATXP 不支持操作: {action_id}")

    def _action_reauth_privy(self, account: Account) -> dict:
        """通过邮箱 OTP 重新认证 Privy，获取全新 privy_token + refresh_token。"""
        from core.base_mailbox import create_mailbox, MailboxAccount

        proxy = self.config.proxy if self.config else None
        client = AtxpClient(proxy=proxy, log_fn=self.log)
        email = account.email

        # 1) 从 provider_accounts 找到关联的 mailbox provider
        provider_accounts = account.extra.get("provider_accounts") or []
        mailbox_pa = next(
            (pa for pa in provider_accounts if pa.get("provider_type") == "mailbox"),
            None,
        )
        if not mailbox_pa:
            return {"ok": False, "data": {"message": "该账号没有关联 mailbox provider，无法通过邮箱 OTP 重新认证"}}

        provider_name = str(mailbox_pa.get("provider_name") or "")
        if not provider_name:
            return {"ok": False, "data": {"message": "mailbox provider_name 为空"}}

        self.log(f"mailbox provider: {provider_name}")

        # 2) 用 create_mailbox 创建 mailbox 实例（全局配置自动从 ProviderSettings 加载）
        mailbox = create_mailbox(provider=provider_name, proxy=proxy)

        # 3) 构建 MailboxAccount（含 per-account credentials 和 metadata）
        credentials = mailbox_pa.get("credentials") or {}
        metadata = mailbox_pa.get("metadata") or {}

        mail_email = str(mailbox_pa.get("login_identifier") or email)

        # 3a) 确保临时邮箱在 215.im 上存在（24h 过期后需重建）
        ensure_fn = getattr(mailbox, "ensure_inbox", None)
        if callable(ensure_fn):
            self.log(f"重建临时邮箱: {mail_email}")
            try:
                new_token = ensure_fn(mail_email)
                if new_token:
                    self.log(f"临时邮箱重建成功，新 token: {new_token[:8]}...")
                    credentials["mailbox_token"] = new_token
                else:
                    self.log("临时邮箱重建成功，但未返回 token")
            except Exception as exc:
                self.log(f"ensure_inbox 失败: {exc}，尝试继续...")
        else:
            # 非 YYDS Mail：注入已有 token
            mailbox_token = str(credentials.get("mailbox_token") or "").strip()
            if mailbox_token and hasattr(mailbox, "_mailbox_token"):
                mailbox._mailbox_token = mailbox_token
                self.log(f"注入 mailbox_token: {mailbox_token[:8]}...")

        provider_resource = None
        provider_resources = account.extra.get("provider_resources") or []
        for pr in provider_resources:
            if pr.get("provider_type") == "mailbox":
                provider_resource = pr
                break

        mail_acct = MailboxAccount(
            email=mail_email,
            account_id=str(metadata.get("account_id") or credentials.get("address_id") or ""),
            extra={
                "provider_account": mailbox_pa,
                "provider_resource": provider_resource or {},
            },
        )

        # 4) 发送 OTP → 读取验证码 → 认证
        ca_id = str(uuid.uuid4())
        self.log(f"获取当前邮件列表...")
        try:
            before_ids = mailbox.get_current_ids(mail_acct)
        except Exception as exc:
            self.log(f"get_current_ids failed: {exc}")
            before_ids = set()
        self.log(f"before_ids count: {len(before_ids)}")

        self.log(f"发送 Privy 验证码到 {mail_email}...")
        client.send_privy_code(mail_email, ca_id)

        self.log("等待邮箱验证码（带诊断日志）...")
        otp = self._poll_otp_with_logging(
            mailbox, mail_acct, before_ids, keyword="ATXP",
            code_pattern=r"(?<!\d)(\d{6})(?!\d)", timeout=120,
        )
        self.log(f"收到验证码: {otp[:2]}****")

        self.log("认证 Privy...")
        auth_result = client.authenticate_privy(mail_email, otp, ca_id)
        privy_token = str(auth_result.get("token") or "")
        refresh_token = str(auth_result.get("refresh_token") or "")

        if not privy_token:
            return {"ok": False, "data": {"message": "Privy 认证成功但未返回 token"}}

        self.log("Privy 认证成功，刷新 ATXP bundle...")

        # 5) 用新 token 重新获取 bundle（connection_token 等可能也过期了）
        try:
            bundle = client.fetch_atxp_bundle(privy_token)
            account_id = str(bundle.get("account_id") or "")
            connection_token = str(bundle.get("connection_token") or "")
            wallet_address = str(bundle.get("wallet_address") or "")
            self.log(f"bundle 刷新完成: account_id={account_id}")
        except Exception as exc:
            self.log(f"fetch_atxp_bundle failed: {exc}，仅更新 token")
            account_id = str(account.user_id or account.extra.get("account_id") or "")
            connection_token = ""
            wallet_address = ""

        credential_updates = {
            "privy_token": privy_token,
            "refresh_token": refresh_token,
        }
        if account_id:
            credential_updates["account_id"] = account_id
        if connection_token:
            credential_updates["connection_token"] = connection_token
            connection_string = (
                f"https://accounts.atxp.ai?connection_token={connection_token}"
                f"&account_id={account_id}"
            )
            credential_updates["connection_string"] = connection_string
        if wallet_address:
            credential_updates["wallet_address"] = wallet_address

        return {
            "ok": True,
            "data": {
                "message": f"Privy 重新认证成功，token 已刷新",
                "credential_updates": credential_updates,
            },
        }

    def _action_retry_clowdbot(self, account: Account) -> dict:
        client = AtxpClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        privy_token = str(account.extra.get("privy_token") or "")
        refresh_token = str(account.extra.get("refresh_token") or "")
        credential_updates: dict = {}

        # 先尝试刷新 token，防止过期导致 OIDC 401
        refresh_ok = False
        if refresh_token:
            try:
                refreshed = client.refresh_privy_token(refresh_token)
                new_token = str(refreshed.get("token") or "")
                new_refresh = str(refreshed.get("refresh_token") or "")
                if new_token:
                    privy_token = new_token
                    credential_updates["privy_token"] = new_token
                    refresh_ok = True
                if new_refresh:
                    credential_updates["refresh_token"] = new_refresh
            except Exception as exc:
                self.log(f"[ATXP] refresh_privy_token failed: {exc}")

        # refresh 失败时自动走邮箱 OTP 重新认证
        if not refresh_ok:
            self.log("[ATXP] token refresh 失败，尝试邮箱 OTP 重新认证...")
            reauth_result = self._action_reauth_privy(account)
            if not reauth_result.get("ok"):
                return reauth_result
            reauth_creds = (reauth_result.get("data") or {}).get("credential_updates") or {}
            privy_token = str(reauth_creds.get("privy_token") or privy_token)
            credential_updates.update(reauth_creds)

        task_result = client.complete_clowdbot_tasks(
            privy_token,
            str(account.user_id or account.extra.get("account_id") or ""),
            account.email,
        )
        credential_updates["clowdbot_instance_id"] = str(task_result.get("instance_id") or "")
        credential_updates["claimed_agent_email"] = str(task_result.get("claimed_agent_email") or "")
        return {
            "ok": True,
            "data": {
                "message": "Clowdbot 任务补跑完成",
                "credential_updates": credential_updates,
                "account_overview": {
                    "clowdbot_status": "completed",
                    "create_clowdbot_completed": bool(task_result.get("create_clowdbot_completed")),
                    "claim_email_completed": bool(task_result.get("claim_email_completed")),
                    "reward_progress": task_result.get("reward_progress"),
                    "task_error": "",
                    "clowdbot_result": task_result,
                },
            },
        }

    def _action_check_balance(self, account: Account) -> dict:
        privy_token = str(account.extra.get("privy_token") or "")
        refresh_token = str(account.extra.get("refresh_token") or "")
        if not privy_token:
            return {"ok": False, "data": {"message": "缺少 privy_token，无法查询余额"}}
        client = AtxpClient(proxy=self.config.proxy if self.config else None, log_fn=self.log)
        balance_data = client.check_balance(privy_token, refresh_token=refresh_token)
        balance = balance_data.get("balance") or {}
        restriction = balance_data.get("restriction") or {}
        usdc = str(balance.get("usdc") or "0")
        iou = str(balance.get("iou") or "0")
        restriction_error = str(restriction.get("error") or "")

        credential_updates: dict = {}
        refreshed_token = balance_data.get("_refreshed_token", "")
        refreshed_refresh = balance_data.get("_refreshed_refresh_token", "")
        if refreshed_token:
            credential_updates["privy_token"] = refreshed_token
        if refreshed_refresh:
            credential_updates["refresh_token"] = refreshed_refresh

        result: dict = {
            "ok": True,
            "data": {
                "message": f"余额: IOU={iou}, USDC={usdc}" + (f" (受限: {restriction_error})" if restriction_error else ""),
                "balance_iou": iou,
                "balance_usdc": usdc,
                "balance_restriction": restriction_error,
                "account_overview": {
                    "balance_iou": iou,
                    "balance_usdc": usdc,
                    "balance_restriction": restriction_error,
                },
            },
        }
        if credential_updates:
            result["data"]["credential_updates"] = credential_updates
        return result

    def _poll_otp_with_logging(
        self,
        mailbox,
        mail_acct,
        before_ids: set,
        *,
        keyword: str,
        code_pattern: str,
        timeout: int = 120,
    ) -> str:
        """wait_for_code 的诊断版本，把每次轮询结果写入日志。遇 429 自动退避。"""
        import re
        import time

        seen = {str(item) for item in (before_ids or set())}
        pattern = re.compile(code_pattern, re.IGNORECASE)
        start = time.time()
        poll_count = 0
        base_interval = 3
        current_interval = base_interval
        consecutive_429 = 0
        while time.time() - start < timeout:
            poll_count += 1
            try:
                # 尝试使用 _list_messages（YYDS / GptMail 等通用 mailbox）
                list_fn = getattr(mailbox, "_list_messages", None)
                if callable(list_fn):
                    messages = list_fn(mail_acct.email)
                else:
                    # 兜底：直接调 wait_for_code（无法获取中间日志）
                    self.log(f"[poll#{poll_count}] mailbox 无 _list_messages，fallback wait_for_code")
                    return mailbox.wait_for_code(
                        mail_acct, keyword=keyword, code_pattern=code_pattern,
                        before_ids=before_ids, timeout=max(1, timeout - int(time.time() - start)),
                    )
                # 成功请求 → 重置退避
                consecutive_429 = 0
                current_interval = base_interval
                self.log(f"[poll#{poll_count}] 收到 {len(messages)} 封邮件")
                for msg in messages:
                    msg_id = str(msg.get("id") or "")
                    if not msg_id or msg_id in seen:
                        continue
                    seen.add(msg_id)
                    # 获取详情
                    detail_fn = getattr(mailbox, "_message_detail", None)
                    detail = detail_fn(msg_id) if callable(detail_fn) else {}
                    subject = str(msg.get("subject") or detail.get("subject") or "")
                    body = str(
                        msg.get("text") or msg.get("body_text") or msg.get("body") or msg.get("content") or msg.get("html") or msg.get("body_html")
                        or detail.get("text") or detail.get("body_text") or detail.get("body") or detail.get("content") or detail.get("html") or detail.get("body_html")
                        or ""
                    )
                    text = f"{subject} {body}"
                    self.log(f"[poll#{poll_count}] 新邮件 id={msg_id} subject={subject[:60]}")
                    if keyword and keyword.lower() not in text.lower():
                        self.log(f"[poll#{poll_count}] 关键词 {keyword!r} 未匹配，跳过")
                        continue
                    match = pattern.search(text)
                    if match:
                        code = match.group(1) if match.lastindex else match.group(0)
                        self.log(f"[poll#{poll_count}] 匹配到验证码")
                        return code
                    self.log(f"[poll#{poll_count}] 关键词匹配但未找到验证码模式")
            except Exception as exc:
                error_text = str(exc).lower()
                is_429 = "429" in error_text or "rate limit" in error_text or "too many" in error_text
                if is_429:
                    consecutive_429 += 1
                    current_interval = min(base_interval * (2 ** consecutive_429), 30)
                    self.log(f"[poll#{poll_count}] 429 限流，退避 {current_interval}s (连续第{consecutive_429}次)")
                else:
                    self.log(f"[poll#{poll_count}] 轮询异常: {type(exc).__name__}: {exc}")
            time.sleep(current_interval)
        raise TimeoutError(f"等待 ATXP 验证码超时 ({timeout}s, {poll_count} 次轮询)")

    def check_valid(self, account: Account) -> bool:
        return bool(account.token)
