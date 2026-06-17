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


def _digits_only(value: str) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _split_filter_values(value: str) -> list[str]:
    items = re.split(r"[\s,，;；|/]+", str(value or "").strip())
    return [_digits_only(item) for item in items if _digits_only(item)]


def _china_local_phone_digits(value: str) -> str:
    digits = _digits_only(value)
    if len(digits) > 11 and digits.startswith("86"):
        return digits[-11:]
    return digits


def _first_extra_value(extra: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(extra.get(key, "") or "").strip()
        if value:
            return value
    return ""


def _phone_filter_config(extra: dict) -> tuple[list[str], list[str], int]:
    exact = [
        _china_local_phone_digits(item)
        for item in _split_filter_values(_first_extra_value(extra, (
            "phone_number",
            "phone_target_number",
            "phone_fixed_number",
            "specified_phone",
            "target_phone",
        )))
    ]
    segments = _split_filter_values(_first_extra_value(extra, (
        "phone_segment",
        "phone_prefix",
        "phone_number_prefix",
        "phone_no_segment",
    )))
    attempts = _to_positive_int(
        _first_extra_value(extra, ("phone_filter_attempts", "phone_segment_attempts", "phone_retry_attempts")),
        5,
    )
    return exact, segments, attempts


def _first_segment_value(extra: dict, keys: tuple[str, ...]) -> str:
    return (_split_filter_values(_first_extra_value(extra, keys)) or [""])[0]


def _single_segment_value(extra: dict, keys: tuple[str, ...]) -> str:
    segments = _split_filter_values(_first_extra_value(extra, keys))
    return segments[0] if len(segments) == 1 else ""


class FilteredPhoneProvider(BasePhoneProvider):
    """通用手机号过滤包装器。

    用于所有手机号接码 provider：底层先正常取号，随后按指定手机号
    或号段校验。不匹配则尽量释放号码并重试，避免各平台重复实现。
    """

    def __init__(self, provider: BasePhoneProvider, *, exact_numbers: list[str], segments: list[str], attempts: int = 5):
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_exact_numbers", [_china_local_phone_digits(item) for item in (exact_numbers or []) if _digits_only(item)])
        object.__setattr__(self, "_segments", [_digits_only(item) for item in (segments or []) if _digits_only(item)])
        object.__setattr__(self, "_attempts", max(int(attempts or 1), 1))

    def __getattr__(self, name: str):
        return getattr(self._provider, name)

    def __setattr__(self, name: str, value) -> None:
        if name.startswith("_") or "_provider" not in self.__dict__:
            object.__setattr__(self, name, value)
            return
        setattr(self._provider, name, value)

    def get_phone(self) -> PhoneAccount:
        last_phone = ""
        for _ in range(self._attempts):
            account = self._provider.get_phone()
            last_phone = str(getattr(account, "phone", "") or "")
            if self._matches(last_phone):
                object.__setattr__(self, "_last_account", account)
                return account
            self.release_phone(account)
        raise RuntimeError(
            "手机号来源未取到符合条件的号码: "
            f"指定手机号={','.join(self._exact_numbers) or '-'} "
            f"指定号段={','.join(self._segments) or '-'} "
            f"最后号码={last_phone or '-'}"
        )

    def wait_for_code(self, account: PhoneAccount, timeout: int = 180, poll_interval: int = 15, code_pattern: str | None = None) -> str:
        return self._provider.wait_for_code(account, timeout=timeout, poll_interval=poll_interval, code_pattern=code_pattern)

    def release_phone(self, account: PhoneAccount) -> bool:
        try:
            return bool(self._provider.release_phone(account))
        except Exception:
            return False

    def blacklist_phone(self, account: PhoneAccount) -> bool:
        return bool(self._provider.blacklist_phone(account))

    def _matches(self, phone: str) -> bool:
        digits = _digits_only(phone)
        local = _china_local_phone_digits(phone)
        if self._exact_numbers:
            if not any(digits == item or local == item or digits.endswith(item) for item in self._exact_numbers):
                return False
        if self._segments:
            if not any(digits.startswith(item) or local.startswith(item) for item in self._segments):
                return False
        return True


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
        isp: str | int = "",
        province: str | int = "",
        ascription: str | int = "",
        paragraph: str | int = "",
        exclude: str = "",
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
        self.isp = str(isp or "").strip()
        self.province = str(province or "").strip()
        self.ascription = str(ascription or "").strip()
        self.paragraph = _digits_only(str(paragraph or "").strip())
        self.exclude = str(exclude or "").strip()
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
        if self.isp:
            params["isp"] = self.isp
        if self.province:
            params["Province"] = self.province
        if self.ascription:
            params["ascription"] = self.ascription
        if self.paragraph:
            params["paragraph"] = self.paragraph
        if self.exclude:
            params["exclude"] = self.exclude
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


class FiveSimPhoneProvider(BasePhoneProvider):
    """5sim 接码平台适配器。"""

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        api_token: str = "",
        country: str = "any",
        operator: str = "any",
        product: str = "",
        max_price: str | int | float = "",
        forwarding: str | int | bool = "",
        reuse: str | int | bool = "",
        poll_interval: int | str = 5,
        phone_timeout: int | str = 180,
        proxy: str | None = None,
    ):
        import requests

        self.api = _normalize_api_base_url(api_base_url, default="https://5sim.net", label="5sim API URL")
        self.api_token = str(api_token or "").strip()
        self.country = str(country or "any").strip().lower() or "any"
        self.operator = str(operator or "any").strip().lower() or "any"
        self.product = str(product or "").strip().lower()
        self.max_price = str(max_price or "").strip()
        self.forwarding = str(forwarding or "").strip()
        self.reuse = str(reuse or "").strip()
        self.poll_interval = _to_positive_int(poll_interval, 5)
        self.phone_timeout = _to_positive_int(phone_timeout, 180)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._session = requests.Session()
        self._session.proxies = self.proxy
        self._session.headers.update({
            "accept": "application/json",
            "user-agent": "any-auto-register/5sim-phone-provider",
        })
        if self.api_token:
            self._session.headers.update({"Authorization": f"Bearer {self.api_token}"})

    def _request(self, endpoint: str, params: dict | None = None) -> dict:
        if not self.api_token:
            raise RuntimeError("5sim 未配置 API Token")
        response = self._session.get(f"{self.api}/{endpoint.lstrip('/')}", params=params or {}, timeout=30)
        try:
            data = response.json()
        except Exception as exc:
            text = response.text.strip()
            if response.status_code >= 400:
                raise RuntimeError(f"5sim 请求失败 HTTP {response.status_code}: {text[:200]}") from exc
            raise RuntimeError(f"5sim 响应不是有效 JSON: {text[:200]}") from exc
        if response.status_code >= 400:
            raise RuntimeError(f"5sim 请求失败 HTTP {response.status_code}: {_extract_response_message(data) or data}")
        if not isinstance(data, dict):
            raise RuntimeError(f"5sim 响应格式异常: {data!r}")
        return data

    def _ensure_product(self) -> str:
        product = str(self.product or "").strip().lower()
        if not product:
            raise RuntimeError("5sim 未配置产品 / product，例如 openai、telegram")
        return product

    @staticmethod
    def _order_id_from_account(account: PhoneAccount) -> str:
        extra = dict(getattr(account, "extra", {}) or {})
        metadata = dict(extra.get("metadata") or {}) if isinstance(extra.get("metadata"), dict) else {}
        return str(metadata.get("order_id") or metadata.get("id") or "").strip()

    @staticmethod
    def _extract_sms_code(payload: dict, pattern: re.Pattern) -> str:
        sms_items = payload.get("sms")
        if not isinstance(sms_items, list):
            return ""
        for item in sms_items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if code:
                return code
            text = html.unescape(str(item.get("text") or item.get("message") or ""))
            match = pattern.search(text)
            if match:
                return match.group(1) if match.groups() else match.group(0)
        return ""

    def get_phone(self) -> PhoneAccount:
        product = self._ensure_product()
        endpoint = f"v1/user/buy/activation/{self.country}/{self.operator}/{product}"
        params = {}
        if self.max_price:
            params["maxPrice"] = self.max_price
        if self.forwarding:
            params["forwarding"] = self.forwarding
        if self.reuse:
            params["reuse"] = self.reuse
        payload = self._request(endpoint, params=params)
        order_id = str(payload.get("id") or "").strip()
        phone = str(payload.get("phone") or "").strip()
        if not order_id:
            raise RuntimeError(f"5sim 取号成功但未返回订单 ID: {payload}")
        if not phone:
            raise RuntimeError(f"5sim 取号成功但未返回手机号: {payload}")
        metadata = {
            "order_id": order_id,
            "country": payload.get("country") or self.country,
            "operator": payload.get("operator") or self.operator,
            "product": payload.get("product") or product,
            "price": payload.get("price"),
            "status": payload.get("status"),
            "expires": payload.get("expires"),
            "api_base_url": self.api,
        }
        account = PhoneAccount(
            phone=phone,
            project_id=product,
            token=self.api_token,
            country_code=str(payload.get("country") or self.country),
            provider_name="5sim",
            extra={
                "provider_account": {
                    "provider_type": "phone",
                    "provider_name": "5sim",
                    "login_identifier": "api_token",
                    "display_name": "5sim API Token",
                    "credentials": {"token": self.api_token},
                    "metadata": {"api_base_url": self.api},
                },
                "provider_resource": {
                    "provider_type": "phone",
                    "provider_name": "5sim",
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
        order_id = self._order_id_from_account(account)
        if not order_id:
            raise RuntimeError("5sim 等待短信验证码时缺少订单 ID")
        deadline = time.time() + _to_positive_int(timeout, self.phone_timeout)
        interval = _to_positive_int(poll_interval, self.poll_interval)
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{4,8})(?!\d)")
        last_message = ""
        while time.time() < deadline:
            payload = self._request(f"v1/user/check/{order_id}")
            code = self._extract_sms_code(payload, pattern)
            if code:
                return code
            last_message = _extract_response_message(payload) or str(payload.get("status") or payload)
            time.sleep(interval)
        raise TimeoutError(f"等待 5sim 短信验证码超时 ({timeout}s): {last_message}")

    def release_phone(self, account: PhoneAccount) -> bool:
        return self._order_action("cancel", account)

    def blacklist_phone(self, account: PhoneAccount) -> bool:
        return self._order_action("ban", account)

    def finish_phone(self, account: PhoneAccount) -> bool:
        return self._order_action("finish", account)

    def _order_action(self, action: str, account: PhoneAccount) -> bool:
        order_id = self._order_id_from_account(account)
        if not order_id:
            return False
        payload = self._request(f"v1/user/{action}/{order_id}")
        return bool(payload.get("id") or str(payload.get("status") or "").lower() in {"canceled", "cancelled", "finished", "banned"})

class ApiccPhoneProvider(BasePhoneProvider):
    """api.cc 免费公共短信平台适配器。

    免费号为公共号码（任何人可见短信），一个号可重复用于无限次注册。手机号需在配置里指定
    （apicc_phone_number / 通用 phone_number），本 provider 不“取号”，仅返回配置号并轮询其短信。
    数据源：GET {api}/home/index/getSmsRecords.html → {code,msg,data:[{id,from,to,msg,create_time}],last_id}
    接口返回全局最新短信（不支持按号码过滤），按 `to` 字段客户端过滤；用取号时记录的最大 id 作基线，
    只接受其后到达的新短信，避免读到上一次注册的旧验证码。
    """

    DEFAULT_SMS_PATH = "/home/index/getSmsRecords.html"

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        phone_number: str = "",
        country_code: str = "",
        sender: str = "",
        poll_interval: int | str = 5,
        phone_timeout: int | str = 180,
        proxy: str | None = None,
        log_fn=None,
    ):
        import requests

        self.api = _normalize_api_base_url(api_base_url, default="https://api.cc", label="api.cc URL")
        self.phone_number = _digits_only(phone_number)
        self.country_code = str(country_code or "").strip()
        # 发送方过滤（可选，可配多个）：公共号收多服务短信，按发送方锁定目标服务，更精准。
        self.senders = [s for s in re.split(r"[\s,，;；|]+", str(sender or "").strip()) if s]
        self.poll_interval = _to_positive_int(poll_interval, 5)
        self.phone_timeout = _to_positive_int(phone_timeout, 180)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self.log = log_fn if callable(log_fn) else (lambda _m: None)
        self._baseline_id = 0
        self._session = requests.Session()
        self._session.proxies = self.proxy
        self._session.headers.update({
            "accept": "application/json, text/javascript, */*; q=0.01",
            "x-requested-with": "XMLHttpRequest",
            "referer": f"{self.api}/home/index/free.html",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        })

    def _fetch_records(self) -> list[dict]:
        response = self._session.get(f"{self.api}{self.DEFAULT_SMS_PATH}", timeout=30)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"api.cc 响应不是有效 JSON: {response.text[:200]}") from exc
        rows = data.get("data") if isinstance(data, dict) else None
        return rows if isinstance(rows, list) else []

    def _sender_ok(self, rec: dict) -> bool:
        if not self.senders:
            return True
        frm = str(rec.get("from") or "")
        frm_l = frm.lower()
        frm_d = _digits_only(frm)
        for s in self.senders:
            sd = _digits_only(s)
            if sd and frm_d:
                if frm_d == sd or frm_d.endswith(sd) or sd.endswith(frm_d):
                    return True
            elif s.lower() in frm_l:
                return True
        return False

    def _match(self, rec: dict, number: str) -> bool:
        to = _digits_only(rec.get("to"))
        n = _digits_only(number)
        if not to or not n:
            return False
        if not (to == n or to.endswith(n) or n.endswith(to)):
            return False
        return self._sender_ok(rec)

    def _records_for(self, number: str) -> list[dict]:
        rows = self._fetch_records()
        mine = [r for r in rows if self._match(r, number)]
        mine.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
        return mine

    def get_phone(self) -> PhoneAccount:
        if not self.phone_number:
            raise RuntimeError("api.cc 未配置手机号（apicc_phone_number / phone_number）")
        # 基线：记录该号当前最大短信 id，wait_for_code 只接受其后到达的新短信。
        try:
            mine = self._records_for(self.phone_number)
            self._baseline_id = int(mine[0].get("id") or 0) if mine else 0
        except Exception:
            self._baseline_id = 0
        metadata = {
            "api_base_url": self.api,
            "baseline_id": self._baseline_id,
            "country_code": self.country_code,
        }
        account = PhoneAccount(
            phone=self.phone_number,
            project_id="apicc_free",
            country_code=self.country_code,
            provider_name="apicc",
            extra={
                "apicc_baseline_id": self._baseline_id,
                "provider_resource": {
                    "provider_type": "phone",
                    "provider_name": "apicc",
                    "resource_type": "phone_number",
                    "resource_identifier": self.phone_number,
                    "handle": self.phone_number,
                    "display_name": self.phone_number,
                    "metadata": metadata,
                },
                "metadata": metadata,
            },
        )
        self._last_account = account
        return account

    def wait_for_code(self, account: PhoneAccount, timeout: int = 180, poll_interval: int = 5, code_pattern: str | None = None) -> str:
        number = _digits_only(getattr(account, "phone", "")) or self.phone_number
        if not number:
            raise RuntimeError("api.cc 等待短信验证码时缺少手机号")
        extra = dict(getattr(account, "extra", {}) or {})
        try:
            baseline = int(extra.get("apicc_baseline_id") or self._baseline_id or 0)
        except (TypeError, ValueError):
            baseline = self._baseline_id or 0
        deadline = time.time() + _to_positive_int(timeout, self.phone_timeout)
        interval = _to_positive_int(poll_interval, self.poll_interval)
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{4,8})(?!\d)")
        last_seen = ""
        while time.time() < deadline:
            try:
                mine = self._records_for(number)
            except Exception as exc:
                last_seen = f"拉取失败: {exc}"
                time.sleep(interval)
                continue
            for rec in mine:  # newest first
                if int(rec.get("id") or 0) <= baseline:
                    break  # 已到基线，后面都是旧短信
                text = html.unescape(str(rec.get("msg") or ""))
                last_seen = text[:120]
                match = pattern.search(text)
                if match:
                    code = match.group(1) if match.groups() else match.group(0)
                    self.log(f"[api.cc] 命中验证码 {code} 收件号={number} 发送方={rec.get('from')!r} 短信={text[:80]!r}")
                    return code
            time.sleep(interval)
        raise TimeoutError(f"等待 api.cc 短信验证码超时 ({timeout}s) 号码={number}: {last_seen or '无新短信'}")

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
    exact_phone = _first_extra_value(extra, (
        "qianchuan_phone_num",
        "phone_number",
        "phone_target_number",
        "phone_fixed_number",
        "specified_phone",
        "target_phone",
    ))
    exact_phone = _china_local_phone_digits(exact_phone)
    return QianchuanPhoneProvider(
        api_base_url=extra.get("qianchuan_api_base_url"),
        username=extra.get("qianchuan_username", ""),
        password=extra.get("qianchuan_password", ""),
        token=extra.get("qianchuan_token", ""),
        channel_id=extra.get("qianchuan_channel_id", extra.get("phone_project_id", "")),
        phone_num=exact_phone,
        operator=extra.get("qianchuan_operator", "0"),
        scope=extra.get("qianchuan_scope", ""),
        poll_interval=extra.get("qianchuan_poll_interval", 5),
        phone_timeout=extra.get("qianchuan_phone_timeout", extra.get("phone_otp_timeout", 180)),
        proxy=proxy,
    )

