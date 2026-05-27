from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.base_platform import Account, AccountStatus, RegisterConfig


class FreeModelPluginTests(unittest.TestCase):
    def test_platform_declares_outlook_mailbox_only_capability(self):
        from platforms.freemodel.plugin import FreeModelPlatform

        platform = FreeModelPlatform()

        self.assertEqual(platform.name, 'freemodel')
        self.assertEqual(platform.display_name, 'FreeModel')
        self.assertEqual(platform.supported_identity_modes, ['mailbox'])
        self.assertEqual(platform.supported_oauth_providers, [])
        self.assertEqual(platform.supported_executors, ['protocol'])

    def test_platform_rejects_oauth_browser_identity(self):
        from platforms.freemodel.plugin import FreeModelPlatform

        platform = FreeModelPlatform(config=RegisterConfig(extra={'identity_provider': 'oauth_browser'}))

        with self.assertRaises(NotImplementedError):
            platform._get_identity_provider()

    def test_plugin_maps_api_key_phone_and_referral_fields(self):
        from platforms.freemodel.plugin import FreeModelPlatform

        mapped = FreeModelPlatform()._map_result(
            {
                'email': 'demo@example.com',
                'api_key': 'sk-free-model',
                'api_key_id': 'key-1',
                'api_key_name': 'auto-register',
                'referral_code': 'FRE-next',
                'used_invite_code': 'FRE-current',
                'verified_at': '2026-05-22T10:00:00Z',
                'phone': '13800138000',
                'cookies': {'sid': 'cookie'},
                'user': {'id': 'user-1'},
            }
        )

        self.assertEqual(mapped.email, 'demo@example.com')
        self.assertEqual(mapped.token, 'sk-free-model')
        self.assertEqual(mapped.status, AccountStatus.REGISTERED)
        self.assertEqual(mapped.extra['api_key'], 'sk-free-model')
        self.assertEqual(mapped.extra['ai_api_token'], 'sk-free-model')
        self.assertEqual(mapped.extra['api_key_id'], 'key-1')
        self.assertEqual(mapped.extra['referral_code'], 'FRE-next')
        self.assertEqual(mapped.extra['invite_code'], 'FRE-next')
        self.assertEqual(mapped.extra['used_invite_code'], 'FRE-current')
        self.assertEqual(mapped.extra['site_url'], 'https://freemodel.dev/')
        self.assertEqual(mapped.extra['api_base'], 'https://api.freemodel.dev')
        self.assertEqual(mapped.extra['verification_phone']['phone'], '13800138000')
        self.assertTrue(mapped.extra['account_overview']['api_key_created'])
        self.assertTrue(mapped.extra['account_overview']['verified_phone'])
        self.assertEqual(mapped.extra['account_overview']['referral_code'], 'FRE-next')

    def test_register_prefers_outlook_token_mail_provider(self):
        from platforms.freemodel.plugin import FreeModelPlatform

        platform = FreeModelPlatform()

        self.assertEqual(platform.default_mail_provider, 'outlook_token')

    def test_build_platform_instance_uses_platform_default_mail_provider(self):
        import application.tasks as tasks
        from platforms.freemodel.plugin import FreeModelPlatform

        class DummyLogger:
            def log(self, *_args, **_kwargs):
                pass

        captured = {}

        def fake_create_mailbox(provider, extra, proxy):
            captured['provider'] = provider
            captured['extra'] = dict(extra)
            captured['proxy'] = proxy
            return None

        with patch('core.base_mailbox.create_mailbox', fake_create_mailbox), patch.object(tasks, 'get', return_value=FreeModelPlatform):
            tasks._build_platform_instance(
                'freemodel',
                {
                    'executor_type': 'protocol',
                    'captcha_solver': 'auto',
                    'proxy': '',
                    'extra': {'identity_provider': 'mailbox'},
                },
                DummyLogger(),
            )

        self.assertEqual(captured['provider'], 'outlook_token')
        self.assertEqual(captured['extra']['mail_provider'], 'outlook_token')

    def test_register_passes_mailbox_otp_options_to_protocol_runner(self):
        from core.base_mailbox import MailboxAccount
        from platforms.freemodel.plugin import FreeModelPlatform

        captured = {}

        class DummyMailbox:
            def get_email(self):
                return MailboxAccount(email='outlook@example.com', extra={})

            def get_current_ids(self, _account):
                return set()

            def wait_for_code(self, account, **kwargs):
                captured['mailbox_account'] = account
                captured['otp_kwargs'] = kwargs
                return '123456'

        def fake_register_with_email_otp(**kwargs):
            captured.update(kwargs)
            otp_code = kwargs['otp_callback']()
            return {
                'email': kwargs['email'],
                'api_key': 'sk-mailbox',
                'referral_code': 'FRE-mailbox-next',
                'otp_code': otp_code,
            }

        phone_provider = object()
        platform = FreeModelPlatform(
            config=RegisterConfig(
                extra={
                    'identity_provider': 'mailbox',
                    'freemodel_invite_code': 'FRE-current',
                    'freemodel_key_name': 'mail-key',
                    'phone_otp_timeout': '222',
                    'phone_poll_interval': '7',
                }
            ),
            mailbox=DummyMailbox(),
        )
        platform.phone_provider = phone_provider

        with patch('platforms.freemodel.browser_oauth.register_with_email_otp', fake_register_with_email_otp):
            account = platform.register()

        self.assertEqual(account.email, 'outlook@example.com')
        self.assertEqual(account.token, 'sk-mailbox')
        self.assertEqual(captured['email'], 'outlook@example.com')
        self.assertEqual(captured['invite_code'], 'FRE-current')
        self.assertEqual(captured['phone_provider'], phone_provider)
        self.assertEqual(captured['phone_timeout'], 222)
        self.assertEqual(captured['phone_poll_interval'], 7)
        self.assertEqual(captured['key_name'], 'mail-key')
        self.assertEqual(captured['otp_kwargs']['keyword'], '')


    def test_register_uses_discreet_default_api_key_name(self):
        from core.base_mailbox import MailboxAccount
        from platforms.freemodel.plugin import FreeModelPlatform

        captured = {}

        class DummyMailbox:
            def get_email(self):
                return MailboxAccount(email='outlook@example.com', extra={})

            def get_current_ids(self, _account):
                return set()

            def wait_for_code(self, _account, **_kwargs):
                return '123456'

        def fake_register_with_email_otp(**kwargs):
            captured.update(kwargs)
            kwargs['otp_callback']()
            return {
                'email': kwargs['email'],
                'api_key': 'sk-mailbox',
                'api_key_name': kwargs['key_name'],
                'referral_code': 'FRE-mailbox-next',
            }

        platform = FreeModelPlatform(
            config=RegisterConfig(extra={'identity_provider': 'mailbox'}),
            mailbox=DummyMailbox(),
        )

        with patch('platforms.freemodel.browser_oauth.register_with_email_otp', fake_register_with_email_otp):
            account = platform.register()

        self.assertEqual(captured['key_name'], 'default')
        self.assertEqual(account.extra['api_key_name'], 'default')


