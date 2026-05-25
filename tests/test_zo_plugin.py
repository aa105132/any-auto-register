from __future__ import annotations

import inspect
import os
import unittest
from unittest.mock import patch

from core.base_platform import AccountStatus, RegisterConfig
from core.registry import get, list_platforms, load_all
from platforms.zo import browser_oauth, protocol_mailbox
from platforms.zo.core import API_BASE, AUTH_BASE, SITE_URL, ZoClient, extract_workspace_info, mask_card_info, normalize_billing_country, normalize_card_info, resolve_card_info, sanitize_sensitive
from platforms.zo.plugin import ZoPlatform


class _FakeZoResponse:
    def __init__(self, status_code=200, data=None, text='', url='https://api.zo.computer/signup'):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.url = url
        self.headers = {'Content-Type': 'text/event-stream'}
        self.ok = 200 <= status_code < 300
        self.content = text.encode('utf-8')

    def json(self):
        if self._data is None:
            raise ValueError('not json')
        return self._data

    def iter_lines(self, decode_unicode=False):
        for line in self.text.splitlines():
            yield line if decode_unicode else line.encode('utf-8')


class _RecordingZoSession:
    def __init__(self):
        self.headers = {}
        self.cookies = __import__('requests').cookies.RequestsCookieJar()
        self.proxies = {}
        self.calls = []

    def request(self, method, url, headers=None, **kwargs):
        self.calls.append({'method': method, 'url': url, 'headers': dict(headers or {}), 'kwargs': kwargs})
        if url.endswith('/signup/testhandle/available'):
            return _FakeZoResponse(data={'available': True}, url=url)
        if url.endswith('/signup') and method == 'POST':
            return _FakeZoResponse(
                text='event: SignupStepEvent\ndata: {"step":"account","status":"success"}\n\n'
                     'event: SignupCompleteEvent\ndata: {"handle":"testhandle"}\n\n',
                url=url,
            )
        if url.endswith('/api/login-state'):
            return _FakeZoResponse(data={'claims': {'properties': {'email': 'demo@example.com'}}, 'workspaces': []}, url=url)
        return _FakeZoResponse(status_code=404, data={'detail': 'not found'}, url=url)


class _BillingRecordingZoSession(_RecordingZoSession):
    def request(self, method, url, headers=None, **kwargs):
        self.calls.append({'method': method, 'url': url, 'headers': dict(headers or {}), 'kwargs': kwargs})
        if url.endswith('/billing/setup-intent') and method == 'POST':
            return _FakeZoResponse(data={'client_secret': 'seti_test_123_secret_hidden'}, url=url)
        if 'api.stripe.com/v1/setup_intents/seti_test_123/confirm' in url and method == 'POST':
            return _FakeZoResponse(
                data={
                    'id': 'seti_test_123',
                    'payment_method': 'pm_test_456',
                    'status': 'succeeded',
                },
                url=url,
            )
        if url.endswith('/billing/credit-balance?testmode=false') and method == 'GET':
            return _FakeZoResponse(data={'billing': {'credit_balance': {'amount': 100, 'currency': 'USD'}}}, url=url)
        if url.endswith('/api-keys/') and method == 'GET':
            return _FakeZoResponse(data=[], url=url)
        if url.endswith('/api-keys/') and method == 'POST':
            return _FakeZoResponse(data={'id': 'key_test_123', 'name': 'auto-register', 'key': 'zo_test_api_key_value_1234567890'}, url=url)
        return _FakeZoResponse(status_code=404, data={'detail': 'not found'}, url=url)


