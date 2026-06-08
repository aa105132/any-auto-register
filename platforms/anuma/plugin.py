"""Anuma 平台插件。"""

from __future__ import annotations

import base64
import json
from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, RegistrationResult
from core.registry import register


def _decode_storage_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    raw = value.strip()
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        return {}
    try:
        payload = raw.split(".", 2)[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(decoded.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_linked_accounts(value: Any) -> list[dict[str, Any]]:
    decoded = _decode_storage_value(value)
    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, dict)]
    return []


def _extract_wallet_address(*groups: Any) -> str:
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            if str(item.get("type", "")).lower() != "wallet":
                continue
            address = str(item.get("address", "") or "").strip()
            if address:
                return address
    return ""


def _join_cookie_header(cookies: dict[str, str]) -> str:
    parts = []
    for key, value in cookies.items():
        if key and value not in (None, ""):
            parts.append(f"{key}={value}")
    return "; ".join(parts)


@register
class AnumaPlatform(BasePlatform):
    name = "anuma"
    display_name = "Anuma"
    version = "1.0.0"
    supported_executors = ["headless", "headed"]
    supported_identity_modes = ["mailbox"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        # Anuma 当前邮箱注册流不要求设置密码，保留空串即可。
        return password or ""

    def _map_anuma_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        cookies = {
            str(item.get("name", "") or ""): str(item.get("value", "") or "")
            for item in list(result.get("cookies") or [])
            if isinstance(item, dict) and item.get("name")
        }
        local_storage = dict(result.get("local_storage") or {})
        session_storage = dict(result.get("session_storage") or {})

        privy_token = (
            str(result.get("privy_token", "") or "")
            or str(cookies.get("privy-token", "") or "")
            or str(_decode_storage_value(local_storage.get("privy:token")) or "")
        )
        privy_id_token = (
            str(result.get("privy_id_token", "") or "")
            or str(cookies.get("privy-id-token", "") or "")
            or str(_decode_storage_value(local_storage.get("privy:id_token")) or "")
        )
        privy_refresh_token = (
            str(result.get("privy_refresh_token", "") or "")
            or str(_decode_storage_value(local_storage.get("privy:refresh_token")) or "")
        )
        privy_session = (
            str(result.get("privy_session", "") or "")
            or str(cookies.get("privy-session", "") or "")
        )
        privy_caid = (
            str(result.get("privy_caid", "") or "")
            or str(_decode_storage_value(local_storage.get("privy:caid")) or "")
        )

        token_payload = _decode_jwt_payload(privy_token)
        id_token_payload = _decode_jwt_payload(privy_id_token)
        connections = _decode_storage_value(local_storage.get("privy:connections"))
        if not isinstance(connections, list):
            connections = []
        linked_accounts = _normalize_linked_accounts(id_token_payload.get("linked_accounts"))

        user_id = (
            str(result.get("user_id", "") or "")
            or str(token_payload.get("sub", "") or "")
            or str(id_token_payload.get("sub", "") or "")
        )
        wallet_address = _extract_wallet_address(connections, linked_accounts)

        account_overview = {
            "title": str(result.get("title", "") or ""),
            "url": str(result.get("url", "") or ""),
            "privy_did": user_id,
            "wallet_address": wallet_address,
            "auth_method": "email_otp",
        }

        return RegistrationResult(
            email=str(result.get("email", "") or ""),
            password=password or str(result.get("password", "") or ""),
            user_id=user_id,
            token=privy_token,
            status=AccountStatus.REGISTERED,
            extra={
                "privy_token": privy_token,
                "privy_id_token": privy_id_token,
                "privy_refresh_token": privy_refresh_token,
                "privy_session": privy_session,
                "privy_caid": privy_caid,
                "privy_token_payload": token_payload,
                "privy_id_token_payload": id_token_payload,
                "linked_accounts": linked_accounts,
                "connections": connections,
                "wallet_address": wallet_address,
                "cookies": _join_cookie_header(cookies),
                "cookie_map": cookies,
                "local_storage": local_storage,
                "session_storage": session_storage,
                "account_overview": account_overview,
            },
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_anuma_result(result, password=ctx.password or ""),
            browser_worker_builder=lambda ctx, artifacts: __import__(
                "platforms.anuma.browser_register",
                fromlist=["AnumaBrowserRegister"],
            ).AnumaBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            otp_spec=OtpSpec(
                keyword="Anuma",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="等待 Anuma 验证码...",
                success_label="Anuma 验证码",
            ),
        )

    def build_protocol_mailbox_adapter(self):
        is_cdp = self.config.executor_type == "cdp_protocol"
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_anuma_result(result, password=ctx.password or ""),
            worker_builder=lambda ctx, artifacts: __import__(
                "platforms.anuma.protocol_mailbox",
                fromlist=["AnumaProtocolMailboxWorker"],
            ).AnumaProtocolMailboxWorker(
                proxy=ctx.proxy,
                log_fn=ctx.log,
                captcha_solver=artifacts.captcha_solver,
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
                otp_callback=artifacts.otp_callback,
            ),
            use_captcha=not is_cdp,
            otp_spec=OtpSpec(
                keyword="Anuma",
                code_pattern=r"(?<!\d)(\d{6})(?!\d)",
                wait_message="等待 Anuma 验证码...",
                success_label="Anuma 验证码",
            ),
        )

    def check_valid(self, account: Account) -> bool:
        extra = dict(account.extra or {})
        return bool(
            account.token
            or extra.get("privy_token")
            or extra.get("privy_refresh_token")
            or extra.get("cookies")
        )
