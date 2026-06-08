"""Anuma 协议邮箱注册流程。"""

from __future__ import annotations

import uuid
from typing import Callable

from platforms.anuma.core import AnumaClient, _decode_jwt_payload


class AnumaProtocolMailboxWorker:
    def __init__(
        self,
        client: AnumaClient | None = None,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        captcha_solver = None,
    ) -> None:
        self.client = client or AnumaClient(
            proxy=proxy, log_fn=log_fn, captcha_solver=captcha_solver,
        )
        self.log = log_fn

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Callable[[], str] | None = None,
    ) -> dict:
        ca_id = str(uuid.uuid4())

        # 1. 发码
        self.client.send_privy_code(email, ca_id)

        # 2. 收码
        otp = str(otp_callback() if otp_callback else "").strip()
        if not otp:
            raise RuntimeError("未获取到 Anuma Privy OTP")

        # 3. 验码 → 拿到 PAT
        auth = self.client.authenticate_privy(email, otp, ca_id)
        pat = str(auth.get("privy_access_token") or auth.get("token") or "").strip()
        if not pat:
            raise RuntimeError("Privy authenticate 未返回 access token")

        privy_token = str(auth.get("token") or "").strip()
        identity_token = str(auth.get("identity_token") or "").strip()
        refresh_token = str(auth.get("refresh_token") or "").strip()

        # 4. 接受条款
        self.client.accept_terms(pat, ca_id)

        # 5. 创建会话
        self.client.create_session(pat, ca_id, refresh_token)

        # 6. 创建嵌入式钱包 → 100 lifetime credits 自动分配
        wallet = self.client.create_wallet(pat, ca_id)
        wallet_address = str(wallet.get("address") or "").strip()
        if not wallet_address:
            raise RuntimeError("已创建钱包但缺少地址")

        token_payload = _decode_jwt_payload(privy_token)
        id_token_payload = _decode_jwt_payload(identity_token) if identity_token else {}
        user_id = (
            str(auth.get("user_id") or "")
            or str((auth.get("user") or {}).get("id") or "")
            or str(token_payload.get("sub", "") or "")
            or str(id_token_payload.get("sub", "") or "")
        )

        account_overview = {
            "title": "Anuma (protocol)",
            "url": "https://chat.anuma.ai/zh-CN",
            "privy_did": user_id,
            "wallet_address": wallet_address,
            "auth_method": "email_otp_protocol",
        }

        return {
            "email": email,
            "password": password or "",
            "user_id": user_id,
            "url": "https://chat.anuma.ai/zh-CN",
            "title": "Anuma (protocol)",
            "privy_token": privy_token,
            "privy_id_token": identity_token,
            "privy_refresh_token": refresh_token,
            "privy_session": "",
            "privy_caid": ca_id,
            "cookies": {},
            "local_storage": {},
            "session_storage": {},
            "connections": [wallet],
            "linked_accounts": [
                {"type": "wallet", "address": wallet_address, "walletClientType": "privy"},
                {"type": "email", "address": email, "verified_at": True},
            ],
            "wallet_address": wallet_address,
            "account_overview": account_overview,
        }
