"""Independent-review reproductions: offline transport, real store/worker.

No authenticated model calls. Each finding first ran red against the candidate.
"""
import json
import threading
import time
import unittest
from unittest.mock import patch

from hermes_codex.state import Conflict
import test_controls
import test_worker
from test_worker import Transport, brief


class WorkerReviewTests(unittest.TestCase):
    setUp = test_worker.WorkerTests.setUp
    make_service = test_worker.WorkerTests.make_service
    wait = test_worker.WorkerTests.wait

    def start(self, service, payload=None):
        p = service.propose('owner', payload or brief(self.project))
        service.authorize('owner', p['id'])
        task = service.submit('owner', p['id'])
        return self.wait(lambda: (t if (t := service.store.task('owner', task['id']))['state'] == 'running' else None))

    def complete(self, service, task, items=None):
        a = task['attempts'][-1]
        service.clients[task['id']].events.put({'method': 'turn/completed', 'params': {'threadId': a['thread'], 'turn': {'id': a['turn'], 'status': 'completed', 'items': items or []}}})
        self.wait(lambda: service.status('owner', task['id'])['state'] == 'completed')
        service.threads[task['id']].join(2)

    def test_f6_early_file_decisions_expiry_cancel_and_mismatched_reply(self):
        service = self.make_service()
        for number, choice in enumerate(('acceptForSession', 'decline', 'expire', 'cancel-task', 'mismatch')):
            replies = []
            class EarlyTransport(Transport):
                def call(self, method, params):
                    if method == 'turn/start':
                        self.calls.append((method, params))
                        wrong = self.handler('item/fileChange/requestApproval', {'threadId': 'other-thread', 'turnId': 'early', 'itemId': 'wrong'})
                        self.assert_denied = wrong == {'decision': 'decline'}
                        replies.append(self.handler('item/fileChange/requestApproval', {'threadId': params['threadId'], 'turnId': 'early', 'itemId': 'file'}))
                        return {'turn': {'id': 'different' if choice == 'mismatch' else 'early', 'status': 'inProgress'}}
                    return super().call(method, params)
            service.factory = EarlyTransport
            p = service.propose('owner', brief(self.project, str(number)))
            service.authorize('owner', p['id'])
            task = service.submit('owner', p['id'])
            request = self.wait(lambda: next((r for r in service.store.list('owner', 'interaction') if r['task'] == task['id']), None))
            self.assertEqual(request['state'], 'pending')
            self.assertTrue(service.clients[task['id']].assert_denied)
            if choice == 'expire':
                service.store.put('owner', 'interaction', request['id'], request | {'expires': 0})
            elif choice == 'cancel-task':
                service.cancel('owner', task['id'])
            else:
                service.decide('owner', request['id'], 'decline' if choice == 'mismatch' else choice)
            self.wait(lambda: replies)
            self.assertEqual(replies[0], {'decision': choice if choice in {'acceptForSession', 'decline'} else 'decline'})
            if choice == 'mismatch':
                self.wait(lambda: service.status('owner', task['id'])['state'] == 'unknown')
                self.assertEqual(service.status('owner', task['id'])['attempts'][-1]['turn'], 'early')
            else:
                self.wait(lambda: service.status('owner', task['id'])['state'] in {'running', 'cancelling'})
                self.complete(service, service.store.task('owner', task['id']))

    def test_f5_identity_is_rechecked_after_runtime_creation(self):
        service = self.make_service()
        clients = []
        def factory(*args, **kwargs):
            client = Transport(*args, **kwargs)
            clients.append(client)
            self.project.rename(self.root / 'before-factory')
            self.project.mkdir()
            return client
        service.factory = factory
        p = service.propose('owner', brief(self.project))
        service.authorize('owner', p['id'])
        task = service.submit('owner', p['id'])
        self.wait(lambda: service.status('owner', task['id'])['state'] == 'failed')
        self.assertFalse(any(m == 'turn/start' for m, _ in clients[0].calls))
        service.close()
        service = self.make_service()
        with self.assertRaises(PermissionError):
            service.store.check_paths('owner', task['id'])

    def test_f4_delta_only_and_terminal_item_merge_are_lossless(self):
        service = self.make_service()
        task = self.start(service)
        a = task['attempts'][-1]
        base = {'threadId': a['thread'], 'turnId': a['turn']}
        client = service.clients[task['id']]
        for key, delta in [('one', 'first '), ('one', 'answer'), ('two', 'streamed partial')]:
            client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'itemId': key, 'delta': delta}})
        self.complete(service, task, [{'id': 'two', 'type': 'agentMessage', 'text': 'terminal answer'}])
        items = service.result('owner', task['id'])['runtime']['items']
        self.assertEqual([i['text'] for i in items], ['first answer', 'terminal answer'])
        self.assertEqual(len({i['id'] for i in items}), 2)

    def test_f2_secret_patterns_and_identifier_preservation(self):
        from hermes_codex.presentation import redact_text
        from hermes_codex.service import sanitized
        service = self.make_service()
        for source in ('PASSWORD = "two words"', "api-key: 'two words'", 'api_key=opaque+fixture/==', 'Authorization: Bearer fixture', 'refresh_token: opaque'):
            with self.subTest(source=source):
                result = redact_text(source)
                self.assertIn('[redacted]', result)
                self.assertNotIn('two words', result)
                self.assertNotIn('opaque', result)
                self.assertNotIn('fixture', result)
                self.assertEqual(redact_text(result), result)
        for field in ('idempotency_key', 'authorization_ref'):
            with self.assertRaises(PermissionError):
                service.propose('owner', brief(self.project) | {field: 'sk-SYNTHETIC-ONLY'})
        self.assertEqual(sanitized({'tokenBudget': 100, 'tokensUsed': 42}), {'tokenBudget': 100, 'tokensUsed': 42})

    def test_f6_unbound_callback_without_dispatch_is_denied(self):
        service = self.make_service()
        task = service.store.submit('owner', str(self.project), 'unbound', brief(self.project), False)
        attempt = service.store.start('owner', task['id'])
        replies = []
        caller = threading.Thread(target=lambda: replies.append(service._interaction('owner', task['id'], attempt,
            'item/fileChange/requestApproval', {'threadId': None, 'turnId': 'forged', 'itemId': 'x'})))
        caller.start()
        caller.join(.2)
        try:
            self.assertEqual(service.store.list('owner', 'interaction'), [], 'No live dispatch exists for this callback')
            self.assertEqual(replies, [{'decision': 'decline'}])
        finally:
            service.stop.set()
            caller.join(2)

    def test_f6_approval_before_start_reply_has_direct_user_workflow(self):
        service = self.make_service()
        entered = threading.Event()
        replies = []
        class EarlyTransport(Transport):
            def call(self, method, params):
                if method == 'turn/start':
                    self.calls.append((method, params))
                    entered.set()
                    replies.append(self.handler('item/commandExecution/requestApproval', {
                        'threadId': params['threadId'], 'turnId': 'early-turn', 'itemId': 'early-command', 'command': 'touch x'}))
                    return {'turn': {'id': 'early-turn', 'status': 'inProgress'}}
                return super().call(method, params)
        service.factory = EarlyTransport
        p = service.propose('owner', brief(self.project))
        service.authorize('owner', p['id'])
        task = service.submit('owner', p['id'])
        self.assertTrue(entered.wait(2))
        requests = self.wait(lambda: service.store.list('owner', 'interaction'))
        self.assertEqual(requests[0]['state'], 'pending')
        self.assertEqual(replies, [])
        from hermes_codex.state import Denied
        with self.assertRaises(Denied):
            service.decide('other', requests[0]['id'], 'accept')
        service.decide('owner', requests[0]['id'], 'accept')
        self.wait(lambda: service.status('owner', task['id'])['state'] == 'running')
        self.assertEqual(replies, [{'decision': 'accept'}])
        self.assertEqual(service.store.get('owner', 'interaction', requests[0]['id'])['state'], 'consumed')
        with self.assertRaises(Conflict):
            service.decide('owner', requests[0]['id'], 'accept')

    def test_f5_replaced_workspace_cannot_continue_authorized_task(self):
        service = self.make_service()
        task = self.start(service)
        self.complete(service, task)
        original = service.clients[task['id']]
        self.project.rename(self.root / 'old-project')
        self.project.mkdir()
        from hermes_codex.state import Denied
        with self.assertRaises(Denied):
            service.followup('owner', task['id'], 'after-replacement', 'Continue')
        self.assertEqual(service.status('owner', task['id'])['state'], 'completed')
        self.assertIs(service.clients[task['id']], original)
        self.assertEqual(service.queue_items('owner', task['id']), [])

    def test_f4_streamed_items_survive_empty_terminal_and_restart(self):
        service = self.make_service()
        task = self.start(service)
        a = task['attempts'][-1]
        client = service.clients[task['id']]
        content = 'long answer ไทย ' * 2000
        base = {'threadId': a['thread'], 'turnId': a['turn']}
        client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'turnId': 'stale', 'itemId': 'wrong', 'delta': 'STALE'}})
        client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'itemId': 'msg', 'delta': content[:17000]}})
        client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'itemId': 'msg', 'delta': content[17000:]}})
        client.events.put({'method': 'item/completed', 'params': base | {'item': {'type': 'agentMessage', 'id': 'msg', 'text': content, 'phase': 'final_answer'}}})
        client.events.put({'method': 'turn/completed', 'params': {'threadId': a['thread'], 'turn': {'id': a['turn'], 'status': 'completed', 'items': [], 'itemsView': 'notLoaded'}}})
        self.wait(lambda: service.status('owner', task['id'])['state'] == 'completed')
        service.close()
        service = self.make_service()
        parts, cursor = [], 0
        while True:
            page = service.result_page('owner', task['id'], attempt=a['id'], cursor=cursor)
            parts.append(page['text'])
            if page['next'] is None:
                break
            cursor = page['next']
        data = json.loads(''.join(parts))
        self.assertEqual(data['runtime']['items'], [{'type': 'agentMessage', 'id': 'msg', 'text': content, 'phase': 'final_answer'}])
        self.assertNotIn('STALE', str(data))
        self.assertFalse(data['verified'])

    def test_f3_reasoning_item_content_never_reaches_events_or_result(self):
        service = self.make_service()
        task = self.start(service)
        a = task['attempts'][-1]
        item = {'type': 'reasoning', 'id': 'r', 'content': ['PRIVATE-CONTENT'], 'summary': ['PRIVATE-SUMMARY']}
        for method in ('item/started', 'item/completed'):
            service.clients[task['id']].events.put({'method': method, 'params': {'threadId': a['thread'], 'turnId': a['turn'], 'item': item}})
        self.complete(service, task, [item, {'type': 'agentMessage', 'id': 'a', 'text': 'public answer'}])
        for output in (str(service.events('owner', task['id'])), str(service.result('owner', task['id'])), str(list(service.store.db.iterdump()))):
            self.assertNotIn('PRIVATE-', output)
            self.assertIn('public answer', output)

    def test_f2_assignment_and_followup_never_persist_or_send_secrets(self):
        service = self.make_service()
        secrets = ['sk-SYNTHETIC-ONLY-FIXTURE', 'pw_fixture_123', 'key_fixture_456']
        text = f'Inspect with {secrets[0]} password={secrets[1]} api_key="{secrets[2]}"'
        task = self.start(service, brief(self.project) | {'objective': text, 'context': [text], 'instructions': [text]})
        original = service.clients[task['id']]
        self.complete(service, task)
        service.followup('owner', task['id'], 'next', text)
        self.wait(lambda: service.status('owner', task['id'])['state'] == 'running')
        rows = list(service.store.db.iterdump())
        calls = original.calls + service.clients[task['id']].calls
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, str(rows), 'secret persisted in SQLite')
                self.assertNotIn(secret, str(calls), 'secret reached runtime')
        from hermes_codex.presentation import chunks, error_info
        from hermes_codex.service import sanitized
        for output in (sanitized(text), ''.join(chunks(text)), str(error_info(ValueError(text)))):
            for secret in secrets:
                self.assertNotIn(secret, output)


