"""AI-ROUTER 协议注册与 API Key 创建。"""
from __future__ import annotations

import random
import time
from typing import Any

import requests

SITE_URL = "https://ai-router.dev/"
REGISTER_URL = "https://ai-router.dev/register"
DASHBOARD_URL = "https://ai-router.dev/dashboard"
API_BASE = "https://api.ai-router.dev/api/v1"
SETTINGS_URL = f"{API_BASE}/settings/public"
SEND_VERIFY_URL = f"{API_BASE}/auth/send-verify-code"
REGISTER_API_URL = f"{API_BASE}/auth/register"
ME_URL = f"{API_BASE}/auth/me"
KEYS_URL = f"{API_BASE}/keys"
GROUPS_AVAILABLE_URL = f"{API_BASE}/groups/available"
MODELS_URL = "https://api.ai-router.dev/v1/models"


_CHROME_MAJOR_VERSIONS = (136, 137, 138, 139, 140, 141, 142, 143)


def build_airouter_browser_fingerprint(seed: str = "") -> dict[str, Any]:
    """为单个注册账号生成一套轻量浏览器指纹。

    目标不是伪装完整设备，而是保证同一账号内的 Turnstile、发码、注册
    使用一致的 UA / Client Hints / 语言 / 视口；不同账号之间自然隔离。
    """
    rng = random.Random(seed or f"{time.time_ns()}-{random.random()}")
    major = rng.choice(_CHROME_MAJOR_VERSIONS)
    build = rng.randint(0, 7390)
    patch = rng.randint(40, 180)
    width, height = rng.choice(((1366, 768), (1440, 900), (1536, 864), (1600, 900), (1920, 1080)))
    locale = rng.choice(("en-US", "en-US", "en-GB"))
    platform = "Windows"
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.{build}.{patch} Safari/537.36"
    )
    return {
        "user_agent": ua,
        "accept_language": f"{locale},{locale.split('-')[0]};q=0.9",
        "locale": locale,
        "platform": platform,
        "viewport_width": width,
        "viewport_height": height,
        "device_scale_factor": rng.choice((1, 1, 1.25)),
        "chrome_major": major,
    }


def _safe_json(response: Any) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:
        return {"raw": str(getattr(response, "text", "") or "")[:2000]}
    return data if isinstance(data, dict) else {"data": data}


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "code" in payload and "data" in payload:
        return payload.get("data")
    if isinstance(payload, dict) and "data" in payload:
        return payload.get("data")
    return payload


def _raise_api_error(response: Any, payload: dict[str, Any], label: str) -> None:
    ok_code = not isinstance(payload, dict) or payload.get("code", 0) == 0
    if response.ok and ok_code:
        return
    raise RuntimeError(f"AI-ROUTER {label}失败: status={response.status_code} body={payload}")


