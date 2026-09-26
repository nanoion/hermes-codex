import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class SDKTests(unittest.TestCase):
    def test_adapter_never_inherits_credentials_or_auto_approves(self):
        from hermes_codex.sdk import SDK
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(os.environ, {'UNRELATED_SECRET': 'do-not-inherit'}):
                sdk = SDK(Path(d))
                self.assertEqual(sdk.client.config.env['UNRELATED_SECRET'], '')
                self.assertEqual(sdk.client.config.env['CODEX_HOME'], str(Path(d).resolve()))
                for method in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval'):
                    self.assertEqual(sdk.client._approval_handler(method, {}), {'decision': 'decline'})
                with self.assertRaises(PermissionError):
                    sdk.client._approval_handler('item/tool/requestUserInput', {})
                with self.assertRaises(PermissionError):
                    sdk.call('turn/start', {})

    @unittest.skipUnless(os.environ.get('HERMES_CODEX_NATIVE_TEST') == '1', 'explicit isolated non-model runtime qualification only')
    def test_native_initialize_and_read_only_sandbox(self):
        from hermes_codex.sdk import SDK
        with tempfile.TemporaryDirectory() as d:
            with SDK(Path(d)) as sdk:
                health = sdk.health()
                self.assertEqual(health['sdk_version'], '0.157.0')
                self.assertFalse(health['authenticated'])
                self.assertFalse(health['execution_ready'])
                result = sdk.qualify_sandbox()
                self.assertEqual(result['read_only_write']['exit_code'], 1)
                self.assertFalse(result['sentinel_exists'])
                self.assertEqual(result['read_only_noop']['exit_code'], 0)
                streams = (sdk.client._proc.stdout, sdk.client._proc.stderr)
            self.assertTrue(all(stream.closed for stream in streams), 'SDK qualification leaked owned pipe handles')


if __name__ == '__main__':
    unittest.main()
