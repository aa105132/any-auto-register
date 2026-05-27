from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

BASE_URL = "https://gettoken.dev"
PORTAL_LOGIN_URL = "https://pay.imgto.link"
PORTAL_APP_ID = "appw084AkI0Jtflej7t"


class GetTokenRegistrationClosed(RuntimeError):
    pass


def _unwrap_api_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict) and ("code" in payload or "status" in payload or "error" in payload):
        return data
    return payload


def _collect_json_api_key(payload: Any) -> tuple[str, dict]:
    if isinstance(payload, dict):
        direct = payload.get("apiKey") or payload.get("api_key") or payload.get("key") or payload.get("token")
        if isinstance(direct, str) and direct.strip():
            return direct.strip(), payload
        for key in ("apiKeys", "api_keys", "items", "list", "data", "result"):
            found, info = _collect_json_api_key(payload.get(key))
            if found:
                return found, info
        for value in payload.values():
            found, info = _collect_json_api_key(value)
            if found:
                return found, info
    if isinstance(payload, list):
        for item in payload:
            found, info = _collect_json_api_key(item)
            if found:
                return found, info
    return "", {}


def _iter_json_ids(payload: Any):
    stack = [payload]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            raw_id = current.get("apiKeyId") or current.get("id")
            if raw_id not in (None, ""):
                item_id = str(raw_id)
                if item_id not in seen:
                    seen.add(item_id)
                    yield item_id
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _normalize_china_phone(value: str) -> str:
    digits = re.sub(r"\D+", "", str(value or ""))
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) > 11:
        digits = digits[2:]
    return digits[:11]


def _normalize_country_code(value: str, default: str = "+86") -> str:
    raw = str(value or "").strip()
    if not raw:
        return default
    if raw.upper() in {"CN", "CHN", "中国"}:
        return "+86"
    digits = re.sub(r"\D+", "", raw)
    if not digits:
        return default
    return f"+{digits}"


