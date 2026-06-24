"""共享 Google OAuth 自动化辅助。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from core.google_account_pool import GoogleAccountPool
from core.oauth_browser import OAuthBrowser

GOOGLE_CONSENT_LABELS = (
    "Continue", "Allow", "I agree", "I understand", "Next", "Accept", "Agree", "Done",
    "继续", "允许", "我同意", "我了解", "下一步", "接受", "同意", "完成",
)
GOOGLE_DENY_LABELS = (
    "Cancel", "Back", "No", "Not now", "取消", "返回", "拒绝", "暂不",
)
GOOGLE_CONSENT_HINTS = GOOGLE_CONSENT_LABELS + (
    "wants to access", "Sign in to", "continue to", "terms of service", "privacy policy",
    "review permissions", "choose what", "confirm your choices",
    "继续前往", "登录", "授权", "服务条款", "隐私权政策", "隐私政策", "确认您的选择",
)
GOOGLE_CREDENTIAL_INPUT_SELECTOR = (
    'input[type="email"]:visible, input[name="identifier"]:visible, #identifierId:visible, '
    'input[type="password"]:visible, input[name="Passwd"]:visible'
)


@dataclass
class GoogleOAuthResult:
    """Google OAuth 辅助处理结果。"""

    email_submitted: bool = False
    password_submitted: bool = False
    totp_submitted: bool = False
    clicked_prompt: bool = False
    blocked_on_password: bool = False
    last_url: str = ""
    last_body: str = ""


def _body_text(page, *, timeout: int = 1000) -> str:
    try:
        return str(page.locator("body").inner_text(timeout=timeout) or "")
    except Exception:
        return ""



def _fill_google_input_js(page, selectors: list[str], value: str) -> bool:
    """用 DOM 事件填 Google 输入框，覆盖 Playwright locator 识别不到的 Material 输入。

    Google 登录页偶尔把输入框放在子 frame 或 Material 包装层里；这里逐个
    frame 扫描，任何一个 frame 填入成功即返回，避免停在密码页还继续点
    consent/Next 造成假成功。
    """
    if not value:
        return False
    script = """
        ({selectors, value}) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
          };
          const candidates = [];
          for (const selector of selectors) {
            candidates.push(...document.querySelectorAll(selector));
          }
          const input = candidates.find(el => visible(el) && !el.disabled && !el.readOnly);
          if (!input) return false;
          input.focus();
          const proto = Object.getPrototypeOf(input);
          const desc = Object.getOwnPropertyDescriptor(proto, 'value') || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
          const setValue = (v) => desc && desc.set ? desc.set.call(input, v) : (input.value = v);
          setValue('');
          input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward', data: null}));
          setValue(value);
          input.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value}));
          input.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        }
    """
    for frame in list(getattr(page, "frames", []) or [page]):
        try:
            if bool(frame.evaluate(script, {"selectors": selectors, "value": value})):
                return True
        except Exception:
            continue
    return False


def _has_google_credential_input(page) -> bool:
    script = """
        () => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
          };
          return [...document.querySelectorAll('input[type="email"],input[name="identifier"],#identifierId,input[type="password"],input[name="Passwd"]')]
            .some(el => visible(el) && !el.disabled);
        }
    """
    for frame in list(getattr(page, "frames", []) or [page]):
        try:
            if bool(frame.evaluate(script)):
                return True
        except Exception:
            continue
    return False


def _fill_google_input_playwright(page, selectors: list[str], value: str) -> bool:
    """Playwright/键盘兜底填 Google 输入框。"""
    if not value:
        return False
    for selector in selectors:
        for frame in list(getattr(page, "frames", []) or [page]):
            try:
                locator = frame.locator(selector).first
                locator.wait_for(state="attached", timeout=1500)
                try:
                    locator.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
                try:
                    locator.click(timeout=2000, force=True)
                except Exception:
                    box = locator.bounding_box(timeout=1000)
                    if not box:
                        continue
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                try:
                    locator.fill(value, timeout=3000, force=True)
                except Exception:
                    page.keyboard.press("Control+A")
                    page.keyboard.type(value, delay=20)
                try:
                    current = locator.input_value(timeout=1000)
                except Exception:
                    current = ""
                if current:
                    return True
                page.keyboard.press("Control+A")
                page.keyboard.type(value, delay=20)
                try:
                    return bool(locator.input_value(timeout=1000))
                except Exception:
                    return True
            except Exception:
                continue
    return False


def _google_input_debug(page, selectors: list[str]) -> str:
    """返回当前 Google 输入框现场，便于定位密码页填充失败。"""
    script = """
        ({selectors}) => {
          const visible = (el) => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
          };
          const out = [];
          for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
              const r = el.getBoundingClientRect();
              out.push({
                selector, tag: el.tagName, type: el.getAttribute('type'), name: el.getAttribute('name'),
                id: el.id || '', aria: el.getAttribute('aria-label') || '', disabled: !!el.disabled,
                readonly: !!el.readOnly, visible: visible(el), rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
                valueLen: String(el.value || '').length
              });
            }
          }
          return JSON.stringify(out).slice(0, 1200);
        }
    """
    parts = []
    for idx, frame in enumerate(list(getattr(page, "frames", []) or [page])):
        try:
            parts.append(f"frame{idx}:{frame.evaluate(script, {'selectors': selectors})}")
        except Exception as exc:
            parts.append(f"frame{idx}:ERR:{exc!r}")
    return " | ".join(parts)[:2000]


def _looks_like_embedded_app_shell(page) -> bool:
    try:
        return bool(page.evaluate(
            """
            () => {
              const body = (document.body && document.body.innerText) || '';
              return body.includes('Unified API gateway')
                || (body.includes('Command Palette') && body.includes('Get Started') && body.includes('APIs'));
            }
            """
        ))
    except Exception:
        return False


def _is_password_challenge(page) -> bool:
    # URL 有时滞留在 Google challenge，但 DOM 已经被目标站内容替换；
    # 这种状态不能继续当密码页，否则会无限等待/重复误点。
    if _looks_like_embedded_app_shell(page):
        return False
    url = str(page.url or "")
    if "challenge/pwd" in url or "Passwd" in url:
        return True
    try:
        return bool(page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              return [...document.querySelectorAll('input[type="password"],input[name="Passwd"]')]
                .some(el => visible(el) && !el.disabled && !el.readOnly);
            }
            """
        ))
    except Exception:
        return False




