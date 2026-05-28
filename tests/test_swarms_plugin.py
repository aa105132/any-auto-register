from __future__ import annotations

from unittest.mock import Mock

from platforms.swarms.core import SwarmsClient


def test_swarms_client_uses_current_api_key_trpc_paths_and_cookie_auth():
    client = SwarmsClient(log_fn=lambda _msg: None)
    client._access_token = "access-token"
    client._refresh_token = "refresh-token"
    client._user_id = "user-id"
    client._user_info = {"id": "user-id", "email": "demo@swarms.test"}

    response = Mock(status_code=200, text='{"result":{"data":{"json":{"key":"sk-current-swarms-demo-12345678901234567890"}}}}')
    response.json.return_value = {
        "result": {
            "data": {
                "json": {"key": "sk-current-swarms-demo-12345678901234567890"}
            }
        }
    }
    client.session.post = Mock(return_value=response)

    result = client.create_api_key("auto-register")

    assert result["key"] == "sk-current-swarms-demo-12345678901234567890"
    url = client.session.post.call_args.args[0]
    kwargs = client.session.post.call_args.kwargs
    assert url == "https://swarms.world/api/trpc/apiKey.addApiKey"
    assert kwargs["json"] == {"json": {"name": "auto-register"}}
    assert "sb-db-auth-token" in kwargs["cookies"]
    cookie_value = kwargs["cookies"]["sb-db-auth-token"]
    assert cookie_value.startswith("base64-")
    assert '"access_token":"access-token"' in client._decode_supabase_storage_cookie(cookie_value)


def test_swarms_client_lists_api_keys_from_current_trpc_shape():
    client = SwarmsClient(log_fn=lambda _msg: None)
    client._access_token = "access-token"
    client._refresh_token = "refresh-token"
    client._user_info = {"id": "user-id"}

    response = Mock(status_code=200, text='{}')
    response.json.return_value = {
        "result": {
            "data": {
                "json": [
                    {"id": "key-id", "name": "auto-register", "key": "sk-listed-swarms-demo-12345678901234567890"}
                ]
            }
        }
    }
    client.session.get = Mock(return_value=response)

    keys = client.list_api_keys()

    assert keys[0]["key"] == "sk-listed-swarms-demo-12345678901234567890"
    assert client.session.get.call_args.args[0] == "https://swarms.world/api/trpc/apiKey.getApiKeys"
    assert "sb-db-auth-token" in client.session.get.call_args.kwargs["cookies"]



def test_swarms_client_uses_marketplace_server_action_for_signup():
    client = SwarmsClient(log_fn=lambda _msg: None)

    get_response = Mock(status_code=200, text='<html></html>')
    post_response = Mock(
        status_code=200,
        text='1:"/?status=Success!&status_description=Please%20check%20your%20email%20for%20a%20confirmation%20link."',
    )
    client.session.get = Mock(return_value=get_response)
    client.session.post = Mock(return_value=post_response)

    result = client.signup('demo@swarms.test', 'Password123!')

    assert result['signup_method'] == 'swarms_server_action'
    assert result['email'] == 'demo@swarms.test'
    assert client.session.get.call_args.args[0] == 'https://swarms.world/signin/signup'
    post_url = client.session.post.call_args.args[0]
    post_kwargs = client.session.post.call_args.kwargs
    assert post_url == 'https://swarms.world/signin/signup'
    assert post_kwargs['headers']['Next-Action']
    assert post_kwargs['headers']['Accept'] == 'text/x-component'
    field_names = set(post_kwargs['files'].keys())
    assert {'1_email', '1_password', '1_fingerprint', '0'} <= field_names


def test_swarms_client_updates_username_with_current_trpc_shape():
    client = SwarmsClient(log_fn=lambda _msg: None)
    client._access_token = 'access-token'
    client._refresh_token = 'refresh-token'
    client._user_info = {'id': 'user-id'}

    response = Mock(status_code=200, text='{}')
    response.json.return_value = {
        'result': {'data': {'json': {'username': 'sw_auto_123'}}}
    }
    client.session.post = Mock(return_value=response)

    result = client.update_username('sw_auto_123')

    assert result['username'] == 'sw_auto_123'
    assert client.session.post.call_args.args[0] == 'https://swarms.world/api/trpc/main.updateUsername'
    assert client.session.post.call_args.kwargs['json'] == {'json': {'username': 'sw_auto_123'}}


