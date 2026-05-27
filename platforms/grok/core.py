"""
Grok (x.ai) 自动注册 - 协议优先实现。

运行时证据（2026-05）：
- 邮箱验证码 RPC 实际服务端 endpoint 为根路径 /auth_mgmt...；/xai-account 当前会回落 HTML。
- 最终注册走 Next.js Server Action，需要当前 next-action id。
- Cloudflare/Castle/Turnstile 属于反滥用边界：默认协议链路，只有遇到
  Cloudflare 页面挑战时才通过 CDP/真实浏览器同步 cookies 后回到协议。
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import string
import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from curl_cffi import requests as cffi_requests

ACCOUNTS_URL = "https://accounts.x.ai"
SIGNUP_URL = f"{ACCOUNTS_URL}/sign-up"
RPC_BASE_PATH = ""
TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"
CASTLE_PUBLISHABLE_KEY = "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"
NEXT_ACTION = "7f16f8dd3aab1de7d10bcc2b117e6c24c0e38a935a"

# 由 /sign-up 的 Next 初始 flight tree 推导。Server Action 请求需要该头，
# 缺失时新版 Next 可能返回 action not found 或普通 HTML。
RSC_SIGNUP_STATE = (
    "%5B%22%22%2C%7B%22children%22%3A%5B%22%28app%29%22%2C%7B%22children%22"
    "%3A%5B%22%28auth%29%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B"
    "%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%5D%7D%2Cnull"
    "%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Chromium";v="143", "Google Chrome";v="143", "Not A(Brand";v="24"'
CF_SESSION_CACHE_TTL_SECONDS = 25 * 60
CF_COOKIE_NAMES = {"cf_clearance", "__cf_bm", "xai_anon_id", "__cuid"}
DEFAULT_CF_CACHE_DIR = Path(__file__).resolve().parents[2] / "output" / "grok_cf_sessions"


def _cf_cache_key(proxy: str) -> str:
    identity = str(proxy or "direct").strip() or "direct"
    return hashlib.sha256(identity.encode("utf-8", errors="ignore")).hexdigest()[:24]


def _normalize_cache_dir(cache_dir: str | Path | None = None) -> Path:
    return Path(cache_dir).expanduser().resolve() if cache_dir else DEFAULT_CF_CACHE_DIR


def save_cf_session_cache(
    *,
    proxy: str,
    user_agent: str,
    cookies: dict[str, str],
    cache_dir: str | Path | None = None,
    ttl_seconds: int = CF_SESSION_CACHE_TTL_SECONDS,
) -> Path:
    selected_cookies = {
        str(name): str(value)
        for name, value in dict(cookies or {}).items()
        if str(name) in CF_COOKIE_NAMES and value is not None and str(value)
    }
    if not selected_cookies.get("cf_clearance"):
        raise ValueError("cf_clearance missing; skip Grok CF session cache")
    now = int(time.time())
    payload = {
        "schema": 1,
        "proxy": str(proxy or ""),
        "user_agent": str(user_agent or ""),
        "cookies": selected_cookies,
        "created_at": now,
        "expires_at": now + int(ttl_seconds),
    }
    root = _normalize_cache_dir(cache_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_cf_cache_key(proxy)}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_cf_session_cache(*, proxy: str, cache_dir: str | Path | None = None) -> dict[str, Any]:
    path = _normalize_cache_dir(cache_dir) / f"{_cf_cache_key(proxy)}.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if int(payload.get("expires_at") or 0) <= int(time.time()):
        return {}
    cookies = payload.get("cookies") or {}
    if not isinstance(cookies, dict) or not cookies.get("cf_clearance"):
        return {}
    return payload


def _pb_string(field: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    tag = (field << 3) | 2
    return _varint(tag) + _varint(len(encoded)) + encoded


def _pb_bool(field: int, value: bool) -> bytes:
    tag = (field << 3) | 0
    return _varint(tag) + _varint(1 if value else 0)


def _varint(n: int) -> bytes:
    buf = []
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            break
    return bytes(buf)


def _grpc_frame(body: bytes) -> bytes:
    return bytes([0]) + struct.pack(">I", len(body)) + body


def _rand_name(n: int = 6) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n)).capitalize()


def _rand_password(n: int = 12) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=n)) + ",,,aA1"


def _normalize_email_validation_code(code: str) -> str:
    """匹配前端 OTP 输入：只提交 6 位字母数字，分隔符仅用于展示。"""
    return "".join(re.findall(r"[A-Za-z0-9]", str(code or ""))).upper()[:6]


def _header(response: Any, name: str) -> str:
    headers = getattr(response, "headers", {}) or {}
    if hasattr(headers, "get"):
        value = headers.get(name) or headers.get(name.lower()) or headers.get(name.title())
        if value is not None:
            return str(value)
    lowered = name.lower()
    try:
        for key, value in dict(headers).items():
            if str(key).lower() == lowered:
                return str(value)
    except Exception:
        pass
    return ""


def _response_text(response: Any) -> str:
    text = getattr(response, "text", "")
    if text:
        return str(text)
    content = getattr(response, "content", b"") or b""
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="ignore")
    return str(content)


def _clip(value: Any, limit: int = 700) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit]


def _looks_like_cloudflare_text(text: str) -> bool:
    low = str(text or "").lower()
    return (
        "cloudflare" in low
        and (
            "just a moment" in low
            or "checking your browser" in low
            or "challenge-platform" in low
            or "cf-chl" in low
            or "安全验证" in text
        )
    )


def _looks_like_cloudflare_response(response: Any) -> bool:
    status = int(getattr(response, "status_code", 0) or getattr(response, "status", 0) or 0)
    content_type = _header(response, "content-type").lower()
    text = _response_text(response)
    return (
        status in {403, 429, 503}
        and ("html" in content_type or "text" in content_type or text.lstrip().startswith("<"))
        and _looks_like_cloudflare_text(text)
    )


def _cookie_dict_from_jar(jar: Any) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not jar:
        return cookies
    if isinstance(jar, dict):
        return {str(k): str(v) for k, v in jar.items() if k and v is not None}
    try:
        items = jar.items()
        return {str(k): str(v) for k, v in items if k and v is not None}
    except Exception:
        pass
    try:
        get_dict = getattr(jar, "get_dict", None)
        if callable(get_dict):
            return {str(k): str(v) for k, v in get_dict().items() if k and v is not None}
    except Exception:
        pass
    try:
        for cookie in jar:
            name = getattr(cookie, "name", "") or ""
            value = getattr(cookie, "value", "") or ""
            if name:
                cookies[str(name)] = str(value)
    except Exception:
        return cookies
    return cookies


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in dict(cookies or {}).items() if name and value)


def _decode_next_url_escapes(value: str) -> str:
    raw = str(value or "")
    try:
        return str(json.loads('"' + raw.replace('"', '\"') + '"'))
    except Exception:
        return (
            raw.replace("\\u0026", "&")
            .replace("\\u003d", "=")
            .replace("\\u003D", "=")
            .replace("\\u002f", "/")
            .replace("\\u002F", "/")
            .replace("\\/", "/")
        )


def extract_set_cookie_urls(signup_body: str) -> list[str]:
    """从 Next Server Action 响应里提取完整 set-cookie URL。

    Next 响应里查询参数常用 ``\u0026`` 转义，如果正则排除反斜杠会把
    URL 截断到第一个参数，auth 服务会返回 400，导致拿不到 sso cookie。
    """
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'https://auth\.[^"\s]+/set-cookie[^"\s]*', signup_body or ""):
        url = _decode_next_url_escapes(match.group(0))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    raw = parts[1]
    try:
        import base64

        data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        payload = json.loads(data.decode("utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def expand_set_cookie_redirect_chain(initial_url: str, *, max_depth: int = 8) -> list[str]:
    """展开 x.ai 注册返回的嵌套 set-cookie success_url 链。

    运行时响应会先给 ``auth.grokipedia.com`` 的外层 URL。该外层节点
    在协议请求下返回 400，但 JWT payload 里的 ``success_url`` 指向下一
    个真实 cookie 设置节点；浏览器也会沿这条链跳转。
    """
    urls: list[str] = []
    seen: set[str] = set()
    current = str(initial_url or "").strip()
    for _ in range(max(1, int(max_depth or 8))):
        if not current or current in seen:
            break
        seen.add(current)
        urls.append(current)
        parsed = urlparse(current)
        if not parsed.netloc or "/set-cookie" not in parsed.path:
            break
        token = parse_qs(parsed.query, keep_blank_values=True).get("q", [""])[0]
        payload = _decode_jwt_payload(token)
        config = payload.get("config") if isinstance(payload, dict) else {}
        next_url = str((config or {}).get("success_url") or "").strip()
        if not next_url:
            break
        current = next_url
    return urls


def _split_proxy_server(proxy_server: str) -> str:
    """从 Windows ProxyServer 里取 HTTPS/HTTP 代理地址。"""
    value = str(proxy_server or "").strip()
    if not value:
        return ""
    if ";" not in value and "=" not in value:
        return value
    parts: dict[str, str] = {}
    for item in value.split(";"):
        if "=" not in item:
            continue
        key, raw = item.split("=", 1)
        parts[key.strip().lower()] = raw.strip()
    return parts.get("https") or parts.get("http") or parts.get("socks") or ""


def detect_windows_user_proxy() -> str:
    """读取当前 Windows 用户的 WinINET 代理，供 curl_cffi 协议请求复用 Chrome 出口。"""
    if not sys.platform.startswith("win"):
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            proxy_enable = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
            if proxy_enable != 1:
                return ""
            proxy_server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except Exception:
        return ""
    endpoint = _split_proxy_server(proxy_server)
    if not endpoint:
        return ""
    if "://" not in endpoint:
        endpoint = f"http://{endpoint}"
    return endpoint.rstrip("/")


def _click_cloudflare_challenge(page: Any, *, log_fn: Callable[[str], None] = print) -> bool:
    """真实浏览器里尽量点击 Cloudflare 可见 challenge 区域。"""
    try:
        box = page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 80 && r.height > 80 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const nodes = [...document.querySelectorAll('[role=main], main, body > div, div')].filter(visible);
              nodes.sort((a, b) => (b.getBoundingClientRect().width * b.getBoundingClientRect().height) - (a.getBoundingClientRect().width * a.getBoundingClientRect().height));
              const el = nodes[0] || document.body;
              const r = el.getBoundingClientRect();
              return {x: Math.max(120, Math.min(innerWidth - 120, innerWidth / 2)), y: Math.max(240, Math.min(innerHeight - 60, r.y + r.height * 0.62))};
            }
            """
        ) or {}
        x = float(box.get("x") or 320)
        y = float(box.get("y") or 360)
        page.mouse.move(x, y)
        page.mouse.down()
        time.sleep(0.12)
        page.mouse.up()
        log_fn(f"[Grok] 已点击 Cloudflare 主页面验证区域: x={int(x)} y={int(y)}")
        return True
    except Exception:
        pass

    for frame in list(getattr(page, "frames", []) or []):
        frame_url = str(getattr(frame, "url", "") or "")
        if "cloudflare" not in frame_url and "turnstile" not in frame_url:
            continue
        for selector in ("input[type='checkbox']", "[role='checkbox']", ".ctp-checkbox-label", "label", "button"):
            try:
                loc = frame.locator(selector).first
                loc.wait_for(state="visible", timeout=1000)
                loc.click(timeout=2000, force=True)
                log_fn(f"[Grok] 已点击 Cloudflare 验证元素: {selector}")
                return True
            except Exception:
                pass
    return False