def _is_google_captcha_page(page) -> bool:
    body = _body_text(page, timeout=800)
    captcha_hints = (
        "输入您听到或看到的文字",
        "输入你听到或看到的文字",
        "Enter the text you hear or see",
        "Type the text you hear or see",
    )
    if any(text in body for text in captcha_hints):
        return True
    try:
        return bool(page.evaluate(
            """
            () => {
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
              };
              return [...document.querySelectorAll('input[name="ca"], #ca')]
                .some(el => visible(el) && !el.disabled && !el.readOnly);
            }
            """
        ))
    except Exception:
        return False


def _is_google_deleted_account_page(body: str) -> bool:
    text = str(body or "").strip()
    lowered = text.lower()
    if not text:
        return False
    deleted_markers = (
        "账号已被删除",
        "帳號已刪除",
        "此账号最近已被删除",
        "此帳號最近已遭刪除",
        "account was deleted",
        "account has been deleted",
        "this account was recently deleted",
        "this account has recently been deleted",
    )
    recover_markers = (
        "恢复",
        "復原",
        "恢复此账号",
        "復原這個帳號",
        "recover",
        "restore",
    )
    has_deleted_marker = any(marker in text or marker in lowered for marker in deleted_markers)
    if not has_deleted_marker:
        return False
    return any(marker in text or marker in lowered for marker in recover_markers) or "google" in lowered


