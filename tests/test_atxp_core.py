import datetime as dt
import json

from requests.cookies import RequestsCookieJar

from platforms.atxp.core import AtxpClient


PRIVY_HEADERS = {
    "privy-client": "react-auth:3.10.2",
    "privy-app-id": "cma1jnfkk01mml20n6fyvsmll",
    "privy-client-id": "client-WY6L6ApVtkaEUHas1qqZ4fFKtQuUF67ghGYyd82oa5PTw",
    "privy-ui": "t",
    "origin": "https://accounts.atxp.ai",
    "referer": "https://accounts.atxp.ai/",
    "content-type": "application/json",
}


class _DummyResponse:
    def __init__(self, payload=None, text=None, cookies=None):
        self._payload = payload or {}
        self.text = text if text is not None else json.dumps(self._payload)
        self.cookies = cookies or RequestsCookieJar()

    def json(self):
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
    client = AtxpClient(session=session)

    result = client.send_privy_code(email="demo@example.com", ca_id="ca-1")

    assert result == {"status": "ok"}
    method, url, kwargs = session.calls[0]
    assert method == "post"
    assert url == "https://auth.privy.io/api/v1/passwordless/init"
    assert kwargs["headers"] == {**PRIVY_HEADERS, "privy-ca-id": "ca-1"}
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
    assert kwargs["headers"] == {**PRIVY_HEADERS, "privy-ca-id": "ca-1"}
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
    session.queue_post(_DummyResponse(payload={"wallet": {"address": "0xwallet"}}))
    session.queue_get(
        _DummyResponse(
            payload={"connectionToken": "conn-1"},
            text='{"connectionToken":"conn-1"}',
        )
    )
    client = AtxpClient(session=session)

    bundle = client.fetch_atxp_bundle(token="privy-token")

    assert bundle["me"] == {"accountId": "acct-1", "embeddedWallets": [{"address": "0xembedded"}]}
    assert bundle["wallet_info"] == {"wallet": {"address": "0xwallet"}}
    assert bundle["account_id"] == "acct-1"
    assert bundle["wallet_address"] == "0xwallet"
    assert bundle["connection_token"] == "conn-1"
    assert bundle["connection_text"] == '{"connectionToken":"conn-1"}'
    assert session.calls == [
        (
            "get",
            "https://accounts.atxp.ai/me",
            {
                "headers": {
                    "authorization": "Bearer privy-token",
                    "content-type": "application/json",
                    "origin": "https://accounts.atxp.ai",
                    "referer": "https://accounts.atxp.ai/",
                },
                "timeout": 30.0,
            },
        ),
        (
            "post",
            "https://accounts.atxp.ai/wallets/ensure",
            {
                "headers": {
                    "authorization": "Bearer privy-token",
                    "content-type": "application/json",
                    "origin": "https://accounts.atxp.ai",
                    "referer": "https://accounts.atxp.ai/",
                },
                "json": {},
                "timeout": 30.0,
            },
        ),
        (
            "get",
            "https://accounts.atxp.ai/connection-token",
            {
                "headers": {
                    "authorization": "Bearer privy-token",
                    "content-type": "application/json",
                    "origin": "https://accounts.atxp.ai",
                    "referer": "https://accounts.atxp.ai/",
                },
                "timeout": 30.0,
            },
        ),
    ]


def test_probe_gateway_connection_uses_connection_string_and_top_level_data():
    session = _DummySession()
    session.queue_get(
        _DummyResponse(
            payload={"data": [{"id": "gpt-4.1-mini"}, {"id": "gpt-4.1-nano"}]}
        )
    )
    client = AtxpClient(session=session)

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
            "https://llm.atxp.ai/v1/models",
            {
                "headers": {"authorization": f"Bearer {connection_string}"},
                "timeout": 30.0,
            },
        )
    ]
