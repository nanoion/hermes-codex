"""Explicitly gated non-model native controls. Never sends turn/start or review/start."""
import os
import tempfile
import unittest
from pathlib import Path

from hermes_codex.controls import Controls
from hermes_codex.runtime import Runtime


@unittest.skipUnless(os.environ.get('HERMES_CODEX_NATIVE_TEST') == '1', 'isolated native non-model gate')
class NativeControlTests(unittest.TestCase):
    def test_native_catalog_and_transient_thread_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = Runtime(root / 'worker', approval_handler=Controls._deny_control)
            try:
                self.assertIsNone(client.call('account/read', {'refreshToken': False})['account'])
                models = Controls.model_catalog(client)
                self.assertTrue(models)
                response = client.call('thread/start', {'cwd': str(root), 'approvalPolicy': 'on-request', 'sandbox': 'read-only', 'ephemeral': False})
                thread = response['thread']['id']
                self.assertFalse(response['thread']['turns'])
                client.call('thread/name/set', {'threadId': thread, 'name': 'Non-model parity qualification'})
                read = client.call('thread/read', {'threadId': thread, 'includeTurns': True})['thread']
                self.assertEqual(read['name'], 'Non-model parity qualification')
                # SDK 0.157 does not list a zero-turn thread as durable history.
                # Archive acceptance needs a real persisted turn and remains gated;
                # do not seed fabricated rollout files to make this test pass.
                self.assertEqual(read['turns'], [])
            finally:
                client.close()
