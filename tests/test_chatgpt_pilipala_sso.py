import unittest

from core.base_identity import normalize_oauth_provider
from core.oauth_browser import oauth_provider_label
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.protocol_sso import (
    _build_sso_form_data,
    _pick_sso_form,
    is_pilipala_sso_provider,
    resolve_pilipala_sso_email,
)


class ChatGPTPilipalaSSOTests(unittest.TestCase):
    def test_provider_aliases_normalize_to_pilipala_sso(self):
        self.assertEqual(normalize_oauth_provider("sso"), "pilipala_sso")
        self.assertEqual(normalize_oauth_provider("pilipala-sso"), "pilipala_sso")
        self.assertTrue(is_pilipala_sso_provider("edu.pilipala.store"))
        self.assertEqual(oauth_provider_label("sso"), "Pilipala SSO")

    def test_chatgpt_advertises_pilipala_sso(self):
        self.assertIn("pilipala_sso", ChatGPTPlatform.supported_oauth_providers)

    def test_resolve_sso_email_uses_hint_prefix(self):
        email, prefix = resolve_pilipala_sso_email(email_hint="demo@edu.pilipala.store")
        self.assertEqual(email, "demo@edu.pilipala.store")
        self.assertEqual(prefix, "demo")

    def test_sso_form_parser_fills_prefix_and_password(self):
        html = """
        <form action="/login" method="post">
          <input type="hidden" name="csrf" value="token">
          <input name="prefix">
          <input type="password" name="password">
          <button>Continue</button>
        </form>
        """
        form = _pick_sso_form(html)
        data = _build_sso_form_data(form, prefix="abc123", password="ciallo")
        self.assertEqual(data["csrf"], "token")
        self.assertEqual(data["prefix"], "abc123")
        self.assertEqual(data["password"], "ciallo")

    def test_openai_sso_selection_form_submits_connection(self):
        html = """
        <form action="/sso" method="post">
          <input type="hidden" name="username" value='{"value":"abc@edu.pilipala.store","kind":"email"}'>
          <input type="hidden" name="screen_hint" value="login">
          <button name="ssoConnection" value='{"connection_name":"conn_demo","title":"Ciallo~"}'>Ciallo~</button>
        </form>
        """
        form = _pick_sso_form(html)
        data = _build_sso_form_data(form, prefix="abc", password="ciallo")
        self.assertEqual(data["screen_hint"], "login")
        self.assertIn("conn_demo", data["ssoConnection"])
        self.assertNotIn("prefix", data)
        self.assertNotIn("password", data)


if __name__ == "__main__":
    unittest.main()
