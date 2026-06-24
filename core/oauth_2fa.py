"""平台无关的 OAuth 二次验证（2FA / TOTP）驱动框架。

把 2FA 流程里"平台专属"的部分（challenge 页 URL 模式、输入框选择器、
多语言文案）抽成 TwoFactorStrategy 策略对象，"通用骨架"（按按钮位置
定位、多语言文本兜底、Playwright 键盘填码+校验、30s 窗口对齐）在本模块
实现。新平台接入 2FA 只需定义自己的 TwoFactorStrategy，调 drive_oauth_2fa。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from core.totp import (
    fill_totp_playwright,
    generate_totp_code,
    is_valid_totp_secret,
    wait_for_fresh_totp_window,
)


@dataclass
class TwoFactorStrategy:
    """某平台 2FA 流程的专属参数集合。

    所有列表字段均为多语言文案/选择器，按顺序尝试。通用骨架用这些值
    做判定与点击，平台差异全集中在此 dataclass。
    """

    # challenge 页 URL 里出现此子串即认为是 2FA 挑战页（Google: "challenge/"）
    challenge_url_pattern: str
    # 排除的 challenge 子路径（密码页不算 2FA，Google: ["challenge/pwd"]）
    challenge_url_exclude: list[str] = field(default_factory=list)
    # 2FA 页正文提示文案（页面 URL 不含 challenge 时按正文兜底判定）
    challenge_body_hints: list[str] = field(default_factory=list)
    # TOTP 验证码输入框选择器（按优先级，Google: ['input[name="totpPin"]', ...]）
    totp_input_selectors: list[str] = field(default_factory=list)
    # 要排除的输入框选择器（手机推送码等，Google: ['#ootp-pin', 'input[name="Pin"]']）
    exclude_input_selectors: list[str] = field(default_factory=list)
    # "Try another way" 多语言文案（切换验证方式入口）
    try_another_way_labels: list[str] = field(default_factory=list)
    # Authenticator 选项多语言文案（方式列表里选 TOTP）
    authenticator_option_labels: list[str] = field(default_factory=list)
    # 提交按钮多语言文案（Next/Suivant/Siguiente...）
    submit_labels: list[str] = field(default_factory=list)
    # 方式列表页 URL 子串（此页只能选 Authenticator，禁点 Try another way，Google: "challenge/selection"）
    selection_url_pattern: str = ""
    # 日志前缀（Google: "[GoogleOAuth]"，Microsoft: "[MSOAuth]" 等）
    log_prefix: str = "[2FA]"


def _log(strategy: TwoFactorStrategy, log_fn: Callable[[str], None], msg: str) -> None:
    log_fn(f"{strategy.log_prefix} {msg}")


def _body_text(page, timeout: int = 800) -> str:
    try:
        return (page.inner_text("body", timeout=timeout) or "").strip()
    except Exception:
        try:
            return str(page.evaluate("() => (document.body && document.body.innerText) || ''") or "")
        except Exception:
            return ""


def is_2fa_challenge(page, strategy: TwoFactorStrategy) -> bool:
    """识别平台 2FA 挑战页（URL 模式优先 + 正文文案兜底 + 输入框存在性兜底）。"""
    url = str(page.url or "").lower()
    pattern = (strategy.challenge_url_pattern or "").lower()
    if pattern and pattern in url:
        # 排除密码页等非 2FA challenge
        for exclude in strategy.challenge_url_exclude:
            if exclude.lower() in url:
                return False
        return True
    try:
        body = _body_text(page, timeout=800).lower()
        for hint in strategy.challenge_body_hints:
            if hint.lower() in body:
                return True
    except Exception:
        pass
    # 输入框存在性兜底
    if strategy.totp_input_selectors:
        try:
            return bool(page.evaluate(
                """
                (selectors) => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                  };
                  return [...document.querySelectorAll(selectors.join(','))]
                    .some(el => visible(el) && !el.disabled && !el.readOnly);
                }
                """,
                strategy.totp_input_selectors,
            ))
        except Exception:
            pass
    return False


def has_totp_input(page, strategy: TwoFactorStrategy) -> bool:
    """当前 2FA 页是否已有真正的 TOTP（Authenticator）验证码输入框。

    排除 strategy.exclude_input_selectors 指定的手机推送码等干扰输入框。
    """
    try:
        return bool(page.evaluate(
            """
            ({selectors, excludeSelectors}) => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              const all = [...document.querySelectorAll('input')].filter(el => visible(el) && !el.disabled && !el.readOnly);
              const isExcluded = (el) => excludeSelectors.some(sel => {
                if (sel.startsWith('#')) {
                  if ('#' + (el.id || '') === sel) return true;
                }
                if (sel.indexOf('name="') !== -1) {
                  const m = sel.match(/name="([^"]+)"/);
                  if (m && (el.name || '').toLowerCase() === m[1].toLowerCase()) return true;
                }
                return false;
              });
              const totpInputs = all.filter(el => !isExcluded(el));
              const preciseSels = selectors.map(s => s.toLowerCase());
              const matchPrecise = (el) => {
                const nameId = [(el.name||'').toLowerCase(), (el.id||'').toLowerCase()];
                for (const s of preciseSels) {
                  if (s.includes('totppin') && (nameId.includes('totppin') || nameId.includes('totp'))) return true;
                  if (s.includes('one-time-code') && (el.autocomplete||'') === 'one-time-code') return true;
                }
                return false;
              };
              const precise = totpInputs.filter(matchPrecise);
              if (precise.length) return true;
              const body = (document.body && document.body.innerText) || '';
              const isAuthPage = /authenticator/i.test(body);
              const lenientVisible = totpInputs.filter(el => ['tel','number','text'].includes((el.type||'').toLowerCase()) || !el.type);
              if (isAuthPage && lenientVisible.length >= 1) return true;
              return false;
            }
            """,
            {"selectors": strategy.totp_input_selectors, "excludeSelectors": strategy.exclude_input_selectors},
        ))
    except Exception:
        return False


def click_try_another_way(page, strategy: TwoFactorStrategy, *,
                          log_fn: Callable[[str], None] = print) -> bool:
    """在 2FA 页点 "Try another way" 切换验证方式（语言无关按按钮位置 + 多语言兜底）。

    ootp 页固定有 2 个 button：第 1 个是 Next/继续，第 2 个是 Try another way。
    按按钮位置定位可绕过语言差异。force=True 绕过 actionability 检查（按钮偶发
    被 overlay/动画遮盖导致 click 超时）。
    """
    # 策略 1：语言无关——点第 2 个可见 button
    try:
        buttons = page.locator("button:visible")
        if buttons.count() >= 2:
            second_text = buttons.nth(1).inner_text(timeout=1500)
            first_text = buttons.nth(0).inner_text(timeout=1500)
            if first_text and second_text and first_text != second_text:
                try:
                    buttons.nth(1).click(timeout=8000, force=True)
                except Exception:
                    buttons.nth(1).click(timeout=8000)
                _log(strategy, log_fn, f"点击切换验证方式(第2按钮): {second_text[:40]}")
                time.sleep(3)
                return True
    except Exception as exc:
        _log(strategy, log_fn, f"第2按钮定位失败: {exc!r}")

    # 策略 2：多语言文本匹配兜底
    for text in strategy.try_another_way_labels:
        try:
            loc = page.get_by_text(text, exact=False).first
            try:
                loc.wait_for(state="visible", timeout=1000)
            except Exception:
                continue
            try:
                loc.click(timeout=4000, force=True)
            except Exception:
                loc.click(timeout=4000)
            _log(strategy, log_fn, f"点击切换验证方式(文本): {text}")
            time.sleep(3)
            return True
        except Exception:
            continue
    return False


def select_authenticator_option(page, strategy: TwoFactorStrategy, *,
                                wait: int = 15, log_fn: Callable[[str], None] = print) -> bool:
    """在"选择验证方式"列表里点 Authenticator 选项（多语言）。

    选项是 div[role="link"] 或 li，文案含 "Authenticator"。selection 页加载有延迟，
    用 Playwright locator 点击（自动等待可见，不阻塞页面 JS）。force=True 兜底遮盖。
    """
    deadline = time.time() + max(1, int(wait))
    while time.time() < deadline:
        for text in strategy.authenticator_option_labels:
            try:
                loc = page.get_by_text(text, exact=False).first
                try:
                    loc.wait_for(state="visible", timeout=1500)
                except Exception:
                    continue
                try:
                    loc.click(timeout=4000, force=True)
                except Exception:
                    loc.click(timeout=4000)
                _log(strategy, log_fn, f"选择 Authenticator: {text}")
                time.sleep(3)
                return True
            except Exception:
                continue
        time.sleep(0.8)
    return False


def submit_totp(page, strategy: TwoFactorStrategy, *, log_fn: Callable[[str], None] = print) -> None:
    """提交 TOTP 验证码：显式点提交按钮（Next 等），focus+Enter 在 AJAX 页不触发提交。

    提交按钮语言无关按第 1 个可见 button 定位，多语言文案兜底，focus+Enter 兜底。
    """
    # 策略 1（主）：点第 1 个可见 button（Next/Suivant/Siguiente/Weiter）
    try:
        buttons = page.locator("button:visible")
        if buttons.count() >= 1:
            try:
                buttons.nth(0).click(timeout=6000, force=True)
            except Exception:
                buttons.nth(0).click(timeout=6000)
            _log(strategy, log_fn, "提交 totp: 点第1按钮(Next)")
            return
    except Exception as exc:
        _log(strategy, log_fn, f"点 Next 按钮失败: {exc!r}")
    # 策略 2：多语言文本兜底
    for text in strategy.submit_labels:
        try:
            loc = page.get_by_text(text, exact=False).first
            try:
                loc.wait_for(state="visible", timeout=1000)
            except Exception:
                continue
            try:
                loc.click(timeout=3000, force=True)
            except Exception:
                loc.click(timeout=3000)
            _log(strategy, log_fn, f"提交 totp: 点文本 {text}")
            return
        except Exception:
            continue
    # 策略 3（兜底）：focus 到 TOTP 输入框后按 Enter
    for sel in strategy.totp_input_selectors[:3]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.focus(timeout=1500)
                page.keyboard.press("Enter")
                _log(strategy, log_fn, "提交 totp: focus + Enter(兜底)")
                return
        except Exception:
            continue


def drive_oauth_2fa_step(page, strategy: TwoFactorStrategy, *, totp_secret: str,
                         totp_attempts: int, max_attempts: int = 3,
                         log_fn: Callable[[str], None] = print,
                         fill_input_js: Callable | None = None) -> tuple[bool, bool, int]:
    """处理单个 page 的 2FA 一步，返回 (progressed, blocked, new_attempts)。

    这是 2FA 主循环里对单个 page 的处理逻辑，供 drive_google_oauth 等平台 driver
    在自己的主循环里调用。fill_input_js 是平台专属的 evaluate 设值兜底函数
    （Google 的 _fill_google_input_js），签名 (page, selectors, value) -> bool；
    为 None 时只用 Playwright 键盘填码。
    """
    if not totp_secret or totp_attempts >= max_attempts:
        return (False, False, totp_attempts)
    if not is_2fa_challenge(page, strategy):
        return (False, False, totp_attempts)

    if has_totp_input(page, strategy):
        # 时序对齐：窗口末尾等新窗口
        wait_for_fresh_totp_window(log_fn=log_fn)
        code = generate_totp_code(totp_secret)
        if not code:
            _log(strategy, log_fn, "2FA 输入框出现但 TOTP secret 为空或非 base32，停止")
            return (False, True, totp_attempts)
        selectors = strategy.totp_input_selectors
        filled = fill_totp_playwright(page, selectors, code, log_fn=log_fn)
        if not filled and fill_input_js is not None:
            # 兜底：evaluate 设值（部分非受控 totp 页有效）
            filled = fill_input_js(page, selectors, code)
        if filled:
            submit_totp(page, strategy, log_fn=log_fn)
            new_attempts = totp_attempts + 1
            _log(strategy, log_fn, f"已提交 2FA 验证码 (尝试 {new_attempts}/{max_attempts})")
            time.sleep(5)
            return (True, False, new_attempts)
        _log(strategy, log_fn, "2FA 输入框存在但未能写入验证码")
        time.sleep(1)
        return (False, False, totp_attempts)

    # 无输入框（手机推送或方式列表页）：切换不消耗 totp_attempts
    url_low = str(page.url or "").lower()
    kind = url_low.split(str(strategy.challenge_url_pattern))[-1][:20].split('&')[0] if strategy.challenge_url_pattern and strategy.challenge_url_pattern in url_low else url_low[:30]
    # 方式列表页只能选 Authenticator，禁点 Try another way（会触发 signin/rejected）
    if strategy.selection_url_pattern and strategy.selection_url_pattern in url_low:
        _log(strategy, log_fn, f"方式列表页 ({kind})，等待并选 Authenticator")
        time.sleep(3)
        if select_authenticator_option(page, strategy, log_fn=log_fn, wait=20):
            time.sleep(3)
            return (True, False, totp_attempts)
        _log(strategy, log_fn, "方式列表页未找到 Authenticator 选项，停止避免 rejected")
        return (False, True, totp_attempts)
    # 初始推送页：先尝试选 Authenticator（覆盖已渲染列表），失败再点 Try another way
    if select_authenticator_option(page, strategy, log_fn=log_fn, wait=2):
        time.sleep(3)
        return (True, False, totp_attempts)
    _log(strategy, log_fn, f"2FA 页无 Authenticator 选项 ({kind})，点 Try another way 切换")
    if click_try_another_way(page, strategy, log_fn=log_fn):
        time.sleep(3)
        return (True, False, totp_attempts)
    _log(strategy, log_fn, "无法切换到 Authenticator，可能仅手机推送")
    time.sleep(2)
    return (False, False, totp_attempts)
