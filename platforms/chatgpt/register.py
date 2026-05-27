"""
注册流程引擎
从 main.py 中提取并重构的注册流程
"""

import re
import json
import time
import logging
import secrets
import string
from typing import Optional, Dict, Any, Tuple, Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from curl_cffi import requests as cffi_requests

from .oauth import OAuthManager, OAuthStart
from .http_client import OpenAIHTTPClient, HTTPClientError
# from ..services import EmailServiceFactory, BaseEmailService, EmailServiceType  # removed: external dep
# from ..database import crud  # removed: external dep
# from ..database.session import get_db  # removed: external dep
from .constants import (
    OPENAI_API_ENDPOINTS,
    OPENAI_PAGE_TYPES,
    generate_random_user_info,
    OTP_CODE_PATTERN,
    DEFAULT_PASSWORD_LENGTH,
    PASSWORD_CHARSET,
    AccountStatus,
    TaskStatus,
)
# from ..config.settings import get_settings  # removed: external dep


logger = logging.getLogger(__name__)


@dataclass
class RegistrationResult:
    """注册结果"""
    success: bool
    email: str = ""
    password: str = ""  # 注册密码
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""  # 会话令牌
    error_message: str = ""
    logs: list = None
    metadata: dict = None
    source: str = "register"  # 'register' 或 'login'，区分账号来源

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "email": self.email,
            "password": self.password,
            "account_id": self.account_id,
            "workspace_id": self.workspace_id,
            "access_token": self.access_token[:20] + "..." if self.access_token else "",
            "refresh_token": self.refresh_token[:20] + "..." if self.refresh_token else "",
            "id_token": self.id_token[:20] + "..." if self.id_token else "",
            "session_token": self.session_token[:20] + "..." if self.session_token else "",
            "error_message": self.error_message,
            "logs": self.logs or [],
            "metadata": self.metadata or {},
            "source": self.source,
        }


@dataclass
class SignupFormResult:
    """提交注册表单的结果"""
    success: bool
    page_type: str = ""  # 响应中的 page.type 字段
    is_existing_account: bool = False  # 是否为已注册账号
    response_data: Dict[str, Any] = None  # 完整的响应数据
    error_message: str = ""


