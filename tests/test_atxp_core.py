import datetime as dt
import json

import pytest
import requests
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
    def __init__(self, payload=None, text=None, cookies=None, json_error=None, status_code=200):
        self._payload = {} if payload is None else payload
        self._json_error = json_error
        self.text = text if text is not None else json.dumps(self._payload)
        self.cookies = cookies or RequestsCookieJar()
        self.status_code = status_code

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} Server Error: Service Unavailable for url: https://llm.atxp.ai/v1/models",
                response=self,
            )
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


@pytest.mark.parametrize(
    "plain_text",
    [
        "connection_pending_123456",
        "token_expired_retry_01",
    ],
)
def test_fetch_atxp_bundle_rejects_status_like_plain_text_tokens(plain_text):
    session = _DummySession()
    session.queue_get(_DummyResponse(payload={"accountId": "acct-1"}))
    session.queue_post(_DummyResponse(payload={"address": "0xwallet"}))
    session.queue_get(
        _DummyResponse(
            text=plain_text,
            json_error=ValueError("not json"),
        )
    )
    client = AtxpClient(session=session)

    with pytest.raises(
        ValueError, match="ATXP /connection-token 返回格式无法提取 connection token"
    ):
        client.fetch_atxp_bundle(token="privy-token")


def test_fetch_atxp_bundle_rejects_unrelated_long_string_fields_without_token():
    session = _DummySession()
    session.queue_get(_DummyResponse(payload={"accountId": "acct-1"}))
    session.queue_post(_DummyResponse(payload={"address": "0xwallet"}))
    session.queue_get(
        _DummyResponse(
            payload={"message": "ThisIsJustALongString1234567890", "status": "pending"},
            text='{"message":"ThisIsJustALongString1234567890","status":"pending"}',
        )
    )
    client = AtxpClient(session=session)

    with pytest.raises(
        ValueError, match="ATXP /connection-token 返回格式无法提取 connection token"
    ):
        client.fetch_atxp_bundle(token="privy-token")


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


def test_probe_gateway_connection_rejects_non_list_data_payload():
    session = _DummySession()
    session.queue_get(_DummyResponse(payload={"data": {"id": "gpt-4.1-mini"}}))
    client = AtxpClient(session=session)

    with pytest.raises(
        TypeError, match="ATXP Gateway /v1/models.data 必须是 list，实际为 dict"
    ):
        client.probe_gateway_connection(
            connection_string="https://accounts.atxp.ai?connection_token=conn-1"
        )


def test_probe_gateway_connection_retries_on_503(monkeypatch):
    session = _DummySession()
    session.queue_get(_DummyResponse(payload={"detail": "verifying"}, status_code=503))
    session.queue_get(
        _DummyResponse(
            payload={"data": [{"id": "gpt-4.1-mini"}]},
            status_code=200,
        )
    )
    sleeps: list[float] = []
    monkeypatch.setattr("platforms.atxp.core.time.sleep", lambda seconds: sleeps.append(seconds))
    client = AtxpClient(session=session)

    result = client.probe_gateway_connection(connection_string="https://accounts.atxp.ai?connection_token=conn-1")

    assert result["model"] == "gpt-4.1-mini"
    assert sleeps == [3.0]
    assert [call[0] for call in session.calls] == ["get", "get"]


def test_complete_clowdbot_tasks_returns_retry_summary(monkeypatch):
    """Mock the OIDC login + clowdbot API calls to verify orchestration logic."""
    client = AtxpClient(session=_DummySession())

    fake_cb_session = object()  # opaque sentinel — only passed through to _clowdbot_api

    # _clowdbot_oidc_login just returns the fake session
    monkeypatch.setattr(client, "_clowdbot_oidc_login", lambda privy_token: fake_cb_session)

    # Track API calls and return canned responses
    api_calls: list[tuple[str, str]] = []
    api_responses: dict[str, dict] = {
        "/auth/check": {"authenticated": True},
        "/user": {"id": "user-1"},
        "/onboarding/steps": {
            "steps": [],
            "completedCount": 0,
            "totalSteps": 2,
        },
        "POST /onboarding/complete/create_clowdbot": {
            "status": "completed",
            "creditDisplay": "+10 IOU",
        },
        "/onboarding/check-email/demo": {
            "available": True,
            "email": "demo@atxp.email",
        },
        "POST /onboarding/complete/claim_email": {
            "status": "completed",
            "creditDisplay": "+10 IOU",
        },
        # Second read of /onboarding/steps (final state)
        "final /onboarding/steps": {
            "steps": [
                {"slug": "create_clowdbot", "completed": True},
                {"slug": "claim_email", "completed": True},
            ],
            "completedCount": 2,
            "totalSteps": 2,
            "totalEarnedDisplay": "+20 IOU",
        },
        "/instance": {
            "instances": [{"id": "instance-from-api"}],
        },
    }

    steps_call_count = [0]

    def _fake_clowdbot_api(_cb_session, path, *, method="GET", json_body=None):
        api_calls.append((method, path))
        key = f"{method} {path}" if method != "GET" else path

        # /onboarding/steps is called twice: initial + final
        if path == "/onboarding/steps":
            steps_call_count[0] += 1
            if steps_call_count[0] >= 2:
                return dict(api_responses["final /onboarding/steps"])
            return dict(api_responses["/onboarding/steps"])

        if key in api_responses:
            return dict(api_responses[key])
        raise RuntimeError(f"Unmocked clowdbot API call: {key}")

    monkeypatch.setattr(client, "_clowdbot_api", _fake_clowdbot_api)

    result = client.complete_clowdbot_tasks(
        privy_token="privy-token",
        account_id="acct-1",
        email="demo@example.com",
    )

    assert result == {
        "instance_id": "instance-from-api",
        "claimed_agent_email": "demo@atxp.email",
        "create_clowdbot_completed": True,
        "claim_email_completed": True,
        "reward_progress": {"completed": 2, "total": 2, "earned": "+20 IOU"},
    }
