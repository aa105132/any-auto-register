"""验证码解决器基类"""
from abc import ABC, abstractmethod


class BaseCaptcha(ABC):
    @abstractmethod
    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        """返回 Turnstile token"""
        ...

    @abstractmethod
    def solve_image(self, image_b64: str) -> str:
        """返回图片验证码文字"""
        ...


class YesCaptcha(BaseCaptcha):
    def __init__(self, client_key: str, api_base: str = "https://api.yescaptcha.com"):
        self.client_key = client_key
        self.api = str(api_base or "https://api.yescaptcha.com").rstrip("/")

    def solve_turnstile(self, page_url: str, site_key: str, proxy: str | None = None, user_agent: str = "") -> str:
        import requests, time, urllib.parse, urllib3
        urllib3.disable_warnings()
        task = {
            "type": "TurnstileTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        if proxy:
            parsed = urllib.parse.urlsplit(proxy)
            if parsed.hostname:
                task = {
                    "type": "TurnstileTask",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                    "proxyType": (parsed.scheme or "http").lower(),
                    "proxyAddress": parsed.hostname,
                    "proxyPort": int(parsed.port or 80),
                    "proxyLogin": urllib.parse.unquote(parsed.username or ""),
                    "proxyPassword": urllib.parse.unquote(parsed.password or ""),
                }
        if user_agent:
            task["userAgent"] = user_agent
        session = requests.Session()
        session.trust_env = False
        r = session.post(f"{self.api}/createTask", json={
            "clientKey": self.client_key,
            "task": task,
        }, timeout=30, verify=False)
        task_id = r.json().get("taskId")
        if not task_id:
            raise RuntimeError(f"YesCaptcha 创建任务失败: {r.text}")
        for _ in range(60):
            time.sleep(3)
            d = session.post(f"{self.api}/getTaskResult", json={
                "clientKey": self.client_key, "taskId": task_id
            }, timeout=30, verify=False).json()
            if d.get("status") == "ready":
                return d["solution"]["token"]
            if d.get("errorId", 0) != 0:
                raise RuntimeError(f"YesCaptcha 错误: {d}")
        raise TimeoutError("YesCaptcha Turnstile 超时")

    def solve_hcaptcha(self, page_url: str, site_key: str, proxy: str | None = None, user_agent: str = "", invisible: bool = False) -> str:
        """YesCaptcha 解 hCaptcha（Stripe 绑卡用）。

        YesCaptcha 支持 HCaptchaTaskProxyless / HCaptchaTask。返回 h-captcha-response token。
        invisible=True 用于不可见 hCaptcha（Stripe 内嵌的常是 invisible）。
        """
        import requests, time, urllib.parse, urllib3
        urllib3.disable_warnings()
        task = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": site_key,
            "isInvisible": bool(invisible),
        }
        if proxy:
            parsed = urllib.parse.urlsplit(proxy)
            if parsed.hostname:
                task = {
                    "type": "HCaptchaTask",
                    "websiteURL": page_url,
                    "websiteKey": site_key,
                    "isInvisible": bool(invisible),
                    "proxyType": (parsed.scheme or "http").lower(),
                    "proxyAddress": parsed.hostname,
                    "proxyPort": int(parsed.port or 80),
                    "proxyLogin": urllib.parse.unquote(parsed.username or ""),
                    "proxyPassword": urllib.parse.unquote(parsed.password or ""),
                }
        if user_agent:
            task["userAgent"] = user_agent
        session = requests.Session()
        session.trust_env = False
        r = session.post(f"{self.api}/createTask", json={
            "clientKey": self.client_key, "task": task,
        }, timeout=30, verify=False)
        task_id = r.json().get("taskId")
        if not task_id:
            raise RuntimeError(f"YesCaptcha hCaptcha 创建任务失败: {r.text}")
        for _ in range(80):
            time.sleep(3)
            d = session.post(f"{self.api}/getTaskResult", json={
                "clientKey": self.client_key, "taskId": task_id
            }, timeout=30, verify=False).json()
            if d.get("status") == "ready":
                sol = d.get("solution") or {}
                # hCaptcha solution: {"gRecaptchaResponse":"P0_...", ".userAgent":"..."}
                return sol.get("gRecaptchaResponse") or sol.get("token") or ""
            if d.get("errorId", 0) != 0:
                raise RuntimeError(f"YesCaptcha hCaptcha 错误: {d}")
        raise TimeoutError("YesCaptcha hCaptcha 超时")

    def solve_image(self, image_b64: str) -> str:
        raise NotImplementedError


class TwoCaptcha(BaseCaptcha):
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.api = "https://2captcha.com"

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        import time
        import requests

        create = requests.post(
            f"{self.api}/in.php",
            data={
                "key": self.api_key,
                "method": "turnstile",
                "sitekey": site_key,
                "pageurl": page_url,
                "json": 1,
            },
            timeout=30,
        )
        create.raise_for_status()
        payload = create.json()
        if payload.get("status") != 1:
            raise RuntimeError(f"2Captcha 创建任务失败: {payload}")
        task_id = payload.get("request")
        if not task_id:
            raise RuntimeError(f"2Captcha 未返回任务 ID: {payload}")

        for _ in range(60):
            time.sleep(3)
            result = requests.get(
                f"{self.api}/res.php",
                params={
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                    "json": 1,
                },
                timeout=30,
            )
            result.raise_for_status()
            data = result.json()
            if data.get("status") == 1:
                return str(data.get("request") or "")
            if data.get("request") not in {"CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"}:
                raise RuntimeError(f"2Captcha 错误: {data}")
        raise TimeoutError("2Captcha Turnstile 超时")

    def solve_image(self, image_b64: str) -> str:
        raise NotImplementedError


class ManualCaptcha(BaseCaptcha):
    """人工打码，阻塞等待用户输入"""
    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        return input(f"请手动获取 Turnstile token ({page_url}): ").strip()

    def solve_image(self, image_b64: str) -> str:
        return input("请输入图片验证码: ").strip()