class GetTokenProtocolOAuthWorker:
    """GetToken 协议 OAuth 工作器。

    协议层无法直接完成 Google OAuth，因为 gettoken 前端依赖外部 Portal Login SDK
    产出的 loginToken。若调用方未传 `gettoken_portal_login_token`，这里会明确
    返回可降级错误，由任务切到 CDP/浏览器链路。
    """

    def __init__(self, *, proxy: str | None = None, log_fn=print):
        self.proxy = proxy
        self.log = log_fn
        self.session = requests.Session()
        self.session.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "origin": BASE_URL,
            "referer": f"{BASE_URL}/console/api-keys",
        })
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})
        self.request_trace: list[dict[str, Any]] = []

    def _record(self, method: str, url: str, *, status: int | None = None, note: str = "") -> None:
        self.request_trace.append({"method": method, "url": url, "status": status, "note": note})

    def _json(self, response: requests.Response) -> dict:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {"raw": response.text[:1000]}

    def check_user(self) -> dict:
        url = f"{BASE_URL}/api/user/me"
        r = self.session.get(url, timeout=30)
        self._record("GET", "/api/user/me", status=r.status_code, note="验证登录态")
        return self._json(r)

    def portal_login(self, *, login_token: str, referral_code: str = "", referral_slug: str = "") -> dict:
        url = f"{BASE_URL}/api/auth/portal-login"
        payload = {
            "loginToken": login_token,
            "referralCode": referral_code or None,
            "referralHost": "gettoken.dev",
            "referralSlug": referral_slug or None,
        }
        r = self.session.post(url, json=payload, timeout=30)
        self._record("POST", "/api/auth/portal-login", status=r.status_code, note="提交 Portal loginToken 换取 gettoken session")
        data = self._json(r)
        if not r.ok or not data.get("success", False):
            err = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = str(err.get("code") or "")
            msg = str(err.get("message") or data.get("message") or "Portal login failed")
            if code == "REGISTRATION_CLOSED":
                raise GetTokenRegistrationClosed(msg)
            raise RuntimeError(f"GetToken portal login failed: {code or r.status_code} {msg}")
        return data

    def api_request(self, path: str, *, method: str = "GET", body: dict | None = None) -> dict:
        url = f"{BASE_URL}{path}"
        headers = {"content-type": "application/json"}
        if method.upper() == "GET":
            response = self.session.get(url, timeout=30)
        else:
            response = self.session.request(method.upper(), url, json=body, headers=headers, timeout=30)
        self._record(method.upper(), path, status=response.status_code, note="GetToken API 请求")
        return {"ok": response.ok, "status": response.status_code, "data": self._json(response)}

    def extract_or_create_api_key(self, *, create_api_key: bool = True) -> tuple[str, dict]:
        request_trace_start = len(self.request_trace)
        for list_path in ("/api/workspace/api-keys", "/api/workspace/api-keys?page=1&pageSize=20"):
            listed = self.api_request(list_path)
            data = listed.get("data") or {}
            key, info = _collect_json_api_key(data)
            if key:
                return key, {"source": "protocol_list", "request_trace": self.request_trace[request_trace_start:], **(info if isinstance(info, dict) else {})}
            for key_id in _iter_json_ids(data):
                revealed = self.api_request(f"/api/workspace/api-keys/{key_id}/reveal", method="POST")
                key, info = _collect_json_api_key(revealed.get("data"))
                if key:
                    return key, {"source": "protocol_reveal", "apiKeyId": key_id, "request_trace": self.request_trace[request_trace_start:], **(info if isinstance(info, dict) else {})}

        if create_api_key:
            created = self.api_request(
                "/api/workspace/api-keys",
                method="POST",
                body={"name": f"auto-register-{int(time.time())}"},
            )
            data = created.get("data") or {}
            if created.get("ok"):
                key, info = _collect_json_api_key(data)
                if key:
                    return key, {"source": "protocol_create", "request_trace": self.request_trace[request_trace_start:], **(info if isinstance(info, dict) else {})}
                for key_id in _iter_json_ids(data):
                    revealed = self.api_request(f"/api/workspace/api-keys/{key_id}/reveal", method="POST")
                    key, info = _collect_json_api_key(revealed.get("data"))
                    if key:
                        return key, {"source": "protocol_create_reveal", "apiKeyId": key_id, "request_trace": self.request_trace[request_trace_start:], **(info if isinstance(info, dict) else {})}
            else:
                self.log(f"[GetToken] 协议层创建 API Key 失败: {created}")
        return "", {"source": "not_found", "request_trace": self.request_trace[request_trace_start:]}

    def discover_api_keys(self) -> dict:
        """登录后用于发现/读取 API Key 的保守探测。

        当前未登录状态只确认 /api/user/me、/api/referrals/invite-link 等端点。
        实际 API Key 端点可能由 RSC/server action 下发，浏览器链路会记录真实请求。
        """
        candidates = [
            "/api/referrals/invite-link",
            "/api/api-keys",
            "/api/keys",
            "/api/console/api-keys",
            "/api/portal/api-keys",
        ]
        results: dict[str, Any] = {}
        for path in candidates:
            r = self.session.get(f"{BASE_URL}{path}", timeout=20)
            self._record("GET", path, status=r.status_code, note="API Key 端点探测")
            if r.ok:
                results[path] = self._json(r)
        return results

    def run(
        self,
        *,
        email_hint: str = "",
        portal_login_token: str = "",
        referral_code: str = "",
        referral_slug: str = "",
        create_api_key: bool = True,
    ) -> dict:
        self.log("[GetToken] 协议链路：检查当前登录态")
        user_result = self.check_user()
        user = ((user_result.get("data") or {}) if isinstance(user_result, dict) else {}).get("user") or {}
        if user:
            self.log("[GetToken] 已有登录态，开始探测 API Key 端点")
        elif portal_login_token:
            self.log("[GetToken] 使用外部 Portal loginToken 换取 session")
            login_result = self.portal_login(login_token=portal_login_token, referral_code=referral_code, referral_slug=referral_slug)
            user = ((login_result.get("data") or {}) if isinstance(login_result, dict) else {}).get("user") or {}
            if not user:
                user = (((self.check_user()).get("data") or {}).get("user") or {})
        else:
            self._record("OPEN", f"{PORTAL_LOGIN_URL}/auth/connect", note=f"需要 Portal Login SDK 生成 loginToken，appId={PORTAL_APP_ID}")
            raise RuntimeError(
                "GetToken 协议链路缺少 gettoken_portal_login_token；请降级到 CDP/真实浏览器完成 Google OAuth，或提供已获取的 portal loginToken。"
            )

        api_key, api_key_info = self.extract_or_create_api_key(create_api_key=create_api_key) if create_api_key else ("", {})
        if create_api_key and not api_key:
            self.log("[GetToken] 协议层未发现可用 API Key")
        return {
            "email": str(user.get("email") or email_hint or ""),
            "user_id": str(user.get("id") or ""),
            "api_key": api_key,
            "api_key_info": api_key_info,
            "account_info": user,
            "cookies": self.session.cookies.get_dict(),
            "session_cookie": "; ".join(f"{c.name}={c.value}" for c in self.session.cookies),
            "request_trace": self.request_trace,
            "registration_note": "protocol_probe" if not api_key else "protocol_api_key_found",
        }

