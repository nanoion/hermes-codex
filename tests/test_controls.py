"""Offline SDK boundary tests; never evidence of authenticated native parity."""
import copy
import tempfile
import time
import unittest
from pathlib import Path

from hermes_codex.service import Service
from hermes_codex.state import Conflict, Denied
from test_worker import brief


class Backend:
    def __init__(self):
        self.calls = []
        self.thread = {'id': 'thread-1', 'name': 'Original', 'turns': [], 'status': {'type': 'idle'}}
        self.archived = False
        self.fail_after_write = False
        self.goal = None
        self.logged_in = False
        self.homes = []
        import queue
        self.events = queue.Queue()

    def factory(self, home, *, approval_handler):
        self.homes.append(Path(home))
        backend = self
        class Client:
            identity = {'userAgent': 'offline-test'}
            def __init__(self):
                import queue
                self.events = queue.Queue()
            def call(self, method, params):
                backend.calls.append((method, copy.deepcopy(params)))
                if method == 'thread/read':
                    return {'thread': copy.deepcopy(backend.thread)}
                if method == 'thread/name/set':
                    backend.thread['name'] = params['name']
                    if backend.fail_after_write:
                        raise ConnectionError('lost reply')
                    return {}
                if method in {'thread/archive', 'thread/unarchive'}:
                    backend.archived = method == 'thread/archive'
                    return {'thread': copy.deepcopy(backend.thread)} if not backend.archived else {}
                if method == 'thread/list':
                    return {'data': [copy.deepcopy(backend.thread)] if params.get('archived', False) == backend.archived else [], 'nextCursor': None}
                if method == 'thread/goal/get':
                    return {'goal': copy.deepcopy(backend.goal)}
                if method == 'thread/goal/clear':
                    backend.goal = None
                    return {}
                if method == 'thread/goal/set':
                    if backend.goal is None or params.get('objective', backend.goal['objective']) != backend.goal['objective']:
                        backend.goal = {'threadId': params['threadId'], 'objective': params['objective'], 'tokensUsed': 0, 'timeUsedSeconds': 0, 'tokenBudget': None, 'status': 'active'}
                    backend.goal.update({k: v for k, v in params.items() if k != 'threadId'})
                    if backend.fail_after_write:
                        raise ConnectionError('lost goal reply')
                    return {'goal': copy.deepcopy(backend.goal)}
                if method == 'model/list':
                    models = [{'id': 'test', 'model': 'test-model', 'isDefault': True, 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}, {'reasoningEffort': 'high'}], 'serviceTiers': [{'id': 'fast'}, {'id': 'flex'}]}]
                    return {'data': models if not params.get('cursor') else [{'id': 'second', 'model': 'second-model', 'isDefault': False, 'supportedReasoningEfforts': [{'reasoningEffort': 'low'}], 'serviceTiers': []}], 'nextCursor': 'second-page' if not params.get('cursor') else None}
                if method == 'account/login/start':
                    return {'type': 'chatgpt', 'loginId': 'login-one', 'authUrl': 'https://auth.openai.com/oauth/authorize?state=private-link'}
                if method == 'account/login/cancel':
                    return {'status': 'canceled'}
                if method == 'account/read':
                    return {'account': {'type': 'chatgpt', 'email': 'isolated@example.test', 'planType': 'test'} if backend.logged_in else None, 'requiresOpenaiAuth': True}
                if method == 'account/rateLimits/read':
                    return {'rateLimits': None}
                raise AssertionError(method)
            def next_event(self):
                return self.events.get()
            def close(self):
                self.events.put(None)
        return Client()


class ControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from unittest.mock import patch
        registry = patch('hermes_codex.state.writer_directory', return_value=self.root / 'writers')
        registry.start()
        self.addCleanup(registry.stop)
        self.backend = Backend()
        self.svc = Service(self.root / 'state', [str(self.root)], transport_factory=self.backend.factory)
        self.addCleanup(self.svc.close)
        self.owner = 'alice'
        task = self.svc.store.submit(self.owner, str(self.root), 'one', brief(self.root), False)
        self.task = task['id']
        attempt = self.svc.store.start(self.owner, self.task)
        self.svc.store.bind(self.owner, self.task, attempt, 'thread-1', 'turn-1')
        self.svc.store.finish(self.owner, self.task, attempt, 'completed', {'verified': False})
        self.svc.store.put(self.owner, 'binding', self.task, {'task': self.task, 'thread': 'thread-1', 'provider': 'default', 'policy_verified': True, 'effective': {'model': 'test-model', 'sandbox': {'type': 'readOnly', 'networkAccess': False}}})

    def wait_job(self, job):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            current = self.svc.control_status(self.owner, job['id'])
            if current['state'] not in {'pending', 'running'}:
                return current
            time.sleep(.005)
        self.fail('control job did not complete')

    def run_control(self, action, **payload):
        job = self.svc.control(self.owner, action, payload)
        result = self.wait_job(job)
        self.assertEqual(result['state'], 'completed', result)
        return result['result']

    def test_account_login_secure_delivery_status_switch_cancel_and_recovery(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('account', CONTROL_FIELDS, 'Isolated account workflows missing')
        presented = []
        self.svc.login_presenter = lambda url: presented.append(url)
        login = self.run_control('account', operation='login', name='Personal')
        self.assertEqual(login['state'], 'pending')
        self.assertIn('private-link', presented[0])
        rows = self.svc.store.db.execute('SELECT value FROM documents').fetchall()
        self.assertNotIn('private-link', str([r[0] for r in rows]))
        self.backend.logged_in = True
        ready = self.run_control('account', operation='status', name='Personal')
        self.assertEqual(ready['state'], 'ready')
        self.run_control('account', operation='switch', name='Personal')
        selected = self.svc.store.get(self.owner, 'selected-account', 'default')
        new = self.svc.store.submit(self.owner, str(self.root), 'account-route', brief(self.root, 'account-route'), False)
        self.svc.store.put(self.owner, 'route', new['id'], {'provider': 'default', 'account': selected['id']})
        self.assertEqual(self.svc.worker_home(self.owner, new['id']), self.backend.homes[-1])
        self.svc.store.cancel(self.owner, new['id'], 'test cleanup')
        self.backend.logged_in = False
        pending = self.run_control('account', operation='login', name='Other')
        self.assertEqual(self.run_control('account', operation='cancel', name='Other')['state'], 'cancelled')
        self.run_control('account', operation='login', name='Recover')
        self.svc.close()
        self.svc = Service(self.root / 'state', [str(self.root)], transport_factory=self.backend.factory)
        self.addCleanup(self.svc.close)
        listed = self.run_control('account', operation='list')
        self.assertEqual(next(a for a in listed['data'] if a['name'] == 'Recover')['state'], 'stale')
        self.assertFalse(any(m in {'turn/start', 'thread/start'} for m, p in self.backend.calls))

    def test_provider_switch_busy_guards_defaults_and_sticky_routes(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('providers', CONTROL_FIELDS, 'Provider profiles missing')
        self.svc.profiles = {'default': {'modelProvider': None, 'defaults': {}}, 'alternate': {'modelProvider': 'openai', 'defaults': {'model': 'test-model'}}}
        providers = self.run_control('providers')
        self.assertEqual({p['id'] for p in providers['data']}, {'default', 'alternate'})
        self.run_control('open', task=self.task)
        selected = self.run_control('provider-select', provider='alternate')
        self.assertEqual(selected['configured']['model'], 'test-model')
        self.assertEqual(self.svc.store.get(self.owner, 'selection', 'current'), {})
        self.assertEqual(self.svc.worker_home(self.owner, self.task), self.root / 'state' / 'workers' / __import__('hashlib').sha256(self.owner.encode()).hexdigest() / self.task)
        changed = self.wait_job(self.svc.control(self.owner, 'thread-draft', {'task': self.task, 'mutation': 'rename', 'name': 'wrong backend'}))
        self.assertEqual(changed['state'], 'failed')
        self.run_control('provider-select', provider='default')
        self.assertEqual(self.svc.store.get(self.owner, 'selection', 'current')['task'], self.task)
        self.svc.store.put(self.owner, 'plan', 'pending-provider-plan', {'id': 'pending-provider-plan', 'task': self.task, 'state': 'pending'})
        blocked = self.wait_job(self.svc.control(self.owner, 'provider-select', {'provider': 'alternate'}))
        self.assertEqual(blocked['state'], 'failed')
        self.assertEqual(self.svc.settings(self.owner)['provider'], 'default')

    def test_scope_snapshot_reports_pending_work_and_history_preferences(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('snapshot', CONTROL_FIELDS, 'Comprehensive scoped status missing')
        self.run_control('settings-set', task=self.task, values={'plan_confirmation': False, 'plan_history': False})
        attempt = self.svc.store.task(self.owner, self.task)['attempts'][-1]['id']
        self.svc.save_plan(self.owner, self.task, attempt, 'thread-1', {'items': [{'type': 'plan', 'id': 'history', 'text': 'Recorded only'}]})
        self.assertIsNone(self.svc.pending_plan(self.owner, self.task))
        snapshot = self.run_control('snapshot', task=self.task)
        self.assertEqual(snapshot['plans'], [])
        self.assertFalse(snapshot['configured']['plan_confirmation'])
        self.assertEqual(snapshot['health']['source'], 'unavailable')
        self.run_control('settings-set', task=self.task, values={'plan_history': True})
        snapshot = self.run_control('snapshot', task=self.task)
        self.assertEqual(snapshot['plans'][0]['state'], 'recorded')

    def test_pending_plan_recovery_cancel_and_revision(self):
        attempt = self.svc.store.task(self.owner, self.task)['attempts'][-1]['id']
        self.svc.save_plan(self.owner, self.task, attempt, 'thread-1', {'items': [{'type': 'plan', 'id': 'p1', 'text': 'First revision'}]})
        first = self.svc.pending_plan(self.owner, self.task)
        self.svc.close()
        self.svc = Service(self.root / 'state', [str(self.root)], transport_factory=self.backend.factory)
        self.addCleanup(self.svc.close)
        self.assertEqual(self.svc.plan_action(self.owner, first['id'], 'show')['text'], 'First revision')
        self.assertEqual(self.svc.plan_action(self.owner, first['id'], 'cancel')['state'], 'cancelled')
        self.svc.save_plan(self.owner, self.task, attempt, 'thread-1', {'items': [{'type': 'plan', 'id': 'p2', 'text': 'Second revision'}]})
        second = self.svc.pending_plan(self.owner, self.task)
        with self.assertRaises(Conflict):
            self.svc.plan_action(self.owner, first['id'], 'confirm')
        from test_worker import Transport
        self.svc.factory = Transport
        self.assertEqual(self.svc.plan_action(self.owner, second['id'], 'revise', 'Focus on security')['state'], 'revising')
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.svc.status(self.owner, self.task)['state'] != 'running':
            time.sleep(.01)
        client = self.svc.clients[self.task]
        params = next(p for m, p in client.calls if m == 'turn/start')
        self.assertEqual(params['collaborationMode']['mode'], 'plan')
        self.assertEqual(params['input'][0]['text'], 'Focus on security')

    def test_archive_permission_confirmation_cleanup_and_stale_rename(self):
        denied = self.wait_job(self.svc.control(self.owner, 'thread-draft', {'task': self.task, 'mutation': 'archive'}))
        self.assertEqual(denied['state'], 'failed')
        self.run_control('settings-set', task=self.task, values={'allow_archive': True})
        self.run_control('open', task=self.task)
        draft = self.run_control('thread-draft', task=self.task, mutation='archive')
        self.run_control('thread-confirm', id=draft['id'], decision='confirm')
        self.assertEqual(self.svc.store.get(self.owner, 'selection', 'current'), {})
        self.assertEqual(self.run_control('threads', archived=False)['data'], [])
        self.assertEqual(len(self.run_control('threads', archived=True)['data']), 1)
        draft = self.run_control('thread-draft', task=self.task, mutation='unarchive')
        self.run_control('thread-confirm', id=draft['id'], decision='confirm')
        self.assertFalse(self.svc.store.get(self.owner, 'binding', self.task)['archived'])
        draft = self.run_control('thread-draft', task=self.task, mutation='rename', name='Intended')
        self.backend.thread['name'] = 'Changed by another native client'
        stale = self.wait_job(self.svc.control(self.owner, 'thread-confirm', {'id': draft['id'], 'decision': 'confirm'}))
        self.assertEqual(stale['state'], 'failed')
        self.assertEqual(self.backend.thread['name'], 'Changed by another native client')

    def test_desktop_controls_report_headless_and_sync_without_false_focus(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('reveal', CONTROL_FIELDS, 'Desktop controls not wired')
        from unittest.mock import patch
        failed = self.wait_job(self.svc.control(self.owner, 'reveal', {'task': self.task}))
        self.assertEqual(failed['state'], 'failed')
        self.svc.desktop_command = ['/usr/bin/xdg-open']
        self.svc.store.put(self.owner, 'settings', 'current', self.svc.settings(self.owner) | {'sync_on_open': True})
        with patch('hermes_codex.presentation.reveal', return_value={'requested': True, 'focused': None, 'thread': 'thread-1'}) as reveal:
            result = self.run_control('open', task=self.task)
            self.assertIsNone(result['desktop']['focused'])
            reveal.assert_called_once_with('thread-1', ['/usr/bin/xdg-open'])

    def test_goal_native_events_are_tracked_without_synthetic_turns(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('goal', CONTROL_FIELDS)
        self.run_control('goal', task=self.task, operation='set', objective='Observe native continuation', budget=100)
        client = self.svc.goal_clients[self.task]
        client.events.put({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': {'id': 'native-2', 'status': 'inProgress'}}})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.svc.status(self.owner, self.task)['state'] != 'running':
            time.sleep(.01)
        self.assertEqual(self.svc.status(self.owner, self.task)['state'], 'running')
        self.run_control('goal', task=self.task, operation='pause')
        self.assertEqual(self.svc.status(self.owner, self.task)['state'], 'running')
        blocked = self.wait_job(self.svc.control(self.owner, 'goal', {'task': self.task, 'operation': 'resume'}))
        self.assertEqual(blocked['state'], 'failed')
        client.events.put({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'native-2', 'status': 'completed', 'items': []}}})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.svc.status(self.owner, self.task)['state'] != 'completed':
            time.sleep(.01)
        self.assertEqual(self.svc.status(self.owner, self.task)['review_state'], 'pending_review')
        client.events.put({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': {'id': 'native-2', 'status': 'inProgress'}}})
        client.events.put({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': {'id': 'native-2', 'status': 'completed', 'items': []}}})
        client.events.put({'method': 'thread/goal/updated', 'params': {'threadId': 'thread-1', 'goal': self.backend.goal | {'status': 'budgetLimited'}}})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.svc.store.get(self.owner, 'goal', self.task)['state'] != 'budgetLimited':
            time.sleep(.01)
        self.assertEqual(len(self.svc.status(self.owner, self.task)['attempts']), 2)
        self.assertEqual(self.svc.store.get(self.owner, 'goal', self.task)['state'], 'budgetLimited')
        self.assertFalse(any(m in {'turn/start', 'turn/interrupt'} for m, p in self.backend.calls))

    def test_reconcile_exact_turn_and_guarded_maintenance(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('reconcile', CONTROL_FIELDS, 'Uncertain submission reconciliation missing')
        self.svc.store.db.execute("UPDATE tasks SET state='unknown' WHERE id=?", (self.task,))
        self.svc.store.db.execute("UPDATE attempts SET state='unknown' WHERE task=?", (self.task,))
        self.backend.thread['turns'] = [{'id': 'other-turn', 'status': 'completed', 'items': []}]
        result = self.run_control('reconcile', task=self.task)
        self.assertEqual(result['state'], 'unknown')
        blocked = self.wait_job(self.svc.control(self.owner, 'maintenance', {'task': self.task, 'operation': 'restart'}))
        self.assertEqual(blocked['state'], 'failed')
        self.backend.thread['turns'].append({'id': 'turn-1', 'status': 'interrupted', 'items': []})
        result = self.run_control('reconcile', task=self.task)
        self.assertEqual(result['state'], 'cancelled')
        self.assertEqual(result['review_state'], 'pending_review')
        health = self.run_control('maintenance', task=self.task, operation='restart')
        self.assertTrue(health['ready'])
        self.assertFalse(health['execution_ready'])
        self.assertEqual(health['identity']['userAgent'], 'offline-test')
        with self.assertRaises(Denied):
            self.svc.store.task('wrong', self.task)
        self.assertFalse(any(m == 'turn/start' for m, p in self.backend.calls))

    def test_attachment_album_staging_once_clear_and_escape_denial(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('attachment-stage', CONTROL_FIELDS, 'Attachment staging missing')
        source = self.root / 'document.txt'
        source.write_text('Document contents')
        one = self.run_control('attachment-stage', task=self.task, path=str(source), kind='document', album='album', key='one', caption='First')
        two = self.run_control('attachment-stage', task=self.task, path=str(source), kind='audio', album='album', key='two', caption='Second')
        self.assertEqual(one['id'], two['id'])
        self.assertEqual(len(two['files']), 2)
        duplicate = self.run_control('attachment-stage', task=self.task, path=str(source), kind='document', album='album', key='one', caption='First')
        self.assertEqual(len(duplicate['files']), 2)
        source.write_text('changed')
        conflict = self.wait_job(self.svc.control(self.owner, 'attachment-stage', {'task': self.task, 'path': str(source), 'kind': 'document', 'album': 'album', 'key': 'one', 'caption': 'First'}))
        self.assertEqual(conflict['state'], 'failed')
        # Queue without running the fake control backend as a worker.
        self.svc.store.db.execute("UPDATE tasks SET state='running' WHERE id=?", (self.task,))
        queued = self.svc.followup(self.owner, self.task, 'message', 'Inspect attachments')
        self.assertEqual(len(queued['attachments']), 2)
        self.assertEqual(self.svc.followup(self.owner, self.task, 'message', 'Inspect attachments')['id'], queued['id'])
        second = self.svc.followup(self.owner, self.task, 'next', 'No attachment reuse')
        self.assertEqual(second['attachments'], [])
        self.assertEqual(self.svc.store.get(self.owner, 'attachment', one['id'])['state'], 'queued')
        with tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / 'outside.txt'
            secret.write_text('not in scope')
            link = self.root / 'escape'
            link.symlink_to(secret)
            denied = self.wait_job(self.svc.control(self.owner, 'attachment-stage', {'task': self.task, 'path': str(link), 'kind': 'document', 'key': 'escape'}))
            self.assertEqual(denied['state'], 'failed')
        batch = self.run_control('attachment-stage', task=self.task, path=str(source), kind='video', key='clear')
        cleared = self.run_control('attachment-clear', task=self.task, id=batch['id'])
        self.assertEqual(cleared['state'], 'cleared')
        self.svc.close()
        self.svc = Service(self.root / 'state', [str(self.root)], transport_factory=self.backend.factory)
        self.addCleanup(self.svc.close)
        self.assertEqual(self.svc.store.get(self.owner, 'attachment', one['id'])['state'], 'queued')

    def test_native_goal_budget_preservation_pause_clear_and_ambiguity(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('goal', CONTROL_FIELDS, 'Native goal lifecycle missing')
        result = self.run_control('goal', task=self.task, operation='set', objective='Inspect everything', budget=100)
        self.assertEqual(result['goal']['tokenBudget'], 100)
        self.backend.goal['tokensUsed'] = 90
        self.backend.goal['timeUsedSeconds'] = 12
        result = self.run_control('goal', task=self.task, operation='pause')
        self.assertEqual(result['goal']['status'], 'paused')
        self.assertEqual(result['goal']['tokensUsed'], 90)
        result = self.run_control('goal', task=self.task, operation='resume')
        self.assertEqual(result['goal']['tokensUsed'], 90)
        self.assertEqual(result['goal']['tokenBudget'], 100)
        self.backend.goal.update(status='budgetLimited', tokensUsed=100)
        for budget in (None, 100, 0, True):
            p = {'task': self.task, 'operation': 'resume'}
            if budget is not None:
                p['budget'] = budget
            rejected = self.wait_job(self.svc.control(self.owner, 'goal', p))
            self.assertEqual(rejected['state'], 'failed')
        result = self.run_control('goal', task=self.task, operation='resume', budget=200)
        self.assertEqual(result['goal']['tokensUsed'], 100)
        self.assertEqual(result['goal']['tokenBudget'], 200)
        self.run_control('goal', task=self.task, operation='pause')
        result = self.run_control('goal', task=self.task, operation='set', objective='New objective', budget=50)
        self.assertEqual(result['goal']['tokensUsed'], 0)
        self.run_control('goal', task=self.task, operation='clear')
        self.assertIsNone(self.run_control('goal', task=self.task, operation='get')['goal'])
        self.assertFalse(any(m == 'turn/interrupt' for m, p in self.backend.calls))
        self.backend.fail_after_write = True
        uncertain = self.wait_job(self.svc.control(self.owner, 'goal', {'task': self.task, 'operation': 'set', 'objective': 'Maybe started', 'budget': 30}))
        self.assertEqual(uncertain['state'], 'unknown')
        count = sum(m == 'thread/goal/set' for m, p in self.backend.calls)
        self.assertEqual(self.run_control('goal', task=self.task, operation='get')['goal']['objective'], 'Maybe started')
        self.assertEqual(sum(m == 'thread/goal/set' for m, p in self.backend.calls), count)

    def test_models_settings_defaults_capability_validation_and_busy_guard(self):
        from hermes_codex.controls import CONTROL_FIELDS
        self.assertIn('models', CONTROL_FIELDS, 'Model discovery missing')
        models = self.run_control('models', task=self.task)
        self.assertEqual(len(models['data']), 2)
        first = self.run_control('models', task=self.task, limit=1)
        self.assertEqual(len(first['data']), 1)
        second = self.run_control('models', task=self.task, page=first['next'])
        self.assertEqual(second['data'][0]['model'], 'second-model')
        self.assertIsNone(second['next'])
        self.svc.model_variants = {'careful': {'model': 'test-model', 'effort': 'high', 'serviceTier': 'fast'}}
        variant = self.run_control('settings-set', task=self.task, values={'variant': 'careful'})
        self.assertEqual(variant['configured']['effort'], 'high')
        selected = self.run_control('settings-set', task=self.task, values={'model': 'test-model', 'effort': 'high', 'serviceTier': 'fast', 'mode': 'plan', 'auto_queue': False, 'allow_archive': True})
        self.assertEqual(selected['configured']['mode'], 'plan')
        self.assertEqual(selected['effective']['model'], 'test-model')
        self.assertNotIn('mode', selected['effective'])
        for values in ({'model': 'invented'}, {'effort': 'ultra'}, {'serviceTier': 'priority'}, {'mode': 'invalid'}, {'auto_queue': 'yes'}, {'provider': 'other'}, {'model': 'second-model'}):
            with self.subTest(values=values):
                result = self.wait_job(self.svc.control(self.owner, 'settings-set', {'task': self.task, 'values': values}))
                self.assertEqual(result['state'], 'failed')
        reset = self.run_control('settings-set', task=self.task, values={'model': None, 'effort': None, 'serviceTier': None, 'mode': 'execute'})
        self.assertIsNone(reset['configured']['model'])
        self.svc.store.db.execute("UPDATE tasks SET state='unknown' WHERE id=?", (self.task,))
        result = self.wait_job(self.svc.control(self.owner, 'settings-set', {'task': self.task, 'values': {'mode': 'plan'}}))
        self.assertEqual(result['state'], 'failed')

    def test_browse_native_pages_archives_and_route_bound_cursors(self):
        original_factory = self.svc.factory
        threads = {f'thread-{i}': {'id': f'thread-{i}', 'name': f'Report {i}', 'status': {'type': 'idle'},
                   'turns': [{'id': f'turn-{i}', 'status': 'interrupted' if i == 2 else 'completed'}]} for i in range(1, 4)}
        def factory(*args, **kwargs):
            client = original_factory(*args, **kwargs)
            original_call = client.call
            def call(method, params):
                if method == 'thread/list':
                    self.backend.calls.append((method, copy.deepcopy(params)))
                    if params.get('archived'):
                        return {'data': [threads['thread-3']], 'nextCursor': None}
                    return {'data': [threads['thread-1']] if not params.get('cursor') else [threads['thread-2']],
                            'nextCursor': 'page-two' if not params.get('cursor') else None}
                if method == 'thread/read':
                    return {'thread': threads[params['threadId']]}
                return original_call(method, params)
            client.call = call
            return client
        self.svc.factory = factory
        for i in (2, 3):
            task = self.svc.store.submit(self.owner, str(self.root), str(i), brief(self.root, str(i)), False)
            self.svc.store.cancel(self.owner, task['id'], 'fixture')
            self.svc.store.put(self.owner, 'binding', task['id'], {'task': task['id'], 'thread': f'thread-{i}'})
        first = self.run_control('threads', search='Report', limit=1)
        self.assertEqual(first['total'], 2, 'Must use native archive state, not cached binding flags')
        second = self.run_control('threads', page=first['next'])
        self.assertEqual(second['data'][0]['turns'][0]['status'], 'interrupted')
        previous = self.run_control('threads', page=second['previous'])
        self.assertEqual(previous['data'], first['data'])
        self.assertTrue(any(m == 'thread/list' and p.get('cursor') == 'page-two' for m, p in self.backend.calls))
        self.assertTrue(all('appServer' in p.get('sourceKinds', []) for m, p in self.backend.calls if m == 'thread/list'))
        archived = self.run_control('threads', archived=True)
        self.assertEqual([x['thread'] for x in archived['data']], ['thread-3'])
        self.svc.profiles['other'] = {'modelProvider': None, 'defaults': {}}
        self.run_control('provider-select', provider='other')
        stale = self.wait_job(self.svc.control(self.owner, 'threads', {'page': first['next']}))
        self.assertEqual(stale['state'], 'failed')
        self.assertEqual(self.run_control('threads')['data'], [])

    def test_thread_browse_open_confirmed_mutations_and_recovery(self):
        self.assertTrue(hasattr(self.svc, 'control'), 'Scoped async controls missing')
        page = self.run_control('threads', search='Original', archived=False, limit=1)
        self.assertEqual(page['data'][0]['task'], self.task)
        self.assertIsNone(page['next'])
        opened = self.run_control('open', task=self.task)
        self.assertEqual(opened['thread']['id'], 'thread-1')
        draft = self.run_control('thread-draft', task=self.task, mutation='rename', name='Renamed')
        with self.assertRaises(Denied):
            self.svc.control_status('bob', draft['id'])
        cancelled = self.run_control('thread-confirm', id=draft['id'], decision='cancel')
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(self.backend.thread['name'], 'Original')
        draft = self.run_control('thread-draft', task=self.task, mutation='rename', name='Renamed')
        changed = self.run_control('thread-confirm', id=draft['id'], decision='confirm')
        self.assertEqual(changed['state'], 'applied')
        self.assertEqual(self.backend.thread['name'], 'Renamed')
        replay = self.wait_job(self.svc.control(self.owner, 'thread-confirm', {'id': draft['id'], 'decision': 'confirm'}))
        self.assertEqual(replay['state'], 'failed')
        draft = self.run_control('thread-draft', task=self.task, mutation='rename', name='Uncertain')
        self.backend.fail_after_write = True
        result = self.wait_job(self.svc.control(self.owner, 'thread-confirm', {'id': draft['id'], 'decision': 'confirm'}))
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(self.svc.store.get(self.owner, 'thread-draft', draft['id'])['state'], 'applying')
        self.svc.close()
        self.svc = Service(self.root / 'state', [str(self.root)], transport_factory=self.backend.factory)
        self.addCleanup(self.svc.close)
        self.assertEqual(self.svc.store.get(self.owner, 'thread-draft', draft['id'])['state'], 'unknown')
        writes = sum(m == 'thread/name/set' for m, p in self.backend.calls)
        reconciled = self.run_control('thread-reconcile', id=draft['id'])
        self.assertEqual(reconciled['state'], 'reconciled_applied')
        self.assertEqual(sum(m == 'thread/name/set' for m, p in self.backend.calls), writes)
        self.assertEqual(self.run_control('threads', search='no-match', archived=False, limit=20)['data'], [])
