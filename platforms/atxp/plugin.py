from __future__ import annotations

import importlib

from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
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
            "clowdbot_status": result.get("clowdbot_status", "pending"),
            "create_clowdbot_completed": bool(result.get("create_clowdbot_completed")),
            "claim_email_completed": bool(result.get("claim_email_completed")),
            "reward_progress": result.get("reward_progress"),
            "task_error": result.get("task_error", ""),
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
                "clowdbot_status": result.get("clowdbot_status", "pending"),
                "reward_progress": result.get("reward_progress"),
                "task_error": result.get("task_error", ""),
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
            ),
            otp_spec=OtpSpec(
                keyword="Privy",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="等待 Privy 验证码...",
                success_label="Privy 验证码",
            ),
        )

    def get_platform_actions(self) -> list:
        return [
            {
                "id": "retry_clowdbot_tasks",
                "label": "重试 Clowdbot 任务",
                "params": [],
            }
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "retry_clowdbot_tasks":
            raise NotImplementedError(f"ATXP 不支持操作: {action_id}")
        return {"ok": True, "data": {"message": "Clowdbot 重试逻辑将在后续任务中补齐"}}

    def check_valid(self, account: Account) -> bool:
        return bool(account.token)