class ZoPlatformTests(unittest.TestCase):
    def test_registry_loads_zo_platform(self):
        load_all()
        names = {item["name"] for item in list_platforms()}
        self.assertIn("zo", names)
        self.assertIs(get("zo"), ZoPlatform)

    def test_capabilities_include_mailbox_google_oauth_and_cdp_protocol(self):
        platform = ZoPlatform(RegisterConfig(executor_type="protocol"))
        self.assertIn("mailbox", platform.supported_identity_modes)
        self.assertIn("oauth_browser", platform.supported_identity_modes)
        self.assertIn("google", platform.supported_oauth_providers)
        self.assertIn("cdp_protocol", platform.supported_executors)

    def test_plugin_maps_access_token_as_ai_api_key_after_credit_and_card(self):
        result = ZoPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "zo_test_token_value_1234567890",
            "api_verification": {"ok": True},
            "credit_result": {"ok": True, "amount": 100.0, "currency": "USD"},
            "card_binding_result": {"ok": True, "card": {"last4": "1111"}},
        })
        self.assertEqual(result.status, AccountStatus.REGISTERED)
        self.assertEqual(result.token, "zo_test_token_value_1234567890")
        self.assertEqual(result.extra["api_key"], "zo_test_token_value_1234567890")
        self.assertEqual(result.extra["ai_api_token"], "zo_test_token_value_1234567890")
        self.assertEqual(result.extra["api_base"], API_BASE)
        self.assertEqual(result.extra["auth_base"], AUTH_BASE)
        self.assertEqual(result.extra["site_url"], SITE_URL)
        self.assertEqual(result.extra["auth_header"], "Authorization: Bearer")

    def test_missing_credit_or_card_does_not_count_as_success(self):
        missing_card = ZoPlatform()._map_result({
            "email": "demo@example.com",
            "api_key": "zo_test_token_value_1234567890",
            "credit_result": {"ok": True, "amount": 100.0},
            "card_binding_result": {"ok": False},
        })
        self.assertEqual(missing_card.status, AccountStatus.INVALID)


    def test_billing_country_is_normalized_for_card_binding(self):
        self.assertEqual(normalize_billing_country("United States of America"), "US")
        self.assertEqual(normalize_card_info({"zo_card": {"country": "USA"}})["country"], "US")

    def test_card_info_is_runtime_only_and_masked(self):
        card = normalize_card_info({
            "zo_card": {
                "number": "1234567890123456",
                "exp_month": "12",
                "exp_year": "29",
                "cvv": "999",
                "country": "United States of America",
                "address": "example address",
                "city": "Aloha",
                "postal_code": "97003",
                "state": "Oregon",
            }
        })
        self.assertEqual(card["number"], "1234567890123456")
        masked = mask_card_info(card)
        self.assertEqual(masked["last4"], "3456")
        self.assertNotIn("number", masked)
        self.assertNotIn("cvv", masked)
        self.assertNotIn("1234567890123456", repr(masked))
        self.assertNotIn("999", repr(masked))



    def test_card_sanitizer_removes_server_echoes(self):
        data = {"payment_method": {"card": {"number": "5555555555554444", "cvc": "123"}}}
        sanitized = sanitize_sensitive(data)
        self.assertNotIn("5555555555554444", repr(sanitized))
        self.assertNotIn("123", repr(sanitized))
        self.assertIn("4444", repr(sanitized))

    def test_card_sanitizer_masks_stripe_client_secret(self):
        sanitized = sanitize_sensitive({'client_secret': 'seti_test_123_secret_hidden'})
        self.assertNotIn('seti_test_123_secret_hidden', repr(sanitized))
        self.assertIn('seti_test_123_secret_', repr(sanitized))

    def test_env_sandbox_card_is_used_when_extra_missing(self):
        env = {
            "ZO_CARD_NUMBER": "5555555555554444",
            "ZO_CARD_EXP_MONTH": "12",
            "ZO_CARD_EXP_YEAR": "2029",
            "ZO_CARD_CVV": "123",
            "ZO_CARD_COUNTRY": "US",
            "ZO_CARD_ADDRESS": "example address",
            "ZO_CARD_CITY": "Example",
            "ZO_CARD_POSTAL_CODE": "00000",
            "ZO_CARD_STATE": "Example",
        }
        with patch.dict(os.environ, env, clear=False):
            card = resolve_card_info({})
        masked = mask_card_info(card)
        self.assertEqual(masked["last4"], "4444")
        self.assertEqual(masked["exp_month"], "12")
        self.assertEqual(masked["exp_year"], "2029")
        self.assertNotIn("cvv", masked)
        self.assertNotIn("5555555555554444", repr(masked))

    def test_missing_card_requires_runtime_configuration(self):
        with patch.dict(os.environ, {key: "" for key in (
            "ZO_CARD_NUMBER", "ZO_CARD_EXP_MONTH", "ZO_CARD_EXP_YEAR", "ZO_CARD_CVV",
            "ZO_CARD_COUNTRY", "ZO_CARD_ADDRESS", "ZO_CARD_CITY", "ZO_CARD_POSTAL_CODE", "ZO_CARD_STATE",
        )}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "ZO_CARD"):
                resolve_card_info({})

    def test_bind_card_uses_setup_intent_and_stripe_confirm_without_leaking_card(self):
        session = _BillingRecordingZoSession()
        client = ZoClient(session=session)
        client.import_tokens(access_token='token.abc.def')
        client.set_workspace(handle='testhandle')
        
        env = {"ZO_CARD_NUMBER": "5555555555554444", "ZO_CARD_EXP_MONTH": "12", "ZO_CARD_EXP_YEAR": "2029", "ZO_CARD_CVV": "123", "ZO_CARD_COUNTRY": "US", "ZO_CARD_ADDRESS": "example address", "ZO_CARD_CITY": "Example", "ZO_CARD_POSTAL_CODE": "00000", "ZO_CARD_STATE": "Example", "ZO_STRIPE_PUBLISHABLE_KEY": "pk_test_placeholder"}
        with patch.dict(os.environ, env, clear=False):
            result = client.bind_card(card=resolve_card_info({}), require_confirmed=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['payment_method'], 'pm_test_456')
        self.assertEqual(result['setup_intent'], 'seti_test_123')
        urls = [call['url'] for call in session.calls]
        self.assertIn(f'{API_BASE}/billing/setup-intent', urls)
        self.assertTrue(any('api.stripe.com/v1/setup_intents/seti_test_123/confirm' in url for url in urls))
        stripe_call = next(call for call in session.calls if 'api.stripe.com' in call['url'])
        self.assertEqual(stripe_call['headers']['Origin'], 'https://testhandle.zo.computer')
        self.assertEqual(stripe_call['headers']['Referer'], 'https://testhandle.zo.computer/')
        self.assertNotIn(f'{API_BASE}/billing/payment-methods', urls)
        self.assertNotIn('5555555555554444', repr(result))
        self.assertNotIn('cvv', repr(result).lower())

    def test_check_credits_prefers_real_credit_balance_endpoint(self):
        session = _BillingRecordingZoSession()
        client = ZoClient(session=session)
        client.set_workspace(handle='testhandle')
        result = client.check_credits(min_amount=1.0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['amount'], 100.0)
        self.assertEqual(result['source'], '/billing/credit-balance?testmode=false')
        self.assertEqual(session.calls[0]['url'], f'{API_BASE}/billing/credit-balance?testmode=false')

    def test_create_access_token_uses_api_keys_endpoint_before_user_services(self):
        session = _BillingRecordingZoSession()
        client = ZoClient(session=session)
        client.set_workspace(handle='testhandle')
        result = client.create_access_token(name='auto-register')
        self.assertTrue(result['ok'])
        self.assertEqual(result['api_key'], 'zo_test_api_key_value_1234567890')
        urls = [call['url'] for call in session.calls]
        self.assertEqual(urls[0], f'{API_BASE}/api-keys/')
        self.assertEqual(urls[1], f'{API_BASE}/api-keys/')
        self.assertEqual(session.calls[1]['kwargs']['json'], {'name': 'auto-register'})
        self.assertNotIn(f'{API_BASE}/user-services/', urls)

    def test_mailbox_flow_includes_coupon_card_and_access_token_steps(self):
        self.assertTrue(hasattr(ZoClient, "send_email_registration"))
        source = inspect.getsource(protocol_mailbox.ZoProtocolMailboxWorker.run)
        self.assertIn("start_email_authorize", source)
        self.assertIn("visit_verification_link", source)
        self.assertIn("skip_onboarding", source)
        self.assertIn("skip_phone", source)
        self.assertIn("redeem_coupon", source)
        self.assertIn("bind_card", source)
        self.assertIn("create_access_token", source)
        self.assertIn("check_credits", source)

    def test_oauth_flow_uses_google_then_http_post_login_steps(self):
        self.assertTrue(hasattr(browser_oauth, "isolated_oauth_browser_options"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("drive_google_oauth", source)
        self.assertIn("redeem_coupon", source)
        self.assertIn("bind_card", source)
        self.assertIn("create_access_token", source)
        self.assertIn("_bind_card_in_browser", source)
        self.assertIn("_create_access_token_in_browser", source)

    def test_oauth_flow_tolerates_site_home_timeout_before_openauth(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("page.goto(SITE_URL", source)
        self.assertIn("Zo 首页打开超时", source)
        self.assertLess(source.index("page.goto(SITE_URL"), source.index("_start_google_oauth_protocol"))

    def test_oauth_callback_code_is_exchanged_without_waiting_for_cookie(self):
        self.assertTrue(hasattr(browser_oauth, "_extract_oauth_callback"))
        self.assertTrue(hasattr(browser_oauth, "_exchange_callback_code"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_has_oauth_callback_url", source)
        self.assertIn("_exchange_callback_code", source)

    def test_oauth_defaults_to_isolated_profile_for_shared_cdp(self):
        with browser_oauth.isolated_oauth_browser_options(
            chrome_user_data_dir="",
            chrome_cdp_url="http://127.0.0.1:9222",
            allow_shared_cdp=False,
        ) as options:
            self.assertEqual(options["chrome_cdp_url"], "")
            self.assertIn("zo_oauth_", options["chrome_user_data_dir"])


    def test_zo_account_clicker_does_not_touch_google_consent_continue_page(self):
        class _Locator:
            def wait_for(self, *_args, **_kwargs):
                raise RuntimeError('not an account row')

            def filter(self, *_args, **_kwargs):
                return self

            @property
            def first(self):
                return self

        class _Page:
            url = 'https://accounts.google.com/signin/oauth/consent'

            def __init__(self):
                self.locator_calls = 0
                self.evaluate_calls = 0

            def is_closed(self):
                return False

            def locator(self, *_args, **_kwargs):
                self.locator_calls += 1
                return _Locator()

            def evaluate(self, *_args, **_kwargs):
                self.evaluate_calls += 1
                return ''

        page = _Page()

        class _Browser:
            def pages(self):
                return [page]

        self.assertFalse(
            browser_oauth._click_google_account_for_zo(
                _Browser(),
                email_hint='user@example.com',
                log_fn=lambda _msg: None,
            )
        )
        self.assertEqual(page.locator_calls, 0)
        self.assertEqual(page.evaluate_calls, 0)


    def test_zo_account_clicker_rejects_accountchooser_continue_consent_body(self):
        class _BodyLocator:
            def inner_text(self, *_args, **_kwargs):
                return "You're signing back in to Zo Computer\nContinue"

        class _ActionLocator:
            def wait_for(self, *_args, **_kwargs):
                raise RuntimeError('account rows must not be queried on consent body')

            def filter(self, *_args, **_kwargs):
                return self

            @property
            def first(self):
                return self

        class _Page:
            url = 'https://accounts.google.com/signin/oauth/accountchooser?continue=https%3A%2F%2Fauth.zo.computer%2Fcallback'

            def __init__(self):
                self.body_locator_calls = 0
                self.action_locator_calls = 0
                self.evaluate_calls = 0

            def is_closed(self):
                return False

            def locator(self, selector, *_args, **_kwargs):
                if selector == 'body':
                    self.body_locator_calls += 1
                    return _BodyLocator()
                self.action_locator_calls += 1
                return _ActionLocator()

            def evaluate(self, *_args, **_kwargs):
                self.evaluate_calls += 1
                return ''

        page = _Page()

        class _Browser:
            def pages(self):
                return [page]

        self.assertFalse(
            browser_oauth._click_google_account_for_zo(
                _Browser(),
                email_hint='user@example.com',
                log_fn=lambda _msg: None,
            )
        )
        self.assertEqual(page.body_locator_calls, 1)
        self.assertEqual(page.action_locator_calls, 0)
        self.assertEqual(page.evaluate_calls, 0)

    def test_browser_access_token_fallback_uses_api_keys_endpoint_first(self):
        source = inspect.getsource(browser_oauth._create_access_token_in_browser)
        self.assertIn('https://api.zo.computer/api-keys/', source)
        self.assertLess(
            source.index('https://api.zo.computer/api-keys/'),
            source.index('https://api.zo.computer/access-tokens'),
        )


    def test_oauth_flow_does_not_use_generic_google_account_autoselect_after_driver(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertNotIn('auto_select_google_account', source)


    def test_workspace_origin_header_is_added_after_set_workspace(self):
        client = ZoClient(session=_RecordingZoSession())
        self.assertNotIn('X-Zo-Workspace-Origin', client.auth_headers())
        client.set_workspace(handle='testhandle')
        self.assertEqual(client.auth_headers()['X-Zo-Workspace-Origin'], 'https://testhandle.zo.computer')

    def test_workspace_info_can_be_extracted_from_claim_domains(self):
        data = {
            'claims': {
                'properties': {
                    'domains': ['demo-workspace-123'],
                },
            },
            'workspaces': [],
        }
        workspace = extract_workspace_info(data)
        self.assertEqual(workspace['handle'], 'demoworkspace123')
        self.assertEqual(workspace['origin'], 'https://demoworkspace123.zo.computer')

    def test_create_workspace_posts_signup_stream_and_sets_workspace(self):
        session = _RecordingZoSession()
        client = ZoClient(session=session)
        client.import_tokens(access_token='token.abc.def')
        result = client.create_workspace(handle='testhandle', promo_code='SHEK100')
        self.assertTrue(result['ok'])
        self.assertEqual(result['handle'], 'testhandle')
        self.assertEqual(client.auth_headers()['X-Zo-Workspace-Origin'], 'https://testhandle.zo.computer')
        signup_calls = [call for call in session.calls if call['url'].endswith('/signup') and call['method'] == 'POST']
        self.assertEqual(len(signup_calls), 1)
        self.assertEqual(signup_calls[0]['kwargs']['json']['promo_code'], 'SHEK100')
        self.assertEqual(signup_calls[0]['kwargs']['json']['handle'], 'testhandle')





    def test_ensure_workspace_reuses_existing_workspace_after_signup_conflict(self):
        class _ExistingWorkspaceSession(_RecordingZoSession):
            def request(self, method, url, headers=None, **kwargs):
                self.calls.append({'method': method, 'url': url, 'headers': dict(headers or {}), 'kwargs': kwargs})
                if url.endswith('/api/login-state'):
                    return _FakeZoResponse(data={'claims': {'properties': {'email': 'demo@example.com'}}, 'workspaces': []}, url=url)
                if url.endswith('/signup/testhandle/available'):
                    return _FakeZoResponse(data={'available': True}, url=url)
                if url.endswith('/signup') and method == 'POST':
                    return _FakeZoResponse(status_code=400, data={'error': 'Account already has a workspace'}, text='{"error":"Account already has a workspace"}', url=url)
                if url.endswith('/settings/'):
                    return _FakeZoResponse(data={'workspaces': [{'handle': 'existinghandle', 'url': 'https://existinghandle.zo.computer'}]}, url=url)
                return _FakeZoResponse(status_code=404, data={'detail': 'not found'}, url=url)

        session = _ExistingWorkspaceSession()
        client = ZoClient(session=session)
        result = client.ensure_workspace(handle='testhandle', promo_code='SHEK100')
        self.assertTrue(result['ok'])
        self.assertEqual(result['source'], 'existing-after-conflict')
        self.assertEqual(result['workspace']['handle'], 'existinghandle')
        self.assertEqual(client.auth_headers()['X-Zo-Workspace-Origin'], 'https://existinghandle.zo.computer')
        self.assertIn(f'{API_BASE}/settings/', [call['url'] for call in session.calls])




    def test_browser_fallback_fetches_include_workspace_origin_header(self):
        bind_source = inspect.getsource(browser_oauth._bind_card_in_browser)
        token_source = inspect.getsource(browser_oauth._create_access_token_in_browser)
        self.assertIn('X-Zo-Workspace-Origin', bind_source)
        self.assertIn('X-Zo-Workspace-Origin', token_source)
        self.assertIn('location.origin', bind_source)
        self.assertIn('location.origin', token_source)

    def test_oauth_code_exchange_syncs_tokens_back_to_browser_for_cdp_fallback(self):
        self.assertTrue(hasattr(browser_oauth, "_sync_client_auth_to_browser"))
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("_sync_client_auth_to_browser", source)
        self.assertLess(source.index("_sync_client_auth_to_browser"), source.index("ensure_workspace"))

    def test_mailbox_flow_creates_or_selects_workspace_before_workspace_steps(self):
        source = inspect.getsource(protocol_mailbox.ZoProtocolMailboxWorker.run)
        self.assertIn("ensure_workspace", source)
        self.assertLess(source.index("ensure_workspace"), source.index("skip_onboarding"))
        self.assertLess(source.index("ensure_workspace"), source.index("redeem_coupon"))

    def test_oauth_flow_creates_or_selects_workspace_and_reuses_same_client(self):
        source = inspect.getsource(browser_oauth.register_with_browser_oauth)
        self.assertIn("ensure_workspace", source)
        self.assertLess(source.index("ensure_workspace"), source.index("skip_onboarding"))
        self.assertIn("client.redeem_coupon", source)
        self.assertNotIn("_redeem_coupon_http(cookies", source)

    def test_task_auto_export_hook_includes_zo(self):
        import application.tasks as tasks

        self.assertTrue(hasattr(tasks, "_auto_export_zo_key"))
        source = inspect.getsource(tasks)
        self.assertIn("_auto_export_zo_key(logger, account)", source)


if __name__ == "__main__":
    unittest.main()
