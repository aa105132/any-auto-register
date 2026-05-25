from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import html
import re
import time
from urllib.parse import urlparse


def _normalize_api_base_url(value: str | None, *, default: str, label: str) -> str:
    raw = str(value or "").strip() or default
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"{label} 无效: {value!r}")
    return raw.rstrip("/")


def _extract_response_message(payload) -> str:
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "detail", "description", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("errors", "data", "result", "payload"):
            nested = _extract_response_message(payload.get(key))
            if nested:
                return nested
        return ""
    if isinstance(payload, list):
        for item in payload:
            nested = _extract_response_message(item)
            if nested:
                return nested
        return ""
    if isinstance(payload, str):
        return payload.strip()
    return ""

@dataclass
class PhoneAccount:
    phone: str
    project_id: str = ""
    token: str = ""
    country_code: str = ""
    country_prefix: str = ""
    provider_name: str = ""
    extra: dict = field(default_factory=dict)


class BasePhoneProvider(ABC):
    @abstractmethod
    def get_phone(self) -> PhoneAccount:
        """获取一个可用手机号。"""
        ...

    @abstractmethod
    def wait_for_code(self, account: PhoneAccount, timeout: int = 180, poll_interval: int = 15, code_pattern: str | None = None) -> str:
        """等待并返回短信验证码。"""
        ...

    def release_phone(self, account: PhoneAccount) -> bool:
        return False

    def blacklist_phone(self, account: PhoneAccount) -> bool:
        return False


