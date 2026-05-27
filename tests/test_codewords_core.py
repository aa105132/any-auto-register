from platforms.codewords.core import CodewordsClient


def test_extract_session_token_does_not_fallback_to_tracking_cookie():
    cookies = {
        "__Host-next-auth.csrf-token": "csrf-value",
        "cw_session_id": "tracking-value",
        "snitcher_device_id": "device-value",
    }

    assert CodewordsClient.extract_session_token(cookies) == ""


def test_extract_session_token_accepts_auth_session_cookie():
    cookies = {
        "cw_session_id": "tracking-value",
        "__Secure-authjs.session-token": "real-session-token",
    }

    assert CodewordsClient.extract_session_token(cookies) == "real-session-token"


def test_session_email_requires_user_email():
    assert CodewordsClient.session_email({}) == ""
    assert CodewordsClient.session_email({"user": {}}) == ""
    assert CodewordsClient.session_email({"user": {"email": "demo@example.com"}}) == "demo@example.com"