def _mark_google_account_invalid(email: str, *, reason: str, log_fn: Callable[[str], None] = print) -> bool:
    normalized_email = str(email or "").strip()
    if not normalized_email:
        return False
    try:
        marked = GoogleAccountPool().mark_invalid(normalized_email, reason=reason)
        if marked:
            log_fn(f"[GoogleOAuth] Google 账号已标记失效: {normalized_email} ({reason})")
        return bool(marked)
    except Exception as exc:
        log_fn(f"[GoogleOAuth] 标记 Google 账号失效失败: {normalized_email} ({exc})")
        return False

def _click_text_or_prompt(page, labels: tuple[str, ...] = GOOGLE_CONSENT_LABELS) -> str:
    """点击 Google 页面上的继续/允许/条款确认类按钮。

    Google 的 TOS/speedbump 经常需要先滚到底部、点一次“我同意/继续”，
    随后再进入 consent 页再点“继续/允许”。这里每轮只点一个最可信按钮，
    drive_google_oauth 会循环处理下一阶段。
    """
    # 先用 Playwright 点真实 action button，避免 JS 滚动/容器命中导致漏点 consent 页右下 Continue。
    action_labels = {"continue", "allow", "i agree", "next", "accept", "agree", "done", "继续", "允许", "我同意", "下一步", "接受", "同意", "完成"}
    for label in labels:
        if str(label).strip().lower() not in action_labels and str(label).strip() not in action_labels:
            continue
        try:
            locator = page.locator("button, [role='button'], input[type='submit'], input[type='button']").filter(has_text=re.compile(f"^{re.escape(label)}$", re.IGNORECASE)).last
            locator.wait_for(state="visible", timeout=800)
            try:
                locator.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            box = None
            try:
                box = locator.bounding_box(timeout=800)
            except Exception:
                box = None
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                locator.click(timeout=1500, force=True)
            return label
        except Exception:
            continue
    try:
        return str(page.evaluate(
            """
            ({labels, denyLabels}) => {
              const deny = new Set(denyLabels.map(x => String(x).toLowerCase()));
              const visible = (el) => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
              };
              const textOf = (el) => (el.value || el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('data-mdc-dialog-action') || '').trim();
              const clickEl = (el) => {
                el.scrollIntoView({block:'center', inline:'center'});
                const r = el.getBoundingClientRect();
                const x = Math.max(2, Math.min(innerWidth - 2, r.left + r.width / 2));
                const y = Math.max(2, Math.min(innerHeight - 2, r.top + r.height / 2));
                const target = document.elementFromPoint(x, y) || el;
                for (const type of ['pointerover','mouseover','pointerdown','mousedown','pointerup','mouseup','click']) {
                  target.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                }
              };
              // TOS 页面按钮常在底部；先滚到底再收集按钮。
              window.scrollTo(0, document.body.scrollHeight);
              const candidates = [...document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"],a,div[role="link"]')]
                .filter(el => visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true')
                .map(el => ({el, text: textOf(el), r: el.getBoundingClientRect()}))
                .filter(x => x.r.width > 24 && x.r.height > 10)
                .filter(x => !deny.has(String(x.text || '').toLowerCase()));
              const actionable = candidates.filter(x => String(x.text || '').trim());
              const actionLabels = new Set(['continue','allow','i agree','i understand','next','accept','agree','done','继续','允许','我同意','我了解','下一步','接受','同意','完成']);
              for (const label of labels) {
                const lower = String(label).toLowerCase();
                if (!actionLabels.has(lower)) continue;
                const exact = actionable.find(x => String(x.text).toLowerCase() === lower);
                if (exact) { clickEl(exact.el); return exact.text || label; }
                const partial = actionable.find(x => {
                  const text = String(x.text).toLowerCase();
                  const role = String(x.el.getAttribute('role') || '').toLowerCase();
                  return (x.el.tagName === 'BUTTON' || role === 'button' || x.el.matches('input[type="submit"],input[type="button"]')) && text.includes(lower);
                });
                if (partial) { clickEl(partial.el); return partial.text || label; }
              }
              // Material/Google 按钮有时文本被拆散到子节点，按常见 action 属性兜底。
              const actionNodes = candidates.filter(x => {
                const action = String(x.el.getAttribute('data-mdc-dialog-action') || x.el.getAttribute('jsname') || '').toLowerCase();
                return ['accept','agree','continue','ok','next'].some(k => action.includes(k));
              });
              if (actionNodes.length) {
                clickEl(actionNodes[actionNodes.length - 1].el);
                return actionNodes[actionNodes.length - 1].text || 'action-button';
              }
              // 如果页面明显是 TOS/consent 且只有一个非拒绝主按钮，点击右下/最后一个。
              const body = (document.body.innerText || '').toLowerCase();
              const looksPrompt = ['terms of service','privacy policy','wants to access','continue to','服务条款','隐私权政策','隐私政策','授权'].some(k => body.includes(k));
              if (looksPrompt && candidates.length) {
                const sorted = candidates.slice().sort((a,b) => (a.r.top - b.r.top) || (a.r.left - b.r.left));
                const target = sorted[sorted.length - 1];
                clickEl(target.el);
                return target.text || 'last-visible-prompt-button';
              }
              return '';
            }
            """,
            {"labels": list(labels), "denyLabels": list(GOOGLE_DENY_LABELS)},
        ) or "")
    except Exception as exc:
        msg = str(exc)
        if "Execution context was destroyed" in msg or "Target closed" in msg or "Navigation" in msg:
            return "navigation-after-click"
        return ""