def _create_haozhu(extra: dict, proxy: str | None) -> BasePhoneProvider:
    paragraph = _first_segment_value(extra, ("haozhu_paragraph",))
    if not paragraph:
        paragraph = _single_segment_value(extra, (
            "phone_segment",
            "phone_prefix",
            "phone_number_prefix",
            "phone_no_segment",
        ))
    if not paragraph:
        exact_phone = _china_local_phone_digits(_first_extra_value(extra, (
            "phone_number",
            "phone_target_number",
            "phone_fixed_number",
            "specified_phone",
            "target_phone",
        )))
        if len(exact_phone) >= 3:
            paragraph = exact_phone[:3]
    return HaozhuPhoneProvider(
        api_base_url=extra.get("haozhu_api_base_url"),
        username=extra.get("haozhu_username", ""),
        password=extra.get("haozhu_password", ""),
        token=extra.get("haozhu_token", ""),
        project_id=extra.get("haozhu_project_id", extra.get("phone_project_id", "")),
        uid=extra.get("haozhu_uid", ""),
        author=extra.get("haozhu_author", ""),
        isp=extra.get("haozhu_isp", ""),
        province=extra.get("haozhu_province", ""),
        ascription=extra.get("haozhu_ascription", ""),
        paragraph=paragraph,
        exclude=extra.get("haozhu_exclude", ""),
        poll_interval=extra.get("haozhu_poll_interval", 15),
        phone_timeout=extra.get("haozhu_phone_timeout", extra.get("phone_otp_timeout", 180)),
        proxy=proxy,
    )


