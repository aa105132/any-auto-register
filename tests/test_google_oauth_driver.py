import inspect
import unittest
from unittest.mock import patch

from core import google_oauth


class GoogleOAuthDriverTests(unittest.TestCase):
    def test_drive_google_oauth_handles_inputs_before_account_or_prompt_clicks(self):
        source = inspect.getsource(google_oauth.drive_google_oauth)
        email_pos = source.index('input[type="email"]')
        password_pos = source.index('input[type="password"]')
        account_pos = source.index('_click_account_or_other')
        prompt_pos = source.index('_click_text_or_prompt')
        self.assertLess(email_pos, account_pos)
        self.assertLess(password_pos, prompt_pos)

    def test_password_challenge_is_checked_before_captcha_detection(self):
        source = inspect.getsource(google_oauth.drive_google_oauth)
        password_pos = source.index('_is_password_challenge(page)')
        captcha_pos = source.index('_is_google_captcha_page(page)')
        self.assertLess(password_pos, captcha_pos)

    def test_credential_input_guard_runs_before_captcha_detection(self):
        source = inspect.getsource(google_oauth.drive_google_oauth)
        credential_guard_pos = source.index('_has_google_credential_input(page)')
        captcha_pos = source.index('_is_google_captcha_page(page)')
        self.assertLess(credential_guard_pos, captcha_pos)


    def test_prompt_clicker_handles_tos_and_action_buttons(self):
        source = inspect.getsource(google_oauth._click_text_or_prompt)
        self.assertIn("terms of service", source)
        self.assertIn("data-mdc-dialog-action", source)
        self.assertIn("last-visible-prompt-button", source)

    def test_consent_hints_include_terms_pages(self):
        self.assertIn("terms of service", google_oauth.GOOGLE_CONSENT_HINTS)
        self.assertIn("服务条款", google_oauth.GOOGLE_CONSENT_HINTS)

    def test_captcha_detection_requires_visible_captcha_input(self):
        source = inspect.getsource(google_oauth._is_google_captcha_page)
        self.assertIn('visible(el)', source)
        self.assertIn('!el.disabled', source)
        self.assertNotIn('"captcha"', source)

    def test_accountchooser_other_email_guard_runs_before_prompt_click(self):
        source = inspect.getsource(google_oauth.drive_google_oauth)
        guard_pos = source.index('_is_account_chooser_for_other_email')
        prompt_pos = source.index('_click_text_or_prompt')
        self.assertLess(guard_pos, prompt_pos)

    def test_accountchooser_guard_detects_other_cached_account(self):
        class _Page:
            def __init__(self):
                self.url = 'https://accounts.google.com/v3/signin/accountchooser'
            def is_closed(self):
                return False

        body = 'Choose an account to continue to Gumloop old@example.com Use another account'
        self.assertTrue(google_oauth._is_account_chooser_for_other_email(_Page(), 'new@example.com', body))
        self.assertFalse(google_oauth._is_account_chooser_for_other_email(_Page(), 'old@example.com', body))

    def test_accountchooser_terms_text_is_not_consent_page(self):
        body = (
            'Sign in with Google\nChoose an account\nto continue to Zo Computer\n'
            'user@example.com\nUse another account\nBefore using this app, you can review '
            'Zo Computer’s Privacy Policy and Terms of Service.'
        )
        self.assertFalse(google_oauth._looks_like_google_consent_page(body))

    def test_signing_back_in_page_is_consent_page(self):
        body = (
            'Sign in with Google\nYou’re signing back in to Zo Computer\n'
            'user@example.com\nReview Zo Computer’s Privacy Policy and Terms of Service\n'
            'Cancel\nContinue'
        )
        self.assertTrue(google_oauth._looks_like_google_consent_page(body))

    def test_deleted_account_page_aborts_immediately_and_marks_invalid(self):
        class _Browser:
            def pages(self):
                return [type('Page', (), {'url': 'https://accounts.google.com/v3/signin/identifier'})()]

        deleted_body = (
            '使用 Google 账号登录\n'
            '账号已被删除\n'
            '此账号最近已被删除，但或许可以再恢复。要尝试恢复此账号，请点击下一步。'
        )
        with patch('core.google_oauth._cleanup_google_policy_pages', lambda _browser: None),             patch('core.google_oauth._is_google_oauth_page', return_value=True),             patch('core.google_oauth._body_text', return_value=deleted_body),             patch('core.google_oauth._mark_google_account_invalid', return_value=True) as mark_invalid,             patch('core.google_oauth.time.sleep', return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                google_oauth.drive_google_oauth(
                    _Browser(),
                    email='deleted@example.com',
                    timeout=10,
                    log_fn=lambda _msg: None,
                )

        self.assertIn('账号已被删除', str(ctx.exception))
        self.assertEqual(mark_invalid.call_count, 1)
        self.assertEqual(mark_invalid.call_args.args[0], 'deleted@example.com')
        self.assertEqual(mark_invalid.call_args.kwargs['reason'], 'google_account_deleted')


    def test_password_challenge_prefers_playwright_input_before_js(self):
        source = inspect.getsource(google_oauth.drive_google_oauth)
        password_block = source[source.index('if _is_password_challenge(page):'):source.index('if _has_google_credential_input(page):')]
        self.assertLess(
            password_block.index('_fill_google_input_playwright'),
            password_block.index('_fill_google_input_js'),
        )



if __name__ == '__main__':
    unittest.main()