def test_swarms_post_login_completes_profile_before_api_key():
    from platforms.swarms.protocol_mailbox import SwarmsProtocolMailboxWorker

    events: list[str] = []

    class FakeClient:
        user_id = 'user-id'
        access_token = 'access-token'
        refresh_token = 'refresh-token'
        cookies = {'sb-db-auth-token': '{}'}

        def get_user(self):
            events.append('get_user')
            return {'id': 'user-id', 'email': 'demo@swarms.test', 'user_metadata': {}}

        def ensure_profile(self, *, email: str, full_name: str = ''):
            events.append('ensure_profile')
            return {'username': 'demo_swarms', 'full_name': 'Auto Register'}

        def get_credit(self):
            events.append('get_credit')
            return {'credit': 5}

        def create_api_key(self, name: str = 'auto-register'):
            events.append('create_api_key')
            return {'key': 'sk-swarms-demo-12345678901234567890'}

        def list_api_keys(self):
            events.append('list_api_keys')
            return []

    worker = SwarmsProtocolMailboxWorker(log_fn=lambda _msg: None)
    worker.client = FakeClient()

    result = worker._post_login('demo@swarms.test', 'Password123!')

    assert result['api_key'] == 'sk-swarms-demo-12345678901234567890'
    assert result['username'] == 'demo_swarms'
    assert events.index('ensure_profile') < events.index('create_api_key')



def test_verification_link_extractor_accepts_swarms_supabase_verify_link():
    from core.base_mailbox import _extract_verification_link

    link = 'https://db.swarms.world/auth/v1/verify?token=abc123&type=signup&redirect_to=https%3A%2F%2Fswarms.world%2F'
    body = f'Confirm your signup for Swarms: <a href="{link}">Confirm</a>'

    assert _extract_verification_link(body, keyword='swarms') == link



def test_swarms_client_rejects_signup_server_action_error_response():
    client = SwarmsClient(log_fn=lambda _msg: None)
    get_response = Mock(status_code=200, text='<html></html>')
    post_response = Mock(status_code=200, text='1:"/?status=Error&status_description=blocked"')
    client.session.get = Mock(return_value=get_response)
    client.session.post = Mock(return_value=post_response)

    try:
        client.signup('demo@swarms.test', 'Password123!')
    except RuntimeError as exc:
        assert '注册 action 返回异常' in str(exc)
    else:
        raise AssertionError('signup should reject status=Error responses')


def test_verification_link_extractor_trims_markdown_closing_bracket_after_callback_path():
    from core.base_mailbox import _extract_verification_link

    raw = 'https://swarms.world/auth/callback]?code=abc-123'
    body = f'Confirm your Swarms signup: [{raw}]({raw})'

    assert _extract_verification_link(body, keyword='swarms') == 'https://swarms.world/auth/callback?code=abc-123'


def test_swarms_uses_browser_registration_adapter_when_executor_is_headed(monkeypatch):
    from core.base_identity import IdentityMaterial
    from core.base_platform import RegisterConfig
    from core.registration import RegistrationResult
    from platforms.swarms.plugin import SwarmsPlatform

    platform = SwarmsPlatform(config=RegisterConfig(executor_type='headed'))
    platform._resolve_identity = lambda email=None, require_email=True: IdentityMaterial(email='demo@swarms.test')

    class FakeBrowserFlow:
        def __init__(self, adapter):
            self.adapter = adapter

        def run(self, ctx):
            assert ctx.email == 'demo@swarms.test'
            return RegistrationResult(
                email='demo@swarms.test',
                password=ctx.password,
                user_id='user-id',
                token='sk-browser-demo-12345678901234567890',
                extra={'api_key': 'sk-browser-demo-12345678901234567890'},
            )

    monkeypatch.setattr('core.base_platform.BrowserRegistrationFlow', FakeBrowserFlow)

    account = platform.register(email='demo@swarms.test', password='Password123!')

    assert account.token == 'sk-browser-demo-12345678901234567890'


def test_swarms_client_prefers_callback_session_cookie_for_trpc_auth():
    client = SwarmsClient(log_fn=lambda _msg: None)
    client._access_token = 'direct-token'
    client._refresh_token = 'direct-refresh'
    client.session.cookies.set(
        'sb-db-auth-token',
        'base64-real-browser-cookie',
        domain='.swarms.world',
        path='/',
    )

    assert client._trpc_cookies()['sb-db-auth-token'] == 'base64-real-browser-cookie'