class LocalSolverCaptcha(BaseCaptcha):
    """调用本地 api_solver 服务解 Turnstile（Camoufox/patchright）"""

    def __init__(self, solver_url: str = "http://localhost:8889"):
        self.solver_url = solver_url.rstrip("/")

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        import requests, time
        # 提交任务
        r = requests.get(
            f"{self.solver_url}/turnstile",
            params={"url": page_url, "sitekey": site_key},
            timeout=15,
        )
        r.raise_for_status()
        task_id = r.json().get("taskId")
        if not task_id:
            raise RuntimeError(f"LocalSolver 未返回 taskId: {r.text}")
        # 轮询结果
        for _ in range(60):
            time.sleep(2)
            res = requests.get(
                f"{self.solver_url}/result",
                params={"id": task_id},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                status = data.get("status")
                if status == "ready":
                    token = data.get("solution", {}).get("token")
                    if token:
                        return token
                elif status == "CAPTCHA_FAIL":
                    raise RuntimeError("LocalSolver Turnstile 失败")
        raise TimeoutError("LocalSolver Turnstile 超时")

    def solve_image(self, image_b64: str) -> str:
        raise NotImplementedError

    @staticmethod
    def start_solver(headless: bool = True, browser_type: str = "camoufox",
                     port: int = 8889) -> None:
        """在后台线程启动本地 solver 服务"""
        import subprocess, sys, os
        solver_path = os.path.join(
            os.path.dirname(__file__), "..", "services", "turnstile_solver", "start.py"
        )
        cmd = [
            sys.executable, solver_path,
            "--port", str(port),
            "--browser_type", browser_type,
        ]
        if not headless:
            cmd.append("--no-headless")
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等待服务启动
        import time, requests
        for _ in range(20):
            time.sleep(1)
            try:
                requests.get(f"http://localhost:{port}/", timeout=2)
                return
            except Exception:
                pass
        raise RuntimeError("LocalSolver 启动超时")


class TulingCloudCaptcha(BaseCaptcha):
    """图灵云 / fdyscloud 图片识别客户端。

    目前用于腾讯滑块：把滑块挑战截图提交给滑块通用模型，
    读取返回的“滑块/缺口”坐标并换算拖动距离。
    """

    def __init__(
        self,
        *,
        username: str = "",
        password: str = "",
        usertoken: str = "",
        api_base: str = "http://www.tulingcloud.com",
        model_id: str = "48956156",
        developer: str = "",
        timeout: int = 45,
    ):
        self.username = str(username or "").strip()
        self.password = str(password or "").strip()
        self.usertoken = str(usertoken or "").strip()
        self.api_base = str(api_base or "http://www.tulingcloud.com").rstrip("/")
        self.model_id = str(model_id or "48956156").strip() or "48956156"
        self.developer = str(developer or "").strip()
        self.timeout = max(int(timeout or 45), 10)


    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        raise NotImplementedError("图灵云不支持 Turnstile token 直解；请通过 cdp_turnstile 包装使用")

    def solve_image(self, image_b64: str) -> str:
        data = self.predict(image_b64=image_b64)
        result = data.get("data")
        if isinstance(result, str):
            return result
        return str(result or "")

    @property
    def configured(self) -> bool:
        return bool(self.usertoken or (self.username and self.password))

    def predict(self, *, image_b64: str = "", small_b64: str = "", large_b64: str = "", model_id: str = "") -> dict:
        import requests

        if not self.configured:
            raise RuntimeError("图灵验证码识别未配置账号密码或 usertoken")
        payload: dict[str, str] = {
            "ID": str(model_id or self.model_id),
            "version": "3.1.1",
        }
        if self.usertoken:
            payload["usertoken"] = self.usertoken
        else:
            payload["username"] = self.username
            payload["password"] = self.password
        if self.developer:
            payload["developer"] = self.developer
        if small_b64 and large_b64:
            payload["b64_small"] = small_b64
            payload["b64_large"] = large_b64
        else:
            payload["b64"] = image_b64
        session = requests.Session()
        session.trust_env = False
        response = session.post(f"{self.api_base}/tuling/predict", json=payload, timeout=self.timeout)
        response.raise_for_status()
        try:
            data = response.json()
        except Exception as exc:
            raise RuntimeError(f"图灵验证码识别返回非 JSON: {response.text[:300]}") from exc
        if data.get("code") != 1:
            raise RuntimeError(f"图灵验证码识别失败: {data}")
        return data

    def solve_slider_distance_from_b64(self, image_b64: str, *, css_width: float = 0.0, image_width: int = 0) -> dict:
        result = self.predict(image_b64=image_b64)
        slider, gap = self._extract_slider_points(result.get("data") or {})
        if not slider or not gap:
            raise RuntimeError(f"图灵滑块识别结果缺少滑块/缺口坐标: {result}")
        raw_distance = float(gap[0]) - float(slider[0])
        scale = 1.0
        if css_width and image_width:
            scale = float(css_width) / float(image_width)
        distance = raw_distance * scale
        return {
            "distance": distance,
            "raw_distance": raw_distance,
            "scale": scale,
            "slider": {"x": slider[0], "y": slider[1]},
            "gap": {"x": gap[0], "y": gap[1]},
            "result": result,
        }

    @classmethod
    def _extract_slider_points(cls, data) -> tuple[tuple[float, float] | None, tuple[float, float] | None]:
        import json

        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return None, None
        if not isinstance(data, dict):
            return None, None

        def point_from(value):
            if not isinstance(value, dict):
                return None
            x = cls._first_number(value, ("X坐标值", "x", "X", "left"))
            y = cls._first_number(value, ("Y坐标值", "y", "Y", "top"))
            if x is None:
                return None
            return (float(x), float(y or 0))

        slider = None
        gap = None
        for key, value in data.items():
            name = str(key)
            point = point_from(value)
            if point is None:
                continue
            if "缺口" in name or "目标" in name or "坑" in name:
                gap = point
            elif "滑块" in name or "小图" in name or "拼图" in name:
                slider = point
        points = [point_from(value) for value in data.values()]
        points = [point for point in points if point is not None]
        if (slider is None or gap is None) and len(points) >= 2:
            points_sorted = sorted(points, key=lambda item: item[0])
            slider = slider or points_sorted[0]
            gap = gap or points_sorted[-1]
        return slider, gap

    @staticmethod
    def _first_number(data: dict, keys: tuple[str, ...]):
        for key in keys:
            if key not in data:
                continue
            try:
                return float(data.get(key))
            except Exception:
                continue
        return None


class CdpTurnstileSolver(BaseCaptcha):

    DEFAULT_CHROME_PATHS = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
    ]

    def __init__(
        self,
        chrome_path: str = "",
        cdp_url: str = "",
        headless: bool = True,
        navigation_timeout_ms: int = 90000,
        tuling_solver: TulingCloudCaptcha | None = None,
    ):
        self.chrome_path = chrome_path.strip()
        self.cdp_url = cdp_url.strip()
        self.headless = headless
        self.navigation_timeout_ms = max(int(navigation_timeout_ms or 90000), 30000)
        self.tuling_solver = tuling_solver

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            from playwright.sync_api import sync_playwright

        import os, socket, subprocess, time, signal, shutil
        from urllib.parse import urlparse

        process = None
        profile_dir = None
        cdp_endpoint = self.cdp_url

        try:
            if not cdp_endpoint:
                chrome_bin = self._resolve_chrome()
                port = self._find_free_port()
                base_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "output", "cdp_turnstile_profiles",
                )
                self._purge_stale_profiles(base_dir)
                profile_dir = os.path.join(base_dir, f"cdp-{port}")
                os.makedirs(profile_dir, exist_ok=True)
                args = [
                    chrome_bin,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--window-position=-32000,-32000",
                    "--window-size=1280,800",
                ]
                if self.headless:
                    args.append("--headless=new")
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
                cdp_endpoint = f"http://127.0.0.1:{port}"
                self._wait_cdp_ready(port)

            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(cdp_endpoint, timeout=15000)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                try:
                    is_clerk = "venice.ai" in page_url.lower() or "clerk" in page_url.lower()
                    if is_clerk:
                        token = self._solve_clerk_turnstile(page, page_url, site_key)
                    else:
                        token = self._solve_regular_turnstile(page, page_url, site_key)
                    if not token:
                        raise RuntimeError("CDP Turnstile: failed to obtain token after retries")
                    return token
                finally:
                    page.close()
                    browser.close()
        finally:
            if process is not None:
                self._kill_process(process)
            if profile_dir:
                import threading
                threading.Thread(target=self._cleanup_dir, args=(profile_dir,), daemon=True).start()

    def solve_turnstile_with_session(self, page_url: str, site_key: str) -> dict:
        """获取 Turnstile token，并同步 CDP 浏览器里的同域 Cookie 与 UA。"""
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            from playwright.sync_api import sync_playwright

        import os, subprocess, time
        from urllib.parse import urlparse

        process = None
        profile_dir = None
        cdp_endpoint = self.cdp_url
        try:
            if not cdp_endpoint:
                chrome_bin = self._resolve_chrome()
                port = self._find_free_port()
                base_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "output", "cdp_turnstile_profiles",
                )
                self._purge_stale_profiles(base_dir)
                profile_dir = os.path.join(base_dir, f"cdp-{port}")
                os.makedirs(profile_dir, exist_ok=True)
                args = [
                    chrome_bin,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--window-position=-32000,-32000",
                    "--window-size=1280,800",
                ]
                if self.headless:
                    args.append("--headless=new")
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
                cdp_endpoint = f"http://127.0.0.1:{port}"
                self._wait_cdp_ready(port)

            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(cdp_endpoint, timeout=15000)
                ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                page = ctx.new_page()
                try:
                    is_clerk = "venice.ai" in page_url.lower() or "clerk" in page_url.lower()
                    if is_clerk:
                        token = self._solve_clerk_turnstile(page, page_url, site_key)
                    else:
                        token = self._solve_regular_turnstile(page, page_url, site_key)
                    if not token:
                        raise RuntimeError("CDP Turnstile: failed to obtain token after retries")
                    cookies = self._cookie_dict_for_url(ctx, page_url)
                    try:
                        user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip()
                    except Exception:
                        user_agent = ""
                    return {
                        "token": token,
                        "turnstile_token": token,
                        "cookies": cookies,
                        "user_agent": user_agent,
                        "mode": "cdp_protocol",
                    }
                finally:
                    page.close()
                    browser.close()
        finally:
            if process is not None:
                self._kill_process(process)
            if profile_dir:
                import threading
                threading.Thread(target=self._cleanup_dir, args=(profile_dir,), daemon=True).start()


    def solve_tencent_captcha(
        self,
        page_url: str,
        captcha_app_id: str,
        *,
        locale: str = "en",
        timeout_ms: int = 120000,
    ) -> dict:
        """通过真实 Chrome 调起腾讯滑块，返回 ticket/randstr。

        腾讯 TCaptcha.js 在 Playwright 直连 CDP 的旧上下文里偶发不挂载
        window.TencentCaptcha；这里优先使用 Playwright launch(channel="chrome")
        新上下文，实际浏览器行为更接近人工打开页面。
        """
        # 腾讯 TCaptcha.js 在 patchright 上会加载 iframe 但不稳定挂载
        # window.TencentCaptcha；这里显式使用官方 Playwright。
        from playwright.sync_api import sync_playwright

        captcha_app_id = str(captcha_app_id or "").strip()
        if not captcha_app_id:
            raise RuntimeError("CDP TencentCaptcha: captcha_app_id is required")

        with sync_playwright() as pw:
            launch_kwargs = {
                "headless": bool(self.headless),
                "args": [
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            chrome_bin = ""
            try:
                chrome_bin = self._resolve_chrome()
            except Exception:
                chrome_bin = ""
            if chrome_bin:
                launch_kwargs["executable_path"] = chrome_bin
            else:
                launch_kwargs["channel"] = "chrome"
            browser = pw.chromium.launch(**launch_kwargs)
            page = browser.new_page()
            try:
                result = self._solve_tencent_captcha_on_page(
                    page,
                    page_url,
                    captcha_app_id,
                    locale=locale,
                    timeout_ms=max(int(timeout_ms or 120000), 30000),
                )
                ticket = str(result.get("ticket") or "").strip()
                randstr = str(result.get("randstr") or "").strip()
                if not ticket or not randstr:
                    try:
                        result = self._auto_drag_tencent_captcha(page, timeout_ms=max(int(timeout_ms or 120000), 30000))
                    except Exception as drag_exc:
                        result = {**(result or {}), "drag_error": repr(drag_exc)}
                    ticket = str(result.get("ticket") or "").strip()
                    randstr = str(result.get("randstr") or "").strip()
                if not ticket or not randstr:
                    raise RuntimeError(f"CDP TencentCaptcha: failed to obtain ticket/randstr: {result}")
                try:
                    user_agent = str(page.evaluate("() => navigator.userAgent") or "").strip()
                except Exception:
                    user_agent = ""
                return {
                    "ticket": ticket,
                    "randstr": randstr,
                    "ret": result.get("ret"),
                    "user_agent": user_agent,
                    "mode": "chrome_tencent_captcha",
                }
            finally:
                page.close()
                browser.close()


    def _solve_tencent_captcha_on_page(
        self,
        page,
        page_url: str,
        captcha_app_id: str,
        *,
        locale: str = "en",
        timeout_ms: int = 120000,
    ) -> dict:
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=min(max(timeout_ms, 30000), 90000))
        except Exception as exc:
            message = str(exc)
            if "Timeout" not in message and "timeout" not in message:
                raise
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass

        result = page.evaluate(
            """
            ({ appId, locale, timeoutMs }) => new Promise((resolve) => {
                let settled = false;
                const debug = { errors: [], logs: [] };
                window.addEventListener('error', (event) => debug.errors.push(String(event.message || event.error || 'error')), true);
                window.addEventListener('unhandledrejection', (event) => debug.errors.push(String(event.reason || 'unhandledrejection')), true);
                function snapshot(extra) {
                    let perf = [];
                    try {
                        perf = performance.getEntriesByType('resource')
                            .filter(e => /captcha|turing|qcloud|qq\.com/i.test(e.name))
                            .map(e => ({ name: e.name, initiatorType: e.initiatorType, duration: e.duration, transferSize: e.transferSize }));
                    } catch (_) {}
                    return Object.assign({
                        href: location.href,
                        keys: Object.keys(window).filter(k => /captcha|tcap|tencent/i.test(k)).sort(),
                        scripts: Array.from(document.scripts).map(s => s.src).filter(Boolean).filter(src => /captcha|turing|qcloud|qq\.com/i.test(src)),
                        perf,
                        debug
                    }, extra || {});
                }
                function done(value) {
                    if (settled) return;
                    settled = true;
                    resolve(value || {});
                }
                function getCaptchaCtor() {
                    return window.TencentCaptcha || window.TCaptcha || window.TencentCaptchaApp;
                }
                function waitForCtor(timeout) {
                    const start = Date.now();
                    return new Promise((resolveWait) => {
                        const tick = () => {
                            const ctor = getCaptchaCtor();
                            if (ctor) {
                                resolveWait(ctor);
                                return;
                            }
                            if (Date.now() - start >= timeout) {
                                resolveWait(null);
                                return;
                            }
                            setTimeout(tick, 250);
                        };
                        tick();
                    });
                }
                function appendScript(src) {
                    return new Promise((resolveLoad, rejectLoad) => {
                        const existing = document.querySelector(`script[src="${src}"]`);
                        if (existing) {
                            if (getCaptchaCtor()) {
                                resolveLoad();
                                return;
                            }
                            existing.addEventListener('load', () => resolveLoad(), { once: true });
                            existing.addEventListener('error', () => rejectLoad(new Error(`Failed to load ${src}`)), { once: true });
                            setTimeout(() => resolveLoad(), 5000);
                            return;
                        }
                        const script = document.createElement('script');
                        script.src = src;
                        script.async = true;
                        script.onload = () => resolveLoad();
                        script.onerror = () => rejectLoad(new Error(`Failed to load ${src}`));
                        document.head.appendChild(script);
                    });
                }
                async function loadScript() {
                    if (getCaptchaCtor()) return getCaptchaCtor();
                    const urls = [
                        'https://turing.captcha.qcloud.com/TCaptcha.js',
                        'https://ssl.captcha.qq.com/TCaptcha.js'
                    ];
                    let lastError = null;
                    for (const src of urls) {
                        try {
                            await appendScript(src);
                            const ctor = await waitForCtor(8000);
                            if (ctor) return ctor;
                        } catch (err) {
                            lastError = err;
                        }
                    }
                    const snap = snapshot({ lastError: String(lastError && lastError.message || lastError || '') });
                    throw new Error('TencentCaptcha is not available ' + JSON.stringify(snap));
                }
                loadScript().then((Captcha) => {
                    if (!Captcha) {
                        done({ ret: -1, error: 'TencentCaptcha is not available' });
                        return;
                    }
                    const language = String(locale || '').toLowerCase().startsWith('zh') ? 'zh-cn' : 'en';
                    const captcha = new Captcha(appId, (res) => {
                        window.__tencentCaptchaLastResult = res || {};
                        try { captcha.destroy && captcha.destroy(); } catch (_) {}
                        done(res || {});
                    }, {
                        userLanguage: language,
                        needFeedBack: false,
                    });
                    window.__tencentCaptchaInstance = captcha;
                    captcha.show();
                    setTimeout(() => {
                        if (!window.__tencentCaptchaLastResult) {
                            done({ ret: -3, pending: true, snapshot: snapshot() });
                        }
                    }, 3000);
                }).catch((err) => done({ ret: -1, error: String(err && err.message || err), snapshot: snapshot() }));
                setTimeout(() => done({ ret: -2, error: 'Tencent Captcha timeout' }), timeoutMs);
            })
            """,
            {"appId": captcha_app_id, "locale": locale, "timeoutMs": max(int(timeout_ms or 120000), 30000)},
        )
        return result if isinstance(result, dict) else {}


    def _auto_drag_tencent_captcha(self, page, *, timeout_ms: int = 120000) -> dict:
        """尝试用图灵识别距离 + 类人轨迹拖动腾讯滑块。"""
        import time

        deadline = time.time() + max(int(timeout_ms or 120000) / 1000.0, 30.0)
        fallback_distances = [210, 225, 240, 255, 270, 285, 195, 300]
        fallback_index = 0
        attempt = 0
        last_state: dict = {}

        while time.time() < deadline and attempt < 12:
            page.wait_for_timeout(1200)
            frame = self._find_tencent_captcha_frame(page)
            if frame is None:
                last_state = {"ret": -4, "error": "Tencent captcha frame not found"}
                continue

            slider = frame.locator(".tc-slider-normal, .tc-fg-item.tc-slider-normal").first
            try:
                box = slider.bounding_box(timeout=3000)
            except Exception:
                box = None
            if not box:
                last_state = {"ret": -5, "error": "Tencent slider handle not found"}
                continue

            distances: list[float] = []
            if self.tuling_solver and self.tuling_solver.configured:
                try:
                    recognized = self._recognize_tencent_slider_distance(frame)
                    distance = float(recognized.get("distance") or 0)
                    if distance > 0:
                        distances = [distance, distance - 3, distance + 3]
                        last_state = {
                            "ret": -8,
                            "tuling_distance": distance,
                            "tuling_raw_distance": recognized.get("raw_distance"),
                            "tuling_scale": recognized.get("scale"),
                        }
                except Exception as rec_exc:
                    last_state = {"ret": -8, "error": f"Tuling slider recognition failed: {rec_exc!r}"}

            if not distances:
                if fallback_index >= len(fallback_distances):
                    break
                distances = [float(fallback_distances[fallback_index])]
                fallback_index += 1

            for distance in distances:
                attempt += 1
                start_x = box["x"] + box["width"] / 2
                start_y = box["y"] + box["height"] / 2
                self._drag_mouse_like_human(page, start_x, start_y, distance)
                page.wait_for_timeout(2500)
                result = self._read_tencent_captcha_result(page)
                if result.get("ticket") and result.get("randstr"):
                    result["drag_attempts"] = attempt
                    result["drag_distance"] = distance
                    return result
                last_state = result or last_state or {"ret": -6, "error": "No Tencent callback after drag"}
                if attempt >= 12 or time.time() >= deadline:
                    break

            try:
                reload_btn = frame.locator("#reload, .tc-action--refresh").first
                if reload_btn.count() > 0:
                    reload_btn.click(timeout=1500)
            except Exception:
                pass

        return {**last_state, "ret": last_state.get("ret", -7), "error": last_state.get("error", "Tencent auto drag failed")}

    def _recognize_tencent_slider_distance(self, frame) -> dict:
        """截图腾讯滑块 iframe，并用图灵识别换算拖动距离。"""
        import base64

        if not self.tuling_solver or not self.tuling_solver.configured:
            raise RuntimeError("图灵验证码识别未配置")

        target = frame.locator("body").first
        box = target.bounding_box(timeout=3000)
        if not box:
            target = frame.locator("#slideBg, .tc-bg-img").first
            box = target.bounding_box(timeout=3000)
        if not box:
            raise RuntimeError("腾讯滑块截图区域不存在")

        png = target.screenshot(type="png", timeout=5000)
        image_width = self._png_width(png)
        image_b64 = base64.b64encode(png).decode("ascii")
        solved = self.tuling_solver.solve_slider_distance_from_b64(
            image_b64,
            css_width=float(box.get("width") or 0),
            image_width=image_width,
        )
        solved["screenshot_css_width"] = float(box.get("width") or 0)
        solved["screenshot_image_width"] = image_width
        return solved

    @staticmethod
    def _png_width(data: bytes) -> int:
        import struct

        if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            return int(struct.unpack(">I", data[16:20])[0])
        return 0

    @staticmethod
    def _find_tencent_captcha_frame(page):
        for frame in page.frames:
            url = str(frame.url or "")
            if "turing.captcha" in url and "drag" in url:
                return frame
        for frame in page.frames:
            try:
                if frame.locator(".tc-slider-normal, #slideBg").count() > 0:
                    return frame
            except Exception:
                continue
        return None

    @staticmethod
    def _read_tencent_captcha_result(page) -> dict:
        try:
            data = page.evaluate("() => window.__tencentCaptchaLastResult || null")
        except Exception:
            data = None
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _drag_mouse_like_human(page, start_x: float, start_y: float, distance: float) -> None:
        import random
        steps = random.randint(28, 42)
        page.mouse.move(start_x, start_y, steps=6)
        page.mouse.down()
        moved = 0.0
        for i in range(1, steps + 1):
            t = i / steps
            moved = distance * (1 - (1 - t) ** 2.6)
            jitter_y = random.uniform(-1.2, 1.2)
            page.mouse.move(start_x + moved, start_y + jitter_y, steps=1)
            page.wait_for_timeout(random.randint(8, 24))
        for delta in [random.uniform(-3, -1), random.uniform(1, 3), random.uniform(-1, 1)]:
            moved += delta
            page.mouse.move(start_x + moved, start_y + random.uniform(-0.8, 0.8), steps=2)
            page.wait_for_timeout(random.randint(20, 60))
        page.mouse.up()


    def _solve_regular_turnstile(self, page, page_url: str, site_key: str) -> str:
        """常规 Turnstile 页面求解。

        Cloudflare 有时会让真实浏览器导航长期停在挑战/中间页，
        导致 Playwright 的 domcontentloaded 等待超时。但页面主体或
        Turnstile iframe 可能已经可交互，因此导航超时不应直接中断
        注册链路，而是继续尝试读取/点击 token。
        """
        try:
            page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
        except Exception as exc:
            message = str(exc)
            if "Timeout" not in message and "timeout" not in message:
                raise
            try:
                page.wait_for_timeout(1000)
            except Exception:
                pass
        return self._click_until_token(page, site_key)

    @staticmethod
    def _cookie_dict_for_url(context, page_url: str) -> dict[str, str]:
        from urllib.parse import urlparse

        host = str(urlparse(page_url).hostname or "").lower()
        cookies: dict[str, str] = {}
        try:
            for cookie in context.cookies():
                name = str(cookie.get("name") or "")
                value = str(cookie.get("value") or "")
                domain = str(cookie.get("domain") or "").lstrip(".").lower()
                if not name or value is None:
                    continue
                if host and domain and not (host == domain or host.endswith(f".{domain}") or domain.endswith(f".{host}")):
                    continue
                cookies[name] = value
        except Exception:
            return cookies
        return cookies

    def _solve_clerk_turnstile(self, page, page_url: str, site_key: str) -> str:
        import logging
        log = logging.getLogger("cdp_turnstile")

        log.info("Clerk Turnstile: direct widget render with sitekey=%s", site_key)
        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

        page.wait_for_timeout(2000)

        token = page.evaluate("""
            (siteKey) => new Promise((resolve) => {
                const existing = document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]');
                function renderWidget() {
                    if (typeof window.turnstile === 'undefined') {
                        setTimeout(renderWidget, 500);
                        return;
                    }
                    let container = document.getElementById('cdp-turnstile-container');
                    if (!container) {
                        container = document.createElement('div');
                        container.id = 'cdp-turnstile-container';
                        document.body.appendChild(container);
                    }
                    window.turnstile.render(container, {
                        sitekey: siteKey,
                        callback: (token) => resolve(token),
                        'error-callback': () => resolve(''),
                        'timeout-callback': () => resolve(''),
                    });
                }
                if (!existing) {
                    const script = document.createElement('script');
                    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
                    script.onload = () => renderWidget();
                    script.onerror = () => resolve('');
                    document.head.appendChild(script);
                } else {
                    renderWidget();
                }
                setTimeout(() => resolve(''), 45000);
            })
        """, site_key)

        token = str(token or "").strip()
        if token:
            log.info("Clerk Turnstile token obtained (%d chars) via direct render", len(token))
        else:
            log.warning("Clerk Turnstile direct render: no token after 45s, falling back to intercept method")
            token = self._solve_clerk_turnstile_intercept(page, page_url, site_key)
        return token

    def _solve_clerk_turnstile_intercept(self, page, page_url: str, site_key: str) -> str:
        import logging, uuid
        from urllib.parse import parse_qs
        log = logging.getLogger("cdp_turnstile")

        captured = {"token": ""}

        def _intercept(route):
            req = route.request
            if "sign_ups" in req.url and req.method == "POST":
                body = req.post_data or ""
                params = parse_qs(body)
                t = params.get("captcha_token", [""])[0]
                if t:
                    captured["token"] = t
            route.continue_()

        page.route("**/v1/client/sign_ups**", _intercept)

        page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

        try:
            page.wait_for_selector('input[type="email"], input[name="email"]', timeout=20000)
        except Exception:
            return ""

        dummy_email = f"ts{uuid.uuid4().hex[:10]}@proton.me"
        dummy_pass = f"Ts{uuid.uuid4().hex[:12]}!1"

        try:
            page.locator('input[type="email"], input[name="email"]').first.fill(dummy_email)
            page.locator('button:has-text("Sign up")').first.click()
        except Exception:
            return ""
        page.wait_for_timeout(2000)

        try:
            page.wait_for_selector('input[type="password"]', timeout=10000)
            page.locator('input[type="password"]').first.fill(dummy_pass)
        except Exception:
            pass

        page.wait_for_timeout(500)

        try:
            page.locator('button:has-text("Sign up")').first.click()
        except Exception:
            return ""

        for i in range(15):
            page.wait_for_timeout(2000)
            if captured["token"]:
                log.info("Intercept token captured (%d chars) after %ds", len(captured["token"]), (i+1)*2)
                return captured["token"]

        return ""

    def _click_until_token(self, page, site_key: str, retries: int = 6, wait_ms: int = 4000) -> str:
        page.wait_for_timeout(2000)
        for attempt in range(1, retries + 1):
            token = self._read_token(page)
            if token:
                return token
            frame = self._find_turnstile_frame(page, site_key)
            if frame:
                try:
                    checkbox = frame.locator("input[type='checkbox']")
                    if checkbox.count() > 0:
                        checkbox.first.click(timeout=3000)
                    else:
                        box = frame.locator("body").bounding_box()
                        if box:
                            page.mouse.click(box["x"] + 28, box["y"] + 22, delay=100)
                except Exception:
                    pass
            else:
                widget = page.locator(f"[data-sitekey='{site_key}'], .cf-turnstile, #cf-turnstile")
                if widget.count() > 0:
                    box = widget.first.bounding_box()
                    if box:
                        page.mouse.move(box["x"] + 20, box["y"] + 18, steps=8)
                        page.wait_for_timeout(100)
                        page.mouse.click(box["x"] + 28, box["y"] + 22, delay=100)
            page.wait_for_timeout(wait_ms)
            token = self._read_token(page)
            if token:
                return token
        return ""

    def _find_turnstile_frame(self, page, site_key: str):
        for frame in page.frames:
            url = frame.url or ""
            if "challenges.cloudflare.com" in url or "turnstile" in url:
                return frame
        return None

    @staticmethod
    def _read_token(page) -> str:
        return str(page.evaluate("""() => {
            const f = document.querySelector(
                "input[name='cf-turnstile-response'], textarea[name='cf-turnstile-response'], "
                + "input[name='captcha'], textarea[name='captcha']"
            );
            return f ? (f.value || "") : "";
        }""") or "")

    def _resolve_chrome(self) -> str:
        import pathlib
        if self.chrome_path:
            if pathlib.Path(self.chrome_path).exists():
                return self.chrome_path
            raise RuntimeError(f"CDP Turnstile: chrome_path not found: {self.chrome_path}")
        for candidate in self.DEFAULT_CHROME_PATHS:
            if pathlib.Path(candidate).exists():
                return candidate
        raise RuntimeError("CDP Turnstile: Chrome/Chromium not found, set chrome_path in settings")

    @staticmethod
    def _find_free_port() -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            return int(s.getsockname()[1])

    @staticmethod
    def _wait_cdp_ready(port: int, timeout: int = 20) -> None:
        import time, requests as _req
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{port}/json/version"
        while time.time() < deadline:
            try:
                if _req.get(url, timeout=1.5).ok:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"CDP Turnstile: Chrome not ready on port {port}")

    @staticmethod
    def _kill_process(process) -> None:
        import os, subprocess, signal
        try:
            if process.poll() is not None:
                return
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            else:
                os.kill(process.pid, signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    @staticmethod
    def _purge_stale_profiles(base_dir: str, max_age_sec: int = 300) -> None:
        import os, time, shutil
        if not os.path.isdir(base_dir):
            return
        now = time.time()
        try:
            for name in os.listdir(base_dir):
                d = os.path.join(base_dir, name)
                if not os.path.isdir(d) or not name.startswith("cdp-"):
                    continue
                try:
                    if now - os.path.getmtime(d) > max_age_sec:
                        shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _cleanup_dir(path: str) -> None:
        import os, time, shutil, subprocess as _sp
        for delay in [1, 2, 5, 10, 30]:
            time.sleep(delay)
            try:
                shutil.rmtree(path, ignore_errors=True)
                if not os.path.exists(path):
                    return
            except Exception:
                pass
        if os.name == "nt" and os.path.exists(path):
            try:
                _sp.run(["cmd", "/c", "rmdir", "/s", "/q", path],
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10, check=False)
            except Exception:
                pass

    def solve_image(self, image_b64: str) -> str:
        raise NotImplementedError


class PatchrightHarvester(BaseCaptcha):
    """Lightweight local Turnstile solver using patchright headless Chromium.

    Renders the Turnstile widget on a minimal stub page — no real site loaded,
    minimal memory, no third-party captcha service needed.
    """

    _STEALTH_INIT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
    const _origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery.call(window.navigator.permissions, params);
    """

    _LAUNCH_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--disable-hang-monitor",
        "--disable-domain-reliability",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-ipc-flooding-protection",
        "--disable-component-update",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--metrics-recording-only",
        "--mute-audio",
        "--window-position=-32000,-32000",
        "--window-size=800,600",
    ]

    def __init__(self, *, headless: bool = False, max_contexts: int = 3, proxy: str = ""):
        self._headless = False
        self._max_contexts = max(1, max_contexts)
        self._proxy = proxy.strip()
        self._pw = None
        self._browser = None
        self._engine = "patchright"

    def _ensure_browser(self):
        if self._browser and self._browser.is_connected():
            return
        try:
            from patchright.sync_api import sync_playwright
            self._engine = "patchright"
        except ImportError:
            raise RuntimeError(
                "PatchrightHarvester requires patchright (CDP-patched Chromium). "
                "Install: pip install patchright && python -m patchright install chromium"
            )

        if self._pw is None:
            self._pw = sync_playwright().start()
        launch_opts = {
            "headless": self._headless,
            "args": self._LAUNCH_ARGS,
        }
        if self._proxy:
            from urllib.parse import urlparse
            parsed = urlparse(self._proxy)
            proxy_cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
            if parsed.username:
                proxy_cfg["username"] = parsed.username
            if parsed.password:
                proxy_cfg["password"] = parsed.password
            launch_opts["proxy"] = proxy_cfg
        self._browser = self._pw.chromium.launch(**launch_opts)

    def solve_turnstile(self, page_url: str, site_key: str) -> str:
        """Harvest Turnstile token by driving Clerk's own sign-up flow.

        Navigate to the sign-up page, fill dummy email+password, click submit,
        and intercept the captcha_token from Clerk's sign_ups POST. Clerk's JS
        triggers invisible Turnstile automatically in smart mode.
        """
        import logging, time, uuid
        from urllib.parse import parse_qs
        log = logging.getLogger("patchright_harvester")
        self._ensure_browser()

        ctx = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.add_init_script(self._STEALTH_INIT)

        captured = {"token": ""}

        def _intercept(route):
            req = route.request
            if "sign_ups" in req.url and req.method == "POST":
                body = req.post_data or ""
                params = parse_qs(body)
                t = params.get("captcha_token", [""])[0]
                if t:
                    captured["token"] = t
                    route.abort()
                    return
            route.continue_()

        page.route("**/v1/client/sign_ups**", _intercept)

        try:
            t0 = time.time()
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)

            page.wait_for_selector('input[type="email"], input[name="email"]', timeout=20000)

            dummy_email = f"ph{uuid.uuid4().hex[:10]}@proton.me"
            dummy_pass = f"Ph{uuid.uuid4().hex[:12]}!1"

            page.locator('input[type="email"], input[name="email"]').first.fill(dummy_email)
            page.locator('button:has-text("Sign up")').first.click()
            page.wait_for_timeout(2000)

            try:
                page.wait_for_selector('input[type="password"]', timeout=10000)
                page.locator('input[type="password"]').first.fill(dummy_pass)
            except Exception:
                pass

            page.wait_for_timeout(500)
            page.locator('button:has-text("Sign up")').first.click()

            turnstile_clicked = False
            submit_after_turnstile = False
            for i in range(40):
                if captured["token"]:
                    elapsed = round(time.time() - t0, 2)
                    log.warning("PatchrightHarvester: token (%d chars) in %.1fs", len(captured["token"]), elapsed)
                    return captured["token"]
                if not turnstile_clicked:
                    try:
                        cf_frames = [f for f in page.frames if "challenges.cloudflare.com" in f.url]
                        for frame in cf_frames:
                            log.warning("PatchrightHarvester: found CF frame: %s", frame.url[:120])
                            try:
                                body = frame.locator("body")
                                body.click(timeout=3000, position={"x": 24, "y": 24})
                                turnstile_clicked = True
                                log.warning("PatchrightHarvester: clicked Turnstile body")
                                break
                            except Exception as click_exc:
                                log.warning("PatchrightHarvester: body click failed: %s", str(click_exc)[:200])
                    except Exception as exc:
                        log.warning("PatchrightHarvester: frame scan failed: %s", str(exc)[:200])
                elif not submit_after_turnstile:
                    try:
                        btn = page.locator('button:has-text("Sign up")').first
                        if btn.is_visible(timeout=500):
                            btn.click(timeout=3000)
                            submit_after_turnstile = True
                            log.warning("PatchrightHarvester: re-clicked Sign up after Turnstile")
                    except Exception:
                        pass
                page.wait_for_timeout(2000)

            elapsed = round(time.time() - t0, 2)
            raise RuntimeError(f"PatchrightHarvester: no captcha_token intercepted after {elapsed}s")
        finally:
            page.close()
            ctx.close()

    def solve_image(self, image_b64: str) -> str:
        raise NotImplementedError

    def close(self):
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._browser = None
        self._pw = None


def _definition_auth_fields(definition) -> list[str]:
    if not definition:
        return []
    return [
        str(field.get("key") or "")
        for field in definition.get_fields()
        if str(field.get("category") or "") == "auth" and str(field.get("key") or "")
    ]


def has_captcha_configured(provider_key: str, extra: dict | None = None) -> bool:
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    key = str(provider_key or "").strip()
    if key in {"manual", "local_solver", "cdp_turnstile", "patchright_harvester"}:
        return True
    definition = ProviderDefinitionsRepository().get_by_key("captcha", key)
    if not definition or not definition.enabled:
        return False

    merged = ProviderSettingsRepository().resolve_runtime_settings("captcha", key, extra or {})
    auth_fields = _definition_auth_fields(definition)
    if key == "tulingcloud":
        return bool(
            str(merged.get("tuling_usertoken", "") or "").strip()
            or (
                str(merged.get("tuling_username", "") or "").strip()
                and str(merged.get("tuling_password", "") or "").strip()
            )
        )
    if not auth_fields:
        return True
    return any(str(merged.get(field_key, "")).strip() for field_key in auth_fields)


def create_captcha_solver(provider_key: str, extra: dict | None = None) -> BaseCaptcha:
    from infrastructure.provider_definitions_repository import ProviderDefinitionsRepository
    from infrastructure.provider_settings_repository import ProviderSettingsRepository

    key = str(provider_key or "").strip().lower()
    if key == "manual":
        return ManualCaptcha()

    definition = ProviderDefinitionsRepository().get_by_key("captcha", key)
    settings_repo = ProviderSettingsRepository()
    merged = settings_repo.resolve_runtime_settings("captcha", key, extra or {})
    driver_type = (definition.driver_type if definition else key).lower()

    if driver_type == "local_solver":
        return LocalSolverCaptcha(merged.get("solver_url", "") or "http://localhost:8889")
    if driver_type == "cdp_turnstile":
        tuling_solver = None
        tuling_settings = settings_repo.resolve_runtime_settings("captcha", "tulingcloud", {})
        for tuling_key in (
            "tuling_username",
            "tuling_password",
            "tuling_usertoken",
            "tuling_api_base",
            "tuling_slider_model_id",
            "tuling_developer",
        ):
            if not str(merged.get(tuling_key, "") or "").strip() and str(tuling_settings.get(tuling_key, "") or "").strip():
                merged[tuling_key] = tuling_settings[tuling_key]
        tuling_username = str(merged.get("tuling_username", "") or "").strip()
        tuling_password = str(merged.get("tuling_password", "") or "").strip()
        tuling_usertoken = str(merged.get("tuling_usertoken", "") or "").strip()
        if tuling_usertoken or (tuling_username and tuling_password):
            tuling_solver = TulingCloudCaptcha(
                username=tuling_username,
                password=tuling_password,
                usertoken=tuling_usertoken,
                api_base=str(merged.get("tuling_api_base", "") or "http://www.tulingcloud.com"),
                model_id=str(merged.get("tuling_slider_model_id", "") or "48956156"),
                developer=str(merged.get("tuling_developer", "") or ""),
            )
        return CdpTurnstileSolver(
            chrome_path=str(merged.get("chrome_path", "") or ""),
            cdp_url=str(merged.get("chrome_cdp_url", "") or ""),
            headless=str(merged.get("cdp_headless", "false") or "false").strip().lower() in {"1", "true", "yes"},
            navigation_timeout_ms=int(merged.get("cdp_navigation_timeout_ms", 90000) or 90000),
            tuling_solver=tuling_solver,
        )
    if driver_type == "patchright_harvester":
        return PatchrightHarvester(
            headless=str(merged.get("harvester_headless", "false") or "false").strip().lower() in {"1", "true", "yes"},
            max_contexts=int(merged.get("harvester_max_contexts", 3) or 3),
            proxy=str(merged.get("harvester_proxy", "") or ""),
        )
    if driver_type == "tulingcloud_api":
        return TulingCloudCaptcha(
            username=str(merged.get("tuling_username", "") or ""),
            password=str(merged.get("tuling_password", "") or ""),
            usertoken=str(merged.get("tuling_usertoken", "") or ""),
            api_base=str(merged.get("tuling_api_base", "") or "http://www.tulingcloud.com"),
            model_id=str(merged.get("tuling_slider_model_id", "") or "48956156"),
            developer=str(merged.get("tuling_developer", "") or ""),
        )
    if driver_type == "yescaptcha_api":
        client_key = str(merged.get("yescaptcha_key", "") or "")
        if not client_key:
            raise RuntimeError("YesCaptcha / 兼容 API Client Key 未配置，无法继续协议注册")
        api_base = str(merged.get("yescaptcha_api_url", "") or "https://api.yescaptcha.com")
        return YesCaptcha(client_key, api_base)
    if driver_type == "twocaptcha_api":
        api_key = str(merged.get("twocaptcha_key", "") or "")
        if not api_key:
            raise RuntimeError("2Captcha Key 未配置，无法继续协议注册")
        return TwoCaptcha(api_key)
    raise ValueError(f"未知验证码解决器: {provider_key}")
