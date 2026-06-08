"""Dify Cloud 平台插件。

纯协议注册链路（无 Turnstile）：
  1. POST /console/api/email-code-login 发送验证码
  2. 邮箱收取 6 位验证码
  3. POST /console/api/email-code-login/validity 验证码登录（自动注册）
  4. POST /console/api/apps 创建聊天助手
  5. POST /console/api/apps/{id}/api-keys 生成 API Key

API Key 格式: app-xxxx，用于 /v1/chat-messages 等应用 API。
Dify 不提供原生 OpenAI 兼容接口，需自行转换或使用 dify2openai 桥接。
"""

from __future__ import annotations

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registry import register


@register
class DifyPlatform(BasePlatform):
    name = "dify"
    display_name = "Dify Cloud"
    version = "1.0.0"
    supported_executors = ["protocol"]
    supported_identity_modes = ["mailbox"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        return password or self._make_random_password(length=16)

    def _map_dify_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        api_key = result.get("api_key", "")
        api_key_info = dict(result.get("api_key_info") or {})
        account_info = dict(result.get("account_info") or {})
        app_info = dict(result.get("app_info") or {})
        cookies = dict(result.get("cookies") or {})
        session_cookie = result.get("session_cookie", "")
        app_id = result.get("app_id", "")

        app_url = f"https://cloud.dify.ai/app/{app_id}" if app_id else ""
        dsl_imported = result.get("dsl_imported", False)

        account_overview = {
            "email": result.get("email", ""),
            "user_id": account_info.get("id", ""),
            "user_name": account_info.get("name", ""),
            "app_id": app_id,
            "app_name": app_info.get("name", ""),
            "app_url": app_url,
            "api_key_preview": api_key[:12] + "..." if len(api_key) > 12 else api_key,
            "dsl_imported": dsl_imported,
        }

        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=account_info.get("id", ""),
            token=api_key,
            status=AccountStatus.REGISTERED,
            extra={
                "api_key": api_key,
                "ai_api_token": api_key,
                "api_key_info": api_key_info,
                "app_id": app_id,
                "app_url": app_url,
                "app_info": app_info,
                "account_info": account_info,
                "cookies": session_cookie,
                "cookie_map": cookies,
                "access_token": result.get("access_token", ""),
                "account_overview": account_overview,
                "dsl_imported": dsl_imported,
                "api_base": "https://api.dify.ai/v1",
                "api_note": "Dify /v1/chat-messages 格式；需 dify2openai 桥接转 OpenAI 格式",
            },
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_dify_result(
                result, password=ctx.password or ""
            ),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.dify.protocol_mailbox",
                fromlist=["DifyProtocolMailboxWorker"],
            ).DifyProtocolMailboxWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
            ),
            otp_spec=OtpSpec(
                keyword="Dify",
                timeout=120,
                code_pattern=r"\b(\d{6})\b",
                wait_message="等待 Dify 验证码...",
                success_label="Dify 验证码",
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
                "label": "导出全部 Dify API Key",
                "params": [],
            },
            {
                "id": "import_dsl_template",
                "label": "导入 DSL 模板到账号",
                "params": [],
            },
            {
                "id": "export_dsl",
                "label": "导出应用 DSL",
                "params": [],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        if action_id == "export_api_key":
            return self._do_export_api_key(account)
        if action_id == "export_all_keys":
            return self._do_export_all_keys()
        if action_id == "import_dsl_template":
            return self._do_import_dsl(account)
        if action_id == "export_dsl":
            return self._do_export_dsl(account)
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
        path = os.path.join(output_dir, "dify_keys.txt")
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
    def _do_import_dsl(account: Account) -> dict:
        import os
        extra = dict(account.extra or {})
        access_token = str(extra.get("access_token", "")).strip()
        if not access_token:
            return {"ok": False, "error": "缺少 access_token，无法调用 Console API"}
        template_path = os.path.join(os.path.dirname(__file__), "template.yml")
        if not os.path.isfile(template_path):
            return {"ok": False, "error": f"DSL 模板文件不存在: {template_path}"}
        with open(template_path, "r", encoding="utf-8") as f:
            dsl_content = f.read()
        from platforms.dify.core import DifyClient
        client = DifyClient()
        client._access_token = access_token
        result = client.import_dsl(dsl_content, name="OpenAI Bridge")
        app_id = result.get("app_id", "")
        if not app_id:
            return {"ok": False, "error": f"DSL 导入失败: {result}"}
        api_key_info = client.create_api_key(app_id)
        api_key = api_key_info.get("token", "")
        return {
            "ok": True,
            "data": {
                "message": f"DSL 模板导入成功",
                "app_id": app_id,
                "app_url": f"https://cloud.dify.ai/app/{app_id}",
                "api_key": api_key,
                "api_key_preview": api_key[:12] + "..." if len(api_key) > 12 else api_key,
            },
        }

    @staticmethod
    def _do_export_dsl(account: Account) -> dict:
        import os
        extra = dict(account.extra or {})
        access_token = str(extra.get("access_token", "")).strip()
        app_id = str(extra.get("app_id", "")).strip()
        if not access_token:
            return {"ok": False, "error": "缺少 access_token"}
        if not app_id:
            return {"ok": False, "error": "缺少 app_id"}
        from platforms.dify.core import DifyClient
        client = DifyClient()
        client._access_token = access_token
        dsl_yaml = client.export_dsl(app_id)
        if not dsl_yaml:
            return {"ok": False, "error": "导出为空"}
        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"dify_dsl_{app_id[:8]}.yml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(dsl_yaml)
        return {
            "ok": True,
            "data": {
                "message": f"DSL 已导出到 {path}",
                "app_id": app_id,
                "path": path,
            },
        }

    @staticmethod
    def _do_export_all_keys() -> dict:
        import os
        from core.db import AccountModel, engine
        from sqlmodel import Session, select

        output_dir = "output"
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "dify_keys.txt")

        count = 0
        with Session(engine) as session:
            rows = session.exec(
                select(AccountModel).where(AccountModel.platform == "dify")
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