def _cleanup_google_policy_pages(browser: OAuthBrowser) -> None:
    """关闭 Google 隐私/条款标签页，避免 OAuth driver 被干扰。"""
    for page in browser.pages():
        if page.is_closed():
            continue
        url = str(page.url or "")
        if "policies.google.com" in url or "privacy.google.com" in url:
            try:
                page.close()
            except Exception:
                pass


def _is_google_oauth_page(page) -> bool:
    if page.is_closed():
        return False
    url = str(page.url or "")
    # 这个函数在 driver 高频筛选中调用，必须只看 URL，禁止读 DOM，
    # 否则跨站/导航中页面会把整个 OAuth 状态机卡住。
    return "accounts.google.com" in url and "policies.google.com" not in url


def _click_google_account_row_precisely(page, *, email: str, log_fn: Callable[[str], None] = print) -> bool:
    """优先点击 Google account chooser 的真实账号行。

    Google chooser 的正文容器也会包含邮箱文本；如果按 DOM 顺序点第一个
    “包含邮箱”的节点，会点到整页容器而不是账号行。这里优先锁定
    data-identifier/data-email/role=link 这些实际带点击处理的节点。
    """
    target_email = (email or "").strip().lower()
    if not target_email or not _is_google_oauth_page(page):
        return False
    selectors = ("[data-identifier]", "[data-email]", "div[role='link']", "li")
    pattern = re.compile(re.escape(target_email), re.IGNORECASE)
    for selector in selectors:
        try:
            locator = page.locator(selector).filter(has_text=pattern).first
            locator.wait_for(state="visible", timeout=1200)
            try:
                locator.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            box = None
            try:
                box = locator.bounding_box(timeout=1000)
            except Exception:
                box = None
            if box:
                page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                locator.click(timeout=2000, force=True)
            log_fn(f"[GoogleOAuth] 精确点击 Google 账号行: {target_email}")
            time.sleep(2.5)
            return True
        except Exception:
            continue
    return False


