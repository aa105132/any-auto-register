"""Outlook/Hotmail 浏览器注册 worker。

注册流程（参考公开实现 base_controller.outlook_register）：
  1. 打开 https://outlook.live.com/mail/0/?prompt=create_account
  2. 点"同意并继续"
  3. 选 @outlook.com / @hotmail.com 后缀，填邮箱本地名 → primaryButton
  4. 填密码 → primaryButton
  5. 填出生年月日 → primaryButton
  6. 填姓/名 → primaryButton → 等防机器人链接 detached
  7. 检查 IP 频率限制文案 / FunCaptcha 类型（enforcementFrame）
  8. Arkose 长按压验证（ArkoseLongPressSolver，协议级 proof 合成 + 纯短按降级）
  9. 等 [aria-label="新邮件"] 确认注册成功
  10. 复用已登录 page 走 Microsoft OAuth2 PKCE 拿 refresh_token
  11. 双写：mailbox_inventory(outlook_token) + outlook_accounts_pool.json
"""
from __future__ import annotations

import random
import secrets
import string
import time
from typing import Any, Callable

from platforms.outlook.constants import (
    ACCOUNT_BLOCKED_TEXTS,
    ARKOSE_RETRY_TEXTS,
    ARKOSE_SUCCESS_TEXTS,
    DEFAULT_BOT_PROTECTION_WAIT_SECONDS,
    DEFAULT_EMAIL_SUFFIX,
    DEFAULT_MAX_CAPTCHA_RETRIES,
    DEFAULT_OAUTH_TIMEOUT,
    DEFAULT_REGISTER_TIMEOUT,
    EXTRA_BOT_PROTECTION_WAIT,
    EXTRA_EMAIL_SUFFIX,
    EXTRA_MAX_CAPTCHA_RETRIES,
    EXTRA_OAUTH_TIMEOUT,
    EXTRA_REGISTER_TIMEOUT,
    EXTRA_USE_CAMOUFOX,
    EXTRA_USE_PROTOCOL_PROOF,
    OUTLOOK_SIGNUP_URL,
    RATE_LIMIT_TEXTS,
    SEL_AGREE_CONTINUE_TEXTS,
    SEL_ARKOSE_DRAW,
    SEL_ARKOSE_FIRST_PRESS,
    SEL_ARKOSE_FIRST_PRESS_EN,
    SEL_ARKOSE_INNER_IFRAME,
    SEL_ARKOSE_LOADING_STATUS,
    SEL_ARKOSE_LOADING_STATUS_EN,
    SEL_ARKOSE_OUTER_IFRAME,
    SEL_ARKOSE_OUTER_IFRAME_EN,
    SEL_ARKOSE_SECOND_PRESS,
    SEL_ARKOSE_SECOND_PRESS_EN,
    SEL_BIRTH_DAY,
    SEL_BIRTH_MONTH,
    SEL_BIRTH_YEAR,
    SEL_BOT_PROTECTION_LINK,
    SEL_DAY_OPTION_TEMPLATE_CN,
    SEL_DAY_OPTION_TEMPLATE_EN,
    SEL_EMAIL_INPUT,
    SEL_EMAIL_INPUT_EN,
    SEL_EMAIL_INPUT_FALLBACK,
    SEL_ENFORCEMENT_FRAME,
    SEL_FIRST_NAME,
    SEL_HOTMAIL_SUFFIX_OPTION,
    SEL_LAST_NAME,
    SEL_MONTH_NAMES_EN,
    SEL_MONTH_OPTION_TEMPLATE_CN,
    SEL_NEW_MAIL_BUTTON,
    SEL_NEW_MAIL_BUTTON_EN,
    SEL_OUTLOOK_SUFFIX_TEXT,
    SEL_PASSWORD_INPUT,
    SEL_PRIMARY_BUTTON,
    SUPPORTED_EMAIL_SUFFIXES,
)


def random_email_local(length: int = 0) -> str:
    """生成邮箱本地名：首字符小写字母，其余 7% 概率数字、93% 小写字母。"""
    n = int(length) if length and length >= 8 else random.randint(12, 14)
    first = random.choice(string.ascii_lowercase)
    chars = []
    for _ in range(n - 1):
        if random.random() < 0.07:
            chars.append(random.choice(string.digits))
        else:
            chars.append(random.choice(string.ascii_lowercase))
    return first + "".join(chars)


def generate_strong_password(length: int = 0) -> str:
    """生成强密码：保证大小写字母、数字、符号各至少一个。"""
    n = int(length) if length and length >= 8 else random.randint(11, 15)
    symbols = "!@#$%^&*"
    pool = string.ascii_letters + string.digits + symbols
    while True:
        pwd = "".join(secrets.choice(pool) for _ in range(n))
        if (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(c in symbols for c in pwd)):
            return pwd


def _random_first_name() -> str:
    return random.choice([
        "Aaron", "Brian", "Chloe", "Diane", "Ethan", "Grace", "Hannah", "Ian",
        "Julia", "Kevin", "Laura", "Mason", "Nora", "Oscar", "Paula", "Quinn",
    ])