class HaozhuPhoneProvider(BasePhoneProvider):
    """豪猪接码平台适配器。"""

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        username: str = "",
        password: str = "",
        token: str = "",
        project_id: str = "",
        uid: str = "",
        author: str = "",
        poll_interval: int | str = 15,
        phone_timeout: int | str = 180,
        proxy: str | None = None,
    ):
        import requests

        self.api = _normalize_api_base_url(api_base_url, default="https://api.haozhuma.com", label="豪猪 API URL")
        self.username = str(username or "").strip()
        self.password = str(password or "").strip()
        self._token = str(token or "").strip()
        self.project_id = str(project_id or "").strip()
        self.uid = str(uid or "").strip()
        self.author = str(author or "").strip()
        self.poll_interval = _to_positive_int(poll_interval, 15)
        self.phone_timeout = _to_positive_int(phone_timeout, 180)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._session = requests.Session()
        self._session.proxies = self.proxy
        self._session.headers.update({
            "accept": "application/json, text/plain, */*",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        })

    def _request(self, params: dict) -> dict:
        response = self._session.get(f"{self.api}/sms/", params=params, timeout=30)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"豪猪响应不是有效 JSON: {response.text[:200]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"豪猪响应格式异常: {data!r}")
        return data

    @staticmethod
    def _is_success(payload: dict) -> bool:
        raw_code = payload.get("code", "")
        code = str(raw_code).strip().lower()
        return code in {"0", "200"}

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        if not self.username or not self.password:
            raise RuntimeError("豪猪未配置 token，也未配置 API 账号/密码")
        payload = self._request({"api": "login", "user": self.username, "pass": self.password})
        if not self._is_success(payload):
            raise RuntimeError(f"豪猪登录失败: {_extract_response_message(payload) or payload}")
        token = str(payload.get("token") or "").strip()
        if not token:
            raise RuntimeError(f"豪猪登录成功但未返回 token: {payload}")
        self._token = token
        return token

    def _ensure_project_id(self) -> str:
        project_id = str(self.project_id or "").strip()
        if not project_id:
            raise RuntimeError("豪猪未配置项目 ID / sid")
        return project_id

    def get_phone(self) -> PhoneAccount:
        token = self._ensure_token()
        project_id = self._ensure_project_id()
        params = {"api": "getPhone", "token": token, "sid": project_id}
        if self.uid:
            params["uid"] = self.uid
        if self.author:
            params["author"] = self.author
        payload = self._request(params)
        if not self._is_success(payload):
            raise RuntimeError(f"豪猪取号失败: {_extract_response_message(payload) or payload}")
        phone = str(payload.get("phone") or "").strip()
        if not phone:
            raise RuntimeError(f"豪猪取号成功但未返回手机号: {payload}")
        resolved_project_id = str(payload.get("sid") or project_id).strip()
        metadata = {
            "sid": resolved_project_id,
            "shop_name": payload.get("shop_name", ""),
            "country_name": payload.get("country_name", ""),
            "country_code": payload.get("country_code", ""),
            "country_qu": payload.get("country_qu", ""),
            "uid": payload.get("uid", ""),
            "sp": payload.get("sp", ""),
            "phone_gsd": payload.get("phone_gsd", ""),
            "api_base_url": self.api,
        }
        account = PhoneAccount(
            phone=phone,
            project_id=resolved_project_id,
            token=token,
            country_code=str(payload.get("country_code") or ""),
            country_prefix=str(payload.get("country_qu") or ""),
            provider_name="haozhu",
            extra={
                "provider_account": {
                    "provider_type": "phone",
                    "provider_name": "haozhu",
                    "login_identifier": self.username or "token",
                    "display_name": self.username or "豪猪 Token",
                    "credentials": {
                        "token": token,
                    },
                    "metadata": {
                        "api_base_url": self.api,
                    },
                },
                "provider_resource": {
                    "provider_type": "phone",
                    "provider_name": "haozhu",
                    "resource_type": "phone_number",
                    "resource_identifier": phone,
                    "handle": phone,
                    "display_name": phone,
                    "metadata": metadata,
                },
                "metadata": metadata,
            },
        )
        self._last_account = account
        return account

    def wait_for_code(self, account: PhoneAccount, timeout: int = 180, poll_interval: int = 15, code_pattern: str | None = None) -> str:
        token = str(account.token or "").strip() or self._ensure_token()
        project_id = str(account.project_id or "").strip() or self._ensure_project_id()
        phone = str(account.phone or "").strip()
        if not phone:
            raise RuntimeError("豪猪等待短信验证码时缺少手机号")
        deadline = time.time() + _to_positive_int(timeout, self.phone_timeout)
        interval = _to_positive_int(poll_interval, self.poll_interval)
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{4,8})(?!\d)")
        last_message = ""
        while time.time() < deadline:
            payload = self._request({"api": "getMessage", "token": token, "sid": project_id, "phone": phone})
            if self._is_success(payload):
                code = str(payload.get("yzm") or "").strip()
                if code:
                    return code
                text = html.unescape(str(payload.get("sms") or ""))
                match = pattern.search(text)
                if match:
                    return match.group(1) if match.groups() else match.group(0)
            last_message = _extract_response_message(payload) or str(payload)
            time.sleep(interval)
        raise TimeoutError(f"等待豪猪短信验证码超时 ({timeout}s): {last_message}")

    def release_phone(self, account: PhoneAccount) -> bool:
        return self._phone_action("cancelRecv", account)

    def blacklist_phone(self, account: PhoneAccount) -> bool:
        return self._phone_action("addBlacklist", account)

    def _phone_action(self, api_name: str, account: PhoneAccount) -> bool:
        token = str(account.token or "").strip() or self._ensure_token()
        project_id = str(account.project_id or "").strip() or self._ensure_project_id()
        phone = str(account.phone or "").strip()
        if not phone:
            return False
        payload = self._request({"api": api_name, "token": token, "sid": project_id, "phone": phone})
        return self._is_success(payload)