def _create_5sim(extra: dict, proxy: str | None) -> BasePhoneProvider:
    return FiveSimPhoneProvider(
        api_base_url=extra.get("5sim_api_base_url"),
        api_token=extra.get("5sim_api_token", extra.get("5sim_token", "")),
        country=extra.get("5sim_country", "any"),
        operator=extra.get("5sim_operator", "any"),
        product=extra.get("5sim_product", extra.get("phone_project_id", "")),
        max_price=extra.get("5sim_max_price", ""),
        forwarding=extra.get("5sim_forwarding", ""),
        reuse=extra.get("5sim_reuse", ""),
        poll_interval=extra.get("5sim_poll_interval", extra.get("phone_poll_interval", 5)),
        phone_timeout=extra.get("5sim_phone_timeout", extra.get("phone_otp_timeout", 180)),
        proxy=proxy,
    )


def _create_apicc(extra: dict, proxy: str | None) -> BasePhoneProvider:
    phone_number = _first_extra_value(extra, (
        "apicc_phone_number",
        "phone_number",
        "phone_target_number",
        "phone_fixed_number",
        "specified_phone",
        "target_phone",
    ))
    return ApiccPhoneProvider(
        api_base_url=extra.get("apicc_api_base_url"),
        phone_number=phone_number,
        country_code=extra.get("apicc_country_code", extra.get("phone_country_code", "")),
        sender=_first_extra_value(extra, ("apicc_sender", "apicc_from", "phone_sender", "phone_sender_filter", "sms_sender")),
        poll_interval=extra.get("apicc_poll_interval", extra.get("phone_poll_interval", 5)),
        phone_timeout=extra.get("apicc_phone_timeout", extra.get("phone_otp_timeout", 180)),
        proxy=proxy if str(extra.get("apicc_use_proxy", "")).strip().lower() in {"1", "true", "yes", "on"} else None,
        log_fn=print,
    )


PHONE_FACTORY_REGISTRY = {
    "haozhu_sms_api": _create_haozhu,
    "haozhu": _create_haozhu,
    "qianchuan_sms_api": _create_qianchuan,
    "qianchuan": _create_qianchuan,
    "5sim_api": _create_5sim,
    "5sim": _create_5sim,
    "apicc_sms_api": _create_apicc,
    "apicc": _create_apicc,
    "apicc_free": _create_apicc,
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
    phone_provider = factory(runtime_extra, proxy)
    exact_numbers, segments, attempts = _phone_filter_config(runtime_extra)
    if exact_numbers or segments:
        phone_provider = FilteredPhoneProvider(
            phone_provider,
            exact_numbers=exact_numbers,
            segments=segments,
            attempts=attempts,
        )
    return phone_provider
