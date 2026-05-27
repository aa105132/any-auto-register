"""ChatGPT / Codex CLI 平台插件"""
import random, string
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    supported_executors = ["protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google", "microsoft", "apple"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        try:
            from platforms.chatgpt.payment import check_subscription_status
            class _A: pass
            a = _A()
            extra = account.extra or {}
            a.access_token = extra.get("access_token") or account.token
            a.cookies = extra.get("cookies", "")
            status = check_subscription_status(a, proxy=self.config.proxy if self.config else None)
            return status not in ("expired", "invalid", "banned", None)
        except Exception:
            return False

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        return "".join(random.choices(
            string.ascii_letters + string.digits + "!@#$", k=16))

    def _map_chatgpt_result(self, result: dict, *, password: str = "", user_id: str = "") -> RegistrationResult:
        return RegistrationResult(
            email=result.get("email", ""),
            password=password or result.get("password", ""),
            user_id=user_id or result.get("account_id", ""),
            token=result.get("access_token", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "access_token": result.get("access_token", ""),
                "refresh_token": result.get("refresh_token", ""),
                "id_token": result.get("id_token", ""),
                "session_token": result.get("session_token", ""),
                "workspace_id": result.get("workspace_id", ""),
                "cookies": result.get("cookies", ""),
                "profile": result.get("profile", {}),
            },
        )

    def _run_protocol_oauth(self, ctx) -> dict:
        from platforms.chatgpt.browser_oauth import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=ctx.identity.oauth_provider,
            email_hint=ctx.identity.email,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_chatgpt_result(result),
            browser_worker_builder=lambda ctx, artifacts: __import__("platforms.chatgpt.browser_register", fromlist=["ChatGPTBrowserRegister"]).ChatGPTBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            oauth_runner=self._run_protocol_oauth,
            capability=RegistrationCapability(oauth_headless_requires_browser_reuse=True),
            otp_spec=OtpSpec(wait_message="等待验证码..."),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_protocol_oauth,
            result_mapper=lambda ctx, result: self._map_chatgpt_result(result, user_id=result.get("account_id", "")),
        )

    def build_protocol_mailbox_adapter(self):
        def _build_worker(ctx, artifacts):
            from platforms.chatgpt.protocol_mailbox import ChatGPTProtocolMailboxWorker

            return ChatGPTProtocolMailboxWorker(
                mailbox=self.mailbox,
                mailbox_account=ctx.identity.mailbox_account,
                provider=(self.config.extra or {}).get("mail_provider", "tempmail_lol"),
                proxy_url=ctx.proxy,
                log_fn=ctx.log,
                sub_mail_mode=str(ctx.extra.get("sub_mail_mode", "") or ""),
                sub_mail_length=ctx.extra.get("sub_mail_length", 4),
                otp_timeout=resolve_timeout(ctx.extra, ("registration.otp_timeout", "registration.timeout"), 120),
                otp_resend_interval=resolve_timeout(ctx.extra, ("registration.otp_resend_interval",), 10),
                login_otp_timeout=resolve_timeout(
                    ctx.extra,
                    ("registration.login_otp_timeout", "registration.otp_timeout", "registration.timeout"),
                    120,
                ),
            )

        def _map_result(ctx, result):
            return RegistrationResult(
                email=result.email,
                password=result.password or (ctx.password or ""),
                user_id=result.account_id,
                token=result.access_token,
                status=AccountStatus.REGISTERED,
                extra={
                    "access_token": result.access_token,
                    "refresh_token": result.refresh_token,
                    "id_token": result.id_token,
                    "session_token": result.session_token,
                    "workspace_id": result.workspace_id,
                    "registration_metadata": dict(result.metadata or {}),
                },
            )

        return ProtocolMailboxAdapter(
            result_mapper=_map_result,
            worker_builder=_build_worker,
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
            ),
        )

    def get_platform_actions(self) -> list:
        return [
            {"id": "switch_account", "label": "切换到 Codex 桌面端", "params": []},
            {"id": "get_account_state", "label": "查询账号状态/订阅", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "payment_link", "label": "生成支付链接",
             "params": [
                 {"key": "country", "label": "地区", "type": "select",
                  "options": ["US","SG","TR","HK","JP","GB","AU","CA"]},
                 {"key": "plan", "label": "套餐", "type": "select",
                  "options": ["plus", "team"]},
             ]},
            {"id": "upload_cpa", "label": "上传 CPA",
             "params": [
                 {"key": "api_url", "label": "CPA API URL", "type": "text"},
                 {"key": "api_key", "label": "CPA API Key", "type": "text"},
             ]},
            {"id": "upload_tm", "label": "上传 Team Manager",
             "params": [
                 {"key": "api_url", "label": "TM API URL", "type": "text"},
                 {"key": "api_key", "label": "TM API Key", "type": "text"},
             ]},
        ]

    def get_desktop_state(self) -> dict:
        from platforms.chatgpt.switch import get_codex_desktop_state

        return get_codex_desktop_state()

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A: pass
        a = _A()
        a.email = account.email
        a.access_token = extra.get("access_token") or account.token
        a.refresh_token = extra.get("refresh_token", "")
        a.id_token = extra.get("id_token", "")
        a.session_token = extra.get("session_token", "")
        a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        a.cookies = extra.get("cookies", "")

        if action_id == "switch_account":
            from platforms.chatgpt.switch import (
                close_codex_app,
                extract_session_token,
                fetch_chatgpt_account_state,
                get_codex_desktop_state,
                read_current_codex_account,
                restart_codex_app,
                switch_codex_account,
            )

            session_token = extract_session_token(a.session_token, a.cookies)
            if not session_token:
                return {"ok": False, "error": "账号缺少 session_token 或可用 cookie，无法切换到本地 Codex 桌面端"}

            close_ok, close_msg = close_codex_app()
            switch_ok, switch_data = switch_codex_account(session_token=session_token, cookies=a.cookies)
            if not switch_ok:
                return {"ok": False, "error": switch_data.get("error", "切换失败")}

            remote_state = fetch_chatgpt_account_state(
                access_token=a.access_token,
                session_token=session_token,
                cookies=a.cookies,
                proxy=proxy,
            )
            local_state = read_current_codex_account()
            restart_ok, restart_msg = restart_codex_app()
            message_parts = [switch_data.get("message", "已写入本地 Codex 凭证")]
            if close_msg:
                message_parts.append(close_msg)
            if restart_msg:
                message_parts.append(restart_msg)
            data = {
                "message": "。".join(part for part in message_parts if part),
                "close": {"ok": close_ok, "message": close_msg},
                "restart": {"ok": restart_ok, "message": restart_msg},
                "local_app_account": local_state,
                "desktop_app_state": get_codex_desktop_state(),
                "remote_state": remote_state,
                "switch_details": switch_data,
            }
            if remote_state.get("access_token"):
                data["access_token"] = remote_state["access_token"]
            if remote_state.get("refresh_token"):
                data["refresh_token"] = remote_state["refresh_token"]
            return {"ok": True, "data": data}

        if action_id == "get_account_state":
            from platforms.chatgpt.switch import fetch_chatgpt_account_state, get_codex_desktop_state, read_current_codex_account

            data = fetch_chatgpt_account_state(
                access_token=a.access_token,
                session_token=a.session_token,
                cookies=a.cookies,
                proxy=proxy,
            )
            data["local_app_account"] = read_current_codex_account()
            data["desktop_app_state"] = get_codex_desktop_state()
            return {"ok": True, "data": data}

        if action_id == "refresh_token":
            from platforms.chatgpt.token_refresh import TokenRefreshManager
            manager = TokenRefreshManager(proxy_url=proxy)
            result = manager.refresh_account(a)
            if result.success:
                data = {"access_token": result.access_token, "refresh_token": result.refresh_token}
                try:
                    from platforms.chatgpt.switch import fetch_chatgpt_account_state
                    data["account_state"] = fetch_chatgpt_account_state(
                        access_token=result.access_token,
                        session_token=a.session_token,
                        cookies=a.cookies,
                        proxy=proxy,
                    )
                except Exception:
                    pass
                return {"ok": True, "data": data}
            return {"ok": False, "error": result.error_message}

        elif action_id == "payment_link":
            from platforms.chatgpt.payment import generate_plus_link, generate_team_link, open_url_incognito
            plan = params.get("plan", "plus")
            country = params.get("country", "US")
            
            # 手动拼凑基础 cookie，以防历史老账号没有保存完整的 cookie 字符串
            if not a.cookies and a.session_token:
                a.cookies = f"__Secure-next-auth.session-token={a.session_token}"
                
            if plan == "plus":
                url = generate_plus_link(a, proxy=proxy, country=country)
            else:
                url = generate_team_link(a, proxy=proxy, country=country)
            
            # 使用本地指纹浏览器无痕挂载 Cookie 强制打开支付页面（防止直接在自己浏览器被踢出登录）
            if url and a.cookies:
                open_url_incognito(url, a.cookies)
                
            return {"ok": bool(url), "data": {"url": url, "message": "支付链接已生成，正在启动带凭证的独立无痕浏览器..."}}

        elif action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import upload_to_cpa, generate_token_json
            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(token_data, api_url=params.get("api_url"),
                                    api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        elif action_id == "upload_tm":
            from platforms.chatgpt.cpa_upload import upload_to_team_manager
            ok, msg = upload_to_team_manager(a, api_url=params.get("api_url"),
                                             api_key=params.get("api_key"))
            return {"ok": ok, "data": msg}

        raise NotImplementedError(f"未知操作: {action_id}")
