"""CodeBanana 平台插件。"""

from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registry import register


@register
class CodeBananaPlatform(BasePlatform):
    name = "codebanana"
    display_name = "CodeBanana"
    version = "1.0.0"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password(length=16)

    def _map_codebanana_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        session_json = dict(result.get("session_json") or {})
        user = dict(session_json.get("user") or {})
        user_id = str(result.get("user_id") or user.get("id") or "")
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id,
            token=result.get("session_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "username": result.get("username", ""),
                "cbbot_key": user_id,
                "session_token": result.get("session_token", ""),
                "jwtToken": result.get("jwtToken", ""),
                "cookies": result.get("cookies", {}),
                "session_json": session_json,
                "csrf_token": result.get("csrf_token", ""),
            },
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_codebanana_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.codebanana.protocol_mailbox",
                fromlist=["CodeBananaProtocolMailboxWorker"],
            ).CodeBananaProtocolMailboxWorker(
                base_url=str(
                    ctx.extra.get("codebanana_base_url", "https://www.codebanana.com")
                    or "https://www.codebanana.com"
                ),
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
            ),
            otp_spec=OtpSpec(
                keyword="CodeBanana",
                code_pattern=r"(?<!\d)(\d{4})(?!\d)",
                wait_message="等待 CodeBanana 验证码...",
                success_label="CodeBanana 验证码",
            ),
        )

    def check_valid(self, account: Account) -> bool:
        return bool(account.token)
