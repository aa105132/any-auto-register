"""Zo Computer 系统邮箱注册 worker。"""
from __future__ import annotations

from typing import Callable

from platforms.zo.core import DEFAULT_COUPON_CODE, ZoClient, resolve_card_info


class ZoProtocolMailboxWorker:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        extra: dict | None = None,
    ) -> None:
        self.client = ZoClient(proxy=proxy, log_fn=log_fn)
        self.log = log_fn
        self.extra = dict(extra or {})

    def run(
        self,
        *,
        email: str,
        password: str = "",
        verification_link_callback: Callable[[], str] | None = None,
    ) -> dict:
        # 1. OpenAuth email provider：优先纯 HTTP 发送系统邮箱验证链接。
        signin_result = self.client.start_email_authorize(email=email)
        self.log("Zo 邮箱验证链接已发送，等待系统邮箱收取...")

        # 2. 访问验证链接，完成 session/token 写入。
        if not verification_link_callback:
            raise RuntimeError("Zo 邮箱注册需要验证链接回调，但未提供 verification_link_callback")
        verification_link = verification_link_callback()
        if not verification_link:
            raise RuntimeError("Zo: 未获取到验证链接")
        visit_result = self.client.visit_verification_link(verification_link)

        # 3. 先创建/选择 workspace；后续账单、额度、token API 都依赖 workspace origin。
        coupon_code = str(self.extra.get("zo_coupon_code") or DEFAULT_COUPON_CODE).strip() or DEFAULT_COUPON_CODE
        workspace_result = self.client.ensure_workspace(
            handle=str(self.extra.get("zo_workspace_handle") or ""),
            promo_code=coupon_code,
            signup_code=str(self.extra.get("zo_signup_code") or ""),
        )

        # 4. 注册后跳过 onboarding 与手机号绑定。
        onboarding_result = self.client.skip_onboarding()
        phone_result = self.client.skip_phone()

        # 5. 兑换优惠券 SHEK100，目标 $100 额度。
        coupon_result = self.client.redeem_coupon(code=coupon_code)
        credit_result = self.client.check_credits(min_amount=float(self.extra.get("zo_min_credit", 100.0) or 100.0))

        # 5. 第三步必做：绑定测试卡。默认使用用户指定靶场测试卡，可被 extra.zo_card 覆盖。
        card = resolve_card_info(self.extra)
        card_binding_result = self.client.bind_card(card=card, require_confirmed=True)

        pool_card_id = str(card.get("_pool_id") or "").strip()
        if pool_card_id and card_binding_result.get("ok"):
            try:
                from core.credit_card_pool import CreditCardPool
                CreditCardPool(str(card.get("_pool_path") or "")).mark_used(pool_card_id, platform="zo", account_email=email)
            except Exception as exc:
                self.log(f"Zo 信用卡池使用记录回写失败: {exc!r}")

        # 6. 创建 Access Token，作为 API key 保存。        token_name = str(self.extra.get("zo_access_token_name") or "auto-register").strip() or "auto-register"
        key_create_result = self.client.create_access_token(name=token_name)
        api_key = str(key_create_result.get("api_key") or "").strip()
        api_verification = self.client.verify_api_key(api_key)
        settings = self.client.get_settings()

        return {
            "email": email,
            "password": password,
            "api_key": api_key,
            "api_key_info": key_create_result.get("api_key_info") or {},
            "api_verification": api_verification,
            "key_create_result": key_create_result,
            "signin_result": signin_result,
            "visit_result": visit_result,
            "workspace_result": workspace_result,
            "onboarding_result": onboarding_result,
            "phone_result": phone_result,
            "coupon_result": coupon_result,
            "credit_result": credit_result,
            "card_binding_result": card_binding_result,
            "settings": settings.get("data") or {},
            "account_info": settings.get("data") or {},
            "cookies": self.client.cookies,
            "cookie_header": self.client.cookie_header,
        }
