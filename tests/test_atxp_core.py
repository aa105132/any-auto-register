import datetime as dt

from platforms.atxp.core import AtxpClient


class _DummyResponse:
    def __init__(self, payload=None, cookies=None):
        self._payload = payload or {}
        self.cookies = cookies or {}

    def json(self):
        return self._payload


class _DummySession:
    def __init__(self):
        self.calls = []
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


def test_authenticate_privy_extracts_token_and_refresh_token():
    session = _DummySession()
    session.queue_post(
        _DummyResponse(
            payload={"token": "token-from-json"},
            cookies={"refresh_token": "refresh-from-cookie"},
        )
    )
    client = AtxpClient(session=session)

    result = client.authenticate_privy(code="123456")

    assert result["token"] == "token-from-json"
    assert result["refresh_token"] == "refresh-from-cookie"


def test_fetch_atxp_bundle_extracts_required_fields():
    session = _DummySession()
    session.queue_get(
        _DummyResponse(
            payload={
                "me": {"id": "u-1"},
                "wallet_info": {
                    "account_id": "acc-123",
                    "wallet_address": "0xabc",
                    "connection_token": "conn-token-1",
                    "connection_text": "connect to gateway",
                },
            }
        )
    )
    client = AtxpClient(session=session)

    bundle = client.fetch_atxp_bundle(token="token-1")

    assert bundle["me"]["id"] == "u-1"
    assert bundle["wallet_info"]["wallet_address"] == "0xabc"
    assert bundle["account_id"] == "acc-123"
    assert bundle["wallet_address"] == "0xabc"
    assert bundle["connection_token"] == "conn-token-1"
    assert bundle["connection_text"] == "connect to gateway"


def test_probe_gateway_connection_returns_first_model_id():
    session = _DummySession()
    session.queue_get(
        _DummyResponse(
            payload={
                "success": True,
                "models": [{"id": "model-first"}, {"id": "model-second"}],
            }
        )
    )
    client = AtxpClient(session=session)

    result = client.probe_gateway_connection(connection_token="ct-1")

    assert result["success"] is True
    dt.datetime.fromisoformat(result["checked_at"])
    assert result["model"] == "model-first"
    assert result["model_count"] == 2
