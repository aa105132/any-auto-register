from __future__ import annotations

import inspect
import unittest

from platforms.blendspace import browser_oauth


class BlendSpaceOAuthTests(unittest.TestCase):
    def test_google_prompt_clicker_has_no_mojibake_selector(self):
        source = inspect.getsource(browser_oauth._click_google_prompt)
        self.assertNotIn('???', source)
        self.assertNotIn('??', source)
        self.assertIn('我同意', browser_oauth.GOOGLE_PROMPT_LABELS)
        self.assertIn('继续', browser_oauth.GOOGLE_PROMPT_LABELS)
        self.assertIn('允许', browser_oauth.GOOGLE_PROMPT_LABELS)

    def test_google_credentials_handle_inputs_before_prompt_click(self):
        source = inspect.getsource(browser_oauth._try_google_password_login)
        email_pos = source.index('input[type="email"]')
        password_pos = source.index('input[type="password"]')
        prompt_pos = source.index('_has_google_credential_input')
        self.assertLess(email_pos, prompt_pos)
        self.assertLess(password_pos, prompt_pos)

    def test_wait_returns_when_chat_page_already_has_session(self):
        class FakePage:
            url = 'https://blendspace.ai/chat'

            def is_closed(self):
                return False

            def wait_for_load_state(self, *_args, **_kwargs):
                return None

            def evaluate(self, _script, key):
                self.requested_key = key
                return {'key': key, 'value': 'session_from_chat_page_1234567890'}

        class FakeBrowser:
            def pages(self):
                return [FakePage()]

        session, final_url = browser_oauth._wait_for_local_storage_session(FakeBrowser(), timeout=1)
        self.assertEqual(session, 'session_from_chat_page_1234567890')
        self.assertEqual(final_url, 'https://blendspace.ai/chat')

    def test_blendspace_passes_reuse_existing_cdp_flag_explicitly(self):
        import platforms.blendspace.plugin as blendspace_plugin

        runner = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("reuse_existing_cdp", runner)
        self.assertIn("reuse_existing_cdp=reuse_existing_cdp", runner)
        plugin_source = inspect.getsource(blendspace_plugin.BlendSpacePlatform._run_protocol_oauth)
        self.assertIn("oauth_reuse_existing_cdp", plugin_source)
        self.assertIn("reuse_existing_cdp=", plugin_source)


if __name__ == '__main__':
    unittest.main()