def _click_account_or_other(browser: OAuthBrowser, *, email: str, log_fn: Callable[[str], None] = print) -> bool:
    target_email = (email or "").strip().lower()
    labels = [
        (email or "").strip(),
        "使用其他账号", "使用其它账号", "使用其他帐号", "使用其它帐号",
        "Use another account", "Use other account", "Another account", "Different account",
    ]
    labels = [x for x in labels if x]
    for page in browser.pages():
        if not _is_google_oauth_page(page):
            continue
        if target_email and _click_google_account_row_precisely(page, email=email, log_fn=log_fn):
            return True
        try:
            clicked = page.evaluate(
                """
                ({labels, targetEmail}) => {
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    const st = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
                  };
                  const textOf = (el) => (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim();
                  const fireClick = (el) => {
                    el.scrollIntoView({block:'center', inline:'center'});
                    const r = el.getBoundingClientRect();
                    const x = Math.max(2, Math.min(innerWidth - 2, r.left + r.width / 2));
                    const y = Math.max(2, Math.min(innerHeight - 2, r.top + r.height / 2));
                    const target = document.elementFromPoint(x, y) || el;
                    for (const type of ['pointerover','mouseover','pointerdown','mousedown','pointerup','mouseup','click']) {
                      target.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, clientX:x, clientY:y}));
                    }
                  };
                  const clickable = [...document.querySelectorAll('[data-email], [data-identifier], [role=link], [role=button], button, a, li, div')]
                    .filter(el => visible(el) && textOf(el));
                  if (targetEmail) {
                    const accountNodes = clickable
                      .filter(el => {
                        const role = String(el.getAttribute('role') || '').toLowerCase();
                        const dataEmail = String(el.getAttribute('data-email') || el.getAttribute('data-identifier') || '').toLowerCase();
                        const r = el.getBoundingClientRect();
                        return dataEmail || role === 'link' || role === 'button' || el.tagName === 'LI' || getComputedStyle(el).cursor === 'pointer' || (r.width < innerWidth * 0.7 && r.height < innerHeight * 0.5);
                      })
                      .filter(el => !['HTML', 'BODY', 'MAIN'].includes(el.tagName));
                    const score = (el) => {
                      const role = String(el.getAttribute('role') || '').toLowerCase();
                      const dataEmail = String(el.getAttribute('data-email') || el.getAttribute('data-identifier') || '').toLowerCase();
                      const r = el.getBoundingClientRect();
                      let out = 0;
                      if (dataEmail === targetEmail) out += 100;
                      if (role === 'link' || role === 'button') out += 30;
                      if (el.tagName === 'LI') out += 10;
                      if (getComputedStyle(el).cursor === 'pointer') out += 10;
                      out -= Math.min(50, (r.width * r.height) / 10000);
                      return out;
                    };
                    const exactAccount = accountNodes
                      .filter(el => {
                        const dataEmail = String(el.getAttribute('data-email') || el.getAttribute('data-identifier') || '').toLowerCase();
                        const text = textOf(el).toLowerCase();
                        return dataEmail === targetEmail || text.includes(targetEmail);
                      })
                      .sort((a, b) => score(b) - score(a))[0];
                    if (exactAccount) {
                      fireClick(exactAccount);
                      return targetEmail;
                    }
                  }
                  const otherLabels = labels.filter(label => label.toLowerCase() !== targetEmail);
                  for (const label of otherLabels) {
                    const lower = label.toLowerCase();
                    const exact = clickable.find(el => textOf(el).toLowerCase() === lower);
                    if (exact) { fireClick(exact); return label; }
                    const partial = clickable
                      .filter(el => ['BUTTON', 'A', 'LI'].includes(el.tagName) || el.getAttribute('role') === 'button' || el.getAttribute('role') === 'link')
                      .find(el => textOf(el).toLowerCase().includes(lower));
                    if (partial) { fireClick(partial); return label; }
                  }
                  return '';
                }
                """,
                {"labels": labels, "targetEmail": target_email},
            )
            if clicked:
                log_fn(f"[GoogleOAuth] 点击账号/切换账号: {clicked}")
                time.sleep(2.5)
                return True
        except Exception:
            pass
    return False


