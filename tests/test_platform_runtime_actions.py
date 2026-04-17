import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from domain.actions import ActionExecutionCommand


def _ensure_sqlalchemy_stub() -> None:
    if 'sqlalchemy' in sys.modules:
        return

    module = types.ModuleType('sqlalchemy')

    class UniqueConstraint:
        def __init__(self, *_args, **_kwargs):
            pass

    def inspect(*_args, **_kwargs):
        return None

    module.UniqueConstraint = UniqueConstraint
    module.inspect = inspect
    sys.modules['sqlalchemy'] = module


def _ensure_sqlmodel_stub() -> None:
    if 'sqlmodel' in sys.modules:
        return

    module = types.ModuleType('sqlmodel')

    class SQLModel:
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__()

    class Session:
        pass

    def Field(default=None, default_factory=None, **_kwargs):
        if default_factory is not None and default is None:
            return default_factory()
        return default

    def create_engine(*_args, **_kwargs):
        return object()

    def select(*_args, **_kwargs):
        return ('select', _args, _kwargs)

    def delete(*_args, **_kwargs):
        return ('delete', _args, _kwargs)

    module.Field = Field
    module.SQLModel = SQLModel
    module.Session = Session
    module.create_engine = create_engine
    module.select = select
    module.delete = delete
    sys.modules['sqlmodel'] = module


def _import_module(module_name: str):
    _ensure_sqlalchemy_stub()
    _ensure_sqlmodel_stub()
    return importlib.import_module(module_name)


def _load_attr(testcase: unittest.TestCase, module_name: str, attr_name: str):
    try:
        module = _import_module(module_name)
    except Exception as exc:  # pragma: no cover - RED 阶段用于把导入错误转成失败
        testcase.fail(f'导入 {module_name} 失败: {exc}')
    if not hasattr(module, attr_name):
        testcase.fail(f'{module_name} 缺少 {attr_name}')
    return getattr(module, attr_name)


class AccountGraphAtxpTests(unittest.TestCase):
    def test_atxp_primary_token_key_is_connection_string(self):
        primary_keys = _load_attr(self, 'core.account_graph', 'PRIMARY_TOKEN_WRITE_KEYS')

        self.assertEqual(primary_keys.get('atxp'), 'connection_string')

    def test_atxp_extra_credentials_mark_connection_string_as_primary(self):
        credentials_from_extra = _load_attr(self, 'core.account_graph', '_platform_credentials_from_extra')

        rows = credentials_from_extra(
            {
                'platform': 'atxp',
                'privy_token': 'privy-token',
                'account_id': 'acct-1',
                'connection_token': 'conn-1',
                'connection_string': 'https://accounts.atxp.ai?connection_token=conn-1&account_id=acct-1',
                'wallet_address': '0xabc',
            }
        )

        primary = next(row for row in rows if row['is_primary'])
        row_map = {row['key']: row for row in rows}

        self.assertEqual(primary['key'], 'connection_string')
        self.assertEqual(primary['credential_type'], 'token')
        self.assertIn('privy_token', row_map)
        self.assertEqual(row_map['privy_token']['credential_type'], 'token')
        self.assertIn('wallet_address', row_map)
        self.assertEqual(row_map['wallet_address']['credential_type'], 'identifier')


class PlatformRuntimePatchTests(unittest.TestCase):
    def test_collect_runtime_patch_payload_prefers_explicit_updates(self):
        collect_payload = _load_attr(self, 'infrastructure.platform_runtime', '_collect_runtime_patch_payload')

        summary, credentials = collect_payload(
            'atxp',
            'retry_clowdbot_tasks',
            {
                'connection_string': 'legacy-connection-string',
                'clowdbot_instance_id': 'legacy-instance',
                'credential_updates': {
                    'connection_string': 'explicit-connection-string',
                    'clowdbot_instance_id': 'instance-1',
                    'claimed_agent_email': 'agent@example.com',
                },
                'account_overview': {
                    'clowdbot_status': 'completed',
                    'claim_email_completed': True,
                },
                'valid': False,
            },
        )

        self.assertEqual(credentials['connection_string'], 'explicit-connection-string')
        self.assertEqual(credentials['clowdbot_instance_id'], 'instance-1')
        self.assertEqual(credentials['claimed_agent_email'], 'agent@example.com')
        self.assertEqual(summary['clowdbot_status'], 'completed')
        self.assertTrue(summary['claim_email_completed'])
        self.assertNotIn('valid', summary)

    def test_collect_runtime_patch_payload_falls_back_to_stateful_overview(self):
        collect_payload = _load_attr(self, 'infrastructure.platform_runtime', '_collect_runtime_patch_payload')

        summary, credentials = collect_payload(
            'cursor',
            'get_account_state',
            {
                'valid': True,
                'remote_user': {'email': 'demo@example.com'},
                'access_token': 'token-1',
            },
        )

        self.assertEqual(credentials, {'access_token': 'token-1'})
        self.assertTrue(summary['valid'])
        self.assertEqual(summary['remote_email'], 'demo@example.com')
        self.assertIn('有效', summary['chips'])

    def test_execute_action_uses_collected_patch_payload(self):
        runtime_module = _import_module('infrastructure.platform_runtime')
        runtime = runtime_module.PlatformRuntime()
        command = ActionExecutionCommand(platform='atxp', account_id=7, action_id='retry_clowdbot_tasks', params={})
        model = MagicMock(platform='atxp')
        model.updated_at = None
        fake_session = MagicMock()
        fake_session.get.return_value = model

        class _FakeSessionContext:
            def __enter__(self_inner):
                return fake_session

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        instance = MagicMock()
        instance.execute_action.return_value = {
            'ok': True,
            'data': {'message': 'done'},
        }

        with (
            patch.object(runtime_module, 'load_all'),
            patch.object(runtime_module, 'Session', return_value=_FakeSessionContext()),
            patch.object(runtime_module, 'get', return_value=lambda config=None: instance),
            patch.object(runtime_module, 'build_platform_account', return_value=MagicMock()),
            patch.object(runtime_module, '_collect_runtime_patch_payload', return_value=({'clowdbot_status': 'completed'}, {'connection_string': 'conn-str'})) as collect_mock,
            patch.object(runtime_module, 'patch_account_graph') as patch_graph,
        ):
            result = runtime.execute_action(command)

        self.assertTrue(result.ok)
        collect_mock.assert_called_once_with('atxp', 'retry_clowdbot_tasks', {'message': 'done'})
        patch_graph.assert_called_once()
        self.assertEqual(patch_graph.call_args.kwargs['summary_updates'], {'clowdbot_status': 'completed'})
        self.assertEqual(patch_graph.call_args.kwargs['credential_updates'], {'connection_string': 'conn-str'})
        self.assertEqual(patch_graph.call_args.kwargs['cashier_url'], None)
        fake_session.add.assert_called_once_with(model)
        fake_session.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