class GetTokenProtocolPhoneWorker(GetTokenProtocolOAuthWorker):
    """GetToken 纯协议手机号注册/登录工作器。"""

    def __init__(self, *, proxy: str | None = None, log_fn=print):
        super().__init__(proxy=proxy, log_fn=log_fn)
        self.portal_session = requests.Session()
        self.portal_session.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36",
            "accept": "application/json, text/plain, */*",
            "origin": PORTAL_LOGIN_URL,
            "referer": f"{PORTAL_LOGIN_URL}/en/auth/connect/{PORTAL_APP_ID}?origin={BASE_URL}&preferredChannel=PHONE_SMS",
        })
        if proxy:
            self.portal_session.proxies.update({"http": proxy, "https": proxy})

    def _portal_json(self, response: requests.Response) -> dict:
        data = self._json(response)
        if not response.ok:
            message = str(data.get("message") or data.get("error") or response.text[:200])
            raise RuntimeError(f"GetToken Portal phone request failed: {response.status_code} {message}")
        if isinstance(data, dict) and data.get("code") not in (None, 200, "200") and data.get("success") is False:
            raise RuntimeError(f"GetToken Portal phone request failed: {data}")
        return data

    def fetch_login_providers(self, *, app_id: str = PORTAL_APP_ID, origin: str = BASE_URL) -> dict:
        response = self.portal_session.get(
            f"{PORTAL_LOGIN_URL}/api/internal/login/providers",
            params={"appId": app_id, "origin": origin},
            timeout=30,
        )
        self._record("GET", "/api/internal/login/providers", status=response.status_code, note="Portal 登录渠道探测")
        return _unwrap_api_payload(self._portal_json(response))

    def create_sms_attempt(
        self,
        *,
        app_id: str,
        origin: str,
        locale: str,
        phone_country_code: str,
        phone_number: str,
    ) -> dict:
        payload = {
            "appId": app_id,
            "autoClose": True,
            "origin": origin,
            "channel": "PHONE_SMS",
            "locale": locale,
            "preferredChannel": "PHONE_SMS",
            "phoneCountryCode": phone_country_code,
            "phoneNumber": phone_number,
        }
        response = self.portal_session.post(f"{PORTAL_LOGIN_URL}/api/v1/login/attempts", json=payload, timeout=30)
        self._record("POST", "/api/v1/login/attempts", status=response.status_code, note="发送手机号短信验证码")
        return _unwrap_api_payload(self._portal_json(response))

    def complete_sms_attempt(self, *, attempt_id: str, code: str) -> dict:
        path = f"/api/v1/login/attempts/{attempt_id}/sms/complete"
        response = self.portal_session.post(f"{PORTAL_LOGIN_URL}{path}", json={"code": code}, timeout=30)
        self._record("POST", path, status=response.status_code, note="提交短信验证码换取 Portal loginToken")
        return _unwrap_api_payload(self._portal_json(response))

    @staticmethod
    def _phone_metadata(account) -> dict:
        return {
            "phone": str(getattr(account, "phone", "") or ""),
            "project_id": str(getattr(account, "project_id", "") or ""),
            "country_code": str(getattr(account, "country_code", "") or ""),
            "country_prefix": str(getattr(account, "country_prefix", "") or ""),
            "provider_name": str(getattr(account, "provider_name", "") or ""),
        }

    @staticmethod
    def _resolve_country_code(account) -> str:
        extra = dict(getattr(account, "extra", {}) or {})
        metadata = dict(extra.get("metadata") or {}) if isinstance(extra.get("metadata"), dict) else {}
        for value in (
            getattr(account, "country_prefix", ""),
            metadata.get("country_qu", ""),
            getattr(account, "country_code", ""),
            metadata.get("country_code", ""),
        ):
            normalized = _normalize_country_code(str(value or ""), default="")
            if normalized:
                return normalized
        return "+86"

    def run(
        self,
        *,
        phone_provider,
        app_id: str = PORTAL_APP_ID,
        origin: str = BASE_URL,
        locale: str = "en",
        referral_code: str = "",
        referral_slug: str = "",
        create_api_key: bool = True,
        otp_timeout: int = 180,
        poll_interval: int = 15,
        code_pattern: str | None = None,
    ) -> dict:
        if phone_provider is None:
            raise RuntimeError("GetToken 手机号注册需要配置 phone_provider")

        self.log("[GetToken] 手机号协议链路：检查当前登录态")
        user_result = self.check_user()
        user = ((user_result.get("data") or {}) if isinstance(user_result, dict) else {}).get("user") or {}
        phone_account = None
        portal_result: dict[str, Any] = {}
        if not user:
            providers = self.fetch_login_providers(app_id=app_id, origin=origin)
            provider_items = providers.get("providers") if isinstance(providers, dict) else []
            if not any(str(item.get("channel") or "") == "PHONE_SMS" for item in provider_items if isinstance(item, dict)):
                raise RuntimeError("GetToken Portal 当前未开放 PHONE_SMS 登录渠道")

            self.log("[GetToken] 从手机号 provider 获取号码")
            phone_account = phone_provider.get_phone()
            phone_number = _normalize_china_phone(str(getattr(phone_account, "phone", "") or ""))
            if len(phone_number) != 11:
                raise RuntimeError(f"GetToken PHONE_SMS 需要 11 位中国大陆手机号，当前号码无效: {getattr(phone_account, 'phone', '')}")
            phone_country_code = self._resolve_country_code(phone_account)
            self.log(f"[GetToken] 发送短信验证码: {phone_number[:4]}****")
            attempt = self.create_sms_attempt(
                app_id=app_id,
                origin=origin,
                locale=locale,
                phone_country_code=phone_country_code,
                phone_number=phone_number,
            )
            if str(attempt.get("action") or "") != "sms_code":
                raise RuntimeError(f"GetToken Portal 未返回 sms_code action: {attempt}")
            attempt_id = str(attempt.get("attemptId") or "").strip()
            if not attempt_id:
                raise RuntimeError(f"GetToken Portal 未返回 attemptId: {attempt}")
            self.log("[GetToken] 等待短信验证码")
            code = phone_provider.wait_for_code(
                phone_account,
                timeout=int(otp_timeout or 180),
                poll_interval=int(poll_interval or 15),
                code_pattern=code_pattern,
            )
            if not code:
                raise RuntimeError("GetToken 手机号 provider 未返回短信验证码")
            completed = self.complete_sms_attempt(attempt_id=attempt_id, code=str(code).strip())
            portal_result = completed.get("result") if isinstance(completed.get("result"), dict) else completed
            login_token = str(portal_result.get("loginToken") or "").strip()
            if not login_token:
                raise RuntimeError(f"GetToken Portal SMS 未返回 loginToken: {completed}")
            login_result = self.portal_login(login_token=login_token, referral_code=referral_code, referral_slug=referral_slug)
            user = ((login_result.get("data") or {}) if isinstance(login_result, dict) else {}).get("user") or {}
            if not user:
                user = (((self.check_user()).get("data") or {}).get("user") or {})
        else:
            self.log("[GetToken] 已有登录态，直接提取 API Key")

        api_key, api_key_info = self.extract_or_create_api_key(create_api_key=create_api_key) if create_api_key else ("", {})
        if create_api_key and not api_key:
            raise RuntimeError("GetToken 手机号协议链路未找到或创建 API Key")
        phone_e164 = str(user.get("phoneE164") or portal_result.get("phoneE164") or "").strip()
        if not phone_e164 and phone_account is not None:
            phone_e164 = f"+86{_normalize_china_phone(str(getattr(phone_account, 'phone', '') or ''))}"
        return {
            "email": str(user.get("email") or phone_e164 or ""),
            "user_id": str(user.get("id") or ""),
            "api_key": api_key,
            "api_key_info": api_key_info,
            "account_info": user,
            "cookies": self.session.cookies.get_dict(),
            "session_cookie": "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.session.cookies),
            "request_trace": self.request_trace,
            "registration_note": "protocol_phone_sms",
            "phone": self._phone_metadata(phone_account) if phone_account is not None else {},
            "portal_result": {key: value for key, value in portal_result.items() if key != "loginToken"},
        }
