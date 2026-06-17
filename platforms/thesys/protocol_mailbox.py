"""Thesys 纯协议邮箱 OTP 注册 worker。"""
from __future__ import annotations

import time
from typing import Callable, Any

from platforms.thesys.core import DEFAULT_FREE_MODEL, ThesysClient, extract_api_key


class ThesysProtocolMailboxWorker:
    def __init__(self, *, proxy: str | None = None, log_fn: Callable[[str], None] = print) -> None:
        self.client = ThesysClient(proxy=proxy, log_fn=log_fn)
        self.log = log_fn or (lambda _msg: None)

    def _l(self, message: str) -> None:
        self.log(f"[Thesys] {message}")

    def run(
        self,
        *,
        email: str,
        password: str = "",
        otp_callback: Callable[[], str] | None = None,
        key_name: str = "auto-register",
        verify_chat: bool = True,
        verify_model: str = DEFAULT_FREE_MODEL,
    ) -> dict[str, Any]:
        if not email:
            raise RuntimeError("Thesys 注册缺少邮箱")
        if not otp_callback:
            raise RuntimeError("Thesys 注册需要邮箱 OTP 回调")

        self._l("发送邮箱 OTP")
        otp_send_result = self.client.generate_email_otp(email)
        pre_auth_session_id = self.client.extract_pre_auth_session_id(otp_send_result)
        if not pre_auth_session_id:
            raise RuntimeError(f"Thesys 发码响应缺少 preAuthSessionId: {otp_send_result}")

        self._l("等待邮箱 OTP")
        code = str(otp_callback() or "").strip()
        if not code:
            raise RuntimeError("Thesys 邮箱 OTP 为空")

        self._l("提交 OTP 登录/注册")
        auth_result = self.client.verify_otp(pre_auth_session_id=pre_auth_session_id, code=code)
        cookies = dict(auth_result.get("cookies") or {})
        if not (cookies.get("sAccessToken") or cookies.get("sRefreshToken")):
            self._l("OTP 已验证，但未从 Set-Cookie 中看到 sAccessToken/sRefreshToken，继续尝试控制台 API")

        self._l("读取用户和组织信息")
        user = self.client.user_me()
        orgs = self.client.list_orgs()
        if not orgs:
            raise RuntimeError("Thesys 未获取到组织，无法创建 API Key")
        org = orgs[0]
        org_id = str(org.get("id") or org.get("orgId") or "").strip()
        if not org_id:
            raise RuntimeError(f"Thesys 组织响应缺少 id: {org}")

        resolved_key_name = key_name or f"auto-register-{int(time.time())}"
        self._l(f"创建 API Key: {resolved_key_name}")
        key_create_result = self.client.create_api_key(org_id=org_id, name=resolved_key_name)
        api_key = extract_api_key(key_create_result)
        if not api_key:
            raise RuntimeError(f"Thesys 创建 API Key 未返回明文 key: {key_create_result}")

        api_key_list: dict[str, Any] = {}
        billing: dict[str, Any] = {}
        try:
            api_key_list = self.client.list_api_keys(org_id=org_id)
        except Exception as exc:
            self._l(f"读取 API Key 列表失败（非阻塞）: {exc}")
        try:
            billing = self.client.get_billing(org_id=org_id)
        except Exception as exc:
            self._l(f"读取账单信息失败（非阻塞）: {exc}")

        self._l("验证 OpenAI 兼容 models 端点")
        api_verification = self.client.verify_models(api_key)
        if not api_verification.get("ok"):
            raise RuntimeError(f"Thesys API Key models 验证失败: {api_verification}")

        chat_verification: dict[str, Any] = {}
        if verify_chat:
            self._l(f"验证 free chat/completions: {verify_model}")
            chat_verification = self.client.probe_chat_completion(api_key, model=verify_model or DEFAULT_FREE_MODEL)
            if not chat_verification.get("ok"):
                raise RuntimeError(f"Thesys free 模型调用验证失败: {chat_verification}")

        return {
            "email": email,
            "password": password,
            "user_id": str(user.get("id") or user.get("userId") or user.get("uid") or ""),
            "user": user,
            "org": org,
            "orgs": orgs,
            "org_id": org_id,
            "api_key": api_key,
            "api_key_name": resolved_key_name,
            "api_key_info": key_create_result,
            "api_key_list": api_key_list,
            "billing": billing,
            "auth_result": auth_result,
            "auth_cookies": cookies,
            "otp_send_result": otp_send_result,
            "api_verification": api_verification,
            "chat_verification": chat_verification,
            "verified_model": verify_model,
        }
