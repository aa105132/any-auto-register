"""Dify Cloud 协议邮箱注册 worker。

两种注册策略:
  A. email-code-login (免密码，验证码登录自动创建账号)
  B. email-register (密码注册，需三步：发码→验证→设密码)

默认使用策略 A，失败降级到策略 B。
"""

from __future__ import annotations

import time
from typing import Callable

from platforms.dify.core import DifyClient


class DifyProtocolMailboxWorker:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.client = DifyClient(proxy=proxy, log_fn=log_fn)
        self.log = log_fn

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Callable[[], str] | None = None,
        verification_link_callback: Callable[[], str] | None = None,
    ) -> dict:
        # 1. 尝试 email-code-login（无密码注册）
        register_token = ""
        try:
            send_result = self.client.send_email_code(email)
            register_token = ""
            if isinstance(send_result, dict):
                data = send_result.get("data")
                if isinstance(data, str):
                    register_token = data
                elif isinstance(data, dict):
                    register_token = data.get("token", "")
            self.log("验证码已发送，等待接收...")
        except Exception as exc:
            self.log(f"email-code-login 发送失败: {exc}，尝试 email-register...")
            return self._register_with_password(email, password, otp_callback)

        # 2. 等待验证码
        if not otp_callback:
            raise RuntimeError("Dify 注册需要验证码回调，但未提供 otp_callback")
        code = otp_callback()
        if not code:
            raise RuntimeError("Dify: 未获取到验证码")

        # 3. 验证码登录
        try:
            self.client.verify_email_code(email, code, token=register_token)
        except Exception as exc:
            self.log(f"email-code-login 验证失败: {exc}，尝试 email-register...")
            return self._register_with_password(email, password, otp_callback)

        return self._post_login(email, password)

    def _register_with_password(
        self,
        email: str,
        password: str,
        otp_callback: Callable[[], str] | None,
    ) -> dict:
        send_result = self.client.send_register_email(email)
        register_token = ""
        if isinstance(send_result, dict):
            data = send_result.get("data")
            if isinstance(data, str):
                register_token = data
            elif isinstance(data, dict):
                register_token = data.get("token", "")

        if not otp_callback:
            raise RuntimeError("Dify 注册需要验证码回调")
        code = otp_callback()
        if not code:
            raise RuntimeError("Dify: 未获取到注册验证码")

        verify_result = self.client.verify_register_code(email, code, token=register_token)
        new_token = ""
        if isinstance(verify_result, dict):
            new_token = verify_result.get("token", "")
            if not new_token:
                data = verify_result.get("data")
                if isinstance(data, dict):
                    new_token = data.get("token", "")
                elif isinstance(data, str):
                    new_token = data
        if not new_token:
            new_token = register_token

        self.client.register_with_password(new_token, password)
        return self._post_login(email, password)

    def _post_login(self, email: str, password: str) -> dict:
        # 获取账户信息
        account_info: dict = {}
        try:
            account_info = self.client.get_account_info()
        except Exception as exc:
            self.log(f"获取账户信息失败（非阻塞）: {exc}")

        # 安装免费 AI 额度插件（openai/anthropic/gemini/deepseek/x/tongyi）
        plugins_installed: list[str] = []
        try:
            self.log("安装免费 AI 额度插件...")
            install_result = self.client.install_trial_plugins()
            if install_result.get("ok") or install_result.get("all_installed"):
                plugins_installed = list(self.client.TRIAL_PLUGIN_IDS)
                self.log(f"已安装 {len(plugins_installed)} 个免费插件")
            else:
                self.log(f"插件安装结果: {install_result}")
        except Exception as exc:
            self.log(f"插件安装失败（非阻塞）: {exc}")

        # 优先通过 DSL 模板导入创建应用
        app_info: dict = {}
        app_id = ""
        dsl_imported = False
        try:
            dsl_content = self._load_dsl_template()
            if dsl_content:
                import_result = self.client.import_dsl(dsl_content, name="OpenAI Bridge")
                app_id = import_result.get("app_id", "")
                if app_id:
                    dsl_imported = True
                    self.log(f"DSL 模板导入成功: {app_id[:8]}...")
                    try:
                        app_info = self.client._get(f"/apps/{app_id}", auth=True)
                    except Exception:
                        pass
        except Exception as exc:
            self.log(f"DSL 导入失败: {exc}，回退到手动创建...")

        # 回退：手动创建应用
        if not app_id:
            try:
                app_info = self.client.create_app(name="auto-chat", mode="chat")
                app_id = app_info.get("id", "")
                if not app_id:
                    self.log("创建应用未返回 app_id，尝试获取已有应用...")
                    apps = self.client.list_apps()
                    app_list = apps.get("data", [])
                    if app_list:
                        app_id = app_list[0].get("id", "")
                        app_info = app_list[0]
            except Exception as exc:
                self.log(f"创建应用失败: {exc}，尝试获取已有应用...")
                try:
                    apps = self.client.list_apps()
                    app_list = apps.get("data", [])
                    if app_list:
                        app_id = app_list[0].get("id", "")
                        app_info = app_list[0]
                except Exception:
                    pass

        # 创建 API Key
        api_key = ""
        api_key_info: dict = {}
        if app_id:
            try:
                api_key_info = self.client.create_api_key(app_id)
                api_key = api_key_info.get("token", "")
            except Exception as exc:
                self.log(f"创建 API Key 失败（非阻塞）: {exc}")
                try:
                    keys = self.client.list_api_keys(app_id)
                    key_list = keys.get("data", [])
                    if key_list:
                        api_key = key_list[0].get("token", "")
                        api_key_info = key_list[0]
                except Exception:
                    pass

        cookies = self.client.cookies
        return {
            "email": email,
            "password": password,
            "user_id": account_info.get("id", ""),
            "user_name": account_info.get("name", ""),
            "api_key": api_key,
            "api_key_info": api_key_info,
            "app_id": app_id,
            "app_info": app_info,
            "account_info": account_info,
            "cookies": cookies,
            "session_cookie": "; ".join(f"{k}={v}" for k, v in cookies.items() if v),
            "access_token": self.client.access_token,
            "dsl_imported": dsl_imported,
            "plugins_installed": plugins_installed,
        }

    TEMPLATE_GITHUB_URL = (
        "https://raw.githubusercontent.com/patchescamerababy/"
        "any-auto-register/main/platforms/dify/template.yml"
    )

    @classmethod
    def _load_dsl_template(cls) -> str:
        import os
        import requests as _req

        try:
            resp = _req.get(cls.TEMPLATE_GITHUB_URL, timeout=15)
            if resp.status_code == 200 and resp.text.strip():
                return resp.text
        except Exception:
            pass
        template_path = os.path.join(os.path.dirname(__file__), "template.yml")
        if not os.path.isfile(template_path):
            return ""
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