def _is_account_chooser_for_other_email(page, expected_email: str, body: str = "") -> bool:
    expected = (expected_email or "").strip().lower()
    if not expected or not _is_google_oauth_page(page):
        return False
    url = str(page.url or "").lower()
    text = (body or _body_text(page, timeout=800) or "").lower()
    if expected in text:
        return False
    chooser_markers = (
        "accountchooser",
        "choose an account",
        "use another account",
        "选择账号",
        "使用其他账号",
        "使用其他帐号",
    )
    return any(marker in url or marker in text for marker in chooser_markers)


def _looks_like_google_consent_page(body: str) -> bool:
    """判断当前 Google 页面是否为 OAuth 授权确认页。

    纯 account chooser 也会包含 Privacy Policy / Terms of Service，不能因此
    点击 Terms 链接。只有出现明确授权/回登确认文案时才进入 prompt clicker。
    """
    text = str(body or "").strip()
    lower = text.lower()
    if not text:
        return False
    chooser_markers = (
        "choose an account" in lower,
        "请选择账号" in text,
        "use another account" in lower,
        "使用其他账号" in text,
        "使用其他帐号" in text,
    )
    decisive_markers = (
        "google will allow",
        "access this info about you",
        "you're signing back in",
        "you’re signing back in",
        "you are signing back in",
        "will allow",
        "name and profile picture",
        "email address",
        "review permissions",
        "wants to access",
        "您将登录",
        "正在重新登录",
        "允许",
        "授权",
    )
    has_decisive_marker = any(marker in lower or marker in text for marker in decisive_markers)
    if any(chooser_markers) and not has_decisive_marker:
        return False
    return has_decisive_marker and (
        "continue" in lower
        or "allow" in lower
        or "继续" in text
        or "允许" in text
    )


def _submit_current_step(page, *, log_fn: Callable[[str], None], step: str) -> None:
    try:
        page.keyboard.press("Enter")
        return
    except Exception:
        pass
    clicked = _click_text_or_prompt(page, ("Next", "下一步", "Continue", "继续"))
    if clicked:
        log_fn(f"[GoogleOAuth] 提交 {step}: {clicked}")


