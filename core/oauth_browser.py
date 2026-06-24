"""共享的 OAuth 浏览器辅助（支持普通 Playwright / Chrome Profile / CDP）。"""
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from .base_identity import normalize_oauth_provider


OAUTH_PROVIDER_LABELS = {
    "google": "Google",
    "github": "GitHub",
    "linkedin": "LinkedIn",
    "microsoft": "Microsoft",
    "apple": "Apple",
    "x": "X",
    "builderid": "Builder ID",
    "pilipala_sso": "Pilipala SSO",
}

OAUTH_PROVIDER_HINTS = {
    "google": ("google", "google-oauth2"),
    "github": ("github",),
    "linkedin": ("linkedin", "linkedin-openid"),
    "microsoft": ("microsoft", "windowslive", "live"),
    "apple": ("apple",),
    "x": ("x", "twitter"),
    "builderid": ("builder id", "builderid", "aws builder id", "amazon q"),
    "pilipala_sso": ("sso", "single sign-on", "pilipala", "edu.pilipala.store"),
}


def oauth_provider_label(provider: str) -> str:
    normalized = normalize_oauth_provider(provider)
    return OAUTH_PROVIDER_LABELS.get(normalized, normalized.title() if normalized else "")


def oauth_provider_hint_text(provider: str) -> str:
    label = oauth_provider_label(provider)
    if label:
        return label
    return "邮箱、Google、GitHub 等任一可用方式"


# backward-compat alias
browser_login_method_text = oauth_provider_hint_text


def finalize_oauth_email(actual_email: str, email_hint: str, platform_name: str) -> str:
    actual = (actual_email or "").strip()
    hint = (email_hint or "").strip()
    if actual and hint and actual.lower() != hint.lower():
        raise RuntimeError(
            f"{platform_name} OAuth 登录邮箱与预期不一致: 实际 {actual}，预期 {hint}"
        )
    resolved = actual or hint
    if not resolved:
        raise RuntimeError(
            f"{platform_name} OAuth 流程未识别到邮箱，请在任务里传入 email 或 oauth_email_hint"
        )
    return resolved


def _detect_running_chrome_cdp(ports: tuple = (9222, 9223, 9224)) -> str:
    """检测本机是否有 Chrome 开启了远程调试端口，返回 CDP URL 或空字符串。"""
    import urllib.request
    for port in ports:
        try:
            url = f"http://127.0.0.1:{port}/json/version"
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return ""


def _detect_chrome_user_data_dir() -> str:
    """自动检测系统 Chrome 用户数据目录。"""
    import os, sys
    if sys.platform == "darwin":
        path = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    elif sys.platform == "win32":
        path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    else:
        path = os.path.expanduser("~/.config/google-chrome")
    return path if os.path.isdir(path) else ""



def _find_external_chromium_executable() -> str:
    """Return a real desktop Chrome/Edge executable path, if available."""
    import os
    import shutil
    import sys

    candidates: list[str] = []
    if sys.platform == "win32":
        roots = [os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""), os.environ.get("LOCALAPPDATA", "")]
        for root in roots:
            if not root:
                continue
            candidates.extend([
                os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
            ])
    elif sys.platform == "darwin":
        candidates.extend([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ])
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "microsoft-edge"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def _build_external_chromium_args(port: int, user_data_dir: str, initial_url: str = "about:blank") -> list[str]:
    """Build visible desktop Chromium args for CDP OAuth automation.

    Do not force --use-gl/--use-angle here: on this Windows host those flags
    caused GPU startup crashes and Target closed/Target crashed symptoms.
    """
    return [
        f"--remote-debugging-port={int(port)}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-allow-origins=*",
        f"--user-data-dir={str(user_data_dir)}",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-software-rasterizer",
        initial_url or "about:blank",
    ]


