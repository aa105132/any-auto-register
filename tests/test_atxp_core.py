import datetime as dt
import json

import pytest
from requests.cookies import RequestsCookieJar

from platforms.atxp.core import AtxpClient


CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def _privy_headers(ca_id, base_url="https://accounts.atxp.ai"):
    return {
        "user-agent": CHROME_UA,
        "accept": "application/json",
        "content-type": "application/json",
        "origin": base_url,
        "referer": f"{base_url}/",
        "privy-ca-id": ca_id,
        **PRIVY_HEADERS_BASE,
    }


def _atxp_headers(token, base_url="https://accounts.atxp.ai"):
    return {
        "authorization": f"Bearer {token}",
        "user-agent": CHROME_UA,
        "accept": "application/json",
        "content-type": "application/json",
        "origin": base_url,
        "referer": f"{base_url}/",
    }


def _gateway_headers(connection_string):
    return {
        "authorization": f"Bearer {connection_string}",
        "user-agent": CHROME_UA,
        "accept": "application/json",
        "content-type": "application/json",
    }


PRIVY_HEADERS_BASE = {
    "privy-client": "react-auth:3.10.2",
    "privy-app-id": "cma1jnfkk01mml20n6fyvsmll",
    "privy-client-id": "client-WY6L6ApVtkaEUHas1qqZ4fFKtQuUF67ghGYyd82oa5PTw",
    "privy-ui": "t",
}


class _DummyResponse:
    def __init__(self, payload=None, text=None, cookies=None, json_error=None):
        self._payload = {} if payload is None else payload
        self._json_error = json_error
        self.text = text if text is not None else json.dumps(self._payload)
        self.cookies = cookies or RequestsCookieJar()

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    def raise_for_status(self):
        return None


class _DummySession:
    def __init__(self):
        self.calls = []
        self.cookies = RequestsCookieJar()
        self.proxies = {}
        self._responses = {"post": [], "get": []}

    def queue_post(self, response):
        self._responses["post"].append(response)

    def queue_get(self, response):
        self._responses["get"].append(response)

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return self._responses["post"].pop(0)

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return self._responses["get"].pop(0)


def test_send_privy_code_uses_passwordless_init_and_privy_headers():
    session = _DummySession()
    session.queue_post(_DummyResponse(payload={"status": "ok"}))
    client = AtxpClient(session=session, base_url="https://accounts.example.test")

    result = client.send_privy_code(email="demo@example.com", ca_id="ca-1")

    assert result == {"status": "ok"}
    method, url, kwargs = session.calls[0]
    assert method == "post"
    assert url == "https://auth.privy.io/api/v1/passwordless/init"
    assert kwargs["headers"] == _privy_headers(
        ca_id="ca-1", base_url="https://accounts.example.test"
    )
    assert kwargs["json"] == {"email": "demo@example.com"}


def test_authenticate_privy_returns_payload_and_reads_privy_refresh_token_cookie():
    session = _DummySession()
    session.cookies.set("privy-refresh-token", "refresh-123")
    session.queue_post(_DummyResponse(payload={"token": "privy-token", "user": {"id": "did:privy:1"}}))
    client = AtxpClient(session=session)

    result = client.authenticate_privy(email="demo@example.com", code="123456", ca_id="ca-1")

    assert result == {
        "token": "privy-token",
        "user": {"id": "did:privy:1"},
        "refresh_token": "refresh-123",
    }
    method, url, kwargs = session.calls[0]
    assert method == "post"
    assert url == "https://auth.privy.io/api/v1/passwordless/authenticate"
    assert kwargs["headers"] == _privy_headers(ca_id="ca-1")
    assert kwargs["json"] == {
        "email": "demo@example.com",
        "code": "123456",
        "mode": "login-or-sign-up",
    }


def test_authenticate_privy_falls_back_to_refresh_token_cookie_name():
    session = _DummySession()
    session.cookies.set("refresh_token", "refresh-456")
    session.queue_post(_DummyResponse(payload={"token": "privy-token"}))
    client = AtxpClient(session=session)

    result = client.authenticate_privy(email="demo@example.com", code="123456", ca_id="ca-1")

    assert result["refresh_token"] == "refresh-456"


