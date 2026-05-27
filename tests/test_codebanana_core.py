import unittest

import requests
from unittest.mock import MagicMock, patch

from requests.cookies import RequestsCookieJar

from platforms.codebanana.core import CodeBananaClient


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class CodeBananaClientTests(unittest.TestCase):
    @patch("requests.Session")
    def test_fetch_csrf_token_reads_csrf_token_field(self, session_cls):
        session = MagicMock()
        session.get.return_value = _Response({"csrfToken": "csrf-123"})
        session_cls.return_value = session

        client = CodeBananaClient()

        self.assertEqual(client.fetch_csrf_token(), "csrf-123")

    @patch("requests.Session")
    def test_fetch_csrf_token_non_2xx_raises_runtime_error(self, session_cls):
        session = MagicMock()
        session.get.return_value = _Response({"error": "down"}, status_code=500)
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(RuntimeError, r"GET /api/auth/csrf"):
            client.fetch_csrf_token()

    @patch("requests.Session")
    def test_fetch_csrf_token_invalid_json_raises_value_error(self, session_cls):
        session = MagicMock()
        session.get.return_value = _Response(ValueError("bad json"), status_code=200)
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(ValueError, r"GET /api/auth/csrf"):
            client.fetch_csrf_token()

    @patch("requests.Session")
    def test_fetch_csrf_token_missing_csrf_token_raises(self, session_cls):
        session = MagicMock()
        session.get.return_value = _Response({})
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(ValueError, "/api/auth/csrf"):
            client.fetch_csrf_token()

    @patch("requests.Session")
    def test_fetch_csrf_token_transport_failure_raises_runtime_error(self, session_cls):
        session = MagicMock()
        session.get.side_effect = requests.exceptions.Timeout("timeout")
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(RuntimeError, r"GET /api/auth/csrf"):
            client.fetch_csrf_token()


    @patch("requests.Session")
    def test_login_extracts_next_auth_session_cookie(self, session_cls):
        session = MagicMock()
        jar = RequestsCookieJar()
        jar.set("__Secure-next-auth.session-token", "session-abc")
        session.cookies = jar
        session.post.return_value = _Response({})
        session_cls.return_value = session

        client = CodeBananaClient()
        result = client.login(email="demo@example.com", password="secret", csrf_token="csrf-123")

        self.assertEqual(result["session_token"], "session-abc")
        self.assertEqual(result["cookies"]["__Secure-next-auth.session-token"], "session-abc")
        session.post.assert_called_once_with(
            "https://www.codebanana.com/api/auth/callback/credentials",
            data={
                "email": "demo@example.com",
                "password": "secret",
                "csrfToken": "csrf-123",
                "redirect": "false",
                "json": "true",
                "callbackUrl": "https://www.codebanana.com",
            },
            timeout=30,
        )

    @patch("requests.Session")
    def test_login_without_session_cookie_raises(self, session_cls):
        session = MagicMock()
        session.cookies = RequestsCookieJar()
        session.post.return_value = _Response({})
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(RuntimeError, "/api/auth/callback/credentials"):
            client.login(email="demo@example.com", password="secret", csrf_token="csrf-123")

    @patch("requests.Session")
    def test_send_verification_code_uses_expected_endpoint_and_payload(self, session_cls):
        session = MagicMock()
        session.post.return_value = _Response({"success": True})
        session_cls.return_value = session

        client = CodeBananaClient(timeout=11)
        result = client.send_verification_code("demo@example.com", "banana")

        self.assertEqual(result["success"], True)
        session.post.assert_called_once_with(
            "https://www.codebanana.com/api/auth/send-verification-code",
            json={"email": "demo@example.com", "username": "banana"},
            timeout=11,
        )

    @patch("requests.Session")
    def test_send_verification_code_payload_success_false_raises(self, session_cls):
        session = MagicMock()
        session.post.return_value = _Response({"success": False, "message": "blocked"})
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(RuntimeError, r"POST /api/auth/send-verification-code"):
            client.send_verification_code("demo@example.com", "banana")

    @patch("requests.Session")
    def test_verify_and_register_uses_expected_endpoint_and_payload(self, session_cls):
        session = MagicMock()
        session.post.return_value = _Response({"ok": True})
        session_cls.return_value = session

        client = CodeBananaClient(timeout=12)
        result = client.verify_and_register(
            email="demo@example.com",
            username="banana",
            password="secret",
            code="1234",
        )

        self.assertEqual(result["ok"], True)
        session.post.assert_called_once_with(
            "https://www.codebanana.com/api/auth/verify-and-register",
            json={
                "email": "demo@example.com",
                "username": "banana",
                "password": "secret",
                "verificationCode": "1234",
            },
            timeout=12,
        )

    @patch("requests.Session")
    def test_verify_and_register_payload_error_raises(self, session_cls):
        session = MagicMock()
        session.post.return_value = _Response({"error": "invalid code"})
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(RuntimeError, r"POST /api/auth/verify-and-register"):
            client.verify_and_register("demo@example.com", "banana", "secret", "1234")

    @patch("requests.Session")
    def test_login_and_fetch_session_returns_jwt_and_session_json(self, session_cls):
        session = MagicMock()
        jar = RequestsCookieJar()
        jar.set("__Secure-next-auth.session-token", "session-abc")
        session.cookies = jar
        session.get.side_effect = [
            _Response({"csrfToken": "csrf-123"}),
            _Response({"jwtToken": "jwt-456", "user": {"id": "user-1"}}),
        ]
        session.post.return_value = _Response({})
        session_cls.return_value = session

        client = CodeBananaClient()
        result = client.login_and_fetch_session(email="demo@example.com", password="secret")

        self.assertEqual(result["csrf_token"], "csrf-123")
        self.assertEqual(result["session_token"], "session-abc")
        self.assertEqual(result["cookies"]["__Secure-next-auth.session-token"], "session-abc")
        self.assertEqual(result["session_json"]["jwtToken"], "jwt-456")
        self.assertEqual(result["session_json"]["user"]["id"], "user-1")

    @patch("requests.Session")
    def test_login_and_fetch_session_raises_when_missing_jwt_token(self, session_cls):
        session = MagicMock()
        jar = RequestsCookieJar()
        jar.set("__Secure-next-auth.session-token", "session-abc")
        session.cookies = jar
        session.get.side_effect = [
            _Response({"csrfToken": "csrf-123"}),
            _Response({"user": {"id": "user-1"}}),
        ]
        session.post.return_value = _Response({})
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(ValueError, r"/api/auth/session"):
            client.login_and_fetch_session(email="demo@example.com", password="secret")

    @patch("requests.Session")
    def test_ensure_username_available_returns_true_and_uses_expected_payload(self, session_cls):
        session = MagicMock()
        session.post.return_value = _Response({"available": True})
        session_cls.return_value = session

        client = CodeBananaClient(timeout=9)
        result = client.ensure_username_available("banana_user")

        self.assertTrue(result)
        session.post.assert_called_once_with(
            "https://www.codebanana.com/api/auth/check-username",
            json={"username": "banana_user"},
            timeout=9,
        )

    @patch("requests.Session")
    def test_ensure_username_available_raises_when_unavailable(self, session_cls):
        session = MagicMock()
        session.post.return_value = _Response({"available": False, "message": "Username already taken"})
        session_cls.return_value = session

        client = CodeBananaClient()

        with self.assertRaisesRegex(ValueError, "username unavailable"):
            client.ensure_username_available("taken_user")


if __name__ == "__main__":
    unittest.main()
