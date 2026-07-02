# -*- coding: utf-8 -*-
"""ruyiPage 通用浏览器封装（项目级反检测浏览器插件）。

把 scripts/test_vercel_register_ruyipage.py 里验证过的 ruyiPage 启动/代理/指纹
逻辑提炼成通用类，供任意平台按需调用。ruyiPage 走 Firefox + WebDriver BiDi，
无 CDP 暴露面，适合打 Kasada/Cloudflare/hCaptcha 这类针对 CDP 的检测。

**不实现 OAuthBrowser 接口**：ruyiPage 的元素/动作 API（page.ele/input/actions）
跟 Playwright 不兼容，不能当 OAuthBrowser 的第四后端。这里是独立封装，调用方
用 ruyiPage 原生 API 操作 self.page（FirefoxPage 对象）。

核心能力（都已实测）：
  - 自动探测 `python -m ruyipage install` 装的定制 Firefox 指纹内核
    （FirefoxOptions 默认指向系统 Firefox，必须显式 set_browser_path 指定制内核，
     否则丢了 set_fpfile/smart_fingerprint/ruyi:true 反检测能力）
  - resin 代理自动走 SOCKS5：resin 网关同时支持 HTTP/SOCKS5，但 ruyiPage 定制
    Firefox 对带密码 HTTP 代理认证链路坏（导航停 about:home），SOCKS5 走
    socksauth.* fpfile 路径认证正常
  - smart_fingerprint 一键指纹：探测出口 IP → 匹配语言/时区 → 抽 22 套真机硬件
    → 拼 UA + canvas 种子 → 写 fpfile → 配 proxy/user_dir
  - 导航 wait="none" + 轮询：Vercel/Cloudflare 这类有重定向的站，BiDi navigate
    命令默认 30s 超时，wait="none" 发起即返回 + 自己轮询 url 到目标域

接入示例（3 行起浏览器 + 指纹 + 代理）::

    from core.ruyipage_browser import RuyiPageBrowser
    with RuyiPageBrowser(proxy=resin_url, headless=True) as rp:
        page = rp.page               # ruyiPage FirefoxPage，用原生 API 操作
        rp.navigate("https://vercel.com/signup")
        page.ele("css:#email").input("a@b.com")
        cookies = rp.cookie_dict()   # 取 cookie 供后续协议调用

依赖：pip install ruyiPage[async] --upgrade && python -m ruyipage install
（未装时本模块 import 不崩，RuyiPageBrowser 启动时给安装指引）
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional, Callable
from urllib.parse import urlparse

# ruyiPage 未装时给友好提示，别让 import 报错把依赖它的平台搞崩。
try:
    from ruyipage import FirefoxOptions, FirefoxPage, Keys  # noqa: F401
    _RUYIPAGE_OK = True
    _RUYIPAGE_ERR = ""
except Exception as _e:  # pragma: no cover - 环境未就绪
    _RUYIPAGE_OK = False
    _RUYIPAGE_ERR = repr(_e)


def is_ruyipage_available() -> bool:
    """ruyiPage 是否已安装可用（供平台层判断是否走 ruyiPage 路径）。"""
    return _RUYIPAGE_OK


def detect_firefox() -> str:
    """探测 `python -m ruyipage install` 装的定制 Firefox 指纹内核路径。

    ruyiPage 的 FirefoxOptions 默认指向系统 Firefox（C:\\Program Files\\Mozilla Firefox），
    不会自动用定制内核——set_fpfile/smart_fingerprint/ruyi:true 都依赖定制内核，
    用系统 Firefox 等于丢了反检测能力。这里按官方安装路径规律探测，找不到返回 ""。
    """
    home = Path.home()
    base = home / "AppData" / "Local" / "ruyipage" / "browsers"
    if not base.exists():
        return ""
    # 目录形如 firefox-151.0a1-151-ruyi-win64/firefox/firefox.exe
    for ff in sorted(base.glob("firefox-*/firefox/firefox.exe"), reverse=True):
        if ff.exists():
            return str(ff)
    return ""


class RuyiPageBrowser:
    """ruyiPage 反检测浏览器封装（contextmanager，跟 OAuthBrowser 用法类似）。

    用 ``with RuyiPageBrowser(...) as rp:`` 启动，``rp.page`` 是 ruyiPage 原生
    FirefoxPage，用 ``page.ele/input/actions/run_js`` 等 ruyiPage API 操作。
    """

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        headless: bool = False,
        browser_path: str = "",
        use_smart_fingerprint: bool = True,
        require_country: Optional[str] = None,
        log_fn: Callable[[str], None] = print,
    ):
        """
        Args:
            proxy: 代理 URL。resin 代理（host=20.193.157.62 或 user 含 "Default."）
                自动转 SOCKS5 绕开 ruyiPage HTTP 代理认证 bug；其他代理按原 scheme。
            headless: 是否无头。反检测场景有头人机分更稳，但调试/批量可无头。
            browser_path: 定制 Firefox 内核路径。留空自动探测（detect_firefox）。
            use_smart_fingerprint: True 时一键配指纹（出口IP/语言/时区/硬件/canvas）。
                False 时只 set_proxy 起浏览器，反检测能力受限。
            require_country: smart_fingerprint 出口 IP 国家校验（None=不校验）。
            log_fn: 日志函数。
        """
        self.proxy = proxy
        self.headless = headless
        self.browser_path = browser_path
        self.use_smart_fingerprint = use_smart_fingerprint
        self.require_country = require_country
        self.log = log_fn
        self.page = None          # ruyiPage FirefoxPage（__enter__ 后可用）
        self._fp_ctx = None       # smart_fingerprint 返回的 FingerprintContext
        self._resolved_path = ""  # 实际用的 Firefox 内核路径

    def __enter__(self):
        if not _RUYIPAGE_OK:
            raise RuntimeError(
                f"ruyiPage 未安装或导入失败: {_RUYIPAGE_ERR}\n"
                "请先: pip install ruyiPage[async] --upgrade && python -m ruyipage install"
            )
        opts = FirefoxOptions()
        self._resolved_path = self.browser_path or detect_firefox()
        if self._resolved_path:
            opts.set_browser_path(self._resolved_path)
            self.log(f"[ruyipage] Firefox 内核: {self._resolved_path}")
        else:
            self.log("[ruyipage] [WARN] 未探测到定制 Firefox 内核，回退默认（反检测能力受限）")
        opts.headless(self.headless)

        if self.use_smart_fingerprint and self.proxy:
            self._fp_ctx = self._apply_smart_fingerprint(opts)
        elif self.proxy:
            opts.set_proxy(self.proxy)

        self.page = FirefoxPage(opts)
        if self._fp_ctx is not None:
            try:
                self._fp_ctx.apply_emulation(self.page, logger=lambda m: self.log(f"[ruyipage][emu] {m}"))
            except Exception as exc:
                self.log(f"[ruyipage] apply_emulation 异常(非致命): {str(exc)[:80]!r}")
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.page is not None:
            try:
                self.page.quit()
            except Exception:
                pass
            self.page = None

    def _apply_smart_fingerprint(self, opts):
        """smart_fingerprint 一键配指纹 + resin 代理自动转 SOCKS5。"""
        p = urlparse(self.proxy)
        # resin 网关同时支持 HTTP/SOCKS5，但 ruyiPage 对带密码 HTTP 代理认证坏，
        # SOCKS5 走 socksauth.* fpfile 路径认证正常。resin 代理强制 SOCKS5。
        is_resin = (p.hostname == "20.193.157.62") or ("Default." in (p.username or ""))
        proxy_scheme = "socks5" if is_resin else (p.scheme or "http")
        if is_resin:
            self.log("[ruyipage] 检测到 resin 代理，走 SOCKS5 入口绕开 HTTP 认证 bug")
        try:
            ctx = opts.smart_fingerprint(
                proxy_host=p.hostname or "",
                proxy_port=p.port or 0,
                proxy_user=p.username or None,
                proxy_pwd=p.password or None,
                proxy_scheme=proxy_scheme,
                require_country=self.require_country,
                logger=lambda m: self.log(f"[ruyipage][fp] {m}"),
            )
            self.log(f"[ruyipage] smart_fingerprint: {ctx.summary()}")
            return ctx
        except Exception as exc:
            self.log(f"[ruyipage] smart_fingerprint 失败回退 set_proxy: {str(exc)[:80]!r}")
            opts.set_proxy(self.proxy)
            return None

    # ---------------------------------------------------------- 导航辅助
    def navigate(self, url: str, *, timeout: int = 10, wait_url_contains: str = "",
                 max_wait: int = 50) -> bool:
        """导航到 url，wait="none" 发起即返回 + 轮询验证 url 到目标域。

        Vercel/Cloudflare 这类有 Kasada 脚本重定向的站，BiDi navigate 命令默认 30s
        超时（wait="interactive" 会卡），用 wait="none" + timeout 快速发起，再轮询
        url 确认真到目标域，避免停在 about:home 误判。

        Args:
            url: 目标 URL。
            timeout: page.get 的命令超时（秒），快速发起/快速失败。
            wait_url_contains: 轮询验证 url 含此子串才算到（如 "vercel.com"）。
                留空则从 url 提取 host 作验证条件。
            max_wait: 轮询 url 最长等待（秒）。
        Returns:
            True 已到目标域，False 超时未到。
        """
        if self.page is None:
            raise RuntimeError("RuyiPageBrowser 未初始化（未在 with 块内）")
        verify = wait_url_contains or (urlparse(url).hostname or "")
        try:
            self.page.get(url, wait="none", timeout=timeout)
        except Exception as exc:
            self.log(f"[ruyipage] navigate 发起: {str(exc)[:100]!r}（容忍，轮询验证）")
        # 轮询等 url 到目标域
        for _ in range(max(1, max_wait // 2)):
            self.page.wait(2)
            try:
                cur = self.page.url or ""
            except Exception:
                cur = ""
            if verify and verify.lower() in cur.lower():
                return True
            if not verify and cur and not cur.startswith("about:"):
                return True
        self.log(f"[ruyipage] navigate 超时未到 {verify!r}，停在 {(self.page.url or '')[:60]!r}")
        return False

    # ---------------------------------------------------------- 元素输入辅助
    def input_text(self, ele, text: str) -> bool:
        """填文本到 ruyiPage 元素，防 React 受控 input 清空。

        主路径 ele.input()（BiDi 原生 input，isTrusted=true）；
        失败兜底 actions.human_type（拟人化逐字）；
        再失败用 ruyi:true InputEvent（让 isTrusted 更贴近真人，React onChange 能捕获）。
        """
        page = self.page
        try:
            ele.clear()
        except Exception:
            pass
        try:
            ele.input(text)
            page.wait(0.3)
            if (ele.value or "") == text:
                return True
        except Exception as exc:
            self.log(f"[ruyipage] ele.input 异常: {str(exc)[:80]!r}")
        try:
            ele.click_self()
            page.actions.click(ele).human_type(text).perform()
            page.wait(0.3)
            if (ele.value or "") == text:
                return True
        except Exception as exc:
            self.log(f"[ruyipage] human_type 兜底异常: {str(exc)[:80]!r}")
        try:
            page.run_js(
                "(args) => {const [sel, val] = args;"
                "const el = document.querySelector(sel); if (!el) return false;"
                "const setter = Object.getOwnPropertyDescriptor("
                "window.HTMLInputElement.prototype, 'value').set;"
                "setter.call(el, val);"
                "el.dispatchEvent(new InputEvent('input',"
                "{bubbles: true, data: val, inputType: 'insertText', ruyi: true}));"
                "el.dispatchEvent(new Event('change', {bubbles: true, ruyi: true}));"
                "return el.value === val;}",
                [getattr(ele, "_selector", None), text],
                as_expr=False,
            )
            page.wait(0.3)
            return (ele.value or "") == text
        except Exception as exc:
            self.log(f"[ruyipage] ruyi:true 兜底异常: {str(exc)[:80]!r}")
            return False

    def has_visible(self, css_selector: str, *, timeout: float = 0.5) -> bool:
        """是否有**可见可交互**的元素（排除 display:none/隐藏预渲染）。

        比 page.ele 更严：Vercel/React 站常预渲染隐藏 input，page.ele 会误判。
        用 run_js 校验 offsetParent/offsetWidth/offsetHeight/visibility。
        """
        if self.page is None:
            return False
        try:
            visible = self.page.run_js(
                "return Array.from(document.querySelectorAll("
                f"\"{css_selector}\"))"
                ".some(e => e.offsetParent !== null && e.offsetWidth > 0 && e.offsetHeight > 0 "
                "&& getComputedStyle(e).visibility !== 'hidden' && !e.disabled)"
            )
            return bool(visible)
        except Exception:
            try:
                return self.page.ele(f"css:{css_selector}", timeout=timeout) is not None
            except Exception:
                return False

    # ---------------------------------------------------------- cookie / 网络
    def cookie_dict(self, *, domain_substrings: tuple = ()) -> dict:
        """取 cookie 字典 {name: value}，可按 domain 过滤。供后续协议调用复用登录态。"""
        if self.page is None:
            return {}
        try:
            cookies = self.page.cookies() or []
        except Exception:
            try:
                cookies = self.page.get_cookies() or []
            except Exception:
                return {}
        out = {}
        for c in cookies:
            c = dict(c) if not isinstance(c, dict) else c
            domain = c.get("domain") or ""
            if domain_substrings and not any(d in domain for d in domain_substrings):
                continue
            out[c.get("name", "")] = c.get("value", "")
        return out

    def watch_requests(self, url_contains: str, *, on_request: Optional[Callable] = None,
                       on_response: Optional[Callable] = None) -> dict:
        """挂 /url_contains/ 请求监听（两阶段 intercept），返回 meta dict 供调用方读。

        beforeRequestSent 阶段调 on_request(req)（可读 req.headers/req.body），
        responseStarted 阶段调 on_response(req)（可读 req.response_status）。
        调用方传 on_request/on_response 把要抓的字段写进自己持有的 meta。
        返回的 meta 含 intercept 启动状态，方便诊断。

        示例（抓 Kasada x-is-human v 值 + appeals status）::

            meta = {"v": None, "status": None}
            rp.watch_requests(
                "/api/appeals",
                on_request=lambda r: meta.update(v=json.loads(r.headers.get("x-is-human","{}")).get("v")),
                on_response=lambda r: meta.update(status=r.response_status),
            )
            # ... 触发请求 ...
            print(meta["v"], meta["status"])
        """
        if self.page is None:
            return {"ok": False, "error": "未初始化"}
        meta = {"ok": False, "error": None}

        def _handler(req):
            try:
                url = req.url or ""
                if url_contains not in url:
                    try:
                        if getattr(req, "is_response_phase", False):
                            req.continue_response()
                        else:
                            req.continue_request()
                    except Exception:
                        pass
                    return
                if getattr(req, "is_response_phase", False):
                    if on_response is not None:
                        try:
                            on_response(req)
                        except Exception as exc:
                            self.log(f"[ruyipage] on_response 异常: {str(exc)[:60]!r}")
                    try:
                        req.continue_response()
                    except Exception:
                        pass
                    return
                if on_request is not None:
                    try:
                        on_request(req)
                    except Exception as exc:
                        self.log(f"[ruyipage] on_request 异常: {str(exc)[:60]!r}")
                try:
                    req.continue_request()
                except Exception:
                    pass
            except Exception as exc:
                self.log(f"[ruyipage] watch handler 异常: {str(exc)[:60]!r}")
                try:
                    req.continue_request()
                except Exception:
                    pass

        try:
            self.page.intercept.start(
                _handler, phases=["beforeRequestSent", "responseStarted"], collect_response=True
            )
            meta["ok"] = True
            self.log(f"[ruyipage] intercept 已挂载（watch {url_contains!r}）")
        except Exception:
            try:
                self.page.intercept.start_requests(_handler, collect_response=True)
                meta["ok"] = True
                self.log(f"[ruyipage] intercept start_requests 已挂载（watch {url_contains!r}）")
            except Exception as exc:
                meta["error"] = str(exc)[:80]
                self.log(f"[ruyipage] intercept 启动失败: {str(exc)[:80]!r}")
        return meta
