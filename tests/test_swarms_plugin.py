from __future__ import annotations

from unittest.mock import Mock

from platforms.swarms.core import SwarmsClient


def test_swarms_client_forces_explicit_proxy_per_request():
    client = SwarmsClient(proxy='http://proxy.local:8080', log_fn=lambda _msg: None)
    response = Mock(status_code=200, text='203.0.113.9')
    client.session.get = Mock(return_value=response)

    result = client.log_proxy_probe()

    assert result == '203.0.113.9'
    assert client.session.trust_env is False
    assert client.session.get.call_args.args[0] == 'https://api.ipify.org'
    assert client.session.get.call_args.kwargs['proxies'] == {
        'http': 'http://proxy.local:8080',
        'https': 'http://proxy.local:8080',
    }


def test_swarms_signup_uses_explicit_proxy_for_page_action_and_chunks():
    client = SwarmsClient(proxy='http://proxy.local:8080', log_fn=lambda _msg: None)

    page_response = Mock(status_code=200, text='<script src="/_next/static/chunks/signin.js"></script>')
    chunk_response = Mock(status_code=200, text='createServerReference("abcdef1234567890abcdef1234567890abcdef12",x,"signUp")')
    post_response = Mock(
        status_code=200,
        text='1:"/?status=Success!&status_description=Please%20check%20your%20email%20for%20a%20confirmation%20link."',
    )
    client.session.get = Mock(side_effect=[page_response, chunk_response])
    client.session.post = Mock(return_value=post_response)

    result = client.signup('demo@swarms.test', 'Password123!')

    assert result['signup_method'] == 'swarms_server_action'
    for call in client.session.get.call_args_list:
        assert call.kwargs['proxies'] == {'http': 'http://proxy.local:8080', 'https': 'http://proxy.local:8080'}
    assert client.session.post.call_args.kwargs['proxies'] == {
        'http': 'http://proxy.local:8080',
        'https': 'http://proxy.local:8080',
    }


def test_swarms_signup_extracts_action_id_from_current_next_chunk_shape():
    client = SwarmsClient(log_fn=lambda _msg: None)

    page_response = Mock(
        status_code=200,
        text='<script src="/_next/static/chunks/app/(site)/signin/%5Bid%5D/page-live.js"></script>',
    )
    chunk_response = Mock(
        status_code=200,
        text='let c=(0,o.createServerReference)("607a9bf372984e91426801fb2a24efeb8a37e0505b",o.callServer,void 0,o.findSourceMapURL,"signUp");',
    )
    post_response = Mock(
        status_code=200,
        text='1:"/?status=Success!&status_description=Please%20check%20your%20email%20for%20a%20confirmation%20link."',
    )
    client.session.get = Mock(side_effect=[page_response, chunk_response])
    client.session.post = Mock(return_value=post_response)

    result = client.signup('demo@swarms.test', 'Password123!')

    assert result['action_id'] == '607a9bf372984e91426801fb2a24efeb8a37e0505b'
    assert client.session.post.call_args.kwargs['headers']['Next-Action'] == (
        '607a9bf372984e91426801fb2a24efeb8a37e0505b'
    )


def test_swarms_signup_retries_when_server_action_not_found():
    client = SwarmsClient(log_fn=lambda _msg: None)

    first_page = Mock(
        status_code=200,
        text='<script src="/_next/static/chunks/app/(site)/signin/%5Bid%5D/page-old.js"></script>',
    )
    first_chunk = Mock(
        status_code=200,
        text='let c=(0,o.createServerReference)("111111111111111111111111111111111111111111",o.callServer,void 0,o.findSourceMapURL,"signUp");',
    )
    second_page = Mock(
        status_code=200,
        text='<script src="/_next/static/chunks/app/(site)/signin/%5Bid%5D/page-new.js"></script>',
    )
    second_chunk = Mock(
        status_code=200,
        text='let c=(0,o.createServerReference)("222222222222222222222222222222222222222222",o.callServer,void 0,o.findSourceMapURL,"signUp");',
    )
    first_post = Mock(status_code=404, text='Server action not found.')
    second_post = Mock(
        status_code=200,
        text='1:"/?status=Success!&status_description=Please%20check%20your%20email%20for%20a%20confirmation%20link."',
    )
    client.session.get = Mock(side_effect=[first_page, first_chunk, second_page, second_chunk])
    client.session.post = Mock(side_effect=[first_post, second_post])

    result = client.signup('demo@swarms.test', 'Password123!')

    assert result['action_id'] == '222222222222222222222222222222222222222222'
    assert client.session.post.call_count == 2
    first_headers = client.session.post.call_args_list[0].kwargs['headers']
    second_headers = client.session.post.call_args_list[1].kwargs['headers']
    assert first_headers['Next-Action'] == '111111111111111111111111111111111111111111'
    assert second_headers['Next-Action'] == '222222222222222222222222222222222222222222'


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