class PluginReviewTests(unittest.TestCase):
    def test_f7_cli_completion_uses_exact_local_session_without_key(self):
        from hermes_codex import plugin
        from hermes_codex.service import Service
        from test_plugin import Context
        from types import SimpleNamespace
        import tempfile
        from pathlib import Path
        import sys
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'project').mkdir()
            ctx = Context(root)
            ctx._manager = SimpleNamespace(_cli_ref=SimpleNamespace(session_id='cli-one'))
            accepted = []
            ctx.inject_message = lambda message, **kw: accepted.append((message, kw)) or True
            fields = {'HERMES_SESSION_ID': 'cli-one', 'HERMES_SESSION_SOURCE': 'cli'}
            fake = SimpleNamespace(get_session_env=lambda name, default='': fields.get(name, default))
            hosts = []
            def factory(*a, **kw):
                hosts.append(Service(*a, **kw, transport_factory=Transport))
                return hosts[-1]
            with patch.dict(sys.modules, {'gateway.session_context': fake}), patch.object(plugin, 'profile_home', return_value=root), patch.object(plugin, 'Service', side_effect=factory), patch('hermes_codex.state.writer_directory', return_value=root / 'writers'):
                plugin.register(ctx)
                try:
                    tool = ctx.tools['codex']['handler']
                    owner, _ = plugin.current_scope()
                    p = json.loads(tool({'action': 'propose', 'payload': brief(root / 'project')}))['data']
                    hosts[0].authorize(owner, p['id'])
                    task = hosts[0].submit(owner, p['id'])
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline and hosts[0].status(owner, task['id'])['state'] != 'running':
                        time.sleep(.01)
                    a = hosts[0].status(owner, task['id'])['attempts'][-1]
                    hosts[0].clients[task['id']].events.put({'method': 'turn/completed', 'params': {'threadId': a['thread'], 'turn': {'id': a['turn'], 'status': 'completed', 'items': []}}})
                    hosts[0].threads[task['id']].join(2)
                    self.assertEqual(len(accepted), 1, 'CLI completion was not injected')
                    self.assertFalse(accepted[0][1].get('session_key'))
                    ctx._manager._cli_ref.session_id = 'different-session'
                    self.assertFalse(hosts[0].notify(owner, 'must not cross sessions'))
                    self.assertFalse(hosts[0].notify('unknown-owner', 'must not fallback'))
                    self.assertEqual(len(accepted), 1)
                    # Existing gateway path still passes only its captured key;
                    # unknown/missing keys never fall back to local injection.
                    ctx._manager._cli_ref = None
                    fields.update(HERMES_SESSION_ID='gateway-one', HERMES_SESSION_SOURCE='telegram',
                                  HERMES_SESSION_PLATFORM='telegram', HERMES_SESSION_USER_ID='user', HERMES_SESSION_KEY='gateway-route')
                    gateway_owner, _ = plugin.current_scope()
                    self.assertTrue(json.loads(tool({'action': 'help'}))['success'])
                    self.assertTrue(hosts[0].notify(gateway_owner, 'gateway completion'))
                    self.assertEqual(accepted[-1][1]['session_key'], 'gateway-route')
                    fields['HERMES_SESSION_KEY'] = ''
                    unroutable, _ = plugin.current_scope()
                    tool({'action': 'help'})
                    self.assertFalse(hosts[0].notify(unroutable, 'no route'))
                    ctx._manager._cli_ref = SimpleNamespace(session_id='gateway-one')
                    self.assertFalse(hosts[0].notify(gateway_owner, 'must not go to CLI'))
                finally:
                    for close in ctx.cleanups:
                        close()


