"""Enter platform plugin."""

from __future__ import annotations

import random
import string
from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    OtpSpec,
    ProtocolMailboxAdapter,
    RegistrationResult,
)
from core.registry import register
from platforms.enter.core import EnterClient


@register
class EnterPlatform(BasePlatform):
    name = "enter"
    display_name = "Enter"
    version = "1.0.0"
    supported_executors = ["protocol", "cdp_protocol", "headed"]
    supported_identity_modes = ["mailbox"]
    protocol_captcha_order = ("yescaptcha", "2captcha", "patchright_harvester")

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _strong_password(self) -> str:
        required = [
            random.choice(string.ascii_lowercase),
            random.choice(string.ascii_uppercase),
            random.choice(string.digits),
            random.choice("!@#$%^&*_-+=?"),
        ]
        pool = string.ascii_letters + string.digits + "!@#$%^&*_-+=?"
        chars = required + [random.choice(pool) for _ in range(12)]
        random.shuffle(chars)
        return "".join(chars)

    def _password_is_strong_enough(self, password: str) -> bool:
        raw = str(password or "")
        if len(raw) < 8:
            return False
        classes = [
            any(ch.islower() for ch in raw),
            any(ch.isupper() for ch in raw),
            any(ch.isdigit() for ch in raw),
            any(not ch.isalnum() for ch in raw),
        ]
        return sum(bool(item) for item in classes) >= 3

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if self._password_is_strong_enough(str(password or "")):
            return str(password)
        return self._strong_password()

    def _map_enter_result(self, result: dict[str, Any], *, password: str = "") -> RegistrationResult:
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=str(result.get("user_id", "")),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "access_token": result.get("access_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "id_token": result.get("id_token", ""),
                "expires_in": result.get("expires_in", 0),
                "token_type": result.get("token_type", ""),
                "workspace_id": result.get("workspace_id", ""),
                "plan_type": result.get("plan_type", ""),
                "balance": result.get("balance", 0),
                "balance_bonus": result.get("balance_bonus", 0),
                "balance_daily": result.get("balance_daily", 0),
                "balance_monthly": result.get("balance_monthly", 0),
                "balance_purchase": result.get("balance_purchase", 0),
                "entitlement_daily_credits": result.get("entitlement_daily_credits", 0),
                "entitlement_monthly_build": result.get("entitlement_monthly_build", 0),
                "entitlement_monthly_ai": result.get("entitlement_monthly_ai", 0),
                "entitlement_plan_name": result.get("entitlement_plan_name", ""),
                "subscription_status": result.get("subscription_status", ""),
                "enter_ai_credits_status": result.get("enter_ai_credits_status", ""),
                "project_id": result.get("project_id", ""),
                "project_name": result.get("project_name", ""),
                "preview_url": result.get("preview_url", ""),
                "thread_id": result.get("thread_id", ""),
                "entercloud_enabled": result.get("entercloud_enabled", False),
                "entercloud_setup_completed": result.get("entercloud_setup_completed", False),
                "entercloud_provider": result.get("entercloud_provider", ""),
                "entercloud_cloud_ref": result.get("entercloud_cloud_ref", ""),
                "entercloud_api_url": result.get("entercloud_api_url", ""),
                "entercloud_anon_key": result.get("entercloud_anon_key", ""),
                "ai_api_token": result.get("ai_api_token", ""),
                "ai_connection_state": result.get("ai_connection_state", ""),
                "referral_code_self": result.get("referral_code_self", ""),
                "referral_claimed": result.get("referral_claimed", False),
                "metadata": result.get("metadata", {}),
            },
        )

    def _captcha_preflight(self, ctx) -> None:
        if ctx.identity.identity_provider == "mailbox" and ctx.platform._resolve_captcha_solver() == "local_solver":
            from services.solver_manager import start
            start()

    def build_browser_registration_adapter(self):
        extra = (self.config.extra if self.config else {}) or {}
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_enter_result(result, password=ctx.password or ""),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.enter.browser_register",
                fromlist=["EnterBrowserRegistrar"],
            ).EnterBrowserRegistrar(
                captcha=artifacts.captcha_solver,
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                referrer_code=str(extra.get("enter_referrer_code", "") or ""),
                workspace_id=str(extra.get("enter_workspace_id", "10000010136") or "10000010136"),
                chrome_path=str(extra.get("enter_chrome_path", "") or ""),
                cdp_url=str(extra.get("enter_cdp_url", "") or ""),
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                keyword="Auth0",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for Auth0 email verification code...",
                success_label="Auth0 OTP",
            ),
            use_captcha_for_mailbox=True,
            preflight=self._captcha_preflight,
        )

    def build_protocol_mailbox_adapter(self):
        extra = (self.config.extra if self.config else {}) or {}
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_enter_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.enter.protocol_mailbox",
                fromlist=["EnterProtocolMailboxWorker"],
            ).EnterProtocolMailboxWorker(
                proxy=ctx.proxy,
                referrer_code=str(extra.get("enter_referrer_code", "") or ""),
                workspace_id=str(extra.get("enter_workspace_id", "10000010136") or "10000010136"),
                project_name_prefix=str(extra.get("enter_project_name_prefix", "enter-project") or "enter-project"),
                project_prompt=str(extra.get("enter_project_prompt", "Create a minimal hello world web app.") or "Create a minimal hello world web app."),
                enable_entercloud=bool(extra.get("enter_enable_entercloud", True)),
                enable_ai_capability=bool(extra.get("enter_enable_ai_capability", True)),
                enter2api_enabled=bool(extra.get("enter2api_enabled", False)),
                enter2api_base_url=str(extra.get("enter2api_base_url", "") or ""),
                chrome_path=str(extra.get("enter_chrome_path", "") or ""),
                cdp_url=str(extra.get("enter_cdp_url", "") or ""),
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
                captcha_solver=artifacts.captcha_solver,
            ),
            otp_spec=OtpSpec(
                keyword="Auth0",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="Waiting for Auth0 email verification code...",
                success_label="Auth0 OTP",
            ),
            use_captcha=True,
            preflight=self._captcha_preflight,
        )

    def get_platform_actions(self) -> list:
        return [
            {
                "id": "get_account_state",
                "label": "Query Enter account state (workspace, balance, tokens)",
                "params": [],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id != "get_account_state":
            raise NotImplementedError(f"Enter action not supported: {action_id}")

        access_token = str((account.extra or {}).get("access_token") or account.token or "")
        if not access_token:
            return {"ok": False, "error": "Account missing access_token"}

        try:
            client = EnterClient(proxy=self.config.proxy if self.config else None)
            ws_info = client.get_workspaces(access_token)
            user_info = client.get_user_info(access_token)

            ws_list = []
            plan_type = ""
            balance = 0
            if isinstance(ws_info, dict):
                ws_list = (ws_info.get("data") or {}).get("workspaces") or []
                if ws_list:
                    plan_type = ws_list[0].get("plan_type", "")
                    balance = (ws_list[0].get("credits_balance") or {}).get("total", 0)

            user_data = {}
            if isinstance(user_info, dict):
                user_data = (user_info.get("data") or {}).get("user") or {}

            return {
                "ok": True,
                "data": {
                    "valid": True,
                    "remote_user": {
                        "email": user_data.get("email") or account.email,
                        "user_id": user_data.get("user_id", ""),
                        "referral_code": user_data.get("referral_code", ""),
                        "active_code": user_data.get("active_code", ""),
                    },
                    "workspace": {
                        "plan_type": plan_type,
                        "balance": balance,
                        "workspace_id": ws_list[0].get("id", "") if ws_list else "",
                    },
                },
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def check_valid(self, account: Account) -> bool:
        access_token = str((account.extra or {}).get("access_token") or account.token or "")
        if not access_token:
            return False
        try:
            client = EnterClient(proxy=self.config.proxy if self.config else None)
            ws = client.get_workspaces(access_token)
            return isinstance(ws, dict) and ws.get("code") == 0
        except Exception:
            return False
