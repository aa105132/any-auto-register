"""ChatGPT edu.pilipala.store SSO 纯协议 OAuth 流程。"""
from __future__ import annotations

import base64
import json
import re
import secrets
import string
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from core.base_identity import normalize_oauth_provider
from platforms.chatgpt.constants import OAUTH_REDIRECT_URI, OPENAI_API_ENDPOINTS
from platforms.chatgpt.http_client import OpenAIHTTPClient
from platforms.chatgpt.oauth import OAuthManager


PILIPALA_SSO_PROVIDER = "pilipala_sso"
PILIPALA_SSO_DOMAIN = "edu.pilipala.store"
PILIPALA_SSO_PASSWORD = "ciallo"
_MAX_SSO_OAUTH_ATTEMPTS = 3


def is_pilipala_sso_provider(provider: str) -> bool:
    return normalize_oauth_provider(provider) == PILIPALA_SSO_PROVIDER


def _random_prefix(length: int = 10) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "aar" + "".join(secrets.choice(alphabet) for _ in range(max(4, int(length))))


def resolve_pilipala_sso_email(
    *,
    email_hint: str = "",
    prefix: str = "",
    domain: str = PILIPALA_SSO_DOMAIN,
) -> tuple[str, str]:
    """解析 SSO 邮箱和前缀；未提供时生成随机前缀。"""
    normalized_domain = (domain or PILIPALA_SSO_DOMAIN).strip().lstrip("@").lower()
    raw_hint = (email_hint or "").strip()
    raw_prefix = (prefix or "").strip()

    if raw_hint and "@" in raw_hint:
        local, hint_domain = raw_hint.rsplit("@", 1)
        if hint_domain.strip().lower() == normalized_domain and not raw_prefix:
            raw_prefix = local.strip()
    if raw_hint and "@" not in raw_hint and not raw_prefix:
        raw_prefix = raw_hint

    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "", raw_prefix) or _random_prefix()
    return f"{safe_prefix}@{normalized_domain}", safe_prefix


def _is_callback_url(url: str) -> bool:
    candidate = str(url or "")
    return candidate.startswith(OAUTH_REDIRECT_URI) and "code=" in candidate and "state=" in candidate


def _is_add_phone_url(url: str) -> bool:
    path = urlparse(str(url or "")).path.lower()
    return "add-phone" in path or "phone" in path and "auth.openai.com" in str(url or "")


def _is_openai_auth_error_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if "auth.openai.com" not in parsed.netloc or parsed.path != "/error":
        return False
    query = parse_qs(parsed.query)
    payload = unquote((query.get("payload") or [""])[0] or "")
    return "AuthApiFailure" in payload or "AuthApiFailure" in str(url or "")


