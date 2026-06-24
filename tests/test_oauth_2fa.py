"""core/oauth_2fa.py 策略化 2FA 骨架测试：strategy 可注入、判定/切换/提交流程。"""
from core.oauth_2fa import TwoFactorStrategy, is_2fa_challenge, has_totp_input


def _make_strategy():
    return TwoFactorStrategy(
        challenge_url_pattern="challenge/",
        challenge_url_exclude=["challenge/pwd"],
        challenge_body_hints=["2-step verification"],
        totp_input_selectors=['input[name="totpPin"]', 'input[id="totpPin"]'],
        exclude_input_selectors=['#ootp-pin', 'input[name="Pin"]'],
        try_another_way_labels=["Try another way"],
        authenticator_option_labels=["Google Authenticator"],
        submit_labels=["Next"],
        selection_url_pattern="challenge/selection",
        log_prefix="[Test2FA]",
    )


class _FakePage:
    """最小 mock page，只支持 url / evaluate。"""

    def __init__(self, url: str = "", evaluate_result=None, body_text: str = ""):
        self.url = url
        self._evaluate_result = evaluate_result
        self._body_text = body_text

    def inner_text(self, selector, timeout=800):
        return self._body_text

    def evaluate(self, expression, arg=None):
        if callable(self._evaluate_result):
            return self._evaluate_result(expression, arg)
        return self._evaluate_result


def test_strategy_is_injectable():
    """不同平台可注入不同 strategy，字段独立。"""
    google = _make_strategy()
    microsoft = TwoFactorStrategy(
        challenge_url_pattern="identity/2fa",
        totp_input_selectors=['input[name="otc"]'],
        log_prefix="[MSOAuth]",
    )
    assert google.challenge_url_pattern == "challenge/"
    assert microsoft.challenge_url_pattern == "identity/2fa"
    assert google.totp_input_selectors != microsoft.totp_input_selectors
    assert google.log_prefix == "[Test2FA]"
    assert microsoft.log_prefix == "[MSOAuth]"


def test_is_2fa_challenge_url_match():
    strategy = _make_strategy()
    page = _FakePage(url="https://accounts.google.com/v3/signin/challenge/totp?x=1")
    assert is_2fa_challenge(page, strategy) is True


def test_is_2fa_challenge_excludes_password_page():
    strategy = _make_strategy()
    page = _FakePage(url="https://accounts.google.com/v3/signin/challenge/pwd?x=1")
    assert is_2fa_challenge(page, strategy) is False


def test_is_2fa_challenge_body_hint_fallback():
    strategy = _make_strategy()
    page = _FakePage(url="https://example.com/some/path", body_text="2-Step Verification to help keep your account safe")
    assert is_2fa_challenge(page, strategy) is True


def test_is_2fa_challenge_no_match():
    strategy = _make_strategy()
    page = _FakePage(url="https://example.com/normal", body_text="Welcome back")
    assert is_2fa_challenge(page, strategy) is False


def test_has_totp_input_true_when_precise_selector_visible():
    strategy = _make_strategy()
    # evaluate 返回 True 模拟找到了 totpPin 可见输入框
    page = _FakePage(evaluate_result=True)
    assert has_totp_input(page, strategy) is True


def test_has_totp_input_false_when_no_visible_input():
    strategy = _make_strategy()
    page = _FakePage(evaluate_result=False)
    assert has_totp_input(page, strategy) is False