def test_swarms_protocol_worker_uses_confirmation_link_session_before_password_login():
    from platforms.swarms.protocol_mailbox import SwarmsProtocolMailboxWorker

    events: list[str] = []

    class FakeClient:
        access_token = ''
        refresh_token = ''
        user_id = 'user-id'
        cookies = {'sb-db-auth-token': 'base64-callback-cookie'}

        def signup(self, email, password):
            events.append('signup')
            return {'ok': True}

        def parse_verification_params(self, url):
            events.append('parse_verification_params')
            return {'token_hash': 'abc123', 'type': 'signup'}

        def verify_email_link(self, url):
            events.append('verify_email_link')
            self.access_token = 'callback-access-token'
            self.refresh_token = 'callback-refresh-token'
            return {'ok': True, 'auth_cookie': True}

        def verify_email(self, token_hash, signup_type='signup'):
            events.append('verify_email')
            raise AssertionError('不应绕过确认链接去直接 POST Supabase verify')

        def login(self, email, password):
            events.append('login')
            raise AssertionError('确认链接已拿到登录态时不应再用密码登录覆盖 cookie 会话')

        def get_user(self):
            events.append('get_user')
            return {'id': 'user-id', 'email': 'demo@swarms.test', 'user_metadata': {}}

        def ensure_profile(self, *, email: str, full_name: str = ''):
            events.append('ensure_profile')
            return {'username': 'demo_swarms', 'full_name': 'Auto Register'}

        def wait_for_credit(self, *, min_credit: float = 0.01, timeout: float = 20, interval: float = 2):
            events.append('wait_for_credit')
            return {'data': 5}

        def create_api_key(self, name: str = 'auto-register'):
            events.append('create_api_key')
            return {'key': 'sk-swarms-demo-12345678901234567890'}

        def list_api_keys(self):
            events.append('list_api_keys')
            return []

    worker = SwarmsProtocolMailboxWorker(log_fn=lambda _msg: None)
    worker.client = FakeClient()

    result = worker.run(
        email='demo@swarms.test',
        password='Password123!',
        verification_link_callback=lambda: 'https://db.swarms.world/auth/v1/verify?token=abc123&type=signup&redirect_to=https%3A%2F%2Fswarms.world%2Fauth%2Fcallback',
    )

    assert result['api_key'] == 'sk-swarms-demo-12345678901234567890'
    assert 'verify_email_link' in events
    assert 'verify_email' not in events
    assert 'login' not in events
    assert events.index('verify_email_link') < events.index('get_user')



def test_swarms_client_parses_nested_hash_from_verify_redirect_url():
    client = SwarmsClient(log_fn=lambda _msg: None)

    result = client._session_from_redirect_url(
        'https://swarms.world/auth/callback?next=/platform#access_token=tok&refresh_token=ref&expires_in=172800&token_type=bearer'
    )

    assert result['access_token'] == 'tok'
    assert result['refresh_token'] == 'ref'
    assert result['expires_in'] == 172800



def test_swarms_signup_allows_rsc_chunks_that_only_contain_generic_error_text():
    client = SwarmsClient(log_fn=lambda _msg: None)

    get_response = Mock(status_code=200, text='<html></html>')
    post_response = Mock(
        status_code=200,
        text='2:"$Sreact.fragment"\n4:I["static/chunks/page.js","error boundary chunk"]',
    )
    client.session.get = Mock(return_value=get_response)
    client.session.post = Mock(return_value=post_response)

    result = client.signup('demo@swarms.test', 'Password123!')

    assert result['ok'] is True
    assert result['signup_method'] == 'swarms_server_action'



def test_swarms_signup_rejects_url_encoded_error_redirect():
    client = SwarmsClient(log_fn=lambda _msg: None)

    get_response = Mock(status_code=200, text='<html></html>')
    post_response = Mock(
        status_code=200,
        text='1:"/signin/signup?error=Sign-up%20limit%20reached.&error_description=Please%20wait%2024%20hours%20before%20creating%20another%20account"',
    )
    client.session.get = Mock(return_value=get_response)
    client.session.post = Mock(return_value=post_response)

    try:
        client.signup('demo@swarms.test', 'Password123!')
    except RuntimeError as exc:
        assert 'Sign-up limit reached' in str(exc)
        assert 'Please wait 24 hours' in str(exc)
    else:
        raise AssertionError('signup should reject URL encoded error redirects')



def test_swarms_client_loads_access_token_from_existing_auth_cookie():
    client = SwarmsClient(log_fn=lambda _msg: None)
    cookie_value = client._encode_supabase_storage_cookie('{"access_token":"tok","refresh_token":"ref","user":{"id":"user-id"}}')
    client.session.cookies.set('sb-db-auth-token', cookie_value, domain='swarms.world', path='/')

    payload = client._load_auth_cookie_session()

    assert payload['access_token'] == 'tok'
    assert client.access_token == 'tok'
    assert client.refresh_token == 'ref'
    assert client.user_id == 'user-id'