def _preview(text: str, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _decode_workspace_cookie(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        segment = raw.split(".", 1)[0]
        segment += "=" * ((4 - len(segment) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")).decode("utf-8"))
        workspaces = payload.get("workspaces") or []
        if isinstance(workspaces, list) and workspaces:
            return str((workspaces[0] or {}).get("id") or "").strip()
    except Exception:
        return ""
    return ""


def _extract_continue_url(payload: dict[str, Any] | None) -> str:
    data = payload or {}
    for key in ("continue_url", "continueUrl", "redirect_url", "redirectUrl", "url"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    nested = data.get("data")
    if isinstance(nested, dict):
        return _extract_continue_url(nested)
    return ""


def _pick_first_org(payload: dict[str, Any] | None) -> tuple[str, str]:
    data = payload or {}
    orgs = data.get("orgs")
    if not isinstance(orgs, list):
        nested = data.get("data")
        if isinstance(nested, dict):
            orgs = nested.get("orgs")
    if not isinstance(orgs, list):
        return "", ""
    org = next((item for item in orgs if isinstance(item, dict)), None)
    if not org:
        return "", ""
    org_id = str(org.get("id") or org.get("org_id") or "").strip()
    project_id = str(org.get("project_id") or org.get("default_project_id") or "").strip()
    projects = org.get("projects")
    if not project_id and isinstance(projects, list):
        project = next((item for item in projects if isinstance(item, dict)), None)
        if project:
            project_id = str(project.get("id") or project.get("project_id") or "").strip()
    return org_id, project_id


def _collect_url_candidates(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            for match in re.findall(r"https?://[^\s\"'<>]+", item):
                found.append(match)
            if item.startswith("/"):
                found.append(item)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if "url" in str(key).lower() and isinstance(child, str):
                    found.append(child)
                visit(child)
            return
        if isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    deduped: list[str] = []
    for url in found:
        if url and url not in deduped:
            deduped.append(url)
    return deduped


@dataclass
class _InputField:
    name: str
    input_type: str
    value: str


@dataclass
class _Form:
    action: str
    method: str
    inputs: list[_InputField]


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms: list[_Form] = []
        self._current: _Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "form":
            self._current = _Form(
                action=attrs_dict.get("action", ""),
                method=(attrs_dict.get("method", "post") or "post").lower(),
                inputs=[],
            )
            return
        if tag.lower() == "input":
            field = _InputField(
                name=attrs_dict.get("name", ""),
                input_type=(attrs_dict.get("type", "text") or "text").lower(),
                value=attrs_dict.get("value", ""),
            )
            if self._current is not None:
                self._current.inputs.append(field)
            return
        if tag.lower() == "button":
            field = _InputField(
                name=attrs_dict.get("name", ""),
                input_type=(attrs_dict.get("type", "submit") or "submit").lower(),
                value=attrs_dict.get("value", ""),
            )
            if self._current is not None and field.name:
                self._current.inputs.append(field)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._current is not None:
            self.forms.append(self._current)
            self._current = None

    def close(self) -> None:
        if self._current is not None:
            self.forms.append(self._current)
            self._current = None
        super().close()


def _pick_sso_form(html: str) -> _Form | None:
    parser = _FormParser()
    parser.feed(html or "")
    parser.close()
    if not parser.forms:
        return None
    return next(
        (
            form
            for form in parser.forms
            if any(field.input_type == "password" for field in form.inputs)
            or any(field.name.lower() == "prefix" for field in form.inputs)
        ),
        parser.forms[0],
    )


def _build_sso_form_data(form: _Form | None, *, prefix: str, password: str) -> dict[str, str]:
    if form is None:
        return {"prefix": prefix, "password": password}

    data: dict[str, str] = {}
    text_filled = False
    password_filled = False
    is_openai_sso_selection = any(field.name == "ssoConnection" for field in form.inputs)
    for field in form.inputs:
        name = (field.name or "").strip()
        if not name:
            continue
        lower_name = name.lower()
        lower_type = (field.input_type or "text").lower()
        if lower_type in {"submit", "button", "image"}:
            if is_openai_sso_selection and name == "ssoConnection":
                data[name] = field.value or data.get(name, "")
            continue
        if lower_type in {"reset", "file"}:
            continue
        value = field.value or ""
        if is_openai_sso_selection:
            data[name] = value
        elif lower_type == "password" or lower_name in {"password", "pass", "pwd"}:
            value = password
            password_filled = True
        elif lower_name in {"prefix", "username", "user", "login", "identifier"}:
            value = prefix
            text_filled = True
        elif lower_type in {"text", "email"} and not text_filled:
            value = prefix
            text_filled = True
        data[name] = value

    if is_openai_sso_selection:
        return data

    if not text_filled:
        data.setdefault("prefix", prefix)
    if not password_filled:
        data.setdefault("password", password)
    return data


class ChatGPTPilipalaSSOProtocol:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        email_hint: str = "",
        prefix: str = "",
        sso_password: str = PILIPALA_SSO_PASSWORD,
        sso_domain: str = PILIPALA_SSO_DOMAIN,
        timeout: int = 300,
        log_fn=print,
    ):
        self.proxy = proxy
        self.email, self.prefix = resolve_pilipala_sso_email(
            email_hint=email_hint,
            prefix=prefix,
            domain=sso_domain,
        )
        self.sso_password = (sso_password or PILIPALA_SSO_PASSWORD).strip()
        self.timeout = max(30, int(timeout or 300))
        self.log = log_fn or print
        self.http_client = OpenAIHTTPClient(proxy_url=proxy)
        self.session = self.http_client.session
        self.oauth_manager = OAuthManager(proxy_url=proxy)
        self.oauth_start = None

    def _reset_openai_session(self) -> None:
        """重新创建 OpenAI 会话，保留 SSO 前缀但丢弃上一次 add-phone/error 状态。"""
        self.http_client = OpenAIHTTPClient(proxy_url=self.proxy)
        self.session = self.http_client.session
        self.oauth_manager = OAuthManager(proxy_url=self.proxy)
        self.oauth_start = None

    def run(self) -> dict[str, Any]:
        started_at = time.time()
        last_url = ""
        for attempt in range(1, _MAX_SSO_OAUTH_ATTEMPTS + 1):
            self.oauth_start = self.oauth_manager.start_oauth()
            self.log(f"[ChatGPT SSO] OAuth 尝试 {attempt}/{_MAX_SSO_OAUTH_ATTEMPTS}: {self.email}")
            callback_url, final_url = self._run_oauth_once()
            last_url = final_url or last_url
            if callback_url:
                token_info = self.oauth_manager.handle_callback(
                    callback_url=callback_url,
                    expected_state=self.oauth_start.state,
                    code_verifier=self.oauth_start.code_verifier,
                )
                profile = self._fetch_profile(token_info.get("access_token", ""))
                return {
                    "email": token_info.get("email") or profile.get("email") or self.email,
                    "password": self.sso_password,
                    "account_id": token_info.get("account_id", ""),
                    "access_token": token_info.get("access_token", ""),
                    "refresh_token": token_info.get("refresh_token", ""),
                    "id_token": token_info.get("id_token", ""),
                    "session_token": self._cookie_value("__Secure-next-auth.session-token"),
                    "cookies": self._cookie_header(("chatgpt.com", "openai.com")),
                    "workspace_id": _decode_workspace_cookie(self._cookie_value("oai-client-auth-session")),
                    "profile": profile,
                    "sso": {
                        "provider": PILIPALA_SSO_PROVIDER,
                        "prefix": self.prefix,
                        "domain": self.email.rsplit("@", 1)[-1],
                        "attempts": attempt,
                    },
                }

            if (_is_add_phone_url(final_url) or _is_openai_auth_error_url(final_url)) and attempt < _MAX_SSO_OAUTH_ATTEMPTS:
                if _is_add_phone_url(final_url):
                    self.log("[ChatGPT SSO] 检测到 add-phone，清理会话后使用同一前缀重新走 OAuth")
                else:
                    self.log("[ChatGPT SSO] 检测到 OpenAI AuthApiFailure，清理会话后使用同一前缀重试 OAuth")
                self._reset_openai_session()
                continue

            if time.time() - started_at > self.timeout:
                break

        raise RuntimeError(f"ChatGPT SSO 未拿到 OAuth callback，最后页面: {last_url or '(unknown)'}")

    def _run_oauth_once(self) -> tuple[str, str]:
        did = self._prepare_openai_auth_session()
        payload = self._submit_openai_email(did)

        callback_url = self._find_callback_in_payload(payload)
        if callback_url:
            return callback_url, callback_url

        entry_url = self._pick_sso_entry_url(payload)
        if not entry_url:
            workspace_callback = self._try_openai_consent()
            if workspace_callback:
                return workspace_callback, workspace_callback
            raise RuntimeError(f"OpenAI SSO 响应未包含跳转地址: {_preview(json.dumps(payload, ensure_ascii=False))}")

        return self._visit_and_submit_sso(entry_url)

    def _prepare_openai_auth_session(self) -> str:
        response = self.session.get(self.oauth_start.auth_url, timeout=30, allow_redirects=True)
        if response.status_code >= 400:
            self.log(f"[ChatGPT SSO] OAuth 入口状态异常: {response.status_code} {_preview(response.text)}")
        did = self._cookie_value("oai-did") or self._cookie_value("oai-device-id")
        if not did:
            self.log("[ChatGPT SSO] 未获取到 oai device id，继续尝试 authorize/continue")
        return did

    def _request_sentinel_token(self, did: str, flow: str) -> str:
        if not did:
            return ""
        try:
            response = self.session.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=json.dumps({"p": "", "id": did, "flow": flow}, separators=(",", ":")),
                timeout=20,
            )
            if response.status_code == 200:
                return str((response.json() or {}).get("token") or "")
        except Exception as exc:
            self.log(f"[ChatGPT SSO] Sentinel 获取失败，继续尝试: {exc}")
        return ""

    def _openai_headers(self, *, referer: str, did: str, flow: str = "authorize_continue") -> dict[str, str]:
        headers = {
            "referer": referer,
            "accept": "application/json",
            "content-type": "application/json",
        }
        if did:
            headers["oai-device-id"] = did
            token = self._request_sentinel_token(did, flow)
            if token:
                headers["openai-sentinel-token"] = json.dumps(
                    {"p": "", "t": "", "c": token, "id": did, "flow": flow},
                    separators=(",", ":"),
                )
        return headers

    def _submit_openai_email(self, did: str) -> dict[str, Any]:
        body = {"username": {"kind": "email", "value": self.email}}
        response = self.session.post(
            OPENAI_API_ENDPOINTS["login_authorize_continue"],
            headers=self._openai_headers(referer="https://auth.openai.com/log-in", did=did),
            data=json.dumps(body, separators=(",", ":")),
            timeout=30,
        )
        self.log(f"[ChatGPT SSO] 提交企业邮箱: {response.status_code}")
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI authorize/continue 失败: {response.status_code} {_preview(response.text)}")
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {"html": response.text, "url": str(getattr(response, "url", "") or "")}

    def _find_callback_in_payload(self, payload: dict[str, Any]) -> str:
        for candidate in _collect_url_candidates(payload):
            absolute = urljoin("https://auth.openai.com/", candidate)
            if _is_callback_url(absolute):
                return absolute
        return ""

    def _pick_sso_entry_url(self, payload: dict[str, Any]) -> str:
        primary = _extract_continue_url(payload)
        if primary:
            return urljoin("https://auth.openai.com/", primary)
        for candidate in _collect_url_candidates(payload):
            absolute = urljoin("https://auth.openai.com/", candidate)
            if _is_callback_url(absolute):
                return absolute
            if not any(skip in absolute.lower() for skip in ("/static/", ".js", ".css", ".png", ".svg")):
                return absolute
        return ""

    def _visit_and_submit_sso(self, entry_url: str) -> tuple[str, str]:
        response, final_url = self._follow_until_html_or_terminal(entry_url, referer="https://auth.openai.com/log-in")
        for _ in range(4):
            if _is_callback_url(final_url) or _is_add_phone_url(final_url):
                return (final_url if _is_callback_url(final_url) else ""), final_url

            html = response.text or ""
            form = _pick_sso_form(html)
            if form is None:
                callback = self._try_openai_consent()
                return callback, callback or final_url

            data = _build_sso_form_data(form, prefix=self.prefix, password=self.sso_password)
            if "ssoConnection" in data:
                payload = self._submit_openai_sso_connection(data["ssoConnection"])
                callback = self._find_callback_in_payload(payload)
                if callback:
                    return callback, callback
                next_url = _extract_continue_url(payload) or self._pick_sso_entry_url(payload)
                if not next_url:
                    raise RuntimeError(f"OpenAI SSO connection 响应未包含跳转地址: {_preview(json.dumps(payload, ensure_ascii=False))}")
                response, final_url = self._follow_until_html_or_terminal(
                    urljoin("https://auth.openai.com/", next_url),
                    referer="https://auth.openai.com/sso",
                )
                continue

            action = urljoin(final_url or entry_url, form.action if form else "")
            method = (form.method if form else "post").lower()
            headers = {
                "referer": final_url or entry_url,
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "content-type": "application/x-www-form-urlencoded",
            }
            parsed = urlparse(action)
            if parsed.scheme and parsed.netloc:
                headers["origin"] = f"{parsed.scheme}://{parsed.netloc}"

            form_kind = "OpenAI SSO connection" if "ssoConnection" in data else "SSO credential"
            self.log(f"[ChatGPT SSO] 提交 {form_kind} 表单: {parsed.netloc or '(relative)'}")
            if method == "get":
                submitted = self.session.get(action, params=data, headers=headers, timeout=30, allow_redirects=False)
            else:
                submitted = self.session.post(action, data=data, headers=headers, timeout=30, allow_redirects=False)
            callback, terminal_url = self._resolve_after_sso_response(submitted, action)
            if callback or _is_add_phone_url(terminal_url):
                return callback, terminal_url
            response, final_url = self._follow_until_html_or_terminal(terminal_url or action, referer=action)

        callback = self._try_openai_consent()
        return callback, callback or final_url

    def _submit_openai_sso_connection(self, sso_connection: str) -> dict[str, Any]:
        try:
            connection_payload = json.loads(sso_connection)
        except Exception as exc:
            raise RuntimeError(f"无法解析 OpenAI SSO connection: {sso_connection[:120]}") from exc

        connection = str(connection_payload.get("connection_name") or connection_payload.get("connection") or "").strip()
        provider = connection_payload.get("connection_provider")
        if not connection:
            raise RuntimeError(f"OpenAI SSO connection 缺少 connection_name: {sso_connection[:120]}")

        body: dict[str, Any] = {"connection": connection}
        if provider not in (None, ""):
            body["connection_provider"] = provider

        did = self._cookie_value("oai-did") or self._cookie_value("oai-device-id")
        response = self.session.post(
            OPENAI_API_ENDPOINTS["login_authorize_continue"],
            headers=self._openai_headers(referer="https://auth.openai.com/sso", did=did),
            data=json.dumps(body, separators=(",", ":")),
            timeout=30,
        )
        self.log(f"[ChatGPT SSO] 提交 OpenAI SSO connection: {response.status_code}")
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI SSO connection 失败: {response.status_code} {_preview(response.text)}")
        try:
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {"html": response.text, "url": str(getattr(response, "url", "") or "")}

    def _follow_until_html_or_terminal(self, url: str, *, referer: str = ""):
        current = url
        headers = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
        if referer:
            headers["referer"] = referer
        response = None
        for _ in range(12):
            if _is_callback_url(current) or _is_add_phone_url(current) or _is_openai_auth_error_url(current):
                return response or self.session.get(current, timeout=20, allow_redirects=False), current
            response = self.session.get(current, headers=headers, timeout=30, allow_redirects=False)
            location = response.headers.get("location") or response.headers.get("Location") or ""
            if response.status_code in {301, 302, 303, 307, 308} and location:
                current = urljoin(current, location)
                continue
            content_type = (response.headers.get("content-type") or "").lower()
            if "application/json" in content_type:
                payload = response.json() if response.text else {}
                candidate = _extract_continue_url(payload) or self._find_callback_in_payload(payload)
                if candidate:
                    current = urljoin(current, candidate)
                    continue
            return response, str(getattr(response, "url", "") or current)
        return response, current

    def _resolve_after_sso_response(self, response, request_url: str) -> tuple[str, str]:
        terminal = str(getattr(response, "url", "") or request_url)
        for _ in range(12):
            location = response.headers.get("location") or response.headers.get("Location") or ""
            if response.status_code in {301, 302, 303, 307, 308} and location:
                terminal = urljoin(terminal, location)
                if _is_callback_url(terminal) or _is_add_phone_url(terminal) or _is_openai_auth_error_url(terminal):
                    return (terminal if _is_callback_url(terminal) else ""), terminal
                response = self.session.get(terminal, timeout=30, allow_redirects=False)
                terminal = str(getattr(response, "url", "") or terminal)
                continue

            if _is_callback_url(terminal) or _is_add_phone_url(terminal) or _is_openai_auth_error_url(terminal):
                return (terminal if _is_callback_url(terminal) else ""), terminal

            text = response.text or ""
            if "add-phone" in terminal or "add-phone" in text:
                return "", terminal or "https://auth.openai.com/add-phone"
            if "AuthApiFailure" in terminal or "AuthApiFailure" in text or "/error?payload=" in terminal:
                return "", terminal
            for candidate in _collect_url_candidates(text):
                absolute = urljoin(terminal, candidate)
                if _is_callback_url(absolute):
                    return absolute, absolute
                if _is_add_phone_url(absolute):
                    return "", absolute
            break

        callback = self._try_openai_consent()
        return callback, callback or terminal

    def _try_openai_consent(self) -> str:
        callback = self._visit_openai_for_callback(OPENAI_API_ENDPOINTS["codex_consent"])
        if callback:
            return callback

        workspace_id = _decode_workspace_cookie(self._cookie_value("oai-client-auth-session"))
        if not workspace_id:
            return ""
        did = self._cookie_value("oai-did") or self._cookie_value("oai-device-id")
        response = self.session.post(
            OPENAI_API_ENDPOINTS["select_workspace"],
            headers=self._openai_headers(referer=OPENAI_API_ENDPOINTS["codex_consent"], did=did),
            data=json.dumps({"workspace_id": workspace_id}, separators=(",", ":")),
            timeout=30,
        )
        if response.status_code >= 400:
            self.log(f"[ChatGPT SSO] workspace/select 失败: {response.status_code} {_preview(response.text)}")
            return ""
        payload = response.json() if response.text else {}
        org_id, project_id = _pick_first_org(payload)
        if org_id:
            org_continue = self._select_organization(org_id, project_id)
            if org_continue:
                return org_continue
        continue_url = _extract_continue_url(payload) or self._find_callback_in_payload(payload)
        if not continue_url:
            return ""
        return self._visit_openai_for_callback(urljoin("https://auth.openai.com/", continue_url))

    def _select_organization(self, org_id: str, project_id: str = "") -> str:
        did = self._cookie_value("oai-did") or self._cookie_value("oai-device-id")
        body: dict[str, Any] = {"org_id": org_id}
        if project_id:
            body["project_id"] = project_id
        response = self.session.post(
            OPENAI_API_ENDPOINTS["select_organization"],
            headers=self._openai_headers(referer=OPENAI_API_ENDPOINTS["codex_consent"], did=did),
            data=json.dumps(body, separators=(",", ":")),
            timeout=30,
        )
        if response.status_code >= 400:
            self.log(f"[ChatGPT SSO] organization/select 失败: {response.status_code} {_preview(response.text)}")
            return ""
        payload = response.json() if response.text else {}
        continue_url = _extract_continue_url(payload) or self._find_callback_in_payload(payload)
        if not continue_url:
            return ""
        return self._visit_openai_for_callback(urljoin("https://auth.openai.com/", continue_url))

    def _visit_openai_for_callback(self, url: str) -> str:
        current = url
        for _ in range(8):
            if _is_callback_url(current):
                return current
            if _is_add_phone_url(current) or _is_openai_auth_error_url(current):
                return ""
            response = self.session.get(current, timeout=30, allow_redirects=False)
            location = response.headers.get("location") or response.headers.get("Location") or ""
            if response.status_code in {301, 302, 303, 307, 308} and location:
                current = urljoin(current, location)
                continue
            payload = None
            try:
                payload = response.json()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                candidate = _extract_continue_url(payload) or self._find_callback_in_payload(payload)
                if candidate:
                    current = urljoin(current, candidate)
                    continue
            for candidate in _collect_url_candidates(response.text or ""):
                absolute = urljoin(current, candidate)
                if _is_callback_url(absolute):
                    return absolute
            return ""
        return ""

    def _fetch_profile(self, access_token: str) -> dict[str, Any]:
        if not access_token:
            return {}
        try:
            response = self.session.get(
                "https://chatgpt.com/backend-api/me",
                headers={
                    "authorization": f"Bearer {access_token}",
                    "accept": "application/json",
                },
                timeout=20,
            )
            if response.status_code == 200:
                data = response.json()
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    def _cookie_value(self, name: str) -> str:
        try:
            return str(self.session.cookies.get(name) or "")
        except Exception:
            return ""

    def _cookie_header(self, domain_substrings: tuple[str, ...]) -> str:
        items = []
        try:
            for cookie in self.session.cookies.jar:
                domain = getattr(cookie, "domain", "") or ""
                if domain_substrings and not any(part in domain for part in domain_substrings):
                    continue
                items.append(f"{cookie.name}={cookie.value}")
        except Exception:
            return ""
        return "; ".join(items)


def register_with_protocol_sso(
    *,
    proxy: str | None = None,
    email_hint: str = "",
    prefix: str = "",
    sso_password: str = PILIPALA_SSO_PASSWORD,
    sso_domain: str = PILIPALA_SSO_DOMAIN,
    timeout: int = 300,
    log_fn=print,
) -> dict[str, Any]:
    return ChatGPTPilipalaSSOProtocol(
        proxy=proxy,
        email_hint=email_hint,
        prefix=prefix,
        sso_password=sso_password,
        sso_domain=sso_domain,
        timeout=timeout,
        log_fn=log_fn,
    ).run()
