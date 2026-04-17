from __future__ import annotations

import uuid
from typing import Callable

from platforms.atxp.core import AtxpClient


class AtxpProtocolMailboxWorker:
    def __init__(
        self,
        client: AtxpClient | None = None,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.client = client or AtxpClient(proxy=proxy, log_fn=log_fn)
        self.log = log_fn

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Callable[[], str] | None = None,
    ) -> dict:
        ca_id = str(uuid.uuid4())
        self.client.send_privy_code(email, ca_id)
        otp = str(otp_callback() if otp_callback else "").strip()
        if not otp:
            raise RuntimeError("未获取到 Privy OTP")

        auth = self.client.authenticate_privy(email, otp, ca_id)
        privy_token = str(auth.get("token") or "").strip()
        if not privy_token:
            raise RuntimeError("Privy 登录成功但未返回 token")

        bundle = self.client.fetch_atxp_bundle(privy_token)
        account_id = str(bundle.get("account_id") or "").strip()
        connection_token = str(bundle.get("connection_token") or "").strip()
        if not account_id or not connection_token:
            raise RuntimeError("ATXP bundle 缺少 account_id 或 connection_token")

        connection_string = (
            f"https://accounts.atxp.ai?connection_token={connection_token}"
            f"&account_id={account_id}"
        )
        gateway_health = self.client.probe_gateway_connection(connection_string)

        result = {
            "email": email,
            "password": password,
            "privy_token": privy_token,
            "refresh_token": str(auth.get("refresh_token") or "").strip(),
            "account_id": account_id,
            "connection_token": connection_token,
            "connection_string": connection_string,
            "wallet_address": str(bundle.get("wallet_address") or "").strip(),
            "me": bundle.get("me") or {},
            "wallet_info": bundle.get("wallet_info") or {},
            "gateway_health": gateway_health or {},
            "clowdbot_status": "pending",
            "create_clowdbot_completed": False,
            "claim_email_completed": False,
            "reward_progress": None,
            "task_error": "",
            "clowdbot_result": {},
            "clowdbot_instance_id": "",
            "claimed_agent_email": "",
        }

        try:
            clowdbot_runner = getattr(self.client, "complete_clowdbot_tasks")
            if not callable(clowdbot_runner):
                raise AttributeError("AtxpClient.complete_clowdbot_tasks 不可调用")
            task_result = clowdbot_runner(privy_token, account_id, email) or {}
            result.update(
                {
                    "clowdbot_status": "completed",
                    "clowdbot_instance_id": str(task_result.get("instance_id") or "").strip(),
                    "claimed_agent_email": str(task_result.get("claimed_agent_email") or "").strip(),
                    "create_clowdbot_completed": bool(task_result.get("create_clowdbot_completed")),
                    "claim_email_completed": bool(task_result.get("claim_email_completed")),
                    "reward_progress": task_result.get("reward_progress"),
                    "task_error": "",
                    "clowdbot_result": task_result,
                }
            )
        except Exception as exc:
            result["clowdbot_status"] = "failed"
            result["task_error"] = str(exc)

        return result
