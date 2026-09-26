"""Billable smoke: explicit opt-in AND an explicitly named isolated worker home.

Never run this automatically. The operator must first authorize identity/model/cost
and provision authentication in the dedicated home without copying another profile.
"""
import os
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get('HERMES_CODEX_LIVE_APPROVED') == 'I_AUTHORIZE_ONE_HARMLESS_TURN',
                     'requires explicit user authorization for one billable harmless turn')
class LiveSDKSmoke(unittest.TestCase):
    def test_one_harmless_read_only_turn(self):
        from hermes_codex.runtime import Runtime
        home = os.environ.get('HERMES_CODEX_APPROVED_WORKER_HOME')
        self.assertTrue(home, 'Name an explicitly authorized dedicated worker home')
        path = Path(home)
        self.assertTrue(path.is_absolute() and path.is_dir() and not path.is_symlink())
        self.assertNotEqual(path.resolve(), (Path.home() / '.codex').resolve(), 'Default host credential reuse is not authorized by this smoke')
        runtime = Runtime(path, approval_handler=lambda method, params: {'decision': 'decline'})
        events = queue.Queue()
        def pump():
            try:
                while True:
                    events.put(runtime.next_event())
            except Exception as exc:
                events.put(exc)
        try:
            account = runtime.call('account/read', {'refreshToken': False})
            self.assertIsNotNone(account['account'], 'Dedicated worker login required')
            with tempfile.TemporaryDirectory(prefix='codex-approved-smoke-') as workspace:
                params = {'cwd': workspace, 'sandbox': 'read-only', 'approvalPolicy': 'on-request'}
                if os.environ.get('HERMES_CODEX_APPROVED_MODEL'):
                    params['model'] = os.environ['HERMES_CODEX_APPROVED_MODEL']
                thread = runtime.call('thread/start', params)['thread']['id']
                turn = runtime.call('turn/start', {'threadId': thread, 'cwd': workspace,
                    'sandboxPolicy': {'type': 'readOnly', 'networkAccess': False}, 'approvalPolicy': 'on-request',
                    'input': [{'type': 'text', 'text': 'Reply with exactly SDK_SMOKE_OK. Do not call tools, read files, or change anything.'}]})['turn']['id']
                threading.Thread(target=pump, daemon=True).start()
                deadline = time.monotonic() + 60
                output = []
                while time.monotonic() < deadline:
                    try:
                        event = events.get(timeout=max(.01, deadline - time.monotonic()))
                    except queue.Empty:
                        runtime.call('turn/interrupt', {'threadId': thread, 'turnId': turn})
                        self.fail('Timeout: interruption requested, termination unconfirmed')
                    if isinstance(event, Exception):
                        raise event
                    data = event['params']
                    if data.get('threadId') != thread:
                        continue
                    if event['method'] == 'item/agentMessage/delta':
                        output.append(data['delta'])
                    if event['method'] == 'turn/completed' and data['turn']['id'] == turn:
                        self.assertEqual(data['turn']['status'], 'completed')
                        self.assertIn('SDK_SMOKE_OK', ''.join(output))
                        print('Live SDK identifiers:', thread, turn)
                        return
                self.fail('No confirmed terminal event')
        finally:
            runtime.close()
