"""Swarms Marketplace 协议客户端。

Swarms Marketplace 注册链路：
  1. POST /signin/signup Next Server Action → 发送确认邮件并初始化 Marketplace 用户
  2. POST /auth/v1/verify  → 验证邮箱 (token_hash + type=signup)
  3. POST /auth/v1/token?grant_type=password → 获取 access_token + refresh_token
  4. GET  /auth/v1/user    → 获取用户信息
  5. tRPC main.updateUsername/main.updateFullName → 补全资料
  6. POST /api/trpc/apiKey.addApiKey → 创建 API Key

API Key 用于 swarms.world 所有 API 调用，格式: sk-xxxx
"""

from __future__ import annotations

import base64
import json
import re
import secrets
import time
from typing import Any, Callable
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests


SWARMS_BASE = "https://swarms.world"
SUPABASE_BASE = "https://db.swarms.world"
SUPABASE_AUTH = f"{SUPABASE_BASE}/auth/v1"
SWARMS_SIGNUP_PAGE = f"{SWARMS_BASE}/signin/signup"
SWARMS_SIGNUP_ACTION_ID = "601153ec011ca1038abff057e5007df14067cfaf34"

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
        self._user_info: dict[str, Any] = {}
        self._session_payload: dict[str, Any] = {}

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

    def _extract_signup_action_id(self, html: str) -> str:
        # Next Server Action ID 会随部署变化；优先从登录页引用的 chunk 中解析。
        # 解析失败时回退到当前线上实测 ID，避免临时页面结构变化导致注册中断。
        candidates = re.findall(r'<script[^>]+src=["\']([^"\']+\.js[^"\']*)["\']', html or "")
        for src in candidates:
            if "signin" not in src and "page" not in src and "chunks" not in src:
                continue
            try:
                chunk_url = urljoin(SWARMS_BASE, src)
                resp = self.session.get(chunk_url, timeout=30)
                if resp.status_code >= 400:
                    continue
                match = re.search(
                    r'createServerReference\("([0-9a-f]{40,})"[^)]*?,"signUp"\)',
                    resp.text,
                )
                if match:
                    return match.group(1)
                match = re.search(
                    r'createServerReference\("([0-9a-f]{40,})"[^)]*?signUp',
                    resp.text,
                )
                if match:
                    return match.group(1)
            except Exception:
                continue
        return SWARMS_SIGNUP_ACTION_ID

    def _signup_fingerprint(self) -> str:
        value = str(self.session.cookies.get("sf_rsint") or "").strip()
        if value:
            return value
        return secrets.token_hex(32)

    def signup(self, email: str, password: str) -> dict:
        self.log(f"注册 Swarms Marketplace 账号: {email}")
        # 不能直接打 Supabase /signup：实测直连注册不会初始化 marketplace 额度，
        # 导致后续 apiKey.addApiKey 报 No credit available。
        page_resp = self.session.get(SWARMS_SIGNUP_PAGE, timeout=30)
        if page_resp.status_code >= 400:
            self.log(f"Swarms 注册页请求失败 {page_resp.status_code}: {page_resp.text[:300]}")
            page_resp.raise_for_status()
        action_id = self._extract_signup_action_id(page_resp.text)
        fingerprint = self._signup_fingerprint()
        files = {
            "1_email": (None, email),
            "1_password": (None, password),
            "1_fingerprint": (None, fingerprint),
            "0": (None, '["$K1"]'),
        }
        headers = {
            "Accept": "text/x-component",
            "Next-Action": action_id,
            "Origin": SWARMS_BASE,
            "Referer": SWARMS_SIGNUP_PAGE,
        }
        resp = self.session.post(
            SWARMS_SIGNUP_PAGE,
            files=files,
            headers=headers,
            timeout=30,
            allow_redirects=False,
        )
        text = resp.text or ""
        if resp.status_code >= 400:
            self.log(f"Swarms 注册 action 失败 {resp.status_code}: {text[:500]}")
            resp.raise_for_status()
        decoded_text = unquote(text)
        error_match = re.search(r"(?:error|error_description)=([^\"&]+(?:[^\"]{0,160}))", decoded_text, re.IGNORECASE)
        explicit_error = (
            "status=Error" in decoded_text
            or "status_description=Error" in decoded_text
            or "Sign-up limit reached" in decoded_text
            or "Please wait 24 hours before creating another account" in decoded_text
        )
        if explicit_error:
            # Next RSC 响应没有稳定 JSON shape，只在出现明确业务错误时中断。
            # 普通 chunk 里可能包含 error 字符串，不能据此判定注册失败。
            detail = (error_match.group(0) if error_match else decoded_text[:500]).strip()
            if "Sign-up limit reached" in decoded_text or "Please wait 24 hours" in decoded_text:
                detail = "Sign-up limit reached. Please wait 24 hours before creating another account."
            raise RuntimeError(f"Swarms 注册 action 返回异常: {detail[:500]}")
        if "status=Success" not in text and "confirmation" not in text.lower():
            self.log("警告: Swarms 注册 action 未出现确认邮件成功提示，继续等待邮箱验证")
        return {
            "ok": True,
            "email": email,
            "signup_method": "swarms_server_action",
            "action_id": action_id,
            "fingerprint": fingerprint,
            "raw_preview": text[:500],
        }

    def verify_email(self, token_hash: str, signup_type: str = "signup") -> dict:
        self.log("验证邮箱...")
        return self._post(f"{SUPABASE_AUTH}/verify", {
            "token_hash": token_hash,
            "type": signup_type,
        })

    def _apply_session_result(self, result: dict, *, persist_cookie: bool = False) -> dict:
        self._access_token = str(result.get("access_token") or "")
        self._refresh_token = str(result.get("refresh_token") or "")
        self._session_payload = dict(result) if isinstance(result, dict) else {}
        user = result.get("user")
        if isinstance(user, dict):
            self._user_info = dict(user)
            self._user_id = str(user.get("id") or self._user_id or "")
        if persist_cookie and self._access_token:
            self._store_auth_cookie()
        return result

    def login(self, email: str, password: str) -> dict:
        self.log("密码登录...")
        result = self._post(
            f"{SUPABASE_AUTH}/token?grant_type=password",
            {"email": email, "password": password},
        )
        self._apply_session_result(result, persist_cookie=True)
        if self._access_token:
            self.log("登录成功，已获取 access_token")
        return result

    @staticmethod
    def _decode_supabase_storage_cookie(value: str) -> str:
        raw = str(value or "")
        if not raw.startswith("base64-"):
            return raw
        encoded = raw[len("base64-"):]
        padding = "=" * ((4 - len(encoded) % 4) % 4)
        return base64.urlsafe_b64decode((encoded + padding).encode("ascii")).decode("utf-8")

    @staticmethod
    def _encode_supabase_storage_cookie(value: str) -> str:
        encoded = base64.urlsafe_b64encode(str(value or "").encode("utf-8")).decode("ascii")
        return "base64-" + encoded.rstrip("=")

    def _cookie_value(self, name: str) -> str:
        for cookie in self.session.cookies:
            if cookie.name == name and cookie.value:
                return str(cookie.value)
        return ""

    def _storage_cookie_item(self, name: str) -> str:
        direct = self._cookie_value(name)
        if direct:
            return self._decode_supabase_storage_cookie(direct)
        chunks: list[str] = []
        for index in range(5):
            value = self._cookie_value(f"{name}.{index}")
            if not value:
                break
            chunks.append(value)
        if not chunks:
            return ""
        return self._decode_supabase_storage_cookie("".join(chunks))

    def _existing_auth_cookie(self) -> str:
        direct = self._cookie_value("sb-db-auth-token")
        if direct:
            return direct
        chunks: list[str] = []
        for index in range(5):
            value = self._cookie_value(f"sb-db-auth-token.{index}")
            if not value:
                break
            chunks.append(value)
        return "".join(chunks)

    def _store_auth_cookie(self) -> str:
        value = self._encode_supabase_storage_cookie(self._session_cookie_value())
        self.session.cookies.set("sb-db-auth-token", value, domain="swarms.world", path="/")
        return value

    def _load_auth_cookie_session(self) -> dict:
        raw = self._storage_cookie_item("sb-db-auth-token")
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except Exception:
            return {}
        if not isinstance(payload, dict):
            return {}
        self._apply_session_result(payload)
        return payload

    @staticmethod
    def _first_param(params: dict[str, list[str]], key: str) -> str:
        values = params.get(key) or []
        return str(values[0] or "") if values else ""

    @staticmethod
    def _int_param(value: str, default: int = 0) -> int:
        try:
            return int(float(str(value).strip()))
        except Exception:
            return default

    def _session_from_redirect_url(self, url: str) -> dict:
        parsed = urlparse(str(url or ""))
        candidates = [parsed.fragment, parsed.query]
        if parsed.query and "#" in parsed.query:
            candidates.append(parsed.query.split("#", 1)[1])
        for candidate in candidates:
            params = parse_qs(candidate or "")
            access_token = self._first_param(params, "access_token")
            refresh_token = self._first_param(params, "refresh_token")
            token_type = self._first_param(params, "token_type") or "bearer"
            expires_in = self._int_param(self._first_param(params, "expires_in"), 3600)
            expires_at = self._int_param(self._first_param(params, "expires_at"), 0)
            if access_token and refresh_token:
                if not expires_at:
                    expires_at = int(time.time()) + max(expires_in, 0)
                return {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_in": expires_in,
                    "expires_at": expires_at,
                    "token_type": token_type,
                }
        return {}

    def verify_email_link(self, url: str) -> dict:
        self.log("打开 Swarms 邮箱确认链接...")
        current_url = str(url or "").strip()
        if not current_url:
            raise ValueError("Swarms 邮箱确认链接为空")
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": SWARMS_SIGNUP_PAGE,
        }
        last_response = None
        session_result: dict = {}
        for _ in range(5):
            response = self.session.get(
                current_url,
                headers=headers,
                timeout=30,
                allow_redirects=False,
            )
            last_response = response
            session_result = self._session_from_redirect_url(current_url)
            if session_result:
                break
            location = response.headers.get("Location") or ""
            if location:
                current_url = urljoin(current_url, location)
                session_result = self._session_from_redirect_url(current_url)
                if session_result:
                    break
                continue
            break
        if not session_result and last_response is not None and last_response.status_code >= 400:
            self.log(f"Swarms 确认链接请求失败 {last_response.status_code}: {last_response.text[:300]}")
            last_response.raise_for_status()
        if not session_result:
            cookie_payload = self._load_auth_cookie_session()
            return {
                "ok": bool(cookie_payload),
                "auth_cookie": bool(self._existing_auth_cookie()),
                "access_token": bool(self._access_token),
            }
        self._apply_session_result(session_result)
        try:
            self.get_user()
        except Exception as exc:
            self.log(f"确认链接登录态获取用户失败（继续使用会话）: {exc}")
        self._store_auth_cookie()
        self.log("邮箱确认链接已产生 Swarms 登录态")
        return {"ok": True, "auth_cookie": True, "access_token": bool(self._access_token)}

    def get_user(self) -> dict:
        self.log("获取用户信息...")
        result = self._get(f"{SUPABASE_AUTH}/user", auth=True)
        self._user_id = result.get("id", "")
        self._user_info = dict(result) if isinstance(result, dict) else {}
        return result

    # --- tRPC API ---

    def _session_cookie_value(self) -> str:
        payload = dict(self._session_payload or {})
        if self._access_token:
            payload["access_token"] = self._access_token
        if self._refresh_token:
            payload["refresh_token"] = self._refresh_token
        payload.setdefault("token_type", "bearer")
        payload.setdefault("expires_in", 3600)
        if self._user_info:
            payload["user"] = self._user_info
        elif self._user_id:
            payload.setdefault("user", {"id": self._user_id})
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    def _trpc_cookies(self) -> dict[str, str]:
        existing = self._existing_auth_cookie()
        if existing:
            return {"sb-db-auth-token": existing}
        if not self._access_token:
            return {}
        # swarms.world 当前前端会话使用这个短 cookie 名；仅带 Authorization 会被 tRPC 拒绝。
        return {"sb-db-auth-token": self._store_auth_cookie()}

    @staticmethod
    def _unwrap_trpc_response(data: Any) -> Any:
        if isinstance(data, list):
            if len(data) == 1:
                return SwarmsClient._unwrap_trpc_response(data[0])
            return [SwarmsClient._unwrap_trpc_response(item) for item in data]
        if isinstance(data, dict):
            result = data.get("result")
            if isinstance(result, dict):
                data_part = result.get("data", result)
                if isinstance(data_part, dict) and "json" in data_part:
                    return data_part.get("json")
                return data_part
            if "json" in data:
                return data.get("json")
        return data

    def _trpc(self, path: str, payload: dict | None = None, *, method: str = "POST") -> Any:
        headers = {
            "Content-Type": "application/json",
            "Origin": SWARMS_BASE,
            "Referer": f"{SWARMS_BASE}/",
        }
        url = f"{SWARMS_BASE}/api/trpc/{path}"
        cookies = self._trpc_cookies()
        request_method = str(method or "POST").upper()
        if request_method == "GET":
            resp = self.session.get(
                url,
                headers=headers,
                cookies=cookies,
                timeout=30,
            )
        else:
            resp = self.session.post(
                url,
                json={"json": payload or {}},
                headers=headers,
                cookies=cookies,
                timeout=30,
            )
        if resp.status_code >= 400:
            self.log(f"tRPC error {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        try:
            return self._unwrap_trpc_response(resp.json())
        except Exception:
            return {"raw": resp.text}

    def create_api_key(self, name: str = "auto-register") -> dict:
        self.log(f"创建 API Key [{name}]...")
        result = self._trpc("apiKey.addApiKey", {"name": name}, method="POST")
        return result if isinstance(result, dict) else {"data": result}

    def list_api_keys(self) -> list[dict]:
        self.log("获取 API Key 列表...")
        result = self._trpc("apiKey.getApiKeys", method="GET")
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if isinstance(result, dict):
            keys = result.get("apiKeys") or result.get("keys") or result.get("data") or []
            if isinstance(keys, list):
                return [item for item in keys if isinstance(item, dict)]
            if isinstance(keys, dict):
                return [keys]
            if result.get("key") or result.get("apiKey"):
                return [result]
        return []

    def get_profile(self) -> dict:
        self.log("获取 Swarms 用户资料...")
        result = self._trpc("main.getUser", method="GET")
        return result if isinstance(result, dict) else {"data": result}

    def update_username(self, username: str) -> dict:
        username = str(username or "").strip()
        if not username:
            raise ValueError("Swarms 用户名不能为空")
        self.log(f"设置用户名: {username}")
        result = self._trpc("main.updateUsername", {"username": username}, method="POST")
        return result if isinstance(result, dict) else {"data": result}

    def update_full_name(self, full_name: str) -> dict:
        full_name = str(full_name or "").strip()
        if not full_name:
            raise ValueError("Swarms 昵称不能为空")
        self.log(f"设置昵称: {full_name}")
        result = self._trpc("main.updateFullName", {"full_name": full_name}, method="POST")
        return result if isinstance(result, dict) else {"data": result}

    @staticmethod
    def _username_from_email(email: str) -> str:
        local = str(email or "").split("@", 1)[0].lower()
        normalized = re.sub(r"[^a-z0-9_]", "_", local).strip("_")
        if not normalized or not normalized[0].isalpha():
            normalized = f"sw_{normalized}" if normalized else "sw_user"
        suffix = secrets.token_hex(2)
        base = normalized[: max(3, 16 - len(suffix) - 1)].strip("_") or "sw_user"
        username = f"{base}_{suffix}"[:16].strip("_")
        if len(username) < 3:
            username = (username + suffix + "000")[:3]
        return username

    @staticmethod
    def credit_amount(value: Any) -> float:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            for key in ("credit", "credits", "balance", "amount", "data"):
                if key not in value:
                    continue
                try:
                    return SwarmsClient.credit_amount(value.get(key))
                except Exception:
                    continue
        try:
            return float(str(value).strip())
        except Exception:
            return 0.0

    def get_credit(self) -> dict:
        self.log("查询账户额度...")
        result = self._trpc("panel.getUserCredit", method="GET")
        return result if isinstance(result, dict) else {"data": result}

    def wait_for_credit(self, *, min_credit: float = 0.01, timeout: float = 15.0, interval: float = 2.0) -> dict:
        deadline = time.time() + max(float(timeout or 0), 0.0)
        last: dict = {}
        while True:
            try:
                last = self.get_credit()
                amount = self.credit_amount(last)
                if amount >= min_credit:
                    self.log(f"账户额度: ${amount:g}")
                    return last
                self.log(f"账户额度暂未到账: ${amount:g}")
            except Exception as exc:
                last = {"error": str(exc)}
                self.log(f"查询账户额度失败（稍后重试）: {exc}")
            if time.time() >= deadline:
                return last
            time.sleep(max(float(interval or 1.0), 0.5))

    def ensure_profile(self, *, email: str, full_name: str = "Auto Register") -> dict:
        profile: dict = {}
        try:
            profile = self.get_profile()
        except Exception as exc:
            self.log(f"获取 Swarms 用户资料失败（继续补资料）: {exc}")
        username = str(profile.get("username") or "").strip()
        if not username:
            username = self._username_from_email(email)
            try:
                updated = self.update_username(username)
                if isinstance(updated, dict):
                    profile.update(updated)
                profile["username"] = username
            except Exception as exc:
                self.log(f"设置用户名失败（非阻塞）: {exc}")
        current_full_name = str(profile.get("full_name") or "").strip()
        if not current_full_name and full_name:
            try:
                updated = self.update_full_name(full_name)
                if isinstance(updated, dict):
                    profile.update(updated)
                profile.setdefault("full_name", full_name)
            except Exception as exc:
                self.log(f"设置昵称失败（非阻塞）: {exc}")
        return profile

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
        cookies = {c.name: c.value for c in self.session.cookies}
        cookies.update(self._trpc_cookies())
        return cookies
