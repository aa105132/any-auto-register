"""Swarms Marketplace 平台插件。

纯协议注册链路（无 Turnstile）：
  1. Swarms Marketplace /signin/signup Next Server Action 注册
  2. 邮箱收取确认链接 → 解析 token_hash + type=signup
  3. Supabase GoTrue /auth/v1/verify 验证邮箱
  4. /auth/v1/token?grant_type=password 登录
  5. /auth/v1/user + tRPC main.getUser 获取用户信息
  6. tRPC main.updateUsername/updateFullName 补全资料
  7. tRPC apiKey.addApiKey 创建 API Key

API Key 格式: sk-xxxx，用于 swarms.world 所有 API 调用。
"""

from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import (
    BrowserRegistrationAdapter,
    LinkSpec,
    ProtocolMailboxAdapter,
    RegistrationResult,
)
from core.registry import register


@register
class SwarmsPlatform(BasePlatform):
    name = "swarms"
    display_name = "Swarms Marketplace"
    version = "1.0.0"
    supported_executors = ["protocol", "headed", "headless"]
    supported_identity_modes = ["mailbox"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password(length=16)

    def _map_swarms_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        api_key = result.get("api_key", "")
        api_key_info = dict(result.get("api_key_info") or {})
        user_info = dict(result.get("user_info") or {})
        cookies = dict(result.get("cookies") or {})
        session_cookie = result.get("session_cookie", "")

        profile = dict(result.get("profile") or {})
        credit_info = result.get("credit_info") if isinstance(result.get("credit_info"), dict) else {}
        account_overview = {
            "email": result.get("email", ""),
            "user_id": result.get("user_id", ""),
            "user_name": result.get("user_name", ""),
            "username": result.get("username", "") or profile.get("username", ""),
            "api_key_preview": api_key[:12] + "..." if len(api_key) > 12 else api_key,
            "credit_info": credit_info,
        }

        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=result.get("user_id", ""),
            token=api_key,
            status=AccountStatus.REGISTERED,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_info": api_key_info,
                "profile": profile,
                "username": result.get("username", "") or profile.get("username", ""),
                "credit_info": credit_info,
                "access_token": result.get("access_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "user_info": user_info,
                "cookies": session_cookie,
                "cookie_map": cookies,
                "account_overview": account_overview,
                "api_base": "https://swarms.world/api",
                "api_note": "Bearer token 格式，设置 SWARMS_API_KEY 或 Authorization 头",
            },
        )


    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_swarms_result(
                result, password=ctx.password or ""
            ),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.swarms.browser_mailbox",
                fromlist=["SwarmsBrowserMailboxWorker"],
            ).SwarmsBrowserMailboxWorker(
                headless=ctx.executor_type == "headless",
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                verification_link_callback=artifacts.verification_link_callback,
            ),
            link_spec=LinkSpec(
                keyword="swarms",
                timeout=240,
                wait_message="等待 Swarms 浏览器注册确认链接...",
                success_label="Swarms 确认链接",
                preview_chars=120,
            ),
        )
    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_swarms_result(
                result, password=ctx.password or ""
            ),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.swarms.protocol_mailbox",
                fromlist=["SwarmsProtocolMailboxWorker"],
            ).SwarmsProtocolMailboxWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                verification_link_callback=artifacts.verification_link_callback,
            ),
            link_spec=LinkSpec(
                keyword="swarms",
                timeout=180,
                wait_message="等待 Swarms 邮件确认链接...",
                success_label="Swarms 确认链接",
                preview_chars=120,
            ),
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        return bool(account.token or extra.get("api_key") or extra.get("access_token"))

    def get_platform_actions(self) -> list:
        return [
            {
                "id": "export_api_key",
                "label": "导出 API Key",
                "params": [],
            },
            {
                "id": "export_all_keys",
                "label": "导出全部 Swarms API Key",
                "params": [],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "export_api_key":
            return self._do_export_api_key(account)
        if action_id == "export_all_keys":
            return self._do_export_all_keys()
        raise NotImplementedError(f"未知操作: {action_id}")

    @staticmethod
    def _do_export_api_key(account: Account) -> dict:
        from pathlib import Path
        extra = dict(account.extra or {})
        api_key = str(extra.get("api_key", "") or account.token or "").strip()
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key"}
        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "swarms_keys.txt"
        with path.open("a", encoding="utf-8") as f:
            f.write(api_key + "\n")
        return {
            "ok": True,
            "data": {
                "message": f"API Key 已导出到 {path}",
                "email": account.email,
                "key_preview": api_key[:12] + "..." if len(api_key) > 12 else api_key,
            },
        }

    @staticmethod
    def _do_export_all_keys() -> dict:
        from pathlib import Path
        from core.db import AccountModel, engine
        from sqlmodel import Session, select

        output_dir = Path("output")
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "swarms_keys.txt"

        count = 0
        with Session(engine) as session:
            rows = session.exec(
                select(AccountModel).where(AccountModel.platform == "swarms")
            ).all()
            with path.open("w", encoding="utf-8") as f:
                for row in rows:
                    extra = dict(row.extra or {})
                    key = str(extra.get("api_key", "") or row.token or "").strip()
                    if key:
                        f.write(key + "\n")
                        count += 1

        return {
            "ok": True,
            "data": {
                "message": f"已导出 {count} 个 API Key 到 {path}",
                "count": count,
            },
        }
