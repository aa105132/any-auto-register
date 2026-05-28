"""Swarms Marketplace 协议邮箱注册 worker。

注册流程:
  1. Swarms Marketplace /signin/signup Server Action (email + password)
  2. 等待邮箱确认链接 → 解析 token_hash + type=signup
  3. Supabase GoTrue verify → email confirmed
  4. password grant login → access_token + refresh_token
  5. get user info + 补全 username/full_name
  6. 查询 credit 后 tRPC createApiKey → API key (sk-xxxx)
"""

from __future__ import annotations

from typing import Callable

from platforms.swarms.core import SwarmsClient


class SwarmsProtocolMailboxWorker:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.client = SwarmsClient(proxy=proxy, log_fn=log_fn)
        self.log = log_fn

    def run(
        self,
        *,
        email: str,
        password: str,
        otp_callback: Callable[[], str] | None = None,
        verification_link_callback: Callable[[], str] | None = None,
    ) -> dict:
        # 1. Supabase signup
        signup_result: dict = {}
        try:
            signup_result = self.client.signup(email, password)
            self.log("注册请求已提交，等待邮箱确认邮件...")
        except Exception as exc:
            self.log(f"注册请求失败: {exc}")
            raise RuntimeError(f"Swarms 注册请求失败: {exc}") from exc

        # 2. 等待确认链接
        if not verification_link_callback:
            raise RuntimeError("Swarms 注册需要验证链接回调，但未提供 verification_link_callback")
        confirm_url = verification_link_callback()
        if not confirm_url:
            raise RuntimeError("Swarms: 未获取到邮箱确认链接")

        # 3. 解析确认链接中的 token_hash 和 type
        verify_params = self.client.parse_verification_params(confirm_url)
        token_hash = verify_params.get("token_hash", "")
        verify_type = verify_params.get("type", "signup")
        if not token_hash:
            self.log(f"确认链接解析结果: {verify_params}")
            raise RuntimeError("Swarms: 无法从确认链接中解析 token_hash")

        self.log(f"解析到 token_hash={token_hash[:16]}... type={verify_type}")

        # 4. 优先按真实浏览器链路打开确认链接。
        # Swarms 的 $5 初始化和 API Key 创建依赖前端确认回调产生的 sb-db-auth-token；
        # 仅 POST Supabase verify 后 password login，实测会出现额度为 0 / addApiKey 拒绝。
        link_verified = False
        if hasattr(self.client, "verify_email_link"):
            try:
                verify_result = self.client.verify_email_link(confirm_url)
                link_verified = bool(verify_result.get("auth_cookie") or verify_result.get("access_token"))
                if link_verified:
                    self.log("邮箱验证成功，已获取 Swarms 回调登录态")
            except Exception as exc:
                self.log(f"确认链接登录态验证失败（回退 Supabase verify）: {exc}")

        if not link_verified:
            try:
                self.client.verify_email(token_hash, signup_type=verify_type)
                self.log("邮箱验证成功")
            except Exception as exc:
                self.log(f"邮箱验证失败: {exc}")
                raise RuntimeError(f"Swarms 邮箱验证失败: {exc}") from exc

            # 5. 登录获取 token
            try:
                self.client.login(email, password)
            except Exception as exc:
                self.log(f"登录失败: {exc}")
                raise RuntimeError(f"Swarms 登录失败: {exc}") from exc

        return self._post_login(email, password)

    def _post_login(self, email: str, password: str) -> dict:
        # 获取用户信息
        user_info: dict = {}
        try:
            user_info = self.client.get_user()
        except Exception as exc:
            self.log(f"获取用户信息失败（非阻塞）: {exc}")

        # Swarms Marketplace 原站注册会初始化用户资料/额度；协议链路必须补齐资料后再建 key。
        profile: dict = {}
        try:
            self.log("补全 Swarms 用户资料...")
            profile = self.client.ensure_profile(email=email, full_name="Auto Register")
        except Exception as exc:
            self.log(f"补全 Swarms 用户资料失败（非阻塞）: {exc}")

        credit_info: dict = {}
        try:
            if hasattr(self.client, "wait_for_credit"):
                credit_info = self.client.wait_for_credit(min_credit=0.01, timeout=90, interval=3)
            else:
                credit_info = self.client.get_credit()
        except Exception as exc:
            self.log(f"查询账户额度失败（非阻塞）: {exc}")

        # 创建 API Key
        api_key = ""
        api_key_info: dict = {}
        try:
            api_key_info = self.client.create_api_key(name="auto-register")
            api_key = api_key_info.get("key", "") or api_key_info.get("apiKey", "")
        except Exception as exc:
            self.log(f"创建 API Key 失败（尝试获取已有 key）: {exc}")
            try:
                keys = self.client.list_api_keys()
                if keys and len(keys) > 0:
                    first_key = keys[0]
                    api_key = first_key.get("key", "") or first_key.get("apiKey", "")
                    api_key_info = first_key
            except Exception:
                pass

        if not api_key:
            self.log("警告: 未能获取 API Key")

        cookies = self.client.cookies
        session_cookie = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
        username = str(profile.get("username") or "")
        full_name = str(profile.get("full_name") or "")

        return {
            "email": email,
            "password": password,
            "user_id": self.client.user_id or user_info.get("id", ""),
            "user_name": full_name or (user_info.get("user_metadata") or {}).get("name", ""),
            "username": username,
            "profile": profile,
            "credit_info": credit_info,
            "api_key": api_key,
            "api_key_info": api_key_info,
            "access_token": self.client.access_token,
            "refresh_token": self.client.refresh_token,
            "user_info": user_info,
            "cookies": cookies,
            "session_cookie": session_cookie,
            "signup_result": signup_result if "signup_result" in dir() else {},
        }