def drive_google_oauth(
    browser: OAuthBrowser,
    *,
    email: str = "",
    password: str = "",
    totp_secret: str = "",
    timeout: int = 180,
    log_fn: Callable[[str], None] = print,
    stop_when: Callable[[OAuthBrowser], bool] | None = None,
) -> GoogleOAuthResult:
    """统一处理 Google OAuth 登录页、账号选择、密码页、2FA(TOTP) 和授权提示。"""
    result = GoogleOAuthResult()
    email_attempts = 0
    password_attempts = 0
    totp_attempts = 0
    deadline = time.time() + max(5, int(timeout or 180))
    last_status_log = 0.0
    while time.time() < deadline:
        if stop_when and stop_when(browser):
            return result
        _cleanup_google_policy_pages(browser)
        progressed = False
        google_pages = [p for p in browser.pages() if _is_google_oauth_page(p)]
        if not google_pages:
            time.sleep(0.5)
            continue
        for page in google_pages:
            url = page.url or ""
            result.last_url = url
            body = _body_text(page)
            if body:
                result.last_body = body[:1000]
            if time.time() - last_status_log > 10:
                last_status_log = time.time()
                preview = " | ".join((body or "").splitlines())[:240]
                log_fn(f"[GoogleOAuth] 当前页面: {url} :: {preview}")

            if _is_google_deleted_account_page(body):
                result.blocked_on_password = True
                _mark_google_account_invalid(email, reason="google_account_deleted", log_fn=log_fn)
                log_fn("[GoogleOAuth] Google 页面提示账号已被删除，终止 OAuth 任务")
                raise RuntimeError(
                    f"Google OAuth 账号已被删除，已标记账号池失效: "
                    f"{email or 'unknown'} :: {body[:300]}"
                )

            if "signin/rejected" in url or "无法登录" in body or "contact your domain administrator" in body.lower():
                result.blocked_on_password = True
                log_fn("[GoogleOAuth] Google 登录被域策略拒绝，停止 OAuth driver")
                return result

            if body and ("Unified API gateway" in body or ("Command Palette" in body and "Get Started" in body and "APIs" in body)):
                try:
                    frames = [str(getattr(f, 'url', '') or '') for f in list(getattr(page, 'frames', []) or [])]
                except Exception:
                    frames = []
                log_fn(f"[GoogleOAuth] Google URL 下出现目标站 shell，继续等待 Google/回调；frames={frames[:6]}")
                time.sleep(2)
                continue

            # 1) Credential inputs first. Identifier pages often contain text like
            # "continue to <app>" and "Next"; treating them as consent pages causes
            # endless empty Next clicks without filling the email.
            try:
                if email and email_attempts < 3:
                    filled_email = (
                        _fill_google_input_js(page, ['input[type="email"]', 'input[name="identifier"]', '#identifierId'], email)
                        or _fill_google_input_playwright(page, ['input[type="email"]', 'input[name="identifier"]', '#identifierId'], email)
                    )
                    if filled_email:
                        _submit_current_step(page, log_fn=log_fn, step="email")
                        result.email_submitted = True
                        email_attempts += 1
                        log_fn("[GoogleOAuth] 已提交 Google 邮箱")
                        time.sleep(3)
                        progressed = True
                        continue
            except Exception as exc:
                log_fn(f"[GoogleOAuth] Google 邮箱输入失败: {exc!r}")

            try:
                if _is_password_challenge(page):
                    if not password:
                        result.blocked_on_password = True
                        log_fn("[GoogleOAuth] 已停在密码页，但当前任务没有传入 Google 密码，停止误点下一步")
                        time.sleep(1)
                        continue
                    if password_attempts >= 3:
                        result.blocked_on_password = True
                        time.sleep(1)
                        continue
                    filled_password = (
                        _fill_google_input_playwright(page, ['input[type="password"]', 'input[name="Passwd"]'], password)
                        or _fill_google_input_js(page, ['input[type="password"]', 'input[name="Passwd"]'], password)
                    )
                    if filled_password:
                        _submit_current_step(page, log_fn=log_fn, step="password")
                        result.password_submitted = True
                        password_attempts += 1
                        log_fn("[GoogleOAuth] 已提交 Google 密码")
                        time.sleep(7)
                        progressed = True
                        continue
                    result.blocked_on_password = True
                    log_fn("[GoogleOAuth] 密码页存在，但未能写入密码，禁止继续误点 consent/Next")
                    log_fn(f"[GoogleOAuth] 密码输入框现场: {_google_input_debug(page, ['input[type=\"password\"]', 'input[name=\"Passwd\"]', 'input'])}")
                    time.sleep(1)
                    continue
            except Exception:
                pass

            # 1b) 2FA / TOTP challenge after password submission.
            # Google 2FA 页 URL 含 challenge/totp，body 含 "2-step verification"。
            # 用 core.oauth_2fa 的策略化驱动处理 TOTP 输入框填写 + 提交。
            if totp_secret and result.password_submitted and totp_attempts < 3:
                try:
                    from core.oauth_2fa import TwoFactorStrategy, is_2fa_challenge, drive_oauth_2fa_step
                    google_2fa_strategy = TwoFactorStrategy(
                        challenge_url_pattern="challenge/",
                        challenge_url_exclude=["challenge/pwd"],
                        challenge_body_hints=["2-step verification", "两步验证"],
                        totp_input_selectors=['input[name="totpPin"]', 'input[id="totpPin"]'],
                        exclude_input_selectors=['#ootp-pin', 'input[name="Pin"]'],
                        try_another_way_labels=["Try another way", "尝试其他方式"],
                        authenticator_option_labels=["Google Authenticator", "身份验证器"],
                        submit_labels=["Next", "下一步"],
                        selection_url_pattern="challenge/selection",
                        log_prefix="[GoogleOAuth-2FA]",
                    )
                    if is_2fa_challenge(page, google_2fa_strategy):
                        log_fn("[GoogleOAuth] 检测到 2FA 验证页，开始处理 TOTP")
                        handled, fatal, totp_attempts = drive_oauth_2fa_step(
                            page, google_2fa_strategy, totp_secret=totp_secret,
                            totp_attempts=totp_attempts, max_attempts=3, log_fn=log_fn,
                        )
                        if handled:
                            result.totp_submitted = True
                            log_fn("[GoogleOAuth] TOTP 已提交")
                            time.sleep(5)
                            progressed = True
                            continue
                        if fatal:
                            log_fn("[GoogleOAuth] 2FA 处理失败（TOTP secret 无效或尝试次数耗尽）")
                            time.sleep(1)
                            continue
                except Exception as exc_2fa:
                    log_fn(f"[GoogleOAuth] 2FA 处理异常: {exc_2fa!r}")

            # 2) Credential pages are not captcha pages. Some Google identifier
            # pages contain hidden fields such as #ca; do not stop automation while
            # a normal email/password input is still visible.
            if _has_google_credential_input(page):
                time.sleep(0.5)
                continue

            if _is_google_captcha_page(page):
                result.blocked_on_password = True
                log_fn("[GoogleOAuth] Google 页面出现验证码/文字验证，停止自动 OAuth")
                time.sleep(1)
                continue

            # 3) Account chooser with a different cached Chrome profile account is
            # not a consent page. Keep forcing "Use another account" and never
            # click Continue/Allow for authuser=0 unless the expected email was
            # submitted or visibly selected.
            if email and not result.email_submitted and _is_account_chooser_for_other_email(page, email, body):
                if _click_account_or_other(browser, email=email, log_fn=log_fn):
                    progressed = True
                    continue
                result.blocked_on_password = True
                log_fn(f"[GoogleOAuth] 账号选择器未出现目标邮箱，拒绝授权旧账号: expected={email}")
                time.sleep(1)
                continue

            # 4) Consent / ToS / speedbump before normal account chooser. Google sometimes
            # keeps the accountchooser URL while rendering the permission screen;
            # in that state, clicking the email row repeatedly does nothing useful.
            chooser_text = "accountchooser" in str(url).lower() or "choose an account" in str(body).lower() or "请选择账号" in str(body)
            if _looks_like_google_consent_page(body) or (not chooser_text and any(hint in body for hint in GOOGLE_CONSENT_HINTS)) or "gaplustos" in url or "speedbump" in url:
                clicked = _click_text_or_prompt(page)
                if clicked:
                    result.clicked_prompt = True
                    log_fn(f"[GoogleOAuth] 点击 Google 提示: {clicked}")
                    time.sleep(3)
                    progressed = True
                    continue

            # 5) Account chooser after explicit inputs and consent pages are absent.
            if email and not result.email_submitted and ("accountchooser" in url or "signin" in url or "oauth" in url):
                if _click_account_or_other(browser, email=email, log_fn=log_fn):
                    progressed = True
                    continue
        if not progressed:
            time.sleep(1)
    return result


def google_oauth_snapshot(browser: OAuthBrowser, *, limit: int = 1500) -> list[dict[str, str]]:
    """采集 Google OAuth 失败现场，便于判断是否遇到验证码/风控。"""
    pages: list[dict[str, str]] = []
    for page in browser.pages():
        if page.is_closed():
            continue
        try:
            pages.append({"url": str(page.url or ""), "body": _body_text(page, timeout=1200)[:limit]})
        except Exception:
            pass
    return pages