def _wait_for_cdp(port: int, timeout: int = 30) -> bool:
    import urllib.request

    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/json/version", timeout=1) as r:
                if getattr(r, "status", 0) == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _terminate_process_tree(process: object | None) -> None:
    """Terminate a browser process and its children on Windows.

    Visible CDP Chrome often leaves renderer/utility child processes alive if only
    the parent Popen object is terminated. taskkill /T keeps batch OAuth runs from
    accumulating stray windows while only targeting the process we launched.
    """
    if process is None:
        return
    try:
        pid = int(getattr(process, "pid", 0) or 0)
    except Exception:
        pid = 0
    if pid <= 0:
        return
    try:
        import subprocess
        import sys

        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return
    except Exception:
        pass
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _terminate_chromium_profile_processes(user_data_dir: str) -> None:
    """Best-effort cleanup for Chrome processes using a specific profile dir."""
    if not user_data_dir:
        return
    try:
        import subprocess
        import sys

        if not sys.platform.startswith("win"):
            return
        normalized = str(Path(user_data_dir)).replace("'", "''")
        ps = (
            "$needle = '" + normalized + "'; "
            + "$procs = Get-CimInstance Win32_Process -Filter \"name='chrome.exe'\" | "
            + "Where-Object { $_.CommandLine -like ('*' + $needle + '*') }; "
            + "foreach ($p in $procs) { & taskkill /PID $p.ProcessId /T /F | Out-Null }"
        )
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:
        pass


def _cleanup_owned_temp_profile(user_data_dir: str, *, log_fn: Callable[[str], None] = print) -> None:
    """删除本程序自动创建的临时 Chrome Profile。

    只清理 any_auto_register_chrome_*，避免误删用户手动配置的
    chrome_user_data_dir 或其它浏览器数据目录。
    """
    if not user_data_dir:
        return
    profile_dir = Path(user_data_dir)
    if profile_dir.name.startswith("any_auto_register_chrome_"):
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception as exc:
            log_fn(f"[OAuthBrowser] cleanup temp Chrome profile failed: {profile_dir} ({exc})")


def _launch_external_chromium_cdp(user_data_dir: str, *, port: int = 0, initial_url: str = "about:blank", log_fn: Callable[[str], None] = print) -> tuple[str, object | None]:
    """Launch a visible system Chrome/Edge and return its CDP URL.

    注意：这个裸 CDP 启动路径不能可靠注入带账号密码的代理。
    OAuthBrowser 在传入 proxy 时会跳过该路径，改用 Playwright 原生
    proxy 配置，避免页面请求绕过 Resin/任务代理直连。
    """
    import random
    import subprocess

    exe = _find_external_chromium_executable()
    if not exe:
        return "", None
    selected_port = int(port or (9300 + random.randint(0, 399)))
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    args = [exe, *_build_external_chromium_args(selected_port, user_data_dir, initial_url)]
    try:
        log_fn(f"[OAuthBrowser] launch external browser CDP: {exe} port={selected_port}")
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        log_fn(f"[OAuthBrowser] launch external browser failed: {exc}")
        return "", None
    # 真实 Chrome 在 Windows 上偶尔先起窗口、后开放 /json/version。
    # 之前 35s 会误判 not ready，导致回退到 Playwright persistent context，
    # YepAPI/Google 更容易触发 CF/验证码。这里拉长等待，确保真的走 CDP。
    if _wait_for_cdp(selected_port, timeout=120):
        return f"http://127.0.0.1:{selected_port}", process
    log_fn(f"[OAuthBrowser] external browser CDP not ready after 120s: port={selected_port}")
    try:
        process.terminate()
    except Exception:
        pass
    return "", None

def _relaunch_chrome_with_debug_port(port: int = 9222) -> bool:
    """macOS: 关闭 Chrome 并用远程调试端口重启，成功返回 True。"""
    import subprocess, sys, time
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["pkill", "-x", "Google Chrome"], capture_output=True)
        time.sleep(1.5)
        subprocess.Popen([
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            f"--remote-debugging-port={port}",
            "--no-first-run",
        ])
        # wait for CDP to be ready
        import urllib.request
        for _ in range(20):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1):
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False




def _build_proxy_config(proxy: Optional[str]) -> Optional[dict]:
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.scheme or not parsed.hostname or not parsed.port:
        return {"server": proxy}
    config = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