def test_fetch_atxp_bundle_requests_real_endpoints_and_extracts_protocol_fields():
    session = _DummySession()
    session.queue_get(
        _DummyResponse(
            payload={"accountId": "acct-1", "embeddedWallets": [{"address": "0xembedded"}]}
        )
    )
    session.queue_post(_DummyResponse(payload={"address": "0xwallet"}))
    session.queue_get(
        _DummyResponse(
            payload={"connection_token": "ConnTokenSnake123456"},
            text='{"connection_token":"ConnTokenSnake123456"}',
        )
    )
    client = AtxpClient(session=session, base_url="https://accounts.example.test")

    bundle = client.fetch_atxp_bundle(token="privy-token")

    assert bundle["me"] == {"accountId": "acct-1", "embeddedWallets": [{"address": "0xembedded"}]}
    assert bundle["wallet_info"] == {"address": "0xwallet"}
    assert bundle["account_id"] == "acct-1"
    assert bundle["wallet_address"] == "0xwallet"
    assert bundle["connection_token"] == "ConnTokenSnake123456"
    assert bundle["connection_text"] == '{"connection_token":"ConnTokenSnake123456"}'
    assert session.calls == [
        (
            "get",
            "https://accounts.example.test/me",
            {
                "headers": _atxp_headers(
                    token="privy-token", base_url="https://accounts.example.test"
                ),
                "timeout": 30.0,
            },
        ),
        (
            "post",
            "https://accounts.example.test/wallets/ensure",
            {
                "headers": _atxp_headers(
                    token="privy-token", base_url="https://accounts.example.test"
                ),
                "json": {},
                "timeout": 30.0,
            },
        ),
        (
            "get",
            "https://accounts.example.test/connection-token",
            {
                "headers": _atxp_headers(
                    token="privy-token", base_url="https://accounts.example.test"
                ),
                "timeout": 30.0,
            },
        ),
    ]


def test_fetch_atxp_bundle_falls_back_to_embedded_wallet_and_connection_token_url_text():
    session = _DummySession()
    session.queue_get(
        _DummyResponse(
            payload={"accountId": "acct-1", "embeddedWallets": [{"address": "0xembedded"}]}
        )
    )
    session.queue_post(_DummyResponse(payload={}))
    session.queue_get(
        _DummyResponse(
            text="https://accounts.atxp.ai?connection_token=ConnTokenUrl123456&account_id=acct-1",
            json_error=ValueError("not json"),
        )
    )
    client = AtxpClient(session=session)

    bundle = client.fetch_atxp_bundle(token="privy-token")

    assert bundle["wallet_address"] == "0xembedded"
    assert bundle["connection_token"] == "ConnTokenUrl123456"
    assert (
        bundle["connection_text"]
        == "https://accounts.atxp.ai?connection_token=ConnTokenUrl123456&account_id=acct-1"
    )


def test_fetch_atxp_bundle_accepts_plain_text_connection_token():
    session = _DummySession()
    session.queue_get(_DummyResponse(payload={"accountId": "acct-1"}))
    session.queue_post(_DummyResponse(payload={"address": "0xwallet"}))
    session.queue_get(
        _DummyResponse(
            text="ConnTokenPlain123456",
            json_error=ValueError("not json"),
        )
    )
    client = AtxpClient(session=session)

    bundle = client.fetch_atxp_bundle(token="privy-token")

    assert bundle["connection_token"] == "ConnTokenPlain123456"


def test_fetch_atxp_bundle_rejects_non_object_json_payloads():
    session = _DummySession()
    session.queue_get(_DummyResponse(payload=[]))
    client = AtxpClient(session=session)

    with pytest.raises(TypeError, match="ATXP /me 响应必须是 JSON object，实际为 list"):
        client.fetch_atxp_bundle(token="privy-token")


def test_fetch_atxp_bundle_raises_stable_error_when_connection_token_unparseable():
    session = _DummySession()
    session.queue_get(_DummyResponse(payload={"accountId": "acct-1"}))
    session.queue_post(_DummyResponse(payload={"address": "0xwallet"}))
    session.queue_get(
        _DummyResponse(
            text="connection token missing",
            json_error=ValueError("not json"),
        )
    )
    client = AtxpClient(session=session)

    with pytest.raises(
        ValueError, match="ATXP /connection-token 返回格式无法提取 connection token"
    ):
        client.fetch_atxp_bundle(token="privy-token")


def test_probe_gateway_connection_uses_connection_string_and_top_level_data():
    session = _DummySession()
    session.queue_get(
        _DummyResponse(
            payload={"data": [{"id": "gpt-4.1-mini"}, {"id": "gpt-4.1-nano"}]}
        )
    )
    client = AtxpClient(session=session, gateway_url="https://gateway.example.test")

    connection_string = (
        "https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1"
    )
    result = client.probe_gateway_connection(connection_string=connection_string)

    assert result["success"] is True
    dt.datetime.fromisoformat(result["checked_at"].replace("Z", "+00:00"))
    assert result["model"] == "gpt-4.1-mini"
    assert result["model_count"] == 2
    assert session.calls == [
        (
            "get",
            "https://gateway.example.test/v1/models",
            {
                "headers": _gateway_headers(connection_string),
                "timeout": 30.0,
            },
        )
    ]
