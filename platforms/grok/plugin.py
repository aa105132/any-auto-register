"""Grok (x.ai) 平台插件"""
from __future__ import annotations

from core.base_captcha import create_captcha_solver
from core.base_platform import BasePlatform, Account, AccountStatus, RegisterConfig
from core.base_mailbox import BaseMailbox
from core.registration import BrowserRegistrationAdapter, OtpSpec, ProtocolMailboxAdapter, ProtocolOAuthAdapter, RegistrationCapability, RegistrationResult
from core.registration.helpers import resolve_timeout
from core.registry import register


class _FallbackTurnstileSolver:
    def __init__(self, named_solvers: list[tuple[str, object]], log_fn=print):
        self.named_solvers = list(named_solvers)
        self.log = log_fn or (lambda _msg: None)

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        errors: list[str] = []
        for name, solver in self.named_solvers:
            try:
                token = str(solver.solve_turnstile(page_url, site_key) or "").strip()
                if token:
                    if len(self.named_solvers) > 1:
                        self.log(f"  Turnstile provider: {name}")
                    return token
                errors.append(f"{name}: empty token")
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                self.log(f"  Turnstile provider {name} 失败，尝试下一个: {exc}")
        raise RuntimeError("所有 Turnstile provider 均失败: " + " | ".join(errors))

    def solve_image(self, image_b64: str) -> str:
        if not self.named_solvers:
            raise RuntimeError("未配置验证码 provider")
        return self.named_solvers[0][1].solve_image(image_b64)