def test_swarms_client_ensure_profile_sets_random_full_name_by_default():
    client = SwarmsClient(log_fn=lambda _msg: None)
    client.get_profile = Mock(return_value={'username': 'demo_swarms', 'full_name': ''})
    client.update_username = Mock()
    client.update_full_name = Mock(return_value={'full_name': 'Swarms User 1234'})

    profile = client.ensure_profile(email='demo@swarms.test')

    assert profile['username'] == 'demo_swarms'
    client.update_username.assert_not_called()
    client.update_full_name.assert_called_once()
    nickname = client.update_full_name.call_args.args[0]
    assert nickname
    assert nickname != 'Auto Register'


def test_swarms_post_login_completes_profile_before_api_key():
    from platforms.swarms.protocol_mailbox import SwarmsProtocolMailboxWorker

    events: list = []

    class FakeClient:
        user_id = 'user-id'
        access_token = 'access-token'
        refresh_token = 'refresh-token'
        cookies = {'sb-db-auth-token': '{}'}

        def get_user(self):
            events.append('get_user')
            return {'id': 'user-id', 'email': 'demo@swarms.test', 'user_metadata': {}}

        def ensure_profile(self, *, email: str, full_name: str = ''):
            events.append(('ensure_profile', full_name))
            assert email == 'demo@swarms.test'
            assert full_name
            assert full_name != 'Auto Register'
            return {'username': 'demo_swarms', 'full_name': full_name}

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
    event_names = [event[0] if isinstance(event, tuple) else event for event in events]
    assert event_names.index('ensure_profile') < event_names.index('create_api_key')



def test_swarms_post_login_fails_without_credit_before_creating_api_key():
    from platforms.swarms.protocol_mailbox import SwarmsProtocolMailboxWorker
    import pytest

    events: list[tuple] = []

    class FakeClient:
        user_id = 'user-id'
        access_token = 'access-token'
        refresh_token = 'refresh-token'
        cookies = {'sb-db-auth-token': '{}'}

        def get_user(self):
            events.append(('get_user',))
            return {'id': 'user-id', 'email': 'demo@swarms.test', 'user_metadata': {}}

        def ensure_profile(self, *, email: str, full_name: str = ''):
            events.append(('ensure_profile', full_name))
            assert email == 'demo@swarms.test'
            assert full_name
            assert full_name != 'Auto Register'
            return {'username': 'demo_swarms', 'full_name': full_name}

        def wait_for_credit(self, *, min_credit: float = 0.01, timeout: float = 20, interval: float = 2):
            events.append(('wait_for_credit', min_credit, timeout, interval))
            return {'data': 0}

        def create_api_key(self, name: str = 'auto-register'):
            events.append(('create_api_key', name))
            raise AssertionError('额度未到账时不应创建 API Key')

        def list_api_keys(self):
            events.append(('list_api_keys',))
            raise AssertionError('额度未到账时不应查询/兜底 API Key')

    worker = SwarmsProtocolMailboxWorker(log_fn=lambda _msg: None)
    worker.client = FakeClient()

    with pytest.raises(RuntimeError, match='额度'):
        worker._post_login('demo@swarms.test', 'Password123!')

    assert any(event[0] == 'ensure_profile' and event[1] and event[1] != 'Auto Register' for event in events)
    assert ('wait_for_credit', 0.01, 15, 3) in events
    assert not any(event[0] == 'create_api_key' for event in events)
    assert not any(event[0] == 'list_api_keys' for event in events)


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


def test_swarms_result_mapper_rejects_missing_api_key():
    from platforms.swarms.plugin import SwarmsPlatform
    import pytest

    with pytest.raises(RuntimeError, match='API Key'):
        SwarmsPlatform()._map_swarms_result({
            'email': 'demo@swarms.test',
            'password': 'Password123!',
            'credit_info': {'data': 0},
        })


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


