"""Fireworks AI 平台插件。

纯协议注册链路（无 Turnstile）：
  1. POST /signup 提交邮箱+密码
  2. 邮箱收取验证链接，点击验证
  3. POST /login/email 登录获取 session
  4. 通过 web dashboard 或 REST API 创建 API key
  5. $5 注册奖励自动发放
"""

from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import LinkSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registry import register


@register
class FireworksPlatform(BasePlatform):
    name = "fireworks"
    display_name = "Fireworks AI"
    version = "1.0.0"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password(length=16)

    def _map_fireworks_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        api_key = result.get("api_key", "")
        api_key_info = dict(result.get("api_key_info") or {})
        account_info = dict(result.get("account_info") or {})
        cookies = dict(result.get("cookies") or {})
        session_cookie = result.get("session_cookie", "")

        account_overview = {
            "email": result.get("email", ""),
            "account_id": account_info.get("account_id", ""),
            "user_id": account_info.get("user_id", ""),
            "api_key_prefix": api_key_info.get("prefix", ""),
            "api_key_id": api_key_info.get("key_id", ""),
        }

        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=account_info.get("user_id", ""),
            token=api_key,
            status=AccountStatus.REGISTERED,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,  # 通用导出兼容字段
                "api_key_info": api_key_info,
                "account_info": account_info,
                "cookies": session_cookie,
                "cookie_map": cookies,
                "account_overview": account_overview,
            },
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_fireworks_result(
                result, password=ctx.password or ""
            ),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.fireworks.protocol_mailbox",
                fromlist=["FireworksProtocolMailboxWorker"],
            ).FireworksProtocolMailboxWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                verification_link_callback=artifacts.verification_link_callback,
            ),
            link_spec=LinkSpec(
                keyword="",  # 空 keyword 匹配所有邮件，由 _extract_verification_link 做链接提取
                timeout=120,
                wait_message="等待 Fireworks AI 验证链接...",
                success_label="Fireworks AI 验证链接",
                preview_chars=80,
            ),
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        return bool(account.token or extra.get("api_key") or extra.get("cookies"))

    def get_platform_actions(self) -> list:
        return [
            {
                "id": "export_api_key",
                "label": "导出 API Key",
                "params": [],
            },
            {
                "id": "export_all_keys",
                "label": "导出全部 Fireworks API Key",
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
        import os
        extra = dict(account.extra or {})
        api_key = str(extra.get("api_key", "") or account.token or "").strip()
        if not api_key:
            return {"ok": False, "error": "该账号没有 API Key"}
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "fireworks_keys.txt")
        with open(path, "a", encoding="utf-8") as f:
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
        import os
        from core.db import AccountModel, engine
        from sqlmodel import Session, select

        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "fireworks_keys.txt")

        count = 0
        with Session(engine) as session:
            rows = session.exec(
                select(AccountModel).where(AccountModel.platform == "fireworks")
            ).all()
            with open(path, "w", encoding="utf-8") as f:
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