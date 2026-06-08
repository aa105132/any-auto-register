"""AI-ROUTER 协议优先邮箱注册流程。"""
from __future__ import annotations

import time
from typing import Any

from platforms.airouter.browser_turnstile import AiRouterTurnstileHarvester
from platforms.airouter.core import API_BASE, DASHBOARD_URL, REGISTER_URL, SITE_URL, AiRouterClient


class AiRouterMailboxRegistrar:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        otp_callback=None,
        timeout: int = 180,
        chrome_path: str = "",
        cdp_url: str = "",
        log_fn=print,
        promo_code: str = "",
        invitation_code: str = "",
        aff_code: str = "",
        api_key_name: str = "auto-register",
        group_id: int | str | None = None,
        min_success_balance: float = 20.0,
        webrtc_client_ip: str = "",
    ) -> None:
        self.proxy = proxy
        self.otp_callback = otp_callback
        self.timeout = timeout
        self.chrome_path = chrome_path
        self.cdp_url = cdp_url
        self.log = log_fn or (lambda _msg: None)
        self.promo_code = promo_code
        self.invitation_code = invitation_code
        self.aff_code = aff_code
        self.api_key_name = api_key_name
        self.group_id = group_id
        self.min_success_balance = float(min_success_balance or 20.0)
        self.webrtc_client_ip = webrtc_client_ip
        self.client = AiRouterClient(proxy=proxy, log_fn=log_fn)

    def _l(self, msg: str) -> None:
        self.log(f"[AI-ROUTER] {msg}")

    def _harvest_turnstile(self, *, email: str, password: str) -> str:
        harvester = AiRouterTurnstileHarvester(
            proxy=self.proxy,
            timeout=self.timeout,
            chrome_path=self.chrome_path,
            cdp_url=self.cdp_url,
            log_fn=self.log,
        )
        return harvester.harvest(email=email, password=password)

    def run(self, *, email: str, password: str) -> dict[str, Any]:
        settings = self.client.public_settings()
        email_verify_enabled = bool(settings.get("email_verify_enabled"))
        turnstile_enabled = bool(settings.get("turnstile_enabled"))
        self._l(
            f"settings: email_verify={email_verify_enabled} turnstile={turnstile_enabled} "
            f"site_key={settings.get('turnstile_site_key') or '-'}"
        )

        turnstile_token = ""
        if turnstile_enabled:
            self._l("通过 CDP 采集 Turnstile token")
            turnstile_token = self._harvest_turnstile(email=email, password=password)
            if not turnstile_token:
                raise RuntimeError("AI-ROUTER 注册需要 Turnstile token，但采集失败")

        verify_code = ""
        send_code_result: dict[str, Any] = {}
        if email_verify_enabled:
            if not self.otp_callback:
                raise RuntimeError("AI-ROUTER 邮箱验证开启，但未提供 otp_callback")
            self._l("协议发送邮箱验证码")
            send_code_result = self.client.send_verify_code(
                email=email,
                turnstile_token=turnstile_token,
                webrtc_client_ip=self.webrtc_client_ip,
            )
            self._l("等待 AI-ROUTER 邮箱验证码")
            verify_code = str(self.otp_callback() or "").strip()
            if not verify_code:
                raise RuntimeError("AI-ROUTER 邮箱验证码为空")

        self._l("协议提交注册")
        registration = self.client.register(
            email=email,
            password=password,
            verify_code=verify_code,
            turnstile_token=turnstile_token,
            promo_code=self.promo_code,
            invitation_code=self.invitation_code,
            aff_code=self.aff_code,
            webrtc_client_ip=self.webrtc_client_ip,
        )
        access_token = str(registration.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError(f"AI-ROUTER 注册响应缺少 access_token: {registration}")

        key_name = self.api_key_name or f"auto-register-{int(time.time())}"
        group_id, group_info = self.client.resolve_api_key_group_id(access_token, self.group_id)
        if group_id is not None:
            self._l(f"选择 API Key 分组: id={group_id} name={group_info.get('name') or group_info.get('source') or '-'}")
        else:
            self._l("未找到可用 API Key 分组，将按平台默认方式创建")
        self._l(f"协议创建 API Key: {key_name}")
        key_result = self.client.create_api_key(access_token, name=key_name, group_id=group_id)
        api_key = str(key_result.get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError(f"AI-ROUTER 创建 API Key 未返回完整 key: {key_result}")
        api_verification = self.client.verify_api_key(api_key)
        if not api_verification.get("ok"):
            raise RuntimeError(f"AI-ROUTER API Key 不可用，models 验证失败: {api_verification}")

        me = self.client.get_me(access_token)
        profile_data = me.get("data") if isinstance(me.get("data"), dict) else {}
        balance = profile_data.get("balance")
        if balance is None and isinstance(registration.get("user"), dict):
            balance = registration["user"].get("balance")
        try:
            numeric_balance = float(balance)
        except Exception:
            numeric_balance = -1.0
        if numeric_balance < self.min_success_balance:
            raise RuntimeError(
                f"AI-ROUTER 注册余额不足，判定失败: balance={balance}, "
                f"required>={self.min_success_balance}. 可能同 IP 注册超过限制，请更换 IP 后重试"
            )

        return {
            "email": email,
            "password": password,
            "api_key": api_key,
            "access_token": access_token,
            "refresh_token": str(registration.get("refresh_token") or ""),
            "expires_in": registration.get("expires_in", 0),
            "token_type": str(registration.get("token_type") or ""),
            "user": registration.get("user") if isinstance(registration.get("user"), dict) else {},
            "me": me,
            "balance": numeric_balance,
            "min_success_balance": self.min_success_balance,
            "send_code_result": send_code_result,
            "key_create_result": key_result,
            "group_id": group_id,
            "group_info": group_info,
            "api_key_info": key_result.get("data") if isinstance(key_result.get("data"), dict) else {},
            "api_verification": api_verification,
            "site_url": SITE_URL,
            "register_url": REGISTER_URL,
            "dashboard_url": DASHBOARD_URL,
            "api_base": API_BASE,
            "native_api_base": API_BASE,
        }