class GrokRegister:
    def __init__(
        self,
        captcha_solver: Any = None,
        yescaptcha_key: str = "",
        proxy: str | None = None,
        log_fn: Callable[[str], None] = print,
        *,
        use_cdp_bridge: bool = False,
        chrome_cdp_url: str = "",
        chrome_user_data_dir: str = "",
        castle_request_token_provider: Callable[[], str] | None = None,
        cf_cache_dir: str | Path | None = None,
        cf_cache_ttl_seconds: int = CF_SESSION_CACHE_TTL_SECONDS,
        use_cf_cache: bool = True,
    ) -> None:
        self.captcha_solver = captcha_solver
        self.key = yescaptcha_key
        resolved_proxy = proxy or detect_windows_user_proxy()
        self.proxy = resolved_proxy
        self.log = log_fn
        self.use_cdp_bridge = bool(use_cdp_bridge)
        self.chrome_cdp_url = str(chrome_cdp_url or "")
        self.chrome_user_data_dir = str(chrome_user_data_dir or "")
        self.castle_request_token_provider = castle_request_token_provider
        self.castle_request_token = ""
        self.cdp_bootstrap_result: dict[str, Any] = {}
        self._cdp_bootstrapped = False
        self._last_grpc_headers: dict[str, str] = {}
        self.cf_cache_dir = _normalize_cache_dir(cf_cache_dir)
        self.cf_cache_ttl_seconds = int(cf_cache_ttl_seconds or CF_SESSION_CACHE_TTL_SECONDS)
        self.use_cf_cache = bool(use_cf_cache)
        self._cf_cache_loaded = False
        self.s = cffi_requests.Session(impersonate="chrome131")
        if resolved_proxy:
            self.s.proxies = {"http": resolved_proxy, "https": resolved_proxy}
        self.s.headers.update({
            "user-agent": UA,
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "sec-ch-ua": SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })
        if self.use_cf_cache:
            self._load_cf_session_cache()

    def _load_cf_session_cache(self) -> bool:
        cached = load_cf_session_cache(proxy=self.proxy or "", cache_dir=self.cf_cache_dir)
        if not cached:
            return False
        cookies = dict(cached.get("cookies") or {})
        if not cookies.get("cf_clearance"):
            return False
        self.import_cookies(cookies)
        user_agent = str(cached.get("user_agent") or "").strip()
        if user_agent:
            self.s.headers.update({"user-agent": user_agent})
        self._cf_cache_loaded = True
        self.log("[Grok] 已加载 Cloudflare session 缓存")
        return True

    def _save_current_cf_session_cache(self) -> None:
        if not self.use_cf_cache:
            return
        cookies = self.cookies
        if not cookies.get("cf_clearance"):
            return
        save_cf_session_cache(
            proxy=self.proxy or "",
            user_agent=str(self.s.headers.get("user-agent") or UA),
            cookies=cookies,
            cache_dir=self.cf_cache_dir,
            ttl_seconds=self.cf_cache_ttl_seconds,
        )
        self.log("[Grok] 已保存 Cloudflare session 缓存")

    @property
    def cookies(self) -> dict[str, str]:
        return _cookie_dict_from_jar(getattr(self.s, "cookies", {}))

    @property
    def cookie_header(self) -> str:
        return _cookie_header(self.cookies)

    def import_cookies(self, cookies: dict[str, str]) -> None:
        jar = getattr(self.s, "cookies", None)
        for name, value in dict(cookies or {}).items():
            if not name or value is None:
                continue
            try:
                jar.set(str(name), str(value))
                jar.set(str(name), str(value), domain=".x.ai")
                jar.set(str(name), str(value), domain="accounts.x.ai")
                jar.set(str(name), str(value), domain="auth.x.ai")
                jar.set(str(name), str(value), domain="auth.grokipedia.com")
            except Exception:
                try:
                    jar[str(name)] = str(value)
                except Exception:
                    pass

    def _handle_challenge_and_retry(self, operation: Callable[[], Any], response: Any, *, label: str) -> Any:
        if not _looks_like_cloudflare_response(response):
            return response
        if not self.use_cdp_bridge:
            raise RuntimeError(
                f"Grok {label} 被 Cloudflare challenge 拦截；请使用 executor_type=cdp_protocol "
                "或配置 chrome_cdp_url/chrome_user_data_dir 后重试"
            )
        if not self._cdp_bootstrapped:
            self.bootstrap_cdp_challenge()
        retried = operation()
        if _looks_like_cloudflare_response(retried):
            raise RuntimeError(f"Grok {label} 通过 CDP 同步后仍被 Cloudflare challenge 拦截")
        return retried

    def _grpc_post(self, path: str, body: bytes) -> bytes:
        url = f"{ACCOUNTS_URL}{path}"

        def do_post() -> Any:
            return self.s.post(
                url,
                headers={
                    "Content-Type": "application/grpc-web+proto",
                    "Accept": "application/grpc-web+proto",
                    "X-Grpc-Web": "1",
                    "X-User-Agent": "connect-es/2.1.1",
                    "Origin": ACCOUNTS_URL,
                    "Referer": SIGNUP_URL,
                },
                data=_grpc_frame(body),
            )

        response = self._handle_challenge_and_retry(do_post, do_post(), label=f"RPC {path}")
        try:
            self._last_grpc_headers = {
                str(key).lower(): str(value)
                for key, value in (getattr(response, "headers", {}) or {}).items()
            }
        except Exception:
            self._last_grpc_headers = {}
        status = int(getattr(response, "status_code", 0) or 0)
        if status and status >= 400:
            raise RuntimeError(f"Grok RPC {path} HTTP {status}: {_clip(_response_text(response))}")
        return getattr(response, "content", b"") or b""

    def _ensure_grpc_ok(self, path: str, content: bytes) -> None:
        headers = getattr(self, "_last_grpc_headers", {}) or {}
        header_grpc_status = str(headers.get("grpc-status") or "").strip()
        if header_grpc_status == "0":
            return

        text = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content)
        lowered = text.lower()
        if b"grpc-status:0" in (content or b"") or "grpc-status: 0" in lowered or "grpc-status:0" in lowered:
            return

        if header_grpc_status:
            header_grpc_message = unquote(str(headers.get("grpc-message") or "")).strip()
            detail = header_grpc_message or _clip(text)
            raise RuntimeError(f"Grok RPC {path} grpc-status={header_grpc_status}: {detail}")

        body_status = re.search(r"grpc-status:\s*([^\r\n]+)", text, flags=re.IGNORECASE)
        if body_status:
            body_grpc_message = re.search(r"grpc-message:\s*([^\r\n]+)", text, flags=re.IGNORECASE)
            detail = unquote(body_grpc_message.group(1)).strip() if body_grpc_message else _clip(text)
            raise RuntimeError(f"Grok RPC {path} grpc-status={body_status.group(1).strip()}: {detail}")

        raise RuntimeError(f"Grok RPC {path} 未确认成功: {_clip(text)}")

    def _solve_turnstile(self) -> str:
        self.log("获取 Turnstile token...")
        solver = self.captcha_solver
        if not solver:
            from core.base_captcha import YesCaptcha

            solver = YesCaptcha(self.key)
        token = str(solver.solve_turnstile(SIGNUP_URL, TURNSTILE_SITEKEY) or "").strip()
        if not token:
            raise RuntimeError("Grok Turnstile token 为空")
        self.log(f"  Turnstile: {token[:40]}...")
        return token

    def _castle_token(self) -> str:
        if self.castle_request_token_provider:
            token = str(self.castle_request_token_provider() or "").strip()
            if token:
                self.castle_request_token = token
        return str(self.castle_request_token or "").strip()

    def bootstrap_cdp_challenge(self, timeout: int = 120) -> dict[str, Any]:
        """CDP 混合链路：只通过真实浏览器通过 CF/JSD，并同步 Cookie。"""
        from core.oauth_browser import OAuthBrowser

        self.log("[Grok] CDP bootstrap: 打开 sign-up 通过 Cloudflare challenge")
        with OAuthBrowser(
            proxy=self.proxy,
            headless=False,
            chrome_user_data_dir=self.chrome_user_data_dir,
            chrome_cdp_url=self.chrome_cdp_url,
            reuse_existing_cdp=bool(self.chrome_cdp_url),
            log_fn=self.log,
        ) as browser:
            page = browser.active_page() or browser.new_page()
            try:
                page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=90000)
            except Exception as exc:
                self.log(f"[Grok] sign-up 首次加载异常，继续等待页面状态: {exc!r}")
            deadline = time.time() + max(15, int(timeout or 120))
            last_body = ""
            click_attempts = 0
            while time.time() < deadline:
                try:
                    last_body = str(page.inner_text("body", timeout=3000) or "")
                except Exception:
                    last_body = ""
                cookies = browser.cookie_dict(domain_substrings=("x.ai",))
                if cookies.get("cf_clearance") and not _looks_like_cloudflare_text(last_body):
                    break
                if not _looks_like_cloudflare_text(last_body):
                    break
                if click_attempts < 4 and _click_cloudflare_challenge(page, log_fn=self.log):
                    click_attempts += 1
                    time.sleep(8)
                    continue
                time.sleep(2)

            cookies = browser.cookie_dict(domain_substrings=("x.ai",))
            self.import_cookies(cookies)
            try:
                ua = str(page.evaluate("() => navigator.userAgent") or "").strip()
                if ua:
                    self.s.headers.update({"user-agent": ua})
            except Exception:
                ua = ""
            try:
                castle_token = str(page.evaluate(
                    """
                    async () => {
                      try {
                        if (window.Castle && typeof window.Castle.createRequestToken === 'function') {
                          return await window.Castle.createRequestToken();
                        }
                        if (window._castle && typeof window._castle.createRequestToken === 'function') {
                          return await window._castle.createRequestToken();
                        }
                      } catch (e) {}
                      return '';
                    }
                    """
                ) or "").strip()
                if castle_token:
                    self.castle_request_token = castle_token
            except Exception:
                castle_token = ""

        self._cdp_bootstrapped = True
        self.cdp_bootstrap_result = {
            "ok": bool(cookies),
            "cookies": cookies,
            "cookie_header": _cookie_header(cookies),
            "user_agent": ua,
            "castle_request_token": bool(castle_token),
            "last_body": last_body[:500],
        }
        self._save_current_cf_session_cache()
        self.log(f"[Grok] CDP bootstrap cookies={list(cookies.keys())[:8]}")
        return self.cdp_bootstrap_result

    def step1_send_otp(self, email: str) -> None:
        self.log(f"Step1: 发送验证码到 {email}...")
        path = f"{RPC_BASE_PATH}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
        body = _pb_string(1, email)
        resp = self._grpc_post(path, body)
        self._ensure_grpc_ok(path, resp)
        self.log("  验证码已发送")

    def step2_verify_otp(self, email: str, code: str) -> bool:
        normalized_code = _normalize_email_validation_code(code)
        self.log(f"Step2: 验证码校验 {normalized_code}...")
        path = f"{RPC_BASE_PATH}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
        body = _pb_string(1, email) + _pb_string(2, normalized_code) + _pb_bool(3, False)
        resp = self._grpc_post(path, body)
        self._ensure_grpc_ok(path, resp)
        self.log("  校验: OK")
        return True

    def _post_signup_action(self, payload: list[dict[str, Any]]) -> Any:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        def do_post() -> Any:
            return self.s.post(
                SIGNUP_URL,
                headers={
                    "Content-Type": "text/plain;charset=UTF-8",
                    "Accept": "text/x-component",
                    "next-action": NEXT_ACTION,
                    "next-router-state-tree": RSC_SIGNUP_STATE,
                    "Origin": ACCOUNTS_URL,
                    "Referer": SIGNUP_URL,
                },
                data=body,
            )

        return self._handle_challenge_and_retry(do_post, do_post(), label="sign-up")

    def step3_signup(self, email: str, password: str, code: str, given_name: str, family_name: str) -> str:
        normalized_code = _normalize_email_validation_code(code)
        turnstile = self._solve_turnstile()
        self.log("Step3: 提交注册...")
        payload = [{
            "emailValidationCode": normalized_code,
            "createUserAndSessionRequest": {
                "email": email,
                "givenName": given_name,
                "familyName": family_name,
                "clearTextPassword": password,
                "tosAcceptedVersion": 1,
            },
            "turnstileToken": turnstile,
            "conversionId": str(uuid.uuid4()),
            "castleRequestToken": self._castle_token(),
        }]
        response = self._post_signup_action(payload)
        status = int(getattr(response, "status_code", 0) or 0)
        text = _response_text(response)
        self.log(f"  sign-up status={status}")
        if status and status >= 400:
            raise RuntimeError(f"Grok sign-up HTTP {status}: {_clip(text)}")
        if _looks_like_cloudflare_text(text):
            raise RuntimeError("Grok sign-up 返回 Cloudflare challenge HTML，注册未完成")
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                data = json.loads(stripped)
                if isinstance(data, dict) and data.get("error"):
                    raise RuntimeError(f"Grok sign-up error: {data.get('error')}")
            except json.JSONDecodeError:
                pass
        return text

    def step4_set_cookies(self, signup_body: str) -> None:
        self.log("Step4: 设置 session cookies...")
        entry_urls = extract_set_cookie_urls(signup_body)
        if not entry_urls and self.cookies.get("sso"):
            return

        urls: list[str] = []
        seen: set[str] = set()
        for entry_url in entry_urls:
            for url in expand_set_cookie_redirect_chain(entry_url):
                if url not in seen:
                    seen.add(url)
                    urls.append(url)

        last_status = 0
        last_cookie_names: list[str] = []
        for index, url in enumerate(urls):
            parsed = urlparse(url)
            if parsed.path == "/set-cookie":
                self.log(f"  {parsed.netloc}{parsed.path}...")
            response = self.s.get(
                url,
                headers={
                    "user-agent": str(self.s.headers.get("user-agent") or UA),
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "referer": ACCOUNTS_URL + "/",
                },
                allow_redirects=True,
            )
            last_status = int(getattr(response, "status_code", 0) or 0)
            last_cookie_names = sorted(self.cookies.keys())
            if _looks_like_cloudflare_response(response):
                raise RuntimeError("Grok set-cookie 被 Cloudflare challenge 拦截，session cookies 未设置")
            if self.cookies.get("sso"):
                return
            if last_status and last_status >= 400 and index + 1 >= len(urls):
                break

        if urls and not self.cookies.get("sso"):
            raise RuntimeError(
                f"Grok set-cookie 未下发 sso: last_status={last_status}, cookies={last_cookie_names}"
            )
