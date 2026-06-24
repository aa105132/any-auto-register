"""TOTP / 2FA 通用工具：纯算法 + 浏览器键盘填码，不绑定任何具体平台。

平台专属的 2FA 流程（challenge 页判定、切换验证方式、选 Authenticator、
点 Next 提交）见 core/oauth_2fa.py，通过 TwoFactorStrategy 注入本模块的
通用填码能力。
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Callable


def normalize_totp_secret(raw: str) -> str:
    """把各种形态的 2FA 第三字段归一成存池用的字符串。

    支持三种输入：
    1. otpauth:// URL（如 otpauth://totp/Google:user@gmail.com?secret=XXX&...）
       → 提取 query 的 secret 参数，upper + 去空格
    2. 纯 base32 secret（如 5DJIRENHW5DABLRXBXN62TRCPZP2T5VG）
       → upper + 去空格直接返回
    3. recovery/备份码（非 base32 的任意字符串）
       → 原样 strip 返回，运行时 generate_totp_code 检测非 base32 会跳过

    空值返回 ""。
    """
    value = str(raw or "").strip()
    if not value:
        return ""

    # 形态 1：otpauth:// URL，提取 secret= 参数
    lowered = value.lower()
    if lowered.startswith("otpauth://"):
        try:
            parsed = urllib.parse.urlparse(value)
            params = urllib.parse.parse_qs(parsed.query or "")
            secret_values = params.get("secret") or []
            if secret_values:
                return secret_values[0].strip().replace(" ", "").upper()
        except Exception:
            pass
        # URL 里没解析出 secret，退而求其次返回原值
        return value

    # 形态 2/3：upper + 去空格后判断是否 base32
    cleaned = value.replace(" ", "").upper()
    return cleaned


def is_valid_totp_secret(secret: str) -> bool:
    """判断字符串是否为合法 base32 TOTP secret（长度 ≥16 且全在 A-Z2-7）。

    用于区分存池的 totp_secret 是真 TOTP secret 还是 recovery 备份码：
    前者能 generate_totp_code，后者只能人工输入。
    """
    cleaned = str(secret or "").strip().replace(" ", "").upper()
    if len(cleaned) < 16:
        return False
    return all(ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in cleaned)


def generate_totp_code(secret: str) -> str:
    """用 base32 TOTP secret 生成当前 6 位验证码。

    secret 非合法 base32（如 recovery 备份码）时返回空串，调用方应跳过自动填码。
    默认 SHA1/6 位/30 秒窗口，与 Google Authenticator、Microsoft Authenticator 一致。
    """
    import pyotp

    cleaned = str(secret or "").strip().replace(" ", "").upper()
    if not cleaned or not is_valid_totp_secret(cleaned):
        return ""
    return str(pyotp.TOTP(cleaned).now())


def wait_for_fresh_totp_window(*, period: int = 30, margin: int = 8, max_wait: int = 12,
                               log_fn: Callable[[str], None] = print) -> None:
    """对齐到新的 TOTP 时间窗口开头，避免在窗口末尾生成码后填+提交时已过期。

    TOTP 默认 30s 一个窗口。若当前已进入窗口末尾 margin 秒内（剩余 < margin），
    睡到下一个窗口开头，给"填码 + 点提交"留出接近一整个窗口的时间。
    max_wait 上限防止意外长睡（跨窗口边界最多睡 ~margin 秒）。
    """
    now = time.time()
    into_window = now % period
    remaining = period - into_window
    if remaining >= margin:
        return
    sleep_for = min(remaining + 0.5, max_wait)
    log_fn(f"[TOTP] 窗口仅剩 {remaining:.1f}s，等 {sleep_for:.1f}s 到新窗口再生成码")
    time.sleep(sleep_for)


def fill_totp_playwright(page, selectors: list[str], code: str, *,
                         log_fn: Callable[[str], None] = print) -> bool:
    """用真实键盘输入填 TOTP 码并校验值写入。

    受控组件（React/Vue 等）直接 evaluate 设 value 会被 state 覆盖回空。
    这里用 Playwright click + fill（触发完整 keystroke 事件链），填后读
    input_value 确认等于目标码。读不到值时降级逐字 type，再校验一次。
    selectors 由调用方传入（平台专属的 totp 输入框选择器）。
    """
    if not code:
        return False
    for selector in selectors:
        for frame in list(getattr(page, "frames", []) or [page]):
            try:
                loc = frame.locator(selector).first
                try:
                    loc.wait_for(state="attached", timeout=1500)
                except Exception:
                    continue
                try:
                    loc.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
                try:
                    loc.click(timeout=2000, force=True)
                except Exception:
                    box = loc.bounding_box(timeout=1000)
                    if not box:
                        continue
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                # 清空再填：偶发残留旧码
                try:
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Delete")
                except Exception:
                    pass
                try:
                    loc.fill(code, timeout=3000, force=True)
                except Exception:
                    page.keyboard.type(code, delay=30)
                # 校验：input_value 必须等于 code，否则受控组件回滚了
                try:
                    val = loc.input_value(timeout=1000)
                except Exception:
                    val = ""
                if val == code:
                    log_fn(f"[TOTP] 码已写入输入框 (sel={selector}, val={val})")
                    return True
                # 兜底：逐字 type（fill 被框架拦截时，逐字 keystroke 更可靠）
                try:
                    loc.click(timeout=1500, force=True)
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Delete")
                    page.keyboard.type(code, delay=40)
                    val2 = loc.input_value(timeout=1000)
                except Exception:
                    val2 = ""
                if val2 == code:
                    log_fn(f"[TOTP] 码已写入(逐字 type) (sel={selector}, val={val2})")
                    return True
                log_fn(f"[TOTP] 填码校验失败 (sel={selector}, got='{val2}' want='{code}')")
            except Exception:
                continue
    return False