@register
class GrokPlatform(BasePlatform):
    name = "grok"
    display_name = "Grok"
    version = "1.0.0"
    supported_executors = ["protocol", "cdp_protocol", "headless", "headed"]
    supported_identity_modes = ["mailbox", "oauth_browser"]
    supported_oauth_providers = ["google", "apple", "x"]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def _prepare_registration_password(self, password: str | None) -> str | None:
        if password:
            return password
        if self._should_generate_registration_password():
            from platforms.grok.core import _rand_password

            return _rand_password()
        return ""

    def _should_generate_registration_password(self) -> bool:
        extra = self.config.extra or {}
        mode = str(extra.get("grok_registration_mode") or extra.get("grok_browser_mode") or "").strip().lower()
        if mode in {"browser", "browser_register", "full_browser", "cdp_browser", "true", "1", "yes", "on"}:
            return True
        if self.config.executor_type == "cdp_protocol":
            return True
        return False

    def _candidate_captcha_solvers(self) -> list[str]:
        protocol_order = list(self.protocol_captcha_order)
        try:
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            protocol_order = ProviderSettingsRepository().get_enabled_captcha_order(protocol_order)
        except Exception:
            protocol_order = list(self.protocol_captcha_order)

        candidates: list[str] = []
        for solver_name in protocol_order:
            if solver_name in {"local_solver", "cdp_turnstile", "patchright_harvester"}:
                continue
            if self._has_configured_captcha(solver_name):
                candidates.append(solver_name)
        return candidates

    def _resolve_captcha_solver(self) -> str:
        requested = str(self.config.captcha_solver or "").strip().lower()
        if requested and requested not in {"", "auto"}:
            return super()._resolve_captcha_solver()

        if self.config.executor_type != "cdp_protocol":
            return super()._resolve_captcha_solver()

        # 用户选择 cdp_protocol 时，优先走本地 CDP/真实浏览器，不默认消耗
        # 2captcha/yescaptcha 等远程打码额度。只有显式选择远程 provider 时才使用。
        return "cdp_turnstile"

    def _make_captcha(self, **kwargs):
        requested = str(self.config.captcha_solver or "").strip().lower()
        if self.config.executor_type == "cdp_protocol" and (not requested or requested == "auto"):
            return create_captcha_solver("cdp_turnstile", self.config.extra)
        return super()._make_captcha(**kwargs)

    def _grok_registration_mode(self) -> str:
        raw = str(
            (self.config.extra or {}).get("grok_registration_mode")
            or (self.config.extra or {}).get("grok_browser_mode")
            or ""
        ).strip().lower()
        if raw in {"protocol", "cdp_protocol", "hybrid", "mixed"}:
            return "protocol"
        if raw in {"browser", "browser_register", "full_browser", "cdp_browser", "true", "1", "yes", "on"}:
            return "browser"
        if self.config.executor_type == "cdp_protocol":
            return "browser"
        return raw

    def _should_use_browser_registration_flow(self, identity) -> bool:
        if (self.config.executor_type or "") == "cdp_protocol" and getattr(identity, "identity_provider", "") != "oauth_browser":
            return self._grok_registration_mode() == "browser"
        return super()._should_use_browser_registration_flow(identity)

    def _map_grok_result(self, result: dict, *, password: str = "") -> RegistrationResult:
        return RegistrationResult(
            email=result["email"],
            password=password or result.get("password", ""),
            status=AccountStatus.REGISTERED,
            extra={
                "sso": result.get("sso", ""),
                "sso_rw": result.get("sso_rw", ""),
                "given_name": result.get("given_name", ""),
                "family_name": result.get("family_name", ""),
                "cookies": dict(result.get("cookies") or {}),
                "cookie_header": str(result.get("cookie_header") or ""),
                "cdp_bootstrap": dict(result.get("cdp_bootstrap") or {}),
            },
        )

    def _run_protocol_oauth(self, ctx) -> dict:
        from platforms.grok.browser_oauth import register_with_browser_oauth

        return register_with_browser_oauth(
            proxy=ctx.proxy,
            oauth_provider=ctx.identity.oauth_provider,
            email_hint=ctx.identity.email,
            timeout=resolve_timeout(ctx.extra, ("browser_oauth_timeout", "manual_oauth_timeout"), 300),
            log_fn=ctx.log,
            headless=(ctx.executor_type == "headless"),
            chrome_user_data_dir=ctx.identity.chrome_user_data_dir,
            chrome_cdp_url=ctx.identity.chrome_cdp_url,
        )

    def build_browser_registration_adapter(self):
        return BrowserRegistrationAdapter(
            result_mapper=lambda ctx, result: self._map_grok_result(result),
            browser_worker_builder=lambda ctx, artifacts: __import__("platforms.grok.browser_register", fromlist=["GrokBrowserRegister"]).GrokBrowserRegister(
                headless=(ctx.executor_type == "headless"),
                proxy=ctx.proxy,
                otp_callback=artifacts.otp_callback,
                log_fn=ctx.log,
                chrome_cdp_url=str(ctx.extra.get("chrome_cdp_url") or getattr(ctx.identity, "chrome_cdp_url", "") or ""),
                chrome_user_data_dir=str(ctx.extra.get("chrome_user_data_dir") or getattr(ctx.identity, "chrome_user_data_dir", "") or ""),
                use_oauth_browser=(ctx.executor_type == "cdp_protocol"),
            ),
            browser_register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email or "",
                password=ctx.password or "",
            ),
            oauth_runner=self._run_protocol_oauth,
            capability=RegistrationCapability(oauth_allowed_executor_types=("headed",)),
            otp_spec=OtpSpec(wait_message="等待验证码...", code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}"),
        )

    def build_protocol_oauth_adapter(self):
        return ProtocolOAuthAdapter(
            oauth_runner=self._run_protocol_oauth,
            result_mapper=lambda ctx, result: self._map_grok_result(result),
        )

    def build_protocol_mailbox_adapter(self):
        return ProtocolMailboxAdapter(
            result_mapper=lambda ctx, result: self._map_grok_result(result),
            worker_builder=lambda ctx, artifacts: __import__("platforms.grok.protocol_mailbox", fromlist=["GrokProtocolMailboxWorker"]).GrokProtocolMailboxWorker(
                captcha_solver=artifacts.captcha_solver,
                proxy=ctx.proxy,
                log_fn=ctx.log,
                use_cdp_bridge=(ctx.executor_type == "cdp_protocol"),
                chrome_cdp_url=str(ctx.extra.get("chrome_cdp_url") or getattr(ctx.identity, "chrome_cdp_url", "") or ""),
                chrome_user_data_dir=str(ctx.extra.get("chrome_user_data_dir") or getattr(ctx.identity, "chrome_user_data_dir", "") or ""),
            ),
            register_runner=lambda worker, ctx, artifacts: worker.run(
                email=ctx.identity.email,
                password=ctx.password,
                otp_callback=artifacts.otp_callback,
            ),
            otp_spec=OtpSpec(wait_message="等待验证码...", code_pattern=r"[A-Z0-9]{3}-[A-Z0-9]{3}"),
            use_captcha=True,
        )

    def check_valid(self, account: Account) -> bool:
        return bool((account.extra or {}).get("sso"))

    def get_platform_actions(self) -> list:
        return []

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        raise NotImplementedError(f"未知操作: {action_id}")
