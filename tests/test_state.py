import tempfile
import unittest
from pathlib import Path


class StateTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry = patch('hermes_codex.state.writer_directory', return_value=Path(directory.name) / 'writers')
        registry.start()
        self.addCleanup(registry.stop)

    def test_scoped_idempotent_handoff_survives_restart(self):
        from hermes_codex.state import Store, Conflict, Denied
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.db'
            store = Store(path)
            brief = {'objective': 'inspect', 'criteria': ['report findings']}
            task = store.submit('owner-a', str(Path(d)), 'key', brief, False)
            self.assertEqual(task['id'], store.submit('owner-a', str(Path(d)), 'key', brief, False)['id'])
            with self.assertRaises(Conflict):
                store.submit('owner-a', str(Path(d)), 'key', {'objective': 'different'}, False)
            with self.assertRaises(Denied):
                store.task('owner-b', task['id'])
            attempt = store.start('owner-a', task['id'])
            store.bind('owner-a', task['id'], attempt, 'thread', 'turn')
            store.finish('owner-a', task['id'], attempt, 'completed', {'summary': 'worker says passed'})
            self.assertEqual(store.task('owner-a', task['id'])['review_state'], 'pending_review')
            store.close()
            store = Store(path)
            self.assertEqual(store.task('owner-a', task['id'])['review_state'], 'pending_review')
            store.review('owner-a', task['id'], 'accepted', [{'criterion': 'report findings', 'status': 'passed', 'evidence': 'artifact checked'}])
            self.assertEqual(store.task('owner-a', task['id'])['review_state'], 'accepted')
            store.close()

    def test_uncertain_execution_holds_workspace_until_confirmed_stop(self):
        from hermes_codex.state import Store, Conflict
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'state.db'
            store = Store(path)
            first = store.submit('a', d, '1', {'criteria': ['x']}, True)
            second = store.submit('b', d, '2', {'criteria': ['x']}, True)
            attempt = store.start('a', first['id'])
            with self.assertRaises(Conflict):
                store.start('b', second['id'])
            store.recover()
            self.assertEqual(store.task('a', first['id'])['state'], 'unknown')
            with self.assertRaises(Conflict):
                store.start('b', second['id'])
            store.cancel('a', first['id'], 'timeout')
            self.assertEqual(store.task('a', first['id'])['state'], 'cancelling')
            store.finish('a', first['id'], attempt, 'cancelled', {'stop_confirmation': 'runtime event'})
            store.start('b', second['id'])
            store.close()

    def test_pending_cancel_and_unverified_acceptance(self):
        from hermes_codex.state import Store, Conflict
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 'state.db')
            task = store.submit('a', d, '1', {'criteria': ['x']}, False)
            store.cancel('a', task['id'], 'user')
            self.assertEqual(store.task('a', task['id'])['state'], 'cancelled')
            with self.assertRaises(Conflict):
                store.start('a', task['id'])
            task = store.submit('a', d, '2', {'criteria': ['x']}, False)
            attempt = store.start('a', task['id'])
            store.finish('a', task['id'], attempt, 'completed', {'summary': 'passed'})
            with self.assertRaises(ValueError):
                store.review('a', task['id'], 'accepted', [{'criterion': 'x', 'status': 'unverified'}])
            with self.assertRaises(Conflict):
                store.finish('a', task['id'], attempt, 'failed', {})
            store.close()


if __name__ == '__main__':
    unittest.main()
