"""Offline orchestration checks: fake transport, real store and background host."""
import queue
import tempfile
import time
import unittest
from pathlib import Path


def brief(workspace, key='one'):
    return dict(objective='Inspect project', task_type='inspect', workspace=str(workspace),
                write_roots=[], context=[], instructions=[], in_scope=['inspect'], out_of_scope=['write'],
                criteria=['report'], verification=['read evidence'], sandbox='read-only', network=False,
                approval_policy='on-request', authorization_ref='user-turn', timeout_seconds=10,
                idempotency_key=key)


class Transport:
    def __init__(self, *args, **kwargs):
        self.events = queue.Queue()
        self.calls = []
        self.handler = kwargs.get('approval_handler')
        self.closed = False

    def call(self, method, params):
        self.calls.append((method, params))
        if method == 'account/read':
            return {'account': {'type': 'chatgpt'}, 'requiresOpenaiAuth': True}
        if method in ('thread/start', 'thread/resume'):
            from hermes_codex.access import sandbox
            effective = {'sandbox': 'full-access' if params.get('sandbox') == 'danger-full-access' else params.get('sandbox', 'read-only'), 'network': params.get('config', {}).get('sandbox_workspace_write.network_access', False), 'write_roots': params.get('config', {}).get('sandbox_workspace_write.writable_roots', [])}
            return {'thread': {'id': 'thread-1', 'turns': []}, 'model': 'test-model', 'sandbox': sandbox(effective)}
        if method == 'review/start':
            return {'turn': {'id': 'review-turn', 'status': 'inProgress'}, 'reviewThreadId': 'review-detached' if params['delivery'] == 'detached' else params['threadId']}
        if method == 'turn/steer':
            return {'turnId': params['expectedTurnId']}
        if method == 'turn/start':
            return {'turn': {'id': 'turn-' + str(len(self.calls)), 'status': 'inProgress'}}
        return {}

    def next_event(self):
        return self.events.get()

    def close(self):
        self.closed = True
        self.events.put(None)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        from unittest.mock import patch
        registry = patch('hermes_codex.state.writer_directory', return_value=self.root / 'writers')
        registry.start()
        self.addCleanup(registry.stop)
        self.project = self.root / 'project'
        self.project.mkdir()

    def make_service(self):
        import hermes_codex
        self.assertTrue(hasattr(hermes_codex, 'Service'), 'Nonblocking Service is missing')
        service = hermes_codex.Service(self.root / 'state', [str(self.project)], transport_factory=Transport)
        self.addCleanup(service.close)
        return service

    def wait(self, fn):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            value = fn()
            if value:
                return value
            time.sleep(.01)
        self.fail('background operation did not reach expected state')

    def test_lossless_bounded_result_pages_survive_restart(self):
        import json
        from hermes_codex.state import Conflict, Denied
        service = self.make_service()
        self.assertTrue(hasattr(service, 'result_page'), 'Lossless result pagination missing')
        owner = 'owner'
        proposal = service.propose(owner, brief(self.project))
        service.authorize(owner, proposal['id'])
        task = service.submit(owner, proposal['id'])
        running = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'running' else None))
        attempt = running['attempts'][-1]
        text = 'long unicode ไทย ' * 3000
        items = [{'type': 'agentMessage', 'id': str(n), 'text': text if n == 0 else str(n)} for n in range(105)]
        service.clients[task['id']].events.put({'method': 'turn/completed', 'params': {
            'threadId': attempt['thread'], 'turn': {'id': attempt['turn'], 'status': 'completed', 'items': items, 'access_token': 'private-fixture'}}})
        self.wait(lambda: service.status(owner, task['id'])['state'] == 'completed')
        service.close()
        service = self.make_service()
        chunks, cursor = [], 0
        while True:
            page = service.result_page(owner, task['id'], attempt=attempt['id'], cursor=cursor)
            self.assertLessEqual(len(page['text']), 12000)
            self.assertEqual(page['attempt'], attempt['id'])
            chunks.append(page['text'])
            if page['next'] is None:
                break
            cursor = page['next']
        result = json.loads(''.join(chunks))
        self.assertEqual(result['runtime']['items'], items)
        self.assertEqual(result['runtime']['access_token'], '[redacted]')
        with self.assertRaises(Denied):
            service.result_page('another', task['id'])
        with self.assertRaises(Conflict):
            service.result_page(owner, task['id'], cursor=12000)
        with self.assertRaises(ValueError):
            service.result_page(owner, task['id'], cursor=-1)

    def test_approval_requires_direct_user_exact_live_scope_and_expires(self):
        import threading
        service = self.make_service()
        owner = 'owner'
        proposal = service.propose(owner, brief(self.project))
        service.authorize(owner, proposal['id'])
        task = service.submit(owner, proposal['id'])
        running = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'running' else None))
        attempt = running['attempts'][-1]
        result = []
        params = {'threadId': attempt['thread'], 'turnId': attempt['turn'], 'itemId': 'command-1', 'command': 'touch x', 'cwd': str(self.project)}
        caller = threading.Thread(target=lambda: result.append(service.clients[task['id']].handler('item/commandExecution/requestApproval', params)))
        caller.start()
        self.addCleanup(lambda: caller.join(2))
        requests = self.wait(lambda: service.store.list(owner, 'interaction'))
        request = requests[0]
        self.assertEqual(request['state'], 'pending')
        with self.assertRaises(PermissionError):
            service.decide('other-owner', request['id'], 'accept')
        service.decide(owner, request['id'], 'decline')
        caller.join(2)
        self.assertEqual(result, [{'decision': 'decline'}])
        with self.assertRaises(ValueError):
            service.decide(owner, request['id'], 'accept')
        stale = service.clients[task['id']].handler('item/fileChange/requestApproval', params | {'turnId': 'stale'})
        self.assertEqual(stale, {'decision': 'decline'})

    def test_approval_decisions_expiry_replay_and_restart(self):
        import threading
        service = self.make_service()
        owner = 'owner'
        proposal = service.propose(owner, brief(self.project))
        service.authorize(owner, proposal['id'])
        task = service.submit(owner, proposal['id'])
        running = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'running' else None))
        attempt = running['attempts'][-1]
        for index, decision in enumerate(('accept', 'acceptForSession', 'decline', 'cancel', 'expire')):
            for kind in ('commandExecution', 'fileChange'):
                with self.subTest(decision=decision, kind=kind):
                    params = {'threadId': attempt['thread'], 'turnId': attempt['turn'], 'itemId': f'{kind}-{index}'}
                    method = f'item/{kind}/requestApproval'
                    result = []
                    caller = threading.Thread(target=lambda: result.append(service.clients[task['id']].handler(method, params)))
                    caller.start()
                    request = self.wait(lambda: next((r for r in service.store.list(owner, 'interaction') if r['item'] == params['itemId']), None))
                    if decision == 'expire':
                        with service.store.lock:
                            request['expires'] = time.time() - 1
                            service.store.put(owner, 'interaction', request['id'], request)
                        with self.assertRaises(ValueError):
                            service.decide(owner, request['id'], 'accept')
                    else:
                        service.decide(owner, request['id'], decision)
                    caller.join(2)
                    self.assertFalse(caller.is_alive())
                    self.assertEqual(result, [{'decision': 'decline' if decision == 'expire' else decision}])
                    self.assertEqual(service.clients[task['id']].handler(method, params), {'decision': 'decline'})
        pending = request | {'id': 'lost-callback', 'state': 'pending', 'expires': time.time() + 60}
        service.store.put(owner, 'interaction', pending['id'], pending)
        decided = pending | {'id': 'lost-decided', 'state': 'decided', 'response': {'decision': 'accept'}}
        service.store.put(owner, 'interaction', decided['id'], decided)
        service.close()
        recovered = self.make_service()
        self.assertEqual(recovered.store.get(owner, 'interaction', decided['id'])['state'], 'stale')
        self.assertEqual(recovered.store.get(owner, 'interaction', pending['id'])['state'], 'stale')
        with self.assertRaises(ValueError):
            recovered.decide(owner, pending['id'], 'accept')

    def test_structured_input_edit_submit_and_cancel(self):
        import threading
        service = self.make_service()
        self.assertTrue(hasattr(service, 'answer'), 'Structured input mediation missing')
        owner = 'input-owner'
        proposal = service.propose(owner, brief(self.project))
        service.authorize(owner, proposal['id'])
        task = service.submit(owner, proposal['id'])
        running = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'running' else None))
        attempt = running['attempts'][-1]
        for item in ('submit', 'cancel'):
            params = {'threadId': attempt['thread'], 'turnId': attempt['turn'], 'itemId': item,
                      'questions': [{'id': 'one', 'header': 'Choice', 'question': 'Which?', 'options': [{'label': 'A', 'description': 'first'}], 'isOther': True, 'isSecret': False},
                                    {'id': 'two', 'header': 'Text', 'question': 'Why?', 'options': None, 'isOther': False, 'isSecret': False}]}
            result = []
            caller = threading.Thread(target=lambda: result.append(service.clients[task['id']].handler('item/tool/requestUserInput', params)))
            caller.start()
            request = self.wait(lambda: next((r for r in service.store.list(owner, 'interaction') if r['item'] == item), None))
            if item == 'cancel':
                service.answer(owner, request['id'], 'cancel')
            else:
                with self.assertRaises(PermissionError):
                    service.answer('other', request['id'], 'edit', question='one', answers=['A'])
                with self.assertRaises(ValueError):
                    service.answer(owner, request['id'], 'submit')
                service.answer(owner, request['id'], 'edit', question='one', answers=['A'])
                service.answer(owner, request['id'], 'edit', question='one', answers=['Other text'])
                service.answer(owner, request['id'], 'edit', question='two', answers=['Reason'])
                service.answer(owner, request['id'], 'submit')
            caller.join(2)
            self.assertFalse(caller.is_alive())
            expected = {} if item == 'cancel' else {'one': {'answers': ['Other text']}, 'two': {'answers': ['Reason']}}
            self.assertEqual(result, [{'answers': expected}])
            with self.assertRaises(ValueError):
                service.answer(owner, request['id'], 'submit')

    def test_durable_queue_steer_and_same_thread_continuation(self):
        service = self.make_service()
        self.assertTrue(hasattr(service, 'followup'), 'Durable follow-up workflow missing')
        owner = 'queue-owner'
        proposal = service.propose(owner, brief(self.project))
        service.authorize(owner, proposal['id'])
        task = service.submit(owner, proposal['id'])
        running = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'running' else None))
        attempt = running['attempts'][-1]
        first = service.followup(owner, task['id'], 'one', 'Inspect first')
        second = service.followup(owner, task['id'], 'two', 'Inspect second')
        third = service.followup(owner, task['id'], 'three', 'Inspect third')
        self.assertEqual(service.followup(owner, task['id'], 'three', 'Inspect third')['id'], third['id'])
        with self.assertRaises(ValueError):
            service.followup(owner, task['id'], 'three', 'Different')
        service.queue_action(owner, task['id'], 'cancel-next')
        service.queue_action(owner, task['id'], 'steer', second['id'])
        client = service.clients[task['id']]
        self.wait(lambda: any(m == 'turn/steer' for m, p in client.calls))
        self.assertEqual([e['id'] for e in service.queue_items(owner, task['id'])], [third['id']])
        client.events.put({'method': 'turn/completed', 'params': {'threadId': attempt['thread'], 'turn': {'id': attempt['turn'], 'status': 'completed', 'items': []}}})
        continued = self.wait(lambda: (t if len((t := service.status(owner, task['id']))['attempts']) == 2 and t['state'] == 'running' else None))
        self.assertEqual(continued['attempts'][-1]['thread'], attempt['thread'])
        resumed_client = service.clients[task['id']]
        self.assertIn(('thread/resume', {'threadId': attempt['thread'], 'cwd': str(self.project), 'sandbox': 'read-only', 'approvalPolicy': 'on-request'}), resumed_client.calls)
        self.assertEqual(service.queue_items(owner, task['id']), [])
        self.assertEqual(service.store.get(owner, 'queue', second['id'])['state'], 'steered')
        self.assertEqual(service.store.get(owner, 'queue', third['id'])['state'], 'submitted')

    def test_provider_routes_dispatch_to_immutable_isolated_home(self):
        service = self.make_service()
        from hermes_codex.state import Conflict
        homes = []
        class ProfileTransport(Transport):
            def __init__(self, home, **kw):
                homes.append(home)
                super().__init__(home, **kw)
            def call(self, method, params):
                result = super().call(method, params)
                if method in {'thread/start', 'thread/resume'}:
                    result['modelProvider'] = params.get('modelProvider')
                return result
        service.factory = ProfileTransport
        service.profiles['alternate'] = {'modelProvider': 'openai', 'defaults': {'model': 'test-model'}}
        service.select_provider('routed', 'alternate')
        proposal = service.propose('routed', brief(self.project))
        service.authorize('routed', proposal['id'])
        task = service.submit('routed', proposal['id'])
        running = self.wait(lambda: (t if (t := service.status('routed', task['id']))['state'] == 'running' else None))
        self.assertEqual(service.task_provider('routed', task['id']), 'alternate')
        self.assertIn('provider-', str(homes[0]))
        client = service.clients[task['id']]
        self.assertEqual(next(p for m, p in client.calls if m == 'thread/start')['modelProvider'], 'openai')
        with self.assertRaises(Conflict):
            service.select_provider('routed', 'default')
        attempt = running['attempts'][-1]
        client.events.put({'method': 'turn/completed', 'params': {'threadId': attempt['thread'], 'turn': {'id': attempt['turn'], 'status': 'completed', 'items': []}}})
        self.wait(lambda: service.status('routed', task['id'])['state'] == 'completed')
        service.select_provider('routed', 'default')
        self.assertEqual(service.worker_home('routed', task['id']), homes[0])
        with self.assertRaises(Conflict):
            service.followup('routed', task['id'], 'wrong-profile', 'Continue')

    def test_native_review_targets_delivery_and_manager_separation(self):
        service = self.make_service()
        self.assertTrue(hasattr(service, 'native_review'), 'Native review flow missing')
        owner = 'reviewer'
        targets = [{'type': 'uncommittedChanges'}, {'type': 'baseBranch', 'branch': 'main'}, {'type': 'commit', 'sha': 'abc123', 'title': 'Subject'}, {'type': 'custom', 'instructions': 'Check security'}]
        for i, target in enumerate(targets):
            for delivery in ('inline', 'detached'):
                task = service.store.submit(owner, str(self.project), f'{i}-{delivery}', brief(self.project, f'{i}-{delivery}'), False)
                a = service.store.start(owner, task['id'])
                service.store.bind(owner, task['id'], a, 'thread-1', 'before')
                service.store.finish(owner, task['id'], a, 'completed', {})
                service.store.put(owner, 'binding', task['id'], {'task': task['id'], 'thread': 'thread-1', 'effective': {'model': 'test-model'}})
                service.native_review(owner, task['id'], target, delivery)
                running = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'running' else None))
                client = service.clients[task['id']]
                self.assertIn(('review/start', {'threadId': 'thread-1', 'target': target, 'delivery': delivery}), client.calls)
                expected = 'review-detached' if delivery == 'detached' else 'thread-1'
                self.assertEqual(running['attempts'][-1]['thread'], expected)
                client.events.put({'method': 'turn/completed', 'params': {'threadId': expected, 'turn': {'id': 'review-turn', 'status': 'completed', 'items': []}}})
                done = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'completed' else None))
                self.assertEqual(done['review_state'], 'pending_review')
        with self.assertRaises(ValueError):
            service.native_review(owner, task['id'], {'type': 'commit', 'sha': ''}, 'inline')
        with self.assertRaises(PermissionError):
            service.native_review('other', task['id'], targets[0], 'inline')

    def test_plan_mode_reaches_runtime_and_confirmation_is_revision_bound(self):
        service = self.make_service()
        self.assertTrue(hasattr(service, 'plan_action'), 'Guided plan lifecycle missing')
        owner = 'planner'
        service.store.put(owner, 'settings', 'current', {'provider': 'default', 'auto_queue': True, 'mode': 'plan', 'locale': 'en', 'plan_confirmation': True})
        proposal = service.propose(owner, brief(self.project))
        service.authorize(owner, proposal['id'])
        task = service.submit(owner, proposal['id'])
        running = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'running' else None))
        client = service.clients[task['id']]
        params = next(p for m, p in client.calls if m == 'turn/start')
        self.assertEqual(params['collaborationMode']['mode'], 'plan')
        self.assertEqual(params['sandboxPolicy']['type'], 'readOnly')
        attempt = running['attempts'][-1]
        client.events.put({'method': 'turn/completed', 'params': {'threadId': attempt['thread'], 'turn': {'id': attempt['turn'], 'status': 'completed', 'items': [{'type': 'plan', 'id': 'plan-item', 'text': 'Inspect files then report'}]}}})
        plans = self.wait(lambda: service.store.list(owner, 'plan'))
        plan = plans[-1]
        self.wait(lambda: service.status(owner, task['id'])['state'] == 'completed')
        self.assertEqual(len(service.status(owner, task['id'])['attempts']), 1)
        with self.assertRaises(PermissionError):
            service.plan_action('wrong', plan['id'], 'confirm')
        with self.assertRaises(ValueError):
            service.followup(owner, task['id'], 'bypass', 'execute anyway')
        decision = service.plan_action(owner, plan['id'], 'confirm')
        self.assertEqual(decision['state'], 'confirmed')
        continued = self.wait(lambda: (t if len((t := service.status(owner, task['id']))['attempts']) == 2 and t['state'] == 'running' else None))
        next_client = service.clients[task['id']]
        params = next(p for m, p in next_client.calls if m == 'turn/start')
        self.assertEqual(params['collaborationMode']['mode'], 'default')
        with self.assertRaises(ValueError):
            service.plan_action(owner, plan['id'], 'confirm')
        self.assertEqual(continued['review_state'], 'not_ready')

    def test_scoped_nonblocking_dispatch_completion_review_and_recovery(self):
        service = self.make_service()
        owner = 'trusted-scope'
        proposal = service.propose(owner, brief(self.project))
        with self.assertRaises(PermissionError):
            service.submit(owner, proposal['id'])
        service.authorize(owner, proposal['id'])  # trusted human command, NOT model tool
        start = time.monotonic()
        task = service.submit(owner, proposal['id'])
        self.assertLess(time.monotonic() - start, .5)
        self.assertEqual(service.submit(owner, proposal['id'])['id'], task['id'])
        running = self.wait(lambda: (t if (t := service.store.task(owner, task['id']))['state'] == 'running' else None))
        with self.assertRaises(PermissionError):
            service.status('another-scope', task['id'])
        attempt = running['attempts'][-1]
        client = service.clients[task['id']]
        client.events.put({'method': 'item/agentMessage/delta', 'params': {'threadId': 'wrong', 'turnId': attempt['turn'], 'delta': 'IGNORE'}})
        client.events.put({'method': 'item/agentMessage/delta', 'params': {'threadId': attempt['thread'], 'turnId': attempt['turn'], 'delta': 'report'}})
        client.events.put({'method': 'turn/completed', 'params': {'threadId': attempt['thread'], 'turn': {'id': attempt['turn'], 'status': 'completed', 'items': []}}})
        done = self.wait(lambda: (t if (t := service.status(owner, task['id']))['state'] == 'completed' else None))
        self.assertEqual(done['review_state'], 'pending_review')
        events = service.events(owner, task['id'])
        self.assertNotIn('IGNORE', str(events))
        self.assertIn('report', str(events))
        service.store.review(owner, task['id'], 'accepted', [{'criterion': 'report', 'status': 'passed', 'evidence': 'manager inspected output'}])
        self.assertEqual(service.status(owner, task['id'])['review_state'], 'accepted')
        self.assertEqual(service.result(owner, task['id'])['evidence_source'], 'worker-reported')
