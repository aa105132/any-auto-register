"""AI-ROUTER Turnstile CDP 采集器。

只用于通过 Cloudflare Turnstile 挑战；注册、发码、创建 API Key 均由协议接口完成。
"""
from __future__ import annotations

import base64
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from core.proxy_utils import normalize_proxy_url
from platforms.enter.browser_register import DEFAULT_CHROME_PATHS

REGISTER_URL = "https://ai-router.dev/register"


class _MiniCDPWebSocket:
    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        sock = socket.create_connection((host, port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode("ascii"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("CDP websocket handshake failed")
            data += chunk
        if b" 101 " not in data.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"CDP websocket handshake failed: {data[:200]!r}")
        self.sock = sock

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def send_json(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_frame(text)

    def recv_json(self, timeout: float | None = None) -> dict[str, Any]:
        old_timeout = None
        if timeout is not None and self.sock is not None:
            old_timeout = self.sock.gettimeout()
            self.sock.settimeout(timeout)
        try:
            data = self._recv_frame()
            return json.loads(data.decode("utf-8", errors="replace"))
        finally:
            if timeout is not None and self.sock is not None:
                self.sock.settimeout(old_timeout)

    def _send_frame(self, payload: bytes) -> None:
        if not self.sock:
            raise RuntimeError("websocket not connected")
        first = 0x81
        mask_bit = 0x80
        length = len(payload)
        header = bytearray([first])
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n: int) -> bytes:
        if not self.sock:
            raise RuntimeError("websocket not connected")
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise RuntimeError("websocket closed")
            data += chunk
        return data

    def _recv_frame(self) -> bytes:
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("websocket closed")
            if opcode == 0x9:
                self._send_pong(payload)
                continue
            if opcode == 0x1:
                return payload

    def _send_pong(self, payload: bytes) -> None:
        if not self.sock:
            return
        self.sock.sendall(bytes([0x8A, len(payload)]) + payload)


class _RawCDPPage:
    def __init__(self, ws_url: str, *, username: str = "", password: str = "", log_fn=print) -> None:
        self.ws = _MiniCDPWebSocket(ws_url)
        self.next_id = 0
        self.pending: dict[int, dict[str, Any]] = {}
        self.username = username
        self.password = password
        self.log = log_fn or (lambda _msg: None)

    def connect(self) -> None:
        self.ws.connect()

    def close(self) -> None:
        self.ws.close()

    def cmd(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        self.next_id += 1
        msg_id = self.next_id
        self.ws.send_json({"id": msg_id, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                event = self.ws.recv_json(timeout=max(0.5, min(2.0, deadline - time.time())))
            except socket.timeout:
                continue
            if "id" in event:
                if event.get("id") == msg_id:
                    if "error" in event:
                        raise RuntimeError(f"CDP {method} error: {event['error']}")
                    return event.get("result") or {}
                self.pending[int(event.get("id"))] = event
                continue
            self._handle_event(event)
        raise RuntimeError(f"CDP command timeout: {method}")

    def send(self, method: str, params: dict[str, Any] | None = None) -> int:
        self.next_id += 1
        msg_id = self.next_id
        self.ws.send_json({"id": msg_id, "method": method, "params": params or {}})
        return msg_id

    def pump(self, seconds: float = 0.5) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                event = self.ws.recv_json(timeout=max(0.05, min(0.5, deadline - time.time())))
            except TimeoutError:
                return
            except socket.timeout:
                return
            except Exception:
                return
            if "id" not in event:
                self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Fetch.authRequired":
            request_id = params.get("requestId")
            if request_id:
                self.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": self.username,
                            "password": self.password,
                        },
                    },
                )
        elif method == "Fetch.requestPaused":
            request_id = params.get("requestId")
            if request_id:
                self.send("Fetch.continueRequest", {"requestId": request_id})

    def evaluate(self, expression: str, *, timeout: float = 10.0) -> Any:
        result = self.cmd(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=timeout,
        )
        return ((result.get("result") or {}).get("value"))

    def mouse_click(self, x: float, y: float) -> None:
        for typ in ("mouseMoved", "mousePressed", "mouseReleased"):
            params: dict[str, Any] = {"type": typ, "x": x, "y": y, "button": "left", "clickCount": 1}
            if typ == "mouseMoved":
                params.pop("button", None)
                params.pop("clickCount", None)
            self.cmd("Input.dispatchMouseEvent", params, timeout=5)
            time.sleep(0.08)


class AiRouterTurnstileHarvester:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: int = 180,
        chrome_path: str = "",
        cdp_url: str = "",
        log_fn=print,
        browser_fingerprint: dict[str, Any] | None = None,
        allow_external_cdp: bool = False,
    ) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.chrome_path = chrome_path
        self.cdp_url = cdp_url
        self.log = log_fn or (lambda _msg: None)
        self.browser_fingerprint = dict(browser_fingerprint or {})
        self.allow_external_cdp = bool(allow_external_cdp)

    def _l(self, msg: str) -> None:
        self.log(f"[AI-ROUTER:turnstile] {msg}")

    def harvest(self, *, email: str = "", password: str = "") -> str:
        launch_meta = self._prepare_chrome()
        page: _RawCDPPage | None = None
        try:
            ws_url = self._create_or_get_page_ws(launch_meta["cdp_url"])
            username, proxy_password = self._proxy_credentials()
            page = _RawCDPPage(ws_url, username=username, password=proxy_password, log_fn=self.log)
            page.connect()
            page.cmd("Page.enable")
            page.cmd("Runtime.enable")
            page.cmd("DOM.enable")
            self._apply_cdp_fingerprint(page)
            if username or proxy_password:
                page.cmd("Fetch.enable", {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]})
            self._l("CDP 打开注册页")
            # 走带认证代理时 Page.navigate 的响应可能被代理认证流程拖住；
            # 这里不等待 navigate command 返回，持续 pump CDP 事件以处理 Fetch.authRequired。
            page.send("Page.navigate", {"url": REGISTER_URL})
            self._wait_ready(page)
            self._prefill(page, email=email, password=password)
            if not self._has_turnstile_widget(page, timeout_sec=12):
                self._l("页面未发现 Turnstile 组件，跳过 token 采集")
                return ""
            token = self._click_until_token(page)
            if token:
                self._l(f"Turnstile token obtained length={len(token)}")
            return token
        except Exception as exc:
            self._l(f"Turnstile CDP 失败，保留窗口 20 秒用于观察: {exc}")
            time.sleep(20)
            raise
        finally:
            try:
                if page is not None:
                    page.close()
            except Exception:
                pass
            self._teardown_chrome(launch_meta)

    def _prepare_chrome(self) -> dict[str, Any]:
        if self.cdp_url and self.allow_external_cdp:
            self._l("允许附加外部 CDP；请确认该浏览器自身已配置同一个任务代理")
            return {"cdp_url": self.cdp_url, "process": None, "profile_dir": None}
        if self.cdp_url and not self.allow_external_cdp:
            self._l("已忽略外部 CDP 地址，改为启动受控 Chrome，避免继承本机系统代理/Clash")
        port = self._find_free_port()
        profile_dir = Path("output/browser_auth_profiles").resolve() / f"airouter-{int(time.time()*1000)}-{random.randint(1000,9999)}"
        profile_dir.mkdir(parents=True, exist_ok=True)
        chrome_path = self._resolve_chrome_path()
        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--window-size={int(self.browser_fingerprint.get('viewport_width') or 1440)},{int(self.browser_fingerprint.get('viewport_height') or 900)}",
            f"--lang={self.browser_fingerprint.get('locale') or 'en-US'}",
            "--disable-translate",
            "--disable-features=Translate,TranslateUI",
        ]
        # AI-ROUTER 现在要求 Turnstile、发码、注册都走同一出口 IP。
        # 这里把真实 Chrome 也挂到项目代理上，再由 CDP 处理代理认证。
        proxy_server = self._proxy_server_arg()
        if proxy_server:
            args.extend([f"--proxy-server={proxy_server}", "--proxy-bypass-list=<-loopback>"])
            self._l(f"CDP Turnstile token 采集使用项目代理: {proxy_server}")
        ua = str(self.browser_fingerprint.get("user_agent") or "")
        if ua:
            args.append(f"--user-agent={ua}")
        process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._wait_for_cdp(port)
        return {"cdp_url": f"http://127.0.0.1:{port}", "process": process, "profile_dir": profile_dir}

    def _teardown_chrome(self, meta: dict[str, Any]) -> None:
        process = meta.get("process")
        if process:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        profile_dir = meta.get("profile_dir")
        if profile_dir is not None:
            try:
                shutil.rmtree(profile_dir, ignore_errors=True)
            except Exception:
                pass

    def _resolve_chrome_path(self) -> str:
        if self.chrome_path and Path(self.chrome_path).exists():
            return self.chrome_path
        for candidate in DEFAULT_CHROME_PATHS:
            if Path(candidate).exists():
                return candidate
        raise RuntimeError("Chrome not found. Set chrome_path or install Chrome.")

    def _find_free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            return int(s.getsockname()[1])

    def _wait_for_cdp(self, port: int) -> None:
        deadline = time.time() + 30
        url = f"http://127.0.0.1:{port}/json/version"
        while time.time() < deadline:
            try:
                session = requests.Session()
                session.trust_env = False
                if session.get(url, timeout=1.5).ok:
                    return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"Chrome CDP port {port} not ready")

    def _create_or_get_page_ws(self, cdp_url: str) -> str:
        base = cdp_url.rstrip("/")
        try:
            session = requests.Session()
            session.trust_env = False
            resp = session.put(f"{base}/json/new?{urllib.parse.quote(REGISTER_URL, safe='')}", timeout=5)
            if resp.ok:
                data = resp.json()
                ws = data.get("webSocketDebuggerUrl")
                if ws:
                    return ws
        except Exception:
            pass
        session = requests.Session()
        session.trust_env = False
        tabs = session.get(f"{base}/json/list", timeout=5).json()
        for tab in tabs:
            if tab.get("type") == "page" and tab.get("webSocketDebuggerUrl"):
                return tab["webSocketDebuggerUrl"]
        raise RuntimeError("CDP page websocket not found")

    def _proxy_parts(self) -> urllib.parse.SplitResult | None:
        if not self.proxy:
            return None
        normalized = normalize_proxy_url(self.proxy, default_scheme="http")
        if not normalized:
            return None
        return urllib.parse.urlsplit(normalized)

    def _proxy_server_arg(self) -> str:
        parsed = self._proxy_parts()
        if not parsed or not parsed.hostname:
            return ""
        scheme = parsed.scheme or "http"
        port = parsed.port or (443 if scheme == "https" else 80)
        return f"{scheme}://{parsed.hostname}:{port}"

    def _proxy_credentials(self) -> tuple[str, str]:
        parsed = self._proxy_parts()
        if not parsed:
            return "", ""
        return urllib.parse.unquote(parsed.username or ""), urllib.parse.unquote(parsed.password or "")

    def _apply_cdp_fingerprint(self, page: _RawCDPPage) -> None:
        ua = str(self.browser_fingerprint.get("user_agent") or "")
        locale = str(self.browser_fingerprint.get("locale") or "en-US")
        width = int(self.browser_fingerprint.get("viewport_width") or 1440)
        height = int(self.browser_fingerprint.get("viewport_height") or 900)
        scale = float(self.browser_fingerprint.get("device_scale_factor") or 1)
        try:
            if ua:
                page.cmd("Network.enable", timeout=5)
                page.cmd("Network.setUserAgentOverride", {
                    "userAgent": ua,
                    "acceptLanguage": str(self.browser_fingerprint.get("accept_language") or f"{locale},en;q=0.9"),
                    "platform": str(self.browser_fingerprint.get("platform") or "Windows"),
                }, timeout=5)
            page.cmd("Emulation.setDeviceMetricsOverride", {
                "width": width,
                "height": height,
                "deviceScaleFactor": scale,
                "mobile": False,
            }, timeout=5)
            page.cmd("Emulation.setLocaleOverride", {"locale": locale}, timeout=5)
        except Exception as exc:
            self._l(f"CDP 指纹设置失败，继续: {exc}")

    def _wait_ready(self, page: _RawCDPPage) -> None:
        deadline = time.time() + max(30, min(self.timeout, 90))
        while time.time() < deadline:
            page.pump(0.3)
            try:
                state = page.evaluate("document.readyState", timeout=3)
                if state in {"interactive", "complete"}:
                    return
            except Exception:
                pass
            time.sleep(0.5)

    def _prefill(self, page: _RawCDPPage, *, email: str, password: str) -> None:
        script = f"""
        (() => {{
          const setValue = (selector, value) => {{
            const el = document.querySelector(selector);
            if (!el || !value) return false;
            el.focus();
            el.value = value;
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
            el.dispatchEvent(new Event('change', {{bubbles:true}}));
            return true;
          }};
          return {{
            email: setValue("input[type='email'], input#email, input[name='email']", {json.dumps(email)}),
            password: setValue("input[type='password'], input#password, input[name='password']", {json.dumps(password)})
          }};
        }})()
        """
        try:
            page.evaluate(script, timeout=5)
        except Exception as exc:
            self._l(f"预填表单失败，继续采集 Turnstile: {exc}")

    def _read_token(self, page: _RawCDPPage) -> str:
        script = """
        (() => {
          const selectors = [
            "input[name='cf-turnstile-response']",
            "textarea[name='cf-turnstile-response']",
            "input[name='turnstile']",
            "textarea[name='turnstile']"
          ];
          for (const selector of selectors) {
            const el = document.querySelector(selector);
            if (el && el.value) return el.value;
          }
          return '';
        })()
        """
        try:
            return str(page.evaluate(script, timeout=3) or "").strip()
        except Exception:
            return ""

    def _has_turnstile_widget(self, page: _RawCDPPage, *, timeout_sec: int = 8) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            page.pump(0.3)
            if self._turnstile_box(page):
                return True
            time.sleep(0.5)
        return False

    def _turnstile_box(self, page: _RawCDPPage) -> dict[str, float] | None:
        script = """
        (() => {
          const el = document.querySelector("iframe[src*='challenges.cloudflare.com'], .cf-turnstile, [class*='turnstile']");
          if (!el) return null;
          const r = el.getBoundingClientRect();
          if (!r || r.width <= 0 || r.height <= 0) return null;
          return {x:r.x, y:r.y, width:r.width, height:r.height};
        })()
        """
        try:
            box = page.evaluate(script, timeout=3)
            return box if isinstance(box, dict) else None
        except Exception:
            return None

    def _click_until_token(self, page: _RawCDPPage) -> str:
        deadline = time.time() + max(45, self.timeout)
        attempts = 0
        while time.time() < deadline:
            token = self._read_token(page)
            if token:
                return token
            attempts += 1
            clicked = self._click_turnstile_widget(page, attempts=attempts)
            page.pump(2.0 if clicked else 0.8)
            if attempts % 5 == 0:
                self._l("等待 Turnstile token...")
        return self._read_token(page)

    def _click_turnstile_widget(self, page: _RawCDPPage, *, attempts: int = 1) -> bool:
        box = self._turnstile_box(page)
        if not box:
            return False
        x0 = float(box.get("x") or 0)
        y0 = float(box.get("y") or 0)
        w = float(box.get("width") or 0)
        h = float(box.get("height") or 0)
        points = [
            (x0 + 30, y0 + h / 2),       # checkbox 左侧
            (x0 + 24, y0 + 24),          # checkbox 左上
            (x0 + min(45, w * 0.18), y0 + h / 2),
            (x0 + w / 2, y0 + h / 2),    # fallback
        ]
        x, y = points[(attempts - 1) % len(points)]
        self._l(f"点击 Turnstile x={int(x)} y={int(y)} attempt={attempts}")
        try:
            page.mouse_click(x, y)
            return True
        except Exception as exc:
            self._l(f"点击 Turnstile 失败: {exc}")
            return False
