from __future__ import annotations

from unittest.mock import Mock

from core.base_platform import AccountStatus, RegisterConfig
from platforms.thesys.core import (
    CHAT_COMPLETIONS_URL,
    DEFAULT_FREE_MODEL,
    MODELS_URL,
    ThesysClient,
    extract_api_key,
)
from platforms.thesys.plugin import ThesysPlatform
from platforms.thesys.protocol_mailbox import ThesysProtocolMailboxWorker


def test_thesys_extract_api_key_from_create_response():
    payload = {"id": "key-id", "apiKey": "c1_" + "A" * 90, "nested": {"ignored": "x"}}
    assert extract_api_key(payload) == "c1_" + "A" * 90


def test_thesys_client_otp_verify_uses_console_app_and_device_id():
    client = ThesysClient(log_fn=lambda _msg: None)
    response = Mock(status_code=200, ok=True, text='{"ok":true}', headers={"content-type": "application/json"})
    response.json.return_value = {"ok": True}
    client.session.post = Mock(return_value=response)

    result = client.verify_otp(pre_auth_session_id="pre-1", code="123456", device_id="dev-1")

    assert result["ok"] is True
    url = client.session.post.call_args.args[0]
    body = client.session.post.call_args.kwargs["json"]
    assert url.endswith("/auth/otp.verify")
    assert body == {
        "preAuthSessionId": "pre-1",
        "deviceId": "dev-1",
        "userInputCode": "123456",
        "app": "console",
    }


def test_thesys_worker_pure_protocol_chain(monkeypatch):
    worker = ThesysProtocolMailboxWorker(log_fn=lambda _msg: None)
    client = Mock()
    client.generate_email_otp.return_value = {"preAuthSessionId": "pre-1"}
    client.extract_pre_auth_session_id = ThesysClient.extract_pre_auth_session_id
    client.verify_otp.return_value = {"cookies": {"sAccessToken": "access", "sRefreshToken": "refresh"}}
    client.user_me.return_value = {"id": "user-1", "email": "demo@example.com"}
    client.list_orgs.return_value = [{"id": "org-1", "name": "Demo Org"}]
    client.create_api_key.return_value = {"id": "key-1", "apiKey": "c1_" + "B" * 90}
    client.list_api_keys.return_value = {"apiKeys": [{"id": "key-1"}]}
    client.get_billing.return_value = {"availableBalance": 0}
    client.verify_models.return_value = {"ok": True, "status": 200, "data": {"object": "list", "data": []}}
    client.probe_chat_completion.return_value = {"ok": True, "status": 201, "model": DEFAULT_FREE_MODEL, "content_preview": "OK"}
    worker.client = client

    result = worker.run(email="demo@example.com", password="local-pass", otp_callback=lambda: "123456")

    assert result["api_key"] == "c1_" + "B" * 90
    assert result["org_id"] == "org-1"
    client.generate_email_otp.assert_called_once_with("demo@example.com")
    client.verify_otp.assert_called_once()
    client.create_api_key.assert_called_once_with(org_id="org-1", name="auto-register")
    client.probe_chat_completion.assert_called_once()


def test_thesys_platform_maps_openai_compatible_metadata():
    platform = ThesysPlatform(RegisterConfig(extra={}))
    result = platform._map_result(
        {
            "email": "demo@example.com",
            "password": "local-pass",
            "user_id": "user-1",
            "org_id": "org-1",
            "org": {"id": "org-1", "name": "Demo Org"},
            "api_key": "c1_" + "C" * 90,
            "api_verification": {"ok": True, "status": 200},
            "chat_verification": {"ok": True, "status": 201},
        }
    )

    assert result.status == AccountStatus.REGISTERED
    assert result.token == "c1_" + "C" * 90
    assert result.extra["openai_compatible_api_base"] == "https://api.thesys.dev/v1/embed"
    assert result.extra["chat_completions_url"] == CHAT_COMPLETIONS_URL
    assert result.extra["models_url"] == MODELS_URL
    assert DEFAULT_FREE_MODEL in result.extra["free_models"]


def test_thesys_platform_adapter_uses_otp_spec_and_extra_model():
    platform = ThesysPlatform(RegisterConfig(extra={"thesys_verify_model": "model-x", "thesys_verify_chat": "false"}))
    adapter = platform.build_protocol_mailbox_adapter()

    assert adapter.otp_spec is not None
    assert adapter.otp_spec.code_pattern == r"(?<!\d)(\d{6})(?!\d)"
    assert platform._verify_model(Mock(extra={"thesys_verify_model": "model-x"})) == "model-x"
    assert platform._verify_chat(Mock(extra={"thesys_verify_chat": "false"})) is False
