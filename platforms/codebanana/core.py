"""CodeBanana 纯 HTTP client。"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import requests
from requests import Session
from requests import exceptions as req_exc


class CodeBananaClient:
    """封装 CodeBanana 注册/登录/session 相关接口。"""

    def __init__(
        self,
        base_url: str = "https://www.codebanana.com",
        proxy: Optional[str] = None,
        timeout: int = 30,
        log_fn: Optional[Callable[[str], None]] = None,
        session: Optional[Session] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.log_fn = log_fn

        self.session = session if session is not None else requests.Session()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def _log(self, message: str) -> None:
        if self.log_fn:
            self.log_fn(message)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _cookies_dict(self) -> Dict[str, str]:
        return requests.utils.dict_from_cookiejar(self.session.cookies)

    def _extract_session_token(self) -> str:
        for name in (
            "__Secure-next-auth.session-token",
            "next-auth.session-token",
            "__Host-next-auth.session-token",
        ):
            token = self.session.cookies.get(name)
            if token:
                return token
        return ""

    def _parse_json(self, response: Any, method: str, path: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(f"CodeBanana API returned invalid JSON [{method} {path}]") from exc

    def _error_detail(self, response: Any) -> str:
        text = str(getattr(response, "text", ""))
        return text[:200] if text else "no response body"

    def _raise_if_failed_payload(self, payload: Any, method: str, path: str) -> None:
        if not isinstance(payload, dict):
            return

        status = str(payload.get("status", "")).lower()
        has_failure_flag = payload.get("success") is False or payload.get("ok") is False or status in {
            "error",
            "failed",
            "fail",
        }
        error_hint = payload.get("error")
        message_hint = payload.get("message")

        if has_failure_flag or error_hint:
            detail = error_hint or message_hint or str(payload)
            raise RuntimeError(f"CodeBanana API indicated failure [{method} {path}]: {detail}")

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        method_upper = method.upper()
        call = getattr(self.session, method.lower())
        try:
            response = call(self._url(path), timeout=self.timeout, **kwargs)
        except req_exc.RequestException as exc:
            raise RuntimeError(f"CodeBanana request failed [{method_upper} {path}]: {exc}") from exc

        status_code = getattr(response, "status_code", 0)
        if not 200 <= status_code < 300:
            detail = self._error_detail(response)
            raise RuntimeError(
                f"CodeBanana API HTTP error [{method_upper} {path}] status={status_code}: {detail}"
            )

        payload = self._parse_json(response, method_upper, path)
        self._raise_if_failed_payload(payload, method_upper, path)
        return payload

    def ensure_username_available(self, username: str) -> bool:
        self._log(f"check username: {username}")
        payload = self._request_json(
            "POST",
            "/api/auth/check-username",
            json={"username": username},
        )

        if not isinstance(payload, dict):
            raise ValueError("CodeBanana API invalid username check payload [POST /api/auth/check-username]")

        available = payload.get("available")
        if not isinstance(available, bool):
            raise ValueError("CodeBanana API missing availability flag [POST /api/auth/check-username]")

        if not available:
            detail = payload.get("message") or payload.get("error") or "username unavailable"
            raise ValueError(f"CodeBanana username unavailable [POST /api/auth/check-username]: {detail}")

        return True

    def send_verification_code(self, email: str, username: str) -> Dict[str, Any]:
        self._log(f"send verification code: {email}")
        payload = self._request_json(
            "POST",
            "/api/auth/send-verification-code",
            json={"email": email, "username": username},
        )
        if not isinstance(payload, dict):
            raise ValueError(
                "CodeBanana API invalid verification payload [POST /api/auth/send-verification-code]"
            )
        return payload

    def verify_and_register(self, email: str, username: str, password: str, code: str) -> Dict[str, Any]:
        self._log(f"verify and register: {email}")
        payload = self._request_json(
            "POST",
            "/api/auth/verify-and-register",
            json={
                "email": email,
                "username": username,
                "password": password,
                "verificationCode": code,
            },
        )
        if not isinstance(payload, dict):
            raise ValueError("CodeBanana API invalid register payload [POST /api/auth/verify-and-register]")
        return payload

    def fetch_csrf_token(self) -> str:
        payload = self._request_json("GET", "/api/auth/csrf")
        if not isinstance(payload, dict):
            raise ValueError("CodeBanana API invalid csrf payload [GET /api/auth/csrf]")

        csrf_token = payload.get("csrfToken")
        if not isinstance(csrf_token, str) or not csrf_token:
            raise ValueError("CodeBanana API missing csrfToken [GET /api/auth/csrf]")
        return csrf_token

    def login(self, email: str, password: str, csrf_token: str) -> Dict[str, Any]:
        self._log(f"login: {email}")
        self._request_json(
            "POST",
            "/api/auth/callback/credentials",
            data={
                "email": email,
                "password": password,
                "csrfToken": csrf_token,
                "redirect": "false",
                "json": "true",
                "callbackUrl": self.base_url,
            },
        )

        session_token = self._extract_session_token()
        if not session_token:
            raise RuntimeError(
                "CodeBanana login did not yield session cookie [POST /api/auth/callback/credentials]"
            )

        return {
            "session_token": session_token,
            "cookies": self._cookies_dict(),
        }

    def fetch_session(self) -> Dict[str, Any]:
        payload = self._request_json("GET", "/api/auth/session")
        if not isinstance(payload, dict):
            raise ValueError("CodeBanana API invalid session payload [GET /api/auth/session]")

        jwt_token = payload.get("jwtToken")
        if not isinstance(jwt_token, str) or not jwt_token:
            raise ValueError("CodeBanana API missing jwtToken [GET /api/auth/session]")

        return payload

    def login_and_fetch_session(self, email: str, password: str) -> Dict[str, Any]:
        csrf_token = self.fetch_csrf_token()
        login_result = self.login(email=email, password=password, csrf_token=csrf_token)
        session_json = self.fetch_session()

        if not session_json.get("jwtToken"):
            raise ValueError("CodeBanana API missing jwtToken [GET /api/auth/session]")

        return {
            "csrf_token": csrf_token,
            "session_token": login_result["session_token"],
            "cookies": login_result["cookies"],
            "session_json": session_json,
        }