def _random_last_name() -> str:
    return random.choice([
        "Mitchell", "Parker", "Reed", "Sawyer", "Turner", "Walsh", "Hayes",
        "Bennett", "Carter", "Davis", "Ellis", "Foster", "Graham", "Henderson",
    ])


def _random_birthdate() -> tuple[str, str, str]:
    """返回 (year, month, day)，月份和日期不带前导零（select option value 用）。"""
    year = str(random.randint(1960, 2005))
    month = str(random.randint(1, 12))
    day = str(random.randint(1, 28))
    return year, month, day


class OutlookBrowserRegister:
    """Outlook/Hotmail 注册 worker：浏览器自动化 + Arkose 验证 + OAuth 拿 token + 双写池。"""

    def __init__(
        self,
        *,
        headless: bool = True,
        proxy: str | None = None,
        email_suffix: str = DEFAULT_EMAIL_SUFFIX,
        bot_protection_wait: int = DEFAULT_BOT_PROTECTION_WAIT_SECONDS,
        max_captcha_retries: int = DEFAULT_MAX_CAPTCHA_RETRIES,
        use_camoufox: bool = False,
        use_protocol_proof: bool = True,
        register_timeout: int = DEFAULT_REGISTER_TIMEOUT,
        oauth_timeout: int = DEFAULT_OAUTH_TIMEOUT,
        extra: dict | None = None,
        log_fn: Callable[[str], None] = print,
    ):
        self.headless = bool(headless)
        self.proxy = proxy
        self.email_suffix = str(email_suffix or DEFAULT_EMAIL_SUFFIX).strip().lower()
        if self.email_suffix not in SUPPORTED_EMAIL_SUFFIXES:
            self.email_suffix = DEFAULT_EMAIL_SUFFIX
        self.bot_protection_wait = max(0, int(bot_protection_wait))
        self.max_captcha_retries = max(0, int(max_captcha_retries))
        self.use_camoufox = bool(use_camoufox)
        self.use_protocol_proof = bool(use_protocol_proof)
        self.register_timeout = max(30, int(register_timeout))
        self.oauth_timeout = max(30, int(oauth_timeout))
        self.extra = dict(extra or {})
        self.log = log_fn
        self._cancel_token = None

    def set_cancel_token(self, token) -> None:
        self._cancel_token = token

    def _poll_cancel(self) -> None:
        from core.cancel_token import check_cancel
        check_cancel(self._cancel_token)

    # ---- 浏览器启动 ----
    def _launch_browser(self):
        """启动 patchright/camoufox/playwright 浏览器，返回 (playwright_ctx, browser, context, owns_camoufox)。"""
        from core.proxy_utils import build_playwright_proxy_settings
        proxy_cfg = build_playwright_proxy_settings(self.proxy) if self.proxy else None

        if self.use_camoufox:
            from camoufox.sync_api import Camoufox
            # geoip=True 时 camoufox 用代理查公网 IP，resin 代理连 IP 服务经常超时。
            # 用 probe_ip 先查 IP（超时 12s），查到就传字符串给 geoip，查不到就跳过 geoip。
            geoip_val: Any = False  # 默认不查（False 不影响代理，只是不设 timezone/locale）
            if proxy_cfg:
                try:
                    import requests as _req
                    proxy_url = f"http://{proxy_cfg.get('username','')}:{proxy_cfg.get('password','')}@{proxy_cfg['server'].split('://')[1]}"
                    # 用 http（不是 https）查 IP，resin 代理对 http 更稳定
                    ip_resp = _req.get("http://api.ipify.org",
                                       proxies={"http": proxy_url, "https": proxy_url},
                                       timeout=12, verify=False, allow_redirects=True)
                    if ip_resp.status_code == 200 and ip_resp.text.strip():
                        geoip_val = ip_resp.text.strip()
                        self.log(f"[outlook] 代理 IP: {geoip_val}")
                except Exception as exc:
                    self.log(f"[outlook] 查代理 IP 失败，用 geoip=False: {repr(exc)[:80]}")
                    geoip_val = False
            launch_opts: dict[str, Any] = {"headless": self.headless, "humanize": True, "geoip": geoip_val}
            if proxy_cfg:
                launch_opts["proxy"] = proxy_cfg
            self.log("[outlook] 启动 Camoufox（反检测 Firefox）...")
            camoufox = Camoufox(**launch_opts)
            browser = camoufox.__enter__()
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            return camoufox, browser, ctx, True

        try:
            from patchright.sync_api import sync_playwright
            engine = "patchright"
        except ImportError:
            from playwright.sync_api import sync_playwright
            engine = "playwright"
        self.log(f"[outlook] 启动 {engine} Chromium...")
        pw = sync_playwright().start()
        launch_opts = {
            "headless": self.headless,
            # 不加 --disable-blink-features=AutomationControlled：patchright 自己处理 stealth，
            # 加了反而暴露自动化（PerimeterX 会检测这个 flag）
            "args": ["--lang=zh-CN"],
        }
        if proxy_cfg:
            launch_opts["proxy"] = proxy_cfg
        browser = pw.chromium.launch(**launch_opts)
        ctx = browser.new_context(viewport={"width": 1366, "height": 800}, locale="zh-CN")
        return pw, browser, ctx, False

    @staticmethod
    def _close_browser(pw_or_camoufox, ctx, owns_camoufox: bool) -> None:
        try:
            if ctx is not None:
                ctx.close()
        except Exception:
            pass
        if owns_camoufox:
            try:
                pw_or_camoufox.__exit__(None, None, None)
            except Exception:
                pass
            # camoufox 用 asyncio，退出后强制清理 loop，防止下一次创建时
            # "Playwright Sync API inside the asyncio loop" 冲突
            try:
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop and not loop.is_closed():
                        loop.stop()
                        loop.close()
                except RuntimeError:
                    pass  # loop already closed
                # 重置 event loop，让下一次创建新 loop
                asyncio.set_event_loop(asyncio.new_event_loop())
            except Exception:
                pass
        else:
            try:
                pw_or_camoufox.stop()
            except Exception:
                pass

    # ---- 注册表单步骤 ----
    @staticmethod
    def _fill_first_multilang(page, selectors: list[str], value: str, *, type_mode: bool = False, delay: int = 60) -> bool:
        """逐个 selector 试，第一个可见的就填。type_mode=True 时用 type（逐字），否则 fill。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() == 0:
                    continue
                if type_mode:
                    loc.type(value, delay=delay, timeout=10000)
                else:
                    loc.fill(value, timeout=10000)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _click_first_multilang(page, selectors: list[str], *, by_text: list[str] | None = None) -> bool:
        """逐个 selector 试点击；by_text 文本按钮也逐个试。"""
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.click(timeout=8000)
                    return True
            except Exception:
                continue
        for txt in (by_text or []):
            try:
                loc = page.get_by_text(txt).first
                if loc.count() > 0:
                    loc.click(timeout=8000)
                    return True
            except Exception:
                continue
        return False

    def _open_signup_page(self, page) -> bool:
        try:
            # resin IP 较慢，用 60s 超时 + domcontentloaded（不用 networkidle，Outlook 页有持续请求）
            page.goto(OUTLOOK_SIGNUP_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as exc:
            self.log(f"[outlook] 打开注册页失败: {repr(exc)[:120]}")
            return False
        # 同意并继续（多语言文本）。resin IP 慢，给足等待时间。
        try:
            agreed = False
            self._flow_start_time = time.time()
            # 先等 body 有实质内容（React 渲染完成），最多 20s
            for _ in range(40):
                try:
                    body = page.inner_text("body", timeout=1000)
                    if body and len(body.strip()) > 10:
                        break
                except Exception:
                    pass
                page.wait_for_timeout(500)
            for txt in SEL_AGREE_CONTINUE_TEXTS:
                try:
                    page.get_by_text(txt).wait_for(timeout=8000)
                    if self.bot_protection_wait:
                        page.wait_for_timeout(int(self.bot_protection_wait * 100))
                    page.get_by_text(txt).click(timeout=10000)
                    self.log(f"[outlook] 已点击同意并继续: {txt}（耗时 {time.time() - self._flow_start_time:.1f}s）")
                    agreed = True
                    break
                except Exception:
                    continue
            if not agreed:
                # 某些 region/IP 会跳过同意页直接到邮箱表单，检查邮箱输入框是否已出现
                try:
                    for sel in (SEL_EMAIL_INPUT, SEL_EMAIL_INPUT_EN, SEL_EMAIL_INPUT_FALLBACK):
                        if page.locator(sel).count() > 0:
                            self.log("[outlook] 同意页跳过，邮箱表单已直接出现")
                            agreed = True
                            break
                except Exception:
                    pass
            if not agreed:
                self.log("[outlook] 同意并继续按钮未出现且邮箱表单未出现（IP 质量不佳或页面未渲染）")
            else:
                # 同意后页面会跳转到 signup.live.com 并渲染邮箱表单，需要等加载
                self.log("[outlook] 同意后等待邮箱表单加载...")
                email_form_ready = False
                for _ in range(30):
                    try:
                        for sel in (SEL_EMAIL_INPUT, SEL_EMAIL_INPUT_EN, SEL_EMAIL_INPUT_FALLBACK):
                            if page.locator(sel).count() > 0:
                                self.log("[outlook] 邮箱表单已出现")
                                email_form_ready = True
                                break
                    except Exception:
                        pass
                    if email_form_ready:
                        break
                    page.wait_for_timeout(1000)
        except Exception:
            self.log("[outlook] 同意并继续处理异常")
        return True

    def _fill_email_and_password(self, page, email_local: str, password: str) -> bool:
        try:
            # 切换到 @hotmail.com（若要求）
            if self.email_suffix == "@hotmail.com":
                try:
                    page.get_by_text(SEL_OUTLOOK_SUFFIX_TEXT).click(timeout=10000)
                    page.locator(SEL_HOTMAIL_SUFFIX_OPTION).click(timeout=10000)
                except Exception:
                    pass
            # 邮箱输入框：中文 aria-label / 英文 aria-label / 通用 input[type=email][name=email]
            email_ok = self._fill_first_multilang(
                page,
                [SEL_EMAIL_INPUT, SEL_EMAIL_INPUT_EN, SEL_EMAIL_INPUT_FALLBACK],
                email_local, type_mode=True, delay=60,
            )
            if not email_ok:
                self.log("[outlook] 邮箱输入框未找到（中文/英文/通用都失败）")
                return False
            self._click_first_multilang(page, [SEL_PRIMARY_BUTTON])
            page.wait_for_timeout(300)
            # 密码页：等 [type=password] 可见（邮箱提交后页面切换有延迟）
            pwd_loc = page.locator(SEL_PASSWORD_INPUT).first
            try:
                pwd_loc.wait_for(state="visible", timeout=15000)
            except Exception:
                self.log("[outlook] 密码页 [type=password] 未出现（可能被拦或邮箱已存在）")
                return False
            # 用 fill 直接赋值（type 在某些 React 控件下会丢字），失败再回退 type
            pwd_filled = False
            try:
                pwd_loc.fill(password, timeout=10000)
                # 校验确实填进去了
                val = pwd_loc.input_value(timeout=3000)
                if val == password:
                    pwd_filled = True
                else:
                    self.log(f"[outlook] fill 后密码值不匹配（len={len(val)}），回退 type")
            except Exception as exc:
                self.log(f"[outlook] fill 密码失败: {repr(exc)[:100]}，回退 type")
            if not pwd_filled:
                try:
                    pwd_loc.click(timeout=5000)
                    pwd_loc.press_sequentially(password, delay=40, timeout=10000)
                    val = pwd_loc.input_value(timeout=3000)
                    if val == password:
                        pwd_filled = True
                    else:
                        self.log(f"[outlook] type 后密码值仍不匹配（len={len(val)}）")
                except Exception as exc:
                    self.log(f"[outlook] type 密码失败: {repr(exc)[:100]}")
            if not pwd_filled:
                self.log("[outlook] 密码未能填入输入框，放弃")
                return False
            page.wait_for_timeout(300)
            self._click_first_multilang(page, [SEL_PRIMARY_BUTTON])
            # 密码提交后页面切换有动画 + 网络请求，给 800ms 让点击生效再返回（生日页等待由 _fill_birthdate 处理）
            page.wait_for_timeout(800)
            self.log(f"[outlook] 邮箱+密码已填并提交: {email_local}{self.email_suffix}")
            return True
        except Exception as exc:
            self.log(f"[outlook] 填邮箱/密码失败: {repr(exc)[:120]}")
            return False

    def _fill_birthdate(self, page, year: str, month: str, day: str) -> bool:
        """填出生年月日并点 primaryButton 提交，等姓名页出现。

        月份/日期选项文案随语言变化：中文"5月"/"15日"，英文"May"/"15"。
        这里先试原生 select（value=月份数字），再试中文选项，再试英文选项。

        密码提交→生日页有动画切换 + 网络请求延迟，resin IP 下可能要 10-25s 才渲染出 BirthYear。
        所以这里先显式 wait_for(visible, 30s)，再看是否被拦/直接进验证码。
        """
        try:
            # 密码提交后页面切换中：等 BirthYear 可见（resin IP 慢，给 30s）
            birth_visible = False
            try:
                page.locator(SEL_BIRTH_YEAR).wait_for(state="visible", timeout=30000)
                birth_visible = True
            except Exception:
                # BirthYear 没出现：可能是被拦、直接进验证码、或直接跳姓名页
                block = self._check_block_signals(page)
                if block in ("account_blocked", "ratelimit", "wrong_captcha_type"):
                    self.log(f"[outlook] 生日页未出现且检测到拦截信号: {block}")
                    return False
                # 检查是否直接跳到了姓名页（罕见，无生日页 region）
                try:
                    if page.locator(SEL_LAST_NAME).count() > 0:
                        self.log("[outlook] 跳过生日页直接到姓名页，跳过生日填写")
                        return True
                except Exception:
                    pass
                # 检查是否直接进了验证码 iframe（罕见）
                for sel in (SEL_ARKOSE_OUTER_IFRAME, SEL_ARKOSE_OUTER_IFRAME_EN, SEL_ENFORCEMENT_FRAME):
                    try:
                        if page.locator(sel).count() > 0:
                            self.log("[outlook] 跳过生日页直接进验证码，跳过生日填写")
                            return True
                    except Exception:
                        pass
                self.log("[outlook] 生日页 BirthYear 30s 内未出现，放弃")
                return False
            if not birth_visible:
                return False
            page.wait_for_timeout(200)
            page.locator(SEL_BIRTH_YEAR).fill(year, timeout=10000)
            month_idx = int(month)
            # 1) 原生 select（部分 region 仍是原生 select，value=月份数字）
            filled_month = False
            try:
                page.locator(SEL_BIRTH_MONTH).select_option(value=month, timeout=2000)
                page.locator(SEL_BIRTH_DAY).select_option(value=day, timeout=2000)
                filled_month = True
            except Exception:
                pass
            # 2) 中文 combobox 选项 "5月"/"15日"（缩短超时，Firefox 下 option click 慢）
            if not filled_month:
                try:
                    page.locator(SEL_BIRTH_MONTH).click(timeout=3000)
                    page.wait_for_timeout(300)
                    page.locator(SEL_MONTH_OPTION_TEMPLATE_CN.format(month=month)).click(timeout=3000)
                    page.locator(SEL_BIRTH_DAY).click(timeout=3000)
                    page.wait_for_timeout(300)
                    page.locator(SEL_DAY_OPTION_TEMPLATE_CN.format(day=day)).click(timeout=3000)
                    filled_month = True
                except Exception:
                    pass
            # 3) 英文 combobox 选项 "May"/"15"（缩短超时）
            if not filled_month:
                try:
                    page.locator(SEL_BIRTH_MONTH).click(timeout=3000)
                    page.wait_for_timeout(300)
                    month_name_en = SEL_MONTH_NAMES_EN[month_idx] if 1 <= month_idx <= 12 else ""
                    if month_name_en:
                        page.locator(f'[role="option"]:text-is("{month_name_en}")').click(timeout=3000)
                    page.locator(SEL_BIRTH_DAY).click(timeout=3000)
                    page.wait_for_timeout(300)
                    page.locator(SEL_DAY_OPTION_TEMPLATE_EN.format(day=day)).click(timeout=3000)
                except Exception:
                    pass
            # 4) Firefox/camoufox 兜底：用 get_by_text 找选项（Firefox 下 [role="option"] text 匹配不稳定）
            if not filled_month:
                try:
                    # 先确认 month/day 是否已经有值（前面某步可能成功了但没设 flag）
                    month_val = page.locator(SEL_BIRTH_MONTH).evaluate("el => el.getAttribute('data-value') || el.textContent || ''")
                    day_val = page.locator(SEL_BIRTH_DAY).evaluate("el => el.getAttribute('data-value') || el.textContent || ''")
                    if month_val and day_val:
                        filled_month = True
                except Exception:
                    pass
            if not filled_month:
                try:
                    # Firefox 兜底：点击 combobox 打开下拉，用 get_by_text 选
                    page.locator(SEL_BIRTH_MONTH).click(timeout=5000)
                    page.wait_for_timeout(500)
                    # 中文"5月" 或英文"May"
                    month_text_cn = f"{month}月"
                    month_name_en = SEL_MONTH_NAMES_EN[month_idx] if 1 <= month_idx <= 12 else ""
                    for mt in (month_text_cn, month_name_en, month):
                        try:
                            opt = page.get_by_text(mt, exact=False).first
                            if opt.count() > 0:
                                opt.click(timeout=3000)
                                filled_month = True
                                break
                        except Exception:
                            continue
                    if filled_month:
                        page.locator(SEL_BIRTH_DAY).click(timeout=5000)
                        page.wait_for_timeout(500)
                        for dt in (f"{day}日", day):
                            try:
                                opt = page.get_by_text(dt, exact=False).first
                                if opt.count() > 0:
                                    opt.click(timeout=3000)
                                    break
                            except Exception:
                                continue
                except Exception as exc:
                    self.log(f"[outlook] Firefox 生日兜底也失败: {repr(exc)[:100]}")
            # 5) JavaScript 兜底：用 JS 直接点击 option 元素（Firefox 下最可靠）
            if not filled_month:
                try:
                    month_name_en = SEL_MONTH_NAMES_EN[month_idx] if 1 <= month_idx <= 12 else ""
                    month_text_cn = f"{month}月"
                    js_clicked = page.evaluate(
                        """(opts) => {
                            const monthTexts = [opts.month_cn, opts.month_en, opts.month];
                            const dayTexts = [opts.day_cn, opts.day];
                            // 找 month option
                            const monthSel = document.querySelectorAll('[role="option"], [role="listboxoption"], li, option');
                            let monthClicked = false;
                            for (const el of monthSel) {
                                const txt = (el.textContent || '').trim();
                                if (monthTexts.some(t => txt.includes(t)) && txt.length < 10) {
                                    el.click();
                                    monthClicked = true;
                                    break;
                                }
                            }
                            if (!monthClicked) return false;
                            // 找 day option
                            const daySel = document.querySelectorAll('[role="option"], [role="listboxoption"], li, option');
                            for (const el of daySel) {
                                const txt = (el.textContent || '').trim();
                                if (dayTexts.some(t => txt.includes(t)) && txt.length < 10) {
                                    el.click();
                                    return true;
                                }
                            }
                            return false;
                        }""",
                        {"month_cn": month_text_cn, "month_en": month_name_en, "month": month,
                         "day_cn": f"{day}日", "day": day},
                    )
                    if js_clicked:
                        filled_month = True
                        self.log("[outlook] JS 兜底选生日成功")
                except Exception as exc:
                    self.log(f"[outlook] JS 生日兜底失败: {repr(exc)[:80]}")
            self.log(f"[outlook] 生日已填: {year}-{month}-{day}")
            # 提交生日页
            self._click_first_multilang(page, [SEL_PRIMARY_BUTTON])
            # 等姓名页 #lastNameInput 出现（页面动画切换有延迟）
            try:
                page.locator(SEL_LAST_NAME).wait_for(state="visible", timeout=15000)
            except Exception:
                self.log("[outlook] 提交生日后未看到姓名页（可能直接进验证码或被拦）")
            return True
        except Exception as exc:
            self.log(f"[outlook] 填生日失败: {repr(exc)[:120]}")
            return False

    def _fill_name_and_submit(self, page, first_name: str, last_name: str) -> bool:
        """填姓名并点 primaryButton 提交，等防机器人链接 detached。"""
        try:
            # 姓名页 #lastNameInput 已在 _fill_birthdate 里等过；若没出现这里再兜底等一次
            try:
                page.locator(SEL_LAST_NAME).wait_for(state="visible", timeout=10000)
            except Exception:
                self.log("[outlook] 姓名页未出现，跳过姓名填写")
                return True
            page.locator(SEL_LAST_NAME).type(last_name, delay=20, timeout=10000)
            page.wait_for_timeout(200)
            page.locator(SEL_FIRST_NAME).fill(first_name, timeout=10000)
            # 确保达到 bot_protection_wait 总时长（参考实现：从同意按钮按下累计）
            page.locator(SEL_PRIMARY_BUTTON).click(timeout=5000)
            # 等防机器人链接 detached（表示进入验证码/收件页）
            try:
                page.locator(SEL_BOT_PROTECTION_LINK).wait_for(state="detached", timeout=22000)
            except Exception:
                pass
            page.wait_for_timeout(400)
            self.log(f"[outlook] 姓名+提交完成: {first_name} {last_name}")
            return True
        except Exception as exc:
            self.log(f"[outlook] 填姓名/提交失败: {repr(exc)[:120]}")
            return False

    def _enable_imap_pop(self, page) -> None:
        """注册后趁 session 还活着，在同一个 page 里去设置页启用 POP/IMAP。

        新账号 IMAP 返回 "User is authenticated but not connected"，
        需在 Outlook Web 设置 → 同步邮件 → POP 和 IMAP 里开启 POP 开关。

        注册刚完成时 page 已登录 Outlook Web（收件箱已加载），
        直接跳设置页可以避免重新登录的 session 问题。
        """
        url = "https://outlook.live.com/mail/0/options/accounts/pop-imap"
        try:
            self.log("[outlook] 去 Outlook 设置页启用 POP/IMAP...")
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)

            # 设置页是 SPA，可能需要等左侧菜单"同步邮件"加载
            # 先点"同步邮件"（如果还没到 POP/IMAP 子页）
            for sel_text in ("同步邮件", "Sync email"):
                try:
                    link = page.get_by_text(sel_text, exact=False).first
                    if link.count() > 0:
                        link.click(timeout=8000)
                        self.log(f"[outlook] 已点击: {sel_text}")
                        page.wait_for_timeout(3000)
                        break
                except Exception:
                    continue

            # 找 POP 启用开关 — 通常是 select 下拉框
            # 中文："让设备和应用使用 POP" → 选"启用"
            # 英文："Let devices and apps use POP" → select "Enable"
            for sel in page.locator('select').all():
                try:
                    options = sel.evaluate(
                        'el => Array.from(el.options).map(o => o.text + ":" + o.value)'
                    )
                    opt_str = " | ".join(options)
                    if "启用" in opt_str or "Enable" in opt_str or "Yes" in opt_str:
                        # 选启用选项
                        for opt_text in ("启用", "Enable", "Yes", "是"):
                            try:
                                sel.select_option(label=opt_text, timeout=5000)
                                self.log(f"[outlook] 已选 POP 开关: {opt_text}")
                                page.wait_for_timeout(2000)
                                break
                            except Exception:
                                continue
                        break
                except Exception:
                    continue

            # 也尝试 radio/button 形式的开关
            for sel_text in ("启用", "是", "Yes", "Enable", "On", "开启"):
                try:
                    btn = page.get_by_role("button", name=sel_text).first
                    if btn.count() > 0:
                        btn.click(timeout=5000)
                        self.log(f"[outlook] 已点击启用按钮: {sel_text}")
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue

            # 找保存按钮
            for sel_text in ("保存", "Save", "确定", "OK"):
                try:
                    save_btn = page.get_by_role("button", name=sel_text).first
                    if save_btn.count() > 0:
                        save_btn.click(timeout=5000)
                        self.log(f"[outlook] 已点击保存: {sel_text}")
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    continue
            self.log("[outlook] POP/IMAP 启用步骤完成")
        except Exception as exc:
            self.log(f"[outlook] 启用 POP/IMAP 失败（不影响注册结果）: {repr(exc)[:120]}")

    def _check_block_signals(self, page) -> str:
        """检查 IP 频率限制 / 帐户创建被阻止 / FunCaptcha 错误类型（多语言）。

        返回 'ratelimit' | 'account_blocked' | 'wrong_captcha_type' | 'ok'。
        'account_blocked' 是微软在验证码之前就判定机器人并直接拦截，和 ratelimit 不同。
        """
        try:
            for text in ACCOUNT_BLOCKED_TEXTS:
                if page.get_by_text(text).count() > 0:
                    return "account_blocked"
            for text in RATE_LIMIT_TEXTS:
                if page.get_by_text(text).count() > 0:
                    return "ratelimit"
        except Exception:
            pass
        try:
            if page.locator(SEL_ENFORCEMENT_FRAME).count() > 0:
                return "wrong_captcha_type"
        except Exception:
            pass
        return "ok"

    def _wait_captcha_or_block(self, page, timeout_ms: int = 25000) -> str:
        """姓名提交后等验证码 iframe 出现或拦截页出现（多语言）。"""
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            self._poll_cancel()
            block = self._check_block_signals(page)
            if block in ("account_blocked", "ratelimit", "wrong_captcha_type"):
                return "blocked" if block == "account_blocked" else "blocked"
            # 验证码 iframe（中文/英文 title）
            for sel in (SEL_ARKOSE_OUTER_IFRAME, SEL_ARKOSE_OUTER_IFRAME_EN, SEL_ENFORCEMENT_FRAME):
                try:
                    if page.locator(sel).count() > 0:
                        return "captcha"
                except Exception:
                    pass
            # 收件页（中文/英文"新邮件"/"New mail"）
            for sel in (SEL_NEW_MAIL_BUTTON, SEL_NEW_MAIL_BUTTON_EN):
                try:
                    if page.locator(sel).count() > 0:
                        return "inbox"
                except Exception:
                    pass
            # "取消"/"Cancel" 文本（验证码已过的标志）
            for txt in ARKOSE_SUCCESS_TEXTS:
                try:
                    if page.get_by_text(txt).count() > 0:
                        return "captcha"
                except Exception:
                    pass
            page.wait_for_timeout(500)
        return "timeout"

    def _wait_registration_done(self, page, timeout_ms: int = 32000) -> bool:
        """等 [aria-label="新邮件"/"New mail"] 出现，确认注册成功并邮箱已初始化。"""
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            self._poll_cancel()
            for sel in (SEL_NEW_MAIL_BUTTON, SEL_NEW_MAIL_BUTTON_EN):
                try:
                    if page.locator(sel).count() > 0:
                        self.log("[outlook] 注册成功，邮箱已初始化（新邮件按钮可见）")
                        return True
                except Exception:
                    pass
            page.wait_for_timeout(500)
        self.log("[outlook] 未等到新邮件按钮（邮箱可能未初始化）")
        return False

    # ---- 一次完整注册尝试 ----
    def _attempt(self) -> dict:
        from platforms.outlook.arkose_proof import ArkoseLongPressSolver

        pw_or_camoufox, browser, ctx, owns_camoufox = self._launch_browser()
        page = None
        try:
            page = ctx.new_page()
            ctx.set_default_timeout(45000)

            email_local = random_email_local()
            password = generate_strong_password()
            first_name = _random_first_name()
            last_name = _random_last_name()
            year, month, day = _random_birthdate()
            full_email = f"{email_local}{self.email_suffix}"
            self.log(f"[outlook] 准备注册: {full_email}")

            if not self._open_signup_page(page):
                return {"ok": False, "error": "open_signup_failed"}
            self._poll_cancel()

            if not self._fill_email_and_password(page, email_local, password):
                return {"ok": False, "error": "fill_email_password_failed"}
            self._poll_cancel()

            if not self._fill_birthdate(page, year, month, day):
                return {"ok": False, "error": "fill_birthdate_failed"}
            self._poll_cancel()

            if not self._fill_name_and_submit(page, first_name, last_name):
                return {"ok": False, "error": "fill_name_submit_failed"}
            self._poll_cancel()

            # 姓名提交后等验证码 iframe 出现或拦截页出现（验证码加载有延迟）
            next_state = self._wait_captcha_or_block(page, timeout_ms=25000)
            self.log(f"[outlook] 姓名提交后状态: {next_state}")
            if next_state == "blocked":
                # 微软在验证码前直接拦截（"帐户创建已被阻止"）— IP 质量/指纹问题，换 IP 重试
                return {"ok": False, "error": "account_blocked", "email": full_email, "password": password}
            if next_state == "inbox":
                # 没有验证码，直接进了收件页（罕见但可能）
                self.log("[outlook] 跳过验证码直接进收件页")
            if next_state == "timeout":
                # 没等到验证码也没看到拦截，再检查一次 block 信号
                block = self._check_block_signals(page)
                if block in ("account_blocked", "ratelimit"):
                    return {"ok": False, "error": block, "email": full_email, "password": password}
                self.log("[outlook] 未等到验证码 iframe（可能无验证码或被拦），继续尝试 solver")

            block = self._check_block_signals(page)
            if block == "account_blocked":
                return {"ok": False, "error": "account_blocked", "email": full_email, "password": password}
            if block == "ratelimit":
                return {"ok": False, "error": "ratelimit", "email": full_email, "password": password}
            if block == "wrong_captcha_type":
                return {"ok": False, "error": "wrong_captcha_type", "email": full_email, "password": password}

            # Arkose 长按压验证
            solver = ArkoseLongPressSolver(
                page,
                max_retries=self.max_captcha_retries,
                use_protocol_proof=self.use_protocol_proof,
                log_fn=self.log,
            )
            captcha_ok = solver.solve()
            if not captcha_ok:
                return {"ok": False, "error": "captcha_failed", "email": full_email, "password": password}
            self._poll_cancel()

            # 等注册完成
            if not self._wait_registration_done(page):
                return {"ok": False, "error": "inbox_not_ready", "email": full_email, "password": password}

            # OAuth 拿 refresh_token
            from platforms.outlook.outlook_oauth import get_outlook_tokens
            try:
                tokens = get_outlook_tokens(
                    page, full_email,
                    password=password,
                    extra=self.extra,
                    proxy=self.proxy,
                    timeout=self.oauth_timeout,
                    log_fn=self.log,
                )
            except Exception as exc:
                self.log(f"[outlook] OAuth 拿 token 失败: {repr(exc)[:160]}")
                # 注册成功但 OAuth 失败：账号已创建，保存 email/password 到池（无 token）
                # 这样后续可以手动登录拿 token，不会丢失已注册账号
                try:
                    from core.outlook_account_pool import OutlookAccountPool
                    OutlookAccountPool().add_account(
                        email=full_email, password=password,
                        client_id="", refresh_token="", access_token="", expires_at="",
                        source="auto_register_no_token",
                    )
                    self.log(f"[outlook] 已保存无 token 账号到 outlook_accounts_pool.json: {full_email}")
                except Exception as persist_exc:
                    self.log(f"[outlook] 保存无 token 账号失败: {repr(persist_exc)[:100]}")
                return {"ok": False, "error": "oauth_failed", "email": full_email, "password": password,
                        "oauth_error": str(exc)[:300]}

            result = {
                "ok": True,
                "email": full_email,
                "password": password,
                "client_id": tokens.client_id,
                "refresh_token": tokens.refresh_token,
                "access_token": tokens.access_token,
                "expires_at": tokens.expires_at,
                "scope": tokens.scope,
                "first_name": first_name,
                "last_name": last_name,
                "birthdate": f"{year}-{month}-{day}",
            }

            # 启用 IMAP/POP（新账号默认 IMAP 未连接，需在 Outlook 设置页开启）
            self._enable_imap_pop(page)

            self._persist_result(result)
            return result
        finally:
            self._close_browser(pw_or_camoufox, ctx, owns_camoufox)

    def _persist_result(self, result: dict) -> None:
        """双写：mailbox_inventory(outlook_token) 供复用 + outlook_accounts_pool.json 供导出。"""
        email = str(result.get("email") or "").strip()
        password = str(result.get("password") or "").strip()
        client_id = str(result.get("client_id") or "").strip()
        refresh_token = str(result.get("refresh_token") or "").strip()
        if not email or not password or not client_id or not refresh_token:
            self.log(f"[outlook] 持久化跳过：字段不全 email={email} cid={'yes' if client_id else 'no'} rt={'yes' if refresh_token else 'no'}")
            return

        # 1) 写 mailbox_inventory（供其它平台领用）
        try:
            from infrastructure.mailbox_inventory_repository import MailboxInventoryRepository
            line = f"{email}----{password}----{client_id}----{refresh_token}"
            MailboxInventoryRepository().import_lines("outlook_token", [line])
            self.log(f"[outlook] 已写入 mailbox_inventory(outlook_token): {email}")
        except Exception as exc:
            self.log(f"[outlook] 写 mailbox_inventory 失败（不影响注册结果）: {repr(exc)[:120]}")

        # 2) 写 outlook_accounts_pool.json（离线导出）
        try:
            from core.outlook_account_pool import OutlookAccountPool
            OutlookAccountPool().add_account(
                email, password,
                client_id=client_id,
                refresh_token=refresh_token,
                access_token=str(result.get("access_token") or ""),
                expires_at=str(result.get("expires_at") or ""),
                source="auto_register",
            )
            self.log(f"[outlook] 已写入 outlook_accounts_pool.json: {email}")
        except Exception as exc:
            self.log(f"[outlook] 写 outlook_accounts_pool 失败（不影响注册结果）: {repr(exc)[:120]}")

    # ---- 对外入口 ----
    def run(self, *, email: str = "", password: str = "") -> dict:
        """执行一次 Outlook 注册。email/password 参数忽略（平台自生成）。

        返回 dict：ok/email/password/client_id/refresh_token/access_token/expires_at/scope/...
        失败时 ok=False 且含 error 字段。
        """
        deadline = time.time() + self.register_timeout
        last_error = ""
        # 单次注册通常不需要多轮重试（IP 质量问题重试也难救），这里只做一次主尝试；
        # 但 ratelimit/wrong_captcha_type 不重试（换 IP 才有救，由外层代理池处理）。
        try:
            result = self._attempt()
            if result.get("ok"):
                return result
            last_error = str(result.get("error") or "")
            return result
        except Exception as exc:
            self.log(f"[outlook] 注册异常: {repr(exc)[:160]}")
            return {"ok": False, "error": f"exception:{type(exc).__name__}", "exception": str(exc)[:300]}
        finally:
            self.log(f"[outlook] 注册结束 last_error={last_error} 耗时={time.time() - (deadline - self.register_timeout):.1f}s")
