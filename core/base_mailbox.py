"""邮箱池基类 - 抽象临时邮箱/收件服务"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
import html
import ipaddress
import re
import socket
from typing import Callable
from urllib.parse import urlencode, urlparse


def _safe_print(*args, **kwargs):
    """print() wrapper that survives UnicodeEncodeError on Windows GBK consoles."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        print(text.encode("ascii", "replace").decode("ascii"), **kwargs)


@dataclass
class MailboxAccount:
    email: str
    account_id: str = ""
    extra: dict = None  # 平台额外信息


class BaseMailbox(ABC):
    @abstractmethod
    def get_email(self) -> MailboxAccount:
        """获取一个可用邮箱"""
        ...

    @abstractmethod
    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        """等待并返回验证码，code_pattern 为自定义正则（默认匹配6位数字）"""
        ...

    @abstractmethod
    def get_current_ids(self, account: MailboxAccount) -> set:
        """返回当前邮件 ID 集合（用于过滤旧邮件）"""
        ...

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        """等待并返回验证链接。默认由具体 provider 自行实现。"""
        raise NotImplementedError(f"{self.__class__.__name__} 暂不支持 wait_for_link()")



_STATIC_LINK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".map", ".woff", ".woff2")
_STATIC_LINK_PATH_HINTS = ("/logo/", "/logos/", "/asset/", "/assets/", "/_next/", "/static/", "/images/", "/img/")


def _looks_like_static_asset_link(url: str) -> bool:
    try:
        parsed = urlparse(html.unescape(str(url or "")))
    except Exception:
        return False
    path = str(parsed.path or "").lower()
    if any(path.endswith(suffix) for suffix in _STATIC_LINK_SUFFIXES):
        return True
    return any(hint in path for hint in _STATIC_LINK_PATH_HINTS)