class QianchuanPhoneProvider(BasePhoneProvider):
    """千川接码平台适配器。"""

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        username: str = "",
        password: str = "",
        token: str = "",
        channel_id: str = "",
        phone_num: str = "",
        operator: str | int = "0",
        scope: str = "",
        poll_interval: int | str = 5,
        phone_timeout: int | str = 180,
        proxy: str | None = None,
    ):
        import requests

        self.api = _normalize_api_base_url(api_base_url, default="https://api.qc86.shop/api", label="千川 API URL")
        self.username = str(username or "").strip()
        self.password = str(password or "").strip()
        self._token = str(token or "").strip()
        self.channel_id = str(channel_id or "").strip()
        self.phone_num = str(phone_num or "").strip()
        self.operator = str(operator if operator is not None else "0").strip() or "0"
        self.scope = str(scope or "").strip()
        self.poll_interval = _to_positive_int(poll_interval, 5)
        self.phone_timeout = _to_positive_int(phone_timeout, 180)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._session = requests.Session()
        self._session.proxies = self.proxy
        self._session.headers.update({
            "accept": "application/json, text/plain, */*",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        })

    def _request(self, endpoint: str, params: dict) -> dict:
        response = self._session.get(f"{self.api}/{endpoint.lstrip('/')}", params=params, timeout=30)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"千川响应不是有效 JSON: {response.text[:200]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"千川响应格式异常: {data!r}")
        return data

    @staticmethod
    def _is_success(payload: dict) -> bool:
        return str(payload.get("status", "")).strip() == "200" or bool(payload.get("success"))

    def _ensure_token(self) -> str:
        if self._token:
            return self._token
        if not self.username or not self.password:
            raise RuntimeError("千川未配置 token，也未配置 API Username / API Password")
        payload = self._request("login", {"username": self.username, "password": self.password})
        if not self._is_success(payload):
            raise RuntimeError(f"千川登录失败: {_extract_response_message(payload) or payload}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        token = str(data.get("token") or payload.get("token") or "").strip()
        if not token:
            raise RuntimeError(f"千川登录成功但未返回 token: {payload}")
        self._token = token
        return token

    def _ensure_channel_id(self, account: PhoneAccount | None = None) -> str:
        channel_id = str((account.project_id if account else "") or self.channel_id or "").strip()
        if not channel_id:
            raise RuntimeError("千川未配置通道 ID / channelId")
        return channel_id

    def get_phone(self) -> PhoneAccount:
        token = self._ensure_token()
        channel_id = self._ensure_channel_id()
        params = {"token": token, "channelId": channel_id}
        if self.phone_num:
            params["phoneNum"] = self.phone_num
        if self.operator:
            params["operator"] = self.operator
        if self.scope:
            params["scope"] = self.scope
        payload = self._request("getPhone", params)
        if not self._is_success(payload):
            raise RuntimeError(f"千川取号失败: {_extract_response_message(payload) or payload}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        phone = str(data.get("mobile") or data.get("phoneNo") or data.get("phoneNum") or payload.get("mobile") or "").strip()
        if not phone:
            raise RuntimeError(f"千川取号成功但未返回手机号: {payload}")
        sms_task = data.get("smsTask") if isinstance(data.get("smsTask"), dict) else {}
        metadata = {
            "channel_id": channel_id,
            "phone_id": data.get("phoneId"),
            "refresh_time": data.get("refreshTime"),
            "sms_task": sms_task,
            "operator": self.operator,
            "scope": self.scope,
            "api_base_url": self.api,
        }
        account = PhoneAccount(
            phone=phone,
            project_id=channel_id,
            token=token,
            provider_name="qianchuan",
            extra={
                "provider_account": {
                    "provider_type": "phone",
                    "provider_name": "qianchuan",
                    "login_identifier": self.username or "token",
                    "display_name": self.username or "千川 Token",
                    "credentials": {"token": token},
                    "metadata": {"api_base_url": self.api},
                },
                "provider_resource": {
                    "provider_type": "phone",
                    "provider_name": "qianchuan",
                    "resource_type": "phone_number",
                    "resource_identifier": phone,
                    "handle": phone,
                    "display_name": phone,
                    "metadata": metadata,
                },
                "metadata": metadata,
            },
        )
        self._last_account = account
        return account

    def wait_for_code(self, account: PhoneAccount, timeout: int = 180, poll_interval: int = 5, code_pattern: str | None = None) -> str:
        token = str(account.token or "").strip() or self._ensure_token()
        channel_id = self._ensure_channel_id(account)
        phone = str(account.phone or "").strip()
        if not phone:
            raise RuntimeError("千川等待短信验证码时缺少手机号")
        deadline = time.time() + _to_positive_int(timeout, self.phone_timeout)
        interval = _to_positive_int(poll_interval, self.poll_interval)
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{4,8})(?!\d)")
        last_message = ""
        while time.time() < deadline:
            payload = self._request("getCode", {"token": token, "channelId": channel_id, "phoneNum": phone})
            if self._is_success(payload):
                data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                code = str(data.get("code") or payload.get("code") or "").strip()
                if code:
                    return code
                text = html.unescape(str(data.get("modle") or data.get("message") or payload.get("message") or ""))
                match = pattern.search(text)
                if match:
                    return match.group(1) if match.groups() else match.group(0)
            last_message = _extract_response_message(payload) or str(payload)
            time.sleep(interval)
        raise TimeoutError(f"等待千川短信验证码超时 ({timeout}s): {last_message}")

    def release_phone(self, account: PhoneAccount) -> bool:
        return self._phone_action("release", account, {"status": 2})

    def blacklist_phone(self, account: PhoneAccount) -> bool:
        return self._phone_action("phoneCollectAdd", account, {"type": 0})

    def _phone_action(self, endpoint: str, account: PhoneAccount, extra_params: dict) -> bool:
        token = str(account.token or "").strip() or self._ensure_token()
        channel_id = self._ensure_channel_id(account)
        phone = str(account.phone or "").strip()
        if not phone:
            return False
        params = {"token": token, "channelId": channel_id, "phoneNo": phone}
        params.update(extra_params)
        payload = self._request(endpoint, params)
        return self._is_success(payload)

def _to_positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return int(default)
    return parsed if parsed > 0 else int(default)



def _task_field_keys_from_definition(definition) -> set[str]:
    if not definition or not hasattr(definition, "get_fields"):
        return set()
    keys: set[str] = set()
    for field in definition.get_fields() or []:
        if field.get("category") == "task" and field.get("key"):
            keys.add(str(field.get("key")))
    return keys


def _prefer_task_overrides(resolved: dict, overrides: dict, task_field_keys: set[str]) -> dict:
    merged = dict(resolved or {})
    if task_field_keys:
        for key in task_field_keys:
            if key in merged and key not in overrides:
                merged.pop(key, None)
    merged.update(dict(overrides or {}))
    return merged


def _create_qianchuan(extra: dict, proxy: str | None) -> BasePhoneProvider:
    return QianchuanPhoneProvider(
        api_base_url=extra.get("qianchuan_api_base_url"),
        username=extra.get("qianchuan_username", ""),
        password=extra.get("qianchuan_password", ""),
        token=extra.get("qianchuan_token", ""),
        channel_id=extra.get("qianchuan_channel_id", extra.get("phone_project_id", "")),
        phone_num=extra.get("qianchuan_phone_num", ""),
        operator=extra.get("qianchuan_operator", "0"),
        scope=extra.get("qianchuan_scope", ""),
        poll_interval=extra.get("qianchuan_poll_interval", 5),
        phone_timeout=extra.get("qianchuan_phone_timeout", extra.get("phone_otp_timeout", 180)),
        proxy=proxy,
    )

def _create_haozhu(extra: dict, proxy: str | None) -> BasePhoneProvider:
    return HaozhuPhoneProvider(
        api_base_url=extra.get("haozhu_api_base_url"),
        username=extra.get("haozhu_username", ""),
        password=extra.get("haozhu_password", ""),
        token=extra.get("haozhu_token", ""),
        project_id=extra.get("haozhu_project_id", extra.get("phone_project_id", "")),
        uid=extra.get("haozhu_uid", ""),
        author=extra.get("haozhu_author", ""),
        poll_interval=extra.get("haozhu_poll_interval", 15),
        phone_timeout=extra.get("haozhu_phone_timeout", extra.get("phone_otp_timeout", 180)),
        proxy=proxy,
    )


PHONE_FACTORY_REGISTRY = {
    "haozhu_sms_api": _create_haozhu,
    "haozhu": _create_haozhu,
    "qianchuan_sms_api": _create_qianchuan,
    "qianchuan": _create_qianchuan,
}


def _get_provider_definitions_repository():
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    return ProviderDefinitionsRepository()


def _get_provider_settings_repository():
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    return ProviderSettingsRepository()


def create_phone_provider(provider: str, extra: dict | None = None, proxy: str | None = None) -> BasePhoneProvider:
    provider_key = str(provider or "haozhu").strip() or "haozhu"
    runtime_extra = dict(extra or {})
    definition_repo = _get_provider_definitions_repository()
    settings_repo = _get_provider_settings_repository()
    definition = definition_repo.get_by_key("phone", provider_key)
    task_field_keys = _task_field_keys_from_definition(definition)
    resolved_extra = settings_repo.resolve_runtime_settings("phone", provider_key, {})
    runtime_extra.update(_prefer_task_overrides(dict(resolved_extra or {}), extra or {}, task_field_keys))
    setting = settings_repo.get_by_key("phone", provider_key)
    auth_mode = str(getattr(setting, "auth_mode", "") or "").strip()
    if auth_mode:
        runtime_extra["phone_auth_mode"] = auth_mode
        runtime_extra[f"{provider_key}_auth_mode"] = auth_mode
    lookup_key = definition.driver_type if definition else provider_key
    factory = PHONE_FACTORY_REGISTRY.get(lookup_key)
    if not factory:
        raise ValueError(f"未知手机号 provider: {provider}")
    return factory(runtime_extra, proxy)
