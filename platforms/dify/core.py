"""Dify Cloud (cloud.dify.ai) 协议客户端。

纯 HTTP 注册链路：
  1. POST /console/api/email-code-login  发送验证码到邮箱
  2. POST /console/api/email-code-login/validity  验证码登录（自动创建账号）
  3. POST /console/api/apps  创建聊天助手应用
  4. POST /console/api/apps/{app_id}/api-keys  生成 API Key
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable
from urllib.parse import urljoin

import requests


DIFY_BASE = "https://cloud.dify.ai"
DIFY_CONSOLE_API = f"{DIFY_BASE}/console/api"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


class DifyClient:
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
            "Origin": DIFY_BASE,
            "Referer": f"{DIFY_BASE}/signin",
        })
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        self._access_token: str = ""
        self._refresh_token: str = ""
        self._csrf_token: str = ""

    def _api(self, path: str) -> str:
        return f"{DIFY_CONSOLE_API}{path}"

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if self._csrf_token:
            headers["X-CSRF-Token"] = self._csrf_token
        return headers

    def _extract_tokens_from_cookies(self) -> None:
        for cookie in self.session.cookies:
            name = cookie.name
            if name in ("access_token", "session", "__Host-access_token"):
                if not self._access_token:
                    self._access_token = cookie.value
            elif name in ("refresh_token", "__Host-refresh_token"):
                self._refresh_token = cookie.value
            elif name in ("csrf_token", "__Host-csrf_token"):
                self._csrf_token = cookie.value

    def _post(self, path: str, payload: dict | None = None, *, auth: bool = False) -> dict:
        headers = {"Content-Type": "application/json"}
        if auth:
            headers.update(self._auth_headers())
        resp = self.session.post(
            self._api(path),
            json=payload or {},
            headers=headers,
            timeout=30,
        )
        self._extract_tokens_from_cookies()
        if resp.status_code >= 400:
            self.log(f"Dify API error {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    def _get(self, path: str, *, auth: bool = True) -> dict:
        headers = {}
        if auth:
            headers.update(self._auth_headers())
        resp = self.session.get(self._api(path), headers=headers, timeout=30)
        self._extract_tokens_from_cookies()
        if resp.status_code >= 400:
            self.log(f"Dify API error {resp.status_code}: {resp.text[:500]}")
            resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # --- 注册流程 ---

    def send_email_code(self, email: str) -> dict:
        self.log(f"发送验证码到 {email}...")
        return self._post("/email-code-login", {
            "email": email,
            "language": "en-US",
        })

    @staticmethod
    def _b64_code(code: str) -> str:
        import base64
        return base64.b64encode(code.encode()).decode()

    def verify_email_code(self, email: str, code: str, token: str = "") -> dict:
        self.log("验证邮箱验证码...")
        payload: dict[str, Any] = {
            "email": email,
            "code": self._b64_code(code),
            "language": "en-US",
            "timezone": "Asia/Shanghai",
        }
        if token:
            payload["token"] = token
        result = self._post("/email-code-login/validity", payload)
        self._extract_tokens_from_cookies()
        if not self._access_token:
            if isinstance(result, dict) and result.get("data"):
                data = result["data"]
                if isinstance(data, dict):
                    self._access_token = data.get("access_token", "")
                    self._refresh_token = data.get("refresh_token", "")
                elif isinstance(data, str):
                    self._access_token = data
        self.log("验证码验证成功，已登录")
        return result

    def send_register_email(self, email: str) -> dict:
        self.log(f"发送注册验证码到 {email}...")
        return self._post("/email-register/send-email", {
            "email": email,
            "language": "en-US",
        })

    def verify_register_code(self, email: str, code: str, token: str = "") -> dict:
        self.log("验证注册验证码...")
        payload: dict[str, Any] = {
            "email": email,
            "code": self._b64_code(code),
        }
        if token:
            payload["token"] = token
        return self._post("/email-register/validity", payload)

    def register_with_password(self, token: str, password: str) -> dict:
        self.log("设置密码完成注册...")
        result = self._post("/email-register", {
            "token": token,
            "new_password": password,
            "password_confirm": password,
            "language": "en-US",
            "timezone": "Asia/Shanghai",
        })
        self._extract_tokens_from_cookies()
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                self._access_token = data.get("access_token", "") or self._access_token
                self._refresh_token = data.get("refresh_token", "") or self._refresh_token
                self._csrf_token = data.get("csrf_token", "") or self._csrf_token
        self.log("注册成功")
        return result

    def login(self, email: str, password: str) -> dict:
        self.log("密码登录...")
        result = self._post("/login", {
            "email": email,
            "password": password,
            "remember_me": True,
        })
        self._extract_tokens_from_cookies()
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                self._access_token = data.get("access_token", "") or self._access_token
                self._refresh_token = data.get("refresh_token", "") or self._refresh_token
        return result

    # --- 应用 & API Key ---

    def get_account_info(self) -> dict:
        self.log("获取账户信息...")
        return self._get("/account/profile", auth=True)

    def create_app(self, name: str = "auto-chat", mode: str = "chat") -> dict:
        self.log(f"创建应用 [{name}] (mode={mode})...")
        return self._post("/apps", {
            "name": name,
            "description": "Auto-created chat assistant",
            "mode": mode,
            "icon_type": "emoji",
            "icon": "\U0001f916",
            "icon_background": "#FFEAD5",
        }, auth=True)

    def create_api_key(self, app_id: str) -> dict:
        self.log(f"创建 API Key (app={app_id[:8]}...)...")
        return self._post(f"/apps/{app_id}/api-keys", auth=True)

    def list_apps(self, page: int = 1, limit: int = 20) -> dict:
        return self._get(f"/apps?page={page}&limit={limit}&mode=all", auth=True)

    def list_api_keys(self, app_id: str) -> dict:
        return self._get(f"/apps/{app_id}/api-keys", auth=True)

    def import_dsl(self, yaml_content: str, *, name: str | None = None) -> dict:
        self.log("导入 DSL 模板...")
        payload: dict[str, Any] = {
            "mode": "yaml-content",
            "yaml_content": yaml_content,
        }
        if name:
            payload["name"] = name
        result = self._post("/apps/imports", payload, auth=True)
        status = result.get("status", "")
        if status == "pending":
            import_id = result.get("id", "")
            if import_id:
                self.log("DSL 版本差异，自动确认导入...")
                result = self._post(f"/apps/imports/{import_id}/confirm", auth=True)
        app_id = result.get("app_id", "")
        if app_id:
            self.log(f"DSL 导入成功，app_id={app_id[:8]}...")
        else:
            self.log(f"DSL 导入结果: {result}")
        return result

    def export_dsl(self, app_id: str) -> str:
        self.log(f"导出 DSL (app={app_id[:8]}...)...")
        result = self._get(f"/apps/{app_id}/export", auth=True)
        return result.get("data", "")

    # --- 插件管理 ---

    MARKETPLACE_API = "https://marketplace.dify.ai/api/v1"
    TRIAL_PLUGIN_IDS = [
        "langgenius/openai",
        "langgenius/anthropic",
        "langgenius/gemini",
        "langgenius/deepseek",
        "langgenius/x",
        "langgenius/tongyi",
    ]

    def fetch_latest_plugin_identifiers(self, plugin_ids: list[str] | None = None) -> dict[str, str]:
        ids = plugin_ids or self.TRIAL_PLUGIN_IDS
        self.log(f"从 Marketplace 获取 {len(ids)} 个插件最新版本...")
        resp = self.session.post(
            f"{self.MARKETPLACE_API}/plugins/batch",
            json={"plugin_ids": ids},
            headers={"Content-Type": "application/json", "X-Dify-Version": "1.4.0"},
            timeout=30,
        )
        if resp.status_code >= 400:
            self.log(f"Marketplace API error {resp.status_code}: {resp.text[:300]}")
            resp.raise_for_status()
        data = resp.json()
        plugins = data.get("data", {}).get("plugins", [])
        result: dict[str, str] = {}
        for p in plugins:
            pid = p.get("plugin_id", "")
            identifier = p.get("latest_package_identifier", "")
            if pid and identifier:
                result[pid] = identifier
        self.log(f"获取到 {len(result)} 个插件标识符")
        return result

    def install_marketplace_plugins(self, identifiers: list[str]) -> dict:
        self.log(f"安装 {len(identifiers)} 个 Marketplace 插件...")
        result = self._post(
            "/workspaces/current/plugin/install/marketplace",
            {"plugin_unique_identifiers": identifiers},
            auth=True,
        )
        if result.get("all_installed"):
            self.log("所有插件已安装完成")
            return result
        task_id = result.get("task_id", "")
        if task_id:
            self.log(f"插件安装任务已提交: {task_id[:8]}...，轮询状态...")
            return self._poll_plugin_task(task_id)
        return result

    def _poll_plugin_task(self, task_id: str, max_wait: int = 60) -> dict:
        for _ in range(max_wait // 2):
            time.sleep(2)
            try:
                result = self._get(
                    f"/workspaces/current/plugin/tasks/{task_id}", auth=True
                )
                task = result.get("task", result)
                status = task.get("status", "")
                if status == "success":
                    self.log("插件安装成功")
                    return {"ok": True, "status": "success", "task": task}
                if status == "failed":
                    self.log(f"插件安装失败: {task}")
                    return {"ok": False, "status": "failed", "task": task}
            except Exception as exc:
                self.log(f"轮询插件任务出错: {exc}")
        self.log("插件安装超时")
        return {"ok": False, "status": "timeout"}

    def list_installed_plugins(self) -> list[dict]:
        result = self._get(
            "/workspaces/current/plugin/list?page=1&page_size=256", auth=True
        )
        return result.get("plugins", [])

    def install_trial_plugins(self) -> dict:
        try:
            identifiers = self.fetch_latest_plugin_identifiers()
        except Exception as exc:
            self.log(f"获取插件标识符失败: {exc}")
            return {"ok": False, "error": str(exc)}
        if not identifiers:
            return {"ok": False, "error": "未获取到任何插件标识符"}
        return self.install_marketplace_plugins(list(identifiers.values()))

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def cookies(self) -> dict[str, str]:
        return {c.name: c.value for c in self.session.cookies}
