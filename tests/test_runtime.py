import os
import tempfile
import unittest
from pathlib import Path


class RuntimeTests(unittest.TestCase):
    def test_runtime_request_timeout_closes_only_its_client(self):
        import threading
        from types import SimpleNamespace
        from unittest.mock import patch
        from hermes_codex.runtime import Runtime
        class Client:
            _proc = None
            def __init__(self, *a, **kw):
                self.closed = threading.Event()
            def start(self):
                pass
            def initialize(self):
                return SimpleNamespace(model_dump=lambda **kw: {'userAgent': 'offline'})
            def request(self, *a, **kw):
                self.closed.wait(2)
                raise ConnectionError('closed')
            def close(self):
                self.closed.set()
        with tempfile.TemporaryDirectory() as directory, patch('hermes_codex.runtime.CodexClient', Client):
            runtime = Runtime(Path(directory), approval_handler=lambda *a: {'decision': 'decline'})
            import inspect
            self.assertIn('timeout', inspect.signature(runtime.call).parameters, 'Bounded SDK requests missing')
            with self.assertRaises(TimeoutError):
                runtime.call('account/read', {'refreshToken': False}, timeout=.02)
            self.assertTrue(runtime.client.closed.is_set())
            runtime.close()

    def test_allowlist_uses_real_generated_response_models(self):
        from hermes_codex.runtime import OPERATIONS
        from openai_codex.generated import v2_all
        for method, name in OPERATIONS.items():
            with self.subTest(method=method):
                model = getattr(v2_all, name)
                self.assertIsInstance(model.model_json_schema(), dict)

    @unittest.skipUnless(os.environ.get('HERMES_CODEX_NATIVE_TEST') == '1', 'isolated non-model SDK runtime only')
    def test_actual_runtime_initialization_and_isolated_account(self):
        from hermes_codex.runtime import Runtime
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory), approval_handler=lambda method, params: {'decision': 'decline'})
            try:
                account = runtime.call('account/read', {'refreshToken': False})
                self.assertIsNone(account['account'])
                self.assertTrue(account['requiresOpenaiAuth'])
                with self.assertRaises(PermissionError):
                    runtime.call('command/exec', {'command': ['false']})
            finally:
                runtime.close()
