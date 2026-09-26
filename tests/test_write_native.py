"""Non-model native permission qualification in disposable workspaces only."""
import os
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get('HERMES_CODEX_NATIVE_TEST') == '1', 'isolated native non-model gate')
class NativeWritePolicyTests(unittest.TestCase):
    def test_native_write_scope_symlink_escape_and_thread_policy_readback(self):
        from hermes_codex import access
        from hermes_codex.sdk import SDK
        from openai_codex.generated.v2_all import CommandExecResponse, ThreadStartResponse
        self.assertTrue(hasattr(access, 'thread_options'), 'Persistent native thread policy missing')
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            home, workspace, outside = root / 'home', root / 'workspace', root / 'outside'
            for path in (home, workspace, outside):
                path.mkdir()
            (workspace / 'escape').symlink_to(outside, target_is_directory=True)
            brief = {'workspace': str(workspace), 'write_roots': [str(workspace)], 'sandbox': 'workspace-write', 'network': False}
            with SDK(home) as sdk:
                response = sdk.client.request('thread/start', access.thread_options(brief), response_model=ThreadStartResponse).model_dump(mode='json', by_alias=True)
                self.assertEqual(response['sandbox']['type'], 'workspaceWrite')
                self.assertTrue(response['sandbox']['excludeSlashTmp'])
                self.assertTrue(response['sandbox']['excludeTmpdirEnvVar'])
                self.assertFalse(response['sandbox']['networkAccess'])
                for destination, allowed in [(workspace / 'allowed', True), (outside / 'denied', False), (workspace / 'escape' / 'denied-link', False)]:
                    result = sdk.client.request('command/exec', {'command': ['/usr/bin/touch', str(destination)], 'cwd': str(workspace),
                        'sandboxPolicy': access.sandbox(brief), 'timeoutMs': 10000, 'outputBytesCap': 2048}, response_model=CommandExecResponse)
                    self.assertEqual(result.exit_code == 0, allowed)
                    self.assertEqual(destination.exists(), allowed)
