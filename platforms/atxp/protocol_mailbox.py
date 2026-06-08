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
        enable_clowdbot: bool = False,
    ) -> dict:
        ca_id = str(uuid.uuid4())
        self.client.send_privy_code(email, ca_id)
        otp = str(otp_callback() if otp_callback else "").strip()
        if not otp:
            raise RuntimeError("未获取到 Privy OTP")

        auth = self.client.authenticate_privy(email, otp, ca_id)
        privy_token = str(auth.get("token") or "").strip()
        refresh_token = str(auth.get("refresh_token") or "").strip()
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
        gateway_health: dict = {}
        gateway_error = ""
        gateway_probe = getattr(self.client, "probe_gateway_connection", None)
        if callable(gateway_probe):
            try:
                gateway_health = gateway_probe(connection_string)
            except Exception as exc:
                gateway_error = str(exc)
                gateway_health = {"success": False, "error": gateway_error}
                if self._is_gateway_registration_blocking_error(gateway_error):
                    raise RuntimeError(gateway_error) from exc
                self.log(f"ATXP gateway probe skipped: {gateway_error}")

        balance: object = {}
        balance_error = ""
        balance_warning = ""
        balance_data = self.client.check_balance(privy_token, refresh_token=refresh_token)
        restriction = balance_data.get("restriction") or {}
        if restriction:
            reason = str(restriction.get("error") or restriction.get("reason") or restriction)
            raise RuntimeError(f"ATXP balance restricted: {reason}; {restriction}")
        if balance_data.get("balance_unavailable"):
            balance_warning = "ATXP /balance 不可用，已跳过余额数值校验"
        else:
            balance = balance_data.get("balance") or {}
            usdc = float(balance.get("usdc", 0) or 0) if isinstance(balance, dict) else 0.0
            iou = float(balance.get("iou", 0) or 0) if isinstance(balance, dict) else float(balance or 0)
            total = usdc + iou
            if total < 3.0:
                raise RuntimeError(f"ATXP 余额不足 ${total:.2f}，注册失败（需要 >= $3）")

        result = {
            "email": email,
            "password": password,
            "privy_token": privy_token,
            "refresh_token": refresh_token,
            "account_id": account_id,
            "connection_token": connection_token,
            "connection_string": connection_string,
            "wallet_address": str(bundle.get("wallet_address") or "").strip(),
            "me": bundle.get("me") or {},
            "wallet_info": bundle.get("wallet_info") or {},
            "gateway_health": gateway_health,
            "gateway_error": gateway_error,
            "balance": balance,
            "balance_error": balance_error,
            "balance_warning": balance_warning,
            "clowdbot_status": "skipped",
            "create_clowdbot_completed": False,
            "claim_email_completed": False,
            "reward_progress": None,
            "task_error": "",
            "clowdbot_result": {},
            "clowdbot_instance_id": "",
            "claimed_agent_email": "",
        }

        result["key_sync"] = self._sync_keys(connection_string)

        if enable_clowdbot:
            try:
                self.log("Clowdbot 任务开始...")
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

    @staticmethod
    def _is_gateway_registration_blocking_error(error: str) -> bool:
        lowered = str(error or "").lower()
        if "gateway_402" not in lowered and "payment_required" not in lowered:
            return False
        blocking_markers = (
            "account_restricted",
            "fraud_blocked",
            "insufficient_balance",
            "currently restricted",
            "effective balance is $0.00",
            "please add funds",
        )
        return any(marker in lowered for marker in blocking_markers)

    def _sync_keys(self, connection_string: str) -> dict:
        """POST connection_string 到 atxp_key_sync_url。失败非致命。"""
        from core.config_store import config_store

        sync_url = str(config_store.get("atxp_key_sync_url") or "").strip()
        if not sync_url:
            return {"synced": False, "reason": "atxp_key_sync_url 未配置"}

        import requests

        try:
            self.log(f"同步 key 到远端: {sync_url}")
            resp = requests.post(
                sync_url,
                json=[connection_string],
                headers={"content-type": "application/json"},
                timeout=15,
            )
            resp.raise_for_status()
            return {"synced": True, "url": sync_url, "status": resp.status_code}
        except Exception as exc:
            self.log(f"key 同步失败: {exc}")
            return {"synced": False, "url": sync_url, "error": str(exc)}