_GOOGLE_ACCOUNT_SELECTORS = [
    "[data-email]",
    ".JDAKTe",
    "[data-authuser]",
    ".account-name",
    "li[data-identifier]",
]


class OAuthBrowser:
    """全自动 OAuth 浏览器（支持普通 Playwright / Chrome Profile / CDP）。"""

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        headless: bool = False,
        chrome_user_data_dir: str = "",
        chrome_cdp_url: str = "",
        reuse_existing_cdp: bool = False,
        log_fn: Callable[[str], None] = print,
        use_camoufox: bool = False,
        camoufox_user_data_dir: str = "",
    ):
        self.proxy = proxy
        self.headless = headless
        self.chrome_user_data_dir = chrome_user_data_dir
        self.chrome_cdp_url = chrome_cdp_url
        # 默认不自动复用 9222/9223/9224。并发注册时复用同一个 CDP 会共享
        # context/profile，导致账号池账号串号、标签页互相抢焦点。只有显式传入
        # chrome_cdp_url，或设置 reuse_existing_cdp=True，才连接已有浏览器。
        self.reuse_existing_cdp = bool(reuse_existing_cdp)
        # Camoufox（基于 Firefox 的反检测浏览器）：用于 Google 对 OAuth app 做严格
        # 浏览器安全检测的场景（如 Vellum），Playwright Chromium 会被 signin/rejected。
        self.use_camoufox = bool(use_camoufox)
        # Camoufox 持久化 profile 目录：设置后用 persistent_context=True 启动，
        # 登录态（cookie/session）落盘，后续脚本复用同一 profile 免重登。
        self.camoufox_user_data_dir = camoufox_user_data_dir
        self.log = log_fn
        self._pw = None
        self.browser = None
        self.context = None
        self.page = None
        self._persistent = False  # launch_persistent_context path
        self._owns_cdp_browser = False
        self._external_chromium_process = None
        self._external_chromium_user_data_dir = ""
        self._camoufox_runner = None  # Camoufox 实例（反检测 Firefox 路径）
        self._camoufox_persistent = False  # Camoufox persistent_context 路径
        self._cancel_token = None  # 协作式取消令牌（由平台层注入）

    def set_cancel_token(self, token) -> None:
        self._cancel_token = token

    def _poll_cancel(self) -> None:
        from core.cancel_token import check_cancel
        check_cancel(self._cancel_token)

    def __enter__(self):
        # Camoufox 自带 Playwright sync runtime，不能与 OAuthBrowser 的 sync_playwright() 共存
        # （会触发 "Sync API inside asyncio loop"）。Camoufox 路径独立启动，不走 self._pw。
        self._pw = None if self.use_camoufox else sync_playwright().start()
        proxy_cfg = _build_proxy_config(self.proxy)

        if self.use_camoufox:
            # Camoufox（反检测 Firefox）：用于 Google 严格浏览器安全检测的 OAuth app。
            from camoufox.sync_api import Camoufox
            launch_options: dict = {"headless": self.headless}
            if proxy_cfg:
                launch_options["proxy"] = proxy_cfg
            # 持久化 profile：persistent_context=True 时 Camoufox __enter__ 返回
            # BrowserContext（非 Browser），登录态落盘到 camoufox_user_data_dir。
            # user_data_dir 必须作为顶层 kwarg 传给 Camoufox（落入 launch_options() 的
            # **kwargs，再被 spread 进 launch_persistent_context 的参数）。
            if self.camoufox_user_data_dir:
                Path(self.camoufox_user_data_dir).mkdir(parents=True, exist_ok=True)
                launch_options["persistent_context"] = True
                launch_options["user_data_dir"] = self.camoufox_user_data_dir
                self._camoufox_persistent = True
                self.log(f"[OAuthBrowser] 启动 Camoufox 持久化 profile: {self.camoufox_user_data_dir}")
                self._camoufox_runner = Camoufox(**launch_options)
                self.context = self._camoufox_runner.__enter__()  # 返回 BrowserContext
                self.browser = None  # 持久化模式无独立 Browser 对象
                pages = self.context.pages
                self.page = pages[0] if pages else self.context.new_page()
                return self
            # 非持久化：Camoufox __enter__ 返回 Browser，这里创建一个 context。
            self.log("[OAuthBrowser] 启动 Camoufox（反检测 Firefox）")
            self._camoufox_runner = Camoufox(**launch_options)
            self.browser = self._camoufox_runner.__enter__()
            self.context = self.browser.new_context()
            self.page = self.context.new_page()
            return self

        if self.chrome_cdp_url:
            # Connect to a running Chrome instance via CDP
            self.browser = self._pw.chromium.connect_over_cdp(self.chrome_cdp_url)
            self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
            pages = self.context.pages
            self.page = pages[0] if pages else self.context.new_page()
        elif self.chrome_user_data_dir:
            # 使用 Playwright 控制的真实系统 Chrome persistent context。
            # 这仍然会打开真实 Chrome 窗口，但不依赖外部 DevTools HTTP 端口，
            # 避免 Windows 上 Popen Chrome 带 remote-debugging-port 却端口拒绝连接。
            launch_kwargs = {
                "channel": "chrome",
                "headless": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
            self.log(f"[OAuthBrowser] launch controlled real Chrome profile: {self.chrome_user_data_dir}")
            self.context = self._pw.chromium.launch_persistent_context(
                self.chrome_user_data_dir,
                **launch_kwargs,
            )
            self._persistent = True
            pages = self.context.pages
            self.page = pages[0] if pages else self.context.new_page()
        else:
            # 无显式 profile 时启动独立真实系统 Chrome + 临时 CDP profile。
            # 并发 OAuth 不能默认探测并复用 9222/9223/9224：那会把多个账号
            # 放进同一个 Chrome context，造成账号池串号、标签页抢焦点和端口阻塞。
            cdp_url = ""
            if self.reuse_existing_cdp:
                cdp_url = _detect_running_chrome_cdp()
            if cdp_url:
                self.chrome_cdp_url = cdp_url
                self.log(f"[OAuthBrowser] 连接已运行的 Chrome (CDP): {cdp_url}")
                self.browser = self._pw.chromium.connect_over_cdp(cdp_url)
                self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
                pages = self.context.pages
                self.page = pages[0] if pages else self.context.new_page()
            else:
                temp_profile = str(Path(tempfile.gettempdir()) / f"any_auto_register_chrome_{uuid.uuid4().hex}")
                cdp_url, process = "", None
                if proxy_cfg:
                    # 外部 Chrome CDP 无法可靠处理带认证代理；这里必须走
                    # Playwright launch(proxy=...)，保证页面、XHR、OAuth 跳转都
                    # 使用任务代理/Resin，而不是只让后续 requests.Session 走代理。
                    self.log("[OAuthBrowser] proxy configured; skip external CDP and launch browser with Playwright proxy")
                else:
                    cdp_url, process = _launch_external_chromium_cdp(temp_profile, log_fn=self.log)
                if cdp_url:
                    self.chrome_cdp_url = cdp_url
                    self._owns_cdp_browser = True
                    self._external_chromium_process = process
                    self._external_chromium_user_data_dir = temp_profile
                    self.browser = self._pw.chromium.connect_over_cdp(cdp_url)
                    self.context = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
                    pages = self.context.pages
                    self.page = pages[0] if pages else self.context.new_page()
                else:
                    # 最后兜底才使用 Playwright Chromium；有 proxy 时这是首选路径，
                    # 因为 Playwright 能正确拆分 server/username/password。
                    self.log("[OAuthBrowser] 未找到系统 Chrome或需代理，使用 Playwright Chromium")
                    launch_kwargs = {"headless": self.headless}
                    if proxy_cfg:
                        launch_kwargs["proxy"] = proxy_cfg
                    self.browser = self._pw.chromium.launch(**launch_kwargs)
                    self.context = self.browser.new_context()
                    self.page = self.context.new_page()

        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.use_camoufox and self._camoufox_runner is not None:
                # Camoufox 路径独立清理
                try:
                    if self.context:
                        self.context.close()
                finally:
                    if self.browser:
                        self.browser.close()
                    self._camoufox_runner.__exit__(exc_type, exc, tb)
                    self._camoufox_runner = None
                return
            if self._persistent:
                if self.context:
                    self.context.close()
            else:
                try:
                    if self.chrome_cdp_url and not self._owns_cdp_browser:
                        return
                    if self.context:
                        self.context.close()
                finally:
                    if self.browser:
                        self.browser.close()
                    if self._external_chromium_process is not None:
                        _terminate_process_tree(self._external_chromium_process)
                    if self._external_chromium_user_data_dir:
                        _terminate_chromium_profile_processes(self._external_chromium_user_data_dir)
                        _cleanup_owned_temp_profile(self._external_chromium_user_data_dir, log_fn=self.log)
        finally:
            if self._pw:
                self._pw.stop()

    def pages(self) -> list:
        if not self.context:
            return []
        pages = [page for page in self.context.pages if not page.is_closed()]
        return pages or ([self.page] if self.page else [])

    def active_page(self):
        pages = self.pages()
        return pages[-1] if pages else self.page

    def new_page(self):
        """创建新页面并返回。"""
        if not self.context:
            raise RuntimeError("OAuthBrowser 未初始化")
        page = self.context.new_page()
        return page

    def goto(self, url: str, *, wait_until: str = "networkidle", timeout: int = 30000) -> None:
        self.active_page().goto(url, wait_until=wait_until, timeout=timeout)

    def try_click_provider(self, provider: str) -> bool:
        provider = normalize_oauth_provider(provider)
        if not provider:
            return False
        page = self.active_page()
        label = oauth_provider_label(provider)
        hints = list(OAUTH_PROVIDER_HINTS.get(provider, (provider,)))
        try:
            clicked = page.evaluate(
                """
                ({hints, label}) => {
                    const nodes = Array.from(
                        document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]')
                    );
                    let best = null;
                    for (const node of nodes) {
                        if (!node || node.disabled) {
                            continue;
                        }
                        const text = [
                            node.innerText || '',
                            node.textContent || '',
                            node.value || '',
                            node.getAttribute('aria-label') || '',
                            node.getAttribute('name') || '',
                            node.getAttribute('value') || '',
                            node.getAttribute('data-provider') || '',
                            node.getAttribute('data-connection') || '',
                            node.getAttribute('href') || '',
                            node.getAttribute('title') || '',
                        ].join(' ').toLowerCase();
                        let score = 0;
                        if (text.includes(label.toLowerCase())) {
                            score += 3;
                        }
                        for (const hint of hints) {
                            if (hint && text.includes(hint.toLowerCase())) {
                                score += 2;
                            }
                        }
                        if (score <= 0) {
                            continue;
                        }
                        if (!best || score > best.score) {
                            best = { node, score };
                        }
                    }
                    if (!best) {
                        return false;
                    }
                    best.node.click();
                    return true;
                }
                """,
                {"hints": hints, "label": label},
            )
        except Exception:
            clicked = False
        return bool(clicked)

    def auto_select_google_account(self, timeout: int = 15) -> bool:
        """Google 账号选择器出现时自动点击第一个账号。
        适用于 Chrome Profile 模式：Google 已登录，弹出账号选择器。
        """
        deadline = time.time() + timeout
        selectors = ", ".join(_GOOGLE_ACCOUNT_SELECTORS)
        while time.time() < deadline:
            for page in self.pages():
                url = page.url or ""
                if "accounts.google.com" not in url:
                    continue
                try:
                    el = page.query_selector(selectors)
                    if el:
                        el.click()
                        self.log("[OAuthBrowser] Google 账号选择器：已自动点击第一个账号")
                        return True
                except Exception:
                    pass
            time.sleep(0.5)
        return False

    def wait_for_url(
        self,
        predicate: Callable[[str], bool],
        *,
        timeout: int = 300,
        interval: float = 1.0,
    ) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for page in self.pages():
                current_url = (page.url or "").strip()
                if current_url and predicate(current_url):
                    return current_url
            time.sleep(interval)
        return ""

    def wait_for_cookie_value(
        self,
        names: Iterable[str],
        *,
        timeout: int = 300,
        domain_substrings: Iterable[str] = (),
        interval: float = 1.0,
    ) -> str:
        deadline = time.time() + timeout
        wanted = {name.strip() for name in names if name}
        while time.time() < deadline:
            value = self.cookie_value(*wanted, domain_substrings=domain_substrings)
            if value:
                return value
            time.sleep(interval)
        return ""

    def cookies(self) -> list[dict]:
        return list(self.context.cookies()) if self.context else []

    def cookie_value(self, *names: str, domain_substrings: Iterable[str] = ()) -> str:
        wanted = {name for name in names if name}
        domain_filters = tuple(filter(None, domain_substrings))
        for cookie in self.cookies():
            if wanted and cookie.get("name") not in wanted:
                continue
            domain = cookie.get("domain", "")
            if domain_filters and not any(part in domain for part in domain_filters):
                continue
            return cookie.get("value", "")
        return ""

    def cookie_header(self, *, domain_substrings: Iterable[str] = ()) -> str:
        cookie_map = {}
        domain_filters = tuple(filter(None, domain_substrings))
        for cookie in self.cookies():
            domain = cookie.get("domain", "")
            if domain_filters and not any(part in domain for part in domain_filters):
                continue
            cookie_map[cookie.get("name", "")] = cookie.get("value", "")
        return "; ".join(f"{name}={value}" for name, value in cookie_map.items() if name)

    def cookie_dict(self, *, domain_substrings: Iterable[str] = ()) -> dict:
        cookie_map = {}
        domain_filters = tuple(filter(None, domain_substrings))
        for cookie in self.cookies():
            domain = cookie.get("domain", "")
            if domain_filters and not any(part in domain for part in domain_filters):
                continue
            cookie_map[cookie.get("name", "")] = cookie.get("value", "")
        return cookie_map


# Backward-compat alias
ManualOAuthBrowser = OAuthBrowser


def try_click_provider_on_page(page, provider: str) -> bool:
    """Standalone helper: click an OAuth provider button on any Playwright-compatible page."""
    provider = normalize_oauth_provider(provider)
    if not provider:
        return False
    label = oauth_provider_label(provider)
    hints = list(OAUTH_PROVIDER_HINTS.get(provider, (provider,)))
    try:
        clicked = page.evaluate(
            """
            ({hints, label}) => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
                };
                const nodes = Array.from(
                    document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]')
                );
                let best = null;
                for (const node of nodes) {
                    if (!node || node.disabled || !visible(node)) {
                        continue;
                    }
                    const rawText = [
                        node.innerText || '',
                        node.textContent || '',
                        node.value || '',
                        node.getAttribute('aria-label') || '',
                        node.getAttribute('name') || '',
                        node.getAttribute('value') || '',
                        node.getAttribute('data-provider') || '',
                        node.getAttribute('data-connection') || '',
                        node.getAttribute('href') || '',
                        node.getAttribute('title') || '',
                    ].join(' ').trim();
                    if (!rawText) continue;
                    const text = rawText.toLowerCase();
                    let score = 0;
                    if (text.includes(label.toLowerCase())) {
                        score += 3;
                    }
                    for (const hint of hints) {
                        if (hint && text.includes(hint.toLowerCase())) {
                            score += 2;
                        }
                    }
                    if (score <= 0) {
                        continue;
                    }
                    const r = node.getBoundingClientRect();
                    if (!best || score > best.score || (score === best.score && r.width * r.height > best.area)) {
                        best = { node, score, area: r.width * r.height };
                    }
                }
                if (!best) {
                    return false;
                }
                best.node.scrollIntoView({block:'center', inline:'center'});
                best.node.click();
                return true;
            }
            """,
            {"hints": hints, "label": label},
        )
    except Exception:
        clicked = False
    return bool(clicked)