def test_swarms_browser_registration_fails_without_credit_before_api_key(monkeypatch):
    import sys
    import types
    import pytest
    from platforms.swarms.browser_mailbox import SwarmsBrowserMailboxWorker

    class FakeLocator:
        def __init__(self, page):
            self.page = page

        def count(self):
            return 1

        @property
        def first(self):
            return self

        def fill(self, _value, timeout=0):
            return None

        def click(self, timeout=0):
            self.page.url = 'https://swarms.world/signin/check-email'

        def inner_text(self, timeout=0):
            return ''

    class FakePage:
        url = ''

        def goto(self, url, **_kwargs):
            self.url = url

        def wait_for_timeout(self, _timeout):
            return None

        def locator(self, _selector):
            return FakeLocator(self)

    class FakeContext:
        def add_init_script(self, _script):
            return None

        def new_page(self):
            return FakePage()

        def cookies(self, _url):
            return [{'name': 'sb-db-auth-token', 'value': '{}', 'domain': '.swarms.world', 'path': '/'}]

    class FakeBrowser:
        def new_context(self, **_kwargs):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return False

    fake_sync_api = types.SimpleNamespace(sync_playwright=lambda: FakePlaywrightContext())
    fake_playwright = types.SimpleNamespace(sync_api=fake_sync_api)
    monkeypatch.setitem(sys.modules, 'playwright', fake_playwright)
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', fake_sync_api)

    worker = SwarmsBrowserMailboxWorker(headless=True, log_fn=lambda _msg: None)
    calls: list[tuple] = []

    def fake_trpc_get(_session, path):
        calls.append(('get', path))
        if path == 'main.getUser':
            return {'id': 'user-id', 'email': 'demo@swarms.test'}
        if path == 'panel.getUserCredit':
            return {'data': 0}
        raise AssertionError(path)

    def fake_trpc_post(_session, path, payload):
        calls.append(('post', path, payload))
        raise AssertionError('额度未到账时不应创建 API Key')

    worker._trpc_get = fake_trpc_get
    worker._trpc_post = fake_trpc_post

    with pytest.raises(RuntimeError, match='额度'):
        worker.run(
            email='demo@swarms.test',
            password='Password123!',
            verification_link_callback=lambda: 'https://db.swarms.world/auth/v1/verify?token=abc&type=signup',
        )

    assert ('get', 'panel.getUserCredit') in calls
    assert not any(call[0] == 'post' for call in calls)


def test_swarms_browser_registration_probes_browser_proxy_and_avoids_networkidle(monkeypatch):
    import sys
    import types
    import pytest
    from platforms.swarms.browser_mailbox import SwarmsBrowserMailboxWorker

    pages: list = []
    launch_calls: list[dict] = []
    logs: list[str] = []

    class FakeLocator:
        def __init__(self, page, selector):
            self.page = page
            self.selector = selector

        def count(self):
            return 1

        @property
        def first(self):
            return self

        def fill(self, _value, timeout=0):
            return None

        def click(self, timeout=0):
            self.page.url = 'https://swarms.world/signin/check-email'

        def inner_text(self, timeout=0):
            if self.selector == 'body':
                return self.page.body_text
            return ''

    class FakePage:
        def __init__(self):
            self.url = ''
            self.body_text = ''
            self.goto_calls: list[tuple[str, dict]] = []

        def goto(self, url, **kwargs):
            self.goto_calls.append((url, dict(kwargs)))
            self.url = url
            if url == 'https://api.ipify.org':
                self.body_text = '203.0.113.10'
            return types.SimpleNamespace(status=200)

        def wait_for_timeout(self, _timeout):
            return None

        def locator(self, selector):
            return FakeLocator(self, selector)

    class FakeContext:
        def add_init_script(self, _script):
            return None

        def new_page(self):
            page = FakePage()
            pages.append(page)
            return page

        def cookies(self, _url):
            return [{'name': 'sb-db-auth-token', 'value': '{}', 'domain': '.swarms.world', 'path': '/'}]

    class FakeBrowser:
        def new_context(self, **_kwargs):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def launch(self, **kwargs):
            launch_calls.append(kwargs)
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return False

    fake_sync_api = types.SimpleNamespace(sync_playwright=lambda: FakePlaywrightContext())
    monkeypatch.setitem(sys.modules, 'playwright', types.SimpleNamespace(sync_api=fake_sync_api))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', fake_sync_api)

    worker = SwarmsBrowserMailboxWorker(
        headless=True,
        proxy='http://user:pass@proxy.local:8080',
        log_fn=logs.append,
    )
    worker._trpc_get = lambda _session, path: {'id': 'user-id'} if path == 'main.getUser' else {'data': 0}
    worker._trpc_post = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('额度未到账时不应创建 API Key'))

    with pytest.raises(RuntimeError, match='额度'):
        worker.run(
            email='demo@swarms.test',
            password='Password123!',
            verification_link_callback=lambda: 'https://db.swarms.world/auth/v1/verify?token=abc&type=signup',
        )

    assert launch_calls[0]['proxy'] == {
        'server': 'http://proxy.local:8080',
        'username': 'user',
        'password': 'pass',
    }
    assert pages[0].goto_calls[0][0] == 'https://api.ipify.org'
    swarms_gotos = [kwargs for url, kwargs in pages[0].goto_calls if 'swarms.world' in url]
    assert swarms_gotos
    assert all(item.get('wait_until') == 'domcontentloaded' for item in swarms_gotos)
    assert not any(item.get('wait_until') == 'networkidle' for _url, item in pages[0].goto_calls)
    assert any('浏览器代理出口 IP' in item for item in logs)


