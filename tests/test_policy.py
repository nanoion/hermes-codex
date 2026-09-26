import tempfile
import unittest
from pathlib import Path


class PolicyTests(unittest.TestCase):
    def test_traversal_symlink_and_unavailable_root_are_rejected(self):
        from hermes_codex.policy import workspace_path
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            allowed = base / 'allowed'
            allowed.mkdir()
            outside = base / 'outside'
            outside.mkdir()
            (allowed / 'escape').symlink_to(outside, target_is_directory=True)
            self.assertEqual(workspace_path(str(allowed), [str(allowed)]), str(allowed.resolve()))
            for value in [str(outside), str(allowed / 'escape'), str(allowed / '..' / 'outside'), str(allowed / 'missing')]:
                with self.assertRaises((PermissionError, ValueError)):
                    workspace_path(value, [str(allowed)])

    def test_brief_requires_bounded_scope_and_all_criteria(self):
        from hermes_codex.policy import Assignment
        from pydantic import ValidationError
        good = dict(objective='Inspect', task_type='inspect', workspace='/tmp/project', write_roots=[],
                    context=[], instructions=[], in_scope=['inspect'], out_of_scope=['write'],
                    criteria=['report'], verification=['read evidence'], sandbox='read-only',
                    network=False, approval_policy='on-request', authorization_ref='user-turn',
                    timeout_seconds=60, idempotency_key='unique')
        self.assertEqual(Assignment.model_validate(good).timeout_seconds, 60)
        for change in [{'timeout_seconds': 0}, {'timeout_seconds': True}, {'criteria': []},
                       {'owner': 'forged'}, {'sandbox': 'full-access'}, {'network': True},
                       {'criteria': ['duplicate', 'duplicate']}]:
            with self.assertRaises(ValidationError):
                Assignment.model_validate(good | change)
