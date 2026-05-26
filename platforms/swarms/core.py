"""Swarms Marketplace 协议客户端。

Supabase GoTrue REST API 注册链路：
  1. POST /auth/v1/signup  → 发送确认邮件（含确认链接）
  2. POST /auth/v1/verify  → 验证邮箱 (token_hash + type=signup)
  3. POST /auth/v1/token?grant_type=password → 获取 access_token + refresh_token
  4. GET  /auth/v1/user    → 获取用户信息
  5. POST /api/trpc/panel.createApiKey → 创建 API Key

API Key 用于 swarms.world 所有 API 调用，格式: sk-xxxx
"""

from __future__ import annotations

import time
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, parse_qs

import requests


SWARMS_BASE = "https://swarms.world"
SUPABASE_BASE = "https://db.swarms.world"
SUPABASE_AUTH = f"{SUPABASE_BASE}/auth/v1"

SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVwdnhtanF4dGxxaHhycGNybGRxIiwicm9sZSI6ImFub24iLCJpYXQiOjE3MDk5MTcyMDYsImV4cCI6MjAyNTQ5MzIwNn0."
    "0lIW-aSeMGtbh3YMvp7Ds17nzFYyx-cODSXUb15DLvU"
)

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class SwarmsClient:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
    ) -> None:
        self.log = log_fn
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": CHROME_UA,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "apikey": SUPABASE_ANON_KEY,
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._user_id: str = ""

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    def _post(self, url: str, payload: dict | None = None, *, auth: bool = False) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers.update(self._auth_headers())
        resp = self.session.post(
            url,
            json=payload or {},
            headers=headers,
            timeout=30,
        )
        if resp.status_code >= 400:
            self.log(f"Swarms API error {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    def _get(self, url: str, *, auth: bool = True) -> dict:
        headers = {}
        if auth:
            headers.update(self._auth_headers())
        resp = self.session.get(url, headers=headers, timeout=30)
        if resp.status_code >= 400:
            self.log(f"Swarms API error {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # --- Supabase GoTrue Auth ---

    def signup(self, email: str, password: str) -> dict:
        self.log(f"注册 Supabase 账号: {email}")
        return self._post(f"{SUPABASE_AUTH}/signup", {
            "email": email,
            "password": password,
        })

    def verify_email(self, token_hash: str, signup_type: str = "signup") -> dict:
        self.log("验证邮箱...")
        return self._post(f"{SUPABASE_AUTH}/verify", {
            "token_hash": token_hash,
            "type": signup_type,
        })

    def login(self, email: str, password: str) -> dict:
        self.log("密码登录...")
        result = self._post(
            f"{SUPABASE_AUTH}/token?grant_type=password",
            {"email": email, "password": password},
        )
        self._access_token = result.get("access_token", "")
        self._refresh_token = result.get("refresh_token", "")
        if self._access_token:
            self.log("登录成功，已获取 access_token")
        return result

    def get_user(self) -> dict:
        self.log("获取用户信息...")
        result = self._get(f"{SUPABASE_AUTH}/user", auth=True)
        self._user_id = result.get("id", "")
        return result

    # --- tRPC API ---

    def _trpc(self, path: str, payload: dict | None = None) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Origin": SWARMS_BASE,
            "Referer": f"{SWARMS_BASE}/",
        }
        headers.update(self._auth_headers())
        resp = self.session.post(
            f"{SWARMS_BASE}/api/trpc/{path}",
            json=payload or {},
            headers=headers,
            timeout=30,
        )
        if resp.status_code >= 400:
            self.log(f"tRPC error {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        try:
            data = resp.json()
            if isinstance(data, list) and len(data) > 0:
                item = data[0]
                if isinstance(item, dict) and "result" in item:
                    return item["result"].get("data", item["result"])
            return data
        except Exception:
            return {"raw": resp.text}

    def create_api_key(self, name: str = "auto-register") -> dict:
        self.log(f"创建 API Key [{name}]...")
        result = self._trpc("panel.createApiKey", {
            "name": name,
        })
        return result

    def list_api_keys(self) -> list[dict]:
        self.log("获取 API Key 列表...")
        result = self._trpc("panel.getApiKeys")
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            keys = result.get("apiKeys") or result.get("data") or []
            return keys if isinstance(keys, list) else [keys]
        return []

    def get_credit(self) -> dict:
        self.log("查询账户额度...")
        return self._trpc("panel.getUserCredit")

    # --- 工具方法 ---

    @staticmethod
    def parse_verification_params(url: str) -> dict[str, str]:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        return {
            "token_hash": (params.get("token_hash", [""])[0]
                           or params.get("token", [""])[0]),
            "type": params.get("type", ["signup"])[0],
        }

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def cookies(self) -> dict[str, str]:
        return {c.name: c.value for c in self.session.cookies}