def test_swarms_browser_registration_fails_fast_when_browser_proxy_probe_fails(monkeypatch):
    import sys
    import types
    import pytest
    from platforms.swarms.browser_mailbox import SwarmsBrowserMailboxWorker

    visited: list[str] = []

    class FakePage:
        url = ''

        def goto(self, url, **_kwargs):
            visited.append(url)
            if url == 'https://api.ipify.org':
                raise RuntimeError('net::ERR_EMPTY_RESPONSE')
            raise AssertionError('代理预检失败后不应继续打开 Swarms 注册页')

        def locator(self, _selector):
            raise AssertionError('代理预检失败后不应查找页面元素')

    class FakeContext:
        def add_init_script(self, _script):
            return None

        def new_page(self):
            return FakePage()

    class FakeBrowser:
        def new_context(self, **_kwargs):
            return FakeContext()

        def close(self):
            return None

    class FakeChromium:
        def launch(self, **_kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightContext:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_args):
            return False

    fake_sync_api = types.SimpleNamespace(sync_playwright=lambda: FakePlaywrightContext())
    monkeypatch.setitem(sys.modules, 'playwright', types.SimpleNamespace(sync_api=fake_sync_api))
    monkeypatch.setitem(sys.modules, 'playwright.sync_api', fake_sync_api)

    worker = SwarmsBrowserMailboxWorker(
        headless=True,
        proxy='http://user:pass@proxy.local:8080',
        log_fn=lambda _msg: None,
    )

    with pytest.raises(RuntimeError, match='浏览器代理预检失败'):
        worker.run(
            email='demo@swarms.test',
            password='Password123!',
            verification_link_callback=lambda: 'https://db.swarms.world/auth/v1/verify?token=abc&type=signup',
        )

    assert visited == ['https://api.ipify.org']


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

    events: list = []

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
            events.append(('ensure_profile', full_name))
            assert email == 'demo@swarms.test'
            assert full_name
            assert full_name != 'Auto Register'
            return {'username': 'demo_swarms', 'full_name': full_name}

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



def test_swarms_client_loads_access_token_from_existing_auth_cookie():
    client = SwarmsClient(log_fn=lambda _msg: None)
    cookie_value = client._encode_supabase_storage_cookie('{"access_token":"tok","refresh_token":"ref","user":{"id":"user-id"}}')
    client.session.cookies.set('sb-db-auth-token', cookie_value, domain='swarms.world', path='/')

    payload = client._load_auth_cookie_session()

    assert payload['access_token'] == 'tok'
    assert client.access_token == 'tok'
    assert client.refresh_token == 'ref'
    assert client.user_id == 'user-id'



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
        text='2:"$Sreact.fragment"
4:I["static/chunks/page.js","error boundary chunk"]',
    )
    client.session.get = Mock(return_value=get_response)
    client.session.post = Mock(return_value=post_response)

    result = client.signup('demo@swarms.test', 'Password123!')

    assert result['ok'] is True
    assert result['signup_method'] == 'swarms_server_action'