class RegistrationEngine:
    """
    注册引擎
    负责协调邮箱服务、OAuth 流程和 OpenAI API 调用
    """

    def __init__(
        self,
        email_service: Any,
        proxy_url: Optional[str] = None,
        callback_logger: Optional[Callable[[str], None]] = None,
        task_uuid: Optional[str] = None,
        otp_total_timeout: Optional[int] = None,
        otp_resend_interval: Optional[int] = None,
        login_otp_total_timeout: Optional[int] = None,
    ):
        """
        初始化注册引擎

        Args:
            email_service: 邮箱服务实例
            proxy_url: 代理 URL
            callback_logger: 日志回调函数
            task_uuid: 任务 UUID（用于数据库记录）
        """
        self.email_service = email_service
        self.proxy_url = proxy_url
        self.callback_logger = callback_logger or (lambda msg: logger.info(msg))
        self.task_uuid = task_uuid

        # 创建 HTTP 客户端
        self.http_client = OpenAIHTTPClient(proxy_url=proxy_url)

        # 创建 OAuth 管理器
        from .constants import OAUTH_CLIENT_ID, OAUTH_AUTH_URL, OAUTH_TOKEN_URL, OAUTH_REDIRECT_URI, OAUTH_SCOPE
        self.oauth_manager = OAuthManager(
            client_id=OAUTH_CLIENT_ID,
            auth_url=OAUTH_AUTH_URL,
            token_url=OAUTH_TOKEN_URL,
            redirect_uri=OAUTH_REDIRECT_URI,
            scope=OAUTH_SCOPE,
            proxy_url=proxy_url  # 传递代理配置
        )

        # 状态变量
        self.email: Optional[str] = None
        self.password: Optional[str] = None  # 注册密码
        self.email_info: Optional[Dict[str, Any]] = None
        self.oauth_start: Optional[OAuthStart] = None
        self.session: Optional[cffi_requests.Session] = None
        self.session_token: Optional[str] = None  # 会话令牌
        self.logs: list = []
        self._otp_sent_at: Optional[float] = None  # OTP 发送时间戳
        self._is_existing_account: bool = False  # 是否为已注册账号（用于自动登录）
        self._stage: str = "initialized"
        self._signup_page_type: str = ""
        self._password_register_status_code: int = 0
        self._password_register_error_code: str = ""
        self._password_register_error_message: str = ""
        self._registration_disposition: str = ""
        self._provider_email: str = ""
        self._alias_parent_email: str = ""
        self._mailbox_alias_mode: str = ""
        self.prefer_alias_on_existing: bool = False
        self._otp_total_timeout = self._normalize_positive_int(otp_total_timeout, 120)
        self._otp_resend_interval = self._normalize_positive_int(otp_resend_interval, 10)
        self._login_otp_total_timeout = self._normalize_positive_int(login_otp_total_timeout, 120)

    @staticmethod
    def _normalize_positive_int(value: Optional[int], default: int) -> int:
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            resolved = int(default)
        return max(1, resolved)

    def _log(self, message: str, level: str = "info"):
        """记录日志"""
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] {message}"

        # 添加到日志列表
        self.logs.append(log_message)

        # 调用回调函数
        if self.callback_logger:
            self.callback_logger(message)

        # 记录到数据库（如果有关联任务）
        if self.task_uuid:
            try:
                with get_db() as db:
                    crud.append_task_log(db, self.task_uuid, message)
            except Exception as e:
                logger.warning(f"记录任务日志失败: {e}")

        # 根据级别记录到日志系统
        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        else:
            logger.info(message)

    def _set_stage(self, stage: str) -> None:
        self._stage = str(stage or "").strip() or self._stage

    def _finalize_result(self, result: RegistrationResult) -> RegistrationResult:
        metadata = dict(result.metadata or {})
        metadata.setdefault("email_service", self.email_service.service_type.value)
        metadata.setdefault("proxy_used", self.proxy_url)
        metadata["is_existing_account"] = self._is_existing_account
        metadata["last_stage"] = self._stage
        metadata["signup_page_type"] = self._signup_page_type
        metadata["password_register_status_code"] = self._password_register_status_code
        metadata["password_register_error_code"] = self._password_register_error_code
        metadata["password_register_error_message"] = self._password_register_error_message
        metadata["registration_disposition"] = self._registration_disposition or (
            "existing_account" if self._is_existing_account else ""
        )
        if self._provider_email:
            metadata.setdefault("mailbox_base_email", self._provider_email)
        if self._alias_parent_email:
            metadata.setdefault("mailbox_alias_parent_email", self._alias_parent_email)
        if self._mailbox_alias_mode:
            metadata.setdefault("mailbox_alias_mode", self._mailbox_alias_mode)
        metadata["mailbox_alias_used"] = bool(
            self._provider_email and self.email and self._provider_email != self.email
        )
        if self.email:
            metadata.setdefault("resolved_email", self.email)
        if result.workspace_id:
            metadata["workspace_id"] = result.workspace_id
        if result.account_id:
            metadata["account_id"] = result.account_id
        result.metadata = metadata
        result.logs = list(self.logs)
        return result

    def _generate_password(self, length: int = DEFAULT_PASSWORD_LENGTH) -> str:
        """生成随机密码"""
        return ''.join(secrets.choice(PASSWORD_CHARSET) for _ in range(length))

    def _classify_register_error(self, *, message: str, code: str, status_code: int) -> str:
        normalized_message = str(message or "").strip().lower()
        normalized_code = str(code or "").strip().lower()
        if (
            "user_already_exists" in normalized_message
            or normalized_code == "user_already_exists"
        ):
            return "existing_account"
        return ""

    def _check_ip_location(self) -> Tuple[bool, Optional[str]]:
        """检查 IP 地理位置"""
        try:
            return self.http_client.check_ip_location()
        except Exception as e:
            self._log(f"检查 IP 地理位置失败: {e}", "error")
            return False, None

    def _create_email(self) -> bool:
        """创建邮箱"""
        try:
            self._log(f"正在创建 {self.email_service.service_type.value} 邮箱...")
            self.email_info = self.email_service.create_email()

            if not self.email_info or "email" not in self.email_info:
                self._log("创建邮箱失败: 返回信息不完整", "error")
                return False

            provider_email = str(self.email_info.get("email", "") or "").strip()
            self._provider_email = provider_email
            requested_email = str(self.email or "").strip()
            self.email = requested_email or provider_email
            if requested_email and provider_email and requested_email != provider_email:
                self._log(f"Mailbox base email: {provider_email}")
                self._log(f"Using alias email: {self.email}")
            else:
                self._log(f"Created email: {self.email}")
            return True

        except Exception as e:
            self._log(f"创建邮箱失败: {e}", "error")
            return False

    def _start_oauth(self) -> bool:
        """开始 OAuth 流程"""
        try:
            self._log("开始 OAuth 授权流程...")
            self.oauth_start = self.oauth_manager.start_oauth()
            self._log(f"OAuth URL 已生成: {self.oauth_start.auth_url[:80]}...")
            return True
        except Exception as e:
            self._log(f"生成 OAuth URL 失败: {e}", "error")
            return False

    def _init_session(self) -> bool:
        """初始化会话"""
        try:
            self.session = self.http_client.session
            return True
        except Exception as e:
            self._log(f"初始化会话失败: {e}", "error")
            return False

    def _get_device_id(self) -> Optional[str]:
        """获取 Device ID"""
        try:
            if not self.oauth_start:
                return None

            response = self.session.get(
                self.oauth_start.auth_url,
                timeout=15
            )
            did = self.session.cookies.get("oai-did")
            self._log(f"Device ID: {did}")
            return did

        except Exception as e:
            self._log(f"获取 Device ID 失败: {e}", "error")
            return None


    def _sync_device_id(self, did: Optional[str]) -> str:
        """???? ID ??????? Cookie?"""
        device_id = str(did or "").strip()
        if not device_id or not self.session:
            return ""

        for cookie_name in ("oai-did", "oai-device-id"):
            for domain in ("auth.openai.com", ".auth.openai.com", ".openai.com"):
                try:
                    self.session.cookies.set(cookie_name, device_id, domain=domain, path="/")
                except Exception:
                    continue
        return device_id

    def _request_sentinel_token(self, did: str, flow: str = "authorize_continue") -> Optional[str]:
        """? flow ?? Sentinel ???"""
        try:
            device_id = self._sync_device_id(did)
            if not device_id:
                self._log("Sentinel ???? Device ID", "warning")
                return None

            sen_req_body = json.dumps({
                "p": "",
                "id": device_id,
                "flow": flow,
            }, separators=(",", ":"))

            response = self.http_client.post(
                OPENAI_API_ENDPOINTS["sentinel"],
                headers={
                    "origin": "https://sentinel.openai.com",
                    "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6",
                    "content-type": "text/plain;charset=UTF-8",
                },
                data=sen_req_body,
            )

            if response.status_code == 200:
                sen_token = response.json().get("token")
                self._log(f"Sentinel token ?????flow={flow}?")
                return sen_token

            self._log(f"Sentinel ?????flow={flow}?: {response.status_code}", "warning")
            return None

        except Exception as e:
            self._log(f"Sentinel ?????flow={flow}?: {e}", "warning")
            return None

    def _check_sentinel(self, did: str) -> Optional[str]:
        """?? Sentinel ??"""
        return self._request_sentinel_token(did, "authorize_continue")

    def _build_oauth_headers(
        self,
        *,
        referer: str,
        did: str,
        flow: Optional[str] = None,
        accept: str = "application/json",
        content_type: str = "application/json",
    ) -> Dict[str, str]:
        """?? OAuth ??????"""
        device_id = self._sync_device_id(did)
        headers: Dict[str, str] = {
            "referer": referer,
            "accept": accept,
            "content-type": content_type,
        }
        if device_id:
            headers["oai-device-id"] = device_id
        if flow:
            sen_token = self._request_sentinel_token(device_id, flow)
            if sen_token:
                headers["openai-sentinel-token"] = json.dumps({
                    "p": "",
                    "t": "",
                    "c": sen_token,
                    "id": device_id,
                    "flow": flow,
                }, separators=(",", ":"))
            else:
                self._log(f"???? Sentinel token????? flow={flow}", "warning")
        return headers

    def _safe_json(self, response) -> Dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _extract_continue_url_from_payload(self, payload: Optional[Dict[str, Any]]) -> str:
        data = payload or {}
        candidates = [
            data.get("continue_url"),
            data.get("continueUrl"),
            data.get("redirect_url"),
            data.get("redirectUrl"),
        ]
        nested = data.get("data")
        if isinstance(nested, dict):
            candidates.extend([
                nested.get("continue_url"),
                nested.get("continueUrl"),
                nested.get("redirect_url"),
                nested.get("redirectUrl"),
            ])
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value
        return ""

    def _extract_page_type_from_payload(self, payload: Optional[Dict[str, Any]]) -> str:
        data = payload or {}
        page = data.get("page")
        if isinstance(page, dict):
            return str(page.get("type") or "").strip()
        return ""

    def _submit_signup_form(self, did: str, sen_token: Optional[str]) -> SignupFormResult:
        """
        提交注册表单

        Returns:
            SignupFormResult: 提交结果，包含账号状态判断
        """
        try:
            signup_body = f'{{"username":{{"value":"{self.email}","kind":"email"}},"screen_hint":"signup"}}'

            headers = {
                "referer": "https://auth.openai.com/create-account",
                "accept": "application/json",
                "content-type": "application/json",
            }

            if sen_token:
                sentinel = f'{{"p": "", "t": "", "c": "{sen_token}", "id": "{did}", "flow": "authorize_continue"}}'
                headers["openai-sentinel-token"] = sentinel

            response = self.session.post(
                OPENAI_API_ENDPOINTS["signup"],
                headers=headers,
                data=signup_body,
            )

            self._log(f"提交注册表单状态: {response.status_code}")

            if response.status_code != 200:
                return SignupFormResult(
                    success=False,
                    error_message=f"HTTP {response.status_code}: {response.text[:200]}"
                )

            # 解析响应判断账号状态
            try:
                response_data = response.json()
                page_type = response_data.get("page", {}).get("type", "")
                self._signup_page_type = page_type
                self._log(f"响应页面类型: {page_type}")

                # 判断是否为已注册账号
                is_existing = page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]
                is_password_registration = page_type in {
                    OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"],
                    "password",
                }

                if is_existing:
                    self._log("检测到邮箱进入登录 / OTP 流程")
                    self._is_existing_account = True
                    if self._registration_disposition != "existing_account":
                        self._registration_disposition = "login_flow"
                elif is_password_registration:
                    self._registration_disposition = "new_account"

                return SignupFormResult(
                    success=True,
                    page_type=page_type,
                    is_existing_account=is_existing,
                    response_data=response_data
                )

            except Exception as parse_error:
                self._log(f"解析响应失败: {parse_error}", "warning")
                # 无法解析，默认成功
                return SignupFormResult(success=True)

        except Exception as e:
            self._log(f"提交注册表单失败: {e}", "error")
            return SignupFormResult(success=False, error_message=str(e))

    def _register_password(self) -> Tuple[bool, Optional[str]]:
        """注册密码"""
        try:
            # 生成密码
            password = self._generate_password()
            self.password = password  # 保存密码到实例变量
            self._log(f"生成密码: {password}")

            # 提交密码注册
            register_body = json.dumps({
                "password": password,
                "username": self.email
            })

            response = self.session.post(
                OPENAI_API_ENDPOINTS["register"],
                headers={
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=register_body,
            )

            self._log(f"提交密码状态: {response.status_code}")
            self._password_register_status_code = int(response.status_code or 0)

            if response.status_code != 200:
                error_text = response.text[:500]
                self._log(f"密码注册失败: {error_text}", "warning")

                # 解析错误信息，判断是否是邮箱已注册
                try:
                    error_json = response.json()
                    error_msg = error_json.get("error", {}).get("message", "")
                    error_code = error_json.get("error", {}).get("code", "")
                    self._password_register_error_message = str(error_msg or "")
                    self._password_register_error_code = str(error_code or "")
                    disposition = self._classify_register_error(
                        message=error_msg,
                        code=error_code,
                        status_code=response.status_code,
                    )
                    if disposition:
                        self._registration_disposition = disposition

                    # 检测邮箱已注册的情况
                    if disposition == "existing_account":
                        self._log(f"邮箱 {self.email} 可能已在 OpenAI 注册过", "error")
                        # 标记此邮箱为已注册状态
                        self._mark_email_as_registered()
                except Exception:
                    pass

                return False, None

            self._password_register_error_message = ""
            self._password_register_error_code = ""
            self._registration_disposition = "new_account"
            return True, password

        except Exception as e:
            self._log(f"密码注册失败: {e}", "error")
            return False, None

    def _mark_email_as_registered(self):
        """标记邮箱为已注册状态（用于防止重复尝试）"""
        try:
            with get_db() as db:
                # 检查是否已存在该邮箱的记录
                existing = crud.get_account_by_email(db, self.email)
                if not existing:
                    # 创建一个失败记录，标记该邮箱已注册过
                    crud.create_account(
                        db,
                        email=self.email,
                        password="",  # 空密码表示未成功注册
                        email_service=self.email_service.service_type.value,
                        email_service_id=self.email_info.get("service_id") if self.email_info else None,
                        status="failed",
                        extra_data={"register_failed_reason": "email_already_registered_on_openai"}
                    )
                    self._log(f"已在数据库中标记邮箱 {self.email} 为已注册状态")
        except Exception as e:
            logger.warning(f"标记邮箱状态失败: {e}")

    def _send_verification_code(self) -> bool:
        """发送验证码"""
        try:
            snapshot_ids = None
            load_ids_fn = getattr(self.email_service, "_load_current_ids", None)
            set_ids_fn = getattr(self.email_service, "set_otp_before_ids", None)
            if callable(load_ids_fn) and callable(set_ids_fn):
                try:
                    snapshot_ids = load_ids_fn()
                    self._log(f"OTP 邮箱快照已记录 (pre_send)，当前邮件数: {len(snapshot_ids)}")
                except Exception as e:
                    self._log(f"记录 OTP 邮箱快照失败: {e}", "warning")
            else:
                self._capture_otp_snapshot("pre_send")

            send_started_at = time.time()

            response = self.session.get(
                OPENAI_API_ENDPOINTS["send_otp"],
                headers={
                    "referer": "https://auth.openai.com/create-account/password",
                    "accept": "application/json",
                },
            )

            self._log(f"验证码发送状态: {response.status_code}")
            if response.status_code == 200:
                if snapshot_ids is not None and callable(set_ids_fn):
                    set_ids_fn(snapshot_ids)
                self._otp_sent_at = send_started_at
                return True
            return False

        except Exception as e:
            self._log(f"发送验证码失败: {e}", "error")
            return False

    def _capture_otp_snapshot(self, reason: str = "") -> None:
        capture_fn = getattr(self.email_service, "capture_otp_snapshot", None)
        if not callable(capture_fn):
            return
        try:
            count = capture_fn()
            suffix = f" ({reason})" if reason else ""
            self._log(f"OTP 邮箱快照已记录{suffix}，当前邮件数: {count}")
        except Exception as e:
            self._log(f"记录 OTP 邮箱快照失败: {e}", "warning")

    def _get_verification_code(self, *, total_timeout: Optional[int] = None, resend_interval: Optional[int] = None) -> Optional[str]:
        """获取验证码"""
        try:
            receive_email = self._provider_email or self.email or ""
            if receive_email and self.email and receive_email != self.email:
                self._log(f"正在等待验证码... 注册邮箱: {self.email} / 收件邮箱: {receive_email}")
            else:
                self._log(f"正在等待邮箱 {self.email} 的验证码...")
            email_id = self.email_info.get("service_id") if self.email_info else None
            total_timeout = self._normalize_positive_int(
                total_timeout if total_timeout is not None else self._otp_total_timeout,
                self._otp_total_timeout,
            )
            resend_interval = self._normalize_positive_int(
                resend_interval if resend_interval is not None else self._otp_resend_interval,
                self._otp_resend_interval,
            )
            deadline = time.time() + max(1, int(total_timeout))
            resend_interval = max(1, int(resend_interval))
            resend_count = 0

            while True:
                remaining = max(0, int(deadline - time.time()))
                if remaining <= 0:
                    self._log("等待验证码超时", "error")
                    return None

                window = min(resend_interval, remaining)
                try:
                    code = self.email_service.get_verification_code(
                        email=self.email,
                        email_id=email_id,
                        timeout=window,
                        pattern=OTP_CODE_PATTERN,
                        otp_sent_at=self._otp_sent_at,
                    )
                except TimeoutError:
                    code = None

                if code:
                    self._log(f"成功获取验证码: {code}")
                    return code

                try:
                    final_code = self.email_service.get_verification_code(
                        email=self.email,
                        email_id=email_id,
                        timeout=1,
                        pattern=OTP_CODE_PATTERN,
                        otp_sent_at=self._otp_sent_at,
                    )
                except TimeoutError:
                    final_code = None

                if final_code:
                    self._log(f"重发前最后检查命中验证码: {final_code}")
                    return final_code

                remaining_after_window = max(0, int(deadline - time.time()))
                if remaining_after_window <= 0:
                    self._log("等待验证码超时", "error")
                    return None

                resend_count += 1
                self._log(f"{window}s 内未收到验证码，执行第 {resend_count} 次重发...", "warning")
                if not self._send_verification_code():
                    self._log("验证码重发失败，继续等待下一轮", "warning")

        except Exception as e:
            self._log(f"获取验证码失败: {e}", "error")
            return None

    def _validate_verification_code(self, code: str) -> Tuple[bool, int, str]:
        """验证验证码"""
        try:
            code_body = f'{{"code":"{code}"}}'

            response = self.session.post(
                OPENAI_API_ENDPOINTS["validate_otp"],
                headers={
                    "referer": "https://auth.openai.com/email-verification",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=code_body,
            )

            self._log(f"验证码校验状态: {response.status_code}")
            body_preview = (response.text or "")[:300]
            if response.status_code != 200 and body_preview:
                self._log(f"验证码校验响应: {body_preview}", "warning")
            return response.status_code == 200, response.status_code, body_preview

        except Exception as e:
            self._log(f"验证验证码失败: {e}", "error")
            return False, 0, str(e)

    def _validate_verification_code_with_retry(self, code: str, *, max_retries: int = 2) -> bool:
        current_code = code
        attempt = 0
        while True:
            ok, status_code, _body_preview = self._validate_verification_code(current_code)
            if ok:
                return True
            if status_code != 401 or attempt >= max_retries:
                return False

            attempt += 1
            self._log(f"验证码校验返回 401，准备重新发送并重试第 {attempt} 次", "warning")
            if not self._send_verification_code():
                self._log("401 重试时重新发送验证码失败", "error")
                return False

            next_code = self._get_verification_code()
            if not next_code:
                self._log("401 重试时未拿到新验证码", "error")
                return False

            if next_code == current_code:
                self._log("401 重试拿到的验证码与上次相同，继续按新邮件流程校验", "warning")
            current_code = next_code

    def _create_user_account(self) -> bool:
        """创建用户账户"""
        try:
            user_info = generate_random_user_info()
            self._log(f"生成用户信息: {user_info['name']}, 生日: {user_info['birthdate']}")
            create_account_body = json.dumps(user_info)

            response = self.session.post(
                OPENAI_API_ENDPOINTS["create_account"],
                headers={
                    "referer": "https://auth.openai.com/about-you",
                    "accept": "application/json",
                    "content-type": "application/json",
                },
                data=create_account_body,
            )

            self._log(f"账户创建状态: {response.status_code}")

            if response.status_code != 200:
                self._log(f"账户创建失败: {response.text[:200]}", "warning")
                return False

            return True

        except Exception as e:
            self._log(f"创建账户失败: {e}", "error")
            return False

    def _get_workspace_id(self) -> Optional[str]:
        """获取 Workspace ID"""
        try:
            auth_cookie = self.session.cookies.get("oai-client-auth-session")
            if not auth_cookie:
                self._log("未能获取到授权 Cookie", "error")
                return None

            # 解码 JWT
            import base64
            import json as json_module

            try:
                segments = auth_cookie.split(".")
                if len(segments) < 1:
                    self._log("授权 Cookie 格式错误", "error")
                    return None

                # 解码第一个 segment
                payload = segments[0]
                pad = "=" * ((4 - (len(payload) % 4)) % 4)
                decoded = base64.urlsafe_b64decode((payload + pad).encode("ascii"))
                auth_json = json_module.loads(decoded.decode("utf-8"))

                workspaces = auth_json.get("workspaces") or []
                if not workspaces:
                    self._log("授权 Cookie 里没有 workspace 信息", "error")
                    return None

                workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
                if not workspace_id:
                    self._log("无法解析 workspace_id", "error")
                    return None

                self._log(f"Workspace ID: {workspace_id}")
                return workspace_id

            except Exception as e:
                self._log(f"解析授权 Cookie 失败: {e}", "error")
                return None

        except Exception as e:
            self._log(f"获取 Workspace ID 失败: {e}", "error")
            return None

    def _extract_callback_url(self, url: str) -> Optional[str]:
        candidate = str(url or "").strip()
        if candidate and "code=" in candidate and "state=" in candidate:
            self._log(f"??? OAuth ?? URL: {candidate[:100]}...")
            return candidate
        return None

    def _visit_oauth_url(self, url: str, *, referer: Optional[str] = None) -> tuple[Optional[str], Optional[str], str]:
        """?? OAuth ??????????? workspace ? callback?"""
        try:
            headers = {
                "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
            if referer:
                headers["referer"] = referer

            response = self.session.get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=20,
            )
            final_url = str(getattr(response, "url", "") or "").strip()
            if final_url:
                self._log(f"OAuth ????: {final_url[:100]}...")

            callback_url = self._extract_callback_url(final_url)
            if callback_url:
                return None, callback_url, final_url

            payload = self._safe_json(response)
            continue_url = self._extract_continue_url_from_payload(payload)
            callback_url = self._extract_callback_url(continue_url)
            if callback_url:
                return None, callback_url, final_url

            workspace_id = self._get_workspace_id()
            if workspace_id:
                return workspace_id, None, final_url

            return None, None, final_url

        except Exception as e:
            self._log(f"?? OAuth ????: {e}", "warning")
            return None, None, ""

    def _submit_login_email_for_oauth_retry(self, did: str) -> Optional[Dict[str, Any]]:
        """OAuth ??????????"""
        try:
            login_body = json.dumps({
                "username": {
                    "kind": "email",
                    "value": self.email,
                }
            }, separators=(",", ":"))
            response = self.session.post(
                OPENAI_API_ENDPOINTS["login_authorize_continue"],
                headers=self._build_oauth_headers(
                    referer="https://auth.openai.com/log-in",
                    did=did,
                    flow="authorize_continue",
                ),
                data=login_body,
                timeout=20,
            )
            self._log(f"OAuth ??????????: {response.status_code}")
            if response.status_code != 200:
                self._log(f"????????: {response.text[:300]}", "warning")
                return None
            payload = self._safe_json(response)
            page_type = self._extract_page_type_from_payload(payload)
            if page_type:
                self._log(f"??????????: {page_type}")
            return payload
        except Exception as e:
            self._log(f"OAuth ??????????: {e}", "error")
            return None

    def _submit_login_password_for_oauth_retry(self, did: str) -> Optional[Dict[str, Any]]:
        """OAuth ??????????"""
        if not self.password:
            self._log("OAuth ?????????????????", "error")
            return None
        try:
            self._capture_otp_snapshot("oauth_retry_before_password_verify")
            password_body = json.dumps({
                "password": self.password,
            }, separators=(",", ":"))
            response = self.session.post(
                OPENAI_API_ENDPOINTS["password_verify"],
                headers=self._build_oauth_headers(
                    referer="https://auth.openai.com/log-in/password",
                    did=did,
                    flow="password_verify",
                ),
                data=password_body,
                timeout=20,
            )
            self._log(f"OAuth ????????: {response.status_code}")
            if response.status_code != 200:
                self._log(f"????????: {response.text[:300]}", "warning")
                return None
            payload = self._safe_json(response)
            page_type = self._extract_page_type_from_payload(payload)
            if page_type:
                self._log(f"??????????: {page_type}")
            return payload
        except Exception as e:
            self._log(f"OAuth ??????????: {e}", "error")
            return None

    def _get_login_verification_code(self, *, total_timeout: Optional[int] = None) -> Optional[str]:
        """?? OAuth ??????? OTP???? resend?"""
        try:
            receive_email = self._provider_email or self.email or ""
            if receive_email and self.email and receive_email != self.email:
                self._log(f"???? OTP... ????: {self.email} / ????: {receive_email}")
            else:
                self._log(f"???? OTP... ??: {self.email}")
            email_id = self.email_info.get("service_id") if self.email_info else None
            total_timeout = self._normalize_positive_int(
                total_timeout if total_timeout is not None else self._login_otp_total_timeout,
                self._login_otp_total_timeout,
            )
            code = self.email_service.get_verification_code(
                email=self.email,
                email_id=email_id,
                timeout=max(1, int(total_timeout)),
                pattern=OTP_CODE_PATTERN,
                otp_sent_at=self._otp_sent_at,
                strict_otp_sent_at=True,
            )
            if code:
                self._log(f"?????? OTP: {code}")
            return code
        except TimeoutError:
            self._log("???? OTP ??", "error")
            return None
        except Exception as e:
            self._log(f"???? OTP ??: {e}", "error")
            return None

    def _validate_login_otp_for_oauth_retry(self, did: str, *, max_retries: int = 2) -> Optional[Dict[str, Any]]:
        """OAuth ??????? OTP?"""
        attempt = 0
        while attempt <= max_retries:
            self._otp_sent_at = time.time()
            code = self._get_login_verification_code()
            if not code:
                return None

            try:
                response = self.session.post(
                    OPENAI_API_ENDPOINTS["validate_otp"],
                    headers=self._build_oauth_headers(
                        referer="https://auth.openai.com/email-verification",
                        did=did,
                    ),
                    data=json.dumps({"code": code}, separators=(",", ":")),
                    timeout=20,
                )
            except Exception as e:
                self._log(f"OAuth ?????? OTP ??: {e}", "error")
                return None

            self._log(f"OAuth ???? OTP ????: {response.status_code}")
            payload = self._safe_json(response)
            if response.status_code == 200:
                return payload

            preview = (response.text or "")[:300]
            if preview:
                self._log(f"?? OTP ????: {preview}", "warning")
            if response.status_code != 401 or attempt >= max_retries:
                return None

            attempt += 1
            self._log(f"?? OTP ?? 401?????? {attempt} ?", "warning")

        return None

    def _pick_first_org(self, payload: Optional[Dict[str, Any]]) -> tuple[str, str]:
        data = payload or {}
        orgs = data.get("orgs")
        if not isinstance(orgs, list):
            nested = data.get("data")
            if isinstance(nested, dict):
                orgs = nested.get("orgs")
        if not isinstance(orgs, list):
            return "", ""

        org = next((item for item in orgs if isinstance(item, dict)), None)
        if not isinstance(org, dict):
            return "", ""

        org_id = str(org.get("id") or org.get("org_id") or "").strip()
        project_id = str(org.get("project_id") or org.get("default_project_id") or "").strip()
        projects = org.get("projects")
        if not project_id and isinstance(projects, list):
            project = next((item for item in projects if isinstance(item, dict)), None)
            if isinstance(project, dict):
                project_id = str(project.get("id") or project.get("project_id") or "").strip()
        return org_id, project_id

    def _select_organization(self, org_id: str, project_id: str = "") -> Optional[str]:
        """? workspace/select ??????????????"""
        try:
            body: Dict[str, Any] = {"org_id": org_id}
            if project_id:
                body["project_id"] = project_id

            did = self.session.cookies.get("oai-did") or self.session.cookies.get("oai-device-id") or ""
            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_organization"],
                headers=self._build_oauth_headers(
                    referer=OPENAI_API_ENDPOINTS["codex_consent"],
                    did=str(did),
                ),
                data=json.dumps(body, separators=(",", ":")),
                timeout=20,
            )
            self._log(f"?? organization ??: {response.status_code}")
            if response.status_code != 200:
                self._log(f"?? organization ??: {response.text[:300]}", "warning")
                return None

            payload = self._safe_json(response)
            continue_url = self._extract_continue_url_from_payload(payload)
            if continue_url:
                self._log(f"Organization Continue URL: {continue_url[:100]}...")
                return continue_url

            callback_url = self._extract_callback_url(str(getattr(response, "url", "") or ""))
            if callback_url:
                return callback_url

            self._log("organization/select ???? continue_url", "warning")
            return None
        except Exception as e:
            self._log(f"?? organization ??: {e}", "error")
            return None

    def _retry_workspace_context_via_oauth(self) -> tuple[Optional[str], Optional[str]]:
        """??????????? OAuth ????????? workspace ????"""
        try:
            self._log("?? Cookie ?? workspace????????? OAuth ????...", "warning")
            if not self._start_oauth() or not self.oauth_start:
                self._log("OAuth ???????", "error")
                return None, None

            workspace_id, callback_url, _final_url = self._visit_oauth_url(self.oauth_start.auth_url)
            if workspace_id or callback_url:
                return workspace_id, callback_url

            did = self.session.cookies.get("oai-did") or self.session.cookies.get("oai-device-id") or ""
            if not did:
                did = self._get_device_id() or self.session.cookies.get("oai-did") or self.session.cookies.get("oai-device-id") or ""
            did = self._sync_device_id(did)
            if not did:
                self._log("OAuth ?????? Device ID", "error")
                return None, None

            login_payload = self._submit_login_email_for_oauth_retry(did)
            if not login_payload:
                return None, None

            workspace_id = self._get_workspace_id()
            if workspace_id:
                return workspace_id, None

            login_continue_url = self._extract_continue_url_from_payload(login_payload)
            callback_url = self._extract_callback_url(login_continue_url)
            if callback_url:
                return None, callback_url
            if login_continue_url:
                workspace_id, callback_url, _ = self._visit_oauth_url(
                    login_continue_url,
                    referer="https://auth.openai.com/log-in",
                )
                if workspace_id or callback_url:
                    return workspace_id, callback_url

            password_payload = self._submit_login_password_for_oauth_retry(did)
            if not password_payload:
                return None, None

            workspace_id = self._get_workspace_id()
            if workspace_id:
                return workspace_id, None

            page_type = self._extract_page_type_from_payload(password_payload)
            continue_url = self._extract_continue_url_from_payload(password_payload)
            if page_type == OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]:
                otp_payload = self._validate_login_otp_for_oauth_retry(did)
                if not otp_payload:
                    return None, None
                continue_url = self._extract_continue_url_from_payload(otp_payload) or continue_url

            callback_url = self._extract_callback_url(continue_url)
            if callback_url:
                return None, callback_url

            if continue_url:
                workspace_id, callback_url, _ = self._visit_oauth_url(
                    continue_url,
                    referer="https://auth.openai.com/log-in/password",
                )
                if workspace_id or callback_url:
                    return workspace_id, callback_url

            workspace_id, callback_url, _ = self._visit_oauth_url(
                OPENAI_API_ENDPOINTS["codex_consent"],
                referer=self.oauth_start.auth_url,
            )
            if workspace_id or callback_url:
                return workspace_id, callback_url

            self._log("OAuth ??????? workspace ? callback", "error")
            return None, None
        except Exception as e:
            self._log(f"OAuth ???? workspace ??: {e}", "error")
            return None, None

    def _select_workspace(self, workspace_id: str) -> Optional[str]:
        """?? Workspace?????????? organization?"""
        try:
            select_body = json.dumps({"workspace_id": workspace_id}, separators=(",", ":"))
            did = self.session.cookies.get("oai-did") or self.session.cookies.get("oai-device-id") or ""

            response = self.session.post(
                OPENAI_API_ENDPOINTS["select_workspace"],
                headers=self._build_oauth_headers(
                    referer=OPENAI_API_ENDPOINTS["codex_consent"],
                    did=str(did),
                ),
                data=select_body,
                timeout=20,
            )

            if response.status_code != 200:
                self._log(f"?? workspace ??: {response.status_code}", "error")
                self._log(f"??: {response.text[:200]}", "warning")
                return None

            payload = self._safe_json(response)
            continue_url = self._extract_continue_url_from_payload(payload)
            org_id, project_id = self._pick_first_org(payload)
            if org_id:
                self._log(f"??? organization?????: {org_id}")
                org_continue_url = self._select_organization(org_id, project_id)
                if org_continue_url:
                    return org_continue_url

            if not continue_url:
                callback_url = self._extract_callback_url(str(getattr(response, "url", "") or ""))
                if callback_url:
                    return callback_url
                self._log("workspace/select ???? continue_url", "error")
                return None

            self._log(f"Continue URL: {continue_url[:100]}...")
            return continue_url

        except Exception as e:
            self._log(f"?? Workspace ??: {e}", "error")
            return None

    def _follow_redirects(self, start_url: str) -> Optional[str]:
        """跟随重定向链，寻找回调 URL"""
        try:
            current_url = start_url
            callback_url = self._extract_callback_url(current_url)
            if callback_url:
                return callback_url
            max_redirects = 6

            for i in range(max_redirects):
                self._log(f"重定向 {i+1}/{max_redirects}: {current_url[:100]}...")

                response = self.session.get(
                    current_url,
                    allow_redirects=False,
                    timeout=15
                )

                location = response.headers.get("Location") or ""

                # 如果不是重定向状态码，停止
                if response.status_code not in [301, 302, 303, 307, 308]:
                    self._log(f"非重定向状态码: {response.status_code}")
                    break

                if not location:
                    self._log("重定向响应缺少 Location 头")
                    break

                # 构建下一个 URL
                import urllib.parse
                next_url = urllib.parse.urljoin(current_url, location)

                # 检查是否包含回调参数
                if "code=" in next_url and "state=" in next_url:
                    self._log(f"找到回调 URL: {next_url[:100]}...")
                    return next_url

                current_url = next_url

            self._log("未能在重定向链中找到回调 URL", "error")
            return None

        except Exception as e:
            self._log(f"跟随重定向失败: {e}", "error")
            return None

    def _handle_oauth_callback(self, callback_url: str) -> Optional[Dict[str, Any]]:
        """处理 OAuth 回调"""
        try:
            if not self.oauth_start:
                self._log("OAuth 流程未初始化", "error")
                return None

            self._log("处理 OAuth 回调...")
            token_info = self.oauth_manager.handle_callback(
                callback_url=callback_url,
                expected_state=self.oauth_start.state,
                code_verifier=self.oauth_start.code_verifier
            )

            self._log("OAuth 授权成功")
            return token_info

        except Exception as e:
            self._log(f"处理 OAuth 回调失败: {e}", "error")
            return None

    def run(self) -> RegistrationResult:
        """
        执行完整的注册流程

        支持已注册账号自动登录：
        - 如果检测到邮箱已注册，自动切换到登录流程
        - 已注册账号跳过：设置密码、发送验证码、创建用户账户
        - 共用步骤：获取验证码、验证验证码、Workspace 和 OAuth 回调

        Returns:
            RegistrationResult: 注册结果
        """
        result = RegistrationResult(success=False, logs=self.logs)

        try:
            self._set_stage("starting")
            self._log("=" * 60)
            self._log("开始注册流程")
            self._log("=" * 60)

            # 1. 检查 IP 地理位置
            self._set_stage("ip_check")
            self._log("1. 检查 IP 地理位置...")
            ip_ok, location = self._check_ip_location()
            if not ip_ok:
                result.error_message = f"IP 地理位置不支持: {location}"
                self._log(f"IP 检查失败: {location}", "error")
                return self._finalize_result(result)

            self._log(f"IP 位置: {location}")

            # 2. 创建邮箱
            self._set_stage("mailbox_ready")
            self._log("2. 创建邮箱...")
            if not self._create_email():
                result.error_message = "创建邮箱失败"
                return self._finalize_result(result)

            result.email = self.email

            # 3. 初始化会话
            self._set_stage("session_ready")
            self._log("3. 初始化会话...")
            if not self._init_session():
                result.error_message = "初始化会话失败"
                return self._finalize_result(result)

            # 4. 开始 OAuth 流程
            self._set_stage("oauth_started")
            self._log("4. 开始 OAuth 授权流程...")
            if not self._start_oauth():
                result.error_message = "开始 OAuth 流程失败"
                return self._finalize_result(result)

            # 5. 获取 Device ID
            self._set_stage("device_ready")
            self._log("5. 获取 Device ID...")
            did = self._get_device_id()
            if not did:
                result.error_message = "获取 Device ID 失败"
                return self._finalize_result(result)

            # 6. 检查 Sentinel 拦截
            self._set_stage("sentinel_checked")
            self._log("6. 检查 Sentinel 拦截...")
            sen_token = self._check_sentinel(did)
            if sen_token:
                self._log("Sentinel 检查通过")
            else:
                self._log("Sentinel 检查失败或未启用", "warning")

            # 7. 提交注册表单 + 解析响应判断账号状态
            self._set_stage("signup_submitted")
            self._log("7. 提交注册表单...")
            self._capture_otp_snapshot("pre_signup_submit")
            signup_result = self._submit_signup_form(did, sen_token)
            if not signup_result.success:
                result.error_message = f"提交注册表单失败: {signup_result.error_message}"
                return self._finalize_result(result)

            # 8. [已注册账号跳过] 注册密码
            if self._is_existing_account:
                if self.prefer_alias_on_existing:
                    self._set_stage("alias_retry_ready")
                    self._log("8. [existing_account] alias mode enabled, skip login and retry with alias", "warning")
                    result.error_message = "existing account detected"
                    return self._finalize_result(result)
                self._log("8. [已注册账号] 跳过密码设置，OTP 已自动发送")
            else:
                self._set_stage("password_registered")
                self._log("8. 注册密码...")
                password_ok, password = self._register_password()
                if not password_ok:
                    result.error_message = "注册密码失败"
                    return self._finalize_result(result)

            # 9. [已注册账号跳过] 发送验证码
            if self._is_existing_account:
                self._log("9. [已注册账号] 跳过发送验证码，使用自动发送的 OTP")
                # 已注册账号的 OTP 在提交表单时已自动发送，记录时间戳
                self._otp_sent_at = time.time()
            else:
                self._set_stage("otp_sent")
                self._log("9. 发送验证码...")
                if not self._send_verification_code():
                    result.error_message = "发送验证码失败"
                    return self._finalize_result(result)

            # 10. 获取验证码
            self._set_stage("otp_received")
            self._log("10. 等待验证码...")
            code = self._get_verification_code()
            if not code:
                result.error_message = "获取验证码失败"
                return self._finalize_result(result)

            # 11. 验证验证码
            self._set_stage("otp_verified")
            self._log("11. 验证验证码...")
            if not self._validate_verification_code_with_retry(code):
                result.error_message = "验证验证码失败"
                return self._finalize_result(result)

            # 12. [已注册账号跳过] 创建用户账户
            if self._is_existing_account:
                self._log("12. [已注册账号] 跳过创建用户账户")
            else:
                self._set_stage("account_created")
                self._log("12. 创建用户账户...")
                if not self._create_user_account():
                    result.error_message = "创建用户账户失败"
                    return self._finalize_result(result)

            # 13. 获取 Workspace ID
            self._set_stage("workspace_loaded")
            self._log("13. 获取 Workspace ID...")
            callback_url_from_retry = None
            workspace_id = self._get_workspace_id()
            if not workspace_id:
                workspace_id, callback_url_from_retry = self._retry_workspace_context_via_oauth()
            if not workspace_id and not callback_url_from_retry:
                result.error_message = "获取 Workspace ID 失败"
                return self._finalize_result(result)

            result.workspace_id = workspace_id or ""

            if callback_url_from_retry:
                self._set_stage("redirect_followed")
                self._log("14. OAuth 重试已直接拿到回调 URL，跳过 workspace/select")
                callback_url = callback_url_from_retry
            else:
                # 14. 选择 Workspace
                self._set_stage("workspace_selected")
                self._log("14. 选择 Workspace...")
                continue_url = self._select_workspace(workspace_id)
                if not continue_url:
                    result.error_message = "选择 Workspace 失败"
                    return self._finalize_result(result)

                # 15. 跟随重定向链
                self._set_stage("redirect_followed")
                self._log("15. 跟随重定向链...")
                callback_url = self._follow_redirects(continue_url)
                if not callback_url:
                    result.error_message = "跟随重定向链失败"
                    return self._finalize_result(result)

            # 16. 处理 OAuth 回调
            self._set_stage("oauth_callback")
            self._log("16. 处理 OAuth 回调...")
            token_info = self._handle_oauth_callback(callback_url)
            if not token_info:
                result.error_message = "处理 OAuth 回调失败"
                return self._finalize_result(result)

            # 提取账户信息
            result.account_id = token_info.get("account_id", "")
            result.access_token = token_info.get("access_token", "")
            result.refresh_token = token_info.get("refresh_token", "")
            result.id_token = token_info.get("id_token", "")
            result.password = self.password or ""  # 保存密码（已注册账号为空）

            # 设置来源标记
            result.source = "login" if self._is_existing_account else "register"
            if self._is_existing_account:
                if self._registration_disposition != "existing_account":
                    self._registration_disposition = "login_flow"

            # 尝试获取 session_token 从 cookie
            session_cookie = self.session.cookies.get("__Secure-next-auth.session-token")
            if session_cookie:
                self.session_token = session_cookie
                result.session_token = session_cookie
                self._log(f"获取到 Session Token")

            # 17. 完成
            self._log("=" * 60)
            if self._is_existing_account:
                self._log("登录成功! (已注册账号)")
            else:
                self._log("注册成功!")
            self._log(f"邮箱: {result.email}")
            self._log(f"Account ID: {result.account_id}")
            self._log(f"Workspace ID: {result.workspace_id}")
            self._log("=" * 60)

            result.success = True
            result.metadata = {
                "email_service": self.email_service.service_type.value,
                "proxy_used": self.proxy_url,
                "registered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "is_existing_account": self._is_existing_account,
            }

            self._set_stage("completed")
            return self._finalize_result(result)

        except Exception as e:
            self._log(f"注册过程中发生未预期错误: {e}", "error")
            result.error_message = str(e)
            return self._finalize_result(result)

    def save_to_database(self, result: RegistrationResult) -> bool:
        """
        保存注册结果到数据库

        Args:
            result: 注册结果

        Returns:
            是否保存成功
        """
        if not result.success:
            return False

        return True  # 由 account_manager 统一处理存库