def _find_api_key(data: Any) -> str:
    if isinstance(data, str):
        text = data.strip()
        if len(text) >= 16 and (text.startswith(("sk-", "air-", "ak-")) or "_" in text):
            return text
        return ""
    if isinstance(data, dict):
        for key in ("key", "api_key", "apiKey", "token", "secret", "value", "plain_key", "raw_key"):
            found = _find_api_key(data.get(key))
            if found:
                return found
        for value in data.values():
            found = _find_api_key(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = _find_api_key(item)
            if found:
                return found
    return ""


class AiRouterClient:
    def __init__(self, *, proxy: str | None = None, log_fn=print, browser_fingerprint: dict[str, Any] | None = None) -> None:
        self.proxy = proxy
        self.log = log_fn or (lambda _msg: None)
        self.browser_fingerprint = dict(browser_fingerprint or build_airouter_browser_fingerprint())
        self.session = requests.Session()
        # 禁止 requests 继承 HTTP_PROXY/HTTPS_PROXY/系统代理；AI-ROUTER 必须只走任务代理。
        self.session.trust_env = False
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://ai-router.dev",
            "Referer": REGISTER_URL,
            "User-Agent": str(self.browser_fingerprint.get("user_agent") or ""),
            "Accept-Language": str(self.browser_fingerprint.get("accept_language") or "en-US,en;q=0.9"),
            "Sec-CH-UA": self._sec_ch_ua(),
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": f'"{self.browser_fingerprint.get("platform") or "Windows"}"',
        })

    def _sec_ch_ua(self) -> str:
        major = str(self.browser_fingerprint.get("chrome_major") or "140")
        return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not=A?Brand";v="24"'

    def _l(self, msg: str) -> None:
        self.log(f"[AI-ROUTER] {msg}")

    def public_settings(self) -> dict[str, Any]:
        response = self.session.get(SETTINGS_URL, timeout=30)
        payload = _safe_json(response)
        _raise_api_error(response, payload, "读取 public settings")
        data = _unwrap(payload)
        return data if isinstance(data, dict) else {}

    def send_verify_code(self, *, email: str, turnstile_token: str = "", webrtc_client_ip: str = "") -> dict[str, Any]:
        body: dict[str, Any] = {"email": email}
        if turnstile_token:
            body["turnstile_token"] = turnstile_token
        if webrtc_client_ip:
            body["webrtc_client_ip"] = webrtc_client_ip
        response = self.session.post(SEND_VERIFY_URL, json=body, timeout=30)
        payload = _safe_json(response)
        _raise_api_error(response, payload, "发送邮箱验证码")
        data = _unwrap(payload)
        return data if isinstance(data, dict) else {"data": data}

    def register(
        self,
        *,
        email: str,
        password: str,
        verify_code: str = "",
        turnstile_token: str = "",
        promo_code: str = "",
        invitation_code: str = "",
        affiliate_fingerprint: str = "",
        aff_code: str = "",
        webrtc_client_ip: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"email": email, "password": password}
        if verify_code:
            body["verify_code"] = verify_code
        if turnstile_token:
            body["turnstile_token"] = turnstile_token
        if promo_code:
            body["promo_code"] = promo_code
        if invitation_code:
            body["invitation_code"] = invitation_code
        if affiliate_fingerprint:
            body["affiliate_fingerprint"] = affiliate_fingerprint
        if aff_code:
            body["aff_code"] = aff_code
        if webrtc_client_ip:
            body["webrtc_client_ip"] = webrtc_client_ip

        response = self.session.post(REGISTER_API_URL, json=body, timeout=45)
        payload = _safe_json(response)
        _raise_api_error(response, payload, "注册")
        data = _unwrap(payload)
        result = data if isinstance(data, dict) else {"data": data}
        access_token = str(result.get("access_token") or "").strip()
        if access_token:
            self.session.headers.update({"Authorization": f"Bearer {access_token}"})
        return result

    def get_me(self, access_token: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        response = self.session.get(ME_URL, headers=headers, timeout=30)
        payload = _safe_json(response)
        if not response.ok:
            return {"ok": False, "status": response.status_code, "data": payload}
        return {"ok": True, "status": response.status_code, "data": _unwrap(payload)}

    def list_available_groups(self, access_token: str) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {access_token}", "Referer": DASHBOARD_URL}
        response = self.session.get(GROUPS_AVAILABLE_URL, headers=headers, timeout=30)
        payload = _safe_json(response)
        _raise_api_error(response, payload, "读取可用分组")
        data = _unwrap(payload)
        return [item for item in (data or []) if isinstance(item, dict)] if isinstance(data, list) else []

    def resolve_api_key_group_id(self, access_token: str, preferred_group_id: int | str | None = None) -> tuple[int | None, dict[str, Any]]:
        if preferred_group_id not in (None, ""):
            try:
                group_id = int(preferred_group_id)
                return group_id, {"id": group_id, "source": "preferred"}
            except Exception:
                self._l(f"忽略无效 airouter_group_id: {preferred_group_id}")

        groups = self.list_available_groups(access_token)
        if not groups:
            return None, {}

        def score(group: dict[str, Any]) -> tuple[int, float, int]:
            name = str(group.get("name") or "").lower()
            desc = str(group.get("description") or "").lower()
            text = f"{name} {desc}"
            platform = str(group.get("platform") or "").lower()
            status = str(group.get("status") or "").lower()
            billing = str(group.get("billing_policy") or "").lower()
            bad_hint = any(token in text for token in ("do not call", "请不要调用", "暂时没有", "deepseek"))
            # 优先选择前端“使用说明”适配的 OpenAI balance 分组；排除明显占位/不可调用分组。
            rank = 0
            rank += 100 if status == "active" else 0
            rank += 80 if platform == "openai" else 0
            rank += 40 if billing == "balance_only" else 0
            rank += 20 if group.get("allow_messages_dispatch") else 0
            rank -= 200 if bad_hint else 0
            rate = float(group.get("rate_multiplier") or 9999)
            return (rank, -rate, -int(group.get("id") or 0))

        selected = sorted(groups, key=score, reverse=True)[0]
        group_id = selected.get("id")
        try:
            return int(group_id), selected
        except Exception:
            return None, selected

    def create_api_key(
        self,
        access_token: str,
        *,
        name: str = "auto-register",
        group_id: int | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}", "Referer": DASHBOARD_URL}
        body: dict[str, Any] = {"name": name}
        if group_id is not None:
            body["group_id"] = group_id
        response = self.session.post(KEYS_URL, json=body, headers=headers, timeout=30)
        payload = _safe_json(response)
        _raise_api_error(response, payload, "创建 API Key")
        data = _unwrap(payload)
        api_key = _find_api_key(data)
        return {
            "ok": response.ok and bool(api_key),
            "status": response.status_code,
            "api_key": api_key,
            "data": data if isinstance(data, (dict, list)) else {"value": data},
            "raw": payload,
        }

    def verify_api_key(self, api_key: str) -> dict[str, Any]:
        if not api_key:
            return {"ok": False, "reason": "missing_api_key"}
        try:
            response = self.session.get(
                MODELS_URL,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=30,
            )
            payload = _safe_json(response)
            data = payload.get("data") if isinstance(payload, dict) else None
            return {
                "ok": response.ok and isinstance(data, list),
                "status": response.status_code,
                "reason": "models_list_ok" if response.ok else "models_list_failed",
                "model_count": len(data) if isinstance(data, list) else 0,
                "models_preview": [
                    str(item.get("id") or item.get("model") or item.get("name") or "")
                    for item in (data[:12] if isinstance(data, list) else [])
                    if isinstance(item, dict)
                ],
                "data": payload,
                "checked_at": int(time.time()),
            }
        except Exception as exc:
            return {"ok": False, "reason": "models_list_exception", "error": repr(exc), "checked_at": int(time.time())}
