"""Regression coverage for remaining gaps; transports are explicitly offline."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex.service import Service, WorkflowStore
from hermes_codex.state import Store, Conflict, Denied
from test_worker import brief, Transport
import test_controls


class RemainingControlsTests(unittest.TestCase):
    setUp = test_controls.ControlsTests.setUp
    wait_job = test_controls.ControlsTests.wait_job
    run_control = test_controls.ControlsTests.run_control

    def test_reconcile_plan_restores_confirmation_before_any_continuation(self):
        attempt = self.svc.store.task(self.owner, self.task)['attempts'][-1]
        self.svc.store.put(self.owner, 'execution', attempt['id'], {'task': self.task, 'attempt': attempt['id'], 'mode': 'plan'})
        self.svc.store.db.execute("UPDATE tasks SET state='unknown' WHERE id=?", (self.task,))
        self.svc.store.db.execute("UPDATE attempts SET state='unknown' WHERE id=?", (attempt['id'],))
        self.backend.thread['turns'] = [{'id': attempt['turn'], 'status': 'completed', 'items': [{'id': 'plan-recovered', 'type': 'plan', 'text': 'Do not execute before confirmation'}]}]
        recovered = self.run_control('reconcile', task=self.task)
        self.assertEqual(recovered['state'], 'completed')
        self.assertIsNotNone(self.svc.pending_plan(self.owner, self.task))
        with self.assertRaises(Conflict):
            self.svc.followup(self.owner, self.task, 'bypass-plan', 'Execute')

    def test_steer_acknowledgement_must_match_exact_turn(self):
        self.svc.store.db.execute("UPDATE tasks SET state='running' WHERE id=?", (self.task,))
        self.svc.store.db.execute("UPDATE attempts SET state='running' WHERE task=?", (self.task,))
        client = Transport()
        client.call = lambda method, params: {'turnId': 'another-turn'}
        self.svc.clients[self.task] = client
        item = self.svc.followup(self.owner, self.task, 'guide', 'Only this turn')
        with self.assertRaises(Conflict):
            self.svc.queue_action(self.owner, self.task, 'steer', item['id'])
        self.assertEqual(self.svc.store.get(self.owner, 'queue', item['id'])['state'], 'uncertain_steer')

    def test_imported_thread_cannot_activate_unverified_native_policy(self):
        binding = self.svc.store.get(self.owner, 'binding', self.task)
        binding.update(imported=True, effective={'source': 'history only'})
        binding.pop('policy_verified', None)
        self.svc.store.put(self.owner, 'binding', self.task, binding)
        result = self.wait_job(self.svc.control(self.owner, 'goal', {'task': self.task, 'operation': 'set', 'objective': 'Must not inherit full access', 'budget': 100}))
        self.assertEqual(result['state'], 'failed')
        self.assertFalse(any(m == 'thread/goal/set' for m, _ in self.backend.calls))

    def test_native_goal_reserves_writer_before_activation_and_releases_on_pause(self):
        from hermes_codex.state import encoded
        write_brief = brief(self.root) | {'sandbox': 'workspace-write', 'write_roots': [str(self.root)]}
        self.svc.store.db.execute('UPDATE tasks SET writing=1,brief=? WHERE id=?', (encoded(write_brief), self.task))
        other = Store(self.root / 'other.db')
        self.addCleanup(other.close)
        competitor = other.submit('b', str(self.root), 'other', {}, True)
        self.run_control('goal', task=self.task, operation='set', objective='Exclusive writer', budget=100)
        with self.assertRaises(Conflict):
            other.start('b', competitor['id'])
        self.run_control('goal', task=self.task, operation='pause')
        attempt = other.start('b', competitor['id'])
        other.finish('b', competitor['id'], attempt, 'completed', {})

    def test_import_from_selected_account_without_preexisting_task(self):
        self.svc.login_presenter = lambda url: None
        self.run_control('account', operation='login', name='Account')
        self.backend.logged_in = True
        self.run_control('account', operation='switch', name='Account')
        self.backend.thread.update(id='unbound-history', cwd=str(self.root))
        proposal = self.svc.propose(self.owner, brief(self.root, 'unbound-import'))
        self.svc.authorize(self.owner, proposal['id'])
        imported = self.run_control('thread-import', proposal=proposal['id'], thread='unbound-history')
        self.assertEqual(imported['thread']['id'], 'unbound-history')
        self.assertEqual(self.svc.store.get(self.owner, 'route', imported['task'])['account'], self.svc.selected_account(self.owner))
        catalogue = self.run_control('import-catalog')
        self.assertEqual(catalogue['data'][0]['id'], 'unbound-history')

    def test_legacy_host_accounts_backfill_claims_and_conflicts_fail_closed(self):
        path = self.root / 'legacy-claims.sqlite3'
        store = WorkflowStore(path)
        home = str((self.root / 'legacy-home').resolve())
        store.put('owner-a', 'account', 'a', {
            'id': 'a', 'host_alias': 'default', 'host_home': home})
        store.put('owner-b', 'account', 'b', {
            'id': 'b', 'host_alias': 'alternate', 'host_home': home})
        store.close()
        recovered = WorkflowStore(path)
        self.addCleanup(recovered.close)
        with self.assertRaises(Denied):
            recovered.claim_host_alias('owner-a', 'default', home)
        with self.assertRaises(Denied):
            recovered.claim_host_alias('owner-b', 'alternate', home)
        with self.assertRaises(Denied):
            recovered.claim_host_alias('owner-c', 'default', home)

    def test_native_find_uses_default_host_and_follows_unique_name_match(self):
        self.svc.host_account_homes = {'default': str(self.root / 'host-home')}
        (self.root / 'host-home').mkdir()
        native_id = '01a0d634-df3c-73b0-b202-c47e969c3a86'
        self.backend.thread.update(
            id=native_id, name='วางแผน Implement SRS Region Vision',
            cwd=str(self.root), status={'type': 'active'},
            turns=[{'id': 'latest'}])

        found = self.run_control('native-find', query='SRS Region Vision')

        self.assertEqual(found['thread']['id'], native_id)
        self.assertEqual(found['thread']['turns'][-1]['id'], 'latest')
        self.assertEqual(found['matched_by'], 'name')
        self.assertEqual(found['source'], 'operator-configured host account')
        self.assertTrue(found['read_only'])
        self.assertFalse(found['imported'])
        self.assertFalse(any(method in {'thread/resume', 'turn/start', 'turn/steer'}
                             for method, _ in self.backend.calls))

    def test_native_follow_reads_active_host_thread_without_importing_or_owning_it(self):
        self.svc.host_account_homes = {'approved-host': str(self.root / 'host-home')}
        (self.root / 'host-home').mkdir()
        native_id = '01a0d634-df3c-73b0-b202-c47e969c3a86'
        self.backend.thread.update(id=native_id, cwd=str(self.root),
                                   status={'type': 'active'}, turns=[{'id': f'turn-{i}'} for i in range(105)])
        followed = self.run_control('native-follow', thread=native_id, host_alias='approved-host')
        self.assertEqual(followed['thread']['id'], native_id)
        self.assertEqual(followed['thread']['status']['type'], 'active')
        self.assertEqual(followed['turns_omitted'], 5)
        self.assertEqual(followed['thread']['turns'][0]['id'], 'turn-5')
        self.assertEqual(followed['thread']['turns'][-1]['id'], 'turn-104')
        self.assertEqual(followed['source'], 'operator-configured host account')
        self.assertEqual(self.svc.store.tasks(self.owner)[0]['id'], self.task)
        self.assertFalse(any(method in {'thread/resume', 'turn/start', 'turn/steer'}
                             for method, _ in self.backend.calls))
        other_job = self.svc.control(
            'another-owner', 'native-follow',
            {'thread': native_id, 'host_alias': 'approved-host'})
        for _ in range(100):
            other = self.svc.control_status('another-owner', other_job['id'])
            if other['state'] in {'completed', 'failed', 'unknown'}:
                break
            __import__('time').sleep(.01)
        self.assertEqual(other['state'], 'failed')
        self.svc.host_account_homes['same-home'] = str(self.root / 'host-home')
        duplicate_home_job = self.svc.control(
            'another-owner', 'native-follow',
            {'thread': native_id, 'host_alias': 'same-home'})
        for _ in range(100):
            duplicate_home = self.svc.control_status('another-owner', duplicate_home_job['id'])
            if duplicate_home['state'] in {'completed', 'failed', 'unknown'}:
                break
            __import__('time').sleep(.01)
        self.assertEqual(duplicate_home['state'], 'failed')
        denied = self.wait_job(self.svc.control(
            self.owner, 'native-follow', {'thread': native_id, 'host_alias': 'unknown'}))
        self.assertEqual(denied['state'], 'failed')
        malformed = self.wait_job(self.svc.control(
            self.owner, 'native-follow', {'thread': f' {native_id}', 'host_alias': 'approved-host'}))
        self.assertEqual(malformed['state'], 'failed')

    def test_named_host_account_sync_reads_identity_without_copying_credentials(self):
        self.svc.host_account_homes = {'approved-host': str(self.root / 'host-home')}
        (self.root / 'host-home').mkdir()
        self.backend.logged_in = True
        result = self.wait_job(self.svc.control(self.owner, 'account', {'operation': 'sync-host', 'name': 'approved-host'}))
        self.assertEqual(result['state'], 'completed', result)
        account = result['result']
        self.assertEqual(account['state'], 'ready')
        self.assertEqual(self.backend.homes[-1], self.root / 'host-home')
        self.assertEqual(list((self.root / 'host-home').iterdir()), [])
        (self.root / 'replacement-home').mkdir()
        self.svc.host_account_homes['approved-host'] = str(self.root / 'replacement-home')
        changed = self.wait_job(self.svc.control(self.owner, 'account', {'operation': 'sync-host', 'name': 'approved-host'}))
        self.assertEqual(changed['state'], 'failed', 'Named account routes must not retarget saved tasks')
        self.assertFalse(any(m in {'account/login/start', 'account/logout'} for m, _ in self.backend.calls))
        self.assertTrue(all(p.get('refreshToken') is False for m, p in self.backend.calls if m == 'account/read'))
        denied = self.wait_job(self.svc.control(self.owner, 'account', {'operation': 'sync-host', 'name': '/unconfigured'}))
        self.assertEqual(denied['state'], 'failed')

    def test_localized_presentation_is_lossless_paged_and_owner_scoped(self):
        self.assertTrue(hasattr(self.svc, 'present'))
        text = 'résultat ไทย <script>text only</script>\n' * 1000
        attempt = self.svc.store.task(self.owner, self.task)['attempts'][-1]
        from hermes_codex.state import encoded
        self.svc.store.db.execute('UPDATE attempts SET result=? WHERE id=?', (encoded({'summary': text}), attempt['id']))
        for locale, title in [('en', 'Result'), ('zh', '结果'), ('fr', 'Résultat')]:
            self.svc.store.put(self.owner, 'settings', 'current', self.svc.settings(self.owner) | {'locale': locale})
            page = self.svc.present(self.owner, 'result', self.task)
            self.assertIn(title, page['text'])
            parts = [page['text']]
            token = page['next']
            while token:
                with self.assertRaises(Denied):
                    self.svc.present('another', 'result', self.task, page=token)
                page = self.svc.present(self.owner, 'result', self.task, page=token)
                self.assertLessEqual(len(page['text']), 4000)
                parts.append(page['text'])
                token = page['next']
            self.assertEqual(''.join(parts).count('résultat'), 1000)
            self.assertEqual(page['format'], 'plain-text')

    def test_goal_rejects_unrequested_budget_change_and_usage_reset(self):
        self.run_control('goal', task=self.task, operation='set', objective='Bounded', budget=100)
        self.backend.goal.update(tokensUsed=50, timeUsedSeconds=25, status='paused')
        client = self.svc.goal_clients[self.task]
        call = client.call
        def bad_budget(method, params):
            result = call(method, params)
            if method == 'thread/goal/set':
                self.backend.goal['tokenBudget'] = 200
            return result
        client.call = bad_budget
        result = self.wait_job(self.svc.control(self.owner, 'goal', {'task': self.task, 'operation': 'resume'}))
        self.assertEqual(result['state'], 'unknown', 'Unrequested native budget increase must fail read-back')
        self.backend.goal.update(tokenBudget=100, status='paused')
        def bad_time(method, params):
            result = call(method, params)
            if method == 'thread/goal/set':
                self.backend.goal['timeUsedSeconds'] = 0
            return result
        client.call = bad_time
        result = self.wait_job(self.svc.control(self.owner, 'goal', {'task': self.task, 'operation': 'resume'}))
        self.assertEqual(result['state'], 'unknown', 'Same-objective active-time usage must not reset')

    def test_maintenance_blocks_uncertain_steering_and_native_activity(self):
        self.svc.store.put(self.owner, 'queue', 'uncertain', {'id': 'uncertain', 'task': self.task, 'state': 'uncertain_steer'})
        denied = self.wait_job(self.svc.control(self.owner, 'maintenance', {'task': self.task, 'operation': 'restart'}))
        self.assertEqual(denied['state'], 'failed')
        self.svc.store.put(self.owner, 'queue', 'uncertain', {'id': 'uncertain', 'task': self.task, 'state': 'dismissed_uncertain'})
        self.backend.thread['status'] = {'type': 'active'}
        denied = self.wait_job(self.svc.control(self.owner, 'maintenance', {'task': self.task, 'operation': 'reconnect'}))
        self.assertEqual(denied['state'], 'failed')
        self.backend.thread['status'] = {'type': 'idle'}
        ready = self.run_control('maintenance', task=self.task, operation='reconnect')
        self.assertTrue(ready['ready'])
        self.assertIn('observed', ready)

    def test_attachment_ingress_all_kinds_analyze_once_and_restart(self):
        from hermes_codex.attachments import KINDS
        ingress = self.root / 'ingress'
        ingress.mkdir()
        self.svc.attachment_roots = [str(ingress)]
        self.assertIn('attachment-use', __import__('hermes_codex.controls', fromlist=['CONTROL_FIELDS']).CONTROL_FIELDS)
        batches = []
        for kind in sorted(KINDS):
            source = ingress / kind
            source.write_bytes(b'\x89PNG\r\n\x1a\n' if kind in {'image', 'photo'} else kind.encode())
            batch = self.run_control('attachment-stage', task=self.task, path=str(source), kind=kind, key=kind)
            batches.append(batch)
        self.svc.close()
        self.svc = Service(self.root / 'state', [str(self.root)], transport_factory=Transport, attachment_roots=[str(ingress)])
        self.addCleanup(self.svc.close)
        chosen = batches[0]
        result = self.run_control('attachment-use', task=self.task, id=chosen['id'], operation='analyze-now', key='analyze-once', text='Inspect attached file')
        import time
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and self.svc.status(self.owner, self.task)['state'] != 'running':
            time.sleep(.01)
        client = self.svc.clients[self.task]
        params = next(p for m, p in client.calls if m == 'turn/start')
        self.assertIn(chosen['files'][0]['path'], str(params['input']))
        self.assertEqual(len(result['attachments']), 1)
        self.assertEqual(sum(b['state'] == 'staged' for b in self.svc.store.list(self.owner, 'attachment')), len(KINDS) - 1)
        replay = self.run_control('attachment-use', task=self.task, id=chosen['id'], operation='analyze-now', key='analyze-once', text='Inspect attached file')
        self.assertEqual(replay['id'], result['id'])

    def test_device_login_memory_only_delivery_and_account_selection_isolation(self):
        presented = []
        self.svc.device_login_presenter = lambda data: presented.append(data)
        original = self.svc.factory
        def factory(*args, **kwargs):
            client = original(*args, **kwargs)
            call = client.call
            def request(method, params):
                if method == 'account/login/start':
                    self.assertEqual(params, {'type': 'chatgptDeviceCode'})
                    return {'type': 'chatgptDeviceCode', 'loginId': 'device-id', 'verificationUrl': 'https://auth.openai.com/codex/device', 'userCode': 'TEST-ONLY-CODE'}
                return call(method, params)
            client.call = request
            return client
        self.svc.factory = factory
        self.assertIn('method', __import__('hermes_codex.controls', fromlist=['CONTROL_FIELDS']).CONTROL_FIELDS['account'][1])
        pending = self.run_control('account', operation='login', name='Device', method='device')
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(presented[0]['userCode'], 'TEST-ONLY-CODE')
        self.assertNotIn('TEST-ONLY-CODE', str([tuple(r) for r in self.svc.store.db.execute('SELECT * FROM documents')]))
        self.run_control('account', operation='continue', name='Device')
        self.assertEqual(len(presented), 2)
        self.backend.logged_in = True
        self.svc.store.put(self.owner, 'selection', 'current', {'task': self.task, 'thread': 'thread-1'})
        self.run_control('account', operation='switch', name='Device')
        self.assertEqual(self.svc.store.get(self.owner, 'selection', 'current'), {})
        listed = self.run_control('account', operation='list')
        self.assertTrue(listed['data'][0]['selected'])
        self.assertFalse(self.svc.login_links)
        denied = self.wait_job(self.svc.control(self.owner, 'open', {'task': self.task}))
        self.assertEqual(denied['state'], 'failed', 'old-account mutation must not use selected account implicitly')

    def test_recovery_preserves_uncertain_steer_and_never_replays(self):
        self.svc.store.put(self.owner, 'queue', 'lost', {'id': 'lost', 'task': self.task, 'state': 'uncertain_steer', 'text': 'guidance', 'attempt': 'original', 'thread': 'thread-1', 'turn': 'turn-1'})
        self.svc.close()
        self.svc = Service(self.root / 'state', [str(self.root)], transport_factory=self.backend.factory)
        self.addCleanup(self.svc.close)
        self.assertIn('recovery', __import__('hermes_codex.controls', fromlist=['CONTROL_FIELDS']).CONTROL_FIELDS)
        recovery = self.run_control('recovery', task=self.task)
        self.assertEqual(recovery['queue'][0]['state'], 'uncertain_steer')
        unsafe = self.wait_job(self.svc.control(self.owner, 'queue-recover', {'task': self.task, 'id': 'lost', 'decision': 'retry'}))
        self.assertEqual(unsafe['state'], 'failed')
        dismissed = self.run_control('queue-recover', task=self.task, id='lost', decision='dismiss')
        self.assertEqual(dismissed['state'], 'dismissed_uncertain')
        self.assertIsNone(dismissed['consumed_by_runtime'])
        self.assertFalse(any(m in {'turn/start', 'turn/steer'} for m, _ in self.backend.calls))
        again = self.wait_job(self.svc.control(self.owner, 'queue-recover', {'task': self.task, 'id': 'lost', 'decision': 'dismiss'}))
        self.assertEqual(again['state'], 'failed')

    def test_import_native_thread_requires_authorized_assignment_and_route(self):
        self.backend.thread.update(cwd=str(self.root), id='import-me')
        proposal = self.svc.propose(self.owner, brief(self.root, 'import-one'))
        self.assertIn('thread-import', __import__('hermes_codex.controls', fromlist=['CONTROL_FIELDS']).CONTROL_FIELDS)
        denied = self.wait_job(self.svc.control(self.owner, 'thread-import', {'proposal': proposal['id'], 'source_task': self.task, 'thread': 'import-me'}))
        self.assertEqual(denied['state'], 'failed')
        self.svc.authorize(self.owner, proposal['id'])
        imported = self.run_control('thread-import', proposal=proposal['id'], source_task=self.task, thread='import-me')
        task = imported['task']
        self.assertEqual(imported['thread']['id'], 'import-me')
        self.assertEqual(self.svc.worker_home(self.owner, task), self.svc.worker_home(self.owner, self.task))
        self.assertEqual(self.svc.store.task(self.owner, task)['review_state'], 'not_ready')
        duplicate = self.run_control('thread-import', proposal=proposal['id'], source_task=self.task, thread='import-me')
        self.assertEqual(duplicate['task'], task)
        self.assertFalse(any(m in {'turn/start', 'thread/resume'} for m, _ in self.backend.calls))
        self.backend.thread['cwd'] = '/'
        other = self.svc.propose(self.owner, brief(self.root, 'bad-import'))
        self.svc.authorize(self.owner, other['id'])
        blocked = self.wait_job(self.svc.control(self.owner, 'thread-import', {'proposal': other['id'], 'source_task': self.task, 'thread': 'import-me'}))
        self.assertEqual(blocked['state'], 'failed')


class SecurePresentationTests(unittest.TestCase):
    def test_platform_diagnostics_and_validation_fail_before_state_creation(self):
        import hermes_codex.runtime as runtime
        self.assertTrue(hasattr(runtime, 'capabilities'))
        for platform, available in [('linux', True), ('darwin', True), ('win32', False), ('other', False)]:
            with patch('sys.platform', platform):
                result = runtime.capabilities()
                self.assertEqual(result['implementation_available'], available)
                self.assertFalse(result['full_platform_acceptance'])
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / 'not-created'
            with patch('sys.platform', 'win32'), self.assertRaises(FileNotFoundError):
                Service(home, [d], transport_factory=Transport)
            self.assertFalse(home.exists())
            with self.assertRaises(ValueError):
                Service(home, [d], max_workers=True, transport_factory=Transport)
            self.assertFalse(home.exists())

    def test_device_presenter_never_uses_secret_argv_or_environment(self):
        from hermes_codex import presentation
        self.assertTrue(hasattr(presentation, 'open_device_login'))
        private = {'verificationUrl': 'https://auth.openai.com/codex/device', 'userCode': 'TEST-ONLY-CODE'}
        with patch.dict('os.environ', {'DISPLAY': ':1', 'SECRET': 'not-inherited'}, clear=True), patch('subprocess.run') as run:
            presentation.open_device_login(private, ['/usr/bin/zenity', '--text-info', '--no-markup'])
            self.assertNotIn('TEST-ONLY-CODE', str(run.call_args.args))
            self.assertIn('TEST-ONLY-CODE', run.call_args.kwargs['input'])
            self.assertNotIn('SECRET', run.call_args.kwargs['env'])
            with self.assertRaises(ValueError):
                presentation.open_device_login(private | {'verificationUrl': 'https://attacker.invalid'}, ['/usr/bin/zenity'])
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(FileNotFoundError):
                presentation.open_device_login(private, ['/usr/bin/zenity'])


class PolicyAdmissionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry = patch('hermes_codex.state.writer_directory', return_value=Path(directory.name) / 'writers')
        registry.start()
        self.addCleanup(registry.stop)

    def test_full_access_requires_explicit_unrestricted_operator_scope(self):
        from hermes_codex.policy import Assignment
        from hermes_codex.access import validate, sandbox
        raw = brief('/') | {'sandbox': 'full-access', 'write_roots': ['/'], 'network': True}
        assignment = Assignment.model_validate(raw).model_dump()
        for roots, enabled, network in [(['/'], False, True), (['/tmp'], True, True), (['/'], True, False)]:
            with self.assertRaises((Denied, PermissionError)):
                validate(assignment | {'network': network}, roots, enabled)
        approved = validate(assignment, ['/'], True)
        self.assertEqual(sandbox(approved), {'type': 'dangerFullAccess'})

    def test_plan_turn_cannot_inherit_write_access_and_resume_reapplies_policy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            svc = Service(root / 'state', [d], transport_factory=Transport)
            try:
                svc.store.put('a', 'settings', 'current', {'provider': 'default', 'auto_queue': True, 'mode': 'plan', 'locale': 'en'})
                proposal = svc.propose('a', brief(root) | {'sandbox': 'workspace-write', 'write_roots': [d]})
                svc.authorize('a', proposal['id'])
                task = svc.submit('a', proposal['id'])
                import time
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and svc.status('a', task['id'])['state'] not in {'running', 'failed'}:
                    time.sleep(.01)
                client = svc.clients[task['id']]
                start = next(p for m, p in client.calls if m == 'thread/start')
                self.assertEqual(start['sandbox'], 'read-only', 'Native planning must not be write-capable')
                turn = next(p for m, p in client.calls if m == 'turn/start')
                self.assertEqual(turn['sandboxPolicy']['type'], 'readOnly')
            finally:
                svc.close()


    def test_authorized_write_policy_and_symlink_revalidation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace = root / 'workspace'
            workspace.mkdir()
            with self.assertRaises(PermissionError):
                # Full access is not a confined workspace-write alias.
                svc = Service(root / 'state', [str(workspace)], transport_factory=Transport)
                self.addCleanup(svc.close)
                svc.propose('a', brief(workspace) | {'sandbox': 'full-access', 'write_roots': [str(workspace)]})
            proposal = svc.propose('a', brief(workspace) | {'task_type': 'implement', 'sandbox': 'workspace-write', 'write_roots': [str(workspace)]})
            with self.assertRaises(Denied):
                svc.submit('a', proposal['id'])
            svc.authorize('a', proposal['id'])
            policy = svc.sandbox(proposal['brief'])
            self.assertEqual(policy['type'], 'workspaceWrite')
            self.assertEqual(policy['writableRoots'], [str(workspace)])
            self.assertTrue(policy['excludeSlashTmp'])
            self.assertTrue(policy['excludeTmpdirEnvVar'])
            workspace.rename(root / 'old')
            workspace.symlink_to(root / 'old', target_is_directory=True)
            with self.assertRaises((Denied, ValueError, PermissionError)):
                svc.submit('a', proposal['id'])
            svc.close()

    def test_independent_processes_compete_for_one_durable_writer(self):
        import subprocess
        import sys
        import hermes_codex.state as state
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            workspace = directory / 'workspace'
            workspace.mkdir()
            code = "import sys; from pathlib import Path; import hermes_codex.state as s; s.writer_directory=lambda:Path(sys.argv[1]); db=s.Store(sys.argv[2]); task=db.submit('owner',sys.argv[3],'one',{},True); sys.stdin.readline();\ntry:\n db.start('owner',task['id']); print('started')\nexcept s.Conflict:\n print('conflict')\nfinally:\n db.close()"
            processes = [subprocess.Popen([sys.executable, '-c', code, str(state.writer_directory()), str(directory / f'{i}.db'), str(workspace)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for i in range(2)]
            try:
                for process in processes:
                    process.stdin.write('go\n')
                    process.stdin.flush()
                outputs = [process.communicate(timeout=10) for process in processes]
                self.assertEqual([p.returncode for p in processes], [0, 0], outputs)
                self.assertCountEqual([out.strip() for out, err in outputs], ['started', 'conflict'])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_cross_store_writer_claim_survives_close_and_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            workspace = root / 'workspace'
            child = workspace / 'child'
            child.mkdir(parents=True)
            one, two = Store(root / 'one.db'), Store(root / 'two.db')
            self.addCleanup(two.close)
            first = one.submit('a', str(workspace), 'first', {}, True)
            second = two.submit('b', str(child), 'second', {}, True)
            attempt = one.start('a', first['id'])
            with self.assertRaises(Conflict):
                two.start('b', second['id'])
            one.close()
            one = Store(root / 'one.db')
            self.addCleanup(one.close)
            one.recover()
            with self.assertRaises(Conflict):
                two.start('b', second['id'])
            one.finish('a', first['id'], attempt, 'cancelled', {'source': 'confirmed runtime stop'})
            other = two.start('b', second['id'])
            two.finish('b', second['id'], other, 'completed', {})