class FreeModelPhoneVerificationTests(unittest.TestCase):
    def test_phone_send_daily_limit_releases_number_and_retries_next_number(self):
        from core.base_phone import PhoneAccount
        from platforms.freemodel import browser_oauth

        logs: list[str] = []
        calls: list[tuple[str, str]] = []

        class FakePhoneProvider:
            def __init__(self):
                self.accounts = [
                    PhoneAccount(phone='13800138000', project_id='114901', token='token-1'),
                    PhoneAccount(phone='13900139000', project_id='114901', token='token-2'),
                ]
                self.released: list[str] = []
                self.waited: list[str] = []

            def get_phone(self):
                return self.accounts.pop(0)

            def wait_for_code(self, account, **_kwargs):
                self.waited.append(account.phone)
                return '246810'

            def release_phone(self, account):
                self.released.append(account.phone)
                return True

        phone_provider = FakePhoneProvider()

        def fake_request(_session, path, *, method='GET', body=None):
            phone = str((body or {}).get('phone') or '')
            calls.append((path, phone))
            if path in {'/api/auth/me', '/api/billing'}:
                return {'ok': True, 'status': 200, 'data': {'user': None}, 'text': ''}
            if path == '/api/phone/send-sms' and phone == '13800138000':
                return {'ok': False, 'status': 429, 'data': {'error': 'daily_limit_exceeded'}, 'text': ''}
            if path == '/api/phone/send-sms' and phone == '13900139000':
                return {'ok': True, 'status': 200, 'data': {'sent': True}, 'text': ''}
            if path == '/api/phone/verify':
                return {'ok': True, 'status': 200, 'data': {'verifiedAt': '2026-05-23T00:00:00Z'}, 'text': ''}
            raise AssertionError(f'unexpected request: {path} {body}')

        with patch.object(browser_oauth, '_session_request_json', fake_request):
            result = browser_oauth._verify_phone_session(
                SimpleNamespace(),
                phone_provider,
                send_attempts=2,
                log_fn=logs.append,
            )

        self.assertEqual(result['phone'], '13900139000')
        self.assertEqual(result['verified_at'], '2026-05-23T00:00:00Z')
        self.assertEqual(phone_provider.released, ['13800138000'])
        self.assertEqual(phone_provider.waited, ['13900139000'])
        self.assertEqual(
            calls,
            [
                ('/api/auth/me', ''),
                ('/api/billing', ''),
                ('/api/phone/send-sms', '13800138000'),
                ('/api/phone/send-sms', '13900139000'),
                ('/api/phone/verify', '13900139000'),
            ],
        )
        self.assertTrue(any('daily_limit_exceeded' in item for item in logs))


    def test_phone_verification_skips_sms_when_site_user_already_verified(self):
        from platforms.freemodel import browser_oauth

        class NoPhoneProvider:
            def get_phone(self):
                raise AssertionError('should not allocate phone when site already verified')

        calls: list[str] = []

        def fake_request(_session, path, *, method='GET', body=None):
            calls.append(path)
            if path == '/api/auth/me':
                return {'ok': True, 'status': 200, 'data': {'user': {'verified_at': '2026-05-23 01:02:03'}}, 'text': ''}
            if path == '/api/billing':
                return {'ok': True, 'status': 200, 'data': {'phoneVerifiedAt': '2026-05-23 01:02:03'}, 'text': ''}
            raise AssertionError(f'unexpected request: {path}')

        with patch.object(browser_oauth, '_session_request_json', fake_request):
            result = browser_oauth._verify_phone_session(SimpleNamespace(), NoPhoneProvider(), log_fn=lambda _msg: None)

        self.assertEqual(result['verified_at'], '2026-05-23 01:02:03')
        self.assertEqual(result['phone_send_result']['already_verified'], True)
        self.assertEqual(calls, ['/api/auth/me'])

    def test_phone_send_already_verified_response_skips_waiting_for_sms(self):
        from core.base_phone import PhoneAccount
        from platforms.freemodel import browser_oauth

        class FakePhoneProvider:
            def __init__(self):
                self.waited = False

            def get_phone(self):
                return PhoneAccount(phone='13800138000', project_id='114901', token='token')

            def wait_for_code(self, *_args, **_kwargs):
                self.waited = True
                raise AssertionError('should not wait for code when send-sms reports already verified')

        phone_provider = FakePhoneProvider()
        calls: list[str] = []

        def fake_request(_session, path, *, method='GET', body=None):
            calls.append(path)
            if path in {'/api/auth/me', '/api/billing'}:
                return {'ok': True, 'status': 200, 'data': {'user': None}, 'text': ''}
            if path == '/api/phone/send-sms':
                return {
                    'ok': True,
                    'status': 200,
                    'data': {'already_verified': True, 'verified_at': '2026-05-23 02:03:04'},
                    'text': '',
                }
            raise AssertionError(f'unexpected request: {path}')

        with patch.object(browser_oauth, '_session_request_json', fake_request):
            result = browser_oauth._verify_phone_session(SimpleNamespace(), phone_provider, log_fn=lambda _msg: None)

        self.assertEqual(result['verified_at'], '2026-05-23 02:03:04')
        self.assertFalse(phone_provider.waited)
        self.assertIn('/api/phone/send-sms', calls)


