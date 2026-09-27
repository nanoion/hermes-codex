"""Regression tests for slash-first ownership and simple Codex UX."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_codex import plugin
from hermes_codex.accounts import account_home
from hermes_codex.controls import Controls
from hermes_codex.service import WorkflowStore
from hermes_codex.state import Conflict, Denied


class RouteCacheTests(unittest.TestCase):
    def test_empty_slash_route_does_not_erase_established_delivery(self):
        captured = object()
        routes = {'owner': ('route-1', captured)}
        plugin._remember_route(routes, 'owner', '', object())
        self.assertEqual(routes['owner'], ('route-1', captured))
        plugin._remember_route(routes, 'owner', 'route-2', None)
        self.assertEqual(routes['owner'], ('route-2', None))


class WorkflowOwnerTests(unittest.TestCase):
    def test_owner_presence_covers_tasks_documents_events_and_reviews(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch('hermes_codex.state.writer_directory', return_value=root / 'writers'):
                store = WorkflowStore(root / 'state.sqlite3')
                try:
                    self.assertFalse(store.has_owner('owner'))
                    store.put('legacy', 'proposal', 'p1', {'id': 'p1'})
                    store.migrate_owner('legacy', 'canonical')
                    self.assertFalse(store.has_owner('legacy'))
                    self.assertTrue(store.has_owner('canonical'))
                    self.assertEqual(store.get('canonical', 'proposal', 'p1')['id'], 'p1')
                    self.assertEqual(plugin._select_owner(store, 'canonical', 'legacy'), 'canonical')
                    self.assertFalse(store.has_owner('other'))

                    account = {'id': 'account-1', 'provider': 'default', 'state': 'ready'}
                    store.put('legacy-2', 'account', account['id'], account)
                    task = store.submit('legacy-2', str(root), 'legacy-task',
                                        {'workspace': str(root), 'write_roots': []}, False)
                    store.put('legacy-2', 'route', task['id'], {'provider': 'default', 'account': None})
                    service = SimpleNamespace(home=root / 'state', store=store,
                                              host_account_homes={}, task_provider=lambda owner, task_id: 'default')
                    old_account_home = account_home(service, 'legacy-2', account['id'])
                    old_worker_home = Controls.worker_home(service, 'legacy-2', task['id'])
                    store.migrate_owner('legacy-2', 'canonical-2')
                    self.assertEqual(account_home(service, 'canonical-2', account['id']), old_account_home)
                    self.assertEqual(Controls.worker_home(service, 'canonical-2', task['id']), old_worker_home)
                finally:
                    store.close()


class SlashFirstOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.fields = {
            'HERMES_SESSION_PLATFORM': 'telegram',
            'HERMES_SESSION_SOURCE': 'telegram',
            'HERMES_SESSION_CHAT_TYPE': 'dm',
            'HERMES_SESSION_CHAT_ID': 'chat-1',
            'HERMES_SESSION_USER_ID': 'user-1',
            'HERMES_SESSION_PROFILE': 'code-agent',
        }
        fake = SimpleNamespace(get_session_env=lambda name, default='': self.fields.get(name, default))
        self.modules = patch.dict(sys.modules, {'gateway.session_context': fake})
        self.home = patch.object(plugin, 'profile_home', return_value=Path('/profiles/code-agent'))
        self.modules.start()
        self.home.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.home.stop)

    def test_slash_first_and_later_tool_share_messaging_owner(self):
        slash_owner, slash_route = plugin.current_scope()
        self.assertEqual(slash_route, '')

        self.fields.update(HERMES_SESSION_ID='session-1', HERMES_SESSION_KEY='route-1')
        tool_owner, tool_route = plugin.current_scope()

        self.assertEqual(tool_owner, slash_owner)
        self.assertEqual(tool_route, 'route-1')

    def test_messaging_owner_isolated_by_trusted_route_identity(self):
        owner, _ = plugin.current_scope()
        for field, other in (
            ('HERMES_SESSION_USER_ID', 'user-2'),
            ('HERMES_SESSION_CHAT_ID', 'chat-2'),
            ('HERMES_SESSION_THREAD_ID', 'thread-2'),
            ('HERMES_SESSION_PROFILE', 'other-profile'),
            ('HERMES_SESSION_PLATFORM', 'discord'),
        ):
            previous = self.fields.get(field)
            self.fields[field] = other
            self.assertNotEqual(plugin.current_scope()[0], owner, field)
            if previous is None:
                self.fields.pop(field)
            else:
                self.fields[field] = previous

    def test_source_only_messaging_contexts_do_not_collide(self):
        self.fields['HERMES_SESSION_PLATFORM'] = ''
        self.fields['HERMES_SESSION_SOURCE'] = 'telegram'
        telegram, _ = plugin.current_scope()
        self.fields['HERMES_SESSION_SOURCE'] = 'discord'
        discord, _ = plugin.current_scope()
        self.assertNotEqual(telegram, discord)

    def test_messaging_without_trusted_user_and_local_without_session_fail_closed(self):
        self.fields['HERMES_SESSION_USER_ID'] = ''
        with self.assertRaises(Denied):
            plugin.current_scope()

        self.fields['HERMES_SESSION_USER_ID'] = 'user-a'
        self.fields['HERMES_SESSION_CHAT_ID'] = ''
        self.fields['HERMES_SESSION_SCOPE_ID'] = ''
        with self.assertRaises(Denied):
            plugin.current_scope()

        self.fields.update(
            HERMES_SESSION_PLATFORM='local', HERMES_SESSION_SOURCE='cli',
            HERMES_SESSION_CHAT_ID='', HERMES_SESSION_USER_ID='',
        )
        with self.assertRaises(Denied):
            plugin.current_scope()


TASK_ID = '11111111-1111-4111-8111-111111111111'
PAGE_TOKEN = '44444444-4444-4444-8444-444444444444'


class EasyContext:
    def __init__(self):
        self.tools = {}
        self.commands = {}
        self.sections = {}
        self.cleanups = []
        self._manager = SimpleNamespace(_cli_ref=None)

    def get_config(self, key, default=None):
        return default

    def register_tool(self, **kwargs):
        self.tools[kwargs['name']] = kwargs

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def register_system_prompt_section(self, id, content, **kwargs):
        self.sections[id] = content

    def on_unload(self, handler):
        self.cleanups.append(handler)

    def inject_message(self, *args, **kwargs):
        return False


class FakeStore:
    def __init__(self):
        self.documents = {'proposal': [], 'interaction': []}
        self.task_rows = []

    def has_owner(self, owner):
        return False

    def list(self, owner, kind):
        return list(self.documents.get(kind, []))

    def tasks(self, owner):
        return list(self.task_rows)


class FakeService:
    def __init__(self):
        self.store = FakeStore()
        self.calls = []

    def close(self):
        pass

    def settings(self, owner):
        return {'locale': 'en'}

    def authorize(self, owner, proposal):
        self.calls.append(('authorize', owner, proposal))
        return {'id': proposal, 'authorized': True}

    def propose(self, owner, brief):
        self.calls.append(('propose', owner, brief))
        return {'id': 'proposal-1', 'brief': brief, 'authorized': False}

    def cancel_proposal(self, owner, proposal):
        self.calls.append(('proposal-cancel', owner, proposal))
        return {'id': proposal, 'authorized': False, 'cancelled': True}

    def submit(self, owner, proposal):
        self.calls.append(('submit', owner, proposal))
        return {'id': TASK_ID, 'state': 'running'}

    def status(self, owner, task):
        self.calls.append(('status', owner, task))
        return {'id': task, 'state': 'running'}

    def followup(self, owner, task, key, text):
        self.calls.append(('followup', owner, task, key, text))
        return {'id': 'queue-1', 'task': task, 'state': 'queued'}

    def cancel(self, owner, task, reason):
        self.calls.append(('cancel', owner, task, reason))
        return {'id': task, 'state': 'cancelling'}

    def events(self, owner, task, cursor):
        self.calls.append(('events', owner, task, cursor))
        return [{'cursor': cursor + 1, 'type': 'turn/completed', 'payload': {'message': 'done'}}]

    def present(self, owner, kind, task, page=None):
        self.calls.append(('present', owner, kind, task, page))
        if kind == 'proposal':
            record = next(item for item in self.store.documents[kind] if item['id'] == task)
            return {'text': json.dumps(record), 'next': None}
        return {'text': f'result for {task}', 'next': PAGE_TOKEN if page is None else None}

    def result_page(self, owner, task, attempt=None, cursor=0):
        self.calls.append(('result_page', owner, task, cursor))
        return {'task': task, 'items': [], 'next_cursor': None}

    def decide(self, owner, request, decision):
        self.calls.append(('decide', owner, request, decision))
        return {'id': request, 'state': decision}

    def control(self, owner, action, payload):
        self.calls.append(('control', owner, action, payload))
        return {'id': 'control-1', 'action': action, 'state': 'pending'}

    def native_review(self, owner, task, target, delivery):
        self.calls.append(('native-review', owner, task, target, delivery))
        return {'task': task, 'target': target}

    def plan_action(self, owner, key, action, revision=None):
        self.calls.append(('plan', owner, key, action, revision))
        return {'id': key, 'state': action}

    def queue_action(self, owner, task, action, key=None):
        self.calls.append(('queue', owner, task, action, key))
        return {'task': task, 'state': action}

    def answer(self, owner, key, action, question=None, answers=None):
        self.calls.append(('input', owner, key, action, question, answers))
        return {'id': key, 'state': action}


class EasyCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = EasyContext()
        self.service = FakeService()
        fields = {
            'HERMES_SESSION_PLATFORM': 'telegram', 'HERMES_SESSION_SOURCE': 'telegram',
            'HERMES_SESSION_CHAT_TYPE': 'dm', 'HERMES_SESSION_CHAT_ID': 'chat-1',
            'HERMES_SESSION_USER_ID': 'user-1', 'HERMES_SESSION_PROFILE': 'code-agent',
        }
        fake = SimpleNamespace(get_session_env=lambda name, default='': fields.get(name, default))
        stack = [
            patch.dict(sys.modules, {'gateway.session_context': fake}),
            patch.object(plugin, 'profile_home', return_value=self.root),
            patch.object(plugin, 'Service', return_value=self.service),
        ]
        for item in stack:
            item.start()
            self.addCleanup(item.stop)
        plugin.register(self.ctx)
        self.addCleanup(lambda: [close() for close in self.ctx.cleanups])

    def test_registers_natural_chat_guidance_and_simple_command(self):
        self.assertIn('codex.workflow', self.ctx.sections)
        guidance = self.ctx.sections['codex.workflow']
        self.assertIn('codex', guidance.lower())
        self.assertIn('user-control', guidance)
        self.assertIn('clarify', guidance)
        self.assertIn('Approve and start', guidance)
        self.assertIn('View details', guidance)
        self.assertIn('pending_interactions', guidance)
        self.assertIn('pending_plans', guidance)
        self.assertIn('free-text', guidance)
        self.assertIn('native-follow', guidance)
        self.assertIn('Codex session/thread ID', guidance)
        self.assertNotIn('never authorize', guidance.lower())
        self.assertIn('codex', self.ctx.commands)

    def test_model_can_discover_current_work_without_user_ids(self):
        self.service.store.task_rows = [{'id': TASK_ID, 'state': 'running', 'created': 1}]
        result = json.loads(self.ctx.tools['codex']['handler']({'action': 'list'}))
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['tasks'][0]['id'], TASK_ID)

    def test_help_advertises_model_access_to_all_user_controls(self):
        result = json.loads(self.ctx.tools['codex']['handler']({'action': 'help'}))
        self.assertTrue(result['success'], result)
        self.assertEqual(set(result['data']['model_user_control_operations']), {
            'show', 'native-review', 'plan', 'control', 'followup',
            'queue', 'input', 'proposal', 'authorize', 'decide',
        })
        self.assertEqual(result['data']['user_control_envelope']['payload']['operation'], '<operation>')
        self.assertEqual(result['data']['user_controls']['authorize']['required'], ['id'])
        self.assertEqual(result['data']['examples']['input-edit']['payload']['arguments']['action'], 'edit')
        self.assertIn('authorize a proposal before submit', result['data']['sequencing'])
        self.assertEqual(result['data']['user_controls']['native-review'], {
            'required': ['delivery', 'target', 'task'], 'optional': []})
        self.assertEqual(result['data']['user_controls']['followup'], {
            'required': ['key', 'task', 'text'], 'optional': []})
        self.assertEqual(result['data']['user_controls']['queue'], {
            'required': ['action', 'task'], 'optional': ['id']})
        description = self.ctx.tools['codex']['schema']['parameters']['properties']['payload']['description']
        self.assertIn('{"operation":"authorize","arguments":{"id":"<proposal-id>"}}', description)

    def test_list_advertises_native_permission_plan_and_bounded_input_choices(self):
        self.service.store.documents['proposal'] = [{
            'id': 'proposal-pending', 'authorized': False,
            'brief': {'workspace': '/projects/hermes-codex', 'sandbox': 'workspace-write'},
        }]
        self.service.store.documents['interaction'] = [
            {'id': 'approval-1', 'task': TASK_ID, 'state': 'pending',
             'method': 'item/commandExecution/requestApproval',
             'details': {'command': 'touch safe.txt', 'cwd': '/projects/hermes-codex'}},
            {'id': 'input-1', 'task': TASK_ID, 'state': 'pending',
             'method': 'item/tool/requestUserInput', 'details': {'questions': [{
                 'id': 'q1', 'header': 'Mode', 'question': 'Choose mode',
                 'options': [{'label': 'Safe'}, {'label': 'Fast'}],
             }]}},
        ]
        self.service.store.documents['plan'] = [
            {'id': 'plan-1', 'task': TASK_ID, 'state': 'pending', 'text': 'Run tests'},
        ]
        result = json.loads(self.ctx.tools['codex']['handler']({'action': 'list'}))
        self.assertTrue(result['success'], result)
        proposal = result['data']['pending_proposals'][0]['decision_request']
        self.assertEqual(proposal['choices'][0], 'Approve and start')
        self.assertEqual(proposal['resolve']['Approve and start'][0]['payload']['arguments']['id'],
                         'proposal-pending')
        approval = result['data']['pending_interactions'][0]['decision_request']
        self.assertEqual(approval['choices'], ['Accept once', 'Accept for session', 'Decline'])
        self.assertIn('touch safe.txt', approval['question'])
        self.assertIn('/projects/hermes-codex', approval['question'])
        self.assertEqual(approval['resolve']['Decline']['payload']['arguments']['decision'], 'decline')

        self.service.store.documents['interaction'][0]['details']['command'] = (
            r'curl --password alpha,beta --secret foo\ bar --api-key=hidden-token https://example.invalid')
        redacted = json.loads(self.ctx.tools['codex']['handler']({'action': 'list'}))[
            'data']['pending_interactions'][0]['decision_request']['question']
        for secret in ('alpha', 'beta', 'foo', 'bar', 'hidden-token'):
            self.assertNotIn(secret, redacted)
        self.assertIn('[redacted]', redacted)
        bounded = result['data']['pending_interactions'][1]['decision_request']
        self.assertEqual(bounded['questions'][0]['choices'], ['Safe', 'Fast'])
        self.assertEqual(bounded['questions'][0]['resolve']['Safe']['payload']['arguments'], {
            'id': 'input-1', 'action': 'edit', 'question': 'q1', 'answers': ['Safe'],
        })
        self.assertEqual(bounded['after_all']['payload']['arguments'], {
            'id': 'input-1', 'action': 'submit',
        })
        plan = result['data']['pending_plans'][0]['decision_request']
        self.assertEqual(plan['choices'], ['Confirm plan', 'Revise plan', 'Cancel plan'])
        self.assertEqual(plan['resolve']['Confirm plan']['payload']['operation'], 'plan')

    def test_truncated_permission_requires_full_details_before_confirmation(self):
        self.service.store.documents['interaction'] = [{
            'id': 'approval-long', 'task': TASK_ID, 'state': 'pending',
            'method': 'item/commandExecution/requestApproval',
            'details': {'command': 'printf ' + ('x' * 1600), 'cwd': '/important/scope'},
        }]
        decision = json.loads(self.ctx.tools['codex']['handler']({'action': 'list'}))[
            'data']['pending_interactions'][0]['decision_request']
        self.assertEqual(decision['choices'], ['View full details', 'Decline'])
        self.assertNotIn('Accept once', decision['choices'])
        inspect = decision['resolve']['View full details']
        self.assertEqual(inspect['start']['payload']['operation'], 'show')
        self.assertEqual(inspect['while_next']['payload']['arguments']['page'], '<next>')
        self.assertEqual(inspect['only_after_next_is_null']['choices'], [
            'Accept once', 'Accept for session', 'Decline'])
        self.assertNotIn('after', inspect)

    def test_real_other_label_never_collides_with_custom_answer_choice(self):
        self.service.store.documents['interaction'] = [{
            'id': 'input-other', 'task': TASK_ID, 'state': 'pending',
            'method': 'item/tool/requestUserInput', 'details': {'questions': [{
                'id': 'q', 'question': 'Choose', 'isOther': True,
                'options': [{'label': 'Other…'}, {'label': 'Safe'}],
            }]},
        }]
        question = json.loads(self.ctx.tools['codex']['handler']({'action': 'interactions'}))[
            'data'][0]['decision_request']['questions'][0]
        self.assertEqual(question['mode'], 'text')
        self.assertNotIn('choices', question)
        self.assertIn('Other…, Safe', question['question'])

    def test_mixed_input_preserves_per_question_modes_other_and_large_choice_fallback(self):
        self.service.store.documents['interaction'] = [{
            'id': 'input-mixed', 'task': TASK_ID, 'state': 'pending',
            'method': 'item/tool/requestUserInput', 'details': {'questions': [
                {'id': 'bounded', 'question': 'Mode?',
                 'options': [{'label': 'Safe'}, {'label': 'Fast'}], 'isOther': True},
                {'id': 'many', 'question': 'Region?',
                 'options': [{'label': value} for value in ['A', 'B', 'C', 'D', 'E']]},
                {'id': 'text', 'question': 'Why?', 'options': None},
            ]},
        }]
        result = json.loads(self.ctx.tools['codex']['handler']({'action': 'interactions'}))
        questions = result['data'][0]['decision_request']['questions']
        self.assertEqual(questions[0]['mode'], 'choices')
        self.assertEqual(questions[0]['choices'], ['Safe', 'Fast', 'Other…'])
        self.assertTrue(questions[0]['resolve']['Other…']['prompt_for_text'])
        self.assertEqual(questions[1]['mode'], 'text')
        self.assertIn('A, B, C, D, E', questions[1]['question'])
        self.assertEqual(questions[2]['mode'], 'text')

    def test_proposal_details_are_owner_scoped_and_available_from_discovery(self):
        brief = {'workspace': '/projects/hermes-codex', 'sandbox': 'workspace-write',
                 'assignment': 'Implement popup', 'constraints': ['No deploy']}
        self.service.store.documents['proposal'] = [
            {'id': 'proposal-details', 'authorized': False, 'brief': brief}]
        listing = json.loads(self.ctx.tools['codex']['handler']({'action': 'list'}))['data']
        details_call = listing['pending_proposals'][0]['decision_request']['resolve']['View details']
        self.assertEqual(details_call['action'], 'user-control')
        self.assertEqual(details_call['payload'], {
            'operation': 'show',
            'arguments': {'kind': 'proposal', 'id': 'proposal-details'},
        })
        shown = json.loads(self.ctx.tools['codex']['handler'](details_call))
        self.assertTrue(shown['success'], shown)
        self.assertIn('Implement popup', shown['data']['text'])
        self.assertIn('No deploy', shown['data']['text'])

    def test_interactions_action_preserves_resolved_records_without_live_choices(self):
        self.service.store.documents['interaction'] = [
            {'id': 'done', 'task': TASK_ID, 'state': 'consumed',
             'method': 'item/commandExecution/requestApproval', 'details': {'command': 'true'}},
        ]
        result = json.loads(self.ctx.tools['codex']['handler']({'action': 'interactions'}))
        self.assertEqual(result['data'][0]['id'], 'done')
        self.assertNotIn('decision_request', result['data'][0])

    def test_free_text_input_does_not_fake_bounded_choices(self):
        self.service.store.documents['interaction'] = [{
            'id': 'input-2', 'task': TASK_ID, 'state': 'pending',
            'method': 'item/tool/requestUserInput', 'details': {'questions': [{
                'id': 'q2', 'header': 'Reason', 'question': 'Explain why',
            }]},
        }]
        result = json.loads(self.ctx.tools['codex']['handler']({'action': 'interactions'}))
        request = result['data'][0]['decision_request']
        self.assertEqual(request['mode'], 'questions')
        self.assertEqual(request['questions'][0]['mode'], 'text')
        self.assertNotIn('choices', request['questions'][0])

    def test_proposal_requests_native_action_first_confirmation(self):
        result = json.loads(self.ctx.tools['codex']['handler']({
            'action': 'propose',
            'payload': {'workspace': '/projects/hermes-codex', 'sandbox': 'workspace-write'},
        }))
        self.assertTrue(result['success'], result)
        request = result['data']['decision_request']
        self.assertEqual(request['tool'], 'clarify')
        self.assertEqual(request['choices'], [
            'Approve and start', 'Cancel', 'View details',
        ])
        self.assertEqual(request['resolve']['Approve and start'], [
            {'action': 'user-control', 'payload': {
                'operation': 'authorize', 'arguments': {'id': 'proposal-1'}}},
            {'action': 'submit', 'id': 'proposal-1'},
        ])
        self.assertEqual(request['resolve']['Cancel'], [
            {'action': 'user-control', 'payload': {
                'operation': 'proposal', 'arguments': {
                    'id': 'proposal-1', 'action': 'cancel'}}},
        ])
        self.assertNotIn('owner', json.dumps(request).lower())

    def test_model_can_open_task_without_printing_direct_user_command(self):
        result = json.loads(self.ctx.tools['codex']['handler']({
            'action': 'user-control',
            'payload': {'operation': 'control', 'arguments': {
                'action': 'open', 'payload': {'task': TASK_ID},
            }},
        }))
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['action'], 'open')
        self.assertEqual(self.service.calls[-1][0], 'control')
        self.assertEqual(self.service.calls[-1][2:], ('open', {'task': TASK_ID}))

    def test_model_can_follow_up_without_printing_direct_user_command(self):
        result = json.loads(self.ctx.tools['codex']['handler']({
            'action': 'user-control',
            'payload': {'operation': 'followup', 'arguments': {
                'task': TASK_ID, 'key': 'continue-1', 'text': 'Continue integration tests',
            }},
        }))
        self.assertTrue(result['success'], result)
        self.assertEqual(result['data']['state'], 'queued')
        self.assertEqual(self.service.calls[-1][0], 'followup')
        self.assertEqual(self.service.calls[-1][2:],
                         (TASK_ID, 'continue-1', 'Continue integration tests'))

    def test_model_can_invoke_every_direct_user_operation(self):
        cases = {
            'show': {'kind': 'result', 'id': TASK_ID},
            'native-review': {'task': TASK_ID, 'target': {'type': 'uncommittedChanges'}, 'delivery': 'inline'},
            'plan': {'id': 'plan-1', 'action': 'confirm'},
            'queue': {'task': TASK_ID, 'action': 'clear'},
            'input': {'id': 'input-1', 'action': 'edit', 'question': 'choice', 'answers': ['yes']},
            'proposal': {'id': 'proposal-1', 'action': 'cancel'},
            'authorize': {'id': 'proposal-1'},
            'decide': {'id': 'request-1', 'decision': 'accept'},
        }
        for operation, arguments in cases.items():
            with self.subTest(operation=operation):
                result = json.loads(self.ctx.tools['codex']['handler']({
                    'action': 'user-control',
                    'payload': {'operation': operation, 'arguments': arguments},
                }))
                self.assertTrue(result['success'], result)

    def test_model_user_control_rejects_unknown_extra_and_forged_owner(self):
        tool = self.ctx.tools['codex']['handler']
        cases = [
            {'action': 'user-control', 'payload': {'operation': 'unknown', 'arguments': {}}},
            {'action': 'user-control', 'payload': {'operation': 'authorize', 'arguments': {'id': 'p', 'extra': True}}},
            {'action': 'user-control', 'owner': 'forged', 'payload': {'operation': 'authorize', 'arguments': {'id': 'p'}}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertFalse(json.loads(tool(payload))['success'])

    def test_approve_without_id_starts_the_only_pending_proposal(self):
        self.service.store.documents['proposal'] = [
            {'id': 'proposal-1', 'authorized': False},
        ]
        text = self.ctx.commands['codex']('approve')
        self.assertIn(TASK_ID, text)
        self.assertEqual([call[0] for call in self.service.calls], ['authorize', 'submit'])

    def test_approve_never_guesses_between_proposals(self):
        self.service.store.documents['proposal'] = [
            {'id': 'proposal-1', 'authorized': False},
            {'id': 'proposal-2', 'authorized': False},
        ]
        text = self.ctx.commands['codex']('approve')
        self.assertIn('proposal-1', text)
        self.assertIn('proposal-2', text)
        self.assertEqual(self.service.calls, [])

    def test_status_continue_result_and_cancel_default_to_latest_task(self):
        self.service.store.task_rows = [{'id': TASK_ID, 'state': 'running', 'created': 1}]
        self.assertIn('running', self.ctx.commands['codex']('status'))
        self.assertIn('queued', self.ctx.commands['codex']('continue inspect tests'))
        self.assertIn(TASK_ID, self.ctx.commands['codex']('result'))
        self.assertIn('cancelling', self.ctx.commands['codex']('cancel'))
        self.assertEqual([call[0] for call in self.service.calls],
                         ['status', 'followup', 'present', 'cancel'])

    def test_mutations_require_task_choice_when_multiple_are_eligible(self):
        other = '22222222-2222-4222-8222-222222222222'
        self.service.store.task_rows = [
            {'id': TASK_ID, 'state': 'running', 'created': 2},
            {'id': other, 'state': 'running', 'created': 1},
        ]
        continued = self.ctx.commands['codex']('continue inspect tests')
        cancelled = self.ctx.commands['codex']('cancel')
        self.assertIn(TASK_ID, continued)
        self.assertIn(other, continued)
        self.assertIn(TASK_ID, cancelled)
        self.assertEqual(self.service.calls, [])

    def test_unknown_explicit_task_id_never_becomes_instruction_text(self):
        unknown = '33333333-3333-4333-8333-333333333333'
        self.service.store.task_rows = [{'id': TASK_ID, 'state': 'running', 'created': 1}]
        text = self.ctx.commands['codex'](f'continue {unknown} inspect tests')
        self.assertIn('unavailable', text.lower())
        self.assertEqual(self.service.calls, [])

    def test_migration_conflict_is_rendered_instead_of_escaping(self):
        with patch.object(plugin, '_select_owner', side_effect=Conflict('migration conflict')):
            text = self.ctx.commands['codex']('status')
        self.assertIn('migration conflict', text.lower())

    def test_result_uses_opaque_presentation_page_and_events_are_visible(self):
        self.service.store.task_rows = [{'id': TASK_ID, 'state': 'running', 'created': 1}]
        first = self.ctx.commands['codex']('result')
        second = self.ctx.commands['codex'](f'result {PAGE_TOKEN}')
        events = self.ctx.commands['codex']('events')
        self.assertIn(f'Page cursor: {PAGE_TOKEN}', first)
        self.assertNotIn('Page cursor:', second)
        self.assertIn('turn/completed', events)
        self.assertIn('done', events)
        self.assertEqual([call[0] for call in self.service.calls], ['present', 'present', 'events'])


if __name__ == '__main__':
    unittest.main()