def _extract_verification_link(text: str, keyword: str = "") -> str | None:
    combined = str(text or "")
    lowered = combined.lower()
    if keyword and keyword.lower() not in lowered:
        return None

    urls = []
    for raw in re.findall(r'https?://[^\s<>"\']+', combined, re.IGNORECASE):
        url = html.unescape(raw).rstrip(").,;'\"")
        if _looks_like_static_asset_link(url):
            continue
        urls.append(url)
    if not urls:
        return None

    primary_link_hints = ("verif", "confirm", "magic", "auth", "callback", "signin", "signup", "continue")
    primary_host_hints = ("tavily", "firecrawl", "clerk", "stytch", "auth", "login", "fireworks")
    for url in urls:
        url_lower = url.lower()
        if any(token in url_lower for token in primary_link_hints) and any(host in url_lower for host in primary_host_hints):
            return url

    verification_hints = ("verify", "verification", "confirm", "magic link", "sign in", "login", "auth", "tavily", "firecrawl")
    if not any(token in lowered for token in verification_hints):
        return None

    blocked_hosts = {"w3.org", "www.w3.org"}
    for url in urls:
        url_lower = url.lower()
        try:
            host = str(urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if host in blocked_hosts:
            continue
        if any(token in url_lower for token in primary_link_hints):
            return url

    for url in urls:
        try:
            host = str(urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if host not in blocked_hosts:
            return url
    return None


def _normalize_api_base_url(value: str | None, *, default: str, label: str) -> str:
    raw = str(value or "").strip() or default
    if "://" not in raw:
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"{label} 无效: {value!r}")
    return raw.rstrip("/")


def _normalize_bool_flag(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on", "enabled", "accepted", "agree", "agreed"}:
        return True
    if raw in {"0", "false", "no", "off", "disabled", ""}:
        return False
    return default


def _normalize_mailbox_proxy_mode(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"direct", "none", "bypass", "off", "disabled"}:
        return "direct"
    if raw in {"inherit", "proxy", "on", "enabled"}:
        return "inherit"
    return "auto"


def _host_is_internal(host: str) -> bool:
    value = str(host or "").strip().strip("[]")
    if not value:
        return False
    if value.lower() == "localhost":
        return True

    def _ip_is_internal(candidate: str) -> bool:
        try:
            ip = ipaddress.ip_address(candidate)
        except ValueError:
            return False
        return ip.is_private or ip.is_loopback or ip.is_link_local or not ip.is_global

    if _ip_is_internal(value):
        return True

    try:
        infos = socket.getaddrinfo(value, None)
    except OSError:
        return False

    ips: list[str] = []
    for item in infos:
        sockaddr = item[4] if len(item) > 4 else ()
        if not sockaddr:
            continue
        ip = str(sockaddr[0] or "").strip()
        if ip and ip not in ips:
            ips.append(ip)
    return bool(ips) and all(_ip_is_internal(ip) for ip in ips)


def _resolve_mailbox_proxy(
    runtime_extra: dict,
    proxy: str | None,
    *,
    provider_key: str = "",
    driver_type: str = "",
) -> str | None:
    if not proxy:
        return None
    mode = _normalize_mailbox_proxy_mode(runtime_extra.get("mailbox_proxy_mode"))
    if mode == "direct":
        return None
    if mode == "inherit":
        return proxy

    provider_names = {str(provider_key or "").strip(), str(driver_type or "").strip()}
    if provider_names & {"outlook_token", "outlook_token_imap"}:
        return None

    for key, raw_value in dict(runtime_extra or {}).items():
        if not str(key).endswith(("_api_url", "_provider_url")):
            continue
        value = str(raw_value or "").strip()
        if not value:
            continue
        if "://" not in value:
            value = f"https://{value.lstrip('/')}"
        parsed = urlparse(value)
        if parsed.hostname and _host_is_internal(parsed.hostname):
            return None
    return proxy


def _extract_response_message(payload) -> str:
    if isinstance(payload, dict):
        for key in ("message", "msg", "error", "detail", "description", "reason"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("errors", "data", "result", "payload"):
            value = payload.get(key)
            nested = _extract_response_message(value)
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


def _flatten_mail_payload(payload) -> dict:
    if not isinstance(payload, dict):
        return {}
    flattened = dict(payload)
    for key in ("data", "result", "payload", "mail", "email", "message", "item"):
        nested = flattened.get(key)
        if not isinstance(nested, dict):
            continue
        merged = dict(nested)
        for outer_key, outer_value in flattened.items():
            if outer_key in {"data", "result", "payload", "mail", "email", "message", "item"}:
                continue
            merged.setdefault(outer_key, outer_value)
        flattened = merged
        break
    return flattened


def _extract_mail_items(payload, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_mail_items(value, *keys)
            if nested:
                return nested

    for key in ("data", "result", "payload"):
        value = payload.get(key)
        if isinstance(value, (dict, list)):
            nested = _extract_mail_items(value, *keys)
            if nested or isinstance(value, list):
                return nested
    return []


def _normalize_generic_mail_message(payload) -> dict:
    data = _flatten_mail_payload(payload)
    message_id = (
        data.get("id")
        or data.get("message_id")
        or data.get("messageId")
        or data.get("mail_id")
        or data.get("email_id")
        or data.get("_id")
    )
    subject = data.get("subject") or data.get("title") or data.get("name") or ""
    body = data.get("body") or data.get("content") or data.get("text") or data.get("body_text") or ""
    body_text = data.get("body_text") or data.get("text") or body
    body_html = data.get("body_html") or data.get("html") or data.get("html_body") or data.get("content_html") or ""
    raw = data.get("raw") or data.get("source") or ""
    from_value = (
        data.get("from")
        or data.get("sender")
        or data.get("from_address")
        or data.get("fromEmail")
        or data.get("from_email")
        or ""
    )
    created_at = (
        data.get("created_at")
        or data.get("createdAt")
        or data.get("received_at")
        or data.get("receivedAt")
        or data.get("date")
        or data.get("timestamp")
        or data.get("time")
        or ""
    )
    return {
        "id": str(message_id) if message_id not in (None, "") else "",
        "subject": str(subject or ""),
        "from": str(from_value or ""),
        "body": str(body or ""),
        "body_text": str(body_text or ""),
        "body_html": str(body_html or ""),
        "raw": str(raw or ""),
        "created_at": created_at,
    }


def _compose_mail_text(*messages) -> str:
    parts: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            for key in ("from", "subject", "body", "body_text", "body_html", "content", "text", "html", "raw"):
                value = message.get(key)
                if value not in (None, ""):
                    parts.append(str(value))
        elif message not in (None, ""):
            parts.append(str(message))
    combined = html.unescape(" ".join(parts))
    combined = combined.replace("=\r\n", "").replace("=\n", "")
    combined = re.sub(r"<style\b.*?>.*?</style>", " ", combined, flags=re.IGNORECASE | re.DOTALL)
    combined = re.sub(r"<script\b.*?>.*?</script>", " ", combined, flags=re.IGNORECASE | re.DOTALL)
    combined = re.sub(r"<[^>]+>", " ", combined)
    return re.sub(r"\s+", " ", combined).strip()


def _compose_mail_link_source(*messages) -> str:
    parts: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            for key in ("from", "subject", "body", "body_text", "body_html", "content", "text", "html", "raw"):
                value = message.get(key)
                if value not in (None, ""):
                    parts.append(str(value))
        elif message not in (None, ""):
            parts.append(str(message))
    combined = html.unescape(" ".join(parts)).replace("=\r\n", "").replace("=\n", "")
    stripped = re.sub(r"<[^>]+>", " ", combined)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return f"{combined} {stripped}"


def _create_tempmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return TempMailLolMailbox(proxy=proxy)


def _create_duckmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return DuckMailMailbox(
        api_url=extra.get("duckmail_api_url", "https://www.duckmail.sbs"),
        provider_url=extra.get("duckmail_provider_url", "https://api.duckmail.sbs"),
        bearer=extra.get("duckmail_bearer", "kevin273945"),
        proxy=proxy,
    )


def _create_freemail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return FreemailMailbox(
        api_url=extra.get("freemail_api_url", ""),
        admin_token=extra.get("freemail_admin_token", ""),
        username=extra.get("freemail_username", ""),
        password=extra.get("freemail_password", ""),
        proxy=proxy,
    )


def _create_moemail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return MoeMailMailbox(
        api_url=extra.get("moemail_api_url"),
        username=extra.get("moemail_username", ""),
        password=extra.get("moemail_password", ""),
        session_token=extra.get("moemail_session_token", ""),
        proxy=proxy,
    )


def _create_cfworker(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return CFWorkerMailbox(
        api_url=extra.get("cfworker_api_url", ""),
        auth_mode=extra.get("cfworker_auth_mode", extra.get("mailbox_auth_mode", "admin_token")),
        admin_token=extra.get("cfworker_admin_token", ""),
        domain=extra.get("cfworker_domain", ""),
        fingerprint=extra.get("cfworker_fingerprint", ""),
        proxy=proxy,
    )


def _create_laoudo(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return LaoudoMailbox(
        auth_token=extra.get("laoudo_auth", ""),
        email=extra.get("laoudo_email", ""),
        account_id=extra.get("laoudo_account_id", ""),
    )


def _create_luckmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return LuckMailMailbox(
        api_base_url=extra.get("luckmail_api_base_url"),
        email=extra.get("luckmail_email", ""),
        purchase_token=extra.get("luckmail_purchase_token", ""),
        proxy=proxy,
    )


def _create_outlook_token(extra: dict, proxy: str | None) -> 'BaseMailbox':
    from datetime import datetime, timezone

    inventory = dict(extra.get("_inventory") or {})
    inventory_id = int(inventory.get("id", 0) or 0)
    token_update_hook: Callable[[str], None] | None = None
    if inventory_id > 0:
        def _persist_refresh_token(token: str) -> None:
            from sqlmodel import Session
            from core.db import MailboxInventoryModel, engine

            with Session(engine) as session:
                item = session.get(MailboxInventoryModel, inventory_id)
                if not item:
                    return
                metadata = item.get_metadata()
                metadata["client_id"] = str(extra.get("outlook_client_id", "") or metadata.get("client_id") or "")
                metadata["password"] = str(extra.get("outlook_password", "") or metadata.get("password") or "")
                metadata["refresh_token_updated_at"] = datetime.now(timezone.utc).isoformat()
                item.purchase_token = str(token or "")
                item.set_metadata(metadata)
                session.add(item)
                session.commit()

        token_update_hook = _persist_refresh_token
    return OutlookTokenMailbox(
        email=extra.get("outlook_email", ""),
        password=extra.get("outlook_password", ""),
        client_id=extra.get("outlook_client_id", ""),
        refresh_token=extra.get("outlook_refresh_token", ""),
        proxy=proxy,
        token_update_hook=token_update_hook,
        registration_email=(
            extra.get("outlook_registration_email", "")
            or extra.get("outlook_alias_email", "")
        ),
        alias_parent_email=extra.get("outlook_alias_parent_email", ""),
    )


def _create_yyds_mail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return YydsMailMailbox(
        api_base_url=extra.get("yyds_mail_api_base_url"),
        api_key=extra.get("yyds_mail_api_key", ""),
        prefix=extra.get("yyds_mail_prefix", ""),
        domain=extra.get("yyds_mail_domain", ""),
        email=extra.get("yyds_mail_email", ""),
        proxy=proxy,
    )


def _create_hstockplus_google(extra: dict, proxy: str | None) -> 'BaseMailbox':
    provider = HStockPlusGoogleAccountProvider(
        api_base_url=extra.get("hstockplus_api_url"),
        api_key=extra.get("hstockplus_api_key", ""),
        service_id=extra.get("hstockplus_google_service_id", extra.get("hstockplus_service_id", "")),
        quantity=extra.get("hstockplus_quantity", 1),
        link=extra.get("hstockplus_link", ""),
        delivery_timeout=extra.get("hstockplus_delivery_timeout", 600),
        poll_interval=extra.get("hstockplus_poll_interval", 5),
        request_timeout=extra.get("hstockplus_request_timeout", 90),
        enterprise_contract_required=extra.get("hstockplus_enterprise_contract_required", False),
        enterprise_contract_accepted=extra.get("hstockplus_enterprise_contract_accepted", False),
        proxy=proxy,
        reuse_mode=extra.get("hstockplus_reuse_mode", False),
        reuse_email=extra.get("hstockplus_reuse_email", ""),
    )
    # 注入当前平台名，用于 Google 账号池复用
    provider._target_platform = str(extra.get("platform") or extra.get("platform_name") or "").strip().lower()
    return provider


def _create_gptmail(extra: dict, proxy: str | None) -> 'BaseMailbox':
    return GptMailMailbox(
        api_base_url=extra.get("gptmail_api_base_url"),
        api_key=extra.get("gptmail_api_key", ""),
        prefix=extra.get("gptmail_prefix", ""),
        domain=extra.get("gptmail_domain", ""),
        email=extra.get("gptmail_email", ""),
        proxy=proxy,
    )


MAILBOX_FACTORY_REGISTRY = {
    "tempmail_lol_api": _create_tempmail,
    "duckmail_api": _create_duckmail,
    "freemail_api": _create_freemail,
    "moemail_api": _create_moemail,
    "cfworker_admin_api": _create_cfworker,
    "laoudo_api": _create_laoudo,
    "luckmail_token_query": _create_luckmail,
    "outlook_token_imap": _create_outlook_token,
    "yyds_mail_api": _create_yyds_mail,
    "gptmail_api": _create_gptmail,
    "hstockplus_google_account": _create_hstockplus_google,
    # backward-compat fallback
    "tempmail_lol": _create_tempmail,
    "duckmail": _create_duckmail,
    "freemail": _create_freemail,
    "moemail": _create_moemail,
    "cfworker": _create_cfworker,
    "laoudo": _create_laoudo,
    "luckmail": _create_luckmail,
    "outlook_token": _create_outlook_token,
    "yyds_mail": _create_yyds_mail,
    "gptmail": _create_gptmail,
    "hstockplus_google": _create_hstockplus_google,
}


def _get_provider_definitions_repository():
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository

    return ProviderDefinitionsRepository()


def _get_provider_settings_repository():
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    return ProviderSettingsRepository()


def create_mailbox(provider: str, extra: dict = None, proxy: str = None) -> 'BaseMailbox':
    """工厂方法：根据 provider 创建对应的 mailbox 实例"""
    provider_key = str(provider or "moemail")
    runtime_extra = dict(extra or {})
    definition_repo = _get_provider_definitions_repository()
    settings_repo = _get_provider_settings_repository()
    definition = definition_repo.get_by_key("mailbox", provider_key)
    resolved_extra = settings_repo.resolve_runtime_settings("mailbox", provider_key, extra or {})
    runtime_extra.update(dict(resolved_extra or {}))
    setting = settings_repo.get_by_key("mailbox", provider_key)
    auth_mode = str(getattr(setting, "auth_mode", "") or "").strip()
    if auth_mode:
        runtime_extra["mailbox_auth_mode"] = auth_mode
        runtime_extra[f"{provider_key}_auth_mode"] = auth_mode
    lookup_key = definition.driver_type if definition else provider_key
    factory = MAILBOX_FACTORY_REGISTRY.get(lookup_key, _create_laoudo)
    mailbox_proxy = _resolve_mailbox_proxy(
        runtime_extra,
        proxy,
        provider_key=provider_key,
        driver_type=lookup_key,
    )
    return factory(runtime_extra, mailbox_proxy)


class LaoudoMailbox(BaseMailbox):
    """laoudo.com 邮箱服务"""
    def __init__(self, auth_token: str, email: str, account_id: str):
        self.auth = auth_token
        self._email = email
        self._account_id = account_id
        self.api = "https://laoudo.com/api/email"
        self._ua = "Mozilla/5.0"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(
            email=self._email,
            account_id=self._account_id,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "laoudo",
                    "login_identifier": self._email,
                    "display_name": self._email,
                    "credentials": {
                        "authorization": self.auth,
                    },
                    "metadata": {
                        "account_id": self._account_id,
                        "email": self._email,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "laoudo",
                    "resource_type": "mailbox",
                    "resource_identifier": self._account_id,
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "account_id": self._account_id,
                        "email": self._email,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        from curl_cffi import requests as curl_requests
        try:
            r = curl_requests.get(
                f"{self.api}/list",
                params={"accountId": account.account_id, "allReceive": 0,
                        "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                headers={"authorization": self.auth, "user-agent": self._ua},
                timeout=15, impersonate="chrome131"
            )
            if r.status_code == 200:
                mails = r.json().get("data", {}).get("list", []) or []
                return {m.get("id") or m.get("emailId") for m in mails if m.get("id") or m.get("emailId")}
        except Exception:
            pass
        return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "trae",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        from curl_cffi import requests as curl_requests
        seen = set(before_ids) if before_ids else set()
        start = time.time()
        h = {"authorization": self.auth, "user-agent": self._ua}
        while time.time() - start < timeout:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={"accountId": account.account_id, "allReceive": 0,
                            "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                    headers=h, timeout=15, impersonate="chrome131"
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (str(mail.get("subject", "")) + " " +
                                str(mail.get("content") or mail.get("html") or ""))
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                        if m:
                            return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(4)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        from curl_cffi import requests as curl_requests
        seen = set(before_ids or [])
        start = time.time()
        headers = {"authorization": self.auth, "user-agent": self._ua}
        while time.time() - start < timeout:
            try:
                r = curl_requests.get(
                    f"{self.api}/list",
                    params={"accountId": account.account_id, "allReceive": 0,
                            "emailId": 0, "timeSort": 1, "size": 50, "type": 0},
                    headers=headers, timeout=15, impersonate="chrome131"
                )
                if r.status_code == 200:
                    mails = r.json().get("data", {}).get("list", []) or []
                    for mail in mails:
                        mid = mail.get("id") or mail.get("emailId")
                        if not mid or mid in seen:
                            continue
                        seen.add(mid)
                        text = (str(mail.get("subject", "")) + " " +
                                str(mail.get("content") or mail.get("html") or ""))
                        link = _extract_verification_link(text, keyword)
                        if link:
                            return link
            except Exception:
                pass
            time.sleep(4)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class LuckMailMailbox(BaseMailbox):
    """LuckMail 已购邮箱 token 查询模式。"""
    _OTP_REUSE_WINDOW_SECONDS = 600
    _TOKEN_POLL_INTERVAL_SECONDS = 3.5

    def __init__(self, api_base_url: str | None = None, email: str = "", purchase_token: str = "", proxy: str | None = None):
        import requests

        self.api = _normalize_api_base_url(api_base_url, default="https://mails.luckyous.com", label="LuckMail API URL")
        self._email = str(email or "").strip()
        self._purchase_token = str(purchase_token or "").strip()
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._session = requests.Session()
        self._session.proxies = self.proxy
        self._session.headers.update({
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
        })

    def _ensure_token(self) -> str:
        if not self._purchase_token:
            raise RuntimeError("LuckMail 未配置 purchase token")
        return self._purchase_token

    def _request_data(self, path: str):
        token = self._ensure_token()
        response = self._session.get(f"{self.api}{path.format(token=token)}", timeout=15)
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError("LuckMail 响应不是有效 JSON") from exc
        if response.status_code >= 400:
            message = ""
            if isinstance(data, dict):
                message = str(data.get("message") or "").strip()
            suffix = f": {message}" if message else ""
            raise RuntimeError(f"LuckMail 请求失败: HTTP {response.status_code}{suffix}")
        if isinstance(data, dict) and int(data.get("code", 0) or 0) != 0:
            raise RuntimeError(f"LuckMail 请求失败: {data.get('message') or 'unknown error'}")
        if isinstance(data, dict):
            return data.get("data")
        return data

    def _request_json(self, path: str) -> dict:
        data = self._request_data(path)
        return dict(data or {}) if isinstance(data, dict) else {}

    @staticmethod
    def _find_six_digit_code(value) -> str:
        if value in (None, ""):
            return ""
        text = html.unescape(str(value))
        match = re.search(r"(?<!#)(?<!\d)(\d{6})(?!\d)", text)
        return match.group(1) if match else ""

    def _extract_code_value(self, payload) -> str:
        if isinstance(payload, dict):
            for key in ("verification_code", "code", "otp", "email_code"):
                code = self._find_six_digit_code(payload.get(key))
                if code:
                    return code
            for key in ("body", "body_text", "html_body", "body_html", "subject", "content", "html", "text"):
                code = self._find_six_digit_code(payload.get(key))
                if code:
                    return code
            for key in ("data", "result", "value"):
                if key in payload:
                    code = self._extract_code_value(payload.get(key))
                    if code:
                        return code
            return ""
        if isinstance(payload, list):
            for item in payload:
                code = self._extract_code_value(item)
                if code:
                    return code
            return ""
        return self._find_six_digit_code(payload)

    def _normalize_mail(self, payload: dict) -> dict:
        mail = dict(payload or {})
        if not mail:
            return {}
        message_id = mail.get("message_id") or mail.get("id") or mail.get("mail_id") or mail.get("email_id")
        if message_id:
            mail["message_id"] = str(message_id)
        if not mail.get("body"):
            mail["body"] = mail.get("body_text") or mail.get("content") or mail.get("text") or ""
        if not mail.get("html_body"):
            mail["html_body"] = mail.get("body_html") or mail.get("html_body") or mail.get("html") or ""
        if not mail.get("from"):
            mail["from"] = mail.get("sender") or mail.get("from_address") or mail.get("fromEmail") or ""
        if not mail.get("subject"):
            mail["subject"] = mail.get("title") or ""
        if not mail.get("received_at"):
            for key in ("created_at", "date", "timestamp", "time"):
                if mail.get(key):
                    mail["received_at"] = mail.get(key)
                    break
        if not mail.get("verification_code"):
            code = self._extract_code_value(mail)
            if code:
                mail["verification_code"] = code
        return mail

    def _extract_email_from_payload(self, payload) -> str:
        if isinstance(payload, dict):
            for key in ("email_address", "email", "address", "mailbox"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
                if isinstance(value, dict):
                    nested = self._extract_email_from_payload(value)
                    if nested:
                        return nested
        return ""

    def _normalize_inbox_payload(self, payload) -> dict:
        email = self._extract_email_from_payload(payload) or self._email
        if isinstance(payload, dict):
            raw_mails = payload.get("mails") or payload.get("list") or payload.get("items") or []
        elif isinstance(payload, list):
            raw_mails = payload
        else:
            raw_mails = []
        mails = []
        for item in raw_mails:
            if isinstance(item, dict):
                normalized = self._normalize_mail(item)
                if normalized:
                    mails.append(normalized)
        if email:
            self._email = email
        return {
            "email_address": email,
            "mails": mails,
        }

    def _query_openapi_code_payload(self) -> dict:
        payload = self._request_json('/api/v1/openapi/email/token/{token}/code')
        mail = self._normalize_mail(dict(payload.get("mail") or {}))
        if mail:
            payload["mail"] = mail
        code = self._extract_code_value(payload)
        if code:
            payload["verification_code"] = code
        return payload

    def _query_openapi_code(self) -> str:
        return self._extract_code_value(self._query_openapi_code_payload())

    def _query_openapi_inbox(self) -> dict:
        return self._normalize_inbox_payload(
            self._request_data('/api/v1/openapi/email/token/{token}/mails')
        )

    def _query_openapi_detail(self, message_id: str) -> dict:
        return self._normalize_mail(
            self._request_json(f'/api/v1/openapi/email/token/{{token}}/mails/{message_id}')
        )

    def _query_legacy_inbox(self) -> dict:
        return self._normalize_inbox_payload(
            self._request_json('/api/v1/email/query/{token}')
        )

    def _query_legacy_detail(self, message_id: str) -> dict:
        return self._normalize_mail(
            self._request_json(f'/api/v1/email/query/{{token}}/detail/{message_id}')
        )

    def _query_inbox(self) -> dict:
        last_error = None
        for fetcher in (self._query_legacy_inbox, self._query_openapi_inbox):
            try:
                data = fetcher()
                if data.get('mails') or data.get('email_address'):
                    return data
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return {'email_address': self._email, 'mails': []}

    def _query_detail(self, message_id: str) -> dict:
        last_error = None
        for fetcher in (self._query_legacy_detail, self._query_openapi_detail):
            try:
                data = fetcher(message_id)
                if data:
                    return data
            except Exception as exc:
                last_error = exc
        if last_error:
            raise last_error
        return {}

    @staticmethod
    def _is_rate_limited_error(error: Exception | str | None) -> bool:
        text = str(error or "").lower()
        return "http 429" in text or "请求过于频繁" in str(error or "") or "rate limit" in text

    def _message_text(self, mail: dict, detail: dict | None = None) -> str:
        detail = detail or {}
        parts = [
            str(mail.get("from", "")),
            str(mail.get("subject", "")),
            str(mail.get("verification_code", "")),
            str(mail.get("body", "")),
            str(mail.get("html_body", "")),
            str(mail.get("content", "")),
            str(mail.get("html", "")),
            str(mail.get("text", "")),
            str(detail.get("body", "")),
            str(detail.get("html_body", "")),
            str(detail.get("subject", "")),
            str(detail.get("verification_code", "")),
            str(detail.get("content", "")),
            str(detail.get("html", "")),
            str(detail.get("text", "")),
        ]
        return html.unescape(" ".join(part for part in parts if part))

    @staticmethod
    def _mail_timestamp(mail: dict) -> float | None:
        from datetime import datetime, timezone

        for key in ("received_at", "created_at", "date", "timestamp", "time"):
            value = mail.get(key)
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)):
                ts = float(value)
                return ts / 1000.0 if ts > 1_000_000_000_000 else ts
            raw = str(value).strip()
            if not raw:
                continue
            if raw.isdigit():
                ts = float(raw)
                return ts / 1000.0 if ts > 1_000_000_000_000 else ts
            normalized = raw.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except ValueError:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                    try:
                        parsed = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                        return parsed.timestamp()
                    except ValueError:
                        continue
        return None

    def get_email(self) -> MailboxAccount:
        inbox = self._query_inbox()
        email = self._email or str(inbox.get("email_address", "") or "").strip()
        if not email:
            raise RuntimeError("LuckMail 查询成功，但未返回邮箱地址")
        return MailboxAccount(
            email=email,
            account_id=self._purchase_token,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "luckmail",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": {
                        "purchase_token": self._purchase_token,
                    },
                    "metadata": {
                        "email": email,
                        "api_base_url": self.api,
                        "source": "luckmail_purchase",
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "luckmail",
                    "resource_type": "mailbox",
                    "resource_identifier": self._purchase_token,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "purchase_token": self._purchase_token,
                        "api_base_url": self.api,
                        "source": "luckmail_purchase",
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            inbox = self._query_inbox()
            return {
                str(item.get("message_id", ""))
                for item in list(inbox.get("mails") or [])
                if item.get("message_id")
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        otp_sent_at: float | None = None,
        strict_otp_sent_at: bool = False,
        debug_callback=None,
    ) -> str:
        import re
        import time

        def _list_level_text(mail: dict) -> str:
            text = " ".join(
                str(part or "")
                for part in (
                    mail.get("from", ""),
                    mail.get("subject", ""),
                    mail.get("verification_code", ""),
                    mail.get("body", ""),
                    mail.get("html_body", ""),
                    mail.get("content", ""),
                    mail.get("html", ""),
                    mail.get("text", ""),
                )
            )
            text = html.unescape(text)
            text = re.sub(r"<[^>]+>", " ", text)
            return re.sub(r"\s+", " ", text).strip()

        seen = set(before_ids or [])
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)")
        start = time.time()
        last_rate_limit_error = ""
        while time.time() - start < timeout:
            try:
                latest_payload = self._query_openapi_code_payload()
                latest_mail = dict(latest_payload.get("mail") or {})
                latest_message_id = str(latest_mail.get("message_id", "") or "")
                latest_code = str(latest_payload.get("verification_code", "") or "").strip()
                latest_ts = self._mail_timestamp(latest_mail)
                if latest_code and latest_message_id:
                    latest_ts_ok = (
                        (not otp_sent_at)
                        or (
                            latest_ts is not None
                            and latest_ts + 1 >= float(otp_sent_at)
                        )
                        or (
                            not strict_otp_sent_at
                            and latest_ts is None
                        )
                    )
                    if latest_ts_ok:
                        if latest_message_id not in seen:
                            if callable(debug_callback):
                                debug_callback(
                                    "LuckMail /code ??????: "
                                    f"id={latest_message_id}, "
                                    f"received_at={latest_mail.get('received_at', '')}, "
                                    f"code={latest_code}"
                                )
                            return latest_code
                inbox = self._query_inbox()
                mails = list(inbox.get("mails") or [])
                sorted_mails = sorted(mails, key=lambda item: str(item.get("received_at", "")), reverse=True)
                for mail in sorted_mails:
                    try:
                        message_id = str(mail.get("message_id", "") or "")
                        if not message_id or message_id in seen:
                            continue
                        mail_ts = self._mail_timestamp(mail)
                        if otp_sent_at and (
                            (mail_ts is not None and mail_ts + 1 < float(otp_sent_at))
                            or (strict_otp_sent_at and mail_ts is None)
                        ):
                            seen.add(message_id)
                            continue
                        seen.add(message_id)
                        if callable(debug_callback):
                            debug_callback(
                                "LuckMail 新邮件: "
                                f"id={message_id}, "
                                f"received_at={mail.get('received_at', '')}, "
                                f"matched={mail.get('matched', '')}, "
                                f"verification_code={mail.get('verification_code', '') or '-'}, "
                                f"subject={str(mail.get('subject', '') or '')[:120]}"
                            )
                        list_text = _list_level_text(mail)
                        if keyword and keyword.lower() not in list_text.lower():
                            detail = self._query_detail(message_id)
                            full_text = self._message_text(mail, detail)
                            if keyword.lower() not in full_text.lower():
                                continue
                        code = str(mail.get("verification_code", "") or "").strip()
                        if code and code.lower() != "none":
                            return code
                        list_match = pattern.search(list_text)
                        if list_match:
                            return list_match.group(1) if list_match.groups() else list_match.group(0)
                        detail = self._query_detail(message_id)
                        detail_code = str(detail.get("verification_code", "") or "").strip()
                        if detail_code and detail_code.lower() != "none":
                            return detail_code
                        text = self._message_text(mail, detail)
                        text = re.sub(r"<[^>]+>", " ", text)
                        text = re.sub(r"\s+", " ", text).strip()
                        match = pattern.search(text)
                        if match:
                            return match.group(1) if match.groups() else match.group(0)
                        if callable(debug_callback):
                            debug_callback(
                                "LuckMail 邮件未提取到验证码: "
                                f"id={message_id}, preview={text[:180]}"
                            )
                    except Exception as mail_exc:
                        if callable(debug_callback):
                            debug_callback(
                                "LuckMail 邮件处理异常: "
                                f"id={str(mail.get('message_id', '') or '-')}, error={mail_exc}"
                            )
                        continue

                if strict_otp_sent_at:
                    remaining = max(0.0, timeout - (time.time() - start))
                    if remaining <= 0:
                        break
                    time.sleep(min(self._TOKEN_POLL_INTERVAL_SECONDS, remaining))
                    continue

                reuse_threshold = None
                if otp_sent_at:
                    reuse_threshold = float(otp_sent_at) - float(self._OTP_REUSE_WINDOW_SECONDS)
                for mail in sorted_mails:
                    try:
                        message_id = str(mail.get("message_id", "") or "")
                        if not message_id:
                            continue
                        mail_ts = self._mail_timestamp(mail)
                        if reuse_threshold is not None and mail_ts and mail_ts < reuse_threshold:
                            continue
                        list_text = _list_level_text(mail)
                        detail = None
                        if keyword and keyword.lower() not in list_text.lower():
                            detail = self._query_detail(message_id)
                            full_text = self._message_text(mail, detail)
                            if keyword.lower() not in full_text.lower():
                                continue
                        code = str(mail.get("verification_code", "") or "").strip()
                        if not code or code.lower() == "none":
                            list_match = pattern.search(list_text)
                            if list_match:
                                code = list_match.group(1) if list_match.groups() else list_match.group(0)
                        if not code or code.lower() == "none":
                            detail = detail or self._query_detail(message_id)
                            code = str(detail.get("verification_code", "") or "").strip()
                        if not code or code.lower() == "none":
                            detail = detail or self._query_detail(message_id)
                            text = self._message_text(mail, detail)
                            text = re.sub(r"<[^>]+>", " ", text)
                            text = re.sub(r"\s+", " ", text).strip()
                            match = pattern.search(text)
                            if match:
                                code = match.group(1) if match.groups() else match.group(0)
                        if code and code.lower() != "none":
                            if callable(debug_callback):
                                debug_callback(
                                    "LuckMail 复用最近验证码: "
                                    f"id={message_id}, "
                                    f"received_at={mail.get('received_at', '')}, "
                                    f"code={code}"
                                )
                            return code
                    except Exception as mail_exc:
                        if callable(debug_callback):
                            debug_callback(
                                "LuckMail 复用旧邮件异常: "
                                f"id={str(mail.get('message_id', '') or '-')}, error={mail_exc}"
                            )
                        continue
            except Exception as poll_exc:
                if self._is_rate_limited_error(poll_exc):
                    last_rate_limit_error = str(poll_exc)
                    if callable(debug_callback):
                        debug_callback(f"LuckMail token 收件箱触发限流: {poll_exc}")
                elif callable(debug_callback):
                    debug_callback(f"LuckMail 收件箱轮询异常: {poll_exc}")
            remaining = max(0.0, timeout - (time.time() - start))
            if remaining <= 0:
                break
            time.sleep(min(self._TOKEN_POLL_INTERVAL_SECONDS, remaining))
        if last_rate_limit_error:
            raise TimeoutError(f"等待验证码超时 ({timeout}s) [luckmail_rate_limited] {last_rate_limit_error}")
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None) -> str:
        import time

        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                inbox = self._query_inbox()
                mails = list(inbox.get("mails") or [])
                for mail in sorted(mails, key=lambda item: str(item.get("received_at", "")), reverse=True):
                    message_id = str(mail.get("message_id", "") or "")
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    detail = self._query_detail(message_id)
                    link = _extract_verification_link(self._message_text(mail, detail), keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(7)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class OutlookTokenMailbox(BaseMailbox):
    """通过 Outlook refresh token 刷新访问令牌，再经 IMAP 收件。"""

    _TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
    _IMAP_SERVER = "outlook.live.com"
    _IMAP_PORT = 993
    _POLL_INTERVAL_SECONDS = 3

    def __init__(
        self,
        email: str,
        password: str = "",
        client_id: str = "",
        refresh_token: str = "",
        proxy: str | None = None,
        token_update_hook: Callable[[str], None] | None = None,
        registration_email: str = "",
        alias_parent_email: str = "",
    ):
        self._email = str(email or "").strip()
        self._password = str(password or "")
        self._client_id = str(client_id or "").strip()
        self._refresh_token = str(refresh_token or "").strip()
        self._registration_email = str(registration_email or "").strip()
        self._alias_parent_email = str(alias_parent_email or "").strip()
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token_update_hook = token_update_hook

    def get_email(self) -> MailboxAccount:
        if not self._email:
            raise RuntimeError("Outlook 邮箱不能为空")
        if not self._client_id or not self._refresh_token:
            raise RuntimeError("Outlook 邮箱缺少 client_id 或 refresh_token")
        registration_email = self._registration_email or self._email
        alias_parent_email = self._alias_parent_email or (self._email if registration_email != self._email else "")
        account_metadata = {
            "email": self._email,
            "client_id": self._client_id,
            "auth_mode": "refresh_token",
            "token_url": self._TOKEN_URL,
        }
        resource_metadata = {
            "email": registration_email,
            "client_id": self._client_id,
            "auth_mode": "refresh_token",
            "outlook_login_email": self._email,
        }
        if alias_parent_email:
            account_metadata["alias_parent_email"] = alias_parent_email
            account_metadata["registration_email"] = registration_email
            resource_metadata["alias_parent_email"] = alias_parent_email
            resource_metadata["registration_email"] = registration_email
        return MailboxAccount(
            email=registration_email,
            account_id=self._email,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "outlook_token",
                    "login_identifier": self._email,
                    "display_name": self._email,
                    "credentials": {
                        "password": self._password,
                        "client_id": self._client_id,
                        "refresh_token": self._refresh_token,
                    },
                    "metadata": account_metadata,
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "outlook_token",
                    "resource_type": "mailbox",
                    "resource_identifier": registration_email,
                    "handle": registration_email,
                    "display_name": registration_email,
                    "metadata": resource_metadata,
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        access_token = self._refresh_access_token(account)
        ids: set[str] = set()
        for folder in self._mail_folders_to_scan():
            imap_conn = None
            try:
                imap_conn = self._open_imap_connection(access_token)
                typ, _ = imap_conn.select(f'"{folder}"', readonly=True)
                if typ != "OK" and folder.upper() == "INBOX":
                    typ, _ = imap_conn.select("INBOX", readonly=True)
                if typ != "OK":
                    continue
                typ, uid_data = imap_conn.uid("search", None, "ALL")
                if typ != "OK" or not uid_data or not uid_data[0]:
                    continue
                ids.update(f"{folder}:{item.decode('utf-8', 'replace')}" for item in uid_data[0].split())
            except Exception:
                continue
            finally:
                self._close_imap(imap_conn)
        return ids

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        import time

        access_token = self._refresh_access_token(account)
        seen = {str(item) for item in (before_ids or set())}
        start = time.time()
        pattern = code_pattern or r"(?<!\d)(\d{6})(?!\d)"
        while time.time() - start < timeout:
            for message in self._fetch_recent_messages(access_token):
                uid = str(message.get("uid") or "")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                combined = "\n".join(
                    [
                        str(message.get("subject") or ""),
                        str(message.get("body_text") or ""),
                        str(message.get("body_html") or ""),
                    ]
                )
                if keyword and keyword.lower() not in combined.lower():
                    continue
                match = re.search(pattern, combined, re.IGNORECASE)
                if match:
                    return match.group(1) if match.lastindex else match.group(0)
            time.sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None) -> str:
        import time

        access_token = self._refresh_access_token(account)
        seen = {str(item) for item in (before_ids or set())}
        start = time.time()
        while time.time() - start < timeout:
            for message in self._fetch_recent_messages(access_token):
                uid = str(message.get("uid") or "")
                if not uid or uid in seen:
                    continue
                seen.add(uid)
                combined = "\n".join(
                    [
                        str(message.get("subject") or ""),
                        str(message.get("body_text") or ""),
                        str(message.get("body_html") or ""),
                    ]
                )
                link = _extract_verification_link(combined, keyword=keyword)
                if link:
                    return link
            time.sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")

    def _refresh_access_token(self, account: MailboxAccount) -> str:
        import requests

        response = requests.post(
            self._TOKEN_URL,
            data={
                "client_id": self._client_id,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
            },
            proxies=self.proxy,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        access_token = str(data.get("access_token") or "").strip()
        rotated_refresh_token = str(data.get("refresh_token") or "").strip()
        if not access_token:
            raise RuntimeError("Outlook OAuth 刷新失败：未返回 access_token")
        if rotated_refresh_token and rotated_refresh_token != self._refresh_token:
            self._refresh_token = rotated_refresh_token
            self._persist_refresh_token(account, rotated_refresh_token)
        return access_token

    def _persist_refresh_token(self, account: MailboxAccount, refresh_token: str) -> None:
        from datetime import datetime, timezone

        extra = dict(getattr(account, "extra", {}) or {})
        provider_account = dict(extra.get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        credentials["refresh_token"] = refresh_token
        provider_account["credentials"] = credentials
        extra["provider_account"] = provider_account

        provider_resource = dict(extra.get("provider_resource") or {})
        metadata = dict(provider_resource.get("metadata") or {})
        metadata["refresh_token_updated_at"] = datetime.now(timezone.utc).isoformat()
        provider_resource["metadata"] = metadata
        extra["provider_resource"] = provider_resource
        account.extra = extra

        if callable(self._token_update_hook):
            self._token_update_hook(refresh_token)

    def _open_imap_connection(self, access_token: str):
        import imaplib

        imap_conn = imaplib.IMAP4_SSL(self._IMAP_SERVER, self._IMAP_PORT)
        auth_string = f"user={self._email}\x01auth=Bearer {access_token}\x01\x01"
        typ, _ = imap_conn.authenticate("XOAUTH2", lambda _: auth_string.encode("utf-8"))
        if typ != "OK":
            raise RuntimeError("Outlook IMAP 认证失败")
        return imap_conn

    def _fetch_recent_messages(self, access_token: str) -> list[dict]:
        import email

        messages: list[dict] = []
        seen_keys: set[str] = set()
        for folder in self._mail_folders_to_scan():
            imap_conn = None
            try:
                imap_conn = self._open_imap_connection(access_token)
                typ, _ = imap_conn.select(f'"{folder}"', readonly=True)
                if typ != "OK" and folder.upper() == "INBOX":
                    typ, _ = imap_conn.select("INBOX", readonly=True)
                if typ != "OK":
                    continue
                typ, uid_data = imap_conn.uid("search", None, "ALL")
                if typ != "OK" or not uid_data or not uid_data[0]:
                    continue
                uids = list(uid_data[0].split())
                uids.reverse()
                for uid_bytes in uids[:20]:
                    uid_text = uid_bytes.decode("utf-8", "replace")
                    message_key = f"{folder}:{uid_text}"
                    if message_key in seen_keys:
                        continue
                    seen_keys.add(message_key)
                    typ, msg_data = imap_conn.uid("fetch", uid_bytes, "(RFC822)")
                    if typ != "OK" or not msg_data:
                        continue
                    raw_email_bytes = None
                    for item in msg_data:
                        if isinstance(item, tuple) and len(item) == 2:
                            raw_email_bytes = item[1]
                            break
                    if not raw_email_bytes:
                        continue
                    parsed = email.message_from_bytes(raw_email_bytes)
                    body_text, body_html = self._extract_message_bodies(parsed)
                    messages.append(
                        {
                            "uid": message_key,
                            "folder": folder,
                            "subject": self._decode_header_value(parsed.get("Subject", "")),
                            "body_text": body_text,
                            "body_html": body_html,
                        }
                    )
            except Exception:
                continue
            finally:
                self._close_imap(imap_conn)
        return messages

    @staticmethod
    def _mail_folders_to_scan() -> tuple[str, ...]:
        return ("INBOX", "Junk", "Junk Email")

    @staticmethod
    def _extract_message_bodies(message) -> tuple[str, str]:
        body_text = ""
        body_html = ""
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, "replace")
                if part.get_content_type() == "text/plain" and not body_text:
                    body_text = text
                elif part.get_content_type() == "text/html" and not body_html:
                    body_html = text
        else:
            payload = message.get_payload(decode=True) or b""
            charset = message.get_content_charset() or "utf-8"
            text = payload.decode(charset, "replace")
            if message.get_content_type() == "text/html":
                body_html = text
            else:
                body_text = text
        return body_text, body_html

    @staticmethod
    def _decode_header_value(value) -> str:
        from email.header import decode_header

        decoded: list[str] = []
        for part, charset in decode_header(str(value or "")):
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", "replace"))
            else:
                decoded.append(str(part))
        return "".join(decoded)

    @staticmethod
    def _close_imap(imap_conn) -> None:
        if not imap_conn:
            return
        try:
            imap_conn.close()
        except Exception:
            pass
        try:
            imap_conn.logout()
        except Exception:
            pass


class YydsMailMailbox(BaseMailbox):
    """215.im / YYDS Mail 动态邮箱。"""

    _POLL_INTERVAL_SECONDS = 3

    def __init__(
        self,
        api_base_url: str | None = None,
        api_key: str = "",
        prefix: str = "",
        domain: str = "",
        email: str = "",
        proxy: str | None = None,
    ):
        self.api = _normalize_api_base_url(api_base_url, default="https://maliapi.215.im", label="YYDS Mail API URL")
        self._api_key = str(api_key or "").strip()
        self._prefix = str(prefix or "").strip()
        self._domain = str(domain or "").strip()
        self._email = str(email or "").strip()
        self._mailbox_token = ""
        self.proxy = {"http": proxy, "https": proxy} if proxy else None

    def _headers(self) -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        if self._mailbox_token:
            headers["Authorization"] = f"Bearer {self._mailbox_token}"
        return headers

    _REQUEST_MAX_RETRIES = 3
    _REQUEST_BACKOFF_BASE = 5.0  # 首次 429 等 5s，之后 10s、20s

    def _request_json(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
        import requests
        import time as _time

        method_name = str(method or "GET").upper()
        request_func = requests.post if method_name == "POST" else requests.get
        kwargs = {
            "headers": self._headers(),
            "proxies": self.proxy,
            "timeout": 15,
        }
        if method_name == "POST":
            kwargs["json"] = dict(json or {})
        else:
            kwargs["params"] = dict(params or {})

        last_error: RuntimeError | None = None
        for attempt in range(self._REQUEST_MAX_RETRIES + 1):
            response = request_func(f"{self.api}{path}", **kwargs)
            try:
                data = response.json()
            except Exception as exc:
                raise RuntimeError("215.im 响应不是有效 JSON") from exc

            status = getattr(response, "status_code", 200)
            if status < 400:
                return data

            if status == 429 and attempt < self._REQUEST_MAX_RETRIES:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = min(float(retry_after), 60.0)
                    except (ValueError, TypeError):
                        wait = self._REQUEST_BACKOFF_BASE * (2 ** attempt)
                else:
                    wait = self._REQUEST_BACKOFF_BASE * (2 ** attempt)
                _time.sleep(wait)
                continue

            message = _extract_response_message(data)
            suffix = f": {message}" if message else ""
            last_error = RuntimeError(f"215.im 请求失败: HTTP {status}{suffix}")

        if last_error:
            raise last_error
        raise RuntimeError("215.im 请求失败: 未知错误")

    def _build_account(self, email: str) -> MailboxAccount:
        credentials = {}
        if self._mailbox_token:
            credentials["mailbox_token"] = self._mailbox_token
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "yyds_mail",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": credentials,
                    "metadata": {
                        "api_url": self.api,
                        "auth_mode": "api_key",
                        "email": email,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "yyds_mail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "api_url": self.api,
                        "auth_mode": "api_key",
                        "email": email,
                    },
                },
            },
        )

    def _list_messages(self, email: str) -> list[dict]:
        payload = self._request_json("GET", "/v1/messages", params={"address": email})
        return [
            _normalize_generic_mail_message(item)
            for item in _extract_mail_items(payload, "messages", "items", "list", "results")
        ]

    def _message_detail(self, message_id: str) -> dict:
        payload = self._request_json("GET", f"/v1/messages/{message_id}")
        return _normalize_generic_mail_message(payload)

    def get_email(self) -> MailboxAccount:
        if self._email:
            return self._build_account(self._email)
        payload = {}
        if self._prefix:
            payload["prefix"] = self._prefix
        if self._domain:
            payload["domain"] = self._domain
        data = _flatten_mail_payload(self._request_json("POST", "/v1/accounts", json=payload))
        email = str(data.get("address") or data.get("email") or self._email or "").strip()
        if not email:
            raise RuntimeError("215.im 创建邮箱失败: 未返回 address")
        self._email = email
        self._mailbox_token = str(data.get("token") or data.get("access_token") or "").strip()
        return self._build_account(email)

    def ensure_inbox(self, email: str) -> str:
        """确保临时邮箱在 215.im 上存在（过期后重建）。

        返回新的 mailbox_token（201 创建成功时），或空字符串（409 已存在时）。
        409 表示邮箱仍存活，API Key 即可查询邮件。
        """
        import requests as _req
        import time as _time

        parts = str(email or "").strip().split("@", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise RuntimeError(f"ensure_inbox: 邮箱格式无效: {email}")
        local_part, domain = parts

        for attempt in range(self._REQUEST_MAX_RETRIES + 1):
            response = _req.post(
                f"{self.api}/v1/accounts",
                headers=self._headers(),
                json={"localPart": local_part, "domain": domain},
                proxies=self.proxy,
                timeout=15,
            )
            status = response.status_code
            if status == 409:
                return ""
            if status == 429 and attempt < self._REQUEST_MAX_RETRIES:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else self._REQUEST_BACKOFF_BASE * (2 ** attempt)
                _time.sleep(min(wait, 60.0))
                continue
            try:
                data = response.json()
            except Exception:
                raise RuntimeError("215.im ensure_inbox 响应不是有效 JSON")
            if status < 400:
                data = _flatten_mail_payload(data)
                token = str(data.get("token") or data.get("access_token") or "").strip()
                if token:
                    self._mailbox_token = token
                return token
            message = _extract_response_message(data)
            raise RuntimeError(f"215.im ensure_inbox 失败: HTTP {status}: {message}" if message else f"215.im ensure_inbox 失败: HTTP {status}")

        raise RuntimeError("215.im ensure_inbox 失败: 重试耗尽")

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(item.get("id") or "")
                for item in self._list_messages(account.email)
                if item.get("id")
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        import time

        seen = {str(item) for item in (before_ids or set())}
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)", re.IGNORECASE)
        start = time.time()
        while time.time() - start < timeout:
            try:
                messages = sorted(
                    self._list_messages(account.email),
                    key=lambda item: str(item.get("created_at") or item.get("id") or ""),
                    reverse=True,
                )
                for message in messages:
                    message_id = str(message.get("id") or "")
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    detail = self._message_detail(message_id)
                    text = _compose_mail_text(message, detail)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    match = pattern.search(text)
                    if match:
                        return match.group(1) if match.lastindex else match.group(0)
            except Exception:
                pass
            time.sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None) -> str:
        import time

        seen = {str(item) for item in (before_ids or set())}
        start = time.time()
        errors = 0
        while time.time() - start < timeout:
            try:
                messages = sorted(
                    self._list_messages(account.email),
                    key=lambda item: str(item.get("created_at") or item.get("id") or ""),
                    reverse=True,
                )
                for message in messages:
                    message_id = str(message.get("id") or "")
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    detail = self._message_detail(message_id)
                    link = _extract_verification_link(_compose_mail_link_source(message, detail), keyword)
                    if link:
                        return link
            except Exception:
                errors += 1
                if errors <= 2 or errors % 10 == 0:
                    import traceback as _tb
                    _tb.print_exc()
            time.sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class HStockPlusGoogleAccountProvider(BaseMailbox):
    """HStockPlus 购买 Google/Gmail 账号，支持新购和复用两种模式。"""

    def __init__(
        self,
        api_base_url: str | None = None,
        api_key: str = "",
        service_id: str | int = "",
        quantity: str | int = 1,
        link: str = "",
        delivery_timeout: str | int = 600,
        poll_interval: str | float = 5,
        request_timeout: str | float = 90,
        enterprise_contract_required: str | bool = False,
        enterprise_contract_accepted: str | bool = False,
        proxy: str | None = None,
        reuse_mode: str | bool = False,
        reuse_email: str = "",
    ):
        self.api = _normalize_api_base_url(api_base_url, default="https://hstockplus.com/api/v2", label="HStockPlus API URL")
        self._api_key = str(api_key or "").strip()
        self._service_id = str(service_id or "").strip()
        self._quantity = max(1, int(quantity or 1))
        self._link = str(link or "").strip()
        self._delivery_timeout = max(1, int(delivery_timeout or 600))
        self._poll_interval = max(0.0, float(poll_interval or 0))
        self._request_timeout = max(5.0, float(request_timeout or 90))
        self._enterprise_contract_required = _normalize_bool_flag(enterprise_contract_required, default=False)
        self._enterprise_contract_accepted = _normalize_bool_flag(enterprise_contract_accepted, default=False)
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._account: MailboxAccount | None = None
        self._reuse_mode = _normalize_bool_flag(reuse_mode, default=False)
        self._reuse_email = str(reuse_email or "").strip()

    def _request_json(self, action: str, **fields):
        import requests

        if not self._api_key:
            raise ValueError("HStockPlus API Key ???")
        data = {"key": self._api_key, "action": action}
        for key, value in fields.items():
            if value not in (None, ""):
                data[key] = str(value)
        response = requests.post(self.api, data=data, proxies=self.proxy, timeout=self._request_timeout)
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("HStockPlus ?????? JSON") from exc
        if getattr(response, "status_code", 200) >= 400 or (isinstance(payload, dict) and payload.get("error")):
            message = _extract_response_message(payload)
            suffix = f": {message}" if message else ""
            raise RuntimeError(f"HStockPlus ????: HTTP {getattr(response, 'status_code', 200)}{suffix}")
        return payload

    @staticmethod
    def _parse_account(raw: str) -> dict:
        value = str(raw or "").strip()
        if not value:
            return {}
        if "----" in value:
            parts = [part.strip() for part in value.split("----")]
        elif "|" in value:
            parts = [part.strip() for part in value.split("|")]
        elif ":" in value:
            parts = [part.strip() for part in value.split(":")]
        else:
            parts = [value]
        email = next((part for part in parts if "@" in part), parts[0] if parts else "")
        password = ""
        if email in parts:
            index = parts.index(email)
            if index + 1 < len(parts):
                password = parts[index + 1]
        elif len(parts) > 1:
            password = parts[1]
        recovery = next((part for part in parts if part != email and part != password and "@" in part), "")
        return {"email": email, "password": password, "recovery": recovery, "parts": parts, "raw": value}

    def _build_account(self, order_id: str, raw_account: str, status_payload: dict) -> MailboxAccount:
        parsed = self._parse_account(raw_account)
        email = str(parsed.get("email") or "").strip()
        if not email:
            raise RuntimeError("HStockPlus 交付数据中未解析到邮箱")
        password = str(parsed.get("password") or "")
        recovery = str(parsed.get("recovery") or "")
        credentials = {}
        if password:
            credentials["password"] = password
        metadata = {
            "api_url": self.api,
            "auth_mode": "api_key",
            "order_id": str(order_id),
            "raw_account": raw_account,
            "charge": status_payload.get("charge", ""),
            "currency": status_payload.get("currency", ""),
            "status": status_payload.get("status", ""),
        }
        if recovery:
            metadata["recovery"] = recovery
        return MailboxAccount(
            email=email,
            account_id=str(order_id),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "hstockplus_google",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": credentials,
                    "metadata": metadata,
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "hstockplus_google",
                    "resource_type": "google_account",
                    "resource_identifier": str(order_id),
                    "handle": email,
                    "display_name": email,
                    "metadata": metadata,
                },
            },
        )

    @staticmethod
    def _looks_like_google_product(item: dict) -> bool:
        text = " ".join(str(item.get(key, "")) for key in ("name", "category", "subcategory", "description")).lower()
        return any(token in text for token in ("google", "gmail", "gsuite", "workspace", "edu"))

    def list_google_products(self, *, lang: str = "zh") -> list[dict]:
        payload = self._request_json("services", lang=lang or "zh", limit=0)
        rows = payload.get("services") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        return [dict(item) for item in rows if isinstance(item, dict) and str(item.get("entityType", "product")).lower() == "product" and self._looks_like_google_product(item)]

    def _reuse_existing_account(self) -> MailboxAccount | None:
        """从 Google 账号池中取一个未在目标平台注册过的账号。"""
        from core.google_account_pool import GoogleAccountPool, GooglePoolAccount

        pool = GoogleAccountPool()
        target_platform = getattr(self, "_target_platform", "")
        exclude = [target_platform] if target_platform else []
        if self._reuse_email:
            acct = pool.get_by_email(self._reuse_email, exclude_platforms=exclude)
        else:
            acct = pool.acquire(exclude_platforms=exclude)
        if not acct:
            return None
        account = self._build_account(
            str(acct.source_order_id or "pool"),
            f"{acct.email}----{acct.password}",
            {"status": "reused", "charge": "0", "currency": "USD"},
        )
        account.extra["google_pool_reserved_platform"] = target_platform
        account.extra["google_pool_reserved_email"] = acct.email
        return account

    def _save_to_pool(self, email: str, password: str, order_id: str, raw_account: str = "") -> None:
        """HStockPlus 一旦交付账号就立即入池，避免后续平台注册失败导致已购账号丢失。"""
        try:
            from core.google_account_pool import GoogleAccountPool

            pool = GoogleAccountPool()
            email_value = str(email or "").strip()
            password_value = str(password or "")
            if (not email_value or not password_value) and raw_account:
                parsed = self._parse_account(raw_account)
                email_value = email_value or str(parsed.get("email") or "").strip()
                password_value = password_value or str(parsed.get("password") or "")
            if not email_value or not password_value:
                return
            pool.add_account(
                email_value,
                password_value,
                source="hstockplus",
                source_order_id=str(order_id or ""),
            )
        except Exception:
            pass

    def get_email(self) -> MailboxAccount:
        import time

        if self._account:
            return self._account
        if self._reuse_mode:
            reused = self._reuse_existing_account()
            if reused:
                self._account = reused
                return self._account
            if self._reuse_email:
                raise RuntimeError(f"Google 账号池中指定账号不可用或已注册当前平台: {self._reuse_email}")
            raise RuntimeError("Google 账号池无可用的未注册账号，请先批量购买并添加到 output/google_accounts_pool.json")
        if self._enterprise_contract_required and not self._enterprise_contract_accepted:
            raise ValueError("需要先接受企业协议/合同才能购买 Google 账号")
        if not self._service_id:
            raise ValueError("HStockPlus Google 账号 service_id 未配置")
        order_payload = self._request_json(
            "add",
            service=self._service_id,
            quantity=self._quantity,
            link=self._link,
        )
        order_id = str(order_payload.get("order") or "").strip()
        if not order_id:
            raise RuntimeError("HStockPlus 下单失败: 未返回 order")
        deadline = time.time() + self._delivery_timeout
        last_payload = {}
        last_status_error: Exception | None = None
        while time.time() <= deadline:
            try:
                last_payload = self._request_json("status", order=order_id)
                last_status_error = None
            except Exception as exc:
                last_status_error = exc
                if self._poll_interval <= 0:
                    raise
                time.sleep(self._poll_interval)
                continue
            accounts = last_payload.get("accounts") if isinstance(last_payload, dict) else None
            if isinstance(accounts, list) and accounts:
                first_raw = ""
                for raw_item in accounts:
                    raw = str(raw_item or "").strip()
                    if not raw:
                        continue
                    if not first_raw:
                        first_raw = raw
                    parsed = self._parse_account(raw)
                    self._save_to_pool(
                        parsed.get("email", ""),
                        parsed.get("password", ""),
                        order_id,
                        raw,
                    )
                if not first_raw:
                    raise RuntimeError("HStockPlus 交付数据为空")
                self._account = self._build_account(order_id, first_raw, last_payload)
                return self._account
            status = str(last_payload.get("status") or "").strip().lower() if isinstance(last_payload, dict) else ""
            if status in {"canceled", "cancelled"}:
                raise RuntimeError(f"HStockPlus 订单已取消: {order_id}")
            if self._poll_interval <= 0:
                break
            time.sleep(self._poll_interval)
        if last_status_error is not None:
            raise TimeoutError(f"HStockPlus Google 账号交付超时: order={order_id}; last_status_error={last_status_error}")
        raise TimeoutError(f"HStockPlus Google 账号交付超时: order={order_id}")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        raise NotImplementedError("HStockPlus Google 账号不提供邮箱验证码读取，请通过 oauth_browser / Google 登录流程使用")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None) -> str:
        raise NotImplementedError("HStockPlus Google 账号不提供邮箱验证链接读取，请通过 oauth_browser / Google 登录流程使用")


class GptMailMailbox(BaseMailbox):
    """mail.chatgpt.org.uk 动态邮箱。"""

    _POLL_INTERVAL_SECONDS = 3

    def __init__(
        self,
        api_base_url: str | None = None,
        api_key: str = "",
        prefix: str = "",
        domain: str = "",
        email: str = "",
        proxy: str | None = None,
    ):
        self.api = _normalize_api_base_url(api_base_url, default="https://mail.chatgpt.org.uk", label="GPTMail API URL")
        self._api_key = str(api_key or "").strip()
        self._prefix = str(prefix or "").strip()
        self._domain = str(domain or "").strip()
        self._email = str(email or "").strip()
        self.proxy = {"http": proxy, "https": proxy} if proxy else None

    def _headers(self) -> dict:
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
        }
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    def _request_json(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None):
        import requests

        method_name = str(method or "GET").upper()
        request_func = requests.post if method_name == "POST" else requests.get
        kwargs = {
            "headers": self._headers(),
            "proxies": self.proxy,
            "timeout": 15,
        }
        if method_name == "POST":
            kwargs["json"] = dict(json or {})
        else:
            kwargs["params"] = dict(params or {})
        response = request_func(f"{self.api}{path}", **kwargs)
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError("GPTMail 响应不是有效 JSON") from exc
        if getattr(response, "status_code", 200) >= 400:
            message = _extract_response_message(data)
            suffix = f": {message}" if message else ""
            raise RuntimeError(f"GPTMail 请求失败: HTTP {response.status_code}{suffix}")
        return data

    def _build_account(self, email: str) -> MailboxAccount:
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "gptmail",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": {},
                    "metadata": {
                        "api_url": self.api,
                        "auth_mode": "api_key",
                        "email": email,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "gptmail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "api_url": self.api,
                        "auth_mode": "api_key",
                        "email": email,
                    },
                },
            },
        )

    def _generation_params(self) -> dict:
        params = {}
        if self._prefix:
            params["prefix"] = self._prefix
        if self._domain:
            params["domain"] = self._domain
        return params

    def _list_messages(self, email: str) -> list[dict]:
        payload = self._request_json("GET", "/api/emails", params={"email": email})
        return [
            _normalize_generic_mail_message(item)
            for item in _extract_mail_items(payload, "emails", "messages", "items", "list", "results")
        ]

    def _message_detail(self, message_id: str) -> dict:
        payload = self._request_json("GET", f"/api/email/{message_id}")
        return _normalize_generic_mail_message(payload)

    def get_email(self) -> MailboxAccount:
        if self._email:
            return self._build_account(self._email)
        params = self._generation_params()
        data = _flatten_mail_payload(self._request_json("GET", "/api/generate-email", params=params))
        email = str(data.get("email") or data.get("address") or self._email or "").strip()
        if not email:
            data = _flatten_mail_payload(self._request_json("POST", "/api/generate-email", json=params))
            email = str(data.get("email") or data.get("address") or self._email or "").strip()
        if not email:
            raise RuntimeError("GPTMail 创建邮箱失败: 未返回 email")
        self._email = email
        return self._build_account(email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            return {
                str(item.get("id") or "")
                for item in self._list_messages(account.email)
                if item.get("id")
            }
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
    ) -> str:
        import time

        seen = {str(item) for item in (before_ids or set())}
        pattern = re.compile(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)", re.IGNORECASE)
        start = time.time()
        while time.time() - start < timeout:
            try:
                messages = sorted(
                    self._list_messages(account.email),
                    key=lambda item: str(item.get("created_at") or item.get("id") or ""),
                    reverse=True,
                )
                for message in messages:
                    message_id = str(message.get("id") or "")
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    detail = self._message_detail(message_id)
                    text = _compose_mail_text(message, detail)
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    match = pattern.search(text)
                    if match:
                        return match.group(1) if match.lastindex else match.group(0)
            except Exception:
                pass
            time.sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "", timeout: int = 120, before_ids: set = None) -> str:
        import time

        seen = {str(item) for item in (before_ids or set())}
        start = time.time()
        while time.time() - start < timeout:
            try:
                messages = sorted(
                    self._list_messages(account.email),
                    key=lambda item: str(item.get("created_at") or item.get("id") or ""),
                    reverse=True,
                )
                for message in messages:
                    message_id = str(message.get("id") or "")
                    if not message_id or message_id in seen:
                        continue
                    seen.add(message_id)
                    detail = self._message_detail(message_id)
                    link = _extract_verification_link(_compose_mail_link_source(message, detail), keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(self._POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class AitreMailbox(BaseMailbox):
    """mail.aitre.cc 临时邮箱"""
    def __init__(self, email: str):
        self._email = email
        self.api = "https://mail.aitre.cc/api/tempmail"

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email=self._email)

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
            emails = r.json().get("emails", [])
            return {str(m["id"]) for m in emails if "id" in m}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "trae",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids) if before_ids else set()
        last_check = None
        start = time.time()
        while time.time() - start < timeout:
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = mail.get("preview", "") + mail.get("content", "")
                        if keyword and keyword.lower() not in text.lower():
                            continue
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                        if m:
                            return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        last_check = None
        start = time.time()
        while time.time() - start < timeout:
            params = {"email": account.email}
            if last_check:
                params["lastCheck"] = last_check
            try:
                r = requests.get(f"{self.api}/poll", params=params, timeout=10)
                data = r.json()
                last_check = data.get("lastChecked")
                if data.get("count", 0) > 0:
                    r2 = requests.get(f"{self.api}/emails", params={"email": account.email}, timeout=10)
                    for mail in r2.json().get("emails", []):
                        mid = str(mail.get("id", ""))
                        if mid in seen:
                            continue
                        seen.add(mid)
                        text = str(mail.get("preview", "")) + " " + str(mail.get("content", ""))
                        link = _extract_verification_link(text, keyword)
                        if link:
                            return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class TempMailLolMailbox(BaseMailbox):
    """tempmail.lol 免费临时邮箱（无需注册，自动生成）"""

    def __init__(self, proxy: str = None):
        self.api = "https://api.tempmail.lol/v2"
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None
        self._email = None

    def get_email(self) -> MailboxAccount:
        import requests
        r = requests.post(f"{self.api}/inbox/create",
            json={},
            proxies=self.proxy, timeout=15)
        data = r.json()
        self._email = data.get("address") or data.get("email", "")
        self._token = data.get("token", "")
        return MailboxAccount(
            email=self._email,
            account_id=self._token,
            extra={
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "tempmail_lol",
                    "resource_type": "mailbox",
                    "resource_identifier": self._token,
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "email": self._email,
                        "token": self._token,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(f"{self.api}/inbox",
                params={"token": account.account_id},
                proxies=self.proxy, timeout=10)
            return {str(m["id"]) for m in r.json().get("emails", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy, timeout=10)
                for mail in sorted(r.json().get("emails", []), key=lambda x: x.get("date", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    seen.add(mid)
                    text = mail.get("subject", "") + " " + mail.get("body", "") + " " + mail.get("html", "")
                    if keyword and keyword.lower() not in text.lower():
                        continue
                    m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', text)
                    if m:
                        return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"{self.api}/inbox",
                    params={"token": account.account_id},
                    proxies=self.proxy, timeout=10)
                for mail in sorted(r.json().get("emails", []), key=lambda x: x.get("date", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if mid in seen:
                        continue
                    seen.add(mid)
                    text = str(mail.get("subject", "")) + " " + str(mail.get("body", "")) + " " + str(mail.get("html", ""))
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class DuckMailMailbox(BaseMailbox):
    """DuckMail 自动生成邮箱（随机创建账号）"""

    def __init__(self, api_url: str = "https://www.duckmail.sbs",
                 provider_url: str = "https://api.duckmail.sbs",
                 bearer: str = "kevin273945",
                 proxy: str = None):
        self.api = api_url.rstrip("/")
        self.provider_url = provider_url
        self.bearer = bearer
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None
        self._address = None

    def _common_headers(self) -> dict:
        return {
            "authorization": f"Bearer {self.bearer}",
            "content-type": "application/json",
            "x-api-provider-base-url": self.provider_url,
        }

    def get_email(self) -> MailboxAccount:
        import requests, random, string
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        domain = self.provider_url.replace("https://api.", "").replace("https://", "")
        address = f"{username}@{domain}"
        # 创建账号
        r = requests.post(f"{self.api}/api/mail?endpoint=%2Faccounts",
            json={"address": address, "password": password},
            headers=self._common_headers(), proxies=self.proxy, timeout=15, verify=False)
        data = r.json()
        self._address = data.get("address", address)
        # 登录获取 token
        r2 = requests.post(f"{self.api}/api/mail?endpoint=%2Ftoken",
            json={"address": self._address, "password": password},
            headers=self._common_headers(), proxies=self.proxy, timeout=15, verify=False)
        self._token = r2.json().get("token", "")
        return MailboxAccount(
            email=self._address,
            account_id=self._token,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "duckmail",
                    "login_identifier": self._address,
                    "display_name": self._address,
                    "credentials": {
                        "address": self._address,
                        "password": password,
                        "token": self._token,
                    },
                    "metadata": {
                        "provider_url": self.provider_url,
                        "api_url": self.api,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "duckmail",
                    "resource_type": "mailbox",
                    "resource_identifier": self._token,
                    "handle": self._address,
                    "display_name": self._address,
                    "metadata": {
                        "email": self._address,
                        "provider_url": self.provider_url,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        import requests
        try:
            r = requests.get(f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                headers={"authorization": f"Bearer {account.account_id}",
                         "x-api-provider-base-url": self.provider_url},
                proxies=self.proxy, timeout=10, verify=False)
            return {str(m["id"]) for m in r.json().get("hydra:member", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                    headers={"authorization": f"Bearer {account.account_id}",
                             "x-api-provider-base-url": self.provider_url},
                    proxies=self.proxy, timeout=10, verify=False)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen: continue
                    seen.add(mid)
                    # 请求邮件详情获取完整 text
                    try:
                        r2 = requests.get(f"{self.api}/api/mail?endpoint=%2Fmessages%2F{mid}",
                            headers={"authorization": f"Bearer {account.account_id}",
                                     "x-api-provider-base-url": self.provider_url},
                            proxies=self.proxy, timeout=10, verify=False)
                        detail = r2.json()
                        body = str(detail.get("text") or "") + " " + str(detail.get("subject") or "")
                    except Exception:
                        body = str(msg.get("subject") or "")
                    body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
                    m = re.search(r"(?<!#)(?<!\d)(\d{6})(?!\d)", body)
                    if m: return m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time, requests
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(f"{self.api}/api/mail?endpoint=%2Fmessages%3Fpage%3D1",
                    headers={"authorization": f"Bearer {account.account_id}",
                             "x-api-provider-base-url": self.provider_url},
                    proxies=self.proxy, timeout=10, verify=False)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    mid = str(msg.get("id") or msg.get("msgid") or "")
                    if mid in seen:
                        continue
                    seen.add(mid)
                    try:
                        r2 = requests.get(f"{self.api}/api/mail?endpoint=%2Fmessages%2F{mid}",
                            headers={"authorization": f"Bearer {account.account_id}",
                                     "x-api-provider-base-url": self.provider_url},
                            proxies=self.proxy, timeout=10, verify=False)
                        detail = r2.json()
                        body = str(detail.get("text") or "") + " " + str(detail.get("html") or "") + " " + str(detail.get("subject") or "")
                    except Exception:
                        body = str(msg.get("subject") or "")
                    link = _extract_verification_link(body, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class CFWorkerMailbox(BaseMailbox):
    """Cloudflare Worker 自建临时邮箱服务"""

    def __init__(self, api_url: str, auth_mode: str = "admin_token", admin_token: str = "", domain: str = "",
                 fingerprint: str = "", proxy: str = None):
        self.api = api_url.rstrip("/")
        self.auth_mode = str(auth_mode or "admin_token").strip() or "admin_token"
        self.admin_token = admin_token
        self.domain = domain
        self.fingerprint = fingerprint
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._token = None

    def _is_public_jwt(self) -> bool:
        return self.auth_mode == "public_jwt"

    def _headers(self, mailbox_jwt: str = "") -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
        }
        if self._is_public_jwt():
            token = mailbox_jwt or self._token
            if token:
                h["Authorization"] = f"Bearer {token}"
        else:
            h["x-admin-auth"] = self.admin_token
        if self.fingerprint:
            h["x-fingerprint"] = self.fingerprint
        return h

    def _resolve_mailbox_jwt(self, account: MailboxAccount) -> str:
        extra = account.extra if isinstance(account.extra, dict) else {}
        provider_account = extra.get("provider_account", {}) if isinstance(extra, dict) else {}
        credentials = provider_account.get("credentials", {}) if isinstance(provider_account, dict) else {}
        provider_resource = extra.get("provider_resource", {}) if isinstance(extra, dict) else {}
        resource_metadata = provider_resource.get("metadata", {}) if isinstance(provider_resource, dict) else {}
        return str(
            credentials.get("mailbox_jwt")
            or resource_metadata.get("mailbox_jwt")
            or resource_metadata.get("token")
            or self._token
            or ""
        )

    def get_email(self) -> MailboxAccount:
        import requests, random, string
        name = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        payload = {"enablePrefix": True, "name": name}
        if self.domain:
            payload["domain"] = self.domain
        if self._is_public_jwt() and self.fingerprint:
            payload["cf_token"] = self.fingerprint
        endpoint = "/api/new_address" if self._is_public_jwt() else "/admin/new_address"
        r = requests.post(f"{self.api}{endpoint}",
            json=payload, headers=self._headers(),
            proxies=self.proxy, timeout=15)
        _safe_print(f"[CFWorker] new_address status={r.status_code} resp={r.text[:200]}")
        data = r.json()
        email = data.get("email", data.get("address", ""))
        token = data.get("token", data.get("jwt", ""))
        address_id = str(data.get("address_id", data.get("id", "")) or "")
        password = data.get("password", "")
        self._token = token
        account_identifier = (address_id or token) if self._is_public_jwt() else (token or address_id or email)
        _safe_print(f"[CFWorker] 生成邮箱: {email} token={token[:40] if token else 'NONE'}...")
        return MailboxAccount(
            email=email,
            account_id=account_identifier,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "cfworker",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": {
                        "mailbox_jwt": token,
                        "address_password": password,
                        "address_id": address_id,
                    },
                    "metadata": {
                        "api_url": self.api,
                        "auth_mode": self.auth_mode,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "cfworker",
                    "resource_type": "mailbox",
                    "resource_identifier": account_identifier,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "address_id": address_id,
                        "api_url": self.api,
                        "domain": self.domain,
                        "auth_mode": self.auth_mode,
                    },
                },
            },
        )

    def _get_mails(self, account: MailboxAccount) -> list:
        import requests
        if self._is_public_jwt():
            r = requests.get(
                f"{self.api}/api/mails",
                params={"limit": 20, "offset": 0},
                headers=self._headers(self._resolve_mailbox_jwt(account)),
                proxies=self.proxy,
                timeout=10,
            )
        else:
            r = requests.get(
                f"{self.api}/admin/mails",
                params={"limit": 20, "offset": 0, "address": account.email},
                headers=self._headers(),
                proxies=self.proxy,
                timeout=10,
            )
        data = r.json()
        return data.get("results", data) if isinstance(data, dict) else data

    def _get_mail_detail_raw(self, account: MailboxAccount, mail: dict) -> str:
        import requests
        if not self._is_public_jwt():
            return str(mail.get("raw", ""))
        mid = str(mail.get("id", ""))
        if not mid:
            return ""
        r = requests.get(
            f"{self.api}/api/mail/{mid}",
            headers=self._headers(self._resolve_mailbox_jwt(account)),
            proxies=self.proxy,
            timeout=10,
        )
        data = r.json() if hasattr(r, "json") else {}
        if isinstance(data, dict):
            parts = [
                data.get("raw"),
                data.get("text"),
                data.get("content"),
                data.get("html"),
                data.get("subject"),
            ]
            return " ".join(str(p) for p in parts if p)
        return str(data or "")

    def _decode_mail_detail_text(self, raw: str) -> str:
        from email import message_from_string

        source = str(raw or "")
        if not source:
            return ""

        def _strip_html_noise(value: str) -> str:
            text = html.unescape(str(value or ""))
            text = text.replace("=\\r\\n", "").replace("=\\n", "")
            text = re.sub(r"<style\b.*?>.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"<script\b.*?>.*?</script>", " ", text, flags=re.IGNORECASE | re.DOTALL)
            text = re.sub(r"<[^>]+>", " ", text)
            return re.sub(r"\s+", " ", text).strip()

        decoded_chunks: list[str] = []
        try:
            message = message_from_string(source)
            parts = message.walk() if message.is_multipart() else [message]
            for part in parts:
                content_type = str(part.get_content_type() or "").lower()
                if content_type not in {"text/plain", "text/html"}:
                    continue
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="ignore")
                else:
                    text = str(payload or "")
                cleaned = _strip_html_noise(text)
                if cleaned:
                    decoded_chunks.append(cleaned)
        except Exception:
            pass

        if decoded_chunks:
            return " ".join(decoded_chunks)

        body_start = source.find("\\r\\n\\r\\n")
        body = source[body_start + 4:] if body_start != -1 else source
        return _strip_html_noise(body)

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            mails = self._get_mails(account)
            return {str(m.get("id", "")) for m in mails}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._get_mails(account)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    raw = self._get_mail_detail_raw(account, mail)
                    search_text = self._decode_mail_detail_text(raw)
                    keyword_source = f'{mail.get("subject", "")} {raw} {search_text}'
                    if keyword and keyword.lower() not in keyword_source.lower():
                        continue
                    search_text = re.sub(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "", search_text)
                    search_text = re.sub(r"m=\+\d+\.\d+", "", search_text)
                    search_text = re.sub(r"\bt=\d+\b", "", search_text)
                    m = re.search(code_pattern or r"(?<!#)(?<!\d)(\d{6})(?!\d)", search_text)
                    if m:
                        return m.group(1) if m.groups() else m.group(0)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                mails = self._get_mails(account)
                for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                    mid = str(mail.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    raw = self._get_mail_detail_raw(account, mail)
                    decoded = self._decode_mail_detail_text(raw)
                    combined = f'{mail.get("subject", "")} {raw} {decoded}'
                    link = _extract_verification_link(combined, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class MoeMailMailbox(BaseMailbox):
    """MoeMail (sall.cc) 邮箱服务 - 自动注册账号并生成临时邮箱"""

    def __init__(
        self,
        api_url: str = "https://sall.cc",
        username: str = "",
        password: str = "",
        session_token: str = "",
        proxy: str = None,
    ):
        self.api = _normalize_api_base_url(api_url, default="https://sall.cc", label="MoeMail API URL")
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._configured_username = str(username or "").strip()
        self._configured_password = str(password or "")
        self._configured_session_token = str(session_token or "").strip()
        self._session_token = self._configured_session_token or None
        self._email = None
        self._session = None
        self._username = self._configured_username
        self._password = self._configured_password

    def _new_session(self):
        import requests

        s = requests.Session()
        s.proxies = self.proxy
        s.verify = False
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        s.headers.update({"user-agent": ua, "origin": self.api, "referer": f"{self.api}/zh-CN/login"})
        return s

    def _extract_session_token(self, session) -> str:
        for cookie in session.cookies:
            if "session-token" in cookie.name:
                return cookie.value
        return ""

    def _apply_session_token(self, session, token: str) -> None:
        domain = urlparse(self.api).hostname or ""
        cookie_names = [
            "__Secure-authjs.session-token",
            "authjs.session-token",
            "__Secure-next-auth.session-token",
            "next-auth.session-token",
        ]
        for name in cookie_names:
            session.cookies.set(name, token, domain=domain, path="/")
            session.cookies.set(name, token, path="/")

    def _login_with_existing_account(self) -> str:
        s = self._new_session()

        if self._configured_session_token:
            self._apply_session_token(s, self._configured_session_token)
            self._session = s
            self._session_token = self._configured_session_token
            _safe_print("[MoeMail] 使用已提供的 session-token")
            return self._configured_session_token

        if not (self._configured_username and self._configured_password):
            raise RuntimeError("MoeMail 未配置可复用账号，请提供用户名密码或 session-token")

        csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        login_resp = s.post(
            f"{self.api}/api/auth/callback/credentials",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=urlencode({
                "username": self._configured_username,
                "password": self._configured_password,
                "csrfToken": csrf,
                "redirect": "false",
                "callbackUrl": self.api,
            }),
            allow_redirects=True,
            timeout=15,
        )
        self._session = s
        self._username = self._configured_username
        self._password = self._configured_password
        token = self._extract_session_token(s)
        if token:
            self._session_token = token
            _safe_print("[MoeMail] 使用手动注册账号登录成功")
            return token
        raise RuntimeError(
            f"MoeMail 登录失败: 已提供用户名密码，但未获取到 session-token (HTTP {login_resp.status_code})"
        )

    def _ensure_session(self) -> str:
        if self._session_token and self._session is not None:
            return self._session_token
        if self._configured_session_token or self._configured_username:
            return self._login_with_existing_account()
        return self._register_and_login()

    def _register_and_login(self) -> str:
        import random, string

        s = self._new_session()
        # 注册
        username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
        self._username = username
        self._password = password
        _safe_print(f"[MoeMail] 注册账号: {username} / {password}")
        r_reg = s.post(f"{self.api}/api/auth/register",
            json={"username": username, "password": password, "turnstileToken": ""},
            timeout=15)
        _safe_print(f"[MoeMail] 注册结果: {r_reg.status_code} {r_reg.text[:80]}")
        if r_reg.status_code >= 400:
            try:
                register_error = r_reg.json().get("error") or r_reg.text
            except Exception:
                register_error = r_reg.text
            raise RuntimeError(f"MoeMail 注册失败: {str(register_error).strip() or f'HTTP {r_reg.status_code}'}")
        # 获取 CSRF
        csrf_r = s.get(f"{self.api}/api/auth/csrf", timeout=10)
        csrf = csrf_r.json().get("csrfToken", "")
        # 登录
        login_resp = s.post(f"{self.api}/api/auth/callback/credentials",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=urlencode({
                "username": username,
                "password": password,
                "csrfToken": csrf,
                "redirect": "false",
                "callbackUrl": self.api,
            }),
            allow_redirects=True, timeout=15)
        self._session = s
        token = self._extract_session_token(s)
        if token:
            self._session_token = token
            _safe_print("[MoeMail] 登录成功")
            return token
        _safe_print(f"[MoeMail] 登录失败，cookies: {[c.name for c in s.cookies]}")
        raise RuntimeError(
            f"MoeMail 登录失败: 未获取到 session-token (HTTP {login_resp.status_code})"
        )

    # 优先用这些域名（信誉较好，不易被 AWS/Google 等拒绝）
    _PREFERRED_DOMAINS = ("sall.cc", "cnmlgb.de", "zhooo.org", "coolkid.icu")

    def get_email(self) -> MailboxAccount:
        self._session_token = self._configured_session_token or None
        self._session = None
        self._ensure_session()
        import random, string
        name = "".join(random.choices(string.ascii_letters + string.digits, k=8))
        # 获取可用域名列表，优先选信誉好的域名，避免被 AWS 等平台拒绝
        domain = "sall.cc"
        try:
            cfg_r = self._session.get(f"{self.api}/api/config", timeout=10)
            all_domains = [d.strip() for d in cfg_r.json().get("emailDomains", "sall.cc").split(",") if d.strip()]
            if all_domains:
                # 从可用域名中筛选优先域名，按 _PREFERRED_DOMAINS 顺序选择
                preferred = [d for d in self._PREFERRED_DOMAINS if d in all_domains]
                if preferred:
                    domain = random.choice(preferred)
                else:
                    # 无优先域名可用，从剩余中随机选
                    domain = random.choice(all_domains)
        except Exception:
            pass
        r = self._session.post(f"{self.api}/api/emails/generate",
            json={"name": name, "domain": domain, "expiryTime": 86400000},
            timeout=15)
        data = r.json()
        self._email = data.get("email", data.get("address", ""))
        email_id = data.get("id", "")
        _safe_print(f"[MoeMail] 生成邮箱: {self._email} id={email_id} domain={domain} status={r.status_code}")
        if not email_id:
            _safe_print(f"[MoeMail] 生成失败: {data}")
            generate_error = data.get("error") or data.get("message") or r.text
            raise RuntimeError(f"MoeMail 生成邮箱失败: {str(generate_error).strip() or f'HTTP {r.status_code}'}")
        if not self._email:
            raise RuntimeError("MoeMail 生成邮箱失败: 返回结果缺少 email")
        self._email_count = getattr(self, '_email_count', 0) + 1
        return MailboxAccount(
            email=self._email,
            account_id=str(email_id),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "moemail",
                    "login_identifier": getattr(self, "_username", ""),
                    "display_name": getattr(self, "_username", "") or self._email,
                    "credentials": {
                        "username": getattr(self, "_username", ""),
                        "password": getattr(self, "_password", ""),
                        "session_token": self._session_token,
                    },
                    "metadata": {
                        "api_url": self.api,
                        "email": self._email,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "moemail",
                    "resource_type": "mailbox",
                    "resource_identifier": str(email_id),
                    "handle": self._email,
                    "display_name": self._email,
                    "metadata": {
                        "email": self._email,
                        "api_url": self.api,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(f"{self.api}/api/emails/{account.account_id}", timeout=10)
            return {str(m.get("id", "")) for m in r.json().get("messages", [])}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None,
                      code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        pattern = re.compile(code_pattern) if code_pattern else None
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.api}/api/emails/{account.account_id}",
                    timeout=10)
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen: continue
                    seen.add(mid)
                    body = str(msg.get("content") or msg.get("text") or msg.get("body") or msg.get("html") or "") + " " + str(msg.get("subject") or "")
                    body = re.sub(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', '', body)
                    if pattern:
                        m = pattern.search(body)
                    else:
                        m = re.search(code_pattern or r'(?<!#)(?<!\d)(\d{6})(?!\d)', body)
                    if m: return m.group(1) if m.groups() else m.group(0) if code_pattern else m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.api}/api/emails/{account.account_id}",
                    timeout=10)
                msgs = r.json().get("messages", [])
                for msg in msgs:
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    body = (
                        str(msg.get("content") or "") + " " +
                        str(msg.get("text") or "") + " " +
                        str(msg.get("body") or "") + " " +
                        str(msg.get("html") or "") + " " +
                        str(msg.get("subject") or "")
                    )
                    link = _extract_verification_link(body, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")


class FreemailMailbox(BaseMailbox):
    """
    Freemail 自建邮箱服务（基于 Cloudflare Worker）
    项目: https://github.com/idinging/freemail
    支持管理员令牌或账号密码两种认证方式
    """

    def __init__(self, api_url: str, admin_token: str = "",
                 username: str = "", password: str = "",
                 proxy: str = None):
        self.api = api_url.rstrip("/")
        self.admin_token = admin_token
        self.username = username
        self.password = password
        self.proxy = {"http": proxy, "https": proxy} if proxy else None
        self._session = None
        self._email = None

    def _get_session(self):
        import requests
        s = requests.Session()
        s.proxies = self.proxy
        if self.admin_token:
            s.headers.update({"Authorization": f"Bearer {self.admin_token}"})
        elif self.username and self.password:
            s.post(f"{self.api}/api/login",
                json={"username": self.username, "password": self.password},
                timeout=15)
        self._session = s
        return s

    def get_email(self) -> MailboxAccount:
        if not self._session:
            self._get_session()
        import requests
        r = self._session.get(f"{self.api}/api/generate", timeout=15)
        data = r.json()
        email = data.get("email", "")
        self._email = email
        _safe_print(f"[Freemail] 生成邮箱: {email}")
        provider_account = {
            "provider_type": "mailbox",
            "provider_name": "freemail",
            "login_identifier": self.username or email,
            "display_name": self.username or email,
            "credentials": {},
            "metadata": {
                "api_url": self.api,
                "auth_mode": "admin_token" if self.admin_token else "username_password",
            },
        }
        if self.admin_token:
            provider_account["credentials"]["admin_token"] = self.admin_token
        if self.username:
            provider_account["credentials"]["username"] = self.username
        if self.password:
            provider_account["credentials"]["password"] = self.password
        return MailboxAccount(
            email=email,
            account_id=email,
            extra={
                "provider_account": provider_account,
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "freemail",
                    "resource_type": "mailbox",
                    "resource_identifier": email,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "api_url": self.api,
                    },
                },
            },
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            r = self._session.get(f"{self.api}/api/emails",
                params={"mailbox": account.email, "limit": 50}, timeout=10)
            return {str(m["id"]) for m in r.json() if "id" in m}
        except Exception:
            return set()

    def wait_for_code(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None, code_pattern: str = None) -> str:
        import re, time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20}, timeout=10)
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen: continue
                    seen.add(mid)
                    # 直接用 verification_code 字段
                    code = str(msg.get("verification_code") or "")
                    if code and code != "None":
                        return code
                    # 兜底：从 preview 提取
                    text = str(msg.get("preview", "")) + " " + str(msg.get("subject", ""))
                    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
                    if m: return m.group(1)
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证码超时 ({timeout}s)")

    def wait_for_link(self, account: MailboxAccount, keyword: str = "",
                      timeout: int = 120, before_ids: set = None) -> str:
        import time
        seen = set(before_ids or [])
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = self._session.get(f"{self.api}/api/emails",
                    params={"mailbox": account.email, "limit": 20}, timeout=10)
                for msg in r.json():
                    mid = str(msg.get("id", ""))
                    if not mid or mid in seen:
                        continue
                    seen.add(mid)
                    text = " ".join(
                        str(msg.get(key, ""))
                        for key in ("preview", "subject", "html", "text", "content", "body")
                    )
                    link = _extract_verification_link(text, keyword)
                    if link:
                        return link
            except Exception:
                pass
            time.sleep(3)
        raise TimeoutError(f"等待验证链接超时 ({timeout}s)")
