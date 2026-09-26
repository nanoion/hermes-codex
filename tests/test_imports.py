"""Keep state-only subprocesses independent of the heavyweight SDK import."""
import subprocess
import sys
import unittest
from pathlib import Path


class ImportBoundaryTests(unittest.TestCase):
    def run_child(self, code):
        result = subprocess.run([sys.executable, '-c', code],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_state_operations_do_not_import_sdk_or_plugin(self):
        self.run_child('''
import sys
import tempfile
from pathlib import Path
import hermes_codex
assert hasattr(hermes_codex, 'presentation'), 'Presentation convenience access missing'
import hermes_codex.state as state
with tempfile.TemporaryDirectory() as d:
    state.writer_directory = lambda: Path(d) / 'writers'
    store = state.Store(Path(d) / 'state.db')
    try:
        task = store.submit('owner', d, 'one', {}, True)
        store.start('owner', task['id'])
    finally:
        store.close()
assert not any(m == 'openai_codex' or m.startswith('openai_codex.') for m in sys.modules), 'State operations eagerly imported SDK'
assert 'hermes_codex.service' not in sys.modules
assert 'hermes_codex.plugin' not in sys.modules
''')

    def test_public_exports_preserve_canonical_objects(self):
        import hermes_codex
        from hermes_codex import Service, register
        from hermes_codex.service import Service as implementation
        from hermes_codex.plugin import register as registration
        self.assertIs(Service, implementation)
        self.assertIs(hermes_codex.Service, implementation)
        self.assertIs(register, registration)
        self.assertIs(hermes_codex.register, registration)
        self.assertEqual(hermes_codex.__all__, ['Service', 'register'])
        self.assertFalse(hasattr(hermes_codex, 'missing_export'))