class FreeModelInviteChainTests(unittest.TestCase):
    def test_invite_chain_uses_initial_code_then_success_referral(self):
        from application.tasks import _apply_freemodel_chain_invite, _extract_freemodel_next_invite_code

        state = {'next_invite_code': 'FRE-initial'}
        first_extra = _apply_freemodel_chain_invite('freemodel', {'x': '1'}, state)
        self.assertEqual(first_extra['freemodel_invite_code'], 'FRE-initial')

        account = Account(
            platform='freemodel',
            email='first@example.com',
            password='',
            token='sk-1',
            extra={'referral_code': 'FRE-second'},
        )
        self.assertEqual(_extract_freemodel_next_invite_code(account), 'FRE-second')
        state['next_invite_code'] = _extract_freemodel_next_invite_code(account)

        second_extra = _apply_freemodel_chain_invite('freemodel', {'x': '2'}, state)
        self.assertEqual(second_extra['freemodel_invite_code'], 'FRE-second')

    def test_resolve_initial_invite_prefers_configured_code(self):
        from application.tasks import _resolve_freemodel_initial_invite_code

        with patch('application.tasks._latest_freemodel_referral_code', return_value='FRE-latest'):
            self.assertEqual(
                _resolve_freemodel_initial_invite_code('freemodel', {'freemodel_invite_code': 'FRE-configured'}),
                'FRE-configured',
            )
            self.assertEqual(
                _resolve_freemodel_initial_invite_code('freemodel', {'invite_code': 'FRE-short'}),
                'FRE-short',
            )
            self.assertEqual(_resolve_freemodel_initial_invite_code('venice', {}), '')

    def test_resolve_initial_invite_falls_back_to_latest_registered_referral(self):
        from application.tasks import _resolve_freemodel_initial_invite_code

        with patch('application.tasks._latest_freemodel_referral_code', return_value='FRE-latest'):
            self.assertEqual(_resolve_freemodel_initial_invite_code('freemodel', {}), 'FRE-latest')

    def test_freemodel_chain_forces_serial_concurrency(self):
        from application.tasks import _resolve_freemodel_chain_concurrency

        logs: list[str] = []
        logger = SimpleNamespace(log=lambda message, **_kwargs: logs.append(message))

        self.assertEqual(_resolve_freemodel_chain_concurrency('freemodel', 3, logger), 1)
        self.assertTrue(any('邀请链' in item and '并发降为 1' in item for item in logs))
        self.assertEqual(_resolve_freemodel_chain_concurrency('anyapi', 3, logger), 3)


if __name__ == '__main__':
    unittest.main()