class GoalReviewTests(unittest.TestCase):
    setUp = test_controls.ControlsTests.setUp
    wait_job = test_controls.ControlsTests.wait_job
    run_control = test_controls.ControlsTests.run_control
    wait = test_worker.WorkerTests.wait

    def test_f3_f4_goal_items_are_filtered_and_stream_survives_terminal(self):
        self.run_control('goal', task=self.task, operation='set', objective='Observe', budget=100)
        client = self.svc.goal_clients[self.task]
        turn = {'id': 'goal-stream', 'status': 'inProgress', 'items': []}
        client.events.put({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': turn}})
        self.wait(lambda: self.svc.status(self.owner, self.task)['state'] == 'running')
        base = {'threadId': 'thread-1', 'turnId': turn['id']}
        item = {'type': 'reasoning', 'id': 'private', 'content': ['PRIVATE-CONTENT'], 'summary': ['PRIVATE-SUMMARY']}
        client.events.put({'method': 'item/completed', 'params': base | {'item': item}})
        client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'itemId': 'answer', 'delta': 'goal answer'}})
        client.events.put({'method': 'item/agentMessage/delta', 'params': base | {'turnId': 'stale', 'itemId': 'wrong', 'delta': 'STALE'}})
        client.events.put({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': turn | {'status': 'completed', 'items': [item]}}})
        self.wait(lambda: self.svc.status(self.owner, self.task)['state'] == 'completed')
        result = self.svc.result(self.owner, self.task)
        self.assertIn('goal answer', str(result))
        self.assertNotIn('PRIVATE-', str(result))
        self.assertNotIn('STALE', str(result))
        self.assertNotIn('PRIVATE-', str(self.svc.events(self.owner, self.task)))

    def test_f1_clear_before_notification_and_read_failure_keep_claim(self):
        raw = brief(self.root) | {'sandbox': 'workspace-write', 'write_roots': [str(self.root)]}
        self.svc.store.db.execute('UPDATE tasks SET writing=1,brief=? WHERE id=?', (json.dumps(raw), self.task))
        self.run_control('goal', task=self.task, operation='set', objective='Observe', budget=100)
        client = self.svc.goal_clients[self.task]
        original_call = client.call
        def failing_read(method, params):
            if method == 'thread/read':
                raise ConnectionError('offline read failure')
            return original_call(method, params)
        client.call = failing_read
        result = self.wait_job(self.svc.control(self.owner, 'goal', {'task': self.task, 'operation': 'clear'}))
        self.assertEqual(result['state'], 'unknown')
        other = self.svc.store.submit('competitor', str(self.root), 'other', raw, True)
        with self.assertRaises(Conflict):
            self.svc.store.start('competitor', other['id'])
        client.call = original_call
        self.backend.thread['status'] = {'type': 'active'}
        self.run_control('goal', task=self.task, operation='get')
        with self.assertRaises(Conflict):
            self.svc.store.start('competitor', other['id'])
        with self.assertRaises(Conflict):
            self.svc.followup(self.owner, self.task, 'same-task', 'Must not overlap native work')
        self.backend.thread['status'] = {'type': 'idle'}
        self.run_control('goal', task=self.task, operation='get')
        self.svc.store.start('competitor', other['id'])

    def test_f1_pause_before_start_notification_retains_writer(self):
        raw = brief(self.root) | {'sandbox': 'workspace-write', 'write_roots': [str(self.root)]}
        self.svc.store.db.execute('UPDATE tasks SET writing=1,brief=? WHERE id=?', (json.dumps(raw), self.task))
        self.run_control('goal', task=self.task, operation='set', objective='Native work', budget=100)
        # Native start happened; watcher has not received turn/started yet.
        self.backend.thread['status'] = {'type': 'active'}
        self.backend.thread['turns'] = [{'id': 'early-native', 'status': 'inProgress', 'items': []}]
        self.assertEqual(self.svc.status(self.owner, self.task)['state'], 'completed')
        self.run_control('goal', task=self.task, operation='pause')
        other = self.svc.store.submit('competitor', str(self.root), 'competing', raw, True)
        with self.assertRaises(Conflict, msg='Paused goal released writer before native terminal confirmation'):
            self.svc.store.start('competitor', other['id'])
        client = self.svc.goal_clients[self.task]
        client.events.put({'method': 'turn/started', 'params': {'threadId': 'thread-1', 'turn': self.backend.thread['turns'][0]}})
        self.wait(lambda: self.svc.status(self.owner, self.task)['state'] == 'running')
        with self.assertRaises(Conflict):
            self.svc.store.start('competitor', other['id'])
        self.backend.thread['status'] = {'type': 'idle'}
        self.backend.thread['turns'][0]['status'] = 'completed'
        client.events.put({'method': 'turn/completed', 'params': {'threadId': 'thread-1', 'turn': self.backend.thread['turns'][0]}})
        self.wait(lambda: not self.svc.store.db.execute('SELECT 1 FROM writers.claims WHERE task=?', (self.task,)).fetchone())
        self.svc.store.start('competitor', other['id'])
